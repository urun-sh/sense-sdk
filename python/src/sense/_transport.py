"""Transport layer: one standing bidirectional event wire per session.

Every transport carries the SAME OpenAI JSON events (media rides the event
channel as ``input_audio_buffer.append`` / ``conversation.item.create``), so
the session loop is transport-agnostic:

- ``quic``   — aioquic WebTransport: POST ``/v1/realtime/calls?transport=quic``
               -> {callId, url, token, certHashes}, then WT session with a
               msgpack hello control stream and an NDJSON events stream.
- ``webrtc`` — aiortc: POST the SDP offer to ``/v1/realtime/calls``, events on
               the ``oai-events`` data channel (byte-for-byte the WS protocol).
- ``ws``     — aiohttp websocket (the original wire, with dial backoff).

``auto`` tries QUIC (short dial: UDP may be blocked) -> WebRTC -> WS.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import ssl
import struct
import urllib.parse
from typing import Any

import aiohttp

from ._core import OutOfCreditsError

log = logging.getLogger("sense")

#: Dial budgets. QUIC must fail reasonably fast (UDP blocked => QUIC never
#: completes) but 2 s was tighter than the whole POST -> WT-ready -> hello-ack
#: dance through the console's same-origin relay (1.2-1.8 s direct,
#: 2026-11-15; the relay hop adds its own fetch), so prod fell back off a
#: rung that would have connected. WebRTC gets a longer budget for
#: ICE + DTLS + datachannel open.
TRANSPORT_DIAL_TIMEOUTS_S = {"quic": 5.0, "webrtc": 10.0}

#: Largest single inbound message (websocket frame / datachannel message)
#: the client accepts. Server events are small JSON; this only bounds memory
#: against a hostile or broken endpoint.
MAX_INBOUND_MESSAGE_BYTES = 4 * 1024 * 1024
#: Largest partial (newline-less) line buffered from a stream. A decision
#: line is a few hundred bytes.
MAX_LINE_BYTES = 256 * 1024
#: Received-but-unconsumed messages held per wire before the leg is dropped.
MAX_PENDING_MESSAGES = 4096

__all__ = [
    "MAX_INBOUND_MESSAGE_BYTES",
    "MAX_LINE_BYTES",
    "QuicWire",
    "RtcWire",
    "TransportFailed",
    "Wire",
    "WsWire",
    "check_base_url",
]

_LOOPBACK_HOSTS = {"localhost", "::1"}


def _is_loopback(host: str) -> bool:
    """True for localhost, ::1 and 127.0.0.0/8."""
    host = host.lower().rstrip(".")
    if host in _LOOPBACK_HOSTS or host.endswith(".localhost"):
        return True
    parts = host.split(".")
    # ASCII digits only: str.isdigit() also accepts "²" and Arabic-Indic digits.
    return (
        len(parts) == 4
        and parts[0] == "127"
        and all(p.isascii() and p.isdigit() and int(p) <= 255 for p in parts)
    )


def check_base_url(url: str, *, allow_insecure: bool = False) -> str:
    """Validate a service base URL and return it.

    The API key is sent as a bearer header on every dial, so plaintext
    ``http``/``ws`` is refused unless the host is loopback (local test
    servers) or ``allow_insecure`` is set explicitly."""
    p = urllib.parse.urlsplit(url)
    scheme = p.scheme.lower()
    if scheme not in ("https", "wss", "http", "ws") or not p.hostname:
        raise ValueError(f"base_url must be an absolute https:// or wss:// URL, got {url!r}")
    if p.username or p.password:
        raise ValueError("base_url must not embed credentials; pass api_key instead")
    if scheme in ("http", "ws") and not allow_insecure and not _is_loopback(p.hostname):
        raise ValueError(
            f"refusing plaintext base_url {url!r}: the API key would be sent unencrypted. "
            "Use https:// or wss://, or pass allow_insecure=True for a trusted network."
        )
    return url


def _client_ssl_context() -> ssl.SSLContext:
    """TLS client context backed by the operating system trust store
    (macOS Keychain, Windows cert store, system OpenSSL) via truststore.

    Interpreter-default CA stores are unreliable in the wild: python.org
    builds ship no CA bundle until ``Install Certificates.command`` runs,
    and MDM/corporate proxy roots live in the system store, not in any
    bundled pem. Without this, dials can fail TLS verification under a bare
    ``python -m sense.examples...`` even though the endpoint is fine.
    """
    import truststore

    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # truststore verifies against the OS store but does not load the OpenSSL
    # default paths on macOS/Windows. Load them so a private CA supplied via
    # SSL_CERT_FILE / SSL_CERT_DIR (or compiled defaults) keeps working
    # alongside OS trust; on Linux truststore already loaded them and the
    # duplicate load is harmless.
    ctx.load_default_certs()
    return ctx


class TransportFailed(RuntimeError):
    """A non-WS transport could not be established (fallback signal in auto)."""


class Wire:
    """One standing event wire: async text-message send/receive."""

    kind = "?"

    def __init__(self) -> None:
        """Initialize the closed flag and close code to unset."""
        self.closed = False
        self.close_code: int | None = None
        self.close_reason: str | None = None

    async def send(self, payload: dict) -> None:  # pragma: no cover - interface
        """Send one JSON event payload (interface, subclasses implement)."""
        raise NotImplementedError

    async def receive(self) -> str | None:
        """One text message (a JSON event or a raw decision line), or None
        when the wire closed."""
        raise NotImplementedError  # pragma: no cover - interface

    async def close(self) -> None:  # pragma: no cover - interface
        """Tear the wire down (interface, subclasses implement)."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


class WsWire(Wire):
    """WebSocket wire over an aiohttp client websocket."""

    kind = "ws"

    def __init__(self, ws: aiohttp.ClientWebSocketResponse, http: aiohttp.ClientSession) -> None:
        """Adopt a live aiohttp websocket and its owning client session."""
        super().__init__()
        self._ws = ws
        self._http = http

    async def send(self, payload: dict) -> None:
        """Send the payload as a JSON websocket message."""
        await self._ws.send_json(payload)

    async def receive(self) -> str | None:
        """Wait for one text frame; set the close code and return None on close.

        Server events may ride BINARY frames (prod lax does this: its first
        output event lands as a binary frame ~170 ms after the dial). Those
        carry UTF-8 event JSON just like text frames — the TS SDK stringifies
        every message buffer — so decode and return them. PING/PONG are
        answered by aiohttp and skipped. Only real CLOSE/CLOSED/ERROR ends
        the wire."""
        while True:
            msg = await self._ws.receive()
            if msg.type is aiohttp.WSMsgType.TEXT:
                return msg.data
            if msg.type is aiohttp.WSMsgType.BINARY:
                return msg.data.decode("utf-8", "replace")
            if msg.type in (aiohttp.WSMsgType.PING, aiohttp.WSMsgType.PONG):
                continue  # keepalive noise; aiohttp already answered the ping
            self.close_code = self._ws.close_code
            self.close_reason = msg.extra
            self.closed = True
            return None

    async def close(self) -> None:
        """Close the websocket and its client session, swallowing errors."""
        try:
            await self._ws.close()
        except Exception:
            pass
        try:
            await self._http.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# QUIC / WebTransport
# ---------------------------------------------------------------------------


# Gateway-side cap on a single control frame (mirrors the TS client).
WT_CONTROL_FRAME_MAX = 256 * 1024


def _pack_control(msg: dict) -> bytes:
    """Pack a control dict as msgpack with a 4-byte little-endian length prefix."""
    import msgpack

    body = msgpack.packb(msg, use_bin_type=True)
    return struct.pack("<I", len(body)) + body


def _unpack_control(data: bytes, rx: bytearray) -> list[dict]:
    """Buffer control-stream bytes and decode every complete framed message.

    Frames are [u32 LE length][msgpack body]; QUIC delivers them in arbitrary
    chunks, so partial and back-to-back frames must carry across calls.
    Raises ValueError when a length prefix exceeds the frame-size cap — the
    caller treats that as a poisoned stream."""
    rx.extend(data)
    msgs: list[dict] = []
    import msgpack

    while len(rx) >= 4:
        (n,) = struct.unpack_from("<I", rx, 0)
        if n > WT_CONTROL_FRAME_MAX:
            raise ValueError(f"wt control frame too large: {n} bytes")
        if len(rx) < 4 + n:
            break
        msgs.append(msgpack.unpackb(bytes(rx[4 : 4 + n]), raw=False))
        del rx[: 4 + n]
    return msgs


class _WtProtocol:
    """Mixin over aioquic's QuicConnectionProtocol implementing WebTransport:
    extended CONNECT /wt, msgpack hello control stream, NDJSON events stream.
    """

    def _wt_init(self) -> None:
        """Set up the H3 connection and per-wire state (streams, queues, events)."""
        from aioquic.h3.connection import H3Connection

        self._h3 = H3Connection(self._quic, enable_webtransport=True)
        self.session_id: int | None = None
        self.session_ready = asyncio.Event()
        self.hello_acked = asyncio.Event()
        self.control_stream: int | None = None
        self.events_stream: int | None = None
        self._rx = b""
        self._ctrl_rx = bytearray()
        self._ctrl_dead = False  # set once an oversize frame rejects the stream
        self.lines: asyncio.Queue[str | None] = asyncio.Queue()

    def quic_event_received(self, event) -> None:
        """Route QUIC events to H3; a terminated connection closes the line queue."""
        from aioquic.quic.events import ConnectionTerminated

        if isinstance(event, ConnectionTerminated):
            self.lines.put_nowait(None)
            return
        for ev in self._h3.handle_event(event):
            self.h3_event_received(ev)

    def connection_lost(self, exc) -> None:
        """Queue None on connection loss, then hand off to the base class."""
        self.lines.put_nowait(None)
        super().connection_lost(exc)

    def h3_event_received(self, ev) -> None:
        """Dispatch H3 events: session-ready headers, control data, events-stream bytes."""
        from aioquic.h3.events import HeadersReceived, WebTransportStreamDataReceived

        if isinstance(ev, HeadersReceived) and ev.stream_id == self.session_id:
            self.session_ready.set()
        elif isinstance(ev, WebTransportStreamDataReceived):
            if ev.stream_id == self.control_stream:
                self._on_control(ev.data)
            elif ev.stream_id == self.events_stream:
                self._on_events(ev.data)
            # other streams carry voice-out media; the events-based SDK ignores them

    def _on_control(self, data: bytes) -> None:
        """Dispatch control-stream messages; answer gateway pings (keepalives)."""
        if self._ctrl_dead:
            return
        try:
            msgs = _unpack_control(data, self._ctrl_rx)
        except Exception as e:
            # A malformed frame (oversize length, undecodable msgpack) poisons
            # the stream: stop processing it (later chunks must not decode or
            # pong) and end it via the queued None.
            self._poison(f"control stream poisoned: {e}")
            return
        for msg in msgs:
            if not isinstance(msg, dict):
                log.warning("[sense] wt control message not a dict: %r", msg)
                continue
            t = msg.get("t")
            if t == "hello-ack":
                self.hello_acked.set()
            elif t == "ping":
                self._send_pong(msg)

    def _send_pong(self, msg: dict) -> None:
        """Reply to a gateway keepalive ping, echoing its id and ts."""
        if self.control_stream is None:
            return
        self._quic.send_stream_data(
            self.control_stream,
            _pack_control({"t": "pong", "ts": msg.get("ts") or 0, "id": msg.get("id") or 0}),
        )
        self.transmit()

    def _poison(self, reason: str) -> None:
        """Stop reading a misbehaving leg and end the wire (queued None)."""
        if self._ctrl_dead:
            return
        self._ctrl_dead = True
        self._ctrl_rx.clear()
        self._rx = b""
        log.warning("[sense] wt leg rejected (%s)", reason)
        self.lines.put_nowait(None)

    def _on_events(self, data: bytes) -> None:
        """Buffer events-stream bytes and queue each complete NDJSON line.

        Both the partial-line buffer and the line queue are bounded: an
        endpoint that never sends a newline, or floods lines faster than the
        session consumes them, ends the leg instead of exhausting memory."""
        if self._ctrl_dead:
            return
        self._rx += data
        while b"\n" in self._rx:
            line, self._rx = self._rx.split(b"\n", 1)
            if line.strip():
                if self.lines.qsize() >= MAX_PENDING_MESSAGES:
                    self._poison("events queue overflow")
                    return
                self.lines.put_nowait(line.decode("utf-8", "replace"))
        if len(self._rx) > MAX_LINE_BYTES:
            self._poison(f"events line exceeds {MAX_LINE_BYTES} bytes")

    def _mark_wt_bidi(self, stream_id: int) -> None:
        """Work around aioquic 1.7.2 not recording the WT frame type on new streams."""
        # aioquic 1.7.2 gap: create_webtransport_stream writes the
        # WEBTRANSPORT_STREAM prefix but never records the frame type/session
        # on its H3Stream, so inbound data on the stream never surfaces.
        from aioquic.h3.connection import FrameType, H3Stream

        st = self._h3._stream.get(stream_id)
        if st is None:
            st = H3Stream(stream_id)
            self._h3._stream[stream_id] = st
        st.frame_type = FrameType.WEBTRANSPORT_STREAM
        st.session_id = self.session_id

    def open_session(self, path: str) -> None:
        """Send the extended CONNECT opening the webtransport session."""
        self.session_id = self._quic.get_next_available_stream_id()
        self._h3.send_headers(
            self.session_id,
            [
                (b":method", b"CONNECT"),
                (b":scheme", b"https"),
                (b":authority", b"wt"),
                (b":path", path.encode()),
                (b":protocol", b"webtransport"),
            ],
            end_stream=False,
        )
        self._quic.send_stream_data(self.session_id, b"")
        self.transmit()

    def send_hello(self, token: str, session_id: str) -> None:
        """Open the control stream and send the hello frame for the session."""
        self.control_stream = self._h3.create_webtransport_stream(self.session_id)
        self._mark_wt_bidi(self.control_stream)
        self._quic.send_stream_data(
            self.control_stream,
            _pack_control({"t": "hello", "token": token, "sessionId": session_id}),
        )
        self.transmit()

    def open_events_stream(self) -> None:
        """Open the NDJSON events stream for the session."""
        self.events_stream = self._h3.create_webtransport_stream(self.session_id)
        self._mark_wt_bidi(self.events_stream)
        self.transmit()

    def send_event(self, obj: dict) -> None:
        """Write one event as an NDJSON line on the events stream."""
        self._quic.send_stream_data(self.events_stream, json.dumps(obj).encode() + b"\n")
        self.transmit()


def _make_wt_protocol_class():
    """Build the aioquic protocol class mixing _WtProtocol over QuicConnectionProtocol."""
    from aioquic.asyncio.protocol import QuicConnectionProtocol

    # Mixin FIRST: _WtProtocol's quic_event_received must shadow the base
    # class's, or no H3/WT event ever reaches our handlers.
    class SenseWtProtocol(_WtProtocol, QuicConnectionProtocol):
        """aioquic protocol with the WebTransport mixin layered on top."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """Delegate to the base protocol, then set up the WT state."""
            super().__init__(*args, **kwargs)
            self._wt_init()

    return SenseWtProtocol


def _parse_wt_url(url: str) -> tuple[str, int]:
    """Split a (possibly schemeless) webtransport URL into host and port."""
    p = urllib.parse.urlparse(url if "//" in url else f"https://{url}")
    return p.hostname or "", p.port or 443


class QuicWire(Wire):
    """WebTransport-over-QUIC wire: pinned-cert dial, hello handshake, NDJSON events."""

    kind = "quic"

    def __init__(self, answer: dict, http: aiohttp.ClientSession) -> None:
        """Keep the dial answer and client session; the QUIC client is created in connect."""
        super().__init__()
        self._http = http
        self._answer = answer
        self._cm: Any = None
        self._client: Any = None

    @property
    def call_id(self) -> str:
        """The realtime call id from the /calls answer (for client logs)."""
        return str(self._answer.get("callId", ""))

    def _validate_answer(self) -> None:
        """Reject a malformed calls answer before any network I/O."""
        answer = self._answer
        if not isinstance(answer, dict) or not all(
            isinstance(answer.get(k), str) and answer.get(k) for k in ("url", "token", "callId")
        ):
            raise TransportFailed("calls answer must carry string url, token and callId")
        hashes = answer.get("certHashes")
        if hashes is not None and not (
            isinstance(hashes, list) and all(isinstance(h, str) for h in hashes)
        ):
            raise TransportFailed("calls answer certHashes must be a list of strings")

    async def connect(self) -> None:
        """Dial QUIC, pin the served cert, then open session, hello, and events stream."""
        self._validate_answer()
        from aioquic.asyncio.client import connect
        from aioquic.h3.connection import H3_ALPN
        from aioquic.quic.configuration import QuicConfiguration

        try:
            import msgpack  # noqa: F401  (control frames)
        except ImportError as exc:
            raise TransportFailed(
                "aioquic/msgpack not installed (pip install 'urun-sense[quic]')"
            ) from exc

        host, port = _parse_wt_url(self._answer["url"])
        cfg = QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN)
        # The edge serves a short-lived self-signed cert; CA verification is
        # replaced by pinning the served certHashes below.
        cfg.verify_mode = ssl.CERT_NONE
        cfg.max_datagram_frame_size = 65536
        cfg.server_name = host
        self._cm = connect(host, port, configuration=cfg, create_protocol=_make_wt_protocol_class())
        client = await self._cm.__aenter__()
        self._client = client
        # Authenticate the peer before any application data leaves the client.
        self._pin_check(client)
        client.open_session("/wt")
        await asyncio.wait_for(client.session_ready.wait(), 10)
        client.send_hello(self._answer["token"], self._answer["callId"])
        await asyncio.wait_for(client.hello_acked.wait(), 10)
        client.open_events_stream()

    def _pin_check(self, client) -> None:
        """Verify the peer cert sha256 against the served certHashes, failing closed."""
        hashes = self._answer.get("certHashes") or []
        if not hashes:
            # CERT_NONE is the only verification otherwise; fail closed rather
            # than silently downgrading QUIC to unauthenticated.
            raise TransportFailed("no certHashes served; refusing unauthenticated QUIC")
        cert = client._quic.tls._peer_certificate
        if cert is None:
            raise TransportFailed("no peer certificate to pin against certHashes")
        from cryptography.hazmat.primitives.serialization import Encoding

        digest = hashlib.sha256(cert.public_bytes(Encoding.DER)).digest()
        wanted = set()
        for h in hashes:
            # The gateway serves base64 (32 raw bytes); tolerate lowercase hex.
            if len(h) == 64 and all(c in "0123456789abcdefABCDEF" for c in h):
                wanted.add(bytes.fromhex(h))
            else:
                try:
                    wanted.add(base64.b64decode(h, validate=True))
                except (binascii.Error, ValueError) as exc:
                    raise TransportFailed(
                        f"certHashes entry is neither base64 nor hex: {h!r}"
                    ) from exc
        if digest not in wanted:
            raise TransportFailed(
                f"server cert sha256 {digest.hex()[:16]}… not in served certHashes"
            )

    async def send(self, payload: dict) -> None:
        """Send one event over the webtransport events stream."""
        if self.closed or self._client is None:
            raise TransportFailed("quic wire closed")
        self._client.send_event(payload)

    async def receive(self) -> str | None:
        """Wait for the next NDJSON line; None when the connection terminated."""
        line = await self._client.lines.get()
        if line is None:
            self.closed = True
            return None
        return line

    async def close(self) -> None:
        """Exit the QUIC connect context and close the client session."""
        try:
            if self._cm is not None:
                await self._cm.__aexit__(None, None, None)
        except Exception:
            pass
        try:
            await self._http.close()
        except Exception:
            pass
        self.closed = True


# ---------------------------------------------------------------------------
# WebRTC
# ---------------------------------------------------------------------------


class RtcWire(Wire):
    """WebRTC wire: an oai-events datachannel, SDP exchanged with the calls edge."""

    kind = "webrtc"

    def __init__(self) -> None:
        """Initialize the message queue; the peer connection is created in connect."""
        super().__init__()
        self._pc: Any = None
        self._dc: Any = None
        self._msgs: asyncio.Queue[str | None] = asyncio.Queue()

    async def connect(
        self, http: aiohttp.ClientSession, calls_url: str, api_key: str, model: str
    ) -> None:
        """Open the peer connection, POST the offer, and wait for the datachannel."""
        try:
            from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
        except ImportError as exc:
            raise TransportFailed(
                "aiortc not installed (pip install 'urun-sense[webrtc]')"
            ) from exc

        # No STUN/TURN: the SFU is reached via host candidates on its
        # announced ports (aiortc's default STUN adds a 5s gather timeout).
        pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
        self._pc = pc
        dc = pc.createDataChannel("oai-events")
        self._dc = dc
        # The SDK's media rides the event channel as JSON; the audio m-line
        # exists so the SFU's sendrecv merge can answer. We listen only.
        pc.addTransceiver("audio", direction="recvonly")

        dc.on("message", self._on_dc_message)
        dc.on("close", lambda: self._msgs.put_nowait(None))
        pc.on("connectionstatechange", self._on_pc_state)

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        res = await http.post(
            f"{calls_url}?model={urllib.parse.quote(model)}",
            data=pc.localDescription.sdp,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/sdp"},
            # Never replay the bearer key to wherever a redirect points.
            allow_redirects=False,
        )
        body = await res.text()
        if res.status == 402:
            raise OutOfCreditsError()  # admission gate: terminal, never retried
        if res.status != 201:
            raise TransportFailed(f"POST /v1/realtime/calls -> {res.status}: {body[:200]}")
        await pc.setRemoteDescription(RTCSessionDescription(sdp=body, type="answer"))
        await self._wait_dc_open()

    def _on_dc_message(self, message) -> None:
        """Queue an incoming datachannel message, decoding bytes to text.

        Oversize messages and an overflowing queue end the wire rather than
        growing memory without bound."""
        if self.closed:
            return
        size = len(message.encode("utf-8", "replace")) if isinstance(message, str) else len(message)
        if size > MAX_INBOUND_MESSAGE_BYTES or self._msgs.qsize() >= MAX_PENDING_MESSAGES:
            log.warning("[sense] webrtc message too large or queue overflow; dropping the wire")
            self.closed = True
            self._msgs.put_nowait(None)
            return
        if isinstance(message, bytes):
            message = message.decode("utf-8", "replace")
        self._msgs.put_nowait(message)

    def _on_pc_state(self) -> None:
        """Queue None when the peer connection fails or closes."""
        if self._pc.connectionState in ("failed", "closed"):
            self._msgs.put_nowait(None)

    async def _wait_dc_open(self) -> None:
        """Wait for the datachannel to open, raising if ICE or the connection fails."""
        while self._dc.readyState != "open":
            if self._pc.connectionState in ("failed", "closed"):
                raise TransportFailed(
                    f"peer connection {self._pc.connectionState} before datachannel open"
                )
            if self._pc.iceConnectionState == "failed":
                raise TransportFailed("ICE failed before datachannel open")
            await asyncio.sleep(0.05)

    async def send(self, payload: dict) -> None:
        """Send the payload as JSON on the datachannel."""
        if self.closed or self._dc is None or self._dc.readyState != "open":
            raise TransportFailed("webrtc wire closed")
        self._dc.send(json.dumps(payload))

    async def receive(self) -> str | None:
        """Wait for the next datachannel message; None when the connection closed."""
        msg = await self._msgs.get()
        if msg is None:
            self.closed = True
            return None
        return msg

    async def close(self) -> None:
        """Close the datachannel and the peer connection."""
        self.closed = True
        try:
            if self._dc is not None:
                self._dc.close()
        except Exception:
            pass
        try:
            if self._pc is not None:
                await self._pc.close()
        except Exception:
            pass

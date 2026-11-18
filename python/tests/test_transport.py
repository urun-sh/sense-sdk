"""Transport-layer tests: fallback order, explicit override, and event-flow
parity over a non-WS wire. All offline — dialers and wires are fakes."""

import asyncio
import base64
import hashlib
import json
import logging
import os
import struct
import sys
import types

import pytest
from sense import Question, QuestionKind, Session
import sense._session as sess_mod
from sense._transport import (
    WT_CONTROL_FRAME_MAX,
    QuicWire,
    TransportFailed,
    Wire,
    _pack_control,
    _WtProtocol,
)


def dec(qid, t_ms, choice, probs, conf):
    """Build one sense.decision JSON line for a scripted wire."""
    return json.dumps(
        {
            "event": "sense.decision",
            "id": qid,
            "t_ms": t_ms,
            "choice": choice,
            "probabilities": probs,
            "confidence": conf,
        }
    )


class FakeWire(Wire):
    """Wire fake that replays a scripted list of messages."""
    def __init__(self, kind: str, script: list[str] | None = None, close_after: bool = False):
        """Take the wire kind, a message script, and an optional close flag."""
        super().__init__()
        self.kind = kind
        self._script = list(script or [])
        self._close_after = close_after

    async def send(self, payload: dict) -> None:
        """Drop sent payloads on the floor."""
        pass

    async def receive(self) -> str | None:
        """Replay the next scripted message; block or return None once exhausted."""
        if not self._script:
            if self._close_after:
                return None  # wire closed
            await asyncio.sleep(3600)
        return self._script.pop(0) if self._script else None

    async def close(self) -> None:
        """Mark the wire closed."""
        self.closed = True


def scripted_wave_events():
    """session ack + one complete wave, exactly as every transport carries it."""
    return [
        json.dumps({"type": "session.created", "session": {}}),
        json.dumps(
            {
                "type": "response.output_text.delta",
                "delta": dec("finished", 0, "true", {"true": 1.0, "false": 0.0}, 1.0)
                + "\n"
                + dec("intent", 0, "support", {"support": 1.0, "other": 0.0}, 1.0)
                + "\n",
            }
        ),
        json.dumps({"type": "response.completed"}),
    ]


def make_session(**kw) -> Session:
    """A test session with the finished and intent questions already added."""
    s = Session(api_key="sk-test", base_url="http://127.0.0.1:1", **kw)
    s.set_questions(
        [
            Question(name="finished", kind=QuestionKind.NOUL, instructions="finished?"),
            Question(
                name="intent",
                kind=QuestionKind.CHOICE,
                instructions="want?",
                criteria={"support": "Technical help", "other": "Anything else"},
            ),
        ]
    )
    return s


def failing_dial(name: str, dialed: list):
    """A dialer that records its name and always raises TransportFailed."""
    async def dial():
        """Record the attempt, then fail the dial."""
        dialed.append(name)
        raise TransportFailed(f"{name} down")

    return dial


@pytest.fixture
def fake_stacks(monkeypatch):
    """Make aioquic and aiortc importable without installing them.

    _transport_order() import-gates quic/webrtc, so tests that stub their
    dialers need the stacks to appear installed; the fake modules are never
    touched beyond the import probe.
    """
    monkeypatch.setitem(sys.modules, "aioquic", types.ModuleType("aioquic"))
    monkeypatch.setitem(sys.modules, "aiortc", types.ModuleType("aiortc"))


# ---------------------------------------------------------------------------
# selection / fallback
# ---------------------------------------------------------------------------


def test_auto_falls_back_quic_webrtc_then_ws(caplog, fake_stacks):
    """Auto transport dials quic, then webrtc, then lands on ws and logs each failure."""
    s = make_session()
    dialed: list[str] = []

    async def ws():
        """Dialer returning a live fake ws wire."""
        dialed.append("ws")
        return FakeWire("ws")

    s._dial_quic = failing_dial("quic", dialed)
    s._dial_webrtc = failing_dial("webrtc", dialed)
    s._dial_ws = ws
    with caplog.at_level("INFO", logger="sense"):
        wire = asyncio.run(s._connect())
    assert dialed == ["quic", "webrtc", "ws"]
    assert wire.kind == "ws"
    assert "transport=quic failed" in caplog.text
    assert "transport=webrtc failed" in caplog.text
    assert "transport=ws" in caplog.text


def test_auto_picks_quic_when_it_dials(fake_stacks):
    """Auto transport uses quic immediately when it dials, never reaching webrtc."""
    s = make_session()

    async def quic():
        """Dialer returning a live fake quic wire."""
        return FakeWire("quic")

    dialed: list[str] = []
    s._dial_quic = quic
    s._dial_webrtc = failing_dial("webrtc", dialed)
    wire = asyncio.run(s._connect())
    assert wire.kind == "quic"
    assert dialed == []


def test_explicit_transport_does_not_fall_back():
    """An explicit quic transport raises instead of falling back to ws."""
    s = make_session(transport="quic")
    dialed: list[str] = []
    s._dial_quic = failing_dial("quic", dialed)
    s._dial_ws = failing_dial("ws", dialed)
    with pytest.raises(TransportFailed):
        asyncio.run(s._connect())
    assert dialed == ["quic"]


def test_explicit_webrtc_dials_webrtc_only():
    """An explicit webrtc transport dials webrtc and nothing else."""
    s = make_session(transport="webrtc")

    async def webrtc():
        """Dialer returning a live fake webrtc wire."""
        return FakeWire("webrtc")

    dialed: list[str] = []
    s._dial_webrtc = webrtc
    s._dial_quic = failing_dial("quic", dialed)
    assert asyncio.run(s._connect()).kind == "webrtc"
    assert dialed == []


def test_unknown_transport_rejected():
    """Session raises ValueError for a transport it does not know."""
    with pytest.raises(ValueError):
        Session(transport="sse")


def test_auto_skips_uninstalled_stacks(monkeypatch):
    """Auto order drops quic and webrtc when their stacks are not installed."""
    # sys.modules entries set to None make `import x` raise ImportError
    monkeypatch.setitem(sys.modules, "aioquic", None)
    monkeypatch.setitem(sys.modules, "aiortc", None)
    s = make_session()
    assert s._transport_order() == ["ws"]


def test_dial_budgets_cover_each_nonws_transport():
    """Dial budgets exist and webrtc's is longer than quic's."""
    from sense._transport import TRANSPORT_DIAL_TIMEOUTS_S

    # 5 s: the whole POST → WT-ready → hello-ack dance through the console's
    # same-origin relay exceeded the old 2 s budget (1.2–1.8 s direct,
    # 2026-11-15), so prod fell back off a rung that would have connected.
    # Still bounded, so UDP-blocked callers fail over quickly.
    assert TRANSPORT_DIAL_TIMEOUTS_S["quic"] <= 5.0
    assert TRANSPORT_DIAL_TIMEOUTS_S["webrtc"] > TRANSPORT_DIAL_TIMEOUTS_S["quic"]


# ---------------------------------------------------------------------------
# event-flow parity over a fake (non-WS) wire
# ---------------------------------------------------------------------------


def test_event_flow_over_fake_wire_yields_waves():
    """One scripted wave over a fake quic wire produces one wave with both answers."""
    s = make_session()
    s.transport = "quic"

    async def quic():
        """Dialer returning a fake quic wire with the scripted wave."""
        return FakeWire("quic", scripted_wave_events())

    s._dial_quic = quic

    async def go():
        """Run the session and collect the first wave it yields."""
        got = []

        async def consume():
            """Collect waves from the session run, stopping after one."""
            async for wave in s.run():
                got.append(wave)
                break  # one wave is enough

        await asyncio.wait_for(consume(), timeout=2.0)
        return got

    got = asyncio.run(go())
    assert len(got) == 1
    assert got[0].result("finished").value is True
    assert got[0].result("intent").value == "support"


def test_wire_close_reconnects_with_fresh_dial(monkeypatch):
    """When the wire closes mid-run, the session redials with a fresh wire."""
    monkeypatch.setattr(sess_mod, "REDIAL_BASE_S", 0.001)
    s = make_session()
    s.transport = "quic"
    calls = []

    async def quic():
        """Dialer whose first wire closes after its script, then stays open."""
        calls.append(1)
        if len(calls) == 1:
            return FakeWire("quic", scripted_wave_events(), close_after=True)
        return FakeWire("quic")  # alive but silent

    s._dial_quic = quic

    async def go():
        """Consume waves for up to a second, letting the reconnect happen."""
        got = []

        async def consume():
            """Drain waves from the session run until the timeout."""
            async for wave in s.run():
                got.append(wave)

        try:
            await asyncio.wait_for(consume(), timeout=1.0)
        except asyncio.TimeoutError:
            pass

    asyncio.run(go())
    assert len(calls) == 2  # first wire died after its scripted events, redial happened
    assert s.stats.waves == 1


# ---------------------------------------------------------------------------
# QUIC wire unit checks (no network)
# ---------------------------------------------------------------------------


def _self_signed_der() -> bytes:
    """Random DER-shaped bytes; _pin_check only SHA-256 hashes the DER, so no
    real certificate (and no cryptography install) is needed."""
    return os.urandom(1024)


def _have_cryptography() -> bool:
    """True when cryptography is importable."""
    try:
        import cryptography  # noqa: F401
    except ImportError:
        return False
    return True


# _pin_check reads the peer certificate via cryptography (always present when
# the aioquic extra is installed); the raw-bytes pin probes below only need it
# for that import.
needs_cryptography = pytest.mark.skipif(
    not _have_cryptography(), reason="cryptography not installed (ships with the quic extra)"
)


class _PinProbe(QuicWire):
    """Expose _pin_check without dialing."""

    def __init__(self, hashes):
        """Store the served cert hashes without dialing."""
        super().__init__({"certHashes": hashes}, http=None)

    def check(self, der):
        """Run _pin_check against a fake peer certificate with the given DER."""
        class FakeCert:
            """Fake peer certificate with fixed DER bytes."""
            def __init__(self, der):
                """Keep the DER bytes."""
                self._der = der

            def public_bytes(self, encoding):
                """Return the stored DER bytes."""
                return self._der

        client = type("C", (), {})()
        client._quic = type("Q", (), {})()
        client._quic.tls = type("T", (), {})()
        client._quic.tls._peer_certificate = FakeCert(der)
        self._pin_check(client)


@needs_cryptography
def test_quic_pin_check_accepts_served_hash():
    """_pin_check accepts the served hash in base64 and in hex."""
    der = _self_signed_der()
    digest = hashlib.sha256(der).digest()
    _PinProbe([base64.b64encode(digest).decode()]).check(der)  # gateway format: base64
    _PinProbe([digest.hex()]).check(der)  # tolerate hex


@needs_cryptography
def test_quic_pin_check_rejects_unknown_cert():
    """_pin_check rejects a cert whose hash was not served."""
    digest = hashlib.sha256(_self_signed_der()).digest()
    wire = _PinProbe([base64.b64encode(digest).decode()])
    with pytest.raises(TransportFailed):
        wire.check(_self_signed_der())


@needs_cryptography
def test_quic_pin_check_rejects_bad_encoding():
    """_pin_check rejects a certHashes entry that is neither base64 nor hex."""
    wire = _PinProbe(["not-base64!! and not hex"])
    with pytest.raises(TransportFailed):
        wire.check(_self_signed_der())


def test_quic_pin_check_fails_closed_without_hashes():
    """_pin_check refuses the dial when no certHashes were served."""
    # CERT_NONE means no verification at all; an empty certHashes answer must
    # refuse the QUIC dial rather than silently downgrade to unauthenticated.
    with pytest.raises(TransportFailed):
        _PinProbe([]).check(_self_signed_der())


# _unpack_control and _send_pong pack/decode msgpack frames; msgpack ships
# with the quic extra, so the control tests degrade gracefully without it.
def _have_msgpack() -> bool:
    """True when msgpack is importable."""
    try:
        import msgpack  # noqa: F401
    except ImportError:
        return False
    return True


needs_msgpack = pytest.mark.skipif(
    not _have_msgpack(), reason="msgpack not installed (ships with the quic extra)"
)


class _FakeQuic:
    """Record send_stream_data calls without aioquic."""
    def __init__(self):
        """Start with an empty send log."""
        self.sent = []

    def send_stream_data(self, stream_id, data):
        """Log the stream id and the bytes."""
        self.sent.append((stream_id, bytes(data)))


class _ControlStub(_WtProtocol):
    """Drive _on_control offline: fake quic, no H3 connection needed."""
    def __init__(self):
        """Set the control-stream state _on_control and _send_pong touch."""
        self.hello_acked = asyncio.Event()
        self.control_stream = 7
        self._ctrl_rx = bytearray()
        self._ctrl_dead = False
        self.lines = asyncio.Queue()
        self._quic = _FakeQuic()

    def transmit(self):
        """No-op; there is no real QUIC connection to flush."""
        pass


@needs_msgpack
def test_control_stream_survives_fragmented_and_batched_frames():
    """QUIC chunks frames arbitrarily: a split hello-ack and back-to-back
    frames must both decode (the old unpacker silently dropped both)."""
    stub = _ControlStub()
    frame = _pack_control({"t": "hello-ack"})
    stub._on_control(frame[:2])
    assert not stub.hello_acked.is_set()
    stub._on_control(frame[2:] + _pack_control({"t": "ping", "id": 9, "ts": 1}))
    assert stub.hello_acked.is_set()
    ((sid, data),) = stub._quic.sent
    (n,) = struct.unpack_from("<I", data, 0)
    import msgpack

    assert msgpack.unpackb(data[4 : 4 + n]) == {"t": "pong", "id": 9, "ts": 1}


@needs_msgpack
def test_control_stream_answers_gateway_ping():
    """A ping keepalive gets a pong on the control stream echoing its id and ts."""
    import msgpack

    stub = _ControlStub()
    stub._on_control(_pack_control({"t": "ping", "id": 42, "ts": 7}))
    ((sid, data),) = stub._quic.sent
    (n,) = struct.unpack_from("<I", data, 0)
    assert sid == stub.control_stream
    assert msgpack.unpackb(data[4 : 4 + n]) == {"t": "pong", "id": 42, "ts": 7}


@needs_msgpack
def test_control_pong_defaults_missing_fields():
    """A ping without id/ts echoes zeros, matching the TS client."""
    import msgpack

    stub = _ControlStub()
    stub._on_control(_pack_control({"t": "ping"}))
    ((sid, data),) = stub._quic.sent
    (n,) = struct.unpack_from("<I", data, 0)
    assert msgpack.unpackb(data[4 : 4 + n]) == {"t": "pong", "id": 0, "ts": 0}


@needs_msgpack
def test_control_stream_ends_on_oversize_frame(caplog):
    """A length prefix over the cap ends the stream: the termination signal
    stays queued, later chunks on the rejected stream are neither decoded nor
    answered, and the rejection is logged."""
    stub = _ControlStub()
    with caplog.at_level(logging.WARNING, logger="sense"):
        stub._on_control(struct.pack("<I", WT_CONTROL_FRAME_MAX + 1) + b"x")
    assert any("poisoned" in r.message for r in caplog.records)
    stub._on_control(_pack_control({"t": "ping", "id": 5, "ts": 5}))
    assert not stub._quic.sent
    assert stub.lines.get_nowait() is None


@needs_msgpack
def test_control_stream_skips_non_dict_message(caplog):
    """A non-dict control message is logged and skipped instead of raising
    AttributeError into the event loop; the stream keeps working."""
    stub = _ControlStub()
    with caplog.at_level(logging.WARNING, logger="sense"):
        stub._on_control(_pack_control([1, 2]))
        stub._on_control(_pack_control({"t": "ping", "id": 3, "ts": 3}))
    assert any("not a dict" in r.message for r in caplog.records)
    ((sid, data),) = stub._quic.sent
    (n,) = struct.unpack_from("<I", data, 0)
    assert sid == stub.control_stream
    import msgpack

    assert msgpack.unpackb(data[4 : 4 + n]) == {"t": "pong", "id": 3, "ts": 3}


def test_ws_wire_accepts_binary_frames():
    """Prod lax sends server events as BINARY ws frames (first one ~170 ms
    after the dial). They carry UTF-8 event JSON (the TS SDK stringifies every
    message buffer) and must NOT end the wire — 0.2.2 killed the wire on the
    first binary frame and stormed reconnects at ~0.35 s."""
    import aiohttp
    from sense._transport import WsWire

    class Msg:
        def __init__(self, msg_type, data=None, extra=None):
            self.type, self.data, self.extra = msg_type, data, extra

    frames = iter([
        Msg(aiohttp.WSMsgType.BINARY, b'{"type":"session.created"}'),
        Msg(aiohttp.WSMsgType.PING),
        Msg(aiohttp.WSMsgType.PONG),
        Msg(aiohttp.WSMsgType.TEXT, '{"type":"response.created"}'),
        Msg(aiohttp.WSMsgType.CLOSE, extra="done"),
    ])

    class FakeWs:
        close_code = 1000

        async def receive(self):
            return next(frames)

    async def go():
        wire = WsWire(FakeWs(), None)
        first = await wire.receive()   # binary frame decodes as event text
        second = await wire.receive()  # ping/pong are skipped, not closure
        third = await wire.receive()   # a real CLOSE frame ends the wire
        return first, second, third, wire

    first, second, third, wire = asyncio.run(go())
    assert first == '{"type":"session.created"}'
    assert second == '{"type":"response.created"}'
    assert third is None
    assert wire.closed and wire.close_code == 1000 and wire.close_reason == "done"

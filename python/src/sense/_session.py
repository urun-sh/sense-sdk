"""Wire layer: the aiohttp realtime loop (protocol.md §1–§7).

One standing websocket per session: ``session.update`` registers questions,
``commit + response.create(max_output_tokens=250_000)`` opens a cadence turn,
``response.output_text.delta`` lines are ``sense.decision`` events assembled
into waves client-side (§6.3), turns re-open on ``response.completed``.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import math
import os
import random
import re
import time
import urllib.parse
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from typing import Any

import aiohttp

from ._core import (
    Context,
    OutOfCreditsError,
    ProtocolError,
    Question,
    Wave,
    WaveAssembler,
)
from ._transport import (
    MAX_INBOUND_MESSAGE_BYTES,
    MAX_LINE_BYTES,
    TRANSPORT_DIAL_TIMEOUTS_S,
    QuicWire,
    RtcWire,
    TransportFailed,
    Wire,
    WsWire,
    _client_ssl_context,
    check_base_url,
)


class _NoRedirectSession(aiohttp.ClientSession):
    """aiohttp session that never follows redirects.

    ``ws_connect()`` offers no ``allow_redirects`` switch, and aiohttp's
    request default would replay the Authorization bearer to a redirect
    target (TS parity: ``followRedirects: false`` on every lane). The /calls
    POSTs already pass ``allow_redirects=False`` explicitly; this makes the
    refusal global.
    """

    async def _request(self, method: str, str_or_url: Any, **kwargs: Any) -> aiohttp.ClientResponse:
        kwargs["allow_redirects"] = False
        return await super()._request(method, str_or_url, **kwargs)


def _http_session() -> aiohttp.ClientSession:
    """aiohttp session whose connections verify TLS against the OS trust
    store, so a bare Python install without a usable CA bundle still dials."""
    return _NoRedirectSession(connector=aiohttp.TCPConnector(ssl=_client_ssl_context()))

log = logging.getLogger("sense")

REALTIME_PATH = "/v1/realtime"
TURN_TOKEN_BUDGET = 250_000
MAX_FRAME_BYTES = 256_000
RETRY_FLOOR_S = 5.0
RETRY_CAP_S = 30.0
RETRY_MAX_ATTEMPTS = 600

#: Reconnect pacing for wires that die young (relay/GS churn): the same
#: ladder the TS SDK runs. A wire that lived resets the ladder; a wire that
#: dies young backs off (jittered, so co-tenants do not retry in lockstep)
#: and consecutive young deaths are BOUNDED — past the bound the drive ends
#: fatally instead of storming the hub with instant redials.
REDIAL_BASE_S = 1.0
YOUNG_WIRE_S = 10.0
MAX_CONSECUTIVE_YOUNG_DEATHS = 10

#: Per-source outbound audio held while the lane is down (~60 s of 40 ms
#: chunks). Older chunks are dropped first so a long outage cannot grow
#: memory without bound; stale audio is worthless to a realtime lane.
MAX_PENDING_AUDIO_CHUNKS = 1500
#: Text items held for the next send; the oldest is dropped beyond this.
MAX_PENDING_TEXT_ITEMS = 256

#: Explicit presence (owner GO 2026-11-16, hub-primary): the proxy holds a
#: pooled seat only while the client says it is present. The signal is the
#: ``{"type": "session.presence"}`` frame on the realtime lane, sent every
#: ``PRESENCE_CADENCE_S`` while ``monotonic() - _last_activity`` is inside the
#: idle window; an idle lapse stops the frames (an explicit detach — the
#: runtime's #3439 governor then PARKS the seat, it never dies), and the next
#: app-level send re-asserts it. A visible browser tab is NOT activity (owner
#: ruling): only real app sends and ``keepalive()`` count.
PRESENCE_IDLE_S = 60.0
PRESENCE_CADENCE_S = 10.0

#: Keepalive event types the SDK itself sends on the realtime lane. An
#: adapter that rejects one with ``unknown_event`` gets it muted for the
#: rest of that connection (liveness still comes from audio frames).
_KEEPALIVE_EVENTS = frozenset({"session.presence", "ping"})
_UNKNOWN_EVENT_RE = re.compile(r"event type\s+(\S+)\s+is not supported")

__all__ = ["AttachHandle", "Session", "SessionStats"]


class SenseUnavailable(RuntimeError):
    """The Sense lane is unreachable after sustained retries."""


class _LaneDead(Exception):
    """`session_gone` (close 1011): discard local session state, reconnect fresh."""


@dataclass
class SessionStats:
    waves: int = 0
    decisions: int = 0
    errors: int = 0
    protocol_errors: int = 0


class _AudioInput:
    __slots__ = ("_s",)

    def __init__(self, session: Session) -> None:
        self._s = session

    def push(self, pcm_bytes: bytes, source: str = "default") -> None:
        """Queue base64 PCM16 24 kHz mono audio. Sources other than the
        default are queued client-side and pushed round-robin over the
        shared audio stream."""
        if not pcm_bytes:
            return
        self._s._enqueue_audio(source, base64.b64encode(bytes(pcm_bytes)).decode("ascii"))


class _VideoInput:
    __slots__ = ("_s",)

    def __init__(self, session: Session) -> None:
        self._s = session

    def push(self, jpeg_bytes: bytes) -> None:
        if len(jpeg_bytes) > MAX_FRAME_BYTES:
            raise ValueError(
                f"video frame is {len(jpeg_bytes)} bytes; the lane refuses frames over {MAX_FRAME_BYTES}"
            )
        self._s._enqueue_video(base64.b64encode(bytes(jpeg_bytes)).decode("ascii"))


class _TextInput:
    __slots__ = ("_s",)

    def __init__(self, session: Session) -> None:
        self._s = session

    def push(self, text: str, mode: str = "append") -> None:
        """append (default) maintains a rolling transcript re-sent each turn;
        replace resets it; event rides the next turn only. On today's wire the
        conversation is append-only, so replace/event are client-side
        conventions with documented degradation."""
        if mode == "append":
            self._s._transcript += text
        elif mode == "replace":
            self._s._transcript = text
        elif mode == "event":
            pass  # not folded into the rolling transcript; sent as one item
        else:
            raise ValueError(f"unknown text mode {mode!r}; expected append|replace|event")
        self._s._enqueue_text(text)


class AttachHandle:
    """Cancels the input pumps one :meth:`Session.attach` call started."""

    __slots__ = ("_tasks",)

    def __init__(self, tasks: list[asyncio.Task]) -> None:
        self._tasks = tasks

    def detach(self) -> None:
        for t in self._tasks:
            t.cancel()


class Session:
    DEFAULT_BASE_URL = "https://inference.urun.sh"
    #: One seat's sustained floor on the serving GPU is ~12.5 Hz (<80 ms/tick
    #: is the owner's ruling); a faster request coalesces to the newest media
    #: and just realizes fewer waves. 12.5 is the default AND the ceiling.
    MAX_HZ = 12.5

    def __init__(  # noqa: PLR0913 - public options; allow_insecure is a keyword-only opt-in
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "sense-latest:bf16",
        # One seat's sustained floor on the serving GPU is ~12.5 Hz (<80 ms/tick
        # is the owner's ruling); a faster request coalesces to the newest media
        # and just realizes fewer waves. 12.5 is the default the lane can keep.
        hz: float = 12.5,
        transport: str = "auto",
        *,
        allow_insecure: bool = False,
    ) -> None:
        if transport not in ("auto", "quic", "webrtc", "ws"):
            raise ValueError(f"unknown transport {transport!r}; expected auto|quic|webrtc|ws")
        self.transport = transport
        self.api_key = api_key or os.environ.get("URUN_API_KEY")
        self.base_url = check_base_url(
            base_url or os.environ.get("SENSE_BASE_URL") or self.DEFAULT_BASE_URL,
            allow_insecure=allow_insecure,
        )
        self.model = model
        self.hz = float(hz)
        if not math.isfinite(self.hz) or not 0 < self.hz <= self.MAX_HZ:
            raise ValueError(
                f"hz must be in (0, {self.MAX_HZ}] waves per second, got {hz!r}: 12.5 is one"
                " seat's sustained floor; a faster request coalesces to the newest media"
                " and just realizes fewer waves"
            )
        self.questions: list[Question] = []
        self._closed = False
        self.stats = SessionStats()
        self._ctx = Context(session=self)
        self._onwave_handlers: list[tuple[Callable, bool]] = []
        self._transcript = ""
        self._audio: dict[str, deque] = {}
        self._active_source = "default"  # last input source drained (labeling)
        self._video: deque = deque(maxlen=1)  # latest frame wins
        self._text_out: deque = deque(maxlen=MAX_PENDING_TEXT_ITEMS)
        self._wake: asyncio.Event | None = None
        self._out_lock = asyncio.Lock()  # one writer at a time on the wire
        self._turn_open = False
        self._pumps: list[asyncio.Task] = []  # attach()ed input pumps
        #: The ONE session drive (dial + cadence + wave assembly), started by
        #: whichever of attach()/run() runs first. Waves land in ``_wave_q``
        #: for run() consumers; handlers fire in _accept either way.
        self._drive_task: asyncio.Task | None = None
        self._fatal: BaseException | None = None
        self._wave_q: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wire: Wire | None = None
        #: Keepalive event types the current adapter has rejected with
        #: ``unknown_event`` (per connection; reset on every dial).
        self._muted_keepalive: set[str] = set()
        #: Young-death ladder: monotonic time the current wire went live,
        #: consecutive young deaths, and the next redial delay.
        self._connected_at: float | None = None
        self._young_deaths = 0
        self._redial_delay = 0.0
        self._audio_in = _AudioInput(self)
        self._video_in = _VideoInput(self)
        self._text_in = _TextInput(self)
        #: Last app-level send (audio/video/text push, barge-in, keepalive).
        #: A freshly created session is present until this lapses.
        self._last_activity = time.monotonic()

    # -- registration ------------------------------------------------------

    def _register(self, q: Question) -> Question:
        # Replacing a duplicate name keeps registration idempotent — the last
        # registration wins.
        for i, old in enumerate(self.questions):
            if old.name == q.name:
                self.questions[i] = q
                return q
        self.questions.append(q)
        return q

    def set_questions(self, questions: Iterable[Question]) -> None:
        """Replace the whole question registry (adds, edits AND removals) and
        re-latch the schema: barges in and re-sends ``session.update`` so the
        next turn uses the new envelope (TS parity). Call from async code
        (inside the run loop or before ``run()`` with a running loop)."""
        self.questions = []
        for q in questions:
            self._register(q)
        wire = self._wire
        if wire is None or self._closed or wire.closed:
            return
        self._barge_in()
        loop = self._loop
        if loop is None or not loop.is_running():
            raise RuntimeError("set_questions() on a live session needs a running event loop")
        loop.create_task(self._send(wire, self._session_update()))

    def _session_update(self) -> dict:
        return {
            "type": "session.update",
            "session": {
                "model": self.model,
                "modalities": ["text"],
                "instructions": json.dumps(self._envelope()),
            },
        }

    def _envelope(self) -> dict:
        return {"questions": {q.name: q.to_envelope() for q in self.questions}, "hz": self.hz}

    # -- wave handlers (the facade registers; see Sense.when / Sense.on_wave) --

    def _on_wave(
        self, handler: Callable, coalesce: bool = False, *, source: str | None = None
    ) -> None:
        """Observe waves. ``coalesce=True`` skips consecutive same-argmax
        waves (latest wins); ``source=`` restricts to one input source."""
        self._onwave_handlers.append((handler, coalesce, source))

    def _accept(self, wave: Wave) -> None:
        # When the server omits a source label, attribute the wave to the
        # input source last drained. This does not isolate sources server-side.
        if wave.source == "default" and self._active_source != "default":
            wave.source = self._active_source
        prev = self._ctx.wave
        self._ctx._ingest(wave)
        for handler, coalesce, src in self._onwave_handlers:
            if src is not None and src != wave.source:
                continue
            if coalesce and prev is not None and prev._argmax == wave._argmax:
                continue
            self._ctx._fire(handler)
        self.stats.waves += 1

    # -- outbound queues -----------------------------------------------------

    def _enqueue_audio(self, source: str, b64: str) -> None:
        self._last_activity = time.monotonic()
        queue = self._audio.get(source)
        if queue is None:
            queue = self._audio[source] = deque(maxlen=MAX_PENDING_AUDIO_CHUNKS)
        elif len(queue) == queue.maxlen:
            log.warning("[sense] audio backlog full for source %r; dropping oldest chunk", source)
        queue.append(b64)
        self._wake_up()

    def _enqueue_video(self, b64: str) -> None:
        self._last_activity = time.monotonic()
        self._video.append(b64)
        self._wake_up()

    def _enqueue_text(self, text: str) -> None:
        self._last_activity = time.monotonic()
        self._text_out.append(text)
        self._wake_up()

    def _wake_up(self) -> None:
        wake, loop = self._wake, self._loop
        if wake is None or loop is None or not loop.is_running():
            return
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is loop:
            wake.set()
        else:
            loop.call_soon_threadsafe(wake.set)

    def _text_item(self, text: str) -> dict:
        return {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }

    def _image_item(self, b64: str) -> dict:
        return {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"}],
            },
        }

    def _has_pending(self) -> bool:
        return any(self._audio.values()) or bool(self._video) or bool(self._text_out)

    async def _open_turn_on_first_audio(self, wire) -> bool:
        """Pop the first buffered audio item and open the cadence turn with it
        (commit must follow buffered audio). Returns False on
        send failure, requeueing the item."""
        src_with_audio = next((s for s, q in self._audio.items() if q), None)
        first_append = None
        if src_with_audio is not None:
            first_append = self._audio[src_with_audio].popleft()
            self._active_source = src_with_audio
        try:
            if first_append is not None:
                await self._send(wire, {"type": "input_audio_buffer.append", "audio": first_append})
            await self._open_turn(wire, audio_pending=first_append is not None)
        except Exception:
            if first_append is not None:
                self._audio[src_with_audio].appendleft(first_append)
            return False
        return True

    async def _try_send_audio(self, wire, queue, b64: str) -> bool:
        """Send one buffered audio chunk; on failure requeue it and return False."""
        try:
            await self._send(wire, {"type": "input_audio_buffer.append", "audio": b64})
            return True
        except Exception:
            queue.appendleft(b64)
            return False

    async def _try_send_item(self, wire, queue, item, build) -> bool:
        """Send one built wire message; on failure requeue the item and return False."""
        try:
            await self._send(wire, build(item))
            return True
        except Exception:
            queue.appendleft(item)
            return False

    async def _drain_audio_queues(self, wire) -> bool:
        """Round-robin every buffered audio source. Returns False on send failure."""
        for q in list(self._audio.values()):
            if q:
                b64 = q.popleft()
                self._active_source = next(
                    (s for s, qq in self._audio.items() if qq is q), self._active_source
                )
                if not await self._try_send_audio(wire, q, b64):
                    return False
        return True

    async def _drain_pending_queues(self, wire) -> bool:
        """Drain audio, then video (latest frame), then text. Returns False
        on send failure (payloads requeued)."""
        if not await self._drain_audio_queues(wire):
            return False
        if self._video:
            b64 = self._video.popleft()
            if not await self._try_send_item(wire, self._video, b64, self._image_item):
                return False
        if self._text_out:
            text = self._text_out.popleft()
            if not await self._try_send_item(wire, self._text_out, text, self._text_item):
                return False
        return True

    async def _drain_outbound(self, wire) -> bool:
        """Round-robin audio sources, then video (latest frame), then text.
        Opens the cadence turn on the first media item (the audio
        commit must follow buffered audio). Returns False if a send failed
        (payloads requeued)."""
        if not self._turn_open and self._has_pending():
            if not await self._open_turn_on_first_audio(wire):
                return False
        while True:
            if not self._has_pending():
                return True
            if not await self._drain_pending_queues(wire):
                return False

    async def _sender(self) -> None:
        """Background drain of the outbound queues while the lane is live."""
        assert self._wake is not None
        while not self._closed:
            wire = self._wire
            if wire is None or wire.closed or not self._has_pending():
                self._wake.clear()
                if self._wire is not None and not self._wire.closed and self._has_pending():
                    continue
                await self._wake.wait()
                continue
            ok = await self._drain_outbound(wire)
            if not ok:
                await asyncio.sleep(0.1)

    # -- input pumps ---------------------------------------------------------

    async def _pump_chunks(self, src: Any) -> AsyncIterator[Any]:
        """Yield chunks from any supported input source type."""
        if callable(src):
            while True:
                chunk = src()
                if inspect.isawaitable(chunk):
                    chunk = await chunk
                if chunk is None:
                    return
                yield chunk
        elif hasattr(src, "__anext__"):
            async for chunk in src:
                yield chunk
        elif hasattr(src, "__iter__"):
            for chunk in src:
                yield chunk
        else:
            raise TypeError(f"cannot pump input from {src!r}")

    async def _pump_one(self, src: Any, push: Callable) -> None:
        try:
            async for chunk in self._pump_chunks(src):
                push(chunk)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.stats.errors += 1
            log.exception("[sense] input pump failed")

    def attach(self, audio: Any = None, video: Any = None, text: Any = None) -> AttachHandle:
        """The ONE canonical way to stream inputs into the session.

        Each source accepts an async iterable, a sync iterable, or a
        zero-arg callable returning the next chunk (``None`` ends): audio
        chunks are raw PCM16 24 kHz mono bytes, video chunks are JPEG bytes,
        text chunks are strings. Chunks are queued and drained onto the wire
        while the lane is live; ``run()`` consumes the waves.

        Call it from async code (before or during ``run()``); the pumps stop
        when the session closes or the returned handle is ``detach()``ed."""
        if self._closed:
            raise RuntimeError("session is closed")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError(
                "attach() needs a running event loop — call it from async code "
                "(before or during session.run())"
            ) from exc
        # attach IS starting the session: the drive (dial + cadence + wave
        # assembly) begins now, so app.when/app.on_wave handlers fire whether
        # or not anyone iterates run(). Idempotent — run() shares this drive.
        self._ensure_drive()
        tasks = [
            loop.create_task(self._pump_one(src, push), name=f"sense-attach-{what}")
            for what, src, push in (
                ("audio", audio, self._audio_in.push),
                ("video", video, self._video_in.push),
                ("text", text, self._text_in.push),
            )
            if src is not None
        ]
        self._pumps.extend(tasks)
        return AttachHandle(tasks)

    # -- connect / reconnect ---------------------------------------------------

    def _realtime_url(self) -> str:
        p = urllib.parse.urlsplit(self.base_url)
        scheme = {"https": "wss", "http": "ws"}.get(p.scheme.lower(), p.scheme.lower())
        base = urllib.parse.urlunsplit((scheme, p.netloc, p.path, "", "")).rstrip("/")
        return base + REALTIME_PATH + "?model=" + urllib.parse.quote(self.model, safe=":")

    def _transport_order(self) -> list[str]:
        """Dial order. Auto = quic -> webrtc -> ws, skipping transports whose
        client stack is not installed. Explicit overrides dial exactly one."""
        if self.transport != "auto":
            return [self.transport]
        order: list[str] = []
        try:
            import aioquic  # noqa: F401

            order.append("quic")
        except ImportError:
            pass
        try:
            import aiortc  # noqa: F401

            order.append("webrtc")
        except ImportError:
            pass
        return order + ["ws"]

    def _calls_url(self) -> str:
        """URL of the /v1/realtime/calls endpoint on the session's base."""
        return self.base_url.rstrip("/") + "/v1/realtime/calls"

    async def _dial_quic(self) -> QuicWire:
        """POST the calls endpoint for a quic answer and connect the QuicWire."""
        http = _http_session()
        wire: QuicWire | None = None
        try:
            res = await http.post(
                self._calls_url(),
                params={"model": self.model, "transport": "quic"},
                headers={"Authorization": f"Bearer {self.api_key}"},
                # Never replay the bearer key to wherever a redirect points.
                allow_redirects=False,
            )
            body = await res.text()
            if res.status == 402:
                raise OutOfCreditsError()  # admission gate: terminal, never retried
            if res.status != 201:
                raise TransportFailed(
                    f"POST /v1/realtime/calls?transport=quic -> {res.status}: {body[:200]}"
                )
            try:
                answer = json.loads(body)
            except ValueError as exc:
                raise TransportFailed("calls answer is not JSON") from exc
            wire = QuicWire(answer, http)
            await wire.connect()
            return wire
        except BaseException:
            # Covers CancelledError from the dial budget (asyncio.wait_for),
            # which is a BaseException — else the UDP-blocked path leaks the
            # session and QUIC connection on every auto reconnect.
            if wire is not None:
                await asyncio.shield(wire.close())  # closes http too
            else:
                try:
                    await http.close()
                except Exception:
                    pass
            raise

    async def _dial_webrtc(self) -> RtcWire:
        """Create an RtcWire, run its connect, and clean up on failure."""
        http = _http_session()
        wire = RtcWire()
        try:
            await wire.connect(http, self._calls_url(), self.api_key, self.model)
            return wire
        except BaseException:
            await asyncio.shield(wire.close())
            raise
        finally:
            try:
                await http.close()  # only needed for the SDP POST
            except Exception:
                pass

    async def _dial_ws(self) -> WsWire:
        """The original websocket wire with the §7 dial policy (503/429 ->
        backoff and redial)."""
        attempt = 0
        while True:
            http = _http_session()  # owned by the WsWire once dialed
            try:
                wire = await http.ws_connect(
                    self._realtime_url(),
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    max_msg_size=MAX_INBOUND_MESSAGE_BYTES,
                )
                return WsWire(wire, http)
            except aiohttp.WSServerHandshakeError as exc:
                try:
                    await http.close()
                except Exception:
                    pass
                if exc.status == 402:
                    raise OutOfCreditsError(
                        "the organization is out of credits — top up to continue"
                    ) from exc
                if exc.status in (401, 404):
                    raise SenseUnavailable(
                        f"sense endpoint refused the connection (HTTP {exc.status})"
                    ) from exc
                attempt += 1
                retry_after = exc.headers.get("Retry-After") if exc.status in (429, 503) else None
                await self._backoff(attempt, retry_after, exc)
                if self._closed:
                    raise ProtocolError("session closed while dialing") from None
            except (OSError, asyncio.TimeoutError, aiohttp.ClientError) as exc:
                try:
                    await http.close()
                except Exception:
                    pass
                attempt += 1
                await self._backoff(attempt, None, exc)
                if self._closed:
                    raise ProtocolError("session closed while dialing") from None

    async def _backoff(self, attempt: int, retry_after: str | None, exc: Exception) -> None:
        if attempt > RETRY_MAX_ATTEMPTS:
            raise SenseUnavailable(
                f"sense lane unreachable after {attempt} attempts: {exc}"
            ) from exc
        delay = RETRY_FLOOR_S * 2 ** (attempt - 1)
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        delay = min(delay, RETRY_CAP_S)
        log.warning(
            "[sense] dial %d refused (%s: %s) — retrying in %.0fs",
            attempt,
            type(exc).__name__,
            exc,
            delay,
        )
        await asyncio.sleep(delay)

    async def _connect(self) -> Wire:
        if not self.api_key:
            raise ProtocolError("no API key: pass api_key= or set URUN_API_KEY")
        dialers = {"quic": self._dial_quic, "webrtc": self._dial_webrtc}
        for name in self._transport_order():
            if name == "ws":
                log.info("[sense] transport=ws")
                return await self._dial_ws()
            try:
                wire = await asyncio.wait_for(
                    dialers[name](), timeout=TRANSPORT_DIAL_TIMEOUTS_S[name]
                )
            except Exception as exc:
                if self.transport != "auto":
                    raise
                log.info(
                    "[sense] transport=%s failed (%s: %s) — falling back",
                    name,
                    type(exc).__name__,
                    exc,
                )
                continue
            log.info("[sense] transport=%s live", name)
            return wire
        raise SenseUnavailable("no usable transport (auto order exhausted)")

    async def _send(self, wire, payload: dict) -> None:
        async with self._out_lock:
            await wire.send(payload)

    async def _hello(self, wire) -> None:
        await self._send(wire, self._session_update())
        if self._transcript:
            await self._send(wire, self._text_item(self._transcript))

    async def _open_turn(self, wire, *, audio_pending: bool) -> None:
        # The commit must follow buffered audio — an empty commit is refused
        # (empty_buffer) and the turn opens with no audio bound. Later appends sit in
        # the held buffer and waves answer with NO audio at all. The turn
        # opens on the first media item: [append] -> commit -> response.create;
        # further appends ride live.
        if audio_pending:
            await self._send(wire, {"type": "input_audio_buffer.commit"})
        await self._send(
            wire,
            {
                "type": "response.create",
                "response": {
                    "instructions": json.dumps(self._envelope()),
                    "max_output_tokens": TURN_TOKEN_BUDGET,
                },
            },
        )
        self._turn_open = True

    def _barge_in(self) -> None:
        """ctx cancel / barge-in: preempt the active turn's cadence."""
        wire, loop = self._wire, self._loop
        if wire is None or wire.closed or loop is None or not loop.is_running():
            return
        self._last_activity = time.monotonic()
        loop.create_task(self._send(wire, {"type": "response.cancel"}))

    # -- wave assembly --------------------------------------------------------

    def _protocol_error(self, exc: ProtocolError) -> None:
        self.stats.protocol_errors += 1
        log.error("[sense] PROTOCOL ERROR: %s", exc)

    def _feed_line(self, asm: WaveAssembler, line: str) -> Wave | None:
        try:
            return asm.feed_line(line)
        except ProtocolError as exc:
            self._protocol_error(exc)
            return None

    def _flush(self, asm: WaveAssembler) -> Wave | None:
        try:
            return asm.flush()
        except ProtocolError as exc:
            self._protocol_error(exc)
            return None

    # -- the wire loop ----------------------------------------------------------

    def _ensure_drive(self) -> asyncio.Task | None:
        """Start the ONE session drive (dial + cadence + wave assembly), unless
        it is already running or the session is closed. Whoever needs the
        session alive — :meth:`attach` (so handlers fire without a run loop)
        or :meth:`run` (the wave iterator) — starts it; the second caller is a
        no-op. Waves land in ``_wave_q``; handlers fire in :meth:`_accept`
        either way."""
        if self._drive_task is not None or self._closed:
            return self._drive_task
        self._wake = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        sender = self._loop.create_task(self._sender())
        presence = self._loop.create_task(self._presence_loop())
        self._drive_task = self._loop.create_task(
            self._drive(sender, presence), name="sense-drive"
        )
        return self._drive_task

    async def _drive(self, sender: asyncio.Task, presence: asyncio.Task) -> None:
        """The session's drive: connect, register questions, keep a cadence
        turn open, and assemble waves into ``_wave_q`` (handlers fire in
        :meth:`_accept`). Runs until :meth:`close`; a lane death reconnects
        fresh — paced by the young-death ladder: a wire that lived resets the
        ladder, a wire that died young backs off (jittered exponential,
        bounded) and past the bound the drive ends fatally instead of
        storming the hub; a fatal error (out of credits, the young-death
        bound) surfaces through :meth:`run`."""
        try:
            while not self._closed:
                lane_dead = False
                try:
                    await self._serve_once()
                except _LaneDead:
                    lane_dead = True
                # A connection cycle just ended. A wire that went live and
                # died within YOUNG_WIRE_S is churn (relay/GS restart, an
                # adapter refusing us), not a fresh start: back off.
                young = (
                    self._connected_at is not None
                    and time.monotonic() - self._connected_at < YOUNG_WIRE_S
                )
                if young:
                    self._young_deaths += 1
                    if self._young_deaths >= MAX_CONSECUTIVE_YOUNG_DEATHS:
                        raise SenseUnavailable(
                            f"the wire died young {self._young_deaths}x in a row — "
                            "the session appears ended by the hub"
                        )
                    await self._redial_backoff()
                else:
                    self._young_deaths = 0
                    self._redial_delay = 0.0
                    if lane_dead:
                        # An old wire's death is an explicit session_gone:
                        # reconnect immediately, no backoff (TS parity).
                        log.warning("[sense] session_gone — reconnecting fresh")
        except Exception as exc:
            self._fatal = exc  # surfaces through run(); handlers stop here
        finally:
            await self._reap(sender, presence)

    async def _redial_backoff(self) -> None:
        """Jittered exponential backoff after a young wire death: base 1 s,
        doubling, capped at ``RETRY_CAP_S``; jittered so co-tenants do not
        retry in lockstep."""
        delay = max(self._redial_delay, REDIAL_BASE_S)
        self._redial_delay = min(delay * 2, RETRY_CAP_S)
        delay *= 0.5 + random.random()  # ±50% jitter
        wire = self._wire
        code = getattr(wire, "close_code", None)
        reason = getattr(wire, "close_reason", None)
        log.warning(
            "[sense] wire died young (close_code=%s reason=%r, %d in a row) — redialing in %.1fs",
            code,
            reason,
            self._young_deaths,
            delay,
        )
        await asyncio.sleep(delay)

    async def _serve_once(self) -> None:
        """One connect → hello → wire-loop cycle. Raises ``_LaneDead`` when
        the session is gone (the drive reconnects fresh); any other error
        escapes to :meth:`_drive` as fatal."""
        self._turn_open = False
        wire = await self._connect()
        self._wire = wire
        self._connected_at = time.monotonic()
        self._muted_keepalive.clear()  # new connection: the adapter may speak them now
        self._wake_up()  # the sender may hold queued audio
        try:
            await self._hello(wire)
            asm = WaveAssembler(self.questions)
            asm.on_decision = self._count_decision
            self._line_buf = ""
            async for wave in self._run_wire_loop(asm, wire):
                self._queue_wave(wave)
        finally:
            self._wire = None
            try:
                await wire.close()
            except Exception:
                pass

    async def _reap(self, sender: asyncio.Task, presence: asyncio.Task) -> None:
        """Cancel and settle the drive's support tasks, then send the queue
        sentinel that ends any run() consumer."""
        pumps, self._pumps = self._pumps, []
        for t in pumps:
            t.cancel()
        sender.cancel()
        presence.cancel()
        self._wake = None
        for t in [*pumps, sender, presence]:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._queue_wave(None)  # sentinel: end any run() consumer

    def _queue_wave(self, wave: Wave | None) -> None:
        """Hand a wave to run() consumers (drop-oldest at 512); ``None`` is
        the end sentinel. Handlers have already fired in :meth:`_accept`."""
        if self._wave_q.full():
            try:
                self._wave_q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._wave_q.put_nowait(wave)

    async def run(self) -> AsyncIterator[Wave]:
        """Iterate the session's waves. Starts the ONE session drive if
        :meth:`attach` has not already (attach starts it too, so handler-only
        apps never need run(); whoever is first dials). Stream inputs in with
        :meth:`attach` — the one canonical input path."""
        if self._closed:
            return
        self._ensure_drive()
        while True:
            wave = await self._wave_q.get()
            if wave is None:
                if self._fatal is not None:
                    raise self._fatal
                return
            yield wave

    async def _run_wire_loop(self, asm: WaveAssembler, wire) -> AsyncIterator[Wave]:
        """Consume wire messages until the lane ends; yield assembled waves."""
        while not self._closed:
            msg = await wire.receive()
            if msg is None:  # wire closed
                if wire.close_code == 1011:
                    # A credits cut closes 1011 with the reason carrying the
                    # verdict; that is an end, not a reconnect (never _LaneDead).
                    if getattr(wire, "close_reason", None) in (
                        "insufficient_credits",
                        "credits_exhausted",
                    ):
                        raise OutOfCreditsError()
                    raise _LaneDead()
                return  # stream ended; reconnect below
            try:
                data = json.loads(msg)
            except ValueError:
                data = None
            if isinstance(data, dict) and "type" in data:
                async for wave in self._handle_wire_event(asm, data, wire):
                    yield wave
            else:
                self._line_buf += msg
            self._check_line_buf()
            async for wave in self._drain_line_buf(asm):
                yield wave

    def _check_line_buf(self) -> None:
        """Bound the partial-line buffer: a server that streams without
        newlines is a protocol error, not a reason to grow memory forever."""
        tail = self._line_buf.rsplit("\n", 1)[-1]
        # Cheap char-count pre-check; a str char is at most 4 UTF-8 bytes.
        if len(tail) * 4 > MAX_LINE_BYTES and len(tail.encode("utf-8", "replace")) > MAX_LINE_BYTES:
            self._line_buf = self._line_buf[: len(self._line_buf) - len(tail)]
            self._protocol_error(
                ProtocolError(f"decision line exceeds {MAX_LINE_BYTES} bytes; discarded")
            )

    async def _handle_wire_event(self, asm: WaveAssembler, data: dict, wire) -> AsyncIterator[Wave]:
        """Dispatch one JSON wire event on its ``type``."""
        kind = data["type"]
        if kind in ("response.output_text.delta", "response.text.delta"):
            self._line_buf += str(data.get("delta") or "")
        elif kind in ("response.completed", "response.done", "response.failed"):
            async for wave in self._complete_turn(asm):
                yield wave
            self._turn_open = False
        elif kind == "error":
            await self._handle_wire_error(asm, data, wire)
        # everything else (session.*, response.created, ...) is ignored

    async def _complete_turn(self, asm: WaveAssembler) -> AsyncIterator[Wave]:
        """Flush any pending decision line, then flush the assembler."""
        if self._line_buf.strip():
            wave = self._feed_line(asm, self._line_buf)
            self._line_buf = ""
            if wave is not None:
                self._accept(wave)
                yield wave
        wave = self._flush(asm)
        if wave is not None:
            self._accept(wave)
            yield wave

    async def _handle_wire_error(self, asm: WaveAssembler, data: dict, wire) -> None:
        """Handle an error event from the server. Raises ``_LaneDead`` on session_gone."""
        err = data.get("error") or {}
        code = err.get("code") if isinstance(err, dict) else None
        if code == "insufficient_credits":
            # Terminal (wire contract): ONE error event, then close 1011.
            # Propagates out of run() — no reconnect; only a top-up helps.
            self.stats.errors += 1
            message = err.get("message") if isinstance(err, dict) else None
            raise OutOfCreditsError(
                str(message) if message else "the organization is out of credits — top up to continue"
            )
        if code in ("empty_buffer", "audio_input_transcript_unavailable"):
            return  # benign (§5.1, §7): no ASR leg; the model still hears audio
        if code == "buffer_full":
            await self._send(wire, {"type": "input_audio_buffer.commit"})
            return
        if code == "session_gone":
            raise _LaneDead()
        if code == "unknown_event":
            # An adapter that doesn't speak our keepalive events rejects each
            # one once and only once matters: the first rejection mutes that
            # event type for the rest of the connection. Benign — liveness
            # still comes from audio frames — so: debug log, no stats.errors,
            # no turn reopen, and the muted type is never sent again.
            message = err.get("message") if isinstance(err, dict) else ""
            match = _UNKNOWN_EVENT_RE.search(str(message))
            event = match.group(1) if match is not None else None
            if event is not None and event in _KEEPALIVE_EVENTS:
                self._muted_keepalive.add(event)
                log.debug(
                    "[sense] adapter does not support keepalive event %s; muted for this connection",
                    event,
                )
                return
        if code == "response_in_progress":
            # A cancelled cadence turn the server never settles bricks the
            # lane: every reopened turn is refused with response_in_progress
            # and commits bind nothing (empty_buffer) — TS self-heals by
            # dropping the wire after 3 refusals; here a single refusal
            # reconnects fresh, which is the same recovery one step sooner
            # and keeps the loop from hammering response.create (no 2 s
            # backoff on this lane).
            raise _LaneDead()
        self.stats.errors += 1
        log.warning("[sense] lane error: %s", err)
        self._turn_open = False

    async def _drain_line_buf(self, asm: WaveAssembler) -> AsyncIterator[Wave]:
        """Split complete newline-delimited lines off the buffer, yield waves."""
        while "\n" in self._line_buf:
            line, self._line_buf = self._line_buf.split("\n", 1)
            wave = self._feed_line(asm, line)
            if wave is not None:
                self._accept(wave)
                yield wave

    def _count_decision(self) -> None:
        self.stats.decisions += 1

    def keepalive(self) -> None:
        """App-level presence assertion: refreshes the activity window without
        a media payload. Call this when the app is alive but sending nothing."""
        self._last_activity = time.monotonic()

    async def _presence_loop(self) -> None:
        """Send ``{"type": "session.presence"}`` every cadence while the app is
        active (inside the idle window); the idle lapse stops the frames."""
        while not self._closed:
            wire = self._wire
            if (
                wire is not None
                and not wire.closed
                and "session.presence" not in self._muted_keepalive
                and time.monotonic() - self._last_activity < PRESENCE_IDLE_S
            ):
                try:
                    await self._send(wire, {"type": "session.presence"})
                except Exception:
                    pass  # a failed presence frame rides the next tick; the wire loop owns reconnects
            await asyncio.sleep(PRESENCE_CADENCE_S)

    async def close(self) -> None:
        """Stop streams, tasks, session: cancel every tracked ctx task
        (firing its ``on_cancel`` hook) BEFORE tearing down the wire, so a
        reply never completes after the app believes the session is dead."""
        self._closed = True
        pumps, self._pumps = self._pumps, []
        for t in pumps:
            t.cancel()
        drive, self._drive_task = self._drive_task, None
        if drive is not None:
            drive.cancel()
        for key in list(self._ctx._tasks):
            self._ctx.cancel(key, barge_in=False)
        wake = self._wake
        if wake is not None:
            wake.set()
        wire, self._wire = self._wire, None
        if wire is not None:
            try:
                await wire.close()
            except Exception:
                pass
        if drive is not None:
            # The drive's own finally reaps pumps/sender/presence and sends
            # the queue sentinel; await it so nothing outlives close().
            try:
                await drive
            except (asyncio.CancelledError, Exception):
                pass

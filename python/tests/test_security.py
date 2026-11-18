"""Security regressions: transport policy, decoder integrity, resource bounds,
and containment of application errors. Offline only (fake wires)."""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
import sense._session as _session_module
from sense import ProtocolError, Question, QuestionKind, Sense, Session
from sense._core import Context, Wave, WaveAssembler
from sense._session import MAX_PENDING_AUDIO_CHUNKS, _http_session
from sense._transport import (
    MAX_LINE_BYTES,
    MAX_PENDING_MESSAGES,
    QuicWire,
    RtcWire,
    TransportFailed,
    _WtProtocol,
    check_base_url,
)
from yarl import URL

GOOD = {
    "cheese": {"choice": True, "probabilities": {"true": 0.9, "false": 0.1}, "confidence": 0.9},
    "kind": {"choice": "brie", "probabilities": {"brie": 0.9, "feta": 0.1}, "confidence": 0.9},
    "feel": {"probabilities": {"0": 0.2, "1": 0.8}, "confidence": 0.8},
}


def _session() -> Session:
    s = Session(api_key="k")
    s.set_questions(
        [
            Question(name="cheese", kind=QuestionKind.NOUL, instructions="cheese?"),
            Question(
                name="kind",
                kind=QuestionKind.CHOICE,
                instructions="kind?",
                criteria={"brie": "Soft white-rind cheese", "feta": "Crumbly brined cheese"},
            ),
            Question(name="feel", kind=QuestionKind.SCORE, instructions="feel?", criteria=["lo", "hi"]),
        ]
    )
    return s


def _decode(**overrides):
    s = _session()
    return Wave.decode(s.questions, {"wave_id": 0, "t_ms": 0, "results": {**GOOD, **overrides}})


class _FakeWire:
    closed = False
    close_code = None

    def __init__(self, msgs):
        self.msgs = list(msgs)

    async def send(self, payload):
        pass

    async def receive(self):
        return self.msgs.pop(0) if self.msgs else None

    async def close(self):
        pass


def _delta(text: str) -> str:
    return json.dumps({"type": "response.output_text.delta", "delta": text})


def _wave_lines(**overrides) -> list[str]:
    answers = {**GOOD, **overrides}
    lines = [
        _delta(json.dumps({"event": "sense.decision", "id": qid, **a}) + "\n")
        for qid, a in answers.items()
    ]
    return [*lines, json.dumps({"type": "response.completed"})]


async def _drive(s: Session, msgs: list[str]) -> list[Wave]:
    asm = WaveAssembler(s.questions)
    s._line_buf = ""
    return [w async for w in s._run_wire_loop(asm, _FakeWire(msgs))]


# -- base URL policy ----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example",
        "wss://api.example",
        "http://127.0.0.1:9",
        "ws://localhost:9",
        "http://[::1]:9",
    ],
)
def test_secure_and_loopback_base_urls_accepted(url):
    assert check_base_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example",
        "ws://10.0.0.5",
        "https://user:pw@api.example",
        "ftp://api.example",
        "api.example",
    ],
)
def test_insecure_or_malformed_base_urls_refused(url):
    with pytest.raises(ValueError):
        check_base_url(url)


def test_allow_insecure_is_explicit_on_session_and_facade():
    with pytest.raises(ValueError):
        Session(api_key="k", base_url="http://api.example")
    assert Session(api_key="k", base_url="http://api.example", allow_insecure=True).base_url
    assert Sense(api_key="k", base_url="http://api.example", allow_insecure=True).session()


def test_realtime_url_swaps_only_the_scheme_and_encodes_the_model():
    s = Session(api_key="k", base_url="https://h.example/x", model="m:1 &evil=1")
    assert s._realtime_url() == "wss://h.example/x/v1/realtime?model=m:1%20%26evil%3D1"


# -- QUIC calls answer ---------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        None,
        [],
        {"url": "https://e", "token": "t"},
        {"url": 7, "token": "t", "callId": "c"},
        {"url": "https://e", "token": "t", "callId": "c", "certHashes": "abc"},
    ],
)
def test_malformed_calls_answer_refused_before_dialing(answer):
    with pytest.raises(TransportFailed):
        QuicWire(answer, http=None)._validate_answer()


# -- decoder integrity ---------------------------------------------------------


def test_well_formed_wave_still_decodes():
    assert _decode().result("kind").value == "brie"


@pytest.mark.parametrize(
    "override",
    [
        {"cheese": {"choice": True, "probabilities": {"true": 0.9}, "confidence": "high"}},
        {"cheese": {"choice": True, "probabilities": [0.9], "confidence": 1}},
        {"cheese": {"choice": True, "probabilities": {"true": "nan"}, "confidence": 1}},
        {"cheese": {"choice": True, "probabilities": {"true": 7}, "confidence": 1}},
        {"cheese": {"choice": True, "probabilities": {"true": 0.9}, "confidence": float("inf")}},
        {"kind": {"choice": "<img src=x onerror=alert(1)>", "probabilities": {}, "confidence": 1}},
        {"feel": {"probabilities": {"0": 1}, "confidence": 1}},
        {"cheese": "not an object"},
        {"cheese": {"choice": True, "probabilities": {"true": 0.9, "false": 7}, "confidence": 1}},
        {"feel": {"probabilities": {"0": 0.2, "+1": 0.8}, "confidence": 1}},
        {
            "cheese": {
                "choice": True,
                "probabilities": {"true": 0.9, "false": 10**400},
                "confidence": 1,
            }
        },
        {"cheese": {"choice": True, "probabilities": [], "noul": 0.9, "confidence": 1}},
        {"cheese": {"choice": True, "probabilities": "", "noul": 0.9, "confidence": 1}},
    ],
)
def test_malformed_answers_are_protocol_errors(override):
    with pytest.raises(ProtocolError):
        _decode(**override)


def test_malformed_wave_on_the_wire_is_counted_and_the_session_survives():
    s = _session()
    bad = _wave_lines(kind={"choice": "evil", "probabilities": {}, "confidence": 1})
    waves = asyncio.run(_drive(s, bad + _wave_lines()))
    assert s.stats.protocol_errors == 1
    assert len(waves) == 1


def test_infinite_t_ms_is_a_protocol_error():
    with pytest.raises(ProtocolError):
        Wave.decode(_session().questions, {"wave_id": 0, "t_ms": float("inf"), "results": GOOD})


def test_huge_score_int_falls_back_to_the_ordinal():
    """score=10**400 cannot become a float; the ordinal is used, not a crash."""
    s = _session()
    huge = {"feel": {"probabilities": {"0": 0.2, "1": 0.8}, "confidence": 0.8, "score": 10**400}}
    waves = asyncio.run(_drive(s, _wave_lines(**huge)))
    assert len(waves) == 1
    assert waves[0].result("feel").value == pytest.approx(0.8)


def test_falsey_probabilities_on_the_wire_is_a_protocol_error():
    """[]/""/0 must reach from_wire's non-object check, not become {} (TS parity)."""
    s = _session()
    bad = _wave_lines(kind={"choice": "brie", "probabilities": [], "confidence": 0.9})
    waves = asyncio.run(_drive(s, bad + _wave_lines()))
    assert s.stats.protocol_errors == 1
    assert len(waves) == 1


# -- resource bounds -----------------------------------------------------------


def test_newline_less_stream_does_not_grow_the_line_buffer():
    s = _session()
    chunk = _delta("A" * (MAX_LINE_BYTES // 2))
    asyncio.run(_drive(s, [chunk] * 20))
    assert len(s._line_buf) <= MAX_LINE_BYTES
    assert s.stats.protocol_errors >= 1


def test_outbound_text_backlog_is_bounded():
    s = _session()
    for i in range(1000):
        s._text_in.push(str(i), mode="event")
    assert len(s._text_out) == 256


@pytest.mark.parametrize(
    "host", ["127.0.0.²", "127.0.0.256", "127.0.0.1.evil.example", "localhost.evil.example"]
)
def test_lookalike_loopback_hosts_are_not_trusted(host):
    with pytest.raises(ValueError):
        check_base_url(f"http://{host}:9")


def test_outbound_audio_backlog_is_bounded():
    s = _session()
    for _ in range(MAX_PENDING_AUDIO_CHUNKS + 100):
        s._audio_in.push(b"\x00\x00")
    assert len(s._audio["default"]) == MAX_PENDING_AUDIO_CHUNKS


def _wt_probe() -> _WtProtocol:
    p = object.__new__(_WtProtocol)
    p._rx = b""
    p._ctrl_rx = bytearray()
    p._ctrl_dead = False
    p.lines = asyncio.Queue()
    return p


def test_quic_events_line_overflow_ends_the_leg():
    p = _wt_probe()
    p._on_events(b"x" * (MAX_LINE_BYTES + 1))
    assert p._rx == b""
    assert p.lines.get_nowait() is None
    p._on_events(b'{"ok":1}\n')  # a dead leg queues nothing further
    assert p.lines.empty()


def test_quic_events_queue_overflow_ends_the_leg():
    p = _wt_probe()
    p._on_events(b"{}\n" * (MAX_PENDING_MESSAGES + 10))
    items = [p.lines.get_nowait() for _ in range(p.lines.qsize())]
    assert items[-1] is None
    assert len(items) == MAX_PENDING_MESSAGES + 1


def test_line_cap_counts_utf8_bytes_not_characters():
    s = _session()
    # 70k four-byte characters: 280 KB of UTF-8 in under MAX_LINE_BYTES chars.
    asyncio.run(_drive(s, [_delta("\U0001f9c0" * 70_000)]))
    assert s._line_buf == ""
    assert s.stats.protocol_errors == 1


def test_webrtc_oversize_message_ends_the_wire():
    w = RtcWire()
    w._on_dc_message("x" * (5 * 1024 * 1024))
    assert w.closed
    assert w._msgs.get_nowait() is None


# -- application error containment ---------------------------------------------


def test_raising_predicate_does_not_end_the_session():
    s = _session()
    kind = s.questions[1]
    fired = []
    s._ctx.when(kind.p("missing-option") > 0.5, lambda ctx: None)
    s._on_wave(lambda ctx: fired.append(1))
    waves = asyncio.run(_drive(s, _wave_lines()))
    assert len(waves) == 1
    assert fired == [1]


def test_context_predicate_error_counts_as_false():
    ctx = Context()
    calls = []

    def boom(_w):
        raise KeyError("nope")

    ctx.when(boom, lambda c: calls.append(1))
    ctx._ingest(_decode())
    assert calls == []


# -- dial policy ----------------------------------------------------------------


def test_http_session_never_follows_redirects(monkeypatch):
    """ws_connect() cannot disable redirects, so the session refuses them
    globally: a 3xx upgrade must never replay the bearer (TS parity)."""
    captured = {}

    class _DummyResponse:
        pass

    async def spy(_self, method, str_or_url, **kwargs):
        captured.update(kwargs)
        return _DummyResponse()

    monkeypatch.setattr(aiohttp.ClientSession, "_request", spy)

    async def scenario():
        s = _http_session()
        try:
            await s._request("GET", "https://example.invalid/")
        finally:
            await s.close()

    asyncio.run(scenario())
    assert captured["allow_redirects"] is False


def test_dial_backoff_stops_once_the_session_is_closed(monkeypatch):
    """close() during the dial backoff ends dialing: no further bearer dials."""
    s = _session()
    attempts = {"n": 0}

    class _RefusingHttp:
        async def ws_connect(self, url, **kwargs):
            attempts["n"] += 1
            raise aiohttp.WSServerHandshakeError(
                request_info=aiohttp.RequestInfo(
                    url=URL("wss://example.invalid/v1/realtime"),
                    method="GET",
                    headers={},
                    real_url=URL("wss://example.invalid/v1/realtime"),
                ),
                history=(),
                status=503,
                message="busy",
                headers={},
            )

        async def close(self):
            pass

    monkeypatch.setattr(_session_module, "_http_session", lambda: _RefusingHttp())
    monkeypatch.setattr(_session_module, "RETRY_FLOOR_S", 0.01)
    monkeypatch.setattr(_session_module, "RETRY_CAP_S", 0.02)

    async def scenario():
        dial = asyncio.ensure_future(s._dial_ws())
        await asyncio.sleep(0.05)  # at least one refusal and its backoff
        assert attempts["n"] >= 1
        await s.close()
        n_at_close = attempts["n"]
        with pytest.raises(ProtocolError):
            await dial
        await asyncio.sleep(0.05)
        assert attempts["n"] == n_at_close

    asyncio.run(scenario())

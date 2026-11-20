"""Offline tests for the sense SDK, plus a fake-lane wire e2e over aiohttp."""

import asyncio
import json

import pytest
from aiohttp import WSMsgType, web
from sense import (
    Context,
    OutOfCreditsError,
    Predicate,
    ProtocolError,
    Question,
    QuestionKind,
    Score,
    Sense,
    Session,
    Wave,
    session_event,
)


def questions():
    return [
        Question(name="finished", kind=QuestionKind.NOUL, instructions="finished?"),
        Question(
            name="intent",
            kind=QuestionKind.CHOICE,
            instructions="want?",
            criteria={"support": "Technical help", "sales": "Buying questions", "other": "Anything else"},
        ),
        Question(
            name="urgency",
            kind=QuestionKind.SCORE,
            instructions="urgent?",
            criteria=["routine, can wait", "time sensitive today", "blocking, act now"],
        ),
    ]


def payload(wave_id, t_ms, **overrides):
    results = {
        "finished": {
            "kind": "noul",
            "choice": "true",
            "probabilities": {"true": 0.9, "false": 0.1},
            "confidence": 0.9,
        },
        "intent": {
            "kind": "choice",
            "choice": "support",
            "probabilities": {"support": 0.7, "sales": 0.1, "other": 0.2},
            "confidence": 0.7,
        },
        "urgency": {
            "kind": "score",
            "probabilities": {"0": 0.1, "1": 0.4, "2": 0.5},
            "confidence": 0.8,
        },
    }
    results.update(overrides)
    return {"wave_id": wave_id, "t_ms": t_ms, "results": results}


# ---------------------------------------------------------------------------
# decoding
# ---------------------------------------------------------------------------


def test_noul_normalizes_string_and_bool_choices():
    w = Wave.decode(questions(), payload(0, 0))
    assert w.result("finished").value is True
    assert w.result("finished").p == 0.9
    p2 = payload(1, 40)
    p2["results"]["finished"] = {
        "kind": "noul",
        "choice": False,
        "probabilities": {"true": 0.2, "false": 0.8},
        "confidence": 0.5,
    }
    w2 = Wave.decode(questions(), p2)
    assert w2.result("finished").value is False


def test_noul_bogus_choice_is_protocol_error():
    bad = payload(0, 0)
    bad["results"]["finished"] = {
        "kind": "noul",
        "choice": None,
        "probabilities": {},
        "confidence": None,
    }
    with pytest.raises(ProtocolError):
        Wave.decode(questions(), bad)


def test_score_semantics():
    w = Wave.decode(questions(), payload(3, 120))
    u = w.result("urgency")
    assert isinstance(u, Score)
    assert u.value == pytest.approx(0.4 + 2 * 0.5)  # expected ordinal
    assert u.top == u.level == "blocking, act now"  # the level's description
    assert u.p_at_least("time sensitive today") == pytest.approx(0.9)  # p1 + p2
    assert u.p_at_least(2) == pytest.approx(0.5)  # index form
    assert u.p_at_least(0) == pytest.approx(1.0)
    with pytest.raises(ProtocolError):
        u.p_at_least("nope")


def test_missing_and_unknown_raise():
    p = payload(0, 0)
    del p["results"]["intent"]
    with pytest.raises(ProtocolError):
        Wave.decode(questions(), p)
    p2 = payload(0, 0)
    p2["results"]["bogus"] = {"kind": "noul", "choice": True}
    with pytest.raises(ProtocolError):
        Wave.decode(questions(), p2)


def test_result_lookup_unknown_qid():
    w = Wave.decode(questions(), payload(0, 0))
    with pytest.raises(ProtocolError):
        w.result("nope")


# ---------------------------------------------------------------------------
# predicates
# ---------------------------------------------------------------------------


def wave_list():
    return [
        Wave.decode(questions(), payload(i, i * 40, **alt))
        for i, alt in enumerate([{}, {}, {}, {}])
    ]


def test_question_sugar_builds_predicates():
    finished, intent = questions()[:2]
    ws = wave_list()
    pred = finished & (intent == "support")
    assert all(pred(w) for w in ws)
    assert not any((~finished)(w) for w in ws)
    assert all((finished.p >= 0.8)(w) for w in ws)
    assert all((intent.p("support") >= 0.7)(w) for w in ws)
    assert not any((intent != "support")(w) for w in ws)


def test_score_predicate_subjects():
    urgency = questions()[2]
    w = Wave.decode(questions(), payload(0, 0))
    assert (urgency.value >= 1.0)(w)
    assert (urgency.top == "blocking, act now")(w)
    assert (urgency.p_at_least("time sensitive today") >= 0.5)(w)
    assert (urgency == "blocking, act now")(w)  # q-subject is the top level text


def test_between_latches():
    pred = Predicate.between(("p_at_least", "urgency", "time sensitive today"), 0.5, 0.3)
    vals = [
        {"0": 0.9, "1": 0.05, "2": 0.05},  # 0.1 below exit -> False
        {"0": 0.4, "1": 0.3, "2": 0.3},  # 0.6 >= enter -> True (latch)
        {"0": 0.6, "1": 0.2, "2": 0.2},  # 0.4 above exit -> stays True
        {"0": 0.9, "1": 0.05, "2": 0.05},  # 0.1 < exit -> False
    ]
    got = []
    for i, probs in enumerate(vals):
        w = Wave.decode(
            questions(),
            payload(i, i, urgency={"kind": "score", "probabilities": probs, "confidence": 0.5}),
        )
        got.append(pred(w))
    assert got == [False, True, True, False]


def test_all_any_not():
    finished, intent = questions()[:2]
    ws = wave_list()
    a = Predicate.all_of(finished, intent.eq("support"))
    b = Predicate.any_of(intent.ne("support"), Predicate(("q", "finished", None)))
    for w in ws:
        assert a(w) and b(w)
    assert not any((~a)(w) for w in ws)


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------


def _feed(ctx, payloads):
    qs = questions()
    for p in payloads:
        ctx._ingest(Wave.decode(qs, p))


def test_context_latest_waves_coalesce():
    ctx = Context()
    payloads = [payload(i, i * 40) for i in range(3)]
    _feed(ctx, payloads)
    assert ctx.latest().wave_id == 2
    assert [w.wave_id for w in ctx.waves()] == [0, 1, 2]
    # all three waves share argmax values -> coalesced to the last
    assert [w.wave_id for w in ctx.waves(coalesce=True)] == [2]


def test_when_edges_and_cancel():
    ctx = Context()
    fired = []
    handle = ctx.when(
        Predicate(("q", "finished", None)), lambda c: fired.append(c.latest().wave_id)
    )
    off = payload(1, 40)
    off["results"]["finished"] = {
        "kind": "noul",
        "choice": "false",
        "probabilities": {"true": 0.2, "false": 0.8},
        "confidence": 0.8,
    }
    payloads = [payload(0, 0), off, payload(2, 80)]
    _feed(ctx, payloads)
    assert fired == [0, 2]  # true, false, true
    handle.cancel()
    fired.clear()
    _feed(ctx, payloads)
    assert fired == []


def test_onwave_and_get():
    ctx = Context()
    seen = []
    ctx.on_wave(lambda c: seen.append(c.latest().wave_id))
    _feed(ctx, [payload(0, 0), payload(1, 40)])
    assert seen == [0, 1]
    assert ctx.get("intent").value == "support"


def test_context_run_tasks():
    async def go():
        ctx = Context()
        ran = []
        t = ctx.run("job", lambda ctx: _coro(ran, "a"))
        await asyncio.sleep(0)
        with pytest.raises(ValueError):
            ctx.run("job", lambda ctx: _coro(ran, "b"))
        ctx.run("job", lambda ctx: _coro(ran, "b"), replace=True)
        ctx.cancel("job")
        for _ in range(3):
            await asyncio.sleep(0)
        assert t.cancelled()

    async def _coro(ran, tag):
        ran.append(tag)
        await asyncio.sleep(10)

    asyncio.run(go())


def test_session_event():
    ev = session_event({"type": "session.created"})
    assert ev.type == "session.created"
    ev = session_event({"type": "error", "code": "bad_question", "message": "no"})
    assert ev.code == "bad_question" and ev.message == "no"


# ---------------------------------------------------------------------------
# facade + envelope
# ---------------------------------------------------------------------------


def test_facade_registers_questions():
    app = Sense(api_key="k")
    app.noul("finished", instructions="finished?")
    app.choice("intent", instructions="want?", criteria={"support": "Technical help", "other": "Anything else"})
    app.score("urgency", instructions="urgent?", criteria=["routine, can wait", "blocking, act now"])
    s = app.session()
    assert [q.name for q in s.questions] == ["finished", "intent", "urgency"]
    env = s._envelope()
    assert env["questions"]["finished"] == {"type": "noul", "instructions": "finished?"}
    assert env["questions"]["intent"]["criteria"] == {
        "support": "Technical help",
        "other": "Anything else",
    }
    assert env["questions"]["urgency"]["criteria"] == ["routine, can wait", "blocking, act now"]
    assert callable(Predicate(lambda w: True))


def test_jev_shape_only():
    # No invented syntax: no id=, no auto-id, no bare option lists. The name
    # is the answer key; instructions and criteria are Jev's fields.
    app = Sense(api_key="k")
    with pytest.raises(TypeError):
        app.noul("finished?", id="finished")
    with pytest.raises(ValueError):
        app.choice("intent", instructions="want?", criteria=["support", "other"])
    # a two-level description array IS Jev's score shape; a map is not
    with pytest.raises(ValueError):
        app.score("urgency", instructions="urgent?", criteria={"low": "label", "high": "label"})
    with pytest.raises(ValueError):
        app.choice("intent", instructions="want?", criteria={"support": "", "other": "fine"})
    with pytest.raises(ValueError):
        app.noul("n", instructions="")
    with pytest.raises(ValueError):
        app.noul("", instructions="empty name?")
    # no id-first overload remains
    import inspect

    for name in ("noul", "choice", "score"):
        params = [
            p
            for p in inspect.signature(getattr(Sense, name)).parameters.values()
            if p.name != "self"
        ]
        assert params[0].name == "name" and params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        assert all(
            p.name in ("instructions", "criteria") and p.kind is inspect.Parameter.KEYWORD_ONLY
            for p in params[1:]
        )


def test_when_decorator_returns_fn():
    app = Sense(api_key="k")

    @app.when(Predicate(lambda w: True))
    async def handler(ctx):
        return 1

    assert callable(handler)
    assert not isinstance(handler, object.__class__) and handler.__name__ == "handler"


def test_session_defaults():
    s = Session(api_key="sk-test")
    assert s.questions == []
    assert not s._closed
    assert s.base_url == Session.DEFAULT_BASE_URL
    assert s.hz == 12.5
    q = Question(name="finished", kind=QuestionKind.NOUL, instructions="did they finish?")
    s.set_questions([q])
    assert s.questions == [q]
    assert s._realtime_url() == "wss://inference.urun.sh/v1/realtime?model=sense-latest:bf16"


def test_hz_ceiling():
    # 12.5 is the default AND the ceiling: one seat's sustained floor.
    with pytest.raises(ValueError):
        Session(api_key="sk-test", hz=25.0)
    with pytest.raises(ValueError):
        Session(api_key="sk-test", hz=13)
    with pytest.raises(ValueError):
        Session(api_key="sk-test", hz=0)
    with pytest.raises(ValueError):
        Session(api_key="sk-test", hz=float("nan"))
    assert Session(api_key="sk-test", hz=12.5).hz == 12.5
    assert Sense(hz=2).session().hz == 2.0


# ---------------------------------------------------------------------------
# ruling 2: score comparison rule (bare question subject)
# ---------------------------------------------------------------------------


def test_score_bare_comparison_rule():
    urgency = questions()[2]
    w = Wave.decode(questions(), payload(0, 0))  # top="blocking, act now", value=1.4
    # eq/ne compare the TOP LEVEL (the level's description text)
    assert (urgency == "blocking, act now")(w)
    assert not (urgency == "routine, can wait")(w)
    assert (urgency != "routine, can wait")(w)
    # gt/gte/lt/lte compare the EXPECTED ORDINAL
    assert (urgency >= 1.0)(w)
    assert not (urgency >= 2.0)(w)
    assert (urgency < 2.0)(w)
    assert not (urgency > 1.4)(w)
    # gte on a string ordinal is not the name comparison
    with pytest.raises(TypeError):
        (urgency >= "blocking, act now")(w)


def test_question_vs_question_is_identity():
    q1, q2 = questions()[0], questions()[1]
    assert (q1 == q2) is False
    assert (q1 == q1) is True
    assert (q1 != q2) is True
    assert q1 in [q1] and q2 not in [q1]  # membership uses identity now
    assert hash(q1) == hash("finished")


# ---------------------------------------------------------------------------
# nits: registration validation, duplicate names, signal, history bound
# ---------------------------------------------------------------------------


def test_set_questions_replaces_duplicate():
    s = Session(api_key="sk-test")
    q1 = Question(name="finished", kind=QuestionKind.NOUL, instructions="a?")
    s.set_questions([q1])
    q2 = Question(name="finished", kind=QuestionKind.NOUL, instructions="b?")
    s.set_questions([q2])
    assert len(s.questions) == 1 and s.questions[0] is q2


def test_score_levels_validated_2_to_10():
    with pytest.raises(ValueError):
        Question(name="u", kind=QuestionKind.SCORE, instructions="u?", criteria=["only"])
    with pytest.raises(ValueError):
        Question(
            name="u",
            kind=QuestionKind.SCORE,
            instructions="u?",
            criteria=[str(i) for i in range(11)],
        )
    Question(name="u", kind=QuestionKind.SCORE, instructions="u?", criteria=["low", "high"])  # ok
    with pytest.raises(ValueError):
        Question(name="v", kind=QuestionKind.SCORE, instructions="v?", criteria=["one"])


def test_context_run_signal():
    async def go():
        ctx = Context()
        assert not ctx.signal.is_set()
        task = ctx.run("job", lambda ctx: asyncio.sleep(10))
        sig = ctx.signal
        assert sig is ctx.signal
        ctx.cancel("job")
        for _ in range(3):
            await asyncio.sleep(0)
        assert task.cancelled()
        assert sig.is_set()

    asyncio.run(go())


def test_cancel_barge_in_flag():
    class FakeSession:
        def __init__(self):
            self.barges = 0

        def _barge_in(self):
            self.barges += 1

    async def go():
        fs = FakeSession()
        ctx = Context(session=fs)
        t1 = ctx.run("reply", lambda ctx: asyncio.sleep(10))
        t2 = ctx.run("draft", lambda ctx: asyncio.sleep(10))
        # supersede: no barge-in
        ctx.run("draft", lambda ctx: asyncio.sleep(10), replace=True)
        for _ in range(3):
            await asyncio.sleep(0)
        assert fs.barges == 0 and t2.cancelled()
        # explicit cancel with the flag off: no barge-in
        ctx.cancel("reply", barge_in=False)
        for _ in range(3):
            await asyncio.sleep(0)
        assert fs.barges == 0 and t1.cancelled()
        # default cancel: barge-in fires
        ctx.run("other", lambda ctx: asyncio.sleep(10))
        ctx.cancel("other")
        assert fs.barges == 1

    asyncio.run(go())


def test_history_bounded_512():
    ctx = Context()
    for i in range(600):
        ctx._ingest(Wave.decode(questions(), payload(i, i)))
    assert len(ctx.waves()) == 512
    assert ctx.latest().wave_id == 599
    assert ctx.waves()[0].wave_id == 88  # 600 - 512


def test_session_waves_gone_run_is_the_one_iterator():
    # One wave-iteration path: session.run(). The old live `waves()`
    # subscription was a second spelling and is deleted.
    assert not hasattr(Session(api_key="sk-test"), "waves")
    assert not hasattr(Session(api_key="sk-test"), "latest")
    assert not hasattr(Session(api_key="sk-test"), "when")
    assert not hasattr(Session(api_key="sk-test"), "on_wave")
    assert not hasattr(Sense, "predicate")
    assert not hasattr(Session(api_key="sk-test"), "audio")
    assert not hasattr(Session(api_key="sk-test"), "video")
    assert not hasattr(Session(api_key="sk-test"), "text")
    assert not hasattr(Session(api_key="sk-test"), "connect")


# ---------------------------------------------------------------------------
# per-source waves (client-side degradation)
# ---------------------------------------------------------------------------


def test_per_source_labeling_and_filters():
    s = Session(api_key="sk-test")
    _register(s)
    w0 = Wave.decode(questions(), payload(0, 0))
    w1 = Wave.decode(questions(), payload(1, 40))
    # simulate: default drained, then mic drained
    s._active_source = "default"
    s._accept(w0)
    s._active_source = "mic"
    s._accept(w1)
    assert s._ctx.latest("mic").wave_id == 1
    assert s._ctx.latest("default").wave_id == 0
    assert s._ctx.latest().wave_id == 1
    seen = []
    s._ctx.when(
        Predicate(("q", "finished", None)), lambda c: seen.append(c.latest().wave_id), source="mic"
    )
    s._active_source = "default"  # default drained again: next wave labels default
    s._accept(Wave.decode(questions(), payload(2, 80)))  # default source: mic when must not fire
    assert seen == []
    s._active_source = "mic"
    s._accept(Wave.decode(questions(), payload(3, 120)))
    assert seen == [3]
    # server-labeled source wins over client labeling
    labeled = Wave.decode(questions(), dict(payload(4, 160), source="server"))
    s._accept(labeled)
    assert s._ctx.latest("server").wave_id == 4


# ---------------------------------------------------------------------------
# ruling 4: text-only session
# ---------------------------------------------------------------------------


def test_wire_text_only_session():
    async def go():
        state = {"messages": [], "creates": 0}

        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    break
                ev = json.loads(msg.data)
                state["messages"].append(ev["type"])
                if ev["type"] == "response.create":
                    state["creates"] += 1
                    delta = (
                        dec("finished", 0, "true", {"true": 0.9, "false": 0.1}, 0.9)
                        + "\n"
                        + dec("intent", 0, "support", {"support": 0.9, "other": 0.1}, 0.9)
                        + "\n"
                    )
                    await ws.send_json({"type": "response.output_text.delta", "delta": delta})
                    await ws.send_json({"type": "response.completed"})

        runner, port = await _start_fake(handler)
        try:
            s = Session(api_key="sk-test", base_url=f"http://127.0.0.1:{port}")
            _register(s)
            got = []

            task = asyncio.create_task(_consume(s, got))
            # TEXT ONLY: a text push opens and drives the turn, zero audio.
            s._text_in.push("Say something.")
            for _ in range(500):
                if got:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            await s.close()
            assert got, "text-only session must open a turn and yield waves"
            assert "input_audio_buffer.append" not in state["messages"]
            assert "input_audio_buffer.commit" not in state["messages"]
            assert "conversation.item.create" in state["messages"]
            assert "response.create" in state["messages"]
        finally:
            await runner.cleanup()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# prod lax 2026-11-20: a long-lived cadence response ends status=incomplete
# (the member's 300 s response lifetime bound). `response.incomplete` is a
# TERMINAL event — the turn must close so the next input re-opens it, or the
# session goes silent: socket open, audio flowing in, zero waves, no error.
# ---------------------------------------------------------------------------


def test_wire_incomplete_response_reopens_turn():
    async def go():
        state = {"creates": 0}

        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    break
                ev = json.loads(msg.data)
                if ev["type"] == "response.create":
                    state["creates"] += 1
                    delta = dec("finished", 0, "true", {"true": 0.9, "false": 0.1}, 0.9) + "\n"
                    await ws.send_json({"type": "response.output_text.delta", "delta": delta})
                    # The member's response lifetime bound ends the turn visibly
                    # with status incomplete — never a completed/done frame.
                    await ws.send_json({"type": "response.incomplete"})

        runner, port = await _start_fake(handler)
        try:
            s = Session(api_key="sk-test", base_url=f"http://127.0.0.1:{port}")
            _register(s)
            got = []
            task = asyncio.create_task(_consume(s, got))
            s._text_in.push("Say one thing.")
            await _wait_until(lambda: state["creates"] >= 1)
            s._text_in.push("Say another thing.")
            await _wait_until(lambda: state["creates"] >= 2)
            task.cancel()
            await s.close()
            assert state["creates"] >= 2, (
                "a response.incomplete terminal must close the turn so the next "
                f"input re-opens it (got {state['creates']} response.create(s))"
            )
        finally:
            await runner.cleanup()

    asyncio.run(go())


def test_close_cancels_tracked_tasks():
    async def go():
        s = Session(api_key="sk-test")
        cancelled = []
        task = s._ctx.run(
            "reply", lambda ctx: asyncio.sleep(10), on_cancel=lambda: cancelled.append(True)
        )
        await asyncio.sleep(0)
        await s.close()
        for _ in range(3):
            await asyncio.sleep(0)
        assert task.cancelled()
        assert cancelled == [True]

    asyncio.run(go())


# ---------------------------------------------------------------------------
# wire e2e against a fake lane
# ---------------------------------------------------------------------------


def dec(qid, t_ms, choice, probs, conf):
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


async def _start_fake(handler):
    app = web.Application()
    app.router.add_get("/v1/realtime", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, runner.addresses[0][1]


def _register(s):
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


async def _wait_until(condition, attempts=500):
    """Poll condition() until it turns True, sleeping 10ms between attempts."""
    for _ in range(attempts):
        if condition():
            return
        await asyncio.sleep(0.01)


async def _consume(s, got):
    """Collect waves from s.run() into got."""
    async for w in s.run():
        got.append(w)


async def _wire_happy_handler(request, state):
    assert request.headers["Authorization"] == "Bearer sk-test"
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    async for msg in ws:
        if msg.type is not WSMsgType.TEXT:
            break
        ev = json.loads(msg.data)
        state["messages"].append(ev["type"])
        if ev["type"] == "session.update":
            state["update"] = ev
            await ws.send_json({"type": "error", "error": {"code": "empty_buffer"}})  # benign
        elif ev["type"] == "response.create":
            state["creates"] += 1
            if state["creates"] == 1:
                # two waves inside one delta: the repeat qid flushes wave 0
                delta = "\n".join(
                    [
                        dec("finished", 0, "true", {"true": 0.9, "false": 0.1}, 0.9),
                        dec("intent", 0, "other", {"support": 0.3, "other": 0.7}, 0.7),
                        dec("finished", 40, "false", {"true": 0.2, "false": 0.8}, 0.8),
                        dec("intent", 40, "support", {"support": 0.6, "other": 0.4}, 0.6),
                    ]
                )
                await ws.send_json({"type": "response.output_text.delta", "delta": delta + "\n"})
                await ws.send_json({"type": "response.completed"})
            else:
                await ws.send_json(
                    {
                        "type": "response.output_text.delta",
                        "delta": dec("finished", 80, "true", {"true": 0.95, "false": 0.05}, 0.95)
                        + "\n",
                    }
                )
                await ws.send_json(
                    {
                        "type": "response.output_text.delta",
                        "delta": dec("intent", 80, "other", {"support": 0.1, "other": 0.9}, 0.9)
                        + "\n",
                    }
                )
                await ws.send_json({"type": "response.completed"})


async def _wire_reconnect_handler(request, state):
    state["connections"] += 1
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    if state["connections"] == 1:
        async for msg in ws:
            if msg.type is not WSMsgType.TEXT:
                break
            if json.loads(msg.data)["type"] == "response.create":
                await ws.send_json({"type": "error", "error": {"code": "session_gone"}})
                await ws.close(code=1011)
                return
    else:
        async for msg in ws:
            if msg.type is not WSMsgType.TEXT:
                break
            if json.loads(msg.data)["type"] == "response.create":
                delta = (
                    dec("finished", 0, "true", {"true": 1.0, "false": 0.0}, 1.0)
                    + "\n"
                    + dec("intent", 0, "support", {"support": 1.0, "other": 0.0}, 1.0)
                    + "\n"
                )
                await ws.send_json({"type": "response.output_text.delta", "delta": delta})
                await ws.send_json({"type": "response.completed"})


def test_wire_happy_path():
    async def go():
        state = {"messages": [], "update": None, "creates": 0}

        async def handler(request):
            return await _wire_happy_handler(request, state)

        runner, port = await _start_fake(handler)
        try:
            s = Session(api_key="sk-test", base_url=f"http://127.0.0.1:{port}", hz=12.5)
            _register(s)
            fired = []
            s._ctx.when(Predicate(("q", "finished", None)), lambda c: fired.append(c.latest().wave_id))
            got = []
            task = asyncio.create_task(_consume(s, got))
            # Mic-style: each push drives the turn lifecycle. Turn 1 (waves
            # 0+1) opens on the first push; the second push opens turn 2.
            s._audio_in.push(b"\x00\x00" * 48)
            await _wait_until(lambda: len(got) >= 2)
            s._audio_in.push(b"\x00\x00" * 48)
            await _wait_until(lambda: len(got) >= 3)
            task.cancel()
            await s.close()
            assert "input_audio_buffer.append" in state["messages"]
            sess = state["update"]["session"]
            assert sess["model"] == "sense-latest:bf16" and sess["modalities"] == ["text"]
            env = json.loads(sess["instructions"])
            assert env["hz"] == 12.5 and set(env["questions"]) == {"finished", "intent"}
            assert [w.wave_id for w in got] == [0, 1, 2]
            assert got[0].t_ms == 0 and got[1].t_ms == 40 and got[2].t_ms == 80
            assert got[0].result("finished").value is True
            assert got[0].result("intent").value == "other"
            assert got[1].result("finished").value is False
            assert fired == [0, 2]
            assert s.stats.waves == 3 and s.stats.decisions == 6
            assert s.stats.errors == 0 and s.stats.protocol_errors == 0
        finally:
            await runner.cleanup()

    asyncio.run(go())


def test_wave_t_ms_prefers_the_media_position():
    """The wave's t_ms is the decision's MEDIA timestamp (protocol §6):
    audio_t_ms — the position the wave decided on — wins over a t_ms that
    older seats still stamp with per-wave compute time."""

    async def go():
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    break
                ev = json.loads(msg.data)
                if ev["type"] == "session.update":
                    await ws.send_json({"type": "error", "error": {"code": "empty_buffer"}})
                elif ev["type"] == "response.create":
                    lines = []
                    for qid, choice, probs in (
                        ("finished", "true", {"true": 0.9, "false": 0.1}),
                        ("intent", "other", {"support": 0.2, "other": 0.8}),
                    ):
                        line = json.loads(dec(qid, 54, choice, probs, 0.9))
                        line["audio_t_ms"] = 500  # the media position
                        lines.append(json.dumps(line))
                    await ws.send_json({"type": "response.output_text.delta", "delta": "\n".join(lines) + "\n"})
                    await ws.send_json({"type": "response.completed"})
            return ws

        runner, port = await _start_fake(handler)
        try:
            s = Session(api_key="sk-test", base_url=f"http://127.0.0.1:{port}")
            _register(s)
            got = []
            task = asyncio.create_task(_consume(s, got))
            s._audio_in.push(b"\x00\x00" * 48)
            await _wait_until(lambda: len(got) >= 1)
            task.cancel()
            await s.close()
            assert got[0].t_ms == 500
        finally:
            await runner.cleanup()

    asyncio.run(go())


def test_wire_incomplete_wave_is_protocol_error():
    async def go():
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    break
                ev = json.loads(msg.data)
                if ev["type"] == "response.create":
                    # missing `intent`: incomplete wave must never yield
                    await ws.send_json(
                        {
                            "type": "response.output_text.delta",
                            "delta": dec("finished", 0, "true", {"true": 0.9, "false": 0.1}, 0.9)
                            + "\n",
                        }
                    )
                    await ws.send_json({"type": "response.completed"})

        runner, port = await _start_fake(handler)
        try:
            s = Session(api_key="sk-test", base_url=f"http://127.0.0.1:{port}")
            _register(s)
            got = []

            task = asyncio.create_task(_consume(s, got))
            s._audio_in.push(b"\x00\x00" * 48)  # the push opens the turn
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except asyncio.TimeoutError:
                pass
            await s.close()
            assert got == []
            assert s.stats.protocol_errors >= 1
        finally:
            await runner.cleanup()

    asyncio.run(go())


def test_wire_session_gone_reconnects_fresh():
    async def go():
        state = {"connections": 0}

        async def handler(request):
            return await _wire_reconnect_handler(request, state)

        runner, port = await _start_fake(handler)
        try:
            s = Session(api_key="sk-test", base_url=f"http://127.0.0.1:{port}")
            _register(s)
            got = []
            task = asyncio.create_task(_consume(s, got))
            # First push opens a turn on connection 1; the lane dies with
            # session_gone, the SDK reconnects fresh, and a second push
            # opens the turn on connection 2.
            s._audio_in.push(b"\x00\x00" * 48)
            await asyncio.sleep(0.3)
            s._audio_in.push(b"\x00\x00" * 48)
            await _wait_until(lambda: len(got) >= 1)
            task.cancel()
            await s.close()
            assert state["connections"] == 2
            # fresh session: wave ids restart at 0
            assert got[0].wave_id == 0
        finally:
            await runner.cleanup()

    asyncio.run(go())


def test_wire_insufficient_credits_is_terminal():
    async def go():
        state = {"connections": 0}

        async def handler(request):
            state["connections"] += 1
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    break
                if json.loads(msg.data)["type"] == "response.create":
                    await ws.send_json(
                        {
                            "type": "error",
                            "error": {
                                "code": "insufficient_credits",
                                "message": "the organization is out of credits — top up to continue",
                            },
                        }
                    )
                    await ws.close(code=1011)
                    return

        runner, port = await _start_fake(handler)
        try:
            s = Session(api_key="sk-test", base_url=f"http://127.0.0.1:{port}")
            _register(s)
            got = []
            task = asyncio.create_task(_consume(s, got))
            s._audio_in.push(b"\x00\x00" * 48)  # the push opens the turn
            with pytest.raises(OutOfCreditsError):
                await asyncio.wait_for(task, timeout=2.0)
            # The cut is an end, not churn: no reconnect dial may follow.
            await asyncio.sleep(0.3)
            assert state["connections"] == 1
        finally:
            await runner.cleanup()

    asyncio.run(go())


def test_wire_close_reason_credits_exhausted_is_terminal():
    async def go():
        state = {"connections": 0}

        async def handler(request):
            state["connections"] += 1
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            # No error event: the extend/admission rail just cuts the lane.
            await ws.close(code=1011, message=b"credits_exhausted")

        runner, port = await _start_fake(handler)
        try:
            s = Session(api_key="sk-test", base_url=f"http://127.0.0.1:{port}")
            _register(s)
            got = []
            task = asyncio.create_task(_consume(s, got))
            s._audio_in.push(b"\x00\x00" * 48)
            with pytest.raises(OutOfCreditsError):
                await asyncio.wait_for(task, timeout=2.0)
            await asyncio.sleep(0.3)
            assert state["connections"] == 1
        finally:
            await runner.cleanup()

    asyncio.run(go())


def test_dial_402_is_out_of_credits():
    async def go():
        state = {"connections": 0}

        async def handler(request):
            state["connections"] += 1
            return web.json_response(
                {
                    "error": {
                        "message": "the organization is out of credits — top up to continue",
                        "type": "insufficient_quota",
                        "code": "insufficient_credits",
                    }
                },
                status=402,
            )

        runner, port = await _start_fake(handler)
        try:
            s = Session(api_key="sk-test", base_url=f"http://127.0.0.1:{port}")
            _register(s)
            got = []
            with pytest.raises(OutOfCreditsError):
                async for _ in s.run():
                    got.append(_)
            # Not a retryable refusal: exactly one dial attempt.
            await asyncio.sleep(0.3)
            assert state["connections"] == 1
            assert got == []
        finally:
            await runner.cleanup()

    asyncio.run(go())


def test_explicit_presence_frames(monkeypatch):
    """Explicit presence: the SDK sends {"type":"session.presence"} every 10 s
    while the app is active (idle window 60 s), stops when the window lapses,
    and keepalive() re-asserts. Cadence/idle shortened via module constants."""
    import sense._session as sess_mod

    monkeypatch.setattr(sess_mod, "PRESENCE_CADENCE_S", 0.05)
    monkeypatch.setattr(sess_mod, "PRESENCE_IDLE_S", 0.4)

    async def go():
        state = {"types": []}

        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    break
                ev = json.loads(msg.data)
                state["types"].append(ev["type"])
                if ev["type"] == "session.update":
                    await ws.send_json({"type": "error", "error": {"code": "empty_buffer"}})
                elif ev["type"] == "response.create":
                    await ws.send_json(
                        {
                            "type": "response.output_text.delta",
                            "delta": dec("finished", 0, "true", {"true": 1.0, "false": 0.0}, 1.0) + "\n",
                        }
                    )
                    await ws.send_json({"type": "response.completed"})

        runner, port = await _start_fake(handler)
        try:
            s = Session(api_key="sk-test", base_url=f"http://127.0.0.1:{port}", hz=12.5)
            _register(s)
            got = []
            task = asyncio.create_task(_consume(s, got))

            async def presences():
                return state["types"].count("session.presence")

            # App activity keeps the window open: frames flow every cadence.
            s._text_in.push("still here")
            await _wait_until(lambda: state["types"].count("session.presence") >= 2)
            # The wire frame is exactly the presence marker, no extra fields.
            s.keepalive()
            await _wait_until(lambda: state["types"].count("session.presence") >= 3)
            # Idle past the window: the cadence goes quiet (explicit detach).
            # Baseline only once the cadence has stopped ticking (the 0.4 s
            # window from the keepalive has lapsed) — a mid-cadence count
            # would race the next frame.
            prev = -1
            while state["types"].count("session.presence") != prev:
                prev = state["types"].count("session.presence")
                await asyncio.sleep(0.1)
            n = prev
            await asyncio.sleep(0.7)
            assert state["types"].count("session.presence") == n
            # A fresh app send re-asserts presence.
            s._text_in.push("back")
            await _wait_until(lambda: state["types"].count("session.presence") >= n + 1)
            task.cancel()
            await s.close()
        finally:
            await runner.cleanup()

    asyncio.run(go())

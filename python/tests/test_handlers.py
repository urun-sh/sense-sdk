"""Decorator handler forms — the ONE documented way to register handlers:

    @app.when(pred)               # curried on the predicate
    def escalate(event): ...

    @app.on_wave(coalesce=True)   # and bare @app.on_wave
    def observe(event): ...

``app.when(pred, handler)`` still works and goes through the SAME function
(``handler`` is optional, there is no second method). Decorators return the
function unchanged, so stacking two ``@app.when`` on one function registers
two independent rules sharing the handler — each fires on its own edge.
"""

from sense import Question, QuestionKind, Sense, Wave


def questions():
    return [
        Question(name="finished", kind=QuestionKind.NOUL, instructions="finished?"),
        Question(
            name="intent",
            kind=QuestionKind.CHOICE,
            instructions="want?",
            criteria={"support": "Technical help", "other": "Anything else"},
        ),
    ]


def payload(wave_id, finished, intent=None, p=0.9):
    p_true = p if finished else 1 - p
    results = {
        "finished": {
            "kind": "noul",
            "choice": finished,
            "probabilities": {"true": p_true, "false": 1 - p_true},
            "confidence": p,
        }
    }
    if intent is not None:
        results["intent"] = {
            "kind": "choice",
            "choice": intent,
            "probabilities": {"support": 0.8 if intent == "support" else 0.1, "other": 0.1},
            "confidence": 0.8,
        }
    return {"wave_id": wave_id, "t_ms": wave_id * 40, "results": results}


def feed(app, *waves):
    s = app.session()
    return s, [Wave.decode(s.questions, payload(*w)) for w in waves]


def test_when_decorator_registers_and_fires():
    app = Sense(api_key="k")
    finished = app.noul("finished", instructions="finished?")
    calls = []

    def on_finished(event):
        calls.append(event.latest().wave_id)

    decorated = app.when(finished)(on_finished)
    assert decorated is on_finished, "the decorator must return the function unchanged"
    _, waves = feed(app, (0, True), (1, False), (2, True))
    for w in waves:
        app.session()._accept(w)
    # Edge-triggered: first true fires, the false->true transition refires.
    assert calls == [0, 2]


def test_two_arg_when_rides_the_same_function():
    app = Sense(api_key="k")
    finished = app.noul("finished", instructions="finished?")
    calls = []
    app.when(finished, lambda event: calls.append(event.latest().wave_id))
    _, waves = feed(app, (0, True))
    for w in waves:
        app.session()._accept(w)
    assert calls == [0]


def test_stacking_two_when_on_one_function():
    app = Sense(api_key="k")
    finished = app.noul("finished", instructions="finished?")
    intent = app.choice("intent", instructions="want?", criteria={"support": "Technical help", "other": "Anything else"})
    calls = []

    def handle(event):
        calls.append(event.latest().wave_id)

    # Decorator application order: the inner rule (intent) registers first,
    # the outer (finished) second; both share the same unchanged function.
    decorated = app.when(finished)(app.when(intent == "support")(handle))
    assert decorated is handle
    _, waves = feed(
        app,
        (0, True, "support"),  # both rules flip on: two fires, one per rule
        (1, False, "other"),
        (2, True, "other"),  # only the finished rule flips: one fire
    )
    for w in waves:
        app.session()._accept(w)
    assert calls == [0, 0, 2]


def test_on_wave_decorator_bare():
    app = Sense(api_key="k")
    app.noul("finished", instructions="finished?")
    seen = []

    def observe(event):
        seen.append(event.latest().wave_id)

    assert app.on_wave(observe) is observe
    _, waves = feed(app, (0, True), (1, False))
    for w in waves:
        app.session()._accept(w)
    assert seen == [0, 1]


def test_on_wave_decorator_with_kwargs_coalesces():
    app = Sense(api_key="k")
    app.noul("finished", instructions="finished?")
    seen = []

    @app.on_wave(coalesce=True)  # latest-wins runs deliver only the FIRST wave
    def observe(event):
        seen.append(event.latest().wave_id)

    _, waves = feed(
        app,
        (0, True),
        (1, True),  # identical argmax to wave 0: coalesced away
        (2, False),
    )
    for w in waves:
        app.session()._accept(w)
    assert seen == [0, 2]


def test_decorator_source_kwarg_restricts_to_one_source():
    app = Sense(api_key="k")
    finished = app.noul("finished", instructions="finished?")
    calls = []

    @app.when(finished, source="mic")
    def on_mic(event):
        calls.append(event.latest().wave_id)

    s = app.session()
    w0 = Wave.decode(s.questions, payload(0, True))
    w0.source = "default"
    s._accept(w0)  # default-source wave: the mic-only rule must not fire
    w1 = Wave.decode(s.questions, payload(1, False))
    w1.source = "mic"
    s._accept(w1)  # false -> still not fired (was_on False, stays off)
    w2 = Wave.decode(s.questions, payload(2, True))
    w2.source = "mic"
    s._accept(w2)  # mic edge: fires
    assert calls == [2]

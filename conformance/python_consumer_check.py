#!/usr/bin/env python3
"""Clean-consumer check for the packaged urun-sense distribution.

Run from a directory where `urun-sense` has been installed from the built
wheel into a clean venv, with the repo's `conformance/fixtures` copied next
to this script. Imports ONLY the bare `sense` package — no relative imports
into the repository — and proves, fully offline:

  1. the installed distribution is this SDK (`urun-sense`), not an unrelated
     package;
  2. the recorded conformance fixtures replay through the installed code
     (all 8 waves decode; every predicate case matches; an incomplete wave
     raises the protocol error);
  3. a real session streams input against a local mock realtime endpoint:
     bearer auth is checked in the handshake, waves assemble from decision
     lines, edge `when` fires, onWave(coalesce) skips same-argmax runs, and
     `close()` tears the session down cleanly.

Exit 0 = all checks pass; any failure raises a loud assertion.
"""

import asyncio
import json
from importlib.metadata import metadata, version
from pathlib import Path

import sense
from aiohttp import WSMsgType, web
from sense import Predicate, ProtocolError, Question, QuestionKind, Score, Sense, Wave

# -- 1. identity -----------------------------------------------------------------

_dist = metadata("urun-sense")
assert _dist["Name"] == "urun-sense", (
    f"installed distribution must be urun-sense, got {_dist['Name']!r}"
)
version("urun-sense")  # raises if the installed metadata is unusable
for _name in sense.__all__:
    assert hasattr(sense, _name), f"public surface incomplete: missing {_name}"

# -- 2. fixture replay through the installed code ---------------------------------

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def read_json(name: str):
    """Load a fixture file from the local conformance fixtures directory."""
    return json.loads((FIXTURES / name).read_text())


questions = [
    Question(
        id=qid,
        kind=QuestionKind(q["kind"]),
        prompt=q["prompt"],
        options=q.get("options"),
        levels=q.get("levels"),
    )
    for qid, q in read_json("questions.json")["questions"].items()
]
waves = [
    Wave.decode(questions, json.loads(line))
    for line in (FIXTURES / "waves.jsonl").read_text().splitlines()
    if line.strip()
]
assert len(waves) == 8, f"fixture replay: expected 8 recorded waves, got {len(waves)}"
assert [w.t_ms for w in waves] == sorted(w.t_ms for w in waves), (
    "fixture replay: wave t_ms must be monotonic"
)
assert [w.wave_id for w in waves] == list(range(8)), "fixture replay: wave ids must be 0..7"


def subject_from_json(d):
    """Map a JSON subject descriptor to the tuple form the SDK uses."""
    t = d["type"]
    if t == "question":
        return ("q", d["id"], None)
    if t == "p":
        return ("p", d["question"], d.get("option"))
    if t == "top":
        return ("top", d["question"], None)
    if t == "value":
        return ("value", d["question"], None)
    if t == "p_at_least":
        return ("p_at_least", d["question"], d["level"])
    raise AssertionError(f"fixture replay: unknown subject {d}")


def ast_from_json(d):  # noqa: PLR0911 - one return per AST node kind
    """Rebuild a predicate expression tree from a JSON fixture structure."""
    if "type" in d:
        return subject_from_json(d)
    if "and" in d:
        return Predicate.all_of(*(ast_from_json(n) for n in d["and"]["of"]))
    if "or" in d:
        return Predicate.any_of(*(ast_from_json(n) for n in d["or"]["of"]))
    if "not" in d:
        return ~ast_from_json(d["not"]["of"])
    if "truthy" in d:
        return Predicate(ast_from_json(d["truthy"]["subject"]))
    if "between" in d:
        b = d["between"]
        return Predicate.between(ast_from_json(b["subject"]), enter=b["enter"], exit=b["exit"])
    return Predicate(d["op"], ast_from_json(d["subject"]), d["value"])


cases = read_json("cases.json")["predicate_cases"]
for case in cases:
    pred = ast_from_json(case["predicate"])
    got = [pred(w) for w in waves]
    assert got == case["expected"], (
        f"fixture replay: predicate case {case['name']!r} diverged: "
        f"got {got} want {case['expected']}"
    )

score_result = waves[0].result("urgency")
assert type(score_result) is Score, "fixture replay: score results must decode as Score"
try:
    score_result.p_option("nope")
    raise AssertionError("fixture replay: pOption on a score must raise")
except ProtocolError:
    pass

try:
    for w in read_json("incomplete.json")["waves"]:
        Wave.decode(questions, w)
    raise AssertionError("fixture replay: an incomplete wave must raise the protocol error")
except ProtocolError:
    pass

# -- 3. live session against a local mock realtime endpoint -----------------------

TURNS = [
    {"finished": True, "intent": "support", "urgency": {"0": 0.9, "1": 0.08, "2": 0.02}},
    {"finished": True, "intent": "support", "urgency": {"0": 0.85, "1": 0.1, "2": 0.05}},
    {"finished": True, "intent": "support", "urgency": {"0": 0.8, "1": 0.12, "2": 0.08}},
    {"finished": False, "intent": "sales", "urgency": {"0": 0.1, "1": 0.2, "2": 0.7}},
]

state = {"saw_bearer": False, "creates": 0}


def decision(qid: str, t_ms: int, body: dict) -> str:
    """Format a single sense.decision JSON line for the mock stream."""
    return json.dumps({"event": "sense.decision", "id": qid, "t_ms": t_ms, **body})


async def handler(request: web.Request) -> web.WebSocketResponse:
    """Reject the WS handshake unless the full Authorization header matches.

    The mock acts as the realtime endpoint: it must verify the whole bearer
    credential before opening the socket, so an invalid key can never start
    a session.
    """
    if request.headers.get("Authorization") != "Bearer test-key":
        return web.json_response({"error": "unauthorized"}, status=401)
    state["saw_bearer"] = True
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    async for msg in ws:
        if msg.type is not WSMsgType.TEXT:
            break
        ev = json.loads(msg.data)
        if ev["type"] == "response.create":
            state["creates"] += 1
            t0 = (state["creates"] - 1) * 400
            lines = []
            for i, turn in enumerate(TURNS):
                t = t0 + i * 40
                lines.append(
                    decision(
                        "finished",
                        t,
                        {
                            "choice": turn["finished"],
                            "probabilities": {
                                "true": 0.9 if turn["finished"] else 0.1,
                                "false": 0.1 if turn["finished"] else 0.9,
                            },
                            "confidence": 0.9,
                        },
                    )
                )
                lines.append(
                    decision(
                        "intent",
                        t,
                        {
                            "choice": turn["intent"],
                            "probabilities": {
                                "support": 0.8 if turn["intent"] == "support" else 0.1,
                                "sales": 0.8 if turn["intent"] == "sales" else 0.1,
                                "other": 0.1,
                            },
                            "confidence": 0.8,
                        },
                    )
                )
                lines.append(
                    decision(
                        "urgency",
                        t,
                        {
                            "probabilities": turn["urgency"],
                            "confidence": 0.85,
                        },
                    )
                )
            await ws.send_json({"type": "response.output_text.delta", "delta": "\n".join(lines)})
            await ws.send_json({"type": "response.completed"})
    return ws


async def main() -> None:
    """Run the full consumer check against a locally spawned mock endpoint."""
    runner = web.AppRunner(web.Application())
    runner.app.router.add_get("/v1/realtime", handler)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    try:
        app = Sense(api_key="test-key", base_url=f"http://127.0.0.1:{port}")
        finished = app.noul("Did the speaker just finish their turn?", id="finished")
        intent = app.choice("What does the user want?", ["support", "sales", "other"], id="intent")
        app.score("How urgent is the request?", ["low", "medium", "high"], id="urgency")

        session = app.session()
        raw, coalesced, when_fires = [], [], []
        session.onWave(lambda ctx: raw.append(ctx.latest()))
        session.onWave(lambda ctx: coalesced.append(ctx.latest()), coalesce=True)
        session.when(
            finished & (intent == "support"),
            lambda ctx: when_fires.append(ctx.latest().wave_id),
        )

        async def run_session():
            async for _wave in session.run():
                pass

        task = asyncio.create_task(run_session())
        session.audio.push(b"\x00\x00" * 48)
        for _ in range(1000):
            if len(raw) >= len(TURNS):
                break
            await asyncio.sleep(0.01)
        assert state["saw_bearer"], "session must send the bearer key in the WS handshake"
        assert state["creates"] >= 1, "mock endpoint must see at least one response.create"
        assert len(raw) >= len(TURNS), f"expected at least {len(TURNS)} waves, got {len(raw)}"
        for w in raw:
            assert sorted(w.results) == ["finished", "intent", "urgency"], (
                f"wave {w.wave_id} must assemble every registered question, got {sorted(w.results)}"
            )
            assert isinstance(w.result("finished").value, bool)
            u = w.result("urgency")
            assert isinstance(u, Score)
            assert u.top in ("low", "medium", "high")
        expected_fire_ids = [raw[i].wave_id for i in range(0, len(raw), len(TURNS))]
        assert when_fires == expected_fire_ids, (
            f"edge-triggered when must fire exactly on the first wave of each "
            f"four-turn sequence, got {when_fires}, want {expected_fire_ids}"
        )
        assert len(coalesced) < len(raw), (
            f"onWave(coalesce) must skip same-argmax runs ({len(coalesced)} vs {len(raw)})"
        )
        assert len(coalesced) >= 2, (
            "onWave(coalesce) must still deliver the first wave of each matching run"
        )

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await session.close()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
    print("clean-consumer check passed: urun-sense replays fixtures and wires a session end-to-end")

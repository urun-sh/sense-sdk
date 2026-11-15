#!/usr/bin/env python3
"""Python conformance runner: replays recorded fixtures through the `sense` SDK.

Covers: session event envelope, question registration, wave decoding,
latest()/waves() coalescing, predicate evaluation (and/or/not/comparisons/
between), edge-triggered when(), and the protocol-error contract for
missing answers. Wire-level behavior is additionally covered by the live
examples; this runner is offline and deterministic.
"""

import asyncio
import base64
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))
from sense import (
    Context,
    Predicate,
    ProtocolError,
    Question,
    QuestionKind,
    Score,
    Session,
    Wave,
    session_event,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_questions():
    """questions.json keys ARE the ids (the map key is the question id)."""
    raw = json.loads((FIXTURES / "questions.json").read_text())["questions"]
    return [
        Question(
            id=qid,
            kind=QuestionKind(q["kind"]),
            prompt=q["prompt"],
            options=q.get("options"),
            levels=q.get("levels"),
        )
        for qid, q in raw.items()
    ]


def ast_from_json(d):  # noqa: PLR0911 - one return per AST node kind
    if "type" in d:  # a subject dict
        return subj_from_json(d)
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


def subj_from_json(d):
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
    raise AssertionError(f"unknown subject {d}")


class TestSessionEnvelope(unittest.TestCase):
    def test_event_roundtrip(self):
        for raw in ("session.created", "session.ready", "session.closed"):
            ev = session_event(json.loads(json.dumps({"type": raw})))
            self.assertEqual(ev.type, raw)
        ev = session_event({"type": "error", "code": "bad_question", "message": "no"})
        self.assertEqual(ev.type, "error")
        self.assertEqual(ev.code, "bad_question")


class TestWaveDecoding(unittest.TestCase):
    """Decode the recorded waves and assert the shape of every wave."""

    @classmethod
    def setUpClass(cls):
        cls.questions = load_questions()
        cls.waves = [
            Wave.decode(cls.questions, json.loads(line))
            for line in (FIXTURES / "waves.jsonl").read_text().splitlines()
            if line.strip()
        ]

    def test_eight_waves_with_monotonic_t(self):
        self.assertEqual(len(self.waves), 8)
        ts = [w.t_ms for w in self.waves]
        self.assertEqual(ts, sorted(ts))
        self.assertEqual([w.wave_id for w in self.waves], list(range(8)))

    def test_kind_shapes(self):
        for w in self.waves:
            f = w.result("finished")
            self.assertIsInstance(f.value, bool)
            self.assertIsInstance(f.p, float)
            self.assertGreaterEqual(f.confidence, 0.0)
            i = w.result("intent")
            self.assertIn(i.value, ("support", "sales", "other"))
            u = w.result("urgency")
            self.assertIsInstance(u, Score)
            self.assertIn(u.top, ("low", "medium", "high"))
            self.assertAlmostEqual(u.p_at_least("low"), 1.0)


class TestPredicates(unittest.TestCase):
    """Per-wave predicate values must match the recorded expectations."""

    def test_cases(self):
        cases = json.loads((FIXTURES / "cases.json").read_text())
        questions = load_questions()
        waves = [
            Wave.decode(questions, json.loads(line))
            for line in (FIXTURES / "waves.jsonl").read_text().splitlines()
            if line.strip()
        ]
        for case in cases["predicate_cases"]:
            with self.subTest(case=case["name"]):
                pred = ast_from_json(case["predicate"])
                got = [pred(w) for w in waves]
                self.assertEqual(got, case["expected"], f"got {got} want {case['expected']}")

    def test_predicate_subjects_cover_the_spec(self):
        # All five subject kinds must appear across the recorded cases.
        text = (FIXTURES / "cases.json").read_text()
        for name in ("question", "p", "top", "value", "p_at_least"):
            self.assertIn(f'"type": "{name}"', text)

    def test_noul_bare_value_is_bool(self):
        questions = load_questions()
        w = Wave.decode(
            questions, json.loads((FIXTURES / "waves.jsonl").read_text().splitlines()[0])
        )
        self.assertIsInstance(w.result("finished").value, bool)
        self.assertIsInstance(w.result("user_speaking").value, bool)


class TestContext(unittest.TestCase):
    """latest/waves/when behavior over the recorded wave stream."""

    @classmethod
    def setUpClass(cls):
        cls.questions = load_questions()
        cls.payloads = [
            json.loads(line)
            for line in (FIXTURES / "waves.jsonl").read_text().splitlines()
            if line.strip()
        ]

    def test_latest_and_waves(self):
        ctx = Context()
        for p in self.payloads:
            ctx._ingest(Wave.decode(self.questions, p))
        self.assertEqual(ctx.latest().wave_id, 7)
        self.assertEqual(len(ctx.waves()), 8)
        # Coalescing: latest-wins within a run of consecutive waves sharing
        # the same argmax values — w2/w3 agree on every argmax, so the run
        # keeps only the LAST wave (w3). 8 -> 7 waves.
        co = ctx.waves(coalesce=True)
        self.assertEqual([w.wave_id for w in co], [0, 1, 3, 4, 5, 6, 7])
        self.assertEqual(ctx.latest().result("intent").value, "support")

    def test_when_fires_on_edges(self):
        ctx = Context()
        fired = []
        ctx.when(Predicate(("q", "finished", None)), lambda ctx: fired.append(ctx.latest().wave_id))
        for p in self.payloads:
            ctx._ingest(Wave.decode(self.questions, p))
        self.assertEqual(fired, [2, 5, 7])

    def test_when_hysteresis(self):
        ctx = Context()
        fired = []
        ctx.when(
            Predicate.between(("p", "finished", None), enter=0.8, exit=0.6),
            lambda ctx: fired.append(ctx.latest().wave_id),
        )
        for p in self.payloads:
            ctx._ingest(Wave.decode(self.questions, p))
        self.assertEqual(fired, [2, 5, 7])

    def test_when_cancellable(self):
        ctx = Context()
        fired = []
        when = ctx.when(
            Predicate(("q", "finished", None)), lambda ctx: fired.append(ctx.latest().wave_id)
        )
        when.cancel()
        for p in self.payloads:
            ctx._ingest(Wave.decode(self.questions, p))
        self.assertEqual(fired, [])

    def test_onwave(self):
        ctx = Context()
        seen = []
        ctx.onWave(lambda ctx: seen.append(ctx.latest().wave_id))
        for p in self.payloads:
            ctx._ingest(Wave.decode(self.questions, p))
        self.assertEqual(seen, [w.wave_id for w in ctx.waves()])


class TestProtocolErrors(unittest.TestCase):
    def test_missing_answer_raises(self):
        q = load_questions()
        incomplete = json.loads((FIXTURES / "incomplete.json").read_text())
        with self.assertRaises(ProtocolError):
            for w in incomplete["waves"]:
                Wave.decode(q, w)

    def test_unknown_question_raises(self):
        q = load_questions()
        w = json.loads(
            json.dumps(json.loads((FIXTURES / "incomplete.json").read_text())["waves"][0])
        )
        w["results"]["bogus"] = {"kind": "noul", "choice": True}
        with self.assertRaises(ProtocolError):
            Wave.decode(q, w)


class TestSession(unittest.TestCase):
    def test_session_defaults(self):
        s = Session(api_key="sk-test")
        self.assertEqual(s.questions, [])
        self.assertFalse(s._closed)
        self.assertEqual(s.base_url, Session.DEFAULT_BASE_URL)

    def test_question_registration_shape(self):
        s = Session(api_key="sk-test")
        q = s.add_question("finished", QuestionKind.BOOL, prompt="did they finish?")
        self.assertEqual(q.id, "finished")
        self.assertEqual(s.questions, [q])

    def test_streaming_wire_shape(self):
        # REAL wire assertions (protocol.md §2/§4): the session.update
        # instructions envelope the SDK registers, and the
        # input_audio_buffer.append frame a fake lane receives.
        async def go():
            s = Session(api_key="sk-test")
            s.add_question("finished", QuestionKind.NOUL, prompt="did they finish?")
            s.add_question(
                "intent", QuestionKind.CHOICE, prompt="want?", options=["support", "sales", "other"]
            )
            s.add_question(
                "urgency",
                QuestionKind.SCORE,
                prompt="how urgent?",
                levels=["low", "medium", "high"],
            )
            frames = []

            class FakeWS:
                async def send(self, payload):
                    frames.append(payload)

            await s._hello(FakeWS())
            # the session.update envelope: real JSON, real shape
            self.assertEqual(frames[0]["type"], "session.update")
            env = json.loads(frames[0]["session"]["instructions"])
            self.assertEqual(env["hz"], s.hz)
            fin = env["questions"]["finished"]
            self.assertEqual(sorted(fin.keys()), ["kind", "prompt"])
            self.assertEqual(fin["kind"], "noul")
            intent = env["questions"]["intent"]
            self.assertEqual([o["name"] for o in intent["options"]], ["support", "sales", "other"])
            self.assertEqual(env["questions"]["urgency"]["criteria"], ["low", "medium", "high"])
            # noul options are canonicalized to [no, yes] order (protocol §2)
            s.add_question("good", QuestionKind.NOUL, prompt="all good?", options=["yes", "no"])
            frames.clear()
            await s._hello(FakeWS())
            env = json.loads(frames[0]["session"]["instructions"])
            self.assertEqual(env["questions"]["good"]["options"], [["no", ""], ["yes", ""]])
            # the input_audio_buffer.append frame a fake lane receives for one
            # PCM16 chunk (no commit, no response.create while the turn is open)
            s._turn_open = True
            s.audio.push(b"\x00\x00")
            frames.clear()
            ok = await s._drain_outbound(FakeWS())
            self.assertTrue(ok)
            self.assertEqual(len(frames), 1)
            self.assertEqual(frames[0]["type"], "input_audio_buffer.append")
            self.assertEqual(frames[0]["audio"], base64.b64encode(b"\x00\x00").decode())
            self.assertNotIn("sample_rate", frames[0])
            self.assertNotIn("source", frames[0])

        asyncio.run(go())


if __name__ == "__main__":
    unittest.main(verbosity=2)

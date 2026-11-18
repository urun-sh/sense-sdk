/**
 * Clean-consumer check for the packaged @urun-sh/sense distribution.
 *
 * Run from a consumer directory that has installed the packed tarball
 * (`npm install <@urun-sh/sense tarball>`) with the repo's `conformance/fixtures`
 * copied next to this script. Imports ONLY bare package specifiers — no
 * relative imports into the repository — and proves, fully offline:
 *
 *   1. the installed distribution is this SDK, not an unrelated package;
 *   2. the recorded conformance fixtures replay through the installed code
 *      (all 8 waves decode; every predicate case matches; an incomplete wave
 *      raises the protocol error);
 *   3. a real session streams input against a local mock realtime endpoint:
 *      bearer auth is checked, waves assemble from decision lines, edge
 *      `when` fires, `onWave(coalesce)` skips same-argmax runs, and
 *      `close()` tears the session down cleanly.
 *
 * Exit 0 = all checks pass; any failure throws with a loud assertion.
 */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { WebSocketServer } from "ws";
import {
  Predicate,
  ProtocolError,
  Question,
  Score,
  Sense,
  Wave,
} from "@urun-sh/sense";

// -- 1. identity ---------------------------------------------------------------

const require = createRequire(import.meta.url);
const pkg = require("@urun-sh/sense/package.json");
assert.equal(pkg.name, "@urun-sh/sense", `installed package must be @urun-sh/sense, got ${pkg.name}`);

// -- 2. fixture replay through the installed code ------------------------------

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), "fixtures");
const readJson = (name) => JSON.parse(readFileSync(join(FIXTURES, name), "utf8"));

const questions = Object.entries(readJson("questions.json").questions).map(([id, q]) =>
  new Question({ name: id, kind: q.type, instructions: q.instructions, criteria: q.criteria }),
);
const waves = readFileSync(join(FIXTURES, "waves.jsonl"), "utf8")
  .split("\n")
  .filter((l) => l.trim())
  .map((l) => Wave.decode(questions, JSON.parse(l)));
assert.equal(waves.length, 8, "fixture replay: expected 8 recorded waves to decode");

function subjectFromJson(d) {
  switch (d.type) {
    case "question": return { type: "q", id: d.id };
    case "p": return { type: "p", question: d.question, option: d.option };
    case "top": return { type: "top", question: d.question };
    case "value": return { type: "value", question: d.question };
    case "p_at_least": return { type: "p_at_least", question: d.question, level: d.level };
    default: throw new Error(`unknown subject ${JSON.stringify(d)}`);
  }
}
function astFromJson(d) {
  if (d.and) return Predicate.allOf(...d.and.of.map(astFromJson));
  if (d.or) return Predicate.anyOf(...d.or.of.map(astFromJson));
  if (d.not) return astFromJson(d.not.of).not();
  if (d.truthy) return Predicate(subjectFromJson(d.truthy.subject));
  if (d.between) return Predicate.between(subjectFromJson(d.between.subject), d.between.enter, d.between.exit);
  return Predicate(d.op, subjectFromJson(d.subject), d.value);
}
for (const c of readJson("cases.json").predicate_cases) {
  assert.deepEqual(
    waves.map((w) => astFromJson(c.predicate)(w)),
    c.expected,
    `fixture replay: predicate case "${c.name}" diverged`,
  );
}
assert.throws(
  () => {
    for (const w of readJson("incomplete.json").waves) Wave.decode(questions, w);
  },
  ProtocolError,
  "fixture replay: an incomplete wave must raise the protocol error",
);
assert.throws(
  () => waves[0].result("urgency").pOption("nope"),
  ProtocolError,
  "fixture replay: pOption on a score must raise",
);
assert.equal(waves[0].result("urgency").constructor, Score, "fixture replay: score results decode as Score");

// -- 3. session against a local mock realtime endpoint -------------------------

const WAVES_PER_TURN = [
  { finished: true, intent: "support", urgency: { "0": 0.9, "1": 0.08, "2": 0.02 } },
  { finished: true, intent: "support", urgency: { "0": 0.85, "1": 0.1, "2": 0.05 } },
  { finished: true, intent: "support", urgency: { "0": 0.8, "1": 0.12, "2": 0.08 } },
  { finished: false, intent: "sales", urgency: { "0": 0.1, "1": 0.2, "2": 0.7 } },
];

const decision = (id, tMs, body) =>
  JSON.stringify({ event: "sense.decision", id, t_ms: tMs, ...body });
const noul = (v, tMs) =>
  decision("finished", tMs, { choice: v, probabilities: { true: v ? 0.9 : 0.1, false: v ? 0.1 : 0.9 }, confidence: 0.9 });
const choice = (v, tMs) =>
  decision("intent", tMs, { choice: v, probabilities: { support: v === "support" ? 0.8 : 0.1, sales: v === "sales" ? 0.8 : 0.1, other: 0.1 }, confidence: 0.8 });
const score = (probs, tMs) =>
  decision("urgency", tMs, { probabilities: probs, confidence: 0.85 });

let sawBearer = false;
let turns = 0;
const wss = new WebSocketServer({ port: 0 });
await new Promise((resolve) => wss.on("listening", resolve));
const port = wss.address().port;
wss.on("connection", (socket, req) => {
  sawBearer = (req.headers.authorization ?? "").startsWith("Bearer test-key");
  socket.on("message", (data) => {
    const msg = JSON.parse(String(data));
    if (msg.type !== "response.create") return;
    turns++;
    const t0 = (turns - 1) * 400;
    let i = 0;
    for (const w of WAVES_PER_TURN) {
      const t = t0 + i * 40;
      socket.send(JSON.stringify({
        type: "response.output_text.delta",
        delta: [noul(w.finished, t), choice(w.intent, t), score(w.urgency, t)].join("\n"),
      }));
      i++;
    }
    socket.send(JSON.stringify({ type: "response.completed" }));
  });
});

// -- the consumer's own code, written against the installed package only -------

const app = new Sense({ apiKey: "test-key", baseUrl: `ws://127.0.0.1:${port}` });
const finished = app.noul("finished", {
  instructions: "Did the person just finish their turn?",
  criteria: { true: "the person just finished their turn", false: "the person is still mid-turn" },
});
const intent = app.choice("intent", {
  instructions: "What does the person want?",
  criteria: { support: "Help with a problem", sales: "Wants to buy", other: "Anything else" },
});
app.score("urgency", {
  instructions: "How urgent is the request?",
  criteria: ["routine, can wait", "time sensitive today", "blocking, act now"],
});

const raw = [];
const coalesced = [];
const whenFires = [];
const session = app.session();
session.onWave((ctx) => raw.push(ctx.latest()));
session.onWave((ctx) => coalesced.push(ctx.latest()), { coalesce: true });
session.when(finished.and(intent.eq("support")), (ctx) => whenFires.push(ctx.latest().waveId));

async function* input() {
  const chunk = new Uint8Array(2 * 960); // 40 ms of PCM16 24 kHz mono
  for (let i = 0; i < 4; i++) {
    yield { audio: chunk };
    await new Promise((r) => setTimeout(r, 15));
  }
}

const runPromise = (async () => {
  for await (const wave of session.run(input())) void wave;
})();

await new Promise((resolve) => {
  // Promise.race leaves the losing timer running (keeping the Node process
  // alive after the session succeeds), so drive the timeout by hand and
  // clear whichever side loses.
  let timeout;
  const poll = setInterval(() => {
    if (turns >= 1 && raw.length >= WAVES_PER_TURN.length) {
      clearInterval(poll);
      clearTimeout(timeout);
      resolve();
    }
  }, 50);
  timeout = setTimeout(() => {
    clearInterval(poll);
    resolve();
  }, 15_000);
});
await session.close();
await runPromise;

assert.equal(sawBearer, true, "session must send the bearer key in the WS handshake");
assert.ok(turns >= 1, `session must open a turn on buffered input, opened ${turns}`);
assert.ok(raw.length >= WAVES_PER_TURN.length, `session must assemble waves, got ${raw.length}`);
for (const w of raw) {
  assert.deepEqual(w.questionIds().sort(), ["finished", "intent", "urgency"].sort());
  assert.equal(typeof w.result("finished").value, "boolean");
}
assert.ok(whenFires.length >= 1, "edge-triggered when must fire on the support edge");
assert.ok(
  coalesced.length < raw.length,
  `onWave(coalesce) must skip same-argmax runs (${coalesced.length} vs ${raw.length})`,
);
assert.ok(
  coalesced.length >= 2,
  `onWave(coalesce) must still deliver the first wave of each matching run, got ${coalesced.length}`,
);
const coalescedTops = coalesced.map((w) => w.result("intent").top);
assert.deepEqual(
  coalescedTops.slice(0, 2),
  ["support", "sales"],
  `onWave(coalesce) must deliver the first wave of each run in order (support, sales), got ${JSON.stringify(coalescedTops)}`,
);
for (let i = 1; i < coalesced.length; i++) {
  assert.notEqual(coalesced[i].tMs, coalesced[i - 1].tMs, "coalesced delivery must not repeat consecutive same-signature waves");
}
assert.equal(session.stats.protocolErrors, 0, "session must see no protocol errors against the mock");
assert.ok(session.stats.waves >= WAVES_PER_TURN.length, "stats must count assembled waves");

await new Promise((resolve) => wss.close(resolve));
console.log(`consumer check OK: pkg=${pkg.name}@${pkg.version} waves=${raw.length} coalesced=${coalesced.length} whenFires=${whenFires.length} turns=${turns}`);

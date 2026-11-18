/**
 * Flagship full-duplex voice agent on @urun-sh/sense (TypeScript).
 *
 * Sense decides WHEN to respond, WHICH tool, and WHEN to interrupt; your code
 * executes. Reads a WAV (bundled synthetic speech by default), streams it to the Sense
 * endpoint, and reacts on edge-triggered predicates — including a barge-in
 * that cancels the in-flight reply via ctx.cancel().
 *
 * Usage (run `bun install && bun run build` first — the demo imports the
 * built package and the build stages the bundled sample wav):
 *   URUN_API_KEY=<key> bun examples/voice_agent.ts
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { Score, Sense, type Context, type Wave } from "@urun-sh/sense";

// -- questions ----------------------------------------------------------------

const app = new Sense({}); // apiKey/baseUrl default from URUN_API_KEY / SENSE_BASE_URL
const finished = app.noul("finished", {
  instructions: "Did the current speaker just finish their turn?",
  criteria: { true: "the person just finished their turn", false: "the person is still mid-turn" },
});
const userSpeaking = app.noul("user_speaking", {
  instructions: "Is a person speaking right now?",
  criteria: { true: "a person is speaking", false: "nobody is speaking" },
});
const intent = app.choice("intent", {
  instructions: "What does the person want?",
  criteria: { support: "Help with a problem", sales: "Wants to buy", other: "Anything else" },
});
const actions = app.choice("actions", {
  instructions: "What should the agent do?",
  criteria: { respond: "Answer now", ignore: "Stay silent", wait: "Hold for more context" },
});
app.score("urgency", {
  instructions: "How urgent is the request?",
  criteria: ["low — routine", "medium — time sensitive today", "high — blocking right now"],
});
const tool = app.choice("tool", {
  instructions: "Should a tool run?",
  criteria: { none: "No tool needed", weather: "Weather lookup", search: "Web search", timer: "Set a timer" },
});

// The most recent trigger context — barge-in cancels tasks through it.
let lastEvent: Context | null = null;

// -- reactions (edge-triggered: fire on first true, then each false->true) ----

app.when(
  finished
    .and(intent.eq("support"))
    .and(actions.top.eq("respond")),
  (event) => {
    lastEvent = event;
    const wave = event.latest()!;
    console.log(`[respond] wave ${wave.waveId} t=${wave.tMs}ms urgency=${(wave.result("urgency") as Score).value.toFixed(2)}`);
    // A tracked cancellable run: duplicate active keys reject; the barge-in
    // watcher cancels it (ctx.cancel barges in by default).
    event
      .run("reply", (ctx) => reply(wave, ctx.signal), {
        onCancel: () => console.log("[respond] reply cancelled"),
      })
      .catch((err: unknown) => {
        if ((err as Error).message?.includes("cancelled")) return;
        throw err;
      });
  },
);

app.when(finished.and(intent.p("support").gte(0.7)), (event) => {
  const wave = event.latest()!;
  console.log(`[sales-handoff] wave ${wave.waveId}: P(support)=${wave.result("intent").pOption("support").toFixed(2)}`);
});

app.when(tool.ne("none"), (event) => {
  const wave = event.latest()!;
  console.log(`[tool] wave ${wave.waveId}: invoking ${wave.result("tool").value}`);
});

// Turn boundary: the speaker finished AND went quiet — the moment a voice
// agent would hand the floor to its reply path.
app.when(finished.and(userSpeaking.not()), (event) => {
  const wave = event.latest()!;
  console.log(`[turn-boundary] wave ${wave.waveId} t=${wave.tMs}ms — speaker finished and quiet`);
});

// Probability predicate with hysteresis: true at P(finished) >= 0.8, resets
// only below 0.6 — thresholds near the boundary flap and edge triggers re-fire.
app.when(finished.p.between(0.8, 0.6), (event) => {
  console.log(`[settled] wave ${event.latest()!.waveId}: speaker settled`);
});

app.onWave((event) => {
  const w = event.latest()!;
  if (w.result("actions").value === "ignore") return;
  console.log(`[wave ${w.waveId}] t=${w.tMs}ms finished=${w.result("finished").value} intent=${w.result("intent").value}`);
}, { coalesce: true });

function delay(ms: number): Promise<void> {
  const { promise, resolve } = Promise.withResolvers<void>();
  setTimeout(resolve, ms);
  return promise;
}

async function reply(wave: Wave, signal: AbortSignal | null): Promise<void> {
  console.log(`[agent] replying to wave ${wave.waveId} (stub execution)`);
  await delay(2000);
  if (signal?.aborted) throw new Error(`reply for wave ${wave.waveId} cancelled`);
  console.log(`[agent] reply for wave ${wave.waveId} complete`);
}

async function bargeInWatch(): Promise<void> {
  for (;;) {
    await delay(100);
    if (!lastEvent) continue;
    const spk = lastEvent.get("user_speaking");
    if (spk && spk.value === true && spk.p >= 0.9) {
      console.log("[barge-in] user resumed — cancelling the in-flight reply");
      lastEvent.cancel("reply");
      return;
    }
  }
}

// -- the session ---------------------------------------------------------------

/** 24 kHz PCM16 mono audio chunks, paced like a live mic (48 kHz decimated). */
async function* audioFrames(path: string, chunkMs = 40): AsyncGenerator<Uint8Array> {
  const buf = readFileSync(path);
  // Minimal RIFF walk to the data chunk.
  let off = 12;
  let rate = 48000;
  let data: Uint8Array | null = null;
  while (off + 8 <= buf.length) {
    const id = buf.subarray(off, off + 4).toString("latin1");
    const size = buf.readUInt32LE(off + 4);
    const body = buf.subarray(off + 8, off + 8 + size);
    if (id === "fmt ") rate = body.readUInt32LE(4);
    if (id === "data") data = body;
    off += 8 + size + (size % 2);
  }
  if (!data) throw new Error("no data chunk");
  const samples = new Int16Array(data.buffer, data.byteOffset, data.byteLength >> 1);
  const decimated = rate === 48000 ? samples.filter((_, i) => i % 2 === 0) : samples;
  const n = Math.floor((24000 * chunkMs) / 1000);
  for (let i = 0; i + n <= decimated.length; i += n) {
    yield new Uint8Array(decimated.buffer, decimated.byteOffset + i * 2, n * 2).slice();
    await delay(chunkMs);
  }
}

async function main(): Promise<number> {
  const path = process.argv[2] ?? fileURLToPath(new URL("./assets/mic-question-48k-mono.wav", import.meta.url));
  const session = app.session();
  void bargeInWatch();
  // Demo bound: a live agent runs forever; the example stops after 30s.
  const done = Promise.withResolvers<void>();
  const timer = setTimeout(() => done.resolve(), 30_000);
  try {
    // attach() is the one input path: pump the WAV chunks (plus a text source
    // standing in for a transcript from your own ASR — the model hears the
    // audio directly; text is extra context), then read waves.
    const attached = await session.attach({
      audio: audioFrames(path),
      text: (async function* () {
        yield "Hi — my internet has been down since this morning and I need help.";
      })(),
    });
    try {
      const waves = session.run()[Symbol.asyncIterator]();
      for (;;) {
        const next = await Promise.race([waves.next(), done.promise.then(() => null)]);
        if (next === null || next.done) break;
        void next.value; // handlers react; the run loop assembles and yields waves
      }
    } finally {
      attached.stop();
    }
  } finally {
    clearTimeout(timer);
    await session.close();
  }
  console.log(`stats: ${JSON.stringify(session.stats)}`);
  return 0;
}

main().then(
  (code) => process.exitCode = code,
  (err: unknown) => {
    console.error(err);
    process.exitCode = 1;
  },
);

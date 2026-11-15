import { describe, expect, test, vi } from "bun:test";
import {
  Context,
  OutOfCreditsError,
  Predicate,
  ProtocolError,
  Question,
  Score,
  Sense,
  Session,
  Wave,
  instructionsEnvelope,
  sessionEvent,
  type SessionEvent,
  type WireDialer,
} from "../src/index.ts";

const QUESTIONS = [
  new Question({ id: "finished", kind: "noul", prompt: "finished?" }),
  new Question({ id: "intent", kind: "choice", options: ["support", "sales", "other"] }),
  new Question({ id: "urgency", kind: "score", levels: ["low", "medium", "high"] }),
  new Question({ id: "actions", kind: "choice", options: ["respond", "ignore", "wait"] }),
];

function wave(entries: Record<string, unknown>, waveId = 0): Wave {
  return Wave.decode(QUESTIONS, { wave_id: waveId, t_ms: waveId * 40, results: entries });
}

/** Test-authored wire events all carry a `type`; narrow, never cast. */
const hasType = (m: unknown): m is { type: string } =>
  typeof m === "object" && m !== null && "type" in m;
const sentHas = (w: { sent: unknown[] }, type: string): boolean =>
  w.sent.some((m) => hasType(m) && m.type === type);

describe("instructions envelope", () => {
  test("shape", () => {
    const env = instructionsEnvelope(QUESTIONS, 25);
    expect(env.hz).toBe(25);
    expect(env.questions["finished"]).toEqual({ kind: "noul", prompt: "finished?" });
    expect(env.questions["intent"]).toEqual({ kind: "choice", prompt: "", options: ["support", "sales", "other"] });
    expect((env.questions["urgency"] as Record<string, unknown>).criteria).toEqual(["low", "medium", "high"]);
  });
});

describe("predicate DSL", () => {
  test("actions.top.eq mirrors the question subject", () => {
    const actionsQ = QUESTIONS[3]!;
    const w = wave({
      finished: { choice: true, probabilities: { true: 0.9, false: 0.1 }, confidence: 0.9 },
      intent: { choice: "support", probabilities: { support: 0.8, sales: 0.1, other: 0.1 }, confidence: 0.8 },
      urgency: { probabilities: { "0": 0.1, "1": 0.4, "2": 0.5 }, confidence: 0.7 },
      actions: { choice: "respond", probabilities: { respond: 0.7, ignore: 0.2, wait: 0.1 }, confidence: 0.7 },
    });
    const w0 = wave({
      finished: { choice: true, probabilities: { true: 0.9, false: 0.1 }, confidence: 0.9 },
      intent: { choice: "support", probabilities: { support: 0.8, sales: 0.1, other: 0.1 }, confidence: 0.8 },
      urgency: { probabilities: { "0": 0.1, "1": 0.4, "2": 0.5 }, confidence: 0.7 },
      actions: { choice: "ignore", probabilities: { respond: 0.2, ignore: 0.6, wait: 0.2 }, confidence: 0.6 },
    });
    expect(actionsQ.top.eq("respond")(w)).toBe(true);
    expect(actionsQ.top.eq("respond")(w0)).toBe(false);
  });

  test("p accessors and composition", () => {
    const finished = QUESTIONS[0]!;
    const intent = QUESTIONS[1]!;
    const w = wave({
      finished: { choice: "false", probabilities: { true: 0.55, false: 0.45 }, confidence: 0.55 },
      intent: { choice: "other", probabilities: { support: 0.48, sales: 0.12, other: 0.4 }, confidence: 0.4 },
      urgency: { probabilities: { "0": 0.55, "1": 0.35, "2": 0.1 }, confidence: 0.7 },
      actions: { choice: "wait", probabilities: { respond: 0.2, ignore: 0.2, wait: 0.6 }, confidence: 0.6 },
    });
    expect(finished.p.gte(0.8)(w)).toBe(false);
    expect(finished.p.lt(0.8)(w)).toBe(true);
    expect(intent.p("support").gte(0.7)(w)).toBe(false);
    expect(finished.p.gte(0.5).and(intent.p("other").gte(0.3))(w)).toBe(true);
    expect(finished.p.gte(0.8).or(intent.p("support").gte(0.4))(w)).toBe(true);
    expect(finished.p.lt(0.8).not()(w)).toBe(false);
  });

  test("score accessors", () => {
    const urgency = QUESTIONS[2]!;
    const w = wave({
      finished: { choice: false, probabilities: { true: 0.2, false: 0.8 }, confidence: 0.8 },
      intent: { choice: "other", probabilities: { support: 0.1, sales: 0.15, other: 0.75 }, confidence: 0.75 },
      urgency: { probabilities: { "0": 0.3, "1": 0.5, "2": 0.2 }, confidence: 0.6 },
      actions: { choice: "respond", probabilities: { respond: 0.7, ignore: 0.2, wait: 0.1 }, confidence: 0.7 },
    });
    const u = w.result("urgency") as Score;
    expect(u.value).toBeCloseTo(0.9);
    expect(u.top).toBe("medium");
    expect(u.pAtLeast("medium")).toBeCloseTo(0.7);
    expect(urgency.pAtLeast("high").gte(0.5)(w)).toBe(false);
    expect(urgency.value.gte(0.9)(w)).toBe(true);
    expect(urgency.top.eq("medium")(w)).toBe(true);
  });

  test("between latches across waves", () => {
    const finished = QUESTIONS[0]!;
    const mk = (p: number) =>
      wave({
        finished: { choice: p >= 0.5, probabilities: { true: p, false: 1 - p }, confidence: p },
        intent: { choice: "other", probabilities: { support: 0.1, sales: 0.15, other: 0.75 }, confidence: 0.75 },
        urgency: { probabilities: { "0": 1, "1": 0, "2": 0 }, confidence: 1 },
        actions: { choice: "wait", probabilities: { respond: 0.2, ignore: 0.2, wait: 0.6 }, confidence: 0.6 },
      });
    const pred = finished.p.between(0.8, 0.6);
    expect([0.2, 0.55, 0.85, 0.75, 0.5, 0.9, 0.3, 0.95].map((p) => pred(mk(p)))).toEqual([
      false, false, true, true, false, true, false, true,
    ]);
  });
});

describe("wave decoding", () => {
  test("noul string choice normalizes", () => {
    const w = wave({ finished: { choice: "true", probabilities: { true: 0.9, false: 0.1 }, confidence: 0.9 },
      intent: { choice: "support", probabilities: { support: 1 }, confidence: 1 },
      urgency: { probabilities: { "0": 1, "1": 0, "2": 0 }, confidence: 1 },
      actions: { choice: "wait", probabilities: { respond: 0.2, ignore: 0.2, wait: 0.6 }, confidence: 0.6 } });
    expect(w.result("finished").value).toBe(true);
  });

  test("missing and unknown questions raise", () => {
    expect(() => wave({ finished: { choice: true } })).toThrow(ProtocolError);
    expect(() =>
      Wave.decode(QUESTIONS, {
        wave_id: 0,
        t_ms: 0,
        results: {
          finished: { choice: true, probabilities: {}, confidence: 1 },
          intent: { choice: "support", probabilities: {}, confidence: 1 },
          urgency: { probabilities: { "0": 1, "1": 0, "2": 0 }, confidence: 1 },
          bogus: { choice: true },
        },
      }),
    ).toThrow(ProtocolError);
  });
});

describe("context runs", () => {
  test("duplicate key rejected; replace cancels prior", async () => {
    const ctx = new Context();
    const cancelled: string[] = [];
    const gate1 = Promise.withResolvers<void>();
    const first = ctx.run("a", async () => {
      await gate1.promise;
      return 1;
    }, { onCancel: (k) => cancelled.push(k) });
    await expect(ctx.run("a", () => 2)).rejects.toThrow("already active");
    gate1.resolve();
    await first;
    // replace: cancels the still-active run via signal + onCancel
    const gate2 = Promise.withResolvers<void>();
    const second = ctx.run("a", async () => {
      await gate2.promise;
      return 2;
    }, { replace: true, onCancel: (k) => cancelled.push(k) });
    expect(ctx.signal!.aborted).toBe(false);
    ctx.cancel("a");
    await expect(second).rejects.toThrow("cancelled");
    expect(cancelled).toEqual(["a"]);
    expect(ctx.signal).toBeNull();
    gate2.resolve(); // settle the abandoned fn branch
  });
});

describe("session pieces", () => {
  test("text modes and barge-in send shapes", () => {
    const s = new Session({ apiKey: "k" });
    const sent: unknown[] = [];
    Object.defineProperty(s, "sendJson", { value: (o: unknown) => sent.push(o) });
    s.text.push("hello");
    s.text.push("hello", "append");
    s.text.push("reset", "replace");
    s.text.push("once", "event");
    s.bargeIn();
    expect(sent.at(-1)).toEqual({ type: "response.cancel" });
  });

  test("sessionEvent error shape", () => {
    expect(sessionEvent({ type: "error", error: { code: "x" } }).code).toBe("x");
  });

  test("frame too large refuses at push", () => {
    const s = new Session({ apiKey: "k" });
    expect(() => s.video.push(new Uint8Array(256_001))).toThrow(ProtocolError);
  });
});

describe("sense facade", () => {
  test("registers questions and mirrors accessors", () => {
    const app = new Sense({ apiKey: "k" });
    const finished = app.noul("finished?", { id: "finished" });
    const actions = app.choice("What should the agent do?", ["respond", "ignore", "wait"], { id: "actions" });
    const urgency = app.score("How urgent is the request?", ["low", "medium", "high"], { id: "urgency" });
    const s = app.session();
    expect(s.questions.map((q) => q.id)).toEqual(["finished", "actions", "urgency"]);
    expect(typeof finished.p.gte(0.5)).toBe("function");
    expect(typeof actions.top.eq("respond")).toBe("function");
    expect(typeof urgency.pAtLeast("medium").gte(0.6)).toBe("function");
    expect(app.model).toBe("sense-latest:bf16");
    expect(app.hz).toBe(12.5);
  });

  test("when/onWave install onto new sessions", () => {
    const app = new Sense({ apiKey: "k" });
    app.when(Predicate({ type: "q", id: "finished" }), () => {});
    app.onWave(() => {});
    const s = app.session();
    // handlers live on the session's context; a wave through the context fires them
    const w = Wave.decode(
      [
        new Question({ id: "finished", kind: "noul" }),
        new Question({ id: "actions", kind: "choice", options: ["respond"] }),
        new Question({ id: "urgency", kind: "score", levels: ["low", "high"] }),
      ],
      {
        wave_id: 0,
        t_ms: 0,
        results: {
          finished: { choice: true, probabilities: { true: 0.9, false: 0.1 }, confidence: 0.9 },
          actions: { choice: "respond", probabilities: { respond: 1 }, confidence: 1 },
          urgency: { probabilities: { "0": 1, "1": 0 }, confidence: 1 },
        },
      },
    );
    expect(() => s.context.ingest(w)).not.toThrow();
  });

  test("text-first facade: auto-slug ids, prompt capture, validation", () => {
    const app = new Sense({ apiKey: "k" });
    const a = app.noul("Did the speaker just finish their turn?");
    expect(a.id).toBe("did_the_speaker_just_finish_their_turn");
    expect(a.prompt).toBe("Did the speaker just finish their turn?");
    const b = app.noul("Did the speaker just finish their turn?");
    expect(b.id).toBe("did_the_speaker_just_finish_their_turn_2");
    const intent = app.choice("What does the user want?", ["support", "sales", "other"]);
    expect(intent.prompt).toBe("What does the user want?");
    expect(app.score("How urgent is the request?", ["low", "high"]).prompt).toBe("How urgent is the request?");
    expect(() => app.noul("   ")).toThrow("question text must be a non-empty string (text-first API)");
    expect(() => app.noul("x", { id: "  " })).toThrow("explicit id must be non-empty");
  });
});

describe("coalescing", () => {
  const fullResults = (finished: boolean, intent: string): Record<string, unknown> => ({
    finished: { kind: "noul", choice: finished, probabilities: { true: finished ? 0.9 : 0.1, false: finished ? 0.1 : 0.9 }, confidence: 0.9 },
    intent: { kind: "choice", choice: intent, probabilities: { support: 0.8, sales: 0.1, other: 0.1 }, confidence: 0.8 },
    urgency: { kind: "score", probabilities: { "0": 0.9, "1": 0.08, "2": 0.02 }, confidence: 0.85 },
    actions: { kind: "choice", choice: "respond", probabilities: { respond: 1 }, confidence: 1 },
  });
  const fullWave = (waveId: number, finished: boolean, intent: string): Wave =>
    wave(fullResults(finished, intent), waveId);

  test("context.onWave coalesce skips consecutive same-argmax waves", () => {
    const ctx = new Context();
    const seen: number[] = [];
    ctx.onWave((c) => seen.push(c.latest()!.waveId), { coalesce: true });
    ctx.ingest(fullWave(0, true, "support"));
    ctx.ingest(fullWave(1, true, "support"));
    ctx.ingest(fullWave(2, true, "sales"));
    ctx.ingest(fullWave(3, true, "sales"));
    ctx.ingest(fullWave(4, false, "sales"));
    expect(seen).toEqual([0, 2, 4]);
  });

  test("context.onWave without coalesce delivers every wave", () => {
    const ctx = new Context();
    const seen: number[] = [];
    ctx.onWave((c) => seen.push(c.latest()!.waveId));
    for (let i = 0; i < 3; i++) ctx.ingest(fullWave(i, true, "support"));
    expect(seen).toEqual([0, 1, 2]);
  });

  test("session.onWave coalesce passes through to the context", () => {
    const s = new Session({ apiKey: "k", questions: QUESTIONS });
    const seen: number[] = [];
    s.onWave((c) => seen.push(c.latest()!.waveId), { coalesce: true });
    s.context.ingest(fullWave(0, true, "support"));
    s.context.ingest(fullWave(1, true, "support"));
    s.context.ingest(fullWave(2, false, "sales"));
    expect(seen).toEqual([0, 2]);
  });

  test("sense.onWave carries coalesce onto new sessions", () => {
    const app = new Sense({ apiKey: "k" });
    app.noul("Did the speaker just finish their turn?", { id: "finished" });
    const seen: number[] = [];
    app.onWave((c) => seen.push(c.latest()!.waveId), { coalesce: true });
    const s = app.session();
    const mk = (waveId: number, finished: boolean): Wave =>
      Wave.decode(s.questions, {
        wave_id: waveId,
        t_ms: waveId * 40,
        results: {
          finished: { choice: finished, probabilities: { true: finished ? 0.9 : 0.1, false: finished ? 0.1 : 0.9 }, confidence: 0.9 },
        },
      });
    s.context.ingest(mk(0, true));
    s.context.ingest(mk(1, true));
    s.context.ingest(mk(2, false));
    expect(seen).toEqual([0, 2]);
  });
});

class FakeWire {
  sent: unknown[] = [];
  private messageCbs: ((raw: string) => void)[] = [];
  private closeCbs: ((code?: number, reason?: string) => void)[] = [];
  readonly kind = "ws" as const;
  sendJson(obj: unknown): void {
    this.sent.push(obj);
  }
  onMessage(cb: (raw: string) => void): void {
    this.messageCbs.push(cb);
  }
  onClose(cb: (code?: number, reason?: string) => void): void {
    this.closeCbs.push(cb);
  }
  closed = false;
  async close(): Promise<void> {
    this.closed = true;
  }
  drop(code: number, reason?: string): void {
    for (const cb of this.closeCbs) cb(code, reason);
  }
  /** Deliver one server event to the session. */
  emit(event: unknown): void {
    for (const cb of this.messageCbs) cb(JSON.stringify(event));
  }
}

/** Wire factory that records dial keys/wires; `onSecond` hooks the reconnect
 * dial so tests can await it deterministically. */
function recordingFactory(
  keys: string[],
  wires: FakeWire[],
  onSecond?: (w: FakeWire) => void,
): WireDialer {
  return (opts) => {
    keys.push(opts.apiKey);
    const w = new FakeWire();
    wires.push(w);
    if (wires.length === 2) onSecond?.(w);
    return Promise.resolve(w as never);
  };
}

/** The wire-frame `type` fields a FakeWire was sent, in order. */
function typesOnFactory(w: FakeWire): string[] {
  return w.sent.map((m) => {
    if (typeof m === "object" && m !== null && "type" in m && typeof m.type === "string") return m.type;
    return "";
  });
}

/** Flush pending microtasks after a synchronous fake-timer advance (bun's vi
 * exposes only the sync timer API). */
async function drain(n = 8): Promise<void> {
  for (let i = 0; i < n; i++) await Promise.resolve();
}

/** The instructions envelope of the (single) session.update in `sent`. */
function updateEnvelope(sent: unknown[]): Record<string, unknown> | undefined {
  const update = sent.find(
    (m) => typeof m === "object" && m !== null && "type" in m && m.type === "session.update",
  );
  if (update === undefined || update === null || typeof update !== "object" || !("session" in update))
    return undefined;
  const session = update.session;
  if (typeof session !== "object" || session === null || !("instructions" in session)) return undefined;
  const instructions = session.instructions;
  if (typeof instructions !== "string") return undefined;
  return JSON.parse(instructions) as Record<string, unknown>;
}

describe("apiKey provider", () => {
  test("re-resolves the provider on every dial, including the session_gone reconnect", async () => {
    let n = 0;
    const keys: string[] = [];
    const wires: FakeWire[] = [];
    const reDialed = Promise.withResolvers<void>();
    const s = new Session({
      apiKey: () => `key-${++n}`,
      questions: QUESTIONS,
      wireFactory: recordingFactory(keys, wires, () => reDialed.resolve()),
    });
    vi.useFakeTimers();
    try {
      await s.connect();
      expect(keys).toEqual(["key-1"]);
      wires[0]!.drop(1011);
      vi.advanceTimersByTime(60_000);
      await reDialed.promise;
      expect(keys).toEqual(["key-1", "key-2"]);
      expect(s.stats.reconnects).toBe(1);
      await s.close();
    } finally {
      vi.useRealTimers();
    }
  });

  test("a static apiKey still works and is sent on the dial", async () => {
    const keys: string[] = [];
    const wires: FakeWire[] = [];
    const s = new Session({
      apiKey: "k",
      questions: QUESTIONS,
      wireFactory: recordingFactory(keys, wires),
    });
    await s.connect();
    expect(keys).toEqual(["k"]);
    await s.close();
  });
});

describe("reconnect policy", () => {
  test("reconnects on a non-1011 wire death (relay churn) and keeps the questions", async () => {
    const keys: string[] = [];
    const wires: FakeWire[] = [];
    const updateSent = Promise.withResolvers<void>();
    const s = new Session({
      apiKey: "k",
      questions: QUESTIONS,
      wireFactory: recordingFactory(keys, wires, (w) => {
        const orig = w.sendJson.bind(w);
        w.sendJson = (obj: unknown) => {
          orig(obj);
          if (typeof obj === "object" && obj !== null && "type" in obj && obj.type === "session.update") {
            updateSent.resolve();
          }
        };
      }),
    });
    vi.useFakeTimers();
    try {
      await s.connect();
      // The hub realtime relay maps an abnormal upstream close onto 1000, and a
      // dead relay worker leaves the client with no close frame at all — none of
      // these may end the session silently.
      wires[0]!.drop(1000);
      wires[0]!.drop(1000); // RtcWire fires close twice per death: no duplicate redial
      vi.advanceTimersByTime(60_000);
      await updateSent.promise;
      expect(keys).toEqual(["k", "k"]);
      expect(s.stats.reconnects).toBe(1);
      // Questions are schema, not conversation state: the fresh dial's
      // session.update must still carry the registered envelope.
      const envelope = updateEnvelope(wires[1]!.sent);
      expect(envelope !== undefined && JSON.stringify(envelope).includes('"finished"')).toBe(true);
      await s.close();
    } finally {
      vi.useRealTimers();
    }
  });

  test("a wire death after close() does not redial", async () => {
    const keys: string[] = [];
    const wires: FakeWire[] = [];
    const s = new Session({
      apiKey: "k",
      questions: QUESTIONS,
      wireFactory: recordingFactory(keys, wires),
    });
    await s.connect();
    await s.close();
    // drop() fires onClose synchronously; a redial would push a key synchronously.
    wires[0]!.drop(1011);
    expect(keys).toEqual(["k"]);
  });

  test("sustained young-wire churn ends loudly at the redial budget (hub-ended session)", async () => {
    vi.useFakeTimers();
    try {
      const keys: string[] = [];
      const wires: FakeWire[] = [];
      const events: SessionEvent[] = [];
      const s = new Session({
        apiKey: "k",
        questions: QUESTIONS,
        wireFactory: recordingFactory(keys, wires),
      });
      s.onEvent((ev) => events.push(ev));
      await s.connect();
      // Nine young deaths redial with a growing, jittered delay; the wire is
      // killed the instant each redial lands (real churn: accepted then closed).
      for (let i = 0; i < 9; i++) {
        wires[wires.length - 1]!.drop(1000);
        let guard = 0;
        while (wires.length === i + 1 && guard++ < 2000) {
          vi.advanceTimersByTime(100);
          await drain();
        }
        expect(wires.length).toBe(i + 2);
      }
      expect(keys.length).toBe(10);
      // The tenth consecutive young death is the budget: terminal, loud, no spin.
      wires[wires.length - 1]!.drop(1000);
      vi.advanceTimersByTime(60_000);
      await drain();
      expect(events.filter((e) => e.type === "redial_failed").length).toBe(1);
      expect(keys.length).toBe(10); // no further dials
      // A duplicate close-callback after the budget is a no-op: exactly one
      // terminal event, no double-counted reconnects.
      wires[wires.length - 1]!.drop(1000);
      await drain();
      expect(events.filter((e) => e.type === "redial_failed").length).toBe(1);
      expect(s.stats.reconnects).toBe(10);
      await s.close();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("wedge self-heal", () => {
  test("a wedged lane (response_in_progress 3x, no waves) redials fresh and keeps state", async () => {
    const keys: string[] = [];
    const wires: FakeWire[] = [];
    const reDialed = Promise.withResolvers<void>();
    const s = new Session({
      apiKey: "k",
      questions: QUESTIONS,
      wireFactory: (opts) => {
        keys.push(opts.apiKey);
        const w = new FakeWire();
        // A real wire reports its own close() through onClose.
        w.close = async () => {
          w.closed = true;
          w.drop(0);
        };
        wires.push(w);
        if (wires.length === 2) reDialed.resolve();
        return Promise.resolve(w as never);
      },
    });
    await s.connect();
    s.text.push("the pushed state", "replace");
    const refuse = { type: "error", error: { code: "response_in_progress" } };
    wires[0]!.emit(refuse);
    wires[0]!.emit(refuse);
    expect(wires.length).toBe(1); // the ordinary cancel race: back off, no redial
    wires[0]!.emit(refuse);
    await reDialed.promise;
    await drain(); // install() sends session.update after the dial resolves
    expect(wires[0]!.closed).toBe(true);
    expect(s.stats.reconnects).toBe(1);
    // Questions and the pushed state both ride the fresh dial.
    expect(JSON.stringify(updateEnvelope(wires[1]!.sent))).toContain('"finished"');
    s.audio.push(new Uint8Array(1920));
    const loop = s.run().next(); // opens the turn, then parks for a wave
    await drain();
    const text = wires[1]!.sent.find(
      (m) => typeof m === "object" && m !== null && "type" in m && m.type === "conversation.item.create",
    );
    expect(JSON.stringify(text)).toContain("the pushed state");
    await s.close();
    await loop;
  });
});

describe("reconnect window", () => {
  test("audio appended during the outage is replayed and committed on the fresh wire", async () => {
    vi.useFakeTimers();
    try {
      const keys: string[] = [];
      const wires: FakeWire[] = [];
      const s = new Session({ apiKey: "k", questions: QUESTIONS, wireFactory: recordingFactory(keys, wires) });
      await s.connect();
      const consuming = s.run().next(); // the parked loop wakes after reconnect
      await drain();
      wires[0]!.drop(1000);
      s.audio.push(new Uint8Array(48)); // during the backoff window: sendJson would no-op
      vi.advanceTimersByTime(60_000);
      await drain();
      expect(wires.length).toBe(2);
      const types = typesOnFactory(wires[1]!);
      expect(types).toContain("session.update");
      expect(types.filter((t) => t === "input_audio_buffer.append").length).toBe(1); // replayed exactly once
      vi.advanceTimersByTime(1_000); // wake opens the turn binding the carried audio
      await drain();
      const now = typesOnFactory(wires[1]!);
      expect(now).toContain("input_audio_buffer.commit");
      expect(now).toContain("response.create");
      await s.close();
      expect((await consuming).done).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  test("a wire dialed when close() lands mid-dial is closed, not adopted", async () => {
    vi.useFakeTimers();
    try {
      const keys: string[] = [];
      const wires: FakeWire[] = [];
      const gates: Array<ReturnType<typeof Promise.withResolvers<never>>> = [];
      const s = new Session({
        apiKey: "k",
        questions: QUESTIONS,
        wireFactory: (opts) => {
          keys.push(opts.apiKey);
          const w = new FakeWire();
          wires.push(w);
          if (wires.length === 1) return Promise.resolve(w as never); // initial dial lands
          const g = Promise.withResolvers<never>(); // only the REDIAL is gated
          gates.push(g);
          return g.promise;
        },
      });
      await s.connect();
      wires[0]!.drop(1000);
      vi.advanceTimersByTime(2_000); // the redial timer fires; the dial is now in flight
      await drain();
      expect(gates.length).toBe(1);
      await s.close(); // lands MID-DIAL: the session wire is still null
      const late = new FakeWire();
      gates[0]!.resolve(late as never);
      await drain();
      expect(late.closed).toBe(true); // closed, not adopted
      expect(late.sent).toEqual([]); // nothing latched onto it
      expect(keys.length).toBe(2);
      await s.close();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("run loop on wire death", () => {
  test("run loop never opens a turn into the void; it reopens on the live wire", async () => {
    vi.useFakeTimers();
    try {
      const keys: string[] = [];
      const wires: FakeWire[] = [];
      const s = new Session({
        apiKey: "k",
        questions: QUESTIONS,
        wireFactory: recordingFactory(keys, wires),
      });
      await s.connect();
      const consuming = s.run((async function* () {
        yield { audio: new Uint8Array(48) };
      })()).next();
      await drain(); // input applied, first turn opens on wire 1
      const first = wires[0]!.sent.map((m) => (typeof m === "object" && m !== null && "type" in m ? m.type : ""));
      expect(first).toContain("input_audio_buffer.commit");
      expect(first).toContain("response.create");
      wires[0]!.drop(1000); // death mid-turn with input consumed; redial pending
      vi.advanceTimersByTime(300); // park-wake fires while the wire is null
      await drain();
      expect(wires.length).toBe(1); // redial still backing off
      const typesOn = (w: FakeWire) => w.sent.map((m) => (typeof m === "object" && m !== null && "type" in m ? m.type : ""));
      expect(typesOn(wires[0]!).filter((t) => t === "response.create").length).toBe(1); // no turn opened into the void
      vi.advanceTimersByTime(60_000); // redial lands
      await drain(); // connect completes: wire live, session.update sent
      expect(wires.length).toBe(2);
      // New input after the reconnect: the parked loop wakes and opens the
      // turn on the LIVE wire (never into the void), and the commit binds the
      // post-reconnect audio — the pre-death buffer was dropped, so the
      // reopened turn is never an empty one.
      s.audio.push(new Uint8Array(48));
      vi.advanceTimersByTime(1_000); // wake re-evaluates on the live wire
      await drain();
      const second = typesOn(wires[1]!);
      expect(second).toContain("session.update");
      expect(second).toContain("response.create"); // turn reopened on the LIVE wire, not latched
      expect(second).toContain("input_audio_buffer.commit"); // fresh audio bound
      await s.close();
      expect((await consuming).done).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("setQuestions", () => {
  test("replaces the registry (removals included), sends session.update, and barges in so the next turn reopens with the new schema", async () => {
    const w = new FakeWire();
    const s = new Session({ apiKey: "k", questions: QUESTIONS, wireFactory: () => Promise.resolve(w as never) });
    await s.connect();
    w.sent.length = 0;
    s.setQuestions([new Question({ id: "holding", kind: "choice", options: ["a red mug", "a phone", "nothing"] })]);
    const update = w.sent.find((m) => hasType(m) && m.type === "session.update") as {
      session: { instructions: string };
    };
    const envelope = JSON.parse(update.session.instructions);
    expect(Object.keys(envelope.questions)).toEqual(["holding"]);
    // A live cadence turn never ends on its own — the barge-in comes FIRST
    // (an envelope sent mid-response wedges the lane), then the latch.
    const cancelIdx = w.sent.findIndex((m) => hasType(m) && m.type === "response.cancel");
    const updateIdx = w.sent.findIndex((m) => hasType(m) && m.type === "session.update");
    expect(cancelIdx).toBeGreaterThanOrEqual(0);
    expect(updateIdx).toBeGreaterThan(cancelIdx);
    expect(s.questions.map((q) => q.id)).toEqual(["holding"]);
    await s.close();
  });
});

describe("turn boundary on cancel", () => {
  class FakeWire {
    sent: unknown[] = [];
    messageCb: ((raw: string) => void) | null = null;
    readonly kind = "ws" as const;
    sendJson(obj: unknown): void {
      this.sent.push(obj);
    }
    onMessage(cb: (raw: string) => void): void {
      this.messageCb = cb;
    }
    onClose(_cb: (code?: number) => void): void {}
    async close(): Promise<void> {}
  }

  const waitFor = async (check: () => boolean, ms = 2_000): Promise<void> => {
    for (let i = 0; i < ms / 25 && !check(); i++) await new Promise((r) => setTimeout(r, 25));
    if (!check()) throw new Error("condition not met in time");
  };

  test("response.cancelled closes the turn, and a parked run loop re-opens it once input flows", async () => {
    const w = new FakeWire();
    const s = new Session({ apiKey: "k", questions: QUESTIONS, wireFactory: () => Promise.resolve(w as never) });
    await s.connect();
    void (async () => {
      for await (const _ of s.run()) {
        // discard; the loop must keep turning
      }
    })();
    s.audio.push(new Uint8Array(960));
    await waitFor(() => sentHas(w, "response.create"), 2_000);
    // barge-in: response.cancel out, response.cancelled back — then the
    // cadence is dead, so only the wake can re-open the turn.
    s.bargeIn();
    w.messageCb?.(JSON.stringify({ type: "response.cancelled" }));
    w.sent.length = 0;
    s.audio.push(new Uint8Array(960));
    await waitFor(() => sentHas(w, "response.create"), 2_000);
    expect(s.stats.turns).toBe(2);
    await s.close();
  });

  test("a response_in_progress refusal backs off instead of hammering the lane", async () => {
    const w = new FakeWire();
    const s = new Session({ apiKey: "k", questions: QUESTIONS, wireFactory: () => Promise.resolve(w as never) });
    await s.connect();
    void (async () => {
      for await (const _ of s.run()) {
        // discard
      }
    })();
    s.audio.push(new Uint8Array(960));
    await waitFor(() => sentHas(w, "response.create"), 2_000);
    s.bargeIn();
    w.messageCb?.(JSON.stringify({ type: "response.cancelled" }));
    w.sent.length = 0;
    s.audio.push(new Uint8Array(960));
    await waitFor(() => sentHas(w, "response.create"), 2_000);
    // The server refuses the reopen: the client must go quiet, not retry at
    // wake cadence, until the backoff elapses.
    w.sent.length = 0;
    w.messageCb?.(JSON.stringify({ type: "error", error: { code: "response_in_progress" } }));
    await new Promise((r) => setTimeout(r, 1_000));
    expect(sentHas(w, "response.create")).toBe(false);
    s.audio.push(new Uint8Array(960));
    await waitFor(() => sentHas(w, "response.create"), 4_000);
    await s.close();
  });
});

describe("turn-terminal visibility", () => {
  test("failed and cancelled reach onEvent; done/completed stay run-loop-internal", async () => {
    const keys: string[] = [];
    const wires: FakeWire[] = [];
    const events: SessionEvent[] = [];
    const s = new Session({
      apiKey: "k",
      questions: QUESTIONS,
      wireFactory: recordingFactory(keys, wires),
    });
    s.onEvent((ev) => events.push(ev));
    await s.connect();
    const w = wires[wires.length - 1]!;
    w.emit({ type: "response.created", response: { id: "r1" } });
    w.emit({ type: "response.done", response: { id: "r1", status: "completed" } });
    w.emit({ type: "response.failed", response: { id: "r2", status: "failed", error: { code: "upstream_x", message: "boom" } } });
    w.emit({ type: "response.cancelled", response: { id: "r3", status: "cancelled" } });
    const types = events.map((e) => e.type);
    expect(types).toContain("response.failed");
    expect(types).toContain("response.cancelled");
    expect(types).not.toContain("response.done");
    await s.close();
  });
});

describe("out of credits", () => {
  test("insufficient_credits error event is terminal: onEvent + typed throw, no redial", async () => {
    const keys: string[] = [];
    const wires: FakeWire[] = [];
    const s = new Session({
      apiKey: "k",
      questions: QUESTIONS,
      wireFactory: recordingFactory(keys, wires),
    });
    await s.connect();
    const events: SessionEvent[] = [];
    s.onEvent((ev) => events.push(ev));
    const it = s.waves()[Symbol.asyncIterator]();
    const next = it.next(); // parks a waiter, as a live consumer would
    wires[0]!.emit({
      type: "error",
      event_id: "e1",
      error: { code: "insufficient_credits", message: "the organization is out of credits — top up to continue" },
    });
    // The raw error event stays visible to onEvent listeners (contract: the
    // console maps error.code 'insufficient_credits' to the Out-of-credits UI).
    expect(events.some((e) => e.type === "error" && e.code === "insufficient_credits")).toBe(true);
    // The terminal end carries the same code.
    expect(events.find((e) => e.type === "redial_failed")?.code).toBe("insufficient_credits");
    // run()/waves() surface the typed error, not a quiet stream end.
    await expect(next).rejects.toBeInstanceOf(OutOfCreditsError);
    // The server's 1011 close after the error event must NOT redial.
    wires[0]!.drop(1011);
    await new Promise((r) => setTimeout(r, 20));
    expect(keys).toEqual(["k"]);
    expect(s.stats.reconnects).toBe(0);
    await s.close();
  });

  test("a 1011 close with reason 'credits_exhausted' is terminal even without an error event", async () => {
    const keys: string[] = [];
    const wires: FakeWire[] = [];
    const s = new Session({
      apiKey: "k",
      questions: QUESTIONS,
      wireFactory: recordingFactory(keys, wires),
    });
    await s.connect();
    const events: SessionEvent[] = [];
    s.onEvent((ev) => events.push(ev));
    const it = s.waves()[Symbol.asyncIterator]();
    const next = it.next();
    wires[0]!.drop(1011, "credits_exhausted");
    await expect(next).rejects.toBeInstanceOf(OutOfCreditsError);
    expect(keys).toEqual(["k"]);
    expect(s.stats.reconnects).toBe(0);
    expect(events.find((e) => e.type === "redial_failed")?.code).toBe("insufficient_credits");
    await s.close();
  });

  test("a 402 dial refusal surfaces OutOfCreditsError and never retries", async () => {
    vi.useFakeTimers();
    try {
      let dials = 0;
      const s = new Session({
        apiKey: "k",
        questions: QUESTIONS,
        wireFactory: () => {
          dials++;
          return Promise.reject(new OutOfCreditsError());
        },
      });
      const settled = s.connect().then(
        () => "resolved",
        (e: unknown) => e,
      );
      await drain();
      expect(await settled).toBeInstanceOf(OutOfCreditsError);
      // The §7 dial backoff must not keep retrying a paid-quota refusal.
      vi.advanceTimersByTime(60_000);
      await drain();
      expect(dials).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });
});

/** Security regressions: transport policy, decoder integrity, resource bounds,
 * and containment of application errors. Offline only (fake wires). */
import { describe, expect, test, vi } from "bun:test";
import { Context, ProtocolError, Question, Sense, Session, Wave } from "../src/index.ts";
import { HISTORY_MAX } from "../src/context.ts";
import { errMessage } from "../src/errors.ts";
import { MAX_LINE_BYTES, NdjsonSplitter, DialRefused, checkBaseUrl, defaultDialers, parseCallsAnswer } from "../src/transport.ts";

const QUESTIONS = [
  new Question({ id: "finished", kind: "noul" }),
  new Question({ id: "intent", kind: "choice", options: ["support", "other"] }),
  new Question({ id: "urgency", kind: "score", levels: ["low", "high"] }),
];

const GOOD: Record<string, Record<string, unknown>> = {
  finished: { choice: true, probabilities: { true: 0.9, false: 0.1 }, confidence: 0.9 },
  intent: { choice: "support", probabilities: { support: 0.8, other: 0.2 }, confidence: 0.8 },
  urgency: { probabilities: { "0": 0.3, "1": 0.7 }, confidence: 0.7 },
};

function decodeWith(overrides: Record<string, Record<string, unknown>>): Wave {
  return Wave.decode(QUESTIONS, { wave_id: 0, t_ms: 0, results: { ...GOOD, ...overrides } });
}

class FakeWire {
  readonly kind = "ws" as const;
  sent: unknown[] = [];
  messageCb: ((raw: string) => void) | null = null;
  closeCb: ((code?: number) => void) | null = null;
  sendJson(obj: unknown): void {
    this.sent.push(obj);
  }
  onMessage(cb: (raw: string) => void): void {
    this.messageCb = cb;
  }
  onClose(cb: (code?: number) => void): void {
    this.closeCb = cb;
  }
  async close(): Promise<void> {}
  /** Deliver one complete wave as decision lines, then turn completion. */
  deliverWave(): void {
    for (const [id, ans] of Object.entries(GOOD)) {
      const line = JSON.stringify({ event: "sense.decision", id, ...ans });
      this.messageCb?.(JSON.stringify({ type: "response.output_text.delta", delta: `${line}\n` }));
    }
    this.messageCb?.(JSON.stringify({ type: "response.completed" }));
  }
}

async function connected(): Promise<{ s: Session; w: FakeWire }> {
  const w = new FakeWire();
  const s = new Session({ apiKey: "k", questions: QUESTIONS, wireFactory: async () => w as never });
  await s.connect();
  return { s, w };
}

describe("base URL policy", () => {
  test("https/wss and loopback plaintext are accepted", () => {
    for (const u of ["https://api.example", "wss://api.example", "http://127.0.0.1:9", "ws://localhost:9", "http://[::1]:9"]) {
      expect(checkBaseUrl(u)).toBe(u);
    }
  });

  test("remote plaintext, embedded credentials and other schemes are refused", () => {
    for (const u of ["http://api.example", "ws://10.0.0.5", "http://127.0.0.1.evil.example", "http://localhost.evil.example", "https://user:pw@api.example", "ftp://api.example", "api.example"]) {
      expect(() => checkBaseUrl(u)).toThrow(TypeError);
    }
  });

  test("Sense validates the base URL at construction", () => {
    expect(() => new Sense({ apiKey: "k", baseUrl: "http://api.example" })).toThrow(TypeError);
  });

  test("allowInsecure is an explicit opt-in on Session and Sense", () => {
    expect(() => new Session({ apiKey: "k", baseUrl: "http://api.example" })).toThrow(TypeError);
    expect(new Session({ apiKey: "k", baseUrl: "http://api.example", allowInsecure: true }).baseUrl).toBe("http://api.example");
    expect(new Sense({ apiKey: "k", baseUrl: "http://api.example", allowInsecure: true }).session().baseUrl).toBe("http://api.example");
  });
});

describe("dial", () => {
  test("a missing API key fails closed instead of dialing with an empty bearer", async () => {
    let dialed = false;
    const s = new Session({
      apiKey: () => "",
      questions: QUESTIONS,
      wireFactory: async () => {
        dialed = true;
        return new FakeWire() as never;
      },
    });
    await expect(s.connect()).rejects.toThrow(ProtocolError);
    expect(dialed).toBe(false);
  });

  test("a refused WebSocket dial rejects instead of crashing the host process", async () => {
    const dial = defaultDialers.ws({
      transport: "ws",
      apiBaseUrl: "http://127.0.0.1:1",
      apiKey: "k",
      model: "m",
      wsUrl: "ws://127.0.0.1:1/v1/realtime",
    });
    await expect(dial).rejects.toBeDefined();
    await new Promise((r) => setTimeout(r, 100)); // any late 'error' event must stay handled
  });

  test("QUIC calls answer: empty certHashes fail closed; malformed answers are refused", () => {
    const ok = { callId: "c", url: "https://edge:443/wt", token: "t", certHashes: ["AAAA"] };
    expect(parseCallsAnswer(ok).certHashes).toEqual(["AAAA"]);
    expect(() => parseCallsAnswer({ ...ok, certHashes: [] })).toThrow(/certHashes/);
    expect(() => parseCallsAnswer({ ...ok, certHashes: undefined })).toThrow(/certHashes/);
    expect(() => parseCallsAnswer({ ...ok, url: 7 })).toThrow(/url/);
    expect(() => parseCallsAnswer(null)).toThrow();
  });
});

describe("decoder integrity", () => {
  test("well-formed waves still decode", () => {
    expect(decodeWith({}).result("intent").value).toBe("support");
  });

  test.each([
    ["missing confidence", { finished: { choice: true, probabilities: { true: 0.9 } } }],
    ["non-numeric probability", { finished: { choice: true, probabilities: { true: "high" }, confidence: 1 } }],
    ["probability above 1", { finished: { choice: true, probabilities: { true: 7 }, confidence: 1 } }],
    ["noul without P(true)", { finished: { choice: true, probabilities: {}, confidence: 1 } }],
    ["array probabilities", { finished: { choice: true, probabilities: [0.9], confidence: 1 } }],
    ["choice outside registered options", { intent: { choice: "<img src=x onerror=alert(1)>", probabilities: {}, confidence: 1 } }],
    ["score missing a level", { urgency: { probabilities: { "0": 1 }, confidence: 1 } }],
    ["score level index out of range", { urgency: { probabilities: { "0": 0.5, "1": 0.5, "9": 0 }, confidence: 1 } }],
  ])("%s is a protocol error", (_name, override) => {
    expect(() => decodeWith(override as Record<string, Record<string, unknown>>)).toThrow(ProtocolError);
  });

  test("a malformed wave on the wire is counted, not delivered, and the session survives", async () => {
    const { s, w } = await connected();
    const bad = JSON.stringify({ event: "sense.decision", id: "intent", choice: "evil", probabilities: {}, confidence: 1 });
    w.messageCb?.(JSON.stringify({ type: "response.output_text.delta", delta: `${bad}\n` }));
    w.messageCb?.(JSON.stringify({ type: "response.completed" }));
    expect(s.stats.protocolErrors).toBe(1);
    w.deliverWave();
    expect(s.stats.waves).toBe(1);
    await s.close();
  });
});

describe("resource bounds", () => {
  test("NdjsonSplitter refuses an unbounded partial line but keeps complete ones", () => {
    const sp = new NdjsonSplitter();
    expect(sp.push("a\nb")).toEqual(["a"]);
    expect(sp.push(`c\n${"x".repeat(MAX_LINE_BYTES + 1)}`)).toEqual(["bc"]);
    expect(sp.overflowed).toBe(true);
  });

  test("the line cap counts UTF-8 bytes, not UTF-16 units", () => {
    const sp = new NdjsonSplitter();
    sp.push("€".repeat(100_000)); // 100k units, 300 KB of UTF-8
    expect(sp.overflowed).toBe(true);
  });

  test("named audio sources keep a bounded backlog", async () => {
    const { s } = await connected();
    for (let i = 0; i < 2000; i++) s.audio.push(new Uint8Array(2), "mic2");
    expect((s as unknown as { sourceQueues: Map<string, string[]> }).sourceQueues.get("mic2")!.length).toBe(1500);
    await s.close();
  });

  test("a non-finite t_ms is a protocol error", () => {
    expect(() => Wave.decode(QUESTIONS, { wave_id: 0, t_ms: Infinity, results: GOOD })).toThrow(ProtocolError);
  });

  test("context history is bounded", async () => {
    const { s, w } = await connected();
    for (let i = 0; i < HISTORY_MAX + 50; i++) w.deliverWave();
    expect(s.context.waves().length).toBe(HISTORY_MAX);
    await s.close();
  });
});

describe("application error containment", () => {
  test("a throwing onWave handler or predicate does not escape the wire callback", async () => {
    const { s, w } = await connected();
    let after = 0;
    s.onWave(() => {
      throw new Error("app bug");
    });
    s.when(
      Object.assign(() => {
        throw new Error("pred bug");
      }, {}) as never,
      () => undefined,
    );
    s.onWave(() => {
      after++;
    });
    expect(() => w.deliverWave()).not.toThrow();
    expect(after).toBe(1);
    expect(s.stats.waves).toBe(1);
    await s.close();
  });

  test("formatting an unprintable thrown value cannot itself throw", () => {
    expect(errMessage(Object.create(null))).toBe("[unprintable error]");
    const hostile = new Error("x");
    Object.defineProperty(hostile, "message", {
      get() {
        throw new Error("getter");
      },
    });
    expect(errMessage(hostile)).toBe("[unprintable error]");
    const ctx = new Context();
    ctx.onWave(() => {
      throw Object.create(null);
    });
    expect(() => ctx.ingest(decodeWith({}))).not.toThrow();
  });

  test("an async handler rejection is caught", async () => {
    const ctx = new Context();
    ctx.onWave(async () => {
      throw new Error("async bug");
    });
    expect(() => ctx.ingest(decodeWith({}))).not.toThrow();
    await new Promise((r) => setTimeout(r, 0));
  });
});

describe("wire loss", () => {
  test("a consumer between reads still sees the end of the stream", async () => {
    vi.useFakeTimers();
    try {
      const { s, w } = await connected();
      w.deliverWave();
      w.closeCb?.(1006); // no waiter is parked when the wire dies
      // A silent death reconnects instead of ending the streams (§7), so the
      // end comes from the young-death redial budget: each redialed wire dies
      // young again until the budget is exhausted.
      for (let i = 0; i < 40; i++) {
        vi.advanceTimersByTime(60_000); // past any ladder delay plus jitter
        for (let d = 0; d < 16; d++) await Promise.resolve(); // drain the redial chain
        w.closeCb?.(1006);
      }
      const it = s.waves()[Symbol.asyncIterator]();
      expect((await it.next()).done).toBe(false); // the queued wave drains first
      expect((await it.next()).done).toBe(true);
      await s.close();
    } finally {
      vi.useRealTimers();
    }
  });

  test("no turn is opened while there is no wire", async () => {
    let n = 0;
    const w = new FakeWire();
    const s = new Session({
      apiKey: "k",
      questions: QUESTIONS,
      // The session_gone redial never completes, so the wire stays null.
      wireFactory: () => (n++ === 0 ? Promise.resolve(w as never) : new Promise(() => undefined)),
    });
    await s.connect();
    const it = s.run()[Symbol.asyncIterator]();
    void it.next(); // the run loop is live and parked on the live wire
    await new Promise((r) => setTimeout(r, 10));
    const turnsBefore = s.stats.turns;
    w.closeCb?.(1011);
    s.audio.push(new Uint8Array(2)); // input arrives while the redial is pending
    await new Promise((r) => setTimeout(r, 400)); // > one 250 ms run-loop wake
    expect(s.stats.turns).toBe(turnsBefore);
    await s.close();
  });

  test("a late duplicate close from a replaced wire does not disturb the live one", async () => {
    const wires: FakeWire[] = [];
    const s = new Session({
      apiKey: "k",
      questions: QUESTIONS,
      wireFactory: async () => {
        const w = new FakeWire();
        wires.push(w);
        return w as never;
      },
    });
    await s.connect();
    wires[0]!.closeCb?.(1011); // death → immediate redial
    await new Promise((r) => setTimeout(r, 10));
    expect(wires.length).toBe(2);
    // RtcWire fires close from two sources; the second can arrive after the
    // replacement is live and must be attributed to nobody.
    wires[0]!.closeCb?.(1006);
    await new Promise((r) => setTimeout(r, 10));
    expect(s.stats.reconnects).toBe(1);
    await s.close();
  });

  test("no spurious turn opens on the fresh wire without new input", async () => {
    const { s, w } = await connected();
    const it = s.run()[Symbol.asyncIterator]();
    void it.next(); // the run loop is live
    s.audio.push(new Uint8Array(2)); // input since the (closed) turn
    await new Promise((r) => setTimeout(r, 10));
    const createsBefore = w.sent.filter((m) => (m as { type?: string }).type === "response.create").length;
    w.closeCb?.(1011); // death → immediate redial (the same fake wire returns)
    await new Promise((r) => setTimeout(r, 400)); // > one run-loop wake
    const createsAfter = w.sent.filter((m) => (m as { type?: string }).type === "response.create").length;
    expect(createsAfter).toBe(createsBefore); // an empty model turn would spend tokens
    await s.close();
  });
});

describe("session_gone redial races", () => {
  test("close() during a pending redial closes the late wire instead of installing it", async () => {
    let n = 0;
    const first = new FakeWire();
    const late = new FakeWire();
    let lateClosed = false;
    late.close = async () => {
      lateClosed = true;
    };
    let release!: () => void;
    const s = new Session({
      apiKey: "k",
      questions: QUESTIONS,
      wireFactory: () =>
        n++ === 0 ? Promise.resolve(first as never) : new Promise((r) => (release = () => r(late as never))),
    });
    await s.connect();
    first.closeCb?.(1011);
    await new Promise((r) => setTimeout(r, 5));
    await s.close();
    release();
    await new Promise((r) => setTimeout(r, 5));
    expect(lateClosed).toBe(true);
    expect(late.sent).toEqual([]); // no session.update on a closed session
  });

  test("audio pushed during a redial is replayed before the next turn opens", async () => {
    let n = 0;
    const first = new FakeWire();
    const second = new FakeWire();
    let release!: () => void;
    const s = new Session({
      apiKey: "k",
      questions: QUESTIONS,
      wireFactory: () =>
        n++ === 0 ? Promise.resolve(first as never) : new Promise((r) => (release = () => r(second as never))),
    });
    await s.connect();
    first.closeCb?.(1011);
    s.audio.push(new Uint8Array([1, 2]));
    release();
    await new Promise((r) => setTimeout(r, 5));
    const types = second.sent.map((m) => (m as { type: string }).type);
    expect(types).toEqual(["session.update", "input_audio_buffer.append"]);
    await s.close();
  });
});

describe("session_gone (1011)", () => {
  test("keeps the question registry so the new session's waves still decode", async () => {
    const wires: FakeWire[] = [];
    const s = new Session({
      apiKey: "k",
      questions: QUESTIONS,
      wireFactory: async () => {
        const w = new FakeWire();
        wires.push(w);
        return w as never;
      },
    });
    await s.connect();
    wires[0]!.closeCb?.(1011);
    for (let i = 0; i < 50 && !wires[1]?.messageCb; i++) await new Promise((r) => setTimeout(r, 5));
    expect(s.questions.length).toBe(QUESTIONS.length);
    wires[1]!.deliverWave();
    expect(s.stats.waves).toBe(1);
    expect(s.stats.protocolErrors).toBe(0);
    await s.close();
  });

  test("a failed redial ends the streams instead of an unhandled rejection", async () => {
    let n = 0;
    const first = new FakeWire();
    const s = new Session({
      apiKey: "k",
      questions: QUESTIONS,
      wireFactory: async () => {
        if (n++ === 0) return first as never;
        // A hostile thrown value: formatting it inside endFatal must not
        // crash the rejection handler itself.
        throw new Proxy({}, { get: () => { throw new Error("boom"); } });
      },
    });
    await s.connect();
    const unhandled: unknown[] = [];
    const onUnhandled = (e: unknown) => unhandled.push(e);
    process.on("unhandledRejection", onUnhandled);
    const it = s.waves()[Symbol.asyncIterator]();
    const next = it.next();
    first.closeCb?.(1011);
    expect((await next).done).toBe(true);
    await new Promise((r) => setTimeout(r, 10));
    process.off("unhandledRejection", onUnhandled);
    expect(unhandled).toEqual([]);
    await s.close();
  });

  test("close() during dial-refusal backoff stops dialing", async () => {
    vi.useFakeTimers();
    try {
      let dials = 0;
      const s = new Session({
        apiKey: "k",
        questions: QUESTIONS,
        wireFactory: async () => {
          dials++;
          throw new DialRefused(503);
        },
      });
      void s.connect().catch(() => {}); // refusal → backoff timer scheduled
      for (let d = 0; d < 16; d++) await Promise.resolve();
      expect(dials).toBe(1);
      await s.close();
      vi.advanceTimersByTime(120_000); // a closed session must not dial again
      for (let d = 0; d < 16; d++) await Promise.resolve();
      expect(dials).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });
});

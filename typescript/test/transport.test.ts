/** Transport-layer tests: fallback order, explicit override, codecs, and
 * event-flow parity over a fake (non-WS) wire. All offline. */
import { describe, expect, test } from "bun:test";
import { Session, Question, type Wave } from "../src/index.ts";
import {
  NdjsonSplitter,
  QuicWire,
  RtcWire,
  TransportDialError,
  MAX_INBOUND_MESSAGE_BYTES,
  connectWire,
  dialWs,
  hashToBytes,
  transportOrder,
} from "../src/transport.ts";
import type { Transport, Wire, WireOptions } from "../src/transport.ts";

function dec(qid: string, tMs: number, choice: string, probs: Record<string, number>, conf: number, audioTMs?: number): string {
  return JSON.stringify({
    event: "sense.decision",
    id: qid,
    t_ms: tMs,
    ...(audioTMs !== undefined ? { audio_t_ms: audioTMs } : {}),
    choice,
    probabilities: probs,
    confidence: conf,
  });
}

class FakeWire implements Wire {
  readonly kind: Transport;
  sent: unknown[] = [];
  private script: string[];
  private messageCb: ((raw: string) => void) | null = null;
  private closeCb: ((code?: number) => void) | null = null;

  constructor(kind: Transport, script: string[] = [], private closeAfter = false) {
    this.kind = kind;
    this.script = [...script];
  }

  sendJson(obj: unknown): void {
    this.sent.push(obj);
  }
  onMessage(cb: (raw: string) => void): void {
    this.messageCb = cb;
  }
  onClose(cb: (code?: number) => void): void {
    this.closeCb = cb;
  }
  /** Deliver one scripted message; resolves false when the wire "closes". */
  async pump(): Promise<boolean> {
    if (this.script.length === 0) {
      if (this.closeAfter) {
        this.closeCb?.();
        return false;
      }
      await new Promise(() => {}); // stay open, silent
    }
    const raw = this.script.shift();
    if (raw !== undefined) this.messageCb?.(raw);
    return true;
  }
  async close(): Promise<void> {
    this.closeCb?.();
  }
}

function scriptedWaveEvents(): string[] {
  return [
    JSON.stringify({ type: "session.created", session: {} }),
    JSON.stringify({
      type: "response.output_text.delta",
      delta:
        dec("finished", 0, "true", { true: 1.0, false: 0.0 }, 1.0) +
        "\n" +
        dec("intent", 0, "support", { support: 1.0, other: 0.0 }, 1.0) +
        "\n",
    }),
    JSON.stringify({ type: "response.completed" }),
  ];
}

const baseOpts: WireOptions = {
  transport: "auto",
  apiBaseUrl: "https://inference.urun.sh",
  apiKey: "sk-test",
  model: "sense-latest:bf16",
  wsUrl: "wss://inference.urun.sh/v1/realtime?model=sense-latest%3Abf16",
  dcid: "test-dcid",
};

// ---------------------------------------------------------------------------
// selection / fallback
// ---------------------------------------------------------------------------

type Ctors = { WebTransport?: unknown; RTCPeerConnection?: unknown; WebSocket?: unknown };

/** Set global constructors for the duration of `fn`, restoring exactly what
 * was there before (used / undefined) afterwards. */
async function withCtors<T>(ctors: Ctors, fn: () => Promise<T> | T): Promise<T> {
  const g = globalThis as unknown as Record<keyof Ctors, unknown>;
  const saved: Record<string, { used: boolean; value: unknown }> = {};
  for (const name of Object.keys(ctors) as Array<keyof Ctors>) {
    saved[name] = name in g ? { used: true, value: g[name] } : { used: false, value: undefined };
    if (ctors[name] === undefined) delete g[name];
    else g[name] = ctors[name];
  }
  try {
    return await fn();
  } finally {
    for (const name of Object.keys(ctors) as Array<keyof Ctors>) {
      const entry = saved[name];
      if (entry !== undefined && entry.used) g[name] = entry.value;
      else delete g[name];
    }
  }
}

describe("transportOrder", () => {
  test("explicit override dials exactly one", () => {
    expect(transportOrder("quic")).toEqual(["quic"]);
    expect(transportOrder("webrtc")).toEqual(["webrtc"]);
    expect(transportOrder("ws")).toEqual(["ws"]);
  });

  test("auto dials node ponyfills when globals are missing, always ends with ws", async () => {
    await withCtors({ WebTransport: undefined, RTCPeerConnection: undefined }, () => {
      // Under Node (this test runner included) neither global exists, but the
      // @fails-components/webtransport ponyfill is a regular dependency: the
      // quic rung is still offered (a failed ponyfill load falls back at dial
      // time). WebRTC stays browser-only (werift interop evidence in
      // transport.ts). A browser without natives would see only ["ws"] here.
      expect(transportOrder("auto")).toEqual(["quic", "ws"]);
    });
    await withCtors({ WebTransport: function FakeWT() {}, RTCPeerConnection: undefined }, () => {
      expect(transportOrder("auto")).toEqual(["quic", "ws"]);
      expect(QuicWire.available()).toBe(true);
    });
    await withCtors({ WebTransport: function FakeWT() {}, RTCPeerConnection: function FakeRTC() {} }, () => {
      expect(transportOrder("auto")).toEqual(["quic", "webrtc", "ws"]);
      expect(RtcWire.available()).toBe(true);
    });
  });
});

describe("connectWire", () => {
  // deterministic auto order: pretend both client stacks exist in this runtime
  const bothCtors: Ctors = { WebTransport: function FakeWT() {}, RTCPeerConnection: function FakeRTC() {} };

  test("auto falls back quic → webrtc → ws", async () => {
    await withCtors(bothCtors, async () => {
      const dialed: string[] = [];
      const dialers = {
        quic: async () => {
          dialed.push("quic");
          throw new TransportDialError("quic down");
        },
        webrtc: async () => {
          dialed.push("webrtc");
          throw new TransportDialError("rtc down");
        },
        ws: async () => {
          dialed.push("ws");
          return new FakeWire("ws");
        },
      };
      const wire = await connectWire({ ...baseOpts, transport: "auto" }, dialers);
      expect(wire.kind).toBe("ws");
      expect(dialed).toEqual(["quic", "webrtc", "ws"]);
    });
  });

  test("auto picks quic when it dials", async () => {
    await withCtors(bothCtors, async () => {
      const dialed: string[] = [];
      const dialers = {
        quic: async () => {
          dialed.push("quic");
          return new FakeWire("quic");
        },
        webrtc: async () => {
          dialed.push("webrtc");
          throw new TransportDialError("rtc down");
        },
        ws: async () => {
          dialed.push("ws");
          throw new TransportDialError("ws down");
        },
      };
      const wire = await connectWire({ ...baseOpts, transport: "auto" }, dialers);
      expect(wire.kind).toBe("quic");
      expect(dialed).toEqual(["quic"]);
    });
  });

  test("explicit transport does not fall back", async () => {
    const dialed: string[] = [];
    const dialers = {
      quic: async () => {
        dialed.push("quic");
        throw new TransportDialError("quic down");
      },
      ws: async () => {
        dialed.push("ws");
        throw new TransportDialError("ws down");
      },
    };
    const err = await connectWire({ ...baseOpts, transport: "quic" }, dialers).then(
      () => null,
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(TransportDialError);
    expect(dialed).toEqual(["quic"]);
  });
});

// ---------------------------------------------------------------------------
// WebSocket dial shape (browser subprotocol auth vs Node header auth)
// ---------------------------------------------------------------------------

/** Run `fn` as a browser: no `process` global, `WebSocket` = `ctor`. */
async function asBrowser<T>(ctor: unknown, fn: () => Promise<T>): Promise<T> {
  const g = globalThis as typeof globalThis & Record<string, unknown>;
  const savedProcess = Object.getOwnPropertyDescriptor(g, "process");
  Object.defineProperty(g, "process", { value: undefined, configurable: true, writable: true });
  try {
    return await withCtors({ WebSocket: ctor }, fn);
  } finally {
    if (savedProcess) Object.defineProperty(g, "process", savedProcess);
  }
}

describe("dialWs", () => {
  test("browser dials with the client secret as a subprotocol and no options object", async () => {
    const made: Array<{ args: unknown[]; ws: FakeBrowserWs }> = [];
    class FakeBrowserWs {
      private listeners: Record<string, Array<(ev: unknown) => void>> = {};
      constructor(...args: unknown[]) {
        made.push({ args, ws: this });
      }
      addEventListener(type: string, cb: (ev: unknown) => void) {
        (this.listeners[type] ??= []).push(cb);
        if (type === "open") queueMicrotask(() => cb({})); // handshake succeeds once someone listens
      }
      emit(type: string, ev: unknown) {
        for (const cb of this.listeners[type] ?? []) cb(ev);
      }
      send() {}
      close() {
        this.emit("close", { code: 1000, reason: "" });
      }
    }
    const wire = await asBrowser(FakeBrowserWs, () => dialWs({ ...baseOpts, transport: "ws" }));
    expect(made.map((m) => m.args)).toEqual([[baseOpts.wsUrl, ["realtime", `openai-insecure-api-key.${baseOpts.apiKey}`]]]);
    // browser event surface: messages and close reach the wire
    const got: string[] = [];
    wire.onMessage((raw) => got.push(raw));
    made[0]?.ws.emit("message", { data: '{"type":"x"}' });
    expect(got).toEqual(['{"type":"x"}']);
    // no maxPayload in a browser: an oversized message closes the wire undelivered
    made[0]?.ws.emit("message", { data: "x".repeat(MAX_INBOUND_MESSAGE_BYTES + 1) });
    expect(got).toEqual(['{"type":"x"}']);
    await wire.close(); // already closed: settles instead of waiting for a second 'close'
  });

  test("browser refuses to dial without a client secret", async () => {
    let constructed = false;
    const ctor = function FakeBrowserWs() {
      constructed = true;
    };
    const err = await asBrowser(ctor, () => dialWs({ ...baseOpts, transport: "ws", apiKey: "  " })).then(
      () => null,
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(TransportDialError);
    expect(err instanceof Error && err.message).toBe("browser WebSocket needs a client secret (no header auth)");
    expect(constructed).toBe(false);
  });

  test("Node dials with the ws package: Authorization header, no subprotocols", async () => {
    const seen: Array<{ auth: string | null; proto: string | null }> = [];
    const server = Bun.serve({
      port: 0,
      fetch(req, srv) {
        seen.push({ auth: req.headers.get("authorization"), proto: req.headers.get("sec-websocket-protocol") });
        return srv.upgrade(req) ? undefined : new Response("no", { status: 400 });
      },
      websocket: { message() {} },
    });
    try {
      const wire = await dialWs({ ...baseOpts, transport: "ws", wsUrl: `ws://127.0.0.1:${server.port}/v1/realtime` });
      expect(seen).toEqual([{ auth: `Bearer ${baseOpts.apiKey}`, proto: null }]);
      await wire.close();
    } finally {
      server.stop(true);
    }
  });
});

// ---------------------------------------------------------------------------
// codecs / helpers
// ---------------------------------------------------------------------------

describe("hashToBytes", () => {
  test("base64 (gateway format) and lowercase hex", () => {
    const raw = new Uint8Array(32);
    for (let i = 0; i < 32; i++) raw[i] = i;
    const b64 = Buffer.from(raw).toString("base64");
    expect(hashToBytes(b64)).toEqual(raw);
    const hex = Buffer.from(raw).toString("hex");
    expect(hashToBytes(hex)).toEqual(raw);
  });

  test("rejects garbage", () => {
    expect(() => hashToBytes("short")).toThrow(/neither base64 nor hex/);
  });
});

describe("NdjsonSplitter", () => {
  test("splits lines across chunk boundaries", () => {
    const s = new NdjsonSplitter();
    expect(s.push('{"a":1}\n{"b"')).toEqual(['{"a":1}']);
    expect(s.push(':2}\n\n')).toEqual(['{"b":2}']);
    expect(s.push("partial")).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// event-flow parity over a fake (non-WS) wire
// ---------------------------------------------------------------------------

describe("session over fake wire", () => {
  const questions = [
    new Question({ name: "finished", kind: "noul", instructions: "finished?" }),
    new Question({ name: "intent", kind: "choice", instructions: "What does the person want?", criteria: { support: "Help with a problem", other: "Anything else" } }),
  ];

  test("quic wire yields the same waves as ws", async () => {
    const wire = new FakeWire("quic", scriptedWaveEvents(), true);
    const s = new Session({
      apiKey: "sk-test",
      questions,
      transport: "quic",
      wireFactory: async () => wire,
    });
    const got: unknown[] = [];
    const consumer = (async () => {
      for await (const wave of s.run()) {
        got.push(wave);
        break;
      }
      await s.close();
    })();
    // pump the scripted events like the wire's reader task would; each await
    // yields the microtask queue, so the consumer's wave lands deterministically
    for (;;) {
      if (got.length > 0) break;
      if (!(await wire.pump())) break;
    }
    await consumer;
    expect(got.length).toBe(1);
    const w = got[0] as { result(id: string): { value: unknown } };
    expect(w.result("finished").value).toBe(true);
    expect(w.result("intent").value).toBe("support");
  });

  test("wave tMs is the decision's media timestamp (audio_t_ms wins)", async () => {
    // Older seats stamp per-wave COMPUTE time in t_ms; audio_t_ms is the
    // media position (protocol §6) and must win.
    const scripted = [
      JSON.stringify({ type: "session.created", session: {} }),
      JSON.stringify({
        type: "response.output_text.delta",
        delta:
          dec("finished", 54, "true", { true: 1.0, false: 0.0 }, 1.0, 500) +
          "\n" +
          dec("intent", 54, "support", { support: 1.0, other: 0.0 }, 1.0, 500) +
          "\n",
      }),
      JSON.stringify({ type: "response.completed" }),
    ];
    const wire = new FakeWire("ws", scripted, true);
    const s = new Session({
      apiKey: "sk-test",
      questions,
      transport: "ws",
      wireFactory: async () => wire,
    });
    const got: Wave[] = [];
    const consumer = (async () => {
      for await (const wave of s.run()) {
        got.push(wave);
        break;
      }
      await s.close();
    })();
    for (;;) {
      if (got.length > 0) break;
      if (!(await wire.pump())) break;
    }
    await consumer;
    expect(got.length).toBe(1);
    expect(got[0]!.tMs).toBe(500);
  });
});

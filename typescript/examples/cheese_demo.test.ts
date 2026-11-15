/**
 * Tests for the cheese demo's wave → verdict mapping (mirrors
 * python/tests/test_cheese_demo.py's decision-threshold checks).
 * Pure logic only: no sockets, no mic.
 */
import { describe, expect, test } from "bun:test";
import { Result, Score, Wave, type Session } from "@urun-sh/sense";
import {
  MAX_SESSIONS_PER_RUN,
  authorizeUpgrade,
  closeRelay,
  handleAudio,
  pump,
  serverHosts,
  sessionBudget,
  tokenMatches,
  toVerdict,
  type Relay,
} from "./cheese_demo.ts";

function result(
  id: string,
  kind: "noul" | "choice",
  value: boolean | string,
  p: number,
  distribution: Record<string, number>,
): Result {
  return new Result({ id, kind, waveId: 7, value, p, confidence: 1, distribution, top: String(value) });
}

const FEELING_LEVELS = ["hate", "dislike", "meh", "like", "love"];

/** A score result whose name-keyed dist is mapped onto the feeling levels. */
function scoreResult(dist: Record<string, number>): Score {
  const probs: Record<string, number> = {};
  for (const [name, p] of Object.entries(dist)) {
    probs[String(FEELING_LEVELS.indexOf(name))] = p;
  }
  return new Score({ id: "feeling", waveId: 7, probs, levels: FEELING_LEVELS, confidence: 1 });
}

function cheeseWave(p: number, kindDist: Record<string, number>, feelingDist: Record<string, number>): Wave {
  return new Wave(7, 0, {
    speech: result("speech", "noul", true, 1, { true: 1, false: 0 }),
    cheese: result("cheese", "noul", true, p, { true: p, false: 1 - p }),
    cheese_type: result("cheese_type", "choice", "cheddar", 0.99, kindDist),
    feeling: scoreResult(feelingDist),
  });
}

describe("toVerdict", () => {
  test("talking threshold: not cheese below 0.6", () => {
    const verdict = toVerdict(cheeseWave(0.59, { cheddar: 0.9 }, { love: 0.9 }));
    if (verdict.talking) throw new Error("expected talking=false at p=0.59");
    expect(verdict.speaking).toBe(true);
    expect("kind" in verdict).toBe(false);
  });

  test("talking threshold: cheese at exactly 0.6", () => {
    const verdict = toVerdict(cheeseWave(0.6, { cheddar: 0.9 }, { love: 0.9, like: 0.1 }));
    if (!verdict.talking) throw new Error("expected talking=true at p=0.6");
    expect(verdict.p).toBe(0.6);
    expect(verdict.kind).toBe("cheddar");
    expect(verdict.feeling).toBe("love");
    expect(verdict.score).toBeCloseTo(3.9);
    expect(verdict.waveId).toBe(7);
  });

  test("kind falls back to 'unknown type' below 0.6", () => {
    const verdict = toVerdict(cheeseWave(0.9, { cheddar: 0.59, other: 0.41 }, { love: 0.9 }));
    if (!verdict.talking) throw new Error("expected talking=true at p=0.9");
    expect(verdict.kind).toBe("unknown type");
    expect(verdict.feeling).toBe("love");
  });

  test("feeling falls back to 'meh' below 0.6", () => {
    const verdict = toVerdict(cheeseWave(0.9, { cheddar: 0.9 }, { love: 0.59, meh: 0.41 }));
    if (!verdict.talking) throw new Error("expected talking=true at p=0.9");
    expect(verdict.kind).toBe("cheddar");
    expect(verdict.feeling).toBe("meh");
  });

  test("speaking follows the speech result, not the cheese detection", () => {
    const silent = new Wave(1, 0, {
      speech: result("speech", "noul", false, 0, { true: 0, false: 1 }),
      cheese: result("cheese", "noul", false, 0.2, { true: 0.2, false: 0.8 }),
      cheese_type: result("cheese_type", "choice", "cheddar", 0.5, { cheddar: 0.5 }),
      feeling: scoreResult({ meh: 0.5 }),
    });
    const verdict = toVerdict(silent);
    expect(verdict.speaking).toBe(false);
  });
});

// -- relay lifecycle (fake session; no sockets) --------------------------------

/** Minimal Session double: enough for the relay functions under test. */
function fakeSession(opts: { connect?: () => Promise<void>; run?: () => AsyncGenerator<Wave> } = {}) {
  const calls = { connect: 0, close: 0, run: 0, pushed: 0 as number };
  const session = {
    connect: opts.connect ?? (async () => {}),
    run: opts.run ?? (async function* () {}),
    audio: { push: () => { calls.pushed++; } },
    close: () => { calls.close++; },
  };
  return { calls, session: session as unknown as Session };
}

function testRelay(session: Session | null, send: (event: unknown) => void = () => {}): Relay {
  return {
    session,
    send,
    capTimer: null,
    done: false,
    closed: false,
    pending: [],
    streaming: false,
    wake: null,
    end: () => {},
  };
}

describe("relay lifecycle", () => {
  test("frames buffer during the dial and flush in order before run()", async () => {
    const { calls, session } = fakeSession();
    const relay = testRelay(session);
    const runPromise = pump(relay);
    handleAudio(relay, new Uint8Array(4));
    handleAudio(relay, new Uint8Array(8));
    expect(relay.pending.length).toBe(2); // still dialing: buffered, not pushed
    expect(calls.pushed).toBe(0);
    await runPromise;
    expect(calls.pushed).toBe(2); // flushed before the run loop consumed them
    expect(relay.pending.length).toBe(0);
    expect(relay.streaming).toBe(true);
  });

  test("pump stops when the client leaves during the dial", async () => {
    let runStarted = false;
    const { calls, session } = fakeSession({
      run: async function* () {
        runStarted = true;
        yield* [];
      },
    });
    const relay = testRelay(session);
    const runPromise = pump(relay);
    closeRelay(relay); // the browser socket closes mid-dial
    await runPromise;
    expect(relay.closed).toBe(true);
    expect(runStarted).toBe(false); // the upstream session was shut down, not left open
    expect(calls.close).toBeGreaterThan(0);
  });

  test("pump stops when the client leaves during the frame wait", async () => {
    let runStarted = false;
    const { calls, session } = fakeSession({
      run: async function* () {
        runStarted = true;
        yield* [];
      },
    });
    const relay = testRelay(session);
    const runPromise = pump(relay);
    await new Promise((r) => setTimeout(r, 5)); // let the pump reach the frame wait
    expect(relay.wake).not.toBeNull();
    closeRelay(relay);
    await runPromise;
    expect(runStarted).toBe(false);
    expect(calls.close).toBeGreaterThan(0);
  });

  test("handleAudio drops frames after close", () => {
    const { calls, session } = fakeSession();
    const relay = testRelay(session);
    closeRelay(relay);
    handleAudio(relay, new Uint8Array(4));
    expect(relay.pending.length).toBe(0);
    expect(calls.pushed).toBe(0);
  });

  test("pending buffer overflow ends the relay instead of growing unboundedly", () => {
    const sent: unknown[] = [];
    let ended = false;
    const { calls, session } = fakeSession();
    const relay = testRelay(session, (e) => sent.push(e));
    relay.end = () => { ended = true; };
    const big = new Uint8Array(1_100_000); // > MAX_PENDING_BYTES on its own
    handleAudio(relay, big);
    expect(ended).toBe(true);
    expect(relay.pending.length).toBe(0);
    expect(calls.pushed).toBe(0);
    expect(sent.some((e) => (e as { type: string }).type === "error")).toBe(true);
  });

  test("closeRelay is idempotent: only the first close releases the slot", () => {
    const { session } = fakeSession();
    const relay = testRelay(session);
    expect(closeRelay(relay)).toBe(true);
    expect(closeRelay(relay)).toBe(false);
  });
});

describe("/ws access control", () => {
  const TOKEN = "per-run-test-token"; // low-entropy fixture, not a credential
  const HOSTS = new Set(["127.0.0.1:8080", "localhost:8080", "192.168.1.20:8080"]);
  const upgrade = (opts: { t?: string; host?: string; origin?: string | null }) => {
    const host = opts.host ?? "127.0.0.1:8080";
    const headers: Record<string, string> = { host };
    const origin = opts.origin === undefined ? `https://${host}` : opts.origin;
    if (origin !== null) headers.origin = origin;
    const q = opts.t === undefined ? `?t=${TOKEN}` : opts.t === "" ? "" : `?t=${opts.t}`;
    return new Request(`https://${host}/ws${q}`, { headers });
  };

  test("the page served for this run, with the run token, is allowed", () => {
    expect(authorizeUpgrade(upgrade({}), TOKEN, HOSTS)).toBeNull();
    expect(authorizeUpgrade(upgrade({ host: "192.168.1.20:8080" }), TOKEN, HOSTS)).toBeNull();
  });

  test("a LAN peer without the token is refused", () => {
    expect(authorizeUpgrade(upgrade({ t: "" }), TOKEN, HOSTS)).toMatch(/token/);
    expect(authorizeUpgrade(upgrade({ t: "guess" }), TOKEN, HOSTS)).toMatch(/token/);
  });

  test("another website driving the operator's browser is refused", () => {
    expect(authorizeUpgrade(upgrade({ origin: "https://evil.example" }), TOKEN, HOSTS)).toMatch(/cross-origin/);
    expect(authorizeUpgrade(upgrade({ origin: null }), TOKEN, HOSTS)).toMatch(/cross-origin/);
  });

  test("a DNS-rebinding Host is refused even with a matching Origin", () => {
    expect(authorizeUpgrade(upgrade({ host: "evil.example:8080" }), TOKEN, HOSTS)).toMatch(/Host/);
  });

  test("token comparison rejects null, prefixes and different lengths", () => {
    expect(tokenMatches(TOKEN, TOKEN)).toBe(true);
    expect(tokenMatches(null, TOKEN)).toBe(false);
    expect(tokenMatches(TOKEN.slice(0, -1), TOKEN)).toBe(false);
    expect(tokenMatches(`${TOKEN}x`, TOKEN)).toBe(false);
  });
});

describe("demo exposure limits", () => {
  test("the session budget spans the whole run: reconnecting cannot refill it", () => {
    const b = sessionBudget(MAX_SESSIONS_PER_RUN);
    const granted = Array.from({ length: MAX_SESSIONS_PER_RUN + 5 }, () => b.take());
    expect(granted.filter(Boolean).length).toBe(MAX_SESSIONS_PER_RUN);
    expect(granted.slice(MAX_SESSIONS_PER_RUN).every((g) => !g)).toBe(true);
    expect(b.used).toBe(MAX_SESSIONS_PER_RUN);
  });

  test("without --lan only loopback hosts are allowed", () => {
    expect(serverHosts(8080, false)).toEqual(["localhost:8080", "127.0.0.1:8080"]);
  });

  test("with --lan the allowlist still names only this machine", () => {
    const hosts = serverHosts(8080, true);
    expect(hosts.slice(0, 2)).toEqual(["localhost:8080", "127.0.0.1:8080"]);
    for (const h of hosts) expect(h).toMatch(/^(localhost|\d+\.\d+\.\d+\.\d+):8080$/);
  });
});

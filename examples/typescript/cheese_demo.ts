#!/usr/bin/env bun
/**
 * 🧀 Sense SDK cheese classifier — browser + mic (Bun server).
 *
 *   git clone https://github.com/urun-sh/sense-sdk.git
 *   cd sense-sdk/typescript
 *   bun install && bun run build
 *   URUN_API_KEY="<urun-key>" bun examples/cheese_demo.ts          # this machine only
 *   URUN_API_KEY="<urun-key>" bun examples/cheese_demo.ts --lan    # other devices on your network
 *
 * Open the printed URL: it carries a per-run access token in its #fragment.
 * The relay spends the operator's API key, so /ws requires that token, a
 * same-origin browser, and a Host that names this machine.
*/
import { spawnSync } from "node:child_process";
import { randomBytes, timingSafeEqual } from "node:crypto";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { homedir, networkInterfaces } from "node:os";
import { join } from "node:path";
import { Sense, type Session, type Wave } from "@urun-sh/sense";
import { apiKeysUrl } from "./api_key_hint";

// -- Sense: define the questions, relay audio, read decisions -----------------

const app = new Sense(); // apiKey / baseUrl default from URUN_API_KEY / SENSE_BASE_URL

app.noul("speech", {
  instructions: "Is someone speaking right now?",
  criteria: { true: "a person is speaking", false: "nobody is speaking" },
});
app.noul("cheese", {
  instructions: "Is the person talking about cheese right now?",
  criteria: { true: "the conversation is about cheese", false: "the conversation is not about cheese" },
});
app.choice("cheese_type", {
  instructions: "Which type of cheese are they talking about?",
  criteria: {
    cheddar: "Firm yellow cheese",
    brie: "Soft white-rind cheese",
    gouda: "Semi-hard Dutch cheese",
    parmesan: "Hard aged grating cheese",
    camembert: "Soft bloomy-rind cheese",
    mozzarella: "Stretchy fresh cheese",
    blue: "Veined blue cheese",
    feta: "Crumbly brined cheese",
    other: "Any other kind of cheese",
  },
});
app.score("feeling", {
  instructions: "How does the person feel about that cheese?",
  criteria: ["disgusted", "negative", "neutral", "positive", "delighted"],
});

/** A wave is a batch of decisions, not an audio response. */
export type Verdict =
  | { waveId: number; speaking: boolean; talking: false }
  | { waveId: number; speaking: boolean; talking: true; kind: string; feeling: string; score: number; p: number };

export function toVerdict(wave: Wave): Verdict {
  const speaking = Boolean(wave.result("speech").value);
  const detection = wave.result("cheese");
  if (detection.p < 0.6) return { waveId: wave.waveId, speaking, talking: false };
  const kind = wave.result("cheese_type");
  const feeling = wave.result("feeling");
  return {
    waveId: wave.waveId,
    speaking,
    talking: true,
    kind: kind.pOption(String(kind.value)) >= 0.6 ? String(kind.value) : "unknown type",
    feeling: feeling.p >= 0.6 ? feeling.top : "neutral",
    score: Number(feeling.value),
    p: detection.p,
  };
}

// -- relay: browser WebSocket ⇄ SDK session -----------------------------------

const CAP_MS = 300_000; // 5-minute session cap
const MAX_PENDING_BYTES = 1_000_000; // ~20 s of mic audio; the dial takes seconds normally
const MAX_RELAYS = 4; // the server spends its own API budget on LAN clients
/** Online sessions per server run. CAP_MS bounds one session; this bounds the
 * run, so reconnecting cannot reset the spend (12 × 5 min = 1 hour). */
export const MAX_SESSIONS_PER_RUN = 12;
const MAX_WS_MESSAGE_BYTES = 64 * 1024; // one 40 ms PCM16 frame is 1920 bytes
let activeRelays = 0;

/** Counts online sessions for the whole server run; never refills. */
export function sessionBudget(max: number): { take(): boolean; readonly used: number } {
  let used = 0;
  return {
    take: () => (used < max ? (used++, true) : false),
    get used() {
      return used;
    },
  };
}
const budget = sessionBudget(MAX_SESSIONS_PER_RUN);

// -- access control for /ws ----------------------------------------------------

/** Constant-time token comparison. */
export function tokenMatches(given: string | null, expected: string): boolean {
  if (given === null) return false;
  const a = Buffer.from(given);
  const b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}

/** Decide whether a /ws upgrade may spend the operator's key. Returns null
 * when allowed, else the refusal reason.
 *  - token: a per-run secret only the printed URL carries (LAN peers and
 *    other websites cannot know it);
 *  - Host: must name this machine, which defeats DNS rebinding;
 *  - Origin: must be this same page, so another site cannot drive a socket
 *    from the operator's browser. */
export function authorizeUpgrade(req: Request, token: string, allowedHosts: ReadonlySet<string>): string | null {
  const url = new URL(req.url);
  if (!tokenMatches(url.searchParams.get("t"), token)) return "missing or wrong access token";
  const host = req.headers.get("host")?.toLowerCase() ?? "";
  if (!allowedHosts.has(host)) return "unexpected Host";
  if (req.headers.get("origin")?.toLowerCase() !== `https://${host}`) return "cross-origin request";
  return null;
}

/** Relay state for one browser socket. Exported for tests. */
export type Relay = {
  session: Session | null;
  send: (event: unknown) => void;
  capTimer: ReturnType<typeof setTimeout> | null;
  done: boolean;
  closed: boolean;
  pending: (Uint8Array | ArrayBuffer)[]; // mic frames buffered until the dial completes
  streaming: boolean; // true once the run loop has started
  wake: (() => void) | null;
  end: () => void; // assigned by the ws handler; tears down the browser socket too
};

/** First close wins; returns false when the relay was already closed. */
export function closeRelay(relay: Relay): boolean {
  if (relay.closed) return false;
  relay.closed = true;
  if (relay.capTimer !== null) {
    clearTimeout(relay.capTimer);
    relay.capTimer = null;
  }
  relay.wake?.();
  relay.session?.close();
  return true;
}

export function handleAudio(relay: Relay, frame: Uint8Array | ArrayBuffer): void {
  if (relay.closed || !relay.session) return;
  let total = frame.byteLength;
  for (const f of relay.pending) total += f.byteLength;
  if (total > MAX_PENDING_BYTES) {
    // A stuck upstream would grow this buffer unboundedly; give up instead.
    relay.pending.length = 0;
    relay.send({ type: "error", message: "upstream connection too slow — audio buffer overflow" });
    relay.end();
    return;
  }
  relay.pending.push(frame); // PCM16, mono, 24 kHz; one 40 ms block
  relay.wake?.();
}

/** The relay's browser frames as ONE attach source: buffered frames first,
 * then each new frame as it lands; ends when the relay closes. */
async function* relayAudio(relay: Relay): AsyncGenerator<Uint8Array | ArrayBuffer> {
  try {
    for (;;) {
      for (const frame of relay.pending.splice(0)) yield frame;
      if (relay.closed) return;
      await new Promise<void>((resolve) => (relay.wake = resolve));
    }
  } finally {
    relay.wake?.(); // release a parked frame-wait so nothing hangs on a dead relay
  }
}

export async function pump(relay: Relay): Promise<void> {
  const session = relay.session;
  if (!session) return;
  try {
    // run() dials on first read; audio pushed before the dial is buffered
    // client-side and replayed onto the fresh wire before any turn opens.
    if (relay.closed) {
      // The client left before the loop started; shut the session down instead of lingering.
      await session.close();
      return;
    }
    // The turn opens on the first pushed audio frame, so wait for mic audio
    // before starting the loop.
    if (relay.pending.length === 0) await new Promise<void>((resolve) => (relay.wake = resolve));
    if (relay.closed) {
      await session.close();
      return;
    }
    relay.streaming = true;
    const attached = await session.attach({ audio: relayAudio(relay) });
    try {
      for await (const wave of session.run()) {
        relay.send({ type: "wave", verdict: toVerdict(wave) });
      }
    } finally {
      attached.detach();
    }
    if (relay.done) return; // cap path already reported stats and ended
    relay.send({ type: "stats", stats: session.stats });
    relay.send({ type: "ended", reason: "closed" });
  } catch (err) {
    relay.send({ type: "error", message: err instanceof Error ? err.message : String(err) });
  }
}

function openRelay(send: (event: unknown) => void): Relay {
  const relay: Relay = {
    session: app.apiKey ? app.session() : null,
    send,
    capTimer: null,
    done: false,
    closed: false,
    pending: [],
    streaming: false,
    wake: null,
    end: () => {},
  };
  relay.send({ type: "hello", mode: relay.session ? "online" : "mic-only" });
  if (relay.session) {
    relay.capTimer = setTimeout(() => {
      relay.done = true;
      relay.send({ type: "stats", stats: relay.session!.stats });
      relay.send({ type: "ended", reason: "cap" });
      relay.end();
    }, CAP_MS);
    void pump(relay);
  }
  return relay;
}

// -- server: the demo page and the browser relay -------------------------------

type WsData = { relay: Relay | null };

function generateCert(): { cert: Buffer; key: Buffer } {
  const dir = join(homedir(), ".cache", "sense-cheese-demo");
  const certPath = join(dir, "cert.pem");
  const keyPath = join(dir, "key.pem");
  if (!existsSync(certPath) || !existsSync(keyPath)) {
    mkdirSync(dir, { recursive: true });
    const generated = spawnSync(
      "openssl",
      [
        "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
        "-nodes", "-keyout", keyPath, "-out", certPath, "-days", "365", "-subj", "/CN=cheese-demo",
      ],
      { stdio: "pipe" },
    );
    if (generated.error) throw new Error("openssl is required to generate the HTTPS certificate");
    if (generated.status !== 0) {
      throw new Error(`openssl certificate generation failed: ${(generated.stderr?.toString() ?? "").trim()}`);
    }
  }
  return { cert: readFileSync(certPath), key: readFileSync(keyPath) };
}

/** host:port values that name this server: loopback always, plus the LAN
 * addresses when listening on the network. */
export function serverHosts(port: number, lan: boolean): string[] {
  const names = ["localhost", "127.0.0.1"];
  if (lan) {
    for (const addrs of Object.values(networkInterfaces())) {
      for (const addr of addrs ?? []) {
        if (addr.family === "IPv4" && !addr.internal) names.push(addr.address);
      }
    }
  }
  // Browsers omit the default https port from Host and Origin.
  return names.map((n) => (port === 443 ? n : `${n}:${port}`));
}

const PAGE_HEADERS = {
  "content-type": "text/html; charset=utf-8",
  "cache-control": "no-store",
  "referrer-policy": "no-referrer",
  "x-content-type-options": "nosniff",
  // The page holds microphone permission: never let another site frame it.
  "x-frame-options": "DENY",
  "content-security-policy": "frame-ancestors 'none'",
};

/** Start the relay server. Exported for the npm bin shim: an import is never
 * import.meta.main, so the shim calls this directly. */
export function main(argv: string[] = process.argv.slice(2)): void {
  const port = Number(process.env.PORT) || 8080;
  const lan = argv.includes("--lan") || process.env.SENSE_DEMO_LAN === "1";
  const token = randomBytes(24).toString("base64url");
  const hosts = serverHosts(port, lan);
  const allowedHosts = new Set(hosts);
  const { cert, key } = generateCert();
  const page = readFileSync(join(import.meta.dir, "cheese_demo.html"));
  console.log(`🧀 cheese demo (mode: ${app.apiKey ? "online" : `mic-only — set URUN_API_KEY to classify (make one at ${apiKeysUrl})`})`);
  // The token rides the #fragment: browsers never send it in requests or Referer headers.
  for (const host of hosts.slice(1)) console.log(`  https://${host}/#${token}`);
  console.log("  accept the self-signed certificate warning; HTTPS is required for mic access");
  if (lan) {
    console.log("  Listening on your network. Anyone with the printed URL can use your URUN API key");
    console.log(`  (up to ${MAX_SESSIONS_PER_RUN} five-minute sessions per run). Use a trusted network; Ctrl+C when finished.`);
  } else {
    console.log("  Listening on this machine only; pass --lan to open it to phones on your network.");
  }
  Bun.serve<WsData>({
    port,
    hostname: lan ? "0.0.0.0" : "127.0.0.1",
    tls: { cert, key },
    fetch: (req, server) => {
      const { pathname } = new URL(req.url);
      if (pathname === "/ws") {
        // Browsers cannot set the bearer header Sense needs, so the server
        // relays the browser's audio and events — only for authorized pages.
        const refused = authorizeUpgrade(req, token, allowedHosts);
        if (refused !== null) return new Response(`forbidden: ${refused}`, { status: 403 });
        if (server.upgrade(req, { data: { relay: null } })) return undefined;
        return new Response("upgrade failed", { status: 500 });
      }
      if (pathname === "/") return new Response(page, { headers: PAGE_HEADERS });
      return new Response("not found", { status: 404 });
    },
    websocket: {
      maxPayloadLength: MAX_WS_MESSAGE_BYTES,
      open(ws) {
        if (activeRelays >= MAX_RELAYS) {
          try {
            ws.send(JSON.stringify({ type: "error", message: `relay busy: ${MAX_RELAYS} concurrent sessions max` }));
          } catch {
            /* the client is gone */
          }
          ws.close();
          return;
        }
        if (app.apiKey && !budget.take()) {
          // Refuse outright: a mic-only fallback after an error would hide it.
          try {
            ws.send(JSON.stringify({ type: "error", message: `session budget for this run used (${MAX_SESSIONS_PER_RUN}); restart the demo to continue` }));
          } catch {
            /* the client is gone */
          }
          ws.close();
          return;
        }
        let relay: Relay;
        try {
          relay = openRelay((event) => {
            try {
              ws.send(JSON.stringify(event));
            } catch {
              /* the client is gone; the pump's remaining sends are best-effort */
            }
          });
        } catch (err) {
          // Never leave a half-open socket holding a relay slot.
          console.error(`relay setup failed: ${err instanceof Error ? err.message : String(err)}`);
          ws.close();
          return;
        }
        activeRelays++;
        relay.end = () => {
          if (closeRelay(relay)) activeRelays--;
          ws.close();
        };
        ws.data.relay = relay;
      },
      message(ws, message) {
        if (ws.data.relay && typeof message !== "string") handleAudio(ws.data.relay, message);
      },
      close(ws) {
        if (ws.data.relay && closeRelay(ws.data.relay)) activeRelays--;
      },
    },
  });
}

if (import.meta.main) main();

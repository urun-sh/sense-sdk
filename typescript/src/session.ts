/** Session: inputs, the §1–§7 wire loop over `wire`, and wave assembly. */
import { toB64 } from "./bytes.ts";
import { attachStream, type AttachHandle, type AttachOptions, type AttachStream } from "./attach.ts";
import { checkBaseUrl, connectWire, DialRefused } from "./transport.ts";
import type { Transport, Wire, WireDialer } from "./transport.ts";
import { Question, instructionsEnvelope } from "./question.ts";
import { Wave } from "./wave.ts";
import { Context } from "./context.ts";
import { OutOfCreditsError, ProtocolError, errMessage } from "./errors.ts";

const MAX_FRAME_BYTES = 256_000;
const TURN_TOKEN_BUDGET = 250_000;
const MAX_BACKOFF_MS = 30_000;
const MAX_DIALS = 600;
/** Waves held for run()/waves() consumers; the oldest is dropped beyond this
 * so an app that only uses onWave/when cannot grow memory without bound. */
const MAX_QUEUED_WAVES = 512;
/** Per named audio source, chunks held client-side (~60 s of 40 ms chunks);
 * the oldest is dropped beyond this (Python parity). */
const MAX_PENDING_AUDIO_CHUNKS = 1500;

/** Explicit presence (owner GO 2026-11-16, hub-primary): the proxy holds a
 * pooled seat only while the client says it is present. The signal is the
 * `{"type":"session.presence"}` frame on the realtime event channel, sent
 * every PRESENCE_CADENCE_MS while `Date.now() - lastActivityAt` is inside
 * the 60 s idle window; an idle lapse stops the frames (an explicit detach —
 * the runtime's #3439 governor then PARKS the seat, it never dies), and the
 * next app-level send re-asserts it. A visible browser tab is NOT activity
 * (owner ruling): only real app sends and keepalive() count. */
const PRESENCE_IDLE_MS = 60_000;
const PRESENCE_CADENCE_MS = 10_000;

/** Keepalive event types the SDK itself sends on the realtime lane. An
 * adapter that rejects one with `unknown_event` gets it muted for the rest
 * of that connection (liveness still comes from audio frames). */
const KEEPALIVE_EVENTS: Record<string, true> = {
  "session.presence": true,
  ping: true,
};

/** Read an environment variable without referencing `process.env.NAME`
 * literally: bundlers that define-substitute that expression would inline the
 * build machine's secret into client bundles. Returns undefined off Node/Bun. */
export function envVar(name: string): string | undefined {
  const g = globalThis as typeof globalThis & { process?: { env?: Record<string, string | undefined> } };
  const v = g.process?.env?.[name];
  return v === "" ? undefined : v;
}

/** Reconnect pacing for wires that die young (relay/GS churn): base redial
 * delay, the young-wire threshold, and the consecutive-young-death budget —
 * sustained churn with no surviving connection is how a hub-ended session
 * presents on this wire (the realtime relay collapses every upstream close),
 * so past the budget the client stops loudly instead of spinning forever. */
const REDIAL_BASE_MS = 1_000;
const YOUNG_WIRE_MS = 10_000;
const MAX_CONSECUTIVE_REDIALS = 10;

export interface SessionEvent {
  type: string;
  code?: string;
  message?: string;
  raw: unknown;
}

/** Parse a server event object into the SDK's session-event shape. */
export function sessionEvent(raw: unknown): SessionEvent {
  const obj = (typeof raw === "object" && raw !== null ? raw : {}) as Record<string, unknown>;
  const err = (obj.error ?? null) as Record<string, unknown> | null;
  return {
    type: typeof obj.type === "string" ? obj.type : "unknown",
    code: typeof err?.code === "string" ? err.code : typeof obj.code === "string" ? obj.code : undefined,
    message: typeof err?.message === "string" ? err.message : undefined,
    raw,
  };
}


/** Static key, or a provider called on every dial/redial so short-lived
 * credentials can be refreshed before each connection attempt. */
export type ApiKeyProvider = string | (() => string | Promise<string>);

export interface SessionOptions {
  apiKey?: ApiKeyProvider;
  baseUrl?: string;
  model?: string;
  hz?: number;
  questions?: Question[];
  /** Wire transport: "auto" = quic → webrtc → ws (default). */
  transport?: Transport;
  /** Permit a plaintext http:// or ws:// baseUrl on a non-loopback host. The
   * API key is then sent unencrypted; only use this on a trusted network. */
  allowInsecure?: boolean;
  /** @internal test seam: override the wire dialer. */
  wireFactory?: WireDialer;
}

/** hz must be a finite number in (0, 12.5]: 12.5 Hz is one seat's sustained
 * floor on the serving GPU — a faster request coalesces to the newest media
 * and just realizes fewer waves, so it is refused up front. */
function checkHz(hz: number, given: number | undefined): void {
  if (!Number.isFinite(hz) || hz <= 0 || hz > Session.DEFAULT_HZ) {
    throw new RangeError(
      `hz must be a finite number in (0, ${Session.DEFAULT_HZ}] — 12.5 Hz is one seat's sustained floor on the serving GPU (got ${String(given)})`,
    );
  }
}

function wireDefaults(opts: SessionOptions): { transport: Transport; wireFactory: WireDialer } {
  return { transport: opts.transport ?? "auto", wireFactory: opts.wireFactory ?? connectWire };
}

export class Session {
  static readonly DEFAULT_BASE_URL = "wss://inference.urun.sh";
  static readonly DEFAULT_MODEL = "sense-latest:bf16";
  /** One seat's sustained floor on the serving GPU is ~12.5 Hz (<80 ms/tick
   * is the owner's ruling); a faster request coalesces to the newest media
   * and just realizes fewer waves. 12.5 is the default the lane can keep. */
  static readonly DEFAULT_HZ = 12.5;

  private readonly apiKeyProvider: ApiKeyProvider;
  readonly baseUrl: string;
  readonly model: string;
  readonly hz: number;
  readonly transport: Transport;
  readonly context = new Context(this);
  readonly stats = { waves: 0, turns: 0, errors: 0, reconnects: 0, decisions: 0, protocolErrors: 0 };

  private isClosed = false;
  private reconnecting = false; // a redial is scheduled or in flight
  private fatalEnded = false; // a terminal end already surfaced — exactly one
  private orphanedAudio: string[] = []; // appends sent while the wire was down
  private connectedAt = 0; // when the current wire was dialed (young-wire test)
  private redialDelay = 0; // next young-death redial base, ms
  private youngDeaths = 0; // consecutive young-wire deaths (redial budget)
  private wire: Wire | null = null;
  private readonly wireFactory: WireDialer;
  private waveSeq = 0;
  private sourceQueues = new Map<string, string[]>();
  /** Default-source audio pushed while no wire is live (e.g. during a
   * session_gone redial); replayed onto the next wire before any turn opens. */
  private rrCursor = 0;
  private oneShotText: string[] = [];
  private transcript = "";
  private waveQueue: Wave[] = [];
  private waveWaiters: ((w: Wave | null) => void)[] = [];
  private accumulator = new Map<string, Record<string, unknown>>();
  private accTMs = 0;
  private turnOpen = false;
  private audioPending = false;
  private inputSinceTurn = 0;
  private registry: Question[] = [];
  private eventListeners: ((ev: SessionEvent) => void)[] = [];
  /** Until when the run loop must not re-open a turn (response_in_progress backoff). */
  private reopenNotBefore = 0;
  /** Consecutive response_in_progress refusals with no wave in between —
   * ≥3 means the lane is wedged (a cancelled turn the server never settles),
   * not the ordinary cancel race. */
  private wedgeRefusals = 0;
  /** The current wire death is the wedge self-heal, not hub churn: keep the
   * pushed state and dial fresh immediately. */
  private selfHealing = false;
  /** The wire died and was not replaced: wave streams end once drained. */
  private streamsEnded = false;
  /** The organization ran out of credits (wire 'insufficient_credits' or a
   * close reason 'credits_exhausted'): the terminal error surfaced through
   * run()/waves() and endFatal — never a redial. */
  private creditsError: OutOfCreditsError | null = null;
  /** Set only for OutOfCreditsError terminal ends: run()/waves() throw it
   * instead of ending quietly (other fatal ends keep ending quietly). */
  private fatalErr: OutOfCreditsError | null = null;
  /** Last app-level send (audio/video/text push, barge-in, setQuestions,
   * keepalive). A freshly created session is present until this lapses. */
  private lastActivityAt = Date.now();
  private presenceTimer: ReturnType<typeof setInterval> | null = null;
  /** The session drive (dial + turn-open loop): started once, by whichever of
   * attach()/run() runs first. */
  private driveStarted = false;
  private driveWaiters: (() => void)[] = [];
  /** A dial already in flight; a second connect() joins it instead of double-dialing. */
  private connecting: Promise<void> | null = null;
  /** Keepalive event types the current adapter has rejected with
   * `unknown_event` (per connection; reset on every dial). */
  private mutedKeepalive = new Set<string>();

  constructor(opts: SessionOptions = {}) {
    this.apiKeyProvider = opts.apiKey ?? envVar("URUN_API_KEY") ?? "";
    this.baseUrl = checkBaseUrl(opts.baseUrl ?? envVar("SENSE_BASE_URL") ?? Session.DEFAULT_BASE_URL, opts.allowInsecure);
    this.model = opts.model ?? Session.DEFAULT_MODEL;
    this.hz = opts.hz ?? Session.DEFAULT_HZ;
    checkHz(this.hz, opts.hz);
    ({ transport: this.transport, wireFactory: this.wireFactory } = wireDefaults(opts));
    for (const q of opts.questions ?? []) this.addQuestionRaw(q);
  }

  get questions(): Question[] {
    return this.registry.slice();
  }

  get closed(): boolean {
    return this.isClosed;
  }

  /** Replace the whole question registry (adds, edits AND removals) and push
   * the updated instructions envelope as `session.update` (§2: the proxy
   * latches it and folds it into the per-turn envelope). A mid-turn schema
   * change takes effect at the NEXT turn open — and a live cadence turn
   * never ends on its own ("a stream, not a completion") — so this also
   * barges in FIRST: an envelope sent while a response is streaming wedges
   * the lane; cancel, then latch the new schema for the reopened turn. */
  setQuestions(questions: Question[]): void {
    this.touch();
    this.registry = [];
    for (const q of questions) this.addQuestionRaw(q);
    this.bargeIn();
    this.sendSessionUpdate();
  }

  private addQuestionRaw(q: Question): Question {
    const i = this.registry.findIndex((x) => x.name === q.name);
    if (i >= 0) this.registry[i] = q;
    else this.registry.push(q);
    return q;
  }

  realtimeUrl(): string {
    return `${this.baseUrl.replace(/\/+$/, "")}/v1/realtime?model=${encodeURIComponent(this.model)}`;
  }

  // ---- explicit presence --------------------------------------------------

  /** Stamp app-level activity: every public send and keepalive() routes here. */
  private touch(): void {
    this.lastActivityAt = Date.now();
  }

  /** App-level presence assertion: refreshes the activity window without a
   * media payload. Call this when the app is alive but sending nothing. */
  keepalive(): void {
    this.touch();
  }

  /** Start the 10 s presence cadence (once per session, across redials).
   * A tick inside the idle window sends `{"type":"session.presence"}` on the
   * event channel (every transport carries it — the same sendJson path as
   * all app events); a lapsed window sends nothing. */
  private startPresence(): void {
    if (this.presenceTimer !== null || this.isClosed) return;
    this.presenceTimer = setInterval(() => this.presenceTick(), PRESENCE_CADENCE_MS);
  }

  /** One presence cadence tick: send `{"type":"session.presence"}` while the
   * app is inside the idle window and the adapter has not muted the type. */
  private presenceTick(): void {
    if (this.mutedKeepalive.has("session.presence")) return;
    if (Date.now() - this.lastActivityAt < PRESENCE_IDLE_MS) this.sendJson({ type: "session.presence" });
  }

  private stopPresence(): void {
    if (this.presenceTimer === null) return;
    clearInterval(this.presenceTimer);
    this.presenceTimer = null;
  }

  // ---- inputs -------------------------------------------------------------

  /** @internal */
  emitAudio(b64: string, source?: string): void {
    this.touch();
    if (source !== undefined && source !== "default") {
      let queue = this.sourceQueues.get(source);
      if (!queue) {
        queue = [];
        this.sourceQueues.set(source, queue);
      }
      queue.push(b64);
      if (queue.length > MAX_PENDING_AUDIO_CHUNKS) queue.shift();
      return;
    }
    this.inputSinceTurn++;
    this.audioPending = true;
    if (this.wire === null) {
      // Reconnect window: sendJson would no-op, so the server would never see
      // this append while audioPending latched — the first reopened turn would
      // commit an empty server buffer (refused empty_buffer, turn opens with
      // no audio bound, the utterance is silently dropped). Carry the append
      // and replay it on the fresh wire before any commit.
      this.orphanedAudio.push(b64);
      if (this.orphanedAudio.length > MAX_PENDING_AUDIO_CHUNKS) this.orphanedAudio.shift();
      return;
    }
    this.sendJson({ type: "input_audio_buffer.append", audio: b64 });
    this.drainSourceQueues();
  }

  /** Replay appends that were made while the wire was down (immediately after
   * session.update, before any turn can open, so the first commit binds them). */
  private flushOrphanedAudio(): void {
    if (this.orphanedAudio.length === 0) return;
    for (const b64 of this.orphanedAudio) {
      this.sendJson({ type: "input_audio_buffer.append", audio: b64 });
    }
    this.orphanedAudio = [];
  }

  /** Send one queued non-default chunk per default push, round-robin.
   * Sources share the audio stream; they are not isolated server-side. */
  private drainSourceQueues(): void {
    const sources = [...this.sourceQueues.keys()];
    const source = sources[this.rrCursor++ % Math.max(sources.length, 1)];
    if (source === undefined) return;
    const b64 = this.sourceQueues.get(source)?.shift();
    if (b64 !== undefined) this.sendJson({ type: "input_audio_buffer.append", audio: b64, source });
  }

  /** @internal One JPEG frame (Uint8Array or ArrayBuffer). */
  emitVideo(jpeg: Uint8Array | ArrayBuffer): void {
    this.touch();
    if (jpeg.byteLength > MAX_FRAME_BYTES) {
      throw new ProtocolError("frame_too_large", `frame ${jpeg.byteLength}B > ${MAX_FRAME_BYTES}B`);
    }
    this.inputSinceTurn++;
    this.sendJson({
      type: "conversation.item.create",
      item: {
        type: "message",
        role: "user",
        content: [{ type: "input_image", image_url: `data:image/jpeg;base64,${toB64(jpeg)}` }],
      },
    });
  }

  /** @internal Text context for the model: append/replace the transcript, or
   * a one-shot event text. */
  emitText(text: string, mode: TextMode): void {
    this.touch();
    this.inputSinceTurn++;
    if (mode === "event") {
      this.oneShotText.push(text);
      return;
    }
    if (mode === "replace") this.transcript = text;
    else this.transcript += (this.transcript ? "\n" : "") + text;
  }

  // ---- wire ---------------------------------------------------------------

  private sendJson(obj: unknown): void {
    this.wire?.sendJson(obj);
  }

  private sendSessionUpdate(): void {
    this.sendJson({
      type: "session.update",
      session: {
        model: this.model,
        modalities: ["text"],
        instructions: JSON.stringify(instructionsEnvelope(this.registry, this.hz)),
      },
    });
  }

  private openTurn(): void {
    const text = [this.transcript, ...this.oneShotText].filter(Boolean).join("\n");
    this.oneShotText = [];
    if (text) {
      this.sendJson({
        type: "conversation.item.create",
        item: { type: "message", role: "user", content: [{ type: "input_text", text }] },
      });
    }
    // The commit must follow buffered audio — an empty commit is refused
    // (empty_buffer) and the turn opens with no audio bound, so later
    // appends never reach the model.
    // Appends sent before the commit are held server-side (GA semantics).
    if (this.audioPending) {
      this.sendJson({ type: "input_audio_buffer.commit" });
      this.audioPending = false;
    }
    this.sendJson({
      type: "response.create",
      response: {
        instructions: JSON.stringify(instructionsEnvelope(this.registry, this.hz)),
        max_output_tokens: TURN_TOKEN_BUDGET,
      },
    });
    this.accumulator.clear();
    this.turnOpen = true;
    this.inputSinceTurn = 0;
    this.stats.turns++;
  }

  /** Barge-in: stop the active turn's cadence. @internal — Context.cancel
   * and setQuestions barge in; apps call ctx.cancel(key). */
  bargeIn(): void {
    this.touch();
    this.sendJson({ type: "response.cancel" });
    this.turnOpen = false;
  }

  /** Connect with the §7 dial policy (503/429 → backoff and redial). @internal
   * — the drive dials at start; tests dial through here. Idempotent: a dial
   * already in flight is joined, never duplicated. The
   * transport selection (auto: quic → webrtc → ws) happens in the wire
   * factory; a refused dial restarts the whole order. */
  async connect(): Promise<void> {
    if (this.isClosed) throw new ProtocolError("protocol", "session is closed");
    if (this.connecting) return this.connecting;
    const dial = this.dial();
    this.connecting = dial;
    try {
      await dial;
    } finally {
      if (this.connecting === dial) this.connecting = null;
    }
  }

  private async dial(): Promise<void> {
    let delay = 1_000;
    for (let attempt = 1; ; attempt++) {
      try {
        // Resolve on every attempt: a short-lived credential must be fresh
        // for each dial/redial, including the session_gone reconnect.
        const apiKey =
          typeof this.apiKeyProvider === "function" ? await this.apiKeyProvider() : this.apiKeyProvider;
        if (typeof apiKey !== "string" || apiKey === "") {
          // Fail closed (Python parity): never dial with an empty bearer.
          throw new ProtocolError("protocol", "no API key: pass apiKey or set URUN_API_KEY");
        }
        const wire = await this.wireFactory({
          transport: this.transport,
          apiBaseUrl: this.baseUrl.replace(/^wss:/, "https:").replace(/^ws:/, "http:").replace(/\/+$/, ""),
          apiKey,
          model: this.model,
          wsUrl: this.realtimeUrl(),
        });
        await this.install(wire);
        return;
      } catch (err) {
        if (attempt >= MAX_DIALS || !(err instanceof DialRefused)) throw err;
        delay = await this.backoffStep(delay);
      }
    }
  }

  /** Wait out one dial-refusal backoff step and return the next delay.
   * close() during the window ends the dialing: a closed session must never
   * keep dialing with the bearer key (the pending sleep would also keep the
   * process alive). */
  private async backoffStep(delay: number): Promise<number> {
    if (this.isClosed) throw new ProtocolError("protocol", "session closed while connecting");
    await new Promise((r) => setTimeout(r, delay));
    if (this.isClosed) throw new ProtocolError("protocol", "session closed while connecting");
    return Math.min(delay * 2, MAX_BACKOFF_MS);
  }

  /** Adopt a freshly dialed wire: register the schema, then replay audio
   * buffered while disconnected so the next turn's commit binds real audio. */
  private async install(wire: Wire): Promise<void> {
    if (this.isClosed) {
      // close() ran while this dial was in flight: never install a live,
      // authenticated wire on a closed session.
      await wire.close();
      throw new ProtocolError("protocol", "session closed while connecting");
    }
    this.wire = wire;
    this.connectedAt = Date.now();
    this.streamsEnded = false;
    this.wedgeRefusals = 0;
    this.mutedKeepalive.clear(); // new connection: the adapter may speak them now
    // A fresh server session has no in-flight response: a backoff earned on
    // the old wire must not delay the first turn on the new one.
    this.reopenNotBefore = 0;
    // Wire-identity guards: RtcWire fires close from two sources (datachannel
    // and peer connection), so a late duplicate close from a replaced wire
    // must not be attributed to the live one.
    wire.onMessage((raw) => {
      if (this.wire === wire) this.onMessage(raw);
    });
    wire.onClose((code, reason) => {
      if (this.wire === wire) this.onClose(code ?? 0, reason);
    });
    this.sendSessionUpdate();
    this.flushOrphanedAudio();
    this.startPresence();
  }

  private onClose(code: number, reason?: string): void {
    this.wire = null;
    this.turnOpen = false;
    // Audio appended before the death rode the dead wire and is gone; keeping
    // the flag would make the first reopened turn commit an empty server
    // buffer (refused empty_buffer, turn opens with no audio bound). The
    // counter must reset too, or the first reopened turn opens with no input
    // behind it (a spurious empty model turn).
    this.audioPending = false;
    this.inputSinceTurn = 0;
    if (this.isClosed || this.reconnecting || this.fatalEnded) return; // terminal (user close / ended), or a redial is already scheduled
    if (this.endForCredits(reason)) return; // credits cut: fatal end, never redial
    this.redialAfterDeath(code, this.resetForRedial());
  }

  /** §7 redial after a wire death that is not terminal: session_gone (or our
   * own wedge self-heal) is an explicit signal and dials fresh immediately;
   * silent deaths (1000/1005/1006) get the young-death ladder instead — back
   * off before redialing (jittered so co-tenants do not retry in lockstep);
   * a wire that lived resets the ladder. */
  private redialAfterDeath(code: number, healing: boolean): void {
    // A wire that died young is churn, not a fresh start. A self-heal is a
    // deliberate drop, not churn: it never counts toward the ladder.
    const young = !healing && Date.now() - this.connectedAt < YOUNG_WIRE_MS;
    this.youngDeaths = young ? this.youngDeaths + 1 : 0;
    if (this.youngDeaths >= MAX_CONSECUTIVE_REDIALS) {
      this.endFatal(`wire died young ${this.youngDeaths}x in a row — session appears ended by the hub`);
      return;
    }
    this.reconnecting = true;
    if (code === 1011 || healing) {
      void this.connect().then(
        () => {
          this.reconnecting = false;
        },
        (err: unknown) => {
          this.reconnecting = false;
          if (this.isClosed) return;
          this.endFatal(err);
        },
      );
      return;
    }
    const delay = young ? Math.max(this.redialDelay, REDIAL_BASE_MS) : 0;
    this.redialDelay = Math.min(delay * 2, MAX_BACKOFF_MS);
    setTimeout(() => {
      void this.connect().then(
        () => {
          this.reconnecting = false;
        },
        (err: unknown) => {
          this.reconnecting = false;
          if (this.isClosed) return; // user closed during the backoff window
          this.endFatal(err);
        },
      );
    }, Math.round(delay * (0.5 + Math.random())));
  }

  /** Reset per-wire state for a redial; returns whether this death was the
   * wedge self-heal.
   *
   * Any wire death while the session is open reconnects fresh (§7). 1011 is
   * session_gone; everything else is hub churn the client must also survive:
   * the realtime relay maps an abnormal upstream close onto 1000/1011, and a
   * dead relay worker gives the client no close frame at all (1005/1006).
   * Questions (schema, not state) survive; only the conversation state
   * resets. Python parity. The wedge self-heal is the exception: it drops a
   * HEALTHY wire on purpose to escape a bricked server session, so the pushed
   * state rides to the fresh dial — the transcript replays at the next turn
   * open and the stream continues without losing context. */
  private resetForRedial(): boolean {
    const healing = this.selfHealing;
    this.selfHealing = false;
    if (!healing) {
      this.transcript = "";
      this.oneShotText = [];
    }
    this.accumulator.clear();
    this.stats.reconnects++;
    return healing;
  }

  /** Loud terminal end: log, count, surface as a redial_failed event, and end
   * the streams — a session the client will never resume must not hang the
   * run loop silently. An OutOfCreditsError additionally rethrows through
   * run()/waves() (next wave awaited ends in the typed error). */
  private endFatal(err: unknown): void {
    if (this.fatalEnded) return; // one loud terminal end per session
    this.fatalEnded = true;
    if (err instanceof OutOfCreditsError) this.fatalErr = err;
    // errMessage, not err.message/err.stack: a hostile thrown value (a Proxy,
    // a throwing getter) must not crash this handler itself.
    const message = errMessage(err);
    console.error(`[sense] session ended fatally; ending streams: ${message}`);
    this.stats.errors++;
    this.eventListeners.forEach((h) =>
      h({
        type: "redial_failed",
        message,
        code: err instanceof OutOfCreditsError ? err.code : undefined,
        raw: err,
      }),
    );
    // Recorded, not just signalled: a consumer that is between reads (no
    // waiter parked) must still see the end on its next read.
    this.streamsEnded = true;
    for (const w of this.waveWaiters.splice(0)) w(null);
    this.wakeDrive();
  }

  private onMessage(raw: string): void {
    let msg: Record<string, unknown>;
    try {
      msg = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      return;
    }
    if (this.dispatchByType(msg)) return;
    this.eventListeners.forEach((h) => h(sessionEvent(msg)));
  }

  /** Route one parsed event by type; true when fully handled client-side. */
  private dispatchByType(msg: Record<string, unknown>): boolean {
    const type = typeof msg.type === "string" ? msg.type : "";
    if (type === "response.output_text.delta" || type === "response.text.delta") {
      this.onDeltaMessage(msg);
      return true;
    }
    if (type === "response.completed" || type === "response.done" || type === "response.failed" || type === "response.cancelled") {
      this.onTurnCompleted();
      // Normal completion (done/completed) is run-loop bookkeeping. FAILED
      // and CANCELLED are turn VERDICTS the app must see: the console's
      // Apply flow barges in (response.cancel) and needs the server's
      // settlement before it re-asks, and a failed turn was previously
      // INVISIBLE — the plain `error` event path surfaces to listeners, the
      // GA response.failed terminal did not.
      return type === "response.done" || type === "response.completed";
    }
    if (type === "error") {
      // A handled error (benign code, or commit sent) stops here; a mid-turn
      // failure falls through so listeners still see the event.
      return this.onErrorMessage(msg);
    }
    return false;
  }

  private onDeltaMessage(msg: Record<string, unknown>): void {
    for (const line of String(msg.delta ?? "").split("\n")) this.onDecisionLine(line);
  }

  private onTurnCompleted(): void {
    this.flushAccumulator();
    this.turnOpen = false;
  }

  /** Returns true when the error was handled locally and needs no listener dispatch. */
  private onErrorMessage(msg: Record<string, unknown>): boolean {
    const err = msg.error as Record<string, unknown> | undefined;
    const code = err?.code;
    if (code === "unknown_event" && this.onUnknownKeepalive(err)) return true;
    this.stats.errors++;
    if (code === "insufficient_credits") return this.onInsufficientCredits(err);
    if (code === "empty_buffer" || code === "audio_input_transcript_unavailable") return true; // benign (§5.1, §7)
    if (code === "buffer_full") {
      this.sendJson({ type: "input_audio_buffer.commit" });
      return true;
    }
    if (code === "response_in_progress") return this.onResponseInProgress();
    // mid-turn failure: end the turn; the run loop re-opens.
    console.error(`[sense] lane error: ${JSON.stringify(msg.error)}`);
    this.turnOpen = false;
    return false;
  }

  /** A response is still running server-side (typically ours, racing its
   * own cancellation). Do NOT retry immediately: a response.create storm
   * into a cancelling lane keeps it wedged. Back off, let the in-flight
   * response end (done/cancelled), then the run loop re-opens. */
  private onResponseInProgress(): boolean {
    this.reopenNotBefore = Date.now() + 2_000;
    this.turnOpen = false;
    // A live cadence turn the server never settles bricks the lane: the
    // cancelled response never resolves (no done/cancelled event), every
    // reopened turn is refused with response_in_progress, and commits bind
    // nothing (empty_buffer). Prod evidence 2026-11-15: any barge-in
    // (question or state edits) left the session spinning on this pair
    // forever. Three refusals in a row with no wave in between (~6 s of
    // 2 s backoff) is the stuck signature, not the cancel race — drop the
    // wire and let the §7 reconnect dial a FRESH server session.
    if (++this.wedgeRefusals >= 3) {
      this.wedgeRefusals = 0;
      console.error("[sense] response_in_progress refused 3x with no waves — redialing fresh");
      this.selfHealing = true;
      // Do NOT null this.wire first: the install() close guard only routes a
      // close from the CURRENT wire into onClose, which runs the redial.
      void this.wire?.close();
    }
    return true;
  }

  /** An `unknown_event` naming one of the SDK's own keepalive types
   * (`session.presence`, `ping`) is expected on adapters that don't speak
   * them: mute that type for the rest of the connection (liveness still
   * comes from audio frames), log at debug, don't count it as an error.
   * Any other unknown_event falls through loud. */
  private onUnknownKeepalive(err: Record<string, unknown> | undefined): boolean {
    const message = typeof err?.message === "string" ? err.message : "";
    const match = /event type\s+(\S+)\s+is not supported/.exec(message);
    if (match === null) return false;
    const type = match[1]!;
    if (!(type in KEEPALIVE_EVENTS)) return false;
    this.mutedKeepalive.add(type);
    console.debug(`[sense] adapter does not support keepalive event ${type}; muted for this connection`);
    return true;
  }

  /** Terminal credits cut (wire contract): ONE error event, then close 1011.
   * Ends the session fatally — no backoff, no reopen, no redial; only a
   * top-up helps. Returns false so the raw error event still reaches onEvent
   * listeners (the console maps the code to the Out-of-credits UI). */
  private onInsufficientCredits(err: Record<string, unknown> | undefined): boolean {
    const message = typeof err?.message === "string" && err.message !== "" ? err.message : undefined;
    this.creditsError = new OutOfCreditsError(message);
    this.turnOpen = false;
    void this.wire?.close();
    this.endFatal(this.creditsError);
    return false;
  }

  /** A credits cut is an end, not churn: the extend/admission rails close
   * with reason 'credits_exhausted'/'insufficient_credits' — mark the typed
   * error if the reason names one, end fatally, and report no-redial. */
  private endForCredits(reason?: string): boolean {
    if (this.creditsError === null && (reason === "insufficient_credits" || reason === "credits_exhausted")) {
      this.creditsError = new OutOfCreditsError();
    }
    if (this.creditsError === null) return false;
    this.endFatal(this.creditsError);
    return true;
  }

  /** A credits cut is an error the app must see, not a quiet stream end. */
  private throwIfCreditsFatal(): void {
    if (this.fatalErr !== null) throw this.fatalErr;
  }

  onEvent(handler: (ev: SessionEvent) => void): { cancel(): void } {
    this.eventListeners.push(handler);
    return {
      cancel: () => {
        this.eventListeners = this.eventListeners.filter((h) => h !== handler);
      },
    };
  }

  /** §6.3 client-side wave assembly: one decision line per question per wave;
   * a repeat qid closes the previous wave. */
  private onDecisionLine(line: string): void {
    const trimmed = line.trim();
    if (!trimmed.startsWith("{") || !trimmed.includes('"sense.decision"')) return;
    let ev: Record<string, unknown>;
    try {
      ev = JSON.parse(trimmed) as Record<string, unknown>;
    } catch {
      return;
    }
    const qid = String(ev.id ?? ev.question ?? "");
    if (!qid) return;
    if (this.accumulator.has(qid)) this.flushAccumulator();
    this.accumulator.set(qid, ev);
    // The wave's tMs is the decision's MEDIA timestamp (protocol §6).
    // audio_t_ms IS that position on every seat since 2026-11-05; older
    // seats stamped per-wave COMPUTE time in t_ms — prefer the position.
    if (typeof ev.audio_t_ms === "number") this.accTMs = ev.audio_t_ms;
    else if (typeof ev.t_ms === "number") this.accTMs = ev.t_ms;
    this.stats.decisions++;
  }

  private flushAccumulator(): void {
    if (this.accumulator.size === 0) return;
    const payload = {
      wave_id: this.waveSeq,
      t_ms: this.accTMs,
      results: Object.fromEntries(this.accumulator) as Record<string, unknown>,
    };
    this.accumulator.clear();
    try {
      const wave = Wave.decode(this.registry, payload);
      this.waveSeq++;
      this.stats.waves++;
      this.deliver(wave);
    } catch (err) {
      if (err instanceof ProtocolError) {
        // A wave that does not answer every registered question is a
        // protocol error: log it, count it, and drop the partial group.
        // Never treat it as false/hold or let one partial wave kill the session.
        this.stats.protocolErrors++;
        console.error(`[sense] PROTOCOL ERROR: ${err.message}`);
        return;
      }
      throw err;
    }
  }

  private deliver(wave: Wave): void {
    this.context.ingest(wave);
    this.wedgeRefusals = 0; // waves flowing — any refusal now is the cancel race, not a wedge
    this.waveQueue.push(wave);
    if (this.waveQueue.length > MAX_QUEUED_WAVES) this.waveQueue.shift();
    while (this.waveWaiters.length && this.waveQueue.length) {
      this.waveWaiters.shift()!(this.waveQueue.shift() as Wave);
    }
    this.wakeDrive(); // the drive re-evaluates turn-open on wave boundaries, as the run loop did
  }

  private nextWave(): Promise<Wave | null | undefined> {
    const queued = this.waveQueue.shift();
    if (queued) return Promise.resolve(queued);
    if (this.isClosed || this.streamsEnded) {
      if (this.fatalErr !== null) return Promise.reject(this.fatalErr); // e.g. out of credits
      return Promise.resolve(null);
    }
    const { promise, resolve } = Promise.withResolvers<Wave | null | undefined>();
    this.waveWaiters.push(resolve);
    // The parked loop must re-evaluate the turn-open even when nothing is
    // flowing: input can arrive after the park, and after a barge-in the
    // server stops the cadence entirely — without a wake the turn would
    // never re-open. One wake per park; a wave consuming the waiter first
    // makes the timer a no-op.
    setTimeout(() => {
      const i = this.waveWaiters.indexOf(resolve);
      if (i >= 0) {
        this.waveWaiters.splice(i, 1);
        resolve(undefined);
      }
    }, 250);
    return promise;
  }

  // ---- public streaming surface -------------------------------------------

  /** The run loop's turn-open gate (§4: first input opens; backoff holds). */
  private shouldOpenTurn(): boolean {
    // No wire (e.g. a session_gone redial in flight): opening now would drop
    // every send yet mark the turn open, so the redialed lane never gets one.
    return (
      this.wire !== null &&
      !this.turnOpen &&
      !this.isClosed &&
      this.inputSinceTurn > 0 &&
      Date.now() >= this.reopenNotBefore
    );
  }

  /** Async-iterator of waves: THE wave-iteration path. Starts the session
   * drive here or at attach(), whichever runs first — the second is a no-op.
   * Input streams in through attach(); the drive owns dialing and turn-open. */
  async *run(): AsyncGenerator<Wave> {
    this.ensureDrive();
    for (;;) {
      const wave = await this.nextWave();
      if (wave === null) {
        this.throwIfCreditsFatal();
        return; // stream over
      }
      if (wave === undefined) continue; // wake: re-check the stream end
      yield wave;
    }
  }

  /** Start the session drive once: dial, then keep the turn-open loop alive
   * so turns open and handlers fire whether or not anyone iterates run().
   * The drive never consumes waves — deliver() queues them for run()
   * consumers (bounded, drop-oldest) and fires the handlers. */
  private ensureDrive(): void {
    if (this.driveStarted || this.isClosed) return;
    this.driveStarted = true;
    void this.driveLoop();
  }

  private async driveLoop(): Promise<void> {
    try {
      if (!this.wire && !this.isClosed && !this.streamsEnded) await this.connect();
      for (;;) {
        if (this.isClosed || this.streamsEnded) return;
        // The turn opens on first input: an empty audio commit is
        // refused and leaves the turn audio-less forever.
        if (this.shouldOpenTurn()) this.openTurn();
        await this.driveWait();
      }
    } catch (err) {
      this.endFatal(err);
    }
  }

  /** The drive's park: a 250 ms re-evaluation cadence, or an immediate wake
   * from deliver()/close()/endFatal(). */
  private driveWait(): Promise<void> {
    if (this.isClosed || this.streamsEnded) return Promise.resolve();
    return new Promise<void>((resolve) => {
      let settled = false;
      const finish = (): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        const i = this.driveWaiters.indexOf(finish);
        if (i >= 0) this.driveWaiters.splice(i, 1);
        resolve();
      };
      const timer = setTimeout(finish, 250);
      this.driveWaiters.push(finish);
    });
  }

  /** Wake the drive's park immediately (wave delivered, terminal end, close). */
  private wakeDrive(): void {
    for (const w of this.driveWaiters.splice(0)) w();
  }

  /** THE input path — one call, one sources object: `{ stream?, audio?,
   * video?, text? }`. `stream` is a live browser MediaStream (mic and/or
   * camera, resampled to PCM16 24 kHz mono audio + JPEG video at the session
   * cadence); audio chunks are PCM16 24 kHz mono bytes, video chunks are JPEG
   * bytes, text chunks are strings appended to the transcript — each source
   * is an async iterable, a sync iterable, or a zero-arg callable returning
   * the next chunk (null/undefined ends it). Returns a handle whose
   * detach() stops everything attached here; a MediaStream's tracks stay
   * owned by the caller. Iterate `run()` to consume the waves. */
  async attach(sources: InputSources, opts?: AttachOptions): Promise<AttachHandle> {
    this.ensureDrive(); // attach() starts the session: dial + turn-open loop
    let stopped = false;
    const stops: (() => void)[] = [];
    const handle: AttachHandle = {
      detach: () => {
        stopped = true;
        for (const stop of stops.splice(0)) stop();
      },
    };
    if (sources.stream !== undefined) {
      const media = await attachStream(this, sources.stream, opts);
      stops.push(() => media.detach());
    }
    for (const kind of ["audio", "video", "text"] as const) {
      const src = sources[kind];
      if (src === undefined || src === null) continue;
      const pump = pumpSource(this, kind, src, () => stopped);
      stops.push(pump.return);
      void pump.done.catch((err: unknown) =>
        console.error(`[sense] ${kind} input pump failed: ${errMessage(err)}`),
      );
    }
    return handle;
  }

  async close(): Promise<void> {
    this.isClosed = true;
    this.stopPresence();
    this.wakeDrive(); // the drive loop must not park through a close
    for (const w of this.waveWaiters.splice(0)) w(null);
    const wire = this.wire;
    this.wire = null;
    if (wire) await wire.close();
  }
}

/** One programmatic input source: an async iterable, a sync iterable, or a
 * zero-arg callable returning the next chunk (null/undefined ends it). */
export type InputStreamSource =
  | AsyncIterable<unknown>
  | Iterable<unknown>
  | (() => unknown | null | undefined | Promise<unknown | null | undefined>);

/** Attach sources — one call, one object. `stream` is the live browser
 * MediaStream path; the rest are programmatic pumps: audio chunks are
 * PCM16 24 kHz mono bytes, video chunks are JPEG bytes, text chunks are
 * strings appended to the transcript (append mode). */
export interface InputSources {
  stream?: AttachStream;
  audio?: InputStreamSource;
  video?: InputStreamSource;
  text?: InputStreamSource;
}

type TextMode = "append" | "replace" | "event";

function pumpSource(
  session: Session,
  kind: "audio" | "video" | "text",
  src: InputStreamSource,
  isStopped: () => boolean,
): { done: Promise<void>; return(): void } {
  const push = (chunk: unknown): void => {
    if (kind === "audio") session.emitAudio(toB64(chunk as Uint8Array | ArrayBuffer));
    else if (kind === "video") session.emitVideo(chunk as Uint8Array | ArrayBuffer);
    else session.emitText(String(chunk), "append");
  };
  if (typeof src === "function") {
    const done = (async () => {
      for (;;) {
        if (isStopped()) return;
        const chunk = await src();
        if (chunk === null || chunk === undefined) return;
        push(chunk);
      }
    })();
    return { done, return: () => {} }; // cooperative: observed at the next call
  }
  const it: AsyncIterator<unknown> =
    typeof (src as AsyncIterable<unknown>)[Symbol.asyncIterator] === "function"
      ? (src as AsyncIterable<unknown>)[Symbol.asyncIterator]()
      : syncAsAsync((src as Iterable<unknown>)[Symbol.iterator]());
  const done = (async () => {
    try {
      for (;;) {
        const r = await it.next();
        if (r.done || isStopped()) return;
        push(r.value);
      }
    } finally {
      await it.return?.().catch(() => {}); // release a blocked producer
    }
  })();
  return {
    done,
    return: () => {
      void it.return?.().catch(() => {});
    },
  };
}

/** Minimal AsyncIterator view over a sync iterator. */
function syncAsAsync(sync: Iterator<unknown>): AsyncIterator<unknown> {
  return {
    next: () => Promise.resolve(sync.next()),
    return: (value?: unknown) => {
      sync.return?.(value);
      return Promise.resolve({ value, done: true });
    },
  } as AsyncIterator<unknown>;
}


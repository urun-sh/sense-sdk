/** Context: the live view over a wave stream (latest, coalesced waves,
 * edge-triggered when, keyed background runs). */
import type { Predicate } from "./predicate.ts";
import type { Wave, Result } from "./wave.ts";
import { errMessage } from "./errors.ts";

export type WaveHandler = (ctx: Context) => void;

interface WhenHandle {
  pred: Predicate;
  handler: WaveHandler;
  prev: boolean | undefined;
  cancelled: boolean;
}

interface WaveHandlerEntry {
  handler: WaveHandler;
  coalesce: boolean;
}

/** The argmax signature used for coalescing. A run of consecutive waves with
 * identical signatures collapses to its LAST wave in `waves(coalesce)`, while
 * live `onWave({ coalesce })` delivers only the FIRST wave of the run. */
export function coalesceSignature(w: Wave): string {
  // Structured encoding (JSON of [questionId, topLabel] pairs) so distinct
  // argmax values can never collide, even when ids or labels contain ";",
  // "=", or any other join delimiter.
  return JSON.stringify(w.questionIds().map((qid) => [qid, w.result(qid).top]));
}

/** Bounded wave history (Python parity). */
export const HISTORY_MAX = 512;

/** Run an application handler; a throw (or async rejection) is logged, never
 * propagated into the wire's message callback where it would kill the session
 * or crash the host process. */
function fire(handler: WaveHandler, ctx: Context): void {
  try {
    const r: unknown = handler(ctx);
    if (r instanceof Promise) r.catch((err: unknown) => console.error(`[sense] handler failed: ${errMessage(err)}`));
  } catch (err) {
    console.error(`[sense] handler failed: ${errMessage(err)}`);
  }
}

/** Evaluate an app predicate. One that throws (e.g. it reads an option the
 * server omitted) must not end the session; it counts as false this wave. */
function evalPredicate(pred: Predicate, wave: Wave): boolean {
  try {
    return pred(wave);
  } catch (err) {
    console.error(`[sense] predicate failed; treating as false: ${errMessage(err)}`);
    return false;
  }
}

export class Context {
  private history: Wave[] = [];
  private whens: WhenHandle[] = [];
  private waveHandlers: WaveHandlerEntry[] = [];
  private runs = new Map<string, { controller: AbortController; done: Promise<unknown> }>();
  /** The active run's signal (most recent run), or null outside a run. */
  signal: AbortSignal | null = null;

  constructor(readonly session: unknown = null) {}

  /** Feed one wave through the context (edge triggers fire here). */
  ingest(wave: Wave): void {
    // Live coalescing (python Session._accept): a coalescing handler skips
    // delivery when the PREVIOUS wave carries the identical argmax signature.
    const prev = this.remember(wave);
    for (const h of this.waveHandlers.slice()) {
      if (h.coalesce && prev && coalesceSignature(prev) === coalesceSignature(wave)) continue;
      fire(h.handler, this);
    }
    for (const h of this.whens) {
      if (h.cancelled) continue;
      const now = evalPredicate(h.pred, wave);
      if (now && h.prev !== true) fire(h.handler, this);
      h.prev = now;
    }
  }

  /** Append to the bounded history; returns the previous wave, if any. */
  private remember(wave: Wave): Wave | null {
    const prev = this.history[this.history.length - 1] ?? null;
    this.history.push(wave);
    if (this.history.length > HISTORY_MAX) this.history.splice(0, this.history.length - HISTORY_MAX);
    return prev;
  }

  latest(): Wave | null {
    return this.history[this.history.length - 1] ?? null;
  }

  waves(coalesce = false): Wave[] {
    if (!coalesce) return this.history.slice();
    const out: Wave[] = [];
    for (const w of this.history) {
      const prev = out[out.length - 1];
      if (prev && coalesceSignature(prev) === coalesceSignature(w)) out[out.length - 1] = w;
      else out.push(w);
    }
    return out;
  }

  when(pred: Predicate, handler: WaveHandler): { cancel(): void } {
    const handle: WhenHandle = { pred, handler, prev: undefined, cancelled: false };
    this.whens.push(handle);
    return {
      cancel: () => {
        handle.cancelled = true;
      },
    };
  }

  /** Observe waves. `coalesce` skips delivery of waves whose argmax signature
   * equals the previous wave's, so the handler receives the FIRST wave in a
   * matching run; later waves update `latest()` without another callback. */
  onWave(handler: WaveHandler, opts: { coalesce?: boolean } = {}): { cancel(): void } {
    const entry: WaveHandlerEntry = { handler, coalesce: opts.coalesce ?? false };
    this.waveHandlers.push(entry);
    return {
      cancel: () => {
        this.waveHandlers = this.waveHandlers.filter((h) => h !== entry);
      },
    };
  }

  /** Start a keyed background run. Duplicate active keys reject unless
   * `replace` cancels the previous run (via onCancel + signal). */
  run(
    key: string,
    fn: (ctx: Context) => unknown,
    opts: { onCancel?: (key: string) => void; replace?: boolean } = {},
  ): Promise<unknown> {
    const existing = this.runs.get(key);
    if (existing && !opts.replace) {
      return Promise.reject(new Error(`run "${key}" is already active`));
    }
    if (existing) this.cancel(key);
    const controller = new AbortController();
    this.signal = controller.signal;
    const { promise: aborted, reject: rejectAbort } = Promise.withResolvers<never>();
    controller.signal.addEventListener("abort", () => {
      opts.onCancel?.(key);
      rejectAbort(new Error(`run "${key}" cancelled`));
    });
    const done = Promise.race([Promise.resolve(fn(this)), aborted]).finally(() => {
      this.runs.delete(key);
      if (this.signal === controller.signal) this.signal = null;
    });
    this.runs.set(key, { controller, done });
    return done;
  }

  /** Abort a keyed run (its signal + onCancel hook fire). */
  cancel(key: string): void {
    this.runs.get(key)?.controller.abort();
  }

  /** Latest result for a question, or null before the first wave. */
  get(question: string): Result | null {
    return this.latest()?.result(question) ?? null;
  }
}

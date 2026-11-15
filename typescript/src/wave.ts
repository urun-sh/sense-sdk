/** Wave + results: the decoded answer set for one cadence tick (protocol §6). */
import type { Question } from "./question.ts";
import { ProtocolError } from "./errors.ts";

/** One question's answer in a wave. */
export class Result {
  readonly id: string;
  readonly kind: "noul" | "choice" | "score";
  readonly waveId: number;
  /** noul: boolean; choice: top option name; score: expected ordinal Σi·p. */
  readonly value: boolean | string | number;
  /** noul: P(true); choice: P(top); score: P(top level). */
  readonly p: number;
  readonly confidence: number;
  /** Label-keyed probabilities (score: level names). */
  readonly distribution: Record<string, number>;
  /** argmax label ("true"/"false" for noul, option name, level name). */
  readonly top: string;

  constructor(init: {
    id: string;
    kind: "noul" | "choice" | "score";
    waveId: number;
    value: boolean | string | number;
    p: number;
    confidence: number;
    distribution: Record<string, number>;
    top: string;
  }) {
    this.id = init.id;
    this.kind = init.kind;
    this.waveId = init.waveId;
    this.value = init.value;
    this.p = init.p;
    this.confidence = init.confidence;
    this.distribution = init.distribution;
    this.top = init.top;
  }

  pOption(name: string): number {
    if (this.kind === "score") {
      throw new ProtocolError("protocol", `pOption(${name}) on score question ${this.id}`);
    }
    return this.distribution[name] ?? 0;
  }

  /** Only valid on score results: Σ p over levels >= the given level. */
  pAtLeast(level: string): number {
    throw new ProtocolError("protocol", `pAtLeast(${level}) on non-score question ${this.id}`);
  }
}

/** Score result: ordinal distribution over ordered levels. */
export class Score extends Result {
  declare readonly kind: "score";
  declare readonly value: number;
  readonly levels: string[];
  /** argmax level name. */
  readonly level: string;

  constructor(init: {
    id: string;
    waveId: number;
    probs: Record<string, number>;
    levels: string[];
    confidence: number;
  }) {
    const byName: Record<string, number> = {};
    let expected = 0;
    let bestIdx = 0;
    init.levels.forEach((name, i) => {
      const p = init.probs[String(i)] ?? 0;
      byName[name] = p;
      expected += i * p;
      if (p > (init.probs[String(bestIdx)] ?? 0)) bestIdx = i;
    });
    const level = init.levels[bestIdx] ?? "";
    super({
      id: init.id,
      kind: "score",
      waveId: init.waveId,
      value: expected,
      p: byName[level] ?? 0,
      confidence: init.confidence,
      distribution: byName,
      top: level,
    });
    this.levels = init.levels;
    this.level = level;
  }

  override pAtLeast(level: string): number {
    const idx = this.levels.indexOf(level);
    if (idx < 0) {
      throw new ProtocolError("protocol", `unknown level "${level}" for ${this.id}`);
    }
    let sum = 0;
    for (let i = idx; i < this.levels.length; i++) {
      sum += this.distribution[this.levels[i] as string] ?? 0;
    }
    return sum;
  }
}

const noulTop = (choice: boolean) => (choice ? "true" : "false");

/** One wave: exactly one answer per registered question (§6). */
export class Wave {
  readonly waveId: number;
  readonly tMs: number;
  private readonly results: Record<string, Result>;

  constructor(waveId: number, tMs: number, results: Record<string, Result>) {
    this.waveId = waveId;
    this.tMs = tMs;
    this.results = results;
  }

  result(qid: string): Result {
    const r = this.results[qid];
    if (!r) {
      throw new ProtocolError("unknown_question", `no result for question "${qid}"`);
    }
    return r;
  }

  has(qid: string): boolean {
    return qid in this.results;
  }

  questionIds(): string[] {
    return Object.keys(this.results);
  }

  /** Decode one wire payload ({wave_id, t_ms, results}) against registered questions. */
  static decode(questions: readonly Question[], payload: unknown): Wave {
    if (typeof payload !== "object" || payload === null) {
      throw new ProtocolError("protocol", "wave payload must be an object");
    }
    const raw = payload as { wave_id?: unknown; t_ms?: unknown; results?: unknown };
    if (typeof raw.results !== "object" || raw.results === null) {
      throw new ProtocolError("protocol", "wave payload is missing results");
    }
    const incoming = raw.results as Record<string, unknown>;
    const results: Record<string, Result> = {};
    for (const q of questions) {
      const ans = incoming[q.id];
      if (ans === undefined) {
        throw new ProtocolError("missing_answer", `wave has no answer for registered question "${q.id}"`);
      }
      results[q.id] = decodeResult(q, ans, numOr(raw.wave_id, 0));
    }
    for (const qid of Object.keys(incoming)) {
      if (!questions.some((q) => q.id === qid)) {
        throw new ProtocolError("unknown_question", `wave carries unknown question "${qid}"`);
      }
    }
    return new Wave(numOr(raw.wave_id, 0), numOr(raw.t_ms, 0), results);
  }
}

/** An optional wire number: absent/non-numeric falls back; a present but
 * non-finite number (e.g. JSON 1e400) is a protocol error (Python parity). */
function numOr(v: unknown, fallback: number): number {
  if (typeof v !== "number") return fallback;
  if (!Number.isFinite(v)) throw new ProtocolError("protocol", `wave field is not finite: ${v}`);
  return v;
}

/** Slack for float rounding when range-checking probabilities. */
const P_EPS = 1e-6;

/** A wire number as a finite number; numeric strings are tolerated
 * (model-emitted lines), booleans/NaN/Infinity are protocol errors. */
function finite(v: unknown, what: string): number {
  const n = typeof v === "number" ? v : typeof v === "string" && v.trim() !== "" ? Number(v) : NaN;
  if (!Number.isFinite(n)) throw new ProtocolError("protocol", `${what} is not a finite number: ${JSON.stringify(v)}`);
  return n;
}

function prob(v: unknown, what: string): number {
  const n = finite(v, what);
  if (n < -P_EPS || n > 1 + P_EPS) throw new ProtocolError("protocol", `${what} is outside [0, 1]: ${n}`);
  return n;
}

function decodeAnswerEnvelope(q: Question, ans: unknown): {
  a: { choice?: unknown; probabilities?: unknown; confidence?: unknown; noul?: unknown };
  probs: Record<string, number>;
  confidence: number;
} {
  if (typeof ans !== "object" || ans === null || Array.isArray(ans)) {
    throw new ProtocolError("protocol", `answer for "${q.id}" must be an object`);
  }
  const a = ans as { choice?: unknown; probabilities?: unknown; confidence?: unknown; noul?: unknown };
  // A skipped answer is a protocol error, never a silent zero (Python parity).
  if (a.confidence === undefined || a.confidence === null) {
    throw new ProtocolError("missing_answer", `null answer for question "${q.id}"`);
  }
  const confidence = finite(a.confidence, `confidence for "${q.id}"`);
  const rawProbs = a.probabilities ?? {};
  if (typeof rawProbs !== "object" || rawProbs === null || Array.isArray(rawProbs)) {
    throw new ProtocolError("protocol", `probabilities for "${q.id}" must be an object`);
  }
  // fromEntries defines own properties, so a "__proto__" key stays data.
  const probs = Object.fromEntries(
    Object.entries(rawProbs).map(([k, v]) => [k, prob(v, `P(${JSON.stringify(k)}) for "${q.id}"`)]),
  ) as Record<string, number>;
  return { a, probs, confidence };
}

function decodeNoulResult(
  q: Question,
  a: { choice?: unknown; noul?: unknown },
  probs: Record<string, number>,
  confidence: number,
  waveId: number,
): Result {
  let choice: boolean;
  if (typeof a.choice === "boolean") {
    choice = a.choice;
  } else if (a.choice === "true" || a.choice === "false") {
    choice = a.choice === "true";
  } else {
    throw new ProtocolError("protocol", `noul answer for "${q.id}" has bad choice ${JSON.stringify(a.choice)}`);
  }
  const rawP = Object.hasOwn(probs, "true") ? probs["true"] : a.noul;
  if (rawP === undefined || rawP === null) {
    throw new ProtocolError("protocol", `noul answer for "${q.id}" has no P(true)`);
  }
  const pTrue = prob(rawP, `P(true) for "${q.id}"`);
  return new Result({
    id: q.id,
    kind: "noul",
    waveId,
    value: choice,
    p: pTrue,
    confidence,
    distribution: probs,
    top: noulTop(choice),
  });
}

function decodeChoiceResult(
  q: Question,
  a: { choice?: unknown },
  probs: Record<string, number>,
  confidence: number,
  waveId: number,
): Result {
  const top = typeof a.choice === "string" ? a.choice : argmax(probs);
  if (top === undefined) {
    throw new ProtocolError("protocol", `choice answer for "${q.id}" has no choice`);
  }
  // Only registered labels are valid answers: an unregistered label would
  // otherwise flow into application logic and UI unchecked.
  if (q.options?.length && !q.options.includes(top)) {
    throw new ProtocolError("protocol", `choice answer for "${q.id}" is not a registered option: ${JSON.stringify(top)}`);
  }
  return new Result({
    id: q.id,
    kind: "choice",
    waveId,
    value: top,
    p: probs[top] ?? 0,
    confidence,
    distribution: probs,
    top,
  });
}

function decodeResult(q: Question, ans: unknown, waveId: number): Result {
  const { a, probs, confidence } = decodeAnswerEnvelope(q, ans);
  if (q.kind === "noul") {
    return decodeNoulResult(q, a, probs, confidence, waveId);
  }
  if (q.kind === "choice") {
    return decodeChoiceResult(q, a, probs, confidence, waveId);
  }
  // score
  if (!q.levels?.length) {
    throw new ProtocolError("bad_question", `score question "${q.id}" has no levels`);
  }
  for (const k of Object.keys(probs)) {
    const idx = /^\d+$/.test(k) ? Number(k) : -1;
    if (idx < 0 || idx >= q.levels.length) {
      throw new ProtocolError("protocol", `score answer for "${q.id}" has bad level index ${JSON.stringify(k)}`);
    }
  }
  const missing = q.levels.map((_, i) => i).filter((i) => !Object.hasOwn(probs, String(i)));
  if (missing.length) {
    throw new ProtocolError("missing_answer", `score answer for "${q.id}" is missing level(s) ${missing.join(", ")}`);
  }
  return new Score({
    id: q.id,
    waveId,
    probs,
    levels: q.levels,
    confidence,
  });
}

function argmax(probs: Record<string, number>): string | undefined {
  let best: string | undefined;
  for (const [k, v] of Object.entries(probs)) {
    if (best === undefined || v > (probs[best as string] ?? -Infinity)) best = k;
  }
  return best;
}

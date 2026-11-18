/** Questions and the instructions envelope (protocol §2).
 *
 * One question shape everywhere — Jev's (TypeSafe System One):
 * `{ type, instructions, criteria }` on the wire; `{ instructions, criteria }`
 * in the app. The question NAME is the answer key; there is no separate id and
 * no auto-slugging. */
import { opsFor } from "./predicate.ts";
import type { Predicate } from "./predicate.ts";

export type QuestionKind = "noul" | "choice" | "score";

/** noul criteria: an optional {true, false} description pair. */
export interface NoulCriteria {
  true: string;
  false: string;
}

/** choice criteria: option name -> description (insertion order is the wire
 * order; every description must be non-empty). */
export type ChoiceCriteria = Record<string, string>;

/** score criteria: an ORDERED array of level descriptions, low end first
 * (2..10 entries). The list IS Jev's shape for scores. */
export type ScoreCriteria = string[];

export interface QuestionInit {
  /** The answer key on every wave. */
  name: string;
  kind: QuestionKind;
  /** What the model should answer; non-empty when given. */
  instructions?: string;
  /** noul: optional {true,false} map; choice: name -> description map;
   * score: ordered array of level descriptions (low end first). */
  criteria?: NoulCriteria | ChoiceCriteria | ScoreCriteria;
}

/** Validated criteria for one kind: option/level names for decode-time
 * validation, plus the description map (choice/noul) kept for the envelope. */
function parseCriteria(
  kind: QuestionKind,
  name: string,
  criteria: QuestionInit["criteria"],
): { options?: string[]; levels?: string[]; descriptions?: Record<string, string> } {
  if (kind === "score") return scoreCriteria(name, criteria);
  if (kind === "noul") return noulCriteria(name, criteria);
  return choiceCriteria(name, criteria);
}

function scoreCriteria(name: string, criteria: QuestionInit["criteria"]): { levels: string[] } {
  if (!Array.isArray(criteria) || criteria.length < 2 || criteria.length > 10 ||
    criteria.some((d) => typeof d !== "string" || d.trim() === "")) {
    throw new RangeError(`score question "${name}" needs 2..10 non-empty level descriptions (low end first)`);
  }
  return { levels: criteria };
}

function noulCriteria(name: string, criteria: QuestionInit["criteria"]): { options: string[]; descriptions: Record<string, string> } | {} {
  if (criteria === undefined) return {};
  const c = criteria as NoulCriteria;
  if (typeof c !== "object" || Array.isArray(c) || Object.keys(c).sort().join() !== "false,true" ||
    [c.true, c.false].some((d) => typeof d !== "string" || d.trim() === "")) {
    throw new Error(`noul question "${name}" criteria must be a {true, false} description map with non-empty descriptions`);
  }
  return { descriptions: { true: c.true, false: c.false }, options: ["true", "false"] };
}

function choiceCriteria(name: string, criteria: QuestionInit["criteria"]): { options: string[]; descriptions: Record<string, string> } {
  const c = criteria as ChoiceCriteria;
  if (typeof c !== "object" || Array.isArray(c) || Object.keys(c).length === 0) {
    throw new Error(`choice question "${name}" needs a non-empty criteria map (name -> description)`);
  }
  for (const [k, d] of Object.entries(c)) {
    if (typeof d !== "string" || d.trim() === "") {
      throw new Error(`choice question "${name}" needs a non-empty description for option "${k}"`);
    }
  }
  return { descriptions: c, options: Object.keys(c) };
}

/** One registered question. */
export class Question {
  /** The answer key: `wave.result(name)`, `q.top.eq(...)`, predicates. */
  readonly name: string;
  readonly kind: QuestionKind;
  readonly instructions?: string;
  /** choice/noul option NAMES (keys of the criteria map), so predicates and
   * wave validation keep working. */
  readonly options?: string[];
  /** score level DESCRIPTIONS, ordinal order (low end first). The server
   * echoes the description back as the score's choice. */
  readonly levels?: string[];
  /** choice/noul criteria: name -> description. */
  readonly descriptions?: Record<string, string>;

  constructor(init: QuestionInit) {
    if (typeof init.name !== "string" || init.name.trim() === "") {
      throw new Error("question name must be a non-empty string");
    }
    this.name = init.name;
    this.kind = init.kind;
    if (init.instructions !== undefined) {
      if (typeof init.instructions !== "string" || init.instructions.trim() === "") {
        throw new Error(`instructions for question "${init.name}" must be a non-empty string`);
      }
      this.instructions = init.instructions;
    }
    const parsed = parseCriteria(init.kind, init.name, init.criteria);
    this.options = parsed.options;
    this.levels = parsed.levels;
    this.descriptions = parsed.descriptions;
  }

  /** argmax accessor: `actions.top.eq("respond")`. */
  get top() {
    return opsFor({ type: "top", question: this.name });
  }

  /** score expected-ordinal accessor. */
  get value() {
    return opsFor({ type: "value", question: this.name });
  }

  /** noul P(true) accessor; choice: `intent.p("support").gte(0.7)`. */
  get p() {
    return opsFor({ type: "p", question: this.name });
  }

  /** score level-threshold accessor: a zero-based index or the exact level
   * description. */
  pAtLeast(level: number | string) {
    return opsFor({ type: "p_at_least", question: this.name, level });
  }

  // Argmax sugar on the question itself: `intent.eq("support")` == the top
  // option equals "support"; `finished.eq(true)` / `finished.and(...)` /
  // `finished.not()` for noul. Same trees the accessors build.
  private argmax() {
    return opsFor({ type: "question", id: this.name });
  }
  eq(value: string | number | boolean) { return this.argmax().eq(value); }
  ne(value: string | number | boolean) { return this.argmax().ne(value); }
  gt(value: number) { return this.argmax().gt(value); }
  gte(value: number) { return this.argmax().gte(value); }
  lt(value: number) { return this.argmax().lt(value); }
  lte(value: number) { return this.argmax().lte(value); }
  and(...others: Predicate[]) { return this.argmax().truthy().and(...others); }
  or(...others: Predicate[]) { return this.argmax().truthy().or(...others); }
  not() { return this.argmax().truthy().not(); }
}

/** The §2 instructions envelope body (values, not the JSON string). Only
 * Jev's keys go on the wire: `type`, `instructions`, `criteria` — noul omits
 * `criteria` when not given, choice sends the name -> description map, score
 * sends the ordered description array. */
export function instructionsEnvelope(
  questions: readonly Question[],
  hz: number,
): { questions: Record<string, Record<string, unknown>>; hz: number } {
  const out: Record<string, Record<string, unknown>> = {};
  for (const q of questions) {
    const entry: Record<string, unknown> = { type: q.kind, instructions: q.instructions ?? "" };
    if (q.kind === "score") entry.criteria = q.levels;
    else if (q.descriptions) entry.criteria = q.descriptions;
    out[q.name] = entry;
  }
  return { questions: out, hz };
}

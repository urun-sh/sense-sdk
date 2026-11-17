/** Questions and the instructions envelope (protocol §2). */
import { opsFor } from "./predicate.ts";
import type { Predicate } from "./predicate.ts";

export type QuestionKind = "noul" | "choice" | "score";

export interface QuestionInit {
  id: string;
  kind: QuestionKind;
  prompt?: string;
  /** choice option names, or a name -> description map (Jev-style criteria;
   * insertion order is the wire order). */
  options?: string[] | Record<string, string>;
  /** score level names (ordinal order), or a name -> description map. */
  levels?: string[] | Record<string, string>;
}

/** Names + descriptions from either option form: an array of names, or a
 * Jev-style name -> description map (keys are the names, insertion order
 * preserved). */
function optionParts(
  v: string[] | Record<string, string> | undefined,
): { names?: string[]; descriptions?: Record<string, string> } {
  if (v === undefined) return {};
  if (Array.isArray(v)) return { names: v };
  return { names: Object.keys(v), descriptions: v };
}

/** One registered question. */
export class Question {
  readonly id: string;
  readonly kind: QuestionKind;
  readonly prompt?: string;
  /** option/level NAMES (a map form is flattened; descriptions live in
   * `descriptions`), so predicates and wave validation keep working. */
  readonly options?: string[];
  readonly levels?: string[];
  /** name -> description when registered as a map (Jev-style criteria). */
  readonly descriptions?: Record<string, string>;

  constructor(init: QuestionInit) {
    this.id = init.id;
    this.kind = init.kind;
    this.prompt = init.prompt;
    const opt = optionParts(init.options);
    const lvl = optionParts(init.levels);
    this.options = opt.names;
    this.levels = lvl.names;
    this.descriptions = opt.descriptions ?? lvl.descriptions;
    if (init.kind === "score" && (lvl.names?.length ?? 0) < 2) {
      throw new RangeError(`score question "${init.id}" needs 2..10 levels`);
    }
  }

  /** argmax accessor: `actions.top.eq("respond")`. */
  get top() {
    return opsFor({ type: "top", question: this.id });
  }

  /** score expected-ordinal accessor. */
  get value() {
    return opsFor({ type: "value", question: this.id });
  }

  /** noul P(true) accessor; choice: `intent.p("support").gte(0.7)`. */
  get p() {
    return opsFor({ type: "p", question: this.id });
  }

  /** score level-threshold accessor. */
  pAtLeast(level: string) {
    return opsFor({ type: "p_at_least", question: this.id, level });
  }

  // Argmax sugar on the question itself: `intent.eq("support")` == the top
  // option equals "support"; `finished.eq(true)` / `finished.and(...)` /
  // `finished.not()` for noul. Same trees the accessors build.
  private argmax() {
    return opsFor({ type: "question", id: this.id });
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

/** The §2 envelope entry for a score question: an ordered array so the
 * server uses the registered score levels rather than interpreting a string
 * as free text; a map keeps each level's description (insertion order is
 * the ordinal order). */
function scoreEntry(q: Question): Record<string, unknown> {
  return { kind: q.kind, prompt: q.prompt ?? "", criteria: q.descriptions ?? q.levels ?? [] };
}

/** The §2 envelope entry for a noul question with descriptions: the
 * documented pair form `[name, description]`, canonicalized to no-first
 * (the first option is the false arm — same rule the python facade
 * applies). */
function noulEntry(q: Question): Record<string, unknown> {
  const pairs = (q.options ?? []).map((n): [string, string] => [n, q.descriptions?.[n] ?? ""]);
  const [first, second] = pairs;
  if (
    pairs.length === 2 && first && second &&
    first[0].toLowerCase() === "yes" && second[0].toLowerCase() === "no"
  ) {
    pairs.reverse();
  }
  return { kind: q.kind, prompt: q.prompt ?? "", options: pairs };
}

/** The §2 envelope entry for a choice question: a criteria map when the
 * question carries descriptions (canonical TypeSafe wire), else the bare
 * option-name list. */
function choiceEntry(q: Question): Record<string, unknown> {
  if (q.descriptions) return { kind: q.kind, prompt: q.prompt ?? "", criteria: q.descriptions };
  return { kind: q.kind, prompt: q.prompt ?? "", ...(q.options ? { options: q.options } : {}) };
}

/** The §2 instructions envelope body (values, not the JSON string). */
export function instructionsEnvelope(
  questions: readonly Question[],
  hz: number,
): { questions: Record<string, Record<string, unknown>>; hz: number } {
  const out: Record<string, Record<string, unknown>> = {};
  for (const q of questions) {
    out[q.id] =
      q.kind === "score" ? scoreEntry(q)
      : q.descriptions ? (q.kind === "noul" ? noulEntry(q) : choiceEntry(q))
      : choiceEntry(q);
  }
  return { questions: out, hz };
}

/** Questions and the instructions envelope (protocol §2). */
import { opsFor } from "./predicate.ts";
import type { Predicate } from "./predicate.ts";

export type QuestionKind = "noul" | "choice" | "score";

export interface QuestionInit {
  id: string;
  kind: QuestionKind;
  prompt?: string;
  /** choice option names (noul: server defaults to true/false). */
  options?: string[];
  /** score level names, ordinal order. */
  levels?: string[];
}

/** One registered question. */
export class Question {
  readonly id: string;
  readonly kind: QuestionKind;
  readonly prompt?: string;
  readonly options?: string[];
  readonly levels?: string[];

  constructor(init: QuestionInit) {
    this.id = init.id;
    this.kind = init.kind;
    this.prompt = init.prompt;
    this.options = init.options;
    this.levels = init.levels;
    if (init.kind === "score" && (init.levels?.length ?? 0) < 2) {
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

/** The §2 instructions envelope body (values, not the JSON string). */
export function instructionsEnvelope(
  questions: readonly Question[],
  hz: number,
): { questions: Record<string, Record<string, unknown>>; hz: number } {
  const out: Record<string, Record<string, unknown>> = {};
  for (const q of questions) {
    if (q.kind === "score") {
      // Send an ordered array so the server uses the registered score
      // levels rather than interpreting a string as free text.
      out[q.id] = {
        kind: q.kind,
        prompt: q.prompt ?? "",
        criteria: q.levels ?? [],
      };
    } else {
      out[q.id] = {
        kind: q.kind,
        prompt: q.prompt ?? "",
        ...(q.options ? { options: q.options } : {}),
      };
    }
  }
  return { questions: out, hz };
}

/** Predicates: composable boolean expressions over wave subjects. */
import { Score } from "./wave.ts";
import type { Wave } from "./wave.ts";
import type { Result } from "./wave.ts";

/** A subject a predicate reads off a wave. */
export type Subject =
  | { type: "q" | "question"; id: string }
  | { type: "p"; question: string; option?: string }
  | { type: "top"; question: string }
  | { type: "value"; question: string }
  | { type: "p_at_least"; question: string; level: string };

export type Op = "eq" | "ne" | "gt" | "gte" | "lt" | "lte";
type Prim = string | number | boolean;

type Node =
  | { t: "truthy"; subject: Subject }
  | { t: "op"; op: Op; subject: Subject; value: Prim }
  | { t: "between"; subject: Subject; enter: number; exit: number }
  | { t: "and" | "or"; of: Predicate[] }
  | { t: "not"; of: Predicate };

/** A callable predicate: `pred(wave) -> boolean`, composable via and/or/not. */
export interface Predicate {
  (wave: Wave): boolean;
  node: Node;
  and(...others: Predicate[]): Predicate;
  or(...others: Predicate[]): Predicate;
  not(): Predicate;
}

function make(node: Node, state?: { held: boolean }): Predicate {
  const pred = ((wave: Wave): boolean => {
    switch (node.t) {
      case "truthy":
        return Boolean(readSubject(wave, node.subject));
      case "op":
        return compare(node.op, readSubject(wave, node.subject, node.op), node.value);
      case "between": {
        const v = Number(readSubject(wave, node.subject));
        if (v >= node.enter) state!.held = true;
        else if (v < node.exit) state!.held = false;
        return state!.held;
      }
      case "and":
        return node.of.every((p) => p(wave));
      case "or":
        return node.of.some((p) => p(wave));
      case "not":
        return !node.of(wave);
    }
  }) as Predicate;
  pred.node = node;
  pred.and = (...others: Predicate[]) => allOf(pred, ...others);
  pred.or = (...others: Predicate[]) => anyOf(pred, ...others);
  pred.not = () => make({ t: "not", of: pred });
  return pred;
}

function readQuestionSubject(wave: Wave, s: Extract<Subject, { type: "q" | "question" }>, op?: Op): Prim {
  const res = wave.result(s.id);
  // Score comparison rule (mirrors python _subject_value): an EXPLICIT
  // eq/ne compares the top level NAME (argmax); gt/gte/lt/lte compare
  // the EXPECTED ORDINAL. Operator-free reads (bare truthy, between)
  // also stay numeric so Number() never sees a level name.
  return res instanceof Score ? (op === "eq" || op === "ne" ? res.top : res.value) : res.value;
}

function readResultField(wave: Wave, s: Subject): Prim | undefined {
  const r = (qid: string): Result => wave.result(qid);
  switch (s.type) {
    case "p": {
      const res = r(s.question);
      return s.option === undefined ? res.p : res.pOption(s.option);
    }
    case "top":
      return r(s.question).top;
    case "value":
      return r(s.question).value;
    case "p_at_least":
      return r(s.question).pAtLeast(s.level);
    default:
      return undefined;
  }
}

function readSubject(wave: Wave, s: Subject, op?: Op): Prim {
  if (s.type === "q" || s.type === "question") {
    return readQuestionSubject(wave, s, op);
  }
  return readResultField(wave, s) as Prim;
}

function compare(op: Op, a: Prim, b: Prim): boolean {
  switch (op) {
    case "eq":
      return a === b;
    case "ne":
      return a !== b;
    default: {
      const x = Number(a);
      const y = Number(b);
      switch (op) {
        case "gt":
          return x > y;
        case "gte":
          return x >= y;
        case "lt":
          return x < y;
        case "lte":
          return x <= y;
      }
    }
  }
}

function allOf(...preds: Predicate[]): Predicate {
  return make({ t: "and", of: preds });
}
function anyOf(...preds: Predicate[]): Predicate {
  return make({ t: "or", of: preds });
}
function truthy(subject: Subject): Predicate {
  return make({ t: "truthy", subject });
}
function betweenPred(subject: Subject, enter: number, exit: number): Predicate {
  return make({ t: "between", subject, enter, exit }, { held: false });
}
function opPred(op: Op, subject: Subject, value: Prim): Predicate {
  return make({ t: "op", op, subject, value });
}

/**
 * Predicate constructors:
 *   Predicate(subject)                — truthy
 *   Predicate(op, subject, value)     — comparison
 *   Predicate.allOf(p1, p2, …)        — conjunction
 *   Predicate.anyOf(p1, p2, …)        — disjunction
 *   Predicate.between(subject, enter, exit) — stateful hysteresis band
 */
export const Predicate = Object.assign(
  (a: Subject | Op, b?: Subject, c?: Prim): Predicate => {
    if (b !== undefined) return opPred(a as Op, b, c as Prim);
    return truthy(a as Subject);
  },
  { allOf, anyOf, between: betweenPred },
);

/** Comparison builders bound to one subject (`q.p.gte(0.8)`, `q.top.eq("x")`). */
export interface Ops {
  eq(value: Prim): Predicate;
  ne(value: Prim): Predicate;
  gt(value: number): Predicate;
  gte(value: number): Predicate;
  lt(value: number): Predicate;
  lte(value: number): Predicate;
  between(enter: number, exit: number): Predicate;
  truthy(): Predicate;
}

/** Builds an Ops bound to `subject`; on p-subjects the result is also callable
 * with an option name (`intent.p("support").gte(0.7)`). */
export function opsFor(subject: Subject): Ops & ((option?: string) => Ops) {
  const call = ((option?: string): Ops => {
    if (subject.type !== "p") {
      throw new TypeError("p(option) is only valid on a p accessor");
    }
    return opsFor({ ...subject, option });
  }) as Ops & ((option?: string) => Ops);
  return Object.assign(call, {
    eq: (value: Prim) => opPred("eq", subject, value),
    ne: (value: Prim) => opPred("ne", subject, value),
    gt: (value: number) => opPred("gt", subject, value),
    gte: (value: number) => opPred("gte", subject, value),
    lt: (value: number) => opPred("lt", subject, value),
    lte: (value: number) => opPred("lte", subject, value),
    between: (enter: number, exit: number) => betweenPred(subject, enter, exit),
    truthy: () => truthy(subject),
  });
}

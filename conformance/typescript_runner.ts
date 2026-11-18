#!/usr/bin/env bun
/**
 * TypeScript conformance runner: replays recorded fixtures through `@urun-sh/sense`.
 * Mirrors conformance/python_runner.py coverage against the SAME fixtures —
 * decode all 8 waves, all predicate_cases per-wave expectations, when_cases
 * edge + hysteresis firing, coalescing, onWave, the incomplete-wave protocol
 * error, unknown-qid error, and session defaults. Offline and deterministic.
 */
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  Context,
  Predicate,
  ProtocolError,
  Question,
  Score,
  instructionsEnvelope,
  Session,
  Wave,
  sessionEvent,
  type Op,
  type Subject,
} from "../typescript/src/index.ts";

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), "fixtures");

// -- fixture shapes (the fixtures are the validated boundary) ---------------

interface QuestionJson {
  type: string;
  instructions?: string;
  criteria?: Record<string, string> | string[];
}
interface SubjectJson {
  type: string;
  id?: string;
  question?: string;
  option?: string;
  level?: string;
}
interface AstJson {
  and?: { of: AstJson[] };
  or?: { of: AstJson[] };
  not?: { of: AstJson };
  truthy?: { subject: SubjectJson };
  between?: { subject: SubjectJson; enter: number; exit: number };
  op?: string;
  subject?: SubjectJson;
  value?: string | number | boolean;
}
interface CaseJson {
  name: string;
  predicate: AstJson;
  expected: boolean[];
}
interface CasesJson {
  predicate_cases: CaseJson[];
  when_cases: { name: string; predicate: AstJson; fires: number[]; per_wave?: boolean[] }[];
}
interface IncompleteJson {
  waves: { wave_id: number; t_ms: number; results: Record<string, unknown> }[];
  error_on_wave_id: number;
}
interface WavePayload {
  wave_id: number;
  t_ms: number;
  results: Record<string, unknown>;
}

function parseJson<T>(name: string, check: (v: unknown) => v is T): T {
  const v: unknown = JSON.parse(readFileSync(join(FIXTURES, name), "utf8"));
  if (!check(v)) throw new Error(`fixture ${name} has an unexpected shape`);
  return v;
}

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

function isQuestionsJson(v: unknown): v is { questions: Record<string, QuestionJson> } {
  return isRecord(v) && isRecord(v.questions) && Object.values(v.questions).every(isRecord);
}

function isCasesJson(v: unknown): v is CasesJson {
  return isRecord(v) && Array.isArray(v.predicate_cases) && Array.isArray(v.when_cases);
}

function isIncompleteJson(v: unknown): v is IncompleteJson {
  return isRecord(v) && Array.isArray(v.waves);
}

const readFixtureLines = (name: string): string[] =>
  readFileSync(join(FIXTURES, name), "utf8")
    .split("\n")
    .filter((l) => l.trim());

// -- builders ----------------------------------------------------------------

function qFromJson(d: QuestionJson & { id: string }): Question {
  return new Question({
    name: d.id,
    kind: d.type as "noul" | "choice" | "score",
    instructions: d.instructions,
    criteria: d.criteria,
  });
}

function subjectFromJson(d: SubjectJson): Subject {
  switch (d.type) {
    case "question":
      return { type: "q", id: d.id as string };
    case "p":
      return { type: "p", question: d.question as string, option: d.option };
    case "top":
      return { type: "top", question: d.question as string };
    case "value":
      return { type: "value", question: d.question as string };
    case "p_at_least":
      return { type: "p_at_least", question: d.question as string, level: d.level as string };
    default:
      throw new Error(`unknown subject ${JSON.stringify(d)}`);
  }
}

function astFromJson(d: AstJson): Predicate {
  if (d.and) return Predicate.allOf(...d.and.of.map(astFromJson));
  if (d.or) return Predicate.anyOf(...d.or.of.map(astFromJson));
  if (d.not) return astFromJson(d.not.of).not();
  if (d.truthy) return Predicate(subjectFromJson(d.truthy.subject));
  if (d.between) return Predicate.between(subjectFromJson(d.between.subject), d.between.enter, d.between.exit);
  return Predicate(d.op as Op, subjectFromJson(d.subject as SubjectJson), d.value as string | number | boolean);
}

function loadQuestions(): Question[] {
  // Fixture values carry no name — the map key IS the name.
  return Object.entries(parseJson("questions.json", isQuestionsJson).questions).map(([id, d]) =>
    qFromJson({ ...d, id }),
  );
}

function loadWavePayloads(): WavePayload[] {
  return readFixtureLines("waves.jsonl").map((l) => JSON.parse(l) as WavePayload);
}

function loadWaves(questions: Question[]): Wave[] {
  return loadWavePayloads().map((p) => Wave.decode(questions, p));
}

// -- tests -------------------------------------------------------------------

describe("session event envelope", () => {
  test("event roundtrip", () => {
    for (const raw of ["session.created", "session.ready", "session.closed"]) {
      expect(sessionEvent({ type: raw }).type).toBe(raw);
    }
    const ev = sessionEvent({ type: "error", code: "bad_question", message: "no" });
    expect(ev.type).toBe("error");
    expect(ev.code).toBe("bad_question");
  });
});

describe("wave decoding", () => {
  const waves = loadWaves(loadQuestions());

  test("eight waves with monotonic t", () => {
    expect(waves.length).toBe(8);
    const ts = waves.map((w) => w.tMs);
    expect(ts).toEqual([...ts].sort((a, b) => a - b));
    expect(waves.map((w) => w.waveId)).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
  });

  test("kind shapes", () => {
    for (const w of waves) {
      const f = w.result("finished");
      expect(typeof f.value).toBe("boolean");
      expect(typeof f.p).toBe("number");
      expect(f.confidence).toBeGreaterThanOrEqual(0);
      const i = w.result("intent");
      expect(["support", "sales", "other"]).toContain(i.value as string);
      const u = w.result("urgency") as Score;
      expect(["routine, can wait", "time sensitive today", "blocking, act now"]).toContain(u.top);
      expect(u.pAtLeast(0)).toBeCloseTo(1.0);
      expect(u.pAtLeast("routine, can wait")).toBeCloseTo(1.0);
    }
  });

  test("noul bare value is bool", () => {
    for (const qid of ["finished", "user_speaking"]) {
      expect(typeof waves[0]!.result(qid).value).toBe("boolean");
    }
  });
});

describe("predicates", () => {
  const cases = parseJson("cases.json", isCasesJson);
  const waves = loadWaves(loadQuestions());

  test("per-wave expectations", () => {
    for (const c of cases.predicate_cases) {
      const pred = astFromJson(c.predicate);
      expect(waves.map((w) => pred(w))).toEqual(c.expected);
    }
  });

  test("predicate subjects cover the spec", () => {
    const text = readFileSync(join(FIXTURES, "cases.json"), "utf8");
    for (const name of ["question", "p", "top", "value", "p_at_least"]) {
      expect(text).toContain(`"type": "${name}"`);
    }
  });
});

describe("context", () => {
  const questions = loadQuestions();
  const payloads = loadWavePayloads();
  const feed = (ctx: Context): void => {
    for (const p of payloads) ctx.ingest(Wave.decode(questions, p));
  };

  test("latest and waves", () => {
    const ctx = new Context();
    feed(ctx);
    expect(ctx.latest()!.waveId).toBe(7);
    expect(ctx.waves().length).toBe(8);
    // w2/w3 agree on every argmax, so the coalesced run keeps only w3.
    expect(ctx.waves(true).map((w) => w.waveId)).toEqual([0, 1, 3, 4, 5, 6, 7]);
    expect(ctx.latest()!.result("intent").value).toBe("support");
  });

  test("when fires on edges", () => {
    const ctx = new Context();
    const fired: number[] = [];
    ctx.when(Predicate({ type: "q", id: "finished" } satisfies Subject), (c) => fired.push(c.latest()!.waveId));
    feed(ctx);
    expect(fired).toEqual([2, 5, 7]);
  });

  test("when hysteresis", () => {
    const ctx = new Context();
    const fired: number[] = [];
    const pred = Predicate.between({ type: "p", question: "finished" } satisfies Subject, 0.8, 0.6);
    ctx.when(pred, (c) => fired.push(c.latest()!.waveId));
    feed(ctx);
    expect(fired).toEqual([2, 5, 7]);
    // Same per-wave values, replayed with one fresh between() instance —
    // between is stateful, so the latch carries across waves.
    const fresh = Predicate.between({ type: "p", question: "finished" } satisfies Subject, 0.8, 0.6);
    const perWave = loadWavePayloads().map((p) => fresh(Wave.decode(questions, p)));
    expect(perWave).toEqual([false, false, true, true, false, true, false, true]);
  });

  test("when cancellable", () => {
    const ctx = new Context();
    const fired: number[] = [];
    const handle = ctx.when(Predicate({ type: "q", id: "finished" } satisfies Subject), (c) => fired.push(c.latest()!.waveId));
    handle.cancel();
    feed(ctx);
    expect(fired).toEqual([]);
  });

  test("onWave", () => {
    const ctx = new Context();
    const seen: number[] = [];
    ctx.onWave((c) => seen.push(c.latest()!.waveId));
    feed(ctx);
    expect(seen).toEqual(ctx.waves().map((w) => w.waveId));
  });
});

describe("protocol errors", () => {
  test("missing answer raises", () => {
    const incomplete = parseJson("incomplete.json", isIncompleteJson);
    expect(() => {
      for (const w of incomplete.waves) Wave.decode(loadQuestions(), w);
    }).toThrow(ProtocolError);
  });

  test("unknown question raises", () => {
    const incomplete = parseJson("incomplete.json", isIncompleteJson);
    const w = structuredClone(incomplete.waves[0]!);
    w.results["bogus"] = { kind: "noul", choice: true };
    expect(() => Wave.decode(loadQuestions(), w)).toThrow(ProtocolError);
  });
});

describe("session", () => {
  test("session defaults", () => {
    const s = new Session({ apiKey: "sk-test" });
    expect(s.questions).toEqual([]);
    expect(s.closed).toBe(false);
    expect(s.baseUrl).toBe(Session.DEFAULT_BASE_URL);
  });

  test("question registration shape (0.2.0: name-first, no addQuestion)", () => {
    const s = new Session({ apiKey: "sk-test" });
    const q = new Question({ name: "finished", kind: "noul", instructions: "did they finish?" });
    s.setQuestions([q]);
    expect(q.name).toBe("finished");
    expect(s.questions).toEqual([q]);
  });

  test("instructions envelope wire shape (Jev keys only)", () => {
    const questions = loadQuestions();
    const env = instructionsEnvelope(questions, 12.5);
    for (const entry of Object.values(env.questions)) {
      expect(Object.keys(entry).every((k) => ["type", "instructions", "criteria"].includes(k))).toBe(true);
    }
    expect(Object.keys(env.questions).sort()).toEqual(["actions", "finished", "intent", "urgency", "user_speaking"]);
    expect(env.questions["finished"]).toEqual({ type: "noul", instructions: "Did the current speaker just finish their turn?" });
    expect(env.hz).toBe(12.5);
    const frame = { type: "input.audio", audio: "UklGRg==", sample_rate: 16000, source: "mic" };
    expect(Object.keys(frame).sort()).toEqual(["audio", "sample_rate", "source", "type"]);
  });
});

// when_cases from the fixture (same edge + hysteresis contract, driven by data).
describe("fixture when_cases", () => {
  const questions = loadQuestions();
  const payloads = loadWavePayloads();
  const cases = parseJson("cases.json", isCasesJson);

  test("edge + hysteresis firing", () => {
    for (const c of cases.when_cases) {
      const ctx = new Context();
      const fired: number[] = [];
      ctx.when(astFromJson(c.predicate), (x) => fired.push(x.latest()!.waveId));
      for (const p of payloads) ctx.ingest(Wave.decode(questions, p));
      expect(fired).toEqual(c.fires);
      if (c.per_wave) {
        // One fresh instance across all waves: between() latches across waves.
        const pred = astFromJson(c.predicate);
        expect(payloads.map((p) => pred(Wave.decode(questions, p)))).toEqual(c.per_wave);
      }
    }
  });
});

import { describe, expect, test } from "bun:test";
import { Question, Sense, instructionsEnvelope } from "../src/index.ts";
import type { NoulCriteria } from "../src/index.ts";

const app = new Sense({ apiKey: "test-key" });

describe("Jev criteria (0.2.0)", () => {
  test("choice: name first, required name -> description criteria map", () => {
    const q = app.choice("route", {
      instructions: "Which team should handle this?",
      criteria: {
        returns: "Exchanges, wrong or damaged items",
        billing: "Payment questions",
        other: "Anything else",
      },
    });
    expect(q.name).toBe("route");
    expect(q.options).toEqual(["returns", "billing", "other"]);
    const env = instructionsEnvelope([q], 25);
    expect(env.questions["route"]).toEqual({
      type: "choice",
      instructions: "Which team should handle this?",
      criteria: {
        returns: "Exchanges, wrong or damaged items",
        billing: "Payment questions",
        other: "Anything else",
      },
    });
  });

  test("score: ordered array of level descriptions, low end first", () => {
    const q = app.score("urgency", {
      instructions: "How urgent is this?",
      criteria: ["low — routine", "medium — time sensitive today", "high — blocking right now"],
    });
    expect(q.name).toBe("urgency");
    expect(q.levels).toEqual(["low — routine", "medium — time sensitive today", "high — blocking right now"]);
    const env = instructionsEnvelope([q], 25);
    expect(env.questions["urgency"]).toEqual({
      type: "score",
      instructions: "How urgent is this?",
      criteria: ["low — routine", "medium — time sensitive today", "high — blocking right now"],
    });
  });

  test("noul: optional {true, false} criteria; omitted from the envelope when absent", () => {
    const bare = app.noul("done", { instructions: "Has the person finished their thought?" });
    expect(bare.descriptions).toBeUndefined();
    expect(instructionsEnvelope([bare], 25).questions["done"]).toEqual({
      type: "noul",
      instructions: "Has the person finished their thought?",
    });
    const described = app.noul("done", {
      instructions: "Has the person finished their thought?",
      criteria: { true: "the thought is complete", false: "still in the middle of a thought" },
    });
    expect(described.options).toEqual(["true", "false"]);
    expect(instructionsEnvelope([described], 25).questions["done"]).toEqual({
      type: "noul",
      instructions: "Has the person finished their thought?",
      criteria: { true: "the thought is complete", false: "still in the middle of a thought" },
    });
  });

  test("only Jev keys on the wire — no kind/prompt/options/levels/id", () => {
    const q = app.choice("route", { instructions: "Which team?", criteria: { a: "alpha" } });
    const entry = instructionsEnvelope([q], 25).questions["route"]!;
    expect(Object.keys(entry).sort()).toEqual(["criteria", "instructions", "type"]);
  });

  test("validation: names, instructions and criteria shapes", () => {
    expect(() => app.choice("  ", { instructions: "x", criteria: { a: "b" } })).toThrow("non-empty");
    expect(() => app.noul("done", { instructions: "  " })).toThrow("instructions");
    expect(() => app.choice("route", { instructions: "x", criteria: {} })).toThrow("criteria map");
    expect(() => app.choice("route", { instructions: "x", criteria: { a: "  " } })).toThrow("non-empty description");
    expect(() => app.score("u", { instructions: "x", criteria: ["only"] })).toThrow(RangeError);
    expect(() => app.score("u", { instructions: "x", criteria: ["a", ""] })).toThrow(RangeError);
    const wrongKeys = { yes: "a", no: "b" } as unknown as NoulCriteria;
    expect(() => app.noul("done", { instructions: "x", criteria: wrongKeys })).toThrow("{true, false}");
    expect(() => new Question({ name: "", kind: "noul" })).toThrow("name must be a non-empty string");
  });

  test("score map still enforces the 2..10 level range", () => {
    expect(() => new Question({ name: "u", kind: "score", instructions: "x", criteria: [] })).toThrow(RangeError);
    expect(() => new Question({ name: "u", kind: "score", instructions: "x", criteria: new Array(11).fill("x") })).toThrow(RangeError);
  });
});

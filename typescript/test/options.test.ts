import { describe, expect, test } from "bun:test";
import { Question, Sense, instructionsEnvelope } from "../src/index.ts";

const app = new Sense({ apiKey: "test-key" });

describe("Jev-style option description maps", () => {
  test("choice accepts a name -> description map (insertion order)", () => {
    const q = app.choice("Which team should handle this?", {
      returns: "Exchanges, wrong or damaged items",
      billing: "Payment questions",
      other: "Anything else",
    });
    expect(q.options).toEqual(["returns", "billing", "other"]);
    const env = instructionsEnvelope([q], 25);
    expect(env.questions["which_team_should_handle_this"]).toEqual({
      kind: "choice",
      prompt: "Which team should handle this?",
      criteria: {
        returns: "Exchanges, wrong or damaged items",
        billing: "Payment questions",
        other: "Anything else",
      },
    });
  });

  test("score accepts a name -> description map; insertion order is ordinal", () => {
    const q = app.score("How calm is the speaker?", {
      agitated: "Clearly worked up",
      neutral: "Neither calm nor worked up",
      calm: "Relaxed and settled",
    });
    expect(q.levels).toEqual(["agitated", "neutral", "calm"]);
    const env = instructionsEnvelope([q], 25);
    expect(env.questions["how_calm_is_the_speaker"]).toEqual({
      kind: "score",
      prompt: "How calm is the speaker?",
      criteria: {
        agitated: "Clearly worked up",
        neutral: "Neither calm nor worked up",
        calm: "Relaxed and settled",
      },
    });
  });

  test("noul accepts an options map (optional)", () => {
    const q = app.noul("Is the user done?", {
      id: "done",
      options: { yes: "They finished their thought", no: "More to come" },
    });
    expect(q.options).toEqual(["yes", "no"]);
    const env = instructionsEnvelope([q], 25);
    expect(env.questions["done"]).toEqual({
      kind: "noul",
      prompt: "Is the user done?",
      // canonicalized to no-first (protocol §2), same as the python facade
      options: [["no", "More to come"], ["yes", "They finished their thought"]],
    });
  });

  test("string[] overloads are unchanged", () => {
    const choice = new Question({ id: "c", kind: "choice", options: ["a", "b"] });
    const score = new Question({ id: "s", kind: "score", levels: ["low", "high"] });
    const env = instructionsEnvelope([choice, score], 25);
    expect(env.questions["c"]).toEqual({ kind: "choice", prompt: "", options: ["a", "b"] });
    expect(env.questions["s"]).toEqual({ kind: "score", prompt: "", criteria: ["low", "high"] });
  });

  test("score map still enforces the 2..10 level range", () => {
    expect(() => app.score("Too few?", { one: "only" })).toThrow(RangeError);
  });
});

/**
 * Type-level consumer check: compiled against the INSTALLED @urun-sh/sense
 * declaration files (not the repo source) from the clean consumer directory.
 * Exercises the facade (name-first Jev registration), predicate composition,
 * the session surface, and Score narrowing — the API a customer touches first.
 */
import { Context, Predicate, Score, Sense, Wave } from "@urun-sh/sense";

const app = new Sense({ apiKey: "k" });
const q = app.noul("finished", {
  instructions: "Did the person just finish their turn?",
  criteria: { true: "the thought is complete", false: "still in the middle of a thought" },
});
const route = app.choice("route", {
  instructions: "Which team should handle this?",
  criteria: { billing: "Charges, invoices, refunds", technical: "Bugs, errors, setup" },
});
const urgency = app.score("urgency", {
  instructions: "How urgent is this?",
  criteria: ["low — routine", "high — blocking right now"],
});
const p = q.and(Predicate({ type: "q", id: "finished" }));
const byIndex = urgency.pAtLeast(1);
const byDescription = urgency.pAtLeast("high — blocking right now");
const s = app.session();
const r = s.context.get("finished");
const n: number | undefined = r instanceof Score ? r.value : undefined;
const top: string = route.top.eq("billing") ? "billing" : "other";
const c: Context | null = null;
void [p, n, Wave, byIndex, byDescription, top, c];

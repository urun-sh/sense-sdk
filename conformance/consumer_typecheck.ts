/**
 * Type-level consumer check: compiled against the INSTALLED @urun-sh/sense
 * declaration files (not the repo source) from the clean consumer directory.
 * Exercises the facade (text-first registration), predicate composition, the
 * session surface, and Score narrowing — the API a customer touches first.
 */
import { Context, Predicate, Score, Sense, Wave } from "@urun-sh/sense";

const app = new Sense({ apiKey: "k" });
const q = app.noul("Did the speaker just finish their turn?", { id: "finished" });
const p = q.and(Predicate({ type: "q", id: "finished" }));
const s = app.session();
const r = s.latest("finished");
const n: number | undefined = r instanceof Score ? r.value : undefined;
const c: Context | null = null;
void [p, n, Wave, c];

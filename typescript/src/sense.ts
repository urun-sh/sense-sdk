/** Sense: the app facade — register questions, compose predicates, open sessions. */
import { Question } from "./question.ts";
import type { QuestionKind } from "./question.ts";
import type { Predicate } from "./predicate.ts";
import type { WaveHandler } from "./context.ts";
import { Session, envVar, type ApiKeyProvider, type SessionOptions } from "./session.ts";
import { checkBaseUrl, type Transport } from "./transport.ts";

export interface SenseOptions extends SessionOptions {}

/** Mirror of python `_slug`: lowercase, each non-alphanumeric char becomes
 * `_`, leading/trailing `_` trimmed, never empty, unique within the app. */
function slug(text: string, taken: Iterable<string>): string {
  let base = "";
  for (const c of text.trim().toLowerCase()) base += /\p{L}|\p{N}/u.test(c) ? c : "_";
  base = base.replace(/^_+/, "").replace(/_+$/, "") || "question";
  const takenSet = new Set(taken);
  let qid = base;
  let n = 1;
  while (takenSet.has(qid)) {
    n += 1;
    qid = `${base}_${n}`;
  }
  return qid;
}

export class Sense {
  readonly apiKey?: ApiKeyProvider;
  readonly baseUrl?: string;
  readonly model: string;
  readonly hz: number;
  readonly transport: Transport;
  readonly allowInsecure: boolean;
  private questions: Question[] = [];
  private whens: { pred: Predicate; handler: WaveHandler }[] = [];
  private waveHandlers: { handler: WaveHandler; opts: { coalesce?: boolean } }[] = [];

  constructor(opts: SenseOptions = {}) {
    this.apiKey = opts.apiKey ?? envVar("URUN_API_KEY");
    this.baseUrl = opts.baseUrl ?? envVar("SENSE_BASE_URL");
    this.model = opts.model ?? Session.DEFAULT_MODEL;
    this.hz = opts.hz ?? Session.DEFAULT_HZ;
    this.transport = opts.transport ?? "auto";
    this.allowInsecure = opts.allowInsecure ?? false;
    // Fail at construction, not at the first session(): an app should learn
    // about an unsafe SENSE_BASE_URL at startup.
    if (this.baseUrl !== undefined) checkBaseUrl(this.baseUrl, this.allowInsecure);
  }

  /** Register a binary question. TEXT-FIRST like the python facade: the
   * natural-language question is the first argument; the id is optional and
   * auto-slugged from the question text (options optional; the server
   * defaults to true/false). */
  noul(question: string, opts: { id?: string; options?: string[] } = {}): Question {
    return this.register({ id: this.qid(question, opts.id), kind: "noul", prompt: question, options: opts.options });
  }

  /** Register an n-way choice question (text-first; id auto-slugs). */
  choice(question: string, options: string[], opts: { id?: string } = {}): Question {
    return this.register({ id: this.qid(question, opts.id), kind: "choice", prompt: question, options });
  }

  /** Register an ordinal score question, 2..10 levels (text-first; id auto-slugs). */
  score(question: string, levels: string[], opts: { id?: string } = {}): Question {
    return this.register({ id: this.qid(question, opts.id), kind: "score", prompt: question, levels });
  }

  /** Mirror of python `_qid`: the question text is required and non-empty;
   * the id is auto-slugged from it when omitted and must be non-empty when
   * given explicitly. */
  private qid(question: string, id: string | undefined): string {
    if (typeof question !== "string" || question.trim() === "") {
      throw new Error("question text must be a non-empty string (text-first API)");
    }
    if (id === undefined) return slug(question, this.questions.map((q) => q.id));
    if (id.trim() === "") throw new Error("explicit id must be non-empty");
    return id;
  }

  private register(init: { id: string; kind: QuestionKind; prompt?: string; options?: string[]; levels?: string[] }): Question {
    const q = new Question(init);
    const i = this.questions.findIndex((x) => x.id === q.id);
    if (i >= 0) this.questions[i] = q;
    else this.questions.push(q);
    return q;
  }

  when(pred: Predicate, handler: WaveHandler): { cancel(): void } {
    const handle = { pred, handler };
    this.whens.push(handle);
    return {
      cancel: () => {
        this.whens = this.whens.filter((h) => h !== handle);
      },
    };
  }

  /** Observe waves on every session opened from here; `coalesce` skips
   * consecutive same-argmax waves (latest wins). */
  onWave(handler: WaveHandler, opts: { coalesce?: boolean } = {}): { cancel(): void } {
    const entry = { handler, opts };
    this.waveHandlers.push(entry);
    return {
      cancel: () => {
        this.waveHandlers = this.waveHandlers.filter((h) => h !== entry);
      },
    };
  }

  /** Open a session carrying the questions and handlers registered here. */
  session(): Session {
    const s = new Session({
      apiKey: this.apiKey,
      baseUrl: this.baseUrl,
      model: this.model,
      hz: this.hz,
      transport: this.transport,
      allowInsecure: this.allowInsecure,
      questions: this.questions,
    });
    for (const { pred, handler } of this.whens) s.when(pred, handler);
    for (const { handler, opts } of this.waveHandlers) s.onWave(handler, opts);
    return s;
  }
}

export type { QuestionKind };

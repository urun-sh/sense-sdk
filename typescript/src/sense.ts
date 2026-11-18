/** Sense: the app facade — register questions, compose predicates, open sessions. */
import { Question } from "./question.ts";
import type { ChoiceCriteria, NoulCriteria, QuestionInit, ScoreCriteria } from "./question.ts";
import type { Predicate } from "./predicate.ts";
import type { WaveHandler } from "./context.ts";
import { Session, envVar, type ApiKeyProvider, type SessionOptions } from "./session.ts";
import { checkBaseUrl, type Transport } from "./transport.ts";

export interface SenseOptions extends SessionOptions {}

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

  /** Register a binary question. The NAME is the first positional argument —
   * it is the answer key on every wave. Criteria (the {true, false}
   * description pair) is optional; the server defaults to true/false. */
  noul(name: string, opts: { instructions: string; criteria?: NoulCriteria }): Question {
    return this.register({ name, kind: "noul", instructions: opts.instructions, criteria: opts.criteria });
  }

  /** Register an n-way choice question. Criteria is the required
   * name -> description map (Jev-style; insertion order is the wire order). */
  choice(name: string, opts: { instructions: string; criteria: ChoiceCriteria }): Question {
    return this.register({ name, kind: "choice", instructions: opts.instructions, criteria: opts.criteria });
  }

  /** Register an ordinal score question, 2..10 levels. Criteria is the
   * ordered array of level descriptions, low end first. */
  score(name: string, opts: { instructions: string; criteria: ScoreCriteria }): Question {
    return this.register({ name, kind: "score", instructions: opts.instructions, criteria: opts.criteria });
  }

  private register(init: QuestionInit): Question {
    const q = new Question(init);
    const i = this.questions.findIndex((x) => x.name === q.name);
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

export type { QuestionKind } from "./question.ts";

/** Error classes for the Sense wire protocol (docs/protocol.md §7). */

export type ProtocolErrorCode =
  | "bad_question"
  | "missing_answer"
  | "unknown_question"
  | "buffer_full"
  | "empty_buffer"
  | "frame_too_large"
  | "session_gone"
  | "protocol";

/** @internal Message of an unknown thrown value, for logs. */
export function errMessage(err: unknown): string {
  // Everything inside the try: a hostile value can throw from `instanceof`
  // (a Proxy), a `message` getter, or primitive conversion.
  try {
    return err instanceof Error ? String(err.message) : String(err);
  } catch {
    return "[unprintable error]"; // e.g. Object.create(null) has no toString
  }
}

export class ProtocolError extends Error {
  readonly code: ProtocolErrorCode;

  constructor(code: ProtocolErrorCode, message: string) {
    super(message);
    this.name = "ProtocolError";
    this.code = code;
  }
}

/** Terminal: the organization is out of credits (wire code
 * 'insufficient_credits'; the DB vocabulary is 'credits_exhausted'). Never
 * retried, never re-dialed — the session ends and the app must show a
 * top-up path before connecting again. */
export class OutOfCreditsError extends Error {
  readonly code = "insufficient_credits";

  constructor(message = "the organization is out of credits — top up to continue") {
    super(message);
    this.name = "OutOfCreditsError";
  }
}

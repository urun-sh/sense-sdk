/** @urun-sh/sense — public surface. */
export { Sense, type SenseOptions } from "./sense.ts";
export { Session, sessionEvent, AudioInput, VideoInput, TextInput } from "./session.ts";
export type { SessionOptions, SessionEvent, InputFrame, TextMode, ApiKeyProvider } from "./session.ts";
export { connectWire, transportOrder, TRANSPORT_DIAL_TIMEOUT_MS } from "./transport.ts";
export type { Transport, Wire, WireOptions, WireDialer } from "./transport.ts";
export { Question, instructionsEnvelope } from "./question.ts";
export type { QuestionKind, QuestionInit } from "./question.ts";
export { Predicate, opsFor } from "./predicate.ts";
export type { Op, Ops, Subject } from "./predicate.ts";
export { Wave, Result, Score } from "./wave.ts";
export { Context } from "./context.ts";
export type { WaveHandler } from "./context.ts";
export { ProtocolError, OutOfCreditsError } from "./errors.ts";
export type { ProtocolErrorCode } from "./errors.ts";

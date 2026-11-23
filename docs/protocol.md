# Sense Wire Protocol

This reference describes the messages exchanged by `urun-sense` (Python) and `@urun-sh/sense` (TypeScript). Sense uses a subset of the OpenAI Realtime event format, with question registration in `instructions` and decision events carried in text deltas. It is not a claim of compatibility with every OpenAI Realtime feature.

The examples below describe the SDK's wire format. SDK-specific limitations are called out explicitly; server implementation details and future features are outside this reference. For executable examples of supported behavior, see the [shared conformance fixtures](../conformance/fixtures/) and the [Python](../conformance/python_runner.py) and [TypeScript](../conformance/typescript_runner.ts) runners.

## 1. Endpoint and transport

The default WebSocket endpoint is:

```text
wss://inference.urun.sh/v1/realtime?model=sense-latest:bf16
```

- `model` selects the model. Both SDKs default to `sense-latest:bf16`.
- Authenticate the WebSocket handshake with `Authorization: Bearer <key>`. Supply credentials through the SDK configuration or `URUN_API_KEY`; do not embed keys in source code or URLs.
- Configure a different service base with `base_url` in Python, `baseUrl` in TypeScript, or `SENSE_BASE_URL`. Both SDKs require `https://` or `wss://`, because the key is sent on every dial; plaintext `http://`/`ws://` is accepted only for loopback hosts (`localhost`, `127.0.0.0/8`, `::1`) unless you opt in with `allow_insecure=True` (Python) or `allowInsecure: true` (TypeScript). URLs with embedded credentials are refused.
- Both SDKs refuse to dial without an API key, and authenticated `POST /v1/realtime/calls` requests never follow redirects.
- WebSocket authentication failures occur before the connection upgrades. Retry behavior is described in §7.

Both SDKs support `auto`, `quic`, `webrtc`, and `ws` transport selection. In `auto` mode they try QUIC, then WebRTC, then WebSocket, skipping unavailable client stacks and falling back after a failed connection attempt. An explicitly selected transport does not fall back.

All transports carry the same JSON application events, including media:

| Transport | Connection setup | Event channel |
|---|---|---|
| WebSocket | Authenticated handshake at `/v1/realtime?model=…` | JSON WebSocket messages; Python uses `aiohttp`, Node/Bun uses `ws` |
| QUIC / WebTransport | Authenticated `POST /v1/realtime/calls?model=…&transport=quic`; a successful response supplies `callId`, `url`, `token`, and certificate hashes | WebTransport bidirectional streams: a MessagePack control stream and a newline-delimited JSON event stream |
| WebRTC | Authenticated SDP offer posted to `/v1/realtime/calls?model=…` | JSON messages on the `oai-events` data channel |

The QUIC control stream uses a four-byte little-endian body length followed by MessagePack. The client sends `hello` with `token` and `sessionId`, waits for `hello-ack`, and answers `ping` with `pong`. Control frames are limited to 256 KiB. Python verifies the peer certificate against the hashes returned by the authenticated setup request before sending application data; TypeScript passes the supplied hashes to WebTransport.

Python's QUIC and WebRTC dependencies ship with the base install (`pip install urun-sense`); the auto transport order (quic → webrtc → ws) needs no extras. TypeScript detects WebTransport and RTCPeerConnection availability at runtime: Node dials QUIC through the `@fails-components/webtransport` ponyfill when the global is missing, which additionally needs its `@fails-components/webtransport-transport-http3-quiche` binding package (optional install — it carries a compile lifecycle script, so the SDK ships it as no dependency and degrades to WebSocket without it); Node WebRTC stays on WebSocket (no prebuilt W3C-shaped stack interops with the SFU). Its WebSocket implementation requires header authentication and does not support browser WebSocket fallback. These transport options do not imply that every runtime supports every SDK entry point.

### 1.1 Keeping a quiet connection alive

An open realtime connection must not go silent. The service closes a connection that has nothing inbound for about 45 seconds: the edge sends a WebSocket protocol ping every 15 seconds and drops a peer that answers nothing for 30 seconds after one. Any inbound frame resets that clock — the automatic pong, a client-sent WebSocket ping, or any text/binary message — and an application event is the frame every layer counts, including the member leg that serves the session.

If you talk to the service without an SDK, follow these rules:

- **Send audio or any JSON event at least every 20 seconds.** Any valid event works. From SDK version **0.2.9** on, both SDKs send `{"type": "session.presence"}` every 10 seconds for the whole life of the connection, so an SDK session needs nothing from the application. Earlier versions stop sending it once the app has been idle for 60 seconds — on those, send audio or a JSON event yourself at least every 20 seconds, or upgrade. A connection with nothing inbound for about 45 seconds may be closed.
- **Let your WebSocket stack answer protocol pings.** Browsers and the common client libraries (`ws`, `aiohttp`, `websockets`, OkHttp, URLSession) pong automatically. A client that disables automatic pong, or whose read/event loop is blocked for more than 30 seconds, is dropped even though its socket looks open.
- **Reconnect after a suspend.** A backgrounded tab, a phone app suspended by the OS, or a process paused by the debugger cannot pong and cannot send keepalives, so it will be closed; dial again rather than assuming the old socket is live.
- **WebTransport/QUIC needs none of this** beyond its own transport keepalive and the control stream's `ping`/`pong` (§1); the rules above cover the WebSocket (and OpenAI-Realtime-compatible) path.

## 2. Handshake and question registration

After connecting, the SDK sends `session.update`. `instructions` is a **JSON string**, not a nested object:

```json
{
  "type": "session.update",
  "session": {
    "model": "sense-latest:bf16",
    "modalities": ["text"],
    "instructions": "{\"questions\":{\"speech\":{\"type\":\"noul\",\"instructions\":\"Is someone speaking?\"}},\"hz\":12.5}"
  }
}
```

Decoded, the instructions envelope has this shape — each question is exactly Jev's (TypeSafe System One) shape, `type` + `instructions` + `criteria`, keyed by the question name:

```json
{
  "questions": {
    "speech": {"type": "noul", "instructions": "Is someone speaking?"},
    "intent": {
      "type": "choice",
      "instructions": "What does the person want?",
      "criteria": {
        "support": "Technical help",
        "sales": "Product information"
      }
    },
    "urgency": {
      "type": "score",
      "instructions": "How urgent is the request?",
      "criteria": ["routine, can wait", "time sensitive today", "blocking, act now"]
    }
  },
  "hz": 12.5
}
```

`hz` is the requested number of waves per second; the SDK default (and the maximum it accepts) is 12.5. The SDK resends the current question envelope in `response.create.response.instructions` when opening a turn. Changing a local question object does not update an already-running response.

Python's `session.set_questions(questions)` replaces its registry, barges in, and re-sends `session.update` so the next turn uses the new schema (call it from async code). TypeScript's `Session.setQuestions()` does the same.

### Question kinds

| Kind | Registration | Result |
|---|---|---|
| `noul` | `type`, `instructions`, and optional `criteria` (`{"true": …, "false": …}` descriptions) | Boolean decision with P(true) |
| `choice` | `type`, `instructions`, and `criteria`, a name → description map | One option and its probability distribution |
| `score` | `type`, `instructions`, and `criteria`, an ordered array of 2–10 level descriptions (low end first) | Distribution over level indices and an expected ordinal value |

Criteria carry a description for every entry, Jev-style:

- `choice` sends `criteria` as a name → description map, e.g. `{"returns": "Exchanges, wrong or damaged items", "billing": "Payment questions"}` — insertion order is the wire order. The option names are the decision labels.
- `score` sends `criteria` as the ordered array of level descriptions; the level indices correspond to the array order, and the argmax level's description is the decision label.
- `noul` may declare `criteria` as a `{"true": …, "false": …}` description map; without it the standard `true`/`false` representation applies.

Score criteria must be the ordered description array, not a prose string: the level indices must correspond to the registered levels.

Questions are unconditional within a wave. The SDK envelope does not send `when` or `depends_on`; SDK predicates and `when` handlers run client-side after decisions arrive.

### Session.update constraints

The SDK sends `model`, `modalities`, and `instructions`. Do not assume arbitrary OpenAI session fields or application metadata are forwarded to the model. TypeScript's `setQuestions()` replaces the registry, cancels the active response, and sends an updated registration for the next turn.

## 3. Turn lifecycle and cadence

For an audio-driven turn, the sequence is:

1. Send `input_audio_buffer.append` with buffered audio (§5.1).
2. Send `input_audio_buffer.commit` after at least one audio append.
3. Send `response.create` with the current instructions envelope and an output-token budget.
4. Continue sending audio appends while consuming decisions. Do not commit each audio chunk.
5. On a terminal response event, finish assembling the pending wave. The run loop can open another turn when inputs are available.

```json
{
  "type": "response.create",
  "response": {
    "instructions": "{\"questions\":{\"speech\":{\"type\":\"noul\",\"instructions\":\"Is someone speaking?\"}},\"hz\":12.5}",
    "max_output_tokens": 250000
  }
}
```

One `response.create` requests a continuing decision stream, not one wave. The server controls decision cadence; `hz` is a request, not a delivery-rate guarantee. Media inputs and wave consumption must both make progress.

Both SDKs recognize `response.completed`, `response.done`, and `response.failed` as turn endings. TypeScript also recognizes `response.cancelled`. `response.cancel` requests that the active response stop, for example during barge-in. Do not assume an immediate cancellation acknowledgement; TypeScript backs off reopening for two seconds when it receives `response_in_progress`.

Text-only and video-only turns do not need an audio commit. An empty audio commit can be rejected with `empty_buffer`; it is not a substitute for supplying the first audio chunk.

## 4. Decisions on the wire

Decision records are newline-delimited JSON carried in `response.output_text.delta` or `response.text.delta` events. A decision record looks like:

```json
{"event":"sense.decision","id":"speech","t_ms":1200,"choice":"true","probabilities":{"true":0.82,"false":0.18},"confidence":0.82}
```

- `id` identifies the registered question. Both SDKs also accept `question` as the identifier field.
- `t_ms` is the decision's media timestamp in milliseconds.
- `choice` is the selected label, normally the highest-probability option.
- `probabilities` maps labels to probabilities.
- `confidence` is the per-question confidence value; a null value is not a valid answer.

Per-kind representations:

- **Noul:** `choice` uses `"true"` or `"false"`, with corresponding probabilities. The SDK exposes a boolean `value` and `p = P(true)`, not confidence in whichever boolean was selected.
- **Choice:** `choice` is an option name; the probability map is keyed by option name.
- **Score:** the probability map uses string level indices such as `"0"`, `"1"`, and `"2"`. `score` can carry the expected ordinal. The SDK exposes the top level name, its probability, and the expected level index.

The Python session buffers partial decision lines across text deltas. The current TypeScript session parses lines within each delta and requires each decision JSON record to be complete within that delta. Transport chunking of QUIC's outer JSON event stream is handled separately by both transports.

An answer with `choice: null`, `confidence: null`, or an otherwise incomplete result must not be interpreted as a negative decision. See §6.2.

## 5. Inputs

### 5.1 Audio

Send base64-encoded PCM16 audio at 24 kHz, mono:

```json
{"type":"input_audio_buffer.append","audio":"<base64-encoded PCM16>"}
```

The examples use 40 ms chunks. Keep supplying audio while reading waves. The SDK does not resample arbitrary input audio for you; prepare the required format before pushing it.

Open an audio turn only after buffering audio, then commit and create the response (§3). Both SDKs respond to `buffer_full` by committing pending audio and ignore `empty_buffer` as nonfatal.

`audio_input_transcript_unavailable` means an audio transcript is unavailable, not that the decision model cannot consume audio. Both SDKs treat it as nonfatal.

Named input sources share the session's audio stream; do not assume they provide isolated model contexts or automatic echo separation. See §8.1 for SDK differences.

### 5.2 Video

Send JPEG frames as base64 data URIs in a user message:

```json
{
  "type": "conversation.item.create",
  "item": {
    "type": "message",
    "role": "user",
    "content": [{"type": "input_image", "image_url": "data:image/jpeg;base64,<jpeg>"}]
  }
}
```

Both SDKs reject frames larger than 256,000 bytes at push time. Resize or re-encode oversized frames before pushing them; the SDK does not automatically resize them. The input API takes JPEG bytes, not remote image URLs.

### 5.3 Text

Text is sent using `conversation.item.create` with a user message containing an `input_text` part:

```json
{"type":"conversation.item.create","item":{"type":"message","role":"user","content":[{"type":"input_text","text":"The user needs technical support."}]}}
```

SDK text modes are client-side conventions:

- `append` maintains a rolling transcript.
- `replace` replaces the locally retained transcript.
- `event` supplies one-shot text without adding it to that retained transcript.

TypeScript sends the retained transcript and queued one-shot text when opening a turn. Python queues text items for its outbound sender and also sends its retained transcript when connecting. These modes do not guarantee deletion of conversation items already sent to the server. Text input can drive a turn without audio.

## 6. Waves

### 6.1 Definition

A **wave** is a complete answer set with exactly one answer for each registered question. `wave_id` / `waveId` identifies an assembled wave; `t_ms` / `tMs` is its media timestamp. SDK-assigned identifiers describe the local stream, not a persistent identifier across reconnects.

### 6.2 The completeness invariant

The protocol requires complete, non-null answers, never a default false/hold decision for missing data. Both SDKs reject, as protocol errors:

- a missing or null `confidence`, or one that is not a finite number;
- a probability map that is not an object, or a probability that is not a finite number in `[0, 1]` (numeric strings are tolerated);
- a noul answer without `P(true)`;
- a choice label that is not one of the registered options;
- a score answer missing any registered level, or carrying an out-of-range level index.

TypeScript can still infer a choice label from probabilities when `choice` is absent; the inferred label must be a registered option.

Standalone wave decoding rejects unknown question IDs with `ProtocolError`. Python session assembly constructs results only for registered questions, discarding additional unknown IDs before decoding; it does not enforce the standalone unknown-ID rejection rule.

When decoding does reject an assembled wave, session loops log and count it (`protocol_errors` in Python, `protocolErrors` in TypeScript) and drop the partial group rather than terminating solely because of that invalid wave. Application callbacks are isolated the same way: a `when` predicate that raises counts as false for that wave, and a raising wave handler is logged; neither ends the session.

### 6.3 Client-side wave assembly

The common assembly path collects consecutive decisions by question ID:

- Start an empty accumulator for the turn.
- A decision for an ID already in the accumulator ends the previous group: flush it and begin the next group with the new decision.
- A terminal response event flushes the remaining group.
- Deliver a flushed wave only if its answer set is complete and valid.

Python additionally accepts an explicit boundary record:

```json
{"event":"sense.wave","wave_id":12,"seq":12,"t_ms":1200}
```

Python uses `wave_id` (or `seq` when `wave_id` is absent) to advance its next wave identifier and flushes the accumulator. The current TypeScript session does not use `sense.wave` boundary records; it relies on repeated question IDs and terminal response events. Do not assume that a transport close alone delivers a final buffered wave.

### 6.4 Resource limits

Both SDKs bound what a remote endpoint can make them buffer:

- Inbound WebSocket and WebRTC messages are capped at 4 MiB; a larger message ends that connection.
- A partial (newline-less) decision line is capped at 256 KiB. Python discards the oversized line and counts a protocol error; on the QUIC events stream, both SDKs end the connection and log it.
- Python queues received QUIC/WebRTC messages and ends the connection beyond 4096 unconsumed messages. TypeScript delivers them synchronously and holds no such queue.
- Wave history is capped at 512 waves. TypeScript also holds at most 512 undelivered waves for `run()`/`waves()`, dropping the oldest.
- Outbound media buffered client-side is bounded: Python keeps ~60 s of audio per source while disconnected and the last 256 text items; TypeScript keeps ~60 s per named audio source. The oldest entries are dropped first.
- A QUIC answer without certificate hashes is refused in both SDKs.

## 7. Errors

| Signal | Meaning | Current SDK handling |
|---|---|---|
| WebSocket handshake `401` / `404` | Authentication or endpoint/model refusal | Connection fails without the transient-refusal retry path |
| WebSocket handshake `503` / `429` | Service unavailable or rate/capacity limit | Backoff and redial |
| WebSocket close code `1011` | Session failure | Attempt a fresh connection; do not assume session continuity |
| `session_gone` error event | Session no longer available | Both SDKs reconnect fresh; Python also re-sends its transcript (see below) |
| `buffer_full` | Uncommitted audio buffer is full | Send an audio commit |
| `empty_buffer` | Commit without buffered audio | Nonfatal |
| `audio_input_transcript_unavailable` | Audio transcript unavailable | Nonfatal |
| Oversized JPEG / `frame_too_large` | JPEG exceeds the 256,000-byte frame budget | Python push raises `ValueError`; TypeScript push raises `ProtocolError` with code `frame_too_large`. Resize before retrying |
| Other mid-turn `error` | Response failure | Log/count the error and allow the run loop to reopen a turn |

On any wire death while the session is open, both SDKs keep the registered questions, reset the conversation state, and reconnect fresh (`session_gone`/`1011` immediately; any other close code after a backoff). If the TypeScript reconnect fails terminally — dialing errors that are not transient refusals, or ten consecutive connections that each lived under 10 s (sustained churn) — its wave streams end with a `redial_failed` event rather than raising an unhandled rejection or spinning forever.

Both SDKs make the redial observable. `session.state` reads `connecting`, `live` (the current connection has delivered a wave), `reconnecting` (a connection ended and a fresh one is being dialed, or is up but has not answered yet; not a failure), `closed`, or `failed` (terminal). Each wire death that will be redialed emits a `lane_ended` event — `code` is the WebSocket close code as a string and `message` the close reason, when the wire reported them — and the first wave on the fresh connection emits `reconnected` with the outage length (`raw.downMs` in TypeScript, `raw["down_ms"]` in Python). A terminal end emits `redial_failed`. TypeScript delivers these through `session.onEvent(handler)`, Python through `session.on_event(handler)`; `session.stats.reconnects` counts them. A connection that stays open while the server stops answering emits none of these: the state stays `live`.

Retry policies differ. Python retries transient WebSocket/network failures with exponential backoff, incorporating numeric `Retry-After` values for `429`/`503` up to its delay cap; reconnects after a wire death redial immediately, without a young-connection budget. TypeScript retries WebSocket `429`/`503` refusals with exponential backoff; it does not consume `Retry-After`; deaths of wires younger than 10 s back off on a jittered ladder, capped at ten consecutive young deaths before the streams end. Python also re-sends its accumulated transcript to the new session on reconnect; TypeScript discards conversation text across reconnects. QUIC/WebRTC setup failures follow the transport fallback policy in §1.

## 8. Mapping to the SDK surface

| SDK operation | Wire behavior |
|---|---|
| Create `Sense` / `Session` | Configure endpoint, model, credentials, cadence, and transport; registration is sent when connected |
| `app.noul`, `app.choice`, `app.score` | Build Jev-shaped question entries (`type`/`instructions`/`criteria`) in the instructions envelope |
| `session.attach(...)` | The one canonical input path: pumps audio (`input_audio_buffer.append`), video (`conversation.item.create` with `input_image`), and text (`input_text`) from the attached sources |
| `session.run()` | Connect/register, drive turns, assemble decisions, and yield waves |
| `session.close()` | Send `urun.session.end` (the explicit end: the server releases the session's compute at once, not after its re-attach grace), then close the connection |
| `Wave` | Decoded complete answer set (§6) |
| Predicates and handlers | Client-side evaluation; they do not suppress server questions |

Python's `session.attach(audio=…, video=…, text=…)` pumps inputs concurrently with wave consumption. TypeScript's `session.attach(stream | sources)` streams a live browser MediaStream or programmatic sources the same way; `run()` dials on first read and is the wave loop in both. Audio pushed before the dial is buffered client-side and replayed onto the fresh wire before any turn opens. See the [README](../README.md).

Python `Context.cancel()` normally also requests `response.cancel`; `barge_in=False` cancels only the tracked task. TypeScript `Context.cancel(key)` matches: it barges in by default and takes `{ bargeIn: false }` to cancel only the tracked run.

### 8.1 Named sources and limitations

Python queues named audio sources and drains them round-robin. When an assembled wave has no distinct source label, the session attributes it to the last drained source. Python handlers and lookups can filter by source, but this client-side attribution is not proof of isolated server-side media processing.

TypeScript queues non-default audio sources and drains one queued chunk per default-source audio push. Its session assembler does not propagate a source label into delivered waves, and its handler APIs do not expose Python's source filters. An application must not rely on independent per-source wave streams across both SDKs.

### 8.2 Bare-question comparison rule (both SDKs)

Equality and inequality compare the top value: a score level name, a choice option, or a noul boolean. Ordering comparisons on score questions compare the expected ordinal (the probability-weighted, zero-based level index). Python expresses these with operators; TypeScript uses methods such as `eq`, `ne`, and `gte`.

The shared conformance cases `score_bare_gte` and `score_bare_eq_top` exercise this distinction.

### 8.3 Input-driven turns

The SDK does not generate silent audio or periodic text to keep model decisions flowing. Text can start a turn without audio, but applications should not depend on an idle session producing waves indefinitely. This is distinct from the connection keepalive in §1.1 and the QUIC control stream's `ping`/`pong`: those keep the transport alive and never produce waves.

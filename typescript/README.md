# @urun-sh/sense (TypeScript SDK)

TypeScript SDK for uRun's realtime decision model — Sense.

Sense is a *realtime*, *multi-modal* System One model for voice and video agents and was inspired by the Jev model. It runs on continuously streaming media and answers questions about the input many times per second: for example, whether the person is still speaking or done, whether an interruption is a new question or just an "mmmhmm", and what the person's emotional state is.

The Sense SDK gives you one unified surface for perception, decision-making, and reactive agent control.

## Get started

The TypeScript library targets Node.js 22+ and Bun. The demo below needs [Bun](https://bun.sh). Direct browser SDK use is not currently supported; the demo connects the browser to a local Bun server that keeps your API key server-side.

```bash
bun add @urun-sh/sense
```

Or with plain npm:

```bash
npm install @urun-sh/sense
```

## Authenticate with uRun

Make yourself a uRun API key at [urun.sh](https://urun.sh) then use that as your `URUN_API_KEY`:

```bash
export URUN_API_KEY="<urun-api-key>"
```

Try the interactive example (requires a mic).

Examples stream the audio, video, and text you supply to the configured Sense service. Use appropriate test inputs and review the [privacy policy](https://urun.sh/legal/privacy) and [service terms](https://urun.sh/legal/terms). Keep API keys out of browser bundles and source control.

#### TypeScript (local server + browser session):

Launch a server then open the URL in your browser:

```bash
bunx -p @urun-sh/sense sense-cheese-demo
```

Open the exact URL the server prints: it includes a per-run access token after `#`, and the relay refuses connections without it. By default the server listens on this machine only. To try it on a phone, connect the phone to the same network, start the server with `--lan` (`bunx -p @urun-sh/sense sense-cheese-demo --lan`), and open one of the printed network URLs. Anyone who has a printed URL can use your configured URUN API key, up to 12 five-minute sessions per run. Run it on a trusted network and press Ctrl+C when finished.

## TypeScript example walk-through

This is an overview of how `@urun-sh/sense` is used to build the demo above. Sense is a decision model, you program it with questions and let it decide the answers. Sense currently supports 3 types of question:
-   Nouls: yes/no questions that the model will answer with `true` or `false` with a confidence between 0 and 1.
- Choices: categorical questions where the answer must be from a list of choices.
- Scores: again a set of choices but with the important difference that answers naturally form a continuous scale.

**Declare your decisions.** Create the client app and register your questions. The NAME is the first positional argument — it is the answer key you reference results by on every wave. Each question carries `instructions` (what to answer) and `criteria` (Jev-style, exactly one shape per kind):

```ts
import { Sense, type Session } from "@urun-sh/sense";

const app = new Sense(); // Needs an API key: default from URUN_API_KEY

const speech = app.noul("speech", {
    instructions: "Is someone speaking right now?",
    criteria: { true: "a person is speaking", false: "nobody is speaking" }, // optional
});
const cheese = app.noul("cheese", { instructions: "Is the person talking about cheese right now?" });
```

The model can also answer non-binary questions from a list of choices — given a `choice` Sense matches from a fixed option list. Criteria is the required name → description map (insertion order is the wire order; every option needs a non-empty description):

```ts
const kind = app.choice("cheese_type", {
    instructions: "Which type of cheese are they talking about?",
    criteria: {
        cheddar: "Firm yellow cheese",
        brie: "Soft white-rind cheese",
        gouda: "Semi-hard Dutch cheese",
        parmesan: "Hard aged grating cheese",
        camembert: "Soft bloomy-rind cheese",
        mozzarella: "Stretchy fresh cheese",
        blue: "Veined blue cheese",
        feta: "Crumbly brined cheese",
        other: "Any other kind of cheese",
    },
});
```

If you want an ordered scale use a `score` — criteria is the ordered array of level descriptions, low end first (2..10 levels). The level label you read back IS the description text:

```ts
const feeling = app.score("feeling", {
    instructions: "How does the person feel about that cheese?",
    criteria: ["disgusted", "negative", "neutral", "positive", "delighted"],
});
```

**Open a session and start reading waves:** `session.run()` dials on first read and yields `waves`. A wave is a batch of decisions taken over the last slice of audio:

```ts
const session = app.session();
const waves = session.run(); // returns an async iterator

async function readWaves(): Promise<void> {
    for await (const wave of waves) { // iterations can throw if Sense is unreachable
        ...
    }
}
```

**Stream input with attach() — the one input path.** `session.attach({ stream })` takes a live browser MediaStream and does the capture for you — audio is resampled to PCM16 24 kHz mono and pushed in 40 ms chunks, video frames are sampled to JPEG at the session cadence:

```ts
const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: true }); // audio-only streams work too
const attached = await session.attach({ stream }); // resamples + pushes continuously; returns a handle with detach()
// later: attached.detach() — stops the pumping; the stream's tracks stay yours to stop
```

**Browser quickstart (no relay).** Your server keeps the org API key and mints a 10-minute client secret; the browser dials `inference.urun.sh` directly — the dial route (`POST /v1/realtime/calls`) is CORS-open for bearer auth, and the WebTransport/WebRTC media leg needs no relay. A runnable minting server is at [examples/typescript/mint_server.ts](https://github.com/urun-sh/sense-sdk/blob/main/examples/typescript/mint_server.ts):

```ts
// server: POST https://inference.urun.sh/v1/realtime/client_secrets with
// `Authorization: Bearer $URUN_API_KEY` → { value: "ek_…", expires_at }

// browser: the SDK re-resolves a fresh secret per dial/redial
const app = new Sense({
    apiKey: async () => {
        const { value } = (await (await fetch("/client-secret", { method: "POST" })).json()) as { value: string };
        return value; // a fresh 10-minute client secret, org key never leaves the server
    },
});
const session = app.session();
const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: true });
const attached = await session.attach({ stream }); // the one-liner above, now unattended
```

**Stream programmatic input.** Off the browser, attach sources instead. Each source is an async iterable, a sync iterable, or a zero-arg callable returning the next chunk (null/undefined ends it): audio chunks are PCM16 24 kHz mono bytes, video chunks are JPEG bytes, text chunks are strings appended to the transcript. attach() pumps in the background — no run loop needed for pumping — and the turn opens on the first pushed chunk:

```ts
const attached = await session.attach({ audio: mic.chunks() }); // mic, file, queue — same path
async function readWaves(): Promise<void> {
    for await (const wave of session.run()) {
        ...
    }
}
```

`attached.detach()` stops the pumping; the wave loop returns when the source ends or `session.close()` runs.

**Read decisions off each wave.** Every answered question `id` has a `result` with a `.value`, and a confidence/probability `.p`. Additionally:
- Choices return confidences for each option in `.pOption(name)`(normalized so they form a discrete probability distribution).
- Scores return:
  - The argmax level DESCRIPTION in `.top` (the server echoes the description back as the choice)
  - The probability of that level in `.p`
  - The expected index value in `.value`, in this case (0..4) across 5 levels
  - `pAtLeast(level)`: Σ p over levels at or above a zero-based index or the exact description

You will usually want to gate actions on the probability (the example requires p >= 0.6 to trust any result):

```ts
const speaking = Boolean(wave.result("speech").value);
const detection = wave.result("cheese");
if (detection.p >= 0.6) {
    const kind = wave.result("cheese_type");
    const name = kind.pOption(String(kind.value)) >= 0.6 ? String(kind.value) : "unknown type";
    const sentiment = wave.result("feeling");
    const mood = sentiment.p >= 0.6 ? sentiment.top : "undecided";
    const moodScore = Number(sentiment.value); // Expected index value
}
```

**Close in the correct order.** Stop the pumping and close the session — `session.close()` ends the run loop, so the wave iteration above returns:
```ts
try {
    await readWaves(); // first to end: source end or wire death
} finally {
    attached.detach(); // stops the pumping; a live MediaStream's tracks stay yours to stop
    await session.close();
}
```

## Next steps
Beyond this example, the SDK also has predicates + `app.when(pred, handler)` for edge-triggered handlers, and `app.onWave(handler)` to observe every completed wave. Refer to the [Sense reference](https://docs.urun.sh/docs/sense/reference?facet=session) for details.

### Full example code

  - [examples/typescript/cheese_demo.ts](https://github.com/urun-sh/sense-sdk/blob/main/examples/typescript/cheese_demo.ts)

The separate `voice_agent` example illustrates predicate-driven control flow; its reply and tool execution are stubs, not a complete assistant. Its bundled speech sample and additional text input are separate demonstration inputs, not matching transcripts.

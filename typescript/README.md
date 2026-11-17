# @urun-sh/sense (TypeScript SDK)

TypeScript SDK for uRun's realtime decision model — Sense.

Sense is a *realtime*, *multi-modal* System One model for voice and video agents and was inspired by the Jev model. It runs on continuously streaming media and answers questions about the input many times per second: for example, whether the user is still speaking or done, whether an interruption is a new question or just an "mmmhmm", and what the user's emotional state is.

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

**Declare your decisions.** Create the client app and register your nouls, each registration returns a `Question` with an `id` to reference the results by later:

```ts
import { Sense, type Session } from "@urun-sh/sense";

const app = new Sense(); // Needs an API key: default from URUN_API_KEY

const speech = app.noul("Is someone speaking right now?", { id: "speech" });
const cheese = app.noul("Is the speaker talking about cheese right now?", { id: "cheese" });
```

The model can also answer non-binary questions from a list of choices — given a `choice` Sense matches from a fixed option list. Options can carry a short description each (a Jev-style `criteria` map — name → description, insertion order preserved); bare name lists work too:

```ts
const kind = app.choice(
    "Which type of cheese are they talking about?",
    {
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
    { id: "cheese_type" },
);
```

If you want an ordered scale use a `score` — the grades or levels are the range of answers, in this case the user's feeling will be graded in the range (0..4). Levels may be a name → description map (insertion order is the ordinal order) or a bare name list:

```ts
const feeling = app.score(
    "How does the speaker feel about that cheese?",
    { hate: "Disgusted", dislike: "Negative", meh: "Neutral", like: "Positive", love: "Delighted" },
    { id: "feeling" },
);
```

**Open a session and start reading waves:** `session.run()` connects to Sense (unless `connect()` already has), registers the questions,
and yields `waves`. A wave is a batch of decisions taken over the last slice of audio:

```ts
const session = app.session();
await session.connect(); // dial before pushing audio — earlier chunks would be dropped
const waves = session.run(); // returns async iterator

async function readWaves(): Promise<void> {
    for await (const wave of waves) { // iterations can throw if Sense is unreachable
        ...
    }
}
```

**Live mic in one line (browser).** `session.attach(stream)` takes a browser MediaStream and does the capture for you — audio is resampled to PCM16 24 kHz mono and pushed in 40 ms chunks, video frames are sampled to JPEG at the session cadence:

```ts
const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: true }); // audio-only streams work too
const attached = await session.attach(stream); // resamples + pushes continuously; returns a handle with stop()
// later: attached.stop() — stops the pumping; the stream's tracks stay yours to stop
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
const attached = await session.attach(stream); // the one-liner above, now unattended
```

**Feed it audio concurrently.** The SDK currently speaks 24 kHz PCM16 mono in 40 ms chunks. The audio push and the wave consumption must run at the same time — a blocking wave loop starves the audio — so we run them as concurrent tasks:

```ts
async function sendAudio(chunks: AsyncGenerator<ArrayBuffer>, session: Session): Promise<void> {
    for await (const chunk of chunks) {
        session.audio.push(chunk); // PCM16, mono, 24 kHz; one 40 ms block
    }
    await session.close(); // audio ended — end the run loop so readWaves returns
}

const chunks = mic.chunks();
const first = await chunks.next(); // the turn opens on the first pushed chunk — push one before the wave loop starts
if (!first.done) session.audio.push(first.value);
const tasks = [sendAudio(chunks, session), readWaves()];
```

`session.run()` can also take finite audio/video/text inputs and push them for you before reading waves; this example pushes audio manually to keep the mic and the level meter in step.

**Read decisions off each wave.** Every answered question `id` has a `result` with a `.value`, and a confidence/probability `.p`. Additionally:
- Choices return confidences for each option in `.pOption(name)`(normalized so they form a discrete probability distribution).
- Scores return:
  - The argmax grade in `.top`
  - The probability of that grade in `.p`
  - The expected index value in `.value`, in this case (0..4) across 5 levels.

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

**Close in the correct order.** Stop the mic and close the session — `session.close()` ends the run loop, so the wave iteration above returns:
```ts
try {
    await Promise.race(tasks); // first task to finish: mic end or wire death
} finally {
    mic.stop(); // unblocks sendAudio when the wave reader ends first
    await session.close();
    await Promise.all(tasks); // settle both even when the race threw; the race error propagates after
}
```

## Next steps
Beyond this example, the SDK also has predicates + `app.when(pred, handler)` for edge-triggered handlers, and `app.onWave(handler)` to observe every completed wave. Refer to the [Sense reference](https://docs.urun.sh/docs/sense/reference?facet=session) for details.

### Full example code

  - [examples/typescript/cheese_demo.ts](https://github.com/urun-sh/sense-sdk/blob/main/examples/typescript/cheese_demo.ts)

The separate `voice_agent` example illustrates predicate-driven control flow; its reply and tool execution are stubs, not a complete assistant. Its bundled speech sample and additional text input are separate demonstration inputs, not matching transcripts.

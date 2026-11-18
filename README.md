# sense-sdk

Python and TypeScript SDKs for uRun's realtime decision model — Sense.

Sense is a *realtime*, *multi-modal* System One model for voice and video agents and was inspired by the Jev model. It runs on continuously streaming media and answers questions about the input many times per second: for example, whether the user is still speaking or done, whether an interruption is a new question or just an "mmmhmm", and what the user's emotional state is.

The Sense SDK gives you one unified surface for perception, decision-making, and reactive agent control.

## Contents

- [Get started](#get-started-with-python-or-typescript)
- [Authenticate with uRun](#authenticate-with-urun)
- [Run an example](#run-an-example)
- [Python example walk-through](#python-example-walk-through)
- [TypeScript example walk-through](#typescript-example-walk-through)
- [Next steps](#next-steps)

## Get started (with Python or TypeScript)

#### Python 3.11+ for the demo (core SDK: 3.10+):

```bash
pip install "urun-sense[demo]"
```

On Linux, run PipeWire and make `pw-record`, `pw-dump`, and `wpctl` available on your PATH.

#### TypeScript (Bun):

The TypeScript library targets Node.js 22+ and Bun. The demo below needs [Bun](https://bun.sh). Direct browser SDK use is not currently supported; the demo connects the browser to a local Bun server that keeps your API key server-side.

```bash
bun add @urun-sh/sense
```

#### TypeScript (npm):

```bash
npm install @urun-sh/sense
```


## Authenticate with uRun

Make yourself a uRun API key at [urun.sh](https://urun.sh) then use that as your `URUN_API_KEY`:

```bash
export URUN_API_KEY="<urun-api-key>"
```

## Run an example

Try the interactive example (requires a mic).

Examples stream the audio, video, and text you supply to the configured Sense service. Use appropriate test inputs and review the [privacy policy](https://urun.sh/legal/privacy) and [service terms](https://urun.sh/legal/terms). Keep API keys out of browser bundles and source control.

#### Python (standalone terminal demo):

The demo is in the urun-sense PyPi package, run the following command:

```bash
python -m sense.examples.cheese_demo
```

Wait for the connection, select your mic, and start talking to Sense.

#### TypeScript (local server + browser session):

Launch a server then open the URL in your browser:

```bash
bunx -p @urun-sh/sense sense-cheese-demo
```

Open the exact URL the server prints: it includes a per-run access token after `#`, and the relay refuses connections without it. By default the server listens on this machine only. To try it on a phone, connect the phone to the same network, start the server with `--lan` (`bunx -p @urun-sh/sense sense-cheese-demo --lan`), and open one of the printed network URLs. Anyone who has a printed URL can use your configured URUN API key, up to 12 five-minute sessions per run. Run it on a trusted network and press Ctrl+C when finished.

## Python example walk-through

This is an overview of how `sense-sdk` is used to build the demo above. Sense is a decision model, you program it with questions and let it decide the answers. Sense currently supports 3 types of question:
-   Nouls: yes/no questions that the model will answer with `true` or `false` with a confidence between 0 and 1.
- Choices: categorical questions where the answer must be from a list of choices.
- Scores: again a set of choices but with the important difference that answers naturally form a continuous scale.

**Declare your decisions.** Create the client app and register your questions. Each registration returns a `Question`; its NAME is the answer key you read results back by. Questions are exactly Jev's shape: `instructions` (the natural-language question) plus `criteria` per kind, with a description on every option:

```python
from sense import Sense

app = Sense()  # Needs an API key: default from URUN_API_KEY

speech = app.noul("speech", instructions="Is someone speaking right now?")
cheese = app.noul("cheese", instructions="Is the speaker talking about cheese right now?")
```

The model can also answer non-binary questions from a list of choices — given a `choice` Sense matches from a fixed option list. `criteria` is the option map: each option name → a short description that separates it from the others:

```python
kind = app.choice(
    "cheese_type",
    instructions="Which type of cheese are they talking about?",
    criteria={
        "cheddar": "Firm yellow cheese",
        "brie": "Soft white-rind cheese",
        "gouda": "Semi-hard Dutch cheese",
        "parmesan": "Hard aged grating cheese",
        "camembert": "Soft bloomy-rind cheese",
        "mozzarella": "Stretchy fresh cheese",
        "blue": "Veined blue cheese",
        "feta": "Crumbly brined cheese",
        "other": "Any other kind of cheese",
    },
)
```

If you want an ordered scale use a `score` — `criteria` is an ordered array of level descriptions, low end first (2..10), in this case the user's feeling will be graded in the range (0..4):

```python
feeling = app.score(
    "feeling",
    instructions="How does the speaker feel about that cheese?",
    criteria=["Disgusted", "Dislikes it", "Neutral", "Likes it", "Delighted"],
)
```

**Open a session and start reading waves:** `session.run()` connects to Sense, registers the questions,
and yields `waves`. A wave is a batch of decisions taken over the last slice of audio:

```python
session = app.session()
waves = session.run()  # returns async iterator


async def read_waves(waves):
    async for wave in waves:  # iterations can throw SenseUnavailable
        ...
```

**Stream inputs with `session.attach(...)`.** The SDK currently speaks 24 kHz PCM16 mono in 40 ms chunks. `attach` is the one canonical way to stream inputs: it pumps your sources (async iterables, sync iterables, or zero-arg callables — `None` ends) while the wave loop consumes decisions:

```python
session.attach(audio=mic.chunks())  # PCM16, mono, 24 kHz; one 40 ms block
await asyncio.gather(read_waves(waves))
```

Audio pushes and wave consumption must both make progress — a blocking wave loop starves the audio. `attach` starts the pumps for you (before or during `run()`); `detach()` on the returned handle stops them, and the session closing stops them too.

**Where does a live mic come from (Python)?** There is no capture built into the core SDK — keep it dependency-free. Produce PCM16 mono 24 kHz chunks yourself (read a WAV, or use whatever capture library your app already has — e.g. a `sounddevice` raw stream at 24 kHz) and pass the iterator to `session.attach(audio=...)`. For a browser mic with zero plumbing, use the TypeScript SDK's one-liner: `session.attach(await navigator.mediaDevices.getUserMedia({ audio: true }))`.

**Read decisions off each wave.** Every answered question `name` has a `result` with a `.value`, and a confidence/probability `.p`. Additionally:
- Choices return confidences for each option in `.p_option(name)`(normalized so they form a discrete probability distribution).
- Scores return:
  - The argmax level in `.top` (the level's description, as the model echoes it)
  - The probability of that level in `.p`
  - The expected index value in `.value`, in this case (0..4) across 5 levels.
  - Cumulative tail probabilities in `.p_at_least(level)` — a zero-based index or the exact level description.

You will usually want to gate actions on the probability (the example requires p >= 0.6 to trust any result):

```python
speaking = bool(wave.result("speech").value)
detection = wave.result("cheese")
if detection.p >= 0.6:
    kind = wave.result("cheese_type")
    name = kind.value if kind.p_option(kind.value) >= 0.6 else "unknown type"
    sentiment = wave.result("feeling")
    mood = sentiment.top if sentiment.p >= 0.6 else "undecided"
    mood_score = sentiment.value  # Expected index value
```

**Close in the correct order.** Cancel your tasks and let them settle, then `waves.aclose()` and `await session.close()` (closing the session also stops the attached pumps):
```python
try:
    await asyncio.gather(*tasks)
finally:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)  # settle both before closing
    await waves.aclose()
    await session.close()
```

## TypeScript example walk-through

This is an overview of how `@urun-sh/sense` is used to build the demo above. Sense is a decision model, you program it with questions and let it decide the answers. Sense currently supports 3 types of question:
-   Nouls: yes/no questions that the model will answer with `true` or `false` with a confidence between 0 and 1.
- Choices: categorical questions where the answer must be from a list of choices.
- Scores: again a set of choices but with the important difference that answers naturally form a continuous scale.

**Declare your decisions.** Create the client app and register your questions. Each registration returns a `Question`; its NAME is the answer key you read results back by. Questions are exactly Jev's shape: `instructions` (the natural-language question) plus `criteria` per kind, with a description on every option:

```ts
import { Sense, type Session } from "@urun-sh/sense";

const app = new Sense(); // Needs an API key: default from URUN_API_KEY

const speech = app.noul("speech", { instructions: "Is someone speaking right now?" });
const cheese = app.noul("cheese", { instructions: "Is the speaker talking about cheese right now?" });
```

The model can also answer non-binary questions from a list of choices — given a `choice` Sense matches from a fixed option list. `criteria` is the option map: each option name → a short description that separates it from the others:

```ts
const kind = app.choice(
    "cheese_type",
    {
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
    },
);
```

If you want an ordered scale use a `score` — `criteria` is an ordered array of level descriptions, low end first (2..10), in this case the user's feeling will be graded in the range (0..4):

```ts
const feeling = app.score("feeling", {
    instructions: "How does the speaker feel about that cheese?",
    criteria: ["Disgusted", "Dislikes it", "Neutral", "Likes it", "Delighted"],
});
```

**Open a session and start reading waves:** `session.run()` connects to Sense, registers the questions,
and yields `waves`. A wave is a batch of decisions taken over the last slice of audio:

```ts
const session = app.session();
const waves = session.run(); // returns async iterator; dials on first read

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

**Programmatic sources (server/Node).** attach is the ONE input path: instead of a MediaStream, pass a sources object `{ audio?, video?, text? }` — each an async iterable, a sync iterable, or a zero-arg callable returning the next chunk (null ends it). Audio chunks are PCM16 24 kHz mono bytes, video chunks are JPEG bytes, text chunks are strings appended to the transcript:

```ts
const attached = await session.attach({
    audio: micChunks(), // PCM16 mono 24 kHz, e.g. 40 ms blocks
    text: ["Hi — my internet has been down since this morning."], // initial state rides the first turn
});
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

**Read decisions off each wave.** Every answered question `name` has a `result` with a `.value`, and a confidence/probability `.p`. Additionally:
- Choices return confidences for each option in `.pOption(name)`(normalized so they form a discrete probability distribution).
- Scores return:
  - The argmax level in `.top` (the level's description, as the model echoes it)
  - The probability of that level in `.p`
  - The expected index value in `.value`, in this case (0..4) across 5 levels.
  - Cumulative tail probabilities in `.pAtLeast(level)` — a zero-based index or the exact level description.

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

**Close in the correct order.** Stop the attach handle (the pumps) and close the session — `session.close()` ends the run loop, so the wave iteration above returns:
```ts
try {
    await Promise.race([readWaves(), micEnded]); // first to finish: mic end or wire death
} finally {
    attached.stop(); // stops the attach pumps; the stream's tracks stay yours to stop
    await session.close();
}
```

## Next steps
Beyond this example, the SDK also has predicates + `app.when(pred, handler)` for edge-triggered handlers, and `app.onWave(handler)` (Python: `app.on_wave(handler)`) to observe every completed wave. Refer to the [Sense reference](https://docs.urun.sh/docs/sense/reference?facet=session) for details.

### Full example code
  - [python](examples/python/cheese_demo.py)
  - [typescript](examples/typescript/cheese_demo.ts)

The separate `voice_agent` examples illustrate predicate-driven control flow; their reply and tool execution are stubs, not a complete assistant. Their bundled speech sample and additional text input are separate demonstration inputs, not matching transcripts.

For development and support, see [CONTRIBUTING.md](CONTRIBUTING.md). For vulnerability reporting, see [SECURITY.md](SECURITY.md).

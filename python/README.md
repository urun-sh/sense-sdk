# urun-sense (Python SDK)

Python SDK for uRun's realtime decision model — Sense.

Sense is a *realtime*, *multi-modal* System One model for voice and video agents and was inspired by the Jev model. It runs on continuously streaming media and answers questions about the input many times per second: for example, whether the person is still speaking or done, whether an interruption is a new question or just an "mmmhmm", and what the person's emotional state is.

The Sense SDK gives you one unified surface for perception, decision-making, and reactive agent control.

## Get started

The core SDK requires Python 3.10 or newer. The terminal demo below requires Python 3.11 or newer for cancellation cleanup.

```bash
pip install "urun-sense[demo]"
```

On Linux, run PipeWire and make `pw-record`, `pw-dump`, and `wpctl` available on your PATH.

## Authenticate with uRun

Make yourself a uRun API key at [urun.sh](https://urun.sh) then use that as your `URUN_API_KEY`:

```bash
export URUN_API_KEY="<urun-api-key>"
```

Try the interactive example (requires a mic).

Examples stream the audio, video, and text you supply to the configured Sense service. Use appropriate test inputs and review the [privacy policy](https://urun.sh/legal/privacy) and [service terms](https://urun.sh/legal/terms). Keep API keys out of browser bundles and source control.

#### Python (standalone terminal demo):

The demo is in the urun-sense PyPi package, run the following command:

```bash
python -m sense.examples.cheese_demo
```

Wait for the connection, select your mic, and start talking to Sense.

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

If you want an ordered scale use a `score` — `criteria` is an ordered array of level descriptions, low end first (2..10), in this case the person's feeling will be graded in the range (0..4):

```python
feeling = app.score(
    "feeling",
    instructions="How does the speaker feel about that cheese?",
    criteria=["Disgusted", "Dislikes it", "Neutral", "Likes it", "Delighted"],
)
```

**Open a session and start reading waves:** the session drive — dial, turn-open loop, wave assembly — starts at whichever of `session.attach()` or `session.run()` runs first (the second is a no-op), so handler-only sessions work with no run loop at all. `run()` yields `waves`. A wave is a batch of decisions taken over the last slice of audio:

```python
session = app.session()
waves = session.run()  # returns async iterator


async def read_waves(waves):
    async for wave in waves:  # iterations can throw SenseUnavailable
        ...
```

**Stream inputs with `session.attach(...)` — it starts the session.** The SDK currently speaks 24 kHz PCM16 mono in 40 ms chunks. `attach` is the one canonical way to stream inputs: it pumps your sources (async iterables, sync iterables, or zero-arg callables — `None` ends) and starts the session drive (dial + cadence + wave assembly), so `app.when`/`app.on_wave` handlers fire whether or not anyone iterates `run()`:

```python
session.attach(audio=mic.chunks())  # PCM16, mono, 24 kHz; one 40 ms block
await asyncio.gather(read_waves(waves))  # optional — handler-only apps skip this
```

Audio pushes and wave consumption must both make progress — a blocking wave loop starves the audio. `attach` starts the session and the pumps; `detach()` on the returned handle stops the pumps (the session keeps running — close it with `await session.close()`).

**Where does a live mic come from (Python)?** There is no capture built into the core SDK — keep it dependency-free. Produce PCM16 mono 24 kHz chunks yourself (read a WAV, or use whatever capture library your app already has — e.g. a `sounddevice` raw stream at 24 kHz) and pass the iterator to `session.attach(audio=...)`. For a browser mic with zero plumbing, use the TypeScript SDK's one-liner: `session.attach({ stream: await navigator.mediaDevices.getUserMedia({ audio: true }) })`.

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

**Close in the correct order.** Cancel your tasks and let them settle, then `waves.aclose()` and `await session.close()`:
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

## Reconnects
If the connection to Sense ends mid-session, the SDK dials a fresh one on its own and keeps your questions. You can show that in your UI:

```python
@session.on_event
def lane(ev):
    if ev.type == "lane_ended":      # ev.code / ev.message: close code and reason
        show_status("Reconnecting to Sense…")
    elif ev.type == "reconnected":
        show_status("Sense is back")
    elif ev.type == "redial_failed":  # terminal; run() raises the cause
        show_status(f"Sense is unavailable: {ev.message}")

session.state  # "connecting" | "live" | "reconnecting" | "closed" | "failed"
```

## Next steps
Beyond this example, the SDK also has decorator handlers — curried on the predicate, returning the function unchanged:

```python
@app.when(finished & (intent == "support"))
async def respond(event): ...

@app.on_wave(coalesce=True)
def observe(event): ...
```

Refer to the [Sense reference](https://docs.urun.sh/docs/sense/reference?facet=session) for details.

### Full example code

  - [examples/python/cheese_demo.py](https://github.com/urun-sh/sense-sdk/blob/main/examples/python/cheese_demo.py)

The separate `voice_agent` example illustrates predicate-driven control flow; its reply and tool execution are stubs, not a complete assistant. Its bundled speech sample and additional text input are separate demonstration inputs, not matching transcripts.

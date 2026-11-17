# urun-sense (Python SDK)

Python SDK for uRun's realtime decision model — Sense.

Sense is a *realtime*, *multi-modal* System One model for voice and video agents and was inspired by the Jev model. It runs on continuously streaming media and answers questions about the input many times per second: for example, whether the user is still speaking or done, whether an interruption is a new question or just an "mmmhmm", and what the user's emotional state is.

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

**Declare your decisions.** Create the client app and register your nouls, each registration returns a `Question` with an `id` to reference the results by later:

```python
from sense import Sense

app = Sense()  # Needs an API key: default from URUN_API_KEY

speech = app.noul("Is someone speaking right now?", id="speech")
cheese = app.noul("Is the speaker talking about cheese right now?", id="cheese")
```

The model can also answer non-binary questions from a list of choices — given a `choice` Sense matches from a fixed option list. Options can carry a short description each (a Jev-style `criteria` map — name → description, insertion order preserved); bare name lists work too:

```python
kind = app.choice(
    "Which type of cheese are they talking about?",
    {
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
    id="cheese_type",
)
```

If you want an ordered scale use a `score` — the grades or levels are the range of answers, in this case the user's feeling will be graded in the range (0..4). Levels may be a name → description map (insertion order is the ordinal order) or a bare name list:

```python
feeling = app.score(
    "How does the speaker feel about that cheese?",
    {"hate": "Disgusted", "dislike": "Negative", "meh": "Neutral", "like": "Positive", "love": "Delighted"},
    id="feeling",
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

**Feed it audio concurrently.** The SDK currently speaks 24 kHz PCM16 mono in 40 ms chunks. The audio push and the wave consumption must run at the same time — a blocking wave loop starves the audio — so we run them as concurrent tasks:

```python
async def send_audio(mic, session):
    async for chunk in mic.chunks():
        session.audio.push(chunk)  # PCM16, mono, 24 kHz; one 40 ms block
    await session.close()  # audio ended — end the run loop so read_waves returns


tasks = [
    asyncio.create_task(send_audio(mic, session)),
    asyncio.create_task(read_waves(waves)),
]
await asyncio.gather(*tasks)
```

`session.run()` can also take audio/video/text iterators directly and pump them for you; this example pushes audio manually to keep the mic and the level meter in step.

**Where does a live mic come from (Python)?** There is no capture built into the core SDK — keep it dependency-free. The server-side path is exactly the loop above: produce PCM16 mono 24 kHz chunks yourself (read a WAV, or use whatever capture library your app already has — e.g. a `sounddevice` raw stream at 24 kHz) and `session.audio.push(chunk)` each one. For a browser mic with zero plumbing, use the TypeScript SDK's one-liner: `session.attach(await navigator.mediaDevices.getUserMedia({ audio: true }))`.

**Read decisions off each wave.** Every answered question `id` has a `result` with a `.value`, and a confidence/probability `.p`. Additionally:
- Choices return confidences for each option in `.p_option(name)`(normalized so they form a discrete probability distribution).
- Scores return:
  - The argmax grade in `.top` 
  - The probability of that grade in `.p`
  - The expected index value in `.value`, in this case (0..4) across 5 levels.

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

## Next steps
Beyond this example, the SDK also has `app.predicate(fn)` + `app.when(pred, handler)` for edge-triggered handlers, and `app.onWave(handler)` to observe every completed wave. Refer to the [Sense reference](https://docs.urun.sh/docs/sense/reference?facet=session) for details.

### Full example code

  - [examples/python/cheese_demo.py](https://github.com/urun-sh/sense-sdk/blob/main/examples/python/cheese_demo.py)

The separate `voice_agent` example illustrates predicate-driven control flow; its reply and tool execution are stubs, not a complete assistant. Its bundled speech sample and additional text input are separate demonstration inputs, not matching transcripts.

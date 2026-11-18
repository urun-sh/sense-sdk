#!/usr/bin/env python3
"""Flagship full-duplex voice agent on the Sense SDK (Python).

Sense decides WHEN to respond, WHICH tool, and WHEN to interrupt; your code
executes. Reads a WAV (bundled synthetic speech by default), streams it to the Sense endpoint,
and reacts on edge-triggered predicates — including a barge-in that cancels
the in-flight reply via ctx.cancel() (a response.cancel on the wire).

Usage:
    URUN_API_KEY=<key> uv run --project python -m sense.examples.voice_agent
       (a bundled sample wav is used when no path is given)
"""

import asyncio
import sys
import time
import wave
from array import array
from pathlib import Path

from sense import Context, ProtocolError, Sense, SenseUnavailable

# -- questions ----------------------------------------------------------------

app = Sense()  # apiKey/baseURL default from URUN_API_KEY / SENSE_BASE_URL
finished = app.noul("finished", instructions="Did the current speaker just finish their turn?")
user_speaking = app.noul("user_speaking", instructions="Is a human speaking right now?")
intent = app.choice(
    "intent",
    instructions="What does the person want?",
    criteria={"support": "Technical help with the product",
              "sales": "Pricing, plans, buying",
              "other": "Anything else"},
)
actions = app.choice(
    "actions",
    instructions="What should the agent do?",
    criteria={"respond": "Answer the person now",
              "ignore": "Stay quiet; nothing is addressed to the agent",
              "wait": "Let the person finish before deciding"},
)
urgency = app.score(
    "urgency",
    instructions="How urgent is this?",
    criteria=["low — routine, can wait",
              "medium — time sensitive today",
              "high — blocking and needs action right now"],
)
tool = app.choice(
    "tool",
    instructions="Which tool applies?",
    criteria={"none": "No tool is needed",
              "weather": "A weather question",
              "search": "Something to look up",
              "timer": "Set a timer or reminder"},
)

# The most recent trigger context — barge-in cancels tasks through it.
last_event: Context | None = None


# -- reactions (edge-triggered: fire on first true, then each false->true) ----


@app.when(finished & (intent == "support") & (actions.top == "respond"))
async def respond(event):
    global last_event
    last_event = event
    wave = event.wave
    print(
        f"[respond] wave {wave.wave_id} t={wave.t_ms}ms urgency={wave.result('urgency').value:.2f}"
    )
    # A tracked cancellable task: rejected while another 'reply' is active
    # (reject-by-default is the spec policy for tools; pass replace=True to
    # supersede); cancelled by the barge-in watcher below.
    try:
        await event.run(
            "reply", lambda ctx: reply(wave), on_cancel=lambda: print("[respond] reply cancelled")
        )
    except ValueError:
        print("[respond] reply already in flight — edge trigger refired")


@app.when(finished & (intent.p("sales") >= 0.7))
async def handoff(event):
    print(
        f"[sales-handoff] wave {event.wave.wave_id}: "
        f"P(sales)={event.wave.result('intent').p_option('sales'):.2f}"
    )


@app.when(tool != "none")
async def invoke_tool(event):
    print(f"[tool] wave {event.wave.wave_id}: invoking {event.wave.result('tool').value}")


# Turn boundary: the speaker finished AND went quiet — the moment a voice
# agent would hand the floor to its reply path.
@app.when(finished & ~user_speaking)
async def turn_boundary(event):
    w = event.wave
    print(f"[turn-boundary] wave {w.wave_id} t={w.t_ms}ms — speaker finished and quiet")


# Probability predicate with hysteresis: true at P(finished) >= 0.8, resets
# only below 0.6 — thresholds near the boundary flap and edge triggers re-fire.
@app.when(finished.p.between(0.8, 0.6))
async def settled(event):
    print(f"[settled] wave {event.wave.wave_id}: speaker settled")


@app.on_wave(coalesce=True)  # latest-wins: never process every wave at 12.5 Hz
def observe(event):
    w = event.wave
    if w.result("actions").value == "ignore":
        return
    print(
        f"[wave {w.wave_id}] t={w.t_ms}ms finished={w.result('finished').value} "
        f"intent={w.result('intent').value}"
    )


async def barge_in_watch():
    """Interrupt: the user resumed speaking (P >= 0.9) while we were replying."""
    while True:
        await asyncio.sleep(0.1)
        if last_event is None:
            continue
        try:
            spk = last_event.get(user_speaking)
        except ProtocolError:
            continue  # no waves yet
        if spk.value and spk.p >= 0.9:
            print("[barge-in] user resumed — cancelling the in-flight reply")
            last_event.cancel("reply")  # tracked task cancel + response.cancel
            return


async def reply(wave):
    print(f"[agent] replying to wave {wave.wave_id} (stub execution)")
    await asyncio.sleep(2.0)
    print(f"[agent] reply for wave {wave.wave_id} complete")


# -- the session ---------------------------------------------------------------


async def wav_chunks(path: str, chunk_ms: int = 40):
    """Async-yield 24 kHz PCM16 mono chunks paced like a live mic
    (48 kHz decimated in-process)."""
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        samples = array("h", w.readframes(w.getnframes()))
    if rate == 48000:  # drop every other sample: 48k -> 24k
        samples = samples[::2]
    n = int(24000 * chunk_ms / 1000)
    for i in range(0, len(samples) - n, n):
        yield samples[i : i + n].tobytes()
        await asyncio.sleep(chunk_ms / 1000)


async def main():
    path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else str(Path(__file__).parent / "assets" / "mic-question-48k-mono.wav")
    )
    session = app.session()
    monitor = asyncio.create_task(barge_in_watch())
    start = time.monotonic()
    try:
        # Text input alongside the audio (e.g. a transcript from your own ASR):
        # the model hears the audio directly; text is extra context. attach is
        # the one input path — audio and text both ride it.
        session.attach(audio=wav_chunks(path), text=iter([
            "Hi — my internet has been down since this morning and I need help.",
        ]))
        waves = session.run()
        while True:
            try:
                wave = await asyncio.wait_for(waves.__anext__(), timeout=15.0)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                print("[demo] lane quiet — stopping")
                break
            if time.monotonic() - start > 30:
                print("[demo] 20s elapsed — stopping")
                break
            del wave  # handlers react; the run loop assembles and yields waves
    except SenseUnavailable as exc:
        print(f"endpoint unavailable: {exc}", file=sys.stderr)
        return 1
    finally:
        monitor.cancel()
        await session.close()
    print(f"stats: {session.stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""🧀 Sense SDK cheese classifier — terminal + mic.

    git clone https://github.com/urun-sh/sense-sdk.git
    cd sense-sdk
    uv build --project python -o dist
    uv venv --python 3.11
    uv pip install --python .venv/bin/python "$(ls -t dist/urun_sense-*.whl | head -n 1)[demo]"
    URUN_API_KEY="<urun-key>" .venv/bin/python examples/python/cheese_demo.py

Linux mic: PipeWire (pw-record, pw-dump, wpctl).

--debug shows every answer's probability as horizontal bars and lets you
tune the decision threshold live with the arrow keys.
"""
import argparse
import asyncio
import json
import logging
import math
import os
import subprocess
import sys
import time
from array import array
from collections import deque
from contextlib import suppress

import cheese_bars  # the --debug probability bars
from rich.console import Group
from rich.live import Live
from rich.text import Text
from sense import Sense, SenseUnavailable

SAMPLE_RATE = 24000   # the SDK speaks 24 kHz PCM16 mono
CHUNK_MS = 40         # like a live mic cadence
FEELING_EMOJI = {"Disgusted": "🧀🤮", "Dislikes it": "🧀🙁", "Neutral": "🧀😐", "Likes it": "🧀🙂", "Loves it": "🧀❤️"}
BLOCKSIZE = SAMPLE_RATE * CHUNK_MS // 1000  # 960 samples per callback
USE_PIPEWIRE = sys.platform.startswith("linux")

# -- Sense: define the questions, stream audio, read decisions ----------------
app = Sense()  # api key / base URL default from URUN_API_KEY / SENSE_BASE_URL

speech = app.noul("speech", instructions="Is someone speaking right now?")
cheese = app.noul("cheese", instructions="Is the speaker talking about cheese right now?")
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
feeling = app.score(
    "feeling",
    instructions="How does the speaker feel about that cheese?",
    criteria=["Disgusted", "Dislikes it", "Neutral", "Likes it", "Loves it"],
)
# --debug panel rows: display label, question id.
DEBUG_QUESTIONS = (("speech", "speech"), ("cheese", "cheese"),
                    ("cheese type", "cheese_type"), ("feeling", "feeling"))


def show_wave(wave, session, display):
    """A wave is a batch of decisions, not an audio response."""
    display.sense_state = "online"
    display.last_wave_ts = time.monotonic()
    display.wire = session._wire
    display.diagnostic = ""
    display.wave_id = wave.wave_id
    display.wave = wave
    decide(display)


def decide(display):
    """Verdict for the latest wave at the current threshold; re-run on every
    threshold change so decisions tune live."""
    wave = display.wave
    if wave is None:
        return
    display.speaking = bool(wave.result("speech").value)
    detection = wave.result("cheese")
    threshold = display.threshold
    display.verdict = {"talking": detection.p >= threshold}
    if display.verdict["talking"]:
        cheese_type = wave.result("cheese_type")
        sentiment = wave.result("feeling")
        display.verdict.update(
            kind=cheese_type.value if cheese_type.p_option(cheese_type.value) >= threshold else "unknown type",
            feeling=sentiment.top if sentiment.p >= threshold else "Neutral",
            score=sentiment.value,
            p=detection.p,
        )
    display.render()


async def tune_threshold(display):
    """--debug: ↑/↓ (or +/-) move the confidence threshold; every change
    re-decides the latest wave."""
    reader = cheese_bars.Keys()
    try:
        await reader.start()
        async for key in reader:
            if key == "ctrl-c":
                raise KeyboardInterrupt
            threshold = cheese_bars.adjust(key, display.threshold)
            if threshold is None:
                continue
            display.threshold = threshold
            decide(display)
    finally:
        reader.close()


async def send_audio(mic, session, to_sense):
    """Stream the mic into the session; in mic-only mode the chunks only feed
    the level meter (the level updates inside ``mic.chunks()``)."""
    if to_sense:
        session.attach(audio=mic.chunks())  # PCM16, mono, 24 kHz; one 40 ms block
        return
    async for _chunk in mic.chunks():
        pass


async def receive_decisions(waves, session, display):
    async for wave in waves:
        show_wave(wave, session, display)


async def main(args) -> int:
    display = Display(debug=args.debug)
    sense_log = logging.getLogger("sense")
    sense_log.addHandler(LogTail(display))
    sense_log.setLevel(logging.INFO)
    mic = MicStream(display, args.device)
    session = app.session()
    if not args.mic_only and not session.api_key:
        print("Set URUN_API_KEY, or use --mic-only to test audio first.", file=sys.stderr)
        return 1
    await describe_microphone(args.device)

    tasks = []
    waves = session.run()
    try:
        display.live.start()
        await mic.start()
        tasks = [asyncio.create_task(refresh_status(display, None if args.mic_only else session)),
                 asyncio.create_task(send_audio(mic, session, not args.mic_only))]
        if args.debug:
            tasks.append(asyncio.create_task(tune_threshold(display)))
        if args.mic_only:
            display.sense_state = "disabled"
        else:
            tasks.append(asyncio.create_task(receive_decisions(waves, session, display)))
        async with asyncio.timeout(300):
            await asyncio.gather(*tasks)
    except (SenseUnavailable, RuntimeError) as exc:
        display.sense_state = error_state(exc)
        display.speaking = None
        display.diagnostic = str(exc)
        display.render()
        return 1
    except TimeoutError:
        display.diagnostic = "[demo] 5 min cap reached — stopping"
        display.render()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await cleanup(tasks, mic, waves, session, display)
    print(f"{GREY}stats:{RESET} {session.stats}")
    return 0


def error_state(exc):
    """Map a fatal error to the display state that describes it."""
    if isinstance(exc, MicrophoneError):
        return "mic_error"
    if isinstance(exc, SenseUnavailable):
        return "offline"
    return "error"


async def cleanup(tasks, mic, waves, session, display):
    """Cancel the demo tasks, then close the mic and session; never raises
    short of cancellation, and always leaves the terminal display stopped."""
    interrupted = False
    try:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        # A second Ctrl-C landed during task teardown; uncancel so the
        # closers below still get to run.
        interrupted = True
        asyncio.current_task().uncancel()
    for close in (mic.stop, waves.aclose, session.close):
        # A second Ctrl-C can interrupt a closer; uncancel and retry it
        # once so nothing is left half-closed.
        for _ in (1, 2):
            try:
                await asyncio.wait_for(close(), timeout=2.0)
                break
            except asyncio.CancelledError:
                interrupted = True
                asyncio.current_task().uncancel()
            except Exception:
                break
    display.live.stop()
    if interrupted:
        raise asyncio.CancelledError


async def refresh_status(display, session=None, interval=0.1):
    """Demo diagnostics via private SDK fields; the SDK has no public
    connection-status accessor."""
    last_diagnostic = time.monotonic()
    waiting_wire = None    # wire the wait timer tracks
    waiting_since = None
    while True:
        if session is not None:
            wire = session._wire
            now = time.monotonic()
            if wire is None or wire.closed or wire is not display.wire:
                display.last_wave_ts = 0.0
                display.wire = None
                display.verdict = None
                display.speaking = None
                if wire is not None and not wire.closed:
                    # Tie the wait window to this wire so a reconnect restarts it.
                    if waiting_wire is not wire:
                        waiting_wire = wire
                        waiting_since = now
                    display.sense_state = "stalled" if now - waiting_since >= 15 else "waiting"
                else:
                    waiting_wire = None
                    waiting_since = None
                    display.sense_state = "connecting"
            else:
                waiting_wire = None
                waiting_since = None
                display.sense_state = "stalled" if now - display.last_wave_ts >= 15 else "online"
            if now - max(display.last_wave_ts, last_diagnostic) >= 15:
                pending = sum(len(q) for q in session._audio.values())
                display.diagnostic = (
                    f"[diagnostic] No complete Sense wave for 15s: "
                    f"mic_blocks={display.blocks}, queued_audio_blocks={pending}, "
                    f"turn_open={session._turn_open}, {session.stats}"
                )
                last_diagnostic = now
        display.render()
        await asyncio.sleep(interval)


# -- Terminal presentation (no Sense calls below here) -----------------------
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
GREEN = "\x1b[32m"
RED = "\x1b[31m"
YELLOW = "\x1b[33m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"
GREY = "\x1b[90m"
RESET = "\x1b[0m"


class MicrophoneError(RuntimeError):
    """Audio capture could not start or stopped delivering data."""


class LogTail(logging.Handler):
    """Route SDK log lines into the display's tail."""

    def __init__(self, display) -> None:
        super().__init__()
        self.display = display

    def emit(self, record) -> None:
        self.display.log.append(record.getMessage())


class Display:
    """Title/connection, aligned results, then diagnostics and logs, updated in place."""

    def __init__(self, debug: bool = False) -> None:
        self.level = 0.0          # mic peak, 0..1 (purely client-side)
        self.blocks = 0           # audio blocks received from the mic driver
        self.verdict = None       # latest wave dict or None
        self.speaking = None      # unknown until Sense returns a speech result
        self.wave_id = -1
        self.sense_state = "connecting"   # connecting | waiting | online | stalled | offline | disabled
        self.last_wave_ts = 0.0           # monotonic time of the latest wave
        self.wire = None                   # wire on which a complete result was received
        self.diagnostic = ""
        self.log = deque(maxlen=3)         # last SDK log lines, shown below the diagnostic
        self.live = Live(auto_refresh=False)
        self.debug = debug       # --debug: probability bars + threshold tuning
        self.wave = None         # latest wave, re-decided when the threshold moves
        self.threshold = 0.33    # confidence cut for the talking/kind/feeling decisions

    def debug_entries(self):
        """(label, result) pairs for the --debug bars panel."""
        if self.wave is None:
            return []
        return [(label, self.wave.result(qid)) for label, qid in DEBUG_QUESTIONS]

    def _sense_indicator(self):
        """The Sense connection state shown after the title."""
        if self.sense_state == "connecting":
            indicator = f"{YELLOW}● connecting to proxy{RESET}"
        elif self.sense_state == "waiting":
            indicator = f"{YELLOW}● proxy connected · waiting for sense{RESET}"
        elif self.sense_state == "stalled":
            if self.last_wave_ts == 0.0:
                indicator = f"{YELLOW}● Sense not responding · no results received{RESET}"
            else:
                age = time.monotonic() - self.last_wave_ts
                indicator = f"{YELLOW}● Sense not responding · last result {age:.0f}s ago{RESET}"
        elif self.sense_state == "offline":
            indicator = f"{RED}● sense offline{RESET}"
        elif self.sense_state == "mic_error":
            indicator = f"{RED}● mic unavailable{RESET}"
        elif self.sense_state == "error":
            indicator = f"{RED}● demo stopped{RESET}"
        elif self.sense_state == "disabled":
            indicator = f"{GREY}sense disabled (mic test){RESET}"
        else:
            age = time.monotonic() - self.last_wave_ts
            indicator = f"{GREEN}● Sense connected{RESET} {GREY}· last result {age:.0f}s ago{RESET}"
        return indicator

    def render(self) -> None:
        def field(ansi, width):
            # Measure terminal cells, not Python characters (emojis may span two).
            text = Text.from_ansi(ansi)
            text.truncate(width, overflow="ellipsis", pad=True)
            return text

        # Logarithmic display only; leave the captured PCM unchanged.
        db = 20 * math.log10(self.level) if self.level > 0 else -math.inf
        steps = " ▁▂▃▄▅▆▇█"
        filled = max(0.0, min(16.0, (db + 60.0) / 60.0 * 16.0))
        cells = []
        for i in range(16):
            frac = min(1.0, max(0.0, filled - i))
            cells.append(steps[min(8, int(frac * 8))])
        bar = "".join(cells)
        bar_col = RED if db >= -6 else YELLOW if db >= -12 else GREEN if db > -60 else GREY
        mic_state = ("speech unknown" if self.speaking is None else
                     "🎙️ talking" if self.speaking else "· no speech")
        sense = self._sense_indicator()

        if self.sense_state in ("mic_error", "error", "offline"):
            body = f"{GREY}stopped{RESET}"
        elif self.verdict is None:
            body = f"{GREY}listening…{RESET}"
        elif self.verdict["talking"]:
            k, f = self.verdict["kind"], self.verdict["feeling"]
            # The score's levels are description labels now (Jev's criteria).
            col = GREEN if f in ("Likes it", "Loves it") else RED if f in ("Disgusted", "Dislikes it") else YELLOW
            emoji = FEELING_EMOJI.get(f, "🧀😐")
            sentiment = {
                "Disgusted": "you hate it",
                "Dislikes it": "you dislike it",
                "Likes it": "you like it",
                "Loves it": "you love it",
            }.get(f, "neutral sentiment")
            body = Text.assemble(
                Text.from_ansi(f"{BOLD}{MAGENTA}🧀 CHEESE{RESET} {DIM}·{RESET} "),
                field(f"{BOLD}{CYAN}{k}{RESET}", 12),
                Text.from_ansi(f" {DIM}({self.verdict['p']:.2f}) ·{RESET} "),
                field(f"{col}{emoji}{RESET}", 4),
                " ", field(f"{col}{sentiment}{RESET}", 17),
                Text.from_ansi(f" {DIM}({self.verdict.get('score', 0):.1f}){RESET}"),
            )
        else:
            body = f"{GREY}— not cheese (null){RESET}"

        separator = Text.from_ansi(f"  {DIM}·{RESET}  ")
        header = Text.from_ansi(f"  {BOLD}🧀 Sense cheese demo{RESET}  {DIM}·{RESET}  {sense}")
        header.no_wrap = True
        header.overflow = "ellipsis"
        status = Text.assemble(
            field(f"  mic |{bar_col}{bar}{RESET}| {db:5.1f} dBFS {GREY}[{self.blocks}b]{RESET}", 46),
            separator, field(mic_state, 14), "  ",
            body if isinstance(body, Text) else Text.from_ansi(body),
            no_wrap=True, overflow="ellipsis",
        )
        log_lines = [Text(line, style="dim") for line in self.log]
        renderables = [header, status]
        if self.debug:
            bars = cheese_bars.panel(self.debug_entries(), self.threshold)
            if bars is not None:
                renderables.append(bars)
        renderables.append(Text(self.diagnostic, style="dim"))
        renderables.extend(log_lines)
        self.live.update(Group(*renderables), refresh=True)


# -- Microphone input: identical PCM blocks on every platform -----------------
def portable_audio():
    """PortAudio is bundled with sounddevice's macOS and Windows wheels."""
    try:
        import sounddevice
        return sounddevice
    except Exception as exc:  # Import also initializes PortAudio and can fail on device access.
        raise MicrophoneError(
            f'Microphone support could not load: {exc}. Install with: python -m pip install "sounddevice>=0.5"'
        ) from exc


def mic_permission_hint():
    """Some platforms, such as macOS, require per-app permission for
    microphone use but surface a denial as a generic capture-start error,
    so name the likely fix when starting capture fails on one of them."""
    if sys.platform != "darwin":
        return ""
    return (" Also allow microphone access for your terminal app: "
            "System Settings → Privacy & Security → Microphone, then restart the terminal.")


class MicStream:
    """Capture mono PCM16 at 24 kHz; platform code stays out of the Sense demo."""

    def __init__(self, display: Display, device: str) -> None:
        self.display = display
        self.device = device
        self.process = None
        self.stream = None
        self.queue = asyncio.Queue()

    async def start(self) -> None:
        if not USE_PIPEWIRE:
            sd = portable_audio()
            device = None if self.device == "auto" else int(self.device) if self.device.isdecimal() else self.device
            loop = asyncio.get_running_loop()

            def on_audio(data, frames, timing, status):
                # PortAudio owns the buffer and calls from another thread: copy it.
                item = MicrophoneError(f"Microphone capture failed: {status}") if status else bytes(data)
                loop.call_soon_threadsafe(self.queue.put_nowait, item)

            def on_finished():
                # The finished callback must return None (cffi void): returning
                # call_soon_threadsafe's handle makes PortAudio print
                # "callback with the return type 'void' must return None".
                loop.call_soon_threadsafe(self.queue.put_nowait, None)

            try:
                host = sd.query_hostapis(sd.query_devices(device, "input")["hostapi"])["name"]
                settings = sd.WasapiSettings(auto_convert=True) if host == "Windows WASAPI" else None
                self.stream = sd.RawInputStream(
                    device=device, samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                    blocksize=BLOCKSIZE, extra_settings=settings, callback=on_audio,
                    finished_callback=on_finished,
                )
                self.stream.start()
            except (sd.PortAudioError, ValueError) as exc:
                raise MicrophoneError(f"Microphone could not start: {exc}{mic_permission_hint()}") from exc
            return
        try:
            self.process = await asyncio.create_subprocess_exec(
                "pw-record", "--target", self.device, "--raw",
                "--rate", str(SAMPLE_RATE), "--channels", "1", "--format", "s16", "-",
                stdout=asyncio.subprocess.PIPE, start_new_session=True,
            )
        except OSError as exc:
            raise MicrophoneError(f"Microphone could not start: {exc}") from exc

    async def chunks(self):
        while True:
            try:
                chunk = await asyncio.wait_for(
                    self.process.stdout.readexactly(BLOCKSIZE * 2) if USE_PIPEWIRE else self.queue.get(),
                    timeout=5.0,
                )
            except asyncio.TimeoutError as exc:
                raise MicrophoneError("Mic delivered no audio for 5s; try --device with another source.") from exc
            except asyncio.IncompleteReadError as exc:
                raise MicrophoneError("Microphone stopped; check the selected source.") from exc
            if chunk is None:
                raise MicrophoneError("Microphone stopped; check the selected source.")
            if isinstance(chunk, MicrophoneError):
                raise chunk
            self.display.blocks += 1
            peak = max(abs(s) for s in array("h", chunk)) / 32768.0
            self.display.level = max(peak, self.display.level * 0.7)
            yield chunk

    async def stop(self) -> None:
        if self.stream is not None:
            self.stream.close()
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()


async def describe_microphone(device):
    print(f"Microphone source: {device}")
    if USE_PIPEWIRE and device == "auto":
        info = await asyncio.create_subprocess_exec("wpctl", "inspect", "@DEFAULT_AUDIO_SOURCE@", stdout=asyncio.subprocess.PIPE)
        try:
            output, _ = await asyncio.wait_for(info.communicate(), timeout=5.0)
        except asyncio.TimeoutError:
            # Don't let a stalled PipeWire setup block startup.
            with suppress(ProcessLookupError):
                info.terminate()
            await info.wait()  # reap so wpctl does not linger
            return
        for line in output.decode().splitlines():
            if "node.description" in line or "node.name" in line:
                print(line.strip())


def select_microphone() -> str:
    choices = [("auto", "System default")]
    if USE_PIPEWIRE:
        result = subprocess.run(["pw-dump"], capture_output=True, text=True,
                                check=True, timeout=5)
        # Skip unnamed sources; they cannot be selected as devices.
        sources = [props for node in json.loads(result.stdout)
                   if (props := node.get("info", {}).get("props", {})).get("media.class") == "Audio/Source"
                   and "node.name" in props]
        sources.sort(key=lambda p: p.get("node.description", p["node.name"]))
        choices.extend((p["node.name"], p.get("node.description", p["node.name"])) for p in sources)
    else:
        sd = portable_audio()
        try:
            choices.extend((str(i), d["name"]) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"])
        except sd.PortAudioError as exc:
            raise MicrophoneError(f"Microphone setup failed: {exc}") from exc
    print("\nSelect microphone (listed devices may be asleep):")
    for i, (_, label) in enumerate(choices):
        print(f"  {i}. {label}" + (" [default]" if i == 0 else ""))
    while True:
        answer = input("Microphone [0] (q to quit): ").strip()
        if answer.lower() == "q":
            raise SystemExit(0)
        if not answer:
            return choices[0][0]
        if answer.isdecimal() and int(answer) < len(choices):
            return choices[int(answer)][0]
        print(f"Choose a number from 0 to {len(choices) - 1}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device",
                        help="Skip the menu with a device name or ID; auto uses system default")
    parser.add_argument("--mic-only", action="store_true", help="Test microphone without connecting to Sense")
    parser.add_argument("--debug", action="store_true",
                        help="Show every answer's probability as horizontal bars and tune the decision threshold live (arrow keys)")
    args = parser.parse_args()
    if USE_PIPEWIRE:
        os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        if args.device is None:
            args.device = select_microphone() if sys.stdin.isatty() else "auto"
        raise SystemExit(asyncio.run(main(args)))
    except (KeyboardInterrupt, EOFError, asyncio.CancelledError):
        raise SystemExit(130) from None
    except (MicrophoneError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        parser.exit(1, f"Microphone setup failed: {exc}\n")

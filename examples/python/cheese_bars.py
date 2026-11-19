"""Debug-mode bars and live threshold tuning for the cheese demo.

The demo's ``--debug`` flag draws every answer's probability as a
horizontal bar — one row per answer, the decision threshold marked on the
bar — and reads the key presses (arrows or +/-) that tune the threshold
while the demo runs. The drawing and key handling live here so the demo
file itself carries no UI code.
"""

from __future__ import annotations

import asyncio
import os
import sys

from rich.console import Group
from rich.text import Text

__all__ = ["Keys", "adjust", "bar", "panel", "parse_keys"]

STEP = 0.05           # threshold change per key press
BAR_WIDTH = 32        # bar cells spanning probability 0.0 .. 1.0
LABEL_WIDTH = 12      # answer-label column width
EIGHTHS = " ▏▎▍▌▋▊▉"   # partial blocks for a bar's fractional tail cell
ARROWS = {"A": "up", "B": "down", "C": "right", "D": "left"}


def adjust(key, threshold):
    """The threshold after a tuning key (arrows or +/-), or None for any
    other key. Clamped to [0, 1]."""
    delta = {"up": STEP, "+": STEP, "down": -STEP, "-": -STEP}.get(key)
    if delta is None:
        return None
    return min(1.0, max(0.0, round(threshold + delta, 2)))


def panel(entries, threshold):
    """Bars for every answer of every question, threshold marked on each.

    ``entries`` are (question label, result) pairs; results are duck-typed
    on the SDK surface: ``distribution`` (answer -> probability), ``top``
    (the selected answer), ``p`` (its probability), ``kind`` ('noul' |
    'choice' | 'score').
    """
    if not entries:
        return None
    header = Text(f"  confidence threshold {threshold:.2f} · ↑/↓ tune live", style="dim")
    return Group(header, *(section(label, result, threshold) for label, result in entries))


def section(label, result, threshold):
    """One question: its selected answer, then a bar per possible answer."""
    header = Text(no_wrap=True)
    header.append(f"  {label}", style="bold")
    header.append(f" → {result.top}", style="cyan")
    p_top = result.p if result.p is not None else 0.0
    header.append(f" ({p_top:.2f})", style="dim")
    return Group(header, *(row(answer, p, str(answer) == str(result.top), threshold)
                           for answer, p in answers(result)))


def answers(result):
    """Every answer's probability. Scores keep their level order (low end
    first), choices rank by probability, noul keeps its true/false order."""
    distribution = result.distribution or {str(result.top): result.p or 0.0}
    if getattr(result, "kind", "") == "choice":
        return sorted(distribution.items(), key=lambda item: item[1], reverse=True)
    return list(distribution.items())


def row(answer, p, winner, threshold):
    """One bar line: label, bar, probability."""
    text = Text(no_wrap=True)
    text.append("    ")
    text.append(answer[:LABEL_WIDTH - 1].ljust(LABEL_WIDTH), style="bold" if winner else None)
    text.append(" ")
    text.append(bar(p, threshold, winner))
    text.append(f" {p:4.2f}", style="bold" if winner else "dim")
    return text


def bar(p, threshold, winner):
    """One horizontal bar: fill to p over a dotted track, with the decision
    threshold drawn as a cut across the bar."""
    fill = p * BAR_WIDTH
    whole = int(fill)
    fraction = fill - whole
    cut = min(BAR_WIDTH - 1, round(threshold * BAR_WIDTH))
    text = Text(no_wrap=True)
    for i in range(BAR_WIDTH):
        if i == cut:  # the threshold marker, drawn over bar or track
            text.append("┃", style="magenta")
        elif i < whole:
            text.append("█", style="cyan" if winner else "grey37")
        elif i == whole and fraction >= 1 / 8:
            text.append(EIGHTHS[min(7, round(fraction * 8))],
                        style="cyan" if winner else "grey37")
        else:
            text.append("·", style="bright_black")
    return text


def parse_keys(buf):
    """Drain a byte buffer into key events; a partial escape sequence stays
    buffered until its last byte arrives."""
    events = []
    while buf:
        if buf[0] == 0x1B:  # ESC
            if len(buf) >= 3 and buf[1] in (0x5B, 0x4F):  # ESC [ letter: an arrow
                events.append(ARROWS.get(chr(buf[2]), "esc"))
                del buf[:3]
            elif len(buf) == 1:
                break  # lone ESC so far: wait to see if a sequence follows
            elif buf[1] in (0x5B, 0x4F):
                break  # ESC [ arrived, the final letter is still pending
            else:  # a lone Escape: unused by the demo, drop it
                del buf[0]
            continue
        events.append(_name(buf.pop(0)))
    return events


def _name(byte):
    if byte == 0x03:
        return "ctrl-c"
    if 32 <= byte < 127:
        return chr(byte)
    return f"\\x{byte:02x}"


class Keys:
    """Async iterator over normalized key events for threshold tuning.

    POSIX terminals are switched to cbreak for the reader's lifetime; Ctrl-C
    keeps raising KeyboardInterrupt because ISIG stays enabled. close() is
    synchronous and idempotent so a cancelled tuner task still restores the
    terminal. Iteration ends at EOF.
    """

    def __init__(self):
        self._events = asyncio.Queue()
        self._restore = None   # terminal restore callable, set by start()
        self._poller = None    # Windows console poll task

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self._events.get()
        if event is None:  # EOF
            raise StopAsyncIteration
        return event

    async def start(self):
        """Begin reading keys; a no-op when stdin is not a terminal."""
        if sys.platform == "win32":
            self._poller = asyncio.create_task(self._poll_console())
        elif sys.stdin.isatty():
            self._start_posix()
        return self

    def close(self):
        """Stop reading and restore the terminal; safe to call twice."""
        if self._restore is not None:
            self._restore()
            self._restore = None
        if self._poller is not None:
            self._poller.cancel()
            self._poller = None

    def _start_posix(self):
        import termios
        import tty

        loop = asyncio.get_running_loop()
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        buf = bytearray()

        def on_readable():
            data = os.read(fd, 64)
            if not data:  # EOF: end the event stream
                loop.remove_reader(fd)
                self._events.put_nowait(None)
                return
            buf.extend(data)
            for event in parse_keys(buf):
                self._events.put_nowait(event)

        def restore():
            loop.remove_reader(fd)
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)

        self._restore = restore
        tty.setcbreak(fd)
        loop.add_reader(fd, on_readable)

    async def _poll_console(self):
        """Windows has no readability callback for the console: poll msvcrt."""
        import msvcrt

        codes = {"H": "up", "P": "down", "K": "left", "M": "right"}
        while True:
            await asyncio.sleep(0.05)
            while msvcrt.kbhit():
                key = msvcrt.getwch()
                if key in ("\x00", "\xe0"):  # special key: the code follows
                    key = codes.get(msvcrt.getwch(), "")
                elif key == "\x03":  # getch swallows the console interrupt
                    key = "ctrl-c"
                if key:
                    self._events.put_nowait(key)

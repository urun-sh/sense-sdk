"""Offline tests for examples/python/cheese_demo.py, loaded from its repo
location: the demo's import-time work (Sense() plus question registration)
is pure in-memory state, so loading it here is safe and offline."""

import asyncio
import importlib.util
import io
import logging
import pathlib
import sys
from collections import deque
from types import SimpleNamespace

import pytest
from rich.cells import cell_len
from rich.console import Console, Group

DEMO_PATH = pathlib.Path(__file__).resolve().parents[2] / "examples" / "python" / "cheese_demo.py"
BARS_PATH = DEMO_PATH.with_name("cheese_bars.py")


@pytest.fixture(scope="module")
def demo():
    # The demo imports its bars helper as a sibling module (cheese_bars.py
    # next to it in the repo); bind that name to the source file so these
    # tests never run a stale staged copy.
    spec = importlib.util.spec_from_file_location("cheese_bars", BARS_PATH)
    bars = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bars
    spec.loader.exec_module(bars)
    spec = importlib.util.spec_from_file_location("cheese_demo", DEMO_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeResult:
    def __init__(self, value, p, options=None, top=None, kind="choice"):
        self.value = value
        self.p = p
        self.options = options or {}
        self.top = top if top is not None else (value if isinstance(value, str) else None)
        self.distribution = self.options
        self.kind = kind

    def p_option(self, option):
        return self.options.get(option, 0.0)


class FakeScore(FakeResult):
    """A score result: .value is the expected ordinal, .top the argmax level."""

    def __init__(self, top, p, score):
        super().__init__(score, p, top=top, kind="score")
        self.level = top


class FakeWave:
    def __init__(self, results, wave_id=7):
        self.wave_id = wave_id
        self._results = results

    def result(self, qid):
        return self._results[qid]


class FakeSession:
    # The real SDK names the live wire `session._wire` (a _transport.Wire);
    # this stub must mirror the SDK, not whatever the demo reads.
    _wire = object()


class FakeDisplay:
    def __init__(self):
        self.sense_state = "connecting"
        self.last_wave_ts = 0.0
        self.wire = None
        self.diagnostic = ""
        self.wave_id = -1
        self.speaking = None
        self.verdict = None
        self.wave = None
        self.threshold = 0.6
        self.rendered = 0

    def render(self):
        self.rendered += 1


def wave(**overrides):
    """A confident talking-about-brie-and-loving-it wave; override any field."""
    fields = {
        "speech": True,
        "cheese_p": 0.9,
        "kind": "brie",
        "kind_p": 0.9,
        "feeling": "Loves it",
        "feeling_p": 0.9,
        "feeling_score": 3.9,
    }
    fields.update(overrides)
    speech_p = 0.9 if fields["speech"] else 0.1
    cheese_p = fields["cheese_p"]
    return FakeWave({
        "speech": FakeResult(fields["speech"], speech_p,
                             {"true": speech_p, "false": 1 - speech_p},
                             top="true" if fields["speech"] else "false", kind="noul"),
        "cheese": FakeResult(cheese_p >= 0.6, cheese_p,
                             {"true": cheese_p, "false": 1 - cheese_p},
                             top="true" if cheese_p >= 0.6 else "false", kind="noul"),
        "cheese_type": FakeResult(fields["kind"], fields["kind_p"], {fields["kind"]: fields["kind_p"]}),
        "feeling": FakeScore(fields["feeling"], fields["feeling_p"], fields["feeling_score"]),
    })


# ---------------------------------------------------------------------------
# show_wave: the wave -> verdict mapping
# ---------------------------------------------------------------------------


def test_show_wave_reports_talking_with_kind_and_feeling(demo):
    display = FakeDisplay()
    demo.show_wave(wave(), FakeSession(), display)
    assert display.sense_state == "online"
    assert display.speaking is True
    assert display.verdict == {"talking": True, "kind": "brie", "feeling": "Loves it", "score": 3.9, "p": 0.9}
    assert display.wave_id == 7
    assert display.wire is FakeSession._wire
    assert display.rendered == 1


def test_show_wave_threshold_is_inclusive(demo):
    display = FakeDisplay()
    demo.show_wave(wave(cheese_p=0.6), FakeSession(), display)
    assert display.verdict["talking"] is True
    display = FakeDisplay()
    demo.show_wave(wave(cheese_p=0.59), FakeSession(), display)
    assert display.verdict == {"talking": False}


def test_show_wave_nulls_unconfident_kind_and_feeling(demo):
    display = FakeDisplay()
    demo.show_wave(wave(kind_p=0.3, feeling_p=0.3), FakeSession(), display)
    assert display.verdict["kind"] == "unknown type"
    assert display.verdict["feeling"] == "Neutral"


def test_show_wave_null_verdict_when_not_speaking(demo):
    display = FakeDisplay()
    demo.show_wave(wave(speech=False), FakeSession(), display)
    assert display.sense_state == "online"
    assert display.speaking is False


def test_decide_retunes_verdict_at_a_new_threshold(demo):
    display = FakeDisplay()
    demo.show_wave(wave(cheese_p=0.62), FakeSession(), display)
    assert display.verdict["talking"] is True
    display.threshold = 0.65
    demo.decide(display)
    assert display.verdict == {"talking": False}


def test_decide_with_no_wave_is_a_no_op(demo):
    demo.decide(FakeDisplay())  # must not raise before the first wave


class FakeKeys:
    """A Keys stand-in that replays queued key events and records close()."""

    def __init__(self, events):
        self.events = list(events)
        self.closed = False

    async def start(self):
        return self

    def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        return self.events.pop(0)


def test_tune_threshold_redecides_live(demo, monkeypatch):
    display = FakeDisplay()
    demo.show_wave(wave(cheese_p=0.64), FakeSession(), display)
    assert display.verdict["talking"] is True   # 0.64 >= the fake's 0.6 threshold
    keys = FakeKeys(["up", "up", "down"])
    monkeypatch.setattr(demo.cheese_bars, "Keys", lambda: keys)
    asyncio.run(demo.tune_threshold(display))
    assert keys.closed is True                  # the reader is always cleaned up
    assert display.threshold == 0.65            # +0.05 +0.05 -0.05
    assert display.verdict == {"talking": False}  # 0.64 < 0.65: decision flipped


def test_tune_threshold_raises_on_ctrl_c(demo, monkeypatch):
    display = FakeDisplay()
    demo.show_wave(wave(cheese_p=0.9), FakeSession(), display)
    keys = FakeKeys(["ctrl-c"])
    monkeypatch.setattr(demo.cheese_bars, "Keys", lambda: keys)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(demo.tune_threshold(display))
    assert keys.closed is True


def test_error_state_maps_exception_types(demo):
    assert demo.error_state(demo.MicrophoneError("boom")) == "mic_error"
    assert demo.error_state(demo.SenseUnavailable("boom")) == "offline"
    assert demo.error_state(RuntimeError("boom")) == "error"


class FakeSounddevice:
    """Just enough PortAudio surface for MicStream.start to fail at stream open."""

    class PortAudioError(Exception):
        pass

    @staticmethod
    def query_devices(device, kind):
        return {"hostapi": 0}

    @staticmethod
    def query_hostapis(index):
        return {"name": "Core Audio"}

    @staticmethod
    def RawInputStream(**kwargs):
        raise FakeSounddevice.PortAudioError(
            "Error opening RawInputStream: Internal PortAudio error [PaErrorCode -9986]"
        )


def test_mic_start_failure_hints_macos_permissions(demo, monkeypatch):
    monkeypatch.setattr(demo, "USE_PIPEWIRE", False)
    monkeypatch.setattr(demo, "portable_audio", lambda: FakeSounddevice)
    mic = demo.MicStream(SimpleNamespace(), "auto")

    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(demo.MicrophoneError) as excinfo:
        asyncio.run(mic.start())
    assert "Privacy & Security → Microphone" in str(excinfo.value)

    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(demo.MicrophoneError) as excinfo:
        asyncio.run(mic.start())
    assert "Privacy" not in str(excinfo.value)


def test_finished_callback_returns_none(demo, monkeypatch):
    """cffi declares the PortAudio finished callback void: a return value makes
    PortAudio print 'callback ... must return None' when the stream ends."""
    captured = {}

    class RecordingSounddevice(FakeSounddevice):
        @staticmethod
        def RawInputStream(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(start=lambda: None)

    monkeypatch.setattr(demo, "USE_PIPEWIRE", False)
    monkeypatch.setattr(demo, "portable_audio", lambda: RecordingSounddevice)
    mic = demo.MicStream(SimpleNamespace(), "auto")

    async def start_and_fire():
        await mic.start()
        assert captured["finished_callback"]() is None

    asyncio.run(start_and_fire())


# ---------------------------------------------------------------------------
# cleanup: survives a second Ctrl-C, closes everything either way
# ---------------------------------------------------------------------------


def cleanup_args(closed, gate=None):
    """Fakes shaped like the real resources: mic.stop, waves.aclose, session.close."""
    display = SimpleNamespace(live=SimpleNamespace(stop=lambda: closed.append("live")))

    def closer(name):
        async def close():
            if gate is not None:  # stay suspended until the test releases us
                await gate.wait()
            await asyncio.sleep(0.01)
            closed.append(name)

        return close

    mic = SimpleNamespace(stop=closer("mic"))
    waves = SimpleNamespace(aclose=closer("waves"))
    session = SimpleNamespace(close=closer("session"))
    tasks = [asyncio.create_task(asyncio.sleep(3600)) for _ in range(2)]
    return tasks, mic, waves, session, display


def test_cleanup_completes_normally(demo):
    closed = []

    async def scenario():
        args = cleanup_args(closed)
        await demo.cleanup(*args)
        return args[0]

    tasks = asyncio.run(scenario())
    assert closed == ["mic", "waves", "session", "live"]
    assert all(t.cancelled() for t in tasks)


def test_cleanup_survives_cancellation(demo):
    """A second Ctrl-C during cleanup still closes everything, then propagates."""
    closed = []
    gate = asyncio.Event()

    async def scenario():
        args = cleanup_args(closed, gate=gate)  # mic.stop blocks on the gate
        cleanup = asyncio.create_task(demo.cleanup(*args))
        await asyncio.sleep(0.05)  # cleanup is now suspended inside mic.stop
        cleanup.cancel()           # the "second Ctrl-C"
        gate.set()                 # unblock whoever still gets to run
        with pytest.raises(asyncio.CancelledError):
            await cleanup

    asyncio.run(scenario())
    assert closed == ["mic", "waves", "session", "live"]  # nothing skipped


def test_cleanup_survives_cancellation_during_task_gather(demo):
    """Same contract when the second Ctrl-C lands during the task gather."""
    closed = []
    gate = asyncio.Event()

    async def slow_death():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await gate.wait()  # the task is slow to finish dying
            raise

    async def scenario():
        _, mic, waves, session, display = cleanup_args(closed)
        tasks = [asyncio.create_task(slow_death()) for _ in range(2)]
        cleanup = asyncio.create_task(demo.cleanup(tasks, mic, waves, session, display))
        await asyncio.sleep(0.05)  # cleanup is now suspended in the task gather
        cleanup.cancel()           # the "second Ctrl-C"
        gate.set()                 # let the tasks finish dying
        with pytest.raises(asyncio.CancelledError):
            await cleanup

    asyncio.run(scenario())
    assert closed == ["mic", "waves", "session", "live"]  # nothing skipped


def test_describe_microphone_bounds_wpctl_wait(demo, monkeypatch, capsys):
    """A hung wpctl must not block startup: terminate, reap, and continue."""
    if not demo.USE_PIPEWIRE:
        pytest.skip("wpctl path is Linux-only")

    class HungProcess:
        def __init__(self):
            self.terminated = False
            self.reaped = False

        async def communicate(self):
            await asyncio.sleep(3600)

        def terminate(self):
            self.terminated = True

        async def wait(self):
            self.reaped = True

    process = HungProcess()

    async def fake_exec(*args, **kwargs):
        return process

    monkeypatch.setattr(demo.asyncio, "create_subprocess_exec", fake_exec)
    # The 5s wait is real: the timeout is hardcoded in the demo.
    asyncio.run(demo.describe_microphone("auto"))
    assert process.terminated and process.reaped
    assert "Microphone source: auto" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Display.render: the single-line ANSI status
# ---------------------------------------------------------------------------


class FakeLive:
    def __init__(self):
        self.updates = []

    def update(self, renderable, refresh=False):
        self.updates.append(renderable)


def rendered_text(display):
    display.render()
    status = display.live.updates[-1].renderables[1]
    return status.plain


def display_with(demo, **attrs):
    d = demo.Display()
    d.live = FakeLive()
    for key, value in attrs.items():
        setattr(d, key, value)
    return d


def test_render_shows_listening_before_a_verdict(demo):
    text = rendered_text(display_with(demo, level=0.5, blocks=3))
    assert "listening…" in text
    assert "[3b]" in text


def test_render_shows_cheese_verdict(demo):
    verdict = {"talking": True, "kind": "brie", "feeling": "Loves it", "score": 3.9, "p": 0.9}
    text = rendered_text(display_with(demo, level=0.5, blocks=3, verdict=verdict, speaking=True))
    assert "CHEESE" in text
    assert "brie" in text
    assert "you love it" in text
    assert "❤️" in text
    assert "3.9" in text


def test_render_shows_score_levels(demo):
    for feeling, score, emoji in [("Loves it", 3.9, "❤️"), ("Likes it", 3.1, "🙂"), ("Dislikes it", 1.2, "🙁"), ("Disgusted", 0.4, "🤮")]:
        verdict = {"talking": True, "kind": "brie", "feeling": feeling, "score": score, "p": 0.9}
        text = rendered_text(display_with(demo, level=0.5, blocks=3, verdict=verdict, speaking=True))
        assert emoji in text, f"expected {emoji} for {feeling}"
        assert f"{score:.1f}" in text


def test_render_shows_not_cheese(demo):
    verdict = {"talking": False}
    text = rendered_text(display_with(demo, level=0.5, blocks=3, verdict=verdict, speaking=False))
    assert "not cheese" in text


def test_status_fields_stay_in_place_and_never_wrap(demo):
    positions = []
    for i, (kind, feeling) in enumerate([
        ("brie", "Loves it"), ("parmesan", "Disgusted"), ("unknown type", "Neutral"),
        ("mozzarella", "Dislikes it"), ("feta", "Likes it"),
    ]):
        d = display_with(
            demo, level=0 if i == 0 else 0.5, blocks=10 ** i,
            sense_state=["connecting", "waiting", "online", "stalled", "online"][i],
            speaking=[None, True, False, True, False][i],
            verdict={"talking": True, "kind": kind, "feeling": feeling, "score": 2.3, "p": 0.9},
        )
        text = rendered_text(d)
        sentiment = {
            "Disgusted": "you hate it",
            "Dislikes it": "you dislike it",
            "Likes it": "you like it",
            "Loves it": "you love it",
        }.get(feeling, "neutral sentiment")
        positions.append(tuple(cell_len(text[:text.index(label)]) for label in
                               ("🧀 CHEESE", kind, "(0.90)", sentiment, "(2.3)")))
        for width in (80, 160):
            output = io.StringIO()
            Console(file=output, width=width).print(Group(*d.live.updates[-1].renderables[:2]))
            lines = output.getvalue().splitlines()
            assert len(lines) == 2
            assert all(cell_len(line) <= width for line in lines)
            assert "Sense cheese demo" in lines[0] and "●" in lines[0]
            assert "●" not in lines[1]
            if width == 160:
                assert all(label in lines[1] for label in (kind, "(0.90)", sentiment, "(2.3)"))
    assert all(position == positions[0] for position in positions)


def test_render_shows_error_states(demo):
    for state, expected in [("offline", "sense offline"), ("mic_error", "mic unavailable")]:
        d = display_with(demo, sense_state=state)
        text = rendered_text(d)
        assert expected in d.live.updates[-1].renderables[0].plain
        assert "stopped" in text


def test_log_tail_appends_and_render_shows_it(demo):
    d = demo.Display()
    d.live = FakeLive()
    handler = demo.LogTail(d)
    handler.emit(logging.LogRecord("sense", logging.WARNING, "p", 1, "dial 1 refused", (), None))
    assert list(d.log) == ["dial 1 refused"]
    d.render()
    texts = [r.plain for r in d.live.updates[-1].renderables]
    assert "dial 1 refused" in texts[3]


def test_log_tail_keeps_last_three_lines(demo):
    d = demo.Display()
    d.live = FakeLive()
    handler = demo.LogTail(d)
    for i in range(1, 5):
        handler.emit(logging.LogRecord("sense", logging.WARNING, "p", 1, f"line {i}", (), None))
    assert list(d.log) == ["line 2", "line 3", "line 4"]


def test_render_keeps_header_then_results_then_diagnostics_and_log(demo):
    d = display_with(demo, level=0.5, blocks=3)
    d.diagnostic = "diagnostic"
    d.log = deque(["line 1", "line 2"])
    d.render()
    texts = [r.plain for r in d.live.updates[-1].renderables]
    assert "Sense cheese demo" in texts[0]
    assert texts[1].startswith("  mic |")
    assert texts[2:] == ["diagnostic", "line 1", "line 2"]


def test_render_debug_inserts_bars_between_status_and_diagnostics(demo):
    d = display_with(demo, debug=True, level=0.5, blocks=3)
    d.wave = wave()
    d.diagnostic = "diagnostic"
    d.render()
    panel = d.live.updates[-1].renderables[2]
    assert panel.renderables[0].plain.startswith(f"  confidence threshold {d.threshold:.2f}")
    sections = {group.renderables[0].plain.split(" →")[0].strip(): group
                for group in panel.renderables[1:]}
    assert set(sections) == {"speech", "cheese", "cheese type", "feeling"}
    bar_rows = [row.plain for group in sections.values() for row in group.renderables[1:]]
    assert all("┃" in row for row in bar_rows)  # the threshold marker on every bar
    assert d.live.updates[-1].renderables[3].plain == "diagnostic"

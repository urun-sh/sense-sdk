"""Offline tests for examples/python/cheese_bars.py: the debug bars module."""

import importlib.util
import pathlib
import sys
from types import SimpleNamespace

import pytest
from rich.console import Console

BARS_PATH = pathlib.Path(__file__).resolve().parents[2] / "examples" / "python" / "cheese_bars.py"


@pytest.fixture(scope="module")
def bars():
    spec = importlib.util.spec_from_file_location("cheese_bars", BARS_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def result(kind, distribution, top=None):
    """A stand-in for the SDK Result duck-typed surface the bars read."""
    top = top if top is not None else max(distribution, key=distribution.get)
    return SimpleNamespace(kind=kind, distribution=dict(distribution), top=top,
                            p=distribution.get(top, 0.0), value=top)


def lines(renderable):
    console = Console(width=200, record=True)
    console.print(renderable)
    return console.export_text().splitlines()


# Insertion order deliberately differs from probability order: the panel must
# rank answers by probability itself, not inherit dict order.
CHOICE = {"gouda": 0.10, "brie": 0.72, "cheddar": 0.18}
SCORE = {"Disgusted": 0.04, "Dislikes it": 0.05, "Neutral": 0.11, "Likes it": 0.30, "Delighted": 0.50}
NOUL = {"true": 0.62, "false": 0.38}


# ---------------------------------------------------------------------------
# threshold adjustment
# ---------------------------------------------------------------------------


def test_adjust_moves_the_threshold_by_one_step(bars):
    assert bars.adjust("up", 0.60) == 0.65
    assert bars.adjust("down", 0.60) == 0.55
    assert bars.adjust("+", 0.60) == 0.65
    assert bars.adjust("-", 0.60) == 0.55


def test_adjust_clamps_to_the_unit_interval(bars):
    assert bars.adjust("up", 0.98) == 1.0
    assert bars.adjust("up", 1.0) == 1.0
    assert bars.adjust("down", 0.04) == 0.0
    assert bars.adjust("down", 0.0) == 0.0


def test_adjust_ignores_non_tuning_keys(bars):
    for key in ("left", "right", "esc", "q", "\x7f", "x"):
        assert bars.adjust(key, 0.6) is None


# ---------------------------------------------------------------------------
# key parsing
# ---------------------------------------------------------------------------


def test_parse_keys_names_arrows_and_plain_keys(bars):
    assert bars.parse_keys(bytearray(b"\x1b[A\x1b[B+-")) == ["up", "down", "+", "-"]


def test_parse_keys_buffers_a_split_escape_sequence(bars):
    buf = bytearray(b"\x1b[")
    assert bars.parse_keys(buf) == []          # incomplete: stays buffered
    buf.extend(b"A")
    assert bars.parse_keys(buf) == ["up"]


def test_parse_keys_drops_a_lone_escape(bars):
    buf = bytearray(b"\x1bx")
    assert bars.parse_keys(buf) == ["x"]


def test_parse_keys_maps_ctrl_c(bars):
    assert bars.parse_keys(bytearray(b"a\x03")) == ["a", "ctrl-c"]


def test_parse_keys_passes_printables_and_names_the_rest(bars):
    assert bars.parse_keys(bytearray(b"\x7f")) == ["\\x7f"]


# ---------------------------------------------------------------------------
# panel: horizontal bars for every answer of every question
# ---------------------------------------------------------------------------


def test_panel_returns_none_without_entries(bars):
    assert bars.panel([], 0.6) is None


def test_panel_shows_every_answer_of_every_question(bars):
    entries = [("cheese type", result("choice", CHOICE)),
               ("feeling", result("score", SCORE, top="Delighted")),
               ("cheese", result("noul", NOUL))]
    text = "\n".join(lines(bars.panel(entries, 0.6)))
    assert "confidence threshold 0.60" in text
    for label in ("cheese type", "feeling", "cheese"):
        assert label in text
    for answer in ("brie", "cheddar", "gouda", "Disgusted", "Dislikes it", "Neutral",
                   "Likes it", "Delighted", "true", "false"):
        assert answer in text, f"missing an answer bar for {answer!r}"
    for p in ("0.72", "0.18", "0.50", "0.62"):
        assert p in text


def test_panel_marks_the_threshold_on_every_bar(bars):
    entries = [("cheese type", result("choice", CHOICE))]
    rows = lines(bars.panel(entries, 0.6))[2:]  # header, section header, then bars
    assert len(rows) == len(CHOICE)
    for row in rows:
        assert "┃" in row


def test_choice_answers_rank_by_probability(bars):
    text = lines(bars.panel([("cheese type", result("choice", CHOICE))], 0.6))
    row_labels = [line[4:4 + bars.LABEL_WIDTH].strip() for line in text[2:]]
    assert row_labels == ["brie", "cheddar", "gouda"]


def test_score_answers_keep_level_order(bars):
    text = lines(bars.panel([("feeling", result("score", SCORE, top="Delighted"))], 0.6))
    row_labels = [line[4:4 + bars.LABEL_WIDTH].strip() for line in text[2:]]
    assert row_labels == ["Disgusted", "Dislikes it", "Neutral", "Likes it", "Delighted"]


def test_bar_draws_the_threshold_cut_across_fill_and_track(bars):
    # fill 16 cells, cut at 0.25 -> cell 8: marker over the filled part.
    assert bars.bar(0.5, 0.25, True).plain == "█" * 8 + "┃" + "█" * 7 + "·" * 16
    # bar shorter than the threshold: marker lands on the track.
    # 0.2 -> 6 full cells + a 0.4-cell tail; cut at cell 19.
    assert bars.bar(0.2, 0.6, False).plain == "█" * 6 + "▍" + "·" * 12 + "┃" + "·" * 12
    # full-probability bar and degenerate thresholds.
    assert bars.bar(1.0, 1.0, True).plain == "█" * 31 + "┃"
    assert bars.bar(0.0, 0.0, False).plain == "┃" + "·" * 31


def test_bar_partial_fill_uses_eighth_blocks(bars):
    # 0.45 * 32 = 14.4 -> 14 full cells plus a 0.4-cell tail (▍ is 3/8).
    assert bars.bar(0.45, 0.99, True).plain.startswith("█" * 14 + "▍")


def test_row_highlights_only_the_selected_answer(bars):
    winner = bars.row("brie", 0.72, True, 0.6)
    loser = bars.row("cheddar", 0.18, False, 0.6)
    assert any(span.style == "bold" for span in winner.spans)
    assert not any(span.style == "bold" for span in loser.spans)
    bar_start = 4 + bars.LABEL_WIDTH + 1  # label column, then one space
    assert winner.plain[bar_start:bar_start + bars.BAR_WIDTH] == bars.bar(0.72, 0.6, True).plain
    assert winner.plain.endswith(" 0.72")


def test_rows_are_horizontal_bars(bars):
    entries = [("cheese type", result("choice", CHOICE))]
    rows = lines(bars.panel(entries, 0.6))[2:]
    for row in rows:
        assert row.startswith("    ")  # the bar is a full-width line, not a column

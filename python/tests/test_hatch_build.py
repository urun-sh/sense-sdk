"""Offline tests for python/hatch_build.py: staging the examples into the wheel."""

import importlib.util
import pathlib
import sys
import types

import pytest

HOOK_PATH = pathlib.Path(__file__).resolve().parents[2] / "python" / "hatch_build.py"


@pytest.fixture()
def build_hook(monkeypatch):
    # The hook subclasses a hatchling interface just so the build can load it;
    # the staging logic under test never touches that interface, so stand in
    # for it and keep the test independent of an installed build backend.
    interface = types.ModuleType("hatchling.builders.hooks.plugin.interface")
    interface.BuildHookInterface = object
    monkeypatch.setitem(sys.modules, "hatchling.builders.hooks.plugin.interface", interface)
    spec = importlib.util.spec_from_file_location("hatch_build", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def repo_with_examples(tmp_path):
    """A minimal <repo>/examples + <repo>/python layout like the checkout."""
    examples = tmp_path / "examples"
    (examples / "python").mkdir(parents=True)
    (examples / "python" / "cheese_demo.py").write_text(
        "import api_key_hint  # the missing-key message\n"
        "import cheese_bars  # the --debug probability bars\n"
        "print('mozzarella 🧀')\n",
        encoding="utf-8",
    )
    (examples / "python" / "cheese_bars.py").write_text("LABEL_WIDTH = 16\n", encoding="utf-8")
    (examples / "python" / "api_key_hint.py").write_text("API_KEYS_URL = 'x'\n", encoding="utf-8")
    (examples / "assets").mkdir()
    (examples / "assets" / "poster.txt").write_text("brie 🧀\n", encoding="utf-8")
    return tmp_path / "python"


def test_stage_examples_rewrites_the_sibling_import_for_the_wheel(build_hook, tmp_path):
    python_root = repo_with_examples(tmp_path)
    build_hook.stage_examples(python_root)
    staged = python_root / "src" / "sense" / "examples"
    assert (staged / "cheese_demo.py").read_text(encoding="utf-8") == (
        "from . import api_key_hint  # the missing-key message\n"
        "from . import cheese_bars  # the --debug probability bars\n"
        "print('mozzarella 🧀')\n"
    )
    assert (staged / "cheese_bars.py").read_text(encoding="utf-8") == "LABEL_WIDTH = 16\n"
    assert (staged / "api_key_hint.py").read_text(encoding="utf-8") == "API_KEYS_URL = 'x'\n"
    assert (staged / "assets" / "poster.txt").read_text(encoding="utf-8") == "brie 🧀\n"


def test_stage_examples_leaves_an_sdist_tree_already_staged(build_hook, tmp_path):
    python_root = tmp_path / "python"  # no sibling examples/: an sdist build
    staged = python_root / "src" / "sense" / "examples"
    staged.mkdir(parents=True)
    (staged / "cheese_demo.py").write_text("from . import cheese_bars\n", encoding="utf-8")
    build_hook.stage_examples(python_root)
    assert (staged / "cheese_demo.py").read_text(encoding="utf-8") == "from . import cheese_bars\n"

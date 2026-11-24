"""Stage the repo-root examples into the package tree before every build.

The committed source of truth lives at <repo>/examples/{python,assets}; the
published wheel has always shipped them as `sense/examples`, so the build
copies them into src/ first. Building from an sdist finds no ../examples
(the files are already inside src/), so this is a no-op there.
"""

import shutil
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


def stage_examples(python_root: Path) -> None:
    """Stage <repo>/examples into python_root/src/sense/examples."""
    examples = python_root.parent / "examples"
    if not (examples / "python").is_dir():
        return  # building from an sdist: the examples are already staged
    dest = python_root / "src" / "sense" / "examples"
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(examples / "python", dest, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(examples / "assets", dest / "assets")
    # The demo imports its sibling helpers as plain modules; staged inside
    # the package those helpers are submodules, so the imports need the
    # relative form.
    demo = dest / "cheese_demo.py"
    demo.write_text(
        "\n".join(
            line.replace("import cheese_bars", "from . import cheese_bars", 1)
            .replace("import api_key_hint", "from . import api_key_hint", 1)
            if line.startswith(("import cheese_bars", "import api_key_hint"))
            else line
            for line in demo.read_text(encoding="utf-8").splitlines()
        )
        + "\n",
        encoding="utf-8",
    )


class ExamplesBuildHook(BuildHookInterface):
    PLUGIN_NAME = "examples"

    def initialize(self, version, build_data):
        stage_examples(Path(self.root))

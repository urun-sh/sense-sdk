"""Stage the repo-root examples into the package tree before every build.

The committed source of truth lives at <repo>/examples/{python,assets}; the
published wheel has always shipped them as `sense/examples`, so the build
copies them into src/ first. Building from an sdist finds no ../examples
(the files are already inside src/), so this is a no-op there.
"""

import shutil
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class ExamplesBuildHook(BuildHookInterface):
    PLUGIN_NAME = "examples"

    def initialize(self, version, build_data):
        root = Path(self.root)
        examples = root.parent / "examples"
        if not (examples / "python").is_dir():
            return
        dest = root / "src" / "sense" / "examples"
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(examples / "python", dest, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(examples / "assets", dest / "assets")

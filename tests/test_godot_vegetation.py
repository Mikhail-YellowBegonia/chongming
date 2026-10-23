"""Native Godot checks for procedural vegetation resources and caching."""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path

from chongming.rendering.godot import GODOT_VERSION

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "src" / "chongming" / "rendering" / "godot"
SCRIPT = ROOT / "tests" / "godot" / "test_vegetation.gd"


def godot_executable() -> str | None:
    configured = os.environ.get("CHONGMING_GODOT")
    return configured or shutil.which("godot4") or shutil.which("godot")


@unittest.skipUnless(godot_executable(), "Godot 4 executable is required")
class GodotVegetationTests(unittest.TestCase):
    def test_native_generator_and_worker_cache(self) -> None:
        executable = godot_executable()
        assert executable is not None
        version = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
        self.assertTrue(version.startswith(f"{GODOT_VERSION}.stable."), version)
        result = subprocess.run(
            [
                executable,
                "--path",
                str(PROJECT),
                "--audio-driver",
                "Dummy",
                "--rendering-method",
                "gl_compatibility",
                "--disable-render-loop",
                "--script",
                str(SCRIPT),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("vegetation tests passed", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

"""The first preview snapshot must finish without reacquiring its queue lock."""

import subprocess
import sys
import unittest


class PreviewEncodingTests(unittest.TestCase):
    def test_first_snapshot_and_cached_snapshot_complete_without_sensor_images(self) -> None:
        script = """
from chongming.preview import PreviewRuntime
from chongming.scenarios.authoring import compile_authoring, demo_authoring
from chongming.scenarios.demo import demo_source

spec = compile_authoring(demo_authoring(stations=1, duration_s=1))
runtime = PreviewRuntime(spec, video_enabled=False)
try:
    runtime._published_step = next(iter(demo_source(spec, video_enabled=False).generate(spec)))
    runtime._published = {"sequence": 1}
    first = runtime.snapshot()
    assert first["snapshot"]["video"] == []
    assert runtime._pending_encodes == {}
    second = runtime.snapshot()
    assert second["snapshot"] == first["snapshot"]
    assert runtime.snapshot(after_sequence=1)["snapshot"] is None
finally:
    runtime._png_encoder.shutdown(wait=True)
"""
        # A subprocess bounds a regression to an actual non-reentrant-lock deadlock.
        result = subprocess.run(
            [sys.executable, "-c", script], check=False, capture_output=True, text=True, timeout=12
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

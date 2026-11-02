"""Exception diagnostics at preview and interactive workbench boundaries."""

from __future__ import annotations

import unittest
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import numpy as np

from chongming.contracts.scene import ScenarioStep
from chongming.preview import PreviewRuntime
from chongming.scenarios.demo import demo_spec
from chongming.vision.interactive_tuning import InteractivePipeline
from chongming.vision.pipeline import Stage


class PreviewEncodingLoggingTests(unittest.TestCase):
    def make_runtime(self) -> PreviewRuntime:
        runtime = PreviewRuntime(demo_spec(station_count=1), video_enabled=False)
        self.addCleanup(runtime._png_encoder.shutdown)
        runtime._published_step = Mock(spec=ScenarioStep)
        runtime._published = {"sequence": 1}
        return runtime

    def test_encoded_payload_keeps_video_and_numeric_timing(self) -> None:
        runtime = self.make_runtime()
        step = runtime._published_step
        assert step is not None
        station_id = runtime._station_ids[0]
        with (
            patch("chongming.preview._video_payload", return_value=[]) as video,
            patch("chongming.preview.time.perf_counter", return_value=1.25),
        ):
            encoded = runtime._encode_video_payload(step, station_id, began=1.0)

        video.assert_called_once_with(step, station_id)
        self.assertEqual(encoded, {"video": [], "png_encode_ms": 250.0})

    def test_completed_encoding_failure_logs_traceback_and_keeps_cached_payload(self) -> None:
        runtime = self.make_runtime()
        station_id = runtime._station_ids[0]
        failure = ValueError("invalid pixel data")
        future: Future[dict[str, Any]] = Future()
        future.set_exception(failure)
        runtime._pending_encodes[station_id] = future
        runtime._encoded[station_id] = {"video": []}

        with (
            patch.object(runtime._png_encoder, "submit") as submit,
            self.assertLogs("chongming.preview", level="ERROR") as captured,
        ):
            result = runtime.snapshot(station_id)

        submit.assert_not_called()
        self.assertEqual(result["snapshot"]["video"], [])
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.name, "chongming.preview")
        self.assertEqual(record.getMessage(), "Async PNG encoding failed")
        self.assertIn("ValueError: invalid pixel data", captured.output[0])
        assert record.exc_info is not None
        self.assertIs(record.exc_info[1], failure)
        self.assertIsNotNone(record.exc_info[2])

    def test_encoding_timeout_logs_traceback_and_preserves_sync_fallback(self) -> None:
        runtime = self.make_runtime()
        station_id = runtime._station_ids[0]
        failure = TimeoutError("encoding deadline")
        future: Future[dict[str, Any]] = Future()
        future.set_exception(failure)

        with (
            patch.object(runtime._png_encoder, "submit", return_value=future),
            patch("chongming.preview._video_payload", return_value=[]) as video,
            self.assertLogs("chongming.preview", level="WARNING") as captured,
        ):
            result = runtime.snapshot(station_id)

        video.assert_called_once_with(runtime._published_step, station_id)
        self.assertEqual(result["snapshot"]["video"], [])
        self.assertEqual(result["snapshot"]["preview_station_id"], station_id)
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.name, "chongming.preview")
        self.assertIn(
            "Async encoding timeout, falling back to sync: encoding deadline", record.getMessage()
        )
        assert record.exc_info is not None
        self.assertIs(record.exc_info[1], failure)
        self.assertIsNotNone(record.exc_info[2])


class InteractivePipelineLoggingTests(unittest.TestCase):
    def test_pipeline_failure_logs_traceback_and_displays_error(self) -> None:
        failure = ValueError("invalid motion mask")
        stage = Mock(spec=Stage)
        stage.process.side_effect = failure
        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        params = {"mode": "two_frame", "threshold": 2.0, "downsample": 1}

        with patch("chongming.vision.interactive_tuning.cv2") as cv:
            cv.VideoCapture.return_value.get.return_value = 1
            cv.VideoCapture.return_value.read.side_effect = [(True, frame), (False, None)]
            cv.waitKey.return_value = 27
            workbench = InteractivePipeline(Path("test-video.mp4"))
            workbench.stages = [stage]

            with (
                patch.object(workbench, "_get_params", return_value=params),
                patch.object(workbench, "_rebuild_pipeline"),
                self.assertLogs("chongming.vision.interactive_tuning", level="ERROR") as captured,
                self.assertRaisesRegex(ValueError, "invalid motion mask"),
            ):
                workbench.run()

            cv.putText.assert_called_once()
            self.assertEqual(cv.putText.call_args.args[1], "Pipeline Error: invalid motion mask")
            cv.imshow.assert_called_once()
            cv.destroyAllWindows.assert_called_once()

        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.getMessage(), "Pipeline error")
        self.assertIn("ValueError: invalid motion mask", captured.output[0])
        assert record.exc_info is not None
        self.assertIs(record.exc_info[1], failure)
        self.assertIsNotNone(record.exc_info[2])

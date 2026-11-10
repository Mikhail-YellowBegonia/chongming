"""Native binding oracles and failure boundaries for the optional official library."""

from __future__ import annotations

import hashlib
import os
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np

from chongming.vision.pipeline import PipelineContext
from chongming.vision.vibe import DEFAULTS, VibeConfig, VibeStage, seed_native

LIBRARY = os.environ.get("CHONGMING_VIBE_TEST_LIBRARY")


class VibeBoundaryTests(unittest.TestCase):
    def test_rng_rejects_invalid_seeds(self) -> None:
        for seed in (-1, 2**32, True):
            with self.assertRaises(ValueError):
                seed_native(seed)


@unittest.skipUnless(LIBRARY, "set CHONGMING_VIBE_TEST_LIBRARY to the pinned official build")
class NativeVibeTests(unittest.TestCase):
    def test_workbench_and_device_use_identical_native_masks_and_postprocessing(self) -> None:
        import cv2

        from chongming.contracts import device as d
        from chongming.contracts import model as m
        from chongming.modules.workbench_device import WorkbenchMotionModule
        from chongming.vision.interactive_tuning import InteractivePipeline
        from chongming.vision.pipeline import process_stages
        from tests.test_detection import frame
        from tests.test_device_evaluation import policies

        profile = policies()
        assert isinstance(profile.detection, d.FrameDifferencePolicy)
        assert isinstance(profile.tracking, d.KalmanTrackingPolicy)
        assert LIBRARY is not None
        policy = replace(
            profile.detection,
            algorithm_id="vibe-workbench-v1",
            mode="vibe",
            vibe_library_path=LIBRARY,
            vibe_library_sha256=hashlib.sha256(Path(LIBRARY).read_bytes()).hexdigest(),
        )
        images = [np.zeros((64, 64, 3), dtype=np.uint8) for _ in range(4)]
        images[2][20:36, 20:36] = 255
        images[3][20:36, 24:40] = 255
        detector = WorkbenchMotionModule(policy=policy)
        seed_native(42)
        actual = []
        try:
            for index, image in enumerate(images):
                batch = detector.detect(
                    frame(
                        cv2.cvtColor(image, cv2.COLOR_BGR2RGB).astype(np.uint8),
                        time_ns=index * 100_000_000,
                        pixel_format=m.PixelFormat.RGB8,
                    )
                )
                actual.append([item.centroid_px for item in batch.candidates])
        finally:
            for stage in detector.stages:
                stage.reset()
        workbench = InteractivePipeline.__new__(InteractivePipeline)
        controls = {"Show": 0}
        with (
            patch(
                "cv2.setTrackbarPos",
                side_effect=lambda name, window, value: controls.__setitem__(name, value),
            ),
            patch("cv2.getTrackbarPos", side_effect=lambda name, window: controls[name]),
        ):
            workbench.configure(policy, profile.tracking)
            params = workbench._get_params()
        self.assertEqual(params["mode"], "vibe")
        workbench._rebuild_pipeline(params)
        try:
            for index, image in enumerate(images):
                context = PipelineContext(image, index * 0.1)
                process_stages(workbench.stages, context)
                self.assertEqual(
                    actual[index],
                    [(b.centroid_px[0] * 2, b.centroid_px[1] * 2) for b in context.blobs],
                )
        finally:
            for workbench_stage in workbench.stages:
                workbench_stage.reset()

    def make_stage(self) -> VibeStage:
        assert LIBRARY is not None
        path = Path(LIBRARY)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return VibeStage(VibeConfig(str(path), digest))

    def test_background_training_changed_signal_and_default_parameters(self) -> None:
        seed_native(42)
        stage = self.make_stage()
        image = np.zeros((32, 32, 3), dtype=np.uint8)
        try:
            for index in range(3):
                context = PipelineContext(image.copy(), float(index))
                stage.process(context)
                self.assertFalse(np.any(context.motion_mask))
            changed = image.copy()
            changed[10:22, 10:22] = 255
            context = PipelineContext(changed, 3.0)
            stage.process(context)
            assert context.motion_mask is not None
            self.assertEqual(context.motion_mask[16, 16], 255)
            self.assertEqual(set(np.unique(context.motion_mask)), {0, 255})
            self.assertEqual(stage.parameters, DEFAULTS)
        finally:
            stage.reset()

    def test_fixed_seed_replays_identically_and_reset_releases_model(self) -> None:
        frames = [np.zeros((32, 32, 3), dtype=np.uint8) for _ in range(5)]
        frames[3][10:20, 8:16] = 200
        frames[4][10:20, 10:18] = 200
        results = []
        for _ in range(2):
            seed_native(42)
            stage = self.make_stage()
            masks = []
            try:
                for index, frame in enumerate(frames):
                    context = PipelineContext(frame, float(index))
                    stage.process(context)
                    assert context.motion_mask is not None
                    masks.append(context.motion_mask.copy())
            finally:
                stage.reset()
                self.assertIsNone(stage._model)
            results.append(masks)
        for left, right in zip(*results, strict=True):
            np.testing.assert_array_equal(left, right)

    def test_dimension_change_and_digest_mismatch_fail_before_native_processing(self) -> None:
        stage = self.make_stage()
        try:
            stage.process(PipelineContext(np.zeros((32, 32, 3), dtype=np.uint8), 0))
            with self.assertRaisesRegex(ValueError, "dimensions changed"):
                stage.process(PipelineContext(np.zeros((16, 16, 3), dtype=np.uint8), 1))
        finally:
            stage.reset()
        assert LIBRARY is not None
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            VibeStage(VibeConfig(LIBRARY, "0" * 64))

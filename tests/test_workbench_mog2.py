"""Shared workbench MOG2 stages, reset semantics and timing boundaries."""

import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

import numpy as np

from chongming.contracts import device as d
from chongming.modules.workbench_device import WorkbenchMotionModule, validate_workbench_detection
from chongming.vision.mog2 import MOG2Config, MOG2Stage
from chongming.vision.occupancy import OccupancyConfig, OccupancyFilterStage
from chongming.vision.pipeline import PipelineContext, process_stages
from tests.test_device_evaluation import policies


class WorkbenchMOG2Tests(unittest.TestCase):
    def test_model_learns_background_and_detects_changed_region(self) -> None:
        stage = MOG2Stage(MOG2Config(50, 16, 0, False))
        background = np.zeros((32, 32, 3), dtype=np.uint8)
        for i in range(3):
            context = PipelineContext(background, float(i), preprocessed=background)
            stage.process(context)
        self.assertFalse(np.any(context.motion_mask))
        changed = background.copy()
        changed[10:20, 10:20] = 255
        context = PipelineContext(changed, 4.0, preprocessed=changed)
        stage.process(context)
        assert context.motion_mask is not None
        self.assertTrue(np.all(context.motion_mask[10:20, 10:20] == 255))
        self.assertEqual(np.count_nonzero(context.motion_mask), 100)
        model = stage.model
        stage.reset()
        self.assertIsNot(stage.model, model)

    def test_shadows_do_not_become_foreground(self) -> None:
        stage = MOG2Stage(MOG2Config(50, 16, -1, True))
        stage.model = Mock()
        stage.model.apply.return_value = np.array([[0, 127, 255]], dtype=np.uint8)
        image = np.zeros((1, 3, 3), dtype=np.uint8)
        context = PipelineContext(image, 0, preprocessed=image)
        stage.process(context)
        np.testing.assert_array_equal(context.motion_mask, [[0, 0, 255]])
        self.assertEqual(stage.model.apply.call_args.kwargs["learningRate"], -1)

    def test_mog2_policy_requires_explicit_parameters_and_occupancy_can_be_bypassed(self) -> None:
        detection = policies().detection
        assert isinstance(detection, d.FrameDifferencePolicy)
        policy = replace(detection, mode="mog2", algorithm_id="mog2-workbench-v1")
        with self.assertRaises(ValueError):
            validate_workbench_detection(policy)
        policy = replace(
            policy,
            mog2_history=500,
            mog2_variance_threshold=16.0,
            mog2_learning_rate=-1.0,
            mog2_detect_shadows=False,
            apply_occupancy=False,
            apply_erosion=True,
        )
        detector = WorkbenchMotionModule(policy=policy)
        self.assertIsInstance(detector.stages[1], MOG2Stage)
        stage = OccupancyFilterStage(OccupancyConfig(enabled=False))
        image = np.zeros((4, 4), dtype=np.uint8)
        image[1, 1] = 255
        context = PipelineContext(image, 0, motion_mask=image)
        stage.process(context)
        np.testing.assert_array_equal(context.filtered_mask, image)
        self.assertIsNone(stage.score_map)

    def test_stage_timing_measures_only_stage_call(self) -> None:
        stage = Mock()
        context = PipelineContext(np.zeros((1, 1), dtype=np.uint8), 0)
        with patch("chongming.vision.pipeline.perf_counter", side_effect=[1.0, 1.002]):
            process_stages([stage], context)
        stage.process.assert_called_once_with(context)
        self.assertAlmostEqual(context.debug_data["stage_timings_ms"]["Mock"], 2.0)

    def test_workbench_and_device_share_mog2_mask_pipeline(self) -> None:
        import cv2

        from chongming.contracts import model as m
        from chongming.vision.interactive_tuning import InteractivePipeline
        from tests.test_detection import frame

        profile = policies()
        assert isinstance(profile.detection, d.FrameDifferencePolicy)
        assert isinstance(profile.tracking, d.KalmanTrackingPolicy)
        policy = replace(
            profile.detection,
            mode="mog2",
            algorithm_id="mog2-workbench-v1",
            mog2_history=50,
            mog2_variance_threshold=16.0,
            mog2_learning_rate=0.0,
            mog2_detect_shadows=False,
            apply_occupancy=False,
            apply_erosion=True,
        )
        detector = WorkbenchMotionModule(policy=policy)
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
        self.assertEqual(params["mode"], "mog2")
        self.assertFalse(params["apply_occupancy"])
        self.assertTrue(params["apply_erosion"])
        workbench._rebuild_pipeline(params)
        for index in range(4):
            bgr = np.zeros((64, 64, 3), dtype=np.uint8)
            if index > 1:
                bgr[20:32, 10 + index * 4 : 22 + index * 4] = 255
            context = PipelineContext(bgr, index * 0.1)
            process_stages(workbench.stages, context)
            actual = detector.detect(
                frame(
                    np.asarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), dtype=np.uint8),
                    time_ns=index * 100_000_000,
                    pixel_format=m.PixelFormat.RGB8,
                )
            )
            self.assertEqual(
                [c.centroid_px for c in actual.candidates],
                [tuple(v * policy.downsample_factor for v in b.centroid_px) for b in context.blobs],
            )
            self.assertIn("MOG2Stage", detector.stage_timings_ms)
            self.assertIn("TrackingStage", context.debug_data["stage_timings_ms"])

    def test_invalid_model_parameters_rejected(self) -> None:
        for config in ((0, 16, -1, False), (50, 0, -1, False), (50, 16, -0.5, False)):
            with self.assertRaises(ValueError):
                MOG2Config(*config)

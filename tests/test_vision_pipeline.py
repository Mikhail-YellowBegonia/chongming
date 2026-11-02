"""Occupancy masks and typed interactive tuning parameters."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from chongming.vision.interactive_tuning import InteractivePipeline, TuningParameters
from chongming.vision.motion import FrameDifferenceStage
from chongming.vision.occupancy import OccupancyConfig, OccupancyFilterStage
from chongming.vision.pipeline import PipelineContext


class OccupancyFilterTests(unittest.TestCase):
    def test_dilation_and_reset_preserve_binary_masks_and_scores(self) -> None:
        frame = np.zeros((5, 5, 3), dtype=np.uint8)
        first_mask = np.zeros((5, 5), dtype=np.uint8)
        first_mask[2, 2] = 255
        second_mask = first_mask.copy()
        second_mask[2, 3] = 255

        for dilate in (False, True):
            with self.subTest(dilate=dilate):
                stage = OccupancyFilterStage(
                    OccupancyConfig(
                        score_increment=2.0,
                        score_decay=0.5,
                        mask_threshold=2.0,
                        dilate_background_mask=dilate,
                        dilation_kernel_size=3,
                    )
                )
                first = PipelineContext(frame=frame, timestamp=0.0, motion_mask=first_mask)
                stage.process(first)
                np.testing.assert_array_equal(first.filtered_mask, first_mask)

                second = PipelineContext(frame=frame, timestamp=1.0, motion_mask=second_mask)
                stage.process(second)
                assert second.filtered_mask is not None
                self.assertEqual(second.filtered_mask.dtype, np.uint8)
                expected_filtered = np.zeros((5, 5), dtype=np.uint8)
                if not dilate:
                    expected_filtered[2, 3] = 255
                np.testing.assert_array_equal(second.filtered_mask, expected_filtered)
                expected_background = np.zeros((5, 5), dtype=np.uint8)
                expected_background[2, 2] = 255
                if dilate:
                    expected_background[1, 2] = expected_background[3, 2] = 255
                    expected_background[2, 1] = expected_background[2, 3] = 255
                np.testing.assert_array_equal(
                    second.debug_data["background_mask"], expected_background
                )
                expected_scores = np.zeros((5, 5), dtype=np.float32)
                expected_scores[2, 2] = 3.0
                expected_scores[2, 3] = 1.5
                np.testing.assert_array_equal(stage.score_map, expected_scores)
                self.assertEqual(second_mask[2, 2], 255)
                self.assertEqual(second_mask[2, 3], 255)

                stage.reset()
                self.assertIsNone(stage.score_map)
                after_reset = PipelineContext(frame=frame, timestamp=2.0, motion_mask=second_mask)
                stage.process(after_reset)
                np.testing.assert_array_equal(after_reset.filtered_mask, second_mask)

    def test_missing_motion_mask_is_rejected(self) -> None:
        stage = OccupancyFilterStage(OccupancyConfig(dilate_background_mask=False))
        context = PipelineContext(frame=np.zeros((5, 5, 3), dtype=np.uint8), timestamp=0.0)
        with self.assertRaisesRegex(ValueError, "requires motion_mask"):
            stage.process(context)


class InteractiveTuningTests(unittest.TestCase):
    def test_trackbar_values_build_the_expected_stage_configuration(self) -> None:
        trackbars = {
            "Mode": 1,
            "Threshold": 20,
            "Downsample": 2,
            "Morph Kernel": 2,
            "Apply Opening": 1,
            "Score Inc": 10,
            "Score Decay": 5,
            "Occ Threshold": 50,
            "Occ Dilate": 3,
            "Min Area": 10,
            "Blob Dilation": 3,
            "Confirm Frames": 3,
            "Max Lost": 10,
            "Assoc Gate": 50,
            "Show": 3,
            "Use Occupancy": 1,
            "MOG History": 500,
            "MOG Variance": 16,
            "MOG LR x10000": 0,
            "MOG Shadows": 0,
        }
        expected: TuningParameters = {
            "mode": "three_frame",
            "apply_occupancy": True,
            "mog2_history": 500,
            "mog2_variance_threshold": 16.0,
            "mog2_learning_rate": -1.0,
            "mog2_detect_shadows": False,
            "threshold": 20.0,
            "downsample": 2,
            "morph_kernel": 3,
            "apply_opening": True,
            "score_inc": 1.0,
            "score_decay": 0.5,
            "occ_threshold": 50.0,
            "occ_dilate": 3,
            "min_area": 10.0,
            "blob_dilation": 3,
            "confirm_frames": 3,
            "max_lost": 10,
            "assoc_gate": 50.0,
            "show": 3,
        }
        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        with patch("chongming.vision.interactive_tuning.cv2") as cv:
            cv.VideoCapture.return_value.get.return_value = 1
            cv.VideoCapture.return_value.read.side_effect = [(True, frame), (False, None)]
            cv.getTrackbarPos.side_effect = lambda name, window: trackbars[name]
            workbench = InteractivePipeline(Path("test-video.mp4"))
            self.assertIsNone(workbench.last_params)
            params = workbench._get_params()
            self.assertEqual(params, expected)
            workbench._rebuild_pipeline(params)

        self.assertEqual(len(workbench.stages), 6)
        motion = workbench.stages[1]
        assert isinstance(motion, FrameDifferenceStage)
        self.assertEqual(motion.config.mode, "three_frame")
        self.assertEqual(motion.config.threshold, 20.0)
        occupancy = workbench.stages[3]
        assert isinstance(occupancy, OccupancyFilterStage)
        self.assertEqual(occupancy.config.score_increment, 1.0)
        self.assertEqual(occupancy.config.score_decay, 0.5)
        self.assertEqual(occupancy.config.dilation_kernel_size, 3)

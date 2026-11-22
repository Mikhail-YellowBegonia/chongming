"""Physical difficulty labels are independent of pixel matching and false positives."""

import json
import math
import tempfile
import unittest
from pathlib import Path

from chongming_proto.online import common_pb2
from chongming_proto.truth import truth_pb2
from chongming_scorer.difficulty import bin_bounds, range_to_size, score, score_difficulty_strata
from chongming_scorer.storage import TestScenePackage, TestSceneWriter


class DifficultyTests(unittest.TestCase):
    def test_range_is_euclidean_and_common_translation_does_not_change_ratio(self) -> None:
        self.assertEqual(range_to_size((0, 0, 0), (3, 4, 0), 2), 2.5)
        self.assertEqual(range_to_size((10, 20, 30), (13, 24, 30), 2), 2.5)
        for size in (0, -1, math.inf, math.nan):
            with self.assertRaises(ValueError):
                range_to_size((0, 0, 0), (3, 4, 0), size)

    def test_fixed_power_of_two_bins_have_explicit_boundaries(self) -> None:
        self.assertEqual(bin_bounds(4), (4, 8))
        self.assertEqual(bin_bounds(math.nextafter(4, 0)), (2, 4))
        self.assertEqual(bin_bounds(0.5), (0.5, 1))
        for value in (0, -1, math.nan, math.inf):
            with self.assertRaises(ValueError):
                bin_bounds(value)

    def test_false_predictions_do_not_receive_a_truth_difficulty(self) -> None:
        row = {
            "warmup_excluded": False,
            "frame_id": "a",
            "fp": 1,
            "eligible_targets_px": {"near": [0, 0], "far": [100, 0]},
            "target_gates_px": {"near": 10, "far": 10},
            "predictions_px": [[0, 0], [1000, 0]],
        }
        index = {
            "a": {
                "near": {"bin_lower": 2, "difficulty": 3},
                "far": {"bin_lower": 4, "difficulty": 5},
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frames.jsonl"
            path.write_text(json.dumps(row) + "\n")
            result = score(path, index)
        self.assertEqual(result["responsible_samples"], 2)
        self.assertEqual(result["precision"], 0.5)
        self.assertEqual([entry["recall"] for entry in result["bins"]], [1, 0])
        self.assertEqual(sum(entry["responsible_samples"] for entry in result["bins"]), 2)

    def test_score_difficulty_strata_from_test_scene(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scene_dir = Path(directory) / "scene"
            with TestSceneWriter(
                scene_dir,
                scenario_id="diff-test",
                world_frame_id="demo-enu",
                clock_domain_id="demo-clock",
                start_time_ns=0,
                end_time_ns=1_000_000,
                stations=[],
            ) as writer:
                writer.add_truth_frame(
                    truth_pb2.TruthFrame(
                        world_frame_id="demo-enu",
                        experiment_time_ns=0,
                        step_index=0,
                        targets=[
                            truth_pb2.TargetTruth(
                                target_id="near",
                                target_class="sphere",
                                position_enu_m=common_pb2.Vec3(x=0.0, y=6.0, z=0.0),
                                velocity_enu_mps=common_pb2.Vec3(),
                                bounding_radius_m=1.0,
                                body_aabb_diagonal_m=2.0,
                            ),
                            truth_pb2.TargetTruth(
                                target_id="far",
                                target_class="sphere",
                                position_enu_m=common_pb2.Vec3(x=0.0, y=10.0, z=0.0),
                                velocity_enu_mps=common_pb2.Vec3(),
                                bounding_radius_m=1.0,
                                body_aabb_diagonal_m=2.0,
                            ),
                        ],
                        projections=[
                            truth_pb2.StationTargetProjection(
                                station_id="s1",
                                target_id="near",
                                occlusion_status=truth_pb2.OCCLUSION_STATUS_VISIBLE,
                                projected_centroid_px=common_pb2.Vec2(x=0.0, y=0.0),
                                projected_diagonal_px=10.0,
                                range_m=6.0,
                                frame_id="f-0",
                            ),
                            truth_pb2.StationTargetProjection(
                                station_id="s1",
                                target_id="far",
                                occlusion_status=truth_pb2.OCCLUSION_STATUS_VISIBLE,
                                projected_centroid_px=common_pb2.Vec2(x=100.0, y=0.0),
                                projected_diagonal_px=10.0,
                                range_m=10.0,
                                frame_id="f-0",
                            ),
                        ],
                    )
                )
            scene = TestScenePackage.open(scene_dir)
            result = score_difficulty_strata(
                scene,
                [
                    {
                        "warmup_excluded": False,
                        "frame_id": "f-0",
                        "fp": 1,
                        "eligible_targets_px": {"near": (0.0, 0.0), "far": (100.0, 0.0)},
                        "target_gates_px": {"near": 10.0, "far": 10.0},
                        "predictions_px": [(0.0, 0.0), (1000.0, 0.0)],
                    }
                ],
            )
            assert result is not None
            self.assertEqual(result["responsible_samples"], 2)
            self.assertEqual(result["precision"], 0.5)
            self.assertEqual([b["lower"] for b in result["bins"]], [2.0, 4.0])
            self.assertEqual([b["recall"] for b in result["bins"]], [1.0, 0.0])

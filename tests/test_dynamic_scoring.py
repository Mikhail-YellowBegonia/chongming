"""Target extent gates and workbench rescores must not change algorithm state."""

import unittest

from chongming.evaluation import DetectionMetrics, annotate, target_pixel_gates
from chongming.vision.scoring import WorkbenchScoring


class DynamicScoringTests(unittest.TestCase):
    def test_diagonal_gate_floor_and_invalid_dimensions(self) -> None:
        target = {
            "truth_object_id": "plane",
            "projection": {"center_in_field_of_view": True},
            "projected_width_px": 80,
            "projected_height_px": 60,
        }
        self.assertEqual(target_pixel_gates([target], 10, 0.5), {"plane": 50})
        self.assertEqual(target_pixel_gates([target], 10, 0.01), {"plane": 10})
        target["projected_width_px"] = None
        with self.assertRaises(ValueError):
            target_pixel_gates([target], 10, 0.5)
        self.assertEqual(target_pixel_gates([target], 10, None), {"plane": 10})

    def test_wing_hit_and_duplicate_use_each_targets_gate(self) -> None:
        targets = {"plane": (0.0, 0.0), "small": (100.0, 0.0)}
        predictions = [(40.0, 0.0), (45.0, 0.0), (115.0, 0.0)]
        gates = {"plane": 50.0, "small": 10.0}
        metric = DetectionMetrics(10, "px")
        result = metric.add(predictions, targets, target_gates=gates)
        self.assertEqual((result["tp"], result["fp"], result["fn"]), (1, 2, 1))
        self.assertEqual((result["duplicates"], result["false_alarms"]), (1, 1))
        summary = metric.summary()
        self.assertEqual(result["matched_errors"], [40])
        self.assertNotIn("matched_error", summary)
        self.assertNotIn("normalized_matched_error", summary)
        preview = annotate(
            {
                "eligible_targets_px": targets,
                "predictions_px": predictions,
                "target_gates_px": gates,
            },
            10,
        )
        self.assertEqual(
            [p["kind"] for p in preview["predictions"]], ["matched", "duplicate", "false_alarm"]
        )
        self.assertEqual(preview["targets"][0]["gate"], 50)

    def test_workbench_gate_change_rescores_without_mutating_predictions(self) -> None:
        scorer = WorkbenchScoring.__new__(WorkbenchScoring)
        scorer.frames = [
            {
                "truth": {
                    "targets": [
                        {
                            "truth_object_id": "plane",
                            "projection": {"center_in_field_of_view": True, "centroid_px": [0, 0]},
                            "projected_width_px": 80,
                            "projected_height_px": 60,
                        }
                    ]
                }
            }
        ] * 2
        scorer.times = [0, 15]
        scorer.warmup_s = 15
        scorer.predictions = {}
        scorer.cached_key = None
        scorer.cached_result = None
        scorer.observe(0, [(40.0, 0.0)])
        scorer.observe(1, [(40.0, 0.0)])
        current, total = scorer.score(1, 10, 0.1)
        self.assertEqual(total["frames"], 1)
        self.assertEqual(total["tp"], 0)
        current, total = scorer.score(1, 10, 0.5)
        self.assertEqual(total["tp"], 1)
        self.assertEqual(len(scorer.predictions), 2)
        import numpy as np

        image = np.zeros((100, 100, 3), dtype=np.uint8)
        panel = scorer.panel(image, 1, 10, 0.5, [(40.0, 0.0)], point=(0, 0))
        self.assertEqual(panel.shape, (600, 900, 3))
        self.assertTrue(np.any(panel != 24))
        self.assertIs(scorer.score(1, 10, 0.5)[0], current)
        scorer.reset()
        self.assertEqual(scorer.predictions, {})


class WorkbenchExportTests(unittest.TestCase):
    def test_export_preserves_other_station_and_never_overwrites(self) -> None:
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock, patch

        from chongming.vision.interactive_tuning import InteractivePipeline

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            document = json.loads(Path("scenarios/policies/hannover_vision_start.json").read_text())
            source.write_text(json.dumps(document))
            workbench = InteractivePipeline.__new__(InteractivePipeline)
            workbench.export_template = source
            workbench.export_directory = root / "exports"
            workbench.export_station = "station-west"
            workbench.export_index = 0
            parameter_reader = Mock(
                return_value={
                    "mode": "three_frame",
                    "apply_occupancy": True,
                    "threshold": 3,
                    "downsample": 2,
                    "morph_kernel": 3,
                    "apply_opening": False,
                    "apply_erosion": True,
                    "score_inc": 4,
                    "score_decay": 0.1,
                    "occ_threshold": 50,
                    "occ_dilate": 20,
                    "min_area": 12,
                    "blob_dilation": 5,
                    "confirm_frames": 4,
                    "max_lost": 10,
                    "assoc_gate": 40,
                }
            )
            with patch.object(workbench, "_get_params", parameter_reader):
                first = workbench.export_parameters()
                second = workbench.export_parameters()
            self.assertNotEqual(first, second)
            exported = json.loads(first.read_text())
            self.assertEqual(exported["station-east"], document["station-east"])
            self.assertEqual(exported["station-west"]["detection"]["mode"], "three_frame")
            self.assertTrue(exported["station-west"]["detection"]["apply_erosion"])
            self.assertEqual(exported["station-west"]["tracking"]["confirmation_frame_count"], 4)
            self.assertEqual(json.loads(source.read_text()), document)

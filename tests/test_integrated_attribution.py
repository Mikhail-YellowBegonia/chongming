"""Operational responsibility rules retain center and device distinctions."""

import json
import tempfile
import unittest
from pathlib import Path

from chongming.integrated_attribution import false_output_owner, miss_owner, scored_performance


class AttributionTests(unittest.TestCase):
    def test_performance_uses_scored_windows_not_incoming_step_warmup_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device, center = root / "device", root / "center"
            (device / "evaluation").mkdir(parents=True)
            center.mkdir()
            (center / "center-registration.json").write_text(
                json.dumps({"policy": {"windows": {"origin_ns": 0, "width_ns": 10}}})
            )
            (device / "evaluation/frames.jsonl").write_text(
                "\n".join(
                    json.dumps(
                        {
                            "warmup_excluded": False,
                            "time_ns": 11,
                            "timing_ms": {"pipeline_ms": value},
                        }
                    )
                    for value in (1, 2)
                )
            )
            (center / "timings.jsonl").write_text(
                "\n".join(
                    json.dumps(
                        {
                            "center_process_ms": duration,
                            "windows": [
                                {
                                    "window_start_ns": start,
                                    "geometry_ms": geometry,
                                    "geometry_groups": 2,
                                }
                            ],
                        }
                    )
                    for start, duration, geometry in ((0, 100, 99), (10, 4, 3))
                )
            )
            report = scored_performance(device, center, {11})
            self.assertEqual(report["center_process_ms"]["mean"], 4)
            self.assertEqual(report["stages_ms"]["geometry_ms"]["mean"], 3)
            self.assertEqual(report["geometry_ms_per_group_window_average"]["mean"], 1.5)
            self.assertEqual(report["sequential_three_station_device_plus_center_ms"]["mean"], 7)

    def test_miss_ownership_follows_available_evidence_and_stages(self) -> None:
        states = [
            ((1, 1, False, False, False), "device:insufficient_correct_stations"),
            ((2, 1, False, False, False), "center:ingress_rejection"),
            ((2, 2, False, False, True), "device:triangulation_localization_limit"),
            ((2, 2, True, True, True), "center:spatial_suppression_or_assignment"),
            ((2, 2, True, False, True), "center:geometry_quality_or_estimation"),
            ((2, 2, True, False, False), "center:candidate_coverage"),
        ]
        for (emitted, admitted, raw, geometry, examined), expected in states:
            self.assertEqual(
                miss_owner(
                    emitted_stations=emitted,
                    admitted_stations=admitted,
                    raw_can_hit=raw,
                    geometry_can_hit=geometry,
                    correct_groups_examined=examined,
                ),
                expected,
            )

    def test_three_station_requirement_does_not_accept_two_correct_stations(self) -> None:
        self.assertEqual(
            miss_owner(
                emitted_stations=2,
                admitted_stations=2,
                raw_can_hit=True,
                geometry_can_hit=True,
                correct_groups_examined=True,
                minimum_stations=3,
            ),
            "device:insufficient_correct_stations",
        )

    def test_false_outputs_distinguish_cross_target_false_input_and_duplicates(self) -> None:
        a = {"target_id": "a", "kind": "matched"}
        b = {"target_id": "b", "kind": "matched"}
        false = {"target_id": None, "kind": "false_alarm"}
        self.assertEqual(false_output_owner("duplicate", [a, a]), "center:duplicate_location")
        self.assertEqual(
            false_output_owner("false_alarm", [a, b]), "center:cross_target_combination"
        )
        self.assertEqual(
            false_output_owner("false_alarm", [a, false]), "device:false_positive_propagated"
        )
        self.assertEqual(
            false_output_owner("false_alarm", [a, a]), "device:localization_error_propagated"
        )

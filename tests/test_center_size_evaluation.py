"""Center responsibility, physical-size bands and exposure-time evaluation schedule."""

import math
import tempfile
import unittest
from pathlib import Path

from chongming_proto.online import common_pb2, online_pb2
from chongming_proto.truth import truth_pb2
from chongming_scorer.metrics import CenterSizeMetrics
from chongming_scorer.scorer import score_center_run
from chongming_scorer.storage import (
    RunOutputPackage,
    RunOutputWriter,
    TestScenePackage,
    TestSceneWriter,
)


class CenterSizeEvaluationTests(unittest.TestCase):
    def test_precise_and_complete_miss_boundaries(self) -> None:
        metric = CenterSizeMetrics(0.7, 3)
        target: dict[str, tuple[float, ...]] = {"a": (0.0, 0.0, 0.0)}
        for error, precise, coarse in ((0.7, 1, 1), (1.0, 0, 1), (3.0, 0, 0)):
            row = metric.add([(error, 0.0, 0.0)], target, {"a": 1.0}, [], [])
            self.assertEqual(row["precise"]["tp"], precise)
            self.assertEqual(row["within_miss_gate"]["tp"], coarse)
        summary = metric.summary()
        self.assertEqual(summary["intermediate_matches"], 1)
        self.assertEqual(summary["completely_off_target_outputs"], 1)
        self.assertEqual(summary["no_match_within_miss_gate"], 1)

    def test_missing_outputs_duplicates_and_nonresponsible_targets(self) -> None:
        metric = CenterSizeMetrics(0.7, 3)
        row = metric.add(
            [(0.0, 0.0, 0.0), (0.1, 0.0, 0.0), (100.0, 0.0, 0.0)],
            {"a": (0.0, 0.0, 0.0), "b": (10.0, 0.0, 0.0)},
            {"a": 1.0, "b": 1.0},
            [(100.0, 0.0, 0.0)],
            [1.0],
        )
        self.assertEqual(row["precise"]["tp"], 1)
        self.assertEqual(row["precise"]["duplicates"], 1)
        self.assertEqual(row["precise"]["ignored_predictions"], 1)
        self.assertEqual(row["within_miss_gate"]["fn"], 1)
        self.assertEqual(row["completely_off_target_outputs"], 0)

    def test_nonresponsible_target_excludes_only_precise_hits(self) -> None:
        metric = CenterSizeMetrics(0.7, 3)
        row = metric.add([(1.0, 0.0, 0.0)], {}, {}, [(0.0, 0.0, 0.0)], [1.0])
        self.assertEqual(row["precise"]["fp"], 1)
        self.assertEqual(row["precise"]["ignored_predictions"], 0)
        self.assertEqual(row["within_miss_gate"]["ignored_predictions"], 1)

    def test_actual_exposure_schedule_not_window_midpoint_counts_empty_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scene_dir = root / "scene"
            run_dir = root / "run"
            diagonal = 2.0 * math.sqrt(3.0)

            with TestSceneWriter(
                scene_dir,
                scenario_id="size-eval",
                world_frame_id="demo-enu",
                clock_domain_id="demo-clock",
                start_time_ns=0,
                end_time_ns=2_000_000,
                stations=[],
            ) as scene_writer:
                for step_index, ns in enumerate((100_000, 1_100_000)):
                    scene_writer.add_truth_frame(
                        truth_pb2.TruthFrame(
                            world_frame_id="demo-enu",
                            experiment_time_ns=ns,
                            step_index=step_index,
                            targets=[
                                truth_pb2.TargetTruth(
                                    target_id="target",
                                    target_class="sphere",
                                    position_enu_m=common_pb2.Vec3(x=5.0, y=0.0, z=30.0),
                                    velocity_enu_mps=common_pb2.Vec3(),
                                    bounding_radius_m=1.0,
                                    body_aabb_diagonal_m=diagonal,
                                )
                            ],
                            projections=[
                                truth_pb2.StationTargetProjection(
                                    station_id=station,
                                    target_id="target",
                                    occlusion_status=truth_pb2.OCCLUSION_STATUS_VISIBLE,
                                )
                                for station in ("s1", "s2")
                            ],
                        )
                    )

            scene = TestScenePackage.open(scene_dir)
            with RunOutputWriter(
                run_dir,
                run_id="run-size",
                mode="center",
                test_scene_path=scene_dir,
                test_scene_sha256=scene.content_sha256,
            ) as run_writer:
                run_writer.write_center_message(
                    online_pb2.BusMessage(
                        bus_sequence_no=1,
                        fusion_measurement=online_pb2.FusionMeasurement(
                            center_session_id="c1",
                            measurement_id="a",
                            clock_domain_id="demo-clock",
                            world_frame_id="demo-enu",
                            reference_time_ns=100_000,
                            support_exposure_span_ns=0,
                            status=common_pb2.FUSION_STATUS_LOCATED,
                            position_enu_m=common_pb2.Vec3(x=5.0, y=0.0, z=30.0),
                        ),
                    )
                )
                for seq, ns in enumerate((100_000, 1_100_000), start=2):
                    run_writer.write_center_message(
                        online_pb2.BusMessage(
                            bus_sequence_no=seq,
                            progress_report=online_pb2.ProgressReport(
                                source_role=common_pb2.COMPONENT_ROLE_CENTER,
                                source_id="center",
                                session_id="c1",
                                completed_time_ns=ns,
                                completed_sequence_no=seq - 1,
                                finite_end_reached=(seq == 3),
                            ),
                        )
                    )

            run = RunOutputPackage.open(run_dir)
            _, report, frames = score_center_run(scene, run, size_k=0.7, miss_k=3.0)
            self.assertEqual(report["metrics"]["precise"]["tp"], 1)
            self.assertEqual(report["metrics"]["precise"]["fn"], 1)
            self.assertEqual(report["metrics"]["precise"]["recall"], 0.5)
            self.assertEqual([r["reference_time_ns"] for r in frames], [100_000, 1_100_000])

            _, three_station, _ = score_center_run(
                scene, run, size_k=0.7, miss_k=3.0, minimum_stations=3
            )
            self.assertEqual(three_station["minimum_stations"], 3)
            self.assertEqual(three_station["metrics"]["precise"]["fn"], 0)

            _, after_warmup, _ = score_center_run(
                scene, run, size_k=0.7, miss_k=3.0, warmup_s=0.001, warmup_origin_ns=100_000
            )
            self.assertEqual(after_warmup["metrics"]["precise"]["frames"], 1)
            self.assertEqual(after_warmup["metrics"]["precise"]["fn"], 1)
            self.assertEqual(after_warmup["warmup_origin_ns"], 100_000)

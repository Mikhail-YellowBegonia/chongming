"""End-to-end CLI tests for chongming-scorer in edge, center, and integrated modes."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from chongming_proto.online import common_pb2, online_pb2
from chongming_proto.truth import truth_pb2
from chongming_scorer.cli import main
from chongming_scorer.scorer import (
    CenterIncrementalScorer,
    EdgeIncrementalScorer,
    IntegratedIncrementalScorer,
)
from chongming_scorer.storage import (
    RunOutputPackage,
    RunOutputWriter,
    TestScenePackage,
    TestSceneWriter,
)


class ScorerPipelineTests(unittest.TestCase):
    def test_cli_verify_inspect_and_integrated_scoring(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scene_dir = root / "scene"
            run_dir = root / "run"
            alt_out = root / "alt-report"

            with TestSceneWriter(
                scene_dir,
                scenario_id="pipe-1",
                world_frame_id="enu",
                clock_domain_id="clk",
                start_time_ns=0,
                end_time_ns=20_000_000_000,
                stations=[],
            ) as sw:
                for idx, ns in enumerate((0, 16_000_000_000, 17_000_000_000)):
                    cap = common_pb2.CaptureTime(
                        clock_domain_id="clk", exposure_start_ns=ns, exposure_end_ns=ns
                    )
                    for st in ("s1", "s2"):
                        sw.add_frame_index(
                            truth_pb2.FrameIndexEntry(
                                station_id=st,
                                station_session_id=f"sess-{st}",
                                camera_role=common_pb2.CAMERA_ROLE_MAIN,
                                frame_index=idx,
                                frame_id=f"{st}-{idx}",
                                capture_time=cap,
                                station_config_digest="cfg",
                                video_relpath=f"video/{st}-main.mp4",
                                width_px=64,
                                height_px=64,
                            )
                        )
                    sw.add_truth_frame(
                        truth_pb2.TruthFrame(
                            world_frame_id="enu",
                            experiment_time_ns=ns,
                            step_index=idx,
                            targets=[
                                truth_pb2.TargetTruth(
                                    target_id="target-1",
                                    target_class="sphere",
                                    position_enu_m=common_pb2.Vec3(x=0.0, y=50.0, z=10.0),
                                    velocity_enu_mps=common_pb2.Vec3(),
                                    bounding_radius_m=1.0,
                                    body_aabb_diagonal_m=2.0,
                                )
                            ],
                            projections=[
                                truth_pb2.StationTargetProjection(
                                    station_id=st,
                                    target_id="target-1",
                                    occlusion_status=truth_pb2.OCCLUSION_STATUS_VISIBLE,
                                    projected_centroid_px=common_pb2.Vec2(x=20.0, y=20.0),
                                    projected_diagonal_px=4.0,
                                    range_m=50.0,
                                    frame_id=f"{st}-{idx}",
                                )
                                for st in ("s1", "s2")
                            ],
                        )
                    )

            scene = TestScenePackage.open(scene_dir)
            with RunOutputWriter(
                run_dir,
                run_id="run-int-1",
                mode="integrated",
                test_scene_path=scene_dir,
                test_scene_sha256=scene.content_sha256,
            ) as rw:
                seq = 0
                for idx, ns in enumerate((0, 16_000_000_000, 17_000_000_000)):
                    cap = common_pb2.CaptureTime(
                        clock_domain_id="clk", exposure_start_ns=ns, exposure_end_ns=ns
                    )
                    for st in ("s1", "s2"):
                        seq += 1
                        obs = (
                            [
                                online_pb2.Observation2D(
                                    centroid_px=common_pb2.Vec2(x=20.0, y=20.0),
                                    local_track_id="t1",
                                )
                            ]
                            if idx == 1
                            else []
                        )
                        rw.write_edge_message(
                            online_pb2.BusMessage(
                                bus_sequence_no=seq,
                                detection_batch=online_pb2.DetectionBatch(
                                    main_frame_ref=common_pb2.FrameRef(
                                        station_id=st,
                                        station_session_id=f"sess-{st}",
                                        camera_role=common_pb2.CAMERA_ROLE_MAIN,
                                        frame_id=f"{st}-{idx}",
                                    ),
                                    capture_time=cap,
                                    processing_status=common_pb2.PROCESSING_STATUS_PROCESSED,
                                    observations=obs,
                                    decode_duration_ns=5_000_000,
                                    cv_duration_ns=1_000_000,
                                ),
                            )
                        )
                        if idx == 1:
                            seq += 1
                            rw.write_edge_message(
                                online_pb2.BusMessage(
                                    bus_sequence_no=seq,
                                    ray_event=online_pb2.RayEvent(
                                        envelope=common_pb2.EventEnvelope(
                                            event_type=common_pb2.EVENT_TYPE_RAY,
                                            station_id=st,
                                            station_session_id=f"sess-{st}",
                                            sequence_no=idx + 1,
                                        ),
                                        frame_id=f"{st}-{idx}",
                                        local_track_id="t1",
                                        capture_time=cap,
                                        ray_origin_enu_m=common_pb2.Vec3(
                                            x=-10.0 if st == "s1" else 10.0, y=0.0, z=10.0
                                        ),
                                        ray_direction_enu_unit=common_pb2.Vec3(
                                            x=0.2 if st == "s1" else -0.2, y=1.0, z=0.0
                                        ),
                                    ),
                                )
                            )

                rw.write_center_message(
                    online_pb2.BusMessage(
                        bus_sequence_no=1,
                        fusion_measurement=online_pb2.FusionMeasurement(
                            center_session_id="c1",
                            measurement_id="m1",
                            clock_domain_id="clk",
                            world_frame_id="enu",
                            reference_time_ns=16_000_000_000,
                            status=common_pb2.FUSION_STATUS_LOCATED,
                            position_enu_m=common_pb2.Vec3(x=0.0, y=50.0, z=10.0),
                        ),
                    )
                )
                for c_seq, ns in enumerate((0, 16_000_000_000, 17_000_000_000), start=2):
                    rw.write_center_message(
                        online_pb2.BusMessage(
                            bus_sequence_no=c_seq,
                            progress_report=online_pb2.ProgressReport(
                                source_role=common_pb2.COMPONENT_ROLE_CENTER,
                                source_id="center",
                                session_id="c1",
                                completed_time_ns=ns,
                                completed_sequence_no=c_seq - 1,
                                finite_end_reached=(c_seq == 4),
                            ),
                        )
                    )

            main(["verify-scene", str(scene_dir)])
            main(["inspect-run", str(run_dir)])
            main(
                [
                    "score",
                    "--scene",
                    str(scene_dir),
                    "--run",
                    str(run_dir),
                    "--output",
                    str(alt_out),
                    "--size-k-m",
                    "0.7",
                    "--warmup-s",
                    "15.0",
                ]
            )
            report = json.loads((alt_out / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["edge"]["aggregate"]["tp"], 2)
            self.assertEqual(report["edge"]["aggregate"]["fn"], 2)
            self.assertEqual(report["center"]["metrics"]["precise"]["tp"], 1)
            self.assertEqual(report["center"]["metrics"]["precise"]["fn"], 1)
            self.assertEqual(
                report["attribution"]["misses"], {"device:insufficient_correct_stations": 1}
            )
            run = RunOutputPackage.open(run_dir)
            self.assertFalse(run.has_scores)

            # Verify streaming EdgeIncrementalScorer, CenterIncrementalScorer, and
            # IntegratedIncrementalScorer match offline score_run exactly.
            scene = TestScenePackage.open(scene_dir)
            edge_msgs = list(run.iter_edge_events())
            center_msgs = list(run.iter_center_output())

            inc_edge = EdgeIncrementalScorer(scene, run_id=run.run_id, warmup_s=15.0)
            streamed_edge: list[truth_pb2.ScoreEvent] = []
            for msg in edge_msgs:
                streamed_edge.extend(inc_edge.feed_bus_message(msg))
            streamed_edge.extend(inc_edge.flush_remaining())
            edge_all, edge_rep, _ = inc_edge.finalize()
            self.assertEqual(len(streamed_edge), 2)
            self.assertEqual(edge_all, streamed_edge)
            self.assertEqual(edge_rep, report["edge"])

            inc_center = CenterIncrementalScorer(
                scene, run_id=run.run_id, size_k=0.7, miss_k=3.0, warmup_s=15.0
            )
            streamed_center: list[truth_pb2.ScoreEvent] = []
            for msg in center_msgs:
                streamed_center.extend(inc_center.feed_bus_message(msg))
            streamed_center.extend(inc_center.flush_remaining())
            center_all, center_rep, _ = inc_center.finalize()
            self.assertEqual(len(streamed_center), 2)
            self.assertEqual(center_all, streamed_center)
            self.assertEqual(center_rep, report["center"])

            inc_int = IntegratedIncrementalScorer(
                scene, run_id=run.run_id, size_k_m=0.7, miss_k=3.0, warmup_s=15.0
            )
            for msg in edge_msgs:
                inc_int.feed_bus_message(msg)
            for msg in center_msgs:
                inc_int.feed_bus_message(msg)
            _, int_rep = inc_int.finalize()
            self.assertEqual(int_rep, report)

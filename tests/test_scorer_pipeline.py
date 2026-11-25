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

    def test_unlabelled_vs_explicit_empty_truth_and_missing_prerequisites(self) -> None:
        from chongming_scorer.scorer import score_run

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            times = (1_000_000_000, 1_040_000_000)

            def write_scene(name: str, *, truth_mode: str) -> Path:
                scene_dir = root / name
                with TestSceneWriter(
                    scene_dir,
                    scenario_id=name,
                    world_frame_id="enu",
                    clock_domain_id="clk",
                    start_time_ns=times[0],
                    end_time_ns=times[-1],
                    stations=[],
                ) as sw:
                    for idx, ns in enumerate(times):
                        cap = common_pb2.CaptureTime(
                            clock_domain_id="clk", exposure_start_ns=ns, exposure_end_ns=ns
                        )
                        sw.add_frame_index(
                            truth_pb2.FrameIndexEntry(
                                station_id="s1",
                                station_session_id="sess-s1",
                                camera_role=common_pb2.CAMERA_ROLE_MAIN,
                                frame_index=idx,
                                frame_id=f"s1-{idx}",
                                capture_time=cap,
                                station_config_digest="cfg",
                                video_relpath="video/s1-main.mp4",
                                width_px=64,
                                height_px=64,
                            )
                        )
                        if truth_mode == "explicit_empty" or (truth_mode == "partial" and idx == 0):
                            sw.add_truth_frame(
                                truth_pb2.TruthFrame(
                                    world_frame_id="enu",
                                    experiment_time_ns=ns,
                                    step_index=idx,
                                )
                            )
                return scene_dir

            def write_edge_run(name: str, scene_dir: Path, *, with_obs: bool) -> Path:
                scene = TestScenePackage.open(scene_dir)
                run_dir = root / name
                with RunOutputWriter(
                    run_dir,
                    run_id=name,
                    mode="edge",
                    test_scene_path=scene_dir,
                    test_scene_sha256=scene.content_sha256,
                ) as rw:
                    for idx, ns in enumerate(times):
                        cap = common_pb2.CaptureTime(
                            clock_domain_id="clk", exposure_start_ns=ns, exposure_end_ns=ns
                        )
                        obs = (
                            [
                                online_pb2.Observation2D(
                                    centroid_px=common_pb2.Vec2(x=12.0, y=14.0),
                                    local_track_id="t1",
                                )
                            ]
                            if (with_obs and idx == 1)
                            else []
                        )
                        rw.write_edge_message(
                            online_pb2.BusMessage(
                                bus_sequence_no=idx + 1,
                                detection_batch=online_pb2.DetectionBatch(
                                    main_frame_ref=common_pb2.FrameRef(
                                        station_id="s1",
                                        station_session_id="sess-s1",
                                        camera_role=common_pb2.CAMERA_ROLE_MAIN,
                                        frame_id=f"s1-{idx}",
                                    ),
                                    capture_time=cap,
                                    processing_status=common_pb2.PROCESSING_STATUS_PROCESSED,
                                    observations=obs,
                                    decode_duration_ns=1_000_000,
                                    cv_duration_ns=500_000,
                                ),
                            )
                        )
                return run_dir

            # 1. Unlabelled scene (zero-record truth.pb.zst): report unavailable, detections retained.
            unlabelled_scene = write_scene("unlabelled_scene", truth_mode="none")
            unlabelled_run = write_edge_run("unlabelled_run", unlabelled_scene, with_obs=True)
            unlabelled_out = root / "score_unlabelled"
            main(
                [
                    "score",
                    "--scene",
                    str(unlabelled_scene),
                    "--run",
                    str(unlabelled_run),
                    "--output",
                    str(unlabelled_out),
                    "--warmup-s",
                    "0",
                ]
            )
            rep_unlabelled = json.loads((unlabelled_out / "report.json").read_text())
            self.assertEqual(rep_unlabelled["status"], "unavailable")
            self.assertIn("truth_unavailable", rep_unlabelled["reason_codes"])
            self.assertEqual(rep_unlabelled["available_outputs"]["edge"]["detection_batches"], 2)
            self.assertEqual(rep_unlabelled["available_outputs"]["edge"]["observations"], 1)
            self.assertTrue((unlabelled_run / "edge_events.pb.zst").is_file())

            # 2. Explicitly empty truth (TruthFrame with targets=[]): detections count as FP.
            empty_truth_scene = write_scene("empty_truth_scene", truth_mode="explicit_empty")
            empty_truth_run = write_edge_run("empty_truth_run", empty_truth_scene, with_obs=True)
            empty_truth_out = root / "score_empty_truth"
            rep_empty_truth = score_run(
                empty_truth_scene,
                empty_truth_run,
                output_dir=empty_truth_out,
                warmup_s=0.0,
            )
            self.assertEqual(rep_empty_truth["status"], "complete")
            self.assertEqual(rep_empty_truth["aggregate"]["frames"], 2)
            self.assertEqual(rep_empty_truth["aggregate"]["fp"], 1)
            self.assertEqual(rep_empty_truth["aggregate"]["tp"], 0)

            # 3. Partially labelled scene: unlabelled frame (idx=1) must NOT count as FP.
            partial_scene = write_scene("partial_scene", truth_mode="partial")
            partial_run = write_edge_run("partial_run", partial_scene, with_obs=True)
            rep_partial = score_run(
                partial_scene,
                partial_run,
                output_dir=root / "score_partial",
                warmup_s=0.0,
            )
            self.assertEqual(rep_partial["status"], "complete")
            self.assertEqual(rep_partial["aggregate"]["frames"], 1)
            self.assertEqual(rep_partial["aggregate"]["fp"], 0)
            self.assertEqual(rep_partial["stations"]["s1"]["unlabelled_frames"], 1)

            # 4. Mode mismatch (edge run scored as integrated or center): explicit unavailable diagnostic.
            rep_mismatch = score_run(
                empty_truth_scene,
                empty_truth_run,
                output_dir=root / "score_mismatch_int",
                mode="integrated",
                warmup_s=0.0,
            )
            self.assertEqual(rep_mismatch["status"], "unavailable")
            self.assertIn("center_output_unavailable", rep_mismatch["reason_codes"])
            self.assertIn(
                "run_mode_edge_does_not_produce_center_output", rep_mismatch["reason_codes"]
            )
            self.assertEqual(rep_mismatch["available_outputs"]["edge"]["observations"], 1)

            # 5. Center run scored as edge: explicit unavailable diagnostic.
            center_scene_pkg = TestScenePackage.open(empty_truth_scene)
            center_run = root / "center_only_run"
            with RunOutputWriter(
                center_run,
                run_id="center_only_run",
                mode="center",
                test_scene_path=empty_truth_scene,
                test_scene_sha256=center_scene_pkg.content_sha256,
            ) as rw:
                rw.write_center_message(
                    online_pb2.BusMessage(
                        bus_sequence_no=1,
                        progress_report=online_pb2.ProgressReport(
                            source_role=common_pb2.COMPONENT_ROLE_CENTER,
                            source_id="center",
                            session_id="c1",
                            completed_time_ns=times[0],
                            completed_sequence_no=1,
                            finite_end_reached=True,
                        ),
                    )
                )
            rep_center_as_edge = score_run(
                empty_truth_scene,
                center_run,
                output_dir=root / "score_center_as_edge",
                mode="edge",
                warmup_s=0.0,
            )
            self.assertEqual(rep_center_as_edge["status"], "unavailable")
            self.assertIn("edge_events_unavailable", rep_center_as_edge["reason_codes"])
            self.assertIn(
                "run_mode_center_does_not_produce_edge_events",
                rep_center_as_edge["reason_codes"],
            )

            # 6. Manifest-declared missing/corrupted file still fails hard.
            corrupt_run = root / "corrupt_run"
            with RunOutputWriter(
                corrupt_run,
                run_id="corrupt_run",
                mode="edge",
                test_scene_path=empty_truth_scene,
                test_scene_sha256=center_scene_pkg.content_sha256,
            ) as rw:
                rw.write_edge_message(
                    online_pb2.BusMessage(
                        bus_sequence_no=1,
                        progress_report=online_pb2.ProgressReport(
                            source_role=common_pb2.COMPONENT_ROLE_EDGE,
                            source_id="s1",
                            session_id="sess-s1",
                            completed_time_ns=times[0],
                            finite_end_reached=True,
                        ),
                    )
                )
            (corrupt_run / "edge_events.pb.zst").unlink()
            with self.assertRaises(FileNotFoundError):
                score_run(empty_truth_scene, corrupt_run, warmup_s=0.0)

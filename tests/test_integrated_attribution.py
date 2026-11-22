"""Operational responsibility rules retain center and device distinctions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chongming_proto.online import common_pb2, online_pb2
from chongming_proto.truth import truth_pb2
from chongming_scorer.attribution import (
    attribute_run,
    false_output_owner,
    miss_owner,
    unweighted_line_fit,
)
from chongming_scorer.scorer import score_center_run
from chongming_scorer.storage import (
    RunOutputPackage,
    RunOutputWriter,
    TestScenePackage,
    TestSceneWriter,
)


class AttributionTests(unittest.TestCase):
    def test_unweighted_line_fit_recovers_exact_intersection(self) -> None:
        point, rank = unweighted_line_fit(
            [
                ((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)),
                ((10.0, 0.0, 0.0), (-1.0, 1.0, 1.0)),
                ((5.0, 5.0, 0.0), (0.0, 0.0, 1.0)),
            ]
        )
        self.assertEqual(rank, 3)
        self.assertAlmostEqual(point[0], 5.0, places=9)
        self.assertAlmostEqual(point[1], 5.0, places=9)
        self.assertAlmostEqual(point[2], 5.0, places=9)

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

    def test_attribute_run_attributes_device_miss_and_cross_target_false_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scene_dir = root / "scene"
            run_dir = root / "run"
            t_ns = 1_000_000
            cap = common_pb2.CaptureTime(
                clock_domain_id="demo-clock",
                exposure_start_ns=t_ns,
                exposure_end_ns=t_ns,
            )
            with TestSceneWriter(
                scene_dir,
                scenario_id="attr-test",
                world_frame_id="demo-enu",
                clock_domain_id="demo-clock",
                start_time_ns=0,
                end_time_ns=t_ns,
                stations=[],
            ) as sw:
                for idx, st in enumerate(("s1", "s2")):
                    sw.add_frame_index(
                        truth_pb2.FrameIndexEntry(
                            station_id=st,
                            station_session_id=f"sess-{st}",
                            camera_role=common_pb2.CAMERA_ROLE_MAIN,
                            frame_index=0,
                            frame_id=f"{st}-f0",
                            capture_time=cap,
                            station_config_digest="cfg",
                            video_relpath=f"video/{st}-main.mp4",
                            width_px=64,
                            height_px=64,
                        )
                    )
                sw.add_truth_frame(
                    truth_pb2.TruthFrame(
                        world_frame_id="demo-enu",
                        experiment_time_ns=t_ns,
                        step_index=0,
                        targets=[
                            truth_pb2.TargetTruth(
                                target_id="target-a",
                                target_class="sphere",
                                position_enu_m=common_pb2.Vec3(x=0.0, y=50.0, z=10.0),
                                velocity_enu_mps=common_pb2.Vec3(),
                                bounding_radius_m=1.0,
                                body_aabb_diagonal_m=2.0,
                            ),
                            truth_pb2.TargetTruth(
                                target_id="target-b",
                                target_class="sphere",
                                position_enu_m=common_pb2.Vec3(x=20.0, y=50.0, z=10.0),
                                velocity_enu_mps=common_pb2.Vec3(),
                                bounding_radius_m=1.0,
                                body_aabb_diagonal_m=2.0,
                            ),
                        ],
                        projections=[
                            truth_pb2.StationTargetProjection(
                                station_id="s1",
                                target_id="target-a",
                                occlusion_status=truth_pb2.OCCLUSION_STATUS_VISIBLE,
                                projected_centroid_px=common_pb2.Vec2(x=10.0, y=10.0),
                                projected_diagonal_px=4.0,
                                frame_id="s1-f0",
                            ),
                            truth_pb2.StationTargetProjection(
                                station_id="s1",
                                target_id="target-b",
                                occlusion_status=truth_pb2.OCCLUSION_STATUS_VISIBLE,
                                projected_centroid_px=common_pb2.Vec2(x=40.0, y=10.0),
                                projected_diagonal_px=4.0,
                                frame_id="s1-f0",
                            ),
                            truth_pb2.StationTargetProjection(
                                station_id="s2",
                                target_id="target-a",
                                occlusion_status=truth_pb2.OCCLUSION_STATUS_VISIBLE,
                                projected_centroid_px=common_pb2.Vec2(x=10.0, y=10.0),
                                projected_diagonal_px=4.0,
                                frame_id="s2-f0",
                            ),
                            truth_pb2.StationTargetProjection(
                                station_id="s2",
                                target_id="target-b",
                                occlusion_status=truth_pb2.OCCLUSION_STATUS_VISIBLE,
                                projected_centroid_px=common_pb2.Vec2(x=40.0, y=10.0),
                                projected_diagonal_px=4.0,
                                frame_id="s2-f0",
                            ),
                        ],
                    )
                )

            scene = TestScenePackage.open(scene_dir)
            with RunOutputWriter(
                run_dir,
                run_id="run-attr-1",
                mode="integrated",
                test_scene_path=scene_dir,
                test_scene_sha256=scene.content_sha256,
            ) as rw:
                # s1 detects target-a; s2 detects target-b.
                rw.write_edge_message(
                    online_pb2.BusMessage(
                        bus_sequence_no=1,
                        detection_batch=online_pb2.DetectionBatch(
                            main_frame_ref=common_pb2.FrameRef(
                                station_id="s1",
                                station_session_id="sess-s1",
                                camera_role=common_pb2.CAMERA_ROLE_MAIN,
                                frame_id="s1-f0",
                            ),
                            capture_time=cap,
                            processing_status=common_pb2.PROCESSING_STATUS_PROCESSED,
                            observations=[
                                online_pb2.Observation2D(
                                    centroid_px=common_pb2.Vec2(x=10.0, y=10.0),
                                    local_track_id="trk-1",
                                )
                            ],
                        ),
                    )
                )
                rw.write_edge_message(
                    online_pb2.BusMessage(
                        bus_sequence_no=2,
                        detection_batch=online_pb2.DetectionBatch(
                            main_frame_ref=common_pb2.FrameRef(
                                station_id="s2",
                                station_session_id="sess-s2",
                                camera_role=common_pb2.CAMERA_ROLE_MAIN,
                                frame_id="s2-f0",
                            ),
                            capture_time=cap,
                            processing_status=common_pb2.PROCESSING_STATUS_PROCESSED,
                            observations=[
                                online_pb2.Observation2D(
                                    centroid_px=common_pb2.Vec2(x=40.0, y=10.0),
                                    local_track_id="trk-2",
                                )
                            ],
                        ),
                    )
                )
                rw.write_edge_message(
                    online_pb2.BusMessage(
                        bus_sequence_no=3,
                        ray_event=online_pb2.RayEvent(
                            envelope=common_pb2.EventEnvelope(
                                event_type=common_pb2.EVENT_TYPE_RAY,
                                station_id="s1",
                                station_session_id="sess-s1",
                                sequence_no=1,
                            ),
                            frame_id="s1-f0",
                            local_track_id="trk-1",
                            capture_time=cap,
                            ray_origin_enu_m=common_pb2.Vec3(x=-10.0, y=0.0, z=0.0),
                            ray_direction_enu_unit=common_pb2.Vec3(x=0.0, y=1.0, z=0.0),
                        ),
                    )
                )
                rw.write_edge_message(
                    online_pb2.BusMessage(
                        bus_sequence_no=4,
                        ray_event=online_pb2.RayEvent(
                            envelope=common_pb2.EventEnvelope(
                                event_type=common_pb2.EVENT_TYPE_RAY,
                                station_id="s2",
                                station_session_id="sess-s2",
                                sequence_no=1,
                            ),
                            frame_id="s2-f0",
                            local_track_id="trk-2",
                            capture_time=cap,
                            ray_origin_enu_m=common_pb2.Vec3(x=10.0, y=0.0, z=0.0),
                            ray_direction_enu_unit=common_pb2.Vec3(x=0.0, y=1.0, z=0.0),
                        ),
                    )
                )
                rw.write_center_message(
                    online_pb2.BusMessage(
                        bus_sequence_no=1,
                        fusion_measurement=online_pb2.FusionMeasurement(
                            center_session_id="c1",
                            measurement_id="m-cross",
                            clock_domain_id="demo-clock",
                            world_frame_id="demo-enu",
                            reference_time_ns=t_ns,
                            status=common_pb2.FUSION_STATUS_LOCATED,
                            position_enu_m=common_pb2.Vec3(x=10.0, y=30.0, z=10.0),
                            supporting_ray_event_ids=[
                                common_pb2.EventId(
                                    station_id="s1", station_session_id="sess-s1", sequence_no=1
                                ),
                                common_pb2.EventId(
                                    station_id="s2", station_session_id="sess-s2", sequence_no=1
                                ),
                            ],
                        ),
                    )
                )
                rw.write_center_message(
                    online_pb2.BusMessage(
                        bus_sequence_no=2,
                        progress_report=online_pb2.ProgressReport(
                            source_role=common_pb2.COMPONENT_ROLE_CENTER,
                            source_id="center",
                            session_id="c1",
                            completed_time_ns=t_ns,
                            completed_sequence_no=1,
                            finite_end_reached=True,
                        ),
                    )
                )

            run = RunOutputPackage.open(run_dir)
            _, _, center_records = score_center_run(scene, run, size_k=0.7, miss_k=3.0)
            attr = attribute_run(
                scene, run, center_records, gate_px=10.0, size_k_px=None, warmup_s=0.0
            )
            self.assertEqual(attr["misses"], {"device:insufficient_correct_stations": 2})
            self.assertEqual(attr["false_outputs"], {"center:cross_target_combination": 1})
            self.assertEqual(attr["false_output_contributors"]["contains_cross_target_support"], 1)

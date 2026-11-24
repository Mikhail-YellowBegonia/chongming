"""Tests for online Protobuf codecs and TCP bus client handshake/framing."""

from __future__ import annotations

import math
import socket
import threading
import unittest
from collections.abc import Callable

from chongming.bus import (
    PROTOCOL_VERSION,
    BusHandshakeError,
    OnlineBusClient,
    calibration_state_event_from_proto,
    calibration_state_event_to_proto,
    detection_batch_from_proto,
    detection_batch_to_proto,
    diagnostic_event_from_proto,
    diagnostic_event_to_proto,
    fusion_measurement_from_proto,
    fusion_measurement_to_proto,
    progress_report_from_proto,
    progress_report_to_proto,
    ray_event_from_proto,
    ray_event_to_proto,
    sparse_voxel_grid_from_proto,
    sparse_voxel_grid_to_proto,
    station_config_from_proto,
    station_config_to_proto,
    station_event_from_proto,
    station_event_to_proto,
    track_event_from_proto,
    track_event_to_proto,
)
from chongming.contracts import fusion as f
from chongming.contracts import model as m
from chongming.contracts.visualization import ScoredVoxel, VoxelSpaceVisualization
from chongming_proto.online import (
    common_pb2,
    encode_length_prefixed,
    online_pb2,
    read_length_prefixed,
)


def _eye3(scale: float = 1.0) -> m.Matrix3x3:
    return ((scale, 0.0, 0.0), (0.0, scale, 0.0), (0.0, 0.0, scale))


def _eye2(scale: float = 1.0) -> m.Matrix2x2:
    return ((scale, 0.0), (0.0, scale))


def _eye6(scale: float = 1.0) -> m.Matrix6x6:
    return (
        (scale, 0.0, 0.0, 0.0, 0.0, 0.0),
        (0.0, scale, 0.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, scale, 0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0, scale, 0.0, 0.0),
        (0.0, 0.0, 0.0, 0.0, scale, 0.0),
        (0.0, 0.0, 0.0, 0.0, 0.0, scale),
    )


def _sample_station_config() -> m.StationConfig:
    cam = m.CameraModel(
        intrinsics=m.CameraIntrinsics(
            image_width_px=1920,
            image_height_px=1080,
            matrix_px=((1200.0, 0.0, 960.0), (0.0, 1200.0, 540.0), (0.0, 0.0, 1.0)),
        ),
        distortion=m.CameraDistortion(model="none", coefficients=(0.0, 0.0, 0.0, 0.0)),
    )
    return m.StationConfig(
        station_id=m.StationId("s1"),
        config_version="v1",
        config_digest=m.ConfigDigest("cfg-s1"),
        world_frame_id=m.WorldFrameId("enu"),
        geodetic_anchor=m.GeodeticAnchor(
            anchor_id="a1",
            datum="WGS84",
            datum_epoch="2026",
            latitude_deg=52.37,
            longitude_deg=9.73,
            height_m=55.0,
            height_reference="ellipsoidal",
        ),
        main_camera_position_enu_m=(-10.0, 0.0, 5.0),
        baseline_orientation_enu_from_main=_eye3(),
        main_camera=cam,
        auxiliary_camera=cam,
        fixed_rotation_aux_from_main=_eye3(),
        full_rigid_transform_aux_from_main=m.RigidTransform(
            rotation_a_from_b=_eye3(),
            translation_a_m=(0.0, -0.2, 0.0),
        ),
        reference_anchor=m.SurveyedReference(
            reference_id="ref-v1",
            world_frame_id=m.WorldFrameId("enu"),
            landmarks=(
                m.SurveyedLandmark(
                    landmark_id="board-corner-0",
                    position_enu_m=(1.0, 2.0, 0.5),
                    covariance_enu_m2=_eye3(1e-6),
                    visual_description="top-left checkerboard corner",
                ),
            ),
        ),
        reference_scene_version="ref-v1",
        installation_uncertainty=m.InstallationUncertainty(
            state_ref=m.InstallationStateRef(
                station_id=m.StationId("s1"),
                config_digest=m.ConfigDigest("cfg-s1"),
                state_version="inst-v1",
            ),
            position_covariance_enu_m2=_eye3(0.01),
            orientation_covariance_enu_rad2=_eye3(1e-6),
            fixed_extrinsic_covariance_aux_rad2=_eye3(1e-6),
            measurement_source="survey",
        ),
    )


def _sample_capture(ns: int = 1_000_000_000) -> m.CaptureTime:
    return m.CaptureTime(
        clock_domain_id="clk",
        exposure_start_ns=ns,
        exposure_end_ns=ns,
        clock_uncertainty_ns=100,
        rolling_shutter_model="none",
    )


def _sample_score_def(name: str = "score") -> m.ScoreDefinition:
    return m.ScoreDefinition(
        definition_id=name,
        kind=m.ScoreKind.RANKING,
        minimum=0.0,
        maximum=1.0,
        cross_station_comparison_group="g1",
    )


def _run_mock_server(
    handler: Callable[[socket.socket], None],
) -> tuple[int, threading.Thread, list[BaseException]]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = int(server.getsockname()[1])
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            conn, _ = server.accept()
            with conn:
                handler(conn)
        except (OSError, AssertionError, ValueError, EOFError) as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            server.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return port, thread, errors


class BusCodecTests(unittest.TestCase):
    def test_station_config_and_detection_batch_roundtrip(self) -> None:
        cfg = _sample_station_config()
        restored_cfg = station_config_from_proto(station_config_to_proto(cfg))
        self.assertEqual(restored_cfg, cfg)

        capture = _sample_capture()
        frame_ref = m.FrameRef(
            station_id=m.StationId("s1"),
            station_session_id=m.SessionId("sess-1"),
            camera_role=m.CameraRole.MAIN,
            frame_id=m.FrameId("s1-main-0"),
        )
        batch = m.DetectionBatch(
            main_frame_ref=frame_ref,
            capture_time=capture,
            processing_status=m.ProcessingStatus.PROCESSED,
            observations=(
                m.Observation2D(
                    main_frame_ref=frame_ref,
                    capture_time=capture,
                    centroid_px=(320.5, 240.25),
                    centroid_covariance_px2=_eye2(0.25),
                    detection_score=0.95,
                    detection_score_definition=_sample_score_def("det"),
                    local_track_id=m.LocalTrackId("lt-1"),
                    track_quality=0.8,
                    track_quality_definition=_sample_score_def("trk"),
                ),
            ),
            reason_codes=("ok",),
        )
        pb_batch = detection_batch_to_proto(
            batch,
            decoded_pixel_sha256="ab" * 32,
            decode_duration_ns=1_234_567,
            cv_duration_ns=2_345_678,
        )
        self.assertEqual(pb_batch.decoded_pixel_sha256, "ab" * 32)
        self.assertEqual(pb_batch.decode_duration_ns, 1_234_567)
        self.assertEqual(pb_batch.cv_duration_ns, 2_345_678)
        self.assertEqual(detection_batch_from_proto(pb_batch), batch)

    def test_station_events_center_outputs_and_progress_roundtrip(self) -> None:
        capture = _sample_capture(2_000_000_000)
        proc = m.ProcessingTime(clock_domain_id="wall", time_ns=2_500_000_000)
        interval = m.TimeInterval(clock_domain_id="clk", start_ns=0, end_ns=10_000_000_000)
        sol_ref = m.CalibrationSolutionRef(
            station_id=m.StationId("s1"),
            station_session_id=m.SessionId("sess-1"),
            solution_id=m.CalibrationSolutionId("sol-1"),
        )
        solution = m.CalibrationSolution(
            solution_ref=sol_ref,
            station_config_digest=m.ConfigDigest("cfg-s1"),
            world_frame_id=m.WorldFrameId("enu"),
            reference_capture_time=capture,
            accepted_time=proc,
            valid_interval=interval,
            main_camera_position_enu_m=(-10.0, 0.0, 5.0),
            rotation_enu_from_main=_eye3(),
            correction_covariance_enu_rad2=_eye3(1e-7),
            quality=m.CalibrationQuality(
                source="checkerboard",
                matched_reference_count=24,
                reprojection_rmse_px=0.12,
                angular_residual_rad=0.0005,
                reason_codes=("ok",),
                rotation_only_parallax_px=0.02,
            ),
        )
        cal_event = m.CalibrationStateEvent(
            envelope=m.EventEnvelope(
                schema_version="chongming.v1",
                event_type=m.EventType.CALIBRATION_STATE,
                station_id=m.StationId("s1"),
                station_session_id=m.SessionId("sess-1"),
                sequence_no=1,
                station_config_digest=m.ConfigDigest("cfg-s1"),
            ),
            state=m.CalibrationState(
                station_id=m.StationId("s1"),
                station_session_id=m.SessionId("sess-1"),
                station_config_digest=m.ConfigDigest("cfg-s1"),
                state_version=1,
                health=m.CalibrationHealth.VALID,
                solution_ref=sol_ref,
                observed_at=proc,
                last_trusted_time=proc,
                usable_interval=interval,
                reason_codes=("trusted",),
            ),
            solution=solution,
        )
        ray_event = m.RayEvent(
            envelope=m.EventEnvelope(
                schema_version="chongming.v1",
                event_type=m.EventType.RAY,
                station_id=m.StationId("s1"),
                station_session_id=m.SessionId("sess-1"),
                sequence_no=2,
                station_config_digest=m.ConfigDigest("cfg-s1"),
            ),
            frame_id=m.FrameId("f-2"),
            local_track_id=m.LocalTrackId("lt-1"),
            capture_time=capture,
            world_frame_id=m.WorldFrameId("enu"),
            calibration_solution_ref=sol_ref,
            calibration_usage=m.CalibrationUsage.VALID,
            calibration_evidence=m.CalibrationUseEvidence(
                state_version=1,
                last_trusted_time=proc,
                solution_accepted_time=proc,
                solution_reference_capture_time=capture,
                solution_valid_interval=interval,
                usable_interval=interval,
                reason_codes=(),
            ),
            ray_origin_enu_m=(-10.0, 0.0, 5.0),
            ray_direction_enu_unit=(0.0, 1.0, 0.0),
            uncertainty=m.RayUncertainty(
                model="tangent_gaussian_v1",
                tangent_u_enu_unit=(1.0, 0.0, 0.0),
                tangent_v_enu_unit=(0.0, 0.0, 1.0),
                independent_angular_covariance_rad2=_eye2(1e-6),
                independent_origin_covariance_m2=_eye3(0.0),
                shared_installation_state_ref=m.InstallationStateRef(
                    station_id=m.StationId("s1"),
                    config_digest=m.ConfigDigest("cfg-s1"),
                    state_version="inst-v1",
                ),
                shared_calibration_solution_ref=sol_ref,
            ),
            detection_score=0.9,
            detection_score_definition=_sample_score_def("det"),
            track_quality=0.85,
            track_quality_definition=_sample_score_def("trk"),
            quality_flags=("confirmed",),
        )
        diag_event = m.DiagnosticEvent(
            envelope=m.EventEnvelope(
                schema_version="chongming.v1",
                event_type=m.EventType.DIAGNOSTIC,
                station_id=m.StationId("s1"),
                station_session_id=m.SessionId("sess-1"),
                sequence_no=3,
                station_config_digest=m.ConfigDigest("cfg-s1"),
            ),
            related_frame_ref=m.FrameRef(
                station_id=m.StationId("s1"),
                station_session_id=m.SessionId("sess-1"),
                camera_role=m.CameraRole.AUXILIARY,
                frame_id=m.FrameId("aux-1"),
            ),
            capture_time=capture,
            category="calibration",
            reason_codes=("checkerboard_not_found",),
            related_config_ref=m.ConfigDigest("cfg-s1"),
            related_calibration_ref=sol_ref,
        )

        self.assertEqual(
            calibration_state_event_from_proto(calibration_state_event_to_proto(cal_event)),
            cal_event,
        )
        self.assertEqual(ray_event_from_proto(ray_event_to_proto(ray_event)), ray_event)
        self.assertEqual(
            diagnostic_event_from_proto(diagnostic_event_to_proto(diag_event)),
            diag_event,
        )
        for idx, ev in enumerate((cal_event, ray_event, diag_event), start=1):
            bus_msg = online_pb2.BusMessage(
                bus_sequence_no=idx,
                **station_event_to_proto(ev),  # type: ignore[arg-type]
            )
            self.assertEqual(station_event_from_proto(bus_msg), ev)

        meas = m.FusionMeasurement(
            center_session_id=m.SessionId("center-1"),
            measurement_id=m.MeasurementId("m-1"),
            clock_domain_id="clk",
            world_frame_id=m.WorldFrameId("enu"),
            reference_time_ns=2_000_000_000,
            support_exposure_span_ns=500_000,
            status=m.FusionStatus.LOCATED,
            position_enu_m=(1.0, 50.0, 10.0),
            conditional_position_covariance_enu_m2=_eye3(0.5),
            motion_alignment=m.MotionAlignmentEvidence(
                prior_track_id=m.TrackId("trk-1"),
                velocity_enu_mps=(2.0, 0.0, 0.5),
                velocity_projection_s=_eye3(0.03),
                independent_position_covariance_enu_m2=_eye3(0.4),
            ),
            supporting_ray_event_ids=(
                (m.StationId("s1"), m.SessionId("sess-1"), 2),
                (m.StationId("s2"), m.SessionId("sess-2"), 2),
            ),
            supporting_station_ids=(m.StationId("s1"), m.StationId("s2")),
            residuals_m=(0.05, 0.07),
            geometry_condition=3.2,
            minimum_intersection_angle_rad=0.35,
            association_hypothesis_id="h-1",
            reason_codes=("located",),
        )
        self.assertEqual(
            fusion_measurement_from_proto(fusion_measurement_to_proto(meas)),
            meas,
        )

        trk = m.TrackEvent(
            center_session_id=m.SessionId("center-1"),
            track_id=m.TrackId("trk-1"),
            clock_domain_id="clk",
            world_frame_id=m.WorldFrameId("enu"),
            event_time_ns=2_000_000_000,
            lifecycle=m.TrackLifecycle.CONFIRMED,
            position_enu_m=(1.0, 50.0, 10.0),
            velocity_enu_mps=(2.0, 0.0, 0.5),
            conditional_state_covariance=_eye6(0.25),
            source_measurement_refs=(
                m.MeasurementRef(
                    center_session_id=m.SessionId("center-1"),
                    measurement_id=m.MeasurementId("m-1"),
                ),
            ),
            reason_codes=("confirmed",),
        )
        self.assertEqual(track_event_from_proto(track_event_to_proto(trk)), trk)

        prog = f.StationEventTimeProgress(
            station_id=m.StationId("s1"),
            station_session_id=m.SessionId("sess-1"),
            station_config_digest=m.ConfigDigest("cfg-s1"),
            completed_capture_time=m.CaptureTime(
                clock_domain_id="clk",
                exposure_start_ns=2_000_000_000,
                exposure_end_ns=2_000_000_000,
                clock_uncertainty_ns=0,
                rolling_shutter_model="none",
            ),
            received_at=proc,
            stream_ended=False,
        )
        pb_prog = progress_report_to_proto(prog, completed_sequence_no=5)
        self.assertEqual(
            progress_report_from_proto(
                pb_prog,
                station_config_digest=m.ConfigDigest("cfg-s1"),
                capture_clock_domain_id="clk",
                received_at=proc,
            ),
            prog,
        )
        zero_end = f.StationEventTimeProgress(
            station_id=m.StationId("s1"),
            station_session_id=m.SessionId("sess-1"),
            station_config_digest=m.ConfigDigest("cfg-s1"),
            completed_capture_time=None,
            received_at=proc,
            stream_ended=True,
        )
        self.assertEqual(
            progress_report_from_proto(
                progress_report_to_proto(zero_end, completed_sequence_no=0),
                station_config_digest=m.ConfigDigest("cfg-s1"),
                capture_clock_domain_id="clk",
                received_at=proc,
                has_completed_frame=False,
            ),
            zero_end,
        )

        voxels = VoxelSpaceVisualization(
            search_box_min_enu_m=(-50.0, 0.0, 0.0),
            search_box_max_enu_m=(-40.0, 10.0, 10.0),
            voxel_step_enu_m=(5.0, 5.0, 5.0),
            scored_voxels=(ScoredVoxel(grid_index=(0, 1, 1), score=2, supporting_station_count=2),),
            max_score=2,
        )
        pb_voxels = sparse_voxel_grid_to_proto(
            center_session_id=m.SessionId("center-1"),
            world_frame_id=m.WorldFrameId("enu"),
            window_start_ns=0,
            window_end_ns=1_000_000,
            snapshot=voxels,
        )
        restored_voxels = sparse_voxel_grid_from_proto(pb_voxels)
        self.assertEqual(restored_voxels.scored_voxels, voxels.scored_voxels)
        self.assertEqual(restored_voxels.max_score, 2)

    def test_boundary_validation_rejects_invalid_values(self) -> None:
        cfg_pb = station_config_to_proto(_sample_station_config())
        cfg_pb.main_camera_position_enu_m.x = math.nan
        with self.assertRaisesRegex(ValueError, "finite"):
            station_config_from_proto(cfg_pb)

        bad_meas = online_pb2.FusionMeasurement(
            center_session_id="c1",
            measurement_id="m1",
            clock_domain_id="clk",
            world_frame_id="enu",
            status=common_pb2.FUSION_STATUS_UNSPECIFIED,
        )
        with self.assertRaisesRegex(ValueError, "FusionStatus"):
            fusion_measurement_from_proto(bad_meas)

        bad_cfg = _sample_station_config()
        bad_cfg_pb = station_config_to_proto(bad_cfg)
        bad_cfg_pb.installation_uncertainty.position_covariance_enu_m2.values[:] = [1.0, 2.0]
        with self.assertRaisesRegex(ValueError, "9 values"):
            station_config_from_proto(bad_cfg_pb)


class OnlineBusClientTests(unittest.TestCase):
    def test_handshake_and_message_exchange_over_tcp(self) -> None:
        seen_hello: list[online_pb2.SessionHello] = []

        def handle(conn: socket.socket) -> None:
            reader = conn.makefile("rb")
            try:
                hello_msg = read_length_prefixed(reader, online_pb2.BusMessage)
                assert hello_msg is not None
                seen_hello.append(hello_msg.session_hello)
                conn.sendall(
                    encode_length_prefixed(
                        online_pb2.BusMessage(
                            bus_sequence_no=1,
                            session_ack=online_pb2.SessionAck(
                                accepted=True,
                                reason="ok",
                                run_id="run-tcp-1",
                            ),
                        )
                    )
                )
                cfg_msg = read_length_prefixed(reader, online_pb2.BusMessage)
                assert cfg_msg is not None
                self.assertEqual(cfg_msg.bus_sequence_no, 2)
                self.assertEqual(cfg_msg.WhichOneof("payload"), "station_config")
                conn.sendall(
                    encode_length_prefixed(
                        online_pb2.BusMessage(
                            bus_sequence_no=2,
                            progress_report=online_pb2.ProgressReport(
                                source_role=common_pb2.COMPONENT_ROLE_CENTER,
                                source_id="center",
                                session_id="c-1",
                                completed_time_ns=500,
                                completed_sequence_no=1,
                                finite_end_reached=True,
                            ),
                        )
                    )
                )
            finally:
                reader.close()

        port, thread, errors = _run_mock_server(handle)
        with OnlineBusClient("127.0.0.1", port, wall_clock_ns=lambda: 123456) as client:
            ack = client.handshake(
                role=common_pb2.COMPONENT_ROLE_EDGE,
                instance_id="edge-1",
                session_id="sess-1",
                station_ids=("s1",),
            )
            self.assertEqual(ack.run_id, "run-tcp-1")
            sent = client.send_payload(
                station_config=station_config_to_proto(_sample_station_config())
            )
            self.assertEqual(sent.bus_sequence_no, 2)
            self.assertEqual(sent.sent_wall_time_ns, 123456)
            reply = client.receive()
            assert reply is not None
            self.assertEqual(reply.WhichOneof("payload"), "progress_report")
            self.assertIsNone(client.receive())
        thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(len(seen_hello), 1)
        self.assertEqual(seen_hello[0].protocol_version, PROTOCOL_VERSION)
        self.assertEqual(list(seen_hello[0].station_ids), ["s1"])

    def test_rejected_handshake_raises_bus_handshake_error(self) -> None:
        def handle(conn: socket.socket) -> None:
            reader = conn.makefile("rb")
            try:
                read_length_prefixed(reader, online_pb2.BusMessage)
                conn.sendall(
                    encode_length_prefixed(
                        online_pb2.BusMessage(
                            bus_sequence_no=1,
                            session_ack=online_pb2.SessionAck(
                                accepted=False,
                                reason="protocol mismatch",
                            ),
                        )
                    )
                )
            finally:
                reader.close()

        port, thread, errors = _run_mock_server(handle)
        with (
            OnlineBusClient("127.0.0.1", port) as client,
            self.assertRaisesRegex(BusHandshakeError, "protocol mismatch"),
        ):
            client.handshake(
                role=common_pb2.COMPONENT_ROLE_CENTER,
                instance_id="center-1",
                session_id="c-1",
            )
        thread.join(timeout=5)
        self.assertEqual(errors, [])

    def test_unexpected_reply_or_early_eof_raises_handshake_error(self) -> None:
        def handle_eof(conn: socket.socket) -> None:
            reader = conn.makefile("rb")
            try:
                read_length_prefixed(reader, online_pb2.BusMessage)
            finally:
                reader.close()

        port, thread, errors = _run_mock_server(handle_eof)
        with (
            OnlineBusClient("127.0.0.1", port) as client,
            self.assertRaisesRegex(BusHandshakeError, "closed before"),
        ):
            client.handshake(
                role=common_pb2.COMPONENT_ROLE_EDGE,
                instance_id="edge-1",
                session_id="s-1",
            )
        thread.join(timeout=5)
        self.assertEqual(errors, [])

        def handle_wrong_msg(conn: socket.socket) -> None:
            reader = conn.makefile("rb")
            try:
                read_length_prefixed(reader, online_pb2.BusMessage)
                conn.sendall(
                    encode_length_prefixed(
                        online_pb2.BusMessage(
                            bus_sequence_no=1,
                            progress_report=online_pb2.ProgressReport(
                                source_role=common_pb2.COMPONENT_ROLE_CENTER,
                                source_id="center",
                                session_id="c-1",
                            ),
                        )
                    )
                )
            finally:
                reader.close()

        port2, thread2, errors2 = _run_mock_server(handle_wrong_msg)
        with (
            OnlineBusClient("127.0.0.1", port2) as client,
            self.assertRaisesRegex(BusHandshakeError, "expected SessionAck"),
        ):
            client.handshake(
                role=common_pb2.COMPONENT_ROLE_EDGE,
                instance_id="edge-1",
                session_id="s-1",
            )
        thread2.join(timeout=5)
        self.assertEqual(errors2, [])

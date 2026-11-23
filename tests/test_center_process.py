"""Tests for standalone online CenterProcess and Edge->V-Switch->Center bus integration."""

from __future__ import annotations

import math
import socket
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO

import numpy as np

from chongming.bus import (
    CenterProcess,
    EdgeProcess,
    OnlineBusClient,
    calibration_state_event_to_proto,
    load_fusion_policy,
    ray_event_to_proto,
    station_config_to_proto,
)
from chongming.bus.center_process import main as center_bus_main
from chongming.composition.center import reference_policy
from chongming.contracts import device as d
from chongming.contracts import fusion as f
from chongming.contracts import model as m
from chongming.contracts.json_codec import dump_json as _json
from chongming.modules.device import OPENCV_CALIBRATION_ESTIMATOR_ID
from chongming_proto.online import (
    common_pb2,
    encode_length_prefixed,
    online_pb2,
    read_length_prefixed,
)
from tests.test_detection import detection_policy, tracking_policy
from tests.test_device_rays import PIXEL_COVARIANCE
from tests.test_device_rays import policy as ray_policy


def _eye3(scale: float = 1.0) -> m.Matrix3x3:
    return (
        (scale, 0.0, 0.0),
        (0.0, scale, 0.0),
        (0.0, 0.0, scale),
    )


def _eye2(scale: float = 1.0) -> m.Matrix2x2:
    return ((scale, 0.0), (0.0, scale))


def _capture_time(time_ns: int, clock_id: str = "clk") -> m.CaptureTime:
    return m.CaptureTime(
        clock_domain_id=clock_id,
        exposure_start_ns=time_ns,
        exposure_end_ns=time_ns,
        clock_uncertainty_ns=0,
        rolling_shutter_model="none",
    )


def _station_config_and_aux(
    station_id: str,
    position_enu_m: m.Vec3M,
    *,
    width: int = 640,
    height: int = 480,
) -> tuple[m.StationConfig, bytes]:
    fx = 800.0
    fy = 800.0
    cx = width / 2.0
    cy = height / 2.0
    cam = m.CameraModel(
        intrinsics=m.CameraIntrinsics(
            image_width_px=width,
            image_height_px=height,
            matrix_px=((fx, 0.0, cx), (0.0, fy, cy), (0.0, 0.0, 1.0)),
        ),
        distortion=m.CameraDistortion(model="none", coefficients=()),
    )
    board_w, board_h, z_cam = 0.297, 0.210, 1.0
    px_base, py_base, pz_base = position_enu_m
    corners = (
        (px_base - board_w / 2.0, py_base - board_h / 2.0, pz_base + z_cam),
        (px_base + board_w / 2.0, py_base - board_h / 2.0, pz_base + z_cam),
        (px_base + board_w / 2.0, py_base + board_h / 2.0, pz_base + z_cam),
        (px_base - board_w / 2.0, py_base + board_h / 2.0, pz_base + z_cam),
    )
    landmarks = tuple(
        m.SurveyedLandmark(
            landmark_id=f"{station_id}-corner-{i}",
            position_enu_m=pt,
            covariance_enu_m2=_eye3(0.0),
            visual_description="checkerboard corner",
        )
        for i, pt in enumerate(corners)
    )
    img = np.full((height, width, 3), 240, dtype=np.uint8)
    sq = 0.035
    for r in range(5):
        for c in range(7):
            val = 15 if (r + c) % 2 == 0 else 240
            x0 = (c - 3.5) * sq
            x1 = x0 + sq
            y0 = (r - 2.5) * sq
            y1 = y0 + sq
            px0 = round(fx * (x0 / z_cam) + cx + 0.5)
            px1 = round(fx * (x1 / z_cam) + cx + 0.5)
            py0 = round(fy * (y0 / z_cam) + cy + 0.5)
            py1 = round(fy * (y1 / z_cam) + cy + 0.5)
            img[py0:py1, px0:px1] = val

    digest = m.ConfigDigest(f"cfg-{station_id}")
    cfg = m.StationConfig(
        station_id=m.StationId(station_id),
        config_version="v1",
        config_digest=digest,
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
        main_camera_position_enu_m=position_enu_m,
        baseline_orientation_enu_from_main=_eye3(),
        main_camera=cam,
        auxiliary_camera=cam,
        fixed_rotation_aux_from_main=_eye3(),
        full_rigid_transform_aux_from_main=m.RigidTransform(
            rotation_a_from_b=_eye3(),
            translation_a_m=(0.0, 0.0, 0.0),
        ),
        reference_anchor=m.SurveyedReference(
            reference_id=f"{station_id}-ref",
            world_frame_id=m.WorldFrameId("enu"),
            landmarks=landmarks,
        ),
        reference_scene_version="ref-v1",
        installation_uncertainty=m.InstallationUncertainty(
            state_ref=m.InstallationStateRef(
                station_id=m.StationId(station_id),
                config_digest=digest,
                state_version="inst-v1",
            ),
            position_covariance_enu_m2=_eye3(0.0),
            orientation_covariance_enu_rad2=_eye3(0.0),
            fixed_extrinsic_covariance_aux_rad2=_eye3(0.0),
            measurement_source="synthetic",
        ),
    )
    return cfg, img.tobytes()


def _render_spot(
    cfg: m.StationConfig,
    target_enu_m: m.Vec3M,
    *,
    width: int = 640,
    height: int = 480,
) -> bytes:
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    ox, oy, oz = cfg.main_camera_position_enu_m
    dx = target_enu_m[0] - ox
    dy = target_enu_m[1] - oy
    dz = target_enu_m[2] - oz
    k = cfg.main_camera.intrinsics.matrix_px
    fx, cx = k[0][0], k[0][2]
    fy, cy = k[1][1], k[1][2]
    u = round(fx * (dx / dz) + cx)
    v = round(fy * (dy / dz) + cy)
    arr[max(0, v - 2) : min(height, v + 3), max(0, u - 2) : min(width, u + 3)] = 255
    return arr.tobytes()


def _unit_and_tangent(vec: tuple[float, float, float]) -> tuple[m.Vec3, m.Vec3, m.Vec3]:
    norm = math.sqrt(sum(c * c for c in vec))
    d_unit = (vec[0] / norm, vec[1] / norm, vec[2] / norm)
    ref = (1.0, 0.0, 0.0) if abs(d_unit[0]) < 0.9 else (0.0, 1.0, 0.0)
    u_raw = np.cross(np.asarray(ref), np.asarray(d_unit))
    u_unit = u_raw / float(np.linalg.norm(u_raw))
    v_unit = np.cross(np.asarray(d_unit), u_unit)
    return (
        d_unit,
        (float(u_unit[0]), float(u_unit[1]), float(u_unit[2])),
        (float(v_unit[0]), float(v_unit[1]), float(v_unit[2])),
    )


def _calibration_solution(cfg: m.StationConfig, *, clock_id: str = "clk") -> m.CalibrationSolution:
    sid = cfg.station_id
    return m.CalibrationSolution(
        solution_ref=m.CalibrationSolutionRef(
            station_id=sid,
            station_session_id=m.SessionId(f"sess-{sid}"),
            solution_id=m.CalibrationSolutionId(f"sol-{sid}"),
        ),
        station_config_digest=cfg.config_digest,
        world_frame_id=cfg.world_frame_id,
        accepted_time=m.ProcessingTime(clock_domain_id=clock_id, time_ns=0),
        reference_capture_time=_capture_time(0, clock_id),
        valid_interval=m.TimeInterval(clock_domain_id=clock_id, start_ns=0, end_ns=10**10),
        main_camera_position_enu_m=cfg.main_camera_position_enu_m,
        rotation_enu_from_main=_eye3(),
        correction_covariance_enu_rad2=_eye3(0.0),
        quality=m.CalibrationQuality(
            source="synthetic",
            matched_reference_count=4,
            reprojection_rmse_px=0.0,
            angular_residual_rad=0.0,
            reason_codes=(),
        ),
    )


def _online_fusion_policy(
    *,
    timeout_basis: f.StreamTimeoutBasis = f.StreamTimeoutBasis.CAPTURE_TIME,
    awaiting_first_event_timeout_ns: int = 10**9,
    idle_after_ns: int = 10**9,
    offline_after_ns: int = 3 * 10**9,
) -> f.FusionPolicy:
    base = reference_policy(
        search=m.VoxelSearchSpec(
            world_frame_id=m.WorldFrameId("enu"),
            min_enu_m=(-40.0, -40.0, 20.0),
            max_enu_m=(80.0, 40.0, 140.0),
            step_enu_m=(5.0, 5.0, 5.0),
            forward_range_m=None,
        ),
        clock_domain_id="clk",
        window_width_ns=100_000_000,
    )
    return replace(
        base,
        windows=replace(
            base.windows,
            timeout_basis=timeout_basis,
            awaiting_first_event_timeout_ns=awaiting_first_event_timeout_ns,
            idle_after_ns=idle_after_ns,
            offline_after_ns=offline_after_ns,
        ),
    )


def _device_policy() -> d.DevicePolicy:
    return d.DevicePolicy(
        detection=detection_policy(
            background_warmup_frame_count=1,
            background_learning_rate=0.0,
        ),
        tracking=tracking_policy(),
        calibration=d.CalibrationPolicy(
            update_period_ns=0,
            stale_after_ns=2_000_000_000,
            recovery_success_count=1,
            minimum_reference_count=4,
            maximum_reprojection_rmse_px=2.0,
            maximum_rotation_only_parallax_px=None,
            estimator_id=OPENCV_CALIBRATION_ESTIMATOR_ID,
        ),
        rays=replace(ray_policy(), fallback_centroid_covariance_px2=PIXEL_COVARIANCE),
    )


def _start_tcp_server(
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


class CenterProcessTests(unittest.TestCase):
    def test_center_process_replay_with_capture_timeout_and_diagnostics(self) -> None:
        s1_cfg, _ = _station_config_and_aux("s1", (0.0, 0.0, 0.0))
        s2_cfg, _ = _station_config_and_aux("s2", (40.0, 0.0, 0.0))
        sols = {
            s1_cfg.station_id: _calibration_solution(s1_cfg),
            s2_cfg.station_id: _calibration_solution(s2_cfg),
        }
        truths = [
            (10.0, 2.0, 80.0),
            (11.0, 2.5, 80.0),
            (12.0, 3.0, 80.0),
            (13.0, 3.5, 80.0),
        ]
        step_times_ns = [50_000_000, 150_000_000, 250_000_000, 350_000_000]
        outbound: list[online_pb2.BusMessage] = []

        def drain(reader: BinaryIO) -> list[online_pb2.BusMessage]:
            collected: list[online_pb2.BusMessage] = []
            while True:
                msg = read_length_prefixed(reader, online_pb2.BusMessage)
                if msg is None:
                    break
                collected.append(msg)
            return collected

        def server_handler(conn: socket.socket) -> None:
            reader = conn.makefile("rb")
            try:
                hello = read_length_prefixed(reader, online_pb2.BusMessage)
                assert hello is not None
                self.assertEqual(hello.WhichOneof("payload"), "session_hello")
                self.assertEqual(hello.session_hello.role, common_pb2.COMPONENT_ROLE_CENTER)
                conn.sendall(
                    encode_length_prefixed(
                        online_pb2.BusMessage(
                            bus_sequence_no=1,
                            session_ack=online_pb2.SessionAck(accepted=True, run_id="run-1"),
                        )
                    )
                )
                seq = 2
                for cfg in (s1_cfg, s2_cfg):
                    conn.sendall(
                        encode_length_prefixed(
                            online_pb2.BusMessage(
                                bus_sequence_no=seq,
                                station_config=station_config_to_proto(cfg),
                            )
                        )
                    )
                    seq += 1
                    sol = sols[cfg.station_id]
                    cal_ev = m.CalibrationStateEvent(
                        envelope=m.EventEnvelope(
                            schema_version="event-v1",
                            event_type=m.EventType.CALIBRATION_STATE,
                            station_id=cfg.station_id,
                            station_session_id=sol.solution_ref.station_session_id,
                            sequence_no=1,
                            station_config_digest=cfg.config_digest,
                        ),
                        state=m.CalibrationState(
                            station_id=cfg.station_id,
                            station_session_id=sol.solution_ref.station_session_id,
                            station_config_digest=cfg.config_digest,
                            state_version=1,
                            health=m.CalibrationHealth.VALID,
                            solution_ref=sol.solution_ref,
                            observed_at=sol.accepted_time,
                            last_trusted_time=sol.accepted_time,
                            usable_interval=sol.valid_interval,
                            reason_codes=(),
                        ),
                        solution=sol,
                    )
                    conn.sendall(
                        encode_length_prefixed(
                            online_pb2.BusMessage(
                                bus_sequence_no=seq,
                                calibration_state=calibration_state_event_to_proto(cal_ev),
                            )
                        )
                    )
                    seq += 1

                for step_idx, (t_ns, target) in enumerate(zip(step_times_ns, truths, strict=True)):
                    if step_idx == 1:
                        # Exceed 1ms wall timeout deliberately to prove CAPTURE_TIME does not drop stations.
                        time.sleep(0.03)
                    for cfg in (s1_cfg, s2_cfg):
                        sol = sols[cfg.station_id]
                        ox, oy, oz = cfg.main_camera_position_enu_m
                        d_unit, u_unit, v_unit = _unit_and_tangent(
                            (target[0] - ox, target[1] - oy, target[2] - oz)
                        )
                        ray = m.RayEvent(
                            envelope=m.EventEnvelope(
                                schema_version="event-v1",
                                event_type=m.EventType.RAY,
                                station_id=cfg.station_id,
                                station_session_id=sol.solution_ref.station_session_id,
                                sequence_no=step_idx + 2,
                                station_config_digest=cfg.config_digest,
                            ),
                            frame_id=m.FrameId(f"frame-{cfg.station_id}-{step_idx}"),
                            local_track_id=m.LocalTrackId("trk-1"),
                            capture_time=_capture_time(t_ns),
                            world_frame_id=m.WorldFrameId("enu"),
                            calibration_solution_ref=sol.solution_ref,
                            calibration_usage=m.CalibrationUsage.VALID,
                            calibration_evidence=m.CalibrationUseEvidence(
                                state_version=1,
                                last_trusted_time=sol.accepted_time,
                                solution_accepted_time=sol.accepted_time,
                                solution_reference_capture_time=sol.reference_capture_time,
                                solution_valid_interval=sol.valid_interval,
                                usable_interval=sol.valid_interval,
                                reason_codes=(),
                            ),
                            ray_origin_enu_m=cfg.main_camera_position_enu_m,
                            ray_direction_enu_unit=d_unit,
                            uncertainty=m.RayUncertainty(
                                model="tangent_gaussian_v1",
                                tangent_u_enu_unit=u_unit,
                                tangent_v_enu_unit=v_unit,
                                independent_angular_covariance_rad2=_eye2(1e-6),
                                independent_origin_covariance_m2=_eye3(0.0),
                                shared_installation_state_ref=cfg.installation_uncertainty.state_ref,
                                shared_calibration_solution_ref=sol.solution_ref,
                            ),
                            detection_score=10.0,
                            detection_score_definition=m.ScoreDefinition(
                                definition_id="snr",
                                kind=m.ScoreKind.RANKING,
                                minimum=0.0,
                                maximum=None,
                                cross_station_comparison_group=None,
                            ),
                            track_quality=1.0,
                            track_quality_definition=None,
                            quality_flags=(),
                        )
                        conn.sendall(
                            encode_length_prefixed(
                                online_pb2.BusMessage(
                                    bus_sequence_no=seq,
                                    ray_event=ray_event_to_proto(ray),
                                )
                            )
                        )
                        seq += 1
                        conn.sendall(
                            encode_length_prefixed(
                                online_pb2.BusMessage(
                                    bus_sequence_no=seq,
                                    progress_report=online_pb2.ProgressReport(
                                        source_role=common_pb2.COMPONENT_ROLE_EDGE,
                                        source_id=str(cfg.station_id),
                                        session_id=str(sol.solution_ref.station_session_id),
                                        completed_time_ns=t_ns,
                                        completed_sequence_no=step_idx + 2,
                                        finite_end_reached=False,
                                    ),
                                )
                            )
                        )
                        seq += 1

                for cfg in (s1_cfg, s2_cfg):
                    sol = sols[cfg.station_id]
                    conn.sendall(
                        encode_length_prefixed(
                            online_pb2.BusMessage(
                                bus_sequence_no=seq,
                                progress_report=online_pb2.ProgressReport(
                                    source_role=common_pb2.COMPONENT_ROLE_EDGE,
                                    source_id=str(cfg.station_id),
                                    session_id=str(sol.solution_ref.station_session_id),
                                    completed_time_ns=step_times_ns[-1],
                                    completed_sequence_no=len(step_times_ns) + 1,
                                    finite_end_reached=True,
                                ),
                            )
                        )
                    )
                    seq += 1
                conn.shutdown(socket.SHUT_WR)
                outbound.extend(drain(reader))
            finally:
                reader.close()

        port, thread, errors = _start_tcp_server(server_handler)
        policy = _online_fusion_policy(
            timeout_basis=f.StreamTimeoutBasis.CAPTURE_TIME,
            awaiting_first_event_timeout_ns=1_000_000,
            idle_after_ns=1_000_000,
            offline_after_ns=2_000_000,
        )
        with OnlineBusClient("127.0.0.1", port, timeout_s=5.0) as client:
            ack = CenterProcess(
                client,
                policy,
                instance_id="center-test",
                session_id="center-sess-1",
                station_ids=("s1", "s2"),
                voxel_diagnostics_enabled=True,
            ).run()
        thread.join(timeout=5.0)
        self.assertEqual(errors, [])
        self.assertTrue(ack.accepted)

        measurements = [
            msg.fusion_measurement
            for msg in outbound
            if msg.WhichOneof("payload") == "fusion_measurement"
        ]
        located = [m for m in measurements if m.status == common_pb2.FUSION_STATUS_LOCATED]
        self.assertEqual(len(located), 4)
        for meas, truth in zip(located, truths, strict=True):
            err = math.dist(
                (meas.position_enu_m.x, meas.position_enu_m.y, meas.position_enu_m.z),
                truth,
            )
            self.assertLess(err, 1e-4)

        tracks = [msg.track_event for msg in outbound if msg.WhichOneof("payload") == "track_event"]
        self.assertTrue(any(t.lifecycle == common_pb2.TRACK_LIFECYCLE_CONFIRMED for t in tracks))

        grids = [
            msg.sparse_voxel_grid
            for msg in outbound
            if msg.WhichOneof("payload") == "sparse_voxel_grid"
        ]
        self.assertGreaterEqual(len(grids), 1)
        self.assertGreater(len(grids[0].cells), 0)

        reports = [
            msg.progress_report
            for msg in outbound
            if msg.WhichOneof("payload") == "progress_report"
        ]
        self.assertTrue(all(r.source_role == common_pb2.COMPONENT_ROLE_CENTER for r in reports))
        window_reports = [r for r in reports if not r.finite_end_reached]
        self.assertEqual([r.completed_time_ns for r in window_reports], step_times_ns)
        end_reports = [r for r in reports if r.finite_end_reached]
        self.assertEqual(len(end_reports), 1)
        self.assertEqual(end_reports[0].completed_time_ns, step_times_ns[-1])

    def test_integrated_edge_to_center_bus_pipeline(self) -> None:
        width, height = 640, 480
        s1_cfg, aux1 = _station_config_and_aux("s1", (0.0, 0.0, 0.0), width=width, height=height)
        s2_cfg, aux2 = _station_config_and_aux("s2", (40.0, 0.0, 0.0), width=width, height=height)
        dev_policy = _device_policy()
        bg = bytes(width * height * 3)
        targets = [
            (50_000_000, (12.0, 4.0, 80.0)),
            (150_000_000, (13.5, 4.5, 80.0)),
            (250_000_000, (15.0, 5.0, 80.0)),
            (350_000_000, (16.5, 5.5, 80.0)),
        ]

        forwarded_queue: list[online_pb2.BusMessage] = []
        queue_cond = threading.Condition()
        edge_done = False

        def edge_vswitch(conn: socket.socket) -> None:
            nonlocal edge_done
            reader = conn.makefile("rb")
            try:
                hello = read_length_prefixed(reader, online_pb2.BusMessage)
                assert hello is not None
                self.assertEqual(hello.session_hello.role, common_pb2.COMPONENT_ROLE_EDGE)
                conn.sendall(
                    encode_length_prefixed(
                        online_pb2.BusMessage(
                            bus_sequence_no=1,
                            session_ack=online_pb2.SessionAck(accepted=True, run_id="e2e-1"),
                        )
                    )
                )
                seq = 2
                for cfg in (s1_cfg, s2_cfg):
                    cfg_msg = online_pb2.BusMessage(
                        bus_sequence_no=seq,
                        station_config=station_config_to_proto(cfg),
                    )
                    conn.sendall(encode_length_prefixed(cfg_msg))
                    with queue_cond:
                        forwarded_queue.append(cfg_msg)
                        queue_cond.notify_all()
                    seq += 1

                def forward_until_progress(expect_end_t: int | None = None) -> None:
                    while True:
                        msg = read_length_prefixed(reader, online_pb2.BusMessage)
                        assert msg is not None
                        kind = msg.WhichOneof("payload")
                        if kind in {
                            "calibration_state",
                            "ray_event",
                            "diagnostic_event",
                            "progress_report",
                        }:
                            with queue_cond:
                                forwarded_queue.append(msg)
                                queue_cond.notify_all()
                        if (kind == "calibration_state" and expect_end_t is None) or (
                            kind == "progress_report"
                            and expect_end_t is not None
                            and msg.progress_report.completed_time_ns == expect_end_t
                        ):
                            break

                for cfg, aux_pixels in ((s1_cfg, aux1), (s2_cfg, aux2)):
                    cap0 = common_pb2.CaptureTime(
                        clock_domain_id="clk",
                        exposure_start_ns=0,
                        exposure_end_ns=0,
                        rolling_shutter_model="none",
                    )
                    conn.sendall(
                        encode_length_prefixed(
                            online_pb2.BusMessage(
                                bus_sequence_no=seq,
                                video_packet=online_pb2.FramedVideoPacket(
                                    station_id=str(cfg.station_id),
                                    station_session_id=f"img-{cfg.station_id}",
                                    camera_role=common_pb2.CAMERA_ROLE_AUXILIARY,
                                    frame_id=f"{cfg.station_id}-aux-0",
                                    frame_index=0,
                                    capture_time=cap0,
                                    station_config_digest=str(cfg.config_digest),
                                    width_px=width,
                                    height_px=height,
                                    codec=common_pb2.VIDEO_CODEC_RAW,
                                    pixel_format=common_pb2.PIXEL_FORMAT_RGB8,
                                    row_stride_bytes=width * 3,
                                    payload=aux_pixels,
                                ),
                            )
                        )
                    )
                    seq += 1
                    forward_until_progress(None)

                    conn.sendall(
                        encode_length_prefixed(
                            online_pb2.BusMessage(
                                bus_sequence_no=seq,
                                video_packet=online_pb2.FramedVideoPacket(
                                    station_id=str(cfg.station_id),
                                    station_session_id=f"img-{cfg.station_id}",
                                    camera_role=common_pb2.CAMERA_ROLE_MAIN,
                                    frame_id=f"{cfg.station_id}-bg",
                                    frame_index=0,
                                    capture_time=cap0,
                                    station_config_digest=str(cfg.config_digest),
                                    width_px=width,
                                    height_px=height,
                                    codec=common_pb2.VIDEO_CODEC_RAW,
                                    pixel_format=common_pb2.PIXEL_FORMAT_RGB8,
                                    row_stride_bytes=width * 3,
                                    payload=bg,
                                ),
                            )
                        )
                    )
                    seq += 1
                    forward_until_progress(0)

                for frame_idx, (t_ns, target) in enumerate(targets, start=1):
                    cap = common_pb2.CaptureTime(
                        clock_domain_id="clk",
                        exposure_start_ns=t_ns,
                        exposure_end_ns=t_ns,
                        rolling_shutter_model="none",
                    )
                    for cfg in (s1_cfg, s2_cfg):
                        conn.sendall(
                            encode_length_prefixed(
                                online_pb2.BusMessage(
                                    bus_sequence_no=seq,
                                    video_packet=online_pb2.FramedVideoPacket(
                                        station_id=str(cfg.station_id),
                                        station_session_id=f"img-{cfg.station_id}",
                                        camera_role=common_pb2.CAMERA_ROLE_MAIN,
                                        frame_id=f"{cfg.station_id}-{frame_idx}",
                                        frame_index=frame_idx,
                                        capture_time=cap,
                                        station_config_digest=str(cfg.config_digest),
                                        width_px=width,
                                        height_px=height,
                                        codec=common_pb2.VIDEO_CODEC_RAW,
                                        pixel_format=common_pb2.PIXEL_FORMAT_RGB8,
                                        row_stride_bytes=width * 3,
                                        payload=_render_spot(
                                            cfg, target, width=width, height=height
                                        ),
                                    ),
                                )
                            )
                        )
                        seq += 1
                        forward_until_progress(t_ns)

                conn.shutdown(socket.SHUT_WR)
                while True:
                    msg = read_length_prefixed(reader, online_pb2.BusMessage)
                    if msg is None:
                        break
                    if msg.WhichOneof("payload") in {
                        "calibration_state",
                        "ray_event",
                        "diagnostic_event",
                        "progress_report",
                    }:
                        with queue_cond:
                            forwarded_queue.append(msg)
                            queue_cond.notify_all()
            finally:
                with queue_cond:
                    edge_done = True
                    queue_cond.notify_all()
                reader.close()

        center_outbound: list[online_pb2.BusMessage] = []

        def center_vswitch(conn: socket.socket) -> None:
            reader = conn.makefile("rb")
            try:
                hello = read_length_prefixed(reader, online_pb2.BusMessage)
                assert hello is not None
                self.assertEqual(hello.session_hello.role, common_pb2.COMPONENT_ROLE_CENTER)
                conn.sendall(
                    encode_length_prefixed(
                        online_pb2.BusMessage(
                            bus_sequence_no=1,
                            session_ack=online_pb2.SessionAck(accepted=True, run_id="e2e-1"),
                        )
                    )
                )

                def read_center_outbound() -> None:
                    while True:
                        msg = read_length_prefixed(reader, online_pb2.BusMessage)
                        if msg is None:
                            break
                        center_outbound.append(msg)

                drain_thread = threading.Thread(target=read_center_outbound, daemon=True)
                drain_thread.start()

                idx = 0
                seq = 2
                while True:
                    with queue_cond:
                        while idx >= len(forwarded_queue) and not edge_done:
                            queue_cond.wait(timeout=1.0)
                        if idx < len(forwarded_queue):
                            src_msg = forwarded_queue[idx]
                            idx += 1
                        else:
                            break
                    out = online_pb2.BusMessage()
                    out.CopyFrom(src_msg)
                    out.bus_sequence_no = seq
                    seq += 1
                    conn.sendall(encode_length_prefixed(out))
                conn.shutdown(socket.SHUT_WR)
                drain_thread.join(timeout=5.0)
            finally:
                reader.close()

        edge_port, edge_srv, edge_errs = _start_tcp_server(edge_vswitch)
        center_port, center_srv, center_errs = _start_tcp_server(center_vswitch)

        def run_edge() -> None:
            with OnlineBusClient("127.0.0.1", edge_port, timeout_s=10.0) as client:
                EdgeProcess(
                    client,
                    {m.StationId("s1"): dev_policy, m.StationId("s2"): dev_policy},
                    instance_id="edge-e2e",
                    session_id="edge-sess-e2e",
                    station_ids=("s1", "s2"),
                    local_configs={m.StationId("s1"): s1_cfg, m.StationId("s2"): s2_cfg},
                    warmup_hold_ns=0,
                ).run()

        edge_thread = threading.Thread(target=run_edge, daemon=True)
        edge_thread.start()
        with OnlineBusClient("127.0.0.1", center_port, timeout_s=10.0) as center_client:
            ack = CenterProcess(
                center_client,
                _online_fusion_policy(timeout_basis=f.StreamTimeoutBasis.CAPTURE_TIME),
                instance_id="center-e2e",
                session_id="center-sess-e2e",
                station_ids=("s1", "s2"),
            ).run()

        edge_thread.join(timeout=10.0)
        edge_srv.join(timeout=5.0)
        center_srv.join(timeout=5.0)
        self.assertEqual(edge_errs, [])
        self.assertEqual(center_errs, [])
        self.assertTrue(ack.accepted)

        located = [
            msg.fusion_measurement
            for msg in center_outbound
            if msg.WhichOneof("payload") == "fusion_measurement"
            and msg.fusion_measurement.status == common_pb2.FUSION_STATUS_LOCATED
        ]
        self.assertEqual(len(located), len(targets))
        for meas, (t_ns, truth) in zip(located, targets, strict=True):
            self.assertEqual(meas.reference_time_ns, t_ns)
            pos = (meas.position_enu_m.x, meas.position_enu_m.y, meas.position_enu_m.z)
            self.assertLess(math.dist(pos, truth), 0.75)

        tracks = [
            msg.track_event for msg in center_outbound if msg.WhichOneof("payload") == "track_event"
        ]
        self.assertTrue(any(t.lifecycle == common_pb2.TRACK_LIFECYCLE_CONFIRMED for t in tracks))

        center_reports = [
            msg.progress_report
            for msg in center_outbound
            if msg.WhichOneof("payload") == "progress_report"
        ]
        self.assertTrue(any(r.finite_end_reached for r in center_reports))

    def test_load_fusion_policy_and_cli_smoke(self) -> None:
        policy = _online_fusion_policy(timeout_basis=f.StreamTimeoutBasis.PROCESSING_TIME)
        outbound: list[online_pb2.BusMessage] = []

        def handler(conn: socket.socket) -> None:
            reader = conn.makefile("rb")
            try:
                hello = read_length_prefixed(reader, online_pb2.BusMessage)
                assert hello is not None
                conn.sendall(
                    encode_length_prefixed(
                        online_pb2.BusMessage(
                            bus_sequence_no=1,
                            session_ack=online_pb2.SessionAck(accepted=True, run_id="cli-1"),
                        )
                    )
                )
                conn.shutdown(socket.SHUT_WR)
                while True:
                    msg = read_length_prefixed(reader, online_pb2.BusMessage)
                    if msg is None:
                        break
                    outbound.append(msg)
            finally:
                reader.close()

        port, thread, errors = _start_tcp_server(handler)
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "fusion_policy.json"
            policy_path.write_text(_json(policy), encoding="utf-8")
            decoded = load_fusion_policy(policy_path)
            self.assertEqual(decoded, policy)
            center_bus_main(
                [
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--policy",
                    str(policy_path),
                    "--timeout-basis",
                    "disabled",
                ]
            )
        thread.join(timeout=5.0)
        self.assertEqual(errors, [])
        self.assertEqual(len(outbound), 1)
        self.assertEqual(outbound[0].WhichOneof("payload"), "progress_report")
        self.assertTrue(outbound[0].progress_report.finite_end_reached)

    def test_center_process_rejects_unexpected_payload_and_early_progress(self) -> None:
        def send_unexpected(conn: socket.socket) -> None:
            reader = conn.makefile("rb")
            try:
                _ = read_length_prefixed(reader, online_pb2.BusMessage)
                conn.sendall(
                    encode_length_prefixed(
                        online_pb2.BusMessage(
                            bus_sequence_no=1,
                            session_ack=online_pb2.SessionAck(accepted=True, run_id="err-1"),
                        )
                    )
                )
                conn.sendall(
                    encode_length_prefixed(
                        online_pb2.BusMessage(
                            bus_sequence_no=2,
                            progress_report=online_pb2.ProgressReport(
                                source_role=common_pb2.COMPONENT_ROLE_EDGE,
                                source_id="unknown-station",
                                session_id="sess-1",
                                completed_time_ns=0,
                                finite_end_reached=False,
                            ),
                        )
                    )
                )
                conn.shutdown(socket.SHUT_WR)
            finally:
                reader.close()

        port, thread, _ = _start_tcp_server(send_unexpected)
        with (
            OnlineBusClient("127.0.0.1", port, timeout_s=5.0) as client,
            self.assertRaisesRegex(ValueError, "before StationConfig"),
        ):
            CenterProcess(client, _online_fusion_policy()).run()
        thread.join(timeout=5.0)

    def test_center_process_exits_on_vswitch_unspecified_role_end_notice(self) -> None:
        outbound: list[online_pb2.BusMessage] = []

        def handler(conn: socket.socket) -> None:
            reader = conn.makefile("rb")
            try:
                _ = read_length_prefixed(reader, online_pb2.BusMessage)
                conn.sendall(
                    encode_length_prefixed(
                        online_pb2.BusMessage(
                            bus_sequence_no=1,
                            session_ack=online_pb2.SessionAck(accepted=True, run_id="end-1"),
                        )
                    )
                )
                conn.sendall(
                    encode_length_prefixed(
                        online_pb2.BusMessage(
                            bus_sequence_no=2,
                            progress_report=online_pb2.ProgressReport(
                                source_role=common_pb2.COMPONENT_ROLE_UNSPECIFIED,
                                source_id="vswitch",
                                session_id="end-1",
                                completed_time_ns=0,
                                finite_end_reached=True,
                            ),
                        )
                    )
                )
                msg = read_length_prefixed(reader, online_pb2.BusMessage)
                if msg is not None:
                    outbound.append(msg)
                conn.shutdown(socket.SHUT_WR)
            finally:
                reader.close()

        port, thread, errors = _start_tcp_server(handler)
        with OnlineBusClient("127.0.0.1", port, timeout_s=5.0) as client:
            ack = CenterProcess(
                client,
                _online_fusion_policy(timeout_basis=f.StreamTimeoutBasis.CAPTURE_TIME),
            ).run()
        thread.join(timeout=5.0)
        self.assertEqual(errors, [])
        self.assertTrue(ack.accepted)
        self.assertEqual(len(outbound), 1)
        self.assertEqual(outbound[0].WhichOneof("payload"), "progress_report")
        self.assertTrue(outbound[0].progress_report.finite_end_reached)

    def test_center_process_aligns_zero_origin_policy_to_epoch_timestamps(self) -> None:
        s1_cfg, _ = _station_config_and_aux("s1", (0.0, 0.0, 0.0))
        s2_cfg, _ = _station_config_and_aux("s2", (40.0, 0.0, 0.0))
        sols = {
            s1_cfg.station_id: _calibration_solution(s1_cfg),
            s2_cfg.station_id: _calibration_solution(s2_cfg),
        }
        epoch_ns = 1_763_784_000_000_000_000
        step_times_ns = [epoch_ns + 50_000_000, epoch_ns + 150_000_000]
        target = (20.0, 5.0, 80.0)
        outbound: list[online_pb2.BusMessage] = []

        def handler(conn: socket.socket) -> None:
            reader = conn.makefile("rb")
            try:
                _ = read_length_prefixed(reader, online_pb2.BusMessage)
                conn.sendall(
                    encode_length_prefixed(
                        online_pb2.BusMessage(
                            bus_sequence_no=1,
                            session_ack=online_pb2.SessionAck(accepted=True, run_id="epoch-1"),
                        )
                    )
                )
                seq = 2
                for cfg in (s1_cfg, s2_cfg):
                    conn.sendall(
                        encode_length_prefixed(
                            online_pb2.BusMessage(
                                bus_sequence_no=seq,
                                station_config=station_config_to_proto(cfg),
                            )
                        )
                    )
                    seq += 1
                    sol = replace(
                        sols[cfg.station_id],
                        accepted_time=m.ProcessingTime(clock_domain_id="clk", time_ns=epoch_ns),
                        reference_capture_time=_capture_time(epoch_ns),
                        valid_interval=m.TimeInterval(
                            clock_domain_id="clk", start_ns=epoch_ns, end_ns=epoch_ns + 10**10
                        ),
                    )
                    cal_ev = m.CalibrationStateEvent(
                        envelope=m.EventEnvelope(
                            schema_version="event-v1",
                            event_type=m.EventType.CALIBRATION_STATE,
                            station_id=cfg.station_id,
                            station_session_id=sol.solution_ref.station_session_id,
                            sequence_no=1,
                            station_config_digest=cfg.config_digest,
                        ),
                        state=m.CalibrationState(
                            station_id=cfg.station_id,
                            station_session_id=sol.solution_ref.station_session_id,
                            station_config_digest=cfg.config_digest,
                            state_version=1,
                            health=m.CalibrationHealth.VALID,
                            solution_ref=sol.solution_ref,
                            observed_at=m.ProcessingTime(clock_domain_id="clk", time_ns=epoch_ns),
                            last_trusted_time=m.ProcessingTime(
                                clock_domain_id="clk", time_ns=epoch_ns
                            ),
                            usable_interval=sol.valid_interval,
                            reason_codes=(),
                        ),
                        solution=sol,
                    )
                    conn.sendall(
                        encode_length_prefixed(
                            online_pb2.BusMessage(
                                bus_sequence_no=seq,
                                calibration_state=calibration_state_event_to_proto(cal_ev),
                            )
                        )
                    )
                    seq += 1
                for idx, t_ns in enumerate(step_times_ns):
                    for cfg in (s1_cfg, s2_cfg):
                        ox, oy, oz = cfg.main_camera_position_enu_m
                        d_u, u_u, v_u = _unit_and_tangent(
                            (target[0] - ox, target[1] - oy, target[2] - oz)
                        )
                        ray = m.RayEvent(
                            envelope=m.EventEnvelope(
                                schema_version="event-v1",
                                event_type=m.EventType.RAY,
                                station_id=cfg.station_id,
                                station_session_id=m.SessionId(f"sess-{cfg.station_id}"),
                                sequence_no=idx + 2,
                                station_config_digest=cfg.config_digest,
                            ),
                            frame_id=m.FrameId(f"{cfg.station_id}-{idx}"),
                            local_track_id=m.LocalTrackId("1"),
                            capture_time=_capture_time(t_ns),
                            world_frame_id=m.WorldFrameId("enu"),
                            calibration_solution_ref=sols[cfg.station_id].solution_ref,
                            calibration_usage=m.CalibrationUsage.VALID,
                            calibration_evidence=m.CalibrationUseEvidence(
                                state_version=1,
                                last_trusted_time=m.ProcessingTime(
                                    clock_domain_id="clk", time_ns=epoch_ns
                                ),
                                solution_accepted_time=m.ProcessingTime(
                                    clock_domain_id="clk", time_ns=epoch_ns
                                ),
                                solution_reference_capture_time=_capture_time(epoch_ns),
                                solution_valid_interval=m.TimeInterval(
                                    clock_domain_id="clk",
                                    start_ns=epoch_ns,
                                    end_ns=epoch_ns + 10**10,
                                ),
                                usable_interval=m.TimeInterval(
                                    clock_domain_id="clk",
                                    start_ns=epoch_ns,
                                    end_ns=epoch_ns + 10**10,
                                ),
                                reason_codes=(),
                            ),
                            ray_origin_enu_m=cfg.main_camera_position_enu_m,
                            ray_direction_enu_unit=d_u,
                            uncertainty=m.RayUncertainty(
                                model="tangent_gaussian_v1",
                                tangent_u_enu_unit=u_u,
                                tangent_v_enu_unit=v_u,
                                independent_angular_covariance_rad2=_eye2(1e-6),
                                independent_origin_covariance_m2=_eye3(0.0),
                                shared_installation_state_ref=cfg.installation_uncertainty.state_ref,
                                shared_calibration_solution_ref=sols[cfg.station_id].solution_ref,
                            ),
                            detection_score=0.95,
                            detection_score_definition=m.ScoreDefinition(
                                definition_id="d1",
                                kind=m.ScoreKind.RANKING,
                                minimum=0.0,
                                maximum=1.0,
                                cross_station_comparison_group=None,
                            ),
                            track_quality=0.9,
                            track_quality_definition=m.ScoreDefinition(
                                definition_id="q1",
                                kind=m.ScoreKind.RANKING,
                                minimum=0.0,
                                maximum=1.0,
                                cross_station_comparison_group=None,
                            ),
                            quality_flags=(),
                        )
                        conn.sendall(
                            encode_length_prefixed(
                                online_pb2.BusMessage(
                                    bus_sequence_no=seq,
                                    ray_event=ray_event_to_proto(ray),
                                )
                            )
                        )
                        seq += 1
                        conn.sendall(
                            encode_length_prefixed(
                                online_pb2.BusMessage(
                                    bus_sequence_no=seq,
                                    progress_report=online_pb2.ProgressReport(
                                        source_role=common_pb2.COMPONENT_ROLE_EDGE,
                                        source_id=str(cfg.station_id),
                                        session_id=f"sess-{cfg.station_id}",
                                        completed_time_ns=t_ns,
                                        finite_end_reached=False,
                                    ),
                                )
                            )
                        )
                        seq += 1
                for cfg in (s1_cfg, s2_cfg):
                    conn.sendall(
                        encode_length_prefixed(
                            online_pb2.BusMessage(
                                bus_sequence_no=seq,
                                progress_report=online_pb2.ProgressReport(
                                    source_role=common_pb2.COMPONENT_ROLE_EDGE,
                                    source_id=str(cfg.station_id),
                                    session_id=f"sess-{cfg.station_id}",
                                    completed_time_ns=step_times_ns[-1],
                                    finite_end_reached=True,
                                ),
                            )
                        )
                    )
                    seq += 1
                while True:
                    msg = read_length_prefixed(reader, online_pb2.BusMessage)
                    if msg is None:
                        break
                    outbound.append(msg)
            finally:
                reader.close()

        port, thread, errors = _start_tcp_server(handler)
        with OnlineBusClient("127.0.0.1", port, timeout_s=5.0) as client:
            ack = CenterProcess(
                client,
                _online_fusion_policy(timeout_basis=f.StreamTimeoutBasis.CAPTURE_TIME),
            ).run()
        thread.join(timeout=5.0)
        self.assertEqual(errors, [])
        self.assertTrue(ack.accepted)
        located = [
            msg.fusion_measurement
            for msg in outbound
            if msg.WhichOneof("payload") == "fusion_measurement"
            and msg.fusion_measurement.status == common_pb2.FUSION_STATUS_LOCATED
        ]
        self.assertEqual(len(located), 2)
        self.assertTrue(
            any(
                msg.WhichOneof("payload") == "progress_report"
                and msg.progress_report.finite_end_reached
                for msg in outbound
            )
        )

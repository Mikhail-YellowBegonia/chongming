"""Tests for FrameSource decoding and standalone EdgeProcess over TCP."""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import threading
import unittest
from collections.abc import Callable
from dataclasses import replace

import numpy as np

from chongming.bus import (
    EdgeProcess,
    H264StreamDecoder,
    OnlineBusClient,
    decode_video_packet,
    split_annex_b_nals,
    station_config_to_proto,
)
from chongming.contracts import device as d
from chongming.contracts import model as m
from chongming.modules.device import OPENCV_CALIBRATION_ESTIMATOR_ID
from chongming_proto.online import (
    common_pb2,
    encode_length_prefixed,
    online_pb2,
    read_length_prefixed,
)
from tests.test_bus_codec import _eye3
from tests.test_detection import detection_policy, tracking_policy
from tests.test_device_rays import PIXEL_COVARIANCE
from tests.test_device_rays import policy as ray_policy


def _ffmpeg_path() -> str | None:
    configured = os.environ.get("CHONGMING_FFMPEG")
    if configured:
        return configured
    return shutil.which("ffmpeg")


def _station_and_checkerboard(width: int = 640, height: int = 480) -> tuple[m.StationConfig, bytes]:
    """Build a StationConfig with a visible 7x5 A4 checkerboard (4 outer corners) in aux view."""
    fx = 800.0
    fy = 800.0
    cx = width / 2.0
    cy = height / 2.0
    k_mat: m.Matrix3x3 = ((fx, 0.0, cx), (0.0, fy, cy), (0.0, 0.0, 1.0))
    cam = m.CameraModel(
        intrinsics=m.CameraIntrinsics(
            image_width_px=width, image_height_px=height, matrix_px=k_mat
        ),
        distortion=m.CameraDistortion(model="none", coefficients=()),
    )
    board_w = 0.297
    board_h = 0.210
    z_cam = 1.0
    board_corners = (
        (-board_w / 2.0, -board_h / 2.0, z_cam),
        (board_w / 2.0, -board_h / 2.0, z_cam),
        (board_w / 2.0, board_h / 2.0, z_cam),
        (-board_w / 2.0, board_h / 2.0, z_cam),
    )
    landmarks = tuple(
        m.SurveyedLandmark(
            landmark_id=f"board-corner-{index}",
            position_enu_m=point,
            covariance_enu_m2=_eye3(0.0),
            visual_description="checkerboard corner",
        )
        for index, point in enumerate(board_corners)
    )
    img = np.full((height, width, 3), 240, dtype=np.uint8)
    sq_w = 0.035
    sq_h = 0.035
    for sq_r in range(5):
        for sq_c in range(7):
            val = 15 if (sq_r + sq_c) % 2 == 0 else 240
            x0 = (sq_c - 3.5) * sq_w
            x1 = x0 + sq_w
            y0 = (sq_r - 2.5) * sq_h
            y1 = y0 + sq_h
            px0 = round(fx * (x0 / z_cam) + cx + 0.5)
            px1 = round(fx * (x1 / z_cam) + cx + 0.5)
            py0 = round(fy * (y0 / z_cam) + cy + 0.5)
            py1 = round(fy * (y1 / z_cam) + cy + 0.5)
            img[py0:py1, px0:px1] = val

    cfg = m.StationConfig(
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
        main_camera_position_enu_m=(0.0, 0.0, 0.0),
        baseline_orientation_enu_from_main=_eye3(),
        main_camera=cam,
        auxiliary_camera=cam,
        fixed_rotation_aux_from_main=_eye3(),
        full_rigid_transform_aux_from_main=m.RigidTransform(
            rotation_a_from_b=_eye3(),
            translation_a_m=(0.0, 0.0, 0.0),
        ),
        reference_anchor=m.SurveyedReference(
            reference_id="s1-a4",
            world_frame_id=m.WorldFrameId("enu"),
            landmarks=landmarks,
        ),
        reference_scene_version="ref-v1",
        installation_uncertainty=m.InstallationUncertainty(
            state_ref=m.InstallationStateRef(
                station_id=m.StationId("s1"),
                config_digest=m.ConfigDigest("cfg-s1"),
                state_version="inst-v1",
            ),
            position_covariance_enu_m2=_eye3(0.0),
            orientation_covariance_enu_rad2=_eye3(0.0),
            fixed_extrinsic_covariance_aux_rad2=_eye3(0.0),
            measurement_source="synthetic",
        ),
    )
    return cfg, img.tobytes()


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


def _run_server(
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


class FrameDecoderTests(unittest.TestCase):
    def test_raw_packet_decoding_and_validation(self) -> None:
        pixels = bytes([10, 20, 30] * (8 * 6))
        packet = online_pb2.FramedVideoPacket(
            station_id="s1",
            station_session_id="sess-1",
            camera_role=common_pb2.CAMERA_ROLE_MAIN,
            frame_id="f-0",
            frame_index=0,
            capture_time=common_pb2.CaptureTime(
                clock_domain_id="clk",
                exposure_start_ns=100,
                exposure_end_ns=100,
                rolling_shutter_model="none",
            ),
            station_config_digest="cfg-1",
            width_px=8,
            height_px=6,
            codec=common_pb2.VIDEO_CODEC_RAW,
            pixel_format=common_pb2.PIXEL_FORMAT_RGB8,
            row_stride_bytes=24,
            payload=pixels,
        )
        decoded = decode_video_packet(packet, {})
        self.assertEqual(decoded.frame.image.pixels, pixels)
        self.assertEqual(decoded.decoded_pixel_sha256, hashlib.sha256(pixels).hexdigest())
        self.assertGreaterEqual(decoded.decode_duration_ns, 0)

        bad_stride = online_pb2.FramedVideoPacket()
        bad_stride.CopyFrom(packet)
        bad_stride.row_stride_bytes = 8
        with self.assertRaisesRegex(ValueError, "row_stride_bytes"):
            decode_video_packet(bad_stride, {})

    def test_h264_annex_b_decoding_matches_decoded_pixels_sha256(self) -> None:
        ffmpeg = _ffmpeg_path()
        if ffmpeg is None:
            self.skipTest("ffmpeg is required for H.264 decode test")
        width, height = 64, 48
        raw_frames = [
            bytes([12, 34, 56] * (width * height)),
            bytes([200, 120, 40] * (width * height)),
        ]
        encoded = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-s:v",
                f"{width}x{height}",
                "-r",
                "30",
                "-i",
                "pipe:0",
                "-c:v",
                "libx264",
                "-tune",
                "zerolatency",
                "-pix_fmt",
                "yuv420p",
                "-f",
                "h264",
                "pipe:1",
            ],
            input=b"".join(raw_frames),
            capture_output=True,
            check=True,
        ).stdout

        nals = split_annex_b_nals(encoded)
        self.assertGreaterEqual(len(nals), 3)
        # Group NALs into per-frame Annex-B access units (parameter sets stay with IDR).
        aus: list[bytes] = []
        current: list[bytes] = []
        has_slice = False
        for nal in nals:
            nal_type = nal[0] & 0x1F
            if has_slice and nal_type in (1, 5, 7, 8, 9):
                aus.append(b"".join(current))
                current = []
                has_slice = False
            current.append(b"\x00\x00\x00\x01" + nal)
            if nal_type in (1, 5):
                has_slice = True
        if current:
            aus.append(b"".join(current))
        self.assertEqual(len(aus), 2)

        decoders: dict[tuple[str, m.CameraRole], H264StreamDecoder] = {}
        for idx, au in enumerate(aus):
            pkt = online_pb2.FramedVideoPacket(
                station_id="s1",
                station_session_id="sess-1",
                camera_role=common_pb2.CAMERA_ROLE_MAIN,
                frame_id=f"f-{idx}",
                frame_index=idx,
                capture_time=common_pb2.CaptureTime(
                    clock_domain_id="clk",
                    exposure_start_ns=idx * 33_333_333,
                    exposure_end_ns=idx * 33_333_333,
                    rolling_shutter_model="none",
                ),
                station_config_digest="cfg-1",
                width_px=width,
                height_px=height,
                codec=common_pb2.VIDEO_CODEC_H264,
                pixel_format=common_pb2.PIXEL_FORMAT_YUV420P,
                is_keyframe=(idx == 0),
                payload=au,
            )
            decoded = decode_video_packet(pkt, decoders, ffmpeg_bin=ffmpeg)
            self.assertEqual(decoded.frame.image.pixel_format, m.PixelFormat.RGB8)
            self.assertEqual(len(decoded.frame.image.pixels), width * height * 3)
            self.assertEqual(
                decoded.decoded_pixel_sha256,
                hashlib.sha256(decoded.frame.image.pixels).hexdigest(),
            )
            self.assertGreater(decoded.decode_duration_ns, 0)


class EdgeProcessTests(unittest.TestCase):
    def test_edge_process_lockstep_handshake_calibration_rays_and_timings(self) -> None:
        width, height = 640, 480
        config, aux_pixels = _station_and_checkerboard(width, height)
        bg = np.zeros((height, width, 3), dtype=np.uint8).tobytes()
        fg_arr = np.zeros((height, width, 3), dtype=np.uint8)
        fg_arr[110:130, 150:170] = 255
        fg = fg_arr.tobytes()

        expected_hashes = [
            hashlib.sha256(bg).hexdigest(),
            hashlib.sha256(fg).hexdigest(),
        ]
        received: list[online_pb2.BusMessage] = []

        def handle(conn: socket.socket) -> None:
            reader = conn.makefile("rb")
            try:
                hello = read_length_prefixed(reader, online_pb2.BusMessage)
                assert hello is not None
                self.assertEqual(hello.WhichOneof("payload"), "session_hello")
                self.assertEqual(hello.session_hello.role, common_pb2.COMPONENT_ROLE_EDGE)
                conn.sendall(
                    encode_length_prefixed(
                        online_pb2.BusMessage(
                            bus_sequence_no=1,
                            session_ack=online_pb2.SessionAck(
                                accepted=True,
                                reason="ok",
                                run_id="run-edge-1",
                            ),
                        )
                    )
                )
                conn.sendall(
                    encode_length_prefixed(
                        online_pb2.BusMessage(
                            bus_sequence_no=2,
                            station_config=station_config_to_proto(config),
                        )
                    )
                )
                # Group 0: aux checkerboard + empty main background warmup frame.
                cap0 = common_pb2.CaptureTime(
                    clock_domain_id="clk",
                    exposure_start_ns=0,
                    exposure_end_ns=0,
                    rolling_shutter_model="none",
                )
                conn.sendall(
                    encode_length_prefixed(
                        online_pb2.BusMessage(
                            bus_sequence_no=3,
                            video_packet=online_pb2.FramedVideoPacket(
                                station_id="s1",
                                station_session_id="img-s1",
                                camera_role=common_pb2.CAMERA_ROLE_AUXILIARY,
                                frame_id="s1-aux-0",
                                frame_index=0,
                                capture_time=cap0,
                                station_config_digest="cfg-s1",
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
                # Read CalibrationStateEvent emitted by aux before releasing main.
                cal_msg = read_length_prefixed(reader, online_pb2.BusMessage)
                assert cal_msg is not None
                received.append(cal_msg)

                conn.sendall(
                    encode_length_prefixed(
                        online_pb2.BusMessage(
                            bus_sequence_no=4,
                            video_packet=online_pb2.FramedVideoPacket(
                                station_id="s1",
                                station_session_id="img-s1",
                                camera_role=common_pb2.CAMERA_ROLE_MAIN,
                                frame_id="s1-main-0",
                                frame_index=0,
                                capture_time=cap0,
                                station_config_digest="cfg-s1",
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
                # Lock-step: wait for ProgressReport(t=0) before releasing frame 1!
                while True:
                    msg = read_length_prefixed(reader, online_pb2.BusMessage)
                    assert msg is not None
                    received.append(msg)
                    if msg.WhichOneof("payload") == "progress_report":
                        self.assertEqual(msg.progress_report.completed_time_ns, 0)
                        self.assertFalse(msg.progress_report.finite_end_reached)
                        break

                cap1 = common_pb2.CaptureTime(
                    clock_domain_id="clk",
                    exposure_start_ns=100_000_000,
                    exposure_end_ns=100_000_000,
                    rolling_shutter_model="none",
                )
                conn.sendall(
                    encode_length_prefixed(
                        online_pb2.BusMessage(
                            bus_sequence_no=5,
                            video_packet=online_pb2.FramedVideoPacket(
                                station_id="s1",
                                station_session_id="img-s1",
                                camera_role=common_pb2.CAMERA_ROLE_MAIN,
                                frame_id="s1-main-1",
                                frame_index=1,
                                capture_time=cap1,
                                station_config_digest="cfg-s1",
                                width_px=width,
                                height_px=height,
                                codec=common_pb2.VIDEO_CODEC_RAW,
                                pixel_format=common_pb2.PIXEL_FORMAT_RGB8,
                                row_stride_bytes=width * 3,
                                payload=fg,
                            ),
                        )
                    )
                )
                while True:
                    msg = read_length_prefixed(reader, online_pb2.BusMessage)
                    assert msg is not None
                    received.append(msg)
                    if msg.WhichOneof("payload") == "progress_report":
                        self.assertEqual(msg.progress_report.completed_time_ns, 100_000_000)
                        self.assertFalse(msg.progress_report.finite_end_reached)
                        break

                # Signal finite stream end and drain Edge's final ProgressReport.
                conn.sendall(
                    encode_length_prefixed(
                        online_pb2.BusMessage(
                            bus_sequence_no=6,
                            progress_report=online_pb2.ProgressReport(
                                source_role=common_pb2.COMPONENT_ROLE_PRODUCER,
                                source_id="vswitch",
                                session_id="run-edge-1",
                                completed_time_ns=100_000_000,
                                finite_end_reached=True,
                            ),
                        )
                    )
                )
                while True:
                    msg = read_length_prefixed(reader, online_pb2.BusMessage)
                    if msg is None:
                        break
                    received.append(msg)
            finally:
                reader.close()

        port, thread, errors = _run_server(handle)
        with OnlineBusClient("127.0.0.1", port) as client:
            process = EdgeProcess(
                client,
                {m.StationId("s1"): _device_policy()},
                local_configs={m.StationId("s1"): config},
                warmup_hold_ns=0,
            )
            ack = process.run()
        thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(ack.run_id, "run-edge-1")

        kinds = [msg.WhichOneof("payload") for msg in received]
        self.assertIn("calibration_state", kinds)
        self.assertIn("ray_event", kinds)
        batches = [
            msg.detection_batch
            for msg in received
            if msg.WhichOneof("payload") == "detection_batch"
        ]
        self.assertEqual(len(batches), 2)
        self.assertEqual([b.decoded_pixel_sha256 for b in batches], expected_hashes)
        self.assertTrue(all(b.cv_duration_ns > 0 for b in batches))
        self.assertEqual(len(batches[0].observations), 0)
        self.assertEqual(len(batches[1].observations), 1)
        progress_reports = [
            msg.progress_report
            for msg in received
            if msg.WhichOneof("payload") == "progress_report"
        ]
        self.assertEqual(len(progress_reports), 3)
        self.assertTrue(progress_reports[-1].finite_end_reached)
        self.assertEqual(progress_reports[-1].completed_time_ns, 100_000_000)

    def test_h264_stream_decoder_select_frame_across_multi_gop_p_frames(self) -> None:
        ffmpeg = _ffmpeg_path()
        if ffmpeg is None:
            self.skipTest("ffmpeg is not available")
        w, h = 64, 48
        raw = bytearray()
        expected_x: list[float] = []
        # 6 frames encoded with -g 3 (2 GOPs of 3 frames each: I,P,P, I,P,P).
        for idx in range(6):
            arr = np.zeros((h, w, 3), dtype=np.uint8)
            x0 = 6 + idx * 8
            arr[18:28, x0 : x0 + 6] = 255
            raw.extend(arr.tobytes())
            expected_x.append(x0 + 2.5)
        proc = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-s",
                f"{w}x{h}",
                "-r",
                "25",
                "-i",
                "pipe:0",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-g",
                "3",
                "-bf",
                "0",
                "-crf",
                "18",
                "-f",
                "h264",
                "pipe:1",
            ],
            input=bytes(raw),
            capture_output=True,
            check=True,
        )
        nals = split_annex_b_nals(proc.stdout)
        # Re-group Annex B NALs into per-frame access units starting at AUD(9) or slice/IDR(1/5),
        # keeping parameter sets (SPS=7, PPS=8) attached to the IDR access unit.
        units: list[list[bytes]] = []
        pending_headers: list[bytes] = []
        for nal in nals:
            nal_type = nal[0] & 0x1F
            if nal_type in {6, 7, 8, 9}:
                pending_headers.append(nal)
                continue
            units.append([*pending_headers, nal])
            pending_headers.clear()
        self.assertEqual(len(units), 6)

        decoder = H264StreamDecoder(width_px=w, height_px=h, ffmpeg_bin=ffmpeg)
        decoded_centroids: list[float] = []
        for idx, au_nals in enumerate(units):
            is_idr = any((nal[0] & 0x1F) == 5 for nal in au_nals)
            au_payload = b"".join(b"\x00\x00\x00\x01" + nal for nal in au_nals)
            raw_rgb = decoder.decode(au_payload, is_keyframe=is_idr)
            rgb = np.frombuffer(raw_rgb, dtype=np.uint8).reshape((h, w, 3))
            xs = np.nonzero(rgb[:, :, 0] > 180)[1]
            self.assertGreater(len(xs), 0, f"missing bright box at frame {idx}")
            decoded_centroids.append(float(np.mean(xs)))
        for actual, expected in zip(decoded_centroids, expected_x, strict=True):
            self.assertAlmostEqual(actual, expected, delta=1.5)

"""End-to-end tests for Rust Edge (`crates/edge`) against V-Switch, Center, and Scorer."""

from __future__ import annotations

import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

import numpy as np

from chongming.bus.codec import station_config_to_proto
from chongming.composition.center import reference_policy
from chongming.contracts import device as d
from chongming.contracts import model as m
from chongming.contracts.json_codec import dump_json
from chongming.modules.device import OPENCV_CALIBRATION_ESTIMATOR_ID
from chongming_proto.online import common_pb2
from chongming_proto.truth import truth_pb2
from chongming_scorer.scorer import score_run
from chongming_scorer.storage import RunOutputPackage, TestScenePackage, TestSceneWriter
from tests import test_godot_producer_e2e as prod_e2e

ROOT = Path(__file__).resolve().parents[1]
VSWITCH_BIN = ROOT / "target/debug/vswitch"
EDGE_BIN = ROOT / "target/debug/edge"
GODOT = os.environ.get("CHONGMING_GODOT") or shutil.which("godot4") or shutil.which("godot")


def _run_replay_with_binary(
    scene_dir: Path,
    run_dir: Path,
    score_dir: Path,
    *,
    edge_cmd: list[str],
    fus_policy_path: Path,
    work_dir: Path,
) -> dict[str, Any]:
    vswitch_proc = subprocess.Popen(
        [
            str(VSWITCH_BIN),
            "replay",
            "--scene",
            str(scene_dir),
            "--run-output",
            str(run_dir),
            "--run-id",
            f"run-{run_dir.name}",
            "--mode",
            "integrated",
            "--stepping",
            "lock-step",
            "--bus-bind",
            "127.0.0.1:0",
            "--truth-bind",
            "127.0.0.1:0",
            "--wait-scorer",
            "--handshake-timeout-s",
            "60",
            "--step-timeout-s",
            "30",
        ],
        cwd=work_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    edge_proc: subprocess.Popen[str] | None = None
    center_proc: subprocess.Popen[str] | None = None
    scorer_proc: subprocess.Popen[str] | None = None
    try:
        addrs = prod_e2e._wait_vswitch_listening(vswitch_proc)
        bus_host, bus_port = addrs["bus_addr"].rsplit(":", 1)
        truth_host, truth_port = addrs["truth_addr"].rsplit(":", 1)

        edge_proc = subprocess.Popen(
            [*edge_cmd, "--host", bus_host, "--port", bus_port],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        center_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "chongming.bus.center_process",
                "--host",
                bus_host,
                "--port",
                bus_port,
                "--policy",
                str(fus_policy_path),
                "--timeout-basis",
                "capture_time",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        scorer_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "chongming_scorer.cli",
                "subscribe",
                "--host",
                truth_host,
                "--port",
                truth_port,
                "--scene",
                str(scene_dir),
                "--output",
                str(score_dir),
                "--mode",
                "integrated",
                "--warmup-s",
                "0",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        for label, proc in (
            ("edge", edge_proc),
            ("center", center_proc),
            ("scorer", scorer_proc),
            ("vswitch replay", vswitch_proc),
        ):
            out, err = proc.communicate(timeout=90.0)
            if proc.returncode != 0:
                raise AssertionError(
                    f"{label} failed (rc={proc.returncode}):\nSTDOUT:\n{out}\nSTDERR:\n{err}"
                )
        return score_run(scene_dir, run_dir, mode="integrated", warmup_s=0.0)
    finally:
        for p in (edge_proc, center_proc, scorer_proc, vswitch_proc):
            if p is not None and p.poll() is None:
                p.kill()
                p.wait(timeout=5)


class RustEdgeArchitectureTests(unittest.TestCase):
    def test_edge_crate_never_references_truth_proto_or_scorer(self) -> None:
        edge_root = ROOT / "crates/edge"
        for path in edge_root.rglob("*.rs"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("truth.proto", text, str(path))
            self.assertNotIn("TruthFrame", text, str(path))
            self.assertNotIn("chongming_scorer", text, str(path))


@unittest.skipUnless(
    GODOT and VSWITCH_BIN.is_file() and EDGE_BIN.is_file() and prod_e2e._has_display_or_xvfb(),
    "Godot 4, compiled target/debug/vswitch & target/debug/edge, and display are required",
)
class RustEdgeGodotE2ETests(unittest.TestCase):
    def test_rust_edge_matches_python_edge_on_godot_scenes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_tmp = Path(tmp)
            dev_policy, fus_policy = prod_e2e._write_policies(root_tmp)

            for name, scene_tscn, seed in (
                ("clean", "res://scenes/clean_range.tscn", 1001),
                ("scenic", "res://scenes/scenic_ridge.tscn", 2002),
            ):
                with self.subTest(scene=name):
                    scene_dir = root_tmp / f"scene_{name}"
                    prod_e2e._record_scene_from_godot(
                        scene_tscn, scene_dir, root_tmp, f"rust-e2e-{name}", random_seed=seed
                    )
                    pkg = TestScenePackage.open(scene_dir, verify_hashes=True)
                    self.assertGreater(len(list(pkg.iter_truth_frames())), 0)

                    py_run = root_tmp / f"run_py_{name}"
                    py_score = root_tmp / f"score_py_{name}"
                    rs_run = root_tmp / f"run_rs_{name}"
                    rs_score = root_tmp / f"score_rs_{name}"

                    py_report = _run_replay_with_binary(
                        scene_dir,
                        py_run,
                        py_score,
                        edge_cmd=[
                            sys.executable,
                            "-m",
                            "chongming.bus.edge_process",
                            "--policy",
                            str(dev_policy),
                            "--warmup-hold-s",
                            "0",
                        ],
                        fus_policy_path=fus_policy,
                        work_dir=root_tmp,
                    )
                    rs_report = _run_replay_with_binary(
                        scene_dir,
                        rs_run,
                        rs_score,
                        edge_cmd=[
                            str(EDGE_BIN),
                            "--policy",
                            str(dev_policy),
                            "--warmup-hold-s",
                            "0",
                        ],
                        fus_policy_path=fus_policy,
                        work_dir=root_tmp,
                    )

                    py_pkg = RunOutputPackage.open(py_run, verify_hashes=True)
                    rs_pkg = RunOutputPackage.open(rs_run, verify_hashes=True)

                    py_batches = [
                        (
                            m.detection_batch.main_frame_ref.frame_id,
                            m.detection_batch.decoded_pixel_sha256,
                        )
                        for m in py_pkg.iter_edge_events()
                        if m.WhichOneof("payload") == "detection_batch"
                    ]
                    rs_batches = [
                        (
                            m.detection_batch.main_frame_ref.frame_id,
                            m.detection_batch.decoded_pixel_sha256,
                        )
                        for m in rs_pkg.iter_edge_events()
                        if m.WhichOneof("payload") == "detection_batch"
                    ]
                    self.assertEqual(rs_batches, py_batches)

                    rs_cal_valid = {
                        m.calibration_state.state.station_id
                        for m in rs_pkg.iter_edge_events()
                        if m.WhichOneof("payload") == "calibration_state"
                        and m.calibration_state.state.health == common_pb2.CALIBRATION_HEALTH_VALID
                    }
                    self.assertEqual(rs_cal_valid, {"s1", "s2"})

                    self.assertGreater(rs_report["edge"]["aggregate"]["tp"], 0, rs_report)
                    self.assertGreater(rs_report["center"]["metrics"]["tp"], 0, rs_report)
                    self.assertGreaterEqual(
                        rs_report["edge"]["aggregate"]["tp"],
                        py_report["edge"]["aggregate"]["tp"],
                        (rs_report["edge"], py_report["edge"]),
                    )
                    self.assertGreater(
                        sum(
                            m.detection_batch.decode_duration_ns
                            for m in rs_pkg.iter_edge_events()
                            if m.WhichOneof("payload") == "detection_batch"
                        ),
                        0,
                    )
                    self.assertTrue((rs_score / "scores.pb.zst").is_file())
                    summary = json.loads((rs_score / "report.json").read_text(encoding="utf-8"))
                    self.assertEqual(summary["format"], "chongming-scorer-integrated-v1")


def _encode_h264_mp4(ffmpeg: str, path: Path, frames: list[bytes], width: int, height: int) -> None:
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{width}x{height}",
            "-r",
            "25",
            "-i",
            "pipe:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-bf",
            "0",
            "-crf",
            "18",
            str(path),
        ],
        input=b"".join(frames),
        check=True,
    )


def _field_device_policy(sids: tuple[str, ...]) -> dict[str, d.DevicePolicy]:
    eye2 = ((1.0, 0.0), (0.0, 1.0))
    zero2 = ((0.0, 0.0), (0.0, 0.0))
    return {
        sid: d.DevicePolicy(
            detection=d.DetectionPolicy(
                algorithm_id="mog2-components-v1",
                score_definition=m.ScoreDefinition(
                    definition_id="foreground-component-fill-ratio-v1",
                    kind=m.ScoreKind.RANKING,
                    minimum=0.0,
                    maximum=1.0,
                    cross_station_comparison_group=None,
                ),
                minimum_component_area_px2=1.0,
                maximum_component_area_px2=None,
                background_warmup_frame_count=1,
                background_history_frame_count=10,
                background_variance_threshold=16.0,
                background_learning_rate=0.0,
                background_detect_shadows=False,
            ),
            tracking=d.Tracking2DPolicy(
                algorithm_id="constant-velocity-nearest-neighbor-v1",
                association_gate_px=12.0,
                confirmation_frame_count=2,
                maximum_missing_duration_ns=500_000_000,
                quality_definition=m.ScoreDefinition(
                    definition_id="consecutive-detection-fraction-v1",
                    kind=m.ScoreKind.RANKING,
                    minimum=0.0,
                    maximum=1.0,
                    cross_station_comparison_group=None,
                ),
            ),
            calibration=d.CalibrationPolicy(
                update_period_ns=0,
                stale_after_ns=2_000_000_000,
                recovery_success_count=1,
                minimum_reference_count=4,
                maximum_reprojection_rmse_px=2.0,
                maximum_rotation_only_parallax_px=None,
                estimator_id=OPENCV_CALIBRATION_ESTIMATOR_ID,
            ),
            rays=d.RayGenerationPolicy(
                uncertainty_model_id="tangent_gaussian_v1",
                fallback_centroid_covariance_px2=eye2,
                unmodelled_high_frequency_jitter_covariance_rad2=zero2,
                unmodelled_relative_flex_covariance_rad2=zero2,
            ),
        )
        for sid in sids
    }


def _field_station_with_aux(
    sid: str, pos: tuple[float, float, float], width: int, height: int
) -> tuple[m.StationConfig, bytes]:
    eye3 = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    zero3 = ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    fx, fy, cx, cy = 800.0, 800.0, width / 2.0, height / 2.0
    cam = m.CameraModel(
        intrinsics=m.CameraIntrinsics(
            image_width_px=width,
            image_height_px=height,
            matrix_px=((fx, 0.0, cx), (0.0, fy, cy), (0.0, 0.0, 1.0)),
        ),
        distortion=m.CameraDistortion(model="none", coefficients=()),
    )
    board_w, board_h, z_cam = 0.297, 0.210, 1.0
    px_b, py_b, pz_b = pos
    corners = (
        (px_b - board_w / 2.0, py_b - board_h / 2.0, pz_b + z_cam),
        (px_b + board_w / 2.0, py_b - board_h / 2.0, pz_b + z_cam),
        (px_b + board_w / 2.0, py_b + board_h / 2.0, pz_b + z_cam),
        (px_b - board_w / 2.0, py_b + board_h / 2.0, pz_b + z_cam),
    )
    landmarks = tuple(
        m.SurveyedLandmark(
            landmark_id=f"{sid}-corner-{i}",
            position_enu_m=pt,
            covariance_enu_m2=zero3,
            visual_description="checkerboard corner",
        )
        for i, pt in enumerate(corners)
    )
    img = np.full((height, width, 3), 240, dtype=np.uint8)
    sq = 0.035
    for r in range(5):
        for c in range(7):
            val = 15 if (r + c) % 2 == 0 else 240
            x0, x1 = (c - 3.5) * sq, (c - 2.5) * sq
            y0, y1 = (r - 2.5) * sq, (r - 1.5) * sq
            img[
                round(fy * (y0 / z_cam) + cy + 0.5) : round(fy * (y1 / z_cam) + cy + 0.5),
                round(fx * (x0 / z_cam) + cx + 0.5) : round(fx * (x1 / z_cam) + cx + 0.5),
            ] = val
    dig = m.ConfigDigest(f"dig-{sid}")
    cfg = m.StationConfig(
        station_id=m.StationId(sid),
        config_version="v1",
        config_digest=dig,
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
        main_camera_position_enu_m=pos,
        baseline_orientation_enu_from_main=eye3,
        main_camera=cam,
        auxiliary_camera=cam,
        fixed_rotation_aux_from_main=eye3,
        full_rigid_transform_aux_from_main=m.RigidTransform(
            rotation_a_from_b=eye3, translation_a_m=(0.0, 0.0, 0.0)
        ),
        reference_anchor=m.SurveyedReference(
            reference_id=f"{sid}-a4",
            world_frame_id=m.WorldFrameId("enu"),
            landmarks=landmarks,
        ),
        reference_scene_version="ref-v1",
        installation_uncertainty=m.InstallationUncertainty(
            state_ref=m.InstallationStateRef(
                station_id=m.StationId(sid), config_digest=dig, state_version="inst-1"
            ),
            position_covariance_enu_m2=zero3,
            orientation_covariance_enu_rad2=zero3,
            fixed_extrinsic_covariance_aux_rad2=zero3,
            measurement_source="survey",
        ),
    )
    return cfg, img.tobytes()


@unittest.skipUnless(
    VSWITCH_BIN.is_file()
    and EDGE_BIN.is_file()
    and bool(os.environ.get("CHONGMING_FFMPEG") or shutil.which("ffmpeg")),
    "compiled target/debug/vswitch & target/debug/edge and ffmpeg are required",
)
class RustEdgeFieldReplayTests(unittest.TestCase):
    def test_one_cam_unlabelled_and_zero_detection_edge_replay(self) -> None:
        ffmpeg = os.environ.get("CHONGMING_FFMPEG") or shutil.which("ffmpeg")
        assert ffmpeg is not None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            w, h = 64, 48
            s1, _ = _field_station_with_aux("s1", (0.0, 0.0, 0.0), w, h)
            s1_only = station_config_to_proto(s1)
            s1_only.ClearField("auxiliary_camera")
            s1_only.ClearField("fixed_rotation_aux_from_main")
            s1_only.ClearField("full_rigid_transform_aux_from_main")

            blank = np.zeros((h, w, 3), dtype=np.uint8).tobytes()
            spot = np.zeros((h, w, 3), dtype=np.uint8)
            spot[18:26, 20:26] = 255
            mp4_det = root / "s1-det.mp4"
            mp4_zero = root / "s1-zero.mp4"
            frame_count = 18
            subprocess.run(
                [
                    ffmpeg,
                    "-v",
                    "error",
                    "-f",
                    "rawvideo",
                    "-pixel_format",
                    "rgb24",
                    "-video_size",
                    f"{w}x{h}",
                    "-framerate",
                    "25",
                    "-i",
                    "pipe:0",
                    "-c:v",
                    "mpeg4",
                    "-bf",
                    "2",
                    str(mp4_det),
                ],
                input=blank * 15 + spot.tobytes() * 3,
                capture_output=True,
                check=True,
                timeout=15,
            )
            _encode_h264_mp4(ffmpeg, mp4_zero, [blank, blank, blank], w, h)

            pol_path = root / "pol1.json"
            pol_path.write_text(dump_json(_field_device_policy(("s1",))), encoding="utf-8")

            times = (1_000_000_000, 1_040_000_000, 1_080_000_000)

            def write_scene(name: str, mp4_path: Path) -> Path:
                scene_dir = root / name
                with TestSceneWriter(
                    scene_dir,
                    scenario_id=name,
                    world_frame_id="enu",
                    clock_domain_id="clk",
                    start_time_ns=times[0],
                    end_time_ns=times[-1],
                    stations=[s1_only],
                ) as sw:
                    sw.add_video_file(
                        mp4_path,
                        "video/s1-main.mp4",
                        {"fps": 25.0, "frame_count": 3, "width_px": w, "height_px": h},
                    )
                    for idx, t in enumerate(times):
                        sw.add_frame_index(
                            truth_pb2.FrameIndexEntry(
                                station_id="s1",
                                station_session_id="sess-s1",
                                camera_role=common_pb2.CAMERA_ROLE_MAIN,
                                frame_index=idx,
                                frame_id=f"s1-f-{idx}",
                                capture_time=common_pb2.CaptureTime(
                                    clock_domain_id="clk",
                                    exposure_start_ns=t,
                                    exposure_end_ns=t,
                                    rolling_shutter_model="none",
                                ),
                                station_config_digest="dig-s1",
                                video_relpath="video/s1-main.mp4",
                                width_px=w,
                                height_px=h,
                            )
                        )
                return scene_dir

            # 1. Single-main-camera unlabelled scene through Rust Edge (no viewer, no scorer).
            sc_1cam = root / "sc_1cam"
            stamps = root / "timestamps.csv"
            offsets = [i * 40_000_000 + (i % 2) * 3_000_000 for i in range(frame_count)]
            lines = ["station_id,output_frame_index,monotonic_ns,actual_width_px,actual_height_px"]
            lines.extend(
                f"station-a,{i},{1_000_000_000 + offset},{w},{h}"
                for i, offset in enumerate(offsets)
            )
            intrinsics = root / "intrinsics.json"
            intrinsics.write_text(
                json.dumps(
                    {
                        "image_width_px": w,
                        "image_height_px": h,
                        "fx": 80,
                        "fy": 80,
                        "cx": w / 2,
                        "cy": h / 2,
                        "distortion_model": "none",
                    }
                ),
                encoding="utf-8",
            )
            import_cmd = [
                sys.executable,
                str(ROOT / "scripts/import_mac_recording.py"),
                "--video",
                str(mp4_det),
                "--timestamps",
                str(stamps),
                "--intrinsics",
                str(intrinsics),
                "--output",
                str(sc_1cam),
            ]
            stamps.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
            invalid = subprocess.run(
                import_cmd, check=False, capture_output=True, text=True, timeout=20
            )
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("video has 18 frames but timestamp CSV has 17 rows", invalid.stderr)
            self.assertFalse(sc_1cam.exists())
            stamps.write_text("\n".join(lines) + "\n", encoding="utf-8")
            imported = subprocess.run(
                import_cmd, check=False, capture_output=True, text=True, timeout=20
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            scene = TestScenePackage.open(sc_1cam, verify_videos=True)
            (station,) = scene.station_configs()
            self.assertFalse(station.HasField("auxiliary_camera"))
            self.assertFalse(station.HasField("main_camera_position_enu_m"))
            self.assertEqual(list(scene.iter_truth_frames()), [])
            self.assertEqual(list(scene.iter_virtual_rays()), [])
            self.assertEqual(
                [frame.capture_time.exposure_start_ns for frame in scene.iter_frame_index()],
                offsets,
            )
            run_1cam = root / "run_1cam"
            vs = subprocess.Popen(
                [
                    str(VSWITCH_BIN),
                    "replay",
                    "--scene",
                    str(sc_1cam),
                    "--run-output",
                    str(run_1cam),
                    "--run-id",
                    "run-1cam",
                    "--mode",
                    "edge",
                    "--bus-bind",
                    "127.0.0.1:0",
                    "--step-timeout-s",
                    "5",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                addrs = prod_e2e._wait_vswitch_listening(vs)
                host, port = addrs["bus_addr"].rsplit(":", 1)
                ep = subprocess.run(
                    [
                        str(EDGE_BIN),
                        "--host",
                        host,
                        "--port",
                        port,
                        "--policy",
                        str(ROOT / "scenarios/policies/field/mac_edge.json"),
                        "--warmup-hold-s",
                        "0",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                vs_out, vs_err = vs.communicate(timeout=20)
                self.assertEqual(ep.returncode, 0, ep.stderr)
                self.assertEqual(vs.returncode, 0, vs_err)
                manifest = json.loads(vs_out)
                self.assertEqual(manifest["mode"], "edge")
            finally:
                if vs.poll() is None:
                    vs.kill()

            pkg_1cam = RunOutputPackage.open(run_1cam, verify_hashes=True)
            msgs_1cam = list(pkg_1cam.iter_edge_events())
            batches_1cam = [
                m_msg.detection_batch
                for m_msg in msgs_1cam
                if m_msg.WhichOneof("payload") == "detection_batch"
            ]
            self.assertEqual(len(batches_1cam), frame_count)
            self.assertEqual([b.capture_time.exposure_start_ns for b in batches_1cam], offsets)
            self.assertTrue(
                any(
                    m_msg.WhichOneof("payload") == "progress_report"
                    and m_msg.progress_report.finite_end_reached
                    for m_msg in msgs_1cam
                )
            )
            rep_1cam = score_run(sc_1cam, run_1cam, output_dir=root / "score_1cam", warmup_s=0.0)
            self.assertEqual(rep_1cam["status"], "unavailable")
            self.assertIn("truth_unavailable", rep_1cam["reason_codes"])
            self.assertEqual(
                rep_1cam["available_outputs"]["edge"]["detection_batches"], frame_count
            )
            self.assertGreater(rep_1cam["available_outputs"]["edge"]["observations"], 0)

            # 2. Valid zero-detection clip through Rust Edge.
            sc_zero = write_scene("sc_zero", mp4_zero)
            run_zero = root / "run_zero"
            vs_z = subprocess.Popen(
                [
                    str(VSWITCH_BIN),
                    "replay",
                    "--scene",
                    str(sc_zero),
                    "--run-output",
                    str(run_zero),
                    "--run-id",
                    "run-zero",
                    "--mode",
                    "edge",
                    "--bus-bind",
                    "127.0.0.1:0",
                    "--step-timeout-s",
                    "5",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                addrs_z = prod_e2e._wait_vswitch_listening(vs_z)
                host_z, port_z = addrs_z["bus_addr"].rsplit(":", 1)
                ep_z = subprocess.run(
                    [
                        str(EDGE_BIN),
                        "--host",
                        host_z,
                        "--port",
                        port_z,
                        "--policy",
                        str(pol_path),
                        "--warmup-hold-s",
                        "0",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                _, vs_z_err = vs_z.communicate(timeout=20)
                self.assertEqual(ep_z.returncode, 0, ep_z.stderr)
                self.assertEqual(vs_z.returncode, 0, vs_z_err)
            finally:
                if vs_z.poll() is None:
                    vs_z.kill()
            pkg_zero = RunOutputPackage.open(run_zero, verify_hashes=True)
            zero_batches = [
                m_msg.detection_batch
                for m_msg in pkg_zero.iter_edge_events()
                if m_msg.WhichOneof("payload") == "detection_batch"
            ]
            self.assertEqual(len(zero_batches), 3)
            self.assertEqual(sum(len(b.observations) for b in zero_batches), 0)

    def test_integrated_no_aux_and_incomplete_coverage_with_viewer_and_scorer(self) -> None:
        ffmpeg = os.environ.get("CHONGMING_FFMPEG") or shutil.which("ffmpeg")
        assert ffmpeg is not None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            w, h = 640, 480
            s1, aux1 = _field_station_with_aux("s1", (0.0, 0.0, 0.0), w, h)
            s2, aux2 = _field_station_with_aux("s2", (40.0, 0.0, 0.0), w, h)

            blank = np.zeros((h, w, 3), dtype=np.uint8).tobytes()
            spot = np.zeros((h, w, 3), dtype=np.uint8)
            spot[235:245, 315:325] = 255
            mp4_s1 = root / "s1_main.mp4"
            mp4_s2 = root / "s2_blank.mp4"
            mp4_a1 = root / "s1_aux.mp4"
            mp4_a2 = root / "s2_aux.mp4"
            _encode_h264_mp4(ffmpeg, mp4_s1, [blank, spot.tobytes(), spot.tobytes()], w, h)
            _encode_h264_mp4(ffmpeg, mp4_s2, [blank, blank, blank], w, h)
            _encode_h264_mp4(ffmpeg, mp4_a1, [aux1, aux1, aux1], w, h)
            _encode_h264_mp4(ffmpeg, mp4_a2, [aux2, aux2, aux2], w, h)

            pol_path = root / "pol2.json"
            pol_path.write_text(dump_json(_field_device_policy(("s1", "s2"))), encoding="utf-8")
            fus_path = root / "fus.json"
            fus_path.write_text(
                dump_json(
                    reference_policy(
                        search=m.VoxelSearchSpec(
                            world_frame_id=m.WorldFrameId("enu"),
                            min_enu_m=(-10.0, -10.0, 10.0),
                            max_enu_m=(50.0, 30.0, 60.0),
                            step_enu_m=(5.0, 5.0, 5.0),
                            forward_range_m=None,
                        ),
                        clock_domain_id="clk",
                        window_width_ns=40_000_000,
                    )
                ),
                encoding="utf-8",
            )
            times = (1_000_000_000, 1_040_000_000, 1_080_000_000)

            # 1. Dual-camera station with NO auxiliary frames in integrated mode + viewer + online scorer.
            sc_no_aux = root / "sc_no_aux"
            with TestSceneWriter(
                sc_no_aux,
                scenario_id="dual-no-aux",
                world_frame_id="enu",
                clock_domain_id="clk",
                start_time_ns=times[0],
                end_time_ns=times[-1],
                stations=[station_config_to_proto(s1), station_config_to_proto(s2)],
            ) as sw:
                for sid, mp4_p in (("s1", mp4_s1), ("s2", mp4_s2)):
                    sw.add_video_file(
                        mp4_p,
                        f"video/{sid}-main.mp4",
                        {"fps": 25.0, "frame_count": 3, "width_px": w, "height_px": h},
                    )
                    for idx, t in enumerate(times):
                        sw.add_frame_index(
                            truth_pb2.FrameIndexEntry(
                                station_id=sid,
                                station_session_id=f"sess-{sid}",
                                camera_role=common_pb2.CAMERA_ROLE_MAIN,
                                frame_index=idx,
                                frame_id=f"{sid}-m-{idx}",
                                capture_time=common_pb2.CaptureTime(
                                    clock_domain_id="clk",
                                    exposure_start_ns=t,
                                    exposure_end_ns=t,
                                    rolling_shutter_model="none",
                                ),
                                station_config_digest=f"dig-{sid}",
                                video_relpath=f"video/{sid}-main.mp4",
                                width_px=w,
                                height_px=h,
                            )
                        )

            run_no_aux = root / "run_no_aux"
            score_no_aux = root / "score_no_aux"
            vs = subprocess.Popen(
                [
                    str(VSWITCH_BIN),
                    "replay",
                    "--scene",
                    str(sc_no_aux),
                    "--run-output",
                    str(run_no_aux),
                    "--run-id",
                    "run-no-aux",
                    "--mode",
                    "integrated",
                    "--bus-bind",
                    "127.0.0.1:0",
                    "--truth-bind",
                    "127.0.0.1:0",
                    "--viewer-bind",
                    "127.0.0.1:0",
                    "--wait-scorer",
                    "--step-timeout-s",
                    "5",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            viewer_frames: list[dict[str, Any]] = []
            viewer_thread: threading.Thread | None = None
            ep = cp = sp = None
            try:
                addrs = prod_e2e._wait_vswitch_listening(vs)
                bus_host, bus_port = addrs["bus_addr"].rsplit(":", 1)
                truth_host, truth_port = addrs["truth_addr"].rsplit(":", 1)
                v_host, v_port = addrs["viewer_addr"].rsplit(":", 1)

                def read_viewer() -> None:
                    with socket.create_connection((v_host, int(v_port)), timeout=10.0) as sock:
                        while True:
                            hdr = sock.recv(4)
                            if len(hdr) < 4:
                                break
                            (length,) = struct.unpack(">I", hdr)
                            buf = bytearray()
                            while len(buf) < length:
                                chunk = sock.recv(length - len(buf))
                                if not chunk:
                                    break
                                buf.extend(chunk)
                            if len(buf) == length:
                                viewer_frames.append(json.loads(buf.decode("utf-8")))

                viewer_thread = threading.Thread(target=read_viewer, daemon=True)
                viewer_thread.start()

                ep = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "chongming.bus.edge_process",
                        "--host",
                        bus_host,
                        "--port",
                        bus_port,
                        "--policy",
                        str(pol_path),
                        "--warmup-hold-s",
                        "0",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                cp = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "chongming.bus.center_process",
                        "--host",
                        bus_host,
                        "--port",
                        bus_port,
                        "--policy",
                        str(fus_path),
                        "--timeout-basis",
                        "capture_time",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                sp = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "chongming_scorer.cli",
                        "subscribe",
                        "--host",
                        truth_host,
                        "--port",
                        truth_port,
                        "--scene",
                        str(sc_no_aux),
                        "--output",
                        str(score_no_aux),
                        "--mode",
                        "integrated",
                        "--warmup-s",
                        "0",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for label, proc in (
                    ("edge", ep),
                    ("center", cp),
                    ("scorer", sp),
                    ("vswitch", vs),
                ):
                    _, err = proc.communicate(timeout=30)
                    self.assertEqual(proc.returncode, 0, f"{label} failed: {err}")
                viewer_thread.join(timeout=5.0)
            finally:
                for p in (ep, cp, sp, vs):
                    if p is not None and p.poll() is None:
                        p.kill()

            self.assertGreater(len(viewer_frames), 0)
            for vf in viewer_frames:
                self.assertFalse(vf["has_truth"])
                self.assertNotIn("truth", vf)
                self.assertNotIn("score", vf)
            online_rep = json.loads((score_no_aux / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(online_rep["status"], "unavailable")
            self.assertIn("truth_unavailable", online_rep["reason_codes"])
            self.assertEqual(online_rep["available_outputs"]["edge"]["detection_batches"], 6)
            self.assertGreater(online_rep["available_outputs"]["edge"]["observations"], 0)
            pkg_no_aux = RunOutputPackage.open(run_no_aux, verify_hashes=True)
            self.assertFalse(
                any(
                    m_msg.WhichOneof("payload") == "ray_event"
                    for m_msg in pkg_no_aux.iter_edge_events()
                )
            )

            # 2. Incomplete coverage (s1 calibrated with target, s2 calibrated blank) via Rust Edge.
            sc_inc = root / "sc_inc"
            with TestSceneWriter(
                sc_inc,
                scenario_id="inc-cov",
                world_frame_id="enu",
                clock_domain_id="clk",
                start_time_ns=times[0],
                end_time_ns=times[-1],
                stations=[station_config_to_proto(s1), station_config_to_proto(s2)],
            ) as sw:
                for sid, m_mp4, a_mp4 in (
                    ("s1", mp4_s1, mp4_a1),
                    ("s2", mp4_s2, mp4_a2),
                ):
                    sw.add_video_file(
                        m_mp4,
                        f"video/{sid}-main.mp4",
                        {"fps": 25.0, "frame_count": 3, "width_px": w, "height_px": h},
                    )
                    sw.add_video_file(
                        a_mp4,
                        f"video/{sid}-aux.mp4",
                        {"fps": 25.0, "frame_count": 3, "width_px": w, "height_px": h},
                    )
                    for idx, t in enumerate(times):
                        ct = common_pb2.CaptureTime(
                            clock_domain_id="clk",
                            exposure_start_ns=t,
                            exposure_end_ns=t,
                            rolling_shutter_model="none",
                        )
                        sw.add_frame_index(
                            truth_pb2.FrameIndexEntry(
                                station_id=sid,
                                station_session_id=f"sess-{sid}",
                                camera_role=common_pb2.CAMERA_ROLE_AUXILIARY,
                                frame_index=idx,
                                frame_id=f"{sid}-a-{idx}",
                                capture_time=ct,
                                station_config_digest=f"dig-{sid}",
                                video_relpath=f"video/{sid}-aux.mp4",
                                width_px=w,
                                height_px=h,
                            )
                        )
                        sw.add_frame_index(
                            truth_pb2.FrameIndexEntry(
                                station_id=sid,
                                station_session_id=f"sess-{sid}",
                                camera_role=common_pb2.CAMERA_ROLE_MAIN,
                                frame_index=idx,
                                frame_id=f"{sid}-m-{idx}",
                                capture_time=ct,
                                station_config_digest=f"dig-{sid}",
                                video_relpath=f"video/{sid}-main.mp4",
                                width_px=w,
                                height_px=h,
                            )
                        )

            run_inc = root / "run_inc"
            vs_i = subprocess.Popen(
                [
                    str(VSWITCH_BIN),
                    "replay",
                    "--scene",
                    str(sc_inc),
                    "--run-output",
                    str(run_inc),
                    "--run-id",
                    "run-inc",
                    "--mode",
                    "integrated",
                    "--bus-bind",
                    "127.0.0.1:0",
                    "--step-timeout-s",
                    "5",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            ep_i = cp_i = None
            try:
                addrs_i = prod_e2e._wait_vswitch_listening(vs_i)
                bh, bp = addrs_i["bus_addr"].rsplit(":", 1)
                ep_i = subprocess.Popen(
                    [
                        str(EDGE_BIN),
                        "--host",
                        bh,
                        "--port",
                        bp,
                        "--policy",
                        str(pol_path),
                        "--warmup-hold-s",
                        "0",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                cp_i = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "chongming.bus.center_process",
                        "--host",
                        bh,
                        "--port",
                        bp,
                        "--policy",
                        str(fus_path),
                        "--timeout-basis",
                        "capture_time",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for label, proc in (("edge", ep_i), ("center", cp_i), ("vswitch", vs_i)):
                    _, err = proc.communicate(timeout=30)
                    self.assertEqual(proc.returncode, 0, f"{label} failed: {err}")
            finally:
                for live_proc in (ep_i, cp_i, vs_i):
                    if live_proc is not None and live_proc.poll() is None:
                        live_proc.kill()

            pkg_inc = RunOutputPackage.open(run_inc, verify_hashes=True)
            inc_rays = [
                m_msg.ray_event
                for m_msg in pkg_inc.iter_edge_events()
                if m_msg.WhichOneof("payload") == "ray_event"
            ]
            inc_meas = [
                m_msg.fusion_measurement
                for m_msg in pkg_inc.iter_center_output()
                if m_msg.WhichOneof("payload") == "fusion_measurement"
            ]
            self.assertGreater(len(inc_rays), 0)
            self.assertTrue(all(r.envelope.station_id == "s1" for r in inc_rays))
            self.assertEqual(len(inc_meas), 0)

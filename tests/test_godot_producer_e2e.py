"""End-to-end tests: Godot Producer -> V-Switch Record -> Test Scene -> Edge/Center/Scorer."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from chongming.composition.center import reference_policy
from chongming.contracts import device as d
from chongming.contracts import model as m
from chongming.contracts.json_codec import _encode
from chongming.modules.device import OPENCV_CALIBRATION_ESTIMATOR_ID
from chongming_proto.online import common_pb2
from chongming_proto.truth import truth_pb2
from chongming_scorer.scorer import score_run
from chongming_scorer.storage import RunOutputPackage, TestScenePackage

ROOT = Path(__file__).resolve().parents[1]
PRODUCER_DIR = ROOT / "godot_producer"
VSWITCH_BIN = ROOT / "target/debug/vswitch"
GODOT = os.environ.get("CHONGMING_GODOT") or shutil.which("godot4") or shutil.which("godot")


def _has_display_or_xvfb() -> bool:
    return bool(
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or shutil.which("xvfb-run")
    )


def _godot_render_cmd(scene_tscn: str, bus_addr: str, truth_addr: str) -> list[str]:
    assert GODOT is not None
    base = [
        GODOT,
        "--rendering-driver",
        "opengl3",
        "--path",
        str(PRODUCER_DIR),
        "--",
        "--scene-tscn",
        scene_tscn,
        "--bus",
        bus_addr,
        "--truth",
        truth_addr,
    ]
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return base
    xvfb = shutil.which("xvfb-run")
    assert xvfb is not None
    return [xvfb, "-a", *base]


def _wait_vswitch_listening(proc: subprocess.Popen[str], timeout_s: float = 10.0) -> dict[str, Any]:
    assert proc.stderr is not None
    lines: list[str] = []
    bound: dict[str, Any] | None = None
    event = threading.Event()

    def _reader() -> None:
        nonlocal bound
        assert proc.stderr is not None
        for line in proc.stderr:
            lines.append(line)
            if line.startswith("vswitch listening: "):
                bound = json.loads(line.removeprefix("vswitch listening: "))
                event.set()
                return

    thread = threading.Thread(target=_reader, daemon=True)
    thread.start()
    if not event.wait(timeout_s) or bound is None:
        raise TimeoutError(f"vswitch did not report listening addresses; stderr:\n{''.join(lines)}")
    return bound


def _record_scene_from_godot(
    scene_tscn: str,
    scene_dir: Path,
    work_dir: Path,
    scenario_id: str,
    *,
    random_seed: int = 1001,
) -> dict[str, Any]:
    vswitch_proc = subprocess.Popen(
        [
            str(VSWITCH_BIN),
            "record",
            "--output-scene",
            str(scene_dir),
            "--scenario-id",
            scenario_id,
            "--random-seed",
            str(random_seed),
            "--bus-bind",
            "127.0.0.1:0",
            "--truth-bind",
            "127.0.0.1:0",
            "--fps",
            "10",
            "--h264-gop",
            "5",
            "--handshake-timeout-s",
            "20",
        ],
        cwd=work_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        addrs = _wait_vswitch_listening(vswitch_proc)
        cmd = _godot_render_cmd(scene_tscn, addrs["bus_addr"], addrs["truth_addr"])
        prod = subprocess.run(
            cmd,
            cwd=work_dir,
            check=False,
            capture_output=True,
            text=True,
            timeout=60.0,
        )
        if prod.returncode != 0:
            vswitch_proc.terminate()
            v_out, v_err = vswitch_proc.communicate(timeout=5)
            raise AssertionError(
                f"Godot Producer failed (rc={prod.returncode}):\nSTDOUT:\n{prod.stdout}\nSTDERR:\n{prod.stderr}\n"
                f"VSWITCH STDOUT:\n{v_out}\nVSWITCH STDERR:\n{v_err}"
            )
        self_summary = next(
            (
                json.loads(line.removeprefix("PRODUCER_SUMMARY "))
                for line in prod.stdout.splitlines()
                if line.startswith("PRODUCER_SUMMARY ")
            ),
            None,
        )
        if not self_summary or not self_summary.get("ok"):
            raise AssertionError(f"Unexpected producer summary: {prod.stdout}\n{prod.stderr}")
        v_stdout, v_stderr = vswitch_proc.communicate(timeout=30.0)
        if vswitch_proc.returncode != 0:
            raise AssertionError(
                f"vswitch record failed (rc={vswitch_proc.returncode}):\nSTDOUT:\n{v_stdout}\nSTDERR:\n{v_stderr}"
            )
        return dict(json.loads(v_stdout))
    finally:
        if vswitch_proc.poll() is None:
            vswitch_proc.kill()
            vswitch_proc.wait(timeout=5)


def _write_policies(setup_dir: Path) -> tuple[Path, Path]:
    eye2 = ((1.0, 0.0), (0.0, 1.0))
    dev_policy = {
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
                minimum_component_area_px2=4.0,
                maximum_component_area_px2=None,
                background_warmup_frame_count=1,
                background_history_frame_count=10,
                background_variance_threshold=16.0,
                background_learning_rate=0.0,
                background_detect_shadows=False,
            ),
            tracking=d.Tracking2DPolicy(
                algorithm_id="constant-velocity-nearest-neighbor-v1",
                association_gate_px=100.0,
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
                stale_after_ns=5_000_000_000,
                recovery_success_count=1,
                minimum_reference_count=4,
                maximum_reprojection_rmse_px=3.0,
                maximum_rotation_only_parallax_px=None,
                estimator_id=OPENCV_CALIBRATION_ESTIMATOR_ID,
            ),
            rays=d.RayGenerationPolicy(
                uncertainty_model_id="tangent_gaussian_v1",
                fallback_centroid_covariance_px2=eye2,
                unmodelled_high_frequency_jitter_covariance_rad2=((0.0, 0.0), (0.0, 0.0)),
                unmodelled_relative_flex_covariance_rad2=((0.0, 0.0), (0.0, 0.0)),
            ),
        )
        for sid in ("s1", "s2")
    }
    fusion_policy = reference_policy(
        search=m.VoxelSearchSpec(
            world_frame_id=m.WorldFrameId("enu"),
            min_enu_m=(-95.0, 95.0, 20.0),
            max_enu_m=(45.0, 145.0, 70.0),
            step_enu_m=(5.0, 5.0, 5.0),
            forward_range_m=None,
        ),
        clock_domain_id="sim",
        window_width_ns=100_000_000,
    )
    dev_path = setup_dir / "device_policy.json"
    fus_path = setup_dir / "fusion_policy.json"
    dev_path.write_text(json.dumps(_encode(dev_policy), indent=2), encoding="utf-8")
    fus_path.write_text(json.dumps(_encode(fusion_policy), indent=2), encoding="utf-8")
    return dev_path, fus_path


def _replay_and_score(
    scene_dir: Path,
    run_dir: Path,
    score_dir: Path,
    *,
    mode: str,
    dev_policy_path: Path | None,
    fus_policy_path: Path | None,
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
            f"run-{scene_dir.name}-{mode}",
            "--mode",
            mode,
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
        addrs = _wait_vswitch_listening(vswitch_proc)
        bus_host, bus_port = addrs["bus_addr"].rsplit(":", 1)
        truth_host, truth_port = addrs["truth_addr"].rsplit(":", 1)

        if mode in {"edge", "integrated"}:
            assert dev_policy_path is not None
            # No --scenario passed to EdgeProcess: calibration runs purely from wire StationConfig.
            edge_proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "chongming.bus.edge_process",
                    "--host",
                    bus_host,
                    "--port",
                    bus_port,
                    "--policy",
                    str(dev_policy_path),
                    "--warmup-hold-s",
                    "0",
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        if mode in {"center", "integrated"}:
            assert fus_policy_path is not None
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
                mode,
                "--warmup-s",
                "0",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        procs: list[tuple[str, subprocess.Popen[str]]] = []
        if edge_proc is not None:
            procs.append(("edge", edge_proc))
        if center_proc is not None:
            procs.append(("center", center_proc))
        procs.append(("scorer", scorer_proc))
        procs.append(("vswitch replay", vswitch_proc))

        for label, proc in procs:
            out, err = proc.communicate(timeout=90.0)
            if proc.returncode != 0:
                raise AssertionError(
                    f"{label} failed (rc={proc.returncode}):\nSTDOUT:\n{out}\nSTDERR:\n{err}"
                )
        return score_run(scene_dir, run_dir, mode=mode, warmup_s=0.0)
    finally:
        for p in (edge_proc, center_proc, scorer_proc, vswitch_proc):
            if p is not None and p.poll() is None:
                p.kill()
                p.wait(timeout=5)


@unittest.skipUnless(
    GODOT and VSWITCH_BIN.is_file() and _has_display_or_xvfb(),
    "Godot 4, compiled target/debug/vswitch, and X11/Wayland/xvfb-run are required",
)
class GodotProducerE2ETests(unittest.TestCase):
    def test_clean_and_scenic_godot_scenes_record_and_drive_full_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_tmp = Path(tmp)
            dev_policy, fus_policy = _write_policies(root_tmp)

            for name, scene_tscn, expect_occluded, seed in (
                ("clean", "res://scenes/clean_range.tscn", False, 1001),
                ("scenic", "res://scenes/scenic_ridge.tscn", True, 2002),
            ):
                with self.subTest(scene=name):
                    scene_dir = root_tmp / f"scene_{name}"
                    run_dir = root_tmp / f"run_{name}"
                    score_dir = root_tmp / f"score_{name}"

                    manifest = _record_scene_from_godot(
                        scene_tscn, scene_dir, root_tmp, f"e2e-{name}", random_seed=seed
                    )
                    self.assertEqual(manifest["format"], "chongming-test-scene-v1")
                    self.assertEqual(manifest["random_seed"], seed)
                    self.assertIn("tgt-1", manifest["targets"])
                    self.assertGreater(manifest["targets"]["tgt-1"]["body_aabb_diagonal_m"], 0.0)

                    # Verify immutable package via both Rust CLI and Python Scorer.
                    verify_rust = subprocess.run(
                        [str(VSWITCH_BIN), "verify-scene", str(scene_dir), "--verify-videos"],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=30.0,
                    )
                    self.assertEqual(verify_rust.returncode, 0, verify_rust.stderr)
                    pkg = TestScenePackage.open(scene_dir, verify_hashes=True, verify_videos=True)
                    stations = pkg.station_configs()
                    self.assertEqual([s.station_id for s in stations], ["s1", "s2"])
                    for st in stations:
                        self.assertEqual(len(st.reference_anchor.landmarks), 4)

                    truth_frames = list(pkg.iter_truth_frames())
                    self.assertEqual(len(truth_frames), 10)
                    s1_statuses = {
                        p.occlusion_status
                        for tf in truth_frames
                        for p in tf.projections
                        if p.station_id == "s1"
                    }
                    self.assertIn(truth_pb2.OCCLUSION_STATUS_OUT_OF_FOV, s1_statuses)
                    self.assertIn(truth_pb2.OCCLUSION_STATUS_VISIBLE, s1_statuses)
                    if expect_occluded:
                        self.assertIn(truth_pb2.OCCLUSION_STATUS_OCCLUDED, s1_statuses)
                    else:
                        self.assertNotIn(truth_pb2.OCCLUSION_STATUS_OCCLUDED, s1_statuses)

                    # Replay recorded Test Scene through unmodified Edge + Center + Scorer.
                    t0 = time.monotonic()
                    report = _replay_and_score(
                        scene_dir,
                        run_dir,
                        score_dir,
                        mode="integrated",
                        dev_policy_path=dev_policy,
                        fus_policy_path=fus_policy,
                        work_dir=root_tmp,
                    )
                    elapsed = time.monotonic() - t0
                    self.assertGreater(elapsed, 0.0)

                    run_pkg = RunOutputPackage.open(run_dir, verify_hashes=True)
                    cal_events = [
                        msg.calibration_state
                        for msg in run_pkg.iter_edge_events()
                        if msg.WhichOneof("payload") == "calibration_state"
                    ]
                    valid_cal_stations = {
                        ev.state.station_id
                        for ev in cal_events
                        if ev.state.health == common_pb2.CALIBRATION_HEALTH_VALID
                    }
                    self.assertEqual(valid_cal_stations, {"s1", "s2"})

                    self.assertGreater(report["edge"]["aggregate"]["tp"], 0, report)
                    self.assertGreater(report["center"]["metrics"]["tp"], 0, report)
                    self.assertTrue((score_dir / "scores.pb.zst").is_file())

    def test_godot_scene_center_edge_modes_and_determinism(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_tmp = Path(tmp)
            dev_policy, fus_policy = _write_policies(root_tmp)
            scene_a = root_tmp / "scene_det_a"
            scene_b = root_tmp / "scene_det_b"

            _record_scene_from_godot(
                "res://scenes/clean_range.tscn", scene_a, root_tmp, "det-clean", random_seed=1001
            )
            _record_scene_from_godot(
                "res://scenes/clean_range.tscn", scene_b, root_tmp, "det-clean", random_seed=1001
            )

            pkg_a = TestScenePackage.open(scene_a, verify_hashes=True)
            pkg_b = TestScenePackage.open(scene_b, verify_hashes=True)
            self.assertEqual(
                [tf.SerializeToString() for tf in pkg_a.iter_truth_frames()],
                [tf.SerializeToString() for tf in pkg_b.iter_truth_frames()],
            )
            self.assertEqual(
                [r.SerializeToString() for r in pkg_a.iter_virtual_rays()],
                [r.SerializeToString() for r in pkg_b.iter_virtual_rays()],
            )

            center_report = _replay_and_score(
                scene_a,
                root_tmp / "run_center",
                root_tmp / "score_center",
                mode="center",
                dev_policy_path=None,
                fus_policy_path=fus_policy,
                work_dir=root_tmp,
            )
            self.assertGreater(center_report["metrics"]["tp"], 0, center_report)

            edge_report = _replay_and_score(
                scene_a,
                root_tmp / "run_edge",
                root_tmp / "score_edge",
                mode="edge",
                dev_policy_path=dev_policy,
                fus_policy_path=None,
                work_dir=root_tmp,
            )
            self.assertGreater(edge_report["aggregate"]["tp"], 0, edge_report)

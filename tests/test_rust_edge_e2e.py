"""End-to-end tests for Rust Edge (`crates/edge`) against V-Switch, Center, and Scorer."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from chongming_proto.online import common_pb2
from chongming_scorer.scorer import score_run
from chongming_scorer.storage import RunOutputPackage, TestScenePackage
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

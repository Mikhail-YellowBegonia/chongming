"""Tests for passive V-Switch Godot Viewer (`chongming-viewer-v1`) and evaluation.gd removal."""

from __future__ import annotations

import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from chongming.bus.client import OnlineBusClient
from chongming_proto.online import common_pb2, online_pb2

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "src/chongming/rendering/godot"
VSWITCH_BIN = ROOT / "target/debug/vswitch"
GODOT = os.environ.get("CHONGMING_GODOT") or shutil.which("godot4") or shutil.which("godot")
MAX_UINT64_TIME = 2**53 + 98765


def encode_be_json_frame(payload: dict[str, Any]) -> bytes:
    raw = json.dumps(payload, allow_nan=False).encode("utf-8")
    return struct.pack(">I", len(raw)) + raw


def make_display_frame(
    *,
    run_id: str = "run-viewer-1",
    mode: str = "integrated",
    update_id: int = 1,
    sequence_no: int = 1,
    experiment_time_ns: int = MAX_UINT64_TIME,
    has_truth: bool = True,
    include_truth: bool = True,
    include_score: bool = True,
    include_voxels: bool = True,
) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "schema_version": "chongming-viewer-v1",
        "run_id": run_id,
        "mode": mode,
        "update_id": str(update_id),
        "sequence_no": str(sequence_no),
        "experiment_time_ns": str(experiment_time_ns),
        "has_truth": has_truth,
        "stations": [
            {
                "station_id": "station-west",
                "main_camera_position_enu_m": [-250.0, 0.0, 12.0],
                "baseline_orientation_enu_from_main": [
                    [1.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0],
                    [0.0, 0.0, 1.0],
                ],
                "calibrated_orientation_enu_from_main": [
                    [1.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0],
                    [0.0, 0.0, 1.0],
                ],
                "calibration_health": "valid",
                "calibration_usage": "valid",
                "state_version": "3",
                "solution_id": "sol-3",
            }
        ],
        "rays": {
            "items": [
                {
                    "event_id": ["station-west", "sess-w1", str(2**53 + update_id)],
                    "frame_id": f"station-west-main-{update_id:04d}",
                    "capture_time_ns": str(experiment_time_ns),
                    "exposure_start_ns": str(experiment_time_ns - 5),
                    "exposure_end_ns": str(experiment_time_ns + 5),
                    "origin_enu_m": [-250.0, 0.0, 12.0],
                    "direction_enu_unit": [0.0, 1.0, 0.0],
                    "calibration_usage": "valid",
                    "detection_score": 0.9,
                    "angular_std_rad": 0.002,
                    "local_track_id": "1",
                    "quality_flags": [],
                }
            ],
            "calibration_states": [
                {
                    "event_id": ["station-west", "sess-w1", str(2**53 + 100 + update_id)],
                    "station_id": "station-west",
                    "state_version": "3",
                    "health": "valid",
                    "solution_id": "sol-3",
                    "observed_at_ns": str(experiment_time_ns),
                    "reason_codes": [],
                }
            ],
            "diagnostics": [
                {
                    "event_id": ["station-west", "sess-w1", str(2**53 + 200 + update_id)],
                    "station_id": "station-west",
                    "capture_time_ns": str(experiment_time_ns),
                    "category": "ok",
                    "reason_codes": [],
                }
            ],
        },
        "answer": {
            "measurements": [
                {
                    "center_session_id": "sess-c1",
                    "measurement_id": f"m-{update_id}",
                    "reference_time_ns": str(experiment_time_ns),
                    "support_exposure_span_ns": "0",
                    "status": "located",
                    "position_enu_m": [120.0, 840.0, 210.0],
                    "position_std_enu_m": [2.0, 3.0, 4.0],
                    "supporting_ray_event_ids": [
                        ["station-west", "sess-w1", str(2**53 + update_id)]
                    ],
                    "supporting_station_ids": ["station-west"],
                    "minimum_intersection_angle_deg": 28.4,
                    "geometry_condition": 2.1,
                    "reason_codes": [],
                },
                {
                    "center_session_id": "sess-c1",
                    "measurement_id": f"m-unlocated-{update_id}",
                    "reference_time_ns": str(experiment_time_ns),
                    "support_exposure_span_ns": "0",
                    "status": "insufficient_stations",
                    "position_enu_m": None,
                    "position_std_enu_m": None,
                    "supporting_ray_event_ids": [],
                    "supporting_station_ids": ["station-west"],
                    "minimum_intersection_angle_deg": None,
                    "geometry_condition": None,
                    "reason_codes": ["one_station"],
                },
            ],
            "tracks": [
                {
                    "center_session_id": "sess-c1",
                    "track_id": "trk-1",
                    "event_time_ns": str(experiment_time_ns),
                    "lifecycle": "confirmed",
                    "position_enu_m": [120.0, 840.0, 210.0],
                    "velocity_enu_mps": [18.0, -4.0, 0.5],
                    "position_std_enu_m": [1.5, 2.0, 1.0],
                    "velocity_std_enu_mps": [0.5, 0.5, 0.2],
                    "source_measurement_ids": [f"m-{update_id}"],
                    "reason_codes": [],
                }
            ],
        },
    }
    if has_truth and include_truth:
        frame["truth"] = {
            "world_frame_id": "enu",
            "experiment_time_ns": str(experiment_time_ns),
            "step_index": str(update_id),
            "targets": [
                {
                    "target_id": "target-1",
                    "target_class": "f86",
                    "position_enu_m": [120.5, 840.0, 210.0],
                    "velocity_enu_mps": [18.0, -4.0, 0.5],
                    "bounding_radius_m": 6.5,
                    "body_aabb_diagonal_m": 13.0,
                }
            ],
            "projections": [
                {
                    "station_id": "station-west",
                    "target_id": "target-1",
                    "occlusion_status": "visible",
                    "projected_centroid_px": [964.2, 412.8],
                    "projected_diagonal_px": 14.3,
                    "range_m": 939.4,
                    "frame_id": f"station-west-main-{update_id:04d}",
                }
            ],
        }
    if has_truth and include_score:
        frame["score"] = {
            "events": [
                {
                    "run_id": run_id,
                    "experiment_time_ns": str(experiment_time_ns),
                    "matched_count": "1",
                    "unmatched_count": "0",
                    "unevaluable_count": "0",
                    "mean_error": 1.25,
                    "rmse": 1.25,
                    "max_error": 1.25,
                    "error_unit": "px",
                    "extra_metrics": [{"name": "recall", "value": 1.0, "unit": "ratio"}],
                },
                {
                    "run_id": run_id,
                    "experiment_time_ns": str(experiment_time_ns),
                    "matched_count": "1",
                    "unmatched_count": "0",
                    "unevaluable_count": "0",
                    "mean_error": 0.5,
                    "rmse": 0.5,
                    "max_error": 0.5,
                    "error_unit": "m",
                    "extra_metrics": [{"name": "missed_targets", "value": 0.0, "unit": "count"}],
                },
            ]
        }
    if include_voxels:
        frame["sparse_voxel_grid"] = {
            "center_session_id": "sess-c1",
            "world_frame_id": "enu",
            "window_start_ns": str(experiment_time_ns - 10),
            "window_end_ns": str(experiment_time_ns),
            "min_enu_m": [100.0, 820.0, 200.0],
            "step_enu_m": [10.0, 10.0, 10.0],
            "max_score": 3,
            "cells": [{"ix": 2, "iy": 2, "iz": 1, "station_votes": 3}],
        }
    return frame


DISPLAY_RECEIVER_SCRIPT = """extends SceneTree
const Visualization = preload("res://visualization.gd")
func _enu(value: Array) -> Vector3:
    return Vector3(float(value[0]), float(value[2]), -float(value[1]))
func _initialize() -> void:
    var receiver := Visualization.new()
    receiver._enu = _enu
    var frames: Array = JSON.parse_string(FileAccess.get_file_as_string(OS.get_cmdline_user_args()[0]))
    var reports := []
    for frame in frames:
        var accepted := receiver.receive_display_frame(frame, true)
        receiver.select_measurement("m-1")
        reports.append({
            "accepted": accepted,
            "error": receiver.error,
            "run_id": receiver.current_run_id,
            "mode": receiver.current_mode,
            "has_truth": receiver.has_truth,
            "update_count": receiver.update_count,
            "last_update_id": receiver.last_update_id_str,
            "last_sequence_no": receiver.last_sequence_no_str,
            "dropped_sequence_count": receiver.dropped_sequence_count,
            "display_time_ns": receiver.display_time_ns,
            "ray_count": receiver.rays.size(),
            "measurement_count": receiver.measurements.size(),
            "track_count": receiver.tracks.size(),
            "track_history_len": receiver.track_history.get("trk-1", []).size(),
            "score_units": receiver.score_by_unit.keys(),
            "has_latest_truth": not receiver.latest_truth.is_empty(),
            "selection": receiver.selection_summary(),
            "voxel_count": receiver._voxel_cell_count(),
            "performance": receiver.performance_summary(),
        })
    print("REPORT " + JSON.stringify(reports))
    quit()
"""


TEST_VIEWER_WORKER = """extends "res://worker.gd"
var _last_reported_updates := -1
var _last_reported_reconnects := -1
var _last_reported_conn := ""
func _process(delta: float) -> void:
    super._process(delta)
    if _viewer_mode:
        var snap := viewer_snapshot()
        if snap.update_count != _last_reported_updates or snap.reconnect_count != _last_reported_reconnects or snap.connection_state != _last_reported_conn:
            _last_reported_updates = snap.update_count
            _last_reported_reconnects = snap.reconnect_count
            _last_reported_conn = snap.connection_state
            snap["diag_text"] = _ui._diagnostics.text if _ui != null else ""
            snap["info_text"] = _ui._info.text if _ui != null else ""
            snap["station_text"] = _ui._station_states.text if _ui != null else ""
            snap["voxel_text"] = _ui._voxel_status.text if _ui != null else ""
            snap["truth_disabled"] = _ui._visualization_layers["truth"].disabled if _ui != null else false
            snap["truth_children"] = _visualization.layers["truth"].get_child_count()
            snap["ray_children"] = _visualization.layers["rays"].get_child_count()
            snap["meas_children"] = _visualization.layers["measurements"].get_child_count()
            snap["track_children"] = _visualization.layers["tracks"].get_child_count()
            snap["voxel_children"] = _visualization.layers["voxels"].get_child_count()
            print("VIEWER_SNAP " + JSON.stringify(snap))
"""


class _MockViewerPushServer:
    """Minimal non-blocking TCP push server emitting 4-byte BE chongming-viewer-v1 frames."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self._sock.settimeout(0.05)
        self.port: int = self._sock.getsockname()[1]
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.received_from_viewer = bytearray()
        self.accepted_connections = 0
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            client.setblocking(False)
            with self._lock:
                self._clients.append(client)
                self.accepted_connections += 1

    def wait_for_client(self, min_connections: int = 1, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self.accepted_connections >= min_connections and self._clients:
                    return
            time.sleep(0.02)
        raise TimeoutError(f"Timed out waiting for viewer connection >= {min_connections}")

    def push(self, frame: dict[str, Any]) -> int:
        wire = encode_be_json_frame(frame)
        delivered = 0
        with self._lock:
            alive: list[socket.socket] = []
            for client in self._clients:
                try:
                    data = client.recv(4096)
                    if data:
                        self.received_from_viewer.extend(data)
                except BlockingIOError:
                    pass
                except OSError:
                    client.close()
                    continue
                try:
                    client.sendall(wire)
                    alive.append(client)
                    delivered += 1
                except OSError:
                    client.close()
            self._clients = alive
        return delivered

    def drop_clients(self) -> None:
        with self._lock:
            for client in self._clients:
                try:
                    data = client.recv(4096)
                    if data:
                        self.received_from_viewer.extend(data)
                except (BlockingIOError, OSError):
                    pass
                client.close()
            self._clients.clear()

    def close(self) -> None:
        self._stop.set()
        self.drop_clients()
        self._sock.close()
        self._thread.join(timeout=2)


class EvaluationRemovedTests(unittest.TestCase):
    def test_evaluation_gd_is_removed_and_not_referenced(self) -> None:
        self.assertFalse((PROJECT / "evaluation.gd").exists())
        for filename in ("worker.gd", "visualization.gd", "ui.gd"):
            self.assertNotIn("evaluation.gd", (PROJECT / filename).read_text())


@unittest.skipUnless(GODOT, "Godot 4 executable is required")
class ViewerProtocolReceiverTests(unittest.TestCase):
    def receive(self, *frames: dict[str, Any]) -> list[dict[str, Any]]:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            shutil.copy(PROJECT / "visualization.gd", project)
            (project / "project.godot").write_text("config_version=5\n")
            (project / "receiver.gd").write_text(DISPLAY_RECEIVER_SCRIPT)
            source = project / "frames.json"
            source.write_text(json.dumps(frames, allow_nan=False))
            assert GODOT is not None
            result = subprocess.run(
                [
                    GODOT,
                    "--headless",
                    "--path",
                    str(project),
                    "--script",
                    "receiver.gd",
                    "--",
                    str(source),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
        self.assertNotIn("SCRIPT ERROR", result.stderr)
        report = next(
            line.removeprefix("REPORT ")
            for line in result.stdout.splitlines()
            if line.startswith("REPORT ")
        )
        decoded: list[dict[str, Any]] = json.loads(report)
        return decoded

    def test_accepts_virtual_and_live_frames_preserves_64bit_and_rejects_invalid(self) -> None:
        virtual_1 = make_display_frame(update_id=1, sequence_no=1)
        virtual_2 = make_display_frame(
            update_id=2,
            sequence_no=5,  # Simulates V-Switch dropping frames 2..4 on Viewer lag.
            experiment_time_ns=MAX_UINT64_TIME + 100,
        )
        live_1 = make_display_frame(
            run_id="run-live-1",
            mode="live",
            update_id=1,
            sequence_no=1,
            experiment_time_ns=MAX_UINT64_TIME + 200,
            has_truth=False,
            include_truth=False,
            include_score=False,
            include_voxels=False,
        )

        bad_numeric_time = make_display_frame(run_id="run-live-1", mode="live", has_truth=False)
        bad_numeric_time.pop("truth", None)
        bad_numeric_time.pop("score", None)
        bad_numeric_time["update_id"] = "2"
        bad_numeric_time["experiment_time_ns"] = 12345  # Must be decimal string, not JSON number.

        bad_live_with_truth = make_display_frame(
            run_id="run-live-1", mode="integrated", update_id=2, has_truth=True
        )
        bad_live_with_truth["has_truth"] = (
            False  # Illegal: truth/score present with has_truth=false.
        )

        bad_regressed = make_display_frame(
            run_id="run-viewer-1",
            mode="integrated",
            update_id=1,
            sequence_no=6,
            has_truth=True,
        )
        # Send update_id=0 (invalid positive uint64) and also test regression within same run:
        bad_regressed["update_id"] = "0"

        live_2 = make_display_frame(
            run_id="run-live-1",
            mode="live",
            update_id=3,
            sequence_no=3,
            experiment_time_ns=MAX_UINT64_TIME + 300,
            has_truth=False,
            include_truth=False,
            include_score=False,
            include_voxels=False,
        )
        bad_update_regression = make_display_frame(
            run_id="run-live-1",
            mode="live",
            update_id=2,
            sequence_no=4,
            has_truth=False,
            include_truth=False,
            include_score=False,
            include_voxels=False,
        )

        reports = self.receive(
            virtual_1,
            virtual_2,
            live_1,
            bad_numeric_time,
            bad_live_with_truth,
            bad_regressed,
            live_2,
            bad_update_regression,
        )

        r1, r2, r3 = reports[:3]
        self.assertTrue(r1["accepted"], r1)
        self.assertTrue(r1["has_truth"])
        self.assertTrue(r1["has_latest_truth"])
        self.assertEqual(sorted(r1["score_units"]), ["m", "px"])
        self.assertEqual(r1["display_time_ns"], MAX_UINT64_TIME)
        self.assertEqual(
            r1["selection"]["available_ray_ids"], [["station-west", "sess-w1", str(2**53 + 1)]]
        )
        self.assertEqual(r1["voxel_count"], 1)

        self.assertTrue(r2["accepted"], r2)
        self.assertEqual(r2["dropped_sequence_count"], 3)
        self.assertEqual(r2["track_history_len"], 2)
        self.assertEqual(r2["display_time_ns"], MAX_UINT64_TIME + 100)

        # Switching to live run clears previous truth/score/voxels and degrades gracefully.
        self.assertTrue(r3["accepted"], r3)
        self.assertEqual(r3["run_id"], "run-live-1")
        self.assertEqual(r3["mode"], "live")
        self.assertFalse(r3["has_truth"])
        self.assertFalse(r3["has_latest_truth"])
        self.assertEqual(r3["score_units"], [])
        self.assertEqual(r3["voxel_count"], 0)
        self.assertEqual(r3["display_time_ns"], MAX_UINT64_TIME + 200)

        for rejected in reports[3:6]:
            self.assertFalse(rejected["accepted"])
            self.assertTrue(rejected["error"])
            self.assertEqual(rejected["update_count"], r3["update_count"])
            self.assertEqual(rejected["display_time_ns"], r3["display_time_ns"])

        r_live2, r_regress = reports[6], reports[7]
        self.assertTrue(r_live2["accepted"])
        self.assertFalse(r_regress["accepted"])
        self.assertIn("regressed", r_regress["error"])
        self.assertEqual(r_regress["update_count"], r_live2["update_count"])


@unittest.skipUnless(GODOT, "Godot 4 executable is required")
class ViewerTcpWeakDependencyTests(unittest.TestCase):
    def test_late_join_mid_run_disconnect_reconnect_and_live_mode_over_be_tcp(self) -> None:
        server = _MockViewerPushServer()
        # Push frames before Viewer connects (late-join scenario; server never blocks).
        self.assertEqual(server.push(make_display_frame(update_id=1, sequence_no=1)), 0)

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "godot"
            shutil.copytree(PROJECT, project)
            (project / "test_viewer_worker.gd").write_text(TEST_VIEWER_WORKER)
            scene = project / "worker.tscn"
            scene.write_text(
                scene.read_text().replace("res://worker.gd", "res://test_viewer_worker.gd")
            )
            assert GODOT is not None
            proc = subprocess.Popen(
                [
                    GODOT,
                    "--headless",
                    "--path",
                    str(project),
                    "--audio-driver",
                    "Dummy",
                    "--",
                    "--viewer",
                    f"127.0.0.1:{server.port}",
                    "--reconnect-ms",
                    "60",
                ],
                cwd=directory,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            snapshots: list[dict[str, Any]] = []
            stop_reader = threading.Event()

            def read_stdout() -> None:
                assert proc.stdout is not None
                for line in proc.stdout:
                    if line.startswith("VIEWER_SNAP "):
                        snapshots.append(json.loads(line.removeprefix("VIEWER_SNAP ")))
                    if stop_reader.is_set():
                        break

            reader = threading.Thread(target=read_stdout, daemon=True)
            reader.start()
            try:
                server.wait_for_client(1, timeout=8.0)

                def wait_for_updates(target: int, timeout: float = 5.0) -> dict[str, Any]:
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        if snapshots and snapshots[-1]["update_count"] >= target:
                            return snapshots[-1]
                        time.sleep(0.02)
                    raise TimeoutError(
                        f"Timed out waiting for update_count >= {target}; got {snapshots}"
                    )

                # Virtual test mode frame over 4-byte BE TCP.
                self.assertEqual(server.push(make_display_frame(update_id=2, sequence_no=2)), 1)
                snap1 = wait_for_updates(1)
                self.assertEqual(snap1["mode"], "integrated")
                self.assertTrue(snap1["has_truth"])
                self.assertTrue(snap1["has_latest_truth"])
                self.assertEqual(sorted(snap1["score_by_unit"].keys()), ["m", "px"])
                self.assertIn("实时在线评分", snap1["diag_text"])
                self.assertIn("matched=1", snap1["diag_text"])
                self.assertIn("target-1", snap1["info_text"])
                self.assertIn("station-west", snap1["station_text"])
                self.assertIn("1 个非零网格", snap1["voxel_text"])
                self.assertFalse(snap1["truth_disabled"])
                self.assertEqual(snap1["truth_children"], 1)
                self.assertEqual(snap1["ray_children"], 1)
                self.assertEqual(snap1["meas_children"], 1)
                self.assertEqual(snap1["track_children"], 1)
                self.assertEqual(snap1["voxel_children"], 1)

                # Simulate mid-run disconnect; Viewer must stay alive and reconnect.
                server.drop_clients()
                server.wait_for_client(2, timeout=8.0)

                # Push live-mode frame (new run_id, no truth/score) after reconnect.
                live_frame = make_display_frame(
                    run_id="run-live-reconnect",
                    mode="live",
                    update_id=1,
                    sequence_no=3,
                    experiment_time_ns=MAX_UINT64_TIME + 500,
                    has_truth=False,
                    include_truth=False,
                    include_score=False,
                    include_voxels=False,
                )
                self.assertEqual(server.push(live_frame), 1)
                snap2 = wait_for_updates(2)
                self.assertGreaterEqual(snap2["reconnect_count"], 1)
                self.assertEqual(snap2["run_id"], "run-live-reconnect")
                self.assertEqual(snap2["mode"], "live")
                self.assertFalse(snap2["has_truth"])
                self.assertFalse(snap2["has_latest_truth"])
                self.assertEqual(snap2["score_by_unit"], {})
                self.assertIn("实机/外场监测模式", snap2["diag_text"])
                self.assertTrue(snap2["truth_disabled"])
                self.assertEqual(snap2["truth_children"], 0)
                self.assertEqual(snap2["ray_children"], 1)
                self.assertEqual(snap2["meas_children"], 1)
                self.assertEqual(snap2["track_children"], 1)
                self.assertEqual(snap2["voxel_children"], 0)
                # Passive viewer must never send bytes back over the push socket.
                self.assertEqual(bytes(server.received_from_viewer), b"")
                # Viewer must never persist display frames to disk (excluding Godot internal shader/logs if any;
                # check specifically no JSON/pb.zst/output artifacts were created in cwd or project).
                created_files = {
                    p.name
                    for p in Path(directory).rglob("*")
                    if p.is_file() and not p.is_relative_to(project)
                }
                self.assertEqual(created_files, set())
            finally:
                stop_reader.set()
                proc.terminate()
                try:
                    _, stderr = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    _, stderr = proc.communicate(timeout=5)
                server.close()
            self.assertNotIn("SCRIPT ERROR", stderr)

    def test_windowed_viewer_renders_without_blocking_burst_sender(self) -> None:
        server = _MockViewerPushServer()
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "godot"
            shutil.copytree(PROJECT, project)
            (project / "test_viewer_worker.gd").write_text(TEST_VIEWER_WORKER)
            scene = project / "worker.tscn"
            scene.write_text(
                scene.read_text().replace("res://worker.gd", "res://test_viewer_worker.gd")
            )
            assert GODOT is not None
            proc = subprocess.Popen(
                [
                    GODOT,
                    "--path",
                    str(project),
                    "--audio-driver",
                    "Dummy",
                    "--rendering-method",
                    "gl_compatibility",
                    "--",
                    "--viewer",
                    f"127.0.0.1:{server.port}",
                    "--reconnect-ms",
                    "60",
                ],
                cwd=directory,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            snapshots: list[dict[str, Any]] = []
            stop_reader = threading.Event()

            def read_stdout() -> None:
                assert proc.stdout is not None
                for line in proc.stdout:
                    if line.startswith("VIEWER_SNAP "):
                        snapshots.append(json.loads(line.removeprefix("VIEWER_SNAP ")))
                    if stop_reader.is_set():
                        break

            reader = threading.Thread(target=read_stdout, daemon=True)
            reader.start()
            try:
                server.wait_for_client(1, timeout=10.0)
                started = time.monotonic()
                for idx in range(1, 26):
                    server.push(
                        make_display_frame(
                            update_id=idx,
                            sequence_no=idx,
                            experiment_time_ns=MAX_UINT64_TIME + idx,
                        )
                    )
                self.assertLess(time.monotonic() - started, 1.0)
                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline:
                    if snapshots and snapshots[-1]["last_update_id"] == "25":
                        break
                    time.sleep(0.02)
                self.assertTrue(snapshots)
                self.assertEqual(snapshots[-1]["last_update_id"], "25")
                self.assertEqual(bytes(server.received_from_viewer), b"")
            finally:
                stop_reader.set()
                proc.terminate()
                try:
                    _, stderr = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    _, stderr = proc.communicate(timeout=5)
                server.close()
            self.assertNotIn("SCRIPT ERROR", stderr)
            self.assertNotIn("SHADER ERROR", stderr)


@unittest.skipUnless(
    GODOT and VSWITCH_BIN.is_file(),
    "Godot 4 and compiled target/debug/vswitch are required",
)
class VSwitchToGodotViewerTests(unittest.TestCase):
    def test_rust_vswitch_live_hub_pushes_to_real_godot_viewer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "godot"
            shutil.copytree(PROJECT, project)
            (project / "test_viewer_worker.gd").write_text(TEST_VIEWER_WORKER)
            scene = project / "worker.tscn"
            scene.write_text(
                scene.read_text().replace("res://worker.gd", "res://test_viewer_worker.gd")
            )
            vswitch_proc = subprocess.Popen(
                [
                    str(VSWITCH_BIN),
                    "live",
                    "--run-id",
                    "live-vswitch-godot",
                    "--bus-bind",
                    "127.0.0.1:0",
                    "--viewer-bind",
                    "127.0.0.1:0",
                ],
                cwd=directory,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            godot_proc: subprocess.Popen[str] | None = None
            stop_reader = threading.Event()
            try:
                assert vswitch_proc.stderr is not None
                bound: dict[str, Any] | None = None
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    line = vswitch_proc.stderr.readline()
                    if line.startswith("vswitch listening: "):
                        bound = json.loads(line.removeprefix("vswitch listening: "))
                        break
                assert bound is not None
                bus_host, bus_port_str = bound["bus_addr"].rsplit(":", 1)
                viewer_addr: str = bound["viewer_addr"]

                assert GODOT is not None
                godot_proc = subprocess.Popen(
                    [
                        GODOT,
                        "--headless",
                        "--path",
                        str(project),
                        "--audio-driver",
                        "Dummy",
                        "--",
                        "--viewer",
                        viewer_addr,
                        "--reconnect-ms",
                        "50",
                    ],
                    cwd=directory,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                snapshots: list[dict[str, Any]] = []

                def read_godot() -> None:
                    assert godot_proc is not None and godot_proc.stdout is not None
                    for line in godot_proc.stdout:
                        if line.startswith("VIEWER_SNAP "):
                            snapshots.append(json.loads(line.removeprefix("VIEWER_SNAP ")))
                        if stop_reader.is_set():
                            break

                reader = threading.Thread(target=read_godot, daemon=True)
                reader.start()

                # Wait until Godot Viewer actually finishes TCP connect to V-Switch ViewerHub.
                conn_deadline = time.monotonic() + 8.0
                while time.monotonic() < conn_deadline:
                    if snapshots and snapshots[-1]["connection_state"] == "connected":
                        break
                    time.sleep(0.02)
                self.assertTrue(snapshots)
                self.assertEqual(snapshots[-1]["connection_state"], "connected")
                with OnlineBusClient(bus_host, int(bus_port_str), timeout_s=5.0) as edge:
                    ack = edge.handshake(
                        role=common_pb2.COMPONENT_ROLE_EDGE,
                        instance_id="edge-live-test",
                        session_id="sess-e1",
                        station_ids=["station-west"],
                    )
                    self.assertTrue(ack.accepted)
                    edge.send_payload(
                        station_config=online_pb2.StationConfig(
                            station_id="station-west",
                            world_frame_id="enu",
                            main_camera_position_enu_m=common_pb2.Vec3(x=-250.0, y=0.0, z=12.0),
                            baseline_orientation_enu_from_main=common_pb2.Mat3(
                                values=[1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
                            ),
                        )
                    )
                    edge.send_payload(
                        ray_event=online_pb2.RayEvent(
                            envelope=common_pb2.EventEnvelope(
                                event_type=common_pb2.EVENT_TYPE_RAY,
                                station_id="station-west",
                                station_session_id="sess-e1",
                                sequence_no=1,
                            ),
                            frame_id="live-f-0",
                            capture_time=common_pb2.CaptureTime(
                                clock_domain_id="wall",
                                exposure_start_ns=MAX_UINT64_TIME,
                                exposure_end_ns=MAX_UINT64_TIME + 10,
                            ),
                            world_frame_id="enu",
                            calibration_usage=common_pb2.CALIBRATION_USAGE_VALID,
                            ray_origin_enu_m=common_pb2.Vec3(x=-250.0, y=0.0, z=12.0),
                            ray_direction_enu_unit=common_pb2.Vec3(x=0.0, y=1.0, z=0.0),
                            detection_score=0.9,
                        )
                    )
                    # Wait for Godot Viewer to receive the pushed RayEvent DisplayFrame.
                    wait_deadline = time.monotonic() + 5.0
                    while time.monotonic() < wait_deadline:
                        if snapshots and snapshots[-1]["ray_count"] >= 1:
                            break
                        time.sleep(0.05)

                self.assertTrue(snapshots)
                latest = snapshots[-1]
                self.assertEqual(latest["run_id"], "live-vswitch-godot")
                self.assertEqual(latest["mode"], "live")
                self.assertFalse(latest["has_truth"])
                self.assertEqual(latest["station_count"], 1)
                self.assertEqual(latest["ray_count"], 1)
                self.assertEqual(latest["score_by_unit"], {})
            finally:
                stop_reader.set()
                if godot_proc is not None:
                    godot_proc.terminate()
                    try:
                        _, godot_stderr = godot_proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        godot_proc.kill()
                        _, godot_stderr = godot_proc.communicate(timeout=5)
                    self.assertNotIn("SCRIPT ERROR", godot_stderr)
                vswitch_proc.terminate()
                try:
                    vswitch_proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    vswitch_proc.kill()
                    vswitch_proc.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()

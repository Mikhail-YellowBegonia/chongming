"""Fixed visualization increments over the existing UI socket; no run-mode wiring."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from chongming.contracts import model as m
from chongming.contracts import visualization as v
from chongming.preview import GodotUiSession, PreviewRuntime
from chongming.rendering.godot import (
    GodotRenderer,
    UiImage,
    encode_ui_request,
    visualization_ui_state,
)
from chongming.scenarios.demo import demo_spec
from tests.test_godot_ui import _FakeGodot, _frame, _split, _step

PROJECT = Path(__file__).resolve().parents[1] / "src/chongming/rendering/godot"
GODOT = os.environ.get("CHONGMING_GODOT") or shutil.which("godot4") or shutil.which("godot")
# Include values beyond IEEE-754's exact integer range, including low-order ns bits.
TIME = 2**53 + 101


def fixed_update(update_id: int = 1) -> v.AlgorithmOutputUpdate:
    station = m.StationId("station-01")
    session = m.SessionId("session-a")
    event_id = (station, session, 2**53 + 1)
    ray = v.RayVisualization(
        event_id=event_id,
        capture_time_ns=TIME,
        origin_enu_m=(1.0, 2.0, 3.0),
        direction_enu_unit=(0.0, 1.0, 0.0),
        calibration_usage=m.CalibrationUsage.FROZEN,
        detection_score=0.7,
        angular_std_rad=0.002,
    )
    measurement = v.MeasurementVisualization(
        measurement_id=m.MeasurementId(f"measurement-{update_id}"),
        reference_time_ns=TIME + 10,
        status=m.FusionStatus.LOCATED,
        position_enu_m=(4.0, 5.0, 6.0),
        position_std_enu_m=None,
        supporting_ray_ids=(event_id,),
        supporting_station_ids=(station,),
        minimum_intersection_angle_deg=30.0,
        geometry_condition=2.0,
    )
    track = v.TrackVisualization(
        track_id=m.TrackId("track-a"),
        event_time_ns=TIME + 10,
        lifecycle=m.TrackLifecycle.TENTATIVE,
        position_enu_m=(4.0, 5.0, 6.0),
        velocity_enu_mps=None,
        position_std_enu_m=None,
        velocity_std_enu_mps=None,
        recent_position_enu_m=(4.0, 5.0, 6.0),
        recent_time_ns=TIME + 10,
    )
    return v.AlgorithmOutputUpdate(
        update_id=update_id,
        scenario_time_ns=TIME + 20,
        step_no=3,
        edge_events=(
            v.EdgeStationEvent(
                station_id=station,
                station_session_id=session,
                rays=(ray,),
                diagnostics=(
                    v.DiagnosticSummary(
                        capture_time_ns=TIME,
                        category="background_warmup",
                        reason_codes=("warming_up",),
                    ),
                ),
            ),
        ),
        center_events=v.CenterFusionEvent(
            measurements=(measurement,),
            tracks=(
                track,
                replace(
                    track,
                    event_time_ns=TIME + 20,
                    recent_time_ns=TIME + 20,
                    lifecycle=m.TrackLifecycle.CONFIRMED,
                ),
            ),
            window_count=2,
            candidate_count=3,
            association_count=1,
            voxel_space=None,
        ),
        station_states=(
            v.StationVisualizationState(
                station_id=station,
                configured_orientation_enu_from_main=(
                    (1.0, 0.0, 0.0),
                    (0.0, 1.0, 0.0),
                    (0.0, 0.0, 1.0),
                ),
                actual_orientation_enu_from_main=None,
                calibrated_orientation_enu_from_main=None,
                calibration_usage=None,
                truth_orientation_error_rad=None,
                calibration_orientation_error_rad=None,
            ),
        ),
    )


def fixed_truth() -> tuple[v.TruthComparisonSnapshot, ...]:
    target = v.TruthTargetState(
        target_id="truth-a",
        time_ns=TIME,
        position_enu_m=(4.0, 5.0, 6.0),
        velocity_enu_mps=(1.0, 0.0, 0.0),
        geometry_type="sphere",
        bounding_radius_m=0.5,
        visibility_by_station=(
            v.TargetStationVisibility(
                station_id=m.StationId("station-01"), in_field_of_view=True, capture_eligible=False
            ),
        ),
    )
    return (
        v.TruthComparisonSnapshot(targets=(target,)),
        v.TruthComparisonSnapshot(targets=()),
        v.TruthComparisonSnapshot(targets=(replace(target, time_ns=TIME + 20),)),
    )


def state(update_id: int = 1) -> dict[str, Any]:
    return {"version": update_id, **visualization_ui_state(fixed_update(update_id), fixed_truth())}


# This test-only worker reports its display copies through ordinary queued commands.
# Production replies retain only accepted/version/images and user commands.
TEST_WORKER = """extends "res://worker.gd"
func _ui_exchange(request: Dictionary, trailer: PackedByteArray) -> PackedByteArray:
    var reply := super._ui_exchange(request, trailer)
    if reply.is_empty():
        return reply
    var document: Dictionary = JSON.parse_string(reply.get_string_from_utf8())
    document.commands.append({"type": "test_cache", "update_count": _visualization.update_count,
        "last_update": _visualization.last_update, "truth_times": _visualization.truth_by_time.keys(),
        "truth_snapshots": _visualization.truth_by_time.values(), "ray_count": _visualization.rays.size(),
        "measurement_count": _visualization.measurements.size(), "track_history": _visualization.track_history})
    return JSON.stringify(document).to_utf8_buffer()
"""

RECEIVER_SCRIPT = """extends SceneTree
const Visualization = preload("res://visualization.gd")
func _initialize() -> void:
    var receiver := Visualization.new()
    var states: Array = JSON.parse_string(FileAccess.get_file_as_string(OS.get_cmdline_user_args()[0]))
    var reports := []
    for state in states:
        var accepted := receiver.receive(state)
        reports.append({"accepted": accepted, "error": receiver.error, "update_count": receiver.update_count,
            "last_update": receiver.last_update.duplicate(true), "rays": receiver.rays.values().duplicate(true),
            "measurements": receiver.measurements.values().duplicate(true), "tracks": receiver.tracks.values().duplicate(true),
            "track_history": receiver.track_history.duplicate(true), "station_states": receiver.station_states.duplicate(true),
            "truth_times": receiver.truth_by_time.keys(), "truth_snapshots": receiver.truth_by_time.values().duplicate(true),
            "evaluation": receiver.evaluation.summary(), "matches": receiver.evaluation.last_matches.duplicate(true)})
    print("REPORT " + JSON.stringify(reports))
    quit()
"""


class VisualizationUiTests(unittest.TestCase):
    def test_framing_keeps_full_contracts_and_exact_integer_strings(self) -> None:
        output = fixed_update()
        snapshots = fixed_truth()
        before = asdict(output)
        request = _split(encode_ui_request(state(), ()))
        wire = request["state"]
        assert isinstance(wire, dict)
        self.assertEqual(wire["algorithm_output"]["scenario_time_ns"], str(TIME + 20))
        self.assertEqual(
            wire["algorithm_output"]["edge_events"][0]["rays"][0]["event_id"][2], str(2**53 + 1)
        )
        self.assertEqual(wire["truth_snapshots"][1], {"targets": []})
        self.assertEqual(asdict(output), before)
        self.assertEqual(snapshots, fixed_truth())
        for update_id in (-(2**63) - 1, 2**63):
            with self.subTest(update_id=update_id), self.assertRaisesRegex(ValueError, "64 bits"):
                visualization_ui_state(replace(output, update_id=update_id), snapshots)

    def test_each_increment_is_sent_even_when_no_new_image_is_available(self) -> None:
        runtime = PreviewRuntime(demo_spec(fps=2), video_enabled=False)
        godot = _FakeGodot()
        session = GodotUiSession(runtime, godot)
        view = (
            runtime.ui_view()[0],
            {"sequence": 1},
            _step(_frame("station-01", m.CameraRole.MAIN, 2, 2)),
        )
        with patch.object(runtime, "ui_view", return_value=view):
            session.tick(algorithm_output=fixed_update(1), truth_snapshots=fixed_truth())
            session.tick(algorithm_output=fixed_update(2), truth_snapshots=fixed_truth())
            session.tick()
        self.assertEqual([sent["version"] for sent, _ in godot.sent], [1, 2, 3])
        self.assertEqual([len(images) for _, images in godot.sent], [1, 0, 0])
        self.assertEqual(
            [sent["algorithm_output"]["update_id"] for sent, _ in godot.sent[:2]], ["1", "2"]
        )
        self.assertNotIn("algorithm_output", godot.sent[2][0])
        self.assertNotIn("truth_snapshots", godot.sent[2][0])

    def test_increment_and_truth_must_be_paired_and_transport_failure_propagates(self) -> None:
        runtime = PreviewRuntime(demo_spec(fps=2), video_enabled=False)
        godot = _FakeGodot()
        session = GodotUiSession(runtime, godot)
        with self.assertRaisesRegex(ValueError, "together"):
            session.tick(algorithm_output=fixed_update())
        with self.assertRaisesRegex(ValueError, "together"):
            session.tick(truth_snapshots=fixed_truth())
        self.assertEqual(godot.sent, [])

        def disconnected(
            state: dict[str, Any], images: tuple[UiImage, ...]
        ) -> list[dict[str, Any]]:
            raise ConnectionError("disconnected")

        failed = GodotUiSession(runtime, disconnected)
        with self.assertRaisesRegex(ConnectionError, "disconnected"):
            failed.tick(algorithm_output=fixed_update(), truth_snapshots=fixed_truth())


@unittest.skipUnless(GODOT, "Godot 4 executable is required")
class VisualizationReceiverTests(unittest.TestCase):
    def receive(self, *states: dict[str, Any]) -> list[dict[str, Any]]:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            shutil.copy(PROJECT / "visualization.gd", project)
            shutil.copy(PROJECT / "evaluation.gd", project)
            (project / "project.godot").write_text("config_version=5\n")
            (project / "receiver.gd").write_text(RECEIVER_SCRIPT)
            source = project / "states.json"
            source.write_text(json.dumps(states, allow_nan=False))
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

    def test_receiver_preserves_contract_values_and_empty_truth_time_slot(self) -> None:
        report = self.receive(state())[0]
        self.assertTrue(report["accepted"], report)
        self.assertEqual(report["last_update"], json.loads(json.dumps(asdict(fixed_update()))))
        self.assertEqual(report["truth_times"], [TIME, TIME + 10, TIME + 20])
        self.assertEqual(
            report["truth_snapshots"],
            json.loads(json.dumps([asdict(item) for item in fixed_truth()])),
        )
        self.assertEqual(len(report["rays"]), 1)
        self.assertEqual(len(report["measurements"]), 1)
        self.assertEqual(report["tracks"][0]["lifecycle"], "confirmed")
        self.assertEqual(len(report["track_history"]["track-a"]), 2)
        self.assertIsNone(report["measurements"][0]["position_std_enu_m"])
        # An empty increment still has one truth slot, even at signed-64-bit limits.
        for time_ns in (-(2**63), 2**63 - 1):
            empty = replace(
                fixed_update(),
                scenario_time_ns=time_ns,
                edge_events=None,
                center_events=None,
                station_states=None,
            )
            payload = visualization_ui_state(empty, (v.TruthComparisonSnapshot(targets=()),))
            result = self.receive({"version": 1, **payload})[0]
            self.assertTrue(result["accepted"], result)
            self.assertEqual(result["truth_times"], [time_ns])
            self.assertEqual(result["rays"], [])
        # The optional voxel payload uses the same exact-integer framing.
        output = fixed_update()
        assert output.center_events is not None
        voxels = v.VoxelSpaceVisualization(
            search_box_min_enu_m=(-1.0, -1.0, -1.0),
            search_box_max_enu_m=(2.0, 2.0, 2.0),
            voxel_step_enu_m=(1.0, 1.0, 1.0),
            max_score=2,
            scored_voxels=(
                v.ScoredVoxel(grid_index=(1, 0, 1), score=2, supporting_station_count=2),
            ),
        )
        rich = replace(output, center_events=replace(output.center_events, voxel_space=voxels))
        payload = visualization_ui_state(rich, fixed_truth())
        result = self.receive({"version": 1, **payload})[0]
        self.assertTrue(result["accepted"], result)
        self.assertEqual(
            result["last_update"]["center_events"]["voxel_space"],
            json.loads(json.dumps(asdict(voxels))),
        )

    def test_duplicate_and_heartbeat_do_not_append_and_new_worker_starts_empty(self) -> None:
        reports = self.receive(state(), state(), {"version": 3}, state(2))
        self.assertTrue(all(report["accepted"] for report in reports))
        self.assertEqual([report["update_count"] for report in reports], [1, 1, 1, 2])
        self.assertEqual(
            [len(report["track_history"]["track-a"]) for report in reports], [2, 2, 2, 2]
        )
        self.assertEqual([len(report["measurements"]) for report in reports], [1, 1, 1, 2])
        fresh = self.receive({"version": 1})[0]
        self.assertEqual(fresh["update_count"], 0)
        self.assertEqual(fresh["rays"], [])
        self.assertEqual(fresh["track_history"], {})
        self.assertEqual(fresh["truth_snapshots"], [])

    def test_retention_uses_exact_experiment_time_and_deleted_tracks_are_removed(self) -> None:
        initial = fixed_update()
        assert initial.center_events is not None
        initial = replace(
            initial,
            center_events=replace(
                initial.center_events,
                tracks=(
                    replace(initial.center_events.tracks[-1], lifecycle=m.TrackLifecycle.DELETED),
                ),
            ),
        )
        states = [{"version": 1, **visualization_ui_state(initial, fixed_truth())}]
        times = (
            TIME + 1_000_000_000,
            TIME + 1_000_000_001,
            TIME + 10 + 3_000_000_000,
            TIME + 11 + 3_000_000_000,
            TIME + 20 + 3_000_000_000,
            TIME + 21 + 3_000_000_000,
        )
        for index, time_ns in enumerate(times, 2):
            empty = replace(
                initial,
                update_id=index,
                scenario_time_ns=time_ns,
                edge_events=None,
                center_events=None,
                station_states=None,
            )
            states.append(
                {
                    "version": index,
                    **visualization_ui_state(empty, (v.TruthComparisonSnapshot(targets=()),)),
                }
            )
        # A later-arriving old increment must not revive already expired display data.
        states.append(
            {"version": 8, **visualization_ui_state(replace(initial, update_id=8), fixed_truth())}
        )
        reports = self.receive(*states)
        self.assertTrue(all(item["accepted"] for item in reports))
        self.assertEqual([len(item["rays"]) for item in reports], [1, 1, 0, 0, 0, 0, 0, 0])
        self.assertEqual([len(item["measurements"]) for item in reports], [1, 1, 1, 1, 0, 0, 0, 0])
        self.assertEqual([len(item["tracks"]) for item in reports], [1, 1, 1, 1, 1, 1, 0, 0])
        self.assertEqual(reports[-1]["track_history"], {})
        # Display eviction must not truncate the complete accepted increment.
        self.assertEqual(len(reports[-1]["last_update"]["edge_events"][0]["rays"]), 1)
        self.assertEqual(len(reports[-1]["last_update"]["center_events"]["measurements"]), 1)

    def test_retention_at_signed_64_bit_time_limits_does_not_overflow_or_round(self) -> None:
        initial = fixed_update()
        assert initial.edge_events is not None
        for start in (-(2**63), 2**63 - 1 - 1_000_000_001):
            with self.subTest(start=start):
                edge = initial.edge_events[0]
                output = replace(
                    initial,
                    scenario_time_ns=start,
                    center_events=None,
                    station_states=None,
                    edge_events=(
                        replace(
                            edge,
                            rays=(replace(edge.rays[0], capture_time_ns=start),),
                            diagnostics=(),
                        ),
                    ),
                )
                states = []
                for index, elapsed in enumerate((0, 1_000_000_000, 1_000_000_001), 1):
                    update = replace(
                        output,
                        update_id=index,
                        scenario_time_ns=start + elapsed,
                        edge_events=output.edge_events if index == 1 else None,
                    )
                    states.append(
                        {
                            "version": index,
                            **visualization_ui_state(
                                update, (v.TruthComparisonSnapshot(targets=()),)
                            ),
                        }
                    )
                reports = self.receive(*states)
                self.assertTrue(all(item["accepted"] for item in reports))
                self.assertEqual([len(item["rays"]) for item in reports], [1, 1, 0])

    def test_track_history_is_time_ordered_replaces_same_time_and_keeps_latest_100(self) -> None:
        initial = fixed_update()
        assert initial.center_events is not None
        prototype = initial.center_events.tracks[-1]
        points = tuple(
            replace(
                prototype,
                event_time_ns=TIME + index,
                recent_time_ns=TIME + index,
                position_enu_m=(float(index), 0.0, 0.0),
                recent_position_enu_m=(float(index), 0.0, 0.0),
            )
            for index in reversed(range(120))
        )
        corrected = replace(
            points[0], position_enu_m=(999.0, 0.0, 0.0), recent_position_enu_m=(999.0, 0.0, 0.0)
        )
        output = replace(
            initial,
            scenario_time_ns=TIME + 200,
            edge_events=None,
            center_events=replace(
                initial.center_events, measurements=(), tracks=(*points, corrected, points[-1])
            ),
        )
        payload = {
            "version": 1,
            **visualization_ui_state(output, (v.TruthComparisonSnapshot(targets=()),)),
        }
        first, duplicate, heartbeat = self.receive(payload, payload, {"version": 2})
        self.assertTrue(first["accepted"], first)
        history = first["track_history"]["track-a"]
        self.assertEqual([item["time_ns"] for item in history], list(range(TIME + 20, TIME + 120)))
        self.assertEqual(history[-1]["position_enu_m"], [999.0, 0.0, 0.0])
        self.assertEqual(first["tracks"][0]["position_enu_m"], [999.0, 0.0, 0.0])
        self.assertEqual(first, duplicate)
        self.assertEqual(first, heartbeat)

    def test_invalid_updates_leave_all_accepted_caches_unchanged(self) -> None:
        valid = state(2)
        cases: dict[str, dict[str, Any]] = {}

        def invalid(name: str) -> dict[str, Any]:
            cases[name] = copy.deepcopy(valid)
            return cases[name]

        invalid("missing field")["algorithm_output"].pop("step_no")
        invalid("missing truth").pop("truth_snapshots")
        invalid("orphan truth").pop("algorithm_output")
        invalid("wrong count")["truth_snapshots"].pop()
        invalid("wrong timestamp")["truth_snapshots"][0]["targets"][0]["time_ns"] = str(TIME + 1)
        invalid("fractional timestamp")["algorithm_output"]["scenario_time_ns"] = "1.5"
        invalid("rounded numeric timestamp")["algorithm_output"]["scenario_time_ns"] = TIME + 20
        invalid("overflow")["algorithm_output"]["update_id"] = str(2**63)
        invalid("negative counter")["algorithm_output"]["step_no"] = "-1"
        invalid("boolean counter")["algorithm_output"]["step_no"] = True
        invalid("bad enum")["algorithm_output"]["edge_events"][0]["rays"][0][
            "calibration_usage"
        ] = "unknown"
        invalid("bad vector")["algorithm_output"]["edge_events"][0]["rays"][0]["origin_enu_m"] = [
            1,
            2,
        ]
        invalid("zero direction")["algorithm_output"]["edge_events"][0]["rays"][0][
            "direction_enu_unit"
        ] = [0, 0, 0]
        invalid("bad score")["algorithm_output"]["edge_events"][0]["rays"][0]["detection_score"] = (
            1.1
        )
        invalid("wrong session")["algorithm_output"]["edge_events"][0]["rays"][0]["event_id"][1] = (
            "other"
        )
        duplicate = invalid("duplicate ray")["algorithm_output"]["edge_events"][0]["rays"]
        duplicate.append(copy.deepcopy(duplicate[0]))
        invalid("bad measurement enum")["algorithm_output"]["center_events"]["measurements"][0][
            "status"
        ] = "success"
        invalid("failed position")["algorithm_output"]["center_events"]["measurements"][0][
            "status"
        ] = "rejected_input"
        invalid("negative std")["algorithm_output"]["center_events"]["measurements"][0][
            "position_std_enu_m"
        ] = [-1, 0, 0]
        invalid("bad lifecycle")["algorithm_output"]["center_events"]["tracks"][0]["lifecycle"] = (
            "running"
        )
        invalid("unpaired history")["algorithm_output"]["center_events"]["tracks"][0][
            "recent_time_ns"
        ] = None
        invalid("bad rotation")["algorithm_output"]["station_states"][0][
            "configured_orientation_enu_from_main"
        ] = []
        invalid("bad boolean")["truth_snapshots"][0]["targets"][0]["visibility_by_station"][0][
            "capture_eligible"
        ] = "false"
        conflict = state()
        conflict["algorithm_output"]["step_no"] = "99"
        cases["conflicting duplicate"] = conflict
        cases["regressed update"] = state(0)
        reports = self.receive(state(), *cases.values())
        baseline = reports[0]
        for name, report in zip(cases, reports[1:], strict=True):
            with self.subTest(name=name):
                self.assertFalse(report.pop("accepted"), report)
                self.assertTrue(report.pop("error"))
                self.assertEqual(
                    report,
                    {
                        key: value
                        for key, value in baseline.items()
                        if key not in {"accepted", "error"}
                    },
                )


@unittest.skipUnless(GODOT, "Godot 4 executable is required")
class VisualizationSocketTests(unittest.TestCase):
    def test_real_ui_roundtrip_duplicate_heartbeat_and_increment_without_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            project = package / "godot"
            shutil.copytree(PROJECT, project)
            (project / "test_worker.gd").write_text(TEST_WORKER)
            scene = project / "worker.tscn"
            scene.write_text(scene.read_text().replace("res://worker.gd", "res://test_worker.gd"))
            with patch("chongming.rendering.godot.files", return_value=package):
                renderer = GodotRenderer()
            try:
                first = renderer.ui_exchange(
                    state(), (UiImage("station-01/main", 1, 1, bytes((1, 2, 3))),)
                )[0]
                duplicate = renderer.ui_exchange({**state(), "version": 2})[0]
                heartbeat = renderer.ui_exchange({"version": 3})[0]
                second = renderer.ui_exchange({**state(2), "version": 4})[0]
                self.assertEqual(
                    first["last_update"], json.loads(json.dumps(asdict(fixed_update())))
                )
                self.assertEqual(first["truth_times"], [TIME, TIME + 10, TIME + 20])
                self.assertEqual(first["truth_snapshots"][1], {"targets": []})
                self.assertEqual(
                    [row["update_count"] for row in (first, duplicate, heartbeat, second)],
                    [1, 1, 1, 2],
                )
                self.assertEqual(second["measurement_count"], 2)
                self.assertEqual(second["ray_count"], 1)
                self.assertEqual(len(second["track_history"]["track-a"]), 2)
                self.assertNotIn("SCRIPT ERROR", renderer._diagnostics())
            finally:
                renderer.close()

    def test_malformed_update_closes_connection_without_success_acknowledgement(self) -> None:
        renderer = GodotRenderer()
        try:
            self.assertEqual(renderer.ui_exchange(state()), [])
            bad = state(2)
            bad["truth_snapshots"].pop()
            with self.assertRaises((RuntimeError, OSError)):
                renderer.ui_exchange(bad)
            assert renderer._process is not None
            renderer._process.wait(timeout=5)
            self.assertIn("truth snapshot count", renderer._diagnostics())
        finally:
            renderer.close()


if __name__ == "__main__":
    unittest.main()

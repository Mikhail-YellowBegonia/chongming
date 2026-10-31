"""Cross-module acceptance with real images, independent exposures and one worker per run.

Device parameters come from the explicit test-only edge fixture, not deployment
configuration. A compiled fixture controls independent camera schedules. Native
rendering is required; detector, calibration, center results and truth sampling
are never patched.
"""

from __future__ import annotations

import json
import socket
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from chongming.contracts import model as m
from chongming.development import CenterSession
from chongming.preview import GodotUiSession, PreviewRuntime
from chongming.reference import create_algorithm
from chongming.rendering.godot import GodotRenderer, UiImage, default_renderer
from chongming.scenarios.authoring import save_authoring
from chongming.scenarios.serialization import load_scenario
from tests import test_center_ui as ui
from tests import test_godot_visualization as transport
from tests.test_edge_runtime import policies, real_scene, rows
from tests.test_scene_run import normalize

WORKER = ui.RUN_WORKER.replace(
    "    var document: Dictionary = JSON.parse_string(reply.get_string_from_utf8())",
    """    for key in _visualization.layers:
        if request.state.has("test_all_layers"):
            _ui._visualization_layers[key].button_pressed = request.state.test_all_layers
    if request.state.get("test_select_first", false) and _ui._measurement_select.item_count > 1:
        _ui._measurement_select.select(1)
        _ui._measurement_select.item_selected.emit(1)
    var nodes := {}
    var visible := {}
    for key in _visualization.layers:
        nodes[key] = []
        visible[key] = _visualization.layers[key].visible
        for node in _visualization.layers[key].get_children():
            nodes[key].append(node.get_instance_id())
    var document: Dictionary = JSON.parse_string(reply.get_string_from_utf8())""",
).replace(
    '"last_update": _visualization.last_update,',
    """"last_update": _visualization.last_update,
        "last_matches": _visualization.evaluation.last_matches,
        "selection": _visualization.selection_summary(),
        "voxel_space": _visualization.voxel_space,
        "ray_count": _visualization.rays.size(), "nodes": nodes, "visible": visible,""",
)


@unittest.skipUnless(transport.GODOT, "Real Godot graphics are required")
class VisualizationIntegrationTests(unittest.TestCase):
    def asynchronous_session(self, path: Path, output: Path) -> CenterSession:
        spec = load_scenario(path)
        # Independent auxiliary exposures deliberately follow the first main
        # exposure. No future auxiliary frame may be awaited or backfilled.
        spec = replace(
            spec,
            scene_definition=replace(
                spec.scene_definition,
                cameras=tuple(
                    replace(
                        camera,
                        exposure=replace(
                            camera.exposure,
                            first_exposure_start_ns=camera.exposure.first_exposure_start_ns
                            + 50_000_000,
                        ),
                    )
                    if camera.camera_role is m.CameraRole.AUXILIARY
                    else camera
                    for camera in spec.scene_definition.cameras
                ),
            ),
        )
        # Inject only compiled configuration, before assembly and evidence begin.
        # The saved runtime graph must reflect the actual exposure schedules.
        with patch("chongming.development.load_scenario", return_value=spec):
            return CenterSession(
                path, output, mode=4, device_policies=policies(spec.station_configs)
            )

    def replay_updates(
        self,
        session: CenterSession,
        renderer: GodotRenderer,
        reports: list[dict[str, Any]],
        output: Path,
        enabled: bool,
    ) -> list[int]:
        exchange = renderer.ui_exchange
        acknowledged: list[int] = []

        def observe(
            state: dict[str, Any], images: tuple[UiImage, ...] = ()
        ) -> list[dict[str, Any]]:
            state = {**state, "test_all_layers": enabled, "test_select_first": enabled}
            commands = exchange(state, images)
            if "algorithm_output" not in state:
                return commands
            index = int(state["algorithm_output"]["update_id"])
            self.assertEqual(len(rows(output / "center-output.jsonl")), index)
            before = reports[-1]
            clock = session.source.clock.read()
            time.sleep(0.005)
            self.assertEqual(session.source.clock.read(), clock)
            # Replay the complete accepted increment, then a heartbeat.
            # Neither may evaluate again, append history or replace meshes.
            exchange(state, images)
            for field in ("update_count", "evaluation", "nodes", "selection", "voxel_space"):
                self.assertEqual(reports[-1][field], before[field], field)
            heartbeat = {
                key: value
                for key, value in state.items()
                if key not in ("algorithm_output", "truth_snapshots")
            }
            exchange(heartbeat)
            self.assertEqual(reports[-1]["evaluation"], before["evaluation"])
            self.assertEqual(reports[-1]["nodes"], before["nodes"])
            self.assertEqual(session.source.clock.read(), clock)
            acknowledged.append(index)
            return commands

        with (
            patch.object(renderer, "ui_exchange", side_effect=observe),
            patch.object(
                session.source.virtual_generator,
                "generate",
                side_effect=AssertionError("No virtual fallback"),
            ),
        ):
            session.run_ui(tick_hz=240)
        return acknowledged

    def test_async_real_pixels_and_center_inputs_are_invariant_under_ui_replays_and_layers(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(ui, "RUN_WORKER", WORKER),
            ui.observed_workers() as workers,
        ):
            root = Path(directory)
            path = root / "scene.json"
            real_scene(path)
            consumed, hashes, geometry = [], [], []
            for enabled in (False, True):
                output = root / f"layers-{enabled}"
                session = self.asynchronous_session(path, output)
                renderer, reports, _ = workers[-1]
                acknowledged = self.replay_updates(session, renderer, reports, output, enabled)
                self.assertTrue(session.closed)
                self.assertIsNone(renderer._socket)
                self.assertEqual(acknowledged, list(range(1, session.step_count + 2)))
                self.assertGreater(session.ray_count, 0)
                self.assertGreater(reports[-1]["evaluation"]["center"]["matched"], 0)
                self.assertTrue(all(value == enabled for value in reports[-1]["visible"].values()))
                if enabled:
                    self.assertTrue(reports[-1]["selection"]["available_ray_ids"])
                    self.assertFalse(reports[-1]["selection"]["missing_ray_ids"])
                self.assertNotIn("SCRIPT ERROR", reports[-1]["worker_log"])
                journal = rows(output / "edge-output.jsonl")
                sessions = [
                    str(binding.image_session_id) for binding in session.source.station_sessions
                ]
                consumed.append(normalize(rows(output / "center-input.jsonl"), sessions))
                hashes.append(
                    [
                        (
                            row["value"]["station_id"],
                            row["value"]["camera_role"],
                            row["value"]["pixels_sha256"],
                        )
                        for row in journal
                        if row["kind"] == "input_frame"
                    ]
                )
                geometry.append(
                    [
                        [
                            {
                                key: measurement[key]
                                for key in (
                                    "status",
                                    "reference_time_ns",
                                    "position_enu_m",
                                    "conditional_position_covariance_enu_m2",
                                )
                            }
                            for measurement in row["measurements"]
                        ]
                        for row in rows(output / "center-output.jsonl")
                    ]
                )
                for station in session.spec.station_configs:
                    station_frames = [
                        row["value"]
                        for row in journal
                        if row["kind"] == "input_frame"
                        and row["value"]["station_id"] == station.station_id
                    ]
                    self.assertEqual(station_frames[0]["camera_role"], "main")
                    first_main = next(
                        row["value"]
                        for row in journal
                        if row["kind"] == "main_result"
                        and row["value"]["detections"]["main_frame_ref"]["station_id"]
                        == station.station_id
                    )
                    self.assertFalse(
                        any(
                            event["envelope"]["event_type"] == "ray"
                            for event in first_main["events"]
                        )
                    )
                for row in journal:
                    if row["kind"] != "main_result":
                        continue
                    for event in row["value"]["events"]:
                        if event["envelope"]["event_type"] != "ray":
                            continue
                        capture = event["capture_time"]
                        midpoint = (capture["exposure_start_ns"] + capture["exposure_end_ns"]) // 2
                        evidence = event["calibration_evidence"]
                        reference = evidence["solution_reference_capture_time"]
                        self.assertLessEqual(
                            (reference["exposure_start_ns"] + reference["exposure_end_ns"]) // 2,
                            midpoint,
                        )
                        self.assertLessEqual(evidence["last_trusted_time"]["time_ns"], midpoint)
                        self.assertLessEqual(
                            evidence["solution_accepted_time"]["time_ns"], midpoint
                        )
                historical = False
                for report in reports:
                    update = report["last_update"]
                    if not update:
                        continue
                    by_time = {
                        target["time_ns"]: target
                        for snapshot in report["truths"]
                        for target in snapshot["targets"]
                    }
                    for match in report["last_matches"]["center"]:
                        reference = match["time_ns"]
                        historical |= reference != update["scenario_time_ns"]
                        expected = [-65 + 35 * (reference / 1e9), 62.0, 20.0]
                        for actual, wanted in zip(
                            by_time[reference]["position_enu_m"], expected, strict=True
                        ):
                            self.assertAlmostEqual(actual, wanted, places=12)
                self.assertTrue(historical)
                text = (output / "center-input.jsonl").read_text()
                for forbidden in ('"truth_object_id"', '"truth_snapshots"', '"target_id"'):
                    self.assertNotIn(forbidden, text)
            self.assertEqual(consumed[0], consumed[1])
            self.assertEqual(
                hashes[0],
                hashes[1],
                "All native sensor pixels must be independent of debug drawing",
            )
            self.assertEqual(geometry[0], geometry[1])

    def test_offset_stations_without_a_motion_prior_keep_the_alignment_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ui.observed_workers() as workers:
            root = Path(directory)
            path = root / "scene.json"
            real_scene(path)
            document = json.loads(path.read_text())
            document["stations"][1]["start_s"] = 0.025
            save_authoring(document, path)
            session = self.asynchronous_session(path, root / "run")
            session.run_ui(tick_hz=240)
            self.assertGreater(session.ray_count, 0)
            raw = (root / "run/center-output.jsonl").read_text()
            self.assertIn("motion_alignment_unavailable", raw)
            self.assertTrue(
                all(not row["measurements"] for row in rows(root / "run/center-output.jsonl"))
            )
            stats = workers[-1][1][-1]["evaluation"]["center"]
            self.assertEqual(stats["outputs"], 0)
            self.assertIsNone(stats["mean_error"])
            self.assertTrue(session.closed)

    def test_switching_from_image_results_to_empty_modes_and_preview_clears_every_cache(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(ui, "RUN_WORKER", WORKER),
            ui.observed_workers() as workers,
        ):
            root = Path(directory)
            path = root / "scene.json"
            spec = real_scene(path)
            first = CenterSession(
                path, root / "image", mode=4, device_policies=policies(spec.station_configs)
            )
            renderer = workers[-1][0]
            exchange = renderer.ui_exchange
            with patch.object(
                renderer,
                "ui_exchange",
                side_effect=lambda state, images=(): exchange(
                    {**state, "test_select_first": True}, images
                ),
            ):
                first.run_ui(tick_hz=240)
            self.assertTrue(workers[-1][1][-1]["selection"]["available_ray_ids"])
            document = json.loads(path.read_text())
            document.update(targets=[], duration_s=0.21)
            save_authoring(document, path)
            for mode in (3, 2):
                session = CenterSession(
                    path,
                    root / f"empty-{mode}",
                    mode=mode,
                    algorithm=create_algorithm() if mode == 3 else None,
                    device_policies=policies(spec.station_configs) if mode == 2 else None,
                )
                session.run_ui(tick_hz=240)
                _, reports, _ = workers[-1]
                self.assertEqual(reports[0]["update_count"], 0)
                self.assertIsNone(reports[0]["voxel_space"])
                self.assertEqual(reports[0]["selection"]["measurement_id"], "")
                self.assertEqual(reports[-1]["ray_count"], 0)
                self.assertEqual(reports[-1]["track_count"], 0)
                for name in ("edge", "center"):
                    self.assertEqual(reports[-1]["evaluation"][name]["outputs"], 0)
                    self.assertIsNone(reports[-1]["evaluation"][name]["mean_error"])
                self.assertNotIn("SCRIPT ERROR", reports[-1]["worker_log"])
            runtime = PreviewRuntime(
                load_scenario(path),
                video_enabled=False,
                authoring_document=document,
                authoring_path=path,
            )
            runtime.control("pause")
            runtime.start()
            try:
                preview = GodotUiSession(runtime, default_renderer().ui_exchange)
                preview.tick()
                runtime.control("step")
                with runtime._condition:
                    self.assertTrue(
                        runtime._condition.wait_for(
                            lambda: runtime._published is not None or runtime._error is not None,
                            timeout=10,
                        )
                    )
                preview.tick()
                report = workers[-1][1][-1]
                self.assertFalse(
                    any(report["controls"]), "Mode 1 playback and edit controls must remain usable"
                )
                self.assertEqual(report["update_count"], 0)
                self.assertEqual(report["selection"]["measurement_id"], "")
                self.assertIsNone(report["voxel_space"])
                self.assertEqual(report["track_count"], 0)
                self.assertEqual(runtime.ui_view()[0]["logical_step_count"], 1)
                self.assertIsNone(runtime.ui_view()[0]["error"])
                self.assertNotIn("SCRIPT ERROR", report["worker_log"])
            finally:
                runtime.close()
            self.assertEqual(len(workers), 4)
            self.assertTrue(all(renderer._socket is None for renderer, _, _ in workers))

    def test_real_disconnect_after_located_output_keeps_evidence_and_stops_before_next_source_step(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory, ui.observed_workers() as workers:
            root = Path(directory)
            path = root / "scene.json"
            spec = real_scene(path)
            document = json.loads(path.read_text())
            document["duration_s"] = 1.01
            save_authoring(document, path)
            output = root / "failed"
            session = CenterSession(
                path, output, mode=4, device_policies=policies(spec.station_configs)
            )
            renderer = workers[-1][0]
            exchange = renderer.ui_exchange
            failed_at = []

            def disconnect(
                state: dict[str, Any], images: tuple[UiImage, ...] = ()
            ) -> list[dict[str, Any]]:
                commands = exchange(state, images)
                update = state.get("algorithm_output")
                if update is not None and update["center_events"]["measurements"]:
                    self.assertEqual(
                        len(rows(output / "center-output.jsonl")), int(update["update_id"])
                    )
                    self.assertEqual(
                        rows(output / "edge-output.jsonl")[-1]["kind"], "step_complete"
                    )
                    failed_at.append(session.step_count)
                    assert renderer._socket is not None
                    renderer._socket.shutdown(socket.SHUT_RDWR)
                    return exchange(state, images)
                return commands

            with (
                patch.object(renderer, "ui_exchange", side_effect=disconnect),
                self.assertRaises(OSError),
            ):
                session.run_ui(tick_hz=240)
            self.assertEqual(failed_at, [session.step_count])
            self.assertTrue(session.closed)
            self.assertFalse(session.ended)
            self.assertIsNone(renderer._socket)
            self.assertTrue(rows(output / "center-output.jsonl")[-1]["measurements"])
            self.assertFalse(rows(output / "center-input.jsonl")[-1]["stream_ended"])
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["state"], "failed")
            self.assertFalse(summary["shutdown_errors"])
            recovery = CenterSession(path, root / "recovery", mode=3, algorithm=create_algorithm())
            recovery.run_ui(tick_hz=240)
            self.assertEqual(workers[-1][1][0]["update_count"], 0)
            self.assertEqual(workers[-1][1][0]["track_count"], 0)
            self.assertEqual(workers[-1][1][0]["evaluation"]["center"]["outputs"], 0)


if __name__ == "__main__":
    unittest.main()

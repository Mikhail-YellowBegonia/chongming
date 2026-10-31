"""Mode-3 ordering, failure cleanup, and real Godot/center round trips."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from chongming.contracts import model as m
from chongming.contracts import scene as s
from chongming.contracts.development import CenterAlgorithmResult, CenterInputStep
from chongming.development import CenterSession, InputInspector, main
from chongming.reference import ReferenceFusionAlgorithm, create_algorithm
from chongming.rendering.godot import GodotRenderer, UiImage, close_default_renderer
from chongming.scenarios.authoring import demo_authoring, save_authoring
from tests import test_godot_visualization as transport
from tests.test_center_development import Recorder

ROOT = Path(__file__).resolve().parents[1]


class FakeRenderer:
    version = "test-worker"

    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict[str, Any]] = []
        self.images: list[tuple[UiImage, ...]] = []
        self.reply: Callable[[dict[str, Any]], list[dict[str, Any]]] = lambda state: []

    def ui_exchange(
        self, state: dict[str, Any], images: tuple[UiImage, ...]
    ) -> list[dict[str, Any]]:
        assert not self.closed
        self.sent.append(state)
        self.images.append(images)
        return self.reply(state)

    def editor_update(
        self, scene: s.LogicalSceneSnapshot, camera: s.CameraStateTruth, **kwargs: Any
    ) -> bool:
        assert not self.closed
        return bool(kwargs["enabled"])

    def close(self) -> None:
        self.closed = True


class CenterUiTests(unittest.TestCase):
    def setUp(self) -> None:
        close_default_renderer()
        self.worker = FakeRenderer()
        self.factory = patch("chongming.rendering.godot.GodotRenderer", return_value=self.worker)
        self.constructor = self.factory.start()
        self.addCleanup(self.factory.stop)
        self.addCleanup(close_default_renderer)
        provenance = patch("chongming.development._provenance", return_value={})
        provenance.start()
        self.addCleanup(provenance.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "scene.json"
        self.output = self.root / "run"
        self.document = demo_authoring(stations=2, duration_s=0.16)
        self.document["stations"][0].update(fps=10)
        self.document["stations"][1].update(fps=20, start_s=0.025)
        self.document["targets"] = []
        save_authoring(self.document, self.path)

    def session(self, algorithm: Recorder | None = None) -> CenterSession:
        return CenterSession(self.path, self.output, algorithm=algorithm or Recorder())

    def test_each_raw_output_is_persisted_then_acknowledged_before_the_next_step(self) -> None:
        recorder = Recorder()
        session = self.session(recorder)
        acknowledged: list[int] = []
        source_steps: list[int] = []
        source = session._steps

        def steps() -> Iterator[s.ScenarioStep]:
            for step in source:
                self.assertEqual(len(acknowledged), len(source_steps))
                source_steps.append(step.packets.sample_time.time_ns)
                yield step

        session._steps = steps()

        def acknowledge(state: dict[str, Any]) -> list[dict[str, Any]]:
            if "algorithm_output" not in state:
                return []
            update = state["algorithm_output"]
            index = int(update["update_id"])
            self.assertEqual(index, len(recorder.inputs))
            rows = (self.output / "center-output.jsonl").read_text().splitlines()
            self.assertEqual(len(rows), index, "raw evidence must already be flushed")
            self.assertEqual(json.loads(rows[-1])["step_no"], recorder.inputs[-1].step_no)
            self.assertIsNone(update["edge_events"], "virtual rays are not edge algorithm output")
            self.assertEqual(update["center_events"]["measurements"], [])
            before = session.source.clock.read()
            time.sleep(0.005)  # A slow receiver cannot cause source catch-up or dropped steps.
            self.assertEqual(session.source.clock.read(), before)
            self.assertEqual(int(update["scenario_time_ns"]), before.time_ns)
            self.assertEqual(int(update["step_no"]), before.step_no)
            acknowledged.append(index)
            return []

        self.worker.reply = acknowledge
        session.run_ui(tick_hz=240)
        self.assertTrue(session.closed and recorder.closed and self.worker.closed)
        self.assertEqual(acknowledged, list(range(1, len(source_steps) + 2)))
        self.assertEqual(len(source_steps), 5)
        self.assertTrue(recorder.inputs[-1].stream_ended)
        self.assertEqual(self.worker.sent[-1]["playback"]["state"], "complete")
        self.assertTrue(all(state["execution_mode"] == 3 for state in self.worker.sent))
        self.assertFalse(any(self.worker.images))
        self.assertNotIn("truth_object_id", (self.output / "center-input.jsonl").read_text())
        summary = json.loads((self.output / "summary.json").read_text())
        self.assertEqual(summary["state"], "complete")
        self.assertEqual(len(summary["shutdown"]), len(session.health))
        self.assertFalse(summary["shutdown_errors"])

    def test_idle_heartbeat_and_quit_do_not_repeat_output_or_advance_a_step(self) -> None:
        recorder = Recorder()
        session = self.session(recorder)

        def reply(state: dict[str, Any]) -> list[dict[str, Any]]:
            if len(recorder.inputs) == 1 and "algorithm_output" not in state:
                return [{"type": "quit"}]
            return []

        self.worker.reply = reply
        session.run_ui(tick_hz=1)
        self.assertEqual(len(recorder.inputs), 1)
        self.assertEqual(len(self.worker.sent), 3)  # initial, algorithm, idle heartbeat
        self.assertEqual([state["version"] for state in self.worker.sent], [1, 2, 3])
        self.assertNotIn("algorithm_output", self.worker.sent[-1])
        self.assertNotIn("truth_snapshots", self.worker.sent[-1])
        self.assertEqual(self.worker.sent[-1]["playback"]["logical_step_count"], 1)
        self.assertEqual(json.loads((self.output / "summary.json").read_text())["state"], "stopped")
        self.assertTrue(self.worker.closed)

    def test_playback_editing_and_invalid_commands_cannot_mutate_the_running_session(self) -> None:
        recorder = Recorder()
        session = self.session(recorder)
        self.worker.reply = lambda state: (
            [
                {"type": "control", "action": "restart"},
                {"type": "seek", "time_s": 0},
                {"type": "edit_begin"},
                {"type": "select_station", "station": "unknown"},
                {"type": "freeview", "enabled": "true"},
                {"type": "select_station", "station": "station-02"},
                {"type": "freeview", "enabled": False},
            ]
            if state["version"] == 2
            else []
        )
        with self.assertLogs("chongming", level="WARNING") as logs:
            session.run_ui(tick_hz=240)
        self.assertEqual(len(logs.records), 5)
        self.assertEqual(self.worker.sent[-1]["station"], "station-02")
        self.assertFalse(self.worker.sent[-1]["editor"]["freeview"])
        self.assertEqual(session.step_count, 5)
        self.assertTrue(session.ended)
        self.assertEqual(json.loads(self.path.read_text()), self.document)
        self.assertEqual(len(self.worker.sent[-1]["log"]), 5)

    def test_independent_camera_schedules_keep_last_pixels_with_their_own_capture_truth(
        self,
    ) -> None:
        self.document = demo_authoring(stations=1, duration_s=0.08)
        self.document["targets"] = []
        save_authoring(self.document, self.path)
        session = CenterSession(self.path, self.output, video_enabled=True)
        session.spec = replace(
            session.spec,
            scene_definition=replace(
                session.spec.scene_definition,
                cameras=tuple(
                    replace(
                        camera,
                        exposure=replace(camera.exposure, first_exposure_start_ns=10_000_000),
                    )
                    if camera.camera_role is m.CameraRole.AUXILIARY
                    else camera
                    for camera in session.spec.scene_definition.cameras
                ),
            ),
        )
        session._steps = iter(session.source.generate(session.spec))

        def pixels(
            scene: s.LogicalSceneSnapshot, camera: s.CameraStateTruth
        ) -> tuple[bytes, float]:
            marker = (scene.sample_time.time_ns // 1000) % 256
            return (
                bytes([marker])
                * camera.actual_intrinsics.image_width_px
                * camera.actual_intrinsics.image_height_px
                * 3,
                0.0,
            )

        # Pixel/capture association only; real GPU and encoder coverage is separate below.
        with (
            patch("chongming.modules.scene.render_rgb8", side_effect=pixels),
            patch.object(session.writer, "record", return_value=0.0),
        ):
            session.run_ui(tick_hz=240)
        observations = 0
        mixed_captures = False
        for state, images in zip(self.worker.sent, self.worker.images, strict=True):
            if not images:
                continue
            truths = {
                str(truth["camera_role"]): truth
                for truth in state["frame"]["monitor"]["frame_truth"]
            }
            capture_times = set()
            for image in images:
                capture = truths[image.key.split("/")[-1]]["capture_time"]
                midpoint = (capture["exposure_start_ns"] + capture["exposure_end_ns"]) // 2
                self.assertEqual(image.pixels[0], (midpoint // 1000) % 256)
                capture_times.add(midpoint)
                observations += 1
            mixed_captures |= len(capture_times) > 1
        self.assertGreater(observations, session.step_count)
        self.assertTrue(mixed_captures)

    def test_zero_sample_run_still_sends_the_terminal_algorithm_output(self) -> None:
        for station in self.document["stations"]:
            station["start_s"] = 1
        save_authoring(self.document, self.path)
        recorder = Recorder()
        session = self.session(recorder)
        session.run_ui(tick_hz=240)
        self.assertEqual(session.step_count, 0)
        self.assertEqual(len(recorder.inputs), 1)
        self.assertTrue(recorder.inputs[0].stream_ended)
        output = self.worker.sent[-1]
        self.assertEqual(output["algorithm_output"]["update_id"], "1")
        self.assertEqual(output["algorithm_output"]["scenario_time_ns"], "0")
        self.assertEqual(output["truth_snapshots"], [{"targets": []}])
        self.assertIsNone(output["frame"])
        self.assertTrue(self.worker.closed)

    def test_algorithm_and_transport_failures_stop_without_retry_and_preserve_evidence(
        self,
    ) -> None:
        class Failing(Recorder):
            def process(self, step: CenterInputStep) -> CenterAlgorithmResult:
                super().process(step)
                raise RuntimeError("algorithm failed")

        for failure in (
            "algorithm",
            "initial_ui",
            "output_ui",
            "idle_ui",
            "freeview",
            "command_freeview",
        ):
            with self.subTest(failure=failure):
                self.output = self.root / failure
                self.worker = FakeRenderer()
                self.constructor.reset_mock()
                self.constructor.return_value = self.worker
                recorder = Failing() if failure == "algorithm" else Recorder()
                session = self.session(recorder)

                def fail(state: dict[str, Any], failure: str = failure) -> list[dict[str, Any]]:
                    if (
                        failure == "initial_ui"
                        or (failure == "output_ui" and "algorithm_output" in state)
                        or (failure == "idle_ui" and len(self.worker.sent) == 3)
                    ):
                        raise ConnectionError("UI disconnected")
                    if failure == "command_freeview" and "algorithm_output" in state:
                        return [{"type": "freeview", "enabled": False}]
                    return []

                self.worker.reply = fail
                with ExitStack() as stack:
                    if failure in {"freeview", "command_freeview"}:
                        side_effect: Any = ConnectionError("freeview disconnected")
                        if failure == "command_freeview":
                            side_effect = [True, ConnectionError("freeview disconnected")]
                        stack.enter_context(
                            patch.object(self.worker, "editor_update", side_effect=side_effect)
                        )
                    with self.assertRaisesRegex(
                        (RuntimeError, ConnectionError), "failed|disconnected"
                    ):
                        session.run_ui(tick_hz=10)
                expected_inputs = 0 if failure == "initial_ui" else 1
                self.assertEqual(len(recorder.inputs), expected_inputs)
                self.assertTrue(session.closed and recorder.closed and self.worker.closed)
                summary = json.loads((self.output / "summary.json").read_text())
                self.assertEqual(summary["state"], "failed")
                self.assertIn(
                    "failed" if failure == "algorithm" else "disconnected", summary["error"]
                )
                self.assertEqual(len(summary["shutdown"]), len(session.health))
                self.assertFalse(summary["shutdown_errors"])
                lines = (self.output / "center-output.jsonl").read_text().splitlines()
                self.assertEqual(len(lines), 0 if failure in {"algorithm", "initial_ui"} else 1)
                self.constructor.assert_called_once_with()

    def test_graphical_default_and_explicit_algorithm_leave_cli_default_unchanged(self) -> None:
        for options, algorithm_type in (
            (["--ui"], ReferenceFusionAlgorithm),
            (["--ui", "--algorithm", "chongming.development:InputInspector"], InputInspector),
            (["--run"], InputInspector),
        ):
            with (
                self.subTest(options=options),
                patch.object(
                    sys,
                    "argv",
                    [
                        "chongming-center",
                        "--scenario",
                        str(self.path),
                        "--output",
                        str(self.output),
                        *options,
                    ],
                ),
                patch("chongming.development.CenterSession") as constructor,
            ):
                constructor.return_value.ended = True
                main()
                self.assertIsInstance(constructor.call_args.kwargs["algorithm"], algorithm_type)
                self.assertFalse(constructor.call_args.kwargs["video_enabled"])
                self.assertEqual(constructor.return_value.run_ui.call_count, int("--ui" in options))
                constructor.return_value.close.assert_called_once()

    def test_reusing_a_closed_or_previously_stepped_ui_session_is_rejected(self) -> None:
        session = self.session()
        session.step()
        with self.assertRaisesRegex(RuntimeError, "new session"):
            session.run_ui()
        session.close()
        with self.assertRaisesRegex(RuntimeError, "new session"):
            session.run_ui()


RUN_WORKER = """extends "res://worker.gd"
var _idle_redraws := 0
func _redraw_freeview() -> void:
    if _editor_active and not _editor_realtime and not _busy:
        _idle_redraws += 1
    super._redraw_freeview()
func _ui_exchange(request: Dictionary, trailer: PackedByteArray) -> PackedByteArray:
    var reply := super._ui_exchange(request, trailer)
    if reply.is_empty():
        return reply
    var document: Dictionary = JSON.parse_string(reply.get_string_from_utf8())
    document.commands.append({"type": "test_run", "idle_redraws": _idle_redraws, "update_count": _visualization.update_count,
        "last_update": _visualization.last_update, "evaluation": _visualization.evaluation.summary(),
        "measurements": _visualization.measurements.values(), "track_count": _visualization.tracks.size(),
        "truths": _visualization.truth_by_time.values(), "diagnostic_text": _ui._diagnostics.text,
        "info_text": _ui._info.text, "station_text": _ui._station_states.text,
        "controls": [_ui._play.disabled, _ui._step.disabled, _ui._restart.disabled,
            _ui._seek.disabled, not _ui._seek_time.editable, _ui._edit_begin.disabled]})
    return JSON.stringify(document).to_utf8_buffer()
"""


@contextmanager
def observed_workers() -> Iterator[
    list[tuple[GodotRenderer, list[dict[str, Any]], list[dict[str, Any]]]]
]:
    workers: list[tuple[GodotRenderer, list[dict[str, Any]], list[dict[str, Any]]]] = []
    close_default_renderer()
    with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
        package = Path(directory)
        project = package / "godot"
        shutil.copytree(transport.PROJECT, project)
        (project / "test_run.gd").write_text(RUN_WORKER)
        scene = project / "worker.tscn"
        scene.write_text(scene.read_text().replace("res://worker.gd", "res://test_run.gd"))
        resources.enter_context(patch("chongming.rendering.godot.files", return_value=package))

        def factory() -> GodotRenderer:
            if workers:
                assert workers[-1][0]._socket is None
            renderer = GodotRenderer()
            reports: list[dict[str, Any]] = []
            states: list[dict[str, Any]] = []
            exchange = renderer.ui_exchange

            def record(
                state: dict[str, Any], images: tuple[UiImage, ...] = ()
            ) -> list[dict[str, Any]]:
                states.append(state)
                commands = exchange(state, images)
                for command in commands:
                    if command["type"] == "test_run":
                        reports.append({**command, "worker_log": renderer._diagnostics()})
                return [command for command in commands if command["type"] != "test_run"]

            resources.enter_context(patch.object(renderer, "ui_exchange", side_effect=record))
            workers.append((renderer, reports, states))
            return renderer

        resources.enter_context(
            patch("chongming.rendering.godot.GodotRenderer", side_effect=factory)
        )
        try:
            yield workers
        finally:
            close_default_renderer()


@unittest.skipUnless(transport.GODOT, "Godot 4 with a real graphics backend is required")
class CenterUiRoundTripTests(unittest.TestCase):
    def test_algorithm_increment_redraws_freeview_without_another_scene_update(self) -> None:
        from tests.test_scene_rendering import snapshot, test_scene

        with observed_workers() as workers:
            # Start the worker through the same singleton owned by CenterSession.
            from chongming.rendering.godot import default_renderer

            renderer = default_renderer()
            scene = snapshot(test_scene(s.SphereGeometry(1.0), (0.0, 10.0, -30.0)))
            renderer.editor_update(scene, scene.cameras[0], enabled=True, realtime=False)
            renderer.ui_exchange({"version": 1})
            renderer.ui_exchange({"version": 2})
            before = workers[0][1][-1]["idle_redraws"]
            renderer.ui_exchange({**transport.state(), "version": 3})
            renderer.ui_exchange({"version": 4})
            after = workers[0][1][-1]["idle_redraws"]
            self.assertGreater(
                after, before, "algorithm output must redraw without another editor snapshot"
            )

    def test_reference_outputs_reach_godot_and_new_runs_reset_worker_caches_and_statistics(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory, observed_workers() as workers:
            root = Path(directory)
            document = json.loads((ROOT / "scenarios/center_clean.json").read_text())
            document["duration_s"] = 0.12
            save_authoring(document, root / "scene.json")
            sessions = []
            for run in ("clean", "empty"):
                if run == "empty":
                    document["targets"] = []
                    save_authoring(document, root / "scene.json")
                session = CenterSession(
                    root / "scene.json", root / run, algorithm=create_algorithm()
                )
                sessions.append(session)
                session.run_ui(tick_hz=240)
                renderer, reports, states = workers[-1]
                self.assertTrue(session.closed)
                self.assertIsNone(renderer._socket)
                self.assertEqual(reports[0]["update_count"], 0)
                self.assertEqual(reports[0]["evaluation"]["center"]["outputs"], 0)
                self.assertEqual(reports[0]["track_count"], 0)
                self.assertTrue(all(all(report["controls"]) for report in reports))
                increments = [state for state in states if "algorithm_output" in state]
                self.assertEqual(
                    [int(state["algorithm_output"]["update_id"]) for state in increments],
                    list(range(1, session.step_count + 2)),
                )
                raw = [
                    json.loads(line)
                    for line in (root / run / "center-output.jsonl").read_text().splitlines()
                ]
                count = sum(len(row["measurements"]) for row in raw)
                self.assertEqual(reports[-1]["update_count"], len(raw))
                self.assertEqual(reports[-1]["evaluation"]["center"]["outputs"], count)
                self.assertEqual(reports[-1]["evaluation"]["edge"]["outputs"], 0)
                self.assertIn("模式 3", reports[-1]["info_text"])
                self.assertIn("体素仅支持参考中心算法的无先验原始投票", reports[-1]["info_text"])
                self.assertNotIn("辅助相机标定", reports[-1]["info_text"])
                if run == "clean":
                    stats = reports[-1]["evaluation"]["center"]
                    self.assertGreater(stats["sample_count"], 0)
                    self.assertEqual(stats["matched"], count)
                    self.assertLess(stats["max_error"], 1e-8)
                    self.assertGreater(reports[-1]["track_count"], 0)
                    self.assertIn("VALID", reports[-1]["station_text"])
                    historical = False
                    for state in increments:
                        update = state["algorithm_output"]
                        snapshots = state["truth_snapshots"]
                        by_time = {
                            int(target["time_ns"]): target
                            for snapshot in snapshots
                            for target in snapshot["targets"]
                        }
                        for measurement in update["center_events"]["measurements"]:
                            reference = int(measurement["reference_time_ns"])
                            truth = by_time[reference]
                            self.assertEqual(
                                truth["position_enu_m"], [-15 + reference / 1e9, 62.0, 20.0]
                            )
                            historical |= reference != int(update["scenario_time_ns"])
                    self.assertTrue(historical, "window outputs need exact historical truth")
                else:
                    self.assertEqual(count, 0)
                    self.assertEqual(reports[-1]["track_count"], 0)
                    self.assertIsNone(reports[-1]["evaluation"]["center"]["mean_error"])
                self.assertNotIn("SCRIPT ERROR", reports[-1]["worker_log"])
            self.assertEqual(len(workers), 2)
            self.assertNotEqual(
                sessions[0].source.station_sessions, sessions[1].source.station_sessions
            )

    def test_real_ui_cli_defaults_to_reference_and_can_record_optional_video(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = json.loads((ROOT / "scenarios/center_clean.json").read_text())
            document["duration_s"] = 0.01
            save_authoring(document, root / "scene.json")
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "chongming-center",
                        "--ui",
                        "--video",
                        "--scenario",
                        str(root / "scene.json"),
                        "--output",
                        str(root / "run"),
                        "--tick-hz",
                        "240",
                    ],
                ),
                patch(
                    "builtins.input", side_effect=AssertionError("UI must not wait for CLI input")
                ),
            ):
                main()
            manifest = json.loads((root / "run/manifest.json").read_text())
            self.assertEqual(manifest["algorithm"], "ReferenceFusionAlgorithm")
            self.assertTrue(manifest["video_enabled"])
            self.assertEqual(len(list((root / "run/video").glob("*.mp4"))), 4)
            summary = json.loads((root / "run/summary.json").read_text())
            self.assertEqual(summary["state"], "complete")
            self.assertFalse(summary["shutdown_errors"])


if __name__ == "__main__":
    unittest.main()

"""Contract tests for the real clean-ray source and discrete developer tool."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Generator
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np

from chongming.composition.graph import ArchitectureAssembly, declare_assembly
from chongming.contracts import model as m
from chongming.contracts import scene as s
from chongming.contracts.development import CenterInputStep, CenterRegistration
from chongming.contracts.fusion import (
    FusionBatchResult,
    RegistryWriteResult,
    RegistryWriteStatus,
    StationEventTimeProgress,
    WatermarkSnapshot,
    WindowUpdate,
)
from chongming.contracts.runtime import ModuleHealth, RunSpec
from chongming.development import CenterSession, FusionEngineAdapter, InputInspector
from chongming.modules import scene as modules
from chongming.ports.lifecycle import ModuleBinding
from chongming.scenarios.authoring import compile_authoring, demo_authoring, save_authoring
from chongming.scenarios.demo import demo_source
from chongming.scenarios.run import _json, run_scene
from tests.test_scene_run import normalize

ROOT = Path(__file__).resolve().parents[1]


class Recorder(InputInspector):
    def __init__(self) -> None:
        self.inputs: list[CenterInputStep] = []
        self.closed = False

    def start(self, registration: CenterRegistration) -> None:
        super().start(registration)
        self.registration = registration

    def process(
        self, step: CenterInputStep
    ) -> tuple[tuple[m.FusionMeasurement, ...], tuple[m.TrackEvent, ...]]:
        self.inputs.append(step)
        return super().process(step)

    def close(self) -> None:
        super().close()
        self.closed = True


class CenterDevelopmentTests(unittest.TestCase):
    def test_headless_full_lifecycle_clean_rays_truth_and_terminal_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            recorder = Recorder()
            with (
                patch.object(modules, "render_rgb8", side_effect=AssertionError("GPU forbidden")),
                patch("PIL.Image.frombytes", side_effect=AssertionError("pixels forbidden")),
            ):
                session = CenterSession(
                    ROOT / "scenarios/center_clean.json", output, algorithm=recorder
                )
                try:
                    self.assertEqual(session.step_count, 0)
                    self.assertEqual(len(session.health), len(session.assembly.bindings))
                    self.assertTrue(
                        any("algorithm_unimplemented" in h.reason_codes for h in session.health)
                    )
                    session.step()
                    first = recorder.inputs[-1]
                    self.assertEqual(len(first.events), 4)  # two registrations, two rays
                    self.assertEqual(len(first.progress), 2)
                    snapshot = session.snapshot()
                    self.assertEqual(snapshot, session.snapshot())
                    self.assertEqual(len(recorder.inputs), 1)
                    rays = [r.event for r in first.events if isinstance(r.event, m.RayEvent)]
                    truth = session.spec.scene_definition.flying_objects[0]
                    at = first.time.time_ns / 1e9
                    expected = (-15 + at, 62, 20)
                    for ray in rays:
                        d = np.asarray(expected) - np.asarray(ray.ray_origin_enu_m)
                        np.testing.assert_allclose(
                            ray.ray_direction_enu_unit, d / np.linalg.norm(d), atol=1e-12
                        )
                    self.assertEqual(str(truth.truth_object_id), "moving-sphere")
                    # Jump the finite end for this test without generating 900 groups.
                    assert isinstance(session._steps, Generator)
                    session._steps.close()
                    session._steps = iter(())
                    session.step()
                    self.assertTrue(recorder.inputs[-1].stream_ended)
                    self.assertTrue(all(p.stream_ended for p in recorder.inputs[-1].progress))
                    count = len(recorder.inputs)
                    session.step()
                    self.assertEqual(len(recorder.inputs), count)
                finally:
                    session.close()
                self.assertTrue(recorder.closed)
                self.assertFalse((output / "video").exists())
                summary = json.loads((output / "summary.json").read_text())
                self.assertEqual(len(summary["shutdown"]), len(session.health))
                self.assertTrue(all(h["state"] == "stopped" for h in summary["shutdown"]))
                online = (output / "center-input.jsonl").read_text()
                self.assertNotIn("truth_object_id", online)
                self.assertNotIn("experiment_perturbations", online)
                self.assertIn("moving-sphere", (output / "truth.jsonl").read_text())

    def test_video_toggle_preserves_exposures_events_truth_and_calls_no_pixels_when_off(
        self,
    ) -> None:
        spec = compile_authoring(demo_authoring(stations=1, duration_s=0.12))
        off = demo_source(spec, video_enabled=False)
        with patch.object(modules, "render_rgb8", side_effect=AssertionError("GPU forbidden")):
            first_off = next(iter(off.generate(spec)))
        toggled = demo_source(spec, video_enabled=False)
        steps = iter(toggled.generate(spec))
        first = next(steps)
        toggled.video_enabled = True
        second = next(steps)
        toggled.video_enabled = False
        with patch.object(modules, "render_rgb8", side_effect=AssertionError("GPU forbidden")):
            third = next(steps)
        self.assertFalse(first.video.rendered)
        self.assertTrue(second.video.rendered)
        self.assertFalse(third.video.rendered)
        self.assertEqual(third.video.render_skip_reason, "video_disabled")
        assert third.truth.timing is not None
        self.assertEqual(third.truth.timing.gpu_submissions, 0)
        left = [(s.image_session_id, s.virtual_session_id) for s in off.station_sessions]
        right = [(s.image_session_id, s.virtual_session_id) for s in toggled.station_sessions]
        # normalize accepts JSON documents, so use the evidence encoder first.
        from chongming.scenarios.run import _json

        a = json.loads(
            _json({"packets": first_off.packets, "truth": replace(first_off.truth, timing=None)})
        )
        b = json.loads(
            _json({"packets": first.packets, "truth": replace(first.truth, timing=None)})
        )
        self.assertEqual(
            normalize(a, [x for p in left for x in p]), normalize(b, [x for p in right for x in p])
        )

    def test_empty_and_zero_sample_runs_report_progress_and_end(self) -> None:
        for duration, start in ((0.07, 0), (0.001, 1)):
            with self.subTest(duration=duration), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                doc = demo_authoring(stations=2, duration_s=duration)
                doc["targets"] = []
                for station in doc["stations"]:
                    station["start_s"] = start
                save_authoring(doc, root / "scene.json")
                recorder = Recorder()
                session = CenterSession(root / "scene.json", root / "run", algorithm=recorder)
                try:
                    while not session.ended:
                        session.step()
                finally:
                    session.close()
                self.assertTrue(recorder.inputs[-1].stream_ended)
                self.assertEqual(len(recorder.inputs[-1].progress), 2)
                self.assertFalse(
                    any(
                        isinstance(r.event, m.RayEvent)
                        for step in recorder.inputs
                        for r in step.events
                    )
                )
                if start:
                    self.assertEqual(session.step_count, 0)
                    self.assertTrue(
                        all(p.completed_capture_time is None for p in recorder.inputs[-1].progress)
                    )

    def test_algorithm_failure_cleans_every_started_module(self) -> None:
        class Fails(Recorder):
            def process(
                self, step: CenterInputStep
            ) -> tuple[tuple[m.FusionMeasurement, ...], tuple[m.TrackEvent, ...]]:
                raise RuntimeError("algorithm failed")

        with tempfile.TemporaryDirectory() as directory:
            recorder = Fails()
            output = Path(directory) / "run"
            session = CenterSession(ROOT / "scenarios/empty.json", output, algorithm=recorder)
            with self.assertRaisesRegex(RuntimeError, "algorithm failed"):
                session.step()
            self.assertTrue(session.closed)
            self.assertTrue(recorder.closed)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["state"], "failed")
            self.assertEqual(len(summary["shutdown"]), len(session.health))

    def test_authoring_strict_edits_long_orbits_and_search(self) -> None:
        doc = demo_authoring(duration_s=60)
        doc["world_objects"] = []
        doc["search"] = {
            "min_enu_m": [-100, -100, 0],
            "max_enu_m": [100, 100, 100],
            "step_enu_m": [20, 20, 20],
        }
        spec = compile_authoring(doc)
        self.assertEqual(
            spec.virtual_observations.calibration_intervals[0].end_time_ns, 60_000_000_000
        )
        self.assertEqual(spec.voxel_search.step_enu_m, (20, 20, 20))
        self.assertTrue(spec.scene_definition.world_objects)
        self.assertTrue(
            all(
                o.kind is s.WorldObjectKind.FIXED_REFERENCE
                for o in spec.scene_definition.world_objects
            )
        )
        from chongming.scenarios.behaviors import sample_behavior

        target = spec.scene_definition.flying_objects[0]
        assert target.behavior is not None
        a = sample_behavior(target.behavior, target.trajectory.knots[0].state, 0)
        b = sample_behavior(target.behavior, target.trajectory.knots[0].state, 48_000_000_000)
        np.testing.assert_allclose(a.position_enu_m, b.position_enu_m, atol=1e-12)
        for key, value in (("fps", 30.5), ("random_seed", True)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                compile_authoring({**doc, key: value})
        doc["stations"][0]["unknown"] = 1
        with self.assertRaisesRegex(ValueError, "unknown station fields"):
            compile_authoring(doc)

    def test_full_video_and_headless_runs_have_identical_online_and_truth_data(self) -> None:
        doc = json.loads((ROOT / "scenarios/center_clean.json").read_text())
        doc["duration_s"] = 0.07
        spec = compile_authoring(doc)
        streams = [demo_source(spec, video_enabled=enabled) for enabled in (False, True)]
        try:
            normalized = []
            for source in streams:
                sessions = [
                    str(sid)
                    for binding in source.station_sessions
                    for sid in (binding.image_session_id, binding.virtual_session_id)
                ]
                rows = list(source.generate(spec))
                self.assertEqual(len(rows), 3)
                if not source.video_enabled:
                    for row in rows:
                        assert row.truth.timing is not None
                        self.assertEqual(row.truth.timing.gpu_submissions, 0)
                else:
                    self.assertTrue(all(row.video.rendered for row in rows))
                    for row in rows:
                        for frame in row.video.frames:
                            expected = (
                                (1920, 1080)
                                if frame.camera_role is m.CameraRole.MAIN
                                else (640, 480)
                            )
                            self.assertEqual(
                                (frame.image.width_px, frame.image.height_px), expected
                            )
                normalized.append(
                    normalize(
                        json.loads(
                            _json(
                                [
                                    {
                                        "packets": row.packets,
                                        "truth": replace(row.truth, timing=None),
                                    }
                                    for row in rows
                                ]
                            )
                        ),
                        sessions,
                    )
                )
            self.assertEqual(*normalized)
        finally:
            from chongming.rendering.godot import close_default_renderer

            close_default_renderer()

    def test_long_clean_run_keeps_rays_after_24_seconds_and_finite_export_has_no_pixels(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc = json.loads((ROOT / "scenarios/center_clean.json").read_text())
            doc.update(fps=1, duration_s=26)
            save_authoring(doc, root / "scene.json")
            recorder = Recorder()
            with patch.object(modules, "render_rgb8", side_effect=AssertionError("GPU forbidden")):
                session = CenterSession(root / "scene.json", root / "center", algorithm=recorder)
                try:
                    while not session.ended:
                        session.step()
                    self.assertEqual(session.step_count, 26)
                    self.assertEqual(session.ray_count, 52)
                    self.assertEqual(len(recorder.inputs), 27)
                    self.assertTrue(recorder.inputs[-1].stream_ended)
                    late = recorder.inputs[-2]
                    self.assertGreater(late.time.time_ns, 24_000_000_000)
                    self.assertEqual(sum(isinstance(e.event, m.RayEvent) for e in late.events), 2)
                finally:
                    session.close()
                summary = run_scene(root / "scene.json", root / "export", video_enabled=False)
            self.assertFalse((root / "export/video").exists())
            self.assertEqual(summary["counts"]["ray_events"], 52)
            self.assertEqual(summary["counts"]["gpu_submissions"], 0)

    def test_independent_sampling_reports_only_sampled_main_streams(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc = demo_authoring(stations=2, duration_s=0.17)
            doc["targets"] = []
            doc["stations"][0].update(fps=10)
            doc["stations"][1].update(fps=20, start_s=0.025)
            save_authoring(doc, root / "scene.json")
            recorder = Recorder()
            session = CenterSession(root / "scene.json", root / "run", algorithm=recorder)
            # Auxiliary-only exposures must not manufacture main-stream completion.
            session.spec = replace(
                session.spec,
                scene_definition=replace(
                    session.spec.scene_definition,
                    cameras=tuple(
                        replace(
                            camera,
                            exposure=replace(
                                camera.exposure,
                                first_exposure_start_ns=camera.exposure.first_exposure_start_ns
                                + 10_000_000,
                            ),
                        )
                        if camera.camera_role is m.CameraRole.AUXILIARY
                        else camera
                        for camera in session.spec.scene_definition.cameras
                    ),
                ),
            )
            session._steps = iter(session.source.generate(session.spec))
            try:
                while not session.ended:
                    session.step()
                self.assertTrue(any(not step.progress for step in recorder.inputs[:-1]))
                by_station: dict[str, list[int]] = {
                    str(station["id"]): [] for station in doc["stations"]
                }
                for step in recorder.inputs[:-1]:
                    for progress in step.progress:
                        assert progress.completed_capture_time is not None
                        by_station[str(progress.station_id)].append(
                            (
                                progress.completed_capture_time.exposure_start_ns
                                + progress.completed_capture_time.exposure_end_ns
                            )
                            // 2
                        )
                self.assertEqual(sorted(map(len, by_station.values())), [2, 3])
                self.assertTrue(all(len(step.progress) <= 1 for step in recorder.inputs[:-1]))
            finally:
                session.close()

    def test_cli_can_reset_after_finite_end_and_closes_both_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc = demo_authoring(stations=1, duration_s=0.01)
            doc["targets"] = []
            save_authoring(doc, root / "scene.json")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "chongming.development",
                    "--scenario",
                    str(root / "scene.json"),
                    "--output",
                    str(root / "run"),
                ],
                input="run\nstatus\nreset\nstep\nquit\n",
                check=False,
                text=True,
                capture_output=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs = sorted(root.glob("run*"))
            self.assertEqual(len(outputs), 2)
            manifests = [json.loads((out / "manifest.json").read_text()) for out in outputs]
            self.assertNotEqual(manifests[0]["station_sessions"], manifests[1]["station_sessions"])
            for output in outputs:
                summary = json.loads((output / "summary.json").read_text())
                self.assertTrue(all(h["state"] == "stopped" for h in summary["shutdown"]))
                self.assertFalse(summary["shutdown_errors"])

    def test_engine_adapter_registers_before_events_then_progress_and_tick(self) -> None:
        calls: list[tuple[str, object]] = []

        def empty_batch(time: m.ProcessingTime) -> FusionBatchResult:
            return FusionBatchResult(
                center_session_id=m.SessionId("test-center"),
                produced_at=time,
                admissions=(),
                window_update=WindowUpdate(
                    receipts=(),
                    sealed_windows=(),
                    watermark=WatermarkSnapshot(
                        capture_clock_domain_id="test-clock",
                        event_time_ns=None,
                        evaluated_at=time,
                        station_progress=(),
                    ),
                ),
                candidates=(),
                associations=(),
                geometry=(),
                measurements=(),
                tracks=(),
            )

        class Engine:
            def register_station(self, station: m.StationConfig) -> RegistryWriteResult:
                calls.append(("station", station))
                return RegistryWriteResult(status=RegistryWriteStatus.REGISTERED, reason_codes=())

            def register_calibration(self, solution: m.CalibrationSolution) -> RegistryWriteResult:
                calls.append(("solution", solution))
                return RegistryWriteResult(status=RegistryWriteStatus.REGISTERED, reason_codes=())

            def process(self, event: m.ReceivedEvent) -> FusionBatchResult:
                calls.append(("event", event))
                return empty_batch(event.arrival_time)

            def report_progress(self, progress: StationEventTimeProgress) -> FusionBatchResult:
                calls.append(("progress", progress))
                return empty_batch(m.ProcessingTime(clock_domain_id="test-clock", time_ns=0))

            def tick(self, time: m.ProcessingTime) -> FusionBatchResult:
                calls.append(("tick", time))
                return empty_batch(time)

            def close(self) -> None:
                calls.append(("close", None))

        with tempfile.TemporaryDirectory() as directory:
            session = CenterSession(
                ROOT / "scenarios/center_clean.json",
                Path(directory) / "run",
                algorithm=FusionEngineAdapter(Engine()),
            )
            try:
                session.step()
                self.assertEqual(
                    [name for name, _ in calls],
                    ["station"] * 2
                    + ["solution"] * 2
                    + ["event"] * 4
                    + ["progress"] * 2
                    + ["tick"],
                )
            finally:
                session.close()
            self.assertEqual(calls[-1][0], "close")

    def test_initialization_failure_stops_partially_started_module(self) -> None:
        from chongming.modules.base import ModuleShell

        original = ModuleShell.start_module
        bindings: list[ModuleBinding] = []

        def assembly(spec: s.ScenarioSpec, run: RunSpec) -> ArchitectureAssembly:
            result = declare_assembly(spec, run)
            bindings.extend(result.bindings)
            return result

        def fail(module: ModuleShell, key: str, now: m.ProcessingTime) -> ModuleHealth:
            result = original(module, key, now)
            if module is bindings[2].module:
                raise RuntimeError("partial start failed")
            return result

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            with (
                patch("chongming.development.declare_assembly", side_effect=assembly),
                patch.object(ModuleShell, "start_module", fail),
                self.assertRaisesRegex(RuntimeError, "partial start failed"),
            ):
                CenterSession(ROOT / "scenarios/empty.json", output)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(len(summary["shutdown"]), 3)
            self.assertTrue(all(h["state"] == "stopped" for h in summary["shutdown"]))

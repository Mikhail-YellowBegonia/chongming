"""Device workbench behavior through the public source, with no Godot worker."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import fields, is_dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, get_args, get_type_hints
from unittest.mock import patch

from chongming.contracts import model as m
from chongming.contracts import scene as s
from chongming.contracts.development import (
    DeviceAlgorithmOutput,
    DeviceInputStep,
    DeviceRegistration,
)
from chongming.device_development import DeviceSession, FrameInspector, main
from chongming.scenarios.authoring import demo_authoring, save_authoring


class Recorder(FrameInspector):
    def __init__(self, *, fail_start: bool = False, fail_frame: bool = False) -> None:
        self.registration: DeviceRegistration | None = None
        self.frames: list[m.CameraFrame] = []
        self.ticks: list[DeviceInputStep] = []
        self.calls: list[str] = []
        self.closed_count = 0
        self.fail_start = fail_start
        self.fail_frame = fail_frame

    def start(self, registration: DeviceRegistration) -> None:
        super().start(registration)
        self.registration = registration
        self.calls.append("start")
        if self.fail_start:
            raise RuntimeError("start failed after allocation")

    def process_main(self, frame: m.CameraFrame, now: m.ProcessingTime) -> DeviceAlgorithmOutput:
        self.frames.append(frame)
        self.calls.append("main")
        if self.fail_frame:
            raise RuntimeError("frame failed")
        # A test fixture explicitly claims a completed negative measurement.
        return DeviceAlgorithmOutput(
            detections=(
                m.DetectionBatch(
                    main_frame_ref=m.FrameRef(
                        station_id=frame.station_id,
                        station_session_id=frame.station_session_id,
                        camera_role=frame.camera_role,
                        frame_id=frame.frame_id,
                    ),
                    capture_time=frame.capture_time,
                    processing_status=m.ProcessingStatus.PROCESSED,
                    observations=(),
                    reason_codes=(),
                ),
            ),
        )

    def process_auxiliary(
        self, frame: m.CameraFrame, now: m.ProcessingTime
    ) -> DeviceAlgorithmOutput:
        self.frames.append(frame)
        self.calls.append("auxiliary")
        return super().process_auxiliary(frame, now)

    def tick(self, step: DeviceInputStep) -> DeviceAlgorithmOutput:
        self.ticks.append(step)
        self.calls.append("end" if step.stream_ended else "tick")
        return super().tick(step)

    def close(self) -> None:
        self.closed_count += 1
        self.calls.append("close")
        super().close()


class DeviceDevelopmentTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        # Both lifecycle and image paths are replaced before any session exists.
        renderer = SimpleNamespace(version="mock-no-godot")
        self.enterContext(patch("chongming.modules.scene.default_renderer", return_value=renderer))
        self.enterContext(patch("chongming.development.default_renderer", return_value=renderer))
        self.enterContext(patch("chongming.modules.scene.close_default_renderer"))
        self.enterContext(patch("chongming.rendering.godot.close_default_renderer"))
        self.render = self.enterContext(
            patch("chongming.modules.scene.render_rgb8", side_effect=self._pixels)
        )

    @staticmethod
    def _pixels(scene: s.LogicalSceneSnapshot, camera: s.CameraStateTruth) -> tuple[bytes, float]:
        size = camera.actual_intrinsics
        return bytes(size.image_width_px * size.image_height_px * 3), 0.0

    def scenario(self, *, stations: int = 1, duration_s: float = 0.11) -> Path:
        doc = demo_authoring(stations=stations, duration_s=duration_s)
        doc["targets"] = []
        doc["fps"] = 10
        path = self.root / "scene.json"
        save_authoring(doc, path)
        return path

    def session(
        self, recorder: Recorder | None = None, *, video_enabled: bool = True
    ) -> DeviceSession:
        session = DeviceSession(
            self.scenario(), self.root / "run", algorithm=recorder, video_enabled=video_enabled
        )
        self.addCleanup(session.close)
        return session

    def test_independent_station_and_role_sampling_dispatches_main_without_auxiliary(self) -> None:
        recorder = Recorder()
        session = DeviceSession(
            self.scenario(stations=2, duration_s=0.16),
            self.root / "run",
            algorithm=recorder,
        )
        self.addCleanup(session.close)
        # Exercise the real public generator with offset, lower-rate auxiliaries.
        session.spec = replace(
            session.spec,
            scene_definition=replace(
                session.spec.scene_definition,
                cameras=tuple(
                    replace(
                        camera,
                        metadata=replace(
                            camera.metadata,
                            nominal_frame_rate_hz=camera.metadata.nominal_frame_rate_hz
                            / (3 if camera.camera_role is m.CameraRole.AUXILIARY else 1),
                        ),
                        exposure=replace(
                            camera.exposure,
                            first_exposure_start_ns=(
                                camera.exposure.first_exposure_start_ns
                                + (
                                    10_000_000
                                    if camera.camera_role is m.CameraRole.AUXILIARY
                                    else 0
                                )
                                + (
                                    25_000_000
                                    if camera.station_id == m.StationId("station-02")
                                    else 0
                                )
                            ),
                            frame_period_ns=camera.exposure.frame_period_ns
                            * (3 if camera.camera_role is m.CameraRole.AUXILIARY else 1),
                        ),
                    )
                    for camera in session.spec.scene_definition.cameras
                ),
            ),
        )
        session._steps = iter(session.source.generate(session.spec))
        session.step()
        self.assertEqual(recorder.calls[:3], ["start", "main", "tick"])
        while not session.ended:
            session.step()
        self.assertEqual(session.main_count, 4)
        self.assertEqual(session.auxiliary_count, 2)
        self.assertEqual(len({frame.station_id for frame in recorder.frames}), 2)
        self.assertEqual(recorder.calls[-1], "end")
        output = [
            json.loads(row)
            for row in (self.root / "run/device-output.jsonl").read_text().splitlines()
        ]
        for row in output:
            self.assertGreaterEqual(row["device_ms"], 0)
            self.assertTrue(all(call["algorithm_ms"] >= 0 for call in row["calls"]))
        negatives = [
            call["output"]["detections"][0]
            for row in output
            for call in row["calls"]
            if call["kind"] == "main"
        ]
        self.assertTrue(all(item["processing_status"] == "processed" for item in negatives))
        self.assertTrue(all(not item["observations"] for item in negatives))

    def test_video_disabled_keeps_plugin_started_ticks_and_can_resume(self) -> None:
        recorder = Recorder()
        session = self.session(recorder, video_enabled=False)
        self.assertEqual(recorder.calls, ["start"])
        self.assertEqual(len(session.health), len(session.assembly.bindings))
        with patch("PIL.Image.frombytes", side_effect=AssertionError("pixels forbidden")):
            session.step()
        self.render.assert_not_called()
        self.assertEqual(recorder.frames, [])
        self.assertEqual(recorder.calls, ["start", "tick"])
        before = session.snapshot()
        self.assertEqual(before, session.snapshot())
        self.assertEqual(len(recorder.ticks), 1)
        session.set_video(True)
        session.step()
        self.assertTrue(recorder.frames)
        self.assertTrue(self.render.called)
        while not session.ended:
            session.step()
        self.assertTrue(recorder.ticks[-1].stream_ended)
        calls_before = list(recorder.calls)
        session.step()
        self.assertEqual(recorder.calls, calls_before)
        session.close()
        session.close()
        self.assertEqual(recorder.closed_count, 1)
        summary = json.loads((self.root / "run/summary.json").read_text())
        self.assertEqual(summary["state"], "complete")
        self.assertFalse(summary["shutdown_errors"])
        self.assertTrue(all(item["state"] == "stopped" for item in summary["shutdown"]))

    def test_simultaneous_main_is_dispatched_before_auxiliary_in_reversed_input(self) -> None:
        recorder = Recorder()
        session = self.session(recorder)
        sample = next(session._steps)
        frames = tuple(
            sorted(sample.video.frames, key=lambda frame: frame.camera_role is m.CameraRole.MAIN)
        )
        self.assertIs(frames[0].camera_role, m.CameraRole.AUXILIARY)
        session._steps = iter((replace(sample, video=replace(sample.video, frames=frames)),))
        session.step()
        self.assertEqual(recorder.calls, ["start", "main", "auxiliary", "tick"])

    def test_default_probe_reports_no_trusted_calibration_and_never_fake_success(self) -> None:
        session = self.session()
        while not session.ended:
            session.step()
        rows = [
            json.loads(row)
            for row in (self.root / "run/device-output.jsonl").read_text().splitlines()
        ]
        outputs = [call["output"] for row in rows for call in row["calls"]]
        self.assertTrue(all(not output["detections"] for output in outputs))
        self.assertTrue(all(not output["calibration_updates"] for output in outputs))
        events = [event for output in outputs for event in output["events"]]
        self.assertEqual(len(events), session.main_count)
        self.assertTrue(all("no_trusted_calibration" in event["reason_codes"] for event in events))
        self.assertTrue(all(event["envelope"]["event_type"] == "diagnostic" for event in events))

    def test_algorithm_failures_close_plugin_modules_and_evidence(self) -> None:
        for stage in ("start", "frame"):
            with self.subTest(stage=stage):
                recorder = Recorder(fail_start=stage == "start", fail_frame=stage == "frame")
                output = self.root / stage
                with self.assertRaisesRegex(RuntimeError, f"{stage} failed"):
                    session = DeviceSession(self.scenario(), output, algorithm=recorder)
                    session.step()
                self.assertEqual(recorder.closed_count, 1)
                summary = json.loads((output / "summary.json").read_text())
                self.assertEqual(summary["state"], "failed")
                self.assertIn(f"{stage} failed", summary["error"])
                self.assertFalse(summary["shutdown_errors"])
                self.assertTrue(all(item["state"] == "stopped" for item in summary["shutdown"]))

    def test_input_builder_does_not_read_packet_or_truth_exit(self) -> None:
        session = self.session(video_enabled=False)
        sample = next(session._steps)

        class OrdinaryExitOnly:
            video = sample.video

            @property
            def packets(self) -> None:
                raise AssertionError("device input must not read virtual packets")

            @property
            def truth(self) -> None:
                raise AssertionError("device input must not read truth")

        online = session._input(cast(s.ScenarioStep, OrdinaryExitOnly()))
        self.assertEqual(online.frames, ())
        self.assertEqual(online.time.time_ns, sample.video.sample_time.time_ns)

    def test_registration_and_online_contracts_cannot_reach_scene_types(self) -> None:
        recorder = Recorder()
        session = self.session(recorder)
        registration = recorder.registration
        self.assertIsNotNone(registration)
        assert registration is not None
        self.assertEqual(len(registration.stations), 1)
        binding = session.source.station_sessions[0]
        self.assertEqual(registration.stations[0].station_session_id, binding.image_session_id)
        encoded = json.loads((self.root / "run/device-registration.json").read_text())
        self.assertNotIn("virtual_session_id", str(encoded))
        seen: set[Any] = set()

        def check(annotation: Any) -> None:
            if annotation in seen:
                return
            seen.add(annotation)
            self.assertNotEqual(getattr(annotation, "__module__", None), s.__name__)
            for argument in get_args(annotation):
                check(argument)
            if isinstance(annotation, type) and is_dataclass(annotation):
                hints = get_type_hints(annotation)
                for item in fields(annotation):
                    check(hints[item.name])

        for annotation in (
            DeviceRegistration,
            DeviceInputStep,
            DeviceAlgorithmOutput,
            m.CameraFrame,
        ):
            check(annotation)
        session.step()
        text = (self.root / "run/device-input.jsonl").read_text()
        for forbidden in ("truth_object_id", "actual_camera", "perturbation", "virtual_session"):
            self.assertNotIn(forbidden, text)

    def test_mismatched_frame_fails_before_plugin_mutation(self) -> None:
        recorder = Recorder()
        session = self.session(recorder)
        sample = next(session._steps)
        bad_frame = replace(sample.video.frames[0], station_session_id=m.SessionId("wrong"))
        session._steps = iter((replace(sample, video=replace(sample.video, frames=(bad_frame,))),))
        with self.assertRaisesRegex(ValueError, "image session"):
            session.step()
        self.assertEqual(recorder.frames, [])
        self.assertEqual(recorder.closed_count, 1)

    def test_step_waits_for_plugin_and_rejects_overlapping_controls(self) -> None:
        recorder = Recorder()
        session = self.session(recorder)
        original = recorder.process_main

        def process(frame: m.CameraFrame, now: m.ProcessingTime) -> DeviceAlgorithmOutput:
            with self.assertRaisesRegex(RuntimeError, "already in progress"):
                session.step()
            with self.assertRaisesRegex(RuntimeError, "between completed steps"):
                session.set_video(False)
            self.assertEqual(session.step_count, 0)
            return original(frame, now)

        with patch.object(recorder, "process_main", side_effect=process):
            session.step()
        self.assertEqual(session.step_count, 1)
        self.assertEqual(recorder.calls[-1], "tick")

    def test_cli_loads_explicit_plugin_and_finishes_without_video(self) -> None:
        scenario = self.scenario()
        with (
            patch(
                "sys.argv",
                [
                    "chongming-device",
                    "--scenario",
                    str(scenario),
                    "--output",
                    str(self.root / "cli"),
                    "--run",
                    "--no-video",
                    "--algorithm",
                    "chongming.device_development:FrameInspector",
                ],
            ),
            patch("builtins.print"),
            patch("chongming.development.time.sleep"),
        ):
            main()
        self.render.assert_not_called()
        summary = json.loads((self.root / "cli/summary.json").read_text())
        self.assertEqual(summary["state"], "complete")
        self.assertEqual(summary["image_frames"], 0)
        self.assertFalse(summary["shutdown_errors"])

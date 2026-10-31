"""Explicit test-only policies and source-to-device/center runtime checks."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np

from chongming.contracts import device as d
from chongming.contracts import model as m
from chongming.contracts import scene as s
from chongming.development import CenterSession, main
from chongming.edge_runtime import EdgeRun, load_device_policies, validate_device_policies
from chongming.modules.device import OPENCV_CALIBRATION_ESTIMATOR_ID
from chongming.rendering.godot import close_default_renderer
from chongming.scenarios.authoring import demo_authoring, save_authoring
from chongming.scenarios.run import _json
from chongming.scenarios.serialization import load_scenario
from tests import test_godot_visualization as transport
from tests.test_center_development import Recorder
from tests.test_center_ui import ROOT, FakeRenderer, observed_workers
from tests.test_detection import detection_policy, tracking_policy
from tests.test_device_rays import PIXEL_COVARIANCE
from tests.test_device_rays import policy as ray_policy
from tests.test_scene_run import normalize


def policies(
    configs: tuple[m.StationConfig, ...], *, known_pixels: bool = True
) -> dict[m.StationId, d.DevicePolicy]:
    # Development assumptions for these tests, not CLI/runtime defaults.
    return {
        config.station_id: d.DevicePolicy(
            detection=detection_policy(
                background_warmup_frame_count=1, background_learning_rate=0.0
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
            rays=replace(
                ray_policy(),
                fallback_centroid_covariance_px2=PIXEL_COVARIANCE if known_pixels else None,
            ),
        )
        for config in configs
    }


def no_reference(frame: m.CameraFrame, config: m.StationConfig) -> d.ReferenceObservationBatch:
    return d.ReferenceObservationBatch(
        auxiliary_frame_ref=m.FrameRef(
            station_id=frame.station_id,
            station_session_id=frame.station_session_id,
            camera_role=frame.camera_role,
            frame_id=frame.frame_id,
        ),
        capture_time=frame.capture_time,
        processing_status=m.ProcessingStatus.PROCESSED,
        observations=(),
        reason_codes=("test_reference_unavailable",),
    )


def test_pixels(scene: s.LogicalSceneSnapshot, camera: s.CameraStateTruth) -> tuple[bytes, float]:
    image = np.zeros(
        (camera.actual_intrinsics.image_height_px, camera.actual_intrinsics.image_width_px, 3),
        dtype=np.uint8,
    )
    if camera.camera_role is m.CameraRole.MAIN and scene.sample_time.time_ns > 0:
        image[100:116, 120:136] = 255
    return image.tobytes(), 0.0


def rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def real_scene(path: Path) -> s.ScenarioSpec:
    document = json.loads((ROOT / "scenarios/center_clean.json").read_text())
    document.update(duration_s=0.81, fps=5, vegetation=[], wind=None, clouds=[])
    # The target enters the cameras after background warmup. This is a test
    # source, not a deployment profile or a measured detector accuracy claim.
    document["targets"][0]["motion"].update(
        position_enu_m=[-65, 62, 20], velocity_enu_mps=[35, 0, 0]
    )
    save_authoring(document, path)
    return load_scenario(path)


class EdgeRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        close_default_renderer()
        self.addCleanup(close_default_renderer)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "scene.json"
        document = demo_authoring(stations=2, duration_s=0.16, fps=10)
        document["targets"] = []
        document["stations"][1].update(fps=20, start_s=0.025)
        save_authoring(document, self.path)
        self.spec = load_scenario(self.path)
        self.policies = policies(self.spec.station_configs)
        self.worker = FakeRenderer()
        for guard in (
            patch("chongming.rendering.godot.GodotRenderer", return_value=self.worker),
            patch("chongming.modules.scene.render_rgb8", side_effect=test_pixels),
            patch(
                "chongming.modules.device.ReferenceRecognitionModule.recognize",
                side_effect=no_reference,
            ),
            patch("chongming.development._provenance", return_value={}),
        ):
            guard.start()
            self.addCleanup(guard.stop)

    def session(
        self,
        mode: int,
        *,
        algorithm: Recorder | None = None,
        video: bool = False,
        name: str = "run",
    ) -> CenterSession:
        return CenterSession(
            self.path,
            self.root / name,
            mode=mode,
            algorithm=algorithm,
            device_policies=self.policies,
            video_enabled=video,
        )

    def test_policy_file_is_typed_complete_and_validated_without_allocating_a_worker(self) -> None:
        path = self.root / "policies.json"
        path.write_text(_json(self.policies))
        self.assertEqual(load_device_policies(path, self.spec.station_configs), self.policies)
        key = self.spec.station_configs[0].station_id
        original = self.policies[key]
        for bad in (
            {},
            {**self.policies, m.StationId("extra"): original},
            {
                **self.policies,
                key: replace(
                    original,
                    detection=replace(original.detection, background_history_frame_count=2**31),
                ),
            },
            {
                **self.policies,
                key: replace(
                    original,
                    rays=replace(
                        original.rays,
                        unmodelled_high_frequency_jitter_covariance_rad2=((1e-6, 0.0), (0.0, 1e-6)),
                        residual_independence_basis=None,
                    ),
                ),
            },
            {
                **self.policies,
                key: replace(
                    original, detection=replace(original.detection, background_learning_rate=None)
                ),
            },
            {
                **self.policies,
                key: replace(original, tracking=replace(original.tracking, association_gate_px=-1)),
            },
            {
                **self.policies,
                key: replace(
                    original,
                    calibration=replace(original.calibration, estimator_id="unimplemented"),
                ),
            },
            {
                **self.policies,
                key: replace(
                    original,
                    rays=replace(
                        original.rays, fallback_centroid_covariance_px2=((-1.0, 0.0), (0.0, 1.0))
                    ),
                ),
            },
        ):
            with self.subTest(keys=list(bad)):
                with self.assertRaises(ValueError):
                    validate_device_policies(bad, self.spec.station_configs)
                with self.assertRaises(ValueError):
                    CenterSession(self.path, self.root / "invalid", mode=2, device_policies=bad)
                self.assertFalse((self.root / "invalid").exists())
        path.write_text('{"duplicate":{},"duplicate":{}}')
        with self.assertRaisesRegex(ValueError, "duplicate"):
            load_device_policies(path, self.spec.station_configs)

    def test_mode2_never_initializes_center_or_calls_the_virtual_generator(self) -> None:
        with patch("chongming.reference.create_algorithm", side_effect=AssertionError("no center")):
            session = self.session(2)
            self.addCleanup(session.close)
            self.assertIsNone(session.algorithm)
            with patch.object(
                session.source.virtual_generator,
                "generate",
                side_effect=AssertionError("no virtual events"),
            ):
                while not session.ended:
                    session.step()
        self.assertGreater(session.step_count, 0)
        self.assertEqual(session.ray_count, 0)
        self.assertFalse((self.root / "run/center-input.jsonl").exists())
        self.assertFalse((self.root / "run/center-output.jsonl").exists())
        self.assertFalse((self.root / "run/video").exists())
        journal = rows(self.root / "run/edge-output.jsonl")
        inputs = [row["value"] for row in journal if row["kind"] == "input_frame"]
        self.assertTrue(inputs)
        self.assertTrue(all(len(frame["pixels_sha256"]) == 64 for frame in inputs))
        for row in rows(self.root / "run/online.jsonl"):
            self.assertEqual(row["frames"], [])
            self.assertEqual(row["packets"]["rays"], [])
            self.assertEqual(row["packets"]["calibration_solutions"], [])
        manifest = json.loads((self.root / "run/manifest.json").read_text())
        self.assertEqual(manifest["input_source"], "rendered_images")
        self.assertFalse(manifest["virtual_events_enabled"] or manifest["center_algorithm_started"])

    def test_mode4_inputs_equal_journaled_device_events_and_completed_frames(self) -> None:
        recorder = Recorder()
        session = self.session(4, algorithm=recorder)
        self.addCleanup(session.close)
        while not session.ended:
            session.step()
        journal = rows(self.root / "run/edge-output.jsonl")
        expected = []
        for row in journal:
            if row["kind"] == "main_result":
                expected.extend(row["value"]["events"])
            if row["kind"] == "calibration_update":
                expected.extend((row["value"]["state_event"], *row["value"]["diagnostics"]))
        consumed = [
            json.loads(_json(received.event))
            for step in recorder.inputs
            for received in step.events
        ]

        def identity(event: dict[str, Any]) -> tuple[str, str, int]:
            value = event["envelope"]
            return value["station_id"], value["station_session_id"], value["sequence_no"]

        self.assertEqual(sorted(expected, key=identity), sorted(consumed, key=identity))
        self.assertEqual(len({identity(event) for event in consumed}), len(consumed))
        self.assertTrue(
            any(
                isinstance(received.event, m.DiagnosticEvent)
                and received.event.capture_time is None
                for step in recorder.inputs
                for received in step.events
            )
        )
        completed = [row["value"]["progress"] for row in journal if row["kind"] == "main_result"]
        reported = [
            json.loads(_json(progress))
            for step in recorder.inputs
            for progress in step.progress
            if not progress.stream_ended
        ]
        self.assertEqual(completed, reported)
        self.assertEqual(len(recorder.inputs[-1].progress), 2)
        self.assertTrue(all(progress.stream_ended for progress in recorder.inputs[-1].progress))
        sessions = {binding.image_session_id for binding in session.source.station_sessions}
        self.assertEqual(
            {
                received.event.envelope.station_session_id
                for step in recorder.inputs
                for received in step.events
            },
            sessions,
        )
        self.assertNotIn("truth_object_id", (self.root / "run/center-input.jsonl").read_text())

    def test_ui_shares_transport_and_logs_untimed_control_diagnostics_without_fake_exposure(
        self,
    ) -> None:
        session = self.session(2)
        session.run_ui(tick_hz=240)
        increments = [
            state["algorithm_output"] for state in self.worker.sent if "algorithm_output" in state
        ]
        self.assertEqual(
            [int(update["update_id"]) for update in increments],
            list(range(1, session.step_count + 2)),
        )
        self.assertTrue(all(update["center_events"] is None for update in increments))
        self.assertTrue(all(state["execution_mode"] == 2 for state in self.worker.sent))
        self.assertTrue(all(not images for images in self.worker.images))
        self.assertTrue(
            any("not an exposure" in entry["message"] for entry in self.worker.sent[-1]["log"])
        )
        timed = []
        for row in rows(self.root / "run/edge-output.jsonl"):
            if row["kind"] == "main_result":
                for event in row["value"]["events"]:
                    if event["envelope"]["event_type"] == "diagnostic":
                        capture = event["capture_time"]
                        timed.append(
                            (
                                event["envelope"]["station_id"],
                                (capture["exposure_start_ns"] + capture["exposure_end_ns"]) // 2,
                                event["category"],
                                tuple(event["reason_codes"]),
                            )
                        )
        displayed = [
            (
                station["station_id"],
                int(diagnostic["capture_time_ns"]),
                diagnostic["category"],
                tuple(diagnostic["reason_codes"]),
            )
            for update in increments
            for station in update["edge_events"]
            for diagnostic in station["diagnostics"]
        ]
        self.assertEqual(sorted(displayed), sorted(timed))
        self.assertTrue(self.worker.closed)

    def test_video_off_does_not_disable_algorithm_pixels_and_failed_pixels_still_complete(
        self,
    ) -> None:
        recorder = Recorder()
        session = self.session(4, algorithm=recorder)
        self.addCleanup(session.close)
        self.assertTrue(session.source.video_enabled)
        session.set_video(False)
        self.assertTrue(session.source.video_enabled)
        with patch("chongming.modules.scene.render_rgb8", return_value=(b"bad pixels", 0.0)):
            session.step()
        self.assertTrue(recorder.inputs[0].progress)
        self.assertTrue(
            any(
                isinstance(received.event, m.DiagnosticEvent)
                and received.event.category == "main_processing_failed"
                for received in recorder.inputs[0].events
            )
        )

    def test_center_failure_preserves_edge_results_and_stops_without_virtual_fallback(self) -> None:
        recorder = Recorder()
        session = self.session(4, algorithm=recorder)
        with (
            patch.object(recorder, "process", side_effect=RuntimeError("volunteer failure")),
            self.assertRaisesRegex(RuntimeError, "volunteer failure"),
        ):
            session.step()
        self.assertTrue(session.closed and recorder.closed and self.worker.closed)
        self.assertTrue(
            any(row["kind"] == "main_result" for row in rows(self.root / "run/edge-output.jsonl"))
        )
        self.assertEqual(
            json.loads((self.root / "run/summary.json").read_text())["state"], "failed"
        )

    def test_cli_modes_reject_missing_or_unused_policy_and_mode2_plugins_before_output(
        self,
    ) -> None:
        for options in (
            ["--mode", "2"],
            ["--mode", "4"],
            ["--mode", "2", "--algorithm", "not.imported:factory"],
            ["--device-policy", "unused.json"],
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
                        str(self.root / "bad-cli"),
                        *options,
                    ],
                ),
                patch("sys.stderr", new=io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as context:
                    main()
                self.assertEqual(context.exception.code, 2)
                self.assertFalse((self.root / "bad-cli").exists())

    def test_auxiliary_throttle_is_not_redelivered_to_the_center(self) -> None:
        self.policies = {
            key: replace(
                policy, calibration=replace(policy.calibration, update_period_ns=1_000_000_000)
            )
            for key, policy in self.policies.items()
        }
        recorder = Recorder()
        session = self.session(4, algorithm=recorder)
        self.addCleanup(session.close)
        while not session.ended:
            session.step()
        control = [
            received.event
            for step in recorder.inputs
            for received in step.events
            if isinstance(received.event, m.CalibrationStateEvent)
        ]
        self.assertEqual(len(control), 2)

    def test_partial_device_results_are_flushed_before_a_later_device_failure(self) -> None:
        session = self.session(2)
        self.addCleanup(session.close)
        station = session.assembly.stations[1]
        with patch.object(
            station.device, "process_main", side_effect=RuntimeError("later device failure")
        ):
            # Use a simultaneous group for this failure-order check.
            first = next(
                step
                for step in session._steps
                if any(frame.camera_role is m.CameraRole.MAIN for frame in step.video.frames)
            )
            frame = next(
                frame for frame in first.video.frames if frame.camera_role is m.CameraRole.MAIN
            )
            group = tuple(
                replace(
                    frame,
                    station_id=item.config.station_id,
                    station_session_id=item.device.station_session_id,
                    camera_config_ref=item.config.config_digest,
                    camera_role=m.CameraRole.MAIN,
                )
                for item in session.assembly.stations
            )
            journal = io.StringIO()
            edge = EdgeRun(session.assembly.stations, journal)
            with self.assertRaisesRegex(RuntimeError, "later device failure"):
                edge.process(
                    group,
                    m.ProcessingTime(
                        clock_domain_id=frame.capture_time.clock_domain_id,
                        time_ns=first.video.sample_time.time_ns,
                    ),
                )
            records = [json.loads(line) for line in journal.getvalue().splitlines()]
            self.assertEqual(sum(row["kind"] == "main_result" for row in records), 1)
            self.assertEqual(records[-1]["kind"], "step_failed")
            self.assertFalse(any(row["kind"] == "step_complete" for row in records))

    def test_non_graphical_cli_mode2_uses_explicit_policies_and_never_waits_for_input(self) -> None:
        config = self.root / "device.json"
        config.write_text(_json(self.policies))
        with (
            patch.object(
                sys,
                "argv",
                [
                    "chongming-center",
                    "--mode",
                    "2",
                    "--run",
                    "--scenario",
                    str(self.path),
                    "--device-policy",
                    str(config),
                    "--output",
                    str(self.root / "cli"),
                    "--tick-hz",
                    "240",
                ],
            ),
            patch("builtins.input", side_effect=AssertionError("no input")),
            patch("sys.stdout", new=io.StringIO()),
        ):
            main()
        self.assertEqual(
            json.loads((self.root / "cli/summary.json").read_text())["state"], "complete"
        )
        self.assertIsNone(json.loads((self.root / "cli/manifest.json").read_text())["algorithm"])

    def test_cli_mode4_loads_an_explicit_center_plugin(self) -> None:
        config = self.root / "device.json"
        config.write_text(_json(self.policies))
        with (
            patch.object(
                sys,
                "argv",
                [
                    "chongming-center",
                    "--mode",
                    "4",
                    "--run",
                    "--algorithm",
                    "tests.test_center_development:Recorder",
                    "--scenario",
                    str(self.path),
                    "--device-policy",
                    str(config),
                    "--output",
                    str(self.root / "cli"),
                    "--tick-hz",
                    "240",
                ],
            ),
            patch("builtins.input", side_effect=AssertionError("no input")),
            patch("sys.stdout", new=io.StringIO()),
        ):
            main()
        self.assertEqual(
            json.loads((self.root / "cli/manifest.json").read_text())["algorithm"], "Recorder"
        )
        self.assertTrue(rows(self.root / "cli/center-input.jsonl")[-1]["stream_ended"])

    def test_missing_required_images_fail_instead_of_describing_virtual_inputs(self) -> None:
        session = self.session(2)
        session.source.video_enabled = False
        with self.assertRaisesRegex(RuntimeError, "every scheduled sensor image"):
            session.step()
        self.assertTrue(session.closed)
        self.assertEqual(session.step_count, 0)
        self.assertEqual(
            json.loads((self.root / "run/summary.json").read_text())["state"], "failed"
        )

    def test_edge_transport_failure_preserves_raw_events_and_stops_before_a_second_step(
        self,
    ) -> None:
        recorder = Recorder()
        session = self.session(4, algorithm=recorder)

        def disconnect(state: dict[str, Any]) -> list[dict[str, Any]]:
            if "algorithm_output" in state:
                self.assertEqual(
                    rows(self.root / "run/edge-output.jsonl")[-1]["kind"], "step_complete"
                )
                self.assertEqual(len(rows(self.root / "run/center-output.jsonl")), 1)
                raise ConnectionError("edge transport disconnected")
            return []

        self.worker.reply = disconnect
        with self.assertRaisesRegex(ConnectionError, "edge transport disconnected"):
            session.run_ui(tick_hz=240)
        self.assertEqual(session.step_count, 1)
        self.assertEqual(len(self.worker.sent), 2)
        self.assertTrue(session.closed and recorder.closed and self.worker.closed)

    def test_video_flag_changes_evidence_not_device_or_center_inputs(self) -> None:
        consumed = []
        hashes = []
        with patch("chongming.rendering.godot.GodotRenderer", side_effect=FakeRenderer):
            for enabled in (False, True):
                recorder = Recorder()
                session = self.session(
                    4, algorithm=recorder, video=enabled, name=f"video-{enabled}"
                )
                try:
                    while not session.ended:
                        session.step()
                finally:
                    session.close()
                sessions = [
                    str(binding.image_session_id) for binding in session.source.station_sessions
                ]
                consumed.append(normalize(json.loads(_json(recorder.inputs)), sessions))
                journal = rows(self.root / f"video-{enabled}/edge-output.jsonl")
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
                self.assertEqual((self.root / f"video-{enabled}/video.json").exists(), enabled)
        self.assertEqual(consumed[0], consumed[1])
        self.assertEqual(hashes[0], hashes[1])

    def test_zero_main_samples_only_retire_streams_without_inventing_exposures(self) -> None:
        session = self.session(4, algorithm=Recorder())
        self.addCleanup(session.close)
        session.spec = replace(
            session.spec,
            scene_definition=replace(
                session.spec.scene_definition,
                cameras=tuple(
                    replace(
                        camera,
                        exposure=replace(camera.exposure, first_exposure_start_ns=1_000_000_000),
                    )
                    for camera in session.spec.scene_definition.cameras
                ),
            ),
        )
        session._steps = iter(session.source.generate(session.spec))
        result = session.step()
        self.assertEqual(result["logical_steps"], 0)
        self.assertTrue(session.ended)
        assert isinstance(session.algorithm, Recorder)
        self.assertTrue(
            all(
                progress.completed_capture_time is None and progress.stream_ended
                for progress in session.algorithm.inputs[0].progress
            )
        )


@unittest.skipUnless(transport.GODOT, "Godot with real graphics is required")
class EdgeGodotTests(unittest.TestCase):
    def test_real_images_feed_device_and_center_and_new_modes_reset_godot(self) -> None:
        with tempfile.TemporaryDirectory() as directory, observed_workers() as workers:
            root = Path(directory)
            path = root / "scene.json"
            spec = real_scene(path)
            for mode, known_pixels in ((2, True), (4, True), (4, False)):
                output = root / f"mode-{mode}-{known_pixels}"
                session = CenterSession(
                    path,
                    output,
                    mode=mode,
                    video_enabled=mode == 4 and known_pixels,
                    device_policies=policies(spec.station_configs, known_pixels=known_pixels),
                )
                with patch.object(
                    session.source.virtual_generator,
                    "generate",
                    side_effect=AssertionError("virtual generation forbidden"),
                ):
                    session.run_ui(tick_hz=240)
                _, reports, states = workers[-1]
                self.assertEqual(reports[0]["evaluation"]["edge"]["outputs"], 0)
                self.assertEqual(reports[0]["evaluation"]["center"]["outputs"], 0)
                if known_pixels:
                    self.assertGreater(session.ray_count, 0)
                    self.assertGreater(reports[-1]["evaluation"]["edge"]["matched"], 0)
                else:
                    self.assertEqual(session.ray_count, 0)
                    self.assertIsNone(reports[-1]["evaluation"]["edge"]["mean_error"])
                    self.assertEqual(reports[-1]["evaluation"]["center"]["outputs"], 0)
                    journal = rows(output / "edge-output.jsonl")
                    self.assertTrue(
                        any(
                            row["kind"] == "calibration_update"
                            and row["value"]["solution"] is not None
                            for row in journal
                        )
                    )
                    self.assertTrue(
                        any(
                            row["kind"] == "main_result"
                            and row["value"]["detections"]["observations"]
                            for row in journal
                        )
                    )
                    self.assertIn(
                        "unknown_centroid_covariance_px2",
                        (output / "edge-output.jsonl").read_text(),
                    )
                self.assertEqual(reports[-1]["evaluation"]["edge"]["outputs"], session.ray_count)
                self.assertIn(f"模式 {mode}", reports[-1]["info_text"])
                self.assertTrue(all(state["execution_mode"] == mode for state in states))
                self.assertTrue(all(all(report["controls"]) for report in reports))
                self.assertNotIn("SCRIPT ERROR", reports[-1]["worker_log"])
                if mode == 2:
                    self.assertEqual(reports[-1]["evaluation"]["center"]["outputs"], 0)
                elif known_pixels:
                    self.assertGreater(reports[-1]["evaluation"]["center"]["outputs"], 0)
                    self.assertGreater(reports[-1]["evaluation"]["center"]["matched"], 0)
                    events = [
                        received["event"]
                        for row in rows(output / "center-input.jsonl")
                        for received in row["events"]
                        if received["event"]["envelope"]["event_type"] == "ray"
                    ]
                    ids = {
                        (
                            event["envelope"]["station_id"],
                            event["envelope"]["station_session_id"],
                            event["envelope"]["sequence_no"],
                        )
                        for event in events
                    }
                    for row in rows(output / "center-output.jsonl"):
                        for measurement in row["measurements"]:
                            self.assertTrue(
                                set(map(tuple, measurement["supporting_ray_event_ids"])) <= ids
                            )
                    self.assertTrue((output / "video.json").exists())
                    self.assertTrue(any(row["frames"] for row in rows(output / "online.jsonl")))
                    self.assertTrue(
                        any(
                            (state.get("frame") or {}).get("monitor", {}).get("frame_truth")
                            for state in states
                        )
                    )
            self.assertEqual(len(workers), 3)


if __name__ == "__main__":
    unittest.main()

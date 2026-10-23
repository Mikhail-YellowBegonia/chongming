"""Finite JSON acceptance, scheduling and evidence contracts; no pixel goldens."""

from __future__ import annotations

import itertools
import json
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from PIL import Image

from chongming.contracts import scene as s
from chongming.contracts.model import CameraRole
from chongming.modules.scene import _scheduled_exposures
from chongming.rendering.godot import render_rgb8
from chongming.scenarios.authoring import save_authoring
from chongming.scenarios.demo import demo_source, demo_spec, optical_rotation
from chongming.scenarios.run import run_scene
from chongming.scenarios.serialization import (
    load_compiled_scenario,
    save_compiled_scenario,
    scenario_from_dict,
    scenario_to_dict,
)


def short_spec() -> s.ScenarioSpec:
    spec = demo_spec(station_count=2, fps=4)
    cameras = tuple(
        replace(
            c,
            metadata=replace(c.metadata, profile_id="experimental-test"),
            actual_intrinsics=replace(
                c.actual_intrinsics,
                image_width_px=64,
                image_height_px=48,
                focal_x_px=50.0,
                focal_y_px=50.0,
                principal_x_px=32.0,
                principal_y_px=24.0,
            ),
        )
        for c in spec.scene_definition.cameras
    )
    return replace(
        spec,
        scene_definition=replace(spec.scene_definition, cameras=cameras),
        timeline=replace(spec.timeline, end_time_ns=600_000_000),
    )


def records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def normalize(value: Any, sessions: list[str]) -> Any:
    if isinstance(value, dict):
        return {k: normalize(v, sessions) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v, sessions) for v in value]
    if isinstance(value, str):
        for index, session in enumerate(sessions):
            value = value.replace(session, f"session-{index}")
    return value


class SceneRunAcceptanceTests(unittest.TestCase):
    def test_external_terrain_finite_run_preserves_online_and_truth_evidence(self) -> None:
        scenes = Path(__file__).resolve().parents[1] / "scenarios"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outputs = []
            for name in ("heightfield", "heightfield_asset"):
                document = json.loads((scenes / f"{name}.json").read_text())
                document["scenario_id"] = "terrain-parity"
                document["fps"] = 1
                if name.endswith("_asset"):
                    document["terrain"]["asset"]["uri"] = (scenes / "assets/ridge.json").as_uri()
                path = root / f"{name}.json"
                save_authoring(document, path)
                output = root / name
                summary = run_scene(path, output, video_enabled=False)
                self.assertEqual(summary["counts"]["gpu_submissions"], 0)
                self.assertEqual(summary["counts"]["logical_frames"], 24)
                manifest = json.loads((output / "manifest.json").read_text())
                terrain = manifest["scenario"]["scenario"]["scene_definition"]["terrain"]
                self.assertEqual(
                    terrain["heightfield_asset_digest"] is not None, name.endswith("_asset")
                )
                sessions = [
                    binding[key]
                    for binding in manifest["station_sessions"]
                    for key in ("image_session_id", "virtual_session_id")
                ]
                truth = records(output / "truth.jsonl")
                for sample in truth:
                    terrain = sample["logical_scene"]["terrain"]
                    terrain["heightfield_asset_uri"] = None
                    terrain["heightfield_asset_digest"] = None
                outputs.append(
                    normalize(
                        {
                            "online": records(output / "online.jsonl"),
                            "truth": truth,
                        },
                        sessions,
                    )
                )
            self.assertEqual(outputs[0], outputs[1])
            # A modified asset fails before output creation or source startup.
            document["terrain"]["asset"]["sha256"] = "0" * 64
            save_authoring(document, path)
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                run_scene(path, root / "invalid", video_enabled=False)
            self.assertFalse((root / "invalid").exists())

    def test_json_recreates_all_finite_streams_with_separate_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scenario = root / "editable.json"
            save_compiled_scenario(short_spec(), scenario)
            summaries = [run_scene(scenario, root / name) for name in ("first", "second")]
            self.assertEqual(summaries[0]["counts"], summaries[1]["counts"])
            self.assertEqual(summaries[0]["counts"]["rendered_frames"], 12)
            self.assertEqual(summaries[0]["counts"]["gpu_submissions"], 12)
            self.assertGreater(summaries[0]["counts"]["ray_events"], 0)
            normalized = []
            for name in ("first", "second"):
                output = root / name
                manifest = json.loads((output / "manifest.json").read_text())
                self.assertEqual(manifest["scope"], "virtual-subsystem")
                self.assertEqual(len(manifest["stream_mapping"]), 4)
                self.assertTrue(manifest["source_digest"])
                self.assertTrue(manifest["dependency_lock_digest"])
                online = records(output / "online.jsonl")
                truth = records(output / "truth.jsonl")
                self.assertEqual(len(online), 3)
                self.assertNotIn("truth_object_id", json.dumps(online))
                self.assertTrue(any(record["rays"] for record in truth))
                frames = [f for record in online for f in record["frames"]]
                self.assertEqual(len(frames), 12)
                self.assertEqual({f["camera_role"] for f in frames}, {"main", "auxiliary"})
                with Image.open(output / frames[0]["path"]) as image:
                    self.assertEqual(image.size, (64, 48))
                    self.assertEqual(image.mode, "RGB")
                sessions = [
                    session[k]
                    for session in manifest["station_sessions"]
                    for k in ("image_session_id", "virtual_session_id")
                ]
                normalized.append(normalize({"online": online, "truth": truth}, sessions))
            # Reproducibility on the same host, not a fixed image golden.
            self.assertEqual(normalized[0], normalized[1])
            with self.assertRaises(FileExistsError):
                run_scene(scenario, root / "first")
            edited = json.loads(scenario.read_text())
            edited["scenario"]["scene_definition"]["flying_objects"] = []
            edited["scenario"]["timeline"]["end_time_ns"] = 300_000_000
            scenario.write_text(json.dumps(edited))
            summary = run_scene(scenario, root / "edited")
            self.assertEqual(summary["counts"]["rendered_frames"], 8)
            self.assertEqual(summary["counts"]["ray_events"], 0)

    def test_reduced_pixels_preserve_nonempty_events_and_report_every_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "scenario.json"
            save_compiled_scenario(short_spec(), path)
            full = run_scene(path, root / "full")
            reduced = run_scene(path, root / "reduced", render_every=2)
            self.assertEqual(full["counts"]["ray_events"], reduced["counts"]["ray_events"])
            self.assertEqual(reduced["counts"]["logical_frames"], 12)
            self.assertEqual(reduced["counts"]["gpu_submissions"], 8)
            timing = records(root / "reduced" / "timing.jsonl")
            self.assertEqual(len(timing), 3)
            skipped = timing[1]
            self.assertFalse(skipped["rendered"])
            self.assertEqual(skipped["stages"]["gpu_ms"], 0.0)
            self.assertEqual(skipped["stages"]["gpu_submissions"], 0)
            self.assertEqual(skipped["stages"]["render_ms"], 0.0)
            self.assertGreater(skipped["stages"]["total_ms"], 0)
            online = records(root / "reduced" / "online.jsonl")
            self.assertEqual(len(online[1]["skipped_frame_ids"]), 4)
            self.assertEqual(online[1]["frames"], [])
            truths = [records(root / name / "truth.jsonl") for name in ("full", "reduced")]
            for i, name in enumerate(("full", "reduced")):
                manifest = json.loads((root / name / "manifest.json").read_text())
                sessions = [
                    x[k]
                    for x in manifest["station_sessions"]
                    for k in ("image_session_id", "virtual_session_id")
                ]
                truths[i] = normalize(truths[i], sessions)
            self.assertEqual(truths[0], truths[1])
            packets = []
            for name in ("full", "reduced"):
                manifest = json.loads((root / name / "manifest.json").read_text())
                sessions = [
                    x[k]
                    for x in manifest["station_sessions"]
                    for k in ("image_session_id", "virtual_session_id")
                ]
                packets.append(
                    normalize(
                        [record["packets"] for record in records(root / name / "online.jsonl")],
                        sessions,
                    )
                )
            self.assertEqual(packets[0], packets[1])

    def test_independent_camera_cadence_has_no_starvation(self) -> None:
        spec = short_spec()
        cameras = tuple(
            replace(
                c,
                exposure=replace(
                    c.exposure,
                    first_exposure_start_ns=100_000_000
                    if c.camera_role is CameraRole.AUXILIARY
                    else 0,
                    frame_period_ns=200_000_000
                    if c.camera_role is CameraRole.AUXILIARY
                    else 150_000_000,
                ),
            )
            for c in spec.scene_definition.cameras
        )
        spec = replace(spec, scene_definition=replace(spec.scene_definition, cameras=cameras))
        source = demo_source(spec, render_every=2)
        from chongming.modules import scene as module

        with patch.object(module, "render_rgb8", wraps=render_rgb8) as render:
            steps = list(source.generate(spec))
            frames = [f for step in steps for f in step.video.frames]
            self.assertEqual(render.call_count, len(frames))
        for camera in cameras:
            captures = [
                f.capture_time.exposure_start_ns
                for f in frames
                if f.station_id == camera.station_id and f.camera_role == camera.camera_role
            ]
            expected = list(
                range(
                    camera.exposure.first_exposure_start_ns,
                    spec.timeline.end_time_ns,
                    camera.exposure.frame_period_ns,
                )
            )[::2]
            self.assertEqual(captures, expected)

    def test_long_timeline_streams_exposures_without_expansion(self) -> None:
        spec = short_spec()
        groups = _scheduled_exposures(
            spec.scene_definition.cameras, replace(spec.timeline, end_time_ns=10**18)
        )
        first = list(itertools.islice(groups, 3))
        self.assertEqual([t for t, _ in first], [1_000_000, 251_000_000, 501_000_000])
        self.assertTrue(all(len(group) == 4 for _, group in first))

    def test_json_sampling_period_controls_false_alarm_rate_and_preview_cadence(self) -> None:
        from chongming.preview import PreviewRuntime

        document = scenario_to_dict(short_spec())
        for camera in document["scenario"]["scene_definition"]["cameras"]:
            camera["exposure"]["frame_period_ns"] = 100_000_000
        document["scenario"]["virtual_observations"]["false_alarms_per_camera_hour"] = 3600.0
        spec = scenario_from_dict(document)
        # Nominal metadata still says 4 Hz; the edited schedule samples at 10 Hz.
        self.assertEqual(PreviewRuntime(spec, render_fps=5).render_every, 2)
        with patch("chongming.modules.scene._deterministic_poisson", return_value=0) as poisson:
            step = next(iter(demo_source(spec).generate(spec)))
        self.assertTrue(poisson.called)
        for call in poisson.call_args_list:
            self.assertAlmostEqual(call.args[2], 0.1)
        self.assertTrue(all(f.sampling_period_ns == 100_000_000 for f in step.truth.camera_frames))

    def test_failure_has_summary_and_keeps_partial_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "scenario.json"
            save_compiled_scenario(short_spec(), path)
            with (
                patch("chongming.scenarios.run.demo_source", side_effect=RuntimeError("startup")),
                self.assertRaisesRegex(RuntimeError, "startup"),
            ):
                run_scene(path, root / "no-start")
            self.assertFalse((root / "no-start").exists())
            with (
                patch(
                    "chongming.modules.scene.CameraImagingModule.render",
                    side_effect=RuntimeError("GPU failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "GPU failed"),
            ):
                run_scene(path, root / "failed")
            summary = json.loads((root / "failed" / "summary.json").read_text())
            self.assertEqual(summary["status"], "failed")
            self.assertIn("GPU failed", summary["error"])

    def test_behavior_registry_is_editable_and_versioned(self) -> None:
        document = scenario_to_dict(short_spec())
        target = document["scenario"]["scene_definition"]["flying_objects"][0]
        target["behavior"] = {
            "$type": "FlightBehaviorSpec",
            "behavior_id": "constant-velocity",
            "implementation_version": "1",
            "parameters": [["vx", 3], ["vy", 0.0], ["vz", -1]],
            "random_stream": "flight/one",
        }
        spec = scenario_from_dict(document)
        steps = list(demo_source(spec).generate(spec))
        a, b = (
            steps[0].truth.logical_scene.flying_objects[0],
            steps[-1].truth.logical_scene.flying_objects[0],
        )
        self.assertAlmostEqual(b.state.position_enu_m[0] - a.state.position_enu_m[0], 1.5)
        self.assertEqual(a.state.velocity_enu_mps, (3.0, 0.0, -1.0))
        target["behavior"]["implementation_version"] = "unknown"
        with self.assertRaisesRegex(ValueError, "unknown behavior"):
            scenario_from_dict(document)

    def test_malformed_json_and_unimplemented_effects_fail_before_output(self) -> None:
        base = scenario_to_dict(short_spec())
        edits: tuple[Callable[[dict[str, Any]], None], ...] = (
            lambda d: d["scenario"].update(random_seed=None),
            lambda d: d["scenario"]["scene_definition"]["flying_objects"][0]["geometry"].update(
                **{"$type": "FlyingObjectSpec"}
            ),
            lambda d: d["scenario"]["scene_definition"]["cameras"][0]["actual_intrinsics"].update(
                focal_x_px=float("nan")
            ),
            lambda d: d["scenario"]["scene_definition"]["flying_objects"][0]["trajectory"]["knots"][
                0
            ]["state"].update(position_enu_m=[1, 2]),
        )
        for edit in edits:
            document = json.loads(json.dumps(base))
            edit(document)
            with self.assertRaises(ValueError):
                scenario_from_dict(document)
        spec = short_spec()
        spec = replace(
            spec, perturbations=replace(spec.perturbations, fog=s.FogPerturbation(100.0, 0.01))
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "future.json"
            save_compiled_scenario(spec, path)
            self.assertEqual(load_compiled_scenario(path), spec)
            with self.assertRaisesRegex(ValueError, "not implemented"):
                run_scene(path, root / "output")
            self.assertFalse((root / "output").exists())

    def test_profile_and_structural_validation_happens_before_gpu(self) -> None:
        spec = short_spec()
        first = spec.scene_definition.cameras[0]
        invalid = (
            replace(spec, timeline=replace(spec.timeline, logic_step_ns=0)),
            replace(
                spec,
                virtual_observations=replace(spec.virtual_observations, detection_probability=1.01),
            ),
            replace(
                spec,
                scene_definition=replace(
                    spec.scene_definition,
                    cameras=(first, first, *spec.scene_definition.cameras[2:]),
                ),
            ),
            replace(
                spec,
                scene_definition=replace(
                    spec.scene_definition,
                    cameras=(
                        replace(
                            first,
                            optical_pose_trajectory=replace(
                                first.optical_pose_trajectory, knots=()
                            ),
                        ),
                        *spec.scene_definition.cameras[1:],
                    ),
                ),
            ),
        )
        with patch("chongming.modules.scene.render_rgb8") as render:
            for bad in invalid:
                with self.subTest(bad=bad.scenario_id), self.assertRaises(ValueError):
                    next(iter(demo_source(bad).generate(bad)))
            render.assert_not_called()
        baseline = demo_spec()
        auxiliary = baseline.scene_definition.cameras[1]
        first_knot = auxiliary.optical_pose_trajectory.knots[0]
        tilted = replace(
            first_knot,
            time_ns=1_000_000_000,
            state=replace(first_knot.state, rotation_enu_from_body=optical_rotation(0, 0.01)),
        )
        auxiliary = replace(
            auxiliary,
            optical_pose_trajectory=replace(
                auxiliary.optical_pose_trajectory, knots=(first_knot, tilted)
            ),
        )
        bad = replace(
            baseline,
            scene_definition=replace(
                baseline.scene_definition, cameras=(baseline.scene_definition.cameras[0], auxiliary)
            ),
        )
        with self.assertRaisesRegex(ValueError, "not level"):
            scenario_from_dict(scenario_to_dict(bad))


if __name__ == "__main__":
    unittest.main()

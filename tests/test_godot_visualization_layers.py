"""Freeview geometry and real-pixel isolation; test hooks never enter production replies."""

from __future__ import annotations

import base64
import shutil
import tempfile
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np

from chongming.contracts import model as m
from chongming.contracts import scene as s
from chongming.contracts import visualization as v
from chongming.rendering.godot import GodotRenderer, visualization_ui_state
from tests import test_godot_visualization as transport
from tests.test_scene_rendering import snapshot, test_scene

LAYERS = ("truth", "rays", "measurements", "tracks", "voxels")


def layer_state() -> dict[str, Any]:
    initial = transport.fixed_update()
    assert initial.edge_events is not None and initial.center_events is not None
    assert initial.station_states is not None
    edge = initial.edge_events[0]
    rays = tuple(
        replace(
            edge.rays[0],
            event_id=(m.StationId("station-0"), edge.station_session_id, index),
            origin_enu_m=(-5.0 + index * 2, 0.0, 3.0),
            calibration_usage=usage,
            angular_std_rad=0.002 if index == 0 else None,
        )
        for index, usage in enumerate(
            (m.CalibrationUsage.VALID, m.CalibrationUsage.FROZEN, m.CalibrationUsage.STALE)
        )
    )
    measurement = replace(
        initial.center_events.measurements[0],
        position_std_enu_m=(2.0, 4.0, 6.0),
        supporting_ray_ids=(rays[0].event_id,),
        supporting_station_ids=(m.StationId("station-0"),),
    )
    tracks: list[v.TrackVisualization] = []
    for index, lifecycle in enumerate(m.TrackLifecycle):
        track = replace(
            initial.center_events.tracks[-1],
            track_id=m.TrackId(lifecycle.value),
            lifecycle=lifecycle,
            position_enu_m=None
            if lifecycle == m.TrackLifecycle.COASTING
            else (-4.0 + index * 2, 5.0, 8.0),
            recent_position_enu_m=(-4.0 + index * 2, 5.0, 8.0),
        )
        tracks.extend(
            (
                replace(
                    track,
                    event_time_ns=transport.TIME + 10,
                    recent_time_ns=transport.TIME + 10,
                    recent_position_enu_m=(-5.0 + index * 2, 4.0, 8.0),
                ),
                track,
            )
        )
    output = replace(
        initial,
        edge_events=(replace(edge, station_id=m.StationId("station-0"), rays=rays),),
        center_events=replace(
            initial.center_events,
            measurements=(
                measurement,
                replace(
                    measurement,
                    measurement_id=m.MeasurementId("unlocated"),
                    status=m.FusionStatus.GEOMETRY_DEGENERATE,
                    position_enu_m=None,
                    position_std_enu_m=None,
                ),
            ),
            tracks=tuple(tracks),
            voxel_space=v.VoxelSpaceVisualization(
                search_box_min_enu_m=(-3.0, 0.0, 2.0),
                search_box_max_enu_m=(3.0, 8.0, 8.0),
                voxel_step_enu_m=(2.0, 2.0, 2.0),
                max_score=2,
                scored_voxels=(
                    v.ScoredVoxel(grid_index=(0, 2, 1), score=1, supporting_station_count=1),
                    v.ScoredVoxel(grid_index=(1, 2, 1), score=2, supporting_station_count=2),
                ),
            ),
        ),
        station_states=(
            replace(
                initial.station_states[0],
                station_id=m.StationId("station-0"),
                calibration_usage=m.CalibrationUsage.FROZEN,
                truth_orientation_error_rad=(0.001, -0.002, 0.003),
            ),
        ),
    )
    truths = transport.fixed_truth()
    target = replace(
        truths[-1].targets[0],
        visibility_by_station=(
            v.TargetStationVisibility(
                station_id=m.StationId("station-0"), in_field_of_view=True, capture_eligible=False
            ),
            v.TargetStationVisibility(
                station_id=m.StationId("station-1"), in_field_of_view=False, capture_eligible=False
            ),
            v.TargetStationVisibility(
                station_id=m.StationId("unknown"), in_field_of_view=True, capture_eligible=True
            ),
        ),
    )
    truths = (
        v.TruthComparisonSnapshot(
            targets=(replace(truths[0].targets[0], target_id="historical-only"),)
        ),
        truths[1],
        v.TruthComparisonSnapshot(targets=(target,)),
    )
    return {
        "version": 1,
        "editor": {"freeview": True},
        "playback": {"state": "paused"},
        **visualization_ui_state(output, truths),
    }


# Inspect actual Godot mesh buffers. Capture a real rendered SubViewport after
# flushing scene changes; headless/dummy rendering cannot satisfy pixel assertions.
LAYER_WORKER = """extends "res://worker.gd"
var _capture := ""
var _style_camera := false
func _vector_report(value: Vector3) -> Array:
    return [value.x, value.y, value.z]
func _node_report(node: Node3D) -> Dictionary:
    var result := {"id": node.get_meta("id", ""), "name": str(node.name),
        "instance": node.get_instance_id(), "position": _vector_report(node.position), "children": []}
    if node is MeshInstance3D:
        var vertices := []
        for vertex in node.mesh.surface_get_arrays(0)[Mesh.ARRAY_VERTEX]:
            vertices.append(_vector_report(vertex))
        result["vertices"] = vertices
        result["layers"] = node.layers
        result["shadow"] = node.cast_shadow
        var material: Material = node.material_override
        if material is ShaderMaterial:
            result["pattern"] = material.get_shader_parameter("pattern")
            var color: Color = material.get_shader_parameter("line_color")
            result["line_color"] = [color.r, color.g, color.b, color.a]
        else:
            result["color"] = [material.albedo_color.r, material.albedo_color.g, material.albedo_color.b, material.albedo_color.a]
            result["billboard"] = material.billboard_mode
    for child in node.get_children():
        result.children.append(_node_report(child))
    return result
func _ui_exchange(request: Dictionary, trailer: PackedByteArray) -> PackedByteArray:
    var reply := super._ui_exchange(request, trailer)
    if reply.is_empty():
        return reply
    if request.state.has("test_style_camera"):
        _style_camera = request.state.test_style_camera
    for key in request.state.get("test_layers", {}):
        _ui._visualization_layers[key].button_pressed = request.state.test_layers[key]
    if request.state.has("test_select"):
        var id: String = request.state.test_select
        for index in range(_ui._measurement_select.item_count):
            if str(_ui._measurement_select.get_item_metadata(index)) == id:
                _ui._measurement_select.select(index)
                _ui._measurement_select.item_selected.emit(index)
                break
    var layers := {}
    for key in _visualization.layers:
        layers[key] = {"visible": _visualization.layers[key].visible, "geometry": _node_report(_visualization.layers[key])}
    var document: Dictionary = JSON.parse_string(reply.get_string_from_utf8())
    document.commands.append({"type": "test_layers", "layers": layers, "update_count": _visualization.update_count,
        "ray_count": _visualization.rays.size(), "measurement_count": _visualization.measurements.size(),
        "track_count": _visualization.tracks.size(), "station_text": _ui._station_states.text, "capture": _capture,
        "evaluation": _visualization.evaluation.summary(), "diagnostic_text": _ui._diagnostics.text,
        "selection": _visualization.selection_summary(), "selection_text": _ui._measurement_details.text,
        "voxel_space": _visualization.voxel_space, "voxel_text": _ui._voxel_status.text,
        "voxel_update": _visualization.voxel_update_id})
    return JSON.stringify(document).to_utf8_buffer()
func _render(request: Dictionary) -> PackedByteArray:
    var result := await super._render(request)
    if request.get("mode") == "editor" and _editor_active:
        _editor_gizmos.visible = false
        _free_viewport.size = Vector2i(320, 240)
        if _style_camera:
            _free_camera.projection = Camera3D.PROJECTION_ORTHOGONAL
            _free_camera.size = 16.0
            _free_camera.position = Vector3(0, 3, 20)
            _free_camera.look_at(Vector3(0, 3, 0))
        else:
            _free_camera.projection = Camera3D.PROJECTION_PERSPECTIVE
            _free_camera.position = Vector3(0, 6, 18)
            _free_camera.look_at(Vector3(0, 5, -4))
        await get_tree().process_frame
        _free_viewport.render_target_update_mode = SubViewport.UPDATE_ONCE
        RenderingServer.force_draw(false)
        var image := _free_viewport.get_texture().get_image()
        image.convert(Image.FORMAT_RGB8)
        _capture = Marshalls.raw_to_base64(image.get_data())
    return result
"""


@contextmanager
def instrumented_renderer() -> Iterator[GodotRenderer]:
    with tempfile.TemporaryDirectory() as directory:
        package = Path(directory)
        project = package / "godot"
        shutil.copytree(transport.PROJECT, project)
        (project / "test_worker.gd").write_text(LAYER_WORKER)
        scene = project / "worker.tscn"
        scene.write_text(scene.read_text().replace("res://worker.gd", "res://test_worker.gd"))
        with patch("chongming.rendering.godot.files", return_value=package):
            renderer = GodotRenderer()
        try:
            yield renderer
        finally:
            renderer.close()


def descendants(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for child in node["children"]:
        yield from descendants(child)


def named(node: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in descendants(node) if item["name"] == name)


def pixels(report: dict[str, Any]) -> np.ndarray:
    return np.frombuffer(base64.b64decode(report["capture"]), dtype=np.uint8).reshape(240, 320, 3)


@unittest.skipUnless(transport.GODOT, "Godot 4 with a real graphics backend is required")
class VisualizationLayerTests(unittest.TestCase):
    def test_fixed_geometry_styles_visibility_and_sensor_pixels(self) -> None:
        scene = snapshot(test_scene(s.SphereGeometry(1.0), (0.0, 10.0, -30.0)))
        camera = replace(
            scene.cameras[0],
            actual_intrinsics=replace(
                scene.cameras[0].actual_intrinsics,
                image_width_px=96,
                image_height_px=64,
                focal_x_px=65.0,
                focal_y_px=65.0,
                principal_x_px=47.5,
                principal_y_px=31.5,
            ),
        )
        with instrumented_renderer() as renderer:
            original, _ = renderer.render(scene, camera)
            self.assertTrue(renderer.editor_update(scene, camera, realtime=False, reopen=True))
            payload = layer_state()
            renderer.ui_exchange(payload)
            self.assertTrue(renderer.editor_update(scene, camera, realtime=False))
            report = renderer.ui_exchange({"version": 2, "editor": {"freeview": True}})[0]
            layers = report["layers"]
            self.assertEqual(report["update_count"], 1)
            for layer in layers.values():
                self.assertTrue(layer["visible"])
                for node in descendants(layer["geometry"]):
                    if "vertices" in node:
                        self.assertEqual(node["layers"], 2)
                        self.assertEqual(node["shadow"], 0)
            truth = layers["truth"]["geometry"]["children"]
            self.assertEqual([node["id"] for node in truth], ["truth-a"])
            self.assertEqual(truth[0]["position"], [4.0, 6.0, -5.0])
            ring = named(truth[0], "SizeCircle")
            np.testing.assert_allclose(np.linalg.norm(ring["vertices"], axis=1), 0.5, atol=1e-6)
            self.assertNotEqual(ring["billboard"], 0)
            self.assertEqual(named(truth[0], "Velocity")["vertices"][1], [1.0, 0.0, 0.0])
            links = named(truth[0], "StationLinks")["vertices"]
            self.assertEqual(len(links), 2, "Only the known, in-FOV main station gets a truth link")
            station = scene.cameras[0].optical_state.position_enu_m
            np.testing.assert_allclose(
                np.array(links[1]) + truth[0]["position"], [station[0], station[2], -station[1]]
            )
            rays = layers["rays"]["geometry"]["children"]
            self.assertEqual(len(rays), 3)
            self.assertEqual([named(ray, "Ray")["pattern"] for ray in rays], [0, 1, 2])
            self.assertEqual(rays[0]["position"], [-5.0, 3.0, 0.0])
            self.assertEqual(
                named(rays[0], "Ray")["vertices"], [[0.0, 0.0, 0.0], [0.0, 0.0, -5000.0]]
            )
            cone = named(rays[0], "AngularStdCone")
            self.assertAlmostEqual(cone["color"][3], 0.08)
            cone_vertices = np.array(cone["vertices"])
            np.testing.assert_allclose(
                np.linalg.norm(cone_vertices[1::3, :2], axis=1), 5000 * np.tan(0.002), rtol=1e-6
            )
            for ray in rays[1:]:
                self.assertNotIn("AngularStdCone", [node["name"] for node in descendants(ray)])
            measurements = layers["measurements"]["geometry"]["children"]
            self.assertEqual(
                len(measurements), 1, "Unlocated results must not create a fake position"
            )
            np.testing.assert_allclose(
                np.linalg.norm(named(measurements[0], "SizeCircle")["vertices"], axis=1),
                3.0,
                atol=1e-6,
            )
            tracks = {node["id"]: node for node in layers["tracks"]["geometry"]["children"]}
            self.assertEqual(set(tracks), {"tentative", "confirmed", "coasting", "deleted"})
            colors = {key: named(node, "History")["color"] for key, node in tracks.items()}
            self.assertGreater(colors["tentative"][1], colors["confirmed"][1])
            self.assertGreater(colors["coasting"][0], colors["coasting"][1])
            self.assertAlmostEqual(colors["deleted"][0], colors["deleted"][1])
            self.assertEqual(
                named(tracks["confirmed"], "History")["vertices"],
                [[-3.0, 8.0, -4.0], [-2.0, 8.0, -5.0]],
            )
            self.assertNotIn("Position", [node["name"] for node in descendants(tracks["coasting"])])
            self.assertIn("FROZEN", report["station_text"])
            self.assertIn("实际相对配置：0.001 / -0.002 / 0.003", report["station_text"])
            self.assertIn("校准相对配置：—", report["station_text"])
            on = pixels(report).copy()
            duplicate = renderer.ui_exchange({**payload, "version": 3})[0]
            self.assertEqual(
                duplicate["layers"], layers, "Duplicate updates must not rebuild or duplicate nodes"
            )
            time.sleep(1.1)
            paused = renderer.ui_exchange({"version": 4})[0]
            self.assertEqual(
                paused["layers"], layers, "Wall-clock time must not expire paused geometry"
            )
            self.assertEqual(paused["update_count"], 1)
            self.assertEqual(paused["evaluation"], report["evaluation"])
            self.assertEqual(duplicate["evaluation"], report["evaluation"])
            self.assertEqual(renderer.render(scene, camera)[0], original)
            views: dict[str, np.ndarray] = {}
            version = 5
            for selected in (None, *LAYERS):
                enabled = {key: key == selected for key in LAYERS}
                toggled = renderer.ui_exchange({"version": version, "test_layers": enabled})[0]
                self.assertEqual(
                    {key: value["visible"] for key, value in toggled["layers"].items()}, enabled
                )
                self.assertEqual(toggled["update_count"], 1)
                self.assertEqual(toggled["evaluation"], report["evaluation"])
                for key in LAYERS:
                    self.assertEqual(toggled["layers"][key]["geometry"], layers[key]["geometry"])
                self.assertTrue(renderer.editor_update(scene, camera, realtime=False))
                captured = renderer.ui_exchange({"version": version + 1})[0]
                views["off" if selected is None else selected] = pixels(captured).copy()
                self.assertEqual(
                    renderer.render(scene, camera)[0],
                    original,
                    f"Layer {selected} contaminated sensor pixels",
                )
                version += 2
            for key in LAYERS:
                self.assertFalse(
                    np.array_equal(views[key], views["off"]),
                    f"{key} must reach real freeview pixels",
                )
            self.assertFalse(np.array_equal(on, views["off"]))
            expired = renderer.ui_exchange(
                {
                    "version": version,
                    "algorithm_output": {
                        "update_id": "2",
                        "step_no": "4",
                        "scenario_time_ns": str(transport.TIME + 3_000_000_021),
                        "edge_events": None,
                        "center_events": None,
                        "station_states": None,
                    },
                    "truth_snapshots": [{"targets": []}],
                }
            )[0]
            self.assertEqual(expired["update_count"], 2)
            self.assertEqual(expired["evaluation"], report["evaluation"])
            for key in ("truth", "rays", "measurements"):
                self.assertEqual(expired["layers"][key]["geometry"]["children"], [])
            self.assertEqual(expired["track_count"], 3)
            self.assertNotIn(
                "deleted",
                [node["id"] for node in expired["layers"]["tracks"]["geometry"]["children"]],
            )
            self.assertEqual(renderer.render(scene, camera)[0], original)
            self.assertNotIn("SCRIPT ERROR", renderer._diagnostics())
            self.assertNotIn("SHADER ERROR", renderer._diagnostics())

    def test_valid_frozen_and_stale_ray_patterns_reach_real_pixels(self) -> None:
        scene = snapshot(test_scene(s.SphereGeometry(1.0), (0.0, 10.0, -30.0)))
        initial = transport.fixed_update()
        assert initial.edge_events is not None
        edge = initial.edge_events[0]
        rays = tuple(
            replace(
                edge.rays[0],
                event_id=(*edge.rays[0].event_id[:2], index),
                origin_enu_m=(-6.0, 0.0, 2.0 + index),
                direction_enu_unit=(1.0, 0.0, 0.0),
                calibration_usage=usage,
                angular_std_rad=None,
            )
            for index, usage in enumerate(
                (m.CalibrationUsage.VALID, m.CalibrationUsage.FROZEN, m.CalibrationUsage.STALE)
            )
        )
        output = replace(
            initial,
            edge_events=(replace(edge, rays=rays),),
            center_events=None,
            station_states=None,
        )
        truths = (v.TruthComparisonSnapshot(targets=()),) * 2
        with instrumented_renderer() as renderer:
            renderer.ui_exchange(
                {"version": 1, "test_style_camera": True, **visualization_ui_state(output, truths)}
            )
            self.assertTrue(
                renderer.editor_update(scene, scene.cameras[0], realtime=False, reopen=True)
            )
            report = renderer.ui_exchange({"version": 2})[0]
            image = pixels(report)
            yellow = (image[:, :, 0] > 200) & (image[:, :, 1] > 150) & (image[:, :, 2] < 150)
            # With an orthographic camera, each ray occupies a separate horizontal band.
            counts = [
                int(yellow[130:150].sum()),
                int(yellow[110:130].sum()),
                int(yellow[90:110].sum()),
            ]
            self.assertGreater(counts[0], counts[1], counts)
            self.assertGreater(counts[1], counts[2], counts)
            self.assertGreater(counts[2], 0, counts)
            self.assertNotIn("SCRIPT ERROR", renderer._diagnostics())
            self.assertNotIn("SHADER ERROR", renderer._diagnostics())


if __name__ == "__main__":
    unittest.main()

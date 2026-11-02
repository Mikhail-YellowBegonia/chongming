"""Sparse-grid replacement, solid voxel faces and cache-only measurement selection."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np

from chongming.contracts import scene as s
from chongming.contracts import visualization as v
from chongming.development import CenterSession
from chongming.reference import create_algorithm
from chongming.rendering.godot import visualization_ui_state
from chongming.scenarios.authoring import save_authoring
from tests import test_center_ui as run_ui
from tests import test_godot_visualization as transport
from tests import test_godot_visualization_layers as layers
from tests.test_center_ui import ROOT, observed_workers
from tests.test_scene_rendering import snapshot, test_scene


def increment(
    number: int, time_ns: int, *, windows: int = 0, space: v.VoxelSpaceVisualization | None = None
) -> dict[str, Any]:
    output = v.AlgorithmOutputUpdate(
        update_id=number,
        scenario_time_ns=time_ns,
        step_no=number,
        edge_events=None,
        station_states=None,
        center_events=v.CenterFusionEvent(
            measurements=(),
            tracks=(),
            window_count=windows,
            candidate_count=0,
            association_count=0,
            voxel_space=space,
        ),
    )
    return {
        "version": number,
        **visualization_ui_state(output, (v.TruthComparisonSnapshot(targets=()),)),
    }


def empty_space() -> v.VoxelSpaceVisualization:
    return v.VoxelSpaceVisualization(
        search_box_min_enu_m=(-1.0, -1.0, -1.0),
        search_box_max_enu_m=(1.0, 1.0, 1.0),
        voxel_step_enu_m=(1.0, 1.0, 1.0),
        scored_voxels=(),
        max_score=0,
    )


@unittest.skipUnless(transport.GODOT, "Godot is required for receiver logic")
class VoxelReceiverTests(unittest.TestCase):
    def receive(self, *states: dict[str, Any]) -> list[dict[str, Any]]:
        script = transport.RECEIVER_SCRIPT.replace(
            '"accepted": accepted,',
            '"voxel_space": receiver.voxel_space, "voxel_update": receiver.voxel_update_id, "accepted": accepted,',
        )
        with patch.object(transport, "RECEIVER_SCRIPT", script):
            return transport.VisualizationReceiverTests().receive(*states)

    def test_unsealed_increments_keep_last_grid_but_empty_or_unavailable_seals_clear_it(
        self,
    ) -> None:
        initial = layers.layer_state()
        empty = empty_space()
        different = replace(
            empty,
            max_score=1,
            scored_voxels=(
                v.ScoredVoxel(grid_index=(0, 0, 0), score=1, supporting_station_count=1),
            ),
        )
        reports = self.receive(
            initial,
            increment(2, transport.TIME + 30),
            {"version": 3},
            increment(3, transport.TIME + 40, windows=1, space=empty),
            increment(4, transport.TIME + 50, windows=2, space=different),
            increment(5, transport.TIME + 60, windows=1),
        )
        self.assertTrue(all(report["accepted"] for report in reports))
        self.assertEqual(reports[1]["voxel_space"], reports[0]["voxel_space"])
        self.assertEqual(reports[2]["voxel_space"], reports[0]["voxel_space"])
        self.assertEqual([report["voxel_update"] for report in reports], [1, 1, 1, 3, 4, 5])
        self.assertEqual(reports[3]["voxel_space"]["scored_voxels"], [])
        self.assertEqual(reports[3]["voxel_space"]["max_score"], 0)
        self.assertEqual(len(reports[4]["voxel_space"]["scored_voxels"]), 1)
        self.assertIsNone(reports[5]["voxel_space"])
        self.assertTrue(all(report["evaluation"] == reports[0]["evaluation"] for report in reports))

    def test_invalid_grids_are_rejected_before_mutating_any_cache_or_statistics(self) -> None:
        initial = layers.layer_state()
        states = [initial]
        for index in range(9):
            bad = copy.deepcopy(initial)
            bad["algorithm_output"]["update_id"] = "2"
            space = bad["algorithm_output"]["center_events"]["voxel_space"]
            cell = space["scored_voxels"][0]
            if index == 0:
                space["scored_voxels"].append(copy.deepcopy(cell))
            elif index == 1:
                cell["grid_index"][0] = "-1"
            elif index == 2:
                cell["grid_index"][0] = "3"
            elif index == 3:
                cell["grid_index"][0] = True
            elif index == 4:
                cell["score"] = "0"
            elif index == 5:
                space["max_score"] = "1"
            elif index == 6:
                space["voxel_step_enu_m"][0] = 0
            elif index == 7:
                cell["supporting_station_count"] = "-1"
            else:
                space["search_box_max_enu_m"][0] = space["search_box_min_enu_m"][0]
            states.append(bad)
        reports = self.receive(*states)
        self.assertTrue(reports[0]["accepted"])
        for index, report in enumerate(reports[1:]):
            with self.subTest(index=index):
                self.assertFalse(report["accepted"])
                for field in (
                    "voxel_space",
                    "voxel_update",
                    "evaluation",
                    "last_update",
                    "rays",
                    "measurements",
                    "tracks",
                    "track_history",
                    "update_count",
                ):
                    self.assertEqual(report[field], reports[0][field], field)


@unittest.skipUnless(transport.GODOT, "Godot with real graphics is required")
class VoxelGraphicsTests(unittest.TestCase):
    def scene(self) -> tuple[s.LogicalSceneSnapshot, s.CameraStateTruth]:
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
        return scene, camera

    def test_sensor_updates_target_position_on_consecutive_requests(self) -> None:
        scene, camera = self.scene()
        scene = snapshot(test_scene(s.SphereGeometry(1.0)))
        moved = snapshot(test_scene(s.SphereGeometry(1.0), (2.0, 0.0, 10.0)))
        with layers.instrumented_renderer() as renderer:
            original, _ = renderer.render(scene, camera)
            shifted, _ = renderer.render(moved, camera)
            restored, _ = renderer.render(scene, camera)
        self.assertTrue(shifted != original, "Target motion must reach Godot on every frame")
        self.assertEqual(restored, original)

    def test_solid_cells_use_raw_corners_score_alpha_and_clipped_edge_cells(self) -> None:
        scene, camera = self.scene()
        with layers.instrumented_renderer() as renderer:
            renderer.editor_update(scene, camera, realtime=False, reopen=True)
            initial = layers.layer_state()
            space = initial["algorithm_output"]["center_events"]["voxel_space"]
            space["scored_voxels"][0]["score"] = "2"
            space["scored_voxels"][0]["supporting_station_count"] = "2"
            space["scored_voxels"][1]["score"] = "15"
            space["scored_voxels"][1]["supporting_station_count"] = "15"
            space["scored_voxels"].append(
                {"grid_index": ["2", "2", "1"], "score": "1", "supporting_station_count": "1"}
            )
            space["max_score"] = "15"
            report = renderer.ui_exchange(initial)[0]
            geometry = report["layers"]["voxels"]["geometry"]
            self.assertEqual(len(geometry["children"]), 2)
            for score in (2, 15):
                mesh = layers.named(geometry, f"Score{score}")
                # Solid cubes: 6 faces × 2 triangles × 3 vertices = 36 vertices
                self.assertEqual(len(mesh["vertices"]), 36, "One solid cube has 6 faces")
                expected_alpha = 0.06 + 0.12 * (score - 2) / 13
                self.assertAlmostEqual(mesh["color"][3], expected_alpha, places=2)
                self.assertEqual(mesh["layers"], 2)
                self.assertEqual(mesh["shadow"], 0)
                # Every face must consist of two complementary triangles. Vertex counts
                # alone cannot detect overlapping triangles and holes on the same face.
                triangles = np.array(mesh["vertices"]).reshape(-1, 3, 3)
                faces: dict[tuple[int, float], list[set[tuple[float, ...]]]] = {}
                for triangle in triangles:
                    constant_axes = np.flatnonzero(np.ptp(triangle, axis=0) == 0)
                    self.assertEqual(len(constant_axes), 1)
                    axis = int(constant_axes[0])
                    faces.setdefault((axis, float(triangle[0, axis])), []).append(
                        set(map(tuple, triangle))
                    )
                self.assertEqual(len(faces), 6)
                for pair in faces.values():
                    self.assertEqual(len(pair), 2)
                    self.assertEqual(len(pair[0] | pair[1]), 4)
                    self.assertEqual(len(pair[0] & pair[1]), 2)
                    # The shared edge must be a diagonal, not a face boundary.
                    shared = list(pair[0] & pair[1])
                    self.assertEqual(np.count_nonzero(np.subtract(shared[0], shared[1])), 2)
                # Check that all vertices fall within the expected voxel bounds
                corners = {
                    (e, u, -n)
                    for e in ((-3.0, -1.0) if score == 2 else (-1.0, 1.0))
                    for u in (4.0, 6.0)
                    for n in (4.0, 6.0)
                }
                for vertex in mesh["vertices"]:
                    self.assertIn(
                        tuple(vertex), corners, f"Vertex {vertex} not in expected corners"
                    )
            unchanged = renderer.ui_exchange(increment(2, transport.TIME + 30))[0]
            self.assertEqual(unchanged["layers"]["voxels"]["geometry"], geometry)
            partial = v.VoxelSpaceVisualization(
                search_box_min_enu_m=(-3.0, 0.0, 2.0),
                search_box_max_enu_m=(2.0, 3.0, 3.5),
                voxel_step_enu_m=(2.0, 2.0, 2.0),
                scored_voxels=(
                    v.ScoredVoxel(grid_index=(2, 1, 0), score=2, supporting_station_count=2),
                ),
                max_score=2,
            )
            report = renderer.ui_exchange(
                increment(3, transport.TIME + 40, windows=1, space=partial)
            )[0]
            vertices = layers.named(report["layers"]["voxels"]["geometry"], "Score2")["vertices"]
            self.assertEqual(
                set(map(tuple, vertices)),
                {(e, u, -n) for e in (1.0, 2.0) for u in (2.0, 3.5) for n in (2.0, 3.0)},
            )
            for update_id, score, alpha in ((4, 8, 0.06 + 0.12 * 6 / 13), (5, 20, 0.18)):
                scored = replace(
                    partial,
                    scored_voxels=(
                        v.ScoredVoxel(
                            grid_index=(2, 1, 0), score=score, supporting_station_count=score
                        ),
                    ),
                    max_score=score,
                )
                report = renderer.ui_exchange(
                    increment(update_id, transport.TIME + 50 + update_id, windows=1, space=scored)
                )[0]
                mesh = layers.named(report["layers"]["voxels"]["geometry"], f"Score{score}")
                self.assertAlmostEqual(mesh["color"][3], alpha, places=5)
            cleared = renderer.ui_exchange(
                increment(6, transport.TIME + 60, windows=1, space=empty_space())
            )[0]
            self.assertEqual(cleared["layers"]["voxels"]["geometry"]["children"], [])
            self.assertIn("0 个非零网格", cleared["voxel_text"])

    def test_selection_highlights_only_cached_support_and_never_reconstructs_expired_rays(
        self,
    ) -> None:
        scene, camera = self.scene()
        with layers.instrumented_renderer() as renderer:
            sensor, _ = renderer.render(scene, camera)
            renderer.editor_update(scene, camera, realtime=False, reopen=True)
            initial = layers.layer_state()
            measurement = initial["algorithm_output"]["center_events"]["measurements"][0]
            missing = ["station-0", measurement["supporting_ray_ids"][0][1], "999"]
            measurement["supporting_ray_ids"].append(missing)
            renderer.ui_exchange(initial)
            renderer.editor_update(scene, camera, realtime=False)
            before = renderer.ui_exchange({"version": 2})[0]
            stats = before["evaluation"]
            selected = renderer.ui_exchange(
                {"version": 3, "test_select": measurement["measurement_id"]}
            )[0]
            self.assertEqual(len(selected["selection"]["available_ray_ids"]), 1)
            self.assertEqual(
                selected["selection"]["missing_ray_ids"], [[missing[0], missing[1], 999]]
            )
            self.assertIn("可显示 1 / 2", selected["selection_text"])
            ray_nodes = selected["layers"]["rays"]["geometry"]["children"]
            self.assertEqual(len(ray_nodes), 3)
            for index, ray in enumerate(ray_nodes):
                line = layers.named(ray, "Ray")
                self.assertEqual(
                    line["pattern"], index, "Selection preserves calibration line style"
                )
                if index == 0:
                    np.testing.assert_allclose(
                        line["line_color"][:3], [102 / 255, 221 / 255, 1.0], atol=1e-7
                    )
                else:
                    self.assertEqual(
                        line["line_color"],
                        layers.named(
                            before["layers"]["rays"]["geometry"]["children"][index], "Ray"
                        )["line_color"],
                    )
            renderer.editor_update(scene, camera, realtime=False)
            drawn = renderer.ui_exchange({"version": 4})[0]
            self.assertFalse(
                np.array_equal(layers.pixels(before), layers.pixels(drawn)),
                "Selection must reach real freeview pixels",
            )
            self.assertEqual(renderer.render(scene, camera)[0], sensor)
            for visible in (False, True):
                toggled = renderer.ui_exchange(
                    {"version": 5, "test_layers": {"rays": visible, "voxels": visible}}
                )[0]
                self.assertEqual(toggled["evaluation"], stats)
                self.assertEqual(toggled["layers"]["rays"]["visible"], visible)
                self.assertEqual(renderer.render(scene, camera)[0], sensor)
            boundary = renderer.ui_exchange(increment(2, transport.TIME + 1_000_000_000))[0]
            self.assertEqual(len(boundary["selection"]["available_ray_ids"]), 1)
            expired = renderer.ui_exchange(increment(3, transport.TIME + 1_000_000_001))[0]
            self.assertFalse(expired["selection"]["expired"])
            self.assertEqual(expired["selection"]["available_ray_ids"], [])
            self.assertEqual(len(expired["selection"]["missing_ray_ids"]), 2)
            self.assertIn("未接收或已过期", expired["selection_text"])
            self.assertEqual(expired["layers"]["rays"]["geometry"]["children"], [])
            gone = renderer.ui_exchange(increment(4, transport.TIME + 3_000_000_011))[0]
            self.assertTrue(gone["selection"]["expired"])
            self.assertIn("已过期，无法显示", gone["selection_text"])
            self.assertEqual(gone["layers"]["measurements"]["geometry"]["children"], [])
            self.assertEqual(gone["evaluation"], stats)
            cleared = renderer.ui_exchange({"version": 10, "test_select": ""})[0]
            self.assertEqual(cleared["selection"]["measurement_id"], "")
            self.assertEqual(cleared["evaluation"], stats)

    def test_reference_ui_exports_real_votes_and_a_new_worker_has_no_previous_grid(self) -> None:
        script = run_ui.RUN_WORKER.replace(
            '"last_update": _visualization.last_update,',
            '"voxel_space": _visualization.voxel_space, "selection": _visualization.selection_summary(), "last_update": _visualization.last_update,',
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(run_ui, "RUN_WORKER", script),
            observed_workers() as workers,
        ):
            root = Path(directory)
            path = root / "scene.json"
            document = json.loads((ROOT / "scenarios/center_clean.json").read_text())
            document["duration_s"] = 0.12
            save_authoring(document, path)
            for run in range(2):
                session = CenterSession(
                    path, root / f"reference-{run}", algorithm=create_algorithm()
                )
                session.run_ui(tick_hz=240)
                _, reports, states = workers[-1]
                self.assertEqual(reports[0]["update_count"], 0)
                self.assertIsNone(reports[0]["voxel_space"])
                self.assertEqual(reports[0]["selection"]["measurement_id"], "")
                grids = [
                    state["algorithm_output"]["center_events"]["voxel_space"]
                    for state in states
                    if "algorithm_output" in state
                ]
                self.assertTrue(any(grid is not None and grid["scored_voxels"] for grid in grids))
                self.assertEqual(reports[-1]["update_count"], session.step_count + 1)
                self.assertNotIn("SCRIPT ERROR", reports[-1]["worker_log"])
            self.assertEqual(len(workers), 2)


if __name__ == "__main__":
    unittest.main()

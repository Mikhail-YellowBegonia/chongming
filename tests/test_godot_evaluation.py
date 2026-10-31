"""Godot computes the diagnostics; Python supplies analytical fixtures and solver oracles only."""

from __future__ import annotations

import itertools
import json
import math
import random
import shutil
import struct
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from chongming.contracts import model as m
from chongming.contracts import visualization as v
from chongming.rendering.godot import visualization_ui_state
from tests import test_godot_visualization as transport
from tests import test_godot_visualization_layers as layers

TIME = transport.TIME


def ray(
    sequence: int,
    angle: float = 0.0,
    *,
    station: str = "A",
    time_ns: int = TIME,
    origin: m.Vec3 = (0.0, 0.0, 0.0),
    session: str = "session",
) -> v.RayVisualization:
    return v.RayVisualization(
        event_id=(m.StationId(station), m.SessionId(session), sequence),
        capture_time_ns=time_ns,
        origin_enu_m=origin,
        direction_enu_unit=(math.cos(angle), math.sin(angle), 0.0),
        calibration_usage=m.CalibrationUsage.VALID,
        detection_score=0.0,
        angular_std_rad=None,
    )


def measurement(
    id: str,
    position: m.Vec3 | None,
    *,
    time_ns: int = TIME,
    status: m.FusionStatus = m.FusionStatus.LOCATED,
) -> v.MeasurementVisualization:
    return v.MeasurementVisualization(
        measurement_id=m.MeasurementId(id),
        reference_time_ns=time_ns,
        status=status,
        position_enu_m=position,
        position_std_enu_m=None,
        supporting_ray_ids=(),
        supporting_station_ids=(),
        minimum_intersection_angle_deg=None,
        geometry_condition=None,
    )


def target(id: str, position: m.Vec3, *, time_ns: int = TIME) -> v.TruthTargetState:
    return v.TruthTargetState(
        target_id=id,
        time_ns=time_ns,
        position_enu_m=position,
        velocity_enu_mps=(0.0, 0.0, 0.0),
        geometry_type="sphere",
        bounding_radius_m=0.5,
        visibility_by_station=(),
    )


def payload(
    truth: dict[int, tuple[v.TruthTargetState, ...]],
    *,
    rays: tuple[v.RayVisualization, ...] = (),
    measurements: tuple[v.MeasurementVisualization, ...] = (),
    update_id: int = 1,
    scenario_time_ns: int = TIME,
) -> dict[str, Any]:
    stations: dict[tuple[m.StationId, m.SessionId], list[v.RayVisualization]] = {}
    for item in rays:
        stations.setdefault(item.event_id[:2], []).append(item)
    edge = tuple(
        v.EdgeStationEvent(
            station_id=station, station_session_id=session, rays=tuple(items), diagnostics=()
        )
        for (station, session), items in stations.items()
    )
    update = v.AlgorithmOutputUpdate(
        update_id=update_id,
        step_no=update_id,
        scenario_time_ns=scenario_time_ns,
        edge_events=edge,
        center_events=v.CenterFusionEvent(
            measurements=measurements,
            tracks=(),
            window_count=0,
            candidate_count=0,
            association_count=0,
            voxel_space=None,
        ),
        station_states=None,
    )
    times = sorted(
        {
            scenario_time_ns,
            *(item.capture_time_ns for item in rays),
            *(item.reference_time_ns for item in measurements),
        }
    )
    snapshots = tuple(v.TruthComparisonSnapshot(targets=truth[time_ns]) for time_ns in times)
    return {"version": update_id, **visualization_ui_state(update, snapshots)}


def receive(*states: dict[str, Any]) -> list[dict[str, Any]]:
    return transport.VisualizationReceiverTests().receive(*states)


ASSIGNMENT_SCRIPT = """extends SceneTree
const Evaluation = preload("res://evaluation.gd")
func _initialize() -> void:
    var evaluation := Evaluation.new()
    var cases: Array = JSON.parse_string(FileAccess.get_file_as_string(OS.get_cmdline_user_args()[0]))
    var results := []
    for case in cases:
        for row in case.costs:
            for index in range(row.size()):
                if row[index] is String:
                    row[index] = row[index].hex_decode().decode_double(0)
                if row[index] == null:
                    row[index] = INF
        results.append(evaluation._assignment(case.costs, int(case.columns)))
    print("REPORT " + JSON.stringify(results))
    quit()
"""


@unittest.skipUnless(transport.GODOT, "Godot 4 executable is required")
class EvaluationTests(unittest.TestCase):
    def assert_counts(
        self,
        stats: dict[str, Any],
        *,
        outputs: int,
        matched: int,
        unmatched: int = 0,
        unevaluable: int = 0,
    ) -> None:
        self.assertEqual(
            {
                key: stats[key]
                for key in (
                    "outputs",
                    "valid",
                    "matched",
                    "unmatched",
                    "unevaluable",
                    "sample_count",
                )
            },
            {
                "outputs": outputs,
                "valid": matched + unmatched,
                "matched": matched,
                "unmatched": unmatched,
                "unevaluable": unevaluable,
                "sample_count": matched,
            },
        )
        self.assertEqual(outputs, matched + unmatched + unevaluable)

    def test_known_pointing_angles_include_frozen_stale_and_zero_score_rays(self) -> None:
        rays = tuple(
            replace(ray(index, angle, station=str(index)), calibration_usage=usage)
            for index, (angle, usage) in enumerate(
                zip((0.0, 0.002, 0.004), m.CalibrationUsage, strict=True)
            )
        )
        report = receive(payload({TIME: (target("truth", (100.0, 0.0, 0.0)),)}, rays=rays))[0]
        stats = report["evaluation"]["edge"]
        self.assertTrue(report["accepted"], report)
        self.assert_counts(stats, outputs=3, matched=3)
        self.assertAlmostEqual(stats["mean_error"], 0.002, delta=1e-14)
        self.assertAlmostEqual(stats["rmse"], math.sqrt((0.002**2 + 0.004**2) / 3), delta=1e-14)
        self.assertAlmostEqual(stats["max_error"], 0.004, delta=1e-14)
        self.assertEqual([match["target_id"] for match in report["matches"]["edge"]], ["truth"] * 3)
        self.assertEqual(report["evaluation"]["angle_gate_rad"], 0.01)
        self.assertEqual(report["evaluation"]["distance_gate_m"], 10.0)

    def test_known_localization_errors_gate_boundary_and_unlocated_results(self) -> None:
        truths = tuple(target(str(index), (index * 100.0, 0.0, 0.0)) for index in range(4))
        outputs = (
            measurement("zero", (0.0, 0.0, 0.0)),
            measurement("five", (103.0, 4.0, 0.0)),
            measurement("boundary", (210.0, 0.0, 0.0)),
            measurement("outside", (310.001, 0.0, 0.0)),
            measurement("unlocated", None, status=m.FusionStatus.GEOMETRY_DEGENERATE),
            measurement("unknown", None),
        )
        report = receive(payload({TIME: truths}, measurements=outputs))[0]
        stats = report["evaluation"]["center"]
        self.assert_counts(stats, outputs=6, matched=3, unmatched=1, unevaluable=2)
        self.assertEqual(stats["mean_error"], 5.0)
        self.assertAlmostEqual(stats["rmse"], math.sqrt(125 / 3), delta=1e-14)
        self.assertEqual(stats["max_error"], 10.0)
        self.assertEqual(
            sorted(match["error"] for match in report["matches"]["center"]), [0.0, 5.0, 10.0]
        )

    def test_pointing_gate_boundary_is_inclusive_and_backwards_rays_do_not_match(self) -> None:
        rays = (
            ray(0, 0.01, station="boundary"),
            ray(1, 0.010000001, station="outside"),
            ray(2, math.pi, station="backwards"),
        )
        stats = receive(payload({TIME: (target("truth", (100.0, 0.0, 0.0)),)}, rays=rays))[0][
            "evaluation"
        ]["edge"]
        self.assert_counts(stats, outputs=3, matched=1, unmatched=2)
        self.assertAlmostEqual(stats["max_error"], 0.01, delta=1e-15)

    def test_empty_truth_and_undefined_directions_are_not_zero_error_samples(self) -> None:
        state = payload(
            {TIME: (target("at-origin", (0.0, 0.0, 0.0)),), TIME + 1: ()},
            rays=(ray(0), ray(1, time_ns=TIME + 1)),
            measurements=(measurement("empty-truth", (0.0, 0.0, 0.0), time_ns=TIME + 1),),
        )
        summary = receive(state)[0]["evaluation"]
        self.assert_counts(summary["edge"], outputs=2, matched=0, unmatched=1, unevaluable=1)
        self.assert_counts(summary["center"], outputs=1, matched=0, unmatched=1)
        for category in ("edge", "center"):
            self.assertIsNone(summary[category]["mean_error"])
            self.assertIsNone(summary[category]["rmse"])
            self.assertIsNone(summary[category]["max_error"])
        # A coincident target only removes that pair, not another valid target direction.
        stats = receive(
            payload(
                {TIME: (target("at-origin", (0.0, 0.0, 0.0)), target("defined", (1.0, 0.0, 0.0)))},
                rays=(ray(0),),
            )
        )[0]["evaluation"]["edge"]
        self.assert_counts(stats, outputs=1, matched=1)
        self.assertEqual(stats["mean_error"], 0.0)

    def test_groups_use_station_and_exact_reference_time_not_session_or_current_truth(self) -> None:
        truths = {
            TIME: (target("same-truth-id", (100.0, 0.0, 0.0)),),
            TIME + 1: (target("same-truth-id", (0.0, 100.0, 0.0), time_ns=TIME + 1),),
            TIME + 2: (),
        }
        state = payload(
            truths,
            scenario_time_ns=TIME + 2,
            rays=(
                ray(0),
                ray(1),
                ray(0, station="B"),
                ray(2, math.pi / 2, time_ns=TIME + 1),
                ray(3, time_ns=TIME + 2),
                ray(4, origin=(100.0, 0.0, 0.0), station="C"),
                ray(0, session="other-session"),
            ),
            measurements=(
                measurement("historical", (100.0, 3.0, 4.0)),
                measurement("later", (0.0, 100.0, 0.0), time_ns=TIME + 1),
                measurement("empty", (0.0, 0.0, 0.0), time_ns=TIME + 2),
            ),
        )
        report = receive(state)[0]
        self.assert_counts(
            report["evaluation"]["edge"], outputs=7, matched=3, unmatched=3, unevaluable=1
        )
        self.assert_counts(report["evaluation"]["center"], outputs=3, matched=2, unmatched=1)
        self.assertEqual(report["evaluation"]["center"]["mean_error"], 2.5)
        self.assertEqual(
            [match["time_ns"] for match in report["matches"]["center"]], [TIME, TIME + 1]
        )

    def test_maximum_cardinality_precedes_cost_and_competing_matches_minimize_total_error(
        self,
    ) -> None:
        edge_truth = (
            target("A", (1000.0, 0.0, 0.0)),
            target("B", (1000 * math.cos(0.009), 1000 * math.sin(0.009), 0.0)),
        )
        report = receive(payload({TIME: edge_truth}, rays=(ray(0, 0.004), ray(1, -0.006))))[0]
        self.assert_counts(report["evaluation"]["edge"], outputs=2, matched=2)
        self.assertEqual(
            [(match["output_id"][2], match["target_id"]) for match in report["matches"]["edge"]],
            [(0, "B"), (1, "A")],
        )
        self.assertAlmostEqual(report["evaluation"]["edge"]["mean_error"], 0.0055, delta=1e-14)
        truths = (target("A", (0.0, 0.0, 0.0)), target("B", (9.0, 0.0, 0.0)))
        report = receive(
            payload(
                {TIME: truths},
                measurements=(
                    measurement("A", (4.0, 0.0, 0.0)),
                    measurement("B", (-6.0, 0.0, 0.0)),
                ),
            )
        )[0]
        self.assertEqual(
            [(match["output_id"], match["target_id"]) for match in report["matches"]["center"]],
            [("A", "B"), ("B", "A")],
        )
        self.assertEqual(report["evaluation"]["center"]["mean_error"], 5.5)
        report = receive(
            payload(
                {TIME: (truths[0], replace(truths[1], position_enu_m=(10.0, 0.0, 0.0)))},
                measurements=(
                    measurement("first", (4.0, 0.0, 0.0)),
                    measurement("second", (1.0, 0.0, 0.0)),
                ),
            )
        )[0]
        self.assertEqual(report["evaluation"]["center"]["mean_error"], 3.5)

    def test_input_permutations_and_ties_do_not_change_matches_or_aggregates(self) -> None:
        truths = (target("A", (100.0, 0.0, 0.0)), target("B", (100.0, 0.0, 0.0)))
        rays = (ray(0), ray(1), ray(2, 0.2))
        measurements = (
            measurement("A", (100.0, 0.0, 0.0)),
            measurement("B", (100.0, 0.0, 0.0)),
            measurement("far", (200.0, 0.0, 0.0)),
        )
        states: list[dict[str, Any]] = []
        for order in itertools.permutations(range(3)):
            for truth_order in (truths, tuple(reversed(truths))):
                states.append(
                    payload(
                        {TIME: truth_order},
                        update_id=len(states) + 1,
                        rays=tuple(rays[index] for index in order),
                        measurements=tuple(measurements[index] for index in reversed(order)),
                    )
                )
        reports = receive(*states)
        for index, report in enumerate(reports, 1):
            self.assertEqual(report["matches"], reports[0]["matches"])
            for category in ("edge", "center"):
                stats = report["evaluation"][category]
                self.assert_counts(stats, outputs=3 * index, matched=2 * index, unmatched=index)
                self.assertEqual(stats["mean_error"], 0.0)
                self.assertEqual(stats["rmse"], 0.0)
                self.assertEqual(stats["max_error"], 0.0)

    def test_subpixel_offsets_at_large_enu_coordinates_and_tiny_angles_keep_double_precision(
        self,
    ) -> None:
        truth = target("truth", (1e9, 2e9, 3e9))
        state = payload(
            {TIME: (truth,)},
            rays=(ray(0, 1e-10, origin=(1e9 - 10.0, 2e9, 3e9)),),
            measurements=(measurement("quarter-metre", (1e9 + 0.25, 2e9, 3e9)),),
        )
        summary = receive(state)[0]["evaluation"]
        self.assertAlmostEqual(summary["edge"]["mean_error"], 1e-10, delta=1e-20)
        self.assertEqual(summary["center"]["mean_error"], 0.25)

    def test_complete_historical_increment_is_evaluated_before_display_eviction(self) -> None:
        now = TIME + 10_000_000_000
        state = payload(
            {TIME: (target("historical", (100.0, 0.0, 0.0)),), now: ()},
            scenario_time_ns=now,
            rays=(ray(0, 0.002),),
            measurements=(measurement("old-five", (103.0, 4.0, 0.0)),),
        )
        first, next_update = receive(
            state, payload({now + 1: ()}, update_id=2, scenario_time_ns=now + 1)
        )
        self.assertEqual(first["rays"], [])
        self.assertEqual(first["measurements"], [])
        self.assert_counts(first["evaluation"]["edge"], outputs=1, matched=1)
        self.assert_counts(first["evaluation"]["center"], outputs=1, matched=1)
        self.assertAlmostEqual(first["evaluation"]["edge"]["mean_error"], 0.002, delta=1e-14)
        self.assertEqual(first["evaluation"]["center"]["mean_error"], 5.0)
        self.assertEqual(first["evaluation"], next_update["evaluation"])
        self.assertEqual(first["matches"]["center"][0]["time_ns"], TIME)
        self.assertEqual(next_update["matches"], {"edge": [], "center": []})

    def test_godot_assignment_matches_exhaustive_maximum_cardinality_minimum_cost(self) -> None:
        rng = random.Random(47)
        cases: list[dict[str, Any]] = [
            {"columns": 0, "costs": []},
            {"columns": 3, "costs": []},
            {"columns": 0, "costs": [[], []]},
            {"columns": 2, "costs": [[1.0, 2.0], [2.0, 9.0]]},
            {"columns": 2, "costs": [[0.005, 0.005 + 1e-12], [0.005 + 2e-12, 0.005 + 4e-12]]},
        ]
        for _ in range(128):
            rows, columns = rng.randrange(1, 5), rng.randrange(1, 5)
            # Binary fractions make the exhaustive cost comparison exact, including ties.
            cases.append(
                {
                    "columns": columns,
                    "costs": [
                        [
                            rng.choice((None, None, 0.0, 0.125, 0.25, 0.5, 1.0, 2.0))
                            for _ in range(columns)
                        ]
                        for _ in range(rows)
                    ],
                }
            )
        for _ in range(64):
            for near_tie in (False, True):
                cases.append(
                    {
                        "columns": 4,
                        "costs": [
                            [
                                0.005 + rng.randrange(6) * 1e-18
                                if near_tie
                                else rng.uniform(0.0, 0.01)
                                for _ in range(4)
                            ]
                            for _ in range(4)
                        ],
                    }
                )
        # Test solver arithmetic at full double precision, without JSON decimal rounding.
        encoded = [
            {
                "columns": case["columns"],
                "costs": [
                    [None if cost is None else struct.pack("<d", cost).hex() for cost in row]
                    for row in case["costs"]
                ],
            }
            for case in cases
        ]
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            shutil.copy(transport.PROJECT / "evaluation.gd", project)
            (project / "project.godot").write_text("config_version=5\n")
            (project / "assignment.gd").write_text(ASSIGNMENT_SCRIPT)
            source = project / "cases.json"
            source.write_text(json.dumps(encoded))
            assert transport.GODOT is not None
            result = subprocess.run(
                [
                    transport.GODOT,
                    "--headless",
                    "--path",
                    str(project),
                    "--script",
                    "assignment.gd",
                    "--",
                    str(source),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
        self.assertNotIn("SCRIPT ERROR", result.stderr)
        assignments = json.loads(
            next(
                line.removeprefix("REPORT ")
                for line in result.stdout.splitlines()
                if line.startswith("REPORT ")
            )
        )
        self.assertEqual(assignments[3], [1, 0])
        self.assertEqual(
            assignments[4],
            [1, 0],
            "A tiny cost difference must not be replaced with an arbitrary tie epsilon",
        )
        for case, actual in zip(cases, assignments, strict=True):
            costs = case["costs"]
            best = (1, math.inf)
            for candidate in itertools.product(range(-1, case["columns"]), repeat=len(costs)):
                used = [column for column in candidate if column >= 0]
                if len(used) != len(set(used)) or any(
                    column >= 0 and costs[row][column] is None
                    for row, column in enumerate(candidate)
                ):
                    continue
                cost = math.fsum(
                    costs[row][column] for row, column in enumerate(candidate) if column >= 0
                )
                best = min(best, (-len(used), cost))
            used = [column for column in actual if column >= 0]
            self.assertEqual(len(actual), len(costs))
            self.assertEqual(len(used), len(set(used)))
            self.assertTrue(
                all(
                    column < 0 or costs[row][column] is not None
                    for row, column in enumerate(actual)
                )
            )
            self.assertEqual(
                (
                    -len(used),
                    math.fsum(
                        costs[row][column] for row, column in enumerate(actual) if column >= 0
                    ),
                ),
                best,
            )


@unittest.skipUnless(transport.GODOT, "Godot 4 with a real graphics backend is required")
class EvaluationPanelTests(unittest.TestCase):
    def test_panel_shows_no_sample_separately_from_zero_and_new_worker_resets_statistics(
        self,
    ) -> None:
        with layers.instrumented_renderer() as renderer:
            empty = renderer.ui_exchange({"version": 0})[0]
            self.assertEqual(empty["diagnostic_text"].count("不可计算（无匹配样本）"), 6)
            self.assertEqual(empty["evaluation"]["edge"]["sample_count"], 0)
            state = payload(
                {TIME: (target("truth", (100.0, 0.0, 0.0)),)},
                rays=(ray(0, 0.002),),
                measurements=(measurement("five", (103.0, 4.0, 0.0)),),
            )
            report = renderer.ui_exchange(state)[0]
            text = report["diagnostic_text"]
            self.assertIn("门限内匹配样本误差", text)
            self.assertIn("指向 0.01 rad；定位 10.0 m", text)
            self.assertIn("平均误差：0.002 rad", text)
            self.assertIn("平均误差：5.0 m", text)
            self.assertIn("未匹配 0 · 无法评估 0", text)
            self.assertNotIn("不可计算", text)
            duplicate = renderer.ui_exchange({**state, "version": 2})[0]
            self.assertEqual(duplicate["evaluation"], report["evaluation"])
            zero = payload(
                {TIME: (target("truth", (100.0, 0.0, 0.0)),)},
                update_id=2,
                measurements=(measurement("zero", (100.0, 0.0, 0.0)),),
            )
            report = renderer.ui_exchange({**zero, "version": 3})[0]
            self.assertIn("平均误差：2.5 m", report["diagnostic_text"])
            self.assertEqual(report["evaluation"]["center"]["sample_count"], 2)
            self.assertNotIn("SCRIPT ERROR", renderer._diagnostics())
        with layers.instrumented_renderer() as fresh:
            report = fresh.ui_exchange({"version": 1})[0]
            self.assertEqual(report["evaluation"]["center"]["sample_count"], 0)
            self.assertIsNone(report["evaluation"]["center"]["mean_error"])
            self.assertEqual(report["diagnostic_text"].count("不可计算（无匹配样本）"), 6)
            report = fresh.ui_exchange(
                {
                    **payload(
                        {TIME: (target("truth", (100.0, 0.0, 0.0)),)},
                        measurements=(measurement("zero", (100.0, 0.0, 0.0)),),
                    ),
                    "version": 2,
                }
            )[0]
            self.assertIn("平均误差：0.0 m", report["diagnostic_text"])
            self.assertEqual(report["evaluation"]["center"]["sample_count"], 1)
            self.assertEqual(report["diagnostic_text"].count("不可计算（无匹配样本）"), 3)


if __name__ == "__main__":
    unittest.main()

"""Clean center localization and geometry failures; no renderer is required."""

from __future__ import annotations

import json
import math
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import numpy as np

from chongming.composition.center import declare_center
from chongming.contracts import fusion as f
from chongming.contracts import model as m
from chongming.modules.fusion import CalibrationWeightingModule, DirectGeometryCandidateModule
from chongming.modules.fusion_association import CrossStationAssociationModule
from chongming.modules.fusion_geometry import (
    CandidateGenerationModule,
    GeometryRefinementModule,
    VoxelCandidateModule,
    _ray_voxels,
)
from tests.online_fixtures import (
    DEFAULT_SEARCH,
    SyntheticTarget,
    default_clock_mapping,
    default_fusion_policy,
    default_station_configs,
    generate_synthetic_steps,
)

ROOT = Path(__file__).resolve().parents[1]
TIME = 1_000_000
TARGET: m.Vec3 = (0.37, 0.42, 0.59)


def event_id(ray: f.AdmittedRay) -> m.EventId:
    envelope = ray.event.envelope
    return envelope.station_id, envelope.station_session_id, envelope.sequence_no


class CleanFusionTests(unittest.TestCase):
    def setUp(self) -> None:
        configs = default_station_configs()
        steps, solutions = generate_synthetic_steps(
            configs,
            (
                SyntheticTarget(
                    initial_position_enu_m=(-15.0, 62.0, 20.0), velocity_enu_mps=(1.0, 0.0, 0.0)
                ),
            ),
            duration_ns=40_000_000,
        )
        ray_event = next(rec.event for rec in steps[0].events if isinstance(rec.event, m.RayEvent))
        self.template = f.AdmittedRay(
            event=ray_event,
            arrival_time=m.ProcessingTime(
                clock_domain_id=ray_event.capture_time.clock_domain_id, time_ns=TIME
            ),
            config=next(
                config for config in configs if config.station_id == ray_event.envelope.station_id
            ),
            calibration_solution=next(
                solution
                for solution in solutions
                if solution.solution_ref == ray_event.calibration_solution_ref
            ),
        )
        self.policy = default_fusion_policy()
        self.center = m.SessionId("geometry-test")
        self.search = replace(
            DEFAULT_SEARCH,
            min_enu_m=(-3.0, -2.0, -1.0),
            max_enu_m=(3.0, 4.0, 3.0),
            step_enu_m=(1.0, 1.0, 1.0),
        )
        self.candidate_policy = replace(self.policy.candidates, search=self.search)
        self.weights = CalibrationWeightingModule(
            rules=self.policy.admission.calibration_usage_rules
        )
        self.voxels = VoxelCandidateModule(
            policy=self.candidate_policy, motion_policy=self.policy.geometry, weights=self.weights
        )
        self.associator = CrossStationAssociationModule(
            policy=replace(self.policy.association, temporal_gate_ns=20),
            motion_policy=self.policy.geometry,
            weights=self.weights,
        )
        self.geometry = GeometryRefinementModule(
            center_session_id=self.center,
            world_frame_id=self.search.world_frame_id,
            policy=self.policy.geometry,
            weights=CalibrationWeightingModule(rules=self.policy.admission.calibration_usage_rules),
        )

    def assert_position(
        self, measurement: m.FusionMeasurement, expected: m.Vec3, *, atol: float = 1e-12
    ) -> None:
        self.assertIs(measurement.status, m.FusionStatus.LOCATED)
        actual = measurement.position_enu_m
        assert actual is not None
        np.testing.assert_allclose(np.asarray(actual), np.asarray(expected), rtol=0, atol=atol)

    def ray(
        self,
        station: str,
        origin: m.Vec3,
        *,
        target: m.Vec3 = TARGET,
        direction: m.Vec3 | None = None,
        sequence: int = 2,
        offset: int = 0,
    ) -> f.AdmittedRay:
        baseline = self.template
        vector = (
            np.asarray(target) - np.asarray(origin) if direction is None else np.asarray(direction)
        )
        vector = vector / np.linalg.norm(vector)
        axis = np.eye(3)[int(np.argmin(np.abs(vector)))]
        u = np.cross(vector, axis)
        u /= np.linalg.norm(u)
        v = np.cross(vector, u)
        station_id = m.StationId(station)
        installation = replace(
            baseline.config.installation_uncertainty,
            state_ref=replace(
                baseline.config.installation_uncertainty.state_ref, station_id=station_id
            ),
        )
        ref = replace(baseline.calibration_solution.solution_ref, station_id=station_id)
        return replace(
            baseline,
            config=replace(
                baseline.config,
                station_id=station_id,
                main_camera_position_enu_m=origin,
                installation_uncertainty=installation,
            ),
            calibration_solution=replace(
                baseline.calibration_solution, solution_ref=ref, main_camera_position_enu_m=origin
            ),
            event=replace(
                baseline.event,
                envelope=replace(
                    baseline.event.envelope, station_id=station_id, sequence_no=sequence
                ),
                capture_time=replace(
                    baseline.event.capture_time,
                    exposure_start_ns=TIME + offset,
                    exposure_end_ns=TIME + offset,
                ),
                calibration_solution_ref=ref,
                ray_origin_enu_m=origin,
                ray_direction_enu_unit=(float(vector[0]), float(vector[1]), float(vector[2])),
                uncertainty=replace(
                    baseline.event.uncertainty,
                    tangent_u_enu_unit=(float(u[0]), float(u[1]), float(u[2])),
                    tangent_v_enu_unit=(float(v[0]), float(v[1]), float(v[2])),
                    shared_installation_state_ref=installation.state_ref,
                    shared_calibration_solution_ref=ref,
                ),
            ),
        )

    def rays(self) -> tuple[f.AdmittedRay, ...]:
        return (
            self.ray("a", (-2.0, -1.0, 0.0)),
            self.ray("b", (2.0, -1.0, 0.0)),
            self.ray("c", (0.0, 3.0, 0.1)),
        )

    def window(self, rays: tuple[f.AdmittedRay, ...]) -> f.SealedObservationWindow:
        now = self.template.arrival_time
        return f.SealedObservationWindow(
            key=f.WindowKey(
                center_session_id=self.center,
                capture_clock_domain_id=now.clock_domain_id,
                start_ns=TIME,
                end_ns=TIME + 1000,
            ),
            reference_time_ns=TIME + 500,
            support_exposure_span_ns=0,
            rays=rays,
            watermark_at_seal=f.WatermarkSnapshot(
                capture_clock_domain_id=now.clock_domain_id,
                event_time_ns=TIME + 1000,
                evaluated_at=now,
                station_progress=(),
            ),
            sealed_at=now,
            seal_reason=f.WindowSealReason.STREAMS_ENDED,
        )

    def association(self, window: f.SealedObservationWindow) -> f.AssociationBatch:
        rays = window.rays
        times = [
            (ray.event.capture_time.exposure_start_ns + ray.event.capture_time.exposure_end_ns) // 2
            for ray in rays
        ]
        group = f.AssociatedRayGroup(
            hypothesis_id="test-hypothesis",
            candidate_id="test-candidate",
            window=window.key,
            reference_time_ns=(min(times) + max(times)) // 2 if times else TIME,
            support_exposure_span_ns=max(times) - min(times) if times else 0,
            supporting_ray_event_ids=tuple(event_id(ray) for ray in rays),
            supporting_station_ids=tuple(ray.event.envelope.station_id for ray in rays),
            prior_track_id=None,
            motion_alignment_model=f.MotionAlignmentModel.NARROW_WINDOW_WITHOUT_PRIOR,
            score=float(len(rays)),
            score_definition=self.template.event.detection_score_definition,
        )
        return f.AssociationBatch(
            window=window.key,
            selected_groups=(group,),
            conflicts=(),
            unassigned_ray_event_ids=(),
            status=f.AssociationBatchStatus.ASSOCIATIONS_SELECTED,
        )

    def multi_association(
        self,
        rays: tuple[f.AdmittedRay, ...],
        *,
        predictions: tuple[f.TrackPrediction, ...] = (),
        policy: f.AssociationPolicy | None = None,
    ) -> tuple[f.SealedObservationWindow, f.AssociationBatch]:
        window = self.window(rays)
        associator = CrossStationAssociationModule(
            motion_policy=self.policy.geometry,
            weights=self.weights,
            policy=replace(self.associator.policy, spatial_gate_m=1e-6)
            if policy is None
            else policy,
        )
        return window, associator.associate(
            window, self.voxels.propose(window, predictions), predictions
        )

    def test_two_targets_inside_one_voxel_use_each_station_ray_once(self) -> None:
        targets: tuple[m.Vec3, ...] = (TARGET, (0.64, 0.72, 0.8))
        rays = tuple(
            self.ray(station, origin, target=target, sequence=index + 2)
            for station, origin in (
                ("a", (-2.0, -1.0, 0.0)),
                ("b", (2.0, -1.0, 0.0)),
                ("c", (0.0, 3.0, 0.1)),
            )
            for index, target in enumerate(targets)
        )
        rays = tuple(
            replace(
                ray,
                event=replace(
                    ray.event,
                    local_track_id=m.LocalTrackId("unrelated"),
                    detection_score=float(index * 100),
                ),
            )
            for index, ray in enumerate(rays)
        )
        window, association = self.multi_association(rays)
        self.assertIs(association.status, f.AssociationBatchStatus.ASSOCIATIONS_SELECTED)
        self.assertEqual(len(association.selected_groups), 2)
        assigned = [
            event_id
            for group in association.selected_groups
            for event_id in group.supporting_ray_event_ids
        ]
        self.assertEqual(len(set(assigned)), 6)
        self.assertTrue(
            all(
                len(group.supporting_station_ids) == len(set(group.supporting_station_ids)) == 3
                for group in association.selected_groups
            )
        )
        self.assertFalse(association.unassigned_ray_event_ids)
        results = sorted(
            self.geometry.refine(window, association, ()).results,
            key=lambda result: result.measurement.position_enu_m or (),
        )
        for result, target in zip(results, targets, strict=True):
            self.assert_position(result.measurement, target)
        reordered = self.multi_association(tuple(reversed(rays)))[1]
        self.assertEqual(association, reordered)

    def test_two_station_ghost_pairings_need_evidence_not_detector_labels(self) -> None:
        targets: tuple[m.Vec3, ...] = ((-0.5, 1.0, 0.5), (0.5, 1.0, 0.5))
        rays = tuple(
            self.ray(station, origin, target=target, sequence=index + 2)
            for station, origin in (("a", (-2.0, -1.0, 0.5)), ("b", (2.0, -1.0, 0.5)))
            for index, target in enumerate(targets)
        )
        window, ambiguous = self.multi_association(rays)
        self.assertIs(ambiguous.status, f.AssociationBatchStatus.ASSOCIATION_AMBIGUOUS)
        self.assertFalse(ambiguous.selected_groups)
        self.assertEqual(len(ambiguous.unassigned_ray_event_ids), 4)
        predictions = tuple(
            f.TrackPrediction(
                center_session_id=self.center,
                track_id=m.TrackId(f"prior-{index}"),
                clock_domain_id=window.key.capture_clock_domain_id,
                world_frame_id=self.search.world_frame_id,
                reference_time_ns=TIME,
                valid_until_ns=10**18,
                position_enu_m=target,
                velocity_enu_mps=(0.0, 0.0, 0.0),
                conditional_state_covariance_enu=None,
            )
            for index, target in enumerate(targets)
        )
        policy = replace(self.associator.policy, spatial_gate_m=1e-6, prediction_spatial_gate_m=0.1)
        _, selected = self.multi_association(rays, predictions=predictions, policy=policy)
        self.assertIs(selected.status, f.AssociationBatchStatus.ASSOCIATIONS_SELECTED)
        self.assertEqual(
            {group.prior_track_id for group in selected.selected_groups},
            {prediction.track_id for prediction in predictions},
        )
        self.assertEqual(len(selected.selected_groups), 2)
        _, new_target = self.multi_association(rays, predictions=predictions[:1], policy=policy)
        self.assertEqual(len(new_target.selected_groups), 2)
        self.assertEqual(
            sum(group.prior_track_id is None for group in new_target.selected_groups), 1
        )
        self.assertTrue(self.voxels.propose(window, predictions[:1]).unguided_search_performed)

    def test_independent_target_survives_an_ambiguous_or_truncated_component(self) -> None:
        first, second, third = self.rays()
        duplicate = replace(
            first,
            event=replace(first.event, envelope=replace(first.event.envelope, sequence_no=99)),
        )
        target: m.Vec3 = (1.7, 2.0, 1.4)
        independent = (
            self.ray("d", (-2.0, 3.0, 1.0), target=target),
            self.ray("e", (2.0, 3.0, 1.0), target=target),
        )
        rays = (first, second, third, duplicate, *independent)
        for nodes in (20000, 4):
            with self.subTest(maximum_nodes=nodes):
                policy = replace(
                    self.associator.policy, spatial_gate_m=1e-6, maximum_search_nodes=nodes
                )
                window, association = self.multi_association(rays, policy=policy)
                self.assertIs(association.status, f.AssociationBatchStatus.PARTIALLY_ASSOCIATED)
                self.assertEqual(len(association.selected_groups), 1)
                self.assertEqual(
                    set(association.selected_groups[0].supporting_station_ids), {"d", "e"}
                )
                self.assertEqual(len(association.unassigned_ray_event_ids), 4)
                (result,) = self.geometry.refine(window, association, ()).results
                self.assert_position(result.measurement, target)
                if nodes == 4:
                    self.assertTrue(
                        any(
                            f.AssociationConflictReason.ASSOCIATION_SEARCH_TRUNCATED
                            in conflict.reasons
                            for conflict in association.conflicts
                        )
                    )

    def test_station_subsets_recover_when_an_extra_ray_fails_the_temporal_gate(self) -> None:
        first, second, third = self.rays()
        third = replace(
            third,
            event=replace(
                third.event,
                capture_time=replace(
                    third.event.capture_time, exposure_start_ns=TIME + 50, exposure_end_ns=TIME + 50
                ),
            ),
        )
        window, association = self.multi_association((first, second, third))
        self.assertIs(association.status, f.AssociationBatchStatus.ASSOCIATIONS_SELECTED)
        (group,) = association.selected_groups
        self.assertEqual(group.supporting_station_ids, ("a", "b"))
        self.assertEqual(association.unassigned_ray_event_ids, (event_id(third),))
        (result,) = self.geometry.refine(window, association, ()).results
        self.assert_position(result.measurement, TARGET)
        self.assertTrue(
            any(
                f.AssociationConflictReason.TEMPORAL_GATE_FAILED in conflict.reasons
                for conflict in association.conflicts
            )
        )

    def test_combination_budget_never_turns_partial_enumeration_into_success(self) -> None:
        window, association = self.multi_association(
            self.rays(), policy=replace(self.associator.policy, maximum_hypotheses=1)
        )
        self.assertIs(association.status, f.AssociationBatchStatus.ASSOCIATION_AMBIGUOUS)
        self.assertFalse(association.selected_groups)
        self.assertEqual(len(association.unassigned_ray_event_ids), 3)
        self.assertTrue(
            any(
                f.AssociationConflictReason.ASSOCIATION_SEARCH_TRUNCATED in conflict.reasons
                for conflict in association.conflicts
            )
        )
        self.assertFalse(self.geometry.refine(window, association, ()).results)

    def test_prior_expansion_is_bounded_even_for_one_ray_combination(self) -> None:
        rays = self.rays()[:2]
        window = self.window(rays)
        predictions = tuple(
            f.TrackPrediction(
                center_session_id=self.center,
                track_id=m.TrackId(f"nearby-{index}"),
                clock_domain_id=window.key.capture_clock_domain_id,
                world_frame_id=self.search.world_frame_id,
                reference_time_ns=TIME,
                valid_until_ns=10**18,
                position_enu_m=TARGET,
                velocity_enu_mps=(0.0, 0.0, 0.0),
                conditional_state_covariance_enu=None,
            )
            for index in range(4)
        )
        _, association = self.multi_association(
            rays,
            predictions=predictions,
            policy=replace(self.associator.policy, maximum_hypotheses=4),
        )
        self.assertIs(association.status, f.AssociationBatchStatus.ASSOCIATION_AMBIGUOUS)
        self.assertFalse(association.selected_groups)
        self.assertTrue(
            any(
                f.AssociationConflictReason.ASSOCIATION_SEARCH_TRUNCATED in conflict.reasons
                for conflict in association.conflicts
            )
        )

    def test_rank_deficient_hypotheses_do_not_inflate_assignment_support(self) -> None:
        rays = tuple(
            self.ray(station, (-2.0, -1.0, 0.0), direction=(1.0, 0.0, 0.0))
            for station in ("a", "b", "c")
        )
        window, association = self.multi_association(rays)
        self.assertIs(association.status, f.AssociationBatchStatus.NO_VALID_ASSOCIATION)
        self.assertFalse(association.selected_groups)
        self.assertTrue(
            all(
                f.AssociationConflictReason.DEGENERATE_GEOMETRY in conflict.reasons
                for conflict in association.conflicts
            )
        )
        self.assertFalse(self.geometry.refine(window, association, ()).results)

    def test_prediction_without_current_independent_station_support_is_not_a_target(self) -> None:
        ray = self.rays()[0]
        window = self.window((ray,))
        prediction = f.TrackPrediction(
            center_session_id=self.center,
            track_id=m.TrackId("old"),
            clock_domain_id=window.key.capture_clock_domain_id,
            world_frame_id=self.search.world_frame_id,
            reference_time_ns=TIME,
            valid_until_ns=10**18,
            position_enu_m=TARGET,
            velocity_enu_mps=(0.0, 0.0, 0.0),
            conditional_state_covariance_enu=None,
        )
        _, association = self.multi_association((ray,), predictions=(prediction,))
        self.assertIs(association.status, f.AssociationBatchStatus.INSUFFICIENT_STATIONS)
        self.assertFalse(association.selected_groups)
        with self.assertRaisesRegex(ValueError, "prediction"):
            self.multi_association(
                (ray,), predictions=(replace(prediction, world_frame_id=m.WorldFrameId("wrong")),)
            )
        for policy in (
            replace(self.associator.policy, maximum_search_nodes=0),
            replace(self.associator.policy, maximum_hypotheses=0),
            replace(self.associator.policy, prediction_spatial_gate_m=0),
        ):
            with self.assertRaisesRegex(ValueError, "association policy"):
                CrossStationAssociationModule(
                    policy=policy, motion_policy=self.policy.geometry, weights=self.weights
                )

    def test_forward_traversal_supercovers_boundaries_and_clips_range(self) -> None:
        search = replace(self.search, min_enu_m=(0.0, 0.0, 0.0), max_enu_m=(2.0, 2.0, 2.0))
        cases: tuple[
            tuple[m.Vec3, m.Vec3, tuple[float, float] | None, set[tuple[int, int, int]]], ...
        ] = (
            ((-1.0, 0.5, 0.5), (1.0, 0.0, 0.0), None, {(0, 0, 0), (1, 0, 0)}),
            ((0.5, 0.5, 0.5), (1.0, 0.0, 0.0), None, {(0, 0, 0), (1, 0, 0)}),
            ((3.0, 0.5, 0.5), (-1.0, 0.0, 0.0), None, {(0, 0, 0), (1, 0, 0)}),
            ((3.0, 0.5, 0.5), (1.0, 0.0, 0.0), None, set()),
            ((-1.0, 3.0, 0.5), (1.0, 0.0, 0.0), None, set()),
            ((-1.0, 0.5, 0.5), (1.0, 0.0, 0.0), (0.0, 0.5), set()),
            ((-1.0, 0.5, 0.5), (1.0, 0.0, 0.0), (1.0, 1.5), {(0, 0, 0)}),
            ((-1.0, 0.5, 0.5), (1.0, 0.0, 0.0), (0.0, 1.0), {(0, 0, 0)}),
            (
                (-1.0, 1.0, 0.5),
                (1.0, 0.0, 0.0),
                None,
                {(x, y, 0) for x in range(2) for y in range(2)},
            ),
            ((-1.0, 2.0, 0.5), (1.0, 0.0, 0.0), None, {(0, 1, 0), (1, 1, 0)}),
            (
                (-1.0, -1.0, 0.5),
                (1.0, 1.0, 0.0),
                None,
                {(x, y, 0) for x in range(2) for y in range(2)},
            ),
            (
                (-1.0, -1.0, -1.0),
                (1.0, 1.0, 1.0),
                None,
                {(x, y, z) for x in range(2) for y in range(2) for z in range(2)},
            ),
            ((-1.0, 1.0, 0.5), (1.0, 1.0, 0.0), None, {(0, 1, 0)}),
        )
        for origin, direction, forward, expected in cases:
            with self.subTest(origin=origin, direction=direction, forward=forward):
                actual = _ray_voxels(
                    self.ray("a", origin, direction=direction),
                    replace(search, forward_range_m=forward),
                )
                self.assertEqual(actual, expected)
        partial = replace(search, max_enu_m=(2.5, 2.0, 2.0))
        self.assertEqual(
            _ray_voxels(self.ray("a", (-1.0, 0.5, 0.5), direction=(1.0, 0.0, 0.0)), partial),
            {(0, 0, 0), (1, 0, 0), (2, 0, 0)},
        )

    def test_station_votes_and_duplicate_voxels_are_not_extra_targets(self) -> None:
        first = self.ray("a", (-2.0, 0.0, 0.0), direction=(1.0, 0.0, 0.0))
        alternate = replace(
            first,
            event=replace(
                first.event,
                envelope=replace(first.event.envelope, sequence_no=3),
                detection_score=999,
            ),
        )
        second = self.ray("b", (-2.0, 0.0, 0.0), direction=(1.0, 0.0, 0.0))
        batch = self.voxels.propose(self.window((first, alternate)), ())
        self.assertIs(batch.status, f.CandidateBatchStatus.INSUFFICIENT_STATIONS)
        batch = self.voxels.propose(self.window((first, alternate, second)), ())
        self.assertEqual(len(batch.candidates), 1)
        candidate = batch.candidates[0]
        self.assertEqual(candidate.score, 2)
        self.assertIs(candidate.score_definition.kind, m.ScoreKind.RANKING)
        self.assertEqual(
            [
                len(support.alternative_ray_event_ids)
                for support in candidate.independent_station_support
            ],
            [2, 1],
        )
        self.assertEqual(candidate.region_max_enu_m, (3.0, 1.0, 1.0))
        self.assertTrue(batch.unguided_search_performed)
        self.assertIs(candidate.origin, f.CandidateOrigin.UNGUIDED_SEARCH)

    def test_no_candidate_is_limited_to_the_search_volume(self) -> None:
        rays = tuple(
            self.ray(station, (4.0, float(index), 0.5), direction=(1.0, 0.0, 0.0))
            for index, station in enumerate(("a", "b"))
        )
        window = self.window(rays)
        batch = self.voxels.propose(window, ())
        self.assertIs(batch.status, f.CandidateBatchStatus.NO_CANDIDATE_IN_VOLUME)
        self.assertFalse(batch.candidates)
        self.assertCountEqual(batch.examined_ray_event_ids, [event_id(ray) for ray in rays])
        association = self.associator.associate(window, batch, ())
        self.assertIs(association.status, f.AssociationBatchStatus.NO_CANDIDATE_IN_VOLUME)
        self.assertFalse(self.geometry.refine(window, association, ()).results)
        empty = self.window(())
        self.assertIs(
            self.voxels.propose(empty, ()).status, f.CandidateBatchStatus.INSUFFICIENT_STATIONS
        )
        strict = VoxelCandidateModule(
            policy=replace(self.candidate_policy, minimum_candidate_score=4),
            motion_policy=self.policy.geometry,
            weights=self.weights,
        )
        self.assertFalse(strict.propose(self.window(self.rays()), ()).candidates)

    def test_clean_pipeline_solves_off_grid_in_three_dimensions(self) -> None:
        for count in (2, 3):
            for step in (0.5, 1.0, 2.5):
                with self.subTest(stations=count, voxel_step=step):
                    window = self.window(self.rays()[:count])
                    policy = replace(
                        self.candidate_policy,
                        search=replace(self.search, step_enu_m=(step, step, step)),
                    )
                    candidates = VoxelCandidateModule(
                        policy=policy, motion_policy=self.policy.geometry, weights=self.weights
                    ).propose(window, ())
                    association = self.associator.associate(window, candidates, ())
                    self.assertIs(
                        association.status, f.AssociationBatchStatus.ASSOCIATIONS_SELECTED
                    )
                    self.assertEqual(len(association.selected_groups), 1)
                    self.assertEqual(
                        len(association.selected_groups[0].supporting_station_ids), count
                    )
                    (result,) = self.geometry.refine(window, association, ()).results
                    self.assert_position(result.measurement, TARGET)
                    self.assertEqual(result.measurement.reference_time_ns, TIME)
                    self.assertNotEqual(
                        result.measurement.reference_time_ns, window.reference_time_ns
                    )
                    self.assertEqual(len(result.measurement.residuals_m), count)
                    self.assertEqual(
                        result.measurement.conditional_position_covariance_enu_m2,
                        ((0.0, 0.0, 0.0),) * 3,
                    )
                    self.assertEqual(len(result.shared_installation_state_refs), count)
                    self.assertEqual(len(result.shared_calibration_solution_refs), count)
                    self.assertFalse(result.reasons)

    def test_grid_vertex_intersection_is_not_lost(self) -> None:
        rays = (
            self.ray("a", (-2.0, -2.0, 0.0), target=(0.0, 0.0, 0.0)),
            self.ray("b", (-2.0, 2.0, 0.0), target=(0.0, 0.0, 0.0)),
        )
        window = self.window(rays)
        association = self.associator.associate(window, self.voxels.propose(window, ()), ())
        (result,) = self.geometry.refine(window, association, ()).results
        self.assert_position(result.measurement, (0, 0, 0))

    def test_input_order_and_track_predictions_do_not_change_unguided_search(self) -> None:
        window = self.window(self.rays())
        prediction = f.TrackPrediction(
            center_session_id=self.center,
            track_id=m.TrackId("unrelated"),
            clock_domain_id=window.key.capture_clock_domain_id,
            world_frame_id=self.search.world_frame_id,
            reference_time_ns=TIME,
            valid_until_ns=10**18,
            position_enu_m=(1000.0, 1000.0, 1000.0),
            velocity_enu_mps=(0.0, 0.0, 0.0),
            conditional_state_covariance_enu=None,
        )
        first = self.voxels.propose(window, ())
        reordered = replace(window, rays=tuple(reversed(window.rays)))
        second = self.voxels.propose(reordered, (prediction,))
        self.assertEqual(first, second)
        association = self.associator.associate(window, first, ())
        self.assertEqual(association, self.associator.associate(reordered, second, (prediction,)))
        self.assertIsNone(association.selected_groups[0].prior_track_id)
        self.assertEqual(
            self.geometry.refine(window, association, ()),
            self.geometry.refine(reordered, association, (prediction,)),
        )

    def test_alternative_rays_remain_ambiguous_even_with_different_scores(self) -> None:
        first, second = self.rays()[:2]
        alternate = replace(
            first,
            event=replace(
                first.event,
                envelope=replace(first.event.envelope, sequence_no=3),
                detection_score=999,
            ),
        )
        window = self.window((first, alternate, second))
        association = self.associator.associate(window, self.voxels.propose(window, ()), ())
        self.assertIs(association.status, f.AssociationBatchStatus.ASSOCIATION_AMBIGUOUS)
        self.assertFalse(association.selected_groups)
        self.assertEqual(len(association.unassigned_ray_event_ids), 3)
        self.assertTrue(
            any(
                f.AssociationConflictReason.MULTIPLE_SUPPORT_RAYS_FROM_ONE_STATION
                in conflict.reasons
                for conflict in association.conflicts
            )
        )
        self.assertFalse(self.geometry.refine(window, association, ()).results)

    def test_competing_support_sets_are_retained_not_assigned_twice(self) -> None:
        first, second, third = self.rays()
        window = self.window((first, second, third))
        base = self.voxels.propose(window, ()).candidates[0]
        candidates = tuple(
            replace(
                base,
                candidate_id=f"pair-{index}",
                independent_station_support=tuple(
                    f.IndependentStationSupport(
                        station_id=ray.event.envelope.station_id,
                        alternative_ray_event_ids=(event_id(ray),),
                    )
                    for ray in pair
                ),
            )
            for index, pair in enumerate(((first, second), (second, third)))
        )
        batch = replace(self.voxels.propose(window, ()), candidates=candidates)
        association = self.associator.associate(window, batch, ())
        self.assertIs(association.status, f.AssociationBatchStatus.ASSOCIATION_AMBIGUOUS)
        self.assertFalse(association.selected_groups)
        self.assertCountEqual(
            association.unassigned_ray_event_ids, [event_id(ray) for ray in window.rays]
        )
        self.assertEqual(
            association.conflicts[0].reasons,
            (f.AssociationConflictReason.COMPETING_TARGET_HYPOTHESES,),
        )

    def test_candidate_cap_cannot_turn_incomplete_search_into_unique_association(self) -> None:
        rays = (
            *self.rays(),
            self.ray("a", (-2.0, -1.0, 0.0), target=(1.7, 1.4, 1.2), sequence=3),
            self.ray("b", (2.0, -1.0, 0.0), target=(1.7, 1.4, 1.2), sequence=3),
        )
        window = self.window(rays)
        complete = self.voxels.propose(window, ())
        self.assertGreater(len(complete.candidates), 1)
        capped = VoxelCandidateModule(
            policy=replace(self.candidate_policy, maximum_candidates=1),
            motion_policy=self.policy.geometry,
            weights=self.weights,
        ).propose(window, ())
        self.assertEqual(len(capped.candidates), 1)
        self.assertEqual(capped.candidates, complete.candidates[:1])
        self.assertIn("candidate_limit_reached", capped.reason_codes)
        association = self.associator.associate(window, capped, ())
        self.assertIs(association.status, f.AssociationBatchStatus.ASSOCIATION_AMBIGUOUS)
        self.assertFalse(association.selected_groups)
        self.assertTrue(
            any(
                f.AssociationConflictReason.CANDIDATE_SEARCH_TRUNCATED in conflict.reasons
                for conflict in association.conflicts
            )
        )

    def test_temporal_and_spatial_gates_reject_without_false_absence(self) -> None:
        first, second = self.rays()[:2]
        late = replace(
            second,
            event=replace(
                second.event,
                capture_time=replace(
                    second.event.capture_time,
                    exposure_start_ns=TIME + 21,
                    exposure_end_ns=TIME + 21,
                ),
            ),
        )
        window = self.window((first, late))
        association = self.associator.associate(window, self.voxels.propose(window, ()), ())
        self.assertIs(association.status, f.AssociationBatchStatus.NO_VALID_ASSOCIATION)
        self.assertFalse(association.selected_groups)
        self.assertTrue(
            all(
                f.AssociationConflictReason.TEMPORAL_GATE_FAILED in conflict.reasons
                for conflict in association.conflicts
            )
        )
        near = replace(
            late,
            event=replace(
                late.event,
                capture_time=replace(
                    late.event.capture_time, exposure_start_ns=TIME + 20, exposure_end_ns=TIME + 20
                ),
            ),
        )
        window = self.window((first, near))
        association = self.associator.associate(window, self.voxels.propose(window, ()), ())
        self.assertIs(association.status, f.AssociationBatchStatus.ASSOCIATIONS_SELECTED)
        self.assertEqual(association.selected_groups[0].reference_time_ns, TIME + 10)
        self.assertEqual(association.selected_groups[0].support_exposure_span_ns, 20)
        skew = self.window(
            (
                self.ray("a", (0.0, 0.0, 0.0), direction=(1.0, 0.0, 0.0)),
                self.ray("b", (1.0, -1.0, 1.0), direction=(0.0, 1.0, 0.0)),
            )
        )
        strict = replace(
            self.associator, policy=replace(self.associator.policy, spatial_gate_m=0.4)
        )
        association = strict.associate(skew, self.voxels.propose(skew, ()), ())
        self.assertIs(association.status, f.AssociationBatchStatus.NO_VALID_ASSOCIATION)
        self.assertTrue(
            any(
                f.AssociationConflictReason.SPATIAL_GATE_FAILED in conflict.reasons
                for conflict in association.conflicts
            )
        )

    def test_coarse_overlap_cannot_support_a_solution_outside_the_clipped_search(self) -> None:
        cases: tuple[tuple[m.Vec3, tuple[float, float] | None, bool], ...] = (
            ((1.1, 0.5, 0.5), None, False),
            ((1.0, 0.5, 0.5), None, True),
            ((0.9, 0.5, 0.5), (0.0, 1.0), False),
            ((0.5, 0.5, 0.5), (0.8, 1.0), False),
        )
        for target, forward, accepted in cases:
            with self.subTest(target=target, forward=forward):
                search = replace(
                    self.search,
                    min_enu_m=(0.0, 0.0, 0.0),
                    max_enu_m=(1.0, 1.0, 1.0),
                    forward_range_m=forward,
                )
                voxels = VoxelCandidateModule(
                    policy=replace(self.candidate_policy, search=search),
                    motion_policy=self.policy.geometry,
                    weights=self.weights,
                )
                window = self.window(
                    (
                        self.ray("a", (0.0, 0.0, 0.5), target=target),
                        self.ray("b", (0.0, 1.0, 0.5), target=target),
                    )
                )
                candidates = voxels.propose(window, ())
                self.assertEqual(len(candidates.candidates), 1)
                association = self.associator.associate(window, candidates, ())
                if accepted:
                    (result,) = self.geometry.refine(window, association, ()).results
                    self.assert_position(result.measurement, target)
                else:
                    self.assertIs(association.status, f.AssociationBatchStatus.NO_VALID_ASSOCIATION)
                    self.assertFalse(association.selected_groups)
                    self.assertEqual(
                        association.conflicts[0].reasons,
                        (f.AssociationConflictReason.SPATIAL_GATE_FAILED,),
                    )

    def test_numerical_solver_failure_is_a_diagnostic_not_a_position(self) -> None:
        window = self.window(self.rays()[:2])
        candidates = self.voxels.propose(window, ())
        with patch(
            "chongming.modules.fusion_geometry.np.linalg.lstsq",
            side_effect=np.linalg.LinAlgError("solver failed"),
        ):
            (result,) = self.geometry.refine(window, self.association(window), ()).results
            self.assertEqual(result.reasons, (f.GeometryReason.SOLVER_FAILURE,))
            self.assertIs(result.measurement.status, m.FusionStatus.REJECTED_INPUT)
            self.assertIsNone(result.measurement.position_enu_m)
            association = self.associator.associate(window, candidates, ())
            self.assertIs(association.status, f.AssociationBatchStatus.NO_VALID_ASSOCIATION)
            self.assertFalse(association.selected_groups)
            self.assertTrue(
                all(
                    f.AssociationConflictReason.SPATIAL_GATE_FAILED in conflict.reasons
                    for conflict in association.conflicts
                )
            )

    def test_continuous_fit_is_joint_not_an_average_of_pair_intersections(self) -> None:
        rays: tuple[f.AdmittedRay, ...] = (
            self.ray("a", (0.0, 0.0, 0.0), direction=(1.0, 0.0, 0.0)),
            self.ray("b", (1.0, -1.0, 1.0), direction=(0.0, 1.0, 0.0)),
            self.ray("c", (2.0, 0.0, -1.0), direction=(0.0, 0.0, 1.0)),
            self.ray("d", (0.0, 2.0, 0.0), direction=(1.0, 0.0, 0.0)),
        )
        # Skew observations need declared independent noise. Exact zero noise
        # would instead make these mutually inconsistent equality constraints.
        rays = tuple(
            replace(
                ray,
                event=replace(
                    ray.event,
                    uncertainty=replace(
                        ray.event.uncertainty,
                        independent_origin_covariance_m2=(
                            (1.0, 0.0, 0.0),
                            (0.0, 1.0, 0.0),
                            (0.0, 0.0, 1.0),
                        ),
                    ),
                ),
            )
            for ray in rays
        )
        window = self.window(rays)
        geometry = replace(
            self.geometry, policy=replace(self.geometry.policy, maximum_residual_m=2)
        )
        (result,) = geometry.refine(window, self.association(window), ()).results
        self.assert_position(result.measurement, (1.5, 2 / 3, 1 / 3))
        self.assertEqual(result.measurement.minimum_intersection_angle_rad, 0)
        self.assertEqual(len(result.measurement.residuals_m), 4)
        condition = result.measurement.geometry_condition
        assert condition is not None
        self.assertAlmostEqual(condition, 1.5)

    def test_geometry_degeneracy_retains_evidence_without_a_position(self) -> None:
        cases = (
            (
                (
                    self.ray("a", (0.0, 0.0, 0.0), direction=(1.0, 0.0, 0.0)),
                    self.ray("b", (0.0, 1.0, 0.0), direction=(1.0, 0.0, 0.0)),
                ),
                f.GeometryReason.NEAR_PARALLEL_RAYS,
            ),
            (
                (
                    self.ray("a", (0.0, 0.0, 0.0), direction=(1.0, 0.0, 0.0)),
                    self.ray("b", (0.0, 1.0, 0.0), direction=(-1.0, 0.0, 0.0)),
                ),
                f.GeometryReason.NEAR_PARALLEL_RAYS,
            ),
            (
                (
                    self.ray("a", (0.0, 0.0, 0.0), direction=(1.0, 0.0, 0.0)),
                    self.ray("b", (0.0, 0.00001, 0.0), direction=(1.0, -0.00001, 0.0)),
                ),
                f.GeometryReason.POOR_CONDITION,
            ),
            (
                (
                    self.ray("a", (0.0, 0.0, 0.0), direction=(1.0, 0.0, 0.0)),
                    self.ray("b", (1.0, -1.0, 5.0), direction=(0.0, 1.0, 0.0)),
                ),
                f.GeometryReason.EXCESSIVE_RESIDUAL,
            ),
            (
                (
                    self.ray("a", (0.0, 0.0, 0.0), direction=(1.0, 0.0, 0.0)),
                    self.ray("b", (-1.0, -1.0, 0.0), direction=(0.0, 1.0, 0.0)),
                ),
                f.GeometryReason.NONFORWARD_SOLUTION,
            ),
        )
        for rays, reason in cases:
            with self.subTest(reason=reason):
                window = self.window(rays)
                (result,) = self.geometry.refine(window, self.association(window), ()).results
                self.assertIs(result.measurement.status, m.FusionStatus.GEOMETRY_DEGENERATE)
                self.assertIn(reason, result.reasons)
                self.assertIsNone(result.measurement.position_enu_m)
                self.assertIsNone(result.measurement.conditional_position_covariance_enu_m2)
                self.assertEqual(
                    result.measurement.supporting_ray_event_ids,
                    tuple(event_id(ray) for ray in rays),
                )
                self.assertEqual(
                    result.measurement.reason_codes, tuple(item.value for item in result.reasons)
                )
                json.dumps(asdict(result), allow_nan=False)

        window = self.window(self.rays()[:2])
        for policy, reason in (
            (
                replace(self.geometry.policy, minimum_intersection_angle_rad=math.pi / 2),
                f.GeometryReason.NEAR_PARALLEL_RAYS,
            ),
            (
                replace(self.geometry.policy, maximum_condition_number=1),
                f.GeometryReason.POOR_CONDITION,
            ),
        ):
            with self.subTest(limit=reason):
                (result,) = (
                    replace(self.geometry, policy=policy)
                    .refine(window, self.association(window), ())
                    .results
                )
                self.assertEqual(result.reasons, (reason,))
                self.assertIsNone(result.measurement.position_enu_m)

    def test_insufficient_or_repeated_station_support_is_not_localized(self) -> None:
        window = self.window(self.rays()[:1])
        (result,) = self.geometry.refine(window, self.association(window), ()).results
        self.assertIs(result.measurement.status, m.FusionStatus.INSUFFICIENT_STATIONS)
        self.assertEqual(result.reasons, (f.GeometryReason.INSUFFICIENT_STATIONS,))
        first = window.rays[0]
        duplicate = replace(
            first, event=replace(first.event, envelope=replace(first.event.envelope, sequence_no=3))
        )
        window = self.window((first, duplicate))
        (result,) = self.geometry.refine(window, self.association(window), ()).results
        self.assertIs(result.measurement.status, m.FusionStatus.REJECTED_INPUT)
        self.assertEqual(result.reasons, (f.GeometryReason.INVALID_SUPPORT_GROUP,))
        self.assertIsNone(result.measurement.position_enu_m)

    def test_unknown_uncertainty_and_speed_bound_models_do_not_claim_success(self) -> None:
        first, second = self.rays()[:2]
        cases = (
            (
                replace(
                    first,
                    event=replace(
                        first.event,
                        uncertainty=replace(first.event.uncertainty, model="unknown_uncertainty"),
                    ),
                ),
                f.GeometryReason.UNSUPPORTED_UNCERTAINTY_MODEL,
            ),
        )
        for ray, reason in cases:
            window = self.window((ray, second))
            (result,) = self.geometry.refine(window, self.association(window), ()).results
            self.assertIs(result.measurement.status, m.FusionStatus.REJECTED_INPUT)
            self.assertEqual(result.reasons, (reason,))
            self.assertIsNone(result.measurement.position_enu_m)
        window = self.window((first, second))
        association = self.association(window)
        group = replace(association.selected_groups[0], reference_time_ns=window.reference_time_ns)
        (result,) = self.geometry.refine(
            window, replace(association, selected_groups=(group,)), ()
        ).results
        self.assertEqual(result.reasons, (f.GeometryReason.REFERENCE_TIME_ALIGNMENT_UNAVAILABLE,))
        for policy in (
            replace(self.geometry.policy, robust_loss_definition="unknown_loss"),
            replace(
                self.geometry.policy,
                motion_alignment_model=f.MotionAlignmentModel.CONSERVATIVE_SPEED_BOUND,
            ),
        ):
            with self.assertRaises(NotImplementedError):
                replace(self.geometry, policy=policy).refine(window, association, ())

    def test_strategy_selection_keeps_direct_geometry_explicitly_unimplemented(self) -> None:
        direct = DirectGeometryCandidateModule(search=self.search)
        generator = CandidateGenerationModule(
            policy=self.candidate_policy, voxel=self.voxels, direct=direct
        )
        window = self.window(self.rays()[:2])
        self.assertEqual(generator.generate(window, ()), self.voxels.propose(window, ()))
        with self.assertRaises(NotImplementedError):
            replace(
                generator,
                policy=replace(
                    self.candidate_policy, primary_method=f.CandidateMethod.DIRECT_GEOMETRY
                ),
            ).generate(window, ())

    def test_policy_and_cross_stage_reference_errors_are_explicit(self) -> None:
        for policy in (
            replace(self.candidate_policy, minimum_independent_stations=1),
            replace(self.candidate_policy, maximum_candidates=0),
            replace(self.candidate_policy, minimum_candidate_score=math.nan),
            replace(self.candidate_policy, search=replace(self.search, step_enu_m=(1.0, 0.0, 1.0))),
            replace(
                self.candidate_policy, search=replace(self.search, max_enu_m=self.search.min_enu_m)
            ),
            replace(
                self.candidate_policy, search=replace(self.search, forward_range_m=(-1.0, 1.0))
            ),
        ):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                VoxelCandidateModule(
                    policy=policy, motion_policy=self.policy.geometry, weights=self.weights
                )
        with self.assertRaises(ValueError):
            replace(self.associator, policy=replace(self.associator.policy, temporal_gate_ns=-1))
        with self.assertRaises(ValueError):
            replace(self.geometry, policy=replace(self.geometry.policy, maximum_condition_number=0))
        window = self.window(self.rays())
        candidates = self.voxels.propose(window, ())
        wrong = replace(window.key, center_session_id=m.SessionId("other-center"))
        with self.assertRaisesRegex(ValueError, "windows differ"):
            self.associator.associate(window, replace(candidates, window=wrong), ())
        association = self.association(window)
        with self.assertRaisesRegex(ValueError, "windows differ"):
            self.geometry.refine(window, replace(association, window=wrong), ())
        with self.assertRaisesRegex(ValueError, "session or world"):
            replace(self.geometry, world_frame_id=m.WorldFrameId("other-world")).refine(
                window, association, ()
            )
        group = replace(
            association.selected_groups[0],
            supporting_ray_event_ids=((m.StationId("unknown"), m.SessionId("unknown"), 1),),
        )
        with self.assertRaisesRegex(ValueError, "outside the sealed window"):
            self.geometry.refine(window, replace(association, selected_groups=(group,)), ())

    def test_finite_clean_source_runs_through_admission_windows_and_localization(self) -> None:
        configs = default_station_configs()
        policy = default_fusion_policy()
        clock_mapping = default_clock_mapping()
        center, _ = declare_center(
            center_session_id=m.SessionId("center-clean-finite"),
            policy=policy,
            clock_mapping=clock_mapping,
        )
        for config in configs:
            center.stations.register(config)
        measurements: list[m.FusionMeasurement] = []
        expected_times: set[int] = set()
        last: dict[tuple[m.StationId, m.SessionId, m.ConfigDigest], m.CaptureTime] = {}

        def consume(update: f.WindowUpdate) -> None:
            for window in update.sealed_windows:
                candidates = center.candidates.generate(window, ())
                association = center.association.associate(window, candidates, ())
                self.assertIs(association.status, f.AssociationBatchStatus.ASSOCIATIONS_SELECTED)
                batch = center.geometry.refine(window, association, ())
                self.assertEqual(len(batch.results), 1)
                measurements.extend(result.measurement for result in batch.results)

        steps, _ = generate_synthetic_steps(
            configs,
            (
                SyntheticTarget(
                    initial_position_enu_m=(-15.0, 62.0, 20.0), velocity_enu_mps=(1.0, 0.0, 0.0)
                ),
            ),
            duration_ns=100_000_000,
        )
        for step in steps:
            if step.stream_ended:
                continue
            for solution in step.solutions:
                center.calibrations.register_solution(solution)
            for received in step.events:
                outcome = center.admission.admit(received)
                self.assertIs(outcome.record.decision, f.AdmissionDecision.ACCEPTED)
                if outcome.admitted_ray is not None:
                    capture = outcome.admitted_ray.event.capture_time
                    envelope = received.event.envelope
                    last[
                        envelope.station_id,
                        envelope.station_session_id,
                        envelope.station_config_digest,
                    ] = capture
                    expected_times.add((capture.exposure_start_ns + capture.exposure_end_ns) // 2)
                consume(center.windows.offer(outcome))
        now = m.ProcessingTime(
            clock_domain_id=clock_mapping.processing_clock_domain_id,
            time_ns=100_000_001,
        )
        for (station, session, digest), capture in last.items():
            consume(
                center.windows.report_progress(
                    f.StationEventTimeProgress(
                        station_id=station,
                        station_session_id=session,
                        station_config_digest=digest,
                        completed_capture_time=capture,
                        received_at=now,
                        stream_ended=True,
                    )
                )
            )
        self.assertGreater(len(measurements), 1)
        self.assertEqual(
            {measurement.reference_time_ns for measurement in measurements}, expected_times
        )
        self.assertEqual(len(measurements), len(expected_times))
        self.assertEqual(
            len({measurement.measurement_id for measurement in measurements}), len(measurements)
        )
        for measurement in measurements:
            self.assertEqual(len(measurement.supporting_station_ids), 2)
            expected = (-15 + measurement.reference_time_ns / 1e9, 62, 20)
            self.assert_position(measurement, expected, atol=1e-9)
        self.assertFalse(
            center.windows.advance(replace(now, time_ns=now.time_ns + 10**9)).sealed_windows
        )


if __name__ == "__main__":
    unittest.main()

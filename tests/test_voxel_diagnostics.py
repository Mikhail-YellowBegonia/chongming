"""Raw station votes, reference invocation boundaries and export non-interference."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from chongming.contracts import fusion as f
from chongming.contracts import model as m
from chongming.modules.fusion_geometry import VoxelCandidateModule
from chongming.reference import ReferenceFusionAlgorithm
from chongming.visualization import center_fusion_event
from chongming.voxel_diagnostics import VoxelDiagnostics
from tests import test_fusion_geometry as geometry_tests
from tests import test_reference_fusion as reference_tests


class RawVoxelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = geometry_tests.CleanFusionTests()
        self.fixture.setUp()
        self.diagnostics = VoxelDiagnostics(enabled=True)
        self.search = replace(
            self.fixture.search, min_enu_m=(0.0, 0.0, 0.0), max_enu_m=(3.0, 3.0, 1.0)
        )
        self.voxels = VoxelCandidateModule(
            policy=replace(self.fixture.candidate_policy, search=self.search, uncertainty_sigma=0),
            motion_policy=self.fixture.policy.geometry,
            weights=self.fixture.weights,
            diagnostics=self.diagnostics,
        )

    def test_raw_votes_include_below_threshold_cells_and_deduplicate_station_votes(self) -> None:
        rays = (
            self.fixture.ray("a", (-1.0, 0.5, 0.5), direction=(1.0, 0.0, 0.0)),
            self.fixture.ray("a", (-1.0, 0.5, 0.5), direction=(1.0, 0.0, 0.0), sequence=3),
            self.fixture.ray("b", (0.5, -1.0, 0.5), direction=(0.0, 1.0, 0.0)),
        )
        module = replace(
            self.voxels,
            policy=replace(
                self.voxels.policy, minimum_independent_stations=3, minimum_candidate_score=3
            ),
        )
        batch = module.propose(self.fixture.window(rays), ())
        self.assertEqual(batch.candidates, ())
        snapshot = self.diagnostics.snapshot
        assert snapshot is not None
        expected = {(0, 0, 0): 2, (1, 0, 0): 1, (2, 0, 0): 1, (0, 1, 0): 1, (0, 2, 0): 1}
        self.assertEqual(
            {voxel.grid_index: voxel.score for voxel in snapshot.scored_voxels}, expected
        )
        self.assertTrue(
            all(voxel.score == voxel.supporting_station_count for voxel in snapshot.scored_voxels)
        )
        self.assertEqual(snapshot.max_score, 2)
        self.assertEqual([voxel.grid_index for voxel in snapshot.scored_voxels], sorted(expected))
        self.assertEqual(snapshot.search_box_min_enu_m, self.search.min_enu_m)

    def test_raw_grid_does_not_fill_the_merged_candidate_bounding_box(self) -> None:
        rays = (
            self.fixture.ray("a", (-1.0, -1.0, 0.5), direction=(1.0, 1.0, 0.0)),
            self.fixture.ray("b", (4.0, 4.0, 0.5), direction=(-1.0, -1.0, 0.0)),
        )
        batch = self.voxels.propose(self.fixture.window(rays), ())
        self.assertEqual(len(batch.candidates), 1)
        self.assertEqual(batch.candidates[0].region_min_enu_m, (0.0, 0.0, 0.0))
        self.assertEqual(batch.candidates[0].region_max_enu_m, (3.0, 3.0, 1.0))
        snapshot = self.diagnostics.snapshot
        assert snapshot is not None
        self.assertEqual(
            {voxel.grid_index for voxel in snapshot.scored_voxels},
            {(0, 0, 0), (0, 1, 0), (1, 0, 0), (1, 1, 0), (1, 2, 0), (2, 1, 0), (2, 2, 0)},
        )
        self.assertTrue(all(voxel.score == 2 for voxel in snapshot.scored_voxels))

    def test_empty_search_exports_an_explicit_empty_grid(self) -> None:
        window = self.fixture.window(())
        self.voxels.propose(window, ())
        snapshot = self.diagnostics.snapshot
        assert snapshot is not None
        self.assertEqual(snapshot.scored_voxels, ())
        self.assertEqual(snapshot.max_score, 0)
        self.assertEqual(self.diagnostics.window, window.key)

    def test_guided_swept_search_cannot_replace_unguided_votes(self) -> None:
        window = self.fixture.window(
            (self.fixture.rays()[0], self.fixture.ray("b", (2.0, -1.0, 0.0), offset=500))
        )
        module = replace(self.fixture.voxels, diagnostics=self.diagnostics)
        module.propose(window, ())
        expected = self.diagnostics.snapshot
        assert expected is not None
        prediction = f.TrackPrediction(
            center_session_id=window.key.center_session_id,
            track_id=m.TrackId("prior"),
            clock_domain_id=window.key.capture_clock_domain_id,
            world_frame_id=self.fixture.search.world_frame_id,
            reference_time_ns=window.key.start_ns,
            valid_until_ns=window.key.end_ns + 1,
            position_enu_m=geometry_tests.TARGET,
            velocity_enu_mps=(2.0, 3.0, 0.0),
            conditional_state_covariance_enu=(
                (1.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                (0.0, 1.0, 0.0, 0.0, 0.0, 0.0),
                (0.0, 0.0, 1.0, 0.0, 0.0, 0.0),
                (0.0, 0.0, 0.0, 1.0, 0.0, 0.0),
                (0.0, 0.0, 0.0, 0.0, 1.0, 0.0),
                (0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
            ),
        )
        with patch.object(self.diagnostics, "capture", wraps=self.diagnostics.capture) as capture:
            batch = module.propose(window, (prediction,))
        self.assertEqual(capture.call_count, 1)
        self.assertTrue(
            any(
                candidate.origin is f.CandidateOrigin.TRACK_PREDICTION_GATE
                for candidate in batch.candidates
            )
        )
        self.assertEqual(self.diagnostics.snapshot, expected)
        self.assertEqual(expected.search_box_min_enu_m, self.fixture.search.min_enu_m)

    def test_disabled_export_does_not_materialize_a_snapshot_or_change_candidates(self) -> None:
        window = self.fixture.window(self.fixture.rays())
        with_export = self.voxels.propose(window, ())
        self.diagnostics.clear()
        self.diagnostics.enabled = False
        without_export = self.voxels.propose(window, ())
        self.assertEqual(with_export, without_export)
        self.assertIsNone(self.diagnostics.snapshot)
        self.assertIsNone(self.diagnostics.window)


class ReferenceVoxelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = reference_tests.ReferenceFusionTests()
        self.fixture.setUp()

    def algorithm(
        self, *, enabled: bool = True, buffer_windows: bool = False
    ) -> ReferenceFusionAlgorithm:
        algorithm = ReferenceFusionAlgorithm()
        algorithm.voxel_diagnostics.enabled = enabled
        registration = self.fixture.registration
        if buffer_windows:
            # Deliberately hold watermarks until the finite end so one call seals
            # multiple windows; this is not a runtime-policy default.
            registration = replace(
                registration,
                policy=replace(
                    registration.policy,
                    windows=replace(registration.policy.windows, allowed_lateness_ns=1_000_000_000),
                ),
            )
        algorithm.start(registration)
        self.addCleanup(algorithm.close)
        return algorithm

    def test_export_switch_preserves_all_candidates_associations_measurements_and_tracks(
        self,
    ) -> None:
        enabled = self.algorithm()
        disabled = self.algorithm(enabled=False)
        snapshots = []
        for step in self.fixture.steps():
            expected = disabled.process(step)
            actual = enabled.process(step)
            self.assertEqual(actual, expected)
            self.assertIsNone(disabled.voxel_diagnostics.snapshot)
            if enabled.voxel_diagnostics.snapshot is not None:
                snapshots.append(enabled.voxel_diagnostics.snapshot)
                converted = center_fusion_event(
                    actual, voxel_space=enabled.voxel_diagnostics.snapshot
                )
                self.assertIs(converted.voxel_space, enabled.voxel_diagnostics.snapshot)
                self.assertEqual(len(converted.measurements), len(actual.measurements))
        self.assertTrue(snapshots)
        self.assertTrue(any(snapshot.scored_voxels for snapshot in snapshots))

    def test_one_invocation_exports_only_the_last_of_multiple_sealed_windows(self) -> None:
        algorithm = self.algorithm(buffer_windows=True)
        steps = self.fixture.steps()
        for step in steps[:-1]:
            result = algorithm.process(replace(step, progress=()))
            self.assertFalse(any(batch.window_update.sealed_windows for batch in result.batches))
        result = algorithm.process(steps[-1])
        windows = [
            window for batch in result.batches for window in batch.window_update.sealed_windows
        ]
        self.assertGreaterEqual(len(windows), 2)
        self.assertEqual(algorithm.voxel_diagnostics.window, windows[-1].key)
        expected = VoxelDiagnostics(enabled=True)
        assert algorithm.center is not None
        replace(algorithm.center.voxels, diagnostics=expected).propose(windows[-1], ())
        self.assertEqual(algorithm.voxel_diagnostics.snapshot, expected.snapshot)
        self.assertEqual(
            sum(len(batch.measurements) for batch in result.batches), len(result.measurements)
        )
        idle = replace(
            steps[-1], step_no=steps[-1].step_no + 1, events=(), solutions=(), progress=()
        )
        algorithm.process(idle)
        self.assertIsNone(algorithm.voxel_diagnostics.snapshot)

    def test_last_empty_window_replaces_nonempty_votes(self) -> None:
        algorithm = self.algorithm()
        steps = self.fixture.steps()
        for step in steps[:-1]:
            algorithm.process(step)
        horizon = self.fixture.registration.policy.windows.width_ns * 4
        final = steps[-1]
        progress = tuple(
            replace(
                item,
                completed_capture_time=m.CaptureTime(
                    clock_domain_id=item.received_at.clock_domain_id,
                    exposure_start_ns=horizon,
                    exposure_end_ns=horizon,
                    clock_uncertainty_ns=0,
                    rolling_shutter_model="none",
                ),
                received_at=replace(item.received_at, time_ns=horizon + 1),
            )
            for item in final.progress
        )
        result = algorithm.process(
            replace(final, progress=progress, time=replace(final.time, time_ns=horizon + 1))
        )
        windows = [
            window for batch in result.batches for window in batch.window_update.sealed_windows
        ]
        self.assertTrue(windows)
        self.assertEqual(windows[-1].rays, ())
        self.assertEqual(algorithm.voxel_diagnostics.window, windows[-1].key)
        snapshot = algorithm.voxel_diagnostics.snapshot
        assert snapshot is not None
        self.assertEqual(snapshot.scored_voxels, ())
        self.assertEqual(snapshot.max_score, 0)

    def test_last_window_without_voxel_export_cannot_reuse_earlier_votes(self) -> None:
        algorithm = self.algorithm(buffer_windows=True)
        steps = self.fixture.steps()
        for step in steps[:-1]:
            algorithm.process(replace(step, progress=()))
        center = algorithm.center
        assert center is not None
        original = center.candidates.generate
        calls = []

        def alternate(
            window: f.SealedObservationWindow, predictions: tuple[f.TrackPrediction, ...]
        ) -> f.CandidateBatch:
            calls.append(window)
            if len(calls) == 1:
                batch = original(window, predictions)
                self.assertIsNotNone(algorithm.voxel_diagnostics.snapshot)
                return batch
            # Test-only alternate strategy: exercise the missing-export boundary,
            # not the still-unimplemented direct geometry module.
            return f.CandidateBatch(
                window=window.key,
                method=f.CandidateMethod.DIRECT_GEOMETRY,
                search=center.voxels.policy.search,
                status=f.CandidateBatchStatus.NO_CANDIDATE_IN_VOLUME,
                candidates=(),
                examined_ray_event_ids=(),
                unguided_search_performed=False,
                reason_codes=("test_no_voxel_export",),
            )

        with patch.object(type(center.candidates), "generate", side_effect=alternate):
            result = algorithm.process(steps[-1])
        self.assertGreaterEqual(len(calls), 2)
        self.assertIsNone(algorithm.voxel_diagnostics.snapshot)
        self.assertGreater(
            sum(len(batch.window_update.sealed_windows) for batch in result.batches), 1
        )


if __name__ == "__main__":
    unittest.main()

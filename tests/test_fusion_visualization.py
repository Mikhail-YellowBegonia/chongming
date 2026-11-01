"""Test center fusion visualization conversion."""

from __future__ import annotations

import unittest

from chongming.contracts import fusion as f
from chongming.contracts import model as m
from chongming.conversion.fusion_visualization import build_center_fusion_event

IDENTITY: m.Matrix3x3 = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


class VisualizationConversionTests(unittest.TestCase):
    def test_empty_batch_produces_empty_event(self) -> None:
        batch = f.FusionBatchResult(
            center_session_id=m.SessionId("center"),
            produced_at=m.ProcessingTime(clock_domain_id="clock", time_ns=0),
            admissions=(),
            window_update=f.WindowUpdate(
                receipts=(),
                sealed_windows=(),
                watermark=f.WatermarkSnapshot(
                    capture_clock_domain_id="clock",
                    event_time_ns=None,
                    evaluated_at=m.ProcessingTime(clock_domain_id="clock", time_ns=0),
                    station_progress=(),
                ),
            ),
            candidates=(),
            associations=(),
            geometry=(),
            measurements=(),
            tracks=(),
        )
        event = build_center_fusion_event(batch)
        self.assertEqual(len(event.measurements), 0)
        self.assertEqual(len(event.tracks), 0)
        self.assertEqual(event.window_count, 0)
        self.assertEqual(event.candidate_count, 0)
        self.assertEqual(event.association_count, 0)
        self.assertIsNone(event.voxel_space)

    def test_measurement_with_position_converts_std_from_covariance(self) -> None:
        measurement = m.FusionMeasurement(
            center_session_id=m.SessionId("center"),
            measurement_id=m.MeasurementId("m1"),
            clock_domain_id="clock",
            world_frame_id=m.WorldFrameId("enu"),
            reference_time_ns=1000,
            support_exposure_span_ns=100,
            status=m.FusionStatus.LOCATED,
            position_enu_m=(10.0, 20.0, 30.0),
            conditional_position_covariance_enu_m2=(
                (4.0, 0.0, 0.0),
                (0.0, 9.0, 0.0),
                (0.0, 0.0, 16.0),
            ),
            motion_alignment=None,
            supporting_ray_event_ids=(
                (m.StationId("s1"), m.SessionId("s1"), 1),
                (m.StationId("s2"), m.SessionId("s2"), 1),
            ),
            supporting_station_ids=(m.StationId("s1"), m.StationId("s2")),
            residuals_m=(0.1, 0.2),
            geometry_condition=10.0,
            minimum_intersection_angle_rad=0.5,
            association_hypothesis_id="h1",
            reason_codes=(),
        )
        batch = self._make_batch(measurements=(measurement,))
        event = build_center_fusion_event(batch)
        self.assertEqual(len(event.measurements), 1)
        vis = event.measurements[0]
        self.assertEqual(vis.measurement_id, measurement.measurement_id)
        self.assertEqual(vis.reference_time_ns, 1000)
        self.assertEqual(vis.status, m.FusionStatus.LOCATED)
        self.assertEqual(vis.position_enu_m, (10.0, 20.0, 30.0))
        assert vis.position_std_enu_m is not None
        self.assertAlmostEqual(vis.position_std_enu_m[0], 2.0)
        self.assertAlmostEqual(vis.position_std_enu_m[1], 3.0)
        self.assertAlmostEqual(vis.position_std_enu_m[2], 4.0)
        self.assertAlmostEqual(vis.minimum_intersection_angle_deg or 0, 28.6478, places=3)
        self.assertEqual(vis.geometry_condition, 10.0)

    def test_measurement_without_position_has_none_fields(self) -> None:
        measurement = m.FusionMeasurement(
            center_session_id=m.SessionId("center"),
            measurement_id=m.MeasurementId("m1"),
            clock_domain_id="clock",
            world_frame_id=m.WorldFrameId("enu"),
            reference_time_ns=1000,
            support_exposure_span_ns=100,
            status=m.FusionStatus.INSUFFICIENT_STATIONS,
            position_enu_m=None,
            conditional_position_covariance_enu_m2=None,
            motion_alignment=None,
            supporting_ray_event_ids=((m.StationId("s1"), m.SessionId("s1"), 1),),
            supporting_station_ids=(m.StationId("s1"),),
            residuals_m=(),
            geometry_condition=None,
            minimum_intersection_angle_rad=None,
            association_hypothesis_id="h1",
            reason_codes=("insufficient_stations",),
        )
        batch = self._make_batch(measurements=(measurement,))
        event = build_center_fusion_event(batch)
        vis = event.measurements[0]
        self.assertEqual(
            vis.status,
            m.FusionStatus.INSUFFICIENT_STATIONS,
        )
        self.assertIsNone(vis.position_enu_m)
        self.assertIsNone(vis.position_std_enu_m)
        self.assertIsNone(vis.minimum_intersection_angle_deg)
        self.assertIsNone(vis.geometry_condition)

    def test_track_with_full_covariance_converts_position_and_velocity_std(self) -> None:
        track = m.TrackEvent(
            center_session_id=m.SessionId("center"),
            track_id=m.TrackId("1"),
            clock_domain_id="clock",
            world_frame_id=m.WorldFrameId("enu"),
            event_time_ns=2000,
            lifecycle=m.TrackLifecycle.CONFIRMED,
            position_enu_m=(100.0, 200.0, 300.0),
            velocity_enu_mps=(5.0, 10.0, 15.0),
            conditional_state_covariance=(
                (4.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                (0.0, 9.0, 0.0, 0.0, 0.0, 0.0),
                (0.0, 0.0, 16.0, 0.0, 0.0, 0.0),
                (0.0, 0.0, 0.0, 1.0, 0.0, 0.0),
                (0.0, 0.0, 0.0, 0.0, 4.0, 0.0),
                (0.0, 0.0, 0.0, 0.0, 0.0, 9.0),
            ),
            source_measurement_refs=(),
            reason_codes=(),
        )
        batch = self._make_batch(tracks=(track,))
        event = build_center_fusion_event(batch)
        self.assertEqual(len(event.tracks), 1)
        vis = event.tracks[0]
        self.assertEqual(vis.track_id, track.track_id)
        self.assertEqual(vis.event_time_ns, 2000)
        self.assertEqual(vis.lifecycle, m.TrackLifecycle.CONFIRMED)
        self.assertEqual(vis.position_enu_m, (100.0, 200.0, 300.0))
        self.assertEqual(vis.velocity_enu_mps, (5.0, 10.0, 15.0))
        assert vis.position_std_enu_m is not None
        self.assertAlmostEqual(vis.position_std_enu_m[0], 2.0)
        self.assertAlmostEqual(vis.position_std_enu_m[1], 3.0)
        self.assertAlmostEqual(vis.position_std_enu_m[2], 4.0)
        assert vis.velocity_std_enu_mps is not None
        self.assertAlmostEqual(vis.velocity_std_enu_mps[0], 1.0)
        self.assertAlmostEqual(vis.velocity_std_enu_mps[1], 2.0)
        self.assertAlmostEqual(vis.velocity_std_enu_mps[2], 3.0)
        self.assertEqual(vis.recent_position_enu_m, (100.0, 200.0, 300.0))
        self.assertEqual(vis.recent_time_ns, 2000)

    def test_track_without_covariance_has_none_std_fields(self) -> None:
        track = m.TrackEvent(
            center_session_id=m.SessionId("center"),
            track_id=m.TrackId("1"),
            clock_domain_id="clock",
            world_frame_id=m.WorldFrameId("enu"),
            event_time_ns=1000,
            lifecycle=m.TrackLifecycle.TENTATIVE,
            position_enu_m=(10.0, 20.0, 30.0),
            velocity_enu_mps=None,
            conditional_state_covariance=None,
            source_measurement_refs=(),
            reason_codes=(),
        )
        batch = self._make_batch(tracks=(track,))
        event = build_center_fusion_event(batch)
        vis = event.tracks[0]
        self.assertEqual(vis.lifecycle, m.TrackLifecycle.TENTATIVE)
        self.assertIsNone(vis.velocity_enu_mps)
        self.assertIsNone(vis.position_std_enu_m)
        self.assertIsNone(vis.velocity_std_enu_mps)

    def test_batch_counts_aggregate_correctly(self) -> None:
        # Create a batch with multiple windows, candidates, and associations
        window = self._make_sealed_window()
        candidates = (
            f.CandidateBatch(
                window=window.key,
                method=f.CandidateMethod.COARSE_VOXEL,
                search=m.VoxelSearchSpec(
                    world_frame_id=m.WorldFrameId("enu"),
                    min_enu_m=(0.0, 0.0, 0.0),
                    max_enu_m=(100.0, 100.0, 100.0),
                    step_enu_m=(1.0, 1.0, 1.0),
                    forward_range_m=(10.0, 1000.0),
                ),
                status=f.CandidateBatchStatus.CANDIDATES_PROPOSED,
                candidates=(
                    self._make_candidate("c1", window.key),
                    self._make_candidate("c2", window.key),
                ),
                examined_ray_event_ids=(),
                unguided_search_performed=True,
                reason_codes=(),
            ),
        )
        associations = (
            f.AssociationBatch(
                window=window.key,
                selected_groups=(
                    self._make_group("g1", window.key),
                    self._make_group("g2", window.key),
                    self._make_group("g3", window.key),
                ),
                conflicts=(),
                unassigned_ray_event_ids=(),
                status=f.AssociationBatchStatus.ASSOCIATIONS_SELECTED,
            ),
        )
        batch = self._make_batch(
            sealed_windows=(window,), candidates=candidates, associations=associations
        )
        event = build_center_fusion_event(batch)
        self.assertEqual(event.window_count, 1)
        self.assertEqual(event.candidate_count, 2)
        self.assertEqual(event.association_count, 3)

    def _make_batch(
        self,
        *,
        measurements: tuple[m.FusionMeasurement, ...] = (),
        tracks: tuple[m.TrackEvent, ...] = (),
        sealed_windows: tuple[f.SealedObservationWindow, ...] = (),
        candidates: tuple[f.CandidateBatch, ...] = (),
        associations: tuple[f.AssociationBatch, ...] = (),
    ) -> f.FusionBatchResult:
        return f.FusionBatchResult(
            center_session_id=m.SessionId("center"),
            produced_at=m.ProcessingTime(clock_domain_id="clock", time_ns=0),
            admissions=(),
            window_update=f.WindowUpdate(
                receipts=(),
                sealed_windows=sealed_windows,
                watermark=f.WatermarkSnapshot(
                    capture_clock_domain_id="clock",
                    event_time_ns=None,
                    evaluated_at=m.ProcessingTime(clock_domain_id="clock", time_ns=0),
                    station_progress=(),
                ),
            ),
            candidates=candidates,
            associations=associations,
            geometry=(),
            measurements=measurements,
            tracks=tracks,
        )

    def _make_sealed_window(self) -> f.SealedObservationWindow:
        return f.SealedObservationWindow(
            key=f.WindowKey(
                center_session_id=m.SessionId("center"),
                capture_clock_domain_id="clock",
                start_ns=0,
                end_ns=1000000000,
            ),
            reference_time_ns=500000000,
            support_exposure_span_ns=0,
            rays=(),
            watermark_at_seal=f.WatermarkSnapshot(
                capture_clock_domain_id="clock",
                event_time_ns=None,
                evaluated_at=m.ProcessingTime(clock_domain_id="clock", time_ns=0),
                station_progress=(),
            ),
            sealed_at=m.ProcessingTime(clock_domain_id="clock", time_ns=0),
            seal_reason=f.WindowSealReason.WATERMARK_PASSED_END,
        )

    def _make_candidate(self, candidate_id: str, window: f.WindowKey) -> f.SpatialCandidate:
        return f.SpatialCandidate(
            candidate_id=candidate_id,
            window=window,
            method=f.CandidateMethod.COARSE_VOXEL,
            origin=f.CandidateOrigin.UNGUIDED_SEARCH,
            prior_track_id=None,
            coarse_position_enu_m=(0.0, 0.0, 0.0),
            region_min_enu_m=None,
            region_max_enu_m=None,
            independent_station_support=(
                f.IndependentStationSupport(
                    station_id=m.StationId("s1"), alternative_ray_event_ids=()
                ),
                f.IndependentStationSupport(
                    station_id=m.StationId("s2"), alternative_ray_event_ids=()
                ),
            ),
            score=2.0,
            score_definition=m.ScoreDefinition(
                definition_id="station_count",
                kind=m.ScoreKind.RANKING,
                minimum=None,
                maximum=None,
                cross_station_comparison_group=None,
            ),
        )

    def _make_group(self, hypothesis_id: str, window: f.WindowKey) -> f.AssociatedRayGroup:
        return f.AssociatedRayGroup(
            hypothesis_id=hypothesis_id,
            candidate_id="c1",
            window=window,
            reference_time_ns=1000,
            support_exposure_span_ns=0,
            supporting_ray_event_ids=(),
            supporting_station_ids=(m.StationId("s1"), m.StationId("s2")),
            prior_track_id=None,
            motion_alignment_model=f.MotionAlignmentModel.NARROW_WINDOW_WITHOUT_PRIOR,
            score=2.0,
            score_definition=m.ScoreDefinition(
                definition_id="station_count",
                kind=m.ScoreKind.RANKING,
                minimum=None,
                maximum=None,
                cross_station_comparison_group=None,
            ),
        )


if __name__ == "__main__":
    unittest.main()

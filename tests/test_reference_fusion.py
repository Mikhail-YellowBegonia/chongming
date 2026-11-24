"""Reference plugin, sealed-time orchestration, and real CLI delivery."""

from __future__ import annotations

import json
import math
import unittest
from dataclasses import asdict, replace
from typing import Any

import numpy as np

from chongming.composition.center import declare_center
from chongming.contracts import fusion as f
from chongming.contracts import model as m
from chongming.contracts.development import (
    CenterAlgorithmResult,
    CenterInputStep,
)
from chongming.contracts.runtime import ModuleState
from chongming.reference import FusionEngineAdapter, create_algorithm
from tests.online_fixtures import (
    SyntheticTarget,
    default_clock_mapping,
    default_fusion_policy,
    default_registration,
    default_station_configs,
    generate_synthetic_steps,
)

DEFAULT_TARGET = SyntheticTarget(
    initial_position_enu_m=(-15.0, 62.0, 20.0),
    velocity_enu_mps=(1.0, 0.0, 0.0),
)
SECOND_TARGET = SyntheticTarget(
    initial_position_enu_m=(15.0, 80.0, 35.0),
    velocity_enu_mps=(-0.5, 0.2, 0.0),
)


class ReferenceFusionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stations = default_station_configs()
        self.policy = default_fusion_policy()
        self.clock_mapping = default_clock_mapping()
        self.registration = default_registration(
            stations=self.stations,
            policy=self.policy,
            center_session_id="ref-test",
        )
        self.center, _ = declare_center(
            center_session_id=m.SessionId(self.registration.center_session_id),
            policy=self.registration.policy,
            clock_mapping=self.registration.clock_mapping,
        )
        self.adapter = FusionEngineAdapter(self.center.fusion)
        self.adapter.start(self.registration)

    def steps(
        self,
        *,
        stations: tuple[m.StationConfig, ...] | None = None,
        targets: tuple[SyntheticTarget, ...] = (DEFAULT_TARGET,),
        duration_ns: int = 120_000_000,
        **kwargs: Any,
    ) -> list[CenterInputStep]:
        rows, _ = generate_synthetic_steps(
            self.stations if stations is None else stations,
            targets,
            duration_ns=duration_ns,
            **kwargs,
        )
        return rows

    def test_reference_plugin_has_online_only_registration_and_full_diagnostics(self) -> None:
        algorithm = create_algorithm()
        algorithm.start(self.registration)
        bindings = tuple(algorithm._started)
        try:
            results = [algorithm.process(step) for step in self.steps()]
            measurements = [
                measurement for result in results for measurement in result.measurements
            ]
            tracks = [track for result in results for track in result.tracks]
            self.assertGreater(len(measurements), 2)
            self.assertEqual(
                len({measurement.measurement_id for measurement in measurements}), len(measurements)
            )
            self.assertTrue(
                all(measurement.status is m.FusionStatus.LOCATED for measurement in measurements)
            )
            self.assertIs(tracks[0].lifecycle, m.TrackLifecycle.TENTATIVE)
            confirmed = [track for track in tracks if track.lifecycle is m.TrackLifecycle.CONFIRMED]
            self.assertTrue(confirmed)
            self.assertEqual(len({track.track_id for track in tracks}), 1)
            for track in confirmed:
                np.testing.assert_allclose(
                    np.asarray(track.velocity_enu_mps),
                    np.asarray((1.0, 0.0, 0.0)),
                    rtol=0,
                    atol=1e-8,
                )
            batches = [batch for result in results for batch in result.batches]
            self.assertTrue(any(batch.admissions for batch in batches))
            self.assertTrue(any(batch.window_update.sealed_windows for batch in batches))
            self.assertTrue(
                any(batch.candidates and batch.associations and batch.geometry for batch in batches)
            )
            encoded = json.dumps([asdict(result) for result in results], allow_nan=False)
            self.assertNotIn("truth_object_id", encoded)
            self.assertNotIn("scene_definition", encoded)
            self.assertNotIn("experiment_perturbations", encoded)
            last = self.steps()[-1]
            future = replace(last, time=replace(last.time, time_ns=10**12), progress=())
            result = algorithm.process(future)
            self.assertFalse(result.measurements)
            self.assertFalse(result.tracks)
            self.assertTrue(all(not batch.window_update.sealed_windows for batch in result.batches))
        finally:
            algorithm.close()
            algorithm.close()
        self.assertTrue(
            all(
                getattr(binding.module, "_lifecycle_state", None) is ModuleState.STOPPED
                for binding in bindings
            )
        )
        with self.assertRaisesRegex(RuntimeError, "not running"):
            algorithm.process(self.steps()[0])

    def test_open_windows_prevent_wall_clock_ticks_from_advancing_tracks(self) -> None:
        # Explicit HOLD policy: station timeout must not seal an open window.
        policy = replace(
            self.registration.policy,
            windows=replace(
                self.registration.policy.windows,
                exclude_idle_stations=False,
                exclude_offline_stations=False,
                no_active_station_policy=f.EmptyActiveStationPolicy.HOLD,
            ),
        )
        self.center, _ = declare_center(
            center_session_id=m.SessionId(self.registration.center_session_id),
            policy=policy,
            clock_mapping=self.registration.clock_mapping,
        )
        self.adapter = FusionEngineAdapter(self.center.fusion)
        self.adapter.start(replace(self.registration, policy=policy))
        steps = self.steps()
        first = self.adapter.process(steps[0])
        self.assertFalse(first.measurements)
        late_now = replace(steps[0].time, time_ns=10**12)
        tick = self.center.fusion.tick(late_now)
        self.assertFalse(tick.window_update.sealed_windows)
        self.assertFalse(tick.tracks)
        second = replace(
            steps[1],
            time=replace(late_now, time_ns=late_now.time_ns + 1),
            events=tuple(
                replace(event, arrival_time=replace(late_now, time_ns=late_now.time_ns + 1))
                for event in steps[1].events
            ),
            progress=tuple(
                replace(progress, received_at=replace(late_now, time_ns=late_now.time_ns + 1))
                for progress in steps[1].progress
            ),
        )
        result = self.adapter.process(second)
        self.assertEqual(len(result.measurements), 1)
        self.assertIs(result.tracks[0].lifecycle, m.TrackLifecycle.TENTATIVE)
        self.assertLess(result.tracks[0].event_time_ns, 10**9)
        future = self.center.fusion.tick(replace(late_now, time_ns=10**13))
        self.assertFalse(future.tracks)
        self.assertFalse(future.window_update.sealed_windows)

    def test_quarantined_control_events_unlock_rays_before_progress_seals(self) -> None:
        center, _ = declare_center(
            center_session_id=m.SessionId(self.registration.center_session_id),
            policy=self.registration.policy,
            clock_mapping=self.registration.clock_mapping,
        )
        first = self.steps()[0]
        rays = tuple(
            received for received in first.events if isinstance(received.event, m.RayEvent)
        )
        controls = tuple(
            received
            for received in first.events
            if isinstance(received.event, m.CalibrationStateEvent)
        )
        for received in (*rays, *controls):
            batch = center.fusion.process(received)
            self.assertIs(batch.admissions[-1].decision, f.AdmissionDecision.QUARANTINED)
            self.assertFalse(batch.measurements)
        for config in self.registration.stations:
            center.fusion.register_station(config)
        retried = center.fusion.tick(replace(first.time, time_ns=first.time.time_ns + 1))
        self.assertEqual(len(retried.admissions), 4)
        self.assertTrue(
            all(record.decision is f.AdmissionDecision.ACCEPTED for record in retried.admissions)
        )
        self.assertFalse(center.journal.quarantined())
        self.assertTrue(
            all(record.received.arrival_time == first.time for record in retried.admissions)
        )
        self.assertEqual(
            sum(
                receipt.disposition is f.WindowDisposition.BUFFERED
                for receipt in retried.window_update.receipts
            ),
            2,
        )
        final = self.steps()[-1]
        # End the sessions actually represented by these rays, not a new source run.
        sessions = {
            received.event.envelope.station_id: received.event.envelope.station_session_id
            for received in rays
        }
        batches = [
            center.fusion.report_progress(
                replace(
                    progress,
                    station_session_id=sessions[progress.station_id],
                    completed_capture_time=next(
                        received.event.capture_time
                        for received in rays
                        if isinstance(received.event, m.RayEvent)
                        and received.event.envelope.station_id == progress.station_id
                    ),
                )
            )
            for progress in final.progress
        ]
        self.assertEqual(sum(len(batch.measurements) for batch in batches), 1)

    def test_duplicate_conflict_and_late_events_cannot_republish_evidence(self) -> None:
        steps = self.steps()
        for step in steps:
            self.adapter.process(step)
        ray = next(
            received for received in steps[0].events if isinstance(received.event, m.RayEvent)
        )
        now = replace(steps[-1].time, time_ns=steps[-1].time.time_ns + 1)
        duplicate = self.center.fusion.process(replace(ray, arrival_time=now))
        self.assertIs(duplicate.admissions[-1].decision, f.AdmissionDecision.DUPLICATE)
        self.assertFalse(duplicate.measurements or duplicate.tracks)
        assert isinstance(ray.event, m.RayEvent)
        conflict = self.center.fusion.process(
            replace(ray, event=replace(ray.event, detection_score=0.123), arrival_time=now)
        )
        self.assertEqual(
            conflict.admissions[-1].reasons, (f.AdmissionReason.EVENT_ID_PAYLOAD_CONFLICT,)
        )
        late = self.center.fusion.process(
            replace(
                ray,
                event=replace(ray.event, envelope=replace(ray.event.envelope, sequence_no=999)),
                arrival_time=now,
            )
        )
        self.assertIs(
            late.window_update.receipts[-1].disposition, f.WindowDisposition.LATE_AFTER_SEAL
        )
        self.assertFalse(late.measurements or late.tracks)

    def test_ambiguous_window_keeps_diagnostics_without_creating_a_track(self) -> None:
        first = self.steps()[0]
        self.adapter.process(first)
        ray = next(received for received in first.events if isinstance(received.event, m.RayEvent))
        alternate = replace(
            ray, event=replace(ray.event, envelope=replace(ray.event.envelope, sequence_no=999))
        )
        self.center.fusion.process(alternate)
        batches = [
            self.center.fusion.report_progress(replace(progress, stream_ended=True))
            for progress in first.progress
        ]
        self.assertTrue(
            any(
                batch.associations
                and batch.associations[0].status is f.AssociationBatchStatus.ASSOCIATION_AMBIGUOUS
                for batch in batches
            )
        )
        self.assertFalse(any(batch.measurements or batch.tracks for batch in batches))

    def test_empty_and_zero_sample_inputs_never_create_tracks(self) -> None:
        for start, duration_ns in ((0, 70_000_000), (1_000_000_000, 1_000_000)):
            with self.subTest(start=start):
                algorithm = create_algorithm()
                algorithm.start(self.registration)
                try:
                    steps = self.steps(targets=(), duration_ns=duration_ns, station_start_ns=start)
                    results = [algorithm.process(step) for step in steps]
                    self.assertFalse(
                        any(result.measurements or result.tracks for result in results)
                    )
                    windows = [
                        window
                        for result in results
                        for batch in result.batches
                        for window in batch.window_update.sealed_windows
                    ]
                    if start:
                        self.assertFalse(windows)
                    else:
                        self.assertTrue(windows)
                        self.assertTrue(all(not window.rays for window in windows))
                finally:
                    algorithm.close()

    def test_empty_sealed_windows_drive_coasting_and_deletion(self) -> None:
        steps = self.steps()
        for step in steps[:3]:
            self.adapter.process(step)
        events: list[m.TrackEvent] = []
        for time_ns in (1_500_000_000, 4_000_000_000):
            for original in steps[2].progress:
                capture = original.completed_capture_time
                assert capture is not None
                progress = replace(
                    original,
                    completed_capture_time=replace(
                        capture, exposure_start_ns=time_ns, exposure_end_ns=time_ns
                    ),
                    received_at=replace(original.received_at, time_ns=time_ns),
                )
                batch = self.center.fusion.report_progress(progress)
                events.extend(batch.tracks)
        self.assertTrue(any(event.lifecycle is m.TrackLifecycle.COASTING for event in events))
        deleted = [event for event in events if event.lifecycle is m.TrackLifecycle.DELETED]
        self.assertEqual(len(deleted), 1)
        self.assertLessEqual(deleted[0].event_time_ns, 4_000_000_000)

    def test_three_station_source_tracks_two_targets_and_discovers_a_late_detection(self) -> None:
        stations = default_station_configs(include_upper=True)
        registration = replace(self.registration, stations=stations)
        starts = np.asarray(((-15.0, 62.0, 20.0), (15.0, 80.0, 35.0)))
        velocities = np.asarray(((1.0, 0.0, 0.0), (-0.5, 0.2, 0.0)))
        for hidden_steps in (0, 2):
            with self.subTest(hidden_steps=hidden_steps):
                algorithm = create_algorithm()
                algorithm.start(registration)
                results = []
                try:
                    steps = self.steps(
                        stations=stations,
                        targets=(DEFAULT_TARGET, SECOND_TARGET),
                        duration_ns=200_000_000,
                    )
                    for step in steps:
                        if step.step_no < hidden_steps:
                            # Simulate missing detections by dropping the second
                            # observation per station, without changing progress.
                            first_ids = {
                                station.station_id: min(
                                    received.event.envelope.sequence_no
                                    for received in step.events
                                    if isinstance(received.event, m.RayEvent)
                                    and received.event.envelope.station_id == station.station_id
                                )
                                for station in stations
                            }
                            step = replace(
                                step,
                                events=tuple(
                                    received
                                    for received in step.events
                                    if not isinstance(received.event, m.RayEvent)
                                    or received.event.envelope.sequence_no
                                    == first_ids[received.event.envelope.station_id]
                                ),
                            )
                        results.append(algorithm.process(step))
                finally:
                    algorithm.close()
                geometry = [
                    batch
                    for result in results
                    for output in result.batches
                    for batch in output.geometry
                ]
                self.assertEqual(
                    [len(batch.results) for batch in geometry],
                    [1 if index < hidden_steps else 2 for index in range(len(steps) - 1)],
                )
                owner_ids: dict[int, m.TrackId] = {}
                confirmed: set[int] = set()
                for result in results:
                    for measurement in result.measurements:
                        self.assertIs(measurement.status, m.FusionStatus.LOCATED)
                        expected = starts + (measurement.reference_time_ns / 1e9) * velocities
                        point = np.asarray(measurement.position_enu_m)
                        owner = int(np.argmin(np.linalg.norm(expected - point, axis=1)))
                        np.testing.assert_allclose(point, expected[owner], rtol=0, atol=1e-9)
                    for track in result.tracks:
                        if not track.source_measurement_refs:
                            continue
                        expected = starts + (track.event_time_ns / 1e9) * velocities
                        point = np.asarray(track.position_enu_m)
                        owner = int(np.argmin(np.linalg.norm(expected - point, axis=1)))
                        np.testing.assert_allclose(point, expected[owner], rtol=0, atol=1e-9)
                        self.assertEqual(
                            track.track_id, owner_ids.setdefault(owner, track.track_id)
                        )
                        if track.lifecycle is m.TrackLifecycle.CONFIRMED:
                            confirmed.add(owner)
                            np.testing.assert_allclose(
                                np.asarray(track.velocity_enu_mps),
                                velocities[owner],
                                rtol=0,
                                atol=1e-8,
                            )
                self.assertEqual(set(owner_ids), {0, 1})
                self.assertEqual(confirmed, {0, 1})
                self.assertEqual(len(set(owner_ids.values())), 2)
                encoded = json.dumps([asdict(result) for result in results], allow_nan=False)
                self.assertNotIn("second-observation-source", encoded)
                self.assertNotIn("moving-sphere", encoded)

    def test_source_crossing_and_mutual_occlusion_keep_center_ids(self) -> None:
        stations = default_station_configs(include_upper=True)
        crossing_time = (1_000_000 + 5 * (10**9 // 30)) / 1e9
        targets = (
            SyntheticTarget(
                initial_position_enu_m=(-10.0 * crossing_time, 70.0, 24.0),
                velocity_enu_mps=(10.0, 0.0, 0.0),
            ),
            SyntheticTarget(
                initial_position_enu_m=(10.0 * crossing_time, 70.0, 24.0),
                velocity_enu_mps=(-10.0, 0.0, 0.0),
            ),
        )
        policy = replace(
            self.policy,
            association=replace(self.policy.association, spatial_gate_m=1e-6),
        )
        registration = replace(self.registration, stations=stations, policy=policy)
        algorithm = create_algorithm()
        algorithm.start(registration)
        owners: dict[m.TrackId, float] = {}
        source_empty = False
        missed_updates = False
        final_positions: dict[m.TrackId, m.Vec3] = {}
        try:
            for step in self.steps(
                stations=stations,
                targets=targets,
                duration_ns=400_000_000,
                skip_step_indices=(5,),
            ):
                if not step.stream_ended and not any(
                    isinstance(received.event, m.RayEvent) for received in step.events
                ):
                    source_empty = True
                result = algorithm.process(step)
                for track in result.tracks:
                    if "position_seed" in track.reason_codes:
                        assert track.position_enu_m is not None
                        self.assertNotIn(track.track_id, owners)
                        owners[track.track_id] = 1.0 if track.position_enu_m[0] < 0 else -1.0
                    if not track.source_measurement_refs:
                        missed_updates = True
                    if (
                        track.lifecycle is m.TrackLifecycle.CONFIRMED
                        and track.source_measurement_refs
                    ):
                        sign = owners[track.track_id]
                        expected = np.asarray(
                            (sign * 10 * (track.event_time_ns / 1e9 - crossing_time), 70, 24)
                        )
                        np.testing.assert_allclose(
                            np.asarray(track.position_enu_m), expected, rtol=0, atol=1e-9
                        )
                        np.testing.assert_allclose(
                            np.asarray(track.velocity_enu_mps),
                            np.asarray((sign * 10, 0, 0)),
                            rtol=0,
                            atol=1e-8,
                        )
                        assert track.position_enu_m is not None
                        final_positions[track.track_id] = track.position_enu_m
        finally:
            algorithm.close()
        self.assertTrue(source_empty)
        self.assertTrue(missed_updates)
        self.assertEqual(len(owners), 2)
        self.assertEqual(set(final_positions), set(owners))
        self.assertTrue(
            all(
                position[0] * owners[track_id] > 0 for track_id, position in final_positions.items()
            )
        )

    def test_asynchronous_station_joins_motion_fit_independently_of_delivery_order(self) -> None:
        stations = default_station_configs(include_upper=True)
        registration = replace(self.registration, stations=stations)
        steps = self.steps(
            stations=stations,
            targets=(DEFAULT_TARGET,),
            duration_ns=1_200_000_000,
            station_periods_ns={"upper": (10_000_000, 10**9 // 29)},
        )
        upper_windows = {
            (
                received.event.capture_time.exposure_start_ns
                + received.event.capture_time.exposure_end_ns
            )
            // 2
            // self.policy.windows.width_ns
            for step in steps
            for received in step.events
            if isinstance(received.event, m.RayEvent)
            and received.event.envelope.station_id == "upper"
        }
        signatures = []
        for delay in (0, 25_000_000):
            with self.subTest(transport_delay_ns=delay):
                scheduled = [
                    (
                        step.time.time_ns
                        + (
                            delay
                            if any(
                                isinstance(received.event, m.RayEvent)
                                and received.event.envelope.station_id == "upper"
                                for received in step.events
                            )
                            else 0
                        ),
                        step,
                    )
                    for step in steps
                    if not step.stream_ended
                ]
                scheduled.sort(key=lambda item: item[0])
                scheduled.append((max(steps[-1].time.time_ns, scheduled[-1][0] + 1), steps[-1]))
                algorithm = create_algorithm()
                algorithm.start(registration)
                results = []
                try:
                    for number, (arrival, step) in enumerate(scheduled):
                        now = replace(step.time, time_ns=arrival)
                        delivered = replace(
                            step,
                            step_no=number,
                            time=now,
                            events=tuple(
                                replace(received, arrival_time=now) for received in step.events
                            ),
                            progress=tuple(
                                replace(progress, received_at=now) for progress in step.progress
                            ),
                        )
                        results.append(algorithm.process(delivered))
                finally:
                    algorithm.close()
                measurements = [
                    measurement for result in results for measurement in result.measurements
                ]
                self.assertEqual(len(measurements), 36)
                self.assertTrue(
                    all(
                        measurement.status is m.FusionStatus.LOCATED for measurement in measurements
                    )
                )
                self.assertTrue(
                    all(
                        len(measurement.supporting_station_ids) == 2
                        and measurement.motion_alignment is None
                        for measurement in measurements[:2]
                    )
                )
                self.assertEqual(
                    [len(measurement.supporting_station_ids) for measurement in measurements],
                    [3 if index >= 2 and index in upper_windows else 2 for index in range(36)],
                )
                self.assertEqual(
                    [measurement.motion_alignment is not None for measurement in measurements],
                    [index >= 2 and index in upper_windows for index in range(36)],
                )
                for measurement in measurements:
                    np.testing.assert_allclose(
                        np.asarray(measurement.position_enu_m),
                        np.asarray((-15 + measurement.reference_time_ns / 1e9, 62, 20)),
                        rtol=0,
                        atol=1e-9,
                    )
                tracks = [track for result in results for track in result.tracks]
                self.assertEqual(len({track.track_id for track in tracks}), 1)
                for track in tracks:
                    if track.lifecycle is m.TrackLifecycle.CONFIRMED:
                        np.testing.assert_allclose(
                            np.asarray(track.velocity_enu_mps),
                            np.asarray((1.0, 0.0, 0.0)),
                            rtol=0,
                            atol=1e-8,
                        )
                signatures.append([asdict(measurement) for measurement in measurements])
        self.assertEqual(signatures[0], signatures[1])

    def noisy_steps(
        self,
        stations: tuple[m.StationConfig, ...],
        *,
        station_periods_ns: dict[str, tuple[int, int]] | None = None,
    ) -> list[CenterInputStep]:
        return self.steps(
            stations=stations,
            targets=(DEFAULT_TARGET,),
            duration_ns=500_000_000,
            station_periods_ns=station_periods_ns,
            angular_covariance_rad2=((1e-8, 2e-9), (2e-9, 1e-8)),
            origin_covariance_m2=(
                (1e-4, 0.0, 0.0),
                (0.0, 1e-4, 0.0),
                (0.0, 0.0, 1e-4),
            ),
            calibration_phases_ns=(
                (0, m.CalibrationHealth.VALID, m.CalibrationUsage.VALID),
                (100_000_000, m.CalibrationHealth.FROZEN, m.CalibrationUsage.FROZEN),
                (200_000_000, m.CalibrationHealth.STALE, m.CalibrationUsage.STALE),
            ),
            jitter_rad=math.radians(0.005),
        )

    def test_noisy_source_tracks_through_frozen_and_stale_calibration(self) -> None:
        stations = default_station_configs(include_upper=True)
        registration = replace(
            self.registration,
            stations=stations,
            policy=replace(
                self.policy,
                geometry=replace(self.policy.geometry, robust_loss_definition="huber"),
            ),
        )
        algorithm = create_algorithm()
        algorithm.start(registration)
        try:
            results = [algorithm.process(step) for step in self.noisy_steps(stations)]
        finally:
            algorithm.close()
        measurements = [measurement for result in results for measurement in result.measurements]
        self.assertEqual(len(measurements), 15)
        self.assertTrue(
            all(measurement.status is m.FusionStatus.LOCATED for measurement in measurements)
        )
        for measurement in measurements:
            expected = np.asarray((-15 + measurement.reference_time_ns / 1e9, 62, 20))
            self.assertLess(
                float(np.linalg.norm(np.asarray(measurement.position_enu_m) - expected)), 0.3
            )
            covariance = np.asarray(measurement.conditional_position_covariance_enu_m2)
            self.assertGreater(float(np.linalg.eigvalsh(covariance).min()), 0)
        geometry = [
            item
            for result in results
            for batch in result.batches
            for group in batch.geometry
            for item in group.results
        ]
        self.assertTrue(all(item.solver_converged for item in geometry))
        self.assertTrue(
            any(
                any("frozen" in weight.reason_codes[0] for weight in item.ray_weights)
                for item in geometry
            )
        )
        self.assertTrue(
            any(
                any("stale" in weight.reason_codes[0] for weight in item.ray_weights)
                for item in geometry
            )
        )
        self.assertTrue(all(len(item.shared_calibration_solution_refs) == 3 for item in geometry))
        tracks = [track for result in results for track in result.tracks]
        self.assertEqual(len({track.track_id for track in tracks}), 1)
        self.assertTrue(any(track.lifecycle is m.TrackLifecycle.CONFIRMED for track in tracks))
        encoded = json.dumps([asdict(result) for result in results], allow_nan=False)
        self.assertNotIn("truth_object_id", encoded)
        self.assertNotIn("experiment_perturbations", encoded)
        self.assertNotIn("moving-sphere", encoded)

    def test_noisy_asynchronous_source_keeps_the_velocity_dependence_in_tracking(self) -> None:
        stations = default_station_configs(include_upper=True)
        algorithm = create_algorithm()
        algorithm.start(replace(self.registration, stations=stations, policy=self.policy))
        try:
            results = [
                algorithm.process(step)
                for step in self.noisy_steps(
                    stations, station_periods_ns={"upper": (10_000_000, 10**9 // 29)}
                )
            ]
        finally:
            algorithm.close()
        measurements = [measurement for result in results for measurement in result.measurements]
        self.assertEqual(len(measurements), 15)
        self.assertTrue(
            all(measurement.status is m.FusionStatus.LOCATED for measurement in measurements)
        )
        aligned = [
            measurement for measurement in measurements if measurement.motion_alignment is not None
        ]
        self.assertTrue(aligned)
        self.assertTrue(
            all(len(measurement.supporting_station_ids) == 3 for measurement in aligned)
        )
        for measurement in aligned:
            motion = measurement.motion_alignment
            assert motion is not None
            covariance = np.asarray(measurement.conditional_position_covariance_enu_m2)
            independent = np.asarray(motion.independent_position_covariance_enu_m2)
            self.assertGreater(float(np.linalg.eigvalsh(independent).min()), 0)
            self.assertGreaterEqual(
                float(np.linalg.eigvalsh(covariance - independent).min()), -1e-14
            )
            expected = np.asarray((-15 + measurement.reference_time_ns / 1e9, 62, 20))
            self.assertLess(
                float(np.linalg.norm(np.asarray(measurement.position_enu_m) - expected)), 0.3
            )
        tracks = [track for result in results for track in result.tracks]
        self.assertEqual(len({track.track_id for track in tracks}), 1)
        self.assertIs(tracks[-1].lifecycle, m.TrackLifecycle.CONFIRMED)

    def test_station_silence_missing_detections_and_recovery_keep_one_track(self) -> None:
        stations = default_station_configs(include_upper=True)
        width = self.policy.windows.width_ns
        policy = replace(
            self.policy,
            windows=replace(
                self.policy.windows, idle_after_ns=2 * width, offline_after_ns=4 * width
            ),
            tracking=replace(
                self.policy.tracking, coast_after_ns=3 * width, delete_after_ns=8 * width
            ),
        )
        algorithm = create_algorithm()
        algorithm.start(replace(self.registration, stations=stations, policy=policy))
        results: list[CenterAlgorithmResult] = []
        old: m.ReceivedEvent | None = None
        late_result = None
        snapshots = []
        try:
            for step in self.steps(
                stations=stations,
                targets=(DEFAULT_TARGET,),
                duration_ns=600_000_000,
            ):
                if old is not None and late_result is None and step.time.time_ns >= 300_000_000:
                    snapshots = [
                        asdict(measurement)
                        for result in results
                        for measurement in result.measurements
                    ]
                    now = replace(step.time, time_ns=step.time.time_ns - 1)
                    late_result = algorithm.process(
                        CenterInputStep(
                            step_no=step.step_no,
                            time=now,
                            solutions=(),
                            events=(replace(old, arrival_time=now),),
                            progress=(),
                            stream_ended=False,
                        )
                    )
                    self.assertFalse(late_result.measurements)
                    self.assertTrue(
                        any(
                            receipt.disposition is f.WindowDisposition.LATE_AFTER_SEAL
                            for batch in late_result.batches
                            for receipt in batch.window_update.receipts
                        )
                    )
                    self.assertTrue(
                        any(
                            progress.health is f.StationStreamHealth.RECOVERING
                            for batch in late_result.batches
                            for progress in batch.window_update.watermark.station_progress
                        )
                    )
                    self.assertEqual(
                        snapshots,
                        [
                            asdict(measurement)
                            for result in results
                            for measurement in result.measurements
                        ],
                    )
                silence = 100_000_000 <= step.time.time_ns < 300_000_000
                missing = 100_000_000 <= step.time.time_ns < 200_000_000
                if silence and old is None:
                    old = next(
                        (
                            received
                            for received in step.events
                            if isinstance(received.event, m.RayEvent)
                            and received.event.envelope.station_id == "east"
                        ),
                        None,
                    )
                delivered = replace(
                    step,
                    events=tuple(
                        received
                        for received in step.events
                        if not (silence and received.event.envelope.station_id == "east")
                        and not (
                            missing
                            and isinstance(received.event, m.RayEvent)
                            and received.event.envelope.station_id == "upper"
                        )
                    ),
                    progress=tuple(
                        progress
                        for progress in step.progress
                        if not (silence and progress.station_id == "east")
                    ),
                )
                results.append(algorithm.process(delivered))
        finally:
            algorithm.close()
        self.assertIsNotNone(late_result)
        tracks = [track for result in results for track in result.tracks]
        self.assertEqual(len({track.track_id for track in tracks}), 1)
        self.assertTrue(any(track.lifecycle is m.TrackLifecycle.COASTING for track in tracks))
        self.assertTrue(any("track_recovered" in track.reason_codes for track in tracks))
        self.assertFalse(any(track.lifecycle is m.TrackLifecycle.DELETED for track in tracks))
        self.assertTrue(
            any(
                len(measurement.supporting_station_ids) == 2
                for result in results
                for measurement in result.measurements
            )
        )
        self.assertEqual(
            len(
                [measurement for result in results for measurement in result.measurements][
                    -1
                ].supporting_station_ids
            ),
            3,
        )
        self.assertTrue(
            any(
                progress.health is f.StationStreamHealth.OFFLINE
                for result in results
                for batch in result.batches
                for progress in batch.window_update.watermark.station_progress
            )
        )

    def test_all_station_silence_expires_tracks_through_sealed_mapped_clock_windows(self) -> None:
        steps = self.steps()
        for step in steps[:3]:
            self.adapter.process(step)
        # Consume the buffered third exposure via a genuinely completed empty
        # fourth batch before silence; do not discard already buffered rays.
        self.adapter.process(replace(steps[3], events=(), solutions=()))
        now = replace(steps[0].time, time_ns=4 * 10**9)
        result = self.center.fusion.tick(now)
        self.assertTrue(result.window_update.sealed_windows)
        self.assertTrue(
            all(
                not window.rays and window.support_exposure_span_ns == 0
                for window in result.window_update.sealed_windows
            )
        )
        self.assertFalse(result.measurements)
        self.assertTrue(
            any(track.lifecycle is m.TrackLifecycle.COASTING for track in result.tracks)
        )
        self.assertEqual(
            sum(track.lifecycle is m.TrackLifecycle.DELETED for track in result.tracks), 1
        )
        self.assertFalse(self.center.tracking.predict(now.clock_domain_id, now.time_ns))
        old = next(
            received for received in steps[3].events if isinstance(received.event, m.RayEvent)
        )
        late = self.center.fusion.process(
            replace(old, arrival_time=replace(now, time_ns=now.time_ns + 1))
        )
        self.assertFalse(late.measurements)
        self.assertTrue(
            any(
                receipt.disposition is f.WindowDisposition.LATE_AFTER_SEAL
                for receipt in late.window_update.receipts
            )
        )

    def test_online_composition_rejects_inconsistent_clock_and_frame_policy(self) -> None:
        for policy, mapping in (
            (
                replace(
                    self.registration.policy,
                    admission=replace(
                        self.registration.policy.admission, world_frame_id=m.WorldFrameId("other")
                    ),
                ),
                self.registration.clock_mapping,
            ),
            (
                self.registration.policy,
                replace(self.registration.clock_mapping, capture_clock_domain_id="other"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "coordinate/clock"):
                declare_center(
                    center_session_id=m.SessionId(self.registration.center_session_id),
                    policy=policy,
                    clock_mapping=mapping,
                )


if __name__ == "__main__":
    unittest.main()

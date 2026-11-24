"""Center input behavior using ordinary events, without rendering or truth access."""

from __future__ import annotations

import math
import unittest
from dataclasses import replace
from pathlib import Path

from chongming.contracts import fusion as f
from chongming.contracts import model as m
from chongming.modules.fusion_ingress import (
    AdmissionJournalModule,
    CalibrationRegistryModule,
    EventAdmissionModule,
    EventClockMappingModule,
    EventWindowModule,
    StationRegistryModule,
)
from tests.online_fixtures import (
    CLOCK_DOMAIN_ID,
    SyntheticTarget,
    default_clock_mapping,
    default_fusion_policy,
    default_station_configs,
    generate_synthetic_steps,
)

ROOT = Path(__file__).resolve().parents[1]
BASE = 1_000_000


class FusionIngressTests(unittest.TestCase):
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
        policy = default_fusion_policy()
        self.rays = tuple(rec.event for rec in steps[0].events if isinstance(rec.event, m.RayEvent))
        self.configs = tuple(
            next(config for config in configs if config.station_id == ray.envelope.station_id)
            for ray in self.rays
        )
        self.solutions = solutions
        self.states = tuple(
            rec.event for rec in steps[0].events if isinstance(rec.event, m.CalibrationStateEvent)
        )
        self.clock_id = CLOCK_DOMAIN_ID
        self.stations = StationRegistryModule()
        self.calibrations = CalibrationRegistryModule(stations=self.stations)
        for config in self.configs:
            self.assertIs(self.stations.register(config).status, f.RegistryWriteStatus.REGISTERED)
        for solution in self.solutions:
            self.assertIs(
                self.calibrations.register_solution(solution).status,
                f.RegistryWriteStatus.REGISTERED,
            )
        self.journal = AdmissionJournalModule()
        self.clock = EventClockMappingModule(mapping=default_clock_mapping())
        self.admission = EventAdmissionModule(
            policy=policy.admission,
            stations=self.stations,
            calibrations=self.calibrations,
            journal=self.journal,
            clock=self.clock,
        )
        self.window_policy = replace(
            policy.windows,
            origin_ns=BASE,
            width_ns=10,
            allowed_lateness_ns=2,
            awaiting_first_event_timeout_ns=20,
            idle_after_ns=40,
            offline_after_ns=80,
            exclude_idle_stations=False,
            exclude_offline_stations=False,
            no_active_station_policy=f.EmptyActiveStationPolicy.HOLD,
        )

    def time(self, offset: int) -> m.ProcessingTime:
        return m.ProcessingTime(clock_domain_id=self.clock_id, time_ns=BASE + offset)

    def ray(self, station: int = 0, offset: int = 0, sequence: int = 2) -> m.RayEvent:
        ray = self.rays[station]
        return replace(
            ray,
            envelope=replace(ray.envelope, sequence_no=sequence),
            capture_time=replace(
                ray.capture_time, exposure_start_ns=BASE + offset, exposure_end_ns=BASE + offset
            ),
        )

    def admit(self, event: m.StationEvent, arrival: int) -> f.AdmissionOutcome:
        return self.admission.admit(m.ReceivedEvent(event=event, arrival_time=self.time(arrival)))

    def windows(self, policy: f.EventWindowPolicy | None = None) -> EventWindowModule:
        return EventWindowModule(
            center_session_id=m.SessionId("center-test"),
            policy=self.window_policy if policy is None else policy,
            clock=self.clock,
            stations=self.stations,
        )

    def progress(
        self,
        station: int,
        offset: int | None,
        arrival: int,
        *,
        ended: bool = False,
        session: m.SessionId | None = None,
    ) -> f.StationEventTimeProgress:
        return f.StationEventTimeProgress(
            station_id=self.configs[station].station_id,
            station_session_id=self.rays[station].envelope.station_session_id
            if session is None
            else session,
            station_config_digest=self.configs[station].config_digest,
            completed_capture_time=None
            if offset is None
            else self.ray(station, offset).capture_time,
            received_at=self.time(arrival),
            stream_ended=ended,
        )

    def test_station_snapshots_are_versioned_and_immutable(self) -> None:
        config = self.configs[0]
        self.assertIs(
            self.stations.register(config).status, f.RegistryWriteStatus.ALREADY_REGISTERED
        )
        conflict = replace(config, main_camera_position_enu_m=(1.0, 2.0, 3.0))
        self.assertIs(self.stations.register(conflict).status, f.RegistryWriteStatus.CONFLICT)
        self.assertEqual(self.stations.lookup(config.station_id, config.config_digest), config)
        digest = m.ConfigDigest("new-config")
        updated = replace(
            conflict,
            config_digest=digest,
            installation_uncertainty=replace(
                config.installation_uncertainty,
                state_ref=replace(config.installation_uncertainty.state_ref, config_digest=digest),
            ),
        )
        self.assertIs(self.stations.register(updated).status, f.RegistryWriteStatus.REGISTERED)
        self.assertEqual(self.stations.lookup(config.station_id, digest), updated)
        self.assertEqual(
            set(self.stations.registered_stations()), {c.station_id for c in self.configs}
        )
        invalid = replace(updated, config_digest=m.ConfigDigest("mismatched-installation"))
        self.assertIs(self.stations.register(invalid).status, f.RegistryWriteStatus.REJECTED)

    def test_calibration_snapshots_and_session_scopes(self) -> None:
        solution = self.solutions[0]
        self.assertIs(
            self.calibrations.register_solution(solution).status,
            f.RegistryWriteStatus.ALREADY_REGISTERED,
        )
        conflict = replace(solution, main_camera_position_enu_m=(1.0, 2.0, 3.0))
        self.assertIs(
            self.calibrations.register_solution(conflict).status, f.RegistryWriteStatus.CONFLICT
        )
        self.assertEqual(self.calibrations.lookup_solution(solution.solution_ref), solution)
        other_ref = replace(solution.solution_ref, station_session_id=m.SessionId("new-session"))
        self.assertIsNone(self.calibrations.lookup_solution(other_ref))
        self.assertIs(
            self.calibrations.register_solution(replace(solution, solution_ref=other_ref)).status,
            f.RegistryWriteStatus.REGISTERED,
        )
        for bad in (
            replace(
                solution,
                solution_ref=replace(other_ref, solution_id=m.CalibrationSolutionId("unknown")),
                station_config_digest=m.ConfigDigest("unknown"),
            ),
            replace(
                solution,
                solution_ref=replace(other_ref, solution_id=m.CalibrationSolutionId("wrong-world")),
                world_frame_id=m.WorldFrameId("other-world"),
            ),
            replace(
                solution,
                solution_ref=replace(
                    other_ref, solution_id=m.CalibrationSolutionId("bad-covariance")
                ),
                correction_covariance_enu_rad2=((-1.0, 0.0, 0.0),) * 3,
            ),
        ):
            with self.subTest(solution=bad.solution_ref):
                self.assertIs(
                    self.calibrations.register_solution(bad).status, f.RegistryWriteStatus.REJECTED
                )
                self.assertIsNone(self.calibrations.lookup_solution(bad.solution_ref))

    def test_state_history_accepts_reordering_without_replacing_solutions(self) -> None:
        event = self.states[0]
        frozen = replace(
            event,
            envelope=replace(event.envelope, sequence_no=10),
            solution=None,
            state=replace(
                event.state,
                state_version=2,
                health=m.CalibrationHealth.FROZEN,
                observed_at=self.time(10),
            ),
        )
        self.assertIs(
            self.calibrations.record_state(frozen).status, f.RegistryWriteStatus.REGISTERED
        )
        self.assertIs(
            self.calibrations.record_state(event).status, f.RegistryWriteStatus.REGISTERED
        )
        history = self.calibrations.state_history(
            event.state.station_id, event.state.station_session_id
        )
        self.assertEqual([state.state_version for state in history], [1, 2])
        self.assertIs(
            self.calibrations.record_state(event).status, f.RegistryWriteStatus.ALREADY_REGISTERED
        )
        self.assertIs(
            self.calibrations.record_state(
                replace(frozen, state=replace(frozen.state, health=m.CalibrationHealth.STALE))
            ).status,
            f.RegistryWriteStatus.CONFLICT,
        )
        self.assertIs(
            self.calibrations.record_state(
                replace(
                    event,
                    solution=replace(self.solutions[0], main_camera_position_enu_m=(1.0, 2.0, 3.0)),
                )
            ).status,
            f.RegistryWriteStatus.CONFLICT,
        )
        self.assertIs(
            self.calibrations.record_state(
                replace(
                    event,
                    envelope=replace(
                        event.envelope, station_session_id=m.SessionId("wrong-session")
                    ),
                )
            ).status,
            f.RegistryWriteStatus.REJECTED,
        )
        self.assertEqual(
            self.calibrations.lookup_solution(self.solutions[0].solution_ref), self.solutions[0]
        )

    def test_admission_allows_reordering_and_deduplicates_complete_ids(self) -> None:
        first = self.ray(offset=20, sequence=10)
        self.assertIs(self.admit(first, 20).record.decision, f.AdmissionDecision.ACCEPTED)
        self.assertIs(
            self.admit(self.ray(offset=10, sequence=3), 21).record.decision,
            f.AdmissionDecision.ACCEPTED,
        )
        self.assertIs(self.admit(first, 22).record.decision, f.AdmissionDecision.DUPLICATE)
        conflict = self.admit(replace(first, detection_score=0.1), 23)
        self.assertEqual(conflict.record.reasons, (f.AdmissionReason.EVENT_ID_PAYLOAD_CONFLICT,))
        self.assertEqual(self.journal.first_event(conflict.record.event_id), first)
        other_ref = replace(
            first.calibration_solution_ref, station_session_id=m.SessionId("restart")
        )
        self.calibrations.register_solution(replace(self.solutions[0], solution_ref=other_ref))
        restarted = replace(
            first,
            envelope=replace(first.envelope, station_session_id=other_ref.station_session_id),
            calibration_solution_ref=other_ref,
            uncertainty=replace(first.uncertainty, shared_calibration_solution_ref=other_ref),
        )
        self.assertIs(self.admit(restarted, 24).record.decision, f.AdmissionDecision.ACCEPTED)

    def test_quarantine_keeps_original_arrival_and_survives_duplicate_conflicts(self) -> None:
        stations = StationRegistryModule()
        calibrations = CalibrationRegistryModule(stations=stations)
        admission = replace(self.admission, stations=stations, calibrations=calibrations)
        received = m.ReceivedEvent(event=self.ray(), arrival_time=self.time(0))
        first = admission.admit(received)
        self.assertEqual(first.record.reasons, (f.AdmissionReason.CONFIGURATION_UNKNOWN,))
        self.assertIs(
            admission.admit(replace(received, arrival_time=self.time(1))).record.decision,
            f.AdmissionDecision.DUPLICATE,
        )
        admission.admit(
            replace(
                received, event=replace(self.ray(), detection_score=0.1), arrival_time=self.time(2)
            )
        )
        self.assertEqual(self.journal.quarantined(), (first.record,))
        stations.register(self.configs[0])
        retry = admission.reconsider_quarantined(self.time(3))[0]
        self.assertEqual(retry.record.reasons, (f.AdmissionReason.CALIBRATION_SOLUTION_UNKNOWN,))
        self.assertEqual(self.journal.quarantined(), (retry.record,))
        calibrations.register_solution(self.solutions[0])
        accepted = admission.reconsider_quarantined(self.time(4))[0]
        self.assertIs(accepted.record.decision, f.AdmissionDecision.ACCEPTED)
        assert accepted.admitted_ray is not None
        self.assertEqual(accepted.admitted_ray.arrival_time, self.time(0))
        self.assertEqual(accepted.record.received, received)
        self.assertEqual(accepted.record.checked_at, self.time(4))
        self.assertFalse(self.journal.quarantined())
        self.assertFalse(admission.reconsider_quarantined(self.time(5)))

    def test_invalid_rays_have_specific_rejection_reasons(self) -> None:
        ray = self.ray()
        invalid = (
            (
                replace(ray, envelope=replace(ray.envelope, schema_version="unknown")),
                f.AdmissionReason.SCHEMA_UNSUPPORTED,
            ),
            (
                replace(ray, envelope=replace(ray.envelope, event_type=m.EventType.DIAGNOSTIC)),
                f.AdmissionReason.EVENT_TYPE_MISMATCH,
            ),
            (
                replace(ray, envelope=replace(ray.envelope, sequence_no=0)),
                f.AdmissionReason.INVALID_SEQUENCE,
            ),
            (
                replace(ray, world_frame_id=m.WorldFrameId("wrong-world")),
                f.AdmissionReason.WORLD_FRAME_MISMATCH,
            ),
            (
                replace(ray, capture_time=replace(ray.capture_time, clock_domain_id="wrong-clock")),
                f.AdmissionReason.CLOCK_DOMAIN_MISMATCH,
            ),
            (
                replace(ray, capture_time=replace(ray.capture_time, exposure_end_ns=BASE - 1)),
                f.AdmissionReason.EXPOSURE_INTERVAL_INVALID,
            ),
            (
                replace(ray, capture_time=replace(ray.capture_time, clock_uncertainty_ns=-1)),
                f.AdmissionReason.EXPOSURE_INTERVAL_INVALID,
            ),
            (
                replace(ray, ray_direction_enu_unit=(math.nan, 0.0, 0.0)),
                f.AdmissionReason.NONFINITE_RAY,
            ),
            (
                replace(ray, ray_direction_enu_unit=(0.0, 0.0, 0.0)),
                f.AdmissionReason.NONUNIT_DIRECTION,
            ),
            (
                replace(
                    ray,
                    uncertainty=replace(
                        ray.uncertainty, tangent_v_enu_unit=ray.uncertainty.tangent_u_enu_unit
                    ),
                ),
                f.AdmissionReason.INVALID_TANGENT_BASIS,
            ),
            (
                replace(
                    ray,
                    uncertainty=replace(
                        ray.uncertainty,
                        independent_angular_covariance_rad2=((-1e-30, 0.0), (0.0, 0.0)),
                    ),
                ),
                f.AdmissionReason.INVALID_UNCERTAINTY,
            ),
            (
                replace(
                    ray,
                    calibration_solution_ref=replace(
                        ray.calibration_solution_ref,
                        station_session_id=m.SessionId("wrong-session"),
                    ),
                ),
                f.AdmissionReason.CALIBRATION_SOLUTION_SCOPE_MISMATCH,
            ),
            (
                replace(
                    ray,
                    uncertainty=replace(
                        ray.uncertainty,
                        shared_installation_state_ref=replace(
                            ray.uncertainty.shared_installation_state_ref,
                            config_digest=m.ConfigDigest("wrong-config"),
                        ),
                    ),
                ),
                f.AdmissionReason.SHARED_ERROR_REFERENCE_MISMATCH,
            ),
            (
                replace(
                    ray,
                    calibration_evidence=replace(
                        ray.calibration_evidence, solution_accepted_time=self.time(1)
                    ),
                ),
                f.AdmissionReason.CALIBRATION_EVIDENCE_MISMATCH,
            ),
        )
        for event, reason in invalid:
            with self.subTest(reason=reason):
                admission = replace(self.admission, journal=AdmissionJournalModule())
                outcome = admission.admit(m.ReceivedEvent(event=event, arrival_time=self.time(0)))
                self.assertIs(outcome.record.decision, f.AdmissionDecision.REJECTED)
                self.assertEqual(outcome.record.reasons, (reason,))
                self.assertIsNone(outcome.admitted_ray)

        singular = replace(
            ray,
            uncertainty=replace(
                ray.uncertainty,
                independent_angular_covariance_rad2=((1e-12, 1e-12), (1e-12, 1e-12)),
                independent_origin_covariance_m2=((1.0, 1.0, 1.0),) * 3,
            ),
        )
        outcome = self.admit(singular, 0)
        self.assertIs(outcome.record.decision, f.AdmissionDecision.ACCEPTED)

    def test_frozen_and_stale_usage_follow_policy_and_keep_original_solution(self) -> None:
        for usage in m.CalibrationUsage:
            for action in f.CalibrationAction:
                with self.subTest(usage=usage, action=action):
                    ray = replace(self.ray(), calibration_usage=usage)
                    policy = replace(
                        self.admission.policy,
                        calibration_usage_rules=tuple(
                            replace(rule, action=action) if rule.usage is usage else rule
                            for rule in self.admission.policy.calibration_usage_rules
                        ),
                    )
                    admission = replace(
                        self.admission, policy=policy, journal=AdmissionJournalModule()
                    )
                    outcome = admission.admit(m.ReceivedEvent(event=ray, arrival_time=self.time(0)))
                    expected = {
                        f.CalibrationAction.ACCEPT: f.AdmissionDecision.ACCEPTED,
                        f.CalibrationAction.QUARANTINE: f.AdmissionDecision.QUARANTINED,
                        f.CalibrationAction.REJECT: f.AdmissionDecision.REJECTED,
                    }[action]
                    self.assertIs(outcome.record.decision, expected)
                    if outcome.admitted_ray is not None:
                        self.assertEqual(
                            outcome.admitted_ray.calibration_solution, self.solutions[0]
                        )
        end = self.solutions[0].valid_interval.end_ns
        assert end is not None
        ray = replace(
            self.ray(offset=end - BASE),
            calibration_usage=m.CalibrationUsage.FROZEN,
            calibration_evidence=replace(
                self.rays[0].calibration_evidence,
                usable_interval=replace(
                    self.rays[0].calibration_evidence.usable_interval, end_ns=None
                ),
            ),
        )
        self.assertIs(self.admit(ray, end - BASE).record.decision, f.AdmissionDecision.ACCEPTED)
        valid = replace(
            ray,
            envelope=replace(ray.envelope, sequence_no=3),
            calibration_usage=m.CalibrationUsage.VALID,
        )
        self.assertEqual(
            self.admit(valid, end - BASE).record.reasons,
            (f.AdmissionReason.CALIBRATION_EXPOSURE_OUTSIDE_VALIDITY,),
        )

    def test_controls_are_audited_without_becoming_observations(self) -> None:
        calibrations = CalibrationRegistryModule(stations=self.stations)
        admission = replace(self.admission, calibrations=calibrations)
        state = admission.admit(m.ReceivedEvent(event=self.states[0], arrival_time=self.time(0)))
        self.assertIs(state.record.decision, f.AdmissionDecision.ACCEPTED)
        self.assertIsNone(state.admitted_ray)
        self.assertEqual(
            calibrations.lookup_solution(self.solutions[0].solution_ref), self.solutions[0]
        )
        diagnostic = m.DiagnosticEvent(
            envelope=replace(
                self.rays[0].envelope, event_type=m.EventType.DIAGNOSTIC, sequence_no=99
            ),
            related_frame_ref=m.FrameRef(
                station_id=self.rays[0].envelope.station_id,
                station_session_id=m.SessionId("independent-image-session"),
                camera_role=m.CameraRole.MAIN,
                frame_id=self.rays[0].frame_id,
            ),
            capture_time=self.ray().capture_time,
            category="no_detection",
            reason_codes=("no_detection",),
            related_config_ref=self.configs[0].config_digest,
            related_calibration_ref=None,
        )
        outcome = admission.admit(m.ReceivedEvent(event=diagnostic, arrival_time=self.time(1)))
        self.assertIs(outcome.record.decision, f.AdmissionDecision.ACCEPTED)
        windows = self.windows()
        for control in (state, outcome):
            update = windows.offer(control)
            self.assertEqual(
                update.receipts[0].disposition, f.WindowDisposition.NOT_A_FUSION_OBSERVATION
            )
            self.assertFalse(update.sealed_windows)

        for event, reason in (
            (
                replace(
                    self.states[0],
                    state=replace(
                        self.states[0].state,
                        usable_interval=replace(
                            self.solutions[0].valid_interval, clock_domain_id="wrong-clock"
                        ),
                    ),
                ),
                f.AdmissionReason.CLOCK_DOMAIN_MISMATCH,
            ),
            (
                replace(
                    self.states[0], state=replace(self.states[0].state, last_trusted_time=None)
                ),
                f.AdmissionReason.CALIBRATION_EVIDENCE_MISMATCH,
            ),
            (
                replace(
                    diagnostic,
                    related_calibration_ref=replace(
                        self.solutions[0].solution_ref,
                        station_session_id=m.SessionId("wrong-session"),
                    ),
                ),
                f.AdmissionReason.CALIBRATION_SOLUTION_SCOPE_MISMATCH,
            ),
        ):
            with self.subTest(reason=reason):
                outcome = replace(admission, journal=AdmissionJournalModule()).admit(
                    m.ReceivedEvent(event=event, arrival_time=self.time(2))
                )
                self.assertEqual(outcome.record.reasons, (reason,))
                self.assertIs(outcome.record.decision, f.AdmissionDecision.REJECTED)

    def test_clock_mapping_is_explicit_and_uses_integer_arithmetic(self) -> None:
        mapping = f.EventClockMappingSpec(
            processing_clock_domain_id="cpu",
            capture_clock_domain_id="camera",
            scale_numerator=3,
            scale_denominator=2,
            offset_ns=7,
            mapping_uncertainty_ns=2,
        )
        clock = EventClockMappingModule(mapping=mapping)
        reading = clock.map_processing_time(m.ProcessingTime(clock_domain_id="cpu", time_ns=100))
        self.assertEqual(
            reading,
            f.EventClockReading(
                clock_domain_id="camera", event_time_ns=157, mapping_uncertainty_ns=2
            ),
        )
        with self.assertRaisesRegex(ValueError, "clock domain"):
            clock.map_processing_time(self.time(0))
        with self.assertRaisesRegex(ValueError, "positive scale"):
            EventClockMappingModule(mapping=replace(mapping, scale_denominator=0))

    def test_admission_maps_calibration_time_and_rejects_future_use(self) -> None:
        clock = replace(
            self.clock,
            mapping=replace(
                self.clock.mapping,
                processing_clock_domain_id="cpu",
                offset_ns=BASE,
            ),
        )
        accepted_at = m.ProcessingTime(clock_domain_id="cpu", time_ns=0)
        solution = replace(self.solutions[0], accepted_time=accepted_at)
        calibrations = CalibrationRegistryModule(stations=self.stations)
        calibrations.register_solution(solution)
        ray = replace(
            self.ray(),
            calibration_evidence=replace(
                self.rays[0].calibration_evidence,
                solution_accepted_time=accepted_at,
                last_trusted_time=accepted_at,
            ),
        )
        received = m.ReceivedEvent(event=ray, arrival_time=accepted_at)
        admission = replace(self.admission, clock=clock, calibrations=calibrations)
        self.assertIs(admission.admit(received).record.decision, f.AdmissionDecision.ACCEPTED)
        future_clock = replace(clock, mapping=replace(clock.mapping, offset_ns=BASE + 1))
        outcome = replace(admission, clock=future_clock, journal=AdmissionJournalModule()).admit(
            received
        )
        self.assertEqual(
            outcome.record.reasons, (f.AdmissionReason.CALIBRATION_EXPOSURE_OUTSIDE_VALIDITY,)
        )
        wrong_clock = replace(received, arrival_time=self.time(0))
        outcome = replace(admission, journal=AdmissionJournalModule()).admit(wrong_clock)
        self.assertEqual(outcome.record.reasons, (f.AdmissionReason.CLOCK_DOMAIN_MISMATCH,))

    def test_reconsidered_ray_cannot_reopen_a_sealed_window(self) -> None:
        calibrations = CalibrationRegistryModule(stations=self.stations)
        admission = replace(self.admission, calibrations=calibrations)
        windows = self.windows()
        received = m.ReceivedEvent(event=self.ray(), arrival_time=self.time(0))
        windows.offer(admission.admit(received))
        windows.report_progress(self.progress(0, 22, 22))
        sealed = windows.report_progress(self.progress(1, 22, 23)).sealed_windows
        self.assertEqual(len(sealed), 2)
        calibrations.register_solution(self.solutions[0])
        accepted = admission.reconsider_quarantined(self.time(24))[0]
        self.assertEqual(accepted.record.received.arrival_time, self.time(0))
        update = windows.offer(accepted)
        self.assertIs(update.receipts[0].disposition, f.WindowDisposition.LATE_AFTER_SEAL)
        self.assertFalse(update.sealed_windows)
        self.assertTrue(all(not window.rays for window in sealed))

    def test_windows_use_exposure_midpoints_and_never_reopen(self) -> None:
        windows = self.windows()
        windows.advance(self.time(0))
        left_ray = replace(
            self.ray(),
            capture_time=replace(
                self.rays[0].capture_time,
                exposure_start_ns=BASE + 7,
                exposure_end_ns=BASE + 11,
            ),
        )
        right_ray = replace(
            self.ray(station=1),
            capture_time=replace(
                self.rays[1].capture_time,
                exposure_start_ns=BASE + 9,
                exposure_end_ns=BASE + 13,
            ),
        )
        left = windows.offer(self.admit(left_ray, 20))
        right = windows.offer(self.admit(right_ray, 21))
        left_window, right_window = left.receipts[0].window, right.receipts[0].window
        assert left_window is not None and right_window is not None
        self.assertEqual(left_window.start_ns, BASE)
        self.assertEqual(right_window.start_ns, BASE + 10)
        self.assertFalse(windows.report_progress(self.progress(0, 22, 22)).sealed_windows)
        update = windows.report_progress(self.progress(1, 22, 23))
        self.assertEqual(
            [window.key.start_ns for window in update.sealed_windows], [BASE, BASE + 10]
        )
        self.assertEqual(
            [window.reference_time_ns for window in update.sealed_windows], [BASE + 5, BASE + 15]
        )
        self.assertEqual([len(window.rays) for window in update.sealed_windows], [1, 1])
        self.assertEqual(
            [window.support_exposure_span_ns for window in update.sealed_windows], [4, 4]
        )
        sealed = update.sealed_windows
        late = windows.offer(self.admit(self.ray(offset=8, sequence=3), 24))
        self.assertIs(late.receipts[0].disposition, f.WindowDisposition.LATE_AFTER_SEAL)
        self.assertFalse(late.sealed_windows)
        duplicate = windows.offer(self.admit(left_ray, 25))
        self.assertIs(duplicate.receipts[0].disposition, f.WindowDisposition.ALREADY_CONSUMED)
        self.assertEqual([len(window.rays) for window in sealed], [1, 1])

    def test_out_of_order_rays_stay_in_their_unsealed_window(self) -> None:
        windows = self.windows()
        for ray, arrival in (
            (self.ray(offset=8, sequence=10), 9),
            (self.ray(offset=3, sequence=3), 10),
            (self.ray(station=1, offset=4), 11),
        ):
            self.assertIs(
                windows.offer(self.admit(ray, arrival)).receipts[0].disposition,
                f.WindowDisposition.BUFFERED,
            )
        windows.report_progress(self.progress(0, 12, 12))
        sealed = windows.report_progress(self.progress(1, 12, 13)).sealed_windows
        self.assertEqual(len(sealed), 1)
        self.assertEqual(len(sealed[0].rays), 3)
        self.assertEqual(sealed[0].support_exposure_span_ns, 5)

    def test_empty_progress_and_finite_end_preserve_the_last_exposure(self) -> None:
        windows = self.windows()
        windows.report_progress(self.progress(0, 25, 25))
        update = windows.report_progress(self.progress(1, 25, 26))
        self.assertEqual(len(update.sealed_windows), 2)
        self.assertTrue(
            all(
                not window.rays and window.support_exposure_span_ns == 0
                for window in update.sealed_windows
            )
        )
        windows.report_progress(self.progress(0, 25, 27, ended=True))
        ended = windows.report_progress(self.progress(1, 25, 28, ended=True))
        self.assertEqual(len(ended.sealed_windows), 1)
        self.assertIs(ended.sealed_windows[0].seal_reason, f.WindowSealReason.STREAMS_ENDED)
        self.assertEqual(ended.sealed_windows[0].key.end_ns, BASE + 30)
        self.assertTrue(
            all(
                item.maximum_seen_exposure_midpoint_ns == BASE + 25
                for item in ended.watermark.station_progress
            )
        )
        self.assertFalse(
            windows.report_progress(self.progress(1, 25, 29, ended=True)).sealed_windows
        )
        self.assertFalse(windows.advance(self.time(10**6)).sealed_windows)
        with self.assertRaisesRegex(ValueError, "ended stream"):
            windows.report_progress(self.progress(1, 35, 10**6 + 1, ended=True))

    def test_zero_sample_runs_never_fabricate_windows(self) -> None:
        for policy in f.EmptyActiveStationPolicy:
            windows = self.windows(replace(self.window_policy, no_active_station_policy=policy))
            windows.advance(self.time(0))
            windows.report_progress(self.progress(0, None, 1, ended=True))
            ended = windows.report_progress(self.progress(1, None, 2, ended=True))
            self.assertFalse(ended.sealed_windows)
            self.assertTrue(
                all(
                    item.maximum_seen_exposure_midpoint_ns is None
                    for item in ended.watermark.station_progress
                )
            )
            self.assertFalse(windows.advance(self.time(10**6)).sealed_windows)

    def test_station_health_and_exclusion_control_watermark_progress(self) -> None:
        windows = self.windows(
            replace(
                self.window_policy,
                exclude_idle_stations=True,
                exclude_offline_stations=True,
                idle_after_ns=10,
                offline_after_ns=30,
                allowed_lateness_ns=0,
            )
        )
        windows.start_module("center/windows", self.time(0))
        initial = windows.advance(self.time(0))
        self.assertTrue(
            all(
                item.health is f.StationStreamHealth.AWAITING_FIRST_EVENT
                for item in initial.watermark.station_progress
            )
        )
        self.assertIsNone(windows.report_progress(self.progress(0, 15, 15)).watermark.event_time_ns)
        update = windows.advance(self.time(20))
        self.assertEqual(update.watermark.event_time_ns, BASE + 15)
        self.assertEqual(len(update.sealed_windows), 1)
        missing = next(
            item
            for item in update.watermark.station_progress
            if item.station_id == self.configs[1].station_id
        )
        self.assertIs(missing.health, f.StationStreamHealth.OFFLINE)
        self.assertFalse(missing.participates_in_watermark)
        idle = windows.advance(self.time(25))
        self.assertFalse(idle.sealed_windows)
        self.assertTrue(
            any(
                item.health is f.StationStreamHealth.IDLE
                for item in idle.watermark.station_progress
            )
        )
        offline = windows.advance(self.time(45))
        self.assertTrue(
            all(
                item.health is f.StationStreamHealth.OFFLINE
                for item in offline.watermark.station_progress
            )
        )
        recovered = windows.offer(self.admit(self.ray(station=1, offset=35), 46))
        self.assertEqual(len(recovered.sealed_windows), 2)
        self.assertEqual(recovered.watermark.event_time_ns, BASE + 35)

    def test_recovered_old_frames_do_not_stall_other_stations_or_rewrite_sealed_windows(
        self,
    ) -> None:
        windows = self.windows(
            replace(
                self.window_policy,
                allowed_lateness_ns=0,
                exclude_idle_stations=True,
                exclude_offline_stations=True,
                idle_after_ns=10,
                offline_after_ns=30,
            )
        )
        windows.start_module("center/windows", self.time(0))
        windows.report_progress(self.progress(0, 0, 0))
        windows.report_progress(self.progress(1, 0, 0))
        first = windows.report_progress(self.progress(1, 15, 15))
        self.assertEqual([window.key.start_ns for window in first.sealed_windows], [BASE])
        sealed = windows.report_progress(self.progress(1, 35, 35))
        self.assertEqual(
            [window.key.start_ns for window in sealed.sealed_windows], [BASE + 10, BASE + 20]
        )
        preserved = tuple(sealed.sealed_windows)
        late = windows.offer(self.admit(self.ray(station=0, offset=5, sequence=3), 36))
        self.assertIs(late.receipts[0].disposition, f.WindowDisposition.LATE_AFTER_SEAL)
        recovered = next(
            item
            for item in late.watermark.station_progress
            if item.station_id == self.configs[0].station_id
        )
        self.assertIs(recovered.health, f.StationStreamHealth.RECOVERING)
        self.assertFalse(recovered.participates_in_watermark)
        next_update = windows.report_progress(self.progress(1, 45, 45))
        self.assertEqual(next_update.watermark.event_time_ns, BASE + 45)
        self.assertEqual(
            [window.key.start_ns for window in next_update.sealed_windows], [BASE + 30]
        )
        # Catching the first open window is sufficient; no need to catch the
        # fractional watermark itself before accepting useful current evidence.
        rejoined = windows.offer(self.admit(self.ray(station=0, offset=42, sequence=4), 46))
        self.assertIs(rejoined.receipts[0].disposition, f.WindowDisposition.BUFFERED)
        current = next(
            item
            for item in rejoined.watermark.station_progress
            if item.station_id == self.configs[0].station_id
        )
        self.assertIs(current.health, f.StationStreamHealth.ACTIVE)
        self.assertTrue(current.participates_in_watermark)
        self.assertFalse(windows.report_progress(self.progress(1, 55, 55)).sealed_windows)
        final = windows.report_progress(self.progress(0, 55, 56))
        self.assertEqual([window.key.start_ns for window in final.sealed_windows], [BASE + 40])
        self.assertEqual(len(final.sealed_windows[0].rays), 1)
        self.assertEqual(tuple(sealed.sealed_windows), preserved)
        self.assertTrue(all(not window.rays for window in preserved))

    def test_lagging_recovery_does_not_rejoin_merely_by_becoming_idle(self) -> None:
        windows = self.windows(
            replace(
                self.window_policy,
                watermark_strategy=f.WatermarkStrategy.EVENT_CLOCK_DEADLINE,
                allowed_lateness_ns=0,
                idle_after_ns=10,
                offline_after_ns=30,
            )
        )
        windows.report_progress(self.progress(0, 0, 0))
        windows.report_progress(self.progress(1, 0, 0))
        windows.advance(self.time(35))
        late = windows.offer(self.admit(self.ray(station=0, offset=5, sequence=3), 36))
        self.assertIs(late.receipts[0].disposition, f.WindowDisposition.LATE_AFTER_SEAL)
        idle = windows.advance(self.time(50))
        station = next(
            item
            for item in idle.watermark.station_progress
            if item.station_id == self.configs[0].station_id
        )
        self.assertIs(station.health, f.StationStreamHealth.IDLE)
        self.assertFalse(station.participates_in_watermark)
        self.assertFalse(windows.policy.exclude_idle_stations)

    def test_deadlines_include_mapping_uncertainty_and_stop_at_finite_end(self) -> None:
        self.clock = replace(
            self.clock, mapping=replace(self.clock.mapping, mapping_uncertainty_ns=2)
        )
        windows = self.windows(
            replace(
                self.window_policy,
                watermark_strategy=f.WatermarkStrategy.EVENT_CLOCK_DEADLINE,
                allowed_lateness_ns=3,
            )
        )
        windows.report_progress(self.progress(0, 0, 0))
        self.assertFalse(windows.advance(self.time(14)).sealed_windows)
        update = windows.advance(self.time(15))
        self.assertEqual(len(update.sealed_windows), 1)
        self.assertIs(update.sealed_windows[0].seal_reason, f.WindowSealReason.EVENT_CLOCK_DEADLINE)
        self.assertIs(
            windows.offer(self.admit(self.ray(offset=5), 16)).receipts[0].disposition,
            f.WindowDisposition.LATE_AFTER_SEAL,
        )
        windows.report_progress(self.progress(0, 0, 17, ended=True))
        windows.report_progress(self.progress(1, None, 18, ended=True))
        self.assertFalse(windows.advance(self.time(10**6)).sealed_windows)

    def test_no_active_station_can_advance_only_through_explicit_mapping(self) -> None:
        windows = self.windows(
            replace(
                self.window_policy,
                exclude_idle_stations=True,
                exclude_offline_stations=True,
                idle_after_ns=5,
                offline_after_ns=10,
                no_active_station_policy=f.EmptyActiveStationPolicy.ADVANCE_WITH_MAPPED_EVENT_CLOCK,
            )
        )
        windows.report_progress(self.progress(0, 0, 0))
        windows.report_progress(self.progress(1, 0, 0))
        update = windows.advance(self.time(20))
        self.assertEqual(update.watermark.event_time_ns, BASE + 18)
        self.assertEqual(len(update.sealed_windows), 1)
        self.assertIs(update.sealed_windows[0].seal_reason, f.WindowSealReason.EVENT_CLOCK_DEADLINE)

    def test_session_restart_does_not_reactivate_an_ended_stream(self) -> None:
        windows = self.windows()
        windows.report_progress(self.progress(0, 5, 5, ended=True))
        windows.report_progress(self.progress(0, 25, 25, session=m.SessionId("restart")))
        windows.report_progress(self.progress(1, None, 26, ended=True))
        late = windows.offer(self.admit(self.ray(offset=35), 36))
        self.assertIs(late.receipts[0].disposition, f.WindowDisposition.LATE_AFTER_SEAL)
        old = next(
            item
            for item in late.watermark.station_progress
            if item.station_session_id == self.rays[0].envelope.station_session_id
        )
        self.assertIs(old.health, f.StationStreamHealth.ENDED)
        self.assertEqual(old.maximum_seen_exposure_midpoint_ns, BASE + 5)
        ended = windows.report_progress(
            self.progress(0, 25, 37, ended=True, session=m.SessionId("restart"))
        )
        self.assertEqual(ended.sealed_windows[-1].key.end_ns, BASE + 30)

    def test_finite_source_events_are_all_sealed_exactly_once(self) -> None:
        configs = default_station_configs()
        policy = default_fusion_policy()
        windows = self.windows(policy.windows)
        sealed: list[f.SealedObservationWindow] = []
        admitted: list[m.EventId] = []
        last_capture: dict[tuple[m.StationId, m.SessionId, m.ConfigDigest], m.CaptureTime] = {}
        steps, _ = generate_synthetic_steps(
            configs,
            (
                SyntheticTarget(
                    initial_position_enu_m=(-15.0, 62.0, 20.0), velocity_enu_mps=(1.0, 0.0, 0.0)
                ),
            ),
            duration_ns=100_000_000,
            session_suffix="finite",
        )
        for step in steps:
            if step.stream_ended:
                continue
            for solution in step.solutions:
                self.assertIs(
                    self.calibrations.register_solution(solution).status,
                    f.RegistryWriteStatus.REGISTERED,
                )
            for received in step.events:
                outcome = self.admission.admit(received)
                self.assertIs(outcome.record.decision, f.AdmissionDecision.ACCEPTED)
                if outcome.admitted_ray is not None:
                    admitted.append(outcome.record.event_id)
                    envelope = received.event.envelope
                    last_capture[
                        envelope.station_id,
                        envelope.station_session_id,
                        envelope.station_config_digest,
                    ] = outcome.admitted_ray.event.capture_time
                sealed.extend(windows.offer(outcome).sealed_windows)
        now = m.ProcessingTime(clock_domain_id=self.clock_id, time_ns=100_000_001)
        for (station, session, digest), capture in last_capture.items():
            sealed.extend(
                windows.report_progress(
                    f.StationEventTimeProgress(
                        station_id=station,
                        station_session_id=session,
                        station_config_digest=digest,
                        completed_capture_time=capture,
                        received_at=now,
                        stream_ended=True,
                    )
                ).sealed_windows
            )
        actual = [
            (
                ray.event.envelope.station_id,
                ray.event.envelope.station_session_id,
                ray.event.envelope.sequence_no,
            )
            for window in sealed
            for ray in window.rays
        ]
        self.assertTrue(admitted)
        self.assertCountEqual(actual, admitted)
        self.assertEqual(len(actual), len(set(actual)))
        self.assertEqual(len({window.key for window in sealed}), len(sealed))
        self.assertFalse(windows.advance(replace(now, time_ns=now.time_ns + 10**9)).sealed_windows)

    def test_progress_requires_registered_config_and_valid_time(self) -> None:
        windows = self.windows()
        progress = self.progress(0, 0, 0)
        for invalid in (
            replace(progress, station_config_digest=m.ConfigDigest("unknown")),
            replace(progress, completed_capture_time=None),
            replace(
                progress,
                completed_capture_time=replace(
                    self.ray().capture_time, clock_domain_id="wrong-clock"
                ),
            ),
        ):
            with self.subTest(progress=invalid), self.assertRaises(ValueError):
                windows.report_progress(invalid)
        with self.assertRaisesRegex(ValueError, "clock domain"):
            windows.advance(m.ProcessingTime(clock_domain_id="wrong-clock", time_ns=BASE))
        windows.advance(self.time(10))
        with self.assertRaisesRegex(ValueError, "backwards"):
            windows.advance(self.time(9))
        with self.assertRaisesRegex(ValueError, "durations"):
            self.windows(replace(self.window_policy, width_ns=0))

    def test_capture_time_timeout_ignores_wall_delays_and_detects_capture_lag(self) -> None:
        windows = self.windows(
            replace(
                self.window_policy,
                timeout_basis=f.StreamTimeoutBasis.CAPTURE_TIME,
                exclude_idle_stations=True,
                exclude_offline_stations=True,
                awaiting_first_event_timeout_ns=20,
                idle_after_ns=15,
                offline_after_ns=30,
                allowed_lateness_ns=0,
            )
        )
        windows.start_module("center/windows", self.time(0))
        # Large wall-clock jump before any capture arrives keeps both stations awaiting.
        before_first = windows.advance(self.time(10_000_000_000))
        self.assertTrue(
            all(
                item.health is f.StationStreamHealth.AWAITING_FIRST_EVENT
                for item in before_first.watermark.station_progress
            )
        )
        # Station 0 advances in capture time while station 1 has not emitted yet.
        windows.report_progress(self.progress(0, 5, 10_000_000_001))
        update = windows.report_progress(self.progress(0, 25, 10_000_000_002))
        missing = next(
            item
            for item in update.watermark.station_progress
            if item.station_id == self.configs[1].station_id
        )
        self.assertIs(missing.health, f.StationStreamHealth.OFFLINE)
        self.assertEqual(missing.reason_codes, ("first_event_timeout",))
        self.assertEqual([w.key.start_ns for w in update.sealed_windows], [BASE, BASE + 10])

        # Station 1 catches up; huge wall-clock pause does not mark synchronized stations idle.
        caught = windows.report_progress(self.progress(1, 25, 20_000_000_000))
        self.assertTrue(
            all(
                item.health is f.StationStreamHealth.ACTIVE
                for item in caught.watermark.station_progress
            )
        )
        stalled_wall = windows.advance(self.time(90_000_000_000))
        self.assertTrue(
            all(
                item.health is f.StationStreamHealth.ACTIVE
                for item in stalled_wall.watermark.station_progress
            )
        )
        # Station 0 advances 20 ns ahead of station 1 -> station 1 becomes IDLE.
        idle_update = windows.report_progress(self.progress(0, 45, 90_000_000_001))
        s1_idle = next(
            item
            for item in idle_update.watermark.station_progress
            if item.station_id == self.configs[1].station_id
        )
        self.assertIs(s1_idle.health, f.StationStreamHealth.IDLE)

    def test_disabled_timeout_never_marks_stations_idle_or_offline_in_lockstep(self) -> None:
        windows = self.windows(
            replace(
                self.window_policy,
                timeout_basis=f.StreamTimeoutBasis.DISABLED,
                exclude_idle_stations=True,
                exclude_offline_stations=True,
                awaiting_first_event_timeout_ns=1,
                idle_after_ns=1,
                offline_after_ns=2,
                allowed_lateness_ns=0,
            )
        )
        windows.start_module("center/windows", self.time(0))
        windows.report_progress(self.progress(0, 5, 1_000_000_000))
        # Even after huge wall-clock and capture-time gaps, station 1 stays AWAITING_FIRST_EVENT.
        waiting = windows.report_progress(self.progress(0, 500, 10_000_000_000))
        self.assertFalse(waiting.sealed_windows)
        s1 = next(
            item
            for item in waiting.watermark.station_progress
            if item.station_id == self.configs[1].station_id
        )
        self.assertIs(s1.health, f.StationStreamHealth.AWAITING_FIRST_EVENT)
        # Once station 1 sends progress, windows advance synchronously and seal cleanly on end.
        synced = windows.report_progress(self.progress(1, 15, 20_000_000_000))
        self.assertEqual([w.key.start_ns for w in synced.sealed_windows], [BASE])
        s1_active = next(
            item
            for item in synced.watermark.station_progress
            if item.station_id == self.configs[1].station_id
        )
        self.assertIs(s1_active.health, f.StationStreamHealth.ACTIVE)
        windows.report_progress(self.progress(0, 500, 30_000_000_000, ended=True))
        final = windows.report_progress(self.progress(1, 15, 30_000_000_001, ended=True))
        self.assertEqual(final.sealed_windows[-1].key.end_ns, BASE + 510)


if __name__ == "__main__":
    unittest.main()

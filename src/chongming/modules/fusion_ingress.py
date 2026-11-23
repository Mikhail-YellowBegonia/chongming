"""Center registries, event admission, and immutable exposure-time windows."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from chongming.contracts import fusion as f
from chongming.contracts import model as m
from chongming.contracts.runtime import ModuleHealth
from chongming.modules.base import ModuleShell
from chongming.ports import fusion as ports


def _midpoint(capture: m.CaptureTime) -> int:
    return (capture.exposure_start_ns + capture.exposure_end_ns) // 2


def _capture_reason(capture: m.CaptureTime, clock: str) -> f.AdmissionReason | None:
    if capture.clock_domain_id != clock:
        return f.AdmissionReason.CLOCK_DOMAIN_MISMATCH
    if capture.exposure_end_ns < capture.exposure_start_ns or capture.clock_uncertainty_ns < 0:
        return f.AdmissionReason.EXPOSURE_INTERVAL_INVALID
    return None


def _contains(interval: m.TimeInterval, time_ns: int) -> bool:
    return interval.start_ns <= time_ns and (interval.end_ns is None or time_ns < interval.end_ns)


def _valid_covariance(covariance: tuple[tuple[float, ...], ...], dimension: int) -> bool:
    matrix = np.asarray(covariance, dtype=np.float64)
    if matrix.shape != (dimension, dimension) or not np.isfinite(matrix).all():
        return False
    scale = float(np.max(np.abs(matrix)))
    normalized = matrix / scale if scale else matrix
    tolerance = 32 * dimension * np.finfo(np.float64).eps
    return bool(
        np.allclose(normalized, normalized.T, rtol=0, atol=tolerance)
        and np.linalg.eigvalsh((normalized + normalized.T) / 2).min() >= -tolerance
    )


def _valid_rotation(rotation: m.Rotation3x3) -> bool:
    matrix = np.asarray(rotation, dtype=np.float64)
    return bool(
        matrix.shape == (3, 3)
        and np.isfinite(matrix).all()
        and np.allclose(matrix.T @ matrix, np.eye(3), rtol=0, atol=1e-6)
        and abs(float(np.linalg.det(matrix)) - 1) <= 1e-6
    )


class StationRegistryModule(ModuleShell):
    business_implemented = True

    def __init__(self) -> None:
        self._configs: dict[tuple[m.StationId, m.ConfigDigest], m.StationConfig] = {}

    def register(self, config: m.StationConfig) -> f.RegistryWriteResult:
        key = (config.station_id, config.config_digest)
        previous = self._configs.get(key)
        if previous is not None:
            return f.RegistryWriteResult(
                status=f.RegistryWriteStatus.ALREADY_REGISTERED
                if previous == config
                else f.RegistryWriteStatus.CONFLICT,
                reason_codes=() if previous == config else ("configuration_digest_mismatch",),
            )
        installation = config.installation_uncertainty
        if (
            installation.state_ref.station_id != config.station_id
            or installation.state_ref.config_digest != config.config_digest
        ):
            return f.RegistryWriteResult(
                status=f.RegistryWriteStatus.REJECTED,
                reason_codes=("shared_error_reference_mismatch",),
            )
        if (
            not np.isfinite(config.main_camera_position_enu_m).all()
            or not _valid_rotation(config.baseline_orientation_enu_from_main)
            or not all(
                _valid_covariance(covariance, 3)
                for covariance in (
                    installation.position_covariance_enu_m2,
                    installation.orientation_covariance_enu_rad2,
                    installation.fixed_extrinsic_covariance_aux_rad2,
                )
            )
        ):
            return f.RegistryWriteResult(
                status=f.RegistryWriteStatus.REJECTED, reason_codes=("invalid_uncertainty",)
            )
        self._configs[key] = config
        return f.RegistryWriteResult(status=f.RegistryWriteStatus.REGISTERED, reason_codes=())

    def lookup(self, station_id: m.StationId, digest: m.ConfigDigest) -> m.StationConfig | None:
        return self._configs.get((station_id, digest))

    def registered_stations(self) -> tuple[m.StationId, ...]:
        return tuple(sorted({station for station, _ in self._configs}))


class CalibrationRegistryModule(ModuleShell):
    business_implemented = True

    def __init__(self, *, stations: ports.StationRegistry) -> None:
        self.stations = stations
        self._solutions: dict[m.CalibrationSolutionRef, m.CalibrationSolution] = {}
        self._states: dict[tuple[m.StationId, m.SessionId, int], m.CalibrationState] = {}

    def register_solution(self, solution: m.CalibrationSolution) -> f.RegistryWriteResult:
        previous = self._solutions.get(solution.solution_ref)
        if previous is not None:
            return f.RegistryWriteResult(
                status=f.RegistryWriteStatus.ALREADY_REGISTERED
                if previous == solution
                else f.RegistryWriteStatus.CONFLICT,
                reason_codes=() if previous == solution else ("calibration_evidence_mismatch",),
            )
        config = self.stations.lookup(
            solution.solution_ref.station_id, solution.station_config_digest
        )
        reason: str | None = None
        if config is None:
            reason = "configuration_unknown"
        elif config.world_frame_id != solution.world_frame_id:
            reason = "world_frame_mismatch"
        elif (
            solution.reference_capture_time.clock_domain_id
            != solution.valid_interval.clock_domain_id
        ):
            reason = "clock_domain_mismatch"
        elif _capture_reason(
            solution.reference_capture_time, solution.valid_interval.clock_domain_id
        ) or (
            solution.valid_interval.end_ns is not None
            and solution.valid_interval.end_ns <= solution.valid_interval.start_ns
        ):
            reason = "exposure_interval_invalid"
        elif (
            not np.isfinite(solution.main_camera_position_enu_m).all()
            or not _valid_rotation(solution.rotation_enu_from_main)
            or not _valid_covariance(solution.correction_covariance_enu_rad2, 3)
        ):
            reason = "invalid_uncertainty"
        if reason is not None:
            return f.RegistryWriteResult(
                status=f.RegistryWriteStatus.REJECTED, reason_codes=(reason,)
            )
        self._solutions[solution.solution_ref] = solution
        return f.RegistryWriteResult(status=f.RegistryWriteStatus.REGISTERED, reason_codes=())

    def lookup_solution(self, ref: m.CalibrationSolutionRef) -> m.CalibrationSolution | None:
        return self._solutions.get(ref)

    def record_state(self, event: m.CalibrationStateEvent) -> f.RegistryWriteResult:
        state, envelope = event.state, event.envelope
        key = (state.station_id, state.station_session_id, state.state_version)
        previous = self._states.get(key)
        if (
            envelope.event_type is not m.EventType.CALIBRATION_STATE
            or (envelope.station_id, envelope.station_session_id, envelope.station_config_digest)
            != (state.station_id, state.station_session_id, state.station_config_digest)
            or state.state_version <= 0
            or (event.solution is not None and event.solution.solution_ref != state.solution_ref)
            or (
                state.solution_ref is not None
                and (state.solution_ref.station_id, state.solution_ref.station_session_id)
                != key[:2]
            )
        ):
            return f.RegistryWriteResult(
                status=f.RegistryWriteStatus.REJECTED,
                reason_codes=("calibration_solution_scope_mismatch",),
            )
        if self.stations.lookup(state.station_id, state.station_config_digest) is None:
            return f.RegistryWriteResult(
                status=f.RegistryWriteStatus.REJECTED, reason_codes=("configuration_unknown",)
            )
        if state.health in {
            m.CalibrationHealth.VALID,
            m.CalibrationHealth.FROZEN,
            m.CalibrationHealth.STALE,
        } and (
            state.solution_ref is None
            or state.last_trusted_time is None
            or state.usable_interval is None
        ):
            return f.RegistryWriteResult(
                status=f.RegistryWriteStatus.REJECTED,
                reason_codes=("calibration_evidence_mismatch",),
            )
        if (
            state.usable_interval is not None
            and state.usable_interval.end_ns is not None
            and state.usable_interval.end_ns <= state.usable_interval.start_ns
        ):
            return f.RegistryWriteResult(
                status=f.RegistryWriteStatus.REJECTED, reason_codes=("exposure_interval_invalid",)
            )
        solution = event.solution
        if state.solution_ref is not None:
            solution = (
                solution if solution is not None else self.lookup_solution(state.solution_ref)
            )
            if solution is None:
                return f.RegistryWriteResult(
                    status=f.RegistryWriteStatus.REJECTED,
                    reason_codes=("calibration_solution_unknown",),
                )
            if solution.station_config_digest != state.station_config_digest:
                return f.RegistryWriteResult(
                    status=f.RegistryWriteStatus.REJECTED,
                    reason_codes=("calibration_configuration_mismatch",),
                )
            if (
                state.usable_interval is not None
                and state.usable_interval.clock_domain_id != solution.valid_interval.clock_domain_id
            ):
                return f.RegistryWriteResult(
                    status=f.RegistryWriteStatus.REJECTED, reason_codes=("clock_domain_mismatch",)
                )
        if previous is not None and previous != state:
            return f.RegistryWriteResult(
                status=f.RegistryWriteStatus.CONFLICT,
                reason_codes=("calibration_state_conflict",),
            )
        history = self.state_history(state.station_id, state.station_session_id)
        if history and history[0].observed_at.clock_domain_id != state.observed_at.clock_domain_id:
            return f.RegistryWriteResult(
                status=f.RegistryWriteStatus.REJECTED, reason_codes=("clock_domain_mismatch",)
            )
        if event.solution is not None:
            result = self.register_solution(event.solution)
            if result.status in {f.RegistryWriteStatus.CONFLICT, f.RegistryWriteStatus.REJECTED}:
                return result
        if previous is not None:
            return f.RegistryWriteResult(
                status=f.RegistryWriteStatus.ALREADY_REGISTERED, reason_codes=()
            )
        self._states[key] = state
        return f.RegistryWriteResult(status=f.RegistryWriteStatus.REGISTERED, reason_codes=())

    def state_history(
        self, station_id: m.StationId, station_session_id: m.SessionId
    ) -> tuple[m.CalibrationState, ...]:
        return tuple(
            sorted(
                (
                    state
                    for (station, session, _), state in self._states.items()
                    if (station, session) == (station_id, station_session_id)
                ),
                key=lambda state: (state.observed_at.time_ns, state.state_version),
            )
        )


class AdmissionJournalModule(ModuleShell):
    business_implemented = True

    def __init__(self) -> None:
        self.records: list[f.AdmissionRecord] = []
        self._first: dict[m.EventId, m.StationEvent] = {}
        self._pending: dict[m.EventId, f.AdmissionRecord] = {}

    def first_event(self, event_id: m.EventId) -> m.StationEvent | None:
        return self._first.get(event_id)

    def record(self, record: f.AdmissionRecord) -> None:
        first = self._first.setdefault(record.event_id, record.received.event)
        self.records.append(record)
        if record.received.event != first:
            return
        if record.decision is f.AdmissionDecision.QUARANTINED:
            self._pending[record.event_id] = record
        elif record.decision in {f.AdmissionDecision.ACCEPTED, f.AdmissionDecision.REJECTED}:
            self._pending.pop(record.event_id, None)

    def quarantined(self) -> tuple[f.AdmissionRecord, ...]:
        return tuple(self._pending.values())


@dataclass(frozen=True, kw_only=True)
class EventAdmissionModule(ModuleShell):
    business_implemented = True

    policy: f.AdmissionPolicy
    stations: ports.StationRegistry
    calibrations: ports.CalibrationRegistry
    journal: ports.AdmissionJournal
    clock: ports.EventClockMapping

    def __post_init__(self) -> None:
        if (
            not self.policy.accepted_schema_versions
            or not all(
                np.isfinite(value) and value >= 0
                for value in (
                    self.policy.direction_unit_tolerance,
                    self.policy.tangent_basis_orthogonality_tolerance,
                )
            )
            or {rule.usage for rule in self.policy.calibration_usage_rules}
            != set(m.CalibrationUsage)
            or len(self.policy.calibration_usage_rules) != len(m.CalibrationUsage)
            or any(
                not np.isfinite(rule.geometry_weight_scale) or rule.geometry_weight_scale < 0
                for rule in self.policy.calibration_usage_rules
            )
        ):
            raise ValueError("invalid admission policy")

    def admit(self, received: m.ReceivedEvent) -> f.AdmissionOutcome:
        envelope = received.event.envelope
        event_id = (envelope.station_id, envelope.station_session_id, envelope.sequence_no)
        previous = self.journal.first_event(event_id)
        if previous is not None:
            duplicate = previous == received.event
            return self._record(
                received,
                received.arrival_time,
                f.AdmissionDecision.DUPLICATE if duplicate else f.AdmissionDecision.REJECTED,
                f.AdmissionReason.EXACT_DUPLICATE_DELIVERY
                if duplicate
                else f.AdmissionReason.EVENT_ID_PAYLOAD_CONFLICT,
            )
        return self._evaluate(received, received.arrival_time)

    def reconsider_quarantined(self, now: m.ProcessingTime) -> tuple[f.AdmissionOutcome, ...]:
        pending = sorted(
            self.journal.quarantined(),
            key=lambda record: isinstance(record.received.event, m.RayEvent),
        )
        return tuple(self._evaluate(record.received, now) for record in pending)

    def _record(
        self,
        received: m.ReceivedEvent,
        now: m.ProcessingTime,
        decision: f.AdmissionDecision,
        reason: f.AdmissionReason | None = None,
        config: m.StationConfig | None = None,
        ray: f.AdmittedRay | None = None,
    ) -> f.AdmissionOutcome:
        event, envelope = received.event, received.event.envelope
        ref = (
            event.calibration_solution_ref
            if isinstance(event, m.RayEvent)
            else event.state.solution_ref
            if isinstance(event, m.CalibrationStateEvent)
            else event.related_calibration_ref
        )
        record = f.AdmissionRecord(
            event_id=(envelope.station_id, envelope.station_session_id, envelope.sequence_no),
            received=received,
            decision=decision,
            reasons=() if reason is None else (reason,),
            checked_at=now,
            station_config_ref=None
            if config is None
            else f.StationConfigRef(
                station_id=config.station_id,
                config_digest=config.config_digest,
                world_frame_id=config.world_frame_id,
            ),
            calibration_solution_ref=ref,
        )
        self.journal.record(record)
        return f.AdmissionOutcome(record=record, admitted_ray=ray)

    def _evaluate(self, received: m.ReceivedEvent, now: m.ProcessingTime) -> f.AdmissionOutcome:
        event, envelope = received.event, received.event.envelope
        try:
            arrival_clock = self.clock.map_processing_time(received.arrival_time)
        except ValueError:
            return self._record(
                received,
                now,
                f.AdmissionDecision.REJECTED,
                f.AdmissionReason.CLOCK_DOMAIN_MISMATCH,
            )
        if arrival_clock.clock_domain_id != self.policy.capture_clock_domain_id:
            return self._record(
                received,
                now,
                f.AdmissionDecision.REJECTED,
                f.AdmissionReason.CLOCK_DOMAIN_MISMATCH,
            )
        reason: f.AdmissionReason | None = None
        expected_type = (
            m.EventType.RAY
            if isinstance(event, m.RayEvent)
            else m.EventType.CALIBRATION_STATE
            if isinstance(event, m.CalibrationStateEvent)
            else m.EventType.DIAGNOSTIC
        )
        if envelope.schema_version not in self.policy.accepted_schema_versions:
            reason = f.AdmissionReason.SCHEMA_UNSUPPORTED
        elif envelope.event_type is not expected_type:
            reason = f.AdmissionReason.EVENT_TYPE_MISMATCH
        elif type(envelope.sequence_no) is not int or envelope.sequence_no <= 0:
            reason = f.AdmissionReason.INVALID_SEQUENCE
        if reason is not None:
            return self._record(received, now, f.AdmissionDecision.REJECTED, reason)
        config = self.stations.lookup(envelope.station_id, envelope.station_config_digest)
        if config is None:
            return self._record(
                received,
                now,
                f.AdmissionDecision.QUARANTINED,
                f.AdmissionReason.CONFIGURATION_UNKNOWN,
            )
        if config.world_frame_id != self.policy.world_frame_id:
            return self._record(
                received,
                now,
                f.AdmissionDecision.REJECTED,
                f.AdmissionReason.WORLD_FRAME_MISMATCH,
                config,
            )
        if isinstance(event, m.CalibrationStateEvent):
            if (
                event.state.usable_interval is not None
                and event.state.usable_interval.clock_domain_id
                != self.policy.capture_clock_domain_id
            ) or (
                event.solution is not None
                and event.solution.reference_capture_time.clock_domain_id
                != self.policy.capture_clock_domain_id
            ):
                return self._record(
                    received,
                    now,
                    f.AdmissionDecision.REJECTED,
                    f.AdmissionReason.CLOCK_DOMAIN_MISMATCH,
                    config,
                )
            result = self.calibrations.record_state(event)
            if result.status in {
                f.RegistryWriteStatus.REGISTERED,
                f.RegistryWriteStatus.ALREADY_REGISTERED,
            }:
                return self._record(received, now, f.AdmissionDecision.ACCEPTED, config=config)
            reason = f.AdmissionReason(result.reason_codes[0])
            decision = (
                f.AdmissionDecision.QUARANTINED
                if reason is f.AdmissionReason.CALIBRATION_SOLUTION_UNKNOWN
                else f.AdmissionDecision.REJECTED
            )
            return self._record(received, now, decision, reason, config)
        if isinstance(event, m.DiagnosticEvent):
            if event.capture_time is not None:
                reason = _capture_reason(event.capture_time, self.policy.capture_clock_domain_id)
            if reason is None and event.related_config_ref not in (None, config.config_digest):
                reason = f.AdmissionReason.CONFIGURATION_DIGEST_MISMATCH
            if (
                reason is None
                and event.related_frame_ref is not None
                and event.related_frame_ref.station_id != envelope.station_id
            ):
                reason = f.AdmissionReason.CALIBRATION_SOLUTION_SCOPE_MISMATCH
            ref = event.related_calibration_ref
            if (
                reason is None
                and ref is not None
                and (
                    ref.station_id != envelope.station_id
                    or ref.station_session_id != envelope.station_session_id
                )
            ):
                reason = f.AdmissionReason.CALIBRATION_SOLUTION_SCOPE_MISMATCH
            return self._record(
                received,
                now,
                f.AdmissionDecision.ACCEPTED if reason is None else f.AdmissionDecision.REJECTED,
                reason,
                config,
            )
        reason = self._ray_reason(event, config)
        if reason is not None:
            return self._record(received, now, f.AdmissionDecision.REJECTED, reason, config)
        solution = self.calibrations.lookup_solution(event.calibration_solution_ref)
        if solution is None:
            return self._record(
                received,
                now,
                f.AdmissionDecision.QUARANTINED,
                f.AdmissionReason.CALIBRATION_SOLUTION_UNKNOWN,
                config,
            )
        reason = self._calibration_reason(event, solution)
        if reason is not None:
            return self._record(received, now, f.AdmissionDecision.REJECTED, reason, config)
        action = next(
            rule.action
            for rule in self.policy.calibration_usage_rules
            if rule.usage is event.calibration_usage
        )
        if action is not f.CalibrationAction.ACCEPT:
            return self._record(
                received,
                now,
                f.AdmissionDecision.QUARANTINED
                if action is f.CalibrationAction.QUARANTINE
                else f.AdmissionDecision.REJECTED,
                f.AdmissionReason.CALIBRATION_USAGE_QUARANTINED
                if action is f.CalibrationAction.QUARANTINE
                else f.AdmissionReason.CALIBRATION_USAGE_REJECTED,
                config,
            )
        return self._record(
            received,
            now,
            f.AdmissionDecision.ACCEPTED,
            config=config,
            ray=f.AdmittedRay(
                event=event,
                arrival_time=received.arrival_time,
                config=config,
                calibration_solution=solution,
            ),
        )

    def _ray_reason(self, ray: m.RayEvent, config: m.StationConfig) -> f.AdmissionReason | None:
        reason = _capture_reason(ray.capture_time, self.policy.capture_clock_domain_id)
        if reason is not None:
            return reason
        if ray.world_frame_id != self.policy.world_frame_id:
            return f.AdmissionReason.WORLD_FRAME_MISMATCH
        if (
            ray.calibration_solution_ref.station_id,
            ray.calibration_solution_ref.station_session_id,
        ) != (ray.envelope.station_id, ray.envelope.station_session_id):
            return f.AdmissionReason.CALIBRATION_SOLUTION_SCOPE_MISMATCH
        direction, origin = np.asarray(ray.ray_direction_enu_unit), np.asarray(ray.ray_origin_enu_m)
        if (
            direction.shape != (3,)
            or origin.shape != (3,)
            or not np.isfinite([direction, origin]).all()
        ):
            return f.AdmissionReason.NONFINITE_RAY
        if abs(float(np.linalg.norm(direction)) - 1) > self.policy.direction_unit_tolerance:
            return f.AdmissionReason.NONUNIT_DIRECTION
        uncertainty = ray.uncertainty
        u, v = (
            np.asarray(uncertainty.tangent_u_enu_unit),
            np.asarray(uncertainty.tangent_v_enu_unit),
        )
        tolerance = self.policy.tangent_basis_orthogonality_tolerance
        if (
            u.shape != (3,)
            or v.shape != (3,)
            or not np.isfinite([u, v]).all()
            or abs(float(np.linalg.norm(u)) - 1) > tolerance
            or abs(float(np.linalg.norm(v)) - 1) > tolerance
            or abs(float(np.dot(u, v))) > tolerance
            or not np.allclose(np.cross(u, v), direction, rtol=0, atol=tolerance)
        ):
            return f.AdmissionReason.INVALID_TANGENT_BASIS
        if (
            uncertainty.model != "tangent_gaussian_v1"
            or not _valid_covariance(uncertainty.independent_angular_covariance_rad2, 2)
            or not _valid_covariance(uncertainty.independent_origin_covariance_m2, 3)
        ):
            return f.AdmissionReason.INVALID_UNCERTAINTY
        if (
            uncertainty.shared_installation_state_ref != config.installation_uncertainty.state_ref
            or uncertainty.shared_calibration_solution_ref != ray.calibration_solution_ref
        ):
            return f.AdmissionReason.SHARED_ERROR_REFERENCE_MISMATCH
        return None

    def _calibration_reason(
        self, ray: m.RayEvent, solution: m.CalibrationSolution
    ) -> f.AdmissionReason | None:
        if solution.station_config_digest != ray.envelope.station_config_digest:
            return f.AdmissionReason.CALIBRATION_CONFIGURATION_MISMATCH
        if solution.world_frame_id != ray.world_frame_id:
            return f.AdmissionReason.WORLD_FRAME_MISMATCH
        evidence = ray.calibration_evidence
        if (
            evidence.solution_accepted_time != solution.accepted_time
            or evidence.solution_reference_capture_time != solution.reference_capture_time
            or evidence.solution_valid_interval != solution.valid_interval
            or evidence.state_version <= 0
        ):
            return f.AdmissionReason.CALIBRATION_EVIDENCE_MISMATCH
        clock = self.policy.capture_clock_domain_id
        if (
            solution.valid_interval.clock_domain_id != clock
            or evidence.usable_interval.clock_domain_id != clock
        ):
            return f.AdmissionReason.CLOCK_DOMAIN_MISMATCH
        midpoint = _midpoint(ray.capture_time)
        for time in (solution.accepted_time, evidence.last_trusted_time):
            if time.clock_domain_id == clock:
                upper_time = time.time_ns
            else:
                try:
                    reading = self.clock.map_processing_time(time)
                except ValueError:
                    return f.AdmissionReason.CLOCK_DOMAIN_MISMATCH
                if reading.clock_domain_id != clock:
                    return f.AdmissionReason.CLOCK_DOMAIN_MISMATCH
                upper_time = reading.event_time_ns + reading.mapping_uncertainty_ns
            if upper_time > midpoint:
                return f.AdmissionReason.CALIBRATION_EXPOSURE_OUTSIDE_VALIDITY
        if (
            _midpoint(solution.reference_capture_time) > midpoint
            or not _contains(evidence.usable_interval, midpoint)
            or (
                ray.calibration_usage is m.CalibrationUsage.VALID
                and not _contains(solution.valid_interval, midpoint)
            )
        ):
            return f.AdmissionReason.CALIBRATION_EXPOSURE_OUTSIDE_VALIDITY
        return None


@dataclass(frozen=True, kw_only=True)
class EventClockMappingModule(ModuleShell):
    business_implemented = True

    mapping: f.EventClockMappingSpec

    def __post_init__(self) -> None:
        if (
            self.mapping.scale_numerator <= 0
            or self.mapping.scale_denominator <= 0
            or self.mapping.mapping_uncertainty_ns < 0
        ):
            raise ValueError("clock mapping requires positive scale and nonnegative uncertainty")

    def map_processing_time(self, now: m.ProcessingTime) -> f.EventClockReading:
        if now.clock_domain_id != self.mapping.processing_clock_domain_id:
            raise ValueError("processing clock domain mismatch")
        return f.EventClockReading(
            clock_domain_id=self.mapping.capture_clock_domain_id,
            event_time_ns=(
                now.time_ns * self.mapping.scale_numerator // self.mapping.scale_denominator
                + self.mapping.offset_ns
            ),
            mapping_uncertainty_ns=self.mapping.mapping_uncertainty_ns,
        )


@dataclass
class _Stream:
    maximum_exposure_ns: int | None = None
    last_received_at: m.ProcessingTime | None = None
    ended: bool = False


class EventWindowModule(ModuleShell):
    business_implemented = True

    def __init__(
        self,
        *,
        center_session_id: m.SessionId,
        policy: f.EventWindowPolicy,
        clock: ports.EventClockMapping,
        stations: ports.StationRegistry,
    ) -> None:
        if (
            policy.width_ns <= 0
            or min(
                policy.allowed_lateness_ns,
                policy.awaiting_first_event_timeout_ns,
                policy.idle_after_ns,
            )
            < 0
            or policy.offline_after_ns < policy.idle_after_ns
        ):
            raise ValueError("invalid event window durations")
        self.center_session_id, self.policy, self.clock, self.stations = (
            center_session_id,
            policy,
            clock,
            stations,
        )
        self._streams: dict[tuple[m.StationId, m.SessionId | None], _Stream] = {}
        self._rays: dict[int, list[f.AdmittedRay]] = {}
        self._seen: set[m.EventId] = set()
        self._next_start_ns = policy.origin_ns
        self._sealed = False
        self._watermark_ns: int | None = None
        self._started_at_ns: int | None = None
        self._last_time_ns: int | None = None
        self._first_capture_ns: int | None = None

    def start_module(self, module_key: str, now: m.ProcessingTime) -> ModuleHealth:
        self._check_time(now)
        return super().start_module(module_key, now)

    def offer(self, outcome: f.AdmissionOutcome) -> f.WindowUpdate:
        record, ray = outcome.record, outcome.admitted_ray
        now = record.checked_at
        self._check_time(now)
        window: f.WindowKey | None = None
        reasons: tuple[str, ...] = ()
        if record.decision is f.AdmissionDecision.DUPLICATE or record.event_id in self._seen:
            disposition = f.WindowDisposition.ALREADY_CONSUMED
        elif ray is None:
            disposition = f.WindowDisposition.NOT_A_FUSION_OBSERVATION
        else:
            capture = ray.event.capture_time
            window = self._key(_midpoint(capture))
            stream = self._stream(
                ray.event.envelope.station_id, ray.event.envelope.station_session_id
            )
            if stream.ended:
                disposition = f.WindowDisposition.LATE_AFTER_SEAL
                reasons = ("stream_ended",)
            else:
                self._remember(stream, capture, ray.arrival_time)
                if self._sealed and window.start_ns < self._next_start_ns:
                    disposition = f.WindowDisposition.LATE_AFTER_SEAL
                else:
                    if not self._sealed:
                        self._next_start_ns = min(self._next_start_ns, window.start_ns)
                    self._rays.setdefault(window.start_ns, []).append(ray)
                    disposition = f.WindowDisposition.BUFFERED
            self._seen.add(record.event_id)
        receipt = f.WindowReceipt(
            event_id=record.event_id,
            station_id=record.received.event.envelope.station_id,
            disposition=disposition,
            window=window,
            reason_codes=reasons,
        )
        return self._update(now, receipt)

    def report_progress(self, progress: f.StationEventTimeProgress) -> f.WindowUpdate:
        now, capture = progress.received_at, progress.completed_capture_time
        self._check_time(now)
        if self.stations.lookup(progress.station_id, progress.station_config_digest) is None:
            raise ValueError("progress references an unknown station configuration")
        if capture is None and not progress.stream_ended:
            raise ValueError("ordinary progress requires a completed exposure")
        if (
            capture is not None
            and _capture_reason(capture, self.policy.capture_clock_domain_id) is not None
        ):
            raise ValueError("invalid progress capture time")
        stream = self._stream(progress.station_id, progress.station_session_id)
        if stream.ended:
            if not progress.stream_ended or (
                capture is not None
                and (
                    stream.maximum_exposure_ns is None
                    or _midpoint(capture) > stream.maximum_exposure_ns
                )
            ):
                raise ValueError("progress cannot extend an ended stream")
        else:
            if capture is not None:
                self._remember(stream, capture, now)
                if not self._sealed:
                    self._next_start_ns = min(
                        self._next_start_ns, self._key(_midpoint(capture)).start_ns
                    )
            else:
                stream.last_received_at = now
            stream.ended = progress.stream_ended
        receipt = f.WindowReceipt(
            event_id=None,
            station_id=progress.station_id,
            disposition=f.WindowDisposition.PROGRESS_RECORDED,
            window=None if capture is None else self._key(_midpoint(capture)),
            reason_codes=(),
        )
        return self._update(now, receipt)

    def advance(self, now: m.ProcessingTime) -> f.WindowUpdate:
        self._check_time(now)
        return self._update(now)

    def _check_time(self, now: m.ProcessingTime) -> None:
        if now.clock_domain_id != self.policy.processing_clock_domain_id:
            raise ValueError("processing clock domain mismatch")
        if self._last_time_ns is not None and now.time_ns < self._last_time_ns:
            raise ValueError("processing time must not move backwards")
        if self._started_at_ns is None:
            self._started_at_ns = now.time_ns
        self._last_time_ns = now.time_ns

    def _key(self, time_ns: int) -> f.WindowKey:
        start = (
            self.policy.origin_ns
            + ((time_ns - self.policy.origin_ns) // self.policy.width_ns) * self.policy.width_ns
        )
        return f.WindowKey(
            center_session_id=self.center_session_id,
            capture_clock_domain_id=self.policy.capture_clock_domain_id,
            start_ns=start,
            end_ns=start + self.policy.width_ns,
        )

    def _stream(self, station: m.StationId, session: m.SessionId) -> _Stream:
        self._streams.pop((station, None), None)
        return self._streams.setdefault((station, session), _Stream())

    def _remember(
        self, stream: _Stream, capture: m.CaptureTime, received_at: m.ProcessingTime
    ) -> None:
        midpoint = _midpoint(capture)
        if self._first_capture_ns is None or midpoint < self._first_capture_ns:
            self._first_capture_ns = midpoint
        if stream.maximum_exposure_ns is None or midpoint > stream.maximum_exposure_ns:
            stream.maximum_exposure_ns = midpoint
        if stream.last_received_at is None or received_at.time_ns > stream.last_received_at.time_ns:
            stream.last_received_at = received_at

    def _stream_health(
        self,
        stream: _Stream,
        now: m.ProcessingTime,
        global_max_capture_ns: int | None,
    ) -> tuple[f.StationStreamHealth, tuple[str, ...]]:
        if stream.ended:
            return f.StationStreamHealth.ENDED, ()
        basis = self.policy.timeout_basis
        if basis is f.StreamTimeoutBasis.DISABLED:
            if stream.last_received_at is None:
                return f.StationStreamHealth.AWAITING_FIRST_EVENT, ()
            return f.StationStreamHealth.ACTIVE, ()
        if basis is f.StreamTimeoutBasis.CAPTURE_TIME:
            if stream.maximum_exposure_ns is None:
                if (
                    global_max_capture_ns is not None
                    and self._first_capture_ns is not None
                    and global_max_capture_ns - self._first_capture_ns
                    >= self.policy.awaiting_first_event_timeout_ns
                ):
                    return f.StationStreamHealth.OFFLINE, ("first_event_timeout",)
                return f.StationStreamHealth.AWAITING_FIRST_EVENT, ()
            assert global_max_capture_ns is not None
            lag = global_max_capture_ns - stream.maximum_exposure_ns
            if lag >= self.policy.offline_after_ns:
                return f.StationStreamHealth.OFFLINE, ()
            if lag >= self.policy.idle_after_ns:
                return f.StationStreamHealth.IDLE, ()
            return f.StationStreamHealth.ACTIVE, ()
        assert self._started_at_ns is not None
        if stream.last_received_at is None:
            if now.time_ns - self._started_at_ns >= self.policy.awaiting_first_event_timeout_ns:
                return f.StationStreamHealth.OFFLINE, ("first_event_timeout",)
            return f.StationStreamHealth.AWAITING_FIRST_EVENT, ()
        elapsed = now.time_ns - stream.last_received_at.time_ns
        if elapsed >= self.policy.offline_after_ns:
            return f.StationStreamHealth.OFFLINE, ()
        if elapsed >= self.policy.idle_after_ns:
            return f.StationStreamHealth.IDLE, ()
        return f.StationStreamHealth.ACTIVE, ()

    def _progress(self, now: m.ProcessingTime) -> tuple[f.StationProgress, ...]:
        for station in self.stations.registered_stations():
            if not any(key[0] == station for key in self._streams):
                self._streams[station, None] = _Stream()
        result = []
        global_max_capture_ns = max(
            (
                stream.maximum_exposure_ns
                for stream in self._streams.values()
                if stream.maximum_exposure_ns is not None
            ),
            default=None,
        )
        for (station, session), stream in sorted(
            self._streams.items(), key=lambda item: (item[0][0], item[0][1] or "")
        ):
            health, reasons = self._stream_health(stream, now, global_max_capture_ns)
            behind_seal = (
                self._sealed
                and stream.maximum_exposure_ns is not None
                and stream.maximum_exposure_ns < self._next_start_ns
            )
            if health is f.StationStreamHealth.ACTIVE and behind_seal:
                health = f.StationStreamHealth.RECOVERING
                reasons = ("awaiting_open_window_catchup",)
            participates = not (
                behind_seal
                or health in (f.StationStreamHealth.ENDED, f.StationStreamHealth.RECOVERING)
                or (health is f.StationStreamHealth.IDLE and self.policy.exclude_idle_stations)
                or (
                    health is f.StationStreamHealth.OFFLINE and self.policy.exclude_offline_stations
                )
            )
            result.append(
                f.StationProgress(
                    station_id=station,
                    station_session_id=session,
                    maximum_seen_exposure_midpoint_ns=stream.maximum_exposure_ns,
                    last_received_at=stream.last_received_at,
                    health=health,
                    participates_in_watermark=participates,
                    reason_codes=reasons,
                )
            )
        return tuple(result)

    def _update(
        self, now: m.ProcessingTime, receipt: f.WindowReceipt | None = None
    ) -> f.WindowUpdate:
        progress = self._progress(now)
        participants = [item for item in progress if item.participates_in_watermark]
        maximum = max(
            (
                item.maximum_seen_exposure_midpoint_ns
                for item in progress
                if item.maximum_seen_exposure_midpoint_ns is not None
            ),
            default=None,
        )
        all_ended = bool(progress) and all(
            item.health is f.StationStreamHealth.ENDED for item in progress
        )
        watermark: int | None = None
        reason = f.WindowSealReason.WATERMARK_PASSED_END
        if all_ended:
            watermark = None if maximum is None else self._key(maximum).end_ns
            reason = f.WindowSealReason.STREAMS_ENDED
        elif self.policy.watermark_strategy is f.WatermarkStrategy.EVENT_CLOCK_DEADLINE or (
            not participants
            and self.policy.no_active_station_policy
            is f.EmptyActiveStationPolicy.ADVANCE_WITH_MAPPED_EVENT_CLOCK
        ):
            reading = self.clock.map_processing_time(now)
            if reading.clock_domain_id != self.policy.capture_clock_domain_id:
                raise ValueError("mapped capture clock domain mismatch")
            watermark = (
                reading.event_time_ns
                - reading.mapping_uncertainty_ns
                - self.policy.allowed_lateness_ns
            )
            reason = f.WindowSealReason.EVENT_CLOCK_DEADLINE
        elif participants and all(
            item.maximum_seen_exposure_midpoint_ns is not None for item in participants
        ):
            watermark = (
                min(
                    item.maximum_seen_exposure_midpoint_ns
                    for item in participants
                    if item.maximum_seen_exposure_midpoint_ns is not None
                )
                - self.policy.allowed_lateness_ns
            )
        if watermark is not None:
            self._watermark_ns = (
                watermark if self._watermark_ns is None else max(self._watermark_ns, watermark)
            )
        snapshot = f.WatermarkSnapshot(
            capture_clock_domain_id=self.policy.capture_clock_domain_id,
            event_time_ns=self._watermark_ns,
            evaluated_at=now,
            station_progress=progress,
        )
        # Once streams end, mapped wall time must not extend the finite run horizon.
        horizon = watermark if all_ended else self._watermark_ns
        sealed = []
        if maximum is not None and horizon is not None:
            while self._next_start_ns + self.policy.width_ns <= horizon:
                key = self._key(self._next_start_ns)
                rays = tuple(
                    sorted(
                        self._rays.pop(key.start_ns, []),
                        key=lambda ray: (
                            ray.event.envelope.station_id,
                            ray.event.envelope.station_session_id,
                            ray.event.envelope.sequence_no,
                        ),
                    )
                )
                span = (
                    (
                        max(ray.event.capture_time.exposure_end_ns for ray in rays)
                        - min(ray.event.capture_time.exposure_start_ns for ray in rays)
                    )
                    if rays
                    else 0
                )
                sealed.append(
                    f.SealedObservationWindow(
                        key=key,
                        reference_time_ns=(key.start_ns + key.end_ns) // 2,
                        support_exposure_span_ns=span,
                        rays=rays,
                        watermark_at_seal=snapshot,
                        sealed_at=now,
                        seal_reason=reason,
                    )
                )
                self._next_start_ns = key.end_ns
                self._sealed = True
        return f.WindowUpdate(
            receipts=() if receipt is None else (receipt,),
            sealed_windows=tuple(sealed),
            watermark=snapshot,
        )

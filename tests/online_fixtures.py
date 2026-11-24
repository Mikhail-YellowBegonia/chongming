"""Deterministic online synthetic ray and registration fixtures (no legacy scene modules)."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, replace

import numpy as np

from chongming.composition.center import reference_policy
from chongming.contracts import fusion as f
from chongming.contracts import model as m
from chongming.contracts.development import CenterInputStep, CenterRegistration
from tests.test_center_process import _station_config_and_aux

CLOCK_DOMAIN_ID = "sim-clock"
WORLD_FRAME_ID = m.WorldFrameId("demo-enu")
DEFAULT_SEARCH = m.VoxelSearchSpec(
    world_frame_id=WORLD_FRAME_ID,
    min_enu_m=(-100.0, -100.0, 0.0),
    max_enu_m=(100.0, 150.0, 100.0),
    step_enu_m=(10.0, 10.0, 10.0),
    forward_range_m=None,
)


@dataclass(frozen=True, kw_only=True)
class SyntheticTarget:
    initial_position_enu_m: m.Vec3M
    velocity_enu_mps: m.Vec3


def default_station_configs(*, include_upper: bool = False) -> tuple[m.StationConfig, ...]:
    configs = [
        _station_config_and_aux("west", (-20.0, -40.0, 4.0))[0],
        _station_config_and_aux("east", (20.0, -40.0, 4.0))[0],
    ]
    if include_upper:
        configs.append(_station_config_and_aux("upper", (0.0, 0.0, 40.0))[0])
    return tuple(replace(cfg, world_frame_id=WORLD_FRAME_ID) for cfg in configs)


def default_clock_mapping(clock_domain_id: str = CLOCK_DOMAIN_ID) -> f.EventClockMappingSpec:
    return f.EventClockMappingSpec(
        processing_clock_domain_id=clock_domain_id,
        capture_clock_domain_id=clock_domain_id,
        scale_numerator=1,
        scale_denominator=1,
        offset_ns=0,
        mapping_uncertainty_ns=0,
    )


def default_fusion_policy(
    *,
    search: m.VoxelSearchSpec = DEFAULT_SEARCH,
    clock_domain_id: str = CLOCK_DOMAIN_ID,
    window_width_ns: int = 10**9 // 30,
) -> f.FusionPolicy:
    return reference_policy(
        search=search,
        clock_domain_id=clock_domain_id,
        window_width_ns=window_width_ns,
    )


def default_registration(
    *,
    stations: tuple[m.StationConfig, ...] | None = None,
    policy: f.FusionPolicy | None = None,
    center_session_id: str = "center-ref-test",
    clock_domain_id: str = CLOCK_DOMAIN_ID,
) -> CenterRegistration:
    station_tuple = default_station_configs() if stations is None else stations
    resolved_policy = (
        default_fusion_policy(clock_domain_id=clock_domain_id) if policy is None else policy
    )
    return CenterRegistration(
        stations=station_tuple,
        center_session_id=center_session_id,
        policy=resolved_policy,
        clock_mapping=default_clock_mapping(clock_domain_id),
        started_at=m.ProcessingTime(clock_domain_id=clock_domain_id, time_ns=0),
    )


def _tangent_basis(direction: tuple[float, float, float]) -> tuple[m.Vec3, m.Vec3]:
    d = np.asarray(direction, dtype=np.float64)
    d = d / float(np.linalg.norm(d))
    ref = np.asarray((0.0, 0.0, 1.0) if abs(d[2]) < 0.9 else (1.0, 0.0, 0.0), dtype=np.float64)
    u = np.cross(ref, d)
    u = u / float(np.linalg.norm(u))
    v = np.cross(d, u)
    v = v / float(np.linalg.norm(v))
    return (float(u[0]), float(u[1]), float(u[2])), (float(v[0]), float(v[1]), float(v[2]))


def _capture(time_ns: int, clock_id: str) -> m.CaptureTime:
    return m.CaptureTime(
        clock_domain_id=clock_id,
        exposure_start_ns=time_ns,
        exposure_end_ns=time_ns,
        clock_uncertainty_ns=0,
        rolling_shutter_model="none",
    )


def generate_synthetic_steps(
    configs: tuple[m.StationConfig, ...],
    targets: tuple[SyntheticTarget, ...],
    *,
    duration_ns: int = 120_000_000,
    start_ns: int = 1_000_000,
    station_start_ns: int = 0,
    clock_domain_id: str = CLOCK_DOMAIN_ID,
    station_periods_ns: dict[str, tuple[int, int]] | None = None,
    skip_step_indices: Iterable[int] = (),
    angular_covariance_rad2: m.Matrix2x2 = ((0.0, 0.0), (0.0, 0.0)),
    origin_covariance_m2: m.Matrix3x3 = ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
    calibration_phases_ns: tuple[tuple[int, m.CalibrationHealth, m.CalibrationUsage], ...]
    | None = None,
    jitter_rad: float = 0.0,
    session_suffix: str = "",
) -> tuple[list[CenterInputStep], tuple[m.CalibrationSolution, ...]]:
    """Generate sorted online CenterInputSteps + initial CalibrationSolutions."""
    default_period = 10**9 // 30
    skipped = set(skip_step_indices)
    phases = (
        calibration_phases_ns
        if calibration_phases_ns is not None
        else ((0, m.CalibrationHealth.VALID, m.CalibrationUsage.VALID),)
    )
    eye3_cov: m.Matrix3x3 = ((1e-10, 0.0, 0.0), (0.0, 1e-10, 0.0), (0.0, 0.0, 1e-10))

    def _session_id(sid: m.StationId) -> m.SessionId:
        return m.SessionId(f"sess-{sid}-{session_suffix}" if session_suffix else f"sess-{sid}")

    solutions: dict[m.StationId, m.CalibrationSolution] = {}
    station_samples: list[tuple[int, int, m.StationConfig]] = []
    for cfg in configs:
        sid = cfg.station_id
        session_id = _session_id(sid)
        cap0 = _capture(0, clock_domain_id)
        t0 = m.ProcessingTime(clock_domain_id=clock_domain_id, time_ns=0)
        solutions[sid] = m.CalibrationSolution(
            solution_ref=m.CalibrationSolutionRef(
                station_id=sid,
                station_session_id=session_id,
                solution_id=m.CalibrationSolutionId(
                    f"sol-{sid}-{session_suffix}-0" if session_suffix else f"sol-{sid}-0"
                ),
            ),
            station_config_digest=cfg.config_digest,
            world_frame_id=cfg.world_frame_id,
            reference_capture_time=cap0,
            accepted_time=t0,
            valid_interval=m.TimeInterval(
                clock_domain_id=clock_domain_id, start_ns=0, end_ns=10**13
            ),
            main_camera_position_enu_m=cfg.main_camera_position_enu_m,
            rotation_enu_from_main=cfg.baseline_orientation_enu_from_main,
            correction_covariance_enu_rad2=eye3_cov,
            quality=m.CalibrationQuality(
                source="synthetic",
                matched_reference_count=8,
                reprojection_rmse_px=0.1,
                angular_residual_rad=1e-5,
                reason_codes=("synthetic_init",),
            ),
        )
        first_s, period_s = (
            station_periods_ns[str(sid)]
            if station_periods_ns and str(sid) in station_periods_ns
            else (start_ns, default_period)
        )
        idx = 0
        while True:
            t_ns = first_s + idx * period_s
            if t_ns > duration_ns:
                break
            if t_ns >= station_start_ns:
                station_samples.append((t_ns, idx, cfg))
            idx += 1

    by_time: dict[int, list[tuple[int, m.StationConfig]]] = {}
    for t_ns, idx, cfg in station_samples:
        by_time.setdefault(t_ns, []).append((idx, cfg))

    ordered_times = sorted(by_time)
    seq_by_station: dict[m.StationId, int] = {cfg.station_id: 0 for cfg in configs}
    score_def = m.ScoreDefinition(
        definition_id="synthetic-fg-v1",
        kind=m.ScoreKind.RANKING,
        minimum=0.0,
        maximum=1.0,
        cross_station_comparison_group=None,
    )
    inst_ref_map = {cfg.station_id: cfg.installation_uncertainty.state_ref for cfg in configs}

    steps: list[CenterInputStep] = []
    last_cap_by_station: dict[m.StationId, m.CaptureTime] = {}
    sent_solutions = False
    for step_no, t_ns in enumerate(ordered_times):
        now = m.ProcessingTime(clock_domain_id=clock_domain_id, time_ns=t_ns)
        cap = _capture(t_ns, clock_domain_id)
        health = phases[0][1]
        usage = phases[0][2]
        for start_p, h_p, u_p in phases:
            if t_ns >= start_p:
                health, usage = h_p, u_p

        events: list[m.ReceivedEvent] = []
        progress_list: list[f.StationEventTimeProgress] = []
        for idx, cfg in sorted(by_time[t_ns], key=lambda item: str(item[1].station_id)):
            sid = cfg.station_id
            last_cap_by_station[sid] = cap
            session_id = _session_id(sid)
            sol = solutions[sid]
            seq_by_station[sid] += 1
            cal_event = m.CalibrationStateEvent(
                envelope=m.EventEnvelope(
                    schema_version="event-v1",
                    station_id=sid,
                    station_session_id=session_id,
                    station_config_digest=cfg.config_digest,
                    sequence_no=seq_by_station[sid],
                    event_type=m.EventType.CALIBRATION_STATE,
                ),
                state=m.CalibrationState(
                    station_id=sid,
                    station_session_id=session_id,
                    station_config_digest=cfg.config_digest,
                    state_version=step_no + 1,
                    health=health,
                    solution_ref=sol.solution_ref,
                    observed_at=now,
                    last_trusted_time=now,
                    usable_interval=m.TimeInterval(
                        clock_domain_id=clock_domain_id, start_ns=0, end_ns=10**13
                    ),
                    reason_codes=(f"phase_{health.value}",),
                ),
                solution=sol if not sent_solutions else None,
            )
            events.append(m.ReceivedEvent(event=cal_event, arrival_time=now))

            if idx not in skipped:
                t_s = t_ns / 1e9
                origin = np.asarray(cfg.main_camera_position_enu_m, dtype=np.float64)
                for tgt_idx, target in enumerate(targets):
                    pos = np.asarray(
                        target.initial_position_enu_m, dtype=np.float64
                    ) + t_s * np.asarray(target.velocity_enu_mps, dtype=np.float64)
                    vec = pos - origin
                    dist = float(np.linalg.norm(vec))
                    direction = vec / dist
                    u, v = _tangent_basis(
                        (float(direction[0]), float(direction[1]), float(direction[2]))
                    )
                    if jitter_rad != 0.0:
                        angle = jitter_rad * math.sin(step_no * 1.7 + tgt_idx)
                        perturbed = direction + angle * np.asarray(u, dtype=np.float64)
                        direction = perturbed / float(np.linalg.norm(perturbed))
                        u, v = _tangent_basis(
                            (float(direction[0]), float(direction[1]), float(direction[2]))
                        )
                    seq_by_station[sid] += 1
                    ray_event = m.RayEvent(
                        envelope=m.EventEnvelope(
                            schema_version="event-v1",
                            station_id=sid,
                            station_session_id=session_id,
                            station_config_digest=cfg.config_digest,
                            sequence_no=seq_by_station[sid],
                            event_type=m.EventType.RAY,
                        ),
                        frame_id=m.FrameId(f"{sid}:main:{idx}"),
                        local_track_id=m.LocalTrackId(str(tgt_idx + 1)),
                        capture_time=cap,
                        world_frame_id=cfg.world_frame_id,
                        calibration_solution_ref=sol.solution_ref,
                        calibration_usage=usage,
                        calibration_evidence=m.CalibrationUseEvidence(
                            state_version=step_no + 1,
                            last_trusted_time=now,
                            solution_accepted_time=sol.accepted_time,
                            solution_reference_capture_time=sol.reference_capture_time,
                            solution_valid_interval=sol.valid_interval,
                            usable_interval=m.TimeInterval(
                                clock_domain_id=clock_domain_id, start_ns=0, end_ns=10**13
                            ),
                            reason_codes=(f"use_{usage.value}",),
                        ),
                        ray_origin_enu_m=cfg.main_camera_position_enu_m,
                        ray_direction_enu_unit=(
                            float(direction[0]),
                            float(direction[1]),
                            float(direction[2]),
                        ),
                        uncertainty=m.RayUncertainty(
                            model="tangent_gaussian_v1",
                            tangent_u_enu_unit=u,
                            tangent_v_enu_unit=v,
                            independent_angular_covariance_rad2=angular_covariance_rad2,
                            independent_origin_covariance_m2=origin_covariance_m2,
                            shared_installation_state_ref=inst_ref_map[sid],
                            shared_calibration_solution_ref=sol.solution_ref,
                        ),
                        detection_score=0.95,
                        detection_score_definition=score_def,
                        track_quality=0.95,
                        track_quality_definition=score_def,
                        quality_flags=(),
                    )
                    events.append(m.ReceivedEvent(event=ray_event, arrival_time=now))

            progress_list.append(
                f.StationEventTimeProgress(
                    station_id=sid,
                    station_session_id=session_id,
                    station_config_digest=cfg.config_digest,
                    completed_capture_time=cap,
                    received_at=now,
                    stream_ended=False,
                )
            )

        ordered_solutions = tuple(
            solutions[cfg.station_id] for cfg in sorted(configs, key=lambda c: str(c.station_id))
        )
        steps.append(
            CenterInputStep(
                step_no=step_no,
                time=now,
                solutions=ordered_solutions if not sent_solutions else (),
                events=tuple(events),
                progress=tuple(progress_list),
                stream_ended=False,
            )
        )
        sent_solutions = True

    if steps:
        end_ns = max(duration_ns + 1, steps[-1].time.time_ns + 1)
        end_now = m.ProcessingTime(clock_domain_id=clock_domain_id, time_ns=end_ns)
        end_progress = tuple(
            f.StationEventTimeProgress(
                station_id=cfg.station_id,
                station_session_id=_session_id(cfg.station_id),
                station_config_digest=cfg.config_digest,
                completed_capture_time=last_cap_by_station.get(cfg.station_id),
                received_at=end_now,
                stream_ended=True,
            )
            for cfg in configs
        )
        steps.append(
            CenterInputStep(
                step_no=len(steps),
                time=end_now,
                solutions=(),
                events=(),
                progress=end_progress,
                stream_ended=True,
            )
        )

    ordered_all_solutions = tuple(
        solutions[cfg.station_id] for cfg in sorted(configs, key=lambda c: str(c.station_id))
    )
    return steps, ordered_all_solutions

"""Online-only station device policy validation and dependency assembly."""

from __future__ import annotations

import math
from dataclasses import dataclass

from chongming.contracts.device import DevicePolicy, FrameDifferencePolicy, KalmanTrackingPolicy
from chongming.contracts.model import SessionId, StationConfig, StationId
from chongming.contracts.runtime import ModuleNode
from chongming.modules.detection import validate_detection_policy, validate_tracking_policy
from chongming.modules.device import (
    OPENCV_CALIBRATION_ESTIMATOR_ID,
    WARMUP_HOLD_NS,
    AuxiliaryCalibrationModule,
    CalibrationEstimationModule,
    CalibrationHistoryModule,
    CalibrationStateModule,
    DeviceModule,
    EventSequencerModule,
    MainDetectionModule,
    MotionDetectionModule,
    RayGenerationModule,
    ReferenceRecognitionModule,
    Tracking2DModule,
)
from chongming.modules.workbench_device import (
    WorkbenchMotionModule,
    WorkbenchTrackingModule,
    image_modules,
)
from chongming.ports.lifecycle import ModuleBinding, ModuleLifecycle
from chongming.uncertainty import covariance_factor


def validate_device_policy(policy: DevicePolicy) -> None:
    """Validate one explicit device policy configuration."""
    validate_detection_policy(policy.detection)
    validate_tracking_policy(policy.tracking)
    if isinstance(policy.detection, FrameDifferencePolicy) != isinstance(
        policy.tracking, KalmanTrackingPolicy
    ):
        raise ValueError("detector and tracker algorithm families must agree")
    calibration = policy.calibration
    if (
        calibration.estimator_id != OPENCV_CALIBRATION_ESTIMATOR_ID
        or type(calibration.update_period_ns) is not int
        or calibration.update_period_ns < 0
        or type(calibration.stale_after_ns) is not int
        or calibration.stale_after_ns < 0
        or type(calibration.recovery_success_count) is not int
        or calibration.recovery_success_count <= 0
        or type(calibration.minimum_reference_count) is not int
        or calibration.minimum_reference_count < 4
        or not math.isfinite(calibration.maximum_reprojection_rmse_px)
        or calibration.maximum_reprojection_rmse_px <= 0
        or (
            calibration.maximum_rotation_only_parallax_px is not None
            and (
                not math.isfinite(calibration.maximum_rotation_only_parallax_px)
                or calibration.maximum_rotation_only_parallax_px < 0
            )
        )
    ):
        raise ValueError("invalid explicitly configured device calibration policy")
    rays = policy.rays
    if rays.uncertainty_model_id != "tangent_gaussian_v1":
        raise ValueError("unsupported device ray uncertainty model")
    has_residual = False
    for covariance, label in (
        (rays.fallback_centroid_covariance_px2, "fallback_centroid_covariance_px2"),
        (rays.unmodelled_high_frequency_jitter_covariance_rad2, "residual_jitter_rad2"),
        (rays.unmodelled_relative_flex_covariance_rad2, "residual_flex_rad2"),
    ):
        if covariance is not None:
            factor = covariance_factor(covariance, 2, label)
            has_residual |= label != "fallback_centroid_covariance_px2" and bool(factor.any())
    if has_residual and (
        not isinstance(rays.residual_independence_basis, str)
        or not rays.residual_independence_basis.strip()
    ):
        raise ValueError("nonzero residuals require an explicit independence basis")


def validate_device_policies(
    policies: dict[StationId, DevicePolicy], configs: tuple[StationConfig, ...]
) -> None:
    """Validate that a policy map covers every configured station ID."""
    if set(policies) != {config.station_id for config in configs}:
        raise ValueError("device policy station IDs must match scenario station IDs exactly")
    for policy in policies.values():
        validate_device_policy(policy)


@dataclass(frozen=True, kw_only=True)
class StationAssembly:
    config: StationConfig
    sequencer: EventSequencerModule
    motion: MotionDetectionModule | WorkbenchMotionModule
    tracking: Tracking2DModule | WorkbenchTrackingModule
    detection: MainDetectionModule
    references: ReferenceRecognitionModule
    estimation: CalibrationEstimationModule
    history: CalibrationHistoryModule
    calibration_states: CalibrationStateModule
    calibration: AuxiliaryCalibrationModule
    rays: RayGenerationModule
    device: DeviceModule


def declare_station(
    *,
    config: StationConfig,
    policy: DevicePolicy,
    image_session_id: SessionId,
    schema_version: str = "event-v1",
    warmup_hold_ns: int = WARMUP_HOLD_NS,
) -> tuple[StationAssembly, tuple[ModuleBinding, ...]]:
    """Construct one station's device pipeline and lifecycle bindings without starting it."""
    sid = config.station_id
    prefix = f"station/{sid}"
    bindings: list[ModuleBinding] = []

    def bind(
        key: str,
        module: ModuleLifecycle,
        deps: tuple[str, ...] = (),
    ) -> None:
        bindings.append(
            ModuleBinding(
                node=ModuleNode(module_key=key, dependencies=deps, station_id=sid),
                module=module,
            )
        )

    sequencer = EventSequencerModule(
        station_id=sid,
        station_session_id=image_session_id,
        schema_version=schema_version,
    )
    motion, tracking = image_modules(policy.detection, policy.tracking, sid, image_session_id)
    detection = MainDetectionModule(detector=motion, tracker=tracking)
    references = ReferenceRecognitionModule(policy=policy.calibration)
    estimation = CalibrationEstimationModule(policy=policy.calibration)
    history = CalibrationHistoryModule(
        station_id=sid,
        station_session_id=image_session_id,
        station_config_digest=config.config_digest,
    )
    states = CalibrationStateModule(
        policy=policy.calibration,
        history=history,
        sequencer=sequencer,
    )
    calibration = AuxiliaryCalibrationModule(
        recognizer=references,
        estimator=estimation,
        states=states,
        update_period_ns=policy.calibration.update_period_ns,
    )
    rays = RayGenerationModule(policy=policy.rays, sequencer=sequencer)
    device = DeviceModule(
        config=config,
        station_session_id=image_session_id,
        detector=detection,
        calibrator=calibration,
        history=history,
        ray_builder=rays,
        sequencer=sequencer,
        warmup_hold_ns=warmup_hold_ns,
    )
    assembly = StationAssembly(
        config=config,
        sequencer=sequencer,
        motion=motion,
        tracking=tracking,
        detection=detection,
        references=references,
        estimation=estimation,
        history=history,
        calibration_states=states,
        calibration=calibration,
        rays=rays,
        device=device,
    )
    bind(f"{prefix}/sequencer", sequencer)
    bind(f"{prefix}/motion", motion)
    bind(f"{prefix}/tracking", tracking)
    bind(f"{prefix}/detection", detection, (f"{prefix}/motion", f"{prefix}/tracking"))
    bind(f"{prefix}/references", references)
    bind(f"{prefix}/estimation", estimation)
    bind(f"{prefix}/history", history)
    bind(f"{prefix}/calibration-states", states, (f"{prefix}/history", f"{prefix}/sequencer"))
    bind(
        f"{prefix}/calibration",
        calibration,
        (f"{prefix}/references", f"{prefix}/estimation", f"{prefix}/calibration-states"),
    )
    bind(f"{prefix}/rays", rays, (f"{prefix}/sequencer",))
    bind(
        f"{prefix}/device",
        device,
        (
            f"{prefix}/detection",
            f"{prefix}/calibration",
            f"{prefix}/history",
            f"{prefix}/rays",
            f"{prefix}/sequencer",
        ),
    )
    return assembly, tuple(bindings)

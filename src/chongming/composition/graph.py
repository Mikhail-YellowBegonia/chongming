"""Concrete dependency declaration. Construction performs no business operation.

The factory binds every shell, including both observation/candidate routes. All
bindings enter one startup group; selecting center input never removes modules.
"""

from __future__ import annotations

from dataclasses import dataclass

from chongming.composition.center import CenterAssembly, declare_center
from chongming.contracts.model import StationConfig, StationId
from chongming.contracts.runtime import ModuleNode, RunSpec, StartupPlan
from chongming.contracts.scene import ScenarioSpec
from chongming.modules.device import (
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
from chongming.modules.runtime import (
    EventTransportModule,
    EvidenceWriterModule,
    SystemRunnerModule,
)
from chongming.modules.scene import (
    CameraImagingModule,
    CameraProjectionModule,
    EvaluationModule,
    ExperimentClockModule,
    MonitoringModule,
    ScenarioModule,
    SceneLogicModule,
    TrajectoryModule,
    VirtualObservationModule,
    VisibilityModule,
)
from chongming.ports.lifecycle import ModuleBinding, ModuleLifecycle


@dataclass(frozen=True, kw_only=True)
class StationAssembly:
    config: StationConfig
    sequencer: EventSequencerModule
    motion: MotionDetectionModule
    tracking: Tracking2DModule
    detection: MainDetectionModule
    references: ReferenceRecognitionModule
    estimation: CalibrationEstimationModule
    history: CalibrationHistoryModule
    calibration_states: CalibrationStateModule
    calibration: AuxiliaryCalibrationModule
    rays: RayGenerationModule
    device: DeviceModule


@dataclass(frozen=True, kw_only=True)
class SceneAssembly:
    clock: ExperimentClockModule
    trajectories: TrajectoryModule
    projection: CameraProjectionModule
    visibility: VisibilityModule
    logic: SceneLogicModule
    imaging: CameraImagingModule
    virtual_sequencers: tuple[EventSequencerModule, ...]
    virtual_observations: VirtualObservationModule
    scenario: ScenarioModule


@dataclass(frozen=True, kw_only=True)
class ArchitectureAssembly:
    scene: SceneAssembly
    stations: tuple[StationAssembly, ...]
    center: CenterAssembly
    transport: EventTransportModule
    evaluator: EvaluationModule
    monitor: MonitoringModule
    evidence: EvidenceWriterModule
    startup: StartupPlan
    bindings: tuple[ModuleBinding, ...]
    runner: SystemRunnerModule


def declare_assembly(spec: ScenarioSpec, run: RunSpec) -> ArchitectureAssembly:
    """Bind shells and inspectable dependencies; do not initialize or run them.

    Stage 3 must validate unique station IDs, matching scenario/run station sets,
    fresh distinct sessions, common coordinate/clock metadata and consistent search
    specifications BEFORE calling this factory. This is not a config loader.
    """

    bindings: list[ModuleBinding] = []

    def bind(
        key: str,
        module: ModuleLifecycle,
        deps: tuple[str, ...] = (),
        station_id: StationId | None = None,
    ) -> None:
        bindings.append(
            ModuleBinding(
                node=ModuleNode(module_key=key, dependencies=deps, station_id=station_id),
                module=module,
            )
        )

    station_configs = {config.station_id: config for config in spec.station_configs}
    station_assemblies: list[StationAssembly] = []
    virtual_sequencers: list[EventSequencerModule] = []
    for station in run.stations:
        sid = station.sessions.station_id
        config = station_configs[sid]
        policy = station.device_policy
        prefix = f"station/{sid}"
        sequencer = EventSequencerModule(
            station_id=sid,
            station_session_id=station.sessions.image_session_id,
            schema_version=run.schema_version,
        )
        virtual_seq = EventSequencerModule(
            station_id=sid,
            station_session_id=station.sessions.virtual_session_id,
            schema_version=run.schema_version,
        )
        motion = MotionDetectionModule(policy=policy.detection)
        tracking = Tracking2DModule(
            station_id=sid,
            station_session_id=station.sessions.image_session_id,
            policy=policy.tracking,
        )
        detection = MainDetectionModule(detector=motion, tracker=tracking)
        references = ReferenceRecognitionModule(policy=policy.calibration)
        estimation = CalibrationEstimationModule(policy=policy.calibration)
        history = CalibrationHistoryModule(
            station_id=sid,
            station_session_id=station.sessions.image_session_id,
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
            station_session_id=station.sessions.image_session_id,
            detector=detection,
            calibrator=calibration,
            history=history,
            ray_builder=rays,
            sequencer=sequencer,
        )
        station_assemblies.append(
            StationAssembly(
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
        )
        virtual_sequencers.append(virtual_seq)
        bind(f"{prefix}/sequencer", sequencer, station_id=sid)
        bind(f"{prefix}/virtual-sequencer", virtual_seq, station_id=sid)
        bind(f"{prefix}/motion", motion, station_id=sid)
        bind(f"{prefix}/tracking", tracking, station_id=sid)
        bind(f"{prefix}/detection", detection, (f"{prefix}/motion", f"{prefix}/tracking"), sid)
        bind(f"{prefix}/references", references, station_id=sid)
        bind(f"{prefix}/estimation", estimation, station_id=sid)
        bind(f"{prefix}/history", history, station_id=sid)
        bind(
            f"{prefix}/calibration-states",
            states,
            (f"{prefix}/history", f"{prefix}/sequencer"),
            sid,
        )
        bind(
            f"{prefix}/calibration",
            calibration,
            (f"{prefix}/references", f"{prefix}/estimation", f"{prefix}/calibration-states"),
            sid,
        )
        bind(f"{prefix}/rays", rays, (f"{prefix}/sequencer",), sid)
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
            sid,
        )

    clock = ExperimentClockModule()
    trajectories = TrajectoryModule()
    projection = CameraProjectionModule()
    visibility = VisibilityModule()
    logic = SceneLogicModule(trajectories, projection, visibility)
    imaging = CameraImagingModule()
    virtual = VirtualObservationModule(sequencers=tuple(virtual_sequencers))
    scenario = ScenarioModule(
        clock,
        logic,
        imaging,
        virtual,
        station_sessions=tuple(station.sessions for station in run.stations),
    )
    scene = SceneAssembly(
        clock=clock,
        trajectories=trajectories,
        projection=projection,
        visibility=visibility,
        logic=logic,
        imaging=imaging,
        virtual_sequencers=tuple(virtual_sequencers),
        virtual_observations=virtual,
        scenario=scenario,
    )
    bind("scene/clock", clock)
    bind("scene/trajectories", trajectories)
    bind("scene/projection", projection)
    bind("scene/visibility", visibility)
    bind("scene/logic", logic, ("scene/trajectories", "scene/projection", "scene/visibility"))
    bind("scene/imaging", imaging)
    bind(
        "scene/virtual",
        virtual,
        tuple(
            f"station/{station.sessions.station_id}/virtual-sequencer" for station in run.stations
        ),
    )
    bind("scene/source", scenario, ("scene/clock", "scene/logic", "scene/imaging", "scene/virtual"))

    center, center_bindings = declare_center(
        center_session_id=run.center_session_id,
        policy=run.fusion_policy,
        clock_mapping=run.processing_to_capture_clock,
    )
    bindings.extend(center_bindings)

    transport = EventTransportModule(
        policy=run.transport_policy,
        perturbations=spec.perturbations.communication,
    )
    evaluator = EvaluationModule(run.evaluation)
    monitor = MonitoringModule(run.monitoring)
    evidence = EvidenceWriterModule(directory=run.artifacts_directory)
    bind("runtime/transport", transport)
    bind("runtime/evaluation", evaluator)
    bind("runtime/monitor", monitor)
    bind("runtime/evidence", evidence)
    startup = StartupPlan(modules=tuple(binding.node for binding in bindings))
    runner = SystemRunnerModule(
        run_spec=run,
        startup=startup,
        bindings=tuple(bindings),
        scenario=scenario,
        devices=tuple(station.device for station in station_assemblies),
        fusion=center.fusion,
        transport=transport,
        evaluator=evaluator,
        monitor=monitor,
        evidence=evidence,
    )
    return ArchitectureAssembly(
        scene=scene,
        stations=tuple(station_assemblies),
        center=center,
        transport=transport,
        evaluator=evaluator,
        monitor=monitor,
        evidence=evidence,
        startup=startup,
        bindings=tuple(bindings),
        runner=runner,
    )

"""Concrete dependency declaration. Construction performs no business operation.

The factory binds every shell, including both observation/candidate routes. All
bindings enter one startup group; selecting center input never removes modules.
"""

from __future__ import annotations

from dataclasses import dataclass

from chongming.composition.center import CenterAssembly, declare_center
from chongming.composition.station import StationAssembly, declare_station
from chongming.contracts.model import StationId
from chongming.contracts.runtime import ModuleNode, RunSpec, StartupPlan
from chongming.contracts.scene import ScenarioSpec
from chongming.modules.device import WARMUP_HOLD_NS, EventSequencerModule
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

__all__ = [
    "ArchitectureAssembly",
    "SceneAssembly",
    "StationAssembly",
    "declare_assembly",
    "declare_station",
]


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


def declare_assembly(
    spec: ScenarioSpec, run: RunSpec, *, warmup_hold_ns: int = WARMUP_HOLD_NS
) -> ArchitectureAssembly:
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
        station_assembly, station_bindings = declare_station(
            config=config,
            policy=station.device_policy,
            image_session_id=station.sessions.image_session_id,
            schema_version=run.schema_version,
            warmup_hold_ns=warmup_hold_ns,
        )
        virtual_seq = EventSequencerModule(
            station_id=sid,
            station_session_id=station.sessions.virtual_session_id,
            schema_version=run.schema_version,
        )
        station_assemblies.append(station_assembly)
        virtual_sequencers.append(virtual_seq)
        bindings.extend(station_bindings)
        bind(f"station/{sid}/virtual-sequencer", virtual_seq, station_id=sid)

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

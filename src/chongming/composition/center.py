"""Online-only center policies and dependency assembly, shared by plugins and runs."""

from dataclasses import dataclass

from chongming.contracts import fusion as f
from chongming.contracts import model as m
from chongming.contracts.runtime import ModuleNode
from chongming.modules.fusion import CalibrationWeightingModule, DirectGeometryCandidateModule
from chongming.modules.fusion_association import CrossStationAssociationModule
from chongming.modules.fusion_engine import CenterFusionModule
from chongming.modules.fusion_geometry import (
    CandidateGenerationModule,
    GeometryRefinementModule,
    VoxelCandidateModule,
)
from chongming.modules.fusion_ingress import (
    AdmissionJournalModule,
    CalibrationRegistryModule,
    EventAdmissionModule,
    EventClockMappingModule,
    EventWindowModule,
    StationRegistryModule,
)
from chongming.modules.fusion_tracking import CenterTrackingModule
from chongming.ports.lifecycle import ModuleBinding, ModuleLifecycle
from chongming.voxel_diagnostics import VoxelDiagnostics


@dataclass(frozen=True, kw_only=True)
class CenterAssembly:
    stations: StationRegistryModule
    calibrations: CalibrationRegistryModule
    journal: AdmissionJournalModule
    admission: EventAdmissionModule
    clock_mapping: EventClockMappingModule
    windows: EventWindowModule
    voxels: VoxelCandidateModule
    direct_candidates: DirectGeometryCandidateModule
    candidates: CandidateGenerationModule
    association: CrossStationAssociationModule
    weights: CalibrationWeightingModule
    geometry: GeometryRefinementModule
    tracking: CenterTrackingModule
    fusion: CenterFusionModule


def reference_policy(
    *, search: m.VoxelSearchSpec, clock_domain_id: str, window_width_ns: int
) -> f.FusionPolicy:
    """Supply experiment thresholds from online configuration, not target truth."""
    return f.FusionPolicy(
        admission=f.AdmissionPolicy(
            accepted_schema_versions=("event-v1",),
            world_frame_id=search.world_frame_id,
            capture_clock_domain_id=clock_domain_id,
            direction_unit_tolerance=1e-6,
            tangent_basis_orthogonality_tolerance=1e-6,
            calibration_usage_rules=tuple(
                f.CalibrationUsageRule(
                    usage=usage,
                    action=f.CalibrationAction.ACCEPT,
                    geometry_weight_scale={
                        m.CalibrationUsage.VALID: 1.0,
                        m.CalibrationUsage.FROZEN: 0.25,
                        m.CalibrationUsage.STALE: 0.1,
                    }[usage],
                )
                for usage in m.CalibrationUsage
            ),
        ),
        windows=f.EventWindowPolicy(
            capture_clock_domain_id=clock_domain_id,
            processing_clock_domain_id=clock_domain_id,
            origin_ns=0,
            width_ns=window_width_ns,
            allowed_lateness_ns=0,
            awaiting_first_event_timeout_ns=10**9,
            idle_after_ns=10**9,
            offline_after_ns=3 * 10**9,
            exclude_idle_stations=True,
            exclude_offline_stations=True,
            watermark_strategy=f.WatermarkStrategy.ACTIVE_STATION_MINIMUM,
            no_active_station_policy=f.EmptyActiveStationPolicy.ADVANCE_WITH_MAPPED_EVENT_CLOCK,
        ),
        candidates=f.CandidatePolicy(
            search=search,
            primary_method=f.CandidateMethod.COARSE_VOXEL,
            minimum_independent_stations=2,
            minimum_candidate_score=2,
            maximum_candidates=None,
            uncertainty_sigma=3.0,
            maximum_uncertainty_voxels_per_ray=100000,
        ),
        association=f.AssociationPolicy(
            minimum_independent_stations=2,
            temporal_gate_ns=window_width_ns,
            spatial_gate_m=10,
            ambiguity_score_margin=0.1,
            prediction_spatial_gate_m=2,
            maximum_hypotheses=4096,
            maximum_search_nodes=20000,
        ),
        geometry=f.GeometryPolicy(
            minimum_independent_stations=2,
            minimum_intersection_angle_rad=0.01,
            maximum_condition_number=1e6,
            maximum_residual_m=1,
            robust_loss_definition="least_squares",
            robust_loss_scale_sigma=1.5,
            maximum_solver_iterations=50,
            convergence_tolerance_m=1e-8,
            motion_alignment_model=f.MotionAlignmentModel.TRACK_PREDICTION,
            conservative_speed_bound_mps=None,
            maximum_unaligned_span_ns=1_000_000,
        ),
        tracking=f.TrackingPolicy(
            confirmation_measurement_count=2,
            confirmation_max_gap_ns=10**9,
            coast_after_ns=10**9,
            delete_after_ns=3 * 10**9,
            association_gate_mahalanobis_squared=9,
            initialization_spatial_gate_m=10,
            association_ambiguity_margin=1e-6,
            maximum_assignment_search_nodes=20000,
            process_acceleration_covariance_enu_m2ps4=(
                (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0),
                (0.0, 0.0, 1.0),
            ),
        ),
    )


def declare_center(
    *,
    center_session_id: m.SessionId,
    policy: f.FusionPolicy,
    clock_mapping: f.EventClockMappingSpec,
    voxel_diagnostics: VoxelDiagnostics | None = None,
) -> tuple[CenterAssembly, tuple[ModuleBinding, ...]]:
    """Construct every center stage; never read a scenario or start business work."""
    if (
        policy.admission.world_frame_id != policy.candidates.search.world_frame_id
        or policy.admission.capture_clock_domain_id != policy.windows.capture_clock_domain_id
        or policy.windows.capture_clock_domain_id != clock_mapping.capture_clock_domain_id
        or policy.windows.processing_clock_domain_id != clock_mapping.processing_clock_domain_id
    ):
        raise ValueError("inconsistent center policy coordinate/clock domains")
    stations = StationRegistryModule()
    calibrations = CalibrationRegistryModule(stations=stations)
    journal = AdmissionJournalModule()
    clock = EventClockMappingModule(mapping=clock_mapping)
    admission = EventAdmissionModule(
        policy=policy.admission,
        stations=stations,
        calibrations=calibrations,
        journal=journal,
        clock=clock,
    )
    windows = EventWindowModule(
        center_session_id=center_session_id, policy=policy.windows, clock=clock, stations=stations
    )
    weights = CalibrationWeightingModule(rules=policy.admission.calibration_usage_rules)
    voxels = VoxelCandidateModule(
        policy=policy.candidates,
        motion_policy=policy.geometry,
        weights=weights,
        diagnostics=voxel_diagnostics,
    )
    direct = DirectGeometryCandidateModule(search=policy.candidates.search)
    candidates = CandidateGenerationModule(policy=policy.candidates, voxel=voxels, direct=direct)
    association = CrossStationAssociationModule(
        policy=policy.association, motion_policy=policy.geometry, weights=weights
    )
    geometry = GeometryRefinementModule(
        center_session_id=center_session_id,
        world_frame_id=policy.admission.world_frame_id,
        policy=policy.geometry,
        weights=weights,
    )
    tracking = CenterTrackingModule(
        center_session_id=center_session_id,
        clock_domain_id=policy.windows.capture_clock_domain_id,
        world_frame_id=policy.admission.world_frame_id,
        policy=policy.tracking,
    )
    fusion = CenterFusionModule(
        center_session_id=center_session_id,
        policy=policy,
        stations=stations,
        calibrations=calibrations,
        admission=admission,
        windows=windows,
        candidates=candidates,
        association=association,
        geometry=geometry,
        tracker=tracking,
        clock=clock,
    )
    assembly = CenterAssembly(
        stations=stations,
        calibrations=calibrations,
        journal=journal,
        admission=admission,
        clock_mapping=clock,
        windows=windows,
        voxels=voxels,
        direct_candidates=direct,
        candidates=candidates,
        association=association,
        weights=weights,
        geometry=geometry,
        tracking=tracking,
        fusion=fusion,
    )
    bindings: list[ModuleBinding] = []

    def bind(key: str, module: ModuleLifecycle, deps: tuple[str, ...] = ()) -> None:
        bindings.append(
            ModuleBinding(
                node=ModuleNode(module_key=key, dependencies=deps, station_id=None), module=module
            )
        )

    bind("center/stations", stations)
    bind("center/calibrations", calibrations, ("center/stations",))
    bind("center/journal", journal)
    bind("center/clock-mapping", clock)
    bind(
        "center/admission",
        admission,
        ("center/stations", "center/calibrations", "center/journal", "center/clock-mapping"),
    )
    bind("center/windows", windows, ("center/clock-mapping", "center/stations"))
    bind("center/weights", weights)
    bind("center/voxels", voxels, ("center/weights",))
    bind("center/direct-candidates", direct)
    bind("center/candidates", candidates, ("center/voxels", "center/direct-candidates"))
    bind("center/association", association, ("center/weights",))
    bind("center/geometry", geometry, ("center/weights",))
    bind("center/tracking", tracking)
    bind(
        "center/fusion",
        fusion,
        (
            "center/stations",
            "center/calibrations",
            "center/admission",
            "center/windows",
            "center/candidates",
            "center/association",
            "center/geometry",
            "center/tracking",
            "center/clock-mapping",
        ),
    )
    return assembly, tuple(bindings)

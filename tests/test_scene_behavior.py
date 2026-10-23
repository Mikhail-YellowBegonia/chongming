from __future__ import annotations

import unittest
from dataclasses import replace

from chongming.contracts.model import (
    CameraRole,
    CaptureTime,
    ConfigDigest,
    EventType,
    FrameId,
    PixelFormat,
    SessionId,
    StationId,
)
from chongming.contracts.scene import (
    CameraId,
    CameraMetadata,
    CameraStateTruth,
    ClockDomainSpec,
    FlyingObjectTruth,
    FrameImagingInput,
    ImageGenerationRequest,
    ImagingSpec,
    KinematicState,
    SceneCameraDistortion,
    SceneCameraIntrinsics,
    SceneDistortionModel,
    SegmentOcclusionTruth,
    SensorArtifactSpec,
    SimulationTime,
    SphereGeometry,
    SurfaceAppearance,
    TerrainModel,
    TerrainSpec,
    TimelineSpec,
    TimeScale,
    TrajectoryInterpolation,
    TrajectoryKnot,
    TrajectorySpec,
    TruthObjectId,
    VisibilityQuery,
    WorldObjectId,
    WorldObjectKind,
    WorldObjectSpec,
)
from chongming.modules.device import EventSequencerModule
from chongming.modules.scene import (
    CameraImagingModule,
    CameraProjectionModule,
    ExperimentClockModule,
    SceneLogicModule,
    TrajectoryModule,
    VisibilityModule,
)
from tests.fixtures import architecture_specs

IDENTITY = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def state(
    position: tuple[float, float, float],
    velocity: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> KinematicState:
    return KinematicState(
        position_enu_m=position,
        velocity_enu_mps=velocity,
        rotation_enu_from_body=IDENTITY,
        angular_velocity_body_radps=(0.0, 0.0, 0.0),
    )


def camera() -> CameraStateTruth:
    return CameraStateTruth(
        station_id=StationId("station-1"),
        camera_role=CameraRole.MAIN,
        metadata=CameraMetadata(
            camera_id=CameraId("camera-1"),
            display_name="main",
            profile_id="experimental-test",
            sensor_format=None,
            lens_focal_length_mm=None,
            nominal_diagonal_fov_deg=60.0,
            nominal_frame_rate_hz=30.0,
        ),
        sample_time=SimulationTime("clock", 0, 0),
        optical_state=state((0.0, 0.0, 0.0)),
        actual_intrinsics=SceneCameraIntrinsics(100, 100, 100.0, 100.0, 50.0, 50.0, 0.0),
        actual_distortion=SceneCameraDistortion(
            SceneDistortionModel.NONE, (0.0, 0.0, 0.0), (0.0, 0.0)
        ),
    )


def terrain() -> TerrainSpec:
    return TerrainSpec(
        terrain_id=WorldObjectId("ground"),
        model=TerrainModel.FLAT,
        base_elevation_m=-100.0,
        maximum_relief_m=0.0,
        heightfield_asset_uri=None,
        heightfield_asset_digest=None,
        appearance=SurfaceAppearance((0.2, 0.2, 0.2), 0.5),
        occludes_sight_lines=True,
    )


class ScenePrimitiveTests(unittest.TestCase):
    def test_event_sequencer_is_monotonic_across_event_types(self) -> None:
        sequencer = EventSequencerModule(
            station_id=StationId("station-1"),
            station_session_id=SessionId("session-1"),
            schema_version="event-v1",
        )
        envelopes = tuple(
            sequencer.next_envelope(event_type, ConfigDigest("config-1"))
            for event_type in (
                EventType.CALIBRATION_STATE,
                EventType.RAY,
                EventType.DIAGNOSTIC,
            )
        )
        self.assertEqual([item.sequence_no for item in envelopes], [1, 2, 3])
        self.assertEqual({item.station_session_id for item in envelopes}, {"session-1"})

    def test_clock_is_explicit_and_monotonic(self) -> None:
        clock = ExperimentClockModule()
        timeline = TimelineSpec(
            ClockDomainSpec("clock", TimeScale.EXPERIMENT_ELAPSED, "test", None), 10, 30, 5
        )
        clock.initialize(timeline)
        self.assertEqual(clock.read(), SimulationTime("clock", 10, 0))
        self.assertEqual(clock.advance_to(20), SimulationTime("clock", 20, 1))
        with self.assertRaises(ValueError):
            clock.advance_to(19)
        with self.assertRaises(ValueError):
            clock.advance_to(31)

    def test_linear_trajectory_samples_position_and_velocity(self) -> None:
        trajectory = TrajectorySpec(
            TrajectoryInterpolation.LINEAR,
            (
                TrajectoryKnot(0, state((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))),
                TrajectoryKnot(10, state((10.0, 2.0, 0.0), (3.0, 2.0, 0.0))),
            ),
        )
        result = TrajectoryModule().sample(trajectory, SimulationTime("clock", 5, 0))
        self.assertEqual(result.position_enu_m, (5.0, 1.0, 0.0))
        self.assertEqual(result.velocity_enu_mps, (2.0, 1.0, 0.0))

    def test_hold_trajectory_switches_at_the_knot(self) -> None:
        trajectory = TrajectorySpec(
            TrajectoryInterpolation.HOLD,
            (
                TrajectoryKnot(0, state((0.0, 0.0, 0.0))),
                TrajectoryKnot(10, state((10.0, 0.0, 0.0))),
            ),
        )
        result = TrajectoryModule().sample(trajectory, SimulationTime("clock", 10, 0))
        self.assertEqual(result.position_enu_m, (10.0, 0.0, 0.0))

    def test_cubic_trajectory_uses_hermite_velocity(self) -> None:
        trajectory = TrajectorySpec(
            TrajectoryInterpolation.CUBIC_HERMITE,
            (
                TrajectoryKnot(0, state((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))),
                TrajectoryKnot(1_000_000_000, state((10.0, 0.0, 0.0), (0.0, 0.0, 0.0))),
            ),
        )
        result = TrajectoryModule().sample(trajectory, SimulationTime("clock", 500_000_000, 0))
        self.assertEqual(result.position_enu_m, (5.0, 0.0, 0.0))
        self.assertEqual(result.velocity_enu_mps, (15.0, 0.0, 0.0))

    def test_projection_uses_actual_intrinsics_and_rejects_behind_camera(self) -> None:
        projector = CameraProjectionModule()
        visible = projector.project(camera(), (0.0, 0.0, 10.0))
        self.assertEqual(visible.centroid_px, (50.0, 50.0))
        self.assertTrue(visible.center_in_field_of_view)
        behind = projector.project(camera(), (0.0, 0.0, -1.0))
        self.assertIsNone(behind.centroid_px)
        self.assertFalse(behind.in_front_of_camera)

    def test_visibility_only_blocks_the_finite_segment(self) -> None:
        obstacle = WorldObjectSpec(
            object_id=WorldObjectId("obstacle"),
            display_name="obstacle",
            kind=WorldObjectKind.OBSTACLE,
            category="test",
            pose=state((0.0, 0.0, 5.0)),
            geometry=SphereGeometry(1.0),
            appearance=SurfaceAppearance((0.5, 0.5, 0.5), 0.5),
            occludes_sight_lines=True,
        )
        target = WorldObjectSpec(
            object_id=WorldObjectId("target"),
            display_name="target",
            kind=WorldObjectKind.FIXED_REFERENCE,
            category="test",
            pose=state((0.0, 0.0, 10.0)),
            geometry=SphereGeometry(0.5),
            appearance=SurfaceAppearance((0.5, 0.5, 0.5), 0.5),
            occludes_sight_lines=False,
        )
        projection = CameraProjectionModule().project(camera(), target.pose.position_enu_m)
        query = VisibilityQuery(
            sample_time=SimulationTime("clock", 0, 0),
            camera=camera(),
            target=FlyingObjectTruth(
                truth_object_id=TruthObjectId("truth-1"),
                display_name="target",
                model_name="sphere",
                sample_time=SimulationTime("clock", 0, 0),
                state=target.pose,
                center_reference_point_enu_m=target.pose.position_enu_m,
                geometry=target.geometry,
                appearance=target.appearance,
            ),
            projection=projection,
            terrain=terrain(),
            world_objects=(obstacle,),
            vegetation=(),
        )
        result = VisibilityModule().resolve(query)
        self.assertFalse(result.capture_eligible)
        self.assertEqual(len(result.occlusions), 1)
        self.assertIsInstance(result.occlusions[0], SegmentOcclusionTruth)

        beyond = replace(
            query,
            target=replace(query.target, center_reference_point_enu_m=(0.0, 0.0, 3.0)),
            projection=CameraProjectionModule().project(camera(), (0.0, 0.0, 3.0)),
        )
        self.assertTrue(VisibilityModule().resolve(beyond).capture_eligible)

    def test_scene_logic_samples_one_immutable_snapshot(self) -> None:
        spec, _ = architecture_specs()
        trajectory = object.__new__(TrajectoryModule)
        projector = object.__new__(CameraProjectionModule)
        visibility = object.__new__(VisibilityModule)
        logic = object.__new__(SceneLogicModule)
        SceneLogicModule.__init__(logic, trajectory, projector, visibility)
        logic.initialize(spec.scene_definition, spec.perturbations, spec.random_seed)
        snapshot = logic.sample(SimulationTime("structure-clock", 0, 0))
        self.assertEqual(snapshot.sample_time.time_ns, 0)
        self.assertEqual(len(snapshot.cameras), len(spec.scene_definition.cameras))
        self.assertEqual(snapshot.flying_objects, ())
        self.assertEqual(snapshot.target_visibility, ())

    def test_imager_emits_deterministic_rgb8_and_separate_truth(self) -> None:
        spec, _ = architecture_specs()
        logic = SceneLogicModule(TrajectoryModule(), CameraProjectionModule(), VisibilityModule())
        logic.initialize(spec.scene_definition, spec.perturbations, spec.random_seed)
        snapshot = logic.sample(SimulationTime("structure-clock", 500_000, 0))
        frame = FrameImagingInput(
            station_id=spec.scene_definition.cameras[0].station_id,
            station_session_id=SessionId("image-session"),
            camera_role=spec.scene_definition.cameras[0].camera_role,
            frame_id=FrameId("frame-0"),
            capture_time=CaptureTime(
                clock_domain_id="structure-clock",
                exposure_start_ns=0,
                exposure_end_ns=1_000_000,
                clock_uncertainty_ns=0,
                rolling_shutter_model="none",
            ),
            online_camera_config_digest=ConfigDigest("online"),
            exposure_samples=(snapshot,),
        )
        imaging = ImagingSpec(
            color_space="srgb",
            exposure_samples=1,
            sensor_artifacts=SensorArtifactSpec(
                output_pixel_format=PixelFormat.RGB8,
                sensor_noise_std_intensity=0.0,
                intensity_min=0.0,
                intensity_max=1.0,
                auto_exposure_enabled=False,
                dynamic_range_stops=None,
                saturation_level=None,
                glare_enabled=False,
                bloom_strength=None,
            ),
        )
        request = ImageGenerationRequest("scenario", 7, imaging, (frame,))
        ordinary, truth = CameraImagingModule().render(request)
        ordinary_again, _ = CameraImagingModule().render(request)
        self.assertEqual(ordinary.frames[0].image.pixel_format, PixelFormat.RGB8)
        self.assertEqual(
            len(ordinary.frames[0].image.pixels),
            ordinary.frames[0].image.width_px * ordinary.frames[0].image.height_px * 3,
        )
        self.assertEqual(ordinary.frames[0].image.pixels, ordinary_again.frames[0].image.pixels)
        self.assertEqual(len(truth.frame_truth), 1)
        self.assertNotIn(
            "truth_object_id",
            {field.name for field in __import__("dataclasses").fields(ordinary.frames[0])},
        )

    def test_scenario_source_runs_independent_camera_schedules(self) -> None:
        spec, run = architecture_specs()
        from chongming.composition.graph import declare_assembly

        assembly = declare_assembly(spec, run)
        steps = list(assembly.scene.scenario.generate(spec))
        self.assertTrue(steps)
        self.assertTrue(all(step.packets.rays == () for step in steps))
        self.assertEqual(
            [step.truth.sample_time.time_ns for step in steps],
            sorted(step.truth.sample_time.time_ns for step in steps),
        )
        self.assertTrue(all(step.video.frames for step in steps))

        repeat = list(assembly.scene.scenario.generate(spec))
        self.assertEqual(steps, repeat)


if __name__ == "__main__":
    unittest.main()

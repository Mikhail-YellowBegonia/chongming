"""Device orchestration with real image processing and explicit fixture policies."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import Mock

import numpy as np

from chongming.contracts import device as d
from chongming.contracts import fusion as f
from chongming.contracts import model as m
from chongming.modules.device import (
    OPENCV_CALIBRATION_ESTIMATOR_ID,
    AuxiliaryCalibrationModule,
    CalibrationEstimationModule,
    CalibrationHistoryModule,
    CalibrationStateModule,
    DeviceInputError,
    DeviceModule,
    EventSequencerModule,
    MainDetectionModule,
    MotionDetectionModule,
    RayGenerationModule,
    ReferenceRecognitionModule,
    Tracking2DModule,
)
from chongming.modules.fusion_ingress import (
    AdmissionJournalModule,
    CalibrationRegistryModule,
    EventAdmissionModule,
    EventClockMappingModule,
    StationRegistryModule,
)
from tests.test_calibration import IDENTITY, _config, _policy, _references
from tests.test_detection import detection_policy, tracking_policy
from tests.test_device_rays import PIXEL_COVARIANCE, SESSION, capture, policy


def now(time: int) -> m.ProcessingTime:
    return m.ProcessingTime(clock_domain_id="test-clock", time_ns=time)


def frame(
    config: m.StationConfig,
    time: int,
    role: m.CameraRole = m.CameraRole.MAIN,
    *,
    target: bool = False,
    pixels: bytes | None = None,
    session: m.SessionId = SESSION,
) -> m.CameraFrame:
    image = np.zeros((480, 640), dtype=np.uint8)
    if target:
        image[212:230, 340:356] = 255
    return m.CameraFrame(
        station_id=config.station_id,
        station_session_id=session,
        camera_role=role,
        frame_id=m.FrameId(f"{role.value}-{time}"),
        capture_time=capture(time),
        image=m.ImagePlane(
            width_px=640,
            height_px=480,
            pixel_format=m.PixelFormat.GRAY8,
            row_stride_bytes=640,
            pixels=image.tobytes() if pixels is None else pixels,
        ),
        camera_config_ref=config.config_digest,
    )


class ReferenceSequence:
    """Supply independent projected reference corners, not scene truth types."""

    def __init__(self) -> None:
        self.available = True
        self.calls = 0

    def recognize(
        self, auxiliary: m.CameraFrame, config: m.StationConfig
    ) -> d.ReferenceObservationBatch:
        self.calls += 1
        batch = _references(config, np.eye(3), start=auxiliary.capture_time.exposure_start_ns)
        return replace(
            batch,
            auxiliary_frame_ref=m.FrameRef(
                station_id=auxiliary.station_id,
                station_session_id=auxiliary.station_session_id,
                camera_role=auxiliary.camera_role,
                frame_id=auxiliary.frame_id,
            ),
            capture_time=auxiliary.capture_time,
            observations=batch.observations if self.available else (),
            reason_codes=() if self.available else ("checkerboard_not_found",),
        )


def device(
    config: m.StationConfig | None = None,
    *,
    references: ReferenceSequence | None = None,
    calibration_policy: d.CalibrationPolicy | None = None,
    session: m.SessionId = SESSION,
    ray_policy: d.RayGenerationPolicy | None = None,
) -> DeviceModule:
    config = config or _config()
    calibration_policy = calibration_policy or _policy(estimator_id=OPENCV_CALIBRATION_ESTIMATOR_ID)
    sequencer = EventSequencerModule(
        station_id=config.station_id, station_session_id=session, schema_version="device-test-v1"
    )
    history = CalibrationHistoryModule(
        station_id=config.station_id,
        station_session_id=session,
        station_config_digest=config.config_digest,
    )
    states = CalibrationStateModule(policy=calibration_policy, history=history, sequencer=sequencer)
    auxiliary = AuxiliaryCalibrationModule(
        recognizer=references
        if references is not None
        else ReferenceRecognitionModule(policy=calibration_policy),
        estimator=CalibrationEstimationModule(policy=calibration_policy),
        states=states,
        update_period_ns=calibration_policy.update_period_ns,
    )
    main = MainDetectionModule(
        detector=MotionDetectionModule(
            policy=detection_policy(background_warmup_frame_count=1, background_learning_rate=0.0)
        ),
        tracker=Tracking2DModule(
            station_id=config.station_id, station_session_id=session, policy=tracking_policy()
        ),
    )
    return DeviceModule(
        config=config,
        station_session_id=session,
        detector=main,
        calibrator=auxiliary,
        history=history,
        ray_builder=RayGenerationModule(
            policy=ray_policy
            or replace(policy(), fallback_centroid_covariance_px2=PIXEL_COVARIANCE),
            sequencer=sequencer,
        ),
        sequencer=sequencer,
    )


def only_ray(result: d.DeviceFrameResult) -> m.RayEvent:
    rays = [event for event in result.events if isinstance(event, m.RayEvent)]
    if len(rays) != 1:
        raise AssertionError(f"Expected exactly one ray, got events {result.events!r}")
    return rays[0]


def categories(result: d.DeviceFrameResult) -> set[str]:
    return {event.category for event in result.events if isinstance(event, m.DiagnosticEvent)}


class DevicePipelineTests(unittest.TestCase):
    def test_ordinary_auxiliary_and_main_images_produce_a_calibrated_measured_ray(self) -> None:
        config = _config()
        assert isinstance(config.reference_anchor, m.SurveyedReference)
        config = replace(
            config,
            reference_anchor=replace(
                config.reference_anchor,
                landmarks=tuple(
                    replace(landmark, position_enu_m=(*landmark.position_enu_m[:2], 1.0))
                    for landmark in config.reference_anchor.landmarks
                ),
            ),
        )
        pipeline = device(config)
        image = np.full((480, 640), 255, dtype=np.uint8)
        for row in range(5):
            for column in range(7):
                if (row + column) % 2 == 0:
                    image[
                        170 + row * 28 : 170 + (row + 1) * 28,
                        222 + column * 28 : 222 + (column + 1) * 28,
                    ] = 0
        update = pipeline.process_auxiliary(
            frame(config, 1, m.CameraRole.AUXILIARY, pixels=image.tobytes()), now(2)
        )
        self.assertIsNotNone(update.solution)
        self.assertEqual(update.state_event.state.health, m.CalibrationHealth.VALID)
        warmup = pipeline.process_main(frame(config, 3), now(3))
        self.assertEqual(categories(warmup), {"background_warmup"})
        main = frame(config, 4, target=True)
        result = pipeline.process_main(main, now(4))
        ray = only_ray(result)
        self.assertEqual(ray.frame_id, main.frame_id)
        self.assertEqual(ray.capture_time, main.capture_time)
        self.assertEqual(result.detections.observations[0].centroid_px, (347.5, 220.5))
        self.assertIsNone(result.detections.observations[0].centroid_covariance_px2)
        self.assertEqual(ray.calibration_solution_ref, update.state_event.state.solution_ref)
        self.assertIn("explicit_fallback_pixel_covariance", ray.quality_flags)
        self.assertGreater(ray.ray_direction_enu_unit[2], 0.99)
        self.assertEqual(result.progress.completed_capture_time, main.capture_time)
        self.assertEqual(result.progress.received_at, now(4))
        self.assertEqual(result.progress.station_session_id, SESSION)
        self.assertFalse(result.progress.stream_ended)

    def test_no_trusted_solution_never_uses_installation_orientation_as_fresh(self) -> None:
        references = ReferenceSequence()
        references.available = False
        pipeline = device(references=references)
        config = pipeline.config
        update = pipeline.process_auxiliary(frame(config, 1, m.CameraRole.AUXILIARY), now(1))
        self.assertIsNone(update.solution)
        self.assertIsNone(update.state_event.state.solution_ref)
        self.assertEqual(update.state_event.state.health, m.CalibrationHealth.INITIALIZING)
        self.assertTrue(update.diagnostics)
        warmup = pipeline.process_main(frame(config, 2), now(2))
        self.assertEqual(categories(warmup), {"background_warmup", "calibration_unavailable"})
        result = pipeline.process_main(frame(config, 3, target=True), now(3))
        self.assertEqual(len(result.detections.observations), 1)
        self.assertEqual(categories(result), {"calibration_unavailable"})
        self.assertFalse(any(isinstance(event, m.RayEvent) for event in result.events))
        self.assertEqual(result.progress.completed_capture_time, capture(3))

    def test_freeze_stale_recovery_and_late_main_use_historical_evidence(self) -> None:
        references = ReferenceSequence()
        pipeline = device(references=references)
        config = pipeline.config
        first = pipeline.process_auxiliary(frame(config, 1, m.CameraRole.AUXILIARY), now(2))
        pipeline.process_main(frame(config, 3), now(3))
        initial_ray = only_ray(pipeline.process_main(frame(config, 10, target=True), now(10)))
        references.available = False
        failed = pipeline.process_auxiliary(frame(config, 20, m.CameraRole.AUXILIARY), now(20))
        self.assertEqual(failed.state_event.state.health, m.CalibrationHealth.FROZEN)
        self.assertIsNone(failed.solution)
        self.assertEqual(
            failed.state_event.state.solution_ref, first.state_event.state.solution_ref
        )
        late = only_ray(pipeline.process_main(frame(config, 15, target=True), now(25)))
        self.assertEqual(late.calibration_usage, m.CalibrationUsage.VALID)
        frozen = only_ray(pipeline.process_main(frame(config, 25, target=True), now(25)))
        self.assertEqual(frozen.calibration_usage, m.CalibrationUsage.FROZEN)
        references.available = True
        recovering = pipeline.process_auxiliary(frame(config, 30, m.CameraRole.AUXILIARY), now(30))
        self.assertEqual(recovering.state_event.state.health, m.CalibrationHealth.RECOVERING)
        self.assertIsNone(recovering.solution)
        recovering_ray = only_ray(pipeline.process_main(frame(config, 35, target=True), now(35)))
        self.assertEqual(recovering_ray.calibration_usage, m.CalibrationUsage.FROZEN)
        recovered = pipeline.process_auxiliary(frame(config, 40, m.CameraRole.AUXILIARY), now(45))
        self.assertEqual(recovered.state_event.state.health, m.CalibrationHealth.VALID)
        self.assertIsNotNone(recovered.solution)
        self.assertNotEqual(
            recovered.state_event.state.solution_ref, first.state_event.state.solution_ref
        )
        before_acceptance = only_ray(pipeline.process_main(frame(config, 42, target=True), now(45)))
        self.assertEqual(
            before_acceptance.calibration_solution_ref, first.state_event.state.solution_ref
        )
        self.assertEqual(before_acceptance.calibration_usage, m.CalibrationUsage.FROZEN)
        current = only_ray(pipeline.process_main(frame(config, 50, target=True), now(50)))
        self.assertEqual(current.calibration_solution_ref, recovered.state_event.state.solution_ref)
        self.assertEqual(current.calibration_usage, m.CalibrationUsage.VALID)
        self.assertIsNone(pipeline.tick(now(145)))  # age == stale_after is still valid.
        stale = pipeline.tick(now(146))
        assert stale is not None
        self.assertEqual(stale.state_event.state.health, m.CalibrationHealth.STALE)
        self.assertEqual(stale.state_event.state.solution_ref, current.calibration_solution_ref)
        stale_ray = only_ray(pipeline.process_main(frame(config, 150, target=True), now(150)))
        self.assertEqual(stale_ray.calibration_usage, m.CalibrationUsage.STALE)
        self.assertEqual(initial_ray.calibration_usage, m.CalibrationUsage.VALID)
        self.assertEqual(
            initial_ray.calibration_evidence.state_version, first.state_event.state.state_version
        )
        self.assertEqual(
            frozen.calibration_evidence.last_trusted_time, first.state_event.state.last_trusted_time
        )
        self.assertIsNone(pipeline.tick(now(151)))

    def test_latest_auxiliary_source_and_acceptance_cannot_leak_into_old_exposure(self) -> None:
        pipeline = device(references=ReferenceSequence())
        config = pipeline.config
        pipeline.process_auxiliary(frame(config, 100, m.CameraRole.AUXILIARY), now(110))
        pipeline.process_main(frame(config, 50), now(110))
        before_source = pipeline.process_main(frame(config, 90, target=True), now(110))
        before_acceptance = pipeline.process_main(frame(config, 105, target=True), now(110))
        for result in (before_source, before_acceptance):
            self.assertEqual(categories(result), {"calibration_unavailable"})
            self.assertFalse(any(isinstance(event, m.RayEvent) for event in result.events))
        self.assertEqual(
            only_ray(
                pipeline.process_main(frame(config, 110, target=True), now(110))
            ).calibration_usage,
            m.CalibrationUsage.VALID,
        )

    def test_auxiliary_throttle_reuses_an_update_without_minting_events(self) -> None:
        references = ReferenceSequence()
        pipeline = device(
            references=references,
            calibration_policy=_policy(
                estimator_id=OPENCV_CALIBRATION_ESTIMATOR_ID, update_period_ns=100
            ),
        )
        config = pipeline.config
        first = pipeline.process_auxiliary(frame(config, 1, m.CameraRole.AUXILIARY), now(2))
        held = pipeline.process_auxiliary(frame(config, 50, m.CameraRole.AUXILIARY), now(51))
        self.assertIs(held, first)
        self.assertEqual(references.calls, 1)
        second = pipeline.process_auxiliary(frame(config, 101, m.CameraRole.AUXILIARY), now(102))
        self.assertEqual(references.calls, 2)
        self.assertEqual(
            second.state_event.envelope.sequence_no, first.state_event.envelope.sequence_no + 1
        )

    def test_warmup_empty_failed_and_unknown_uncertainty_all_complete_main_progress(self) -> None:
        pipeline = device(references=ReferenceSequence(), ray_policy=policy())
        config = pipeline.config
        pipeline.process_auxiliary(frame(config, 1, m.CameraRole.AUXILIARY), now(1))
        inputs = (
            frame(config, 2),
            frame(config, 3),
            frame(config, 4, pixels=b"bad pixels"),
            frame(config, 5, target=True),
        )
        results = tuple(
            pipeline.process_main(value, now(index + 2)) for index, value in enumerate(inputs)
        )
        self.assertEqual(
            [categories(result) for result in results],
            [
                {"background_warmup"},
                {"no_detections"},
                {"main_processing_failed"},
                {"ray_uncertainty_unavailable"},
            ],
        )
        self.assertEqual(
            [result.detections.processing_status for result in results],
            [
                m.ProcessingStatus.PROCESSED,
                m.ProcessingStatus.PROCESSED,
                m.ProcessingStatus.FAILED,
                m.ProcessingStatus.PROCESSED,
            ],
        )
        for value, result in zip(inputs, results):
            self.assertEqual(result.progress.completed_capture_time, value.capture_time)
            self.assertFalse(any(isinstance(event, m.RayEvent) for event in result.events))
        self.assertEqual(len(results[-1].detections.observations), 1)

    def test_coasting_and_deleted_tracks_never_create_measurement_rays(self) -> None:
        pipeline = device(references=ReferenceSequence())
        config = pipeline.config
        pipeline.process_auxiliary(frame(config, 1, m.CameraRole.AUXILIARY), now(1))
        pipeline.process_main(frame(config, 2), now(2))
        only_ray(pipeline.process_main(frame(config, 3, target=True), now(3)))
        empty = pipeline.process_main(frame(config, 4), now(4))
        self.assertEqual(empty.detections.observations, ())
        self.assertTrue(empty.tracks)
        self.assertEqual(empty.tracks[0].lifecycle, "coasting")
        self.assertFalse(any(isinstance(event, m.RayEvent) for event in empty.events))
        deleted = pipeline.process_main(frame(config, 2_000_000_000), now(2_000_000_000))
        self.assertEqual(deleted.tracks[0].lifecycle, "deleted")
        self.assertFalse(any(isinstance(event, m.RayEvent) for event in deleted.events))

    def test_rejected_metadata_carries_diagnostics_without_touching_algorithms_or_progress(
        self,
    ) -> None:
        pipeline = device(references=ReferenceSequence())
        config = pipeline.config
        detector = Mock(wraps=pipeline.detector)
        calibrator = Mock(wraps=pipeline.calibrator)
        pipeline.detector = detector
        pipeline.calibrator = calibrator
        valid = frame(config, 10)
        alternatives = (
            replace(valid, station_id=m.StationId("wrong")),
            replace(valid, station_session_id=m.SessionId("wrong")),
            replace(valid, camera_role=m.CameraRole.AUXILIARY),
            replace(valid, camera_config_ref=m.ConfigDigest("wrong")),
            replace(valid, frame_id=m.FrameId("")),
            replace(valid, image=replace(valid.image, width_px=1)),
            replace(valid, capture_time=replace(valid.capture_time, clock_domain_id="other")),
            replace(valid, capture_time=replace(valid.capture_time, exposure_end_ns=9)),
            replace(valid, capture_time=replace(valid.capture_time, clock_uncertainty_ns=-1)),
            replace(valid, capture_time=capture(11)),
        )
        for candidate in alternatives:
            with self.subTest(frame=candidate.frame_id, capture=candidate.capture_time):
                with self.assertRaises(DeviceInputError) as context:
                    pipeline.process_main(candidate, now(10))
                self.assertEqual(context.exception.diagnostic.category, "device_input_invalid")
                detector.process.assert_not_called()
                calibrator.process.assert_not_called()
                self.assertEqual(pipeline._last_exposure_ns, {})
        valid_result = pipeline.process_main(valid, now(10))
        self.assertEqual(valid_result.progress.completed_capture_time, valid.capture_time)
        detector.process.assert_called_once()
        with self.assertRaisesRegex(DeviceInputError, "exposure_time_not_increasing"):
            pipeline.process_main(valid, now(10))

    def test_wrong_auxiliary_metadata_cannot_freeze_or_change_existing_history(self) -> None:
        pipeline = device(references=ReferenceSequence())
        config = pipeline.config
        initial = pipeline.process_auxiliary(frame(config, 1, m.CameraRole.AUXILIARY), now(1))
        auxiliary = frame(config, 2, m.CameraRole.AUXILIARY)
        for candidate in (
            replace(auxiliary, station_id=m.StationId("wrong")),
            replace(auxiliary, station_session_id=m.SessionId("wrong")),
            replace(auxiliary, camera_role=m.CameraRole.MAIN),
            replace(auxiliary, camera_config_ref=m.ConfigDigest("wrong")),
        ):
            with self.subTest(role=candidate.camera_role, session=candidate.station_session_id):
                with self.assertRaises(DeviceInputError):
                    pipeline.process_auxiliary(candidate, now(2))
                current = pipeline.history.snapshot_at(capture(3))
                self.assertEqual(current.state, initial.state_event.state)
                self.assertEqual(current.solution, initial.solution)

    def test_tick_rejects_clock_changes_and_time_regression(self) -> None:
        pipeline = device(references=ReferenceSequence())
        self.assertIsNone(pipeline.tick(now(10)))
        for time, reason in (
            (now(9), "processing_time_regressed"),
            (replace(now(11), clock_domain_id="other"), "processing_clock_mismatch"),
        ):
            with self.subTest(time=time):
                with self.assertRaisesRegex(DeviceInputError, reason) as context:
                    pipeline.tick(time)
                self.assertIsNone(context.exception.diagnostic.capture_time)
        self.assertIsNone(pipeline.tick(now(11)))

    def test_event_kinds_share_one_sequence_but_sessions_remain_isolated(self) -> None:
        first = device(references=ReferenceSequence())
        other_session = m.SessionId("other-session")
        second = device(references=ReferenceSequence(), session=other_session)
        for pipeline, session in ((first, SESSION), (second, other_session)):
            config = pipeline.config
            auxiliary = pipeline.process_auxiliary(
                frame(config, 1, m.CameraRole.AUXILIARY, session=session), now(1)
            )
            warmup = pipeline.process_main(frame(config, 2, session=session), now(2))
            ray = only_ray(
                pipeline.process_main(frame(config, 3, target=True, session=session), now(3))
            )
            events = (auxiliary.state_event, *auxiliary.diagnostics, *warmup.events, ray)
            self.assertEqual(
                [event.envelope.sequence_no for event in events], list(range(1, len(events) + 1))
            )
            self.assertEqual({event.envelope.station_session_id for event in events}, {session})
            self.assertEqual(ray.calibration_solution_ref.station_session_id, session)
            self.assertEqual(ray.local_track_id, m.LocalTrackId("local-1"))
        self.assertIsNot(first.history, second.history)

    def test_device_events_satisfy_center_admission_without_virtual_ray_replacement(self) -> None:
        references = ReferenceSequence()
        pipeline = device(references=references)
        config = pipeline.config
        initial = pipeline.process_auxiliary(frame(config, 1, m.CameraRole.AUXILIARY), now(1))
        warmup = pipeline.process_main(frame(config, 2), now(2))
        valid = pipeline.process_main(frame(config, 3, target=True), now(3))
        references.available = False
        frozen = pipeline.process_auxiliary(frame(config, 4, m.CameraRole.AUXILIARY), now(4))
        held = pipeline.process_main(frame(config, 5, target=True), now(5))
        stations = StationRegistryModule()
        self.assertEqual(stations.register(config).status, f.RegistryWriteStatus.REGISTERED)
        admission = EventAdmissionModule(
            policy=f.AdmissionPolicy(
                accepted_schema_versions=("device-test-v1",),
                world_frame_id=config.world_frame_id,
                capture_clock_domain_id="test-clock",
                direction_unit_tolerance=1e-12,
                tangent_basis_orthogonality_tolerance=1e-12,
                calibration_usage_rules=tuple(
                    f.CalibrationUsageRule(
                        usage=usage, action=f.CalibrationAction.ACCEPT, geometry_weight_scale=1.0
                    )
                    for usage in m.CalibrationUsage
                ),
            ),
            stations=stations,
            calibrations=CalibrationRegistryModule(stations=stations),
            journal=AdmissionJournalModule(),
            clock=EventClockMappingModule(
                mapping=f.EventClockMappingSpec(
                    processing_clock_domain_id="test-clock",
                    capture_clock_domain_id="test-clock",
                    scale_numerator=1,
                    scale_denominator=1,
                    offset_ns=0,
                    mapping_uncertainty_ns=0,
                )
            ),
        )
        events = (
            initial.state_event,
            *initial.diagnostics,
            *warmup.events,
            *valid.events,
            frozen.state_event,
            *frozen.diagnostics,
            *held.events,
        )
        for event in events:
            with self.subTest(event=event.envelope.event_type, sequence=event.envelope.sequence_no):
                outcome = admission.admit(m.ReceivedEvent(event=event, arrival_time=now(6)))
                self.assertEqual(
                    outcome.record.decision, f.AdmissionDecision.ACCEPTED, outcome.record.reasons
                )
                if isinstance(event, m.RayEvent):
                    self.assertIsNotNone(outcome.admitted_ray)
        self.assertEqual(only_ray(valid).calibration_usage, m.CalibrationUsage.VALID)
        self.assertEqual(only_ray(held).calibration_usage, m.CalibrationUsage.FROZEN)

    def test_device_estimator_rejects_architecture_placeholder_selection(self) -> None:
        config = _config()
        estimator = CalibrationEstimationModule(policy=_policy(estimator_id="unimplemented"))
        with self.assertRaisesRegex(ValueError, "explicitly selected OpenCV"):
            estimator.estimate(_references(config, np.asarray(IDENTITY)), config, now(10))


if __name__ == "__main__":
    unittest.main()

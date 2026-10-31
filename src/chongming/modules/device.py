"""Online device assembly: measured detections, retained calibration and world rays."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from itertools import count
from typing import Never

from chongming.calibration import (
    AuxiliaryCalibrationPipeline,
    CalibrationHistoryStore,
    CalibrationStateMachine,
    OpenCVCalibrationEstimator,
    OpenCVReferenceRecognizer,
)
from chongming.contracts import device as d
from chongming.contracts import model as m
from chongming.contracts.fusion import StationEventTimeProgress
from chongming.modules.base import ModuleShell
from chongming.modules.detection import (
    MainDetectionModule,
    MotionDetectionModule,
    Tracking2DModule,
    _frame_ref,
    _midpoint,
)
from chongming.modules.rays import RayGenerationModule
from chongming.ports.device import (
    AuxiliaryCalibrator,
    CalibrationHistory,
    EventSequencer,
    MainDetector,
    RayBuilder,
)

__all__ = [
    "AuxiliaryCalibrationModule",
    "CalibrationEstimationModule",
    "CalibrationHistoryModule",
    "CalibrationStateModule",
    "DeviceInputError",
    "DeviceModule",
    "EventSequencerModule",
    "MainDetectionModule",
    "MotionDetectionModule",
    "RayGenerationModule",
    "ReferenceRecognitionModule",
    "Tracking2DModule",
]

OPENCV_CALIBRATION_ESTIMATOR_ID = "opencv-checkerboard-v1"


@dataclass(frozen=True, kw_only=True)
class EventSequencerModule(ModuleShell):
    business_implemented = True
    station_id: m.StationId
    station_session_id: m.SessionId
    schema_version: str
    _sequence: Iterator[int] = field(
        default_factory=lambda: count(1), init=False, repr=False, compare=False
    )

    def next_envelope(
        self, event_type: m.EventType, config_digest: m.ConfigDigest
    ) -> m.EventEnvelope:
        if not self.schema_version:
            raise ValueError("event schema version cannot be empty")
        return m.EventEnvelope(
            schema_version=self.schema_version,
            event_type=event_type,
            station_id=self.station_id,
            station_session_id=self.station_session_id,
            sequence_no=next(self._sequence),
            station_config_digest=config_digest,
        )


class ReferenceRecognitionModule(OpenCVReferenceRecognizer, ModuleShell):
    """Managed adapter to the existing surveyed-checkerboard recognizer."""

    business_implemented = True

    def __init__(self, *, policy: d.CalibrationPolicy) -> None:
        self.policy = policy
        super().__init__()


class CalibrationEstimationModule(OpenCVCalibrationEstimator, ModuleShell):
    business_implemented = True

    def estimate(
        self,
        references: d.ReferenceObservationBatch,
        config: m.StationConfig,
        now: m.ProcessingTime,
    ) -> d.CalibrationAttempt:
        if self.policy.estimator_id != OPENCV_CALIBRATION_ESTIMATOR_ID:
            raise ValueError("device calibration requires an explicitly selected OpenCV estimator")
        return super().estimate(references, config, now)


class CalibrationHistoryModule(CalibrationHistoryStore, ModuleShell):
    business_implemented = True


class CalibrationStateModule(CalibrationStateMachine, ModuleShell):
    business_implemented = True


class AuxiliaryCalibrationModule(AuxiliaryCalibrationPipeline, ModuleShell):
    business_implemented = True


class DeviceInputError(ValueError):
    """Rejected metadata, with an explicit diagnostic but no fabricated progress."""

    def __init__(self, diagnostic: m.DiagnosticEvent) -> None:
        self.diagnostic = diagnostic
        super().__init__(", ".join(diagnostic.reason_codes))


class DeviceModule(ModuleShell):
    """One image session on a common exposure/processing clock.

    Wrong streams and invalid times raise before entering the image algorithms.
    Their diagnostic is attached to DeviceInputError: DeviceFrameResult cannot
    represent rejection without falsely claiming an exposure was completed.
    Legal frames with failed pixels still complete and advance main progress.
    Main/auxiliary exposures increase independently; late main processing queries
    retained history rather than using the latest auxiliary result.
    """

    business_implemented = True

    def __init__(
        self,
        *,
        config: m.StationConfig,
        station_session_id: m.SessionId,
        detector: MainDetector,
        calibrator: AuxiliaryCalibrator,
        history: CalibrationHistory,
        ray_builder: RayBuilder,
        sequencer: EventSequencer,
    ) -> None:
        self.config = config
        self.station_session_id = station_session_id
        self.detector = detector
        self.calibrator = calibrator
        self.history = history
        self.ray_builder = ray_builder
        self.sequencer = sequencer
        self._clock: str | None = None
        self._last_processing_ns: int | None = None
        self._last_exposure_ns: dict[m.CameraRole, int] = {}

    def _diagnostic(
        self,
        frame: m.CameraFrame | None,
        category: str,
        *reasons: str,
        calibration_ref: m.CalibrationSolutionRef | None = None,
    ) -> m.DiagnosticEvent:
        related_frame = (
            _frame_ref(frame)
            if frame is not None
            and frame.station_id == self.config.station_id
            and frame.station_session_id == self.station_session_id
            else None
        )
        capture = None
        if frame is not None:
            try:
                _midpoint(frame.capture_time)
            except (TypeError, ValueError):
                pass  # The invalid input has no usable exposure to report.
            else:
                capture = frame.capture_time
        return m.DiagnosticEvent(
            envelope=self.sequencer.next_envelope(
                m.EventType.DIAGNOSTIC, self.config.config_digest
            ),
            related_frame_ref=related_frame,
            capture_time=capture,
            category=category,
            reason_codes=reasons,
            related_config_ref=self.config.config_digest,
            related_calibration_ref=calibration_ref,
        )

    def _reject(self, frame: m.CameraFrame | None, reason: str) -> Never:
        raise DeviceInputError(self._diagnostic(frame, "device_input_invalid", reason))

    def _check_now(self, now: m.ProcessingTime, frame: m.CameraFrame | None) -> None:
        if not now.clock_domain_id or type(now.time_ns) is not int:
            self._reject(frame, "invalid_processing_time")
        if self._clock is not None and now.clock_domain_id != self._clock:
            self._reject(frame, "processing_clock_mismatch")
        if self._last_processing_ns is not None and now.time_ns < self._last_processing_ns:
            self._reject(frame, "processing_time_regressed")

    def _accept_frame(
        self, frame: m.CameraFrame, role: m.CameraRole, now: m.ProcessingTime
    ) -> None:
        if frame.station_id != self.config.station_id:
            self._reject(frame, "station_mismatch")
        if frame.station_session_id != self.station_session_id:
            self._reject(frame, "station_session_mismatch")
        if frame.camera_role is not role:
            self._reject(frame, "camera_role_mismatch")
        if frame.camera_config_ref != self.config.config_digest:
            self._reject(frame, "camera_configuration_mismatch")
        if not frame.frame_id:
            self._reject(frame, "frame_id_missing")
        camera = (
            self.config.main_camera if role is m.CameraRole.MAIN else self.config.auxiliary_camera
        )
        if (frame.image.width_px, frame.image.height_px) != (
            camera.intrinsics.image_width_px,
            camera.intrinsics.image_height_px,
        ):
            self._reject(frame, "camera_image_dimensions_mismatch")
        try:
            midpoint = _midpoint(frame.capture_time)
        except (TypeError, ValueError):
            self._reject(frame, "invalid_capture_time")
        self._check_now(now, frame)
        if frame.capture_time.clock_domain_id != now.clock_domain_id:
            self._reject(frame, "capture_processing_clock_mismatch")
        if midpoint > now.time_ns:
            self._reject(frame, "exposure_not_completed_at_processing_time")
        previous = self._last_exposure_ns.get(role)
        if previous is not None and midpoint <= previous:
            self._reject(frame, "exposure_time_not_increasing")
        self._clock = now.clock_domain_id
        self._last_processing_ns = now.time_ns
        self._last_exposure_ns[role] = midpoint

    def process_main(self, frame: m.CameraFrame, now: m.ProcessingTime) -> d.DeviceFrameResult:
        self._accept_frame(frame, m.CameraRole.MAIN, now)
        result = self.detector.process(frame)
        detections = result.detections
        events: list[m.StationEvent] = []
        if detections.processing_status is m.ProcessingStatus.FAILED:
            events.append(
                self._diagnostic(frame, "main_processing_failed", *detections.reason_codes)
            )
        else:
            calibration = self.history.snapshot_at(frame.capture_time)
            if detections.observations:
                events.extend(
                    self.ray_builder.build(observation, self.config, calibration)
                    for observation in detections.observations
                )
            else:
                category = (
                    "background_warmup"
                    if "background_warmup" in detections.reason_codes
                    else "no_detections"
                )
                events.append(self._diagnostic(frame, category, *detections.reason_codes))
                if calibration.solution is None or calibration.usage is None:
                    events.append(
                        self._diagnostic(
                            frame, "calibration_unavailable", *calibration.reason_codes
                        )
                    )
        return d.DeviceFrameResult(
            completed_at=now,
            detections=detections,
            tracks=result.tracks,
            events=tuple(events),
            progress=StationEventTimeProgress(
                station_id=self.config.station_id,
                station_session_id=self.station_session_id,
                station_config_digest=self.config.config_digest,
                completed_capture_time=frame.capture_time,
                received_at=now,
                stream_ended=False,
            ),
        )

    def process_auxiliary(self, frame: m.CameraFrame, now: m.ProcessingTime) -> m.CalibrationUpdate:
        self._accept_frame(frame, m.CameraRole.AUXILIARY, now)
        return self.calibrator.process(frame, self.config, now)

    def tick(self, now: m.ProcessingTime) -> m.CalibrationUpdate | None:
        self._check_now(now, None)
        self._clock = now.clock_domain_id
        self._last_processing_ns = now.time_ns
        return self.calibrator.tick(now, self.config)

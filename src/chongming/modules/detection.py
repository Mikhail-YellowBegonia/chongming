"""Explicitly configured MOG2 foreground detection and local image-space tracking."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import cv2

from chongming.calibration import _image_to_gray
from chongming.contracts import device as d
from chongming.contracts import model as m
from chongming.modules.base import ModuleShell
from chongming.ports.device import MotionDetector, Tracker2D
from chongming.uncertainty import covariance_factor

logger = logging.getLogger(__name__)

MOG2_ALGORITHM_ID = "mog2-components-v1"
TRACKING_ALGORITHM_ID = "constant-velocity-nearest-neighbor-v1"
FILL_RATIO_SCORE = m.ScoreDefinition(
    definition_id="foreground-component-fill-ratio-v1",
    kind=m.ScoreKind.RANKING,
    minimum=0.0,
    maximum=1.0,
    cross_station_comparison_group=None,
)
TRACK_QUALITY_SCORE = m.ScoreDefinition(
    definition_id="consecutive-detection-fraction-v1",
    kind=m.ScoreKind.RANKING,
    minimum=0.0,
    maximum=1.0,
    cross_station_comparison_group=None,
)


def _midpoint(capture: m.CaptureTime) -> int:
    if not capture.clock_domain_id or any(
        type(value) is not int
        for value in (
            capture.exposure_start_ns,
            capture.exposure_end_ns,
            capture.clock_uncertainty_ns,
        )
    ):
        raise ValueError("capture time requires a clock and integer nanoseconds")
    if capture.exposure_end_ns < capture.exposure_start_ns or capture.clock_uncertainty_ns < 0:
        raise ValueError("invalid exposure interval or clock uncertainty")
    return (capture.exposure_start_ns + capture.exposure_end_ns) // 2


def _frame_ref(frame: m.CameraFrame) -> m.FrameRef:
    return m.FrameRef(
        station_id=frame.station_id,
        station_session_id=frame.station_session_id,
        camera_role=frame.camera_role,
        frame_id=frame.frame_id,
    )


def _positive(value: float) -> bool:
    return not isinstance(value, bool) and math.isfinite(value) and value > 0


def validate_detection_policy(policy: d.DetectionPolicy) -> None:
    if policy.algorithm_id != MOG2_ALGORITHM_ID or policy.score_definition != FILL_RATIO_SCORE:
        raise ValueError("MOG2 requires its algorithm and fill-ratio score definitions")
    if not _positive(policy.minimum_component_area_px2) or (
        policy.maximum_component_area_px2 is not None
        and (
            not _positive(policy.maximum_component_area_px2)
            or policy.maximum_component_area_px2 < policy.minimum_component_area_px2
        )
    ):
        raise ValueError("invalid component area limits")
    warmup = policy.background_warmup_frame_count
    history = policy.background_history_frame_count
    threshold = policy.background_variance_threshold
    rate = policy.background_learning_rate
    shadows = policy.background_detect_shadows
    if (
        type(warmup) is not int
        or warmup < 0
        or type(history) is not int
        or not 0 < history <= 2_147_483_647  # OpenCV history is a signed 32-bit integer.
        or threshold is None
        or not _positive(threshold)
        or rate is None
        or isinstance(rate, bool)
        or not math.isfinite(rate)
        or not 0 <= rate <= 1
        or not isinstance(shadows, bool)
    ):
        raise ValueError("MOG2 background parameters must all be explicitly configured")


def validate_tracking_policy(policy: d.Tracking2DPolicy) -> None:
    if (
        policy.algorithm_id != TRACKING_ALGORITHM_ID
        or policy.quality_definition != TRACK_QUALITY_SCORE
        or not _positive(policy.association_gate_px)
        or type(policy.confirmation_frame_count) is not int
        or policy.confirmation_frame_count <= 0
        or type(policy.maximum_missing_duration_ns) is not int
        or policy.maximum_missing_duration_ns <= 0
    ):
        raise ValueError("invalid local tracking policy or quality definition")


class MotionDetectionModule(ModuleShell):
    """One grayscale background model, bound by the first valid main-frame metadata.

    The first N successfully processed frames train the model but emit no candidates.
    Malformed pixels fail that exposure without training; wrong streams/times raise
    before any state changes. Failed exposures still interrupt tracking hit streaks.
    Shadow mask values are not foreground. Eight-connected components use inclusive
    area gates; scores measure component/bounding-box fill, not confidence. Centroid
    measurement uncertainty remains unknown, regardless of component shape moments.
    """

    business_implemented = True

    def __init__(self, *, policy: d.DetectionPolicy) -> None:
        self.policy = policy
        self._model: cv2.BackgroundSubtractorMOG2 | None = None
        self._stream: tuple[object, ...] | None = None
        self._last_time_ns: int | None = None
        self._frame_count = 0
        self._backend_failed = False

    def _create_model(self) -> cv2.BackgroundSubtractorMOG2:
        policy = self.policy
        validate_detection_policy(policy)
        history = policy.background_history_frame_count
        threshold = policy.background_variance_threshold
        shadows = policy.background_detect_shadows
        assert history is not None and threshold is not None and shadows is not None
        return cv2.createBackgroundSubtractorMOG2(
            history=history, varThreshold=threshold, detectShadows=shadows
        )

    @staticmethod
    def _failed(frame: m.CameraFrame, *reasons: str) -> d.Candidate2DBatch:
        return d.Candidate2DBatch(
            main_frame_ref=_frame_ref(frame),
            capture_time=frame.capture_time,
            processing_status=m.ProcessingStatus.FAILED,
            candidates=(),
            reason_codes=reasons,
        )

    def detect(self, frame: m.CameraFrame) -> d.Candidate2DBatch:
        if frame.camera_role is not m.CameraRole.MAIN:
            raise ValueError("main camera frame required")
        if not all(
            (frame.station_id, frame.station_session_id, frame.frame_id, frame.camera_config_ref)
        ):
            raise ValueError("invalid main frame identity")
        midpoint = _midpoint(frame.capture_time)
        if frame.image.width_px <= 0 or frame.image.height_px <= 0:
            raise ValueError("invalid main image dimensions")
        stream = (
            frame.station_id,
            frame.station_session_id,
            frame.camera_config_ref,
            frame.capture_time.clock_domain_id,
            (frame.image.height_px, frame.image.width_px),
            frame.image.pixel_format,
        )
        if self._stream is not None and stream != self._stream:
            raise ValueError("main stream mismatch")
        if self._last_time_ns is not None and midpoint <= self._last_time_ns:
            raise ValueError("main exposure must increase strictly")
        self._stream = stream
        self._last_time_ns = midpoint
        try:
            gray = _image_to_gray(frame.image)
        except ValueError as error:
            return self._failed(frame, "invalid_main_frame", str(error))
        except cv2.error:
            logger.exception("Main image conversion failed")
            return self._failed(frame, "image_conversion_failed")
        if self._backend_failed:
            return self._failed(frame, "background_model_unavailable_after_failure")
        try:
            if self._model is None:
                self._model = self._create_model()
            rate = self.policy.background_learning_rate
            assert rate is not None
            mask = self._model.apply(gray, learningRate=rate)
            count, _, stats, centroids = cv2.connectedComponentsWithStats(
                (mask == 255).astype("uint8"), connectivity=8
            )
        except cv2.error:
            # OpenCV may already have mutated the model; do not silently reset/retry it.
            self._model = None
            self._backend_failed = True
            logger.exception("MOG2 foreground processing failed")
            return self._failed(frame, "background_processing_failed")
        self._frame_count += 1
        warmup = self.policy.background_warmup_frame_count
        assert warmup is not None
        candidates = []
        if self._frame_count > warmup:
            for label in range(1, count):
                area = int(stats[label, cv2.CC_STAT_AREA])
                if area < self.policy.minimum_component_area_px2 or (
                    self.policy.maximum_component_area_px2 is not None
                    and area > self.policy.maximum_component_area_px2
                ):
                    continue
                width = int(stats[label, cv2.CC_STAT_WIDTH])
                height = int(stats[label, cv2.CC_STAT_HEIGHT])
                candidates.append(
                    d.Candidate2D(
                        centroid_px=(float(centroids[label, 0]), float(centroids[label, 1])),
                        centroid_covariance_px2=None,
                        detection_score=area / (width * height),
                        detection_score_definition=FILL_RATIO_SCORE,
                    )
                )
        return d.Candidate2DBatch(
            main_frame_ref=_frame_ref(frame),
            capture_time=frame.capture_time,
            processing_status=m.ProcessingStatus.PROCESSED,
            candidates=tuple(sorted(candidates, key=lambda item: item.centroid_px)),
            reason_codes=("background_warmup",)
            if self._frame_count <= warmup
            else ()
            if candidates
            else ("no_foreground_candidates",),
        )


@dataclass
class _Track:
    local_id: m.LocalTrackId
    centroid: m.Vec2Px
    time_ns: int
    velocity: m.Vec2Px | None = None
    consecutive_hits: int = 1
    confirmed: bool = False

    def predict(self, time_ns: int) -> m.Vec2Px:
        if self.velocity is None:
            return self.centroid
        dt = (time_ns - self.time_ns) / 1e9
        return self.centroid[0] + self.velocity[0] * dt, self.centroid[1] + self.velocity[1] * dt


class Tracking2DModule(ModuleShell):
    """Greedy distance-ordered one-to-one matching, not global identity recovery.

    Velocity uses the last two actual detections. Missing frames reset the hit streak,
    but not an already confirmed identity. Quality is min(streak/confirmation_count, 1),
    zero while missing; it is neither a probability nor a positional uncertainty.
    """

    business_implemented = True

    def __init__(
        self,
        *,
        station_id: m.StationId,
        station_session_id: m.SessionId,
        policy: d.Tracking2DPolicy,
    ) -> None:
        self.station_id = station_id
        self.station_session_id = station_session_id
        self.policy = policy
        self._tracks: dict[m.LocalTrackId, _Track] = {}
        self._next_id = 1
        self._clock: str | None = None
        self._last_time_ns: int | None = None

    def _validate(self, batch: d.Candidate2DBatch) -> int:
        validate_tracking_policy(self.policy)
        ref = batch.main_frame_ref
        if (ref.station_id, ref.station_session_id, ref.camera_role) != (
            self.station_id,
            self.station_session_id,
            m.CameraRole.MAIN,
        ) or not ref.frame_id:
            raise ValueError("local tracking station/session/main frame mismatch")
        midpoint = _midpoint(batch.capture_time)
        if self._clock is not None and batch.capture_time.clock_domain_id != self._clock:
            raise ValueError("local tracking clock mismatch")
        if self._last_time_ns is not None and midpoint <= self._last_time_ns:
            raise ValueError("local tracking exposure must increase strictly")
        for candidate in batch.candidates:
            if (
                len(candidate.centroid_px) != 2
                or not all(
                    math.isfinite(value) and not isinstance(value, bool)
                    for value in candidate.centroid_px
                )
                or isinstance(candidate.detection_score, bool)
                or not math.isfinite(candidate.detection_score)
                or not 0 <= candidate.detection_score <= 1
            ):
                raise ValueError("invalid candidate centroid or detection score")
            if candidate.centroid_covariance_px2 is not None:
                covariance_factor(
                    candidate.centroid_covariance_px2, 2, "candidate pixel covariance"
                )
        if batch.processing_status is m.ProcessingStatus.FAILED:
            if batch.candidates:
                raise ValueError("a failed candidate batch cannot contain detections")
        elif batch.processing_status is not m.ProcessingStatus.PROCESSED:
            raise ValueError("unknown candidate processing status")
        return midpoint

    def _state(
        self, track: _Track, batch: d.Candidate2DBatch, time_ns: int, lifecycle: str
    ) -> d.Track2DState:
        return d.Track2DState(
            main_frame_ref=batch.main_frame_ref,
            local_track_id=track.local_id,
            capture_time=batch.capture_time,
            centroid_px=track.predict(time_ns),
            velocity_pxps=track.velocity,
            lifecycle=lifecycle,
            track_quality=min(1.0, track.consecutive_hits / self.policy.confirmation_frame_count),
            track_quality_definition=TRACK_QUALITY_SCORE,
        )

    def update(self, candidates: d.Candidate2DBatch) -> d.Tracking2DResult:
        time_ns = self._validate(candidates)
        states = []
        for local_id, track in tuple(self._tracks.items()):
            if time_ns - track.time_ns > self.policy.maximum_missing_duration_ns:
                track.consecutive_hits = 0
                states.append(self._state(track, candidates, time_ns, "deleted"))
                del self._tracks[local_id]
        pairs = []
        for local_id, track in self._tracks.items():
            predicted = track.predict(time_ns)
            for index, candidate in enumerate(candidates.candidates):
                distance = math.hypot(
                    candidate.centroid_px[0] - predicted[0], candidate.centroid_px[1] - predicted[1]
                )
                if distance <= self.policy.association_gate_px:
                    pairs.append((distance, local_id, index))
        assigned: dict[int, _Track] = {}
        used = set()
        for _, local_id, index in sorted(pairs):
            if local_id not in used and index not in assigned:
                track = self._tracks[local_id]
                centroid = candidates.candidates[index].centroid_px
                dt = (time_ns - track.time_ns) / 1e9
                track.velocity = (
                    (centroid[0] - track.centroid[0]) / dt,
                    (centroid[1] - track.centroid[1]) / dt,
                )
                track.centroid, track.time_ns = centroid, time_ns
                track.consecutive_hits += 1
                track.confirmed |= track.consecutive_hits >= self.policy.confirmation_frame_count
                used.add(local_id)
                assigned[index] = track
        for local_id, track in self._tracks.items():
            if local_id not in used:
                track.consecutive_hits = 0
                states.append(self._state(track, candidates, time_ns, "coasting"))
        observations = []
        for index, candidate in enumerate(candidates.candidates):
            if index not in assigned:
                track = _Track(
                    local_id=m.LocalTrackId(f"local-{self._next_id}"),
                    centroid=candidate.centroid_px,
                    time_ns=time_ns,
                    confirmed=self.policy.confirmation_frame_count == 1,
                )
                self._next_id += 1
                self._tracks[track.local_id] = track
            else:
                track = assigned[index]
            state = self._state(
                track, candidates, time_ns, "confirmed" if track.confirmed else "tentative"
            )
            states.append(state)
            observations.append(
                m.Observation2D(
                    main_frame_ref=candidates.main_frame_ref,
                    capture_time=candidates.capture_time,
                    centroid_px=candidate.centroid_px,
                    centroid_covariance_px2=candidate.centroid_covariance_px2,
                    detection_score=candidate.detection_score,
                    detection_score_definition=candidate.detection_score_definition,
                    local_track_id=track.local_id,
                    track_quality=state.track_quality,
                    track_quality_definition=state.track_quality_definition,
                )
            )
        self._clock = candidates.capture_time.clock_domain_id
        self._last_time_ns = time_ns
        return d.Tracking2DResult(observations=tuple(observations), tracks=tuple(states))


@dataclass(frozen=True, kw_only=True)
class MainDetectionModule(ModuleShell):
    business_implemented = True
    detector: MotionDetector
    tracker: Tracker2D

    def process(self, frame: m.CameraFrame) -> d.MainDetectionResult:
        candidates = self.detector.detect(frame)
        tracked = self.tracker.update(candidates)
        return d.MainDetectionResult(
            detections=m.DetectionBatch(
                main_frame_ref=candidates.main_frame_ref,
                capture_time=candidates.capture_time,
                processing_status=candidates.processing_status,
                observations=tracked.observations,
                reason_codes=candidates.reason_codes,
            ),
            tracks=tracked.tracks,
        )

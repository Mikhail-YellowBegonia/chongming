"""Workbench stages adapted to device ports; no truth or scene inputs."""

from __future__ import annotations

import math

import cv2
import numpy as np

from chongming.contracts import device as d
from chongming.contracts import model as m
from chongming.modules.base import ModuleShell
from chongming.modules.detection import (
    FILL_RATIO_SCORE,
    TRACK_QUALITY_SCORE,
    MotionDetectionModule,
    Tracking2DModule,
    _frame_ref,
    _midpoint,
)
from chongming.vision.blob import BlobExtractionConfig, BlobExtractionStage
from chongming.vision.mog2 import MOG2Config, MOG2Stage
from chongming.vision.morphology import MorphologicalConfig, MorphologicalFilterStage
from chongming.vision.motion import FrameDiffConfig, FrameDifferenceStage
from chongming.vision.occupancy import OccupancyConfig, OccupancyFilterStage
from chongming.vision.pipeline import Blob, PipelineContext, process_stages
from chongming.vision.preprocess import PreprocessConfig, PreprocessStage
from chongming.vision.tracking import TrackingConfig, TrackingStage

DETECTION_ID = "frame-difference-occupancy-v1"
MOG2_ID = "mog2-workbench-v1"
TRACKING_ID = "workbench-kalman-v1"


def _image_to_bgr(image: m.ImagePlane) -> np.ndarray:
    """Preserve color until the workbench's post-downsample grayscale conversion."""
    if image.width_px <= 0 or image.height_px <= 0:
        raise ValueError("image dimensions must be positive")
    if image.pixel_format not in (m.PixelFormat.GRAY8, m.PixelFormat.RGB8, m.PixelFormat.BGR8):
        raise ValueError("unsupported workbench pixel format")
    channels = 1 if image.pixel_format == m.PixelFormat.GRAY8 else 3
    if image.row_stride_bytes < image.width_px * channels:
        raise ValueError("image row stride is smaller than the packed row")
    raw = memoryview(image.pixels).cast("B")
    if len(raw) < image.row_stride_bytes * image.height_px:
        raise ValueError("image pixel buffer is shorter than its declared stride")
    pixels = np.ndarray(
        (image.height_px, image.width_px, channels),
        dtype=np.uint8,
        buffer=raw,
        strides=(image.row_stride_bytes, channels, 1),
    )
    if channels == 1:
        return cv2.cvtColor(np.ascontiguousarray(pixels[:, :, 0]), cv2.COLOR_GRAY2BGR)
    pixels = np.ascontiguousarray(pixels)
    return (
        cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR)
        if image.pixel_format == m.PixelFormat.RGB8
        else pixels
    )


def validate_workbench_detection(policy: d.FrameDifferencePolicy) -> None:
    if (
        policy.algorithm_id != (MOG2_ID if policy.mode == "mog2" else DETECTION_ID)
    ) or policy.mode not in (
        "two_frame",
        "three_frame",
        "five_frame",
        "mog2",
    ):
        raise ValueError("unsupported workbench detector")
    if type(policy.apply_occupancy) is not bool:
        raise ValueError("apply_occupancy must be boolean")
    if policy.mode == "mog2":
        mog2_config(policy)
    if type(policy.downsample_factor) is not int or policy.downsample_factor not in (1, 2, 4):
        raise ValueError("downsample_factor must be 1, 2 or 4")
    for size in (
        policy.morphology_kernel_size,
        policy.occupancy_dilation_size,
        policy.blob_dilation_size,
    ):
        if type(size) is not int or size < 0:
            raise ValueError("kernel sizes must be nonnegative integers")
    if policy.morphology_kernel_size < 1 or policy.morphology_kernel_size % 2 == 0:
        raise ValueError("morphology kernel must be positive and odd")
    if type(policy.apply_opening) is not bool or type(policy.apply_closing) is not bool:
        raise ValueError("morphology switches must be boolean")
    for value in (
        policy.threshold,
        policy.score_increment,
        policy.score_decay,
        policy.mask_threshold,
        policy.minimum_component_area_px2,
    ):
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            raise ValueError("workbench thresholds and rates must be finite and positive")
    if policy.maximum_component_area_px2 is not None and (
        not math.isfinite(policy.maximum_component_area_px2)
        or policy.maximum_component_area_px2 < policy.minimum_component_area_px2
    ):
        raise ValueError("invalid maximum component area")


def mog2_config(policy: d.FrameDifferencePolicy) -> MOG2Config:
    if (
        policy.mog2_history is None
        or policy.mog2_variance_threshold is None
        or policy.mog2_learning_rate is None
        or policy.mog2_detect_shadows is None
    ):
        raise ValueError("MOG2 parameters must be explicit")
    config = MOG2Config(
        policy.mog2_history,
        policy.mog2_variance_threshold,
        policy.mog2_learning_rate,
        policy.mog2_detect_shadows,
    )
    return config


def validate_workbench_tracking(policy: d.KalmanTrackingPolicy) -> None:
    if policy.algorithm_id != TRACKING_ID:
        raise ValueError("unsupported workbench tracker")
    if (
        type(policy.confirmation_frame_count) is not int
        or policy.confirmation_frame_count < 1
        or type(policy.maximum_lost_frame_count) is not int
        or policy.maximum_lost_frame_count < 0
    ):
        raise ValueError("invalid workbench track lifecycle limits")
    for value in (
        policy.association_gate_px,
        policy.covariance_floor_px,
        policy.process_noise_position,
        policy.process_noise_velocity,
        policy.measurement_noise_px,
    ):
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            raise ValueError("Kalman parameters must be finite and positive")


class WorkbenchMotionModule(ModuleShell):
    business_implemented = True

    def __init__(self, *, policy: d.FrameDifferencePolicy) -> None:
        validate_workbench_detection(policy)
        self.policy = policy
        self.stages = [
            PreprocessStage(PreprocessConfig(downsample_factor=policy.downsample_factor)),
            MOG2Stage(mog2_config(policy))
            if policy.mode == "mog2"
            else FrameDifferenceStage(
                FrameDiffConfig(mode=policy.mode, threshold=policy.threshold)
            ),
            MorphologicalFilterStage(
                MorphologicalConfig(
                    kernel_size=policy.morphology_kernel_size,
                    apply_opening=policy.apply_opening,
                    apply_closing=policy.apply_closing,
                )
            ),
            OccupancyFilterStage(
                OccupancyConfig(
                    enabled=policy.apply_occupancy,
                    score_increment=policy.score_increment,
                    score_decay=policy.score_decay,
                    mask_threshold=policy.mask_threshold,
                    dilate_background_mask=policy.occupancy_dilation_size > 0,
                    dilation_kernel_size=policy.occupancy_dilation_size,
                )
            ),
            BlobExtractionStage(
                BlobExtractionConfig(
                    min_area_px2=policy.minimum_component_area_px2,
                    max_area_px2=policy.maximum_component_area_px2,
                    apply_dilation=policy.blob_dilation_size > 0,
                    dilation_kernel_size=policy.blob_dilation_size,
                )
            ),
        ]
        self.stage_timings_ms: dict[str, float] = {}
        self._stream: tuple[object, ...] | None = None
        self._last_ns: int | None = None

    def detect(self, frame: m.CameraFrame) -> d.Candidate2DBatch:
        time_ns = _midpoint(frame.capture_time)
        if frame.camera_role != m.CameraRole.MAIN or not all(
            (frame.station_id, frame.station_session_id, frame.frame_id, frame.camera_config_ref)
        ):
            raise ValueError("valid main frame identity required")
        stream = (
            frame.station_id,
            frame.station_session_id,
            frame.camera_config_ref,
            frame.capture_time.clock_domain_id,
            frame.image.width_px,
            frame.image.height_px,
            frame.image.pixel_format,
        )
        if self._stream is not None and stream != self._stream:
            raise ValueError("main stream mismatch")
        if self._last_ns is not None and time_ns <= self._last_ns:
            raise ValueError("main exposure must increase strictly")
        self._stream, self._last_ns = stream, time_ns
        # Invalid image bytes do not train adaptive stages.
        try:
            bgr = _image_to_bgr(frame.image)
        except (ValueError, cv2.error):
            return d.Candidate2DBatch(
                main_frame_ref=_frame_ref(frame),
                capture_time=frame.capture_time,
                processing_status=m.ProcessingStatus.FAILED,
                candidates=(),
                reason_codes=("invalid_main_frame",),
            )
        context = PipelineContext(frame=bgr, timestamp=time_ns / 1e9)
        process_stages(self.stages, context)
        self.stage_timings_ms = context.debug_data["stage_timings_ms"]
        scale = self.policy.downsample_factor
        candidates = tuple(
            d.Candidate2D(
                centroid_px=(blob.centroid_px[0] * scale, blob.centroid_px[1] * scale),
                centroid_covariance_px2=None,
                detection_score=min(
                    1.0, blob.area_px2 / (blob.bounding_box[2] * blob.bounding_box[3])
                ),
                detection_score_definition=FILL_RATIO_SCORE,
            )
            for blob in context.blobs
        )
        return d.Candidate2DBatch(
            main_frame_ref=_frame_ref(frame),
            capture_time=frame.capture_time,
            processing_status=m.ProcessingStatus.PROCESSED,
            candidates=candidates,
            reason_codes=() if candidates else ("no_foreground_candidates",),
        )


class WorkbenchTrackingModule(ModuleShell):
    business_implemented = True

    def __init__(
        self,
        *,
        station_id: m.StationId,
        station_session_id: m.SessionId,
        policy: d.KalmanTrackingPolicy,
        image_scale: int,
    ) -> None:
        validate_workbench_tracking(policy)
        self.image_scale = image_scale
        self.station_id, self.station_session_id = station_id, station_session_id
        self.stage = TrackingStage(
            TrackingConfig(
                confirm_frames=policy.confirmation_frame_count,
                max_lost_frames=policy.maximum_lost_frame_count,
                association_gate_px=policy.association_gate_px,
                covariance_floor=policy.covariance_floor_px,
                process_noise_pos=policy.process_noise_position,
                process_noise_vel=policy.process_noise_velocity,
                measurement_noise=policy.measurement_noise_px,
            )
        )

    def update(self, candidates: d.Candidate2DBatch) -> d.Tracking2DResult:
        ref = candidates.main_frame_ref
        if (ref.station_id, ref.station_session_id, ref.camera_role) != (
            self.station_id,
            self.station_session_id,
            m.CameraRole.MAIN,
        ):
            raise ValueError("tracker station/session/main frame mismatch")
        # The tracker consumes centroids only. A unit box preserves each detector's score.
        context = PipelineContext(
            frame=np.empty((0, 0, 3), dtype=np.uint8),
            timestamp=_midpoint(candidates.capture_time) / 1e9,
        )
        context.blobs = [
            Blob(
                centroid_px=(
                    c.centroid_px[0] / self.image_scale,
                    c.centroid_px[1] / self.image_scale,
                ),
                area_px2=c.detection_score,
                bounding_box=(0, 0, 1, 1),
            )
            for c in candidates.candidates
        ]
        self.stage.process(context)
        observations = tuple(
            m.Observation2D(
                main_frame_ref=ref,
                capture_time=candidates.capture_time,
                centroid_px=(
                    item.measurement_px[0] * self.image_scale,
                    item.measurement_px[1] * self.image_scale,
                ),
                centroid_covariance_px2=None,
                detection_score=item.measurement_score,
                detection_score_definition=FILL_RATIO_SCORE,
                local_track_id=m.LocalTrackId(str(item.track_id)),
                track_quality=None,
                track_quality_definition=None,
            )
            for item in context.detections
        )
        tracks = tuple(
            d.Track2DState(
                main_frame_ref=ref,
                capture_time=candidates.capture_time,
                local_track_id=m.LocalTrackId(str(track.track_id)),
                centroid_px=(
                    track.kalman.get_position()[0] * self.image_scale,
                    track.kalman.get_position()[1] * self.image_scale,
                ),
                velocity_pxps=(
                    track.kalman.get_velocity()[0] * self.image_scale,
                    track.kalman.get_velocity()[1] * self.image_scale,
                ),
                lifecycle=track.lifecycle,
                track_quality=min(1.0, track.consecutive_hits / self.stage.config.confirm_frames),
                track_quality_definition=TRACK_QUALITY_SCORE,
            )
            for track in self.stage.tracks
        )
        return d.Tracking2DResult(observations=observations, tracks=tracks)


def image_modules(
    detection: d.DetectionPolicy | d.FrameDifferencePolicy,
    tracking: d.Tracking2DPolicy | d.KalmanTrackingPolicy,
    station_id: m.StationId,
    session_id: m.SessionId,
) -> tuple[
    WorkbenchMotionModule | MotionDetectionModule, WorkbenchTrackingModule | Tracking2DModule
]:
    """Select one explicit algorithm family, shared by device and offline execution."""
    if isinstance(detection, d.FrameDifferencePolicy):
        if not isinstance(tracking, d.KalmanTrackingPolicy):
            raise TypeError("workbench detection requires workbench Kalman tracking")
        return WorkbenchMotionModule(policy=detection), WorkbenchTrackingModule(
            station_id=station_id,
            station_session_id=session_id,
            policy=tracking,
            image_scale=detection.downsample_factor,
        )
    if isinstance(tracking, d.KalmanTrackingPolicy):
        raise TypeError("MOG2 requires its local tracking policy")
    return MotionDetectionModule(policy=detection), Tracking2DModule(
        station_id=station_id, station_session_id=session_id, policy=tracking
    )

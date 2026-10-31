"""OpenCV-backed auxiliary-camera calibration.

The public port contracts deliberately do not depend on OpenCV.  This module is
the replaceable reference backend for the first implementation: a surveyed,
planar checkerboard is detected in the auxiliary image and a small rotation
correction is estimated relative to the installation pose.

Only the rotation is updated.  The camera centre remains the configured main
camera centre, as required by the stage-one calibration contract.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache

import cv2
import numpy as np

from chongming.contracts.device import (
    CalibrationAttempt,
    CalibrationAttemptStatus,
    CalibrationEstimate,
    CalibrationPolicy,
    ReferenceObservation,
    ReferenceObservationBatch,
)
from chongming.contracts.model import (
    CalibrationHealth,
    CalibrationQuality,
    CalibrationSnapshot,
    CalibrationSolution,
    CalibrationSolutionId,
    CalibrationSolutionRef,
    CalibrationState,
    CalibrationStateEvent,
    CalibrationUpdate,
    CalibrationUsage,
    CameraFrame,
    CameraRole,
    CaptureTime,
    ConfigDigest,
    DiagnosticEvent,
    EventType,
    FrameRef,
    ImagePlane,
    ProcessingStatus,
    ProcessingTime,
    Rotation3x3,
    ScoreDefinition,
    ScoreKind,
    SessionId,
    StationConfig,
    StationId,
    SurveyedReference,
    TimeInterval,
    Vec3M,
)
from chongming.ports.device import (
    CalibrationEstimator,
    CalibrationHistory,
    CalibrationStateManager,
    EventSequencer,
    ReferenceRecognizer,
)

_INNER_CORNER_PATTERN = re.compile(
    r"(?:^|[^a-z0-9])(?:inner[-_ ]*)?(?:r|row)[-_ ]*(\d+)"
    r"[^a-z0-9]+(?:c|col|column)[-_ ]*(\d+)(?:$|[^a-z0-9])",
    re.IGNORECASE,
)
_INDEXED_CORNER = re.compile(r"(?:corner|point|landmark)[-_ ]*(\d+)$", re.IGNORECASE)
_EPS = 1e-10


def _score_definition() -> ScoreDefinition:
    return ScoreDefinition(
        definition_id="opencv-checkerboard-corner",
        kind=ScoreKind.RANKING,
        minimum=0.0,
        maximum=1.0,
        cross_station_comparison_group=None,
    )


@dataclass(frozen=True, kw_only=True)
class CheckerboardGeometry:
    """Geometry of the first virtual and physical calibration panel.

    ``columns`` and ``rows`` count *inner* corners.  Four surveyed outer
    corners are enough to recover the metric coordinates of all inner corners;
    no scene-generation truth is needed by the estimator.
    """

    columns: int = 6
    rows: int = 4
    board_width_m: float = 0.297
    board_height_m: float = 0.210
    square_pitch_x_m: float = 0.035
    square_pitch_y_m: float = 0.035
    corner_landmark_ids: tuple[str, str, str, str] | None = None

    @property
    def expected_count(self) -> int:
        return self.columns * self.rows

    def validate(self) -> None:
        if self.columns < 2 or self.rows < 2:
            raise ValueError("checkerboard must contain at least 2 by 2 inner corners")
        if (
            min(
                self.board_width_m,
                self.board_height_m,
                self.square_pitch_x_m,
                self.square_pitch_y_m,
            )
            <= 0
        ):
            raise ValueError("checkerboard dimensions must be positive")
        if (self.columns + 1) * self.square_pitch_x_m > self.board_width_m or (
            self.rows + 1
        ) * self.square_pitch_y_m > self.board_height_m:
            raise ValueError("checkerboard cells extend beyond the surveyed panel")


@dataclass(frozen=True, kw_only=True)
class _PointCandidate:
    rotation_enu_from_aux: np.ndarray
    camera_from_enu: np.ndarray
    translation_camera_m: np.ndarray
    image_points: np.ndarray
    world_points: np.ndarray
    reprojection_rmse_px: float
    rotation_only_parallax_px: float | None
    correction_angle_rad: float
    main_rotation_enu: np.ndarray


def _as_np_matrix(matrix: Rotation3x3) -> np.ndarray:
    return np.asarray(matrix, dtype=np.float64)


def _as_rotation(matrix: np.ndarray) -> Rotation3x3:
    values = np.asarray(matrix, dtype=np.float64)
    return tuple(tuple(float(values[row, col]) for col in range(3)) for row in range(3))  # type: ignore[return-value]


def _as_vec3(values: np.ndarray) -> Vec3M:
    return tuple(float(values[index]) for index in range(3))  # type: ignore[return-value]


def _capture_midpoint(capture_time: CaptureTime) -> int:
    if capture_time.exposure_end_ns < capture_time.exposure_start_ns:
        raise ValueError("capture exposure end precedes start")
    return (capture_time.exposure_start_ns + capture_time.exposure_end_ns) // 2


def _is_finite_matrix(values: np.ndarray) -> bool:
    return bool(np.isfinite(values).all())


def _rotation_angle(rotation: np.ndarray) -> float:
    trace = float(np.trace(rotation))
    cosine = max(-1.0, min(1.0, (trace - 1.0) * 0.5))
    return math.acos(cosine)


def _rotation_error(reference: np.ndarray, observed: np.ndarray) -> float:
    return _rotation_angle(reference.T @ observed)


def _image_to_gray(image: ImagePlane) -> np.ndarray:
    """Create a contiguous grayscale view while respecting row stride."""

    if image.width_px <= 0 or image.height_px <= 0:
        raise ValueError("image dimensions must be positive")
    channels = {"gray8": 1, "rgb8": 3, "bgr8": 3}.get(image.pixel_format.value)
    if channels is None:
        raise ValueError(f"unsupported pixel format: {image.pixel_format}")
    minimum_stride = image.width_px * channels
    if image.row_stride_bytes < minimum_stride:
        raise ValueError("image row stride is smaller than the packed row")
    raw = memoryview(image.pixels).cast("B")
    required = image.row_stride_bytes * image.height_px
    if len(raw) < required:
        raise ValueError("image pixel buffer is shorter than its declared stride")
    pixels = np.ndarray(
        (image.height_px, image.width_px, channels),
        dtype=np.uint8,
        buffer=raw,
        strides=(image.row_stride_bytes, channels, 1),
    )
    if channels == 1:
        return np.ascontiguousarray(pixels[:, :, 0])
    code = cv2.COLOR_RGB2GRAY if image.pixel_format.value == "rgb8" else cv2.COLOR_BGR2GRAY
    return cv2.cvtColor(np.ascontiguousarray(pixels), code)


def _camera_matrix(config: StationConfig, auxiliary: bool = True) -> np.ndarray:
    model = config.auxiliary_camera if auxiliary else config.main_camera
    matrix = _as_np_matrix(model.intrinsics.matrix_px)
    if matrix.shape != (3, 3) or not _is_finite_matrix(matrix):
        raise ValueError("camera intrinsic matrix must be finite 3 by 3")
    if abs(float(matrix[2, 2])) < _EPS:
        raise ValueError("camera intrinsic matrix has a zero homogeneous scale")
    return matrix


def _distortion_coefficients(config: StationConfig, auxiliary: bool = True) -> np.ndarray:
    camera = config.auxiliary_camera if auxiliary else config.main_camera
    distortion = camera.distortion
    model = distortion.model.lower().replace("-", "_")
    if model in {"none", "pinhole", "zero"}:
        return np.zeros((5, 1), dtype=np.float64)
    if model in {"brown_conrady", "brownconrady", "opencv"}:
        coefficients = tuple(float(value) for value in distortion.coefficients)
        if len(coefficients) == 4:
            # OpenCV's first four Brown-Conrady terms are k1, k2, p1, p2.
            coefficients = (*coefficients, 0.0)
        if len(coefficients) not in {5, 8, 12, 14}:
            raise ValueError("Brown-Conrady distortion needs 4, 5, 8, 12 or 14 coefficients")
        return np.asarray(coefficients, dtype=np.float64).reshape(-1, 1)
    raise ValueError(f"unsupported camera distortion model: {distortion.model}")


def _landmark_corners(
    reference: SurveyedReference,
    geometry: CheckerboardGeometry,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    landmarks = reference.landmarks
    if geometry.corner_landmark_ids is not None:
        by_id = {landmark.landmark_id: landmark for landmark in landmarks}
        try:
            selected = tuple(by_id[landmark_id] for landmark_id in geometry.corner_landmark_ids)
        except KeyError as error:
            raise ValueError(
                f"configured checkerboard corner is not surveyed: {error.args[0]}"
            ) from error
    else:
        indexed = []
        for landmark in landmarks:
            match = _INDEXED_CORNER.search(landmark.landmark_id)
            if match is not None:
                indexed.append((int(match.group(1)), landmark))
        if len(indexed) >= 4 and {index for index, _ in indexed} >= {0, 1, 2, 3}:
            by_index = {index: landmark for index, landmark in indexed}
            selected = tuple(by_index[index] for index in range(4))
        elif len(landmarks) == 4:
            selected = tuple(landmarks)
        else:
            raise ValueError("a surveyed checkerboard needs four ordered outer corners")
    points = tuple(np.asarray(landmark.position_enu_m, dtype=np.float64) for landmark in selected)
    if any(point.shape != (3,) or not _is_finite_matrix(point) for point in points):
        raise ValueError("surveyed checkerboard corners must be finite ENU points")
    p00, p10, p11, p01 = points
    first = p10 - p00
    second = p01 - p00
    if np.linalg.norm(np.cross(first, second)) < _EPS:
        raise ValueError("surveyed checkerboard corners are geometrically degenerate")
    if np.linalg.norm(p11 - (p10 + p01 - p00)) > 0.001:
        raise ValueError("surveyed checkerboard must be a planar parallelogram")
    return p00, p10, p11, p01


def _bilinear_corner(
    corners: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    row: int,
    column: int,
    geometry: CheckerboardGeometry,
) -> np.ndarray:
    p00, p10, p11, p01 = corners
    alpha = (
        0.5
        + (column - (geometry.columns - 1) * 0.5)
        * geometry.square_pitch_x_m
        / geometry.board_width_m
    )
    beta = (
        0.5
        + (row - (geometry.rows - 1) * 0.5) * geometry.square_pitch_y_m / geometry.board_height_m
    )
    return (
        (1.0 - alpha) * (1.0 - beta) * p00
        + alpha * (1.0 - beta) * p10
        + alpha * beta * p11
        + (1.0 - alpha) * beta * p01
    )


def _parse_inner_index(reference_id: str) -> tuple[int, int] | None:
    match = _INNER_CORNER_PATTERN.search(reference_id)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _matrix_to_rotation_covariance(rmse_px: float, focal_px: float, count: int) -> Rotation3x3:
    sigma_rad = max(rmse_px / max(focal_px, _EPS), 1e-9)
    variance = sigma_rad * sigma_rad / max(count, 1)
    return (
        (variance, 0.0, 0.0),
        (0.0, variance, 0.0),
        (0.0, 0.0, variance),
    )


@lru_cache(maxsize=1)
def _finite_difference_rotations(epsilon: float) -> tuple[tuple[np.ndarray, np.ndarray], ...]:
    """Cache the three Rodrigues perturbations shared by every pose fit."""
    deltas: list[tuple[np.ndarray, np.ndarray]] = []
    for axis in range(3):
        delta = np.zeros(3, dtype=np.float64)
        delta[axis] = epsilon
        plus, _ = cv2.Rodrigues(delta)
        minus, _ = cv2.Rodrigues(-delta)
        deltas.append((plus, minus))
    return tuple(deltas)


class OpenCVReferenceRecognizer(ReferenceRecognizer):
    """Detect the configured surveyed checkerboard in an auxiliary frame."""

    def __init__(
        self,
        geometry: CheckerboardGeometry | None = None,
        *,
        score_definition: ScoreDefinition | None = None,
    ) -> None:
        self.geometry = geometry or CheckerboardGeometry()
        self.geometry.validate()
        self.score_definition = score_definition or _score_definition()

    def recognize(self, frame: CameraFrame, config: StationConfig) -> ReferenceObservationBatch:
        if frame.camera_role is not CameraRole.AUXILIARY:
            return self._failed(frame, "auxiliary_frame_required")
        if frame.station_id != config.station_id:
            return self._failed(frame, "station_mismatch")
        if frame.camera_config_ref != config.config_digest:
            return self._failed(frame, "camera_configuration_mismatch")
        if not isinstance(config.reference_anchor, SurveyedReference):
            return self._failed(frame, "checkerboard_requires_surveyed_reference")
        try:
            gray = _image_to_gray(frame.image)
        except (TypeError, ValueError) as error:
            return ReferenceObservationBatch(
                auxiliary_frame_ref=self._frame_ref(frame),
                capture_time=frame.capture_time,
                processing_status=ProcessingStatus.FAILED,
                observations=(),
                reason_codes=("image_invalid", str(error)),
            )
        pattern = (self.geometry.columns, self.geometry.rows)
        # The classic detector is substantially cheaper for the fixed,
        # well-lit panel used by the auxiliary stream.  SB remains a robust
        # fallback for low-contrast, perspective-heavy, or unusual frames.
        found, corners = cv2.findChessboardCorners(
            gray,
            pattern,
            flags=0,
        )
        detector_reason = "opencv_find_chessboard_corners_classic"
        if found and corners is not None:
            corners = cv2.cornerSubPix(
                gray,
                corners,
                winSize=(5, 5),
                zeroZone=(-1, -1),
                criteria=(cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.01),
            )
        if not found:
            found, corners = cv2.findChessboardCornersSB(
                gray, pattern, flags=cv2.CALIB_CB_NORMALIZE_IMAGE
            )
            detector_reason = "opencv_find_chessboard_corners_sb"
        if not found:
            found, corners = cv2.findChessboardCornersSB(
                gray,
                pattern,
                flags=cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY,
            )
            detector_reason = "opencv_find_chessboard_corners_sb_exhaustive"
        if not found or corners is None or len(corners) != self.geometry.expected_count:
            return self._failed(frame, "checkerboard_not_found")
        observations = tuple(
            ReferenceObservation(
                reference_id=f"{config.reference_anchor.reference_id}:inner-r{row}-c{column}",
                centroid_px=(float(corners[index, 0, 0]), float(corners[index, 0, 1])),
                centroid_covariance_px2=((0.04**2, 0.0), (0.0, 0.04**2)),
                match_score=1.0,
                match_score_definition=self.score_definition,
            )
            for index, (row, column) in enumerate(
                (row, column)
                for row in range(self.geometry.rows)
                for column in range(self.geometry.columns)
            )
        )
        return ReferenceObservationBatch(
            auxiliary_frame_ref=self._frame_ref(frame),
            capture_time=frame.capture_time,
            processing_status=ProcessingStatus.PROCESSED,
            observations=observations,
            reason_codes=(detector_reason, "subpixel_corners"),
        )

    @staticmethod
    def _frame_ref(frame: CameraFrame) -> FrameRef:
        return FrameRef(
            station_id=frame.station_id,
            station_session_id=frame.station_session_id,
            camera_role=frame.camera_role,
            frame_id=frame.frame_id,
        )

    def _failed(self, frame: CameraFrame, reason: str) -> ReferenceObservationBatch:
        return ReferenceObservationBatch(
            auxiliary_frame_ref=self._frame_ref(frame),
            capture_time=frame.capture_time,
            processing_status=ProcessingStatus.PROCESSED,
            observations=(),
            reason_codes=(reason,),
        )


class OpenCVCalibrationEstimator(CalibrationEstimator):
    """Estimate the low-frequency auxiliary rotation with a surveyed prior."""

    def __init__(
        self,
        policy: CalibrationPolicy,
        geometry: CheckerboardGeometry | None = None,
        *,
        maximum_correction_rad: float = math.radians(1.0),
    ) -> None:
        self.policy = policy
        self.geometry = geometry or CheckerboardGeometry()
        self.geometry.validate()
        if maximum_correction_rad <= 0.0:
            raise ValueError("maximum correction angle must be positive")
        self.maximum_correction_rad = maximum_correction_rad

    def estimate(
        self, references: ReferenceObservationBatch, config: StationConfig, now: ProcessingTime
    ) -> CalibrationAttempt:
        reference_time = references.capture_time
        try:
            _capture_midpoint(reference_time)
        except ValueError as error:
            return self._attempt(
                CalibrationAttemptStatus.FAILED,
                references,
                now,
                ("invalid_capture_time", str(error)),
            )
        if references.auxiliary_frame_ref.station_id != config.station_id:
            return self._attempt(
                CalibrationAttemptStatus.FAILED, references, now, ("station_mismatch",)
            )
        if references.auxiliary_frame_ref.camera_role is not CameraRole.AUXILIARY:
            return self._attempt(
                CalibrationAttemptStatus.FAILED, references, now, ("auxiliary_frame_required",)
            )
        if references.processing_status is not ProcessingStatus.PROCESSED:
            return self._attempt(
                CalibrationAttemptStatus.FAILED,
                references,
                now,
                ("reference_processing_failed", *references.reason_codes),
            )
        minimum = max(4, self.policy.minimum_reference_count)
        if len(references.observations) < minimum:
            return self._attempt(
                CalibrationAttemptStatus.REFERENCE_UNAVAILABLE,
                references,
                now,
                ("insufficient_reference_observations",),
            )
        if not isinstance(config.reference_anchor, SurveyedReference):
            return self._attempt(
                CalibrationAttemptStatus.REFERENCE_UNAVAILABLE,
                references,
                now,
                ("checkerboard_requires_surveyed_reference",),
            )
        try:
            candidate = self._solve(references, config)
        except (TypeError, ValueError, cv2.error) as error:
            return self._attempt(
                CalibrationAttemptStatus.GEOMETRY_INVALID,
                references,
                now,
                ("calibration_geometry_invalid", str(error)),
            )
        if candidate is None:
            return self._attempt(
                CalibrationAttemptStatus.GEOMETRY_INVALID,
                references,
                now,
                ("no_pnp_candidate_within_installation_prior",),
            )
        if candidate.reprojection_rmse_px > self.policy.maximum_reprojection_rmse_px:
            return self._attempt(
                CalibrationAttemptStatus.FAILED,
                references,
                now,
                (
                    "reprojection_rmse_exceeded",
                    f"rmse_px={candidate.reprojection_rmse_px:.6g}",
                ),
            )
        quality = CalibrationQuality(
            source="opencv_checkerboard_pose",
            matched_reference_count=len(references.observations),
            reprojection_rmse_px=candidate.reprojection_rmse_px,
            angular_residual_rad=candidate.correction_angle_rad,
            reason_codes=(
                "rotation_only",
                "installation_prior_selected",
                "positive_depth",
            ),
            rotation_only_parallax_px=candidate.rotation_only_parallax_px,
        )
        estimate = CalibrationEstimate(
            reference_capture_time=reference_time,
            rotation_enu_from_main=_as_rotation(candidate.main_rotation_enu),
            correction_covariance_enu_rad2=_matrix_to_rotation_covariance(
                candidate.reprojection_rmse_px,
                float(np.mean([_camera_matrix(config)[0, 0], _camera_matrix(config)[1, 1]])),
                len(references.observations),
            ),
            quality=quality,
        )
        return self._attempt(
            CalibrationAttemptStatus.ESTIMATED,
            references,
            now,
            quality.reason_codes,
            estimate,
        )

    def _solve(
        self, references: ReferenceObservationBatch, config: StationConfig
    ) -> _PointCandidate | None:
        corners = _landmark_corners(config.reference_anchor, self.geometry)  # type: ignore[arg-type]
        image_points, board_indices = self._image_points(references.observations)
        camera = _camera_matrix(config)
        distortion = _distortion_coefficients(config)
        baseline_main = _as_np_matrix(config.baseline_orientation_enu_from_main)
        fixed_aux_from_main = _as_np_matrix(config.fixed_rotation_aux_from_main)
        if baseline_main.shape != (3, 3) or fixed_aux_from_main.shape != (3, 3):
            raise ValueError("installation rotations must be 3 by 3")
        if not _is_finite_matrix(baseline_main) or not _is_finite_matrix(fixed_aux_from_main):
            raise ValueError("installation rotations must be finite")
        baseline_aux = baseline_main @ fixed_aux_from_main.T
        best: _PointCandidate | None = None
        flip_options = ((False, False), (True, False), (False, True), (True, True))
        for flip_index, (flip_row, flip_column) in enumerate(flip_options):
            world_points = np.asarray(
                [
                    _bilinear_corner(
                        corners,
                        self.geometry.rows - 1 - row if flip_row else row,
                        self.geometry.columns - 1 - column if flip_column else column,
                        self.geometry,
                    )
                    for row, column in board_indices
                ],
                dtype=np.float64,
            )
            if not _is_finite_matrix(world_points):
                continue
            pose_candidates: list[tuple[np.ndarray, np.ndarray]] = []
            prior_pose = self._fit_rotation_with_fixed_main_center(
                world_points,
                image_points,
                camera,
                distortion,
                baseline_aux,
                config,
            )
            if prior_pose is not None:
                pose_candidates.append(prior_pose)
            else:
                solved = cv2.solvePnPGeneric(
                    world_points,
                    image_points,
                    camera,
                    distortion,
                    flags=cv2.SOLVEPNP_IPPE,
                )
                if len(solved) >= 3 and bool(solved[0]):
                    pose_candidates.extend(
                        (cv2.Rodrigues(rvec)[0], np.asarray(tvec, dtype=np.float64).reshape(3))
                        for rvec, tvec in zip(solved[1], solved[2])
                    )
            for rotation_camera_from_enu, translation in pose_candidates:
                camera_points = (rotation_camera_from_enu @ world_points.T).T + translation
                if not _is_finite_matrix(camera_points) or np.any(camera_points[:, 2] <= _EPS):
                    continue
                rvec, _ = cv2.Rodrigues(rotation_camera_from_enu)
                projected, _ = cv2.projectPoints(
                    world_points,
                    rvec,
                    translation,
                    camera,
                    distortion,
                )
                projected = projected.reshape(-1, 2)
                residuals = projected - image_points
                rmse = float(np.sqrt(np.mean(np.sum(residuals * residuals, axis=1))))
                rotation_enu_from_aux = rotation_camera_from_enu.T
                correction = baseline_aux.T @ rotation_enu_from_aux
                correction_angle = _rotation_angle(correction)
                if correction_angle > self.maximum_correction_rad + 1e-7:
                    continue
                main_rotation = baseline_aux @ correction @ fixed_aux_from_main
                main_angle = _rotation_error(baseline_main, main_rotation)
                if main_angle > self.maximum_correction_rad + 1e-7:
                    continue
                parallax = self._rotation_only_parallax(
                    world_points,
                    image_points,
                    camera,
                    distortion,
                    rotation_camera_from_enu,
                    config,
                )
                if (
                    self.policy.maximum_rotation_only_parallax_px is not None
                    and parallax is not None
                    and parallax > self.policy.maximum_rotation_only_parallax_px
                ):
                    continue
                candidate = _PointCandidate(
                    rotation_enu_from_aux=rotation_enu_from_aux,
                    camera_from_enu=rotation_camera_from_enu,
                    translation_camera_m=translation,
                    image_points=image_points,
                    world_points=world_points,
                    reprojection_rmse_px=rmse,
                    rotation_only_parallax_px=parallax,
                    correction_angle_rad=correction_angle,
                    main_rotation_enu=main_rotation,
                )
                # All candidates have already passed the installation prior;
                # reprojection error resolves board-order and planar-PnP
                # ambiguities, with the prior angle as a deterministic tie-breaker.
                score = (rmse, correction_angle)
                if best is None or score < (best.reprojection_rmse_px, best.correction_angle_rad):
                    best = candidate
                    # A normal detector ordering and a small installation
                    # correction are the overwhelmingly common case.  Once
                    # that candidate also meets the policy RMSE, trying the
                    # 180-degree board orderings only burns CPU.  If the
                    # prior rejects it, all remaining flip candidates are
                    # still evaluated as a recovery path.
                    if (
                        flip_index == 0
                        and correction_angle <= self.maximum_correction_rad + 1e-7
                        and rmse <= self.policy.maximum_reprojection_rmse_px
                    ):
                        return best
        return best

    @staticmethod
    def _fit_rotation_with_fixed_main_center(
        world_points: np.ndarray,
        image_points: np.ndarray,
        camera: np.ndarray,
        distortion: np.ndarray,
        baseline_aux: np.ndarray,
        config: StationConfig,
    ) -> tuple[np.ndarray, np.ndarray] | None:
        """Refine rotation with the main centre fixed and the auxiliary rigid baseline.

        A nearly fronto-parallel planar target makes free PnP ill-conditioned:
        tiny corner quantization can trade depth and tilt while keeping the
        reprojection error almost unchanged. The surveyed rigid baseline ties
        the auxiliary centre to rotation about the main centre, leaving only
        the three rotational degrees of freedom the contract permits.
        """

        rigid = config.full_rigid_transform_aux_from_main
        if rigid is None:
            return None
        translation_aux = np.asarray(rigid.translation_a_m, dtype=np.float64)
        if translation_aux.shape != (3,) or not _is_finite_matrix(translation_aux):
            raise ValueError("auxiliary installation translation must be finite 3-vector")
        main_center = np.asarray(config.main_camera_position_enu_m, dtype=np.float64)
        if main_center.shape != (3,) or not _is_finite_matrix(main_center):
            raise ValueError("main camera position must be a finite 3-vector")
        rotation = baseline_aux.T.copy()

        def project(current: np.ndarray) -> np.ndarray:
            # The auxiliary centre rotates around the fixed main-camera centre.
            # ``translation_aux`` is already expressed in the current auxiliary
            # camera frame, so it stays additive in camera coordinates.
            translation = -current @ main_center + translation_aux
            rvec, _ = cv2.Rodrigues(current)
            projected, _ = cv2.projectPoints(
                world_points,
                rvec,
                translation,
                camera,
                distortion,
            )
            return projected.reshape(-1, 2)

        # The installation prior bounds the desired correction to a very small
        # angle, so a handful of local finite-difference Gauss-Newton steps is
        # deterministic and cheaper than an unconstrained six-DoF solve.
        epsilon = 1e-6
        finite_difference_rotations = _finite_difference_rotations(epsilon)
        for _ in range(8):
            residual = (project(rotation) - image_points).reshape(-1)
            jacobian = np.empty((residual.size, 3), dtype=np.float64)
            for axis in range(3):
                plus, minus = finite_difference_rotations[axis]
                jacobian[:, axis] = (
                    (project(plus @ rotation) - project(minus @ rotation)) / (2.0 * epsilon)
                ).reshape(-1)
            step, _, _, _ = np.linalg.lstsq(jacobian, -residual, rcond=None)
            step_norm = float(np.linalg.norm(step))
            if not math.isfinite(step_norm):
                return None
            if step_norm > 0.05:
                step *= 0.05 / step_norm
            if step_norm < 1e-10:
                break
            update, _ = cv2.Rodrigues(step)
            rotation = update @ rotation

        return rotation, -rotation @ main_center + translation_aux

    def _image_points(
        self, observations: Sequence[ReferenceObservation]
    ) -> tuple[np.ndarray, tuple[tuple[int, int], ...]]:
        parsed = [_parse_inner_index(observation.reference_id) for observation in observations]
        if all(item is not None for item in parsed):
            indices = tuple(item for item in parsed if item is not None)
            if len(set(indices)) != len(indices):
                raise ValueError("duplicate checkerboard inner-corner index")
            if any(
                row < 0
                or row >= self.geometry.rows
                or column < 0
                or column >= self.geometry.columns
                for row, column in indices
            ):
                raise ValueError("checkerboard inner-corner index is out of range")
        else:
            if len(observations) != self.geometry.expected_count:
                raise ValueError("unlabelled checkerboard observations need the complete grid")
            order = sorted(
                range(len(observations)),
                key=lambda index: (
                    observations[index].centroid_px[1],
                    observations[index].centroid_px[0],
                ),
            )
            by_position = {
                original_index: (row, column)
                for original_index, (row, column) in zip(
                    order,
                    (
                        (row, column)
                        for row in range(self.geometry.rows)
                        for column in range(self.geometry.columns)
                    ),
                )
            }
            indices = tuple(by_position[index] for index in range(len(observations)))
        points = np.asarray(
            [observation.centroid_px for observation in observations], dtype=np.float64
        )
        if points.shape != (len(observations), 2) or not _is_finite_matrix(points):
            raise ValueError("reference centroids must be finite pixel coordinates")
        return points, indices

    @staticmethod
    def _rotation_only_parallax(
        world_points: np.ndarray,
        image_points: np.ndarray,
        camera: np.ndarray,
        distortion: np.ndarray,
        observed_camera_from_enu: np.ndarray,
        config: StationConfig,
    ) -> float | None:
        if config.full_rigid_transform_aux_from_main is None:
            return None
        main_center = np.asarray(config.main_camera_position_enu_m, dtype=np.float64)
        translation_aux = np.asarray(
            config.full_rigid_transform_aux_from_main.translation_a_m, dtype=np.float64
        )
        if main_center.shape != (3,) or translation_aux.shape != (3,):
            raise ValueError("installation positions must be finite 3-vectors")
        if not _is_finite_matrix(main_center) or not _is_finite_matrix(translation_aux):
            raise ValueError("installation positions must be finite 3-vectors")
        baseline_translation = -observed_camera_from_enu @ main_center + translation_aux
        projected, _ = cv2.projectPoints(
            world_points,
            cv2.Rodrigues(observed_camera_from_enu)[0],
            baseline_translation,
            camera,
            distortion,
        )
        projected = projected.reshape(-1, 2)
        return float(np.sqrt(np.mean(np.sum((projected - image_points) ** 2, axis=1))))

    @staticmethod
    def _attempt(
        status: CalibrationAttemptStatus,
        references: ReferenceObservationBatch,
        now: ProcessingTime,
        reason_codes: Iterable[str],
        estimate: CalibrationEstimate | None = None,
    ) -> CalibrationAttempt:
        return CalibrationAttempt(
            status=status,
            estimate=estimate,
            reference_frame_ref=references.auxiliary_frame_ref,
            reference_capture_time=references.capture_time,
            completed_at=now,
            reason_codes=tuple(reason_codes),
        )


class CalibrationHistoryStore(CalibrationHistory):
    """Immutable solution and time-addressed health history for one session."""

    def __init__(
        self,
        station_id: StationId,
        station_session_id: SessionId,
        station_config_digest: ConfigDigest | None = None,
    ) -> None:
        self.station_id = station_id
        self.station_session_id = station_session_id
        self.station_config_digest = station_config_digest
        self._solutions: dict[CalibrationSolutionRef, CalibrationSolution] = {}
        self._solution_order: list[CalibrationSolutionRef] = []
        self._states: list[CalibrationState] = []

    @property
    def solutions(self) -> tuple[CalibrationSolution, ...]:
        return tuple(self._solutions[reference] for reference in self._solution_order)

    @property
    def states(self) -> tuple[CalibrationState, ...]:
        return tuple(self._states)

    def record_solution(self, solution: CalibrationSolution) -> None:
        self._validate_solution_scope(solution)
        existing = self._solutions.get(solution.solution_ref)
        if existing is not None:
            if existing != solution:
                raise ValueError("calibration solution ID was reused with different content")
            return
        midpoint = _capture_midpoint(solution.reference_capture_time)
        if self._solution_order:
            previous = self._solutions[self._solution_order[-1]]
            if midpoint <= _capture_midpoint(previous.reference_capture_time):
                raise ValueError("calibration reference capture times must increase strictly")
        self._solutions[solution.solution_ref] = solution
        self._solution_order.append(solution.solution_ref)

    def record_state(self, state: CalibrationState) -> None:
        if (
            state.station_id != self.station_id
            or state.station_session_id != self.station_session_id
        ):
            raise ValueError("calibration state belongs to a different station session")
        if (
            self.station_config_digest is not None
            and state.station_config_digest != self.station_config_digest
        ):
            raise ValueError("calibration state configuration digest mismatch")
        if self._states:
            previous = self._states[-1]
            if state.state_version < previous.state_version:
                raise ValueError("calibration state versions must be monotonic")
            if state.state_version == previous.state_version:
                if state != previous:
                    raise ValueError("calibration state version was reused with different content")
                return
        if state.solution_ref is not None and state.solution_ref not in self._solutions:
            raise ValueError("calibration state references an unknown solution")
        self._states.append(state)

    def lookup_solution(self, ref: CalibrationSolutionRef) -> CalibrationSolution | None:
        return self._solutions.get(ref)

    def snapshot_at(self, capture_time: CaptureTime) -> CalibrationSnapshot:
        midpoint = _capture_midpoint(capture_time)
        candidates: list[CalibrationSolution] = []
        for candidate_solution in self._solutions.values():
            if (
                candidate_solution.reference_capture_time.clock_domain_id
                != capture_time.clock_domain_id
            ):
                continue
            if candidate_solution.accepted_time.clock_domain_id != capture_time.clock_domain_id:
                continue
            if _capture_midpoint(candidate_solution.reference_capture_time) > midpoint:
                continue
            if candidate_solution.accepted_time.time_ns > midpoint:
                continue
            interval = candidate_solution.valid_interval
            if interval.clock_domain_id != capture_time.clock_domain_id:
                continue
            if interval.start_ns > midpoint or (
                interval.end_ns is not None and midpoint >= interval.end_ns
            ):
                continue
            candidates.append(candidate_solution)
        solution: CalibrationSolution | None = max(
            candidates,
            key=lambda value: (
                _capture_midpoint(value.reference_capture_time),
                value.accepted_time.time_ns,
            ),
            default=None,
        )
        if solution is None:
            state = self._state_at(midpoint, capture_time.clock_domain_id, None)
            if state is None:
                state = self._empty_state(capture_time)
            return CalibrationSnapshot(
                main_capture_time=capture_time,
                state=state,
                solution=None,
                usage=None,
                reason_codes=("no_calibration_solution_known_at_exposure",),
            )
        state = self._state_at(midpoint, capture_time.clock_domain_id, solution.solution_ref)
        if state is None:
            return CalibrationSnapshot(
                main_capture_time=capture_time,
                state=self._empty_state(capture_time),
                solution=None,
                usage=None,
                reason_codes=("no_calibration_state_known_at_exposure",),
            )
        usage = {
            CalibrationHealth.VALID: CalibrationUsage.VALID,
            CalibrationHealth.FROZEN: CalibrationUsage.FROZEN,
            CalibrationHealth.STALE: CalibrationUsage.STALE,
            CalibrationHealth.RECOVERING: CalibrationUsage.FROZEN,
        }.get(state.health)
        if usage is None:
            return CalibrationSnapshot(
                main_capture_time=capture_time,
                state=state,
                solution=None,
                usage=None,
                reason_codes=("calibration_state_not_usable", state.health.value),
            )
        return CalibrationSnapshot(
            main_capture_time=capture_time,
            state=state,
            solution=solution,
            usage=usage,
            reason_codes=state.reason_codes,
        )

    def _state_at(
        self, midpoint: int, clock_domain_id: str, solution_ref: CalibrationSolutionRef | None
    ) -> CalibrationState | None:
        for state in reversed(self._states):
            if state.observed_at.clock_domain_id != clock_domain_id:
                continue
            if state.observed_at.time_ns > midpoint:
                continue
            if solution_ref is not None and state.solution_ref != solution_ref:
                continue
            return state
        return None

    def _empty_state(self, capture_time: CaptureTime) -> CalibrationState:
        observed = ProcessingTime(
            clock_domain_id=capture_time.clock_domain_id,
            time_ns=_capture_midpoint(capture_time),
        )
        return CalibrationState(
            station_id=self.station_id,
            station_session_id=self.station_session_id,
            station_config_digest=self.station_config_digest or ConfigDigest("unknown"),
            state_version=0,
            health=CalibrationHealth.INITIALIZING,
            solution_ref=None,
            observed_at=observed,
            last_trusted_time=None,
            usable_interval=None,
            reason_codes=("no_calibration_state_known_at_exposure",),
        )

    def _validate_solution_scope(self, solution: CalibrationSolution) -> None:
        ref = solution.solution_ref
        if ref.station_id != self.station_id or ref.station_session_id != self.station_session_id:
            raise ValueError("calibration solution belongs to a different station session")
        if (
            self.station_config_digest is not None
            and solution.station_config_digest != self.station_config_digest
        ):
            raise ValueError("calibration solution configuration digest mismatch")


class CalibrationStateMachine(CalibrationStateManager):
    """Apply quality/recovery policy and publish immutable calibration history."""

    def __init__(
        self,
        policy: CalibrationPolicy,
        history: CalibrationHistoryStore,
        sequencer: EventSequencer,
        *,
        station_id: StationId | None = None,
        station_session_id: SessionId | None = None,
    ) -> None:
        self.policy = policy
        self.history = history
        self.sequencer = sequencer
        self.station_id = station_id or history.station_id
        self.station_session_id = station_session_id or history.station_session_id
        self._state: CalibrationState | None = None
        self._solution: CalibrationSolution | None = None
        self._version = 0
        self._solution_sequence = 0
        self._recovery_successes = 0
        self._last_update: CalibrationUpdate | None = None

    @property
    def state(self) -> CalibrationState | None:
        return self._state

    @property
    def solution(self) -> CalibrationSolution | None:
        return self._solution

    def consider(self, attempt: CalibrationAttempt, config: StationConfig) -> CalibrationUpdate:
        if attempt.reference_frame_ref.station_id != config.station_id:
            raise ValueError("calibration attempt station does not match configuration")
        self._check_config(config)
        if attempt.status is CalibrationAttemptStatus.ESTIMATED and attempt.estimate is not None:
            quality = attempt.estimate.quality
            quality_ok = (
                quality.matched_reference_count >= max(4, self.policy.minimum_reference_count)
                and quality.reprojection_rmse_px is not None
                and quality.reprojection_rmse_px <= self.policy.maximum_reprojection_rmse_px
            )
            reference_is_new = self._solution is None or (
                _capture_midpoint(attempt.estimate.reference_capture_time)
                > _capture_midpoint(self._solution.reference_capture_time)
            )
            if quality_ok and reference_is_new:
                recovering = self._state is not None and self._state.health in {
                    CalibrationHealth.FROZEN,
                    CalibrationHealth.STALE,
                    CalibrationHealth.RECOVERING,
                }
                if recovering:
                    self._recovery_successes += 1
                else:
                    self._recovery_successes = self.policy.recovery_success_count
                if self._recovery_successes >= max(1, self.policy.recovery_success_count):
                    return self._accept(attempt, config)
                return self._publish(
                    config,
                    attempt.completed_at,
                    CalibrationHealth.RECOVERING,
                    self._solution.solution_ref if self._solution is not None else None,
                    self._solution.accepted_time if self._solution is not None else None,
                    ("recovery_observation", *attempt.reason_codes),
                    None,
                )
            reason = (
                "calibration_quality_rejected"
                if not quality_ok
                else "reference_capture_time_not_increasing"
            )
        else:
            reason = "calibration_attempt_failed"
        health = (
            CalibrationHealth.FROZEN
            if self._solution is not None
            else CalibrationHealth.INITIALIZING
        )
        if self._state is not None and self._state.health is CalibrationHealth.STALE:
            health = CalibrationHealth.STALE
        self._recovery_successes = 0
        update = self._publish(
            config,
            attempt.completed_at,
            health,
            self._solution.solution_ref if self._solution is not None else None,
            self._solution.accepted_time if self._solution is not None else None,
            (reason, *attempt.reason_codes),
            None,
        )
        return update

    def tick(self, now: ProcessingTime, config: StationConfig) -> CalibrationUpdate | None:
        self._check_config(config)
        if self._state is None or self._solution is None:
            return None
        if self._state.health is CalibrationHealth.STALE:
            return None
        if now.clock_domain_id != self._solution.accepted_time.clock_domain_id:
            raise ValueError("processing clock domain does not match calibration solution")
        if now.time_ns - self._solution.accepted_time.time_ns <= self.policy.stale_after_ns:
            return None
        return self._publish(
            config,
            now,
            CalibrationHealth.STALE,
            self._solution.solution_ref,
            self._solution.accepted_time,
            ("calibration_stale_after_timeout",),
            None,
        )

    def current_update(self) -> CalibrationUpdate:
        if self._last_update is None:
            raise RuntimeError("calibration state has not been initialized")
        return self._last_update

    def _accept(self, attempt: CalibrationAttempt, config: StationConfig) -> CalibrationUpdate:
        assert attempt.estimate is not None
        self._solution_sequence += 1
        solution_ref = CalibrationSolutionRef(
            station_id=self.station_id,
            station_session_id=self.station_session_id,
            solution_id=CalibrationSolutionId(f"opencv-calibration-{self._solution_sequence}"),
        )
        solution = CalibrationSolution(
            solution_ref=solution_ref,
            station_config_digest=config.config_digest,
            world_frame_id=config.world_frame_id,
            reference_capture_time=attempt.estimate.reference_capture_time,
            accepted_time=attempt.completed_at,
            valid_interval=TimeInterval(
                clock_domain_id=attempt.estimate.reference_capture_time.clock_domain_id,
                start_ns=_capture_midpoint(attempt.estimate.reference_capture_time),
                end_ns=None,
            ),
            main_camera_position_enu_m=config.main_camera_position_enu_m,
            rotation_enu_from_main=attempt.estimate.rotation_enu_from_main,
            correction_covariance_enu_rad2=attempt.estimate.correction_covariance_enu_rad2,
            quality=attempt.estimate.quality,
        )
        self.history.record_solution(solution)
        self._solution = solution
        self._recovery_successes = 0
        return self._publish(
            config,
            attempt.completed_at,
            CalibrationHealth.VALID,
            solution_ref,
            attempt.completed_at,
            attempt.reason_codes,
            solution,
        )

    def _publish(
        self,
        config: StationConfig,
        observed_at: ProcessingTime,
        health: CalibrationHealth,
        solution_ref: CalibrationSolutionRef | None,
        last_trusted_time: ProcessingTime | None,
        reason_codes: Iterable[str],
        solution: CalibrationSolution | None,
    ) -> CalibrationUpdate:
        self._version += 1
        interval = self._solution.valid_interval if self._solution is not None else None
        state = CalibrationState(
            station_id=self.station_id,
            station_session_id=self.station_session_id,
            station_config_digest=config.config_digest,
            state_version=self._version,
            health=health,
            solution_ref=solution_ref,
            observed_at=observed_at,
            last_trusted_time=last_trusted_time,
            usable_interval=interval,
            reason_codes=tuple(reason_codes),
        )
        self.history.record_state(state)
        envelope = self.sequencer.next_envelope(EventType.CALIBRATION_STATE, config.config_digest)
        state_event = CalibrationStateEvent(envelope=envelope, state=state, solution=solution)
        diagnostics: tuple[DiagnosticEvent, ...] = ()
        if health in {
            CalibrationHealth.INITIALIZING,
            CalibrationHealth.FROZEN,
            CalibrationHealth.STALE,
            CalibrationHealth.RECOVERING,
        } and not (health is CalibrationHealth.RECOVERING and self._solution is not None):
            diagnostics = (self._diagnostic(config, observed_at, state),)
        update = CalibrationUpdate(
            solution=solution, state_event=state_event, diagnostics=diagnostics
        )
        self._state = state
        self._last_update = update
        return update

    def _diagnostic(
        self, config: StationConfig, observed_at: ProcessingTime, state: CalibrationState
    ) -> DiagnosticEvent:
        envelope = self.sequencer.next_envelope(EventType.DIAGNOSTIC, config.config_digest)
        return DiagnosticEvent(
            envelope=envelope,
            related_frame_ref=None,
            capture_time=None,
            category="calibration_unavailable",
            reason_codes=state.reason_codes,
            related_config_ref=config.config_digest,
            related_calibration_ref=state.solution_ref,
        )

    def _check_config(self, config: StationConfig) -> None:
        if config.station_id != self.station_id or config.config_digest != (
            self.history.station_config_digest or config.config_digest
        ):
            raise ValueError("calibration configuration does not match the state machine")


class AuxiliaryCalibrationPipeline:
    """Lower-rate recognizer/estimator/state-manager composition."""

    def __init__(
        self,
        recognizer: ReferenceRecognizer,
        estimator: CalibrationEstimator,
        states: CalibrationStateMachine,
        *,
        update_period_ns: int,
    ) -> None:
        if update_period_ns < 0:
            raise ValueError("calibration update period cannot be negative")
        self.recognizer = recognizer
        self.estimator = estimator
        self.states = states
        self.update_period_ns = update_period_ns
        self._last_reference_midpoint: int | None = None

    def process(
        self, frame: CameraFrame, config: StationConfig, now: ProcessingTime
    ) -> CalibrationUpdate:
        midpoint = _capture_midpoint(frame.capture_time)
        if (
            self._last_reference_midpoint is not None
            and self.update_period_ns > 0
            and midpoint - self._last_reference_midpoint < self.update_period_ns
        ):
            return self.states.current_update()
        self._last_reference_midpoint = midpoint
        references = self.recognizer.recognize(frame, config)
        attempt = self.estimator.estimate(references, config, now)
        return self.states.consider(attempt, config)

    def tick(self, now: ProcessingTime, config: StationConfig) -> CalibrationUpdate | None:
        return self.states.tick(now, config)


# Short aliases keep the backend replaceable while making the intended port
# implementations discoverable to callers that use "detector" terminology.
OpenCVCheckerboardRecognizer = OpenCVReferenceRecognizer
OpenCVCheckerboardEstimator = OpenCVCalibrationEstimator


__all__ = [
    "AuxiliaryCalibrationPipeline",
    "CalibrationHistoryStore",
    "CalibrationStateMachine",
    "CheckerboardGeometry",
    "OpenCVCalibrationEstimator",
    "OpenCVCheckerboardEstimator",
    "OpenCVCheckerboardRecognizer",
    "OpenCVReferenceRecognizer",
]

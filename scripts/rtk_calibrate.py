"""Standalone manual RTK control-point calibration tool for field testing."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

FloatArray = NDArray[np.float64]

PRESET_MACBOOK_1080P_DFOV122 = "macbook-1080p-dfov122"
CORRESPONDENCE_SCHEMA = "rtk-correspondences-v1"
CALIBRATION_SCHEMA = "rtk-calibration-v1"
_EPS = 1e-10


class CalibrationInputError(ValueError):
    """Actionable validation or geometric degeneracy error."""


@dataclass(frozen=True, kw_only=True)
class ControlPoint:
    point_id: str
    east_m: float
    north_m: float
    up_m: float
    role: str = "fit"
    note: str = ""
    pixel_xy: tuple[float, float] | None = None

    @property
    def enu_m(self) -> tuple[float, float, float]:
        return (self.east_m, self.north_m, self.up_m)


@dataclass(frozen=True, kw_only=True)
class CameraIntrinsicsSpec:
    image_width_px: int
    image_height_px: int
    matrix_px: FloatArray
    distortion_model: str
    distortion_coefficients: tuple[float, ...]
    source: str

    def distortion_array(self) -> FloatArray:
        model = self.distortion_model.lower().replace("-", "_")
        if model in {"none", "pinhole", "zero"}:
            return np.zeros((5, 1), dtype=np.float64)
        if model in {"brown_conrady", "brownconrady", "opencv"}:
            coeffs = tuple(float(v) for v in self.distortion_coefficients)
            if len(coeffs) == 4:
                coeffs = (*coeffs, 0.0)
            if len(coeffs) not in {5, 8, 12, 14}:
                raise CalibrationInputError(
                    "Brown-Conrady distortion requires 4, 5, 8, 12, or 14 coefficients."
                )
            return np.asarray(coeffs, dtype=np.float64).reshape(-1, 1)
        raise CalibrationInputError(f"Unsupported distortion model: {self.distortion_model!r}")


@dataclass(frozen=True, kw_only=True)
class PoseCandidate:
    rvec: FloatArray
    tvec: FloatArray
    rotation_cam_from_enu: FloatArray
    rotation_enu_from_cam: FloatArray
    optical_center_enu_m: FloatArray
    reprojection_rmse_px: float
    method: str


@dataclass(frozen=True, kw_only=True)
class PointResidual:
    point_id: str
    role: str
    position_enu_m: tuple[float, float, float]
    observed_px: tuple[float, float]
    projected_px: tuple[float, float]
    residual_px: tuple[float, float]
    error_px: float
    angular_error_deg: float | None
    in_front_of_camera: bool


def macbook_1080p_dfov122_intrinsics() -> CameraIntrinsicsSpec:
    """Nominal MacBook built-in camera intrinsics (1080p, DFOV 122 deg, pinhole)."""
    width_px = 1920
    height_px = 1080
    dfov_deg = 122.0
    diagonal_px = math.hypot(width_px, height_px)
    focal_px = (diagonal_px * 0.5) / math.tan(math.radians(dfov_deg * 0.5))
    cx = width_px * 0.5
    cy = height_px * 0.5
    matrix = np.array(
        [[focal_px, 0.0, cx], [0.0, focal_px, cy], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    return CameraIntrinsicsSpec(
        image_width_px=width_px,
        image_height_px=height_px,
        matrix_px=matrix,
        distortion_model="none",
        distortion_coefficients=(),
        source=f"preset:{PRESET_MACBOOK_1080P_DFOV122}",
    )


def intrinsics_to_dict(spec: CameraIntrinsicsSpec) -> dict[str, Any]:
    return {
        "image_width_px": spec.image_width_px,
        "image_height_px": spec.image_height_px,
        "matrix_px": [[float(v) for v in row] for row in spec.matrix_px],
        "distortion_model": spec.distortion_model,
        "distortion_coefficients": [float(v) for v in spec.distortion_coefficients],
        "source": spec.source,
    }


def load_intrinsics_json(path: Path) -> CameraIntrinsicsSpec:
    """Load intrinsics from standalone JSON, CameraModel JSON, or StationConfig JSON."""
    if not path.is_file():
        raise CalibrationInputError(f"Intrinsics file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationInputError(f"Failed to read intrinsics JSON {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise CalibrationInputError(f"Intrinsics JSON must be an object: {path}")

    if "main_camera" in raw and isinstance(raw["main_camera"], dict):
        raw = raw["main_camera"]

    if "intrinsics" in raw and isinstance(raw["intrinsics"], dict):
        intr = raw["intrinsics"]
        dist = raw.get("distortion", {})
        if not isinstance(dist, dict):
            raise CalibrationInputError("Invalid 'distortion' section in CameraModel JSON.")
        width_px = intr.get("image_width_px")
        height_px = intr.get("image_height_px")
        matrix_raw = intr.get("matrix_px")
        dist_model = dist.get("model", "none")
        dist_coeffs = dist.get("coefficients", [])
    else:
        width_px = raw.get("image_width_px")
        height_px = raw.get("image_height_px")
        matrix_raw = raw.get("matrix_px")
        if matrix_raw is None:
            for key in ("fx", "fy", "cx", "cy"):
                if key not in raw:
                    raise CalibrationInputError(
                        f"Intrinsics JSON must provide either 'matrix_px' or 'fx/fy/cx/cy' (missing {key!r})."
                    )
            skew = float(raw.get("skew", 0.0))
            matrix_raw = [
                [float(raw["fx"]), skew, float(raw["cx"])],
                [0.0, float(raw["fy"]), float(raw["cy"])],
                [0.0, 0.0, 1.0],
            ]
        dist_model = raw.get("distortion_model", "none")
        dist_coeffs = raw.get("distortion_coefficients", [])

    if (
        not isinstance(width_px, int)
        or isinstance(width_px, bool)
        or not isinstance(height_px, int)
        or isinstance(height_px, bool)
        or width_px <= 0
        or height_px <= 0
    ):
        raise CalibrationInputError(
            "Intrinsics JSON requires positive integer 'image_width_px' and 'image_height_px'."
        )

    try:
        matrix = np.asarray(matrix_raw, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise CalibrationInputError(f"Invalid intrinsic matrix: {exc}") from exc
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        raise CalibrationInputError("Intrinsic matrix must be a finite 3x3 matrix.")
    if abs(float(matrix[2, 2])) < _EPS:
        raise CalibrationInputError("Intrinsic matrix has zero homogeneous scale at [2, 2].")
    matrix = matrix / matrix[2, 2]
    if (
        not np.allclose(matrix[2], (0.0, 0.0, 1.0), atol=1e-12)
        or abs(float(matrix[1, 0])) > 1e-12
        or min(float(matrix[0, 0]), float(matrix[1, 1])) <= 0.0
    ):
        raise CalibrationInputError(
            "Intrinsic matrix must have positive fx, fy and bottom row [0, 0, 1]."
        )

    if not isinstance(dist_model, str):
        raise CalibrationInputError("'distortion_model' must be a string.")
    if not isinstance(dist_coeffs, list):
        raise CalibrationInputError("'distortion_coefficients' must be a list of numbers.")
    try:
        coeffs = tuple(float(v) for v in dist_coeffs)
    except (TypeError, ValueError) as exc:
        raise CalibrationInputError(f"Invalid distortion coefficients: {exc}") from exc
    if not all(math.isfinite(v) for v in coeffs):
        raise CalibrationInputError("Distortion coefficients must be finite.")

    spec = CameraIntrinsicsSpec(
        image_width_px=width_px,
        image_height_px=height_px,
        matrix_px=matrix,
        distortion_model=dist_model,
        distortion_coefficients=coeffs,
        source=str(path),
    )
    spec.distortion_array()
    return spec


def validate_image_resolution(
    image_width_px: int, image_height_px: int, intrinsics: CameraIntrinsicsSpec
) -> None:
    if image_width_px != intrinsics.image_width_px or image_height_px != intrinsics.image_height_px:
        raise CalibrationInputError(
            f"Image resolution {image_width_px}x{image_height_px} does not match "
            f"intrinsics resolution {intrinsics.image_width_px}x{intrinsics.image_height_px} "
            f"({intrinsics.source}). Supply intrinsics matching the recorded resolution."
        )


def _parse_role(value: str | None) -> str:
    role = (value or "fit").strip().lower()
    if role not in {"fit", "check"}:
        raise CalibrationInputError(
            f"Invalid control-point role {value!r}; expected 'fit' or 'check'."
        )
    return role


def _parse_finite_float(raw: str | None, field_name: str, point_id: str) -> float:
    if raw is None or not raw.strip():
        raise CalibrationInputError(f"Missing {field_name!r} for control point {point_id!r}.")
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise CalibrationInputError(
            f"Non-numeric {field_name!r}={raw!r} for control point {point_id!r}."
        ) from exc
    if not math.isfinite(value):
        raise CalibrationInputError(f"Non-finite {field_name!r} for control point {point_id!r}.")
    return value


def load_control_points_csv(path: Path) -> list[ControlPoint]:
    """Load surveyed control points in a shared local ENU frame from CSV."""
    if not path.is_file():
        raise CalibrationInputError(f"Control-point CSV not found: {path}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise CalibrationInputError(f"Failed to read control-point CSV {path}: {exc}") from exc

    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None:
        raise CalibrationInputError(f"Control-point CSV is empty: {path}")
    normalized_headers = {name.strip(): name for name in reader.fieldnames if name is not None}
    required = ("point_id", "east_m", "north_m", "up_m")
    missing = [col for col in required if col not in normalized_headers]
    if missing:
        raise CalibrationInputError(
            f"Control-point CSV {path} is missing required columns: {', '.join(missing)}. "
            "Expected columns: point_id,east_m,north_m,up_m[,role,note]."
        )

    points: list[ControlPoint] = []
    seen_ids: set[str] = set()
    for row_num, row in enumerate(reader, start=2):
        cleaned = {k.strip(): (v if v is not None else "") for k, v in row.items() if k is not None}
        if not any(v.strip() for v in cleaned.values()):
            continue
        point_id = cleaned["point_id"].strip()
        if not point_id:
            raise CalibrationInputError(f"Empty 'point_id' at CSV row {row_num} in {path}.")
        if point_id in seen_ids:
            raise CalibrationInputError(f"Duplicate 'point_id' {point_id!r} in {path}.")
        seen_ids.add(point_id)
        east_m = _parse_finite_float(cleaned.get("east_m"), "east_m", point_id)
        north_m = _parse_finite_float(cleaned.get("north_m"), "north_m", point_id)
        up_m = _parse_finite_float(cleaned.get("up_m"), "up_m", point_id)
        role = _parse_role(cleaned.get("role"))
        note = cleaned.get("note", "").strip()
        points.append(
            ControlPoint(
                point_id=point_id,
                east_m=east_m,
                north_m=north_m,
                up_m=up_m,
                role=role,
                note=note,
            )
        )

    if not points:
        raise CalibrationInputError(f"No control points found in CSV: {path}")
    return points


def load_image_bgr(path: Path) -> NDArray[np.uint8]:
    if not path.is_file():
        raise CalibrationInputError(f"Camera image not found: {path}")
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        raise CalibrationInputError(f"Unable to decode camera image: {path}")
    return np.asarray(image, dtype=np.uint8)


def _resolve_saved_path(raw_path: str | None, base_dir: Path) -> Path | None:
    if raw_path is None or not raw_path.strip():
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        rel = (base_dir / candidate).resolve()
        if rel.exists():
            return rel
    return candidate


def save_correspondences_json(
    path: Path,
    *,
    image_path: Path | None,
    image_size_px: tuple[int, int] | None,
    csv_path: Path | None,
    intrinsics_path: Path | None,
    intrinsics_preset: str | None,
    points: list[ControlPoint],
) -> None:
    """Save point correspondences independently of the pose solver."""
    payload = {
        "schema_version": CORRESPONDENCE_SCHEMA,
        "image_path": str(image_path) if image_path is not None else None,
        "image_width_px": image_size_px[0] if image_size_px is not None else None,
        "image_height_px": image_size_px[1] if image_size_px is not None else None,
        "csv_path": str(csv_path) if csv_path is not None else None,
        "intrinsics_path": str(intrinsics_path) if intrinsics_path is not None else None,
        "intrinsics_preset": intrinsics_preset,
        "points": [
            {
                "point_id": pt.point_id,
                "east_m": pt.east_m,
                "north_m": pt.north_m,
                "up_m": pt.up_m,
                "role": pt.role,
                "note": pt.note,
                "pixel_xy": [float(pt.pixel_xy[0]), float(pt.pixel_xy[1])]
                if pt.pixel_xy is not None
                else None,
            }
            for pt in points
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


@dataclass(frozen=True, kw_only=True)
class LoadedCorrespondences:
    image_path: Path | None
    image_width_px: int | None
    image_height_px: int | None
    csv_path: Path | None
    intrinsics_path: Path | None
    intrinsics_preset: str | None
    points: list[ControlPoint]


def load_correspondences_json(path: Path) -> LoadedCorrespondences:
    if not path.is_file():
        raise CalibrationInputError(f"Correspondences file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationInputError(f"Failed to read correspondences JSON {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise CalibrationInputError(f"Correspondences JSON must be an object: {path}")
    if raw.get("schema_version") != CORRESPONDENCE_SCHEMA:
        raise CalibrationInputError(
            f"Unsupported correspondences schema {raw.get('schema_version')!r}; "
            f"expected {CORRESPONDENCE_SCHEMA!r}."
        )

    raw_points = raw.get("points")
    if not isinstance(raw_points, list) or not raw_points:
        raise CalibrationInputError(f"Correspondences file contains no points: {path}")

    points: list[ControlPoint] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_points):
        if not isinstance(item, dict):
            raise CalibrationInputError(f"Invalid point entry at index {index} in {path}.")
        point_id = str(item.get("point_id", "")).strip()
        if not point_id or point_id in seen_ids:
            raise CalibrationInputError(
                f"Missing or duplicate point_id at index {index} in {path}."
            )
        seen_ids.add(point_id)
        try:
            east_m = float(item["east_m"])
            north_m = float(item["north_m"])
            up_m = float(item["up_m"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CalibrationInputError(
                f"Invalid ENU coordinates for point {point_id!r} in {path}."
            ) from exc
        if not (math.isfinite(east_m) and math.isfinite(north_m) and math.isfinite(up_m)):
            raise CalibrationInputError(f"Non-finite ENU coordinates for point {point_id!r}.")
        role = _parse_role(str(item.get("role", "fit")))
        note = str(item.get("note", ""))
        raw_px = item.get("pixel_xy")
        pixel_xy: tuple[float, float] | None = None
        if raw_px is not None:
            if not isinstance(raw_px, list) or len(raw_px) != 2:
                raise CalibrationInputError(
                    f"Invalid 'pixel_xy' for point {point_id!r} in {path}; expected [u, v] or null."
                )
            u, v = float(raw_px[0]), float(raw_px[1])
            if not (math.isfinite(u) and math.isfinite(v)):
                raise CalibrationInputError(f"Non-finite pixel_xy for point {point_id!r}.")
            pixel_xy = (u, v)
        points.append(
            ControlPoint(
                point_id=point_id,
                east_m=east_m,
                north_m=north_m,
                up_m=up_m,
                role=role,
                note=note,
                pixel_xy=pixel_xy,
            )
        )

    base_dir = path.parent
    width_px = raw.get("image_width_px")
    height_px = raw.get("image_height_px")
    return LoadedCorrespondences(
        image_path=_resolve_saved_path(raw.get("image_path"), base_dir),
        image_width_px=int(width_px) if isinstance(width_px, int) else None,
        image_height_px=int(height_px) if isinstance(height_px, int) else None,
        csv_path=_resolve_saved_path(raw.get("csv_path"), base_dir),
        intrinsics_path=_resolve_saved_path(raw.get("intrinsics_path"), base_dir),
        intrinsics_preset=raw.get("intrinsics_preset"),
        points=points,
    )


def _rotation_diff_deg(r_a: FloatArray, r_b: FloatArray) -> float:
    cos_val = max(-1.0, min(1.0, (float(np.trace(r_a.T @ r_b)) - 1.0) * 0.5))
    return math.degrees(math.acos(cos_val))


def _evaluate_candidate(
    rvec: FloatArray,
    tvec: FloatArray,
    points_enu: FloatArray,
    image_points: FloatArray,
    intrinsics: CameraIntrinsicsSpec,
    method: str,
) -> PoseCandidate | None:
    rvec_arr = np.asarray(rvec, dtype=np.float64).reshape(3, 1)
    tvec_arr = np.asarray(tvec, dtype=np.float64).reshape(3, 1)
    if not (np.isfinite(rvec_arr).all() and np.isfinite(tvec_arr).all()):
        return None
    rot_cam_from_enu_raw, _ = cv2.Rodrigues(rvec_arr)
    rot_cam_from_enu = np.asarray(rot_cam_from_enu_raw, dtype=np.float64)
    t_flat = tvec_arr.reshape(3)
    cam_pts = (rot_cam_from_enu @ points_enu.T).T + t_flat
    if not np.isfinite(cam_pts).all() or np.any(cam_pts[:, 2] <= 1e-6):
        return None
    dist = intrinsics.distortion_array()
    projected, _ = cv2.projectPoints(points_enu, rvec_arr, tvec_arr, intrinsics.matrix_px, dist)
    projected_2d = projected.reshape(-1, 2)
    if not np.isfinite(projected_2d).all():
        return None
    residuals = projected_2d - image_points
    rmse = float(np.sqrt(np.mean(np.sum(residuals * residuals, axis=1))))
    rot_enu_from_cam = rot_cam_from_enu.T
    optical_center = -rot_enu_from_cam @ t_flat
    return PoseCandidate(
        rvec=rvec_arr,
        tvec=tvec_arr,
        rotation_cam_from_enu=rot_cam_from_enu,
        rotation_enu_from_cam=rot_enu_from_cam,
        optical_center_enu_m=optical_center,
        reprojection_rmse_px=rmse,
        method=method,
    )


def _refine_candidate(
    rvec: FloatArray,
    tvec: FloatArray,
    points_enu: FloatArray,
    image_points: FloatArray,
    intrinsics: CameraIntrinsicsSpec,
) -> tuple[FloatArray, FloatArray]:
    dist = intrinsics.distortion_array()
    try:
        refined_rvec, refined_tvec = cv2.solvePnPRefineLM(
            points_enu,
            image_points,
            intrinsics.matrix_px,
            dist,
            rvec.reshape(3, 1).copy(),
            tvec.reshape(3, 1).copy(),
        )
        return np.asarray(refined_rvec, dtype=np.float64), np.asarray(
            refined_tvec, dtype=np.float64
        )
    except cv2.error:
        return rvec, tvec


def _validate_clicked_points(
    points: list[ControlPoint], intrinsics: CameraIntrinsicsSpec
) -> tuple[list[ControlPoint], list[ControlPoint]]:
    clicked_fit: list[ControlPoint] = []
    clicked_check: list[ControlPoint] = []
    for pt in points:
        if pt.pixel_xy is None:
            continue
        u, v = pt.pixel_xy
        if not (math.isfinite(u) and math.isfinite(v)):
            raise CalibrationInputError(f"Non-finite pixel coordinates for point {pt.point_id!r}.")
        if not (0.0 <= u < intrinsics.image_width_px and 0.0 <= v < intrinsics.image_height_px):
            raise CalibrationInputError(
                f"Point {pt.point_id!r} pixel ({u:.2f}, {v:.2f}) is outside "
                f"{intrinsics.image_width_px}x{intrinsics.image_height_px}."
            )
        if pt.role == "fit":
            clicked_fit.append(pt)
        else:
            clicked_check.append(pt)

    if len(clicked_fit) < 4:
        raise CalibrationInputError(
            f"Need at least 4 clicked 'fit' control points to solve pose (found {len(clicked_fit)}). "
            "Click more 'fit' points or switch held-out points to 'fit'."
        )

    for i in range(len(clicked_fit)):
        for j in range(i + 1, len(clicked_fit)):
            a, b = clicked_fit[i], clicked_fit[j]
            dist_3d = math.dist(a.enu_m, b.enu_m)
            if dist_3d < 1e-4:
                raise CalibrationInputError(
                    f"Control points {a.point_id!r} and {b.point_id!r} have coincident ENU coordinates "
                    f"(distance {dist_3d:.6f} m)."
                )
            assert a.pixel_xy is not None and b.pixel_xy is not None
            dist_2d = math.dist(a.pixel_xy, b.pixel_xy)
            if dist_2d < 1e-2:
                raise CalibrationInputError(
                    f"Control points {a.point_id!r} and {b.point_id!r} share nearly identical pixels "
                    f"(distance {dist_2d:.4f} px)."
                )
    return clicked_fit, clicked_check


def _compute_residuals(
    points: list[ControlPoint],
    candidate: PoseCandidate,
    intrinsics: CameraIntrinsicsSpec,
) -> list[PointResidual]:
    if not points:
        return []
    pts_enu = np.asarray([pt.enu_m for pt in points], dtype=np.float64)
    obs_px = np.asarray([pt.pixel_xy for pt in points], dtype=np.float64)
    dist = intrinsics.distortion_array()
    proj, _ = cv2.projectPoints(pts_enu, candidate.rvec, candidate.tvec, intrinsics.matrix_px, dist)
    proj_2d = proj.reshape(-1, 2)
    cam_pts = (candidate.rotation_cam_from_enu @ pts_enu.T).T + candidate.tvec.reshape(3)

    # Undistort observed pixels into normalized camera rays for angular residual.
    k_inv_obs = np.linalg.solve(
        intrinsics.matrix_px,
        np.column_stack((obs_px, np.ones(len(points), dtype=np.float64))).T,
    ).T[:, :2]
    norm_xy = cv2.undistortPointsIter(
        k_inv_obs.reshape(-1, 1, 2),
        np.eye(3),
        dist,
        np.eye(3),
        np.eye(3),
        (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 50, 1e-12),
    ).reshape(-1, 2)

    results: list[PointResidual] = []
    for idx, pt in enumerate(points):
        in_front = bool(cam_pts[idx, 2] > 1e-6)
        pu, pv = float(proj_2d[idx, 0]), float(proj_2d[idx, 1])
        ou, ov = float(obs_px[idx, 0]), float(obs_px[idx, 1])
        du, dv = pu - ou, pv - ov
        err_px = math.hypot(du, dv)
        ang_deg: float | None = None
        if in_front:
            obs_ray = np.array([norm_xy[idx, 0], norm_xy[idx, 1], 1.0], dtype=np.float64)
            obs_ray /= np.linalg.norm(obs_ray)
            pred_ray = cam_pts[idx] / np.linalg.norm(cam_pts[idx])
            cos_val = max(-1.0, min(1.0, float(np.dot(obs_ray, pred_ray))))
            ang_deg = math.degrees(math.acos(cos_val))
        results.append(
            PointResidual(
                point_id=pt.point_id,
                role=pt.role,
                position_enu_m=pt.enu_m,
                observed_px=(ou, ov),
                projected_px=(pu, pv),
                residual_px=(du, dv),
                error_px=err_px,
                angular_error_deg=ang_deg,
                in_front_of_camera=in_front,
            )
        )
    return results


def _residual_summary(residuals: list[PointResidual]) -> dict[str, Any] | None:
    if not residuals:
        return None
    errors_px = [r.error_px for r in residuals]
    angles = [r.angular_error_deg for r in residuals if r.angular_error_deg is not None]
    rmse_px = math.sqrt(sum(e * e for e in errors_px) / len(errors_px))
    return {
        "point_count": len(residuals),
        "reprojection_rmse_px": rmse_px,
        "mean_reprojection_error_px": sum(errors_px) / len(errors_px),
        "max_reprojection_error_px": max(errors_px),
        "mean_angular_error_deg": (sum(angles) / len(angles)) if angles else None,
        "max_angular_error_deg": max(angles) if angles else None,
        "behind_camera_point_ids": [r.point_id for r in residuals if not r.in_front_of_camera],
    }


def _residuals_to_dicts(residuals: list[PointResidual]) -> list[dict[str, Any]]:
    return [
        {
            "point_id": r.point_id,
            "role": r.role,
            "position_enu_m": list(r.position_enu_m),
            "observed_px": list(r.observed_px),
            "projected_px": list(r.projected_px),
            "residual_px": list(r.residual_px),
            "error_px": r.error_px,
            "angular_error_deg": r.angular_error_deg,
            "in_front_of_camera": r.in_front_of_camera,
        }
        for r in residuals
    ]


def solve_rtk_pose(
    points: list[ControlPoint],
    intrinsics: CameraIntrinsicsSpec,
    *,
    image_path: Path | None = None,
) -> dict[str, Any]:
    """Solve camera pose in local ENU from clicked 'fit' points and evaluate held-out checkpoints."""
    clicked_fit, clicked_check = _validate_clicked_points(points, intrinsics)
    pts_enu = np.asarray([pt.enu_m for pt in clicked_fit], dtype=np.float64)
    img_pts = np.asarray([pt.pixel_xy for pt in clicked_fit], dtype=np.float64)

    mean_enu = np.mean(pts_enu, axis=0)
    centered_3d = pts_enu - mean_enu
    _, s_3d, vt_3d = np.linalg.svd(centered_3d, full_matrices=True)
    if s_3d[0] < 1e-4:
        raise CalibrationInputError("Control points marked for fit are effectively coincident.")
    if s_3d[1] / s_3d[0] < 1e-3 or s_3d[1] < 1e-3:
        raise CalibrationInputError(
            "Control points marked for fit are nearly collinear in 3D ENU; "
            "add non-collinear points spanning width and height/depth."
        )

    centered_2d = img_pts - np.mean(img_pts, axis=0)
    _, s_2d, _ = np.linalg.svd(centered_2d, full_matrices=False)
    if s_2d[0] < 1.0 or s_2d[1] / s_2d[0] < 1e-3:
        raise CalibrationInputError(
            "Clicked image pixels for 'fit' points are nearly collinear; "
            "cannot determine camera pose reliably."
        )

    planarity_ratio = float(s_3d[2] / s_3d[0])
    is_planar = planarity_ratio < 0.02
    dist = intrinsics.distortion_array()
    raw_candidates: list[tuple[FloatArray, FloatArray, str]] = []

    if is_planar:
        basis = vt_3d.copy()
        if np.linalg.det(basis) < 0.0:
            basis[2] *= -1.0
        local_xy = centered_3d @ basis[:2].T
        local_plane_3d = np.column_stack((local_xy, np.zeros(len(clicked_fit), dtype=np.float64)))
        ok, rvecs, tvecs, _ = cv2.solvePnPGeneric(
            local_plane_3d,
            img_pts,
            intrinsics.matrix_px,
            dist,
            flags=cv2.SOLVEPNP_IPPE,
        )
        if ok and rvecs is not None and tvecs is not None:
            for rv, tv in zip(rvecs, tvecs):
                r_cam_from_plane, _ = cv2.Rodrigues(np.asarray(rv, dtype=np.float64))
                r_cam_from_enu = r_cam_from_plane @ basis
                t_cam_enu = np.asarray(tv, dtype=np.float64).reshape(3) - r_cam_from_enu @ mean_enu
                rv_enu, _ = cv2.Rodrigues(r_cam_from_enu)
                raw_candidates.append(
                    (np.asarray(rv_enu, dtype=np.float64), t_cam_enu.reshape(3, 1), "ippe_plane+lm")
                )

    if not raw_candidates:
        ok_sq, rvecs_sq, tvecs_sq, _ = cv2.solvePnPGeneric(
            pts_enu,
            img_pts,
            intrinsics.matrix_px,
            dist,
            flags=cv2.SOLVEPNP_SQPNP,
        )
        if ok_sq and rvecs_sq is not None and tvecs_sq is not None:
            for rv, tv in zip(rvecs_sq, tvecs_sq):
                raw_candidates.append(
                    (np.asarray(rv, dtype=np.float64), np.asarray(tv, dtype=np.float64), "sqpnp+lm")
                )

    candidates: list[PoseCandidate] = []
    for rv, tv, method in raw_candidates:
        refined_rv, refined_tv = _refine_candidate(rv, tv, pts_enu, img_pts, intrinsics)
        evaluated = _evaluate_candidate(
            refined_rv, refined_tv, pts_enu, img_pts, intrinsics, method
        )
        if evaluated is None:
            evaluated = _evaluate_candidate(
                rv, tv, pts_enu, img_pts, intrinsics, method.replace("+lm", "")
            )
        if evaluated is None:
            continue
        is_duplicate = any(
            float(np.linalg.norm(evaluated.optical_center_enu_m - existing.optical_center_enu_m))
            < 1e-3
            and _rotation_diff_deg(evaluated.rotation_enu_from_cam, existing.rotation_enu_from_cam)
            < 0.1
            for existing in candidates
        )
        if not is_duplicate:
            candidates.append(evaluated)

    if not candidates:
        raise CalibrationInputError(
            "PnP solver failed to find a valid pose with all 'fit' control points in front of the camera. "
            "Check point ID assignments, clicked pixels, ENU coordinates, and camera intrinsics."
        )

    candidates.sort(key=lambda c: c.reprojection_rmse_px)
    best = candidates[0]

    warnings: list[str] = []
    if intrinsics.source.startswith("preset:"):
        warnings.append(
            f"Nominal camera intrinsics ({intrinsics.source}) were used; verify or replace with "
            "measured calibration intrinsics if precise metric accuracy is required."
        )

    alternate_solutions: list[dict[str, Any]] = []
    ambiguous_planar = False
    for alt in candidates[1:]:
        center_diff_m = float(np.linalg.norm(alt.optical_center_enu_m - best.optical_center_enu_m))
        orient_diff_deg = _rotation_diff_deg(alt.rotation_enu_from_cam, best.rotation_enu_from_cam)
        if center_diff_m < 0.5 and orient_diff_deg < 5.0:
            continue
        is_plausible = is_planar and (
            alt.reprojection_rmse_px
            <= max(best.reprojection_rmse_px * 3.0, best.reprojection_rmse_px + 3.0, 5.0)
        )
        if is_plausible:
            ambiguous_planar = True
        alternate_solutions.append(
            {
                "method": alt.method,
                "optical_center_enu_m": [float(v) for v in alt.optical_center_enu_m],
                "rotation_enu_from_cam": [
                    [float(v) for v in row] for row in alt.rotation_enu_from_cam
                ],
                "reprojection_rmse_px": alt.reprojection_rmse_px,
                "center_distance_from_best_m": center_diff_m,
                "orientation_diff_from_best_deg": orient_diff_deg,
                "plausible": is_plausible,
            }
        )

    if is_planar:
        if ambiguous_planar:
            alt0 = alternate_solutions[0]
            warnings.append(
                "Ambiguous planar PnP solution detected: control points are nearly coplanar "
                f"(planarity ratio {planarity_ratio:.4f}) and an alternate valid pose exists "
                f"(RMSE {alt0['reprojection_rmse_px']:.2f} px vs best {best.reprojection_rmse_px:.2f} px; "
                f"center delta {alt0['center_distance_from_best_m']:.2f} m, "
                f"orientation delta {alt0['orientation_diff_from_best_deg']:.1f} deg). "
                "Add non-coplanar control points or verify the optical center against physical placement."
            )
        else:
            warnings.append(
                f"Control points are nearly coplanar (planarity ratio {planarity_ratio:.4f}); "
                "adding points at distinct heights/depths improves robustness against planar flip ambiguity."
            )

    fit_residuals = _compute_residuals(clicked_fit, best, intrinsics)
    check_residuals = _compute_residuals(clicked_check, best, intrinsics)
    fit_summary = _residual_summary(fit_residuals)
    check_summary = _residual_summary(check_residuals)

    if check_summary and check_summary["behind_camera_point_ids"]:
        warnings.append(
            "Held-out check point(s) projected behind the fitted camera: "
            + ", ".join(check_summary["behind_camera_point_ids"])
        )

    return {
        "schema_version": CALIBRATION_SCHEMA,
        "status": "ambiguous_planar" if ambiguous_planar else "ok",
        "method": best.method,
        "warnings": warnings,
        "image": {
            "path": str(image_path) if image_path is not None else None,
            "width_px": intrinsics.image_width_px,
            "height_px": intrinsics.image_height_px,
        },
        "intrinsics": intrinsics_to_dict(intrinsics),
        "pose": {
            "optical_center_enu_m": [float(v) for v in best.optical_center_enu_m],
            "rotation_enu_from_cam": [
                [float(v) for v in row] for row in best.rotation_enu_from_cam
            ],
            "rotation_cam_from_enu": [
                [float(v) for v in row] for row in best.rotation_cam_from_enu
            ],
            "rvec_cam_from_enu": [float(v) for v in best.rvec.reshape(3)],
            "tvec_cam_m": [float(v) for v in best.tvec.reshape(3)],
        },
        "geometry": {
            "planarity_ratio": planarity_ratio,
            "is_planar": is_planar,
            "singular_values_m": [float(v) for v in s_3d],
        },
        "fit_summary": fit_summary,
        "fit_points": _residuals_to_dicts(fit_residuals),
        "check_summary": check_summary,
        "check_points": _residuals_to_dicts(check_residuals),
        "alternate_planar_solutions": alternate_solutions,
    }


def format_result_summary(result: dict[str, Any]) -> str:
    pose = result["pose"]
    center = pose["optical_center_enu_m"]
    rot = pose["rotation_enu_from_cam"]
    fit = result["fit_summary"]
    check = result["check_summary"]
    lines = [
        f"Status: {result['status']} ({result['method']})",
        f"Optical Center ENU (m): [{center[0]:.4f}, {center[1]:.4f}, {center[2]:.4f}]",
        "R_enu_from_cam:",
        f"  [{rot[0][0]: .6f}, {rot[0][1]: .6f}, {rot[0][2]: .6f}]",
        f"  [{rot[1][0]: .6f}, {rot[1][1]: .6f}, {rot[1][2]: .6f}]",
        f"  [{rot[2][0]: .6f}, {rot[2][1]: .6f}, {rot[2][2]: .6f}]",
        (
            f"Fit ({fit['point_count']} pts): RMSE {fit['reprojection_rmse_px']:.3f} px, "
            f"max {fit['max_reprojection_error_px']:.3f} px"
        ),
    ]
    for item in result["fit_points"]:
        ang = (
            f"{item['angular_error_deg']:.3f} deg"
            if item["angular_error_deg"] is not None
            else "behind-cam"
        )
        lines.append(
            f"  [fit] {item['point_id']}: err={item['error_px']:.3f} px ({ang}), "
            f"res=({item['residual_px'][0]:+.2f}, {item['residual_px'][1]:+.2f})"
        )
    if check is not None:
        lines.append(
            f"Check ({check['point_count']} pts): RMSE {check['reprojection_rmse_px']:.3f} px, "
            f"max {check['max_reprojection_error_px']:.3f} px"
        )
        for item in result["check_points"]:
            ang = (
                f"{item['angular_error_deg']:.3f} deg"
                if item["angular_error_deg"] is not None
                else "behind-cam"
            )
            lines.append(
                f"  [check] {item['point_id']}: err={item['error_px']:.3f} px ({ang}), "
                f"res=({item['residual_px'][0]:+.2f}, {item['residual_px'][1]:+.2f})"
            )
    else:
        lines.append("Check: none")
    for warn in result["warnings"]:
        lines.append(f"WARNING: {warn}")
    return "\n".join(lines)


def create_gui_app(root: Any, args: argparse.Namespace) -> Any:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    from PIL import Image, ImageTk

    class RtkCalibratorApp:
        def __init__(self, tk_root: tk.Tk) -> None:
            self.root = tk_root
            self.root.title("Chongming RTK Control-Point Calibrator")
            self.root.geometry("1380x860")

            self.image_path: Path | None = None
            self.csv_path: Path | None = None
            self.intrinsics_path: Path | None = None
            self.intrinsics_preset: str | None = None
            self.intrinsics: CameraIntrinsicsSpec | None = None

            self.pil_image: Image.Image | None = None
            self.tk_photo: ImageTk.PhotoImage | None = None
            self.image_size_px: tuple[int, int] | None = None

            self.points: list[ControlPoint] = []
            self.selected_id: str | None = None
            self.undo_stack: list[tuple[str, tuple[float, float] | None]] = []
            self.last_result: dict[str, Any] | None = None

            self.zoom = 1.0
            self.offset_x = 0.0
            self.offset_y = 0.0
            self._pan_start: tuple[int, int, float, float] | None = None
            self._space_held = False
            self.auto_advance_var = tk.BooleanVar(value=True)
            self.cursor_var = tk.StringVar(value="Cursor: (-, -)")
            self.status_var = tk.StringVar(
                value="Load a camera frame, ENU CSV, and camera intrinsics (or preset)."
            )

            self._build_ui()
            self._bind_events()
            self._load_initial_args(args)

        def _build_ui(self) -> None:
            toolbar = ttk.Frame(self.root, padding=6)
            toolbar.pack(side=tk.TOP, fill=tk.X)

            for text, cmd in (
                ("Load Image...", self._on_load_image),
                ("Load CSV...", self._on_load_csv),
                ("Load Intrinsics...", self._on_load_intrinsics),
                ("Preset 1080p DFOV122", self._on_use_preset),
                ("Open Corresp...", self._on_open_correspondences),
                ("Save Corresp...", self._on_save_correspondences),
                ("Solve Pose", self._on_solve_pose),
                ("Export Result...", self._on_export_result),
            ):
                ttk.Button(toolbar, text=text, command=cmd).pack(side=tk.LEFT, padx=3)

            ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
            ttk.Button(toolbar, text="Fit View", command=self._fit_image).pack(side=tk.LEFT, padx=2)
            ttk.Button(toolbar, text="1:1", command=self._zoom_one_to_one).pack(
                side=tk.LEFT, padx=2
            )
            ttk.Label(toolbar, textvariable=self.cursor_var).pack(side=tk.RIGHT, padx=6)

            paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
            paned.pack(fill=tk.BOTH, expand=True)

            left_frame = ttk.Frame(paned)
            paned.add(left_frame, weight=4)
            self.canvas = tk.Canvas(left_frame, bg="#1e1e1e", cursor="crosshair")
            self.canvas.pack(fill=tk.BOTH, expand=True)

            right_frame = ttk.Frame(paned, padding=6)
            paned.add(right_frame, weight=2)

            ttk.Label(
                right_frame,
                text="Control Points (click row to select, left-click image to mark; Shift/Right-drag to pan, wheel to zoom)",
                wraplength=420,
            ).pack(anchor=tk.W, pady=(0, 4))

            columns = ("id", "role", "pixel", "error")
            self.tree = ttk.Treeview(
                right_frame, columns=columns, show="headings", selectmode="browse", height=16
            )
            self.tree.heading("id", text="ID")
            self.tree.heading("role", text="Role")
            self.tree.heading("pixel", text="Pixel (u, v)")
            self.tree.heading("error", text="Error (px)")
            self.tree.column("id", width=90, anchor=tk.W)
            self.tree.column("role", width=60, anchor=tk.CENTER)
            self.tree.column("pixel", width=150, anchor=tk.CENTER)
            self.tree.column("error", width=90, anchor=tk.E)
            self.tree.pack(fill=tk.BOTH, expand=True)

            actions = ttk.Frame(right_frame, padding=(0, 6))
            actions.pack(fill=tk.X)
            ttk.Button(actions, text="Undo Click (u)", command=self._on_undo).pack(
                side=tk.LEFT, padx=2
            )
            ttk.Button(actions, text="Clear Point (Del)", command=self._on_clear_point).pack(
                side=tk.LEFT, padx=2
            )
            ttk.Button(actions, text="Toggle fit/check (t)", command=self._on_toggle_role).pack(
                side=tk.LEFT, padx=2
            )
            ttk.Checkbutton(actions, text="Auto-advance", variable=self.auto_advance_var).pack(
                side=tk.RIGHT
            )

            ttk.Label(right_frame, text="Solver Output / Residuals:").pack(anchor=tk.W)
            self.output_text = tk.Text(right_frame, height=16, wrap=tk.WORD)
            self.output_text.pack(fill=tk.BOTH, expand=True, pady=(2, 0))
            self.output_text.configure(state=tk.DISABLED)

            status_bar = ttk.Label(
                self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W, padding=4
            )
            status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        def _bind_events(self) -> None:
            self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
            self.canvas.bind("<Configure>", lambda _e: self._redraw_canvas())
            self.canvas.bind("<Motion>", self._on_mouse_move)
            self.canvas.bind("<Button-1>", self._on_left_click)
            self.canvas.bind("<B1-Motion>", self._on_left_drag)
            self.canvas.bind("<ButtonRelease-1>", self._on_pan_end)
            self.canvas.bind("<Shift-Button-1>", self._on_pan_start)
            self.canvas.bind("<Button-2>", self._on_pan_start)
            self.canvas.bind("<B2-Motion>", self._on_pan_drag)
            self.canvas.bind("<ButtonRelease-2>", self._on_pan_end)
            self.canvas.bind("<Button-3>", self._on_pan_start)
            self.canvas.bind("<B3-Motion>", self._on_pan_drag)
            self.canvas.bind("<ButtonRelease-3>", self._on_pan_end)
            self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)
            self.canvas.bind("<Button-4>", lambda e: self._zoom_at(e.x, e.y, 1.15))
            self.canvas.bind("<Button-5>", lambda e: self._zoom_at(e.x, e.y, 1.0 / 1.15))

            self.root.bind("<KeyPress-space>", lambda _e: setattr(self, "_space_held", True))
            self.root.bind("<KeyRelease-space>", lambda _e: setattr(self, "_space_held", False))
            self.root.bind("<Command-z>", lambda _e: self._on_undo())
            self.root.bind("<Control-z>", lambda _e: self._on_undo())
            self.root.bind("u", lambda _e: self._on_undo())
            self.root.bind("t", lambda _e: self._on_toggle_role())
            self.root.bind("<Delete>", lambda _e: self._on_clear_point())
            self.root.bind("<BackSpace>", lambda _e: self._on_clear_point())
            self.root.bind("<Command-s>", lambda _e: self._on_save_correspondences())
            self.root.bind("<Control-s>", lambda _e: self._on_save_correspondences())
            self.root.bind("+", lambda _e: self._zoom_center(1.25))
            self.root.bind("=", lambda _e: self._zoom_center(1.25))
            self.root.bind("-", lambda _e: self._zoom_center(0.8))

        def _load_initial_args(self, cli_args: argparse.Namespace) -> None:
            try:
                if cli_args.correspondences is not None:
                    self._load_correspondences_file(cli_args.correspondences)
                if cli_args.image is not None:
                    self._load_image_file(cli_args.image)
                if cli_args.csv is not None:
                    self._load_csv_file(cli_args.csv)
                if cli_args.intrinsics is not None:
                    self._load_intrinsics_file(cli_args.intrinsics)
                elif cli_args.preset == PRESET_MACBOOK_1080P_DFOV122:
                    self._on_use_preset()
            except CalibrationInputError as exc:
                messagebox.showerror("Load Error", str(exc), parent=self.root)

        def _set_output_text(self, content: str) -> None:
            self.output_text.configure(state=tk.NORMAL)
            self.output_text.delete("1.0", tk.END)
            self.output_text.insert("1.0", content)
            self.output_text.configure(state=tk.DISABLED)

        def _on_load_image(self) -> None:
            chosen = filedialog.askopenfilename(
                parent=self.root,
                title="Select Camera Frame Image",
                filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff"), ("All", "*.*")],
            )
            if chosen:
                try:
                    self._load_image_file(Path(chosen))
                except CalibrationInputError as exc:
                    messagebox.showerror("Image Error", str(exc), parent=self.root)

        def _load_image_file(self, path: Path) -> None:
            bgr = load_image_bgr(path)
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            self.pil_image = Image.fromarray(rgb)
            self.image_path = path
            self.image_size_px = (self.pil_image.width, self.pil_image.height)
            self.last_result = None
            self.root.after(20, self._fit_image)
            self.status_var.set(
                f"Loaded image {path.name} ({self.pil_image.width}x{self.pil_image.height})"
            )

        def _on_load_csv(self) -> None:
            chosen = filedialog.askopenfilename(
                parent=self.root,
                title="Select Control Points ENU CSV",
                filetypes=[("CSV", "*.csv"), ("All", "*.*")],
            )
            if chosen:
                try:
                    self._load_csv_file(Path(chosen))
                except CalibrationInputError as exc:
                    messagebox.showerror("CSV Error", str(exc), parent=self.root)

        def _load_csv_file(self, path: Path) -> None:
            loaded = load_control_points_csv(path)
            existing_clicks = {pt.point_id: pt.pixel_xy for pt in self.points if pt.pixel_xy}
            self.points = [
                ControlPoint(
                    point_id=pt.point_id,
                    east_m=pt.east_m,
                    north_m=pt.north_m,
                    up_m=pt.up_m,
                    role=pt.role,
                    note=pt.note,
                    pixel_xy=existing_clicks.get(pt.point_id),
                )
                for pt in loaded
            ]
            self.csv_path = path
            self.last_result = None
            if self.points and (
                self.selected_id is None
                or not any(pt.point_id == self.selected_id for pt in self.points)
            ):
                self.selected_id = self.points[0].point_id
            self._refresh_tree()
            self._redraw_canvas()
            self.status_var.set(f"Loaded {len(self.points)} control points from {path.name}")

        def _on_load_intrinsics(self) -> None:
            chosen = filedialog.askopenfilename(
                parent=self.root,
                title="Select Camera Intrinsics JSON",
                filetypes=[("JSON", "*.json"), ("All", "*.*")],
            )
            if chosen:
                try:
                    self._load_intrinsics_file(Path(chosen))
                except CalibrationInputError as exc:
                    messagebox.showerror("Intrinsics Error", str(exc), parent=self.root)

        def _load_intrinsics_file(self, path: Path) -> None:
            self.intrinsics = load_intrinsics_json(path)
            self.intrinsics_path = path
            self.intrinsics_preset = None
            self.status_var.set(
                f"Loaded intrinsics {self.intrinsics.image_width_px}x{self.intrinsics.image_height_px} from {path.name}"
            )

        def _on_use_preset(self) -> None:
            self.intrinsics = macbook_1080p_dfov122_intrinsics()
            self.intrinsics_path = None
            self.intrinsics_preset = PRESET_MACBOOK_1080P_DFOV122
            self.status_var.set("Using MacBook 1080p DFOV 122 deg pinhole preset (1920x1080).")

        def _on_open_correspondences(self) -> None:
            chosen = filedialog.askopenfilename(
                parent=self.root,
                title="Open Saved Correspondences JSON",
                filetypes=[("JSON", "*.json"), ("All", "*.*")],
            )
            if chosen:
                try:
                    self._load_correspondences_file(Path(chosen))
                except CalibrationInputError as exc:
                    messagebox.showerror("Open Correspondences Error", str(exc), parent=self.root)

        def _load_correspondences_file(self, path: Path) -> None:
            loaded = load_correspondences_json(path)
            if loaded.image_path is not None:
                self._load_image_file(loaded.image_path)
            self.points = loaded.points
            self.csv_path = loaded.csv_path
            self.undo_stack.clear()
            self.last_result = None
            if loaded.intrinsics_path is not None and loaded.intrinsics_path.is_file():
                self._load_intrinsics_file(loaded.intrinsics_path)
            elif loaded.intrinsics_preset == PRESET_MACBOOK_1080P_DFOV122:
                self._on_use_preset()
            self.selected_id = self.points[0].point_id if self.points else None
            self._refresh_tree()
            self._redraw_canvas()
            self.status_var.set(f"Reloaded {len(self.points)} correspondences from {path.name}")

        def _on_save_correspondences(self) -> None:
            if not self.points:
                messagebox.showwarning(
                    "Nothing to Save", "Load a control-point CSV first.", parent=self.root
                )
                return
            default_name = (
                f"{self.image_path.stem}_correspondences.json"
                if self.image_path is not None
                else "correspondences.json"
            )
            chosen = filedialog.asksaveasfilename(
                parent=self.root,
                title="Save Correspondences JSON",
                initialfile=default_name,
                defaultextension=".json",
                filetypes=[("JSON", "*.json")],
            )
            if chosen:
                save_correspondences_json(
                    Path(chosen),
                    image_path=self.image_path,
                    image_size_px=self.image_size_px,
                    csv_path=self.csv_path,
                    intrinsics_path=self.intrinsics_path,
                    intrinsics_preset=self.intrinsics_preset,
                    points=self.points,
                )
                self.status_var.set(f"Saved correspondences to {chosen}")

        def _on_solve_pose(self) -> None:
            if self.image_size_px is None:
                messagebox.showerror("Solve Error", "Load a camera frame first.", parent=self.root)
                return
            if self.intrinsics is None:
                messagebox.showerror(
                    "Solve Error",
                    "Load camera intrinsics JSON or select the 1080p DFOV122 preset first.",
                    parent=self.root,
                )
                return
            try:
                validate_image_resolution(
                    self.image_size_px[0], self.image_size_px[1], self.intrinsics
                )
                self.last_result = solve_rtk_pose(
                    self.points, self.intrinsics, image_path=self.image_path
                )
            except CalibrationInputError as exc:
                messagebox.showerror("Calibration Error", str(exc), parent=self.root)
                return

            summary = format_result_summary(self.last_result)
            self._set_output_text(summary)
            self._refresh_tree()
            self._redraw_canvas()
            fit_rmse = self.last_result["fit_summary"]["reprojection_rmse_px"]
            self.status_var.set(
                f"Solved ({self.last_result['status']}): fit RMSE {fit_rmse:.3f} px"
            )
            if self.last_result["status"] == "ambiguous_planar":
                messagebox.showwarning(
                    "Ambiguous Planar Solution",
                    "\n\n".join(self.last_result["warnings"]),
                    parent=self.root,
                )

        def _on_export_result(self) -> None:
            if self.last_result is None:
                messagebox.showwarning(
                    "Nothing to Export", "Run 'Solve Pose' first.", parent=self.root
                )
                return
            default_name = (
                f"{self.image_path.stem}_calibration.json"
                if self.image_path is not None
                else "calibration.json"
            )
            chosen = filedialog.asksaveasfilename(
                parent=self.root,
                title="Export Calibration Result JSON",
                initialfile=default_name,
                defaultextension=".json",
                filetypes=[("JSON", "*.json")],
            )
            if chosen:
                Path(chosen).write_text(
                    json.dumps(self.last_result, indent=2) + "\n", encoding="utf-8"
                )
                self.status_var.set(f"Exported calibration JSON to {chosen}")

        def _refresh_tree(self) -> None:
            error_by_id: dict[str, str] = {}
            if self.last_result is not None:
                for item in self.last_result["fit_points"] + self.last_result["check_points"]:
                    error_by_id[item["point_id"]] = f"{item['error_px']:.2f}"
            for row_id in self.tree.get_children():
                self.tree.delete(row_id)
            for pt in self.points:
                px_text = (
                    f"{pt.pixel_xy[0]:.2f}, {pt.pixel_xy[1]:.2f}"
                    if pt.pixel_xy is not None
                    else "-"
                )
                self.tree.insert(
                    "",
                    tk.END,
                    iid=pt.point_id,
                    values=(pt.point_id, pt.role, px_text, error_by_id.get(pt.point_id, "-")),
                )
            if self.selected_id is not None and self.tree.exists(self.selected_id):
                self.tree.selection_set(self.selected_id)
                self.tree.see(self.selected_id)

        def _on_tree_select(self, _event: object) -> None:
            sel = self.tree.selection()
            if sel:
                self.selected_id = sel[0]
                self._redraw_canvas()

        def _canvas_to_image(self, cx: float, cy: float) -> tuple[float, float]:
            return ((cx - self.offset_x) / self.zoom, (cy - self.offset_y) / self.zoom)

        def _image_to_canvas(self, ix: float, iy: float) -> tuple[float, float]:
            return (ix * self.zoom + self.offset_x, iy * self.zoom + self.offset_y)

        def _fit_image(self) -> None:
            if self.pil_image is None:
                return
            cw = max(1, self.canvas.winfo_width())
            ch = max(1, self.canvas.winfo_height())
            self.zoom = min(cw / self.pil_image.width, ch / self.pil_image.height)
            self.offset_x = (cw - self.pil_image.width * self.zoom) * 0.5
            self.offset_y = (ch - self.pil_image.height * self.zoom) * 0.5
            self._redraw_canvas()

        def _zoom_one_to_one(self) -> None:
            if self.pil_image is None:
                return
            cw = max(1, self.canvas.winfo_width())
            ch = max(1, self.canvas.winfo_height())
            self._zoom_at(cw * 0.5, ch * 0.5, 1.0 / max(self.zoom, 1e-6))

        def _zoom_center(self, factor: float) -> None:
            cw = max(1, self.canvas.winfo_width())
            ch = max(1, self.canvas.winfo_height())
            self._zoom_at(cw * 0.5, ch * 0.5, factor)

        def _zoom_at(self, cx: float, cy: float, factor: float) -> None:
            if self.pil_image is None:
                return
            ix, iy = self._canvas_to_image(cx, cy)
            self.zoom = max(0.1, min(32.0, self.zoom * factor))
            self.offset_x = cx - ix * self.zoom
            self.offset_y = cy - iy * self.zoom
            self._redraw_canvas()

        def _on_mouse_wheel(self, event: tk.Event[tk.Canvas]) -> None:
            if event.delta == 0:
                return
            factor = 1.15 if event.delta > 0 else 1.0 / 1.15
            self._zoom_at(event.x, event.y, factor)

        def _on_mouse_move(self, event: tk.Event[tk.Canvas]) -> None:
            if self.pil_image is None:
                return
            ix, iy = self._canvas_to_image(event.x, event.y)
            if 0.0 <= ix < self.pil_image.width and 0.0 <= iy < self.pil_image.height:
                self.cursor_var.set(f"Cursor: ({ix:.2f}, {iy:.2f})")
            else:
                self.cursor_var.set("Cursor: (out of bounds)")

        def _on_pan_start(self, event: tk.Event[tk.Canvas]) -> None:
            self._pan_start = (event.x, event.y, self.offset_x, self.offset_y)

        def _on_pan_drag(self, event: tk.Event[tk.Canvas]) -> None:
            if self._pan_start is None:
                return
            sx, sy, ox, oy = self._pan_start
            self.offset_x = ox + (event.x - sx)
            self.offset_y = oy + (event.y - sy)
            self._redraw_canvas()

        def _on_pan_end(self, _event: tk.Event[tk.Canvas]) -> None:
            self._pan_start = None

        def _on_left_drag(self, event: tk.Event[tk.Canvas]) -> None:
            if self._pan_start is not None:
                self._on_pan_drag(event)

        def _on_left_click(self, event: tk.Event[tk.Canvas]) -> None:
            if self._space_held:
                self._on_pan_start(event)
                return
            if self.pil_image is None or self.selected_id is None:
                return
            ix, iy = self._canvas_to_image(event.x, event.y)
            if not (0.0 <= ix < self.pil_image.width and 0.0 <= iy < self.pil_image.height):
                self.status_var.set("Click ignored: cursor is outside image bounds.")
                return
            for idx, pt in enumerate(self.points):
                if pt.point_id == self.selected_id:
                    self.undo_stack.append((pt.point_id, pt.pixel_xy))
                    self.points[idx] = ControlPoint(
                        point_id=pt.point_id,
                        east_m=pt.east_m,
                        north_m=pt.north_m,
                        up_m=pt.up_m,
                        role=pt.role,
                        note=pt.note,
                        pixel_xy=(ix, iy),
                    )
                    self.last_result = None
                    self.status_var.set(f"Marked {pt.point_id} at ({ix:.2f}, {iy:.2f})")
                    if self.auto_advance_var.get():
                        for offset in range(1, len(self.points) + 1):
                            nxt = self.points[(idx + offset) % len(self.points)]
                            if nxt.pixel_xy is None:
                                self.selected_id = nxt.point_id
                                break
                    break
            self._refresh_tree()
            self._redraw_canvas()

        def _on_undo(self) -> None:
            if not self.undo_stack:
                self.status_var.set("Nothing to undo.")
                return
            target_id, previous_xy = self.undo_stack.pop()
            for idx, pt in enumerate(self.points):
                if pt.point_id == target_id:
                    self.points[idx] = ControlPoint(
                        point_id=pt.point_id,
                        east_m=pt.east_m,
                        north_m=pt.north_m,
                        up_m=pt.up_m,
                        role=pt.role,
                        note=pt.note,
                        pixel_xy=previous_xy,
                    )
                    self.selected_id = target_id
                    self.last_result = None
                    self.status_var.set(f"Undid click for {target_id}.")
                    break
            self._refresh_tree()
            self._redraw_canvas()

        def _on_clear_point(self) -> None:
            if self.selected_id is None:
                return
            for idx, pt in enumerate(self.points):
                if pt.point_id == self.selected_id and pt.pixel_xy is not None:
                    self.undo_stack.append((pt.point_id, pt.pixel_xy))
                    self.points[idx] = ControlPoint(
                        point_id=pt.point_id,
                        east_m=pt.east_m,
                        north_m=pt.north_m,
                        up_m=pt.up_m,
                        role=pt.role,
                        note=pt.note,
                        pixel_xy=None,
                    )
                    self.last_result = None
                    self.status_var.set(f"Cleared click for {pt.point_id}.")
                    break
            self._refresh_tree()
            self._redraw_canvas()

        def _on_toggle_role(self) -> None:
            if self.selected_id is None:
                return
            for idx, pt in enumerate(self.points):
                if pt.point_id == self.selected_id:
                    new_role = "check" if pt.role == "fit" else "fit"
                    self.points[idx] = ControlPoint(
                        point_id=pt.point_id,
                        east_m=pt.east_m,
                        north_m=pt.north_m,
                        up_m=pt.up_m,
                        role=new_role,
                        note=pt.note,
                        pixel_xy=pt.pixel_xy,
                    )
                    self.last_result = None
                    self.status_var.set(f"Set {pt.point_id} role to {new_role}.")
                    break
            self._refresh_tree()
            self._redraw_canvas()

        def _redraw_canvas(self) -> None:
            self.canvas.delete("all")
            if self.pil_image is None:
                return
            cw = max(1, self.canvas.winfo_width())
            ch = max(1, self.canvas.winfo_height())

            # Crop only the visible image region so zooming deep into 1080p stays fast.
            x0 = max(0, math.floor(-self.offset_x / self.zoom))
            y0 = max(0, math.floor(-self.offset_y / self.zoom))
            x1 = min(self.pil_image.width, math.ceil((cw - self.offset_x) / self.zoom))
            y1 = min(self.pil_image.height, math.ceil((ch - self.offset_y) / self.zoom))
            if x1 > x0 and y1 > y0:
                cropped = self.pil_image.crop((x0, y0, x1, y1))
                disp_w = max(1, round((x1 - x0) * self.zoom))
                disp_h = max(1, round((y1 - y0) * self.zoom))
                resample = (
                    Image.Resampling.NEAREST if self.zoom >= 3.0 else Image.Resampling.BILINEAR
                )
                resized = cropped.resize((disp_w, disp_h), resample)
                self.tk_photo = ImageTk.PhotoImage(resized)
                cx, cy = self._image_to_canvas(x0, y0)
                self.canvas.create_image(cx, cy, anchor=tk.NW, image=self.tk_photo)

            projected_map: dict[str, tuple[float, float]] = {}
            if self.last_result is not None:
                for item in self.last_result["fit_points"] + self.last_result["check_points"]:
                    if item["in_front_of_camera"]:
                        projected_map[item["point_id"]] = (
                            item["projected_px"][0],
                            item["projected_px"][1],
                        )

            for pt in self.points:
                if pt.pixel_xy is None:
                    continue
                px, py = self._image_to_canvas(pt.pixel_xy[0], pt.pixel_xy[1])
                color = "#00e676" if pt.role == "fit" else "#ffb300"
                is_sel = pt.point_id == self.selected_id
                r = 8 if is_sel else 5
                if pt.role == "fit":
                    self.canvas.create_oval(px - r, py - r, px + r, py + r, outline=color, width=2)
                else:
                    self.canvas.create_rectangle(
                        px - r, py - r, px + r, py + r, outline=color, width=2
                    )
                self.canvas.create_line(px - 10, py, px + 10, py, fill=color, width=1)
                self.canvas.create_line(px, py - 10, px, py + 10, fill=color, width=1)
                self.canvas.create_text(
                    px + 10, py - 10, text=pt.point_id, fill=color, anchor=tk.SW
                )
                if pt.point_id in projected_map:
                    rx, ry = self._image_to_canvas(*projected_map[pt.point_id])
                    self.canvas.create_line(px, py, rx, ry, fill="#ff5252", width=1)
                    self.canvas.create_line(rx - 4, ry - 4, rx + 4, ry + 4, fill="#ff5252", width=2)
                    self.canvas.create_line(rx - 4, ry + 4, rx + 4, ry - 4, fill="#ff5252", width=2)

    return RtkCalibratorApp(root)


def _launch_gui(args: argparse.Namespace) -> int:
    import tkinter as tk

    root = tk.Tk()
    create_gui_app(root, args)
    root.mainloop()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, help="Camera frame image path")
    parser.add_argument("--csv", type=Path, help="Surveyed control-point ENU CSV path")
    parser.add_argument("--correspondences", type=Path, help="Saved correspondences JSON path")
    parser.add_argument("--intrinsics", type=Path, help="Camera intrinsics JSON path")
    parser.add_argument(
        "--preset",
        choices=(PRESET_MACBOOK_1080P_DFOV122,),
        help="Built-in nominal camera intrinsics preset",
    )
    parser.add_argument(
        "--solve",
        action="store_true",
        help="Run headless pose solver on --correspondences without launching GUI",
    )
    parser.add_argument("--output", type=Path, help="Calibration result JSON output path")
    parser.add_argument(
        "--write-preset-intrinsics",
        type=Path,
        help="Write the MacBook 1080p DFOV122 nominal intrinsics JSON to PATH and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.intrinsics is not None and args.preset is not None:
        parser.error("Use either --intrinsics or --preset, not both.")

    if args.write_preset_intrinsics is not None:
        spec = macbook_1080p_dfov122_intrinsics()
        args.write_preset_intrinsics.parent.mkdir(parents=True, exist_ok=True)
        args.write_preset_intrinsics.write_text(
            json.dumps(intrinsics_to_dict(spec), indent=2) + "\n", encoding="utf-8"
        )
        print(f"Wrote nominal intrinsics preset to {args.write_preset_intrinsics}")
        return 0

    if args.solve:
        if args.correspondences is None:
            parser.error("--solve requires --correspondences.")
        loaded = load_correspondences_json(args.correspondences)
        image_path = args.image or loaded.image_path
        if image_path is not None:
            bgr = load_image_bgr(image_path)
            img_h, img_w = int(bgr.shape[0]), int(bgr.shape[1])
        elif loaded.image_width_px is not None and loaded.image_height_px is not None:
            img_w, img_h = loaded.image_width_px, loaded.image_height_px
        else:
            raise CalibrationInputError(
                "Actual image resolution is unknown; provide --image or image dimensions in correspondences."
            )

        if args.intrinsics is not None:
            intrinsics = load_intrinsics_json(args.intrinsics)
        elif args.preset == PRESET_MACBOOK_1080P_DFOV122:
            intrinsics = macbook_1080p_dfov122_intrinsics()
        elif loaded.intrinsics_path is not None:
            intrinsics = load_intrinsics_json(loaded.intrinsics_path)
        elif loaded.intrinsics_preset == PRESET_MACBOOK_1080P_DFOV122:
            intrinsics = macbook_1080p_dfov122_intrinsics()
        else:
            raise CalibrationInputError(
                "Camera intrinsics are required: pass --intrinsics <json> or --preset macbook-1080p-dfov122."
            )

        validate_image_resolution(img_w, img_h, intrinsics)
        result = solve_rtk_pose(loaded.points, intrinsics, image_path=image_path)
        print(format_result_summary(result))
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            print(f"Exported calibration result to {args.output}")
        return 0

    return _launch_gui(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CalibrationInputError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

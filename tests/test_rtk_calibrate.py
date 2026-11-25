from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from scripts.rtk_calibrate import (
    CalibrationInputError,
    CameraIntrinsicsSpec,
    ControlPoint,
    load_control_points_csv,
    load_correspondences_json,
    load_intrinsics_json,
    macbook_1080p_dfov122_intrinsics,
    main,
    save_correspondences_json,
    solve_rtk_pose,
    validate_image_resolution,
)


def _project_points(
    points_enu: list[tuple[float, float, float]],
    optical_center_enu_m: tuple[float, float, float],
    rot_enu_from_cam: np.ndarray,
    intrinsics: CameraIntrinsicsSpec,
) -> list[tuple[float, float]]:
    rot_cam_from_enu = rot_enu_from_cam.T
    center = np.asarray(optical_center_enu_m, dtype=np.float64)
    tvec = (-rot_cam_from_enu @ center).reshape(3, 1)
    rvec, _ = cv2.Rodrigues(rot_cam_from_enu)
    obj = np.asarray(points_enu, dtype=np.float64)
    projected, _ = cv2.projectPoints(
        obj, rvec, tvec, intrinsics.matrix_px, intrinsics.distortion_array()
    )
    pts = projected.reshape(-1, 2)
    return [(float(pt[0]), float(pt[1])) for pt in pts]


def _yaw_pitch_rotation(yaw_deg: float, pitch_deg: float) -> np.ndarray:
    """Return R_enu_from_cam for a camera looking along ENU direction with right=+E at 0."""
    # Base camera: +X=East, +Y=-Up (down), +Z=North (forward)
    base = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]], dtype=np.float64)
    yaw = math.radians(yaw_deg)
    pitch = math.radians(pitch_deg)
    r_yaw = np.array(
        [
            [math.cos(yaw), math.sin(yaw), 0.0],
            [-math.sin(yaw), math.cos(yaw), 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    r_pitch = np.array(
        [
            [1.0, 0.0, 0.0],
            [0.0, math.cos(pitch), -math.sin(pitch)],
            [0.0, math.sin(pitch), math.cos(pitch)],
        ],
        dtype=np.float64,
    )
    return r_yaw @ base @ r_pitch


class RtkCalibrateTests(unittest.TestCase):
    def test_known_pose_with_distortion_and_heldout_check_point(self) -> None:
        intrinsics = CameraIntrinsicsSpec(
            image_width_px=1920,
            image_height_px=1080,
            matrix_px=np.array(
                [[620.0, 0.0, 960.0], [0.0, 620.0, 540.0], [0.0, 0.0, 1.0]],
                dtype=np.float64,
            ),
            distortion_model="brown_conrady",
            distortion_coefficients=(-0.08, 0.02, 0.001, -0.001, 0.0),
            source="test-distorted",
        )
        true_center = (12.5, -8.0, 3.2)
        true_rot_enu_from_cam = _yaw_pitch_rotation(yaw_deg=18.0, pitch_deg=6.0)

        world_points = [
            (-8.0, 28.0, 0.5),
            (18.0, 32.0, 1.2),
            (24.0, 45.0, 9.5),
            (-2.0, 50.0, 11.0),
            (10.0, 22.0, 4.0),
            (14.0, 38.0, 6.0),  # held-out check point
        ]
        pixels = _project_points(world_points, true_center, true_rot_enu_from_cam, intrinsics)
        points = [
            ControlPoint(
                point_id=f"P{idx + 1}",
                east_m=w[0],
                north_m=w[1],
                up_m=w[2],
                role="check" if idx == 5 else "fit",
                pixel_xy=px,
            )
            for idx, (w, px) in enumerate(zip(world_points, pixels))
        ]

        result = solve_rtk_pose(points, intrinsics)
        self.assertEqual(result["status"], "ok")
        est_center = np.asarray(result["pose"]["optical_center_enu_m"], dtype=np.float64)
        est_rot = np.asarray(result["pose"]["rotation_enu_from_cam"], dtype=np.float64)

        np.testing.assert_allclose(est_center, true_center, atol=1e-4)
        np.testing.assert_allclose(est_rot, true_rot_enu_from_cam, atol=1e-5)
        np.testing.assert_allclose(est_rot.T @ est_rot, np.eye(3), atol=1e-9)
        self.assertLess(result["fit_summary"]["reprojection_rmse_px"], 1e-4)
        self.assertEqual(result["check_summary"]["point_count"], 1)
        self.assertLess(result["check_summary"]["reprojection_rmse_px"], 1e-4)

        # Perturbing the held-out check point must not change the fitted pose or fit RMSE.
        perturbed_points = list(points)
        chk = perturbed_points[5]
        assert chk.pixel_xy is not None
        perturbed_points[5] = ControlPoint(
            point_id=chk.point_id,
            east_m=chk.east_m,
            north_m=chk.north_m,
            up_m=chk.up_m,
            role="check",
            pixel_xy=(chk.pixel_xy[0] + 12.0, chk.pixel_xy[1] - 5.0),
        )
        perturbed_result = solve_rtk_pose(perturbed_points, intrinsics)
        np.testing.assert_allclose(
            perturbed_result["pose"]["optical_center_enu_m"], est_center, atol=1e-9
        )
        np.testing.assert_allclose(
            perturbed_result["pose"]["rotation_enu_from_cam"], est_rot, atol=1e-9
        )
        self.assertAlmostEqual(perturbed_result["check_points"][0]["error_px"], 13.0, places=3)

    def test_planar_ambiguity_is_reported(self) -> None:
        intrinsics = macbook_1080p_dfov122_intrinsics()
        true_center = (0.0, -30.0, 2.0)
        true_rot = _yaw_pitch_rotation(yaw_deg=0.0, pitch_deg=-3.0)
        # Planar vertical board tilted at ~35 degrees so IPPE yields a clear second branch.
        board_pts = [
            (-5.0, 20.0, 1.0),
            (5.0, 23.0, 1.0),
            (5.0, 23.0, 7.0),
            (-5.0, 20.0, 7.0),
        ]
        pixels = _project_points(board_pts, true_center, true_rot, intrinsics)
        points = [
            ControlPoint(
                point_id=f"B{idx + 1}",
                east_m=w[0],
                north_m=w[1],
                up_m=w[2],
                role="fit",
                pixel_xy=px,
            )
            for idx, (w, px) in enumerate(zip(board_pts, pixels))
        ]
        result = solve_rtk_pose(points, intrinsics)
        self.assertTrue(result["geometry"]["is_planar"])
        self.assertEqual(result["status"], "ambiguous_planar")
        self.assertTrue(result["alternate_planar_solutions"])
        self.assertTrue(any("Ambiguous planar" in w for w in result["warnings"]))

    def test_degenerate_and_invalid_inputs_raise_actionable_errors(self) -> None:
        intrinsics = macbook_1080p_dfov122_intrinsics()
        # Fewer than 4 fit points (even with extra check points).
        pts_few = [
            ControlPoint(
                point_id="P1", east_m=0.0, north_m=10.0, up_m=0.0, pixel_xy=(800.0, 500.0)
            ),
            ControlPoint(
                point_id="P2", east_m=5.0, north_m=10.0, up_m=0.0, pixel_xy=(900.0, 500.0)
            ),
            ControlPoint(
                point_id="P3", east_m=5.0, north_m=15.0, up_m=2.0, pixel_xy=(900.0, 400.0)
            ),
            ControlPoint(
                point_id="P4",
                east_m=0.0,
                north_m=15.0,
                up_m=2.0,
                role="check",
                pixel_xy=(800.0, 400.0),
            ),
        ]
        with self.assertRaisesRegex(CalibrationInputError, "at least 4 clicked 'fit'"):
            solve_rtk_pose(pts_few, intrinsics)

        # Collinear 3D fit points.
        pts_collinear = [
            ControlPoint(
                point_id=f"C{i}",
                east_m=float(i),
                north_m=10.0,
                up_m=0.0,
                pixel_xy=(600.0 + i * 50, 500.0 + i * 20),
            )
            for i in range(4)
        ]
        with self.assertRaisesRegex(CalibrationInputError, "collinear"):
            solve_rtk_pose(pts_collinear, intrinsics)

        # Resolution mismatch.
        with self.assertRaisesRegex(CalibrationInputError, "does not match intrinsics resolution"):
            validate_image_resolution(1280, 720, intrinsics)

    def test_correspondence_roundtrip_and_headless_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            img_path = root / "frame.png"
            csv_path = root / "points.csv"
            corr_path = root / "correspondences.json"
            out_path = root / "calibration.json"
            preset_path = root / "intrinsics.json"

            cv2.imwrite(str(img_path), np.zeros((1080, 1920, 3), dtype=np.uint8))
            intrinsics = macbook_1080p_dfov122_intrinsics()
            true_center = (1.0, -5.0, 1.5)
            true_rot = _yaw_pitch_rotation(yaw_deg=-10.0, pitch_deg=4.0)
            world_pts = [
                (-6.0, 20.0, 0.0),
                (8.0, 22.0, 0.5),
                (9.0, 32.0, 6.0),
                (-5.0, 28.0, 5.5),
                (2.0, 25.0, 3.0),
            ]
            pixels = _project_points(world_pts, true_center, true_rot, intrinsics)

            csv_lines = ["point_id,east_m,north_m,up_m,role,note"]
            for idx, w in enumerate(world_pts):
                role = "check" if idx == 4 else "fit"
                csv_lines.append(f"M{idx + 1},{w[0]},{w[1]},{w[2]},{role},marker {idx + 1}")
            csv_path.write_text("\n".join(csv_lines) + "\n", encoding="utf-8")

            loaded_csv = load_control_points_csv(csv_path)
            self.assertEqual(len(loaded_csv), 5)
            clicked = [
                ControlPoint(
                    point_id=pt.point_id,
                    east_m=pt.east_m,
                    north_m=pt.north_m,
                    up_m=pt.up_m,
                    role=pt.role,
                    note=pt.note,
                    pixel_xy=pixels[idx],
                )
                for idx, pt in enumerate(loaded_csv)
            ]

            save_correspondences_json(
                corr_path,
                image_path=img_path,
                image_size_px=(1920, 1080),
                csv_path=csv_path,
                intrinsics_path=None,
                intrinsics_preset="macbook-1080p-dfov122",
                points=clicked,
            )
            reloaded = load_correspondences_json(corr_path)
            self.assertEqual(reloaded.points, clicked)

            self.assertEqual(main(["--write-preset-intrinsics", str(preset_path)]), 0)
            loaded_intr = load_intrinsics_json(preset_path)
            self.assertEqual(loaded_intr.image_width_px, 1920)
            self.assertEqual(loaded_intr.image_height_px, 1080)

            exit_code = main(
                [
                    "--solve",
                    "--correspondences",
                    str(corr_path),
                    "--intrinsics",
                    str(preset_path),
                    "--output",
                    str(out_path),
                ]
            )
            self.assertEqual(exit_code, 0)
            exported = json.loads(out_path.read_text(encoding="utf-8"))
            np.testing.assert_allclose(
                exported["pose"]["optical_center_enu_m"], true_center, atol=1e-4
            )
            np.testing.assert_allclose(
                exported["pose"]["rotation_enu_from_cam"], true_rot, atol=1e-5
            )
            self.assertLess(exported["check_summary"]["reprojection_rmse_px"], 1e-4)

    def test_load_camera_model_json_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "camera_model.json"
            path.write_text(
                json.dumps(
                    {
                        "intrinsics": {
                            "image_width_px": 1920,
                            "image_height_px": 1080,
                            "matrix_px": [
                                [610.5, 0.0, 960.0],
                                [0.0, 610.5, 540.0],
                                [0.0, 0.0, 1.0],
                            ],
                        },
                        "distortion": {"model": "none", "coefficients": []},
                    }
                ),
                encoding="utf-8",
            )
            spec = load_intrinsics_json(path)
            self.assertEqual(spec.image_width_px, 1920)
            self.assertAlmostEqual(float(spec.matrix_px[0, 0]), 610.5)


if __name__ == "__main__":
    unittest.main()

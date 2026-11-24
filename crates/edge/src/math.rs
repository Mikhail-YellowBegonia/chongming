//! Deterministic 64-bit geometry, Brown-Conrady distortion, Rodrigues, and linear algebra helpers.

use thiserror::Error;

#[derive(Debug, Error)]
pub enum MathError {
    #[error("{0}")]
    Invalid(String),
}

pub type Vec3 = [f64; 3];
pub type Mat2 = [[f64; 2]; 2];
pub type Mat3 = [[f64; 3]; 3];
pub type Mat4 = [[f64; 4]; 4];

pub fn dot3(a: Vec3, b: Vec3) -> f64 {
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}

pub fn cross3(a: Vec3, b: Vec3) -> Vec3 {
    [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]
}

pub fn norm3(a: Vec3) -> f64 {
    dot3(a, a).sqrt()
}

pub fn sub3(a: Vec3, b: Vec3) -> Vec3 {
    [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
}

pub fn add3(a: Vec3, b: Vec3) -> Vec3 {
    [a[0] + b[0], a[1] + b[1], a[2] + b[2]]
}

pub fn scale3(a: Vec3, s: f64) -> Vec3 {
    [a[0] * s, a[1] * s, a[2] * s]
}

pub fn mat3_vec(m: &Mat3, v: Vec3) -> Vec3 {
    [
        m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
        m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
        m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
    ]
}

pub fn mat3_mul(a: &Mat3, b: &Mat3) -> Mat3 {
    let mut out = [[0.0; 3]; 3];
    for i in 0..3 {
        for j in 0..3 {
            out[i][j] = a[i][0] * b[0][j] + a[i][1] * b[1][j] + a[i][2] * b[2][j];
        }
    }
    out
}

pub fn mat3_transpose(m: &Mat3) -> Mat3 {
    [
        [m[0][0], m[1][0], m[2][0]],
        [m[0][1], m[1][1], m[2][1]],
        [m[0][2], m[1][2], m[2][2]],
    ]
}

pub fn mat3_det(m: &Mat3) -> f64 {
    m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
}

pub fn mat3_from_slice(vals: &[f64], label: &str) -> Result<Mat3, MathError> {
    if vals.len() != 9 || vals.iter().any(|x| !x.is_finite()) {
        return Err(MathError::Invalid(format!(
            "{label} must be a finite 3x3 matrix"
        )));
    }
    Ok([
        [vals[0], vals[1], vals[2]],
        [vals[3], vals[4], vals[5]],
        [vals[6], vals[7], vals[8]],
    ])
}

pub fn is_proper_rotation(r: &Mat3, atol: f64) -> bool {
    if r.iter().flatten().any(|x| !x.is_finite()) {
        return false;
    }
    if mat3_det(r) <= 0.0 {
        return false;
    }
    let rt_r = mat3_mul(&mat3_transpose(r), r);
    for (i, row) in rt_r.iter().enumerate() {
        for (j, &val) in row.iter().enumerate() {
            let target = if i == j { 1.0 } else { 0.0 };
            if (val - target).abs() > atol {
                return false;
            }
        }
    }
    true
}

pub fn mat2_mul(a: &Mat2, b: &Mat2) -> Mat2 {
    [
        [
            a[0][0] * b[0][0] + a[0][1] * b[1][0],
            a[0][0] * b[0][1] + a[0][1] * b[1][1],
        ],
        [
            a[1][0] * b[0][0] + a[1][1] * b[1][0],
            a[1][0] * b[0][1] + a[1][1] * b[1][1],
        ],
    ]
}

pub fn mat2_transpose(m: &Mat2) -> Mat2 {
    [[m[0][0], m[1][0]], [m[0][1], m[1][1]]]
}

pub fn invert_2x2(m: &Mat2) -> Result<Mat2, MathError> {
    let det = m[0][0] * m[1][1] - m[0][1] * m[1][0];
    if !det.is_finite() || det.abs() <= 1e-18 {
        return Err(MathError::Invalid("singular 2x2 matrix".to_string()));
    }
    let inv = 1.0 / det;
    Ok([
        [m[1][1] * inv, -m[0][1] * inv],
        [-m[1][0] * inv, m[0][0] * inv],
    ])
}

/// Return a PSD square root `F` such that `F * F^T == covariance`.
pub fn covariance_factor_2x2(covariance: Mat2, label: &str) -> Result<Mat2, MathError> {
    if covariance.iter().flatten().any(|x| !x.is_finite()) {
        return Err(MathError::Invalid(format!(
            "{label} must be a finite 2x2 covariance"
        )));
    }
    let scale = covariance
        .iter()
        .flatten()
        .fold(0.0_f64, |acc, &x| acc.max(x.abs()));
    if scale == 0.0 {
        return Ok([[0.0, 0.0], [0.0, 0.0]]);
    }
    let tol = 32.0 * 2.0 * f64::EPSILON;
    let norm = [
        [covariance[0][0] / scale, covariance[0][1] / scale],
        [covariance[1][0] / scale, covariance[1][1] / scale],
    ];
    if (norm[0][1] - norm[1][0]).abs() > tol {
        return Err(MathError::Invalid(format!("{label} must be symmetric")));
    }
    let a = norm[0][0];
    let b = 0.5 * (norm[0][1] + norm[1][0]);
    let d = norm[1][1];
    let half_trace = 0.5 * (a + d);
    let half_diff = 0.5 * (a - d);
    let disc = half_diff.hypot(b);
    let lambda1 = half_trace + disc;
    let lambda2 = half_trace - disc;
    if lambda2 < -tol {
        return Err(MathError::Invalid(format!(
            "{label} must be positive semidefinite (negative variance found)"
        )));
    }
    let (v1, v2) = if b.abs() > 0.0 || half_diff != 0.0 {
        let mut x1 = if half_diff >= 0.0 {
            half_diff + disc
        } else {
            b
        };
        let mut y1 = if half_diff >= 0.0 {
            b
        } else {
            disc - half_diff
        };
        let n = x1.hypot(y1);
        if n > 0.0 {
            x1 /= n;
            y1 /= n;
        } else {
            x1 = 1.0;
            y1 = 0.0;
        }
        ([x1, y1], [-y1, x1])
    } else {
        ([1.0, 0.0], [0.0, 1.0])
    };
    let s1 = (lambda1.max(0.0) * scale).sqrt();
    let s2 = (lambda2.max(0.0) * scale).sqrt();
    Ok([[v1[0] * s1, v2[0] * s2], [v1[1] * s1, v2[1] * s2]])
}

pub fn rodrigues_vec_to_mat(rvec: Vec3) -> Mat3 {
    let theta = norm3(rvec);
    if theta < 1e-12 {
        // First-order Taylor expansion around I
        return [
            [1.0, -rvec[2], rvec[1]],
            [rvec[2], 1.0, -rvec[0]],
            [-rvec[1], rvec[0], 1.0],
        ];
    }
    let k = scale3(rvec, 1.0 / theta);
    let (s, c) = theta.sin_cos();
    let one_c = 1.0 - c;
    let kx = k[0];
    let ky = k[1];
    let kz = k[2];
    [
        [
            c + kx * kx * one_c,
            kx * ky * one_c - kz * s,
            kx * kz * one_c + ky * s,
        ],
        [
            ky * kx * one_c + kz * s,
            c + ky * ky * one_c,
            ky * kz * one_c - kx * s,
        ],
        [
            kz * kx * one_c - ky * s,
            kz * ky * one_c + kx * s,
            c + kz * kz * one_c,
        ],
    ]
}

pub fn rotation_angle(r: &Mat3) -> f64 {
    let trace = r[0][0] + r[1][1] + r[2][2];
    ((trace - 1.0) * 0.5).clamp(-1.0, 1.0).acos()
}

pub fn solve_3x3(mut a: Mat3, mut b: Vec3) -> Result<Vec3, MathError> {
    for col in 0..3 {
        let mut pivot_row = col;
        let mut max_val = a[col][col].abs();
        for (row, a_row) in a.iter().enumerate().skip(col + 1) {
            if a_row[col].abs() > max_val {
                max_val = a_row[col].abs();
                pivot_row = row;
            }
        }
        if !max_val.is_finite() || max_val <= 1e-18 {
            return Err(MathError::Invalid("singular 3x3 system".to_string()));
        }
        if pivot_row != col {
            a.swap(col, pivot_row);
            b.swap(col, pivot_row);
        }
        let diag = a[col][col];
        for row in (col + 1)..3 {
            let factor = a[row][col] / diag;
            a[row][col] = 0.0;
            let (top, bottom) = a.split_at_mut(row);
            for (dst, &src) in bottom[0][(col + 1)..]
                .iter_mut()
                .zip(top[col][(col + 1)..].iter())
            {
                *dst -= factor * src;
            }
            b[row] -= factor * b[col];
        }
    }
    let mut x = [0.0; 3];
    for i in (0..3).rev() {
        let mut sum = b[i];
        for j in (i + 1)..3 {
            sum -= a[i][j] * x[j];
        }
        x[i] = sum / a[i][i];
    }
    Ok(x)
}

/// Solve `min ||J x - rhs||_2` for `x in R^3` via normal equations `(J^T J) x = J^T rhs`.
pub fn solve_least_squares_3(j_rows: &[[f64; 3]], rhs: &[f64]) -> Result<Vec3, MathError> {
    let mut jt_j = [[0.0_f64; 3]; 3];
    let mut jt_r = [0.0_f64; 3];
    for (row, &r) in j_rows.iter().zip(rhs.iter()) {
        for i in 0..3 {
            jt_r[i] += row[i] * r;
            for j in 0..3 {
                jt_j[i][j] += row[i] * row[j];
            }
        }
    }
    solve_3x3(jt_j, jt_r)
}

#[derive(Debug, Clone, Copy)]
pub struct DistortionCoeffs {
    pub k1: f64,
    pub k2: f64,
    pub p1: f64,
    pub p2: f64,
    pub k3: f64,
    pub k4: f64,
    pub k5: f64,
    pub k6: f64,
    pub s1: f64,
    pub s2: f64,
    pub s3: f64,
    pub s4: f64,
}

impl DistortionCoeffs {
    pub fn from_proto(model: &str, coeffs: &[f64]) -> Result<Self, MathError> {
        let normalized = model.to_ascii_lowercase().replace('-', "_");
        if matches!(normalized.as_str(), "none" | "pinhole" | "zero") {
            return Ok(Self {
                k1: 0.0,
                k2: 0.0,
                p1: 0.0,
                p2: 0.0,
                k3: 0.0,
                k4: 0.0,
                k5: 0.0,
                k6: 0.0,
                s1: 0.0,
                s2: 0.0,
                s3: 0.0,
                s4: 0.0,
            });
        }
        if !matches!(
            normalized.as_str(),
            "brown_conrady" | "brownconrady" | "opencv" | "plumb_bob"
        ) {
            return Err(MathError::Invalid(format!(
                "unsupported camera distortion model: {model}"
            )));
        }
        if !matches!(coeffs.len(), 4 | 5 | 8 | 12 | 14) {
            return Err(MathError::Invalid(
                "Brown-Conrady distortion needs 4, 5, 8, 12, or 14 coefficients".to_string(),
            ));
        }
        if coeffs.iter().any(|x| !x.is_finite()) {
            return Err(MathError::Invalid(
                "distortion coefficients must be finite".to_string(),
            ));
        }
        let get = |idx: usize| -> f64 { coeffs.get(idx).copied().unwrap_or(0.0) };
        if get(12) != 0.0 || get(13) != 0.0 {
            return Err(MathError::Invalid(
                "non-zero tilt distortion coefficients (tauX, tauY) are unsupported".to_string(),
            ));
        }
        Ok(Self {
            k1: get(0),
            k2: get(1),
            p1: get(2),
            p2: get(3),
            k3: get(4),
            k4: get(5),
            k5: get(6),
            k6: get(7),
            s1: get(8),
            s2: get(9),
            s3: get(10),
            s4: get(11),
        })
    }

    pub fn distort_with_jacobian(&self, x: f64, y: f64) -> Result<([f64; 2], Mat2), MathError> {
        let x2 = x * x;
        let y2 = y * y;
        let r2 = x2 + y2;
        let r4 = r2 * r2;
        let r6 = r4 * r2;
        let num = 1.0 + self.k1 * r2 + self.k2 * r4 + self.k3 * r6;
        let den = 1.0 + self.k4 * r2 + self.k5 * r4 + self.k6 * r6;
        if !den.is_finite() || den.abs() <= 1e-12 {
            return Err(MathError::Invalid(
                "singular radial distortion denominator".to_string(),
            ));
        }
        let icdist = num / den;
        let xy = x * y;
        let xd = x * icdist
            + 2.0 * self.p1 * xy
            + self.p2 * (r2 + 2.0 * x2)
            + self.s1 * r2
            + self.s2 * r4;
        let yd = y * icdist
            + self.p1 * (r2 + 2.0 * y2)
            + 2.0 * self.p2 * xy
            + self.s3 * r2
            + self.s4 * r4;

        // d(icdist)/d(r2)
        let d_num = self.k1 + 2.0 * self.k2 * r2 + 3.0 * self.k3 * r4;
        let d_den = self.k4 + 2.0 * self.k5 * r2 + 3.0 * self.k6 * r4;
        let dicdist_dr2 = (d_num * den - num * d_den) / (den * den);
        let dicdist_dx = 2.0 * x * dicdist_dr2;
        let dicdist_dy = 2.0 * y * dicdist_dr2;

        let dxd_dx = icdist
            + x * dicdist_dx
            + 2.0 * self.p1 * y
            + 6.0 * self.p2 * x
            + 2.0 * x * (self.s1 + 2.0 * self.s2 * r2);
        let dxd_dy = x * dicdist_dy
            + 2.0 * self.p1 * x
            + 2.0 * self.p2 * y
            + 2.0 * y * (self.s1 + 2.0 * self.s2 * r2);
        let dyd_dx = y * dicdist_dx
            + 2.0 * self.p1 * x
            + 2.0 * self.p2 * y
            + 2.0 * x * (self.s3 + 2.0 * self.s4 * r2);
        let dyd_dy = icdist
            + y * dicdist_dy
            + 6.0 * self.p1 * y
            + 2.0 * self.p2 * x
            + 2.0 * y * (self.s3 + 2.0 * self.s4 * r2);

        Ok(([xd, yd], [[dxd_dx, dxd_dy], [dyd_dx, dyd_dy]]))
    }
}

/// Iteratively invert Brown-Conrady distortion using Newton's method (`cv2.undistortPointsIter` parity).
pub fn undistort_point_iter(
    xd: f64,
    yd: f64,
    dist: &DistortionCoeffs,
    max_iters: usize,
    eps: f64,
) -> Result<[f64; 2], MathError> {
    let mut x = xd;
    let mut y = yd;
    for _ in 0..max_iters {
        let ([fx, fy], jac) = dist.distort_with_jacobian(x, y)?;
        let ex = fx - xd;
        let ey = fy - yd;
        if ex.hypot(ey) <= eps {
            return Ok([x, y]);
        }
        let inv_jac = invert_2x2(&jac)?;
        x -= inv_jac[0][0] * ex + inv_jac[0][1] * ey;
        y -= inv_jac[1][0] * ex + inv_jac[1][1] * ey;
        if !x.is_finite() || !y.is_finite() {
            return Err(MathError::Invalid("undistort diverged".to_string()));
        }
    }
    Ok([x, y])
}

/// Project a 3D world point through `(R_cam_from_world, t, K, dist)` to pixel coordinates.
pub fn project_point(
    world_pt: Vec3,
    r_cam_from_world: &Mat3,
    t_cam: Vec3,
    k_norm: &Mat3,
    dist: &DistortionCoeffs,
) -> Result<[f64; 2], MathError> {
    let p = add3(mat3_vec(r_cam_from_world, world_pt), t_cam);
    if !p[2].is_finite() || p[2].abs() <= 1e-12 {
        return Err(MathError::Invalid(
            "point projects onto or near z=0 plane".to_string(),
        ));
    }
    let xn = p[0] / p[2];
    let yn = p[1] / p[2];
    let ([xd, yd], _) = dist.distort_with_jacobian(xn, yn)?;
    let u = k_norm[0][0] * xd + k_norm[0][1] * yd + k_norm[0][2];
    let v = k_norm[1][1] * yd + k_norm[1][2];
    Ok([u, v])
}

pub fn normalize_intrinsic_matrix(matrix: &Mat3) -> Result<Mat3, MathError> {
    let k22 = matrix[2][2];
    if !k22.is_finite() || k22.abs() <= 1e-15 {
        return Err(MathError::Invalid("intrinsic K[2][2] is zero".to_string()));
    }
    let mut norm = [[0.0; 3]; 3];
    for i in 0..3 {
        for j in 0..3 {
            norm[i][j] = matrix[i][j] / k22;
        }
    }
    if norm[2] != [0.0, 0.0, 1.0] || norm[1][0] != 0.0 || norm[0][0].min(norm[1][1]) <= 0.0 {
        return Err(MathError::Invalid(
            "camera requires positive focal lengths and an intrinsic matrix".to_string(),
        ));
    }
    Ok(norm)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn undistort_roundtrips_with_skew_and_brown_conrady() {
        let dist =
            DistortionCoeffs::from_proto("brown_conrady", &[-0.12, 0.04, 0.001, -0.002, 0.005])
                .unwrap();
        let xn = 0.25;
        let yn = -0.18;
        let ([xd, yd], _) = dist.distort_with_jacobian(xn, yn).unwrap();
        let [rx, ry] = undistort_point_iter(xd, yd, &dist, 50, 1e-12).unwrap();
        assert!((rx - xn).abs() < 1e-12);
        assert!((ry - yn).abs() < 1e-12);
    }

    #[test]
    fn covariance_factor_handles_zero_and_singular_psd() {
        let zero = covariance_factor_2x2([[0.0, 0.0], [0.0, 0.0]], "zero").unwrap();
        assert_eq!(zero, [[0.0, 0.0], [0.0, 0.0]]);

        let rank1 = [[4.0, 4.0], [4.0, 4.0]];
        let f = covariance_factor_2x2(rank1, "rank1").unwrap();
        let recon = mat2_mul(&f, &mat2_transpose(&f));
        assert!((recon[0][0] - 4.0).abs() < 1e-12);
        assert!((recon[0][1] - 4.0).abs() < 1e-12);
        assert!((recon[1][1] - 4.0).abs() < 1e-12);

        assert!(covariance_factor_2x2([[-0.1, 0.0], [0.0, 1.0]], "neg").is_err());
    }
}

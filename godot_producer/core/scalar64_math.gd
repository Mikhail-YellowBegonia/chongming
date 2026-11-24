class_name Scalar64Math
extends RefCounted
## Pure 64-bit scalar kinematics, pinhole projection, and deterministic RNG.
##
## Truth coordinates, rays, and nanosecond timestamps stay in 64-bit GDScript
## `float` (IEEE-754 double) and `int` (int64) scalars; Godot 32-bit `Vector3` /
## `Basis` / `Transform3D` values are produced ONLY at the visual boundary.

const _SPLITMIX_GAMMA := -7046029254386353131 # 0x9e3779b97f4a7c15
const _SPLITMIX_MUL1 := -4658895280553007687 # 0xbf58476d1ce4e5b9
const _SPLITMIX_MUL2 := -7723592293110705685 # 0x94d049bb133111eb


static func _lsr(value: int, shift: int) -> int:
	if shift <= 0:
		return value
	if shift >= 64:
		return 0
	return ((value >> 1) & 0x7fffffffffffffff) >> (shift - 1)


static func splitmix64(state: int) -> int:
	var z := state + _SPLITMIX_GAMMA
	z = (z ^ _lsr(z, 30)) * _SPLITMIX_MUL1
	z = (z ^ _lsr(z, 27)) * _SPLITMIX_MUL2
	return z ^ _lsr(z, 31)


static func deterministic_u64(random_seed: int, step_index: int, salt: int = 0) -> int:
	var s := random_seed
	s = splitmix64(s ^ step_index)
	s = splitmix64(s ^ salt)
	return s


static func deterministic_unit_float(random_seed: int, step_index: int, salt: int = 0) -> float:
	# 53-bit uniform double in [0.0, 1.0).
	var u := _lsr(deterministic_u64(random_seed, step_index, salt), 11)
	return float(u) / 9007199254740992.0


static func deterministic_normal_pair(random_seed: int, step_index: int, salt: int = 0) -> Array:
	var u1 := maxf(deterministic_unit_float(random_seed, step_index, salt * 2), 1e-12)
	var u2 := deterministic_unit_float(random_seed, step_index, salt * 2 + 1)
	var r := sqrt(-2.0 * log(u1))
	var theta := TAU * u2
	return [r * cos(theta), r * sin(theta)]


static func vec3_add(a: Array, b: Array) -> Array:
	return [float(a[0]) + float(b[0]), float(a[1]) + float(b[1]), float(a[2]) + float(b[2])]


static func vec3_sub(a: Array, b: Array) -> Array:
	return [float(a[0]) - float(b[0]), float(a[1]) - float(b[1]), float(a[2]) - float(b[2])]


static func vec3_scale(v: Array, s: float) -> Array:
	return [float(v[0]) * s, float(v[1]) * s, float(v[2]) * s]


static func vec3_dot(a: Array, b: Array) -> float:
	return float(a[0]) * float(b[0]) + float(a[1]) * float(b[1]) + float(a[2]) * float(b[2])


static func vec3_cross(a: Array, b: Array) -> Array:
	return [
		float(a[1]) * float(b[2]) - float(a[2]) * float(b[1]),
		float(a[2]) * float(b[0]) - float(a[0]) * float(b[2]),
		float(a[0]) * float(b[1]) - float(a[1]) * float(b[0]),
	]


static func vec3_norm(v: Array) -> float:
	return sqrt(vec3_dot(v, v))


static func vec3_normalize(v: Array) -> Array:
	var n := vec3_norm(v)
	if n <= 0.0:
		return [0.0, 0.0, 0.0]
	return [float(v[0]) / n, float(v[1]) / n, float(v[2]) / n]


static func mat3_identity() -> Array:
	return [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]


static func mat3_mul_vec3(m: Array, v: Array) -> Array:
	var vx := float(v[0])
	var vy := float(v[1])
	var vz := float(v[2])
	return [
		float(m[0]) * vx + float(m[1]) * vy + float(m[2]) * vz,
		float(m[3]) * vx + float(m[4]) * vy + float(m[5]) * vz,
		float(m[6]) * vx + float(m[7]) * vy + float(m[8]) * vz,
	]


static func mat3_transpose_mul_vec3(m: Array, v: Array) -> Array:
	var vx := float(v[0])
	var vy := float(v[1])
	var vz := float(v[2])
	return [
		float(m[0]) * vx + float(m[3]) * vy + float(m[6]) * vz,
		float(m[1]) * vx + float(m[4]) * vy + float(m[7]) * vz,
		float(m[2]) * vx + float(m[5]) * vy + float(m[8]) * vz,
	]


static func mat3_mul(a: Array, b: Array) -> Array:
	var out := [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
	for r in 3:
		for c in 3:
			out[r * 3 + c] = (
				float(a[r * 3]) * float(b[c])
				+ float(a[r * 3 + 1]) * float(b[3 + c])
				+ float(a[r * 3 + 2]) * float(b[6 + c])
			)
	return out


static func look_rotation_enu_camera(origin_enu: Array, target_enu: Array) -> Array:
	# Right-handed ENU camera basis: columns = [right, down, forward].
	var forward := vec3_normalize(vec3_sub(target_enu, origin_enu))
	var up := [0.0, 0.0, 1.0]
	var right_raw := vec3_cross(forward, up)
	if vec3_norm(right_raw) < 1e-9:
		right_raw = vec3_cross(forward, [0.0, 1.0, 0.0])
	var right := vec3_normalize(right_raw)
	var down := vec3_normalize(vec3_cross(forward, right))
	return [
		right[0], down[0], forward[0],
		right[1], down[1], forward[1],
		right[2], down[2], forward[2],
	]


static func velocity_aligned_rotation_enu(velocity_enu: Array) -> Array:
	var speed := vec3_norm(velocity_enu)
	if speed < 1e-9:
		return mat3_identity()
	var forward := vec3_scale(velocity_enu, 1.0 / speed)
	var up_ref := [0.0, 0.0, 1.0]
	var right_raw := vec3_cross(forward, up_ref)
	if vec3_norm(right_raw) < 1e-9:
		right_raw = [1.0, 0.0, 0.0]
	var right := vec3_normalize(right_raw)
	var up := vec3_normalize(vec3_cross(right, forward))
	# Body frame columns: +X forward, +Y left (-right), +Z up.
	return [
		forward[0], -right[0], up[0],
		forward[1], -right[1], up[1],
		forward[2], -right[2], up[2],
	]


static func orthonormal_tangent_basis(direction_unit_enu: Array) -> Array:
	var d := vec3_normalize(direction_unit_enu)
	var ref := [0.0, 0.0, 1.0] if absf(float(d[2])) < 0.9 else [1.0, 0.0, 0.0]
	var u := vec3_normalize(vec3_cross(ref, d))
	var v := vec3_normalize(vec3_cross(d, u))
	return [u, v]


static func project_pinhole_px(
	point_enu: Array,
	cam_pos_enu: Array,
	r_enu_from_cam: Array,
	intrinsics: Dictionary,
	near_m: float = 0.02
) -> Dictionary:
	var delta_enu := vec3_sub(point_enu, cam_pos_enu)
	var range_m := vec3_norm(delta_enu)
	var p_cam := mat3_transpose_mul_vec3(r_enu_from_cam, delta_enu)
	var xc := float(p_cam[0])
	var yc := float(p_cam[1])
	var zc := float(p_cam[2])
	if zc <= near_m:
		return {
			"in_fov": false,
			"centroid_px": null,
			"range_m": range_m,
			"depth_m": zc,
		}
	var k: Array = intrinsics["matrix_px"]
	var fx := float(k[0])
	var cx := float(k[2])
	var fy := float(k[4])
	var cy := float(k[5])
	var u := fx * (xc / zc) + cx
	var v := fy * (yc / zc) + cy
	var w := float(intrinsics["image_width_px"])
	var h := float(intrinsics["image_height_px"])
	var in_fov := (u >= -0.5 and u <= w - 0.5 and v >= -0.5 and v <= h - 0.5)
	return {
		"in_fov": in_fov,
		"centroid_px": [u, v],
		"range_m": range_m,
		"depth_m": zc,
	}


static func project_aabb_diagonal_px(
	center_enu: Array,
	half_extents_body_m: Array,
	r_enu_from_body: Array,
	cam_pos_enu: Array,
	r_enu_from_cam: Array,
	intrinsics: Dictionary,
	near_m: float = 0.02
) -> Variant:
	var hx := float(half_extents_body_m[0])
	var hy := float(half_extents_body_m[1])
	var hz := float(half_extents_body_m[2])
	var k: Array = intrinsics["matrix_px"]
	var fx := float(k[0])
	var cx := float(k[2])
	var fy := float(k[4])
	var cy := float(k[5])
	var min_u := INF
	var max_u := -INF
	var min_v := INF
	var max_v := -INF
	var any_front := false
	for sx in [-1.0, 1.0]:
		for sy in [-1.0, 1.0]:
			for sz in [-1.0, 1.0]:
				var corner_body := [sx * hx, sy * hy, sz * hz]
				var corner_enu := vec3_add(center_enu, mat3_mul_vec3(r_enu_from_body, corner_body))
				var p_cam := mat3_transpose_mul_vec3(r_enu_from_cam, vec3_sub(corner_enu, cam_pos_enu))
				var zc := float(p_cam[2])
				if zc <= near_m:
					continue
				any_front = true
				var u := fx * (float(p_cam[0]) / zc) + cx
				var v := fy * (float(p_cam[1]) / zc) + cy
				min_u = minf(min_u, u)
				max_u = maxf(max_u, u)
				min_v = minf(min_v, v)
				max_v = maxf(max_v, v)
	if not any_front:
		return null
	var du := max_u - min_u
	var dv := max_v - min_v
	return sqrt(du * du + dv * dv)


# --- Render-only 64-bit -> 32-bit Godot visual boundary helpers ---

static func enu_to_godot_vec3(enu: Array) -> Vector3:
	return Vector3(float(enu[0]), float(enu[2]), -float(enu[1]))


static func enu_to_godot_basis(r_enu_row9: Array) -> Basis:
	var col0 := enu_to_godot_vec3([r_enu_row9[0], r_enu_row9[3], r_enu_row9[6]])
	var col1 := enu_to_godot_vec3([r_enu_row9[1], r_enu_row9[4], r_enu_row9[7]])
	var col2 := enu_to_godot_vec3([r_enu_row9[2], r_enu_row9[5], r_enu_row9[8]])
	return Basis(col0, col1, col2)


static func godot_camera_transform(cam_pos_enu: Array, r_enu_from_cam: Array) -> Transform3D:
	var cam_axes := Basis(Vector3.RIGHT, Vector3.DOWN, Vector3.FORWARD)
	return Transform3D(enu_to_godot_basis(r_enu_from_cam) * cam_axes, enu_to_godot_vec3(cam_pos_enu))


static func configure_camera_frustum(
	camera: Camera3D,
	intrinsics: Dictionary,
	near_m: float = 0.02,
	far_m: float = 100000.0
) -> void:
	var w := float(intrinsics["image_width_px"])
	var h := float(intrinsics["image_height_px"])
	var k: Array = intrinsics["matrix_px"]
	var fx := float(k[0])
	var cx := float(k[2])
	var fy := float(k[4])
	var cy := float(k[5])
	var offset := Vector2(
		(w * 0.5 - cx - 0.5) * near_m / fx,
		(cy + 0.5 - h * 0.5) * near_m / fy
	)
	camera.set_frustum(h * near_m / fy, offset, near_m, far_m)

class_name AirspaceBaseScene
extends "res://core/scene_contract.gd"
## Reusable two-station airspace benchmark scenario skeleton.
## Concrete scenes override presentation/assets/occluders cleanly while keeping
## 64-bit station geometry, intrinsics, checkerboard calibration, and target kinematics uniform.

const Checkerboard = preload("res://scenes/checkerboard_helper.gd")

@export var scenario_id: String = "airspace-clean"
@export var random_seed: int = 2026
@export var start_time_ns: int = 1763784000000000000
@export var step_period_ns: int = 100000000 # 10 Hz
@export var total_steps: int = 10
@export var image_width_px: int = 640
@export var image_height_px: int = 480
@export var focal_px: float = 600.0
@export var target_start_enu_m: PackedFloat64Array = PackedFloat64Array([-84.0, 120.0, 45.0])
@export var target_velocity_enu_mps: PackedFloat64Array = PackedFloat64Array([120.0, 0.0, 0.0])

var _target_node: Node3D
var _station_specs: Array = []
var _board_tex: Texture2D
var _scene_built: bool = false


func _ready() -> void:
	super._ready()
	_ensure_built()


func _ensure_built() -> void:
	if _scene_built:
		return
	_scene_built = true
	_station_specs = _build_stations()
	_board_tex = Checkerboard.create_board_texture()
	_build_checkerboards()
	_build_presentation()
	_target_node = _build_target_visual()
	add_child(_target_node)


func get_scene_manifest() -> Dictionary:
	_ensure_built()
	return {
		"scenario_id": scenario_id,
		"world_frame_id": "enu",
		"clock_domain_id": "sim",
		"random_seed": random_seed,
		"start_time_ns": start_time_ns,
		"step_period_ns": step_period_ns,
		"total_steps": total_steps,
		"occluder_collision_mask": 1,
		"stations": _station_specs,
	}


func evaluate_targets_64(step_index: int, _experiment_time_ns: int) -> Array:
	var dt := float(step_index) * float(step_period_ns) * 1e-9
	var vx := float(target_velocity_enu_mps[0])
	var vy := float(target_velocity_enu_mps[1])
	var vz := float(target_velocity_enu_mps[2])
	var pos := [
		float(target_start_enu_m[0]) + vx * dt,
		float(target_start_enu_m[1]) + vy * dt,
		float(target_start_enu_m[2]) + vz * dt,
	]
	var vel := [vx, vy, vz]
	var r_body := ScalarMath.velocity_aligned_rotation_enu(vel)
	var half_extents := [3.5, 3.0, 1.2]
	var diag_m := 2.0 * sqrt(3.5 * 3.5 + 3.0 * 3.0 + 1.2 * 1.2)
	return [{
		"target_id": "tgt-1",
		"target_class": "aircraft",
		"position_enu_m": pos,
		"velocity_enu_mps": vel,
		"r_enu_from_body": r_body,
		"half_extents_body_m": half_extents,
		"bounding_radius_m": diag_m * 0.5,
		"body_aabb_diagonal_m": diag_m,
	}]


func apply_visual_state(_step_index: int, _experiment_time_ns: int, targets_64: Array) -> void:
	_ensure_built()
	if targets_64.is_empty() or _target_node == null:
		return
	var tgt: Dictionary = targets_64[0]
	var pos: Array = tgt["position_enu_m"]
	var rot: Array = tgt["r_enu_from_body"]
	_target_node.transform = Transform3D(
		ScalarMath.enu_to_godot_basis(rot),
		ScalarMath.enu_to_godot_vec3(pos)
	)


func _build_stations() -> Array:
	var aim_point := [0.0, 120.0, 45.0]
	var specs := [
		{"id": "s1", "pos": [-45.0, 0.0, 5.0]},
		{"id": "s2", "pos": [45.0, 0.0, 5.0]},
	]
	var intr := {
		"image_width_px": image_width_px,
		"image_height_px": image_height_px,
		"matrix_px": [
			focal_px, 0.0, float(image_width_px) * 0.5,
			0.0, focal_px, float(image_height_px) * 0.5,
			0.0, 0.0, 1.0,
		],
	}
	var cam_model := {
		"intrinsics": intr,
		"distortion": {"model": "none", "coefficients": [0.0, 0.0, 0.0, 0.0]},
	}
	# Aux camera looks straight down (-Z_enu): right=+E, down=-N, forward=-U.
	var r_enu_from_aux := [
		1.0, 0.0, 0.0,
		0.0, -1.0, 0.0,
		0.0, 0.0, -1.0,
	]
	var out := []
	for sp in specs:
		var sid := str(sp["id"])
		var pos_enu: Array = sp["pos"]
		var r_enu_from_main := ScalarMath.look_rotation_enu_camera(pos_enu, aim_point)
		# fixed_rotation_aux_from_main = R_enu_from_aux^T * R_enu_from_main.
		var r_aux_from_main := ScalarMath.mat3_mul(
			[
				r_enu_from_aux[0], r_enu_from_aux[3], r_enu_from_aux[6],
				r_enu_from_aux[1], r_enu_from_aux[4], r_enu_from_aux[7],
				r_enu_from_aux[2], r_enu_from_aux[5], r_enu_from_aux[8],
			],
			r_enu_from_main
		)
		var board_center := ScalarMath.vec3_add(pos_enu, [0.0, 0.0, -0.75])
		var landmarks := Checkerboard.surveyed_landmarks_enu(board_center, r_enu_from_aux)
		var digest := "cfg-%s-%s" % [scenario_id, sid]
		out.append({
			"station_id": sid,
			"config_version": "1",
			"config_digest": digest,
			"world_frame_id": "enu",
			"geodetic_anchor": {
				"anchor_id": "anch-%s" % sid,
				"datum": "WGS84",
				"datum_epoch": "2026",
				"latitude_deg": 52.37,
				"longitude_deg": 9.73,
				"height_m": 55.0,
				"height_reference": "ellipsoidal",
			},
			"main_camera_position_enu_m": pos_enu,
			"baseline_orientation_enu_from_main": r_enu_from_main,
			"main_camera": cam_model,
			"auxiliary_camera": cam_model,
			"fixed_rotation_aux_from_main": r_aux_from_main,
			"full_rigid_transform_aux_from_main": {
				"rotation_a_from_b": r_aux_from_main,
				"translation_a_m": [0.0, 0.0, 0.0],
			},
			"reference_scene_version": "godot-%s-v1" % scenario_id,
			"installation_uncertainty": {
				"state_ref": {"station_id": sid, "config_digest": digest, "state_version": "1"},
				"position_covariance_enu_m2": [1e-6, 0.0, 0.0, 0.0, 1e-6, 0.0, 0.0, 0.0, 1e-6],
				"orientation_covariance_enu_rad2": [1e-8, 0.0, 0.0, 0.0, 1e-8, 0.0, 0.0, 0.0, 1e-8],
				"fixed_extrinsic_covariance_aux_rad2": [1e-8, 0.0, 0.0, 0.0, 1e-8, 0.0, 0.0, 0.0, 1e-8],
				"measurement_source": "survey",
			},
			"reference_anchor": {
				"reference_id": "%s-a4" % sid,
				"world_frame_id": "enu",
				"landmarks": landmarks,
			},
			"board_center_enu": board_center,
			"r_enu_from_aux": r_enu_from_aux,
		})
	return out


func _build_checkerboards() -> void:
	for st in _station_specs:
		var node := Checkerboard.create_board_node(
			st["board_center_enu"],
			st["r_enu_from_aux"],
			_board_tex
		)
		node.name = "Checkerboard_%s" % st["station_id"]
		add_child(node)


## Presentation hook: subclasses override environment, lighting, terrain, vegetation, and occluders.
func _build_presentation() -> void:
	var env := Environment.new()
	env.background_mode = Environment.BG_COLOR
	env.background_color = Color(0.03, 0.04, 0.06)
	var world_env := WorldEnvironment.new()
	world_env.environment = env
	add_child(world_env)

	var sun := DirectionalLight3D.new()
	sun.light_energy = 1.2
	sun.basis = Basis.looking_at(Vector3(-0.3, -0.8, -0.4).normalized(), Vector3.UP)
	add_child(sun)


## Aircraft visual hook: uncolliding 32-bit mesh matching target body half-extents.
func _build_target_visual() -> Node3D:
	var root := Node3D.new()
	root.name = "AircraftVisual"
	var mat := StandardMaterial3D.new()
	mat.albedo_color = Color(0.95, 0.96, 0.98)
	mat.emission_enabled = true
	mat.emission = Color(0.75, 0.78, 0.85)
	mat.emission_energy_multiplier = 0.6

	var fuselage := MeshInstance3D.new()
	var fuse_mesh := BoxMesh.new()
	fuse_mesh.size = Vector3(6.8, 1.4, 1.4)
	fuselage.mesh = fuse_mesh
	fuselage.material_override = mat
	root.add_child(fuselage)

	var wing := MeshInstance3D.new()
	var wing_mesh := BoxMesh.new()
	wing_mesh.size = Vector3(1.6, 6.0, 0.4)
	wing.mesh = wing_mesh
	wing.material_override = mat
	root.add_child(wing)
	return root

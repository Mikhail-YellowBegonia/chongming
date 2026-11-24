class_name ProducerSceneContract
extends Node3D
## Abstract contract separating Godot Scene Presentation/Assets from the
## 64-bit Simulation, Physics Visibility, and Raw-Frame Export Skeleton.
##
## Scene implementations subclass this node, build 3D terrain/vegetation/lighting/
## camera visuals in Godot's scene tree, and provide 64-bit scalar definitions
## for stations, targets, and calibration landmarks.

const ProtoWire = preload("res://core/proto_wire.gd")
const ScalarMath = preload("res://core/scalar64_math.gd")

var _camera_rig_root: Node3D
var _viewports: Dictionary = {} # key: "%s:%d" -> {viewport, camera, width, height}


func _ready() -> void:
	_ensure_rig_root()


func _ensure_rig_root() -> void:
	if _camera_rig_root == null:
		_camera_rig_root = Node3D.new()
		_camera_rig_root.name = "ProducerCameraRig"
		add_child(_camera_rig_root)


## Return scene metadata and station specifications.
## Required keys: scenario_id, world_frame_id, clock_domain_id, random_seed,
## start_time_ns, step_period_ns, total_steps, occluder_collision_mask, stations.
func get_scene_manifest() -> Dictionary:
	push_error("ProducerSceneContract.get_scene_manifest() must be overridden by scene")
	return {}


## Evaluate 64-bit scalar kinematics for all targets at `(step_index, experiment_time_ns)`.
## Each entry must provide: target_id, target_class, position_enu_m, velocity_enu_mps,
## r_enu_from_body, half_extents_body_m, bounding_radius_m, body_aabb_diagonal_m.
func evaluate_targets_64(_step_index: int, _experiment_time_ns: int) -> Array:
	return []


## Project 64-bit target/weather/scene states onto Godot 32-bit visual Node3Ds.
## Visual transforms must never feed back into 64-bit truth or virtual rays.
func apply_visual_state(_step_index: int, _experiment_time_ns: int, _targets_64: Array) -> void:
	pass


## Whether camera `(station_id, camera_role)` should expose a frame on `step_index`.
## Default captures MAIN on every step and AUXILIARY on step 0 (sharing timestamp with MAIN).
func should_capture_camera(_station_id: String, camera_role: int, step_index: int) -> bool:
	if camera_role == ProtoWire.CAMERA_ROLE_MAIN:
		return true
	return step_index == 0


## Return exact 64-bit pose & intrinsics for `(station, camera_role)` at `step_index`.
## Keys: pos_enu_m ([e,n,u]), r_enu_from_cam (row-major 9), intrinsics (CameraIntrinsics dict).
func get_camera_pose_64(
	station: Dictionary,
	camera_role: int,
	_step_index: int,
	_experiment_time_ns: int
) -> Dictionary:
	if camera_role == ProtoWire.CAMERA_ROLE_MAIN:
		return {
			"pos_enu_m": station["main_camera_position_enu_m"],
			"r_enu_from_cam": station["baseline_orientation_enu_from_main"],
			"intrinsics": station["main_camera"]["intrinsics"],
		}
	var aux_tf: Variant = station.get("full_rigid_transform_aux_from_main", null)
	var r_aux_from_main: Array = (
		aux_tf["rotation_a_from_b"]
		if aux_tf != null
		else station["fixed_rotation_aux_from_main"]
	)
	var r_main_from_aux := [
		r_aux_from_main[0], r_aux_from_main[3], r_aux_from_main[6],
		r_aux_from_main[1], r_aux_from_main[4], r_aux_from_main[7],
		r_aux_from_main[2], r_aux_from_main[5], r_aux_from_main[8],
	]
	var r_enu_from_aux := ScalarMath.mat3_mul(
		station["baseline_orientation_enu_from_main"],
		r_main_from_aux
	)
	var pos_aux_enu: Array = station.get("auxiliary_camera_position_enu_m", station["main_camera_position_enu_m"])
	return {
		"pos_enu_m": pos_aux_enu,
		"r_enu_from_cam": r_enu_from_aux,
		"intrinsics": station["auxiliary_camera"]["intrinsics"],
	}


func ensure_camera_viewports(stations: Array) -> void:
	_ensure_rig_root()
	for station in stations:
		var sid := str(station["station_id"])
		_ensure_single_viewport(sid, ProtoWire.CAMERA_ROLE_MAIN, station["main_camera"]["intrinsics"])
		_ensure_single_viewport(sid, ProtoWire.CAMERA_ROLE_AUXILIARY, station["auxiliary_camera"]["intrinsics"])


func _ensure_single_viewport(station_id: String, camera_role: int, intrinsics: Dictionary) -> void:
	var key := "%s:%d" % [station_id, camera_role]
	var width := int(intrinsics["image_width_px"])
	var height := int(intrinsics["image_height_px"])
	if _viewports.has(key):
		var existing: Dictionary = _viewports[key]
		existing["viewport"].size = Vector2i(width, height)
		existing["width"] = width
		existing["height"] = height
		return
	var viewport := SubViewport.new()
	viewport.name = "VP_%s_%d" % [station_id, camera_role]
	viewport.own_world_3d = false
	viewport.size = Vector2i(width, height)
	viewport.render_target_clear_mode = SubViewport.CLEAR_MODE_ALWAYS
	viewport.render_target_update_mode = SubViewport.UPDATE_DISABLED
	_camera_rig_root.add_child(viewport)

	var camera := Camera3D.new()
	camera.name = "Cam_%s_%d" % [station_id, camera_role]
	camera.current = true
	viewport.add_child(camera)

	_viewports[key] = {
		"viewport": viewport,
		"camera": camera,
		"width": width,
		"height": height,
	}


## Determine OcclusionStatus for a target center known to be inside `cam`'s FOV
## using Godot 3D Physics raycasting against `occluder_mask`.
func evaluate_physics_occlusion(
	cam_pos_enu: Array,
	target_pos_enu: Array,
	occluder_mask: int = 1,
	exclude_rids: Array = []
) -> int:
	var world := get_world_3d()
	if world == null:
		return ProtoWire.OCCLUSION_UNKNOWN
	var space := world.direct_space_state
	if space == null:
		return ProtoWire.OCCLUSION_UNKNOWN
	var from := ScalarMath.enu_to_godot_vec3(cam_pos_enu)
	var to := ScalarMath.enu_to_godot_vec3(target_pos_enu)
	if from.distance_squared_to(to) < 1e-8:
		return ProtoWire.OCCLUSION_VISIBLE
	var query := PhysicsRayQueryParameters3D.create(from, to, occluder_mask, exclude_rids)
	query.collide_with_bodies = true
	query.collide_with_areas = false
	query.hit_from_inside = false
	var hit := space.intersect_ray(query)
	if not hit.is_empty():
		return ProtoWire.OCCLUSION_OCCLUDED
	return ProtoWire.OCCLUSION_VISIBLE


## Render one single-exposure RGB8 frame for `(station_id, camera_role)`.
func capture_camera_rgb8(
	station_id: String,
	camera_role: int,
	cam_pos_enu: Array,
	r_enu_from_cam: Array,
	intrinsics: Dictionary
) -> PackedByteArray:
	var key := "%s:%d" % [station_id, camera_role]
	var entry: Dictionary = _viewports[key]
	var viewport: SubViewport = entry["viewport"]
	var camera: Camera3D = entry["camera"]
	var width := int(intrinsics["image_width_px"])
	var height := int(intrinsics["image_height_px"])
	if viewport.size != Vector2i(width, height):
		viewport.size = Vector2i(width, height)
	camera.transform = ScalarMath.godot_camera_transform(cam_pos_enu, r_enu_from_cam)
	ScalarMath.configure_camera_frustum(camera, intrinsics)
	camera.make_current()

	await get_tree().process_frame
	viewport.render_target_update_mode = SubViewport.UPDATE_ONCE
	RenderingServer.force_draw(false)
	var tex := viewport.get_texture()
	if tex == null:
		push_error("Viewport texture is null for %s" % key)
		return PackedByteArray()
	var image := tex.get_image()
	if image == null or image.is_empty():
		push_error("Captured image is empty for %s (headless rendering active?)" % key)
		return PackedByteArray()
	if image.get_format() != Image.FORMAT_RGB8:
		image.convert(Image.FORMAT_RGB8)
	return image.get_data()

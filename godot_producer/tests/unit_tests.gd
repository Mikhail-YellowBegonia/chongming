extends SceneTree
## Headless unit tests for Godot Producer core math, physics raycast visibility,
## deterministic RNG, and Protobuf3 wire encoding (`chongming.v1`).

const ProtoWire = preload("res://core/proto_wire.gd")
const ScalarMath = preload("res://core/scalar64_math.gd")
const SceneContract = preload("res://core/scene_contract.gd")
const Runner = preload("res://core/producer_runner.gd")


class MockPhysicsScene extends SceneContract:
	func build_occluder() -> void:
		var body := StaticBody3D.new()
		body.collision_layer = 1
		body.collision_mask = 0
		var shape := CollisionShape3D.new()
		var box := BoxShape3D.new()
		box.size = Vector3(10.0, 20.0, 2.0)
		shape.shape = box
		body.add_child(shape)
		# Wall sits at ENU [0, 50, 10] between cam [0, 0, 10] and occluded target [0, 100, 10].
		body.position = ScalarMath.enu_to_godot_vec3([0.0, 50.0, 10.0])
		add_child(body)


func _init() -> void:
	call_deferred("_run_all")


func _run_all() -> void:
	var args := OS.get_cmdline_user_args()
	var out_dir := str(args[0]) if args.size() > 0 else ""
	var errors := []

	# 1. 64-bit int precision (> 2^53) and negative varint
	var big_ns := 9007199254740992 + 100 # 2^53 + 100
	var next_ns := big_ns + 7
	if next_ns - big_ns != 7:
		errors.append("64-bit int timestamp addition lost precision: %d" % next_ns)

	# 2. SplitMix64 determinism
	var d1 := ScalarMath.deterministic_unit_float(42, 5, 1)
	var d2 := ScalarMath.deterministic_unit_float(42, 5, 1)
	var d3 := ScalarMath.deterministic_unit_float(43, 5, 1)
	if d1 != d2 or d1 < 0.0 or d1 >= 1.0 or d1 == d3:
		errors.append("Deterministic PRNG mismatch: d1=%s d2=%s d3=%s" % [d1, d2, d3])

	# 3. 64-bit pinhole projection
	var cam_pos := [0.0, -100.0, 0.0]
	var r_enu_from_cam := ScalarMath.look_rotation_enu_camera(cam_pos, [0.0, 0.0, 0.0])
	var intr := {
		"image_width_px": 640,
		"image_height_px": 480,
		"matrix_px": [600.0, 0.0, 320.0, 0.0, 600.0, 240.0, 0.0, 0.0, 1.0],
	}
	var p_center := ScalarMath.project_pinhole_px([0.0, 0.0, 0.0], cam_pos, r_enu_from_cam, intr)
	if not bool(p_center["in_fov"]):
		errors.append("Center target expected in FOV")
	elif absf(float(p_center["centroid_px"][0]) - 320.0) > 1e-12 or absf(float(p_center["centroid_px"][1]) - 240.0) > 1e-12:
		errors.append("Center projection mismatch: %s" % str(p_center["centroid_px"]))
	if absf(float(p_center["range_m"]) - 100.0) > 1e-12:
		errors.append("Center range mismatch: %s" % str(p_center["range_m"]))

	var p_behind := ScalarMath.project_pinhole_px([0.0, -150.0, 0.0], cam_pos, r_enu_from_cam, intr)
	if bool(p_behind["in_fov"]) or p_behind["centroid_px"] != null:
		errors.append("Behind-camera target must be OUT_OF_FOV with null centroid")

	var p_outside := ScalarMath.project_pinhole_px([120.0, 0.0, 0.0], cam_pos, r_enu_from_cam, intr)
	if bool(p_outside["in_fov"]):
		errors.append("Far-right target expected OUT_OF_FOV")

	var diag: Variant = ScalarMath.project_aabb_diagonal_px(
		[0.0, 0.0, 0.0],
		[1.0, 1.0, 1.0],
		ScalarMath.mat3_identity(),
		cam_pos,
		r_enu_from_cam,
		intr
	)
	if diag == null or float(diag) <= 0.0:
		errors.append("Invalid projected AABB diagonal: %s" % str(diag))

	# 4. Godot 3D Physics raycast occlusion in headless SceneTree
	var scene := MockPhysicsScene.new()
	root.add_child(scene)
	scene.build_occluder()
	await physics_frame
	await physics_frame

	var occ_blocked := scene.evaluate_physics_occlusion([0.0, 0.0, 10.0], [0.0, 100.0, 10.0], 1)
	var occ_clear := scene.evaluate_physics_occlusion([0.0, 0.0, 10.0], [40.0, 100.0, 10.0], 1)
	if occ_blocked != ProtoWire.OCCLUSION_OCCLUDED:
		errors.append("Expected OCCLUSION_OCCLUDED (2), got %d" % occ_blocked)
	if occ_clear != ProtoWire.OCCLUSION_VISIBLE:
		errors.append("Expected OCCLUSION_VISIBLE (1), got %d" % occ_clear)
	scene.queue_free()

	# 5. Protobuf3 wire encoding & SessionAck self-decode
	var ack_inner := PackedByteArray()
	ProtoWire.append_bool_field(ack_inner, 1, true)
	ProtoWire.append_string_field(ack_inner, 2, "ok")
	ProtoWire.append_string_field(ack_inner, 3, "run-gd-test")
	var bus_ack_wire := ProtoWire.encode_bus_message(7, 1000, 11, ack_inner)
	var decoded_ack := ProtoWire.decode_bus_message_session_ack(bus_ack_wire)
	if not bool(decoded_ack.get("accepted", false)) or str(decoded_ack.get("run_id", "")) != "run-gd-test":
		errors.append("Failed to decode BusMessage(SessionAck): %s" % JSON.stringify(decoded_ack))
	var tfm_ack_wire := ProtoWire.encode_test_framework_message(3, 12, bus_ack_wire)
	var decoded_tfm_ack := ProtoWire.decode_test_framework_session_ack(tfm_ack_wire)
	if not bool(decoded_tfm_ack.get("accepted", false)) or str(decoded_tfm_ack.get("reason", "")) != "ok":
		errors.append("Failed to decode TestFrameworkMessage(SessionAck): %s" % JSON.stringify(decoded_tfm_ack))

	if not out_dir.is_empty():
		_write_wire_samples(out_dir, big_ns)

	print("GODOT_UNIT_TESTS " + JSON.stringify({
		"ok": errors.is_empty(),
		"errors": errors,
		"occ_blocked": occ_blocked,
		"occ_clear": occ_clear,
		"center_px": p_center["centroid_px"],
		"diag_px": diag,
	}))
	quit(0 if errors.is_empty() else 1)


func _write_wire_samples(out_dir: String, big_ns: int) -> void:
	var st := _sample_station_config()
	var ct := {
		"clock_domain_id": "sim",
		"exposure_start_ns": big_ns,
		"exposure_end_ns": big_ns,
		"clock_uncertainty_ns": 5,
		"rolling_shutter_model": "none",
	}
	var pixels := PackedByteArray([10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120])

	_save_file(out_dir.path_join("session_hello.bin"), ProtoWire.encode_bus_message(
		1,
		big_ns,
		10,
		ProtoWire.encode_session_hello("gd-prod-1", "sess-gd-1", ["s1"])
	))
	_save_file(out_dir.path_join("station_config.bin"), ProtoWire.encode_bus_message(
		2,
		big_ns,
		12,
		ProtoWire.encode_station_config(st)
	))
	_save_file(out_dir.path_join("video_packet.bin"), ProtoWire.encode_bus_message(
		3,
		big_ns,
		13,
		ProtoWire.encode_framed_video_packet({
			"station_id": "s1",
			"station_session_id": "sess-gd-1-s1",
			"camera_role": ProtoWire.CAMERA_ROLE_MAIN,
			"frame_id": "s1-main-0000",
			"frame_index": 0,
			"capture_time": ct,
			"station_config_digest": "cfg-s1",
			"width_px": 2,
			"height_px": 2,
			"codec": ProtoWire.VIDEO_CODEC_RAW,
			"pixel_format": ProtoWire.PIXEL_FORMAT_RGB8,
			"row_stride_bytes": 6,
			"is_keyframe": true,
			"payload": pixels,
		})
	))
	_save_file(out_dir.path_join("ray_event.bin"), ProtoWire.encode_bus_message(
		4,
		big_ns,
		16,
		ProtoWire.encode_ray_event(Runner._build_virtual_ray(
			"s1",
			"sess-gd-1-s1",
			"cfg-s1",
			"enu",
			"sim",
			"s1-main-0000",
			1,
			ct,
			[0.0, 0.0, 10.0],
			[0.0, 100.0, 10.0],
			"tgt-1",
			1e-6
		))
	))
	_save_file(out_dir.path_join("progress_report.bin"), ProtoWire.encode_bus_message(
		5,
		big_ns,
		20,
		ProtoWire.encode_progress_report({
			"source_role": ProtoWire.COMPONENT_ROLE_PRODUCER,
			"source_id": "gd-prod-1",
			"session_id": "sess-gd-1",
			"completed_time_ns": big_ns,
			"completed_sequence_no": 1,
			"finite_end_reached": true,
		})
	))
	_save_file(out_dir.path_join("truth_frame.bin"), ProtoWire.encode_test_framework_message(
		1,
		10,
		ProtoWire.encode_truth_frame({
			"world_frame_id": "enu",
			"experiment_time_ns": big_ns,
			"step_index": 0,
			"targets": [{
				"target_id": "tgt-1",
				"target_class": "aircraft",
				"position_enu_m": [1.5, 100.25, 25.0],
				"velocity_enu_mps": [12.0, -3.5, 0.25],
				"bounding_radius_m": 5.0,
				"body_aabb_diagonal_m": 10.0,
			}],
			"projections": [
				{
					"station_id": "s1",
					"target_id": "tgt-1",
					"occlusion_status": ProtoWire.OCCLUSION_VISIBLE,
					"projected_centroid_px": [320.25, 240.5],
					"projected_diagonal_px": 18.75,
					"range_m": 100.25,
					"frame_id": "s1-main-0000",
				},
				{
					"station_id": "s2",
					"target_id": "tgt-1",
					"occlusion_status": ProtoWire.OCCLUSION_OUT_OF_FOV,
					"projected_centroid_px": null,
					"projected_diagonal_px": null,
					"range_m": 0.0,
					"frame_id": "s2-main-0000",
				},
			],
		})
	))
	_save_file(out_dir.path_join("truth_frame_neg.bin"), ProtoWire.encode_test_framework_message(
		2,
		10,
		ProtoWire.encode_truth_frame({
			"world_frame_id": "enu",
			"experiment_time_ns": -1234567890123456789,
			"step_index": 0,
			"targets": [],
			"projections": [],
		})
	))


func _sample_station_config() -> Dictionary:
	var eye3 := [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
	var cam := {
		"intrinsics": {
			"image_width_px": 640,
			"image_height_px": 480,
			"matrix_px": [600.0, 0.0, 320.0, 0.0, 600.0, 240.0, 0.0, 0.0, 1.0],
		},
		"distortion": {"model": "none", "coefficients": [0.0, 0.0, 0.0, 0.0]},
	}
	return {
		"station_id": "s1",
		"config_version": "v1",
		"config_digest": "cfg-s1",
		"world_frame_id": "enu",
		"geodetic_anchor": {
			"anchor_id": "a1",
			"datum": "WGS84",
			"datum_epoch": "2026",
			"latitude_deg": 52.37,
			"longitude_deg": 9.73,
			"height_m": 55.0,
			"height_reference": "ellipsoidal",
		},
		"main_camera_position_enu_m": [0.0, 0.0, 0.0],
		"baseline_orientation_enu_from_main": eye3,
		"main_camera": cam,
		"auxiliary_camera": cam,
		"fixed_rotation_aux_from_main": eye3,
		"full_rigid_transform_aux_from_main": {
			"rotation_a_from_b": eye3,
			"translation_a_m": [0.0, 0.0, 0.0],
		},
		"reference_scene_version": "ref-v1",
		"installation_uncertainty": {
			"state_ref": {"station_id": "s1", "config_digest": "cfg-s1", "state_version": "1"},
			"position_covariance_enu_m2": [1e-6, 0.0, 0.0, 0.0, 1e-6, 0.0, 0.0, 0.0, 1e-6],
			"orientation_covariance_enu_rad2": [1e-8, 0.0, 0.0, 0.0, 1e-8, 0.0, 0.0, 0.0, 1e-8],
			"fixed_extrinsic_covariance_aux_rad2": [1e-8, 0.0, 0.0, 0.0, 1e-8, 0.0, 0.0, 0.0, 1e-8],
			"measurement_source": "survey",
		},
		"reference_anchor": {
			"reference_id": "board-1",
			"world_frame_id": "enu",
			"landmarks": [{
				"landmark_id": "board-corner-0",
				"position_enu_m": [1.0, 2.0, 0.5],
				"covariance_enu_m2": [1e-6, 0.0, 0.0, 0.0, 1e-6, 0.0, 0.0, 0.0, 1e-6],
				"visual_description": "top-left corner",
			}],
		},
	}


func _save_file(path: String, data: PackedByteArray) -> void:
	var f := FileAccess.open(path, FileAccess.WRITE)
	f.store_buffer(data)
	f.close()

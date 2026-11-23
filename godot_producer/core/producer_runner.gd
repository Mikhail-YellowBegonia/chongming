class_name ProducerRunner
extends RefCounted
## Deterministic, single-exposure Godot Producer execution engine.
## Drives any `ProducerSceneContract` scene, evaluates 64-bit truth and
## physics raycast occlusion, captures single-exposure RGB8 frames, and streams
## everything over TCP to `V-Switch Record`.

const ProtoWire = preload("res://core/proto_wire.gd")
const ScalarMath = preload("res://core/scalar64_math.gd")
const SceneContract = preload("res://core/scene_contract.gd")
const BusClient = preload("res://core/producer_bus_client.gd")


func run(
	scene: SceneContract,
	client: BusClient,
	bus_host: String,
	bus_port: int,
	truth_host: String,
	truth_port: int,
	overrides: Dictionary = {}
) -> Dictionary:
	var manifest := scene.get_scene_manifest()
	if manifest.is_empty():
		return {"ok": false, "error": "Scene returned empty manifest"}

	var scenario_id := str(overrides.get("scenario_id", manifest.get("scenario_id", "godot-scene")))
	var world_frame_id := str(manifest.get("world_frame_id", "enu"))
	var clock_domain_id := str(manifest.get("clock_domain_id", "sim"))
	var random_seed := int(overrides.get("random_seed", manifest.get("random_seed", 42)))
	var start_time_ns := int(manifest.get("start_time_ns", 0))
	var step_period_ns := int(overrides.get("step_period_ns", manifest.get("step_period_ns", 33333333)))
	var total_steps := int(overrides.get("total_steps", manifest.get("total_steps", 10)))
	var occluder_mask := int(manifest.get("occluder_collision_mask", 1))
	var instance_id := str(overrides.get("instance_id", "godot-producer-1"))
	var session_id := str(overrides.get("session_id", "%s-prod-%d" % [scenario_id, random_seed]))
	var stations: Array = manifest.get("stations", [])
	if stations.is_empty():
		return {"ok": false, "error": "Scene manifest contains no stations"}

	scene.ensure_camera_viewports(stations)
	RenderingServer.render_loop_enabled = false
	await scene.get_tree().physics_frame
	await scene.get_tree().physics_frame
	await scene.get_tree().process_frame

	var station_ids := []
	for st in stations:
		station_ids.append(str(st["station_id"]))

	if not client.connect_and_handshake(
		bus_host,
		bus_port,
		truth_host,
		truth_port,
		instance_id,
		session_id,
		station_ids
	):
		return {"ok": false, "error": client.last_error()}

	for st in stations:
		if not client.send_station_config(st):
			client.close()
			return {"ok": false, "error": client.last_error()}

	var frame_counters: Dictionary = {}
	var ray_seq_counters: Dictionary = {}
	for sid in station_ids:
		frame_counters["%s:%d" % [sid, ProtoWire.CAMERA_ROLE_MAIN]] = 0
		frame_counters["%s:%d" % [sid, ProtoWire.CAMERA_ROLE_AUXILIARY]] = 0
		ray_seq_counters[sid] = 1

	var last_time_ns := start_time_ns
	var frames_sent := 0
	var rays_sent := 0
	var truth_frames_sent := 0
	var angular_var := 1e-6

	for step_index in range(total_steps):
		var exp_ns := start_time_ns + step_index * step_period_ns
		last_time_ns = exp_ns
		var targets_64 := scene.evaluate_targets_64(step_index, exp_ns)
		scene.apply_visual_state(step_index, exp_ns, targets_64)
		if bool(manifest.get("dynamic_colliders", false)):
			await scene.get_tree().physics_frame

		var capture_time := {
			"clock_domain_id": clock_domain_id,
			"exposure_start_ns": exp_ns,
			"exposure_end_ns": exp_ns,
			"clock_uncertainty_ns": 0,
			"rolling_shutter_model": "none",
		}
		var projections := []

		for st in stations:
			var sid := str(st["station_id"])
			var cfg_digest := str(st["config_digest"])
			var station_session_id := str(st.get("station_session_id", "%s-%s" % [session_id, sid]))

			# Capture auxiliary before main at identical timestamp when enabled.
			for role in [ProtoWire.CAMERA_ROLE_AUXILIARY, ProtoWire.CAMERA_ROLE_MAIN]:
				if not scene.should_capture_camera(sid, role, step_index):
					continue
				var pose := scene.get_camera_pose_64(st, role, step_index, exp_ns)
				var intr: Dictionary = pose["intrinsics"]
				var width := int(intr["image_width_px"])
				var height := int(intr["image_height_px"])
				var key := "%s:%d" % [sid, role]
				var frame_idx := int(frame_counters[key])
				frame_counters[key] = frame_idx + 1
				var role_label := "main" if role == ProtoWire.CAMERA_ROLE_MAIN else "aux"
				var frame_id := "%s-%s-%04d" % [sid, role_label, frame_idx]

				var rgb8 := await scene.capture_camera_rgb8(
					sid,
					role,
					pose["pos_enu_m"],
					pose["r_enu_from_cam"],
					intr
				)
				if rgb8.size() != width * height * 3:
					client.close()
					return {
						"ok": false,
						"error": "Captured RGB8 size mismatch for %s: got %d expected %d" % [
							frame_id,
							rgb8.size(),
							width * height * 3,
						],
					}
				var pkt := {
					"station_id": sid,
					"station_session_id": station_session_id,
					"camera_role": role,
					"frame_id": frame_id,
					"frame_index": frame_idx,
					"capture_time": capture_time,
					"station_config_digest": cfg_digest,
					"width_px": width,
					"height_px": height,
					"codec": ProtoWire.VIDEO_CODEC_RAW,
					"pixel_format": ProtoWire.PIXEL_FORMAT_RGB8,
					"row_stride_bytes": width * 3,
					"is_keyframe": true,
					"payload": rgb8,
				}
				if not client.send_video_packet(pkt):
					client.close()
					return {"ok": false, "error": client.last_error()}
				frames_sent += 1

				if role == ProtoWire.CAMERA_ROLE_MAIN:
					for tgt in targets_64:
						var proj := ScalarMath.project_pinhole_px(
							tgt["position_enu_m"],
							pose["pos_enu_m"],
							pose["r_enu_from_cam"],
							intr
						)
						var diag: Variant = ScalarMath.project_aabb_diagonal_px(
							tgt["position_enu_m"],
							tgt["half_extents_body_m"],
							tgt["r_enu_from_body"],
							pose["pos_enu_m"],
							pose["r_enu_from_cam"],
							intr
						)
						var occlusion := ProtoWire.OCCLUSION_OUT_OF_FOV
						if bool(proj["in_fov"]):
							occlusion = scene.evaluate_physics_occlusion(
								pose["pos_enu_m"],
								tgt["position_enu_m"],
								occluder_mask,
								tgt.get("exclude_rids", [])
							)
						projections.append({
							"station_id": sid,
							"target_id": str(tgt["target_id"]),
							"occlusion_status": occlusion,
							"projected_centroid_px": proj["centroid_px"],
							"projected_diagonal_px": diag,
							"range_m": float(proj["range_m"]),
							"frame_id": frame_id,
						})
						if occlusion == ProtoWire.OCCLUSION_VISIBLE:
							var ray_seq := int(ray_seq_counters[sid])
							ray_seq_counters[sid] = ray_seq + 1
							var ray_msg := _build_virtual_ray(
								sid,
								station_session_id,
								cfg_digest,
								world_frame_id,
								clock_domain_id,
								frame_id,
								ray_seq,
								capture_time,
								pose["pos_enu_m"],
								tgt["position_enu_m"],
								str(tgt["target_id"]),
								angular_var,
								st["installation_uncertainty"]["state_ref"],
								start_time_ns
							)
							if not client.send_ray_event(ray_msg):
								client.close()
								return {"ok": false, "error": client.last_error()}
							rays_sent += 1

		var truth_targets := []
		for tgt in targets_64:
			truth_targets.append({
				"target_id": str(tgt["target_id"]),
				"target_class": str(tgt["target_class"]),
				"position_enu_m": tgt["position_enu_m"],
				"velocity_enu_mps": tgt["velocity_enu_mps"],
				"bounding_radius_m": float(tgt["bounding_radius_m"]),
				"body_aabb_diagonal_m": float(tgt["body_aabb_diagonal_m"]),
			})
		var truth_frame := {
			"world_frame_id": world_frame_id,
			"experiment_time_ns": exp_ns,
			"step_index": step_index,
			"targets": truth_targets,
			"projections": projections,
		}
		if not client.send_truth_frame(truth_frame):
			client.close()
			return {"ok": false, "error": client.last_error()}
		truth_frames_sent += 1

	if not client.send_progress_report({
		"source_role": ProtoWire.COMPONENT_ROLE_PRODUCER,
		"source_id": instance_id,
		"session_id": session_id,
		"completed_time_ns": last_time_ns,
		"completed_sequence_no": total_steps,
		"finite_end_reached": true,
	}):
		client.close()
		return {"ok": false, "error": client.last_error()}

	client.close()
	return {
		"ok": true,
		"scenario_id": scenario_id,
		"steps": total_steps,
		"frames_sent": frames_sent,
		"rays_sent": rays_sent,
		"truth_frames_sent": truth_frames_sent,
		"last_time_ns": last_time_ns,
	}


static func _build_virtual_ray(
	station_id: String,
	station_session_id: String,
	config_digest: String,
	world_frame_id: String,
	clock_domain_id: String,
	frame_id: String,
	sequence_no: int,
	capture_time: Dictionary,
	cam_pos_enu: Array,
	target_pos_enu: Array,
	target_id: String,
	angular_var: float,
	installation_state_ref: Dictionary = {},
	baseline_start_ns: int = -1
) -> Dictionary:
	var direction := ScalarMath.vec3_normalize(
		ScalarMath.vec3_sub(target_pos_enu, cam_pos_enu)
	)
	var tangents := ScalarMath.orthonormal_tangent_basis(direction)
	var cal_ref := {
		"station_id": station_id,
		"station_session_id": station_session_id,
		"solution_id": "truth-baseline",
	}
	var exp_ns := int(capture_time["exposure_start_ns"])
	var cal_ns := exp_ns if baseline_start_ns < 0 else baseline_start_ns
	var cal_capture := {
		"clock_domain_id": clock_domain_id,
		"exposure_start_ns": cal_ns,
		"exposure_end_ns": cal_ns,
		"clock_uncertainty_ns": 0,
		"rolling_shutter_model": "none",
	}
	var proc_time := {"clock_domain_id": clock_domain_id, "time_ns": cal_ns}
	var interval := {"clock_domain_id": clock_domain_id, "start_ns": cal_ns, "end_ns": null}
	return {
		"envelope": {
			"schema_version": ProtoWire.EVENT_SCHEMA_VERSION,
			"event_type": ProtoWire.EVENT_TYPE_RAY,
			"station_id": station_id,
			"station_session_id": station_session_id,
			"sequence_no": sequence_no,
			"station_config_digest": config_digest,
		},
		"frame_id": frame_id,
		"local_track_id": target_id,
		"capture_time": capture_time,
		"world_frame_id": world_frame_id,
		"calibration_solution_ref": cal_ref,
		"calibration_usage": ProtoWire.CALIBRATION_USAGE_VALID,
		"calibration_evidence": {
			"state_version": 1,
			"last_trusted_time": proc_time,
			"solution_accepted_time": proc_time,
			"solution_reference_capture_time": cal_capture,
			"solution_valid_interval": interval,
			"usable_interval": interval,
			"reason_codes": ["virtual_truth_ray"],
		},
		"ray_origin_enu_m": cam_pos_enu,
		"ray_direction_enu_unit": direction,
		"uncertainty": {
			"model": "tangent_gaussian_v1",
			"tangent_u_enu_unit": tangents[0],
			"tangent_v_enu_unit": tangents[1],
			"independent_angular_covariance_rad2": [angular_var, 0.0, 0.0, angular_var],
			"independent_origin_covariance_m2": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
			"shared_installation_state_ref": (
				installation_state_ref
				if not installation_state_ref.is_empty()
				else {
					"station_id": station_id,
					"config_digest": config_digest,
					"state_version": "1",
				}
			),
			"shared_calibration_solution_ref": cal_ref,
		},
		"detection_score": 1.0,
		"detection_score_definition": {
			"definition_id": "virtual_truth",
			"kind": ProtoWire.SCORE_KIND_RANKING,
			"minimum": 0.0,
			"maximum": 1.0,
		},
		"quality_flags": ["virtual_truth"],
	}

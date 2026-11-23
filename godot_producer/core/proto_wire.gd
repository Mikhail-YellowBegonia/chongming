class_name ChongmingProtoWire
extends RefCounted
## Pure GDScript Protobuf3 wire encoder, minimal SessionAck decoder, and
## 4-byte big-endian length-prefixed framing for `chongming.v1`.

const PROTOCOL_VERSION := "chongming.v1"
const MAX_FRAME_BYTES := 64 * 1024 * 1024

const WIRE_VARINT := 0
const WIRE_FIXED64 := 1
const WIRE_LEN := 2

const COMPONENT_ROLE_PRODUCER := 1

const CAMERA_ROLE_MAIN := 1
const CAMERA_ROLE_AUXILIARY := 2

const PIXEL_FORMAT_RGB8 := 2
const VIDEO_CODEC_RAW := 1

const EVENT_TYPE_RAY := 1

const SCORE_KIND_RANKING := 1
const CALIBRATION_USAGE_VALID := 1

const OCCLUSION_VISIBLE := 1
const OCCLUSION_OCCLUDED := 2
const OCCLUSION_OUT_OF_FOV := 3
const OCCLUSION_UNKNOWN := 4


static func encode_length_prefixed(payload: PackedByteArray) -> PackedByteArray:
	var length := payload.size()
	if length <= 0 or length > MAX_FRAME_BYTES:
		push_error("Frame payload size out of bounds: %d" % length)
		return PackedByteArray()
	var frame := encode_be_u32(length)
	frame.append_array(payload)
	return frame


static func encode_be_u32(value: int) -> PackedByteArray:
	var out := PackedByteArray()
	out.resize(4)
	out[0] = (value >> 24) & 0xff
	out[1] = (value >> 16) & 0xff
	out[2] = (value >> 8) & 0xff
	out[3] = value & 0xff
	return out


static func decode_be_u32(header: PackedByteArray, offset: int = 0) -> int:
	return (
		(int(header[offset]) << 24)
		| (int(header[offset + 1]) << 16)
		| (int(header[offset + 2]) << 8)
		| int(header[offset + 3])
	)


static func _swap_u32_be(value: int) -> int:
	# PackedByteArray.encode_u32 writes little-endian on x86_64/aarch64; swap to store BE.
	return (
		((value & 0x000000ff) << 24)
		| ((value & 0x0000ff00) << 8)
		| ((value & 0x00ff0000) >> 8)
		| ((value >> 24) & 0x000000ff)
	)


static func append_varint(buf: PackedByteArray, value: int) -> void:
	var v := value
	while true:
		var low := v & 0x7f
		# Logical right-shift by 7 for signed 64-bit two's complement GDScript int.
		var next_v := ((v >> 1) & 0x7fffffffffffffff) >> 6
		if next_v == 0:
			buf.append(low)
			return
		buf.append(low | 0x80)
		v = next_v


static func append_tag(buf: PackedByteArray, field_no: int, wire_type: int) -> void:
	append_varint(buf, (field_no << 3) | wire_type)


static func append_int_field(buf: PackedByteArray, field_no: int, value: int, force: bool = false) -> void:
	if value == 0 and not force:
		return
	append_tag(buf, field_no, WIRE_VARINT)
	append_varint(buf, value)


static func append_bool_field(buf: PackedByteArray, field_no: int, value: bool, force: bool = false) -> void:
	if not value and not force:
		return
	append_tag(buf, field_no, WIRE_VARINT)
	buf.append(1 if value else 0)


static func append_double_field(buf: PackedByteArray, field_no: int, value: float, force: bool = false) -> void:
	if value == 0.0 and not force:
		return
	append_tag(buf, field_no, WIRE_FIXED64)
	var start := buf.size()
	buf.resize(start + 8)
	buf.encode_double(start, value)


static func append_bytes_field(buf: PackedByteArray, field_no: int, data: PackedByteArray, force: bool = false) -> void:
	if data.is_empty() and not force:
		return
	append_tag(buf, field_no, WIRE_LEN)
	append_varint(buf, data.size())
	buf.append_array(data)


static func append_string_field(buf: PackedByteArray, field_no: int, text: String, force: bool = false) -> void:
	if text.is_empty() and not force:
		return
	append_bytes_field(buf, field_no, text.to_utf8_buffer(), force)


static func append_packed_doubles_field(buf: PackedByteArray, field_no: int, values: Array) -> void:
	if values.is_empty():
		return
	append_tag(buf, field_no, WIRE_LEN)
	var count := values.size()
	append_varint(buf, count * 8)
	var start := buf.size()
	buf.resize(start + count * 8)
	for i in count:
		buf.encode_double(start + i * 8, float(values[i]))


static func append_message_field(buf: PackedByteArray, field_no: int, encoded: PackedByteArray) -> void:
	append_bytes_field(buf, field_no, encoded, true)


static func encode_vec2(x: float, y: float) -> PackedByteArray:
	var out := PackedByteArray()
	append_double_field(out, 1, x)
	append_double_field(out, 2, y)
	return out


static func encode_vec3(v: Array) -> PackedByteArray:
	var out := PackedByteArray()
	append_double_field(out, 1, float(v[0]))
	append_double_field(out, 2, float(v[1]))
	append_double_field(out, 3, float(v[2]))
	return out


static func encode_mat(values: Array) -> PackedByteArray:
	var out := PackedByteArray()
	append_packed_doubles_field(out, 1, values)
	return out


static func encode_capture_time(ct: Dictionary) -> PackedByteArray:
	var out := PackedByteArray()
	append_string_field(out, 1, str(ct.get("clock_domain_id", "")))
	append_int_field(out, 2, int(ct.get("exposure_start_ns", 0)))
	append_int_field(out, 3, int(ct.get("exposure_end_ns", 0)))
	append_int_field(out, 4, int(ct.get("clock_uncertainty_ns", 0)))
	append_string_field(out, 5, str(ct.get("rolling_shutter_model", "")))
	return out


static func encode_processing_time(pt: Dictionary) -> PackedByteArray:
	var out := PackedByteArray()
	append_string_field(out, 1, str(pt.get("clock_domain_id", "")))
	append_int_field(out, 2, int(pt.get("time_ns", 0)))
	return out


static func encode_time_interval(iv: Dictionary) -> PackedByteArray:
	var out := PackedByteArray()
	append_string_field(out, 1, str(iv.get("clock_domain_id", "")))
	append_int_field(out, 2, int(iv.get("start_ns", 0)))
	if iv.has("end_ns") and iv["end_ns"] != null:
		append_int_field(out, 3, int(iv["end_ns"]), true)
	return out


static func encode_event_envelope(env: Dictionary) -> PackedByteArray:
	var out := PackedByteArray()
	append_string_field(out, 1, str(env.get("schema_version", PROTOCOL_VERSION)))
	append_int_field(out, 2, int(env.get("event_type", EVENT_TYPE_RAY)))
	append_string_field(out, 3, str(env.get("station_id", "")))
	append_string_field(out, 4, str(env.get("station_session_id", "")))
	append_int_field(out, 5, int(env.get("sequence_no", 0)))
	append_string_field(out, 6, str(env.get("station_config_digest", "")))
	return out


static func encode_score_definition(sd: Dictionary) -> PackedByteArray:
	var out := PackedByteArray()
	append_string_field(out, 1, str(sd.get("definition_id", "")))
	append_int_field(out, 2, int(sd.get("kind", SCORE_KIND_RANKING)))
	if sd.has("minimum") and sd["minimum"] != null:
		append_double_field(out, 3, float(sd["minimum"]), true)
	if sd.has("maximum") and sd["maximum"] != null:
		append_double_field(out, 4, float(sd["maximum"]), true)
	if sd.has("cross_station_comparison_group") and sd["cross_station_comparison_group"] != null:
		append_string_field(out, 5, str(sd["cross_station_comparison_group"]), true)
	return out


static func encode_session_hello(
	instance_id: String,
	session_id: String,
	station_ids: Array,
	role: int = COMPONENT_ROLE_PRODUCER,
	protocol_version: String = PROTOCOL_VERSION
) -> PackedByteArray:
	var out := PackedByteArray()
	append_string_field(out, 1, protocol_version)
	append_int_field(out, 2, role)
	append_string_field(out, 3, instance_id)
	append_string_field(out, 4, session_id)
	for sid in station_ids:
		append_string_field(out, 5, str(sid), true)
	return out


static func encode_camera_model(cam: Dictionary) -> PackedByteArray:
	var intr: Dictionary = cam["intrinsics"]
	var dist: Dictionary = cam.get("distortion", {"model": "none", "coefficients": [0.0, 0.0, 0.0, 0.0]})

	var intr_buf := PackedByteArray()
	append_int_field(intr_buf, 1, int(intr["image_width_px"]))
	append_int_field(intr_buf, 2, int(intr["image_height_px"]))
	append_message_field(intr_buf, 3, encode_mat(intr["matrix_px"]))

	var dist_buf := PackedByteArray()
	append_string_field(dist_buf, 1, str(dist.get("model", "none")))
	append_packed_doubles_field(dist_buf, 2, dist.get("coefficients", []))

	var out := PackedByteArray()
	append_message_field(out, 1, intr_buf)
	append_message_field(out, 2, dist_buf)
	return out


static func encode_geodetic_anchor(ga: Dictionary) -> PackedByteArray:
	var out := PackedByteArray()
	append_string_field(out, 1, str(ga.get("anchor_id", "")))
	append_string_field(out, 2, str(ga.get("datum", "")))
	append_string_field(out, 3, str(ga.get("datum_epoch", "")))
	append_double_field(out, 4, float(ga.get("latitude_deg", 0.0)))
	append_double_field(out, 5, float(ga.get("longitude_deg", 0.0)))
	append_double_field(out, 6, float(ga.get("height_m", 0.0)))
	append_string_field(out, 7, str(ga.get("height_reference", "")))
	return out


static func encode_rigid_transform(rt: Dictionary) -> PackedByteArray:
	var out := PackedByteArray()
	append_message_field(out, 1, encode_mat(rt["rotation_a_from_b"]))
	append_message_field(out, 2, encode_vec3(rt["translation_a_m"]))
	return out


static func encode_installation_state_ref(isr: Dictionary) -> PackedByteArray:
	var out := PackedByteArray()
	append_string_field(out, 1, str(isr.get("station_id", "")))
	append_string_field(out, 2, str(isr.get("config_digest", "")))
	append_string_field(out, 3, str(isr.get("state_version", "")))
	return out


static func encode_installation_uncertainty(iu: Dictionary) -> PackedByteArray:
	var out := PackedByteArray()
	append_message_field(out, 1, encode_installation_state_ref(iu["state_ref"]))
	append_message_field(out, 2, encode_mat(iu["position_covariance_enu_m2"]))
	append_message_field(out, 3, encode_mat(iu["orientation_covariance_enu_rad2"]))
	append_message_field(out, 4, encode_mat(iu["fixed_extrinsic_covariance_aux_rad2"]))
	append_string_field(out, 5, str(iu.get("measurement_source", "")))
	return out


static func encode_surveyed_reference(sr: Dictionary) -> PackedByteArray:
	var out := PackedByteArray()
	append_string_field(out, 1, str(sr.get("reference_id", "")))
	append_string_field(out, 2, str(sr.get("world_frame_id", "")))
	for lm in sr.get("landmarks", []):
		var lm_buf := PackedByteArray()
		append_string_field(lm_buf, 1, str(lm.get("landmark_id", "")))
		append_message_field(lm_buf, 2, encode_vec3(lm["position_enu_m"]))
		append_message_field(lm_buf, 3, encode_mat(lm["covariance_enu_m2"]))
		append_string_field(lm_buf, 4, str(lm.get("visual_description", "")))
		append_message_field(out, 3, lm_buf)
	return out


static func encode_station_config(cfg: Dictionary) -> PackedByteArray:
	var out := PackedByteArray()
	append_string_field(out, 1, str(cfg["station_id"]))
	append_string_field(out, 2, str(cfg["config_version"]))
	append_string_field(out, 3, str(cfg["config_digest"]))
	append_string_field(out, 4, str(cfg["world_frame_id"]))
	append_message_field(out, 5, encode_geodetic_anchor(cfg["geodetic_anchor"]))
	append_message_field(out, 6, encode_vec3(cfg["main_camera_position_enu_m"]))
	append_message_field(out, 7, encode_mat(cfg["baseline_orientation_enu_from_main"]))
	append_message_field(out, 8, encode_camera_model(cfg["main_camera"]))
	append_message_field(out, 9, encode_camera_model(cfg["auxiliary_camera"]))
	append_message_field(out, 10, encode_mat(cfg["fixed_rotation_aux_from_main"]))
	if cfg.has("full_rigid_transform_aux_from_main") and cfg["full_rigid_transform_aux_from_main"] != null:
		append_message_field(out, 11, encode_rigid_transform(cfg["full_rigid_transform_aux_from_main"]))
	append_string_field(out, 12, str(cfg.get("reference_scene_version", "")))
	append_message_field(out, 13, encode_installation_uncertainty(cfg["installation_uncertainty"]))
	if cfg.has("reference_anchor") and cfg["reference_anchor"] != null:
		append_message_field(out, 14, encode_surveyed_reference(cfg["reference_anchor"]))
	return out


static func encode_framed_video_packet(pkt: Dictionary) -> PackedByteArray:
	var out := PackedByteArray()
	append_string_field(out, 1, str(pkt["station_id"]))
	append_string_field(out, 2, str(pkt["station_session_id"]))
	append_int_field(out, 3, int(pkt["camera_role"]))
	append_string_field(out, 4, str(pkt["frame_id"]))
	append_int_field(out, 5, int(pkt["frame_index"]))
	append_message_field(out, 6, encode_capture_time(pkt["capture_time"]))
	append_string_field(out, 7, str(pkt["station_config_digest"]))
	append_int_field(out, 8, int(pkt["width_px"]))
	append_int_field(out, 9, int(pkt["height_px"]))
	append_int_field(out, 10, int(pkt.get("codec", VIDEO_CODEC_RAW)))
	append_int_field(out, 11, int(pkt.get("pixel_format", PIXEL_FORMAT_RGB8)))
	append_int_field(out, 12, int(pkt["row_stride_bytes"]))
	append_bool_field(out, 13, bool(pkt.get("is_keyframe", true)))
	append_bytes_field(out, 14, pkt["payload"])
	return out


static func encode_calibration_solution_ref(csr: Dictionary) -> PackedByteArray:
	var out := PackedByteArray()
	append_string_field(out, 1, str(csr.get("station_id", "")))
	append_string_field(out, 2, str(csr.get("station_session_id", "")))
	append_string_field(out, 3, str(csr.get("solution_id", "")))
	return out


static func encode_calibration_use_evidence(ev: Dictionary) -> PackedByteArray:
	var out := PackedByteArray()
	append_int_field(out, 1, int(ev.get("state_version", 0)))
	append_message_field(out, 2, encode_processing_time(ev["last_trusted_time"]))
	append_message_field(out, 3, encode_processing_time(ev["solution_accepted_time"]))
	append_message_field(out, 4, encode_capture_time(ev["solution_reference_capture_time"]))
	append_message_field(out, 5, encode_time_interval(ev["solution_valid_interval"]))
	append_message_field(out, 6, encode_time_interval(ev["usable_interval"]))
	for code in ev.get("reason_codes", []):
		append_string_field(out, 7, str(code), true)
	return out


static func encode_ray_uncertainty(unc: Dictionary) -> PackedByteArray:
	var out := PackedByteArray()
	append_string_field(out, 1, str(unc.get("model", "")))
	append_message_field(out, 2, encode_vec3(unc["tangent_u_enu_unit"]))
	append_message_field(out, 3, encode_vec3(unc["tangent_v_enu_unit"]))
	append_message_field(out, 4, encode_mat(unc["independent_angular_covariance_rad2"]))
	append_message_field(out, 5, encode_mat(unc["independent_origin_covariance_m2"]))
	append_message_field(out, 6, encode_installation_state_ref(unc["shared_installation_state_ref"]))
	append_message_field(out, 7, encode_calibration_solution_ref(unc["shared_calibration_solution_ref"]))
	return out


static func encode_ray_event(ray: Dictionary) -> PackedByteArray:
	var out := PackedByteArray()
	append_message_field(out, 1, encode_event_envelope(ray["envelope"]))
	append_string_field(out, 2, str(ray["frame_id"]))
	if ray.has("local_track_id") and ray["local_track_id"] != null:
		append_string_field(out, 3, str(ray["local_track_id"]), true)
	append_message_field(out, 4, encode_capture_time(ray["capture_time"]))
	append_string_field(out, 5, str(ray["world_frame_id"]))
	append_message_field(out, 6, encode_calibration_solution_ref(ray["calibration_solution_ref"]))
	append_int_field(out, 7, int(ray.get("calibration_usage", CALIBRATION_USAGE_VALID)))
	append_message_field(out, 8, encode_calibration_use_evidence(ray["calibration_evidence"]))
	append_message_field(out, 9, encode_vec3(ray["ray_origin_enu_m"]))
	append_message_field(out, 10, encode_vec3(ray["ray_direction_enu_unit"]))
	append_message_field(out, 11, encode_ray_uncertainty(ray["uncertainty"]))
	append_double_field(out, 12, float(ray.get("detection_score", 1.0)))
	append_message_field(out, 13, encode_score_definition(ray["detection_score_definition"]))
	if ray.has("track_quality") and ray["track_quality"] != null:
		append_double_field(out, 14, float(ray["track_quality"]), true)
	if ray.has("track_quality_definition") and ray["track_quality_definition"] != null:
		append_message_field(out, 15, encode_score_definition(ray["track_quality_definition"]))
	for flag in ray.get("quality_flags", []):
		append_string_field(out, 16, str(flag), true)
	return out


static func encode_progress_report(pr: Dictionary) -> PackedByteArray:
	var out := PackedByteArray()
	append_int_field(out, 1, int(pr.get("source_role", COMPONENT_ROLE_PRODUCER)))
	append_string_field(out, 2, str(pr.get("source_id", "")))
	append_string_field(out, 3, str(pr.get("session_id", "")))
	append_int_field(out, 4, int(pr.get("completed_time_ns", 0)))
	append_int_field(out, 5, int(pr.get("completed_sequence_no", 0)))
	append_bool_field(out, 6, bool(pr.get("finite_end_reached", false)))
	return out


static func encode_bus_message(
	bus_sequence_no: int,
	sent_wall_time_ns: int,
	payload_field_no: int,
	payload_bytes: PackedByteArray
) -> PackedByteArray:
	var out := PackedByteArray()
	append_int_field(out, 1, bus_sequence_no)
	append_int_field(out, 2, sent_wall_time_ns)
	append_message_field(out, payload_field_no, payload_bytes)
	return out


static func encode_target_truth(target: Dictionary) -> PackedByteArray:
	var out := PackedByteArray()
	append_string_field(out, 1, str(target["target_id"]))
	append_string_field(out, 2, str(target["target_class"]))
	append_message_field(out, 3, encode_vec3(target["position_enu_m"]))
	append_message_field(out, 4, encode_vec3(target["velocity_enu_mps"]))
	append_double_field(out, 5, float(target.get("bounding_radius_m", 0.0)))
	if target.has("body_aabb_diagonal_m") and target["body_aabb_diagonal_m"] != null:
		append_double_field(out, 6, float(target["body_aabb_diagonal_m"]), true)
	return out


static func encode_station_target_projection(proj: Dictionary) -> PackedByteArray:
	var out := PackedByteArray()
	append_string_field(out, 1, str(proj["station_id"]))
	append_string_field(out, 2, str(proj["target_id"]))
	append_int_field(out, 3, int(proj["occlusion_status"]))
	if proj.has("projected_centroid_px") and proj["projected_centroid_px"] != null:
		var uv: Array = proj["projected_centroid_px"]
		append_message_field(out, 4, encode_vec2(float(uv[0]), float(uv[1])))
	if proj.has("projected_diagonal_px") and proj["projected_diagonal_px"] != null:
		append_double_field(out, 5, float(proj["projected_diagonal_px"]), true)
	if proj.has("range_m") and proj["range_m"] != null:
		append_double_field(out, 6, float(proj["range_m"]), true)
	if proj.has("frame_id") and proj["frame_id"] != null:
		append_string_field(out, 7, str(proj["frame_id"]), true)
	return out


static func encode_truth_frame(tf: Dictionary) -> PackedByteArray:
	var out := PackedByteArray()
	append_string_field(out, 1, str(tf["world_frame_id"]))
	append_int_field(out, 2, int(tf["experiment_time_ns"]))
	append_int_field(out, 3, int(tf.get("step_index", 0)))
	for target in tf.get("targets", []):
		append_message_field(out, 4, encode_target_truth(target))
	for proj in tf.get("projections", []):
		append_message_field(out, 5, encode_station_target_projection(proj))
	return out


static func encode_test_framework_message(
	sequence_no: int,
	payload_field_no: int,
	payload_bytes: PackedByteArray
) -> PackedByteArray:
	var out := PackedByteArray()
	append_int_field(out, 1, sequence_no)
	append_message_field(out, payload_field_no, payload_bytes)
	return out


static func _read_varint(data: PackedByteArray, pos: int) -> Array:
	var result := 0
	var shift := 0
	var p := pos
	while p < data.size():
		var b := int(data[p])
		p += 1
		result |= (b & 0x7f) << shift
		if (b & 0x80) == 0:
			return [result, p, true]
		shift += 7
		if shift >= 70:
			return [0, p, false]
	return [0, p, false]


static func _skip_field(data: PackedByteArray, pos: int, wire_type: int) -> Array:
	match wire_type:
		0:
			var vr := _read_varint(data, pos)
			return [int(vr[1]), bool(vr[2])]
		1:
			return [pos + 8, pos + 8 <= data.size()]
		2:
			var lr := _read_varint(data, pos)
			if not bool(lr[2]):
				return [pos, false]
			var end_pos := int(lr[1]) + int(lr[0])
			return [end_pos, end_pos <= data.size()]
		5:
			return [pos + 4, pos + 4 <= data.size()]
		_:
			return [pos, false]


static func decode_session_ack(data: PackedByteArray) -> Dictionary:
	var out := {"accepted": false, "reason": "", "run_id": ""}
	var pos := 0
	while pos < data.size():
		var tag_r := _read_varint(data, pos)
		if not bool(tag_r[2]):
			return {"error": "truncated varint tag in SessionAck"}
		var tag := int(tag_r[0])
		pos = int(tag_r[1])
		var field_no := tag >> 3
		var wire_type := tag & 0x07
		if field_no == 1 and wire_type == WIRE_VARINT:
			var vr := _read_varint(data, pos)
			if not bool(vr[2]):
				return {"error": "truncated accepted bool in SessionAck"}
			out["accepted"] = int(vr[0]) != 0
			pos = int(vr[1])
		elif (field_no == 2 or field_no == 3) and wire_type == WIRE_LEN:
			var lr := _read_varint(data, pos)
			if not bool(lr[2]):
				return {"error": "truncated string length in SessionAck"}
			var slen := int(lr[0])
			pos = int(lr[1])
			if pos + slen > data.size():
				return {"error": "truncated string bytes in SessionAck"}
			var text := data.slice(pos, pos + slen).get_string_from_utf8()
			pos += slen
			if field_no == 2:
				out["reason"] = text
			else:
				out["run_id"] = text
		else:
			var sk := _skip_field(data, pos, wire_type)
			if not bool(sk[1]):
				return {"error": "corrupt field in SessionAck"}
			pos = int(sk[0])
	return out


static func decode_bus_message_session_ack(data: PackedByteArray) -> Dictionary:
	var pos := 0
	while pos < data.size():
		var tag_r := _read_varint(data, pos)
		if not bool(tag_r[2]):
			return {"error": "truncated tag in BusMessage"}
		var tag := int(tag_r[0])
		pos = int(tag_r[1])
		var field_no := tag >> 3
		var wire_type := tag & 0x07
		if field_no == 11 and wire_type == WIRE_LEN:
			var lr := _read_varint(data, pos)
			if not bool(lr[2]):
				return {"error": "truncated SessionAck length in BusMessage"}
			var slen := int(lr[0])
			pos = int(lr[1])
			if pos + slen > data.size():
				return {"error": "truncated SessionAck payload in BusMessage"}
			return decode_session_ack(data.slice(pos, pos + slen))
		var sk := _skip_field(data, pos, wire_type)
		if not bool(sk[1]):
			return {"error": "corrupt field in BusMessage"}
		pos = int(sk[0])
	return {"error": "BusMessage did not contain SessionAck"}


static func decode_test_framework_session_ack(data: PackedByteArray) -> Dictionary:
	var pos := 0
	while pos < data.size():
		var tag_r := _read_varint(data, pos)
		if not bool(tag_r[2]):
			return {"error": "truncated tag in TestFrameworkMessage"}
		var tag := int(tag_r[0])
		pos = int(tag_r[1])
		var field_no := tag >> 3
		var wire_type := tag & 0x07
		if field_no == 12 and wire_type == WIRE_LEN:
			var lr := _read_varint(data, pos)
			if not bool(lr[2]):
				return {"error": "truncated BusMessage length in TestFrameworkMessage"}
			var slen := int(lr[0])
			pos = int(lr[1])
			if pos + slen > data.size():
				return {"error": "truncated BusMessage payload in TestFrameworkMessage"}
			return decode_bus_message_session_ack(data.slice(pos, pos + slen))
		var sk := _skip_field(data, pos, wire_type)
		if not bool(sk[1]):
			return {"error": "corrupt field in TestFrameworkMessage"}
		pos = int(sk[0])
	return {"error": "TestFrameworkMessage did not contain BusMessage(SessionAck)"}

extends RefCounted

## Validated display copies only. A new worker owns fresh display and diagnostic state per run.
## Strictly passive: never runs scoring, motion simulation, or disk persistence.

const VIEWER_SCHEMA_VERSION := "chongming-viewer-v1"
const RAY_LIFETIME_NS := 1_000_000_000
const MEASUREMENT_LIFETIME_NS := 3_000_000_000
const DELETED_TRACK_LIFETIME_NS := 3_000_000_000
const HISTORY_LIMIT := 100
# Display-only cache bounds; algorithm output and evidence are unchanged.
const RAY_CACHE_LIMIT := 600
const MEASUREMENT_CACHE_LIMIT := 200
const TRACK_CACHE_LIMIT := 300
const DIAGNOSTIC_CACHE_LIMIT := 64
const TIMING_CAPACITY := 512
const MIN_TIME_NS := -9223372036854775807 - 1
const MAX_UINT64_STR := "18446744073709551615"
const MAX_INT64_STR := "9223372036854775807"

const TRUTH_COLOR := Color("#52e888")
const TRUTH_OCCLUDED_COLOR := Color("#f9a475")
const STATION_COLOR := Color("#26d9f2")
const RAY_COLOR := Color("#ffe052")
const MEASUREMENT_COLOR := Color("#ff424f")
const SELECTION_COLOR := Color("#66ddff")
const VOXEL_COLOR := Color("#80b8ff")
const VOXEL_MIN_ALPHA := 0.06
const VOXEL_MAX_ALPHA := 0.18
const TRACK_COLORS := {
	"tentative": Color("#ff9e9e"),
	"confirmed": Color("#db2638"),
	"coasting": Color("#ff9d32"),
	"deleted": Color("#929292"),
}

const VALID_MODES := ["edge", "center", "integrated", "live"]
const VALID_CALIBRATION_HEALTH := ["initializing", "valid", "frozen", "stale", "recovering", "invalid"]
const VALID_CALIBRATION_USAGE := ["valid", "frozen", "stale"]
const VALID_OCCLUSION_STATUS := ["visible", "occluded", "out_of_fov", "unknown"]
const VALID_FUSION_STATUS := [
	"located",
	"geometry_degenerate",
	"association_ambiguous",
	"insufficient_stations",
	"no_candidate_in_volume",
	"rejected_input",
]
const VALID_TRACK_LIFECYCLE := ["tentative", "confirmed", "coasting", "deleted"]
const VALID_ERROR_UNITS := ["px", "m"]

# UV.x is distance along a ray, so dash/dot spacing is in metres, not frame time.
const RAY_SHADER := """shader_type spatial;
render_mode unshaded, shadows_disabled;
uniform vec4 line_color : source_color;
uniform int pattern = 0;
void fragment() {
    if (pattern == 1 && mod(UV.x, 4.0) > 2.0) { discard; }
    if (pattern == 2 && mod(UV.x, 1.2) > 0.15) { discard; }
    ALBEDO = line_color.rgb;
}
"""

var rays: Dictionary = {}
# Display-only index: one current ray per station/local-track pair.
var ray_by_track: Dictionary = {}
var ray_track_keys: Dictionary = {}
var measurements: Dictionary = {}
var tracks: Dictionary = {}
var track_history: Dictionary = {}
var station_states: Dictionary = {}
var calibration_states: Dictionary = {}
var edge_diagnostics: Array = []
var score_by_unit: Dictionary = {}
var latest_truth: Dictionary = {}
var voxel_space: Variant = null
var voxel_update_id := 0
var voxel_update_id_str := ""
var selected_measurement_id := ""
var _voxel_dirty := true
var update_count := 0
var error := ""

# Passive V-Switch Viewer stream state.
var viewer_active := false
var current_run_id := ""
var current_mode := ""
var has_truth := false
var last_update_id_str := ""
var last_sequence_no_str := ""
var dropped_sequence_count := 0
var last_frame: Dictionary = {}

# Bounded CPU wall timings. These do not measure GPU completion or UI frame rate.
var _timings := {"update_ms": [], "redraw_ms": []}

var display_time_ns := MIN_TIME_NS
var layers: Dictionary = {}
var _root: Node3D
var _stations_root: Node3D
var _camera: Camera3D
var _enu: Callable
var _station_origins: Dictionary = {}
var _ray_materials: Dictionary = {}
var _selected_ray_materials: Dictionary = {}


func _record_timing(key: String, started_us: int) -> void:
	var samples: Array = _timings[key]
	samples.append(float(Time.get_ticks_usec() - started_us) / 1000.0)
	if samples.size() > TIMING_CAPACITY:
		samples.pop_front()


func performance_summary() -> Dictionary:
	var result := {}
	for key in _timings:
		var samples: Array = _timings[key].duplicate()
		samples.sort()
		result[key] = {
			"samples": samples.size(),
			"p50": null if samples.is_empty() else samples[ceili(samples.size() * 0.5) - 1],
			"p95": null if samples.is_empty() else samples[ceili(samples.size() * 0.95) - 1],
			"max": null if samples.is_empty() else samples[-1],
		}
	result["ray_count"] = rays.size()
	result["measurement_count"] = measurements.size()
	result["track_count"] = tracks.size()
	result["voxel_count"] = _voxel_cell_count()
	return result


func _voxel_cell_count() -> int:
	if voxel_space == null:
		return 0
	if voxel_space.has("cells"):
		return voxel_space.cells.size()
	return voxel_space.get("scored_voxels", []).size()


func reset_caches() -> void:
	latest_truth.clear()
	rays.clear()
	ray_by_track.clear()
	ray_track_keys.clear()
	measurements.clear()
	tracks.clear()
	track_history.clear()
	station_states.clear()
	calibration_states.clear()
	edge_diagnostics.clear()
	score_by_unit.clear()
	_station_origins.clear()
	voxel_space = null
	voxel_update_id = 0
	voxel_update_id_str = ""
	selected_measurement_id = ""
	_voxel_dirty = true
	last_frame.clear()
	last_update_id_str = ""
	last_sequence_no_str = ""
	dropped_sequence_count = 0
	display_time_ns = MIN_TIME_NS


## Accept a passive V-Switch `chongming-viewer-v1` DisplayFrame.
## Validates a private copy before mutating any display caches.
func receive_display_frame(frame_value: Variant, defer_redraw := false) -> bool:
	var started_us := Time.get_ticks_usec()
	error = ""
	if not frame_value is Dictionary:
		return _reject("DisplayFrame must be a JSON object")
	var frame: Dictionary = (frame_value as Dictionary).duplicate(true)
	if not _validate_display_frame(frame):
		return false

	var run_id: String = frame["run_id"]
	var update_id_str: String = frame["update_id"]
	var sequence_no_str: String = frame["sequence_no"]
	var mode_str: String = frame["mode"]
	var frame_has_truth: bool = frame["has_truth"]

	if current_run_id != "" and run_id != current_run_id:
		reset_caches()
	elif current_run_id == run_id and last_update_id_str != "":
		var cmp_update := _cmp_uint64_str(update_id_str, last_update_id_str)
		if cmp_update < 0:
			return _reject("update_id regressed within run %s" % run_id)
		if cmp_update == 0:
			if last_sequence_no_str != "" and _cmp_uint64_str(sequence_no_str, last_sequence_no_str) > 0:
				_update_sequence_gap(sequence_no_str)
			return true

	viewer_active = true
	current_run_id = run_id
	current_mode = mode_str
	has_truth = frame_has_truth
	last_update_id_str = update_id_str
	_update_sequence_gap(sequence_no_str)
	last_frame = frame
	display_time_ns = maxi(display_time_ns, frame["experiment_time_ns"])

	station_states.clear()
	_station_origins.clear()
	for station in frame["stations"]:
		var st: Dictionary = station
		station_states[st["station_id"]] = st
		_station_origins[st["station_id"]] = _enu.call(st["main_camera_position_enu_m"])

	if frame_has_truth:
		if frame.has("truth"):
			latest_truth = frame["truth"]
			display_time_ns = maxi(display_time_ns, latest_truth["experiment_time_ns"])
		if frame.has("score"):
			for item in frame["score"]["events"]:
				var ev: Dictionary = item
				score_by_unit[ev["error_unit"]] = ev
	else:
		latest_truth.clear()
		score_by_unit.clear()

	var rays_overlay: Dictionary = frame["rays"]
	for ray in rays_overlay["items"]:
		var r: Dictionary = ray
		var key := _ray_key(r["event_id"])
		var track_key := ""
		if r.get("local_track_id") != null and not str(r["local_track_id"]).is_empty():
			track_key = "%s|%s" % [str(r["event_id"][0]), str(r["local_track_id"])]
		if track_key != "" and ray_by_track.has(track_key):
			rays.erase(ray_by_track[track_key])
		rays[key] = r
		if track_key != "":
			ray_by_track[track_key] = key
			ray_track_keys[key] = track_key

	for cal in rays_overlay["calibration_states"]:
		var cs: Dictionary = cal
		calibration_states[cs["station_id"]] = cs

	for diag in rays_overlay["diagnostics"]:
		edge_diagnostics.append(diag)
		if edge_diagnostics.size() > DIAGNOSTIC_CACHE_LIMIT:
			edge_diagnostics.pop_front()

	var answer_overlay: Dictionary = frame["answer"]
	for measurement in answer_overlay["measurements"]:
		var m_item: Dictionary = measurement
		measurements[m_item["measurement_id"]] = m_item

	for track in answer_overlay["tracks"]:
		var t_item: Dictionary = track
		var tid: String = t_item["track_id"]
		if not tracks.has(tid) or t_item["event_time_ns"] >= tracks[tid]["event_time_ns"]:
			tracks[tid] = t_item
		if t_item["position_enu_m"] != null:
			_append_history_point(tid, t_item["event_time_ns"], t_item["position_enu_m"])

	if frame.has("sparse_voxel_grid"):
		var grid: Dictionary = frame["sparse_voxel_grid"]
		if grid["cells"].is_empty():
			voxel_space = null
		else:
			voxel_space = grid
		voxel_update_id_str = update_id_str
		voxel_update_id = update_id_str.to_int() if _fits_signed_int64(update_id_str) else update_count + 1
		_voxel_dirty = true

	_prune()
	_cap_cache(rays, RAY_CACHE_LIMIT)
	_cap_cache(measurements, MEASUREMENT_CACHE_LIMIT)
	_cap_track_cache()
	_rebuild_ray_index()
	update_count += 1
	if not defer_redraw:
		redraw()
	_record_timing("update_ms", started_us)
	return true


func _update_sequence_gap(seq_str: String) -> void:
	if last_sequence_no_str != "":
		var prev := last_sequence_no_str.to_int() if _fits_signed_int64(last_sequence_no_str) else 0
		var curr := seq_str.to_int() if _fits_signed_int64(seq_str) else 0
		if _fits_signed_int64(last_sequence_no_str) and _fits_signed_int64(seq_str) and curr > prev + 1:
			dropped_sequence_count += curr - prev - 1
	last_sequence_no_str = seq_str


func _append_history_point(track_id: String, time_ns: int, pos: Array) -> void:
	if not track_history.has(track_id):
		track_history[track_id] = []
	var history: Array = track_history[track_id]
	var point := {"time_ns": time_ns, "position_enu_m": pos}
	var index := 0
	while index < history.size() and history[index].time_ns < point.time_ns:
		index += 1
	if index < history.size() and history[index].time_ns == point.time_ns:
		history[index] = point
	else:
		history.insert(index, point)
	while history.size() > HISTORY_LIMIT:
		history.pop_front()


func _expired(time_ns: int, lifetime_ns: int) -> bool:
	# Guard subtraction at the signed-64-bit lower bound; never round nanoseconds to floats.
	return display_time_ns >= MIN_TIME_NS + lifetime_ns and time_ns < display_time_ns - lifetime_ns


func _prune() -> void:
	for key in rays.keys():
		if _expired(rays[key].capture_time_ns, RAY_LIFETIME_NS):
			rays.erase(key)
	for key in measurements.keys():
		if _expired(measurements[key].reference_time_ns, MEASUREMENT_LIFETIME_NS):
			measurements.erase(key)
	for key in tracks.keys():
		if tracks[key].lifecycle == "deleted" and _expired(tracks[key].event_time_ns, DELETED_TRACK_LIFETIME_NS):
			tracks.erase(key)
			track_history.erase(key)


func _ray_key(event_id: Array) -> String:
	return JSON.stringify([str(event_id[0]), str(event_id[1]), str(event_id[2])])


func _rebuild_ray_index() -> void:
	ray_by_track.clear()
	ray_track_keys.clear()
	for key in rays.keys():
		var ray: Dictionary = rays[key]
		if ray.get("local_track_id") == null or str(ray.get("local_track_id")).is_empty():
			continue
		var track_key := "%s|%s" % [str(ray.event_id[0]), str(ray.local_track_id)]
		ray_by_track[track_key] = key
		ray_track_keys[key] = track_key


func _cap_cache(cache: Dictionary, limit: int) -> void:
	# Keep the newest insertions; insertion order is the update order.
	if cache.size() <= limit:
		return
	var keys := cache.keys()
	for index in range(cache.size() - limit):
		cache.erase(keys[index])


func _cap_track_cache() -> void:
	if tracks.size() <= TRACK_CACHE_LIMIT:
		return
	var keys := tracks.keys()
	for index in range(tracks.size() - TRACK_CACHE_LIMIT):
		var key = keys[index]
		tracks.erase(key)
		track_history.erase(key)


func attach(parent: Node3D, camera: Camera3D, enu: Callable) -> void:
	_camera = camera
	_enu = enu
	_root = Node3D.new()
	_root.name = "AlgorithmVisualization"
	parent.add_child(_root)
	_stations_root = Node3D.new()
	_stations_root.name = "ViewerStations"
	_root.add_child(_stations_root)
	for key in ["truth", "rays", "measurements", "tracks", "voxels"]:
		var layer := Node3D.new()
		layer.name = key
		_root.add_child(layer)
		layers[key] = layer
	var shader := Shader.new()
	shader.code = RAY_SHADER
	for pattern in ["valid", "frozen", "stale"]:
		var material := ShaderMaterial.new()
		material.shader = shader
		material.set_shader_parameter("line_color", RAY_COLOR)
		material.set_shader_parameter("pattern", ["valid", "frozen", "stale"].find(pattern))
		_ray_materials[pattern] = material
		var selected := material.duplicate() as ShaderMaterial
		selected.set_shader_parameter("line_color", SELECTION_COLOR)
		_selected_ray_materials[pattern] = selected
	_voxel_dirty = true
	redraw()


func set_layer_visible(key: String, enabled: bool) -> void:
	layers[key].visible = enabled
	# Hidden layers are not rebuilt on every update; rebuild once when shown.
	if enabled:
		if key == "voxels":
			_voxel_dirty = true
		redraw()


func set_station_origins(markers: Array) -> void:
	var origins := {}
	for marker in markers:
		if marker.kind == "camera" and str(marker.id).ends_with("/main"):
			origins[str(marker.id).trim_suffix("/main")] = _enu.call(marker.position)
	if origins != _station_origins:
		_station_origins = origins
		redraw()


func redraw() -> void:
	if _root == null:
		return
	var started_us := Time.get_ticks_usec()
	for child in _stations_root.get_children():
		child.free()
	if viewer_active:
		_draw_viewer_stations()
	for key in layers:
		if key == "voxels" and not _voxel_dirty:
			continue
		for child in layers[key].get_children():
			child.free()
	if layers["truth"].visible and viewer_active and has_truth and not latest_truth.is_empty():
		_draw_viewer_truth(latest_truth)
	if layers["rays"].visible:
		for ray in rays.values():
			_draw_ray(ray)
	if layers["measurements"].visible:
		for measurement in measurements.values():
			if measurement.position_enu_m != null:
				var anchor := _anchor(layers.measurements, measurement.measurement_id, measurement.position_enu_m)
				var color := SELECTION_COLOR if measurement.measurement_id == selected_measurement_id else MEASUREMENT_COLOR
				_marker(anchor, color)
				_uncertainty(anchor, measurement.position_std_enu_m, color)
	if layers["tracks"].visible:
		for track in tracks.values():
			_draw_track(track)
	if layers["voxels"].visible and _voxel_dirty:
		_draw_voxels()
		_voxel_dirty = false
	_record_timing("redraw_ms", started_us)


func select_measurement(id: String) -> bool:
	if id != "" and id != selected_measurement_id and not measurements.has(id):
		return false
	if selected_measurement_id != id:
		selected_measurement_id = id
		redraw()
	return true


func _measurement_ray_ids(measurement: Dictionary) -> Array:
	if measurement.has("supporting_ray_event_ids"):
		return measurement["supporting_ray_event_ids"]
	return measurement.get("supporting_ray_ids", [])


func selection_summary() -> Dictionary:
	var result := {
		"measurement_id": selected_measurement_id,
		"expired": false,
		"available_ray_ids": [],
		"missing_ray_ids": [],
	}
	if selected_measurement_id == "":
		return result
	if not measurements.has(selected_measurement_id):
		result.expired = true
		return result
	for id in _measurement_ray_ids(measurements[selected_measurement_id]):
		if rays.has(_ray_key(id)):
			result.available_ray_ids.append(id)
		else:
			result.missing_ray_ids.append(id)
	return result


func _draw_viewer_stations() -> void:
	for station_id in station_states:
		var st: Dictionary = station_states[station_id]
		var anchor := _anchor(_stations_root, station_id, st["main_camera_position_enu_m"])
		var label := Label3D.new()
		var usage_text := "LIVE" if st.get("calibration_usage") == null else str(st["calibration_usage"]).to_upper()
		label.text = "CAM %s (%s)" % [station_id, usage_text]
		label.position = Vector3(0.0, 2.5, 0.0)
		label.billboard = BaseMaterial3D.BILLBOARD_ENABLED
		label.fixed_size = true
		label.font_size = 22
		label.pixel_size = 0.0012
		label.outline_size = 4
		label.no_depth_test = true
		label.layers = 2
		label.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
		label.modulate = STATION_COLOR
		anchor.add_child(label)
		_mesh(anchor, "StationMarker", [
			Vector3(-0.8, 0, 0), Vector3(0.8, 0, 0),
			Vector3(0, -0.8, 0), Vector3(0, 0.8, 0),
			Vector3(0, 0, -0.8), Vector3(0, 0, 0.8),
		], STATION_COLOR)


func _draw_viewer_truth(truth: Dictionary) -> void:
	var visible_by_target := {}
	var occluded_by_target := {}
	for proj in truth.get("projections", []):
		var p: Dictionary = proj
		var status: String = p["occlusion_status"]
		if status == "out_of_fov":
			continue
		var tid: String = p["target_id"]
		if status == "occluded":
			if not occluded_by_target.has(tid):
				occluded_by_target[tid] = []
			occluded_by_target[tid].append(p["station_id"])
		else:
			if not visible_by_target.has(tid):
				visible_by_target[tid] = []
			visible_by_target[tid].append(p["station_id"])

	for target in truth.get("targets", []):
		var t: Dictionary = target
		var tid: String = t["target_id"]
		var anchor := _anchor(layers.truth, tid, t["position_enu_m"])
		_circle(anchor, float(t["bounding_radius_m"]), TRUTH_COLOR)
		_arrow(anchor, _enu.call(t["velocity_enu_mps"]), TRUTH_COLOR)
		var visible_links: Array[Vector3] = []
		for sid in visible_by_target.get(tid, []):
			if _station_origins.has(sid):
				visible_links.append(Vector3.ZERO)
				visible_links.append(_station_origins[sid] - anchor.position)
		if not visible_links.is_empty():
			_mesh(anchor, "StationLinks", visible_links, TRUTH_COLOR)
		var occ_links: Array[Vector3] = []
		for sid in occluded_by_target.get(tid, []):
			if _station_origins.has(sid):
				occ_links.append(Vector3.ZERO)
				occ_links.append(_station_origins[sid] - anchor.position)
		if not occ_links.is_empty():
			_mesh(anchor, "OccludedStationLinks", occ_links, TRUTH_OCCLUDED_COLOR)


func _draw_voxels() -> void:
	if voxel_space == null:
		return
	var is_protocol_grid: bool = voxel_space.has("cells")
	var items: Array = voxel_space["cells"] if is_protocol_grid else voxel_space.get("scored_voxels", [])
	if items.is_empty():
		return
	var min_enu: Array = voxel_space["min_enu_m"] if is_protocol_grid else voxel_space["search_box_min_enu_m"]
	var step_enu: Array = voxel_space["step_enu_m"] if is_protocol_grid else voxel_space["voxel_step_enu_m"]
	# Batch solid cubes by score, rendering as triangle faces with transparency.
	var groups := {}
	for voxel in items:
		var score: int = int(voxel["station_votes"] if is_protocol_grid else voxel["score"])
		if score <= 2:
			continue
		if not groups.has(score):
			groups[score] = []
		var lower: Array = []
		var upper: Array = []
		for axis in range(3):
			var idx: int = int([voxel["ix"], voxel["iy"], voxel["iz"]][axis]) if is_protocol_grid else int(voxel["grid_index"][axis])
			var lo := float(min_enu[axis]) + float(idx) * float(step_enu[axis])
			var hi := lo + float(step_enu[axis])
			if not is_protocol_grid:
				hi = minf(float(voxel_space["search_box_max_enu_m"][axis]), hi)
			lower.append(lo)
			upper.append(hi)
		var corners: Array[Vector3] = []
		for index in range(8):
			corners.append(_enu.call([
				upper[0] if (index & 1) != 0 else lower[0],
				upper[1] if (index & 2) != 0 else lower[1],
				upper[2] if (index & 4) != 0 else lower[2],
			]))
		var faces := [
			[corners[4], corners[5], corners[6]], [corners[5], corners[7], corners[6]],
			[corners[0], corners[2], corners[1]], [corners[1], corners[2], corners[3]],
			[corners[1], corners[3], corners[5]], [corners[3], corners[7], corners[5]],
			[corners[0], corners[4], corners[2]], [corners[2], corners[4], corners[6]],
			[corners[2], corners[3], corners[6]], [corners[3], corners[7], corners[6]],
			[corners[0], corners[1], corners[4]], [corners[1], corners[5], corners[4]],
		]
		for triangle in faces:
			groups[score].append(triangle[0])
			groups[score].append(triangle[1])
			groups[score].append(triangle[2])
	for score in groups:
		var points: Array[Vector3] = []
		points.assign(groups[score])
		var alpha := lerpf(VOXEL_MIN_ALPHA, VOXEL_MAX_ALPHA, clampf((float(score) - 3.0) / 12.0, 0.0, 1.0))
		_mesh(
			layers.voxels,
			"Score%d" % score,
			points,
			Color(VOXEL_COLOR, alpha),
			Mesh.PRIMITIVE_TRIANGLES,
		)


func _anchor(parent: Node3D, id: String, position: Array) -> Node3D:
	var anchor := Node3D.new()
	# IDs are data, not NodePaths; do not use them as node names.
	anchor.set_meta("id", id)
	anchor.position = _enu.call(position)
	parent.add_child(anchor)
	return anchor


func _mesh(
	parent: Node3D,
	name: String,
	points: Array[Vector3],
	color: Color,
	primitive := Mesh.PRIMITIVE_LINES,
	material: Material = null,
) -> MeshInstance3D:
	var instance := MeshInstance3D.new()
	instance.name = name
	instance.layers = 2
	instance.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	var mesh := ImmediateMesh.new()
	mesh.surface_begin(primitive)
	for index in range(points.size()):
		if primitive == Mesh.PRIMITIVE_LINES:
			mesh.surface_set_uv(Vector2(0.0 if index % 2 == 0 else points[index].distance_to(points[index - 1]), 0.0))
		mesh.surface_add_vertex(points[index])
	mesh.surface_end()
	instance.mesh = mesh
	if material == null:
		var solid := StandardMaterial3D.new()
		solid.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
		solid.albedo_color = color
		if color.a < 1.0:
			solid.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
			solid.cull_mode = BaseMaterial3D.CULL_DISABLED
		material = solid
	instance.material_override = material
	parent.add_child(instance)
	return instance


func _circle(parent: Node3D, radius: float, color: Color) -> void:
	if radius <= 0.0:
		return
	var points: Array[Vector3] = []
	for index in range(48):
		for angle in [TAU * index / 48.0, TAU * (index + 1) / 48.0]:
			points.append(Vector3(cos(angle), sin(angle), 0.0) * radius)
	var ring := _mesh(parent, "SizeCircle", points, color)
	var material := ring.material_override as StandardMaterial3D
	material.billboard_mode = BaseMaterial3D.BILLBOARD_ENABLED


func _marker(parent: Node3D, color: Color) -> void:
	_mesh(parent, "Position", [
		Vector3(-0.3, 0, 0), Vector3(0.3, 0, 0),
		Vector3(0, -0.3, 0), Vector3(0, 0.3, 0),
		Vector3(0, 0, -0.3), Vector3(0, 0, 0.3),
	], color)


func _uncertainty(parent: Node3D, std: Variant, color: Color) -> void:
	if std != null:
		# Contract: circle diameter is the largest diagonal standard deviation, not an ellipsoid.
		_circle(parent, maxf(std[0], maxf(std[1], std[2])) * 0.5, color)


func _arrow(parent: Node3D, direction: Vector3, color: Color) -> void:
	if direction.length_squared() <= 0.0:
		return
	var unit := direction.normalized()
	var side := unit.cross(Vector3.UP)
	if side.length_squared() < 0.001:
		side = unit.cross(Vector3.RIGHT)
	side = side.normalized()
	var head := direction.length() * 0.2
	_mesh(parent, "Velocity", [
		Vector3.ZERO, direction,
		direction, direction - unit * head + side * head * 0.5,
		direction, direction - unit * head - side * head * 0.5,
	], color)


func _draw_ray(ray: Dictionary) -> void:
	var ray_key := _ray_key(ray.event_id)
	var anchor := _anchor(layers.rays, ray_key, ray.origin_enu_m)
	var direction: Vector3 = _enu.call(ray.direction_enu_unit)
	var far_dist := _camera.far if _camera != null else 5000.0
	var end := direction * far_dist
	var selected := false
	if measurements.has(selected_measurement_id):
		var wanted: Array = _measurement_ray_ids(measurements[selected_measurement_id])
		for item in wanted:
			if _ray_key(item) == ray_key:
				selected = true
				break
	var color := SELECTION_COLOR if selected else RAY_COLOR
	_mesh(
		anchor,
		"Ray",
		[Vector3.ZERO, end],
		color,
		Mesh.PRIMITIVE_LINES,
		_selected_ray_materials[ray.calibration_usage] if selected else _ray_materials[ray.calibration_usage],
	)
	if ray.angular_std_rad == null or ray.angular_std_rad <= 0.0 or ray.angular_std_rad >= PI * 0.5:
		return
	var side := direction.cross(Vector3.UP)
	if side.length_squared() < 0.001:
		side = direction.cross(Vector3.RIGHT)
	side = side.normalized()
	var up := direction.cross(side).normalized()
	var radius := far_dist * tan(ray.angular_std_rad)
	var points: Array[Vector3] = []
	for index in range(32):
		for vertex in [
			Vector3.ZERO,
			end + radius * (side * cos(TAU * index / 32.0) + up * sin(TAU * index / 32.0)),
			end + radius * (side * cos(TAU * (index + 1) / 32.0) + up * sin(TAU * (index + 1) / 32.0)),
		]:
			points.append(vertex)
	_mesh(anchor, "AngularStdCone", points, Color(color, 0.08), Mesh.PRIMITIVE_TRIANGLES)


func _draw_track(track: Dictionary) -> void:
	var color: Color = TRACK_COLORS[track.lifecycle]
	var node := Node3D.new()
	node.set_meta("id", track.track_id)
	layers.tracks.add_child(node)
	var points: Array[Vector3] = []
	var history: Array = track_history.get(track.track_id, [])
	for index in range(1, history.size()):
		points.append(_enu.call(history[index - 1].position_enu_m))
		points.append(_enu.call(history[index].position_enu_m))
	if not points.is_empty():
		_mesh(node, "History", points, color)
	if track.position_enu_m != null:
		var anchor := _anchor(node, track.track_id, track.position_enu_m)
		_marker(anchor, color)
		_uncertainty(anchor, track.position_std_enu_m, color)


func _reject(reason: String) -> bool:
	error = reason
	return false


func _record(value: Variant, fields: Array) -> bool:
	return value is Dictionary and value.size() == fields.size() and value.has_all(fields)


func _text(value: Variant) -> bool:
	return value is String and not value.is_empty()


func _string_value(value: Variant) -> bool:
	return value is String


func _optional_string(value: Variant) -> bool:
	return value == null or value is String


func _number(value: Variant) -> bool:
	return (value is float or value is int) and is_finite(float(value))


func _optional_number(value: Variant) -> bool:
	return value == null or _number(value)


func _nonnegative(value: Variant) -> bool:
	return _number(value) and float(value) >= 0.0


func _optional_nonnegative(value: Variant) -> bool:
	return value == null or _nonnegative(value)


func _json_int(value: Variant, nonnegative := false) -> bool:
	if not (value is int or value is float):
		return false
	var f := float(value)
	if not is_finite(f) or f != floor(f):
		return false
	return not nonnegative or f >= 0.0


func _vec2(value: Variant) -> bool:
	return value is Array and value.size() == 2 and _number(value[0]) and _number(value[1])


func _optional_vec2(value: Variant) -> bool:
	return value == null or _vec2(value)


func _vector(value: Variant, nonnegative := false) -> bool:
	if not value is Array or value.size() != 3:
		return false
	for component in value:
		if not _number(component) or (nonnegative and float(component) < 0.0):
			return false
	return true


func _optional_vector(value: Variant, nonnegative := false) -> bool:
	return value == null or _vector(value, nonnegative)


func _rotation(value: Variant) -> bool:
	return value is Array and value.size() == 3 and _vector(value[0]) \
		and _vector(value[1]) and _vector(value[2])


func _strings(value: Variant, allow_empty := false) -> bool:
	if not value is Array:
		return false
	for item in value:
		if not (item is String and (allow_empty or not item.is_empty())):
			return false
	return true


func _texts(value: Variant) -> bool:
	return _strings(value, false)


func _integer(value: Variant, signed := false) -> bool:
	# JSON numbers are doubles in Godot. Contract integers are decimal strings.
	return value is String and value.is_valid_int() and str(value.to_int()) == value \
		and (signed or value.to_int() >= 0)


func _uint64_text(value: Variant) -> bool:
	if not value is String:
		return false
	var s: String = value
	if s.is_empty() or s.length() > 20:
		return false
	if s.length() > 1 and s[0] == "0":
		return false
	for idx in range(s.length()):
		var code := s.unicode_at(idx)
		if code < 48 or code > 57:
			return false
	if s.length() == 20 and s > MAX_UINT64_STR:
		return false
	return true


func _fits_signed_int64(s: String) -> bool:
	return s.length() < 19 or (s.length() == 19 and s <= MAX_INT64_STR)


func _cmp_uint64_str(a: String, b: String) -> int:
	if a.length() != b.length():
		return -1 if a.length() < b.length() else 1
	if a == b:
		return 0
	return -1 if a < b else 1


func _integers(record: Dictionary, fields: Array, signed := false) -> bool:
	for field in fields:
		if not _integer(record[field], signed):
			return false
		record[field] = record[field].to_int()
	return true


func _event_id(value: Variant) -> bool:
	if not value is Array or value.size() != 3 or not _text(value[0]) \
			or not _text(value[1]) or not _integer(value[2]):
		return false
	value[2] = value[2].to_int()
	return true


func _protocol_event_id(value: Variant) -> bool:
	return value is Array and value.size() == 3 \
		and _string_value(value[0]) and _string_value(value[1]) and _uint64_text(value[2])


func _validate_display_frame(frame: Dictionary) -> bool:
	var required := [
		"schema_version",
		"run_id",
		"mode",
		"update_id",
		"sequence_no",
		"experiment_time_ns",
		"has_truth",
		"stations",
		"rays",
		"answer",
	]
	if not frame.has_all(required):
		return _reject("DisplayFrame missing required fields")
	var allowed := required + ["truth", "score", "sparse_voxel_grid"]
	for key in frame.keys():
		if not key in allowed:
			return _reject("DisplayFrame has unexpected field: %s" % str(key))
	if frame["schema_version"] != VIEWER_SCHEMA_VERSION:
		return _reject("unsupported schema_version")
	if not _text(frame["run_id"]):
		return _reject("run_id must be a non-empty string")
	if not frame["mode"] in VALID_MODES:
		return _reject("invalid mode")
	if not _uint64_text(frame["update_id"]) or frame["update_id"] == "0":
		return _reject("update_id must be a positive uint64 decimal string")
	if not _uint64_text(frame["sequence_no"]) or frame["sequence_no"] == "0":
		return _reject("sequence_no must be a positive uint64 decimal string")
	if not _integers(frame, ["experiment_time_ns"], true):
		return _reject("experiment_time_ns must be an int64 decimal string")
	if not frame["has_truth"] is bool:
		return _reject("has_truth must be boolean")
	if frame["mode"] == "live" and frame["has_truth"]:
		return _reject("live mode must have has_truth == false")
	if not frame["has_truth"] and (frame.has("truth") or frame.has("score")):
		return _reject("truth and score must be omitted when has_truth is false")
	if not _validate_stations(frame["stations"]):
		return _reject("invalid stations")
	if frame.has("truth") and not _validate_truth_overlay(frame["truth"]):
		return _reject("invalid truth overlay")
	if not _validate_rays_overlay(frame["rays"]):
		return _reject("invalid rays overlay")
	if not _validate_answer_overlay(frame["answer"]):
		return _reject("invalid answer overlay")
	if frame.has("score") and not _validate_score_overlay(frame["score"]):
		return _reject("invalid score overlay")
	if frame.has("sparse_voxel_grid") and not _validate_sparse_voxel_grid(frame["sparse_voxel_grid"]):
		return _reject("invalid sparse_voxel_grid")
	return true


func _validate_stations(stations: Variant) -> bool:
	if not stations is Array:
		return false
	var seen := {}
	for entry in stations:
		if not _record(entry, [
			"station_id",
			"main_camera_position_enu_m",
			"baseline_orientation_enu_from_main",
			"calibrated_orientation_enu_from_main",
			"calibration_health",
			"calibration_usage",
			"state_version",
			"solution_id",
		]):
			return false
		var st: Dictionary = entry
		if not _text(st["station_id"]) or seen.has(st["station_id"]):
			return false
		seen[st["station_id"]] = true
		if not _vector(st["main_camera_position_enu_m"]) or not _rotation(st["baseline_orientation_enu_from_main"]):
			return false
		if st["calibrated_orientation_enu_from_main"] != null and not _rotation(st["calibrated_orientation_enu_from_main"]):
			return false
		if st["calibration_health"] != null and not st["calibration_health"] in VALID_CALIBRATION_HEALTH:
			return false
		if st["calibration_usage"] != null and not st["calibration_usage"] in VALID_CALIBRATION_USAGE:
			return false
		if st["state_version"] != null and not _uint64_text(st["state_version"]):
			return false
		if not _optional_string(st["solution_id"]):
			return false
	return true


func _validate_truth_overlay(truth: Variant) -> bool:
	if not _record(truth, ["world_frame_id", "experiment_time_ns", "step_index", "targets", "projections"]):
		return false
	var t: Dictionary = truth
	if not _string_value(t["world_frame_id"]) or not _integers(t, ["experiment_time_ns"], true) \
			or not _uint64_text(t["step_index"]) or not t["targets"] is Array or not t["projections"] is Array:
		return false
	var seen_targets := {}
	for target_val in t["targets"]:
		if not _record(target_val, [
			"target_id",
			"target_class",
			"position_enu_m",
			"velocity_enu_mps",
			"bounding_radius_m",
			"body_aabb_diagonal_m",
		]):
			return false
		var item: Dictionary = target_val
		if not _text(item["target_id"]) or seen_targets.has(item["target_id"]) \
				or not _string_value(item["target_class"]) \
				or not _vector(item["position_enu_m"]) or not _vector(item["velocity_enu_mps"]) \
				or not _nonnegative(item["bounding_radius_m"]) \
				or not _optional_nonnegative(item["body_aabb_diagonal_m"]):
			return false
		seen_targets[item["target_id"]] = true
	for proj_val in t["projections"]:
		if not _record(proj_val, [
			"station_id",
			"target_id",
			"occlusion_status",
			"projected_centroid_px",
			"projected_diagonal_px",
			"range_m",
			"frame_id",
		]):
			return false
		var p: Dictionary = proj_val
		if not _text(p["station_id"]) or not _text(p["target_id"]) \
				or not p["occlusion_status"] in VALID_OCCLUSION_STATUS \
				or not _optional_vec2(p["projected_centroid_px"]) \
				or not _optional_nonnegative(p["projected_diagonal_px"]) \
				or not _optional_nonnegative(p["range_m"]) \
				or not _optional_string(p["frame_id"]):
			return false
	return true


func _validate_rays_overlay(rays_val: Variant) -> bool:
	if not _record(rays_val, ["items", "calibration_states", "diagnostics"]):
		return false
	var overlay: Dictionary = rays_val
	if not overlay["items"] is Array or not overlay["calibration_states"] is Array or not overlay["diagnostics"] is Array:
		return false
	var seen_rays := {}
	for ray_val in overlay["items"]:
		if not _record(ray_val, [
			"event_id",
			"frame_id",
			"capture_time_ns",
			"exposure_start_ns",
			"exposure_end_ns",
			"origin_enu_m",
			"direction_enu_unit",
			"calibration_usage",
			"detection_score",
			"angular_std_rad",
			"local_track_id",
			"quality_flags",
		]):
			return false
		var r: Dictionary = ray_val
		if not _protocol_event_id(r["event_id"]) or not _string_value(r["frame_id"]) \
				or not _integers(r, ["capture_time_ns", "exposure_start_ns", "exposure_end_ns"], true) \
				or not _vector(r["origin_enu_m"]) or not _vector(r["direction_enu_unit"]) \
				or not r["calibration_usage"] in VALID_CALIBRATION_USAGE \
				or not _nonnegative(r["detection_score"]) or float(r["detection_score"]) > 1.0 \
				or not _optional_nonnegative(r["angular_std_rad"]) \
				or not _optional_string(r["local_track_id"]) \
				or not _strings(r["quality_flags"], true):
			return false
		var dir: Array = r["direction_enu_unit"]
		if not is_equal_approx(Vector3(dir[0], dir[1], dir[2]).length_squared(), 1.0):
			return false
		var rk := _ray_key(r["event_id"])
		if seen_rays.has(rk):
			return false
		seen_rays[rk] = true
	for cal_val in overlay["calibration_states"]:
		if not _record(cal_val, [
			"event_id",
			"station_id",
			"state_version",
			"health",
			"solution_id",
			"observed_at_ns",
			"reason_codes",
		]):
			return false
		var c: Dictionary = cal_val
		if not _protocol_event_id(c["event_id"]) or not _text(c["station_id"]) \
				or not _uint64_text(c["state_version"]) \
				or not c["health"] in VALID_CALIBRATION_HEALTH \
				or not _optional_string(c["solution_id"]) \
				or not _integers(c, ["observed_at_ns"], true) \
				or not _strings(c["reason_codes"], true):
			return false
	for diag_val in overlay["diagnostics"]:
		if not _record(diag_val, ["event_id", "station_id", "capture_time_ns", "category", "reason_codes"]):
			return false
		var d: Dictionary = diag_val
		if not _protocol_event_id(d["event_id"]) or not _text(d["station_id"]) \
				or not _string_value(d["category"]) or not _strings(d["reason_codes"], true):
			return false
		if d["capture_time_ns"] != null and not _integers(d, ["capture_time_ns"], true):
			return false
	return true


func _validate_answer_overlay(answer_val: Variant) -> bool:
	if not _record(answer_val, ["measurements", "tracks"]):
		return false
	var answer: Dictionary = answer_val
	if not answer["measurements"] is Array or not answer["tracks"] is Array:
		return false
	var seen_meas := {}
	for meas_val in answer["measurements"]:
		if not _record(meas_val, [
			"center_session_id",
			"measurement_id",
			"reference_time_ns",
			"support_exposure_span_ns",
			"status",
			"position_enu_m",
			"position_std_enu_m",
			"supporting_ray_event_ids",
			"supporting_station_ids",
			"minimum_intersection_angle_deg",
			"geometry_condition",
			"reason_codes",
		]):
			return false
		var m_item: Dictionary = meas_val
		if not _string_value(m_item["center_session_id"]) or not _text(m_item["measurement_id"]) \
				or seen_meas.has(m_item["measurement_id"]) \
				or not _integers(m_item, ["reference_time_ns", "support_exposure_span_ns"], true) \
				or not m_item["status"] in VALID_FUSION_STATUS \
				or not _optional_vector(m_item["position_enu_m"]) \
				or not _optional_vector(m_item["position_std_enu_m"], true) \
				or not m_item["supporting_ray_event_ids"] is Array \
				or not _strings(m_item["supporting_station_ids"], false) \
				or not _optional_number(m_item["minimum_intersection_angle_deg"]) \
				or not _optional_number(m_item["geometry_condition"]) \
				or not _strings(m_item["reason_codes"], true):
			return false
		if m_item["status"] != "located" and (m_item["position_enu_m"] != null or m_item["position_std_enu_m"] != null):
			return false
		for eid in m_item["supporting_ray_event_ids"]:
			if not _protocol_event_id(eid):
				return false
		seen_meas[m_item["measurement_id"]] = true
	for track_val in answer["tracks"]:
		if not _record(track_val, [
			"center_session_id",
			"track_id",
			"event_time_ns",
			"lifecycle",
			"position_enu_m",
			"velocity_enu_mps",
			"position_std_enu_m",
			"velocity_std_enu_mps",
			"source_measurement_ids",
			"reason_codes",
		]):
			return false
		var tr: Dictionary = track_val
		if not _string_value(tr["center_session_id"]) or not _text(tr["track_id"]) \
				or not _integers(tr, ["event_time_ns"], true) \
				or not tr["lifecycle"] in VALID_TRACK_LIFECYCLE \
				or not _optional_vector(tr["position_enu_m"]) \
				or not _optional_vector(tr["velocity_enu_mps"]) \
				or not _optional_vector(tr["position_std_enu_m"], true) \
				or not _optional_vector(tr["velocity_std_enu_mps"], true) \
				or not _strings(tr["source_measurement_ids"], false) \
				or not _strings(tr["reason_codes"], true):
			return false
	return true


func _validate_score_overlay(score_val: Variant) -> bool:
	if not _record(score_val, ["events"]) or not score_val["events"] is Array or score_val["events"].is_empty():
		return false
	for ev_val in score_val["events"]:
		if not _record(ev_val, [
			"run_id",
			"experiment_time_ns",
			"matched_count",
			"unmatched_count",
			"unevaluable_count",
			"mean_error",
			"rmse",
			"max_error",
			"error_unit",
			"extra_metrics",
		]):
			return false
		var ev: Dictionary = ev_val
		if not _string_value(ev["run_id"]) \
				or not _integers(ev, ["experiment_time_ns"], true) \
				or not _uint64_text(ev["matched_count"]) \
				or not _uint64_text(ev["unmatched_count"]) \
				or not _uint64_text(ev["unevaluable_count"]) \
				or not _optional_number(ev["mean_error"]) \
				or not _optional_number(ev["rmse"]) \
				or not _optional_number(ev["max_error"]) \
				or not ev["error_unit"] in VALID_ERROR_UNITS \
				or not ev["extra_metrics"] is Array:
			return false
		for metric_val in ev["extra_metrics"]:
			if not _record(metric_val, ["name", "value", "unit"]):
				return false
			var metric: Dictionary = metric_val
			if not _text(metric["name"]) or not _optional_number(metric["value"]) or not _string_value(metric["unit"]):
				return false
	return true


func _validate_sparse_voxel_grid(grid_val: Variant) -> bool:
	if not _record(grid_val, [
		"center_session_id",
		"world_frame_id",
		"window_start_ns",
		"window_end_ns",
		"min_enu_m",
		"step_enu_m",
		"max_score",
		"cells",
	]):
		return false
	var g: Dictionary = grid_val
	if not _string_value(g["center_session_id"]) or not _string_value(g["world_frame_id"]) \
			or not _integers(g, ["window_start_ns", "window_end_ns"], true) \
			or not _vector(g["min_enu_m"]) or not _vector(g["step_enu_m"]) \
			or not _json_int(g["max_score"], true) or not g["cells"] is Array:
		return false
	for axis in range(3):
		if float(g["step_enu_m"][axis]) <= 0.0:
			return false
	var max_score := int(g["max_score"])
	var seen := {}
	for cell_val in g["cells"]:
		if not _record(cell_val, ["ix", "iy", "iz", "station_votes"]):
			return false
		var c: Dictionary = cell_val
		if not _json_int(c["ix"]) or not _json_int(c["iy"]) or not _json_int(c["iz"]) \
				or not _json_int(c["station_votes"], true):
			return false
		var votes := int(c["station_votes"])
		if votes > max_score:
			return false
		c["ix"] = int(c["ix"])
		c["iy"] = int(c["iy"])
		c["iz"] = int(c["iz"])
		c["station_votes"] = votes
		var key := "%d|%d|%d" % [c["ix"], c["iy"], c["iz"]]
		if seen.has(key):
			return false
		seen[key] = true
	g["max_score"] = max_score
	return true

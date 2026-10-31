extends RefCounted

## Validated display copies only. A new worker owns fresh display and diagnostic state per run.
const Evaluation = preload("res://evaluation.gd")
var evaluation := Evaluation.new()
var last_update: Dictionary = {}
var truth_by_time: Dictionary = {}
var rays: Dictionary = {}
var measurements: Dictionary = {}
var tracks: Dictionary = {}
var track_history: Dictionary = {}
var station_states: Dictionary = {}
var edge_diagnostics: Array = []
var voxel_space: Variant = null
var voxel_update_id := 0
var selected_measurement_id := ""
var _voxel_dirty := true
var update_count := 0
var error := ""
var _last_snapshots: Array = []

const RAY_LIFETIME_NS := 1_000_000_000
const MEASUREMENT_LIFETIME_NS := 3_000_000_000
const DELETED_TRACK_LIFETIME_NS := 3_000_000_000
const HISTORY_LIMIT := 100
const MIN_TIME_NS := -9223372036854775807 - 1
const TRUTH_COLOR := Color("#52e888")
const RAY_COLOR := Color("#ffe052")
const MEASUREMENT_COLOR := Color("#ff424f")
const SELECTION_COLOR := Color("#66ddff")
const VOXEL_COLOR := Color("#80b8ff")
const VOXEL_MAX_ALPHA := 0.3
const TRACK_COLORS := {
	"tentative": Color("#ff9e9e"), "confirmed": Color("#db2638"),
	"coasting": Color("#ff9d32"), "deleted": Color("#929292"),
}
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
var display_time_ns := MIN_TIME_NS
var layers: Dictionary = {}
var _root: Node3D
var _camera: Camera3D
var _enu: Callable
var _station_origins: Dictionary = {}
var _ray_materials: Dictionary = {}
var _selected_ray_materials: Dictionary = {}


func receive(state: Dictionary) -> bool:
	error = ""
	if not state.has("algorithm_output") and not state.has("truth_snapshots"):
		return true
	# Validate a private copy before changing any accepted data, including on duplicates.
	var update = state.get("algorithm_output")
	var snapshots = state.get("truth_snapshots")
	if not update is Dictionary or not snapshots is Array:
		return _reject("algorithm_output and truth_snapshots must be supplied together")
	update = update.duplicate(true)
	snapshots = snapshots.duplicate(true)
	if not _update(update):
		return _reject("invalid algorithm_output")
	var times := _truth_times(update)
	if snapshots.size() != times.size():
		return _reject("truth snapshot count does not match reference times")
	for index in range(times.size()):
		if not _truth(snapshots[index], times[index]):
			return _reject("invalid truth snapshot at index %d" % index)
	if not last_update.is_empty():
		if update.update_id < last_update.update_id:
			return _reject("update_id regressed within this worker session")
		if update.update_id == last_update.update_id:
			if update != last_update or snapshots != _last_snapshots:
				return _reject("conflicting duplicate update_id")
			return true
	var truth := {}
	for index in range(times.size()):
		truth[times[index]] = snapshots[index]
	# Evaluate the complete validated increment exactly once, before changing display caches.
	evaluation.process(update, truth)
	last_update = update
	display_time_ns = maxi(display_time_ns, update.scenario_time_ns)
	_last_snapshots = snapshots
	truth_by_time = truth
	edge_diagnostics.clear()
	if update.edge_events != null:
		for station in update.edge_events:
			for ray in station.rays:
				rays[JSON.stringify(ray.event_id)] = ray
			edge_diagnostics.append({"station_id": station.station_id,
				"station_session_id": station.station_session_id, "diagnostics": station.diagnostics})
	if update.center_events != null:
		# No sealed window means no new snapshot. A sealed window without an
		# export (or with an empty grid) replaces the old one rather than mixing it.
		if update.center_events.window_count > 0 or update.center_events.voxel_space != null:
			voxel_space = update.center_events.voxel_space
			voxel_update_id = update.update_id
			_voxel_dirty = true
		for measurement in update.center_events.measurements:
			measurements[measurement.measurement_id] = measurement
		for track in update.center_events.tracks:
			if not tracks.has(track.track_id) or track.event_time_ns >= tracks[track.track_id].event_time_ns:
				tracks[track.track_id] = track
			if track.recent_position_enu_m != null:
				_append_history(track)
	if update.station_states != null:
		for station in update.station_states:
			station_states[station.station_id] = station
	_prune()
	update_count += 1
	redraw()
	return true


func _append_history(track: Dictionary) -> void:
	if not track_history.has(track.track_id):
		track_history[track.track_id] = []
	var history: Array = track_history[track.track_id]
	var point := {"time_ns": track.recent_time_ns, "position_enu_m": track.recent_position_enu_m}
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


func attach(parent: Node3D, camera: Camera3D, enu: Callable) -> void:
	_camera = camera
	_enu = enu
	_root = Node3D.new()
	_root.name = "AlgorithmVisualization"
	parent.add_child(_root)
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
	for key in layers:
		if key == "voxels" and not _voxel_dirty:
			continue
		for child in layers[key].get_children():
			child.free()
	if not last_update.is_empty():
		for target in truth_by_time[last_update.scenario_time_ns].targets:
			_draw_truth(target)
	for ray in rays.values():
		_draw_ray(ray)
	for measurement in measurements.values():
		if measurement.position_enu_m != null:
			var anchor := _anchor(layers.measurements, measurement.measurement_id, measurement.position_enu_m)
			var color := SELECTION_COLOR if measurement.measurement_id == selected_measurement_id else MEASUREMENT_COLOR
			_marker(anchor, color)
			_uncertainty(anchor, measurement.position_std_enu_m, color)
	for track in tracks.values():
		_draw_track(track)
	if _voxel_dirty:
		_draw_voxels()
		_voxel_dirty = false


func select_measurement(id: String) -> bool:
	if id != "" and id != selected_measurement_id and not measurements.has(id):
		return false
	if selected_measurement_id != id:
		selected_measurement_id = id
		redraw()
	return true


func selection_summary() -> Dictionary:
	var result := {"measurement_id": selected_measurement_id, "expired": false,
		"available_ray_ids": [], "missing_ray_ids": []}
	if selected_measurement_id == "":
		return result
	if not measurements.has(selected_measurement_id):
		result.expired = true
		return result
	for id in measurements[selected_measurement_id].supporting_ray_ids:
		if rays.has(JSON.stringify(id)):
			result.available_ray_ids.append(id)
		else:
			result.missing_ray_ids.append(id)
	return result


func _draw_voxels() -> void:
	if voxel_space == null or voxel_space.scored_voxels.is_empty():
		return
	# Batch line segments by score, not one node/material per voxel.
	var groups := {}
	for voxel in voxel_space.scored_voxels:
		if not groups.has(voxel.score):
			groups[voxel.score] = []
		var lower: Array = []
		var upper: Array = []
		for axis in range(3):
			lower.append(voxel_space.search_box_min_enu_m[axis] + voxel.grid_index[axis] * voxel_space.voxel_step_enu_m[axis])
			upper.append(minf(voxel_space.search_box_max_enu_m[axis], lower[axis] + voxel_space.voxel_step_enu_m[axis]))
		var corners: Array[Vector3] = []
		for index in range(8):
			corners.append(_enu.call([upper[0] if (index & 1) != 0 else lower[0],
				upper[1] if (index & 2) != 0 else lower[1], upper[2] if (index & 4) != 0 else lower[2]]))
		for index in range(8):
			for bit in [1, 2, 4]:
				if (index & bit) == 0:
					groups[voxel.score].append(corners[index])
					groups[voxel.score].append(corners[index | bit])
	for score in groups:
		var points: Array[Vector3] = []
		points.assign(groups[score])
		_mesh(layers.voxels, "Score%d" % score, points,
			Color(VOXEL_COLOR, VOXEL_MAX_ALPHA * float(score) / float(voxel_space.max_score)))


func _anchor(parent: Node3D, id: String, position: Array) -> Node3D:
	var anchor := Node3D.new()
	# IDs are data, not NodePaths; do not use them as node names.
	anchor.set_meta("id", id)
	anchor.position = _enu.call(position)
	parent.add_child(anchor)
	return anchor


func _mesh(parent: Node3D, name: String, points: Array[Vector3], color: Color,
		primitive := Mesh.PRIMITIVE_LINES, material: Material = null) -> MeshInstance3D:
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
	_mesh(parent, "Position", [Vector3(-0.3, 0, 0), Vector3(0.3, 0, 0),
		Vector3(0, -0.3, 0), Vector3(0, 0.3, 0), Vector3(0, 0, -0.3), Vector3(0, 0, 0.3)], color)


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
	_mesh(parent, "Velocity", [Vector3.ZERO, direction, direction, direction - unit * head + side * head * 0.5,
		direction, direction - unit * head - side * head * 0.5], color)


func _draw_truth(target: Dictionary) -> void:
	var anchor := _anchor(layers.truth, target.target_id, target.position_enu_m)
	_circle(anchor, target.bounding_radius_m, TRUTH_COLOR)
	# One second of displacement, in metres; this is a velocity glyph, not a prediction.
	_arrow(anchor, _enu.call(target.velocity_enu_mps), TRUTH_COLOR)
	var links: Array[Vector3] = []
	for visibility in target.visibility_by_station:
		if visibility.in_field_of_view and _station_origins.has(visibility.station_id):
			links.append(Vector3.ZERO)
			links.append(_station_origins[visibility.station_id] - anchor.position)
	if not links.is_empty():
		_mesh(anchor, "StationLinks", links, TRUTH_COLOR)


func _draw_ray(ray: Dictionary) -> void:
	var anchor := _anchor(layers.rays, JSON.stringify(ray.event_id), ray.origin_enu_m)
	var direction: Vector3 = _enu.call(ray.direction_enu_unit)
	var end := direction * _camera.far
	var selected := false
	if measurements.has(selected_measurement_id):
		selected = ray.event_id in measurements[selected_measurement_id].supporting_ray_ids
	var color := SELECTION_COLOR if selected else RAY_COLOR
	_mesh(anchor, "Ray", [Vector3.ZERO, end], color, Mesh.PRIMITIVE_LINES,
		_selected_ray_materials[ray.calibration_usage] if selected else _ray_materials[ray.calibration_usage])
	if ray.angular_std_rad == null or ray.angular_std_rad <= 0.0 or ray.angular_std_rad >= PI * 0.5:
		return
	var side := direction.cross(Vector3.UP)
	if side.length_squared() < 0.001:
		side = direction.cross(Vector3.RIGHT)
	side = side.normalized()
	var up := direction.cross(side).normalized()
	var radius := _camera.far * tan(ray.angular_std_rad)
	var points: Array[Vector3] = []
	for index in range(32):
		for vertex in [Vector3.ZERO,
				end + radius * (side * cos(TAU * index / 32.0) + up * sin(TAU * index / 32.0)),
				end + radius * (side * cos(TAU * (index + 1) / 32.0) + up * sin(TAU * (index + 1) / 32.0))]:
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


func _number(value: Variant) -> bool:
	return (value is float or value is int) and is_finite(float(value))


func _optional_number(value: Variant) -> bool:
	return value == null or _number(value)


func _nonnegative(value: Variant) -> bool:
	return _number(value) and value >= 0


func _vector(value: Variant, nonnegative := false) -> bool:
	if not value is Array or value.size() != 3:
		return false
	for component in value:
		if not _number(component) or (nonnegative and component < 0):
			return false
	return true


func _optional_vector(value: Variant, nonnegative := false) -> bool:
	return value == null or _vector(value, nonnegative)


func _rotation(value: Variant) -> bool:
	return value is Array and value.size() == 3 and _vector(value[0]) \
		and _vector(value[1]) and _vector(value[2])


func _texts(value: Variant) -> bool:
	if not value is Array:
		return false
	for item in value:
		if not _text(item):
			return false
	return true


func _integer(value: Variant, signed := false) -> bool:
	# JSON numbers are doubles in Godot. Python sends contract integers as decimal strings.
	return value is String and value.is_valid_int() and str(value.to_int()) == value \
		and (signed or value.to_int() >= 0)


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


func _ray(ray: Variant, station: Dictionary) -> bool:
	if not _record(ray, ["event_id", "capture_time_ns", "origin_enu_m", "direction_enu_unit",
			"calibration_usage", "detection_score", "angular_std_rad"]):
		return false
	if not _event_id(ray.event_id) or ray.event_id[0] != station.station_id \
			or ray.event_id[1] != station.station_session_id:
		return false
	if not _integers(ray, ["capture_time_ns"], true) or not _vector(ray.origin_enu_m) \
			or not _vector(ray.direction_enu_unit):
		return false
	var direction: Array = ray.direction_enu_unit
	return is_equal_approx(Vector3(direction[0], direction[1], direction[2]).length_squared(), 1.0) \
		and ray.calibration_usage in ["valid", "frozen", "stale"] \
		and _nonnegative(ray.detection_score) and ray.detection_score <= 1 \
		and (ray.angular_std_rad == null or _nonnegative(ray.angular_std_rad))


func _edge(station: Variant) -> bool:
	if not _record(station, ["station_id", "station_session_id", "rays", "diagnostics"]) \
			or not _text(station.station_id) or not _text(station.station_session_id) \
			or not station.rays is Array or not station.diagnostics is Array:
		return false
	var ids := {}
	for ray in station.rays:
		if not _ray(ray, station):
			return false
		var key := JSON.stringify(ray.event_id)
		if ids.has(key):
			return false
		ids[key] = true
	for diagnostic in station.diagnostics:
		if not _record(diagnostic, ["capture_time_ns", "category", "reason_codes"]) \
				or not _integers(diagnostic, ["capture_time_ns"], true) \
				or not _text(diagnostic.category) or not _texts(diagnostic.reason_codes):
			return false
	return true


func _measurement(measurement: Variant) -> bool:
	if not _record(measurement, ["measurement_id", "reference_time_ns", "status", "position_enu_m",
			"position_std_enu_m", "supporting_ray_ids", "supporting_station_ids",
			"minimum_intersection_angle_deg", "geometry_condition"]):
		return false
	if not _text(measurement.measurement_id) or not _integers(measurement, ["reference_time_ns"], true) \
			or not measurement.status in ["located", "geometry_degenerate", "association_ambiguous",
				"insufficient_stations", "no_candidate_in_volume", "rejected_input"]:
		return false
	if not _optional_vector(measurement.position_enu_m) \
			or not _optional_vector(measurement.position_std_enu_m, true) \
			or not _texts(measurement.supporting_station_ids) or not measurement.supporting_ray_ids is Array \
			or not _optional_number(measurement.minimum_intersection_angle_deg) \
			or not _optional_number(measurement.geometry_condition):
		return false
	if measurement.status != "located" and (measurement.position_enu_m != null or measurement.position_std_enu_m != null):
		return false
	for event_id in measurement.supporting_ray_ids:
		if not _event_id(event_id):
			return false
	return true


func _track(track: Variant) -> bool:
	if not _record(track, ["track_id", "event_time_ns", "lifecycle", "position_enu_m", "velocity_enu_mps",
			"position_std_enu_m", "velocity_std_enu_mps", "recent_position_enu_m", "recent_time_ns"]):
		return false
	if not _text(track.track_id) or not _integers(track, ["event_time_ns"], true) \
			or not track.lifecycle in ["tentative", "confirmed", "coasting", "deleted"]:
		return false
	if not _optional_vector(track.position_enu_m) or not _optional_vector(track.velocity_enu_mps) \
			or not _optional_vector(track.position_std_enu_m, true) \
			or not _optional_vector(track.velocity_std_enu_mps, true) \
			or not _optional_vector(track.recent_position_enu_m):
		return false
	if (track.recent_position_enu_m == null) != (track.recent_time_ns == null):
		return false
	return track.recent_time_ns == null or _integers(track, ["recent_time_ns"], true)


func _voxels(voxels: Variant) -> bool:
	if voxels == null:
		return true
	if not _record(voxels, ["search_box_min_enu_m", "search_box_max_enu_m", "voxel_step_enu_m", "scored_voxels", "max_score"]) \
			or not _vector(voxels.search_box_min_enu_m) or not _vector(voxels.search_box_max_enu_m) \
			or not _vector(voxels.voxel_step_enu_m) or not _integers(voxels, ["max_score"]) \
			or not voxels.scored_voxels is Array:
		return false
	for axis in range(3):
		if voxels.voxel_step_enu_m[axis] <= 0 or voxels.search_box_min_enu_m[axis] >= voxels.search_box_max_enu_m[axis]:
			return false
	var indices := {}
	for voxel in voxels.scored_voxels:
		if not _record(voxel, ["grid_index", "score", "supporting_station_count"]) \
				or not _integers(voxel, ["score", "supporting_station_count"]) \
				or voxel.score <= 0 or voxel.score > voxels.max_score \
				or not voxel.grid_index is Array or voxel.grid_index.size() != 3:
			return false
		for axis in range(3):
			if not _integer(voxel.grid_index[axis]):
				return false
			voxel.grid_index[axis] = voxel.grid_index[axis].to_int()
			var position: float = voxels.search_box_min_enu_m[axis] + voxel.grid_index[axis] * voxels.voxel_step_enu_m[axis]
			if not is_finite(position) or position >= voxels.search_box_max_enu_m[axis]:
				return false
		var key := JSON.stringify(voxel.grid_index)
		if indices.has(key):
			return false
		indices[key] = true
	return true


func _center(center: Variant) -> bool:
	if not _record(center, ["measurements", "tracks", "window_count", "candidate_count", "association_count", "voxel_space"]) \
			or not center.measurements is Array or not center.tracks is Array \
			or not _integers(center, ["window_count", "candidate_count", "association_count"]) \
			or not _voxels(center.voxel_space):
		return false
	var ids := {}
	for measurement in center.measurements:
		if not _measurement(measurement) or ids.has(measurement.measurement_id):
			return false
		ids[measurement.measurement_id] = true
	for track in center.tracks:
		if not _track(track):
			return false
	return true


func _station(station: Variant) -> bool:
	if not _record(station, ["station_id", "configured_orientation_enu_from_main", "actual_orientation_enu_from_main",
			"calibrated_orientation_enu_from_main", "calibration_usage", "truth_orientation_error_rad", "calibration_orientation_error_rad"]):
		return false
	return _text(station.station_id) and _rotation(station.configured_orientation_enu_from_main) \
		and (station.actual_orientation_enu_from_main == null or _rotation(station.actual_orientation_enu_from_main)) \
		and (station.calibrated_orientation_enu_from_main == null or _rotation(station.calibrated_orientation_enu_from_main)) \
		and station.calibration_usage in [null, "valid", "frozen", "stale"] \
		and _optional_vector(station.truth_orientation_error_rad) and _optional_vector(station.calibration_orientation_error_rad)


func _update(update: Dictionary) -> bool:
	if not _record(update, ["update_id", "scenario_time_ns", "step_no", "edge_events", "center_events", "station_states"]) \
			or not _integers(update, ["update_id", "step_no"]) or not _integers(update, ["scenario_time_ns"], true):
		return false
	if update.edge_events != null:
		if not update.edge_events is Array:
			return false
		var stations := {}
		for station in update.edge_events:
			if not _edge(station):
				return false
			var key := JSON.stringify([station.station_id, station.station_session_id])
			if stations.has(key):
				return false
			stations[key] = true
	if update.center_events != null and not _center(update.center_events):
		return false
	if update.station_states != null:
		if not update.station_states is Array:
			return false
		var stations := {}
		for station in update.station_states:
			if not _station(station) or stations.has(station.station_id):
				return false
			stations[station.station_id] = true
	return true


func _truth_times(update: Dictionary) -> Array:
	var times := {update.scenario_time_ns: true}
	if update.edge_events != null:
		for station in update.edge_events:
			for ray in station.rays:
				times[ray.capture_time_ns] = true
	if update.center_events != null:
		for measurement in update.center_events.measurements:
			times[measurement.reference_time_ns] = true
	var ordered := times.keys()
	ordered.sort()
	return ordered


func _truth(snapshot: Variant, time_ns: int) -> bool:
	if not _record(snapshot, ["targets"]) or not snapshot.targets is Array:
		return false
	var ids := {}
	for target in snapshot.targets:
		if not _record(target, ["target_id", "time_ns", "position_enu_m", "velocity_enu_mps",
				"geometry_type", "bounding_radius_m", "visibility_by_station"]) \
				or not _text(target.target_id) or ids.has(target.target_id) \
				or not _integers(target, ["time_ns"], true) or target.time_ns != time_ns \
				or not _vector(target.position_enu_m) or not _vector(target.velocity_enu_mps) \
				or not target.geometry_type in ["sphere", "box", "aircraft"] \
				or not _nonnegative(target.bounding_radius_m) or not target.visibility_by_station is Array:
			return false
		ids[target.target_id] = true
		var stations := {}
		for visibility in target.visibility_by_station:
			if not _record(visibility, ["station_id", "in_field_of_view", "capture_eligible"]) \
					or not _text(visibility.station_id) or stations.has(visibility.station_id) \
					or not visibility.in_field_of_view is bool or not visibility.capture_eligible is bool:
				return false
			stations[visibility.station_id] = true
	return true

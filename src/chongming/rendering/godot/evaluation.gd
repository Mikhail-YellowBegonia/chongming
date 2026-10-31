extends RefCounted

## Error diagnostics over validated increments, independent of display nodes and caches.
const ANGLE_GATE_RAD := 0.01
const DISTANCE_GATE_M := 10.0
var _edge := _statistics()
var _center := _statistics()
var last_matches := {"edge": [], "center": []}


func _statistics() -> Dictionary:
	return {"outputs": 0, "valid": 0, "matched": 0, "unmatched": 0, "unevaluable": 0,
		"error_sum": 0.0, "squared_error_sum": 0.0, "maximum": null}


func process(update: Dictionary, truth_by_time: Dictionary) -> void:
	last_matches = {"edge": [], "center": []}
	var groups := {}
	if update.edge_events != null:
		for station in update.edge_events:
			for ray in station.rays:
				var key := JSON.stringify([station.station_id, ray.capture_time_ns])
				if not groups.has(key):
					groups[key] = {"time_ns": ray.capture_time_ns, "outputs": []}
				groups[key].outputs.append(ray)
	var keys := groups.keys()
	keys.sort()
	for key in keys:
		_evaluate(groups[key].outputs, truth_by_time[groups[key].time_ns].targets, true)
	groups = {}
	if update.center_events != null:
		for measurement in update.center_events.measurements:
			if not groups.has(measurement.reference_time_ns):
				groups[measurement.reference_time_ns] = []
			groups[measurement.reference_time_ns].append(measurement)
	keys = groups.keys()
	keys.sort()
	for time_ns in keys:
		_evaluate(groups[time_ns], truth_by_time[time_ns].targets, false)


func _evaluate(outputs: Array, targets: Array, edge: bool) -> void:
	# Stable identity ordering only breaks geometric ties; IDs never define correspondences.
	outputs = outputs.duplicate()
	targets = targets.duplicate()
	outputs.sort_custom(func(a: Dictionary, b: Dictionary) -> bool: return _id(a, edge) < _id(b, edge))
	targets.sort_custom(func(a: Dictionary, b: Dictionary) -> bool: return a.target_id < b.target_id)
	var stats: Dictionary = _edge if edge else _center
	var costs := []
	var evaluable := []
	for output in outputs:
		stats.outputs += 1
		if not edge and (output.status != "located" or output.position_enu_m == null):
			stats.unevaluable += 1
			continue
		var row := []
		var defined := targets.is_empty()
		for target in targets:
			var error = _pointing_error(output, target) if edge else _position_error(output, target)
			if error != null:
				defined = true
			row.append(error if error != null and error <= (ANGLE_GATE_RAD if edge else DISTANCE_GATE_M) else INF)
		if not defined:
			stats.unevaluable += 1
			continue
		stats.valid += 1
		costs.append(row)
		evaluable.append(output)
	var assignment := _assignment(costs, targets.size())
	for row in range(assignment.size()):
		var column: int = assignment[row]
		if column < 0:
			stats.unmatched += 1
			continue
		var error: float = costs[row][column]
		stats.matched += 1
		stats.error_sum += error
		stats.squared_error_sum += error * error
		stats.maximum = error if stats.maximum == null else maxf(stats.maximum, error)
		var output: Dictionary = evaluable[row]
		last_matches["edge" if edge else "center"].append({
			"output_id": output.event_id if edge else output.measurement_id,
			"target_id": targets[column].target_id, "error": error,
			"time_ns": output.capture_time_ns if edge else output.reference_time_ns})


func _id(output: Dictionary, edge: bool) -> String:
	return JSON.stringify(output.event_id) if edge else output.measurement_id


func _length(x: float, y: float, z: float) -> float:
	var scale := maxf(absf(x), maxf(absf(y), absf(z)))
	if scale == 0.0 or not is_finite(scale):
		return scale
	return scale * sqrt((x / scale) ** 2 + (y / scale) ** 2 + (z / scale) ** 2)


func _pointing_error(ray: Dictionary, target: Dictionary) -> Variant:
	# Scalar floats remain double precision; Vector3 storage would round large ENU coordinates.
	var x: float = target.position_enu_m[0] - ray.origin_enu_m[0]
	var y: float = target.position_enu_m[1] - ray.origin_enu_m[1]
	var z: float = target.position_enu_m[2] - ray.origin_enu_m[2]
	var length := _length(x, y, z)
	if length == 0.0 or not is_finite(length):
		return null
	x /= length
	y /= length
	z /= length
	var direction: Array = ray.direction_enu_unit
	# atan2 preserves tiny nonzero angles that acos of a rounded dot product would lose.
	return atan2(_length(direction[1] * z - direction[2] * y,
		direction[2] * x - direction[0] * z, direction[0] * y - direction[1] * x),
		direction[0] * x + direction[1] * y + direction[2] * z)


func _position_error(measurement: Dictionary, target: Dictionary) -> Variant:
	var error := _length(measurement.position_enu_m[0] - target.position_enu_m[0],
		measurement.position_enu_m[1] - target.position_enu_m[1],
		measurement.position_enu_m[2] - target.position_enu_m[2])
	return error if is_finite(error) else null


func _assignment(costs: Array, target_count: int) -> Array:
	# Successive shortest augmenting paths in the bipartite residual graph.
	# Continue until maximum cardinality, minimizing total error at each cardinality.
	# Reverse matched edges have negative cost, so use Bellman-Ford, not greedy nearest neighbours.
	var rows := []
	rows.resize(costs.size())
	rows.fill(-1)
	var columns := []
	columns.resize(target_count)
	columns.fill(-1)
	for _augmentation in range(mini(costs.size(), target_count)):
		var row_distance := []
		row_distance.resize(costs.size())
		var column_distance := []
		column_distance.resize(target_count)
		column_distance.fill(INF)
		var previous_row := []
		previous_row.resize(target_count)
		previous_row.fill(-1)
		for row in range(rows.size()):
			row_distance[row] = 0.0 if rows[row] < 0 else INF
		for _pass in range(rows.size() + columns.size()):
			var changed := false
			for row in range(rows.size()):
				for column in range(columns.size()):
					if rows[row] != column and row_distance[row] + costs[row][column] < column_distance[column]:
						column_distance[column] = row_distance[row] + costs[row][column]
						previous_row[column] = row
						changed = true
			for column in range(columns.size()):
				var row: int = columns[column]
				if row >= 0 and column_distance[column] - costs[row][column] < row_distance[row]:
					row_distance[row] = column_distance[column] - costs[row][column]
					changed = true
			if not changed:
				break
		var end := -1
		for column in range(columns.size()):
			if columns[column] < 0 and is_finite(column_distance[column]) \
					and (end < 0 or column_distance[column] < column_distance[end]):
				end = column
		if end < 0:
			break
		while end >= 0:
			var row: int = previous_row[end]
			var old_column: int = rows[row]
			rows[row] = end
			columns[end] = row
			end = old_column
	return rows


func _summary(stats: Dictionary) -> Dictionary:
	var count: int = stats.matched
	return {"outputs": stats.outputs, "valid": stats.valid, "matched": count,
		"unmatched": stats.unmatched, "unevaluable": stats.unevaluable, "sample_count": count,
		"mean_error": null if count == 0 else stats.error_sum / count,
		"rmse": null if count == 0 else sqrt(stats.squared_error_sum / count),
		"max_error": stats.maximum}


func summary() -> Dictionary:
	return {"angle_gate_rad": ANGLE_GATE_RAD, "distance_gate_m": DISTANCE_GATE_M,
		"edge": _summary(_edge), "center": _summary(_center)}

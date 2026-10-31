extends RefCounted

## GPU Instancing for vegetation: batch rendering by variant to reduce draw calls.
##
## Instead of creating 3796 individual Node3D instances, group vegetation by
## variant (tree species, shrub type) and render each group as one MultiMesh.
## This reduces draw calls from ~3796 to ~20 while preserving wind animation
## and deterministic variant selection.

const Vegetation = preload("res://vegetation.gd")
const Assets = preload("res://vegetation_assets.gd")

## Batch state for one vegetation variant (e.g., oak tree trunk, oak tree crown, shrub type 0)
class VegetationBatch:
	var variant_key: String  # Unique key: "tree_0", "tree_0_crown", "shrub_2"
	var multimesh: MultiMesh
	var multimesh_instance: MultiMeshInstance3D
	var instances: Array[Dictionary] = []  # [{entity, visual, variant_key, height, radius, seed, ...}]
	var mesh: Mesh
	var lod_far: float = 750.0
	var is_crown: bool = false  # True for tree crown batches
	var native_height: float = 1.0  # Native mesh height for scaling

	func _init(key: String, instance_mesh: Mesh) -> void:
		variant_key = key
		mesh = instance_mesh
		multimesh = MultiMesh.new()
		multimesh.transform_format = MultiMesh.TRANSFORM_3D
		multimesh.mesh = mesh
		multimesh.instance_count = 0
		multimesh_instance = MultiMeshInstance3D.new()
		multimesh_instance.multimesh = multimesh
		multimesh_instance.name = "VegetationBatch_" + key
		multimesh_instance.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_ON

## Main batching manager
var batches: Dictionary = {}  # variant_key -> VegetationBatch
var _world: Node3D
var _wind_time := 0.0
var _wind_mean := Vector2.ZERO
var _wind_turbulence := 0.0

func initialize(world: Node3D) -> void:
	_world = world

func clear() -> void:
	for batch_value in batches.values():
		var batch: VegetationBatch = batch_value
		if batch.multimesh_instance.get_parent() != null:
			batch.multimesh_instance.queue_free()
	batches.clear()

## Group entities by vegetation variant
func group_vegetation_entities(entities: Array) -> Dictionary:
	var grouped: Dictionary = {}  # variant_key -> Array[entity_data]

	for entity_dict in entities:
		var entity: Dictionary = entity_dict
		var category := str(entity.get("category", ""))
		if category != "vegetation":
			continue

		var visual: Variant = entity.get("visual", null)
		if not visual is Dictionary:
			continue

		var visual_dict: Dictionary = visual
		var kind := str(visual_dict.get("kind", ""))
		var seed := int(visual_dict.get("seed", 0))
		var position: Array = entity.get("position", [0.0, 0.0, 0.0])
		var world_pos := Vector2(float(position[0]), float(position[1]))

		var variant_key := ""
		var height := 1.0
		var radius := 1.0

		if kind == "tree_trunk":
			# Determine tree variant index
			height = float(visual_dict.get("height", 10.0))
			radius = float(visual_dict.get("radius", 2.0))
			var variant_index := Vegetation._variant_index(
				world_pos, seed, Assets.tree_count(), 55.0, 11
			)
			variant_key = "tree_%d" % variant_index
		elif kind == "shrub":
			height = float(visual_dict.get("height", 1.0))
			radius = float(visual_dict.get("radius", 1.0))
			var shrub_weights := [3, 3, 3, 3, 3, 1]
			var variant_index := Vegetation._variant_index_weighted(
				world_pos, seed, shrub_weights, 38.0, 23
			)
			variant_key = "shrub_%d" % variant_index
		elif kind == "grass":
			# Grass already uses MultiMesh internally, skip batching
			continue
		elif kind == "tree_canopy":
			# Canopies are paired with trunks, skip separate batching
			continue
		else:
			continue

		if not grouped.has(variant_key):
			grouped[variant_key] = []

		grouped[variant_key].append({
			"entity": entity,
			"visual": visual_dict,
			"variant_key": variant_key,
			"height": height,
			"radius": radius,
			"seed": seed,
			"world_position": world_pos,
		})

	return grouped

## Create or update MultiMesh batches from grouped entities
func update_batches(grouped: Dictionary, wind_time: float, wind_mean: Vector2, wind_turbulence: float) -> void:
	_wind_time = wind_time
	_wind_mean = wind_mean
	_wind_turbulence = wind_turbulence

	# Remove stale batches
	var active_keys: Dictionary = {}
	for key in grouped.keys():
		active_keys[key] = true
		# Tree batches need both trunk and crown keys
		if key.begins_with("tree_"):
			active_keys[key + "_crown"] = true

	var stale_keys: Array[String] = []
	for key in batches.keys():
		if not active_keys.has(key):
			stale_keys.append(str(key))

	for key in stale_keys:
		var batch: VegetationBatch = batches[key]
		if batch.multimesh_instance.get_parent() != null:
			batch.multimesh_instance.get_parent().remove_child(batch.multimesh_instance)
			batch.multimesh_instance.queue_free()
		batches.erase(key)

	# Update or create batches
	for variant_key in grouped.keys():
		var instances_data: Array = grouped[variant_key]
		if instances_data.is_empty():
			continue

		# Create/update trunk batch
		_update_or_create_batch(variant_key, instances_data, false)

		# For trees, also create/update crown batch
		if variant_key.begins_with("tree_"):
			_update_or_create_batch(variant_key + "_crown", instances_data, true)

func _update_or_create_batch(variant_key: String, instances_data: Array, is_crown: bool) -> void:
	var batch: VegetationBatch
	if batches.has(variant_key):
		batch = batches[variant_key]
	else:
		batch = _create_batch(variant_key, instances_data[0], is_crown)
		if batch == null:
			return
		batches[variant_key] = batch
		_world.add_child(batch.multimesh_instance)

	# Update instance count and transforms
	batch.instances = instances_data
	batch.multimesh.instance_count = instances_data.size()

	for i in range(instances_data.size()):
		var data: Dictionary = instances_data[i]
		var transform := _compute_instance_transform(data, is_crown)
		batch.multimesh.set_instance_transform(i, transform)

## Create a new batch for a variant
func _create_batch(variant_key: String, sample_data: Dictionary, is_crown: bool) -> VegetationBatch:
	var visual: Dictionary = sample_data.visual
	var kind := str(visual.get("kind", ""))

	if kind == "tree_trunk":
		return _create_tree_batch(variant_key, sample_data, is_crown)
	elif kind == "shrub":
		return _create_shrub_batch(variant_key, sample_data)

	return null

func _create_tree_batch(variant_key: String, sample_data: Dictionary, is_crown: bool) -> VegetationBatch:
	var base_key := variant_key.replace("_crown", "")
	var variant_index := int(base_key.trim_prefix("tree_"))
	var prototype: Dictionary = Assets.tree_at(variant_index)
	if prototype.is_empty():
		return null

	var visual: Dictionary = sample_data.visual
	var lod_far := float(visual.get("lod_far", 750.0))

	var mesh: Mesh
	if is_crown:
		mesh = prototype.crown
		if mesh == null:
			return null
	else:
		mesh = prototype.trunk

	var batch := VegetationBatch.new(variant_key, mesh)
	batch.lod_far = lod_far
	batch.multimesh_instance.visibility_range_end = lod_far
	batch.is_crown = is_crown
	batch.native_height = maxf(0.1, float(prototype.native_height))

	return batch

func _create_shrub_batch(variant_key: String, sample_data: Dictionary) -> VegetationBatch:
	var variant_index := int(variant_key.trim_prefix("shrub_"))
	var prototype: Dictionary = Assets.shrub_at(variant_index)
	if prototype.is_empty():
		return null

	var visual: Dictionary = sample_data.visual
	var lod_far := float(visual.get("lod_far", 750.0))

	var batch := VegetationBatch.new(variant_key, prototype.mesh)
	batch.lod_far = lod_far
	batch.multimesh_instance.visibility_range_end = lod_far
	batch.native_height = maxf(0.1, float(prototype.native_height))

	return batch

## Compute instance transform with wind animation
func _compute_instance_transform(data: Dictionary, is_crown: bool) -> Transform3D:
	var entity: Dictionary = data.entity
	var position: Array = entity.get("position", [0.0, 0.0, 0.0])
	var rotation: Array = entity.get("rotation", [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0])
	var pos := Vector3(float(position[0]), float(position[1]), float(position[2]))

	# Convert rotation array to Basis
	var basis := Basis(
		Vector3(float(rotation[0]), float(rotation[1]), float(rotation[2])),
		Vector3(float(rotation[3]), float(rotation[4]), float(rotation[5])),
		Vector3(float(rotation[6]), float(rotation[7]), float(rotation[8]))
	)

	# Apply scale and rotation based on vegetation type
	var height := float(data.get("height", 1.0))
	var radius := float(data.get("radius", 1.0))
	var variant_key: String = data.variant_key
	var seed := int(data.get("seed", 0))

	if variant_key.begins_with("tree_"):
		var variant_index := int(variant_key.trim_prefix("tree_"))
		var prototype: Dictionary = Assets.tree_at(variant_index)
		if not prototype.is_empty():
			var native_height := maxf(0.1, float(prototype.native_height))
			var native_radius := maxf(0.05, float(prototype.native_radius))
			var scale_y := height / native_height
			var scale_xz := maxf(radius / native_radius, scale_y * 0.82)

			# Apply wind animation for crown
			if is_crown:
				# Wind animation: use rotation array to get lean direction
				var lean := basis.z - Vector3.UP
				if lean.length() >= 1e-5:
					var offset := lean * native_height * 0.18
					# Crown offset in model space
					var crown_offset := basis.inverse() * offset
					pos += basis * crown_offset

					# Squash effect
					var squash := minf(0.06, lean.length() * 0.30)
					scale_xz *= (1.0 + squash)
					scale_y *= (1.0 - squash * 0.35)

			# Rotate from Y-up to Z-up
			basis = basis * Basis(Vector3.RIGHT, PI * 0.5)
			basis = basis.scaled(Vector3(scale_xz, scale_y, scale_xz))

	elif variant_key.begins_with("shrub_"):
		var variant_index := int(variant_key.trim_prefix("shrub_"))
		var prototype: Dictionary = Assets.shrub_at(variant_index)
		if not prototype.is_empty():
			var native_height := maxf(0.1, float(prototype.native_height))
			var native_radius := maxf(0.05, float(prototype.native_radius))

			# Apply wind animation for shrubs
			var speed := _wind_mean.length()
			if _wind_mean.length() > 0.05:
				var phase := float(posmod(seed, 100003)) / 100003.0 * TAU
				var time := _wind_time
				var wave := (
					sin(time * 1.7 + phase)
					+ 0.33 * sin(time * 3.3 + phase * 1.7)
					+ 0.15 * sin(time * 6.1 + phase * 0.7)
				)
				var turbulence := 1.0 + minf(1.5, _wind_turbulence / maxf(speed, 0.1))
				var amplitude := 0.045 * minf(speed, 10.0) * turbulence
				var direction := _wind_mean.normalized()
				var wind_offset := Vector3(
					direction.x * amplitude * wave,
					direction.y * amplitude * wave,
					0.0
				)
				pos += basis * wind_offset

				# Squash effect
				var squash := 0.025 * minf(1.0, absf(wave)) * minf(speed / 4.0, 1.0)
				var scale_x := (radius / native_radius) * (1.0 + squash)
				var scale_y := (height / native_height) * (1.0 - squash * 0.5)
				var scale_z := (radius / native_radius) * (1.0 + squash)

				# Rotate from Y-up to Z-up
				basis = basis * Basis(Vector3.RIGHT, PI * 0.5)
				basis = basis.scaled(Vector3(scale_x, scale_y, scale_z))
			else:
				# No wind, just scale
				basis = basis * Basis(Vector3.RIGHT, PI * 0.5)
				basis = basis.scaled(Vector3(radius / native_radius, height / native_height, radius / native_radius))

	return Transform3D(basis, pos)

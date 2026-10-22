extends SceneTree

const Vegetation = preload("res://vegetation.gd")
const Worker = preload("res://worker.gd")

class PreviewWorker extends Worker:
	func _ready() -> void:
		pass

var failures: Array[String] = []


func _initialize() -> void:
	_run.call_deferred()


func _run() -> void:
	# Dummy RenderingServer discards MultiMesh buffers; validate the real resource backend.
	DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_MINIMIZED)
	_test_generator()
	_test_aircraft_models()
	_test_worker_cache()
	await _test_freeview_world()
	if failures.is_empty():
		print("vegetation tests passed")
		quit(0)
	else:
		for failure in failures:
			push_error(failure)
		quit(1)


func _test_freeview_world() -> void:
	var worker := PreviewWorker.new()
	root.add_child(worker)
	worker.set_process(false)
	worker._setup_world()
	var box := MeshInstance3D.new()
	box.mesh = BoxMesh.new()
	var material := StandardMaterial3D.new()
	material.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	material.albedo_color = Color.RED
	box.material_override = material
	box.position.y = 1.0
	worker._world.add_child(box)
	worker._free_camera.position = Vector3(0.0, 1.0, 4.0)
	worker._free_camera.look_at(Vector3(0.0, 1.0, 0.0))
	worker._free_camera.make_current()
	worker._free_viewport.size = Vector2i(64, 64)
	await process_frame
	worker._free_viewport.render_target_update_mode = SubViewport.UPDATE_ONCE
	RenderingServer.force_draw(false)
	var image := worker._free_viewport.get_texture().get_image()
	var center := image.get_pixel(32, 32)
	_check(center.r > 0.8 and center.g < 0.1 and center.b < 0.1,
		"freeview must render geometry from the sensor's shared 3D world")
	worker.free()


func _test_generator() -> void:
	var visual := {"kind": "tree_canopy", "height": 7.0, "radius": 2.0, "seed": 12345,
		"lod_near": 120.0, "lod_far": 640.0}
	var first := Vegetation.create(visual, [0.12, 0.35, 0.08], 0.8)
	var replay := Vegetation.create(visual, [0.12, 0.35, 0.08], 0.8)
	var different := Vegetation.create(visual.merged({"seed": 54321}, true), [0.12, 0.35, 0.08], 0.8)
	var first_leaves := first.get_node("Leaves") as MultiMeshInstance3D
	var replay_leaves := replay.get_node("Leaves") as MultiMeshInstance3D
	var different_leaves := different.get_node("Leaves") as MultiMeshInstance3D
	_check(first_leaves.multimesh.instance_count > 1000, "canopy should use dense leaf instances")
	_check(first_leaves.multimesh.mesh == replay_leaves.multimesh.mesh, "trees should share one leaf prototype")
	_check(first_leaves.multimesh.custom_aabb.has_volume(), "foliage should have precomputed bounds")
	_check(first_leaves.visibility_range_end == 120.0, "detailed foliage should end at near LOD")
	var far := first.get_node("FarProxy") as MeshInstance3D
	_check(far.visibility_range_begin == 120.0 and far.visibility_range_end == 640.0,
		"far tree proxy should cover the configured LOD interval")
	_check(far.mesh is ArrayMesh, "far tree proxy should use a clustered custom mesh")
	for index in [0, 73, first_leaves.multimesh.instance_count - 1]:
		var first_transform := first_leaves.multimesh.get_instance_transform(index)
		_check(first_transform == replay_leaves.multimesh.get_instance_transform(index), "same seed must reproduce leaf transforms")
		_check(_finite_transform(first_transform), "leaf transforms must remain finite")
		_check(first_leaves.multimesh.get_instance_color(index) == replay_leaves.multimesh.get_instance_color(index), "same seed must reproduce leaf colors")
		_check(first_leaves.multimesh.custom_aabb.encloses(first_transform * first_leaves.multimesh.mesh.get_aabb()), "bounds must include transformed leaf geometry")
	_check(first_leaves.multimesh.get_instance_transform(73) != different_leaves.multimesh.get_instance_transform(73), "different seeds should vary leaf placement")
	var shrub := Vegetation.create({"kind": "shrub", "height": 2.2, "radius": 2.8,
		"seed": 91, "lod_near": 120.0, "lod_far": 640.0}, [0.1, 0.3, 0.07], 0.8)
	var shrub_leaves := shrub.get_node("Leaves") as MultiMeshInstance3D
	_check(shrub_leaves.multimesh.instance_count == 420, "shrubs should batch deterministic leaves")
	_check(shrub.has_node("FarProxy"), "shrubs should provide a far LOD proxy")
	var grass_visual := {"kind": "grass", "width": 10.0, "depth": 6.0, "height": 0.5,
		"density": 12.0, "seed": 92, "lod_near": 120.0, "lod_far": 640.0}
	var grass := Vegetation.create(grass_visual, [0.16, 0.34, 0.08], 0.75)
	var grass_replay := Vegetation.create(grass_visual, [0.16, 0.34, 0.08], 0.75)
	var blades := grass.get_node("Blades") as MultiMeshInstance3D
	var replay_blades := grass_replay.get_node("Blades") as MultiMeshInstance3D
	_check(blades.multimesh.instance_count == 720, "grass density should set a bounded instance count")
	_check(blades.multimesh.mesh == replay_blades.multimesh.mesh, "grass patches should share one blade prototype")
	_check(blades.multimesh.get_instance_transform(37) == replay_blades.multimesh.get_instance_transform(37),
		"grass placement should replay from its stable seed")
	_check(blades.visibility_range_end == 120.0, "grass should be culled after the near LOD")
	first.free()
	replay.free()
	different.free()
	shrub.free()
	grass.free()
	grass_replay.free()


func _test_worker_cache() -> void:
	var worker := Worker.new()
	worker._world = Node3D.new()
	var entity := _tree_entity(12345)
	worker._sync_entities([entity])
	var original := worker._instances[0]
	var moved := entity.duplicate(true)
	moved.position = [2.0, 3.0, 4.0]
	worker._sync_entities([moved])
	_check(worker._instances[0] == original, "pose-only updates must reuse generated resources")
	_check(worker._instances[0].position == Vector3(2.0, 4.0, -3.0), "pose updates must still reach the cached tree")
	var changed := moved.duplicate(true)
	changed.visual = moved.visual.merged({"radius": 2.5}, true)
	worker._sync_entities([changed])
	_check(worker._instances[0] != original, "visual parameter changes must rebuild the tree")
	worker._sync_entities([])
	_check(worker._instances.is_empty() and worker._signatures.is_empty(), "removed entities must leave no cached nodes")
	# The production protocol uploads immutable definitions once and then sends
	# only a resource token plus pose. This path must preserve the same cache rule.
	var definition := _tree_entity(54321)
	definition["resource"] = "tree-resource"
	worker._sync_resources([definition])
	var light := {"resource": "tree-resource", "position": [1.0, 2.0, 3.0],
		"rotation": entity.rotation}
	worker._sync_entities([light])
	var resource_instance := worker._instances[0]
	light["position"] = [4.0, 5.0, 6.0]
	worker._sync_entities([light])
	_check(worker._instances[0] == resource_instance, "resource-token pose updates must reuse generated resources")
	_check(worker._instances[0].position == Vector3(4.0, 6.0, -5.0), "resource-token pose must reach cached tree")
	worker._sync_entities([])
	_check(worker._resources.is_empty(), "unused resource definitions must be pruned")
	worker._world.free()
	worker.free()


func _test_aircraft_models() -> void:
	var worker := Worker.new()
	for model_name in ["la11", "f86"]:
		var aircraft := worker._aircraft_model(model_name)
		_check(aircraft != null, "%s GLB should instantiate" % model_name)
		var visual := aircraft.get_child(0) as Node3D
		var mesh_count := 0
		var stack: Array[Node] = [aircraft]
		while not stack.is_empty():
			var node: Node = stack.pop_back()
			if node is MeshInstance3D:
				mesh_count += 1
				var mesh_instance := node as MeshInstance3D
				_check(mesh_instance.mesh != null and mesh_instance.mesh.get_aabb().has_volume(), "%s mesh should have bounds" % model_name)
				_check(mesh_instance.material_override != null, "%s white model should receive a material" % model_name)
			for child in node.get_children():
				stack.append(child)
		_check(mesh_count > 0, "%s GLB should contain visible meshes" % model_name)
		_check(visual.basis.y.dot(Vector3.UP) > 0.999, "%s model correction must keep wings level" % model_name)
		_check(visual.basis.x.dot(Vector3.FORWARD) > 0.999 or visual.basis.z.dot(Vector3.FORWARD) > 0.999, "%s model correction must point the nose along Godot forward" % model_name)
		aircraft.free()
	worker.free()


func _tree_entity(seed_value: int) -> Dictionary:
	return {
		"type": 2,
		"parameters": [2.0, 7.0],
		"vertices": null,
		"triangles": null,
		"visual": {"kind": "tree_canopy", "height": 7.0, "radius": 2.0,
			"seed": seed_value, "lod_near": 120.0, "lod_far": 640.0},
		"color": [0.12, 0.35, 0.08],
		"reflectance": 0.8,
		"position": [0.0, 0.0, 0.0],
		"rotation": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
	}


func _finite_transform(transform_value: Transform3D) -> bool:
	for value in [transform_value.origin.x, transform_value.origin.y, transform_value.origin.z, transform_value.basis.x.x, transform_value.basis.y.y, transform_value.basis.z.z]:
		if is_nan(value) or is_inf(value):
			return false
	return true


func _check(condition: bool, message: String) -> void:
	if not condition:
		failures.append(message)

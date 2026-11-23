extends "res://scenes/airspace_base_scene.gd"
## Richer PBR terrain, vegetation instancing, daylight sky/fog, and ridge
## occluder variant sharing the exact same 64-bit simulation skeleton & downstream contract.


func _init() -> void:
	scenario_id = "godot-scenic-ridge"
	random_seed = 2002
	total_steps = 10


func _build_presentation() -> void:
	var env := Environment.new()
	env.background_mode = Environment.BG_SKY
	var sky := Sky.new()
	var sky_mat := ProceduralSkyMaterial.new()
	sky_mat.sky_top_color = Color(0.24, 0.48, 0.82)
	sky_mat.sky_horizon_color = Color(0.68, 0.78, 0.88)
	sky_mat.ground_bottom_color = Color(0.18, 0.22, 0.16)
	sky_mat.ground_horizon_color = Color(0.42, 0.48, 0.40)
	sky.sky_material = sky_mat
	env.sky = sky
	env.ambient_light_source = Environment.AMBIENT_SOURCE_SKY
	env.fog_enabled = true
	env.fog_density = 0.0008
	env.fog_sky_affect = 0.15
	var world_env := WorldEnvironment.new()
	world_env.environment = env
	add_child(world_env)

	var sun := DirectionalLight3D.new()
	sun.light_energy = 1.6
	sun.shadow_enabled = true
	sun.basis = Basis.looking_at(Vector3(-0.35, -0.75, -0.55).normalized(), Vector3.UP)
	add_child(sun)

	# PBR ground terrain plane at ENU z = 0 m.
	var ground_mat := StandardMaterial3D.new()
	ground_mat.albedo_color = Color(0.22, 0.29, 0.19)
	ground_mat.roughness = 0.88
	var ground_mesh := PlaneMesh.new()
	ground_mesh.size = Vector2(2000.0, 2000.0)
	var ground := MeshInstance3D.new()
	ground.name = "PbrTerrain"
	ground.mesh = ground_mesh
	ground.material_override = ground_mat
	add_child(ground)

	# Instanced procedural tree canopies seeded deterministically via SplitMix64.
	var tree_mesh := CylinderMesh.new()
	tree_mesh.top_radius = 0.0
	tree_mesh.bottom_radius = 2.2
	tree_mesh.height = 7.5
	var tree_mat := StandardMaterial3D.new()
	tree_mat.albedo_color = Color(0.14, 0.27, 0.13)
	tree_mat.roughness = 0.92
	var mm := MultiMesh.new()
	mm.transform_format = MultiMesh.TRANSFORM_3D
	mm.mesh = tree_mesh
	mm.instance_count = 40
	for i in range(40):
		var rx := ScalarMath.deterministic_unit_float(random_seed, 0, 100 + i * 2)
		var ry := ScalarMath.deterministic_unit_float(random_seed, 0, 101 + i * 2)
		var px := -120.0 + rx * 240.0
		var py := 25.0 + ry * 140.0
		var sc := 0.8 + 0.5 * ScalarMath.deterministic_unit_float(random_seed, 0, 200 + i)
		var xform := Transform3D(Basis().scaled(Vector3(sc, sc, sc)), ScalarMath.enu_to_godot_vec3([px, py, 3.75 * sc]))
		mm.set_instance_transform(i, xform)
	var forest := MultiMeshInstance3D.new()
	forest.name = "VegetationInstancing"
	forest.multimesh = mm
	forest.material_override = tree_mat
	add_child(forest)

	# Physical ridge occluder blocking station s1 during early trajectory steps.
	var ridge_body := StaticBody3D.new()
	ridge_body.name = "RidgeOccluder"
	ridge_body.collision_layer = 1
	ridge_body.collision_mask = 0
	ridge_body.position = ScalarMath.enu_to_godot_vec3([-41.0, 60.0, 18.0])

	var ridge_shape := CollisionShape3D.new()
	var box := BoxShape3D.new()
	box.size = Vector3(22.0, 36.0, 8.0) # Godot X=east, Y=up, Z=south
	ridge_shape.shape = box
	ridge_body.add_child(ridge_shape)

	var ridge_mat := StandardMaterial3D.new()
	ridge_mat.albedo_color = Color(0.28, 0.25, 0.22)
	ridge_mat.roughness = 0.95
	var ridge_vis := MeshInstance3D.new()
	var vis_mesh := BoxMesh.new()
	vis_mesh.size = box.size
	ridge_vis.mesh = vis_mesh
	ridge_vis.material_override = ridge_mat
	ridge_body.add_child(ridge_vis)
	add_child(ridge_body)


func _build_target_visual() -> Node3D:
	# High-contrast dark metallic airframe against bright procedural sky.
	var node := super._build_target_visual()
	var dark_mat := StandardMaterial3D.new()
	dark_mat.albedo_color = Color(0.10, 0.12, 0.16)
	dark_mat.metallic = 0.65
	dark_mat.roughness = 0.35
	for child in node.get_children():
		if child is MeshInstance3D:
			(child as MeshInstance3D).material_override = dark_mat
	return node

extends Node3D
## CLI entrypoint for Chongming Godot Producer (`V-Switch Record` client).

const SceneContract = preload("res://core/scene_contract.gd")
const Runner = preload("res://core/producer_runner.gd")
const BusClient = preload("res://core/producer_bus_client.gd")

func _ready() -> void:
	call_deferred("_start")


func _start() -> void:
	var args := _parse_args(OS.get_cmdline_user_args())
	if args.has("error"):
		push_error(str(args["error"]))
		get_tree().quit(2)
		return

	var scene_node: SceneContract = _instantiate_scene(args)
	if scene_node == null:
		get_tree().quit(2)
		return
	add_child(scene_node)

	var overrides: Dictionary = {}
	for key in ["steps", "seed", "step_period_ns", "instance_id", "session_id", "scenario_id"]:
		if args.has(key):
			if key == "steps":
				overrides["total_steps"] = int(args["steps"])
			elif key == "seed":
				overrides["random_seed"] = int(args["seed"])
			else:
				overrides[key] = args[key]

	var runner := Runner.new()
	var client := BusClient.new()
	var summary := await runner.run(
		scene_node,
		client,
		str(args["bus_host"]),
		int(args["bus_port"]),
		str(args["truth_host"]),
		int(args["truth_port"]),
		overrides
	)
	print("PRODUCER_SUMMARY " + JSON.stringify(summary))
	get_tree().quit(0 if bool(summary.get("ok", false)) else 1)


func _instantiate_scene(args: Dictionary) -> SceneContract:
	if args.has("scene_tscn"):
		var packed := load(str(args["scene_tscn"])) as PackedScene
		if packed == null:
			push_error("Failed to load PackedScene: %s" % args["scene_tscn"])
			return null
		var node := packed.instantiate()
		if not (node is SceneContract):
			push_error("Scene root must extend ProducerSceneContract: %s" % args["scene_tscn"])
			node.free()
			return null
		return node as SceneContract
	if args.has("scene_script"):
		var script_res := load(str(args["scene_script"])) as GDScript
		if script_res == null:
			push_error("Failed to load scene script: %s" % args["scene_script"])
			return null
		var instance: Variant = script_res.new()
		if not (instance is SceneContract):
			push_error("Scene script must extend ProducerSceneContract: %s" % args["scene_script"])
			return null
		return instance as SceneContract
	push_error("Missing --scene-tscn or --scene-script argument")
	return null


static func _split_host_port(spec: String) -> Array:
	var idx := spec.rfind(":")
	if idx <= 0 or idx >= spec.length() - 1:
		return []
	return [spec.substr(0, idx), int(spec.substr(idx + 1))]


static func _parse_args(argv: PackedStringArray) -> Dictionary:
	var out := {
		"bus_host": "127.0.0.1",
		"bus_port": 7000,
		"truth_host": "127.0.0.1",
		"truth_port": 7001,
	}
	var i := 0
	while i < argv.size():
		var tok := argv[i]
		if tok == "--bus" and i + 1 < argv.size():
			var hp := _split_host_port(argv[i + 1])
			if hp.is_empty():
				return {"error": "Invalid --bus endpoint: %s" % argv[i + 1]}
			out["bus_host"] = hp[0]
			out["bus_port"] = hp[1]
			i += 2
		elif tok == "--bus-host" and i + 1 < argv.size():
			out["bus_host"] = argv[i + 1]
			i += 2
		elif tok == "--bus-port" and i + 1 < argv.size():
			out["bus_port"] = int(argv[i + 1])
			i += 2
		elif tok == "--truth" and i + 1 < argv.size():
			var hp := _split_host_port(argv[i + 1])
			if hp.is_empty():
				return {"error": "Invalid --truth endpoint: %s" % argv[i + 1]}
			out["truth_host"] = hp[0]
			out["truth_port"] = hp[1]
			i += 2
		elif tok == "--truth-host" and i + 1 < argv.size():
			out["truth_host"] = argv[i + 1]
			i += 2
		elif tok == "--truth-port" and i + 1 < argv.size():
			out["truth_port"] = int(argv[i + 1])
			i += 2
		elif tok == "--scene-tscn" and i + 1 < argv.size():
			out["scene_tscn"] = argv[i + 1]
			i += 2
		elif tok == "--scene-script" and i + 1 < argv.size():
			out["scene_script"] = argv[i + 1]
			i += 2
		elif tok == "--steps" and i + 1 < argv.size():
			out["steps"] = int(argv[i + 1])
			i += 2
		elif tok == "--seed" and i + 1 < argv.size():
			out["seed"] = int(argv[i + 1])
			i += 2
		elif tok == "--fps" and i + 1 < argv.size():
			var fps := float(argv[i + 1])
			if fps <= 0.0:
				return {"error": "--fps must be positive"}
			out["step_period_ns"] = int(round(1000000000.0 / fps))
			i += 2
		elif tok == "--instance-id" and i + 1 < argv.size():
			out["instance_id"] = argv[i + 1]
			i += 2
		elif tok == "--session-id" and i + 1 < argv.size():
			out["session_id"] = argv[i + 1]
			i += 2
		elif tok == "--scenario-id" and i + 1 < argv.size():
			out["scenario_id"] = argv[i + 1]
			i += 2
		else:
			return {"error": "Unknown or incomplete argument: %s" % tok}
	return out

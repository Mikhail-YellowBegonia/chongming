extends "res://scenes/airspace_base_scene.gd"
## Minimal clean-background airspace benchmark scene for CI/smoke verification.


func _init() -> void:
	scenario_id = "godot-clean-range"
	random_seed = 1001
	total_steps = 10

extends Node

## Python owns simulation time; Godot owns procedural visuals. Render only on a snapshot request.
const Vegetation = preload("res://vegetation.gd")
const VegetationInstancing = preload("res://vegetation_instancing.gd")
const AtmosphereShader = preload("res://atmosphere.gdshader")
const OpticsShader = preload("res://optics.gdshader")
const TerrainShader = preload("res://terrain.gdshader")
const BuildingShader = preload("res://building.gdshader")
const GroundcoverShader = preload("res://groundcover.gdshader")
const PreviewUi = preload("res://ui.gd")
var _server := TCPServer.new()
var _peer: StreamPeerTCP
var _buffer := PackedByteArray()
var _expected_payload := -1
var _pending_request: Dictionary = {}
# Display copies only: Python owns the state and resends it on every UI request.
var _ui_state: Dictionary = {}
var _ui_textures: Dictionary = {}
var _ui_commands: Array[Dictionary] = []
# Built on the first UI request; sensor-only clients keep the minimized window.
var _ui: Control
var _port := 0
var _token := ""
var _external_scene_path := ""
var _external_scene_active := false
var _viewport: SubViewport
var _world: Node3D
var _camera: Camera3D
var _environment: Environment
var _sun: DirectionalLight3D
var _sky_material: ShaderMaterial
var _optics_material: ShaderMaterial
var _output_viewport: SubViewport
var _output_rect: ColorRect
var _ground: MeshInstance3D
var _ground_material: ShaderMaterial
var _instances: Array[Node3D] = []
var _signatures: Array[String] = []
var _resources: Dictionary = {}
var _busy := false
var _lod_focal_y := 800.0
var _has_mesh_terrain := false
var _wind_active := false
var _wind_mean := Vector2.ZERO
var _wind_turbulence := 0.0
var _wind_time := 0.0
var _editor_active := false
var _editor_realtime := false
var _editor_closed := false
var _free_viewport: SubViewport
var _vegetation_instancing: VegetationInstancing
var _use_vegetation_instancing := true  # Feature flag for GPU instancing
var _free_camera: Camera3D
var _editor_gizmos: Node3D
var _editor_display: TextureRect
var _editor_fps: Label
var _fps_elapsed := 0.0
var _looking := false
var _free_yaw := 0.0
var _free_pitch := 0.0


func _ready() -> void:
	get_tree().auto_accept_quit = false
	DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_MINIMIZED)
	get_viewport().disable_3d = true
	seed(42)
	_parse_arguments()
	_setup_world()
	var error := _server.listen(_port, "127.0.0.1")
	if error != OK:
		push_error("Godot renderer could not listen: %s" % error_string(error))
		get_tree().quit(2)


func _setup_world() -> void:
	_viewport = SubViewport.new()
	_viewport.own_world_3d = true
	_viewport.render_target_update_mode = SubViewport.UPDATE_DISABLED
	_viewport.transparent_bg = false
	add_child(_viewport)
	_world = Node3D.new()
	_viewport.add_child(_world)
	# Initialize vegetation GPU instancing system
	_vegetation_instancing = VegetationInstancing.new()
	_vegetation_instancing.initialize(_world)
	if not _external_scene_path.is_empty():
		_external_scene_active = true
		var external_scene := load(_external_scene_path) as PackedScene
		if external_scene == null:
			push_error("external Godot scene could not be loaded: %s" % _external_scene_path)
			get_tree().quit(2)
			return
		var external_instance := external_scene.instantiate()
		if not external_instance is Node3D:
			push_error("external Godot scene root must be Node3D: %s" % _external_scene_path)
			get_tree().quit(2)
			return
		external_instance.name = "ExternalScene"
		_world.add_child(external_instance)
	_camera = Camera3D.new()
	_camera.cull_mask = 1
	_camera.current = true
	_world.add_child(_camera)
	_free_viewport = SubViewport.new()
	# own_world_3d creates an internal world; world_3d itself may remain null.
	_free_viewport.world_3d = _viewport.find_world_3d()
	_free_viewport.size = Vector2i(1024, 640)
	_free_viewport.render_target_update_mode = SubViewport.UPDATE_DISABLED
	add_child(_free_viewport)
	_free_camera = Camera3D.new()
	_free_camera.cull_mask = 3
	_free_camera.fov = 70.0
	_free_camera.far = 5000.0
	_free_viewport.add_child(_free_camera)
	_editor_gizmos = Node3D.new()
	_world.add_child(_editor_gizmos)
	_editor_display = TextureRect.new()
	_editor_display.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	_editor_display.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
	_editor_display.stretch_mode = TextureRect.STRETCH_SCALE
	_editor_display.texture = _free_viewport.get_texture()
	_editor_display.visible = false
	add_child(_editor_display)
	_editor_fps = Label.new()
	_editor_fps.position = Vector2(12, 12)
	_editor_fps.add_theme_color_override("font_color", Color.WHITE)
	_editor_fps.add_theme_color_override("font_shadow_color", Color.BLACK)
	_editor_fps.add_theme_constant_override("shadow_offset_x", 1)
	_editor_fps.add_theme_constant_override("shadow_offset_y", 1)
	_editor_fps.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_editor_fps.visible = false
	add_child(_editor_fps)
	_environment = Environment.new()
	_environment.background_mode = Environment.BG_COLOR
	_environment.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	_environment.ambient_light_color = Color.WHITE
	_environment.ambient_light_energy = 0.6
	_environment.tonemap_mode = Environment.TONE_MAPPER_LINEAR
	_sky_material = ShaderMaterial.new()
	_sky_material.shader = AtmosphereShader
	var sky := Sky.new()
	sky.process_mode = Sky.PROCESS_MODE_REALTIME
	sky.sky_material = _sky_material
	_environment.sky = sky
	var environment_node := WorldEnvironment.new()
	environment_node.environment = _environment
	_world.add_child(environment_node)
	_sun = DirectionalLight3D.new()
	_sun.shadow_enabled = true
	_world.add_child(_sun)
	# A second, request-only viewport applies inexpensive optical degradation.
	# Compatibility has no HDR glow/auto-exposure, so use an explicit shader.
	_output_viewport = SubViewport.new()
	_output_viewport.disable_3d = true
	_output_viewport.render_target_update_mode = SubViewport.UPDATE_DISABLED
	add_child(_output_viewport)
	_output_rect = ColorRect.new()
	_optics_material = ShaderMaterial.new()
	_optics_material.shader = OpticsShader
	_optics_material.set_shader_parameter("scene_texture", _viewport.get_texture())
	_output_rect.material = _optics_material
	_output_viewport.add_child(_output_rect)
	_ground = MeshInstance3D.new()
	var plane := PlaneMesh.new()
	plane.size = Vector2(200000.0, 200000.0)
	_ground.mesh = plane
	_ground_material = ShaderMaterial.new()
	_ground_material.shader = TerrainShader
	_ground.material_override = _ground_material
	_world.add_child(_ground)


func _process(_delta: float) -> void:
	if _editor_active and not _busy:
		_move_free_camera(_delta)
		_fps_elapsed += _delta
		if _fps_elapsed >= 0.5:
			_editor_fps.text = "%d FPS · EDIT" % Engine.get_frames_per_second() if _editor_realtime else "FREEVIEW · simulation clock"
			_fps_elapsed = 0.0
	if _peer == null:
		if _server.is_connection_available():
			_peer = _server.take_connection()
			_peer.set_no_delay(true)
			_peer.put_data(("READY %s\n" % _token).to_utf8_buffer())
		return
	_peer.poll()
	if _peer.get_status() != StreamPeerTCP.STATUS_CONNECTED:
		get_tree().quit()
		return
	# Drain everything readable this tick: UI requests carry whole sensor
	# images, and one socket read per frame would cap throughput at the
	# kernel buffer size times the process rate.
	var available := _peer.get_available_bytes()
	while available > 0:
		var packet := _peer.get_data(available)
		if packet[0] != OK:
			get_tree().quit(2)
			return
		_buffer.append_array(packet[1])
		_peer.poll()
		available = _peer.get_available_bytes()
	if not _busy:
		_process_packets()


func _parse_arguments() -> void:
	var args := OS.get_cmdline_user_args()
	for index in range(args.size() - 1):
		if args[index] == "--port":
			_port = int(args[index + 1])
		elif args[index] == "--token":
			_token = args[index + 1]
		elif args[index] == "--external-scene":
			_external_scene_path = args[index + 1]
	if _port <= 0:
		push_error("Godot renderer requires --port")
		get_tree().quit(2)


func _process_packets() -> void:
	if _pending_request.is_empty():
		if _expected_payload < 0:
			if _buffer.size() < 4:
				return
			_expected_payload = _buffer.decode_u32(0)
			_buffer = _buffer.slice(4)
			if _expected_payload < 2 or _expected_payload > 256 * 1024 * 1024:
				get_tree().quit(2)
				return
		if _buffer.size() < _expected_payload:
			return
		var payload := _buffer.slice(0, _expected_payload)
		_buffer = _buffer.slice(_expected_payload)
		_expected_payload = -1
		var parsed = JSON.parse_string(payload.get_string_from_utf8())
		if not parsed is Dictionary:
			get_tree().quit(2)
			return
		_pending_request = parsed
	# Only UI requests carry a raw trailer; render/editor requests end at the JSON.
	var binary_length := int(_pending_request.get("binary_length", 0))
	if binary_length < 0 or binary_length > 256 * 1024 * 1024:
		get_tree().quit(2)
		return
	if _buffer.size() < binary_length:
		return
	var request := _pending_request
	_pending_request = {}
	if str(request.get("mode", "")) == "ui":
		var trailer := _buffer.slice(0, binary_length)
		_buffer = _buffer.slice(binary_length)
		var reply := _ui_exchange(request, trailer)
		if reply.is_empty():
			get_tree().quit(2)
			return
		var header := PackedByteArray()
		header.resize(16)
		header.encode_u64(0, reply.size())
		header.encode_u64(8, 0)
		_peer.put_data(header)
		_peer.put_data(reply)
		return
	_busy = true
	var started := Time.get_ticks_usec()
	var pixels := await _render(request)
	var response := PackedByteArray()
	response.resize(16)
	response.encode_u64(0, pixels.size())
	response.encode_u64(8, (Time.get_ticks_usec() - started) * 1000)
	_peer.put_data(response)
	_peer.put_data(pixels)
	_busy = false


## Store the pushed UI state and images; answer with the queued user commands.
## Returns an empty buffer for a malformed request.
func _ui_exchange(request: Dictionary, trailer: PackedByteArray) -> PackedByteArray:
	var state = request.get("state")
	var images = request.get("images")
	if not state is Dictionary or not images is Array:
		return PackedByteArray()
	var keys: Array[String] = []
	for entry_value in images:
		if not entry_value is Dictionary:
			return PackedByteArray()
		var entry: Dictionary = entry_value
		var key := str(entry.get("key", ""))
		var width := int(entry.get("width", 0))
		var height := int(entry.get("height", 0))
		var offset := int(entry.get("offset", -1))
		var length := int(entry.get("length", -1))
		if key.is_empty() or width <= 0 or height <= 0 or length != width * height * 3 \
				or offset < 0 or offset + length > trailer.size():
			return PackedByteArray()
		var image := Image.create_from_data(width, height, false, Image.FORMAT_RGB8,
			trailer.slice(offset, offset + length))
		var texture: ImageTexture = _ui_textures.get(key)
		if texture != null and texture.get_size() == Vector2(width, height):
			texture.update(image)
		else:
			_ui_textures[key] = ImageTexture.create_from_image(image)
		keys.append(key)
	_ui_state = state
	if _ui == null:
		_open_ui()
	_ui.show_state(state, _ui_textures)
	var reply := {
		"accepted": {"version": state.get("version"), "images": keys},
		"commands": _ui_commands,
	}
	_ui_commands = []
	return JSON.stringify(reply).to_utf8_buffer()


func _open_ui() -> void:
	_ui = PreviewUi.new()
	_ui.worker = self
	add_child(_ui)
	_ui.build(_editor_display, _editor_fps)
	# Match the freeview resolution to its tab instead of stretching 1024x640.
	_editor_display.resized.connect(func() -> void:
		_free_viewport.size = Vector2i(_editor_display.size.max(Vector2.ONE))
		_redraw_freeview())
	DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_NO_FOCUS, false)
	DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_BORDERLESS, false)
	DisplayServer.window_set_title("Chongming 预览")
	DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_WINDOWED)
	DisplayServer.window_set_size(Vector2i(1440, 900))
	RenderingServer.render_loop_enabled = true


## Queue a user command for Python's next UI request.
func ui_emit(command: Dictionary) -> void:
	_ui_commands.append(command)


func _render(request: Dictionary) -> PackedByteArray:
	var width := int(request.get("width", 0))
	var height := int(request.get("height", 0))
	if width <= 0 or height <= 0 or width * height > 64 * 1024 * 1024:
		return PackedByteArray()
	_viewport.size = Vector2i(width, height)
	# ENU -> Godot: (east, up, -north), a proper rotation. Optical axes
	# right/down/forward -> Camera3D right/up/back by flipping y and z.
	_camera.transform = Transform3D(
		_basis(request["rotation"]) * Basis(Vector3.RIGHT, Vector3.DOWN, Vector3.FORWARD),
		_enu(request["origin"])
	)
	var focal: Array = request["focal"]
	_lod_focal_y = float(focal[1])
	_wind_time = float(request.get("windTime", 0.0))
	var wind_value: Variant = request.get("wind", null)
	_wind_active = wind_value is Array and wind_value.size() >= 6
	if _wind_active:
		_wind_mean = Vector2(float(wind_value[0]), float(wind_value[1]))
		_wind_turbulence = Vector2(float(wind_value[3]), float(wind_value[4])).length()
	var near := 0.02
	var offset := Vector2(
		(width * 0.5 - float(focal[2]) - 0.5) * near / float(focal[0]),
		(float(focal[3]) + 0.5 - height * 0.5) * near / float(focal[1])
	)
	_camera.set_frustum(height * near / float(focal[1]), offset, near, 100000.0)
	_environment.background_color = _color(request["sky"])
	var sky_color := _color(request["sky"])
	var visibility_variant: Variant = request.get("atmosphericVisibility", null)
	var sun_diameter := float(request.get("sunDiameter", 0.0))
	var cloud_layers: Array = request.get("clouds", [])
	# Draw the procedural sky whenever the sun is above the horizon, or when the
	# request carries explicit environment features (visibility/fog, a solar
	# disc or clouds). A below-horizon default sun with none of those keeps the
	# flat sky colour: the atmosphere gradient is not meaningful there, and
	# legacy night / structured views stay unchanged.
	var sun_above_horizon := _enu(request["sun"]).y > 0.0
	var explicit_environment := visibility_variant != null or sun_diameter > 0.0 or not cloud_layers.is_empty()
	_environment.background_mode = Environment.BG_SKY if sun_above_horizon or explicit_environment else Environment.BG_COLOR
	_sky_material.set_shader_parameter("sky_tint", Vector3(float(request.sky[0]), float(request.sky[1]), float(request.sky[2])))
	_sky_material.set_shader_parameter("sun_direction", _enu(request["sun"]).normalized())
	_sky_material.set_shader_parameter("sun_radius", deg_to_rad(sun_diameter * 0.5))
	_sky_material.set_shader_parameter("sun_energy", maxf(0.0, float(request.illuminance) / 100000.0))
	_sky_material.set_shader_parameter("cloud_count", mini(cloud_layers.size(), 2))
	_sky_material.set_shader_parameter("camera_horizontal", Vector2(_camera.position.x, _camera.position.z))
	_sky_material.set_shader_parameter("camera_altitude", _camera.position.y)
	_sky_material.set_shader_parameter("cloud_time", float(request.get("cloudTime", 0.0)))
	for layer_index in range(2):
		var cloud := Vector4.ZERO
		if layer_index < cloud_layers.size():
			var layer: Dictionary = cloud_layers[layer_index]
			var drift: Array = layer.get("drift", [0.0, 0.0, 0.0])
			cloud = Vector4(float(layer.get("altitude", 0.0)), float(layer.get("density", 0.0)),
				float(drift[0]), -float(drift[1]))
		_sky_material.set_shader_parameter("cloud_layer_%d" % layer_index, cloud)
	_environment.fog_enabled = visibility_variant != null
	if visibility_variant != null:
		var visibility := maxf(1.0, float(visibility_variant))
		_environment.fog_light_color = sky_color.lightened(0.08)
		_environment.fog_light_energy = 0.7
		# Koschmieder's 2% contrast distance. Clear-air haze only; no ray cutoff.
		_environment.fog_density = 3.912 / visibility
		_environment.fog_height = 0.0
		_environment.fog_height_density = 0.0
		_environment.fog_sky_affect = 0.0
	var ssao_intensity := float(request.get("ssaoIntensity", 0.0))
	_environment.ssao_enabled = ssao_intensity > 0.0
	_environment.ssao_radius = 1.25
	_environment.ssao_intensity = ssao_intensity
	# Artistic baseline: unit directional energy represents full daylight.
	# This is not a calibrated photometric camera/exposure model.
	var cloud_cover := 0.0
	for layer in cloud_layers:
		cloud_cover = 1.0 - (1.0 - cloud_cover) * (1.0 - float(layer.get("density", 0.0)))
	_sun.light_energy = float(request["illuminance"]) / 100000.0 * (1.0 - cloud_cover * 0.32)
	var sun_direction := _enu(request["sun"]).normalized()
	_sun.basis = Basis.looking_at(-sun_direction, Vector3.FORWARD if absf(sun_direction.y) > 0.99 else Vector3.UP)
	_sync_resources(request.get("resources", []))
	_sync_entities(request["entities"])
	# Resolve terrain presence before positioning the fallback. This avoids
	# using the previous snapshot's DEM flag when a scene changes between flat
	# and heightfield terrain.
	_ground.position = Vector3(
		_camera.position.x,
		float(request["terrainElevation"]),
		_camera.position.z
	)
	_ground_material.set_shader_parameter("base_color", Vector3(
		float(request.terrainColor[0]) * float(request.terrainReflectance),
		float(request.terrainColor[1]) * float(request.terrainReflectance),
		float(request.terrainColor[2]) * float(request.terrainReflectance)))
	_ground_material.set_shader_parameter("detail_strength", float(request.get("terrainDetail", 0.0)))
	if str(request.get("mode", "")) == "editor":
		var editor_enabled := bool(request.get("editor_enabled", false))
		var editor_realtime := bool(request.get("editor_realtime", false))
		var was_editor_active := _editor_active
		if bool(request.get("editor_reopen", false)) or editor_realtime:
			_editor_closed = false
		editor_enabled = editor_enabled and not _editor_closed
		if editor_enabled:
			_sync_editor_markers(request.get("editor_markers", []))
			if not _editor_active:
				_position_editor_overview(request.get("entities", []))
				_free_yaw = _free_camera.rotation.y
				_free_pitch = _free_camera.rotation.x
			_editor_active = true
			_editor_realtime = editor_realtime
			_editor_gizmos.visible = true
			_free_camera.make_current()
			_free_viewport.render_target_update_mode = SubViewport.UPDATE_ALWAYS if editor_realtime else SubViewport.UPDATE_ONCE
			if not was_editor_active:
				_display_editor(true, editor_realtime)
			else:
				RenderingServer.render_loop_enabled = editor_realtime or _ui != null
			_sky_material.set_shader_parameter("camera_horizontal", Vector2(_free_camera.position.x, _free_camera.position.z))
			_sky_material.set_shader_parameter("camera_altitude", _free_camera.position.y)
			await get_tree().process_frame
			# Present the root window as well as rendering the freeview texture.
			RenderingServer.force_draw(true)
		else:
			_editor_active = false
			_editor_realtime = false
			_editor_gizmos.visible = false
			_free_viewport.render_target_update_mode = SubViewport.UPDATE_DISABLED
			_display_editor(false, false)
		return PackedByteArray([1 if _editor_active else 0])
	_camera.make_current()
	# No GPU frames between requests, even while the experiment is paused.
	# Flush SceneTree transform/material notifications before drawing; otherwise
	# the first image can contain the previous snapshot or an empty viewport.
	await get_tree().process_frame
	_viewport.render_target_update_mode = SubViewport.UPDATE_ONCE
	RenderingServer.force_draw(false)
	var output := _viewport
	var glare := float(request.get("glareStrength", 0.0))
	var god_rays := float(request.get("godRaysStrength", 0.0))
	var fxaa := bool(request.get("fxaaEnabled", false))
	if glare > 0.0 or god_rays > 0.0 or fxaa:
		_output_viewport.size = Vector2i(width, height)
		_output_rect.size = Vector2(width, height)
		_optics_material.set_shader_parameter("strength", glare)
		_optics_material.set_shader_parameter("pixel_size", Vector2(1.0 / width, 1.0 / height))
		var local_sun := _camera.basis.inverse() * sun_direction
		var sun_uv := Vector2(-10.0, -10.0)
		var sun_radius_uv := Vector2.ZERO
		if local_sun.z < -0.0001 and sun_direction.y > 0.0 and sun_diameter > 0.0:
			sun_uv = Vector2(
				(float(focal[0]) * local_sun.x / -local_sun.z + float(focal[2]) + 0.5) / width,
				(-float(focal[1]) * local_sun.y / -local_sun.z + float(focal[3]) + 0.5) / height
			)
			sun_radius_uv = Vector2(float(focal[0]) / width, float(focal[1]) / height) * tan(deg_to_rad(sun_diameter * 0.5)) / -local_sun.z
		_optics_material.set_shader_parameter("sun_uv", sun_uv)
		_optics_material.set_shader_parameter("sun_radius_uv", sun_radius_uv)
		_optics_material.set_shader_parameter("focal_uv", Vector2(float(focal[0]) / width, float(focal[1]) / height))
		_optics_material.set_shader_parameter("god_rays_strength", god_rays)
		_optics_material.set_shader_parameter("fxaa_enabled", fxaa)
		await get_tree().process_frame
		_output_viewport.render_target_update_mode = SubViewport.UPDATE_ONCE
		RenderingServer.force_draw(false)
		output = _output_viewport
	var image := output.get_texture().get_image()
	image.convert(Image.FORMAT_RGB8)
	return image.get_data()


func _display_editor(enabled: bool, realtime: bool) -> void:
	_editor_display.visible = enabled
	_editor_fps.visible = enabled
	_looking = false
	Input.mouse_mode = Input.MOUSE_MODE_VISIBLE
	if _ui != null:
		# The preview window stays open and drawing; freeview is one of its tabs.
		return
	RenderingServer.render_loop_enabled = enabled and realtime
	DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_NO_FOCUS, not enabled)
	DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_BORDERLESS, not enabled)
	if enabled:
		DisplayServer.window_set_size(Vector2i(1024, 640))
		DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_WINDOWED)
	else:
		DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_MINIMIZED)


func _notification(what: int) -> void:
	if what == NOTIFICATION_WM_CLOSE_REQUEST and _ui != null:
		# Python ends the session; Godot exits when the socket closes.
		ui_emit({"type": "quit"})
	elif what == NOTIFICATION_WM_CLOSE_REQUEST and _editor_active and not _editor_realtime:
		# Closing the viewer must leave the renderer and experiment alive.
		_editor_closed = true
		_editor_active = false
		_editor_gizmos.visible = false
		_free_viewport.render_target_update_mode = SubViewport.UPDATE_DISABLED
		_display_editor(false, false)


func _redraw_freeview() -> void:
	if _editor_active and not _editor_realtime and not _busy:
		_sky_material.set_shader_parameter("camera_horizontal", Vector2(_free_camera.position.x, _free_camera.position.z))
		_sky_material.set_shader_parameter("camera_altitude", _free_camera.position.y)
		_free_viewport.render_target_update_mode = SubViewport.UPDATE_ONCE
		if _ui == null:
			RenderingServer.force_draw(true)


func _input(event: InputEvent) -> void:
	if not _editor_active:
		return
	if event is InputEventMouseButton and event.button_index == MOUSE_BUTTON_RIGHT:
		if event.pressed and _ui != null and not (_editor_display.is_visible_in_tree()
				and _editor_display.get_global_rect().has_point(event.position)):
			return
		_looking = event.pressed
		if _looking:
			# WASD must steer the camera, not type into a focused text field.
			get_viewport().gui_release_focus()
		Input.mouse_mode = Input.MOUSE_MODE_CAPTURED if _looking else Input.MOUSE_MODE_VISIBLE
	elif event is InputEventMouseMotion and _looking:
		_free_yaw -= event.relative.x * 0.0025
		_free_pitch = clampf(_free_pitch - event.relative.y * 0.0025, -1.48, 1.48)
		_free_camera.rotation = Vector3(_free_pitch, _free_yaw, 0.0)
		_redraw_freeview()


func _move_free_camera(delta: float) -> void:
	if not _looking:
		return
	var direction := Vector3.ZERO
	if Input.is_key_pressed(KEY_W): direction.z -= 1.0
	if Input.is_key_pressed(KEY_S): direction.z += 1.0
	if Input.is_key_pressed(KEY_A): direction.x -= 1.0
	if Input.is_key_pressed(KEY_D): direction.x += 1.0
	if Input.is_key_pressed(KEY_E): direction.y += 1.0
	if Input.is_key_pressed(KEY_Q): direction.y -= 1.0
	if direction != Vector3.ZERO:
		_free_camera.position += _free_camera.global_basis * direction.normalized() * delta * (160.0 if Input.is_key_pressed(KEY_SHIFT) else 35.0)
		_redraw_freeview()


func _position_editor_overview(entities: Array) -> void:
	var minimum := Vector3(INF, INF, INF)
	var maximum := Vector3(-INF, -INF, -INF)
	var count := 0
	for entity_value in entities:
		var entity: Dictionary = entity_value
		var position := _enu(entity.get("position", [0.0, 0.0, 0.0]))
		minimum = minimum.min(position)
		maximum = maximum.max(position)
		count += 1
	var focus := _enu([0.0, 0.0, 0.0])
	var radius := 80.0
	if count > 0:
		focus = (minimum + maximum) * 0.5
		# Use roughly half the horizontal extent so a 1 km OSM sample is not a
		# tiny cluster in the middle of the viewport; keep a usable minimum for
		# the small synthetic scenes. Include height so an aerial station and
		# its frustum stay in the overview instead of sitting above the frame.
		radius = maxf(80.0, maxf(
			Vector2(maximum.x - minimum.x, maximum.z - minimum.z).length() * 0.55,
			(maximum.y - minimum.y) * 0.6))
	var distance := radius * 1.4
	_free_camera.position = focus + Vector3(distance * 0.55, distance * 0.8, distance * 0.55)
	_free_camera.look_at(focus, Vector3.UP)


func _sync_editor_markers(markers: Array) -> void:
	for child in _editor_gizmos.get_children():
		child.free()
	for marker_value in markers:
		var marker: Dictionary = marker_value
		var anchor := Node3D.new()
		anchor.position = _enu(marker.position)
		_editor_gizmos.add_child(anchor)
		var is_camera := str(marker.kind) == "camera"
		if not is_camera or str(marker.id).ends_with("/main"):
			var label := Label3D.new()
			label.text = ("CAM " if is_camera else "TARGET ") + str(marker.id)
			label.position = Vector3(0.0, 3.0, 0.0)
			label.billboard = BaseMaterial3D.BILLBOARD_ENABLED
			label.fixed_size = true
			label.font_size = 24
			label.pixel_size = 0.0012
			label.outline_size = 4
			label.no_depth_test = true
			label.layers = 2
			label.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
			label.modulate = Color(0.15, 0.85, 0.95) if is_camera else Color(1.0, 0.7, 0.32)
			anchor.add_child(label)
		if not is_camera:
			# Draw trajectory preview if available
			if marker.has("trajectory") and marker.trajectory is Array and marker.trajectory.size() > 1:
				var trajectory: Array = marker.trajectory
				var mesh := ImmediateMesh.new()
				mesh.surface_begin(Mesh.PRIMITIVE_LINE_STRIP)
				var point_count := trajectory.size()
				for i in range(point_count):
					var point: Array = trajectory[i]
					if point.size() >= 3:
						# Color gradient: hue cycles through rainbow (0.0 to 1.0)
						# Each point gets a hue based on its position in trajectory
						var hue := fmod(float(i) / float(point_count - 1), 1.0)
						var color := Color.from_hsv(hue, 0.8, 1.0, 0.8)  # 80% saturation, full brightness, 80% alpha
						mesh.surface_set_color(color)
						mesh.surface_add_vertex(_enu([point[0], point[1], point[2]]))
				mesh.surface_end()
				var trail := MeshInstance3D.new()
				trail.mesh = mesh
				trail.layers = 2
				trail.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
				var trail_material := StandardMaterial3D.new()
				trail_material.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
				trail_material.vertex_color_use_as_albedo = true  # Use per-vertex colors
				trail_material.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
				trail.material_override = trail_material
				_editor_gizmos.add_child(trail)
			continue
		var basis := _basis(marker.rotation) * Basis(Vector3.RIGHT, Vector3.DOWN, Vector3.FORWARD)
		var focal: Array = marker.focal
		var length := float(marker.length)
		var corners: Array[Vector3] = []
		for pixel in [Vector2(0, 0), Vector2(float(marker.width), 0), Vector2(float(marker.width), float(marker.height)), Vector2(0, float(marker.height))]:
			corners.append(basis * Vector3(
				(pixel.x - float(focal[2])) / float(focal[0]) * length,
				-(pixel.y - float(focal[3])) / float(focal[1]) * length, -length))
		var mesh := ImmediateMesh.new()
		mesh.surface_begin(Mesh.PRIMITIVE_LINES)
		for index in range(4):
			mesh.surface_add_vertex(Vector3.ZERO)
			mesh.surface_add_vertex(corners[index])
			mesh.surface_add_vertex(corners[index])
			mesh.surface_add_vertex(corners[(index + 1) % 4])
		mesh.surface_end()
		var lines := MeshInstance3D.new()
		lines.mesh = mesh
		lines.layers = 2
		lines.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
		var material := StandardMaterial3D.new()
		material.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
		material.albedo_color = Color(0.15, 0.85, 0.95)
		lines.material_override = material
		anchor.add_child(lines)


func _sync_entities(entities: Array) -> void:
	# GPU Instancing: batch vegetation entities first
	if _use_vegetation_instancing:
		var grouped := _vegetation_instancing.group_vegetation_entities(entities)
		_vegetation_instancing.update_batches(grouped, _wind_time, _wind_mean, _wind_turbulence)
		# Filter out vegetation entities from regular processing
		var non_vegetation: Array = []
		for entity_dict in entities:
			var entity: Dictionary = entity_dict
			var category := str(entity.get("category", ""))
			if category != "vegetation":
				non_vegetation.append(entity)
		entities = non_vegetation

	while _instances.size() > entities.size():
		_instances.pop_back().free()
		_signatures.pop_back()
	var resolved: Array = []
	for light_entity in entities:
		resolved.append(_resolve_entity(light_entity))
	# A card tree is rendered as one whole-tree model on its ground trunk entity.
	# Pair it with the canopy entity so semantic crown dimensions and the sampled
	# wind rotation stay available without changing the Python contract.
	var canopies: Dictionary = {}
	for index in range(entities.size()):
		var entity: Dictionary = resolved[index]
		if entity.is_empty():
			continue
		var visual_value: Variant = entity.get("visual", null)
		if visual_value is Dictionary and str(visual_value.get("kind", "")) == "tree_canopy":
			var base_id := str(entity.get("object_id", "")).trim_suffix("/canopy")
			var light: Dictionary = entities[index]
			canopies[base_id] = {
				"visual": visual_value,
				"position": light.get("position", entity.get("position", [0.0, 0.0, 0.0])),
				"rotation": light.get("rotation", entity.get("rotation", [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0])),
			}
	var has_mesh_terrain := false
	for entity_value in resolved:
		var resolved_entity: Dictionary = entity_value
		if (
			str(resolved_entity.get("surface", "")) == "terrain"
			and resolved_entity.get("vertices", null) != null
		):
			has_mesh_terrain = true
			break
	_has_mesh_terrain = has_mesh_terrain
	if _ground != null:
		# A heightfield is the sole terrain and shadow receiver. Keeping the
		# fallback plane enabled creates a second visible/shadow-catching ground
		# one metre below the DEM, especially obvious in freeview.
		_ground.visible = not has_mesh_terrain and not _external_scene_active
	for index in range(entities.size()):
		var light_entity: Dictionary = entities[index]
		var entity: Dictionary = resolved[index]
		if entity.is_empty():
			push_error("render entity could not be resolved")
			continue
		var visual_value: Variant = entity.get("visual", null)
		var create_visual: Dictionary = visual_value if visual_value is Dictionary else {}
		var paired: Dictionary = {}
		var tree_visual: Dictionary = {}
		if create_visual.get("kind", "") == "tree_trunk":
			var base_id := str(entity.get("object_id", "")).trim_suffix("/trunk")
			if canopies.has(base_id):
				paired = canopies[base_id]
				var canopy_visual: Dictionary = paired["visual"]
				var trunk_position: Array = light_entity.get("position", entity.get("position", [0.0, 0.0, 0.0]))
				var canopy_position: Array = paired["position"]
				var crown_offset := maxf(0.0, float(canopy_position[2]) - float(trunk_position[2]))
				var total_height := float(canopy_visual.get("height", 1.0)) + crown_offset
				if total_height <= 0.1:
					total_height = maxf(1.0, float(create_visual.get("height", 1.0)) / 0.58)
				tree_visual = create_visual.duplicate(true)
				tree_visual["kind"] = "tree"
				tree_visual["height"] = total_height
				tree_visual["radius"] = float(canopy_visual.get("radius", 1.0))
				tree_visual["seed"] = int(create_visual.get("seed", 0))
		if index == _instances.size():
			_instances.append(Node3D.new())
			_signatures.append("")
		var signature := ""
		if not str(light_entity.get("resource", "")).is_empty():
			signature = str(light_entity.get("resource", ""))
			if not tree_visual.is_empty():
				signature += "|tree|" + JSON.stringify(tree_visual)
		else:
			signature = JSON.stringify([entity.type, entity.parameters, entity.vertices, entity.triangles, entity.visual, entity.color, entity.reflectance, entity.get("surface", null), entity.get("surface_detail", 0.0), entity.get("category", null)])
			if not tree_visual.is_empty():
				signature += "|tree|" + JSON.stringify(tree_visual)
		if signature != _signatures[index]:
			var old_instance := _instances[index]
			old_instance.free()
			var new_instance: Node3D
			if create_visual.get("kind", "") in ["tree", "tree_trunk", "tree_canopy", "shrub", "grass"]:
				var light_position: Array = light_entity.get("position", [0.0, 0.0, 0.0])
				new_instance = Vegetation.create(
					tree_visual if not tree_visual.is_empty() else create_visual,
					entity.color,
					float(entity.reflectance),
					Vector2(float(light_position[0]), float(light_position[1]))
				)
			else:
				var mesh_instance := MeshInstance3D.new()
				mesh_instance.mesh = _mesh(entity)
				if str(entity.get("surface", "")) == "terrain":
					mesh_instance.material_override = _terrain_material(entity.color, float(entity.reflectance), float(entity.get("surface_detail", 0.0)))
				elif str(entity.get("category", "")) == "osm-building":
					mesh_instance.material_override = _building_material(entity.color, float(entity.reflectance), str(entity.get("object_id", "")))
				elif str(entity.get("category", "")) in ["osm-grass", "osm-forest"]:
					mesh_instance.material_override = _groundcover_material(entity.color, float(entity.reflectance), str(entity.get("category", "")) == "osm-forest")
				else:
					mesh_instance.material_override = _material(entity.color, float(entity.reflectance))
				new_instance = mesh_instance
			_world.add_child(new_instance)
			_instances[index] = new_instance
			_signatures[index] = signature
		var instance := _instances[index]
		instance.transform = Transform3D(_basis(light_entity.rotation), _enu(light_entity.position))
		_apply_visual_lod(instance, paired)
	# Definitions are immutable and the Python registry deliberately re-uploads a
	# definition if an object returns after being absent. Keep the worker bounded.
	var used: Dictionary = {}
	for light_entity in entities:
		var token := str(light_entity.get("resource", ""))
		if not token.is_empty():
			used[token] = true
	var stale: Array[String] = []
	for token in _resources.keys():
		if not used.has(token):
			stale.append(str(token))
	for token in stale:
		_resources.erase(token)


func _resolve_entity(light_entity: Dictionary) -> Dictionary:
	var token := str(light_entity.get("resource", ""))
	if token.is_empty():
		return light_entity
	if not _resources.has(token):
		push_error("render entity references unknown resource %s" % token)
		return {}
	return _resources[token]


func _apply_visual_lod(instance: Node3D, paired: Dictionary) -> void:
	var kind := str(instance.get_meta("visual_kind", ""))
	if kind.is_empty():
		return
	var height := float(instance.get_meta("visual_height", 1.0))
	var pixels := 1000.0
	if _camera != null:
		var distance := _camera.global_position.distance_to(
			instance.global_position + Vector3(0.0, height * 0.5, 0.0)
		)
		# Projected height in pixels: a compact proxy for visual footprint that
		# also works for telephoto and wide-angle cameras without hard-coded
		# world ranges.
		pixels = _lod_focal_y * height / maxf(distance, 1.0)
	if kind == "tree":
		var model := instance.get_node_or_null("Model") as Node3D
		var far_model := instance.get_node_or_null("FarModel") as Node3D
		var near_visible := pixels >= 45.0 or far_model == null
		if model != null:
			model.visible = near_visible
		if far_model != null:
			far_model.visible = not near_visible
		_set_tree_shadows(model, near_visible and pixels >= 75.0)
		if near_visible and pixels >= 60.0 and not paired.is_empty():
			_apply_tree_wind(instance, paired["rotation"])
		else:
			_reset_tree_wind(instance)
	elif kind == "shrub":
		var foliage := instance.get_node_or_null("Model/Foliage") as GeometryInstance3D
		if foliage != null:
			foliage.cast_shadow = (
				GeometryInstance3D.SHADOW_CASTING_SETTING_ON
				if pixels >= 24.0
				else GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
			)
		_apply_understory_wind(instance, pixels, "shrub")
	elif kind == "grass":
		_apply_understory_wind(instance, pixels, "grass")


func _set_tree_shadows(model: Node3D, enabled: bool) -> void:
	if model == null:
		return
	var setting := (
		GeometryInstance3D.SHADOW_CASTING_SETTING_ON
		if enabled
		else GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	)
	for child_name in ["Trunk", "Crown"]:
		var child := model.get_node_or_null(child_name) as GeometryInstance3D
		if child != null:
			child.cast_shadow = setting


func _reset_tree_wind(instance: Node3D) -> void:
	var crown := instance.get_node_or_null("Model/Crown") as Node3D
	if crown != null:
		crown.position = Vector3.ZERO
		crown.scale = Vector3.ONE


func _apply_understory_wind(instance: Node3D, pixels: float, kind: String) -> void:
	var target_name := "Model" if kind == "shrub" else "Clumps"
	var target := instance.get_node_or_null(target_name) as Node3D
	if target == null:
		return
	var base_scale := Vector3.ONE
	if target.has_meta("base_scale"):
		base_scale = target.get_meta("base_scale")
	var speed := _wind_mean.length()
	var minimum_pixels := 5.0 if kind == "grass" else 18.0
	if not _wind_active or speed <= 0.05 or pixels < minimum_pixels:
		target.position = Vector3.ZERO
		target.scale = base_scale
		return
	var seed := int(instance.get_meta("visual_seed", 0))
	var phase := float(posmod(seed, 100003)) / 100003.0 * TAU
	var time := _wind_time
	var wave := (
		sin(time * 1.7 + phase)
		+ 0.33 * sin(time * 3.3 + phase * 1.7)
		+ 0.15 * sin(time * 6.1 + phase * 0.7)
	)
	var turbulence := 1.0 + minf(1.5, _wind_turbulence / maxf(speed, 0.1))
	var amplitude := (0.045 if kind == "shrub" else 0.018) * minf(speed, 10.0) * turbulence
	var direction := _wind_mean.normalized()
	target.position = Vector3(
		direction.x * amplitude * wave, direction.y * amplitude * wave, 0.0
	)
	var squash := 0.025 * minf(1.0, absf(wave)) * minf(speed / 4.0, 1.0)
	target.scale = base_scale * Vector3(1.0 + squash, 1.0 - squash * 0.5, 1.0 + squash)


func _apply_tree_wind(instance: Node3D, rotation: Array) -> void:
	var crown := instance.get_node_or_null("Model/Crown") as Node3D
	if crown == null:
		return
	var basis := _basis(rotation)
	var lean := basis.z - Vector3.UP
	if lean.length() < 1e-5:
		crown.position = Vector3.ZERO
		crown.scale = Vector3.ONE
		return
	var model := crown.get_parent() as Node3D
	var native_height := float(crown.get_meta("native_height", 10.0))
	var offset := lean * native_height * 0.18
	if model != null:
		crown.position = model.basis.inverse() * offset
	else:
		crown.position = offset
	var squash := minf(0.06, lean.length() * 0.30)
	crown.scale = Vector3(1.0 + squash, 1.0 - squash * 0.35, 1.0 + squash)


func _sync_resources(definitions: Array) -> void:
	for definition_value in definitions:
		if not definition_value is Dictionary:
			continue
		var definition: Dictionary = definition_value
		var token := str(definition.get("resource", ""))
		if token.is_empty():
			continue
		_resources[token] = definition.duplicate(true)


func _mesh(entity: Dictionary) -> Mesh:
	var parameters: Array = entity.parameters
	if int(entity.type) == 0:
		var mesh := SphereMesh.new()
		mesh.radius = float(parameters[0])
		mesh.height = 2.0 * mesh.radius
		return mesh
	if int(entity.type) == 1:
		var mesh := BoxMesh.new()
		mesh.size = 2.0 * _vector(parameters)
		return mesh
	var surface := SurfaceTool.new()
	surface.begin(Mesh.PRIMITIVE_TRIANGLES)
	for triangle in entity.triangles:
		var a := _vector(entity.vertices[int(triangle[0])])
		var b := _vector(entity.vertices[int(triangle[1])])
		var c := _vector(entity.vertices[int(triangle[2])])
		var normal := (b - a).cross(c - a)
		if normal.length_squared() > 1e-20:
			normal = normal.normalized()
		# Godot treats clockwise triangles as front-facing in its 3D backend.
		# The scene contract stores outward ENU faces in the conventional
		# counter-clockwise order. Reverse only the submitted vertex order while
		# retaining the outward normal computed from the contract order.
		for vertex in [triangle[2], triangle[1], triangle[0]]:
			surface.set_normal(normal)
			surface.add_vertex(_vector(entity.vertices[int(vertex)]))
	return surface.commit()


func _material(rgb: Array, reflectance: float) -> StandardMaterial3D:
	var material := StandardMaterial3D.new()
	material.albedo_color = _color([float(rgb[0]) * reflectance, float(rgb[1]) * reflectance, float(rgb[2]) * reflectance])
	material.roughness = 0.9
	material.metallic_specular = 0.0
	material.cull_mode = BaseMaterial3D.CULL_DISABLED
	return material


func _terrain_material(rgb: Array, reflectance: float, detail: float) -> ShaderMaterial:
	var material := ShaderMaterial.new()
	material.shader = TerrainShader
	material.set_shader_parameter("base_color", Vector3(
		float(rgb[0]) * reflectance, float(rgb[1]) * reflectance, float(rgb[2]) * reflectance))
	material.set_shader_parameter("detail_strength", detail)
	return material


func _building_material(rgb: Array, reflectance: float, object_id: String) -> ShaderMaterial:
	var material := ShaderMaterial.new()
	material.shader = BuildingShader
	material.set_shader_parameter("base_color", Vector3(
		float(rgb[0]) * reflectance, float(rgb[1]) * reflectance, float(rgb[2]) * reflectance))
	var digest := object_id.hash()
	material.set_shader_parameter("seed", float(abs(digest % 997)))
	return material


func _groundcover_material(rgb: Array, reflectance: float, forest: bool) -> ShaderMaterial:
	var material := ShaderMaterial.new()
	material.shader = GroundcoverShader
	var tint := 0.76 if forest else 1.0
	material.set_shader_parameter("base_color", Vector3(
		float(rgb[0]) * reflectance * tint, float(rgb[1]) * reflectance * tint,
		float(rgb[2]) * reflectance * tint))
	material.set_shader_parameter("variation", 0.10 if forest else 0.16)
	return material


func _vector(value: Array) -> Vector3:
	return Vector3(float(value[0]), float(value[1]), float(value[2]))


func _enu(value: Array) -> Vector3:
	return Vector3(float(value[0]), float(value[2]), -float(value[1]))


func _basis(rotation: Array) -> Basis:
	return Basis(
		_enu([rotation[0], rotation[3], rotation[6]]),
		_enu([rotation[1], rotation[4], rotation[7]]),
		_enu([rotation[2], rotation[5], rotation[8]])
	)


func _color(rgb: Array) -> Color:
	# Linear protocol reflectance -> sRGB API colors -> sRGB RGB8 viewport.
	return Color(float(rgb[0]), float(rgb[1]), float(rgb[2])).linear_to_srgb()

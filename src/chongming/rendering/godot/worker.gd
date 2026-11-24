extends Node

## Passive V-Switch JSON push viewer (`chongming-viewer-v1`).
## Strictly passive: never runs scoring, simulation, or disk persistence.

const ViewerUi = preload("res://ui.gd")
const Visualization = preload("res://visualization.gd")

const MAX_VIEWER_FRAME_BYTES := 64 * 1024 * 1024
const VIEWER_CONNECT_TIMEOUT_MS := 5000

var _visualization := Visualization.new()
var _ui: Control
var _free_viewport: SubViewport
var _world: Node3D
var _environment: Environment
var _sun: DirectionalLight3D
var _free_camera: Camera3D
var _editor_display: TextureRect
var _editor_fps: Label
var _fps_elapsed := 0.0
var _looking := false
var _free_yaw := 0.0
var _free_pitch := 0.0

var _viewer_mode := true
var _viewer_host := "127.0.0.1"
var _viewer_port := 0
var _viewer_reconnect_ms := 250
var _viewer_peer: StreamPeerTCP
var _viewer_buffer := PackedByteArray()
var _viewer_expected_payload := -1
var _viewer_conn_state := "disconnected"
var _viewer_connect_started_ms := 0
var _viewer_next_reconnect_ms := 0
var _viewer_reconnect_count := 0
var _viewer_last_error := ""
var _viewer_camera_initialized := false


func _ready() -> void:
	DisplayServer.window_set_title("Chongming Viewer [V-Switch]")
	get_tree().auto_accept_quit = false
	get_viewport().disable_3d = true
	seed(42)
	if not _parse_arguments():
		return
	_setup_world()
	_start_viewer_mode()


func _setup_world() -> void:
	_free_viewport = SubViewport.new()
	_free_viewport.own_world_3d = true
	_free_viewport.size = Vector2i(1024, 640)
	_free_viewport.render_target_update_mode = SubViewport.UPDATE_DISABLED
	_free_viewport.transparent_bg = false
	add_child(_free_viewport)

	_world = Node3D.new()
	_free_viewport.add_child(_world)

	_free_camera = Camera3D.new()
	_free_camera.cull_mask = 3
	_free_camera.fov = 70.0
	_free_camera.far = 5000.0
	_free_camera.current = true
	_free_viewport.add_child(_free_camera)

	_visualization.attach(_world, _free_camera, _enu)

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
	_environment.background_mode = Environment.BG_SKY
	_environment.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	_environment.ambient_light_color = Color.WHITE
	_environment.ambient_light_energy = 0.6
	var sky := Sky.new()
	var sky_material := ProceduralSkyMaterial.new()
	sky.sky_material = sky_material
	_environment.sky = sky
	var environment_node := WorldEnvironment.new()
	environment_node.environment = _environment
	_world.add_child(environment_node)

	_sun = DirectionalLight3D.new()
	_sun.rotation_degrees = Vector3(-45.0, 35.0, 0.0)
	_sun.shadow_enabled = true
	_world.add_child(_sun)


func _parse_arguments() -> bool:
	var has_viewer := false
	var args := OS.get_cmdline_user_args()
	for index in range(args.size() - 1):
		if args[index] == "--viewer":
			has_viewer = true
			var spec := args[index + 1]
			var colon := spec.rfind(":")
			if colon > 0:
				_viewer_host = spec.substr(0, colon)
				_viewer_port = int(spec.substr(colon + 1))
			else:
				_viewer_port = int(spec)
		elif args[index] == "--viewer-host":
			has_viewer = true
			_viewer_host = args[index + 1]
		elif args[index] == "--viewer-port":
			has_viewer = true
			_viewer_port = int(args[index + 1])
		elif args[index] == "--reconnect-ms":
			_viewer_reconnect_ms = maxi(50, int(args[index + 1]))
	if not has_viewer or _viewer_port <= 0 or _viewer_host.is_empty():
		push_error("Godot Viewer requires valid --viewer <host:port> or --viewer-port")
		get_tree().quit(2)
		return false
	return true


func _open_ui() -> void:
	_ui = ViewerUi.new()
	_ui.worker = self
	add_child(_ui)
	_ui.build(_editor_display, _editor_fps)
	_editor_display.resized.connect(func() -> void:
		_free_viewport.size = Vector2i(_editor_display.size.max(Vector2.ONE))
		_redraw_freeview())
	DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_NO_FOCUS, false)
	DisplayServer.window_set_flag(DisplayServer.WINDOW_FLAG_BORDERLESS, false)
	DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_WINDOWED)
	DisplayServer.window_set_size(Vector2i(1440, 900))
	RenderingServer.render_loop_enabled = true


func _start_viewer_mode() -> void:
	if _ui == null:
		_open_ui()
	if DisplayServer.get_name() != "headless":
		DisplayServer.window_set_title("Chongming Viewer [V-Switch]")
	_editor_display.visible = true
	_editor_fps.visible = true
	_free_camera.make_current()
	_free_camera.position = Vector3(120.0, 120.0, 120.0)
	_free_camera.look_at(Vector3.ZERO, Vector3.UP)
	_free_yaw = _free_camera.rotation.y
	_free_pitch = _free_camera.rotation.x
	_connect_viewer()
	_refresh_viewer_ui()
	_redraw_freeview()


func _process(delta: float) -> void:
	if _editor_fps == null:
		return
	_move_free_camera(delta)
	_fps_elapsed += delta
	if _fps_elapsed >= 0.5:
		_editor_fps.text = "%d FPS · VIEWER" % Engine.get_frames_per_second()
		_fps_elapsed = 0.0
	_process_viewer()


func _notification(what: int) -> void:
	if what == NOTIFICATION_WM_CLOSE_REQUEST:
		_disconnect_viewer("")
		get_tree().quit()


func _connect_viewer() -> void:
	_viewer_buffer.clear()
	_viewer_expected_payload = -1
	var peer := StreamPeerTCP.new()
	var err := peer.connect_to_host(_viewer_host, _viewer_port)
	if err != OK:
		_viewer_peer = null
		_viewer_conn_state = "reconnecting"
		_viewer_last_error = "connect failed (%s)" % error_string(err)
		_viewer_next_reconnect_ms = Time.get_ticks_msec() + _viewer_reconnect_ms
		return
	_viewer_peer = peer
	_viewer_conn_state = "connecting"
	_viewer_connect_started_ms = Time.get_ticks_msec()


func _disconnect_viewer(reason: String) -> void:
	if _viewer_peer != null:
		_viewer_peer.disconnect_from_host()
		_viewer_peer = null
	_viewer_buffer.clear()
	_viewer_expected_payload = -1
	if not reason.is_empty():
		_viewer_last_error = reason
		_viewer_reconnect_count += 1
	_viewer_conn_state = "reconnecting"
	_viewer_next_reconnect_ms = Time.get_ticks_msec() + _viewer_reconnect_ms


func _process_viewer() -> void:
	if _viewer_peer == null:
		if Time.get_ticks_msec() >= _viewer_next_reconnect_ms:
			_connect_viewer()
			_refresh_viewer_ui()
		return
	_viewer_peer.poll()
	var status := _viewer_peer.get_status()
	if status == StreamPeerTCP.STATUS_CONNECTING:
		if Time.get_ticks_msec() - _viewer_connect_started_ms > VIEWER_CONNECT_TIMEOUT_MS:
			_disconnect_viewer("connect timeout")
			_refresh_viewer_ui()
		return
	if status != StreamPeerTCP.STATUS_CONNECTED:
		_disconnect_viewer("socket closed")
		_refresh_viewer_ui()
		return
	if _viewer_conn_state != "connected":
		_viewer_peer.set_no_delay(true)
		_viewer_conn_state = "connected"
		_viewer_last_error = ""
		_refresh_viewer_ui()
	var available := _viewer_peer.get_available_bytes()
	while available > 0:
		var packet := _viewer_peer.get_data(available)
		if packet[0] != OK:
			_disconnect_viewer("socket read error")
			_refresh_viewer_ui()
			return
		_viewer_buffer.append_array(packet[1])
		_viewer_peer.poll()
		available = _viewer_peer.get_available_bytes()
	var accepted_in_batch := _consume_viewer_frames()
	if accepted_in_batch > 0:
		if not _viewer_camera_initialized:
			_focus_viewer_overview()
		_visualization.redraw()
		_redraw_freeview()
		_refresh_viewer_ui()


func _consume_viewer_frames() -> int:
	var accepted := 0
	while true:
		if _viewer_expected_payload < 0:
			if _viewer_buffer.size() < 4:
				break
			var frame_len := (int(_viewer_buffer[0]) << 24) \
				| (int(_viewer_buffer[1]) << 16) \
				| (int(_viewer_buffer[2]) << 8) \
				| int(_viewer_buffer[3])
			if frame_len < 2 or frame_len > MAX_VIEWER_FRAME_BYTES:
				_disconnect_viewer("invalid frame length %d" % frame_len)
				_refresh_viewer_ui()
				break
			_viewer_expected_payload = frame_len
		if _viewer_buffer.size() < 4 + _viewer_expected_payload:
			break
		var payload := _viewer_buffer.slice(4, 4 + _viewer_expected_payload)
		_viewer_buffer = _viewer_buffer.slice(4 + _viewer_expected_payload)
		_viewer_expected_payload = -1
		var parsed = JSON.parse_string(payload.get_string_from_utf8())
		if not parsed is Dictionary:
			_viewer_last_error = "malformed JSON frame"
			continue
		if _visualization.receive_display_frame(parsed, true):
			accepted += 1
			_viewer_last_error = ""
		else:
			_viewer_last_error = _visualization.error
	return accepted


func _focus_viewer_overview() -> void:
	var points: Array = []
	for st in _visualization.station_states.values():
		points.append({"position": st["main_camera_position_enu_m"]})
	if _visualization.has_truth and not _visualization.latest_truth.is_empty():
		for target in _visualization.latest_truth.get("targets", []):
			points.append({"position": target["position_enu_m"]})
	for meas in _visualization.measurements.values():
		if meas.get("position_enu_m") != null:
			points.append({"position": meas["position_enu_m"]})
	if points.is_empty():
		return
	_position_editor_overview(points)
	_free_yaw = _free_camera.rotation.y
	_free_pitch = _free_camera.rotation.x
	_viewer_camera_initialized = true


func ui_focus_viewer() -> void:
	_viewer_camera_initialized = false
	_focus_viewer_overview()
	_redraw_freeview()


func _refresh_viewer_ui() -> void:
	if _ui == null:
		return
	_ui.show_viewer_state({
		"connection_state": _viewer_conn_state,
		"host": _viewer_host,
		"port": _viewer_port,
		"reconnect_count": _viewer_reconnect_count,
		"last_error": _viewer_last_error,
	}, _visualization)


func viewer_snapshot() -> Dictionary:
	return {
		"connection_state": _viewer_conn_state,
		"reconnect_count": _viewer_reconnect_count,
		"last_error": _viewer_last_error,
		"run_id": _visualization.current_run_id,
		"mode": _visualization.current_mode,
		"has_truth": _visualization.has_truth,
		"update_count": _visualization.update_count,
		"last_update_id": _visualization.last_update_id_str,
		"last_sequence_no": _visualization.last_sequence_no_str,
		"dropped_sequence_count": _visualization.dropped_sequence_count,
		"display_time_ns": _visualization.display_time_ns,
		"station_count": _visualization.station_states.size(),
		"ray_count": _visualization.rays.size(),
		"measurement_count": _visualization.measurements.size(),
		"track_count": _visualization.tracks.size(),
		"score_by_unit": _visualization.score_by_unit,
		"has_latest_truth": not _visualization.latest_truth.is_empty(),
		"voxel_count": _visualization._voxel_cell_count(),
	}


func _redraw_freeview() -> void:
	_free_viewport.render_target_update_mode = SubViewport.UPDATE_ONCE
	if _ui == null:
		RenderingServer.force_draw(true)


func _input(event: InputEvent) -> void:
	if event is InputEventMouseButton and event.button_index == MOUSE_BUTTON_RIGHT:
		if event.pressed and _ui != null and not (_editor_display.is_visible_in_tree()
				and _editor_display.get_global_rect().has_point(event.position)):
			return
		_looking = event.pressed
		if _looking:
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
		radius = maxf(80.0, maxf(
			Vector2(maximum.x - minimum.x, maximum.z - minimum.z).length() * 0.55,
			(maximum.y - minimum.y) * 0.6))
	var distance := radius * 1.4
	_free_camera.position = focus + Vector3(distance * 0.55, distance * 0.8, distance * 0.55)
	_free_camera.look_at(focus, Vector3.UP)


func ui_set_visualization_layer(key: String, enabled: bool) -> void:
	_visualization.set_layer_visible(key, enabled)
	_redraw_freeview()


func ui_select_measurement(id: String) -> void:
	if _visualization.select_measurement(id):
		_refresh_viewer_ui()
		_redraw_freeview()


func _enu(value: Array) -> Vector3:
	return Vector3(float(value[0]), float(value[2]), -float(value[1]))

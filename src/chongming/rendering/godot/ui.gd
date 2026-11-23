extends Control

## Passive V-Switch Viewer window (`chongming-viewer-v1`).
## Displays incoming DisplayFrame state and routes camera/layer actions.

const PANEL_WIDTH := 440

var worker: Node
var _viewer_conn_label: Label
var _viewer_mode_label: Label
var _viewer_focus_btn: Button
var _viewer_status_label: Label
var _panel: TabContainer
var _info: TextEdit
var _visualization_layers: Dictionary = {}
var _station_states: TextEdit
var _diagnostics: TextEdit
var _measurement_select: OptionButton
var _measurement_details: Label
var _voxel_status: Label


func build(freeview: TextureRect, freeview_fps: Label) -> void:
	set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	var background := Panel.new()
	background.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	add_child(background)
	var margin := MarginContainer.new()
	margin.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	for side in ["left", "top", "right", "bottom"]:
		margin.add_theme_constant_override("margin_" + side, 8)
	add_child(margin)
	var column := VBoxContainer.new()
	margin.add_child(column)
	column.add_child(_viewer_toolbar())
	var split := HSplitContainer.new()
	split.size_flags_vertical = Control.SIZE_EXPAND_FILL
	column.add_child(split)
	split.add_child(_freeview_pane(freeview, freeview_fps))
	_panel = TabContainer.new()
	_panel.custom_minimum_size.x = PANEL_WIDTH
	split.add_child(_panel)
	_info = _text_view()
	_panel.add_child(_info)
	_panel.add_child(_visualization_tab())
	_diagnostics = _text_view()
	_panel.add_child(_diagnostics)
	_panel.set_tab_title(0, "信息")
	_panel.set_tab_title(1, "算法图层")
	_panel.set_tab_title(2, "实时评分")


func set_viewer_mode(_enabled: bool) -> void:
	pass


func _button(text: String, pressed: Callable) -> Button:
	var button := Button.new()
	button.text = text
	button.pressed.connect(pressed)
	return button


func _viewer_toolbar() -> HBoxContainer:
	var bar := HBoxContainer.new()
	_viewer_conn_label = Label.new()
	_viewer_conn_label.text = "被动 Viewer · 等待连接"
	_viewer_mode_label = Label.new()
	_viewer_mode_label.text = "—"
	_viewer_focus_btn = _button("聚焦总览", func() -> void:
		if worker != null and worker.has_method("ui_focus_viewer"):
			worker.ui_focus_viewer())
	var hint := Label.new()
	hint.text = "右键拖拽转向 · WASD/QE 移动 · Shift 加速"
	_viewer_status_label = Label.new()
	_viewer_status_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_viewer_status_label.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	_viewer_status_label.clip_text = true
	for control in [_viewer_conn_label, VSeparator.new(), _viewer_mode_label, VSeparator.new(),
			_viewer_focus_btn, VSeparator.new(), hint, _viewer_status_label]:
		bar.add_child(control)
	return bar


func _freeview_pane(freeview: TextureRect, freeview_fps: Label) -> Control:
	var box := Control.new()
	box.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	freeview.reparent(box, false)
	freeview.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	freeview_fps.reparent(box, false)
	return box


func _visualization_tab() -> ScrollContainer:
	var scroll := ScrollContainer.new()
	scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	var box := VBoxContainer.new()
	box.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	scroll.add_child(box)
	for pair in [["truth", "真值：绿色圆圈、速度及视场连线"], ["rays", "边缘：黄色射线及独立角度 σ 圆锥"],
			["measurements", "中心：红色测量位置及尺寸圆"], ["tracks", "轨迹：历史及当前位置"],
			["voxels", "体素：无先验原始站点投票（线框）"]]:
		var check := CheckBox.new()
		var key: String = pair[0]
		check.text = pair[1]
		check.button_pressed = true
		check.toggled.connect(func(enabled: bool) -> void: worker.ui_set_visualization_layer(key, enabled))
		_visualization_layers[key] = check
		box.add_child(check)
	var legend := Label.new()
	legend.text = "仅自由视角，不写入传感器像素。\n射线：VALID 实线 / FROZEN 虚线 / STALE 点线\n轨迹：暂定浅红 / 确认深红 / 滑行橙 / 删除灰\n射线保留 1 s，测量及删除轨迹保留 3 s（实验时间）。\n射线按站点+轨迹仅保留最新一条；轨迹保留最近 100 点；速度箭头为 1 s 位移。\n测量圆直径 = max(σE, σN, σU)，仅尺寸示意。\n未知 σ 不绘制；角度 σ ≥ π/2 无法绘制圆锥。\n诊断缓存上限：射线 600 / 量测 200 / 航迹 300，超出时丢最旧。"
	legend.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	box.add_child(legend)
	_voxel_status = Label.new()
	_voxel_status.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	box.add_child(_voxel_status)
	_measurement_select = OptionButton.new()
	_measurement_select.clip_text = true
	_measurement_select.fit_to_longest_item = false
	_measurement_select.item_selected.connect(func(index: int) -> void:
		worker.ui_select_measurement(str(_measurement_select.get_item_metadata(index))))
	box.add_child(_measurement_select)
	_measurement_details = Label.new()
	_measurement_details.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	box.add_child(_measurement_details)
	_station_states = _text_view()
	_station_states.size_flags_vertical = Control.SIZE_EXPAND_FILL
	_station_states.custom_minimum_size.y = 140
	box.add_child(_station_states)
	return scroll


func show_visualization(measurements: Dictionary, selection: Dictionary, voxels: Variant, voxel_update: int) -> void:
	var ids: Array = measurements.keys()
	ids.sort()
	var selected: String = selection.measurement_id
	if selection.expired:
		ids.append(selected)
	ids.push_front("")
	var previous: Array = []
	for index in range(_measurement_select.item_count):
		previous.append(str(_measurement_select.get_item_metadata(index)))
	if previous != ids:
		_measurement_select.clear()
		for id in ids:
			_measurement_select.add_item("不选择测量" if id == "" else ("已过期：" if id == selected and selection.expired else "测量：") + id)
			_measurement_select.set_item_metadata(_measurement_select.item_count - 1, id)
	_measurement_select.select(ids.find(selected))
	if selected == "":
		_measurement_details.text = "选择测量后，以青色高亮缓存中尚存的支持射线。"
	elif selection.expired:
		_measurement_details.text = "测量 %s 已过期，无法显示；不重建支持射线。" % selected
	else:
		var measurement: Dictionary = measurements[selected]
		var ray_ids: Array = measurement["supporting_ray_event_ids"] if measurement.has("supporting_ray_event_ids") else measurement.get("supporting_ray_ids", [])
		_measurement_details.text = "%s · %s · 参考时刻 %d ns\n支持射线：可显示 %d / %d\n%s" % [selected,
			measurement.status, measurement.reference_time_ns, selection.available_ray_ids.size(),
			ray_ids.size(),
			"全部仍在缓存中。" if selection.missing_ray_ids.is_empty() else "部分或全部未接收或已过期，无法显示；不重建。"]
	if voxels == null:
		_voxel_status.text = "体素：尚无快照（或最后封口窗口未提供原始投票），不保留旧窗口。"
	else:
		var count: int = voxels["cells"].size() if voxels.has("cells") else voxels.get("scored_voxels", []).size()
		_voxel_status.text = "体素：更新 #%d · 最后封口窗口\n%d 个非零网格 · 最大站点票数 %d\n仅无先验搜索，0–2 票不显示；3–15 票 alpha 0.06–0.18；空快照会清除旧图。" % [
			voxel_update, count, int(voxels.max_score)]


func _show_station_states(stations: Dictionary) -> void:
	var lines := []
	for station in stations.values():
		var health := "—" if station.get("calibration_health") == null else str(station["calibration_health"]).to_upper()
		var usage := "—" if station.get("calibration_usage") == null else str(station["calibration_usage"]).to_upper()
		var ver := "—" if station.get("state_version") == null else str(station["state_version"])
		var sol := "—" if station.get("solution_id") == null else str(station["solution_id"])
		lines.append("%s · 健康 %s / 使用 %s\n  位置 ENU：%s m\n  状态版本：%s · 标定解：%s" % [
			station["station_id"], health, usage, _vector(station["main_camera_position_enu_m"], 2), ver, sol])
	_set_text(_station_states, "尚无站点状态" if lines.is_empty() else "\n\n".join(lines))


func _diagnostics_text(summary: Dictionary) -> String:
	var lines := []
	var has_truth_flag := bool(summary.get("has_truth", false))
	var scores: Dictionary = summary.get("score_by_unit", {})
	if not has_truth_flag:
		lines.append("实机/外场监测模式（has_truth = false）")
		lines.append("不接收真值与评分；仅显示在线观测、校准与中心输出。")
	elif scores.is_empty():
		lines.append("虚拟测试模式（has_truth = true）")
		lines.append("等待在线 Scorer 通过 V-Switch 推送 ScoreEvent…")
	else:
		lines.append("实时在线评分（由 chongming-scorer 推送，Godot 仅展示）")
		var units := scores.keys()
		units.sort()
		for unit in units:
			var ev: Dictionary = scores[unit]
			lines.append("\n[%s] run=%s · t=%d ns" % [unit, ev["run_id"], int(ev["experiment_time_ns"])])
			lines.append("  matched=%s · unmatched=%s · unevaluable=%s" % [
				ev["matched_count"], ev["unmatched_count"], ev["unevaluable_count"]])
			lines.append("  mean=%s · rmse=%s · max=%s (%s)" % [
				"—" if ev["mean_error"] == null else String.num(float(ev["mean_error"]), 6),
				"—" if ev["rmse"] == null else String.num(float(ev["rmse"]), 6),
				"—" if ev["max_error"] == null else String.num(float(ev["max_error"]), 6),
				unit])
			for extra in ev.get("extra_metrics", []):
				var val_str := "—" if extra["value"] == null else String.num(float(extra["value"]), 6)
				lines.append("  %s: %s %s" % [extra["name"], val_str, extra["unit"]])
	if summary.has("performance"):
		var perf: Dictionary = summary.performance
		lines.append("\n可视化 CPU 墙钟（最近最多 512 次；非 GPU 时间或帧率）")
		for key in ["update_ms", "redraw_ms"]:
			if perf.has(key):
				var timing: Dictionary = perf[key]
				lines.append("%s · n=%d · p50/p95/max ms: %s / %s / %s" % [key,
					timing.samples, str(timing.p50), str(timing.p95), str(timing.max)])
		lines.append("缓存：射线 %d · 测量 %d · 轨迹 %d · 体素 %d" % [
			perf.ray_count, perf.measurement_count, perf.track_count, perf.voxel_count])
	return "\n".join(lines)


func show_viewer_state(viewer_status: Dictionary, visualization: RefCounted) -> void:
	var conn_state := str(viewer_status.get("connection_state", "disconnected"))
	var endpoint := "%s:%d" % [str(viewer_status.get("host", "127.0.0.1")), int(viewer_status.get("port", 0))]
	var conn_color := Color("#52e888") if conn_state == "connected" else Color("#ff9d32")
	var conn_label := "已连接" if conn_state == "connected" else ("连接中" if conn_state == "connecting" else "重连中")
	_viewer_conn_label.text = "%s · %s" % [conn_label, endpoint]
	_viewer_conn_label.add_theme_color_override("font_color", conn_color)

	if visualization.current_run_id.is_empty():
		_viewer_mode_label.text = "等待 DisplayFrame (chongming-viewer-v1)"
		_viewer_status_label.text = "已接收 0 帧"
	else:
		var mode_tag := "实机模式 (LIVE · 无真值/评分)" if not visualization.has_truth else "虚拟测试 (%s · 含真值)" % visualization.current_mode.to_upper()
		_viewer_mode_label.text = "%s · run=%s" % [mode_tag, visualization.current_run_id]
		_viewer_status_label.text = "update=%s · seq=%s · 丢帧=%d · t=%.3f s" % [
			visualization.last_update_id_str,
			visualization.last_sequence_no_str,
			visualization.dropped_sequence_count,
			float(visualization.display_time_ns) / 1e9,
		]

	if _visualization_layers.has("truth"):
		var truth_check: CheckBox = _visualization_layers["truth"]
		if not visualization.current_run_id.is_empty() and not visualization.has_truth:
			truth_check.disabled = true
			truth_check.text = "真值：实机模式不可用"
		else:
			truth_check.disabled = false
			truth_check.text = "真值：绿色圆圈、速度及视场连线"

	_set_text(_info, _viewer_info_text(viewer_status, visualization))
	_show_station_states(visualization.station_states)
	show_visualization(
		visualization.measurements,
		visualization.selection_summary(),
		visualization.voxel_space,
		visualization.voxel_update_id,
	)
	_set_text(_diagnostics, _diagnostics_text({
		"has_truth": visualization.has_truth,
		"score_by_unit": visualization.score_by_unit,
		"performance": visualization.performance_summary(),
	}))


func _viewer_info_text(viewer_status: Dictionary, visualization: RefCounted) -> String:
	var lines := [
		"V-Switch 被动显示客户端 (chongming-viewer-v1)",
		"连接状态：%s (%s:%d)" % [
			str(viewer_status.get("connection_state", "disconnected")),
			str(viewer_status.get("host", "127.0.0.1")),
			int(viewer_status.get("port", 0)),
		],
		"重连次数：%d · 累计接收帧：%d · 序列间隙(丢帧)：%d" % [
			int(viewer_status.get("reconnect_count", 0)),
			int(visualization.update_count),
			int(visualization.dropped_sequence_count),
		],
	]
	if not str(viewer_status.get("last_error", "")).is_empty():
		lines.append("最近提示：%s" % str(viewer_status["last_error"]))
	if visualization.current_run_id.is_empty():
		lines.append("\n等待第一帧 chongming-viewer-v1 数据…")
		return "\n".join(lines)
	lines.append("")
	lines.append("Run ID：%s" % visualization.current_run_id)
	lines.append("模式：%s · has_truth=%s" % [visualization.current_mode, str(visualization.has_truth)])
	lines.append("update_id=%s · sequence_no=%s · experiment_time_ns=%d" % [
		visualization.last_update_id_str,
		visualization.last_sequence_no_str,
		visualization.display_time_ns,
	])
	lines.append("当前缓存：站点 %d · 射线 %d · 量测 %d · 航迹 %d" % [
		visualization.station_states.size(),
		visualization.rays.size(),
		visualization.measurements.size(),
		visualization.tracks.size(),
	])
	lines.append("")
	if not visualization.has_truth:
		lines.append("真值：实机模式未推送（truth / score 缺省）")
	elif visualization.latest_truth.is_empty():
		lines.append("真值：等待 TruthFrame…")
	else:
		var t: Dictionary = visualization.latest_truth
		lines.append("真值目标（step=%s · %d 个）：" % [t["step_index"], t["targets"].size()])
		for target in t["targets"]:
			lines.append("  %s [%s]  pos=%s m  vel=%s m/s" % [
				target["target_id"],
				target["target_class"],
				_vector(target["position_enu_m"], 2),
				_vector(target["velocity_enu_mps"], 2),
			])
	if not visualization.calibration_states.is_empty():
		lines.append("\n最新标定状态：")
		for sid in visualization.calibration_states:
			var cs: Dictionary = visualization.calibration_states[sid]
			lines.append("  %s v%s health=%s sol=%s" % [
				sid, cs["state_version"], cs["health"], str(cs["solution_id"])])
	if not visualization.edge_diagnostics.is_empty():
		lines.append("\n最近边缘诊断（最多 %d 条）：" % visualization.edge_diagnostics.size())
		var start_idx := maxi(0, visualization.edge_diagnostics.size() - 5)
		for idx in range(start_idx, visualization.edge_diagnostics.size()):
			var d: Dictionary = visualization.edge_diagnostics[idx]
			lines.append("  [%s] %s: %s" % [
				d["station_id"], d["category"], ", ".join(d["reason_codes"])])
	return "\n".join(lines)


func _text_view() -> TextEdit:
	var view := TextEdit.new()
	view.editable = false
	view.context_menu_enabled = true
	view.wrap_mode = TextEdit.LINE_WRAPPING_BOUNDARY
	return view


static func _vector(value: Variant, digits := 1) -> String:
	if not value is Array:
		return "—"
	var parts := []
	for item in value:
		parts.append(String.num(float(item), digits))
	return " / ".join(parts)


static func _set_text(view: TextEdit, text: String) -> void:
	# Replacing identical text would reset the reader's scroll and selection.
	if view.text != text:
		var scroll := view.scroll_vertical
		view.text = text
		view.scroll_vertical = scroll

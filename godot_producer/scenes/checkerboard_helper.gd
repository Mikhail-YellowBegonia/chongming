class_name CheckerboardHelper
extends RefCounted
## Builds a physically-scaled 7x5 A4 calibration checkerboard in Godot 3D and
## computes its 4 exact surveyed outer corners (`board-corner-0..3`) in ENU.

const ScalarMath = preload("res://core/scalar64_math.gd")

const BOARD_WIDTH_M := 0.297
const BOARD_HEIGHT_M := 0.210
const SQUARE_SIZE_M := 0.035
const COLS_SQUARES := 7
const ROWS_SQUARES := 5


static func create_board_texture() -> ImageTexture:
	# 2000 px/m => exact integer pixel borders & squares (594x420 px).
	var img_w := 594
	var img_h := 420
	var sq_px := 70
	var margin_x := 52
	var margin_y := 35
	var img := Image.create(img_w, img_h, false, Image.FORMAT_RGB8)
	img.fill(Color(0.94, 0.94, 0.94))
	for row in range(ROWS_SQUARES):
		for col in range(COLS_SQUARES):
			var c := Color(0.05, 0.05, 0.05) if (row + col) % 2 == 0 else Color(0.94, 0.94, 0.94)
			var rect := Rect2i(margin_x + col * sq_px, margin_y + row * sq_px, sq_px, sq_px)
			img.fill_rect(rect, c)
	return ImageTexture.create_from_image(img)


static func create_board_node(center_enu: Array, r_enu_from_aux: Array, tex: Texture2D) -> MeshInstance3D:
	var quad := QuadMesh.new()
	quad.size = Vector2(BOARD_WIDTH_M, BOARD_HEIGHT_M)

	var mat := StandardMaterial3D.new()
	mat.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	mat.albedo_texture = tex
	mat.texture_filter = BaseMaterial3D.TEXTURE_FILTER_LINEAR
	mat.cull_mode = BaseMaterial3D.CULL_DISABLED

	var mesh := MeshInstance3D.new()
	mesh.name = "CalibrationCheckerboard"
	mesh.mesh = quad
	mesh.material_override = mat
	# QuadMesh UV (0,0)->(1,1) maps to local (-W/2, +H/2)->(+W/2, -H/2),
	# aligning 1:1 with Camera3D basis (right, up=-down, -forward).
	mesh.transform = ScalarMath.godot_camera_transform(center_enu, r_enu_from_aux)
	return mesh


static func surveyed_landmarks_enu(
	center_enu: Array,
	r_enu_from_aux: Array,
	covariance_m2: float = 0.0
) -> Array:
	var right := [r_enu_from_aux[0], r_enu_from_aux[3], r_enu_from_aux[6]]
	var down := [r_enu_from_aux[1], r_enu_from_aux[4], r_enu_from_aux[7]]
	var hw := BOARD_WIDTH_M * 0.5
	var hh := BOARD_HEIGHT_M * 0.5
	var offsets := [
		[-hw, -hh], # p00 top-left
		[hw, -hh], # p10 top-right
		[hw, hh], # p11 bottom-right
		[-hw, hh], # p01 bottom-left
	]
	var cov := [
		covariance_m2, 0.0, 0.0,
		0.0, covariance_m2, 0.0,
		0.0, 0.0, covariance_m2,
	]
	var out := []
	for idx in range(4):
		var ox := float(offsets[idx][0])
		var oy := float(offsets[idx][1])
		var pt := ScalarMath.vec3_add(
			center_enu,
			ScalarMath.vec3_add(ScalarMath.vec3_scale(right, ox), ScalarMath.vec3_scale(down, oy))
		)
		out.append({
			"landmark_id": "board-corner-%d" % idx,
			"position_enu_m": pt,
			"covariance_enu_m2": cov,
			"visual_description": "A4 7x5 checkerboard outer corner %d" % idx,
		})
	return out

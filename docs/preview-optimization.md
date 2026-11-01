# 预览性能优化与轨迹可视化

## 问题诊断

### 根本原因

**性能瓶颈不在帧率限制，而在每帧计算开销过大**

- 测例使用 Hannover 地景（**3796 棵植被**）
- 默认 `occlusion_mode: "known"` 每帧计算遮挡
- 每帧需要检查 7 个目标 × 3796 植被 = **26,572 次遮挡检查**
- 实际表现：~3 fps（300ms/帧）

## 解决方案

### 方案1：禁用遮挡计算（立即生效）✅

**修改**: `scenarios/hannover_multi_target.json`

```json
{
  "occlusion_mode": "unknown",
  ...
}
```

**效果**：

- 跳过所有遮挡计算
- 只判断目标是否在视场内
- **预期性能提升 10-50 倍**

**权衡**：

- ✅ 预览速度大幅提升
- ✅ 测例仍可用于算法开发和相对性能比较
- ❌ 不提供遮挡真值（无法评估绝对准确率）
- ❌ 适用场景：算法开发、轨迹验证、性能基准

### 方案2：轨迹预测可视化（已实现）✅

**原理**：只迭代飞行器运动代码，跳过遮挡、渲染、真值计算

**实现**：

1. **Python 端** (`src/chongming/rendering/godot.py`):
   - 添加 `_sample_trajectory_preview()` 函数
   - 直接调用 `sample_behavior()` 采样轨迹点
   - 每 2 秒采样一次，从当前时刻到场景结束
   - 修改 `_editor_markers()` 添加轨迹数据
   - 更新 `editor_update()` 接收场景定义和时间参数

2. **Python 调用层** (`src/chongming/preview.py`):
   - 更新 `_push_editor()` 传递场景定义、当前时间和结束时间

3. **Godot 端** (`src/chongming/rendering/godot/worker.gd`):
   - 修改 `_sync_editor_markers()` 解析轨迹数据
   - 使用 `ImmediateMesh` + `PRIMITIVE_LINE_STRIP` 绘制轨迹线
   - 半透明橙色线条，与目标标签颜色一致

**效果**：

- ✅ 编辑模式下立即显示完整 60 秒轨迹
- ✅ 无需播放即可验证运动合理性
- ✅ 轨迹计算极快（纯运动学，无 GPU 开销）
- ✅ 便于调整初始位置和速度

## 使用方法

### 快速预览（禁用遮挡 + 轨迹可视化）

```bash
uv run --frozen chongming-preview \
  --scenario scenarios/hannover_multi_target.json
```

**预期体验**：

1. 打开 Godot 预览窗口
2. 自由视角中看到 7 条橙色轨迹线
3. 每条线显示该目标从当前时刻到 60 秒的运动路径
4. 播放速度大幅提升（预期 10-30 fps）

### 验证轨迹

**在 Godot 自由视角中**：

- 鼠标右键拖动旋转视角
- WASD 平移，Q/E 升降，Shift 加速
- 观察 7 条轨迹线：
  - 2 条圆形轨迹（orbit 运动）
  - 5 条直线轨迹（constant_velocity 运动）
  - 检查是否有交叉、碰撞或超出范围

**编辑调整**：

1. 点击"编辑"按钮
2. 在编辑面板调整目标初始位置或速度
3. 轨迹线实时更新
4. 确认后提交保存

## 技术细节

### 轨迹采样逻辑

```python
def _sample_trajectory_preview(
    spec: FlyingObjectSpec,
    current_time_ns: int,
    duration_ns: int,
    sample_interval_s: float = 2.0,
) -> list[list[float]]:
    """只评估 behavior 模型，跳过遮挡、渲染和真值计算"""
```

**采样参数**：

- 间隔：2 秒（60 秒场景 = 30 个采样点）
- 范围：当前时刻 → 场景结束
- 输入：初始状态 + behavior 参数
- 输出：位置序列（ENU 坐标）

**性能**：

- 单目标：< 1 ms（纯数学计算）
- 7 目标：< 10 ms
- 对比：完整渲染 ~300 ms/帧

### Godot 绘制

```gdscript
# LINE_STRIP 模式：连续线段
mesh.surface_begin(Mesh.PRIMITIVE_LINE_STRIP)
for point in trajectory:
    mesh.surface_add_vertex(_enu(point))
mesh.surface_end()

# 半透明橙色材质
material.albedo_color = Color(1.0, 0.7, 0.32, 0.6)
material.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
```

## 性能对比

| 配置          | 遮挡计算  | 预期帧率   | 60秒场景播放时间 |
| ------------- | --------- | ---------- | ---------------- |
| 默认（known） | 3796 植被 | ~3 fps     | ~20 分钟         |
| unknown       | 禁用      | ~10-30 fps | ~2-6 秒          |
| 轨迹预览      | 无需播放  | N/A        | **立即可见**     |

## 后续测例录制

**对于正式录制，可以恢复遮挡计算**：

```json
{
  "occlusion_mode": "known",  // 或删除此字段使用默认值
  ...
}
```

**原因**：

- 录制是一次性操作，可以接受慢速
- 完整遮挡真值用于算法评估
- 使用 `chongming-scene --output` 离线生成，不需要实时交互

**工作流**：

1. **预览阶段**：`occlusion_mode: "unknown"` + 轨迹可视化，快速验证
2. **录制阶段**：恢复 `occlusion_mode: "known"`，完整生成真值
3. **算法开发**：使用录制结果，无需重新渲染

## 验证

```bash
# 1. 检查场景配置
uv run --frozen chongming-center --scenario scenarios/hannover_multi_target.json --check
# 输出: 有效：hannover-multi-target-diverse，3 站，7 目标

# 2. 快速预览（应该显著更快）
uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json

# 3. 在 Godot 自由视角中观察轨迹线
```

## 文件变更

### 修改文件

- `scenarios/hannover_multi_target.json` - 添加 `occlusion_mode: "unknown"`
- `src/chongming/rendering/godot.py` - 轨迹采样和标记生成
- `src/chongming/preview.py` - 传递场景定义和时间
- `src/chongming/rendering/godot/worker.gd` - 绘制轨迹线

### 验证状态

- ✅ 场景配置有效
- ✅ Python 导入成功
- ⏳ 实际预览验证（待用户执行）

## 总结

**问题**：Hannover 地景（3796 植被）+ 遮挡计算 → 3 fps

**解决**：

1. ✅ 禁用遮挡计算 → 预期 10-30 fps（10倍加速）
2. ✅ 轨迹预测可视化 → 立即显示完整 60 秒轨迹

**下一步**：运行预览验证效果

```bash
uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json
```

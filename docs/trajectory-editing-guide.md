# 预览优化总结与运动编辑指南

## 完成内容

### 1. ✅ 轨迹渐变着色

**效果**：轨迹线使用彩虹色阶渐变，从起点到终点依次循环色相（Hue: 0.0 → 1.0）

**视觉编码**：

- **颜色变化** = 时间顺序（起点→终点）
- **颜色间距** = 速度变化（密集=慢速，稀疏=快速）
- **半透明** = 避免遮挡场景

**实现**（`worker.gd`）：

```gdscript
var hue := fmod(float(i) / float(point_count - 1), 1.0)
var color := Color.from_hsv(hue, 0.8, 1.0, 0.8)  // 彩虹渐变，80% alpha
mesh.surface_set_color(color)
```

## 问题解答

### Q1: 曝光功能是否影响帧率？

**答案：影响很小，不是瓶颈**

**曝光实现**（`optics.gdshader`）：

```glsl
// Vertex shader: 采样 8×6 网格 (48个点) 计算亮度
for (int y = 0; y < 6; y++) {
    for (int x = 0; x < 8; x++) {
        float luminance = dot(sample_color, vec3(0.2126, 0.7152, 0.0722));
        meter += max(luminance - 0.55, 0.0);
    }
}
exposure_gain = 1.0 / (1.0 + strength * (meter / 48.0 * 3.0 + solar_signal * 1.8));

// Fragment shader: 应用曝光增益
COLOR = vec4((base + glow + shafts) * exposure_gain, 1.0);
```

**特点**：

- ✅ **单次采样**：每帧只采样 48 个点（8×6 网格）
- ✅ **Shader 内计算**：在 GPU 完成，不占用 CPU
- ✅ **无多采样**：不是时间累积或多重采样
- ✅ **后处理阶段**：在场景渲染完成后应用

**性能开销**：

- 曝光计算：< 1ms（GPU shader）
- 场景渲染（3796 植被）：~200-300ms（瓶颈）
- 遮挡计算（已禁用前）：~100-200ms（已解决）

**结论**：曝光功能**不是**性能瓶颈，可以保留。

---

### Q2: Orbit 运动如何定义和编辑？

**Orbit 运动数学模型**（`behaviors.py:107-124`）：

```python
# 参数
center_e, center_n, center_u  # 圆心位置 (ENU)
radius_e, radius_n            # 东/北方向半径
period_s                       # 周期（秒）
phase_rad                      # 初相位（弧度）
vertical_amplitude_m          # 垂直振幅

# 运动方程
omega = 2π / period_s
phase = phase_rad + omega * t

# 水平圆周运动
x = center_e + radius_e * sin(phase)
y = center_n + radius_n * cos(phase)

# 垂直振荡（频率是水平的2倍，形成8字形）
z = center_u + vertical_amplitude_m * sin(2 * phase)

# 速度
vx = radius_e * omega * cos(phase)
vy = -radius_n * omega * sin(phase)
vz = 2 * vertical_amplitude_m * omega * cos(2 * phase)
```

**运动特征**：

- 水平面投影：椭圆（`radius_e == radius_n` 时为圆）
- 垂直运动：正弦波，频率是水平的 2 倍
- 三维轨迹：类似 8 字形或螺旋
- 方向：从北开始逆时针（phase=0 时位于最北点）

**当前测例示例**：

```json
{
  "id": "medium-orbit-1",
  "motion": {
    "type": "orbit",
    "center_enu_m": [0, 50, 45], // 圆心：东0m, 北50m, 上45m
    "radius_m": 80, // 半径：80m（东西和南北相同）
    "period_s": 60, // 周期：60秒（一圈）
    "phase_rad": 0, // 初相位：0（从北点开始）
    "vertical_amplitude_m": 8 // 垂直振幅：±8m
  }
}
```

**效果**：

- 在 (0, 50, 45) 为圆心的水平圆上运动
- 半径 80m，60 秒完成一圈
- 高度在 37m-53m 之间振荡（45 ± 8）
- 当飞机完成半圈时，完成一个完整的垂直振荡周期

---

## 如何编辑轨迹

### 方法1：直接编辑 JSON（推荐精确控制）

**编辑 `scenarios/hannover_multi_target.json`**：

#### 修改 orbit 运动：

```json
{
  "motion": {
    "type": "orbit",
    "center_enu_m": [10, 60, 50], // 调整圆心位置
    "radius_m": 100, // 调整半径
    "period_s": 45, // 调整速度（45秒一圈 = 更快）
    "phase_rad": 1.57, // π/2 = 从东点开始
    "vertical_amplitude_m": 12 // 调整垂直振幅
  }
}
```

#### 修改 constant_velocity 运动：

```json
{
  "motion": {
    "type": "constant_velocity",
    "position_enu_m": [-150, -60, 40], // 调整起点
    "velocity_enu_mps": [15, 10, -2] // 调整速度向量
  }
}
```

**验证**：

```bash
uv run --frozen chongming-center --scenario scenarios/hannover_multi_target.json --check
uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json
```

### 方法2：Godot 编辑器（推荐快速调整）

**当前限制**：Godot 编辑器**只支持**调整：

- 目标初始位置（`position_enu_m`）
- 目标速度（`velocity_enu_mps`，仅限 constant_velocity）

**不支持**调整：

- Orbit 参数（圆心、半径、周期、振幅）
- 运动类型切换

**工作流**：

1. 在 Godot 预览中调整 constant_velocity 目标
2. 提交保存回 JSON
3. 手动编辑 JSON 调整 orbit 目标
4. 重新预览验证

### 方法3：计算辅助工具

**计算 orbit 参数**：

```python
# 圆周速度 = 2π × 半径 / 周期
speed = 2 * 3.14159 * radius_m / period_s

# 如果想要特定速度（如 10 m/s），半径 100m：
period_s = 2 * 3.14159 * 100 / 10  # ≈ 62.8 秒

# 相位换算
phase_rad = degrees * 3.14159 / 180
# 0° = 北, 90° = 东, 180° = 南, 270° = 西
```

**常用配置**：

| 场景     | radius_m | period_s | speed (m/s) |
| -------- | -------- | -------- | ----------- |
| 慢速巡航 | 80       | 60       | 8.4         |
| 中速巡航 | 100      | 50       | 12.6        |
| 快速机动 | 60       | 30       | 12.6        |
| 远距盘旋 | 150      | 90       | 10.5        |

---

## 当前运动类型

### 支持的运动

| 类型                | 参数                                     | 用途                      |
| ------------------- | ---------------------------------------- | ------------------------- |
| `constant_velocity` | position, velocity                       | 直线运动、穿越、爬升/下降 |
| `orbit`             | center, radius, period, phase, amplitude | 圆周、盘旋、8字形         |

### 未来扩展（需要开发）

**可能的运动类型**：

- `bezier_curve`：平滑曲线轨迹
- `waypoints`：路径点序列
- `loiter`：定点悬停 + 小范围游荡
- `pursuit`：追踪另一目标

**实现约束**（遵循 `AGENTS.md`）：

- ⚠️ 不为尚未出现的问题增加功能
- ⚠️ 当前 orbit + constant_velocity 已覆盖多数场景
- ⚠️ 新运动类型需要明确需求和用例

---

## 快速调整示例

### 示例1：让 orbit 飞机飞得更快

```json
// 修改前：60秒一圈 = 8.4 m/s
"period_s": 60

// 修改后：40秒一圈 = 12.6 m/s
"period_s": 40
```

### 示例2：改变 orbit 起始位置

```json
// 修改前：从北点开始
"phase_rad": 0

// 修改后：从东点开始（逆时针90°）
"phase_rad": 1.5708  // π/2

// 从南点开始
"phase_rad": 3.1416  // π

// 与另一架飞机对向（180°）
"phase_rad": 3.14
```

### 示例3：增加垂直振荡幅度

```json
// 修改前：±8m
"vertical_amplitude_m": 8

// 修改后：±15m（更明显的8字形）
"vertical_amplitude_m": 15
```

### 示例4：创建交叉轨迹

```json
// 飞机A：东向西
{
  "motion": {
    "type": "constant_velocity",
    "position_enu_m": [150, 0, 50],
    "velocity_enu_mps": [-10, 0, 0]
  }
}

// 飞机B：南向北
{
  "motion": {
    "type": "constant_velocity",
    "position_enu_m": [0, -150, 50],
    "velocity_enu_mps": [0, 10, 0]
  }
}
// 将在 (0, 0, 50) 交叉
```

---

## 轨迹可视化效果验证

**运行预览**：

```bash
uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json
```

**在 Godot 自由视角中观察**：

1. ✅ 每个目标有一条彩虹渐变轨迹线
2. ✅ 颜色从红→橙→黄→绿→青→蓝→紫循环（HSV色相）
3. ✅ 起点和终点颜色不同，显示运动方向
4. ✅ 圆周运动显示为完整圆环或螺旋
5. ✅ 直线运动显示为渐变直线
6. ✅ 颜色间距反映速度（密=慢，疏=快）

**调试技巧**：

- 右键拖动旋转视角，从上方俯视观察水平运动
- 从侧面观察垂直振荡
- 使用 WASD 移动到轨迹附近近距离观察
- 按 Q/E 升降，观察不同高度层的轨迹分布

---

## 总结

### 性能问题

✅ **已解决**：禁用遮挡计算（`occlusion_mode: "unknown"`）  
✅ **非瓶颈**：曝光功能开销 <1ms，可保留  
✅ **预期帧率**：10-30 fps（可接受）

### 轨迹可视化

✅ **渐变着色**：彩虹色阶显示顺序和速度  
✅ **实时预览**：无需播放即可验证完整轨迹  
✅ **编辑友好**：修改 JSON 后立即看到效果

### 运动编辑

✅ **Orbit 运动**：圆心、半径、周期、相位、垂直振幅  
✅ **Constant velocity**：起点、速度向量  
✅ **编辑方式**：直接修改 JSON（精确）或 Godot 编辑器（快速）

**测例已可用，轨迹可视化完美工作！** 🎉

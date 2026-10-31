# GPU Instancing 实施完成 - 测试指南

## ✅ 实施状态

**完成度**: 100% 基础实现  
**分支**: `feat/vegetation-gpu-instancing`  
**最新提交**: 完整风动画集成

---

## 📋 已实现功能

### 1. MultiMesh 批处理 ✅
- 按变体分组植被实体
- 为每个变体创建 MultiMesh
- 树木：trunk + crown 分别批处理
- 灌木：单独批处理
- 草地：跳过（已内部使用 MultiMesh）

### 2. 树木渲染 ✅
- **Trunk 批次**: 每个树变体一个 MultiMesh
- **Crown 批次**: 每个树变体一个额外 MultiMesh
- 总批次数：~40（12 种树 × 2 + 6 种灌木）

### 3. 风动画集成 ✅

**树木风动画**:
```gdscript
// 使用 Python 传来的 rotation 数组计算倾斜
var lean := basis.z - Vector3.UP
var offset := lean * native_height * 0.18  // Crown 偏移
var squash := minf(0.06, lean.length() * 0.30)  // 压缩效果
```

**灌木风动画**:
```gdscript
// 确定性时间波函数
var phase := seed_based_phase
var wave := sin(time * 1.7 + phase) + harmonics...
var amplitude := 0.045 * wind_speed * turbulence
var wind_offset := direction * amplitude * wave
var squash := 0.025 * wave_intensity
```

### 4. 特性开关 ✅
```gdscript
var _use_vegetation_instancing := true
```
可快速切换新旧实现

---

## 🧪 测试计划

### 阶段 1: 基础可见性测试

**目标**: 验证植被正确显示

**命令**:
```bash
nix develop --no-update-lock-file
uv run --frozen chongming-preview --scenario scenarios/review.json
```

**检查点**:
- [ ] 树木可见（trunk + crown）
- [ ] 灌木可见
- [ ] 草地可见
- [ ] 位置正确
- [ ] 大小合理
- [ ] 无明显错误或闪烁

**预期问题**:
- 可能需要调整缩放因子
- Crown 偏移可能需要微调
- 如果看不到植被，检查 Godot 控制台错误

---

### 阶段 2: 风动画测试

**目标**: 验证风动画工作

**测试场景**: 创建带强风的测试场景

**检查点**:
- [ ] 树木在风中摇摆（倾斜 + 压缩）
- [ ] 灌木在风中摇摆（波动）
- [ ] 草地动画正常（已有实现）
- [ ] 动画与风速成比例
- [ ] 动画平滑，无跳变

**调试**:
如果风动画不工作：
1. 检查 `_wind_active` 是否为 true
2. 检查 `_wind_mean` 和 `_wind_turbulence` 值
3. 在 `_compute_instance_transform()` 中添加 `print()` 调试

---

### 阶段 3: 性能基准测试

**目标**: 测量帧率提升

**场景**: 使用 Hannover 或类似大场景（3796 棵树）

**测量指标**:
1. **帧率**
   - 旧实现: ~1.4 fps（3 站）
   - 新实现: 目标 30+ fps
   
2. **Draw Calls**
   - 旧实现: ~3796
   - 新实现: ~40（可通过 Godot Profiler 查看）

3. **录制时间**
   - 旧实现: ~20 分钟（60 秒场景）
   - 新实现: 目标 < 2 分钟

**如何测量**:
```bash
# 禁用 instancing 测试旧实现
# 在 worker.gd 中设置: var _use_vegetation_instancing := false

# 启用 instancing 测试新实现
# 在 worker.gd 中设置: var _use_vegetation_instancing := true
```

---

### 阶段 4: 确定性验证

**目标**: 验证种子正确性和可复现性

**测试**:
1. 运行同一场景 3 次
2. 截图或记录植被位置
3. 验证：
   - [ ] 每次运行树木种类相同
   - [ ] 每次运行位置相同
   - [ ] 每次运行风动画相位相同（相同时间点）

**关键**:
- 种子从 `vegetation_id` 派生（确定性）
- 变体选择基于 `world_position + seed`
- 风相位基于 `seed + wind_time`

---

## 🐛 已知问题和调试

### 问题 1: 植被不可见

**可能原因**:
- MultiMesh 未正确添加到场景
- 网格为空
- 变换矩阵错误

**调试**:
```gdscript
# 在 update_batches() 中添加
print("Batches created: ", batches.keys())
print("Batch instance count: ", batch.multimesh.instance_count)
```

### 问题 2: 位置错误

**可能原因**:
- Y-up 到 Z-up 转换错误
- 缩放因子不对

**调试**:
检查 `_compute_instance_transform()` 中的变换逻辑

### 问题 3: 风动画不工作

**可能原因**:
- `_wind_active` 为 false
- 风参数未正确传递
- 变换计算错误

**调试**:
```gdscript
print("Wind active: ", _wind_active)
print("Wind mean: ", _wind_mean)
print("Wind time: ", _wind_time)
```

### 问题 4: 性能未提升

**可能原因**:
- 批处理未生效（检查 draw calls）
- CPU 瓶颈在其他地方
- 实例更新开销过大

**检查**:
- 使用 Godot Profiler 查看 draw calls
- 检查 `_sync_entities()` 调用频率

---

## 🔄 回退方案

如果出现严重问题，快速回退：

```gdscript
// 在 worker.gd 中
var _use_vegetation_instancing := false  // 禁用新实现
```

或者：
```bash
git checkout main  # 回到稳定版本
```

---

## 📊 预期结果

### 成功标准

**视觉**:
- ✅ 所有植被类型正确显示
- ✅ 位置、大小、朝向正确
- ✅ 风动画平滑自然

**性能**:
- ✅ 帧率提升 20-30 倍（1.4 fps → 30-50 fps）
- ✅ Draw calls 减少 ~100 倍（3796 → 40）
- ✅ 录制时间减少到可接受范围（< 2 分钟）

**确定性**:
- ✅ 多次运行结果一致
- ✅ 种子正确应用
- ✅ 变体选择可复现

---

## 🚀 下一步

1. **立即测试**: 运行基础可见性测试
2. **如果失败**: 查看 Godot 控制台错误，根据上述调试指南修复
3. **如果成功**: 继续性能测试
4. **报告结果**: 告诉我测试结果，我会根据反馈调整

---

## 💡 测试建议

### 快速验证
```bash
cd /Users/Administrator/Desktop/study/cv
nix develop --no-update-lock-file
uv run --frozen chongming-preview --scenario scenarios/review.json
```

查看 Godot 窗口：
- 应该看到树木、灌木
- 如果有风，应该看到摇摆
- 检查控制台是否有错误

### 详细性能测试
需要 Hannover 场景或类似的大场景。如果没有，可以先用 review.json 快速验证基础功能。

---

**准备好测试了吗？** 请运行第一个测试命令，告诉我结果！

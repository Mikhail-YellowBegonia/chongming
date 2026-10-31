# GPU Instancing 验证测试说明

## ✅ Issue #41 已修复

**修复内容**: 在 `_show_ui()` (line 339) 中保持 GPU Instancing 调试标记

## 🧪 现在可以进行性能测试

### 快速验证

```bash
cd /Users/Administrator/Desktop/study/cv
nix develop --no-update-lock-file
uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json
```

### 预期结果

如果 GPU Instancing 正常工作，你应该看到：

1. **窗口标题**: `Chongming 预览 [GPU INSTANCING]` ✅
2. **FPS 显示**: `X FPS · GPU: 1886 inst / 20 batch · EDIT` ✅
3. **性能提升**: FPS 应该从 3 fps 显著提升到 20-50+ fps ✅

### 关键指标

| 指标 | 之前 | 预期 | 实际 |
|------|------|------|------|
| 窗口标题 | "Chongming 预览" | "Chongming 预览 [GPU INSTANCING]" | ? |
| FPS 显示 | 无 GPU 信息 | 显示 "GPU: X inst / Y batch" | ? |
| 帧率 (3站) | ~3 fps | 20-50+ fps | ? |
| Draw Calls | ~1886 | ~40 | ? |

---

## 📊 性能对比测试

### 测试 1: GPU Instancing 启用（默认）

```bash
# 当前分支已启用
uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json
```

**记录**:
- FPS: ___
- GPU 统计: ___ inst / ___ batch
- 流畅度: ___

### 测试 2: GPU Instancing 禁用（对比）

```bash
# 修改 worker.gd line 53
var _use_vegetation_instancing := false

# 重新运行
uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json
```

**记录**:
- FPS: ___
- GPU 统计: 无（禁用）
- 流畅度: ___

### 计算提升

- **FPS 提升**: 启用 FPS / 禁用 FPS = ___x
- **预期提升**: 10-20x
- **达标**: ✅ / ❌

---

## 🎯 成功标准

**最低要求**:
- ✅ 窗口标题包含 `[GPU INSTANCING]`
- ✅ FPS 显示包含 GPU 统计信息
- ✅ FPS ≥ 15 (相比 3 fps 提升 5x)

**理想目标**:
- ✅ FPS ≥ 30 (提升 10x)
- ✅ GPU 统计: ~1886 inst / ~20 batch
- ✅ 视觉无异常（树木、灌木正确显示）
- ✅ 风动画正常工作

---

## 🐛 如果仍然失败

### 检查点 1: 窗口标题

**如果标题没有 `[GPU INSTANCING]`**:
- 代码可能仍未生效
- 检查是否有其他地方覆盖了标题
- 运行 `./verify_gpu_instancing.sh` 诊断

### 检查点 2: FPS 显示

**如果没有 "GPU:" 统计信息**:
- `_vegetation_instance_count` 可能为 0
- 检查植被是否被识别为 "vegetation" category
- 查看 Godot 日志确认 `vegetation_instancing.gd` 是否被调用

### 检查点 3: 性能

**如果 FPS 仍然很低 (< 10)**:
- GPU Instancing 可能没有真正工作
- 或者有其他性能瓶颈
- 检查 Draw Calls 是否真的降低了（需要 Godot Profiler）

---

## 📝 请报告结果

运行测试后，请告诉我：

1. **窗口标题**: 是否显示 `[GPU INSTANCING]`？
2. **FPS 显示**: 是否显示 GPU 统计信息？具体数值？
3. **实际帧率**: 多少 FPS？
4. **性能提升**: 相比之前提升了多少倍？
5. **视觉效果**: 树木、灌木、风动画是否正常？

**我会根据你的反馈继续优化或解决问题！** 🚀

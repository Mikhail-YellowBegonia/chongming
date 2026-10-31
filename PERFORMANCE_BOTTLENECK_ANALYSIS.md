# 性能瓶颈诊断

## 当前状况

**测试结果**: 
- FPS: 1.4 → 3 fps
- 提升: 仅约 2 倍（预期 20-35 倍）

## 需要确认的信息

我已添加调试日志，请重新运行测试：

```bash
cd /Users/Administrator/Desktop/study/cv
nix develop --no-update-lock-file
uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json
```

**请查看 Godot 控制台输出**，寻找以下信息：

```
[Worker] _sync_entities called with X entities
[Worker] Grouped into Y vegetation variants
[VegetationInstancing] Processing Z vegetation instances across Y variants
[Worker] After filtering, W non-vegetation entities remain
```

## 可能的原因

### 假设 1: 植被数量少
- 场景中可能没有 3796 棵树
- 从截图看：**1121 树 / 765 灌木**（约 1886 个植被）
- 如果只有 1886 个植被，性能提升会受限

### 假设 2: 批处理未生效
- GPU Instancing 可能没有真正工作
- 需要确认调试日志显示批处理数量

### 假设 3: 其他瓶颈
从性能截图分析：

**实际渲染时间**: 
- **帧率轮盘 p50**: 801.06 ms (约 1.2 fps)
- **场景对象**: 869 地物 + 1121 树 + 765 灌木

**可能的其他瓶颈**:
1. **地形渲染**: 869 个地物可能也是独立节点
2. **帧率限制**: 可能有帧率锁或 VSync 限制
3. **CPU 瓶颈**: 场景更新逻辑开销
4. **图形驱动**: 软件渲染或驱动问题

## 下一步诊断

### 步骤 1: 查看调试日志

运行测试后，告诉我日志中的数字：
- 有多少个 entities？
- 有多少个 vegetation variants？
- 有多少个 vegetation instances？

### 步骤 2: 检查 Draw Calls

如果可能，打开 Godot Profiler（Monitor 标签页），查看：
- **Draw Calls**: 应该从 ~1886 降到 ~40
- **Vertices**: 顶点数
- **Objects**: 渲染对象数

### 步骤 3: 对比测试

禁用 instancing 重新测试：

```bash
# 编辑 worker.gd (第 53 行)
var _use_vegetation_instancing := false

# 重新运行
uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json
```

记录 FPS，然后改回 `true` 再测试。

## 预期结果分析

如果日志显示：
- **正常批处理**: "Processing 1886 vegetation instances across 20 variants"
  - 说明批处理工作了
  - 但植被数量比预期少（1886 vs 3796）
  - 2 倍提升可能是合理的（因为还有其他渲染开销）

- **批处理失败**: "Processing 0 vegetation instances"
  - 说明植被没有被识别为 "vegetation" category
  - 需要检查 Python 端数据

- **批处理部分工作**: "Processing 200 vegetation instances"
  - 说明大部分植被被跳过了
  - 需要检查 group_vegetation_entities() 逻辑

---

**请重新运行测试并告诉我调试日志的输出！** 这将帮助我定位真正的瓶颈。

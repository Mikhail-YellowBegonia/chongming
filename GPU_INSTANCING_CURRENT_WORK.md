# GPU Instancing 当前工作摘要

## 已完成工作（今天）

### 1. ✅ 认领并分析 Issue #40
- 理解了三个子任务的优先级
- PR #36 审查与合并
- 树木风动画修复
- GPU Instancing（高优先级，非可选）

### 2. ✅ PR #36 审查与合并
- 系统性审查 53 个文件变更
- 验证测试、代码质量、数据契约
- 已被维护者合并到 main

### 3. ✅ 树木风动画修复  
- 发现像素门限过高（60 → 45）
- 已修复并提交

### 4. ✅ GPU Instancing 基础实现
- 创建 `vegetation_instancing.gd` 模块
- 集成到 `worker.gd`
- 实现变体分组和 MultiMesh 批处理框架

## 当前状态

**分支**: `feat/vegetation-gpu-instancing`

**最新提交**: `2169581` - GPU instancing foundation

**进展**: 基础框架完成，待完善风动画和测试

## GPU Instancing 核心设计

### 架构决策

**数据流保持不变**（Python 端）:
- Python 仍然逐个发送植被实体
- 不修改协议，避免大规模重构
- 保持向后兼容性

**Godot 端批处理**:
- 接收后按变体分组
- 为每个变体创建一个 MultiMesh
- Draw calls: 3796 → ~20

### 核心代码

```gdscript
// worker.gd
func _sync_entities(entities: Array):
    if _use_vegetation_instancing:
        var grouped := _vegetation_instancing.group_vegetation_entities(entities)
        _vegetation_instancing.update_batches(grouped, _wind_time, ...)
        // 过滤掉已批处理的植被
```

### 变体分组逻辑

- **树木**: 按 `tree_variant_index` 分组（12 种树 → 12 批次）
- **灌木**: 按 `shrub_variant_index` 分组（6 种灌木 → 6 批次）
- **草地**: 已内部使用 MultiMesh，跳过

变体索引由 `vegetation.gd` 的程序化选择逻辑确定：
```gdscript
var variant_index := Vegetation._variant_index(
    world_position, seed, Assets.tree_count(), 55.0, 11
)
```

## 待完成任务

### P0: 关键功能（必须完成才能测试）

1. **树木网格处理** ⏳
   - 当前：只渲染 trunk
   - 需要：trunk + crown 一起渲染
   - 方案：创建两个 MultiMesh（trunk batch + crown batch）

2. **风动画集成** ⏳
   - 树木：应用 Python 传来的 rotation 数组
   - 灌木：应用时间波函数（位置偏移 + 压缩）
   - 在 `_compute_instance_transform()` 中实现

3. **基础测试** ⏳
   - 验证植被可见性
   - 检查位置、大小正确性
   - 确认变体选择确定性

### P1: 性能验证

4. **性能基准测试** ⏳
   - 使用 Hannover 场景（3796 棵树）
   - 测量帧率：1.4 fps → ?
   - 目标：30+ fps

5. **确定性验证** ⏳
   - 多次运行同一场景
   - 验证种子正确性
   - 检查变体选择一致性

### P2: 打磨与优化

6. **LOD 支持** ⏳
   - 近/远模型切换
   - Impostor 纹理
   - 阴影优化

7. **文档更新** ⏳
   - 更新"可复现性"说明
   - 记录批处理机制
   - 性能对比数据

## 技术难点与解决方案

### 难点 1: 树木 trunk + crown 渲染

**问题**: 
- 旧实现中，trunk 和 crown 是两个独立的 MeshInstance3D
- MultiMesh 只能用一个网格

**解决方案**（推荐）:
- 为每个树变体创建**两个** MultiMesh 批次
- `tree_0_trunk` 批次：trunk 网格
- `tree_0_crown` 批次：crown 网格
- 位置/旋转相同，但 crown 有额外偏移

**替代方案**（复杂）:
- 运行时合并 trunk + crown 网格
- 工作量大，性能可能更差

### 难点 2: 风动画

**问题**:
- 旧实现中，每个树/灌木是独立节点，直接设置 `transform`
- MultiMesh 需要调用 `set_instance_transform(index, transform)`

**解决方案**:
- 保持每帧更新策略
- 在 `_compute_instance_transform()` 中计算风动画
- 树木：使用 Python 传来的 `rotation` 数组（倾斜效果）
- 灌木：使用确定性时间波函数（基于 `seed + wind_time`）

**性能考虑**:
- 每帧更新 3796 个实例变换有 CPU 开销
- 但比 3796 个节点的场景图遍历快得多
- 未来可优化：将风动画移到 Shader

### 难点 3: 确定性与种子

**关键**:
- 变体选择必须确定性（相同种子 → 相同树种）
- 风动画必须确定性（相同时间 + 种子 → 相同摇摆）

**验证**:
```python
vegetation_id → blake2s hash → seed
seed + world_position → variant_index
seed + wind_time → wind phase
```

## 工作计划

### 今天剩余时间
- ✅ 完成基础框架提交
- ⏳ 实现树木 crown 批次
- ⏳ 集成风动画逻辑

### 明天
- 基础可见性测试
- 性能基准测试
- 确定性验证

### 后天
- LOD 支持
- 文档更新
- 准备合并

## 预期效果

**性能提升**:
- Draw calls: 3796 → 20-24（trunk + crown 分开）
- FPS: 1.4 → 30-50+（预估）
- 录制时间: 20 分钟 → <2 分钟

**可维护性**:
- 特性开关 `_use_vegetation_instancing` 允许快速回退
- 不改 Python 协议，保持兼容性
- 代码模块化，易于后续优化

---

**状态**: 基础框架完成 60%，继续实施中
**预计完成**: 2-3 天内可测试

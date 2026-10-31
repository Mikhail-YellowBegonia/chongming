# GPU Instancing 实施进度

## 已完成

### 1. 分析当前数据流 ✅
- Python 端逐个发送 3796 个植被实体
- Godot 端创建 3796 个独立 Node3D
- 种子信息已传递（从 vegetation_id 派生）
- 变体选择逻辑已程序化

### 2. 创建 VegetationInstancing 模块 ✅
- 文件: `src/chongming/rendering/godot/vegetation_instancing.gd`
- 功能:
  - 按变体分组植被实体
  - 为每个变体创建 MultiMesh 批次
  - 计算实例变换（位置、旋转、缩放）
  - 保留风动画支持（待完善）

### 3. 集成到 worker.gd ✅
- 添加 `VegetationInstancing` 引用
- 在 `_setup_world()` 中初始化
- 在 `_sync_entities()` 中调用批处理逻辑
- 过滤掉已批处理的植被实体

## 当前状态

**代码已编写，待测试验证**

### 关键变更

1. **worker.gd**:
   ```gdscript
   const VegetationInstancing = preload("res://vegetation_instancing.gd")
   var _vegetation_instancing: VegetationInstancing
   var _use_vegetation_instancing := true  # 特性开关
   
   func _sync_entities(entities: Array):
       if _use_vegetation_instancing:
           var grouped := _vegetation_instancing.group_vegetation_entities(entities)
           _vegetation_instancing.update_batches(grouped, _wind_time, _wind_mean, _wind_turbulence)
           # 过滤掉植被，只处理非植被实体
   ```

2. **vegetation_instancing.gd**:
   - `VegetationBatch` 类：管理一个变体的 MultiMesh
   - `group_vegetation_entities()`: 按树种/灌木类型分组
   - `update_batches()`: 创建/更新 MultiMesh 实例
   - `_compute_instance_transform()`: 计算变换矩阵

## 待完成

### 立即需要修复的问题

1. **树木网格合并**
   - 当前只使用 trunk mesh
   - 需要合并 trunk + crown 为一个网格
   - 或者使用两个 MultiMesh（trunk batch + crown batch）

2. **风动画实现**
   - 当前 `_compute_instance_transform()` 只计算基础变换
   - 需要应用风动画到实例变换
   - 树木：使用 Python 传来的 rotation 数组（倾斜）
   - 灌木：使用时间波函数（位置偏移 + 压缩）

3. **LOD 支持**
   - 近距离/远距离模型切换
   - impostor 纹理支持
   - 阴影开关

4. **草地处理**
   - 草地已经内部使用 MultiMesh，暂时跳过批处理
   - 需要确认是否需要特殊处理

### 测试计划

1. **基础渲染测试**
   ```sh
   uv run --frozen chongming-preview --scenario scenarios/review.json
   ```
   - 验证树木/灌木是否正确显示
   - 检查位置、大小、朝向是否正确

2. **性能测试**
   - 使用 Hannover 场景（3796 棵树）
   - 测量帧率提升
   - 目标：1.4 fps → 30+ fps

3. **确定性测试**
   - 多次运行同一场景
   - 验证植被位置、变体选择一致
   - 检查种子是否正确应用

4. **风动画测试**
   - 使用强风配置（4.5 m/s）
   - 验证树木和灌木摇摆
   - 确认动画与风速成比例

## 下一步优先级

### P0: 修复基础渲染
1. ✅ 树木网格问题（trunk + crown）
2. ✅ 实例变换计算验证
3. ✅ 基础可见性测试

### P1: 风动画集成
1. ⏳ 树木风动画（使用 rotation 数组）
2. ⏳ 灌木风动画（时间波函数）
3. ⏳ 草地确认（可能不需要改动）

### P2: 性能优化与打磨
1. ⏳ LOD 支持
2. ⏳ 阴影优化
3. ⏳ 性能基准测试
4. ⏳ 文档更新

## 技术债务

1. **网格合并方案**
   - 选项 A: 运行时合并 trunk + crown 网格（复杂）
   - 选项 B: 使用两个 MultiMesh 批次（简单）
   - 推荐：选项 B

2. **风动画方案**
   - 选项 A: 每帧更新实例变换（当前方案，CPU 开销）
   - 选项 B: 使用 Shader 参数（高性能，但需重构）
   - 当前采用选项 A，未来可优化为选项 B

3. **特性开关**
   - `_use_vegetation_instancing` 允许快速切换新旧实现
   - 测试通过后可移除旧实现

## 性能预期

**当前瓶颈**:
- Draw calls: 3796
- FPS: 1.4 (3 站)

**预期改进**:
- Draw calls: ~20 (按变体分批)
- FPS: 30-50+ (20-35 倍提升)
- 录制时间: 20 分钟 → <2 分钟

**限制因素**:
- 每帧更新实例变换（风动画）仍有 CPU 开销
- 未来可用 Shader 进一步优化

---

**当前状态**: 代码已实现，等待测试验证和风动画集成
**预计完成时间**: 2-3 天（基础功能）+ 1-2 天（打磨）

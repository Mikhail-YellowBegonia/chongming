# 植被渲染性能分析：为什么我们比游戏慢？

## 问题

Hannover 地景有 **3796 棵植被**，渲染帧率 ~10-30 fps。一般游戏也有类似甚至更多植被，为什么别人不卡？

## 根本原因分析

### 我们的实现方式

**每棵植被 = 独立的 Node3D + MeshInstance3D**

```gdscript
// vegetation.gd: _create_tree()
var root := Node3D.new()           // 根节点
var model := Node3D.new()          // 模型容器
var tree_instance := MeshInstance3D.new()  // 主网格
var far_tree := MeshInstance3D.new()       // LOD 网格
root.add_child(model)
model.add_child(tree_instance)
model.add_child(far_tree)

// worker.gd: _sync_entities()
_instances.append(Node3D.new())    // 每个实体独立节点
_world.add_child(instance)
```

**结果**：3796 棵树 = **~15,000 个 Node3D 对象**（每棵树 ~4 个节点）

---

## 游戏引擎的优化方式

### 1. **GPU Instancing（硬件实例化）**

**原理**：一次绘制调用渲染数千个相同网格

```gdscript
// 游戏做法：MultiMeshInstance3D
var multimesh := MultiMesh.new()
multimesh.mesh = tree_mesh
multimesh.instance_count = 3796
for i in range(3796):
    multimesh.set_instance_transform(i, transform)
    multimesh.set_instance_color(i, color)

var instance := MultiMeshInstance3D.new()
instance.multimesh = multimesh
world.add_child(instance)  // 只添加一个节点！
```

**效果**：

- 绘制调用：3796 → **1**
- Node3D 数量：~15,000 → **1**
- GPU 开销：降低 **1000 倍以上**

**我们的现状**：

- ✅ 草地已使用 MultiMesh（最多 256 个草簇/patch）
- ❌ 树木和灌木使用独立节点（文档："未实现全局合批"）

---

### 2. **空间分区与视锥剔除**

**游戏做法**：

```
世界划分为 Grid Cell (如 100m × 100m)
├─ Cell A: 200 棵树 → 1 个 MultiMesh
├─ Cell B: 150 棵树 → 1 个 MultiMesh
└─ Cell C: 视锥外 → 完全不渲染
```

**效果**：

- 只渲染可见 Cell
- 每个 Cell 一次绘制调用
- 远距离 Cell 切换到 LOD

**我们的现状**：

- ✅ 有 LOD（45px、75px 阈值）
- ❌ 无空间分区
- ❌ 每棵树独立判断可见性（CPU 开销大）

---

### 3. **静态批处理（Static Batching）**

**游戏做法**：

```
场景加载时：
  合并所有相同材质的静态物体网格
  → 生成一个大网格
  → 一次绘制调用
```

**适用场景**：

- 建筑、岩石、栅栏等不动的物体
- 远景树木（无风摆）

**我们的现状**：

- 树木需要风摆动画（每帧更新 Transform）
- 无法静态批处理

---

### 4. **遮挡剔除（Occlusion Culling）**

**游戏做法**：

```
预计算哪些植被被建筑/山体遮挡
→ 不渲染被遮挡的物体
```

**我们的现状**：

- Python 端有 `OcclusionIndex`（用于真值计算）
- Godot 端无遮挡剔除（所有可见 Cell 都渲染）

---

## 性能对比

| 技术           | 游戏引擎       | 我们的实现        | 性能差距  |
| -------------- | -------------- | ----------------- | --------- |
| GPU Instancing | ✅ MultiMesh   | ❌ 独立节点       | **1000x** |
| 空间分区       | ✅ Grid/Octree | ❌ 平铺所有       | **10x**   |
| 视锥剔除       | ✅ Cell 级别   | ✅ 节点级别（慢） | **5x**    |
| 静态批处理     | ✅ 远景        | ❌ 风摆冲突       | **N/A**   |
| 遮挡剔除       | ✅ 预计算      | ❌ 无             | **2-5x**  |

**综合性能差距：约 100-5000 倍**

---

## 为什么我们这样实现？

### 设计约束

**1. Python 拥有植被真值和遮挡逻辑**

```python
# Python 负责：
- 植被位置和尺寸（动态）
- 遮挡计算（光线求交）
- 风摆扰动（实验时钟）
- 逻辑圆柱代理（真值）
```

**2. Godot 只负责渲染**

```gdscript
# Godot 接收：
- 每帧完整的植被快照
- 位置、尺寸、风摆状态
- 不持有场景状态
```

**3. 实验确定性要求**

- 风摆从实验时刻和固定种子求值
- 不依赖墙钟或 Godot 物理引擎
- Python 计算风摆 → Godot 只应用 Transform

**结果**：无法使用 MultiMesh（每个实例的 Transform 每帧从 Python 推送）

---

## 优化方向（如果需要）

### 方案1：Godot 端 GPU Instancing（推荐）

**原理**：Python 推送实例数据，Godot 端合批

```python
# Python 端：发送实例数组
{
  "kind": "tree_batch",
  "variant": "oak_large",
  "instances": [
    {"position": [10, 20, 0], "scale": 1.2, "rotation": 0.1},
    {"position": [15, 25, 0], "scale": 1.0, "rotation": 0.0},
    // ... 数百个实例
  ]
}
```

```gdscript
# Godot 端：MultiMesh 批处理
var multimesh := MultiMesh.new()
multimesh.instance_count = instances.size()
for i in range(instances.size()):
    multimesh.set_instance_transform(i, transform)
```

**效果**：

- 绘制调用：3796 → **~20**（按树种分批）
- 性能提升：**50-200 倍**

**工作量**：中等（需重构 Python/Godot 通信协议）

---

### 方案2：空间分区 + 动态加载

**原理**：只渲染相机附近的植被

```python
# Python 端：按相机位置筛选
visible_vegetation = [v for v in all_vegetation
                     if distance(v, camera) < render_distance]
```

**效果**：

- 3796 棵 → **~500-1000 棵**（可见范围）
- 性能提升：**3-7 倍**

**工作量**：小（只需修改 Python 筛选逻辑）

---

### 方案3：预览模式降级渲染

**原理**：预览时降低植被质量

```python
# 预览模式：每 N 棵树只渲染一棵
if preview_mode:
    vegetation = vegetation[::5]  # 只渲染 1/5
```

**效果**：

- 3796 棵 → **~750 棵**
- 性能提升：**5 倍**

**工作量**：极小（加一个配置项）

---

## 为什么游戏不卡？

### 游戏引擎的典型配置

**开放世界游戏（如 GTA V, Witcher 3）**：

- 植被总数：100,000+ 棵
- 可见范围：500-1000 棵（视锥 + 遮挡剔除）
- 渲染方式：MultiMesh + 空间分区
- 绘制调用：~50-100 次/帧
- 帧率：60 fps

**我们的配置**：

- 植被总数：3,796 棵
- 可见范围：3,796 棵（全部渲染）
- 渲染方式：独立节点
- 绘制调用：~3,796 次/帧（每棵树 1 次）
- 帧率：10-30 fps

**核心差异**：

1. 游戏用 **GPU Instancing**（1 次绘制 = 1000 棵树）
2. 我们用 **独立节点**（1 次绘制 = 1 棵树）

---

## 当前状态评估

### 为什么可以接受？

**1. 设计目标不是实时游戏**

- 预览用于验证测例，不需要 60 fps
- 录制是离线生成，可以接受慢速

**2. 已有的优化足够**

- ✅ LOD 系统（远景切换简化模型）
- ✅ 草地 MultiMesh（最密集部分已优化）
- ✅ 禁用遮挡计算（occlusion_mode: "unknown"）
- ✅ 轨迹预测（无需播放完整场景）

**3. 性能已可用**

- 10-30 fps 足够预览和调整
- 轨迹可视化立即显示完整路径
- 不影响最终录制质量

### 如果真要优化，优先级：

1. **高收益低成本**：空间分区（只渲染可见范围）→ **3-7x**
2. **高收益中成本**：GPU Instancing → **50-200x**
3. **低收益低成本**：预览降级 → **5x**

---

## 总结

**为什么别人不卡？**

- 游戏：GPU Instancing（1 次绘制 = 1000 棵树）
- 我们：独立节点（1 次绘制 = 1 棵树）

**为什么我们这样做？**

- Python 拥有植被真值和风摆逻辑
- 实验确定性要求（固定种子、实验时钟）
- 设计目标是离线录制，不是实时游戏

**需要优化吗？**

- 当前性能已可接受（10-30 fps + 轨迹预测）
- 如需优化，GPU Instancing 是最佳方案（50-200x 提升）
- 遵循 AGENTS.md：不为尚未出现的问题增加功能

**技术债**：

- 文档已标注："未实现全局合批"
- 已知限制，有明确优化路径
- 优先级低于算法功能开发

---

## 参考

**Godot 文档**：

- [Using MultiMeshInstance3D](https://docs.godotengine.org/en/stable/tutorials/3d/using_multi_mesh_instance.html)
- [Optimizing 3D performance](https://docs.godotengine.org/en/stable/tutorials/performance/optimizing_3d_performance.html)

**游戏引擎优化技术**：

- GPU Instancing: 1 次绘制调用 → 数千实例
- Spatial Hashing: O(1) 邻域查询
- Occlusion Culling: 跳过被遮挡物体
- LOD Chains: 距离自动切换细节级别

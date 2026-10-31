# GPU Instancing 实施计划

## 当前数据流分析

### Python 端（`rendering/godot.py`）

**现状**：逐个发送植被实体
```python
for vegetation_item in scene.vegetation:  # 3796 次循环
    visual = _vegetation_visual(vegetation_item, scene.render_effects)
    entities.append(_entity(
        _geometry_type(vegetation_item.geometry),
        vegetation_item.base_pose.position_enu_m,  # 每个位置
        vegetation_item.base_pose,                  # 每个旋转
        vegetation_item.geometry,
        vegetation_item.appearance,
        object_id=str(vegetation_item.vegetation_id),
        visual=visual,  # 包含 seed, kind, height, radius
        category="vegetation",
    ))
```

**发送的数据**（每个植被实体）:
- `type`: 几何类型（圆柱体）
- `position`: [x, y, z]
- `rotation`: [9 个浮点数]
- `color`: [r, g, b]
- `visual`: 
  - `seed`: 从 vegetation_id 派生的种子
  - `kind`: "tree_trunk", "tree_canopy", "shrub", "grass"
  - `height`, `radius` (或 `width`, `depth`, `density`)
  - `lod_near`, `lod_far`

### Godot 端（`worker.gd`）

**现状**：逐个创建节点
```gdscript
func _sync_entities(entities: Array) -> void:
    for entity in entities:  # 3796 次循环
        var instance = Vegetation.create(visual, rgb, reflectance, world_position)
        _instances.append(instance)  # 3796 个独立 Node3D
        _world.add_child(instance)
```

---

## 关键发现

### ✅ 已有的优势

1. **种子已传递**: `visual["seed"]` 从 `vegetation_id` 派生
2. **变体选择已程序化**: `vegetation.gd` 使用 `_variant_index()` 基于位置+种子
3. **风动画已有旋转数据**: Python 传递 `rotation` 数组

### ❌ 当前问题

1. **每帧发送 3796 个完整实体**（位置、旋转、颜色、几何）
2. **创建 3796 个独立 Node3D**（巨大的场景图开销）
3. **逐个应用风动画**（虽然计算简单，但循环开销大）
4. **3796 次绘制调用**（GPU 瓶颈）

---

## GPU Instancing 设计方案

### 核心思想：按变体批处理

**不再**：发送 3796 个独立实体  
**改为**：发送批次定义 + 实例数据

```python
# 伪代码
vegetation_batches = {
    "tree_variant_0": {
        "mesh_id": "oak_tree",
        "instances": [
            {"position": [x, y, z], "rotation": [...], "scale": s},
            # ... 200 个该变体的实例
        ]
    },
    "tree_variant_1": { ... },
    # 约 20 个批次（12 种树 + 6 种灌木 + 草地）
}
```

### 三种实施方案对比

#### 方案 A: 完全批处理（激进，最高性能）

**优点**:
- 最少数据传输（只发送实例变换）
- 最高性能（20 draw calls）
- 符合"程序化生成"原则

**缺点**:
- 需要重构 Python 端数据流
- 风动画需要重新设计（Shader 或 SkeletonIK）
- 工作量最大（2-4 周）

#### 方案 B: 保守批处理（推荐）

**Python 端**: 
- 保持现有协议，但添加批次提示
- 发送时按变体分组（减少 JSON 体积）

**Godot 端**:
- 接收后按变体构建 MultiMesh
- 每帧更新实例变换（风动画）
- 使用 `MultiMesh.set_instance_transform()`

**优点**:
- 最小化协议变更
- 保留现有风动画逻辑
- 快速实施（1 周）
- 性能提升显著（20 draw calls）

**缺点**:
- 每帧仍需更新变换（CPU 开销，但可接受）
- 未完全利用程序化生成

#### 方案 C: 混合方案

**静态植被**: 完全批处理（远距离树木）  
**动态植被**: 逐实例更新（近距离有风动画的树木）

**优点**: 平衡性能和灵活性  
**缺点**: 复杂度最高

---

## 推荐实施：方案 B（保守批处理）

### 阶段 1: Godot 端 MultiMesh（本周）

**不改 Python 协议**，只改 Godot 接收逻辑：

```gdscript
# worker.gd 新增
var _vegetation_batches: Dictionary = {}  # variant_id -> MultiMeshInstance3D

func _sync_entities(entities: Array) -> void:
    # 按变体分组
    var grouped := _group_vegetation_by_variant(entities)
    
    # 为每个变体创建/更新 MultiMesh
    for variant_id in grouped.keys():
        var batch = grouped[variant_id]
        _update_multimesh_batch(variant_id, batch.instances)
```

**预期收益**: 
- Draw calls: 3796 → 20
- 帧率: 1.4 fps → 30-50 fps（预估）

### 阶段 2: 优化数据传输（可选，未来）

Python 端发送批次而非逐个实体：

```python
# 新协议（可选）
request["vegetation_batches"] = [
    {
        "variant": "tree_0",
        "mesh": "oak",
        "instances": [[x,y,z,qw,qx,qy,qz,sx,sy,sz], ...]  # 紧凑格式
    }
]
```

---

## 实施检查清单

### 第一步：分析现有代码 ✅
- [x] 理解 Python 数据流
- [x] 理解 Godot 接收逻辑
- [x] 确认种子和变体选择机制

### 第二步：Godot MultiMesh 实现
- [ ] 修改 `worker.gd::_sync_entities()`
- [ ] 添加变体分组逻辑
- [ ] 创建 MultiMesh 批次管理
- [ ] 实现每帧实例变换更新（风动画）
- [ ] 处理 LOD 切换

### 第三步：测试验证
- [ ] 视觉一致性（相同种子 → 相同结果）
- [ ] 风动画工作正常
- [ ] 性能测试（帧率提升）
- [ ] 确定性验证（多次运行结果一致）

### 第四步：文档更新
- [ ] 更新"可复现性"说明
- [ ] 记录批处理机制
- [ ] 性能对比数据

---

## 可复现性共识更新

### 旧理解（已废弃）
> 必须记录每个对象的完整状态才能复现

### 新理解（正确）
> **程序化生成 + 种子 = 确定性复现**
> 
> - 只需传递：种子、规则、参数
> - Godot 用相同种子生成相同结果
> - 批量渲染不影响确定性

**关键原则**:
- 种子派生自 `vegetation_id`（唯一标识）
- 变体选择基于 `world_position + seed`（确定性函数）
- 风动画基于 `wind_time + seed`（确定性时间函数）

这意味着：
1. ✅ 可以使用 MultiMesh 批处理
2. ✅ 不需要记录每棵树的实例 ID
3. ✅ 风动画可以用 Shader 或预计算
4. ✅ 性能不与确定性冲突

---

## 下一步

1. **立即开始**: 实现 Godot 端 MultiMesh 批处理
2. **预期时间**: 3-5 天完成基础实现
3. **验证目标**: 1.4 fps → 30+ fps

准备好开始实施吗？

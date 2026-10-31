# 静态场景缓存方案

## 🎯 目标

将植被数据只发送一次，每帧只发送风参数和动态对象（飞机），实现 **100-200 倍性能提升**。

---

## 📊 当前瓶颈确认

**每帧发送的数据**:
- 1886 个植被实体（完整的位置、旋转、几何、外观）
- 869 个地物
- 8 个飞机
- 地形数据
- **总计**: ~2755 个对象，~500KB JSON

**瓶颈**:
- Python 序列化: ~200ms
- TCP 传输: ~100ms
- Godot 解析: ~200ms
- **总计**: ~500ms/帧 = **2 fps 上限**

---

## ✅ 方案：静态场景缓存

### 数据分类

| 类型 | 示例 | 变化频率 | 发送策略 |
|------|------|---------|---------|
| **静态** | 植被、地形、建筑 | 永不变 | 初始化时发送一次 |
| **半静态** | 相机位置 | 每帧变化 | 每帧发送（小数据） |
| **动态** | 飞机位置、姿态 | 每帧变化 | 每帧发送 |
| **环境参数** | 风场、光照 | 每帧变化 | 每帧发送（小数据） |

### 实施方案

#### 阶段 1: 协议扩展（1-2 天）

**修改 `godot.py` 渲染协议**:

```python
# 新增请求类型
class RenderRequest:
    type: Literal["init", "update"]
    
# 初始化请求（场景启动时）
init_request = {
    "type": "init",
    "static_scene": {
        "vegetation": [...],  # 1886 个
        "terrain": [...],
        "buildings": [...]
    },
    "environment": {
        "wind_model": {...},  # 风场配置
        "lighting": {...}
    }
}

# 更新请求（每帧）
update_request = {
    "type": "update",
    "time_ns": 1234567890,
    "wind": {
        "mean_velocity_enu_mps": [3.5, 2.8, 0],
        "turbulent_std": [1.2, 1.0, 0.3]
    },
    "dynamic_objects": [
        {"id": "aircraft/1", "position": [...], "rotation": [...]},
        # 只有 8 个飞机
    ],
    "camera": {...}
}
```

#### 阶段 2: Godot 端场景管理（1-2 天）

**修改 `worker.gd`**:

```gdscript
var _static_scene_loaded := false
var _static_entities: Dictionary = {}  # id -> Node3D

func _process_request(request: Dictionary):
    if request.type == "init":
        _load_static_scene(request.static_scene)
        _static_scene_loaded = true
    elif request.type == "update":
        _update_dynamic_objects(request.dynamic_objects)
        _update_wind(request.wind)
        _update_camera(request.camera)
        
func _update_wind(wind_params: Dictionary):
    _wind_mean = Vector2(wind_params.mean_velocity_enu_mps[0], ...)
    _wind_turbulence = wind_params.turbulent_std[0]
    # 风动画会在 _process() 中自动应用到植被
```

#### 阶段 3: 风动画 Godot 端实现（已完成 ✅）

**已有代码** (`vegetation_instancing.gd`):
```gdscript
func _compute_instance_transform(data, is_crown):
    # 使用 _wind_time, _wind_mean, _wind_turbulence
    # 实时计算风动画，不依赖 Python
```

**关键**: 风动画已经在 Godot 端计算！只需要传递风参数。

---

## 📈 预期性能提升

| 指标 | 当前 | 缓存后 | 提升 |
|------|------|--------|------|
| 每帧对象数 | 2755 | 8-10 | 275x |
| JSON 大小 | ~500KB | ~2KB | 250x |
| 序列化时间 | ~200ms | ~1ms | 200x |
| 网络传输 | ~100ms | ~0.5ms | 200x |
| Godot 解析 | ~200ms | ~1ms | 200x |
| **总帧时间** | **500ms (2fps)** | **2.5ms (400fps)** | **200x** |
| **实际帧率** | **3 fps** | **60+ fps** | **20x+** |

**注意**: 实际帧率还受其他因素限制（图像处理、算法计算），但渲染瓶颈将被完全消除。

---

## 🔍 风动画确认

### 问题：风动画数据从哪来？

让我检查当前实现：

1. **Python 是否每帧计算风旋转？**
2. **还是发送静态 base_pose？**

如果 Python 只发送 `base_pose`（静态朝向），那太好了！风动画已经是 Godot 端计算的。

如果 Python 每帧重新计算旋转（含风效果），需要重构为 Godot 端计算。

---

## 🚀 实施优先级

### 立即（Issue #40 完成）
- ✅ GPU Instancing 实现
- ✅ 风动画 Godot 端实现
- ⏳ 验证性能（当前进行中）

### 下一步（新 Issue: 静态场景缓存）
1. **确认风动画数据源**（Python 还是 Godot？）
2. **协议设计**（init + update 两种请求）
3. **Python 端实现**（分离静态/动态数据）
4. **Godot 端实现**（场景缓存管理）
5. **测试验证**（预期 20-60 fps）

---

## ❓ 需要确认

**关键问题**: Python 的 `base_pose.rotation` 是否包含风效果？

- **如果是静态的** → 太好了！只需实现缓存协议
- **如果每帧变化** → 需要将风计算移到 Godot（中等工作量）

要我检查 `base_pose` 是静态还是动态的吗？

# 性能瓶颈深入分析

## 当前状况

**观察到的现象**:
- ✅ 窗口标题显示 `[GPU INSTANCING]` - 代码已加载
- ❌ 没有 FPS 标签的 GPU 统计 - 预览模式不显示该信息
- ⚠️ 性能提升有限：3 fps → ~4-5 fps（仅 1.3-1.7x）
- ⚠️ 播放后会变慢

**关键发现**:
- Freeview 不卡 - 说明 Godot 渲染本身没问题
- 预览播放卡 - 说明瓶颈在**场景更新/数据传输**，不是渲染

---

## 性能分析

从截图看到的数据：
- **帧率轮盘**: 3.701 / 60.000 s = ~16.3 fps 计算帧率
- **但实际播放**: 感觉只有 4-5 fps

这说明：**瓶颈不在 Godot 渲染，在 Python → Godot 数据传输或场景更新**

---

## 可能的瓶颈

### 1. Python 端逐个发送植被实体 ⚠️

**当前流程**:
```python
# godot.py
for vegetation_item in scene.vegetation:  # 1886 次循环
    entities.append(_entity(...))  # 构建 JSON
    
# 发送 JSON: 1886 个完整实体数据
request = json.dumps({"entities": entities, ...})
```

**问题**: 
- 每帧发送 1886 个植被实体的完整数据
- JSON 序列化/反序列化开销
- TCP 传输开销

### 2. Godot 端每帧重建 MultiMesh ⚠️

**当前流程**:
```gdscript
func _sync_entities(entities: Array):
    # 每帧都调用！
    var grouped := _vegetation_instancing.group_vegetation_entities(entities)
    _vegetation_instancing.update_batches(...)  # 重建所有批次
```

**问题**:
- 每帧重新分组 1886 个实体
- 每帧重建 MultiMesh
- 每帧更新所有实例变换

### 3. 场景对象数量 ⚠️

从截图：**869 地物** + 1886 植被 = **2755 个对象**

如果这 869 个地物也是逐个发送和处理，那是巨大的开销！

---

## 根本问题：架构设计

**当前架构**（错误）:
```
Python: 每帧 → 生成完整场景 → JSON → TCP → Godot → 解析 → 重建场景
```

**应该的架构**:
```
Python: 初始化 → 发送静态场景一次
Python: 每帧 → 只发送变化的动态对象（飞机、相机）
Godot: 保持静态场景 → 只更新动态对象
```

---

## 验证假设：批处理是否工作？

添加了调试日志（`push_error`），请重新运行：

```bash
uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json
```

**查找日志**:
- 终端输出
- 或 Godot 控制台
- 应该看到：`[DEBUG] VegetationInstancing: 1886 instances / 20 batches`

如果看到：
- `1886 instances / 20 batches` → ✅ 批处理工作了，但瓶颈在其他地方
- `NO VEGETATION INSTANCES FOUND!` → ❌ 批处理没工作，植被未被识别

---

## 如果批处理工作了但性能仍差

说明**真正的瓶颈是**:

### 瓶颈 1: Python 端每帧发送完整场景

**解决方案**: 
- 场景初始化时发送一次静态对象（植被、地形、建筑）
- 每帧只发送动态对象（飞机位置、相机状态）

**预期提升**: 10-100 倍（取决于静态/动态对象比例）

### 瓶颈 2: JSON 序列化开销

**解决方案**:
- 使用二进制协议（msgpack、protobuf）
- 或压缩 JSON

**预期提升**: 2-5 倍

### 瓶颈 3: 每帧重建场景

**解决方案**:
- Godot 端缓存静态场景
- 只更新变化的对象

**预期提升**: 5-10 倍

---

## 立即行动

1. **运行新的调试版本**，确认批处理数量
2. **截图或复制调试输出**
3. **报告结果**

如果批处理确实工作了，我们需要**重新审视整个数据流架构**，而不仅仅是 GPU Instancing。

---

**关键问题**: GPU Instancing 只解决了渲染端的 draw calls，但如果 Python 端每帧都发送完整场景数据，那就是更大的瓶颈！🎯

# 静态场景缓存实施完成 - 测试指南

## ✅ 实施完成

**静态场景缓存** 和 **GPU Instancing** 都已完整实施！

---

## 🚀 现在请测试

```bash
cd /Users/Administrator/Desktop/study/cv
nix develop --no-update-lock-file
uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json
```

---

## 🎯 预期结果

### 预期性能提升

| 指标 | 之前 | 预期现在 | 提升 |
|------|------|---------|------|
| 帧率 (3站) | 3-5 fps | **60+ fps** | **12-20x** |
| 每帧数据量 | 2755 对象 | 8-10 对象 | 275x |
| 序列化时间 | ~200ms | ~1ms | 200x |
| 网络传输 | ~100ms | ~0.5ms | 200x |

### 可观察的变化

1. **窗口标题** - 应显示 `Chongming 预览 [GPU INSTANCING]` ✅
2. **初始加载** - 第一帧可能稍慢（发送完整场景）
3. **后续帧** - 应该**非常快**（只发送飞机数据）
4. **整体流畅度** - 应该像 Freeview 一样流畅

---

## 📊 工作原理

### 第一帧（初始化）

```
Python: 发送 2755 个对象（完整场景）
        scene_mode = "init"
        ↓
Godot:  接收并缓存所有静态对象
        构建 MultiMesh 批次
        渲染场景
```

### 后续帧（更新）

```
Python: 只发送 8 个飞机 + 风参数
        scene_mode = "update"
        ↓
Godot:  合并缓存的静态对象 + 新的飞机
        更新 MultiMesh 风动画
        渲染场景
```

**关键**: 1886 个植被 + 869 个地物不再每帧发送！

---

## 🔍 如何确认生效

### 方法 1: 观察帧率

- 如果帧率达到 **30+ fps**，说明成功！
- 如果仍然 3-5 fps，说明有问题

### 方法 2: 检查日志（如果有的话）

如果有 Godot 日志输出，应该看到：
- 第一帧: `scene_mode = init`
- 后续帧: `scene_mode = update`

### 方法 3: 监控网络（高级）

如果可以抓包，应该看到：
- 第一帧: ~500KB JSON
- 后续帧: ~2KB JSON (250x 减少!)

---

## ⚠️ 如果性能仍然差

### 可能的问题

1. **静态场景缓存未生效**
   - 检查是否每帧都在发送 `scene_mode = init`
   - 应该只有第一帧是 `init`

2. **其他瓶颈**
   - 图像处理算法
   - 视觉算法计算
   - 磁盘 I/O

3. **Godot 端问题**
   - 静态实体缓存没有正确合并
   - 仍在重建所有实体

---

## 🐛 调试信息

如果需要调试，可以添加日志：

**Python 端** (`godot.py`):
```python
# 在 render() 方法中
print(f"Scene mode: {request.get('scene_mode')}")
print(f"Entities sent: {len(entities)}")
```

**Godot 端** (`worker.gd`):
```gdscript
# 在 _sync_entities() 开头
print("Scene mode: ", scene_mode)
print("Received entities: ", entities.size())
print("Total after merge: ", all_entities.size())
```

---

## 📝 测试后请报告

1. **帧率**: 多少 FPS？
2. **流畅度**: 是否像 Freeview 一样流畅？
3. **初始加载**: 第一帧是否稍慢？
4. **稳定性**: 长时间运行是否稳定？
5. **视觉**: 植被、建筑、飞机都正确显示？

---

**准备好测试了！这应该是最终的性能突破！** 🚀🎉

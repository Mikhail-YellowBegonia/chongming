# 性能回归诊断指南

## 问题

静态场景缓存实施后，性能反而从 3 fps 降到 1 fps。

## 可能的原因

### 1. 哈希计算开销过大

**问题**: 每帧计算 1886 个实体的 SHA256 哈希可能比直接发送还慢

```python
# 每帧都在做这个
serialized = json.dumps(static_entities, sort_keys=True, separators=(",", ":"))
hash = hashlib.sha256(serialized.encode()).hexdigest()
```

**解决方案**: 只在场景确实可能变化时才计算哈希

### 2. 数组合并开销

**Godot 端问题**:
```gdscript
all_entities = _static_entities_cached + entities  # 每帧复制 2755 个实体！
```

### 3. 重复的实体处理

可能在 Godot 端重复处理相同的静态实体。

---

## 立即诊断

请运行测试并检查终端输出，应该看到类似：

```
[CACHE] Total: 2755, Static: 2747, Dynamic: 8
[CACHE] Hash: a98af114f59b47b8..., Cached: None...
[CACHE] Mode: INIT
[CACHE] Total: 2755, Static: 2747, Dynamic: 8  
[CACHE] Hash: a98af114f59b47b8..., Cached: a98af114f59b47b8...
[CACHE] Mode: UPDATE
```

**如果每帧都是 Mode: INIT**，说明哈希每帧都在变化（Bug！）

**如果 Mode: UPDATE 正常**，但性能仍差，说明 Godot 端有问题

---

## 快速回退方案

如果静态场景缓存有问题，可以快速禁用它：

```python
# src/chongming/rendering/godot.py
# 在 render() 方法中注释掉缓存逻辑，恢复原样
request = _request(scene, camera, entities)
# 不设置 scene_mode
```

---

**请运行测试并复制终端的 `[CACHE]` 输出！**

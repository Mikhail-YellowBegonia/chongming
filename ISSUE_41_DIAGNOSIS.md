# Issue #41 诊断报告

## 调查结果

### ✅ 代码已正确加载

经过详细诊断，**Godot 脚本确实已经从源码正确加载**，GPU Instancing 功能代码正在运行。

#### 证据

1. **importlib.resources 正确解析**
   ```
   资源路径: /Users/Administrator/Desktop/study/cv/src/chongming/rendering/godot
   worker.gd 包含调试标记: Line 68
   ```

2. **Godot 日志显示新代码正在执行**
   ```
   vegetation_instancing.gd:131 - update_batches()
   worker.gd:702 - _sync_entities()
   ```
   这些调用栈证明 GPU Instancing 代码确实在运行。

3. **Editable install 工作正常**
   ```
   Location: /Users/Administrator/Desktop/study/cv/.venv/lib/python3.12/site-packages
   Editable project location: /Users/Administrator/Desktop/study/cv
   ```

### ❌ 根本原因：窗口标题被覆盖

问题不是代码未加载，而是**调试标记被后续代码覆盖**：

**src/chongming/rendering/godot/worker.gd:**

```gdscript
func _ready() -> void:
    # Line 68: 设置调试标记
    DisplayServer.window_set_title("Chongming 预览 [GPU INSTANCING]")
    # ...
    
func _show_ui() -> void:
    # Line 339: 覆盖回原标题！
    DisplayServer.window_set_title("Chongming 预览")
    DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_WINDOWED)
```

**执行流程：**
1. `_ready()` 在启动时执行，设置 `[GPU INSTANCING]` 标记
2. 窗口保持最小化（Line 70），用户看不到标题
3. 用户打开预览窗口 → 触发 `_show_ui()`
4. `_show_ui()` 重新设置标题为 `"Chongming 预览"`，**覆盖了调试标记**

---

## 修复方案

### 方案 1: 修复窗口标题（推荐）

**目标：** 让调试标记在整个生命周期保持可见

```gdscript
# worker.gd:339
func _show_ui() -> void:
    # ...
    DisplayServer.window_set_title("Chongming 预览 [GPU INSTANCING]")  # 保持调试标记
    DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_WINDOWED)
```

**优点：**
- 简单直接
- 调试标记始终可见
- 不影响功能

**缺点：**
- 发布前需要移除调试标记

---

### 方案 2: 使用条件编译/环境变量

**目标：** 开发模式显示标记，生产模式不显示

```gdscript
# worker.gd 顶部
var _debug_mode := OS.has_feature("editor") or OS.get_environment("CHONGMING_DEBUG") == "1"

func _show_ui() -> void:
    # ...
    var title := "Chongming 预览"
    if _debug_mode:
        title += " [GPU INSTANCING]"
    DisplayServer.window_set_title(title)
```

**优点：**
- 可配置
- 生产环境自动清理
- 可扩展到其他调试信息

---

### 方案 3: 添加 FPS 显示中的标记（推荐用于验证）

**目标：** 在窗口内容中显示 GPU Instancing 状态

```gdscript
# worker.gd 的 FPS 显示逻辑中
func _update_fps_display() -> void:
    var fps_text := "%d FPS" % Engine.get_frames_per_second()
    if _vegetation_instancing.get_instance_count() > 0:
        var batch_count = _vegetation_instancing.get_batch_count()
        var inst_count = _vegetation_instancing.get_instance_count()
        fps_text += " · GPU: %d inst / %d batch" % [inst_count, batch_count]
    # ...
```

**优点：**
- 提供实时性能指标
- 不依赖窗口标题
- 发布后仍然有用（性能监控）

**注意：** 根据日志，这个功能可能已经实现（`vegetation_instancing.gd` 被调用）

---

## 验证方法

### 当前状态验证

运行以下命令确认代码已加载：

```bash
cd /Users/Administrator/Desktop/study/cv
uv run python diagnose_godot_loading.py
```

预期输出：
```
✅ 第 68 行: DisplayServer.window_set_title("Chongming 预览 [GPU INSTANCING]")
```

### 功能验证

检查 Godot 日志确认 GPU Instancing 正在运行：

```bash
tail -50 ~/Library/Application\ Support/Godot/app_userdata/Chongming\ Virtual\ Scene\ Renderer/logs/godot.log
```

查找：
- ✅ `vegetation_instancing.gd` 调用栈
- ✅ `update_batches()` 调用

### 性能测试

1. 修复窗口标题（方案 1）
2. 运行预览：
   ```bash
   uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json
   ```
3. 检查：
   - 窗口标题是否保持 `[GPU INSTANCING]`
   - FPS 是否有提升
   - 是否显示 `GPU: X inst / Y batch`

---

## 后续行动

1. **立即修复：** 应用方案 1 或方案 2，确保调试标记可见
2. **功能验证：** 确认 GPU Instancing 功能正常工作
3. **性能基准：** 对比 GPU Instancing 前后的性能
4. **文档更新：** 更新 `FIX_CODE_NOT_LOADED.md` 说明真实原因
5. **清理：** 决定是否保留调试标记或在发布前移除

---

## 关键发现总结

| 问题 | 状态 | 说明 |
|------|------|------|
| 代码未加载 | ❌ 误判 | editable install 工作正常，代码已加载 |
| importlib.resources 问题 | ❌ 误判 | 正确解析到源码目录 |
| 调试标记不可见 | ✅ 真实问题 | 被 `_show_ui()` 覆盖 |
| GPU Instancing 运行 | ✅ 已运行 | 日志证实代码正在执行 |

**结论：** 这是一个**调试方法问题**，不是代码加载问题。GPU Instancing 功能已经在运行，只是验证方法（窗口标题）选择不当。

---

**推荐操作：** 应用方案 1 快速验证，然后转向方案 3 进行长期性能监控。

# Issue #41 调查与修复报告

## 执行摘要

**问题：** GPU Instancing 调试标记未显示，怀疑代码未加载  
**实际原因：** 窗口标题被后续代码覆盖，代码实际已正常加载并运行  
**修复：** 一行代码修改，保持调试标记在整个生命周期可见  
**状态：** ✅ 已修复，提供完整诊断和验证工具

---

## 调查过程

### 1. 初步诊断

**假设：** editable install 导致 Godot 脚本未从源码加载

**验证步骤：**
```bash
# 检查包安装模式
uv pip show chongming | grep -E "Location|Editable"
# 结果: Editable project location: /Users/Administrator/Desktop/study/cv ✅

# 检查 importlib.resources 解析
python -c "from importlib.resources import files; print(files('chongming.rendering').joinpath('godot'))"
# 结果: /Users/Administrator/Desktop/study/cv/src/chongming/rendering/godot ✅
```

**结论：** 代码加载机制工作正常

### 2. 深入分析

创建 `diagnose_godot_loading.py` 进行系统性检查：

**关键发现：**
1. ✅ importlib.resources 正确解析到源码目录
2. ✅ worker.gd 包含调试标记（Line 68）
3. ✅ 文件修改时间正确（最新）
4. ✅ Godot 日志显示 `vegetation_instancing.gd` 正在运行

### 3. 根本原因定位

检查 Godot 日志：
```bash
tail -50 ~/Library/Application\ Support/Godot/.../logs/godot.log
```

**发现：**
- `vegetation_instancing.gd:131` 出现在调用栈中
- GPU Instancing 代码确实在运行！

**矛盾：** 代码在运行，为什么窗口标题没有调试标记？

### 4. 代码审查

查看 `worker.gd` 中所有 `window_set_title` 调用：

```gdscript
# Line 68 - _ready()
DisplayServer.window_set_title("Chongming 预览 [GPU INSTANCING]")
DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_MINIMIZED)

# Line 339 - _show_ui()
DisplayServer.window_set_title("Chongming 预览")  # ❌ 覆盖了标记！
DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_WINDOWED)
```

**根本原因：**
1. 启动时设置标记，但窗口最小化（用户看不到）
2. 用户打开预览窗口 → `_show_ui()` 被调用
3. `_show_ui()` 重置标题，调试标记消失

---

## 修复方案

### 应用的修复

**文件：** `src/chongming/rendering/godot/worker.gd`  
**位置：** Line 339

```diff
func _show_ui() -> void:
    # ...
-   DisplayServer.window_set_title("Chongming 预览")
+   DisplayServer.window_set_title("Chongming 预览 [GPU INSTANCING]")
    DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_WINDOWED)
```

**影响范围：** 最小化（仅修改窗口标题显示）

---

## 验证工具

### 1. 诊断脚本：`diagnose_godot_loading.py`

**功能：**
- 检查包安装模式
- 验证 importlib.resources 路径解析
- 确认源文件包含调试标记
- 检查 Godot 缓存和临时目录

**使用：**
```bash
uv run python diagnose_godot_loading.py
```

### 2. 验证脚本：`verify_gpu_instancing.sh`

**功能：**
- 检查源码标记
- 运行诊断
- 提供测试步骤和验证清单

**使用：**
```bash
./verify_gpu_instancing.sh
```

### 3. 手动验证

```bash
# 运行预览
uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json

# 检查项：
# ✅ 窗口标题: "Chongming 预览 [GPU INSTANCING]"
# ✅ FPS 显示: "X FPS · GPU: Y inst / Z batch"
# ✅ 性能提升（相比之前）
```

---

## 交付物

### 代码修改

- [x] `src/chongming/rendering/godot/worker.gd:339` - 修复窗口标题

### 诊断工具

- [x] `diagnose_godot_loading.py` - Python 包加载诊断
- [x] `verify_gpu_instancing.sh` - GPU Instancing 验证

### 文档

- [x] `ISSUE_41_DIAGNOSIS.md` - 详细诊断报告（技术细节）
- [x] `ISSUE_41_FIX_SUMMARY.md` - 修复总结（方案对比）
- [x] `ISSUE_41_INVESTIGATION_REPORT.md` - 本文档（调查过程）

### Issue 更新

- [x] 认领 Issue #41
- [x] 添加诊断结果评论
- [x] 添加修复完成评论

---

## 关键洞察

### 1. 问题不在代码加载机制

原始假设（importlib.resources 在 editable install 下加载缓存）是错误的。实际上：
- Python 3.9+ 的 importlib.resources 在 editable install 下工作良好
- uv 的 editable install 实现正确（使用 `.pth` 文件）

### 2. 验证方法的重要性

选择错误的验证点会导致误判：
- ❌ 启动时最小化窗口的标题 → 用户看不到
- ✅ 打开后的窗口标题 → 用户实际看到的
- ✅ Godot 日志 → 代码实际执行情况
- ✅ 性能指标 → 功能实际效果

### 3. 系统性诊断的价值

创建可复用的诊断工具带来的好处：
- 快速排除错误假设
- 提供可验证的证据
- 可用于未来类似问题

---

## 经验总结

### 成功的诊断流程

1. **验证基础设施** → editable install 工作正常
2. **验证加载路径** → importlib.resources 解析正确
3. **验证文件内容** → 源码包含预期修改
4. **检查运行时行为** → 日志显示代码执行
5. **追踪完整流程** → 发现标题被覆盖

### 避免的陷阱

- ❌ 基于假设直接修改基础设施（如替换 importlib.resources）
- ❌ 在未确认问题前强制重新安装包
- ❌ 依赖单一验证点（窗口标题）

### 最佳实践

- ✅ 创建诊断脚本系统性检查
- ✅ 查看运行时日志验证代码执行
- ✅ 追踪完整的代码执行路径
- ✅ 提供可复用的验证工具

---

## 后续建议

### 短期（发布前）

1. **移除调试标记**
   ```diff
   - DisplayServer.window_set_title("Chongming 预览 [GPU INSTANCING]")
   + DisplayServer.window_set_title("Chongming 预览")
   ```

2. **性能基准测试**
   - 对比 GPU Instancing 前后的帧率
   - 验证批次减少的效果
   - 记录性能提升数据

### 中期（下一个版本）

1. **添加开发模式**
   ```gdscript
   var debug_mode = OS.get_environment("CHONGMING_DEBUG") == "1"
   var title = "Chongming 预览" + (" [DEBUG]" if debug_mode else "")
   ```

2. **增强 FPS 显示**
   - 添加 GPU 实例数
   - 添加批次数
   - 添加性能对比

### 长期（架构改进）

1. **统一调试标志管理**
   - 中心化的调试配置
   - 环境变量控制
   - 运行时可切换

2. **完善诊断工具**
   - 集成到开发工作流
   - 自动化验证
   - CI/CD 集成

---

## 结论

**Issue #41 已成功解决。**

- **根本原因：** 窗口标题被后续代码覆盖
- **修复方案：** 一行代码修改
- **验证工具：** 完整的诊断和验证脚本
- **知识积累：** 详细文档记录调查过程

这次调查证明：
1. 代码加载机制工作正常
2. GPU Instancing 功能已在运行
3. 问题是验证方法选择不当

提供的诊断工具可用于未来类似问题的快速排查。

---

**文件列表：**
- `ISSUE_41_DIAGNOSIS.md` - 技术诊断
- `ISSUE_41_FIX_SUMMARY.md` - 修复方案
- `ISSUE_41_INVESTIGATION_REPORT.md` - 本文档
- `diagnose_godot_loading.py` - 诊断工具
- `verify_gpu_instancing.sh` - 验证工具

**状态：** ✅ 已修复，待用户测试验证

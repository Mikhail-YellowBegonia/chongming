# GPU Instancing 调试 - 代码未生效问题

## 问题诊断

**现象**: 
- 修改了代码添加调试信息
- 运行预览没有看到新的调试输出
- 性能仍然是 ~3 fps

**可能原因**:

### 1. Godot 缓存了旧脚本

**解决方案**: 完全重启
```bash
# 确保 Godot 进程完全关闭
pkill -9 godot4 || pkill -9 godot

# 清理可能的缓存
rm -rf ~/.cache/godot/

# 重新进入 nix shell
exit  # 退出当前 shell
nix develop --no-update-lock-file

# 重新运行
uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json
```

### 2. 代码路径问题

**检查**: Godot 是否从正确的目录加载脚本？

```bash
# 查看 Godot 工作目录
cd /Users/Administrator/Desktop/study/cv
pwd

# 确认文件存在
ls -la src/chongming/rendering/godot/vegetation_instancing.gd
ls -la src/chongming/rendering/godot/worker.gd
```

### 3. preload 路径问题

Godot 的 `preload("res://vegetation_instancing.gd")` 使用的是 **Godot 项目路径**，不是文件系统路径。

**检查 Godot 项目根目录**:
```bash
find src/chongming/rendering/godot -name "project.godot" -o -name "*.cfg"
```

---

## 立即测试方案

### 方案 A: 强制重新加载

```bash
cd /Users/Administrator/Desktop/study/cv

# 1. 确保退出所有 Godot 进程
ps aux | grep godot

# 2. 重新进入环境
exit
nix develop --no-update-lock-file

# 3. 运行
uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json
```

### 方案 B: 添加明显的错误来验证代码是否加载

让我在代码中添加一个**故意的错误**，如果代码真的被加载了，程序会崩溃：

```gdscript
# 在 worker.gd 的 _ready() 函数开始处添加
func _ready() -> void:
    push_error("GPU INSTANCING CODE IS LOADED!")  # 应该在日志中可见
    # ... 原有代码
```

---

## 下一步行动

### 选项 1: 我添加明显的测试代码

我可以添加一个会导致明显错误或可见效果的代码，验证代码是否被执行。

### 选项 2: 你手动验证

请运行：
```bash
cd /Users/Administrator/Desktop/study/cv
head -10 src/chongming/rendering/godot/worker.gd
```

确认第 4-5 行是否包含：
```gdscript
const VegetationInstancing = preload("res://vegetation_instancing.gd")
```

### 选项 3: 检查 Python 是否传递了 vegetation 数据

问题可能不在 Godot 端，而是 **Python 端没有标记植被为 "vegetation" category**。

检查：
```bash
grep -n "category.*vegetation" src/chongming/rendering/godot.py
```

---

## 我的建议

让我添加一个**无法忽视的调试标记** - 直接修改窗口标题：

```gdscript
func _ready() -> void:
    DisplayServer.window_set_title("GPU INSTANCING ENABLED")
    # ... 原有代码
```

这样你一眼就能看到代码是否被加载了。

**要我添加这个吗？**

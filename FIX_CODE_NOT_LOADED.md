# GPU Instancing 代码未加载 - 根本原因与解决方案

## 🔍 根本原因

**Godot 脚本从 Python 包资源加载，不是从源码目录！**

```python
# src/chongming/rendering/godot.py:568
project = self._files.enter_context(
    as_file(files("chongming.rendering").joinpath("godot"))
)
```

这意味着：
1. Godot 使用的是 **已安装的包** 中的脚本
2. 修改源码后需要 **重新安装包** 才能生效
3. 或者包必须以 **editable 模式** 安装

---

## ✅ 解决方案

### 方案 1: 重新安装包（推荐）

```bash
cd /Users/Administrator/Desktop/study/cv
nix develop --no-update-lock-file

# 检查当前安装模式
uv pip show chongming | grep -E "Location|Editable"

# 强制重新安装（editable 模式）
uv pip install --force-reinstall --no-deps -e .

# 验证 Godot 脚本已更新
cat .venv/lib/python3.*/site-packages/chongming/rendering/godot/worker.gd | head -5

# 运行测试
uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json
```

### 方案 2: 使用 uv sync 刷新

```bash
cd /Users/Administrator/Desktop/study/cv
nix develop --no-update-lock-file

# uv sync 应该会刷新 editable 安装
uv sync --frozen

# 运行测试
uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json
```

### 方案 3: 直接修改已安装的文件（临时测试）

```bash
# 找到已安装的位置
INSTALL_DIR=$(python -c "import chongming.rendering; print(chongming.rendering.__path__[0])")
echo $INSTALL_DIR

# 直接覆盖已安装的文件
cp src/chongming/rendering/godot/worker.gd $INSTALL_DIR/godot/
cp src/chongming/rendering/godot/vegetation_instancing.gd $INSTALL_DIR/godot/

# 运行测试
uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json
```

---

## 🧪 验证方法

成功后，窗口标题应该显示：
```
Chongming 预览 [GPU INSTANCING]
```

并且左上角应该显示：
```
3 FPS · GPU: 1886 inst / 20 batch · EDIT
```

---

## 📋 完整测试流程

```bash
# 1. 进入环境
cd /Users/Administrator/Desktop/study/cv
nix develop --no-update-lock-file

# 2. 重新安装包
uv pip install --force-reinstall --no-deps -e .

# 3. 验证文件已更新
python -c "import chongming.rendering; import os; print(os.path.join(chongming.rendering.__path__[0], 'godot/worker.gd'))" | xargs head -5

# 应该看到：
# extends Node
# 
# ## Python owns simulation time; Godot owns procedural visuals. Render only on a snapshot request.
# const Vegetation = preload("res://vegetation.gd")
# const VegetationInstancing = preload("res://vegetation_instancing.gd")

# 4. 运行测试
uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json

# 5. 检查窗口标题是否包含 "[GPU INSTANCING]"
```

---

## 🎯 预期结果

**成功标志**:
1. 窗口标题：`Chongming 预览 [GPU INSTANCING]`
2. FPS 显示：`X FPS · GPU: Y inst / Z batch · EDIT`
3. 性能提升：从 3 fps 显著提升

**如果仍然失败**:
- 窗口标题仍是 "Chongming 预览" → 包未正确安装
- 有 GPU 信息但 `0 inst / 0 batch` → Python 端数据问题
- 有实例但性能无提升 → 其他瓶颈

---

## 📝 开发模式注意事项

**问题**: Python 包资源系统（`importlib.resources`）在 editable 安装时的行为

**最佳实践**:
1. 使用 `uv pip install -e .` 安装开发版本
2. 修改代码后，某些情况需要重新安装
3. 或者使用热重载机制（如果支持）

**未来改进**: 考虑添加开发模式，直接从源码目录加载 Godot 脚本。

---

**请立即运行方案 1，然后告诉我结果！**

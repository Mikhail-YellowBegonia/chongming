# GPU Instancing 性能测试说明

## 🎯 测试目标

使用 `hannover_multi_target.json` 验证 GPU Instancing 性能提升。

**场景参数**:
- 3 个观测站
- 8 个目标（飞机、球体、盒体）
- ~3796 棵树（Hannover 地景）
- 60 秒 @ 30 fps
- 强风配置（4.5 m/s）

---

## 🚀 快速测试

### 方法 1: 使用测试脚本

```bash
cd /Users/Administrator/Desktop/study/cv
nix develop --no-update-lock-file
./benchmark_instancing.sh
```

### 方法 2: 手动运行

```bash
cd /Users/Administrator/Desktop/study/cv
nix develop --no-update-lock-file
uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json
```

---

## 📊 观察指标

### 主要指标: 帧率（FPS）

**在 Godot 窗口中查看**:
- 窗口左上角或控制台应显示 FPS
- 或观察画面流畅度

**预期结果**:
- **旧实现**: 1.4 fps（已知基准）
- **新实现**: 30-50+ fps（目标）

**判断标准**:
- FPS < 10: 有问题，需要调试
- FPS 10-20: 有提升但不够理想
- FPS 20-30: 良好，接近目标
- FPS 30+: 优秀，达到预期

### 次要指标

1. **视觉正确性**
   - [ ] 树木正确显示（trunk + crown）
   - [ ] 灌木正确显示
   - [ ] 位置、大小合理
   - [ ] 无闪烁或错误

2. **风动画**
   - [ ] 树木在风中摇摆
   - [ ] 灌木在风中摇摆
   - [ ] 动画平滑自然

3. **错误检查**
   - [ ] 无 Godot 控制台错误
   - [ ] 无 Python 异常

---

## 🔍 A/B 对比测试（可选）

### 测试旧实现性能

**步骤 1**: 禁用 GPU Instancing

```bash
# 编辑 worker.gd
# 找到这一行（约第 53 行）:
var _use_vegetation_instancing := true

# 改为:
var _use_vegetation_instancing := false
```

**步骤 2**: 重新运行测试

```bash
uv run --frozen chongming-preview --scenario scenarios/hannover_multi_target.json
```

**步骤 3**: 记录旧实现的 FPS

**步骤 4**: 恢复设置

```bash
# 将 worker.gd 改回:
var _use_vegetation_instancing := true
```

### 对比表格

| 实现 | FPS | Draw Calls | 提升倍数 |
|------|-----|-----------|---------|
| 旧实现 | 1.4 | ~3796 | 基准 |
| 新实现 | ??? | ~40 | ???x |

---

## 🐛 故障排查

### 问题 1: 看不到树木

**检查**:
1. Godot 控制台是否有错误？
2. 是否有 "VegetationBatch" 相关消息？
3. 切换到自由视角，四处查看

**可能原因**:
- MultiMesh 创建失败
- 网格为空
- 变换矩阵错误

**临时解决**: 禁用 instancing 测试旧实现

### 问题 2: 帧率没提升

**检查**:
1. GPU Instancing 是否真的启用？
   ```gdscript
   // worker.gd
   var _use_vegetation_instancing := true
   ```

2. Draw Calls 是否降低了？
   - 打开 Godot Profiler 查看

3. 是否有其他瓶颈？
   - CPU 瓶颈
   - 图像算法开销
   - 其他渲染开销

### 问题 3: 崩溃或严重错误

**立即回退**:
```gdscript
var _use_vegetation_instancing := false
```

**收集信息**:
- Godot 控制台完整错误信息
- Python 异常堆栈
- 崩溃时的操作

---

## 📝 测试报告模板

完成测试后，请提供以下信息：

```
### GPU Instancing 性能测试结果

**测试环境**:
- 系统: [macOS/Linux]
- Godot: [通过 nix develop]
- 场景: hannover_multi_target.json

**性能指标**:
- 新实现 FPS: [___] fps
- 旧实现 FPS: [1.4] fps (已知)
- 提升倍数: [___]x

**视觉检查**:
- [ ] 树木显示正确
- [ ] 灌木显示正确
- [ ] 风动画工作
- [ ] 无明显错误

**问题（如有）**:
[描述任何观察到的问题]

**Godot 控制台输出**:
[粘贴相关错误或警告]

**总体评价**: [成功/需要调整/失败]
```

---

## 🎯 成功标准

**最低标准** (可接受):
- FPS ≥ 20
- 视觉正确
- 无崩溃

**理想标准** (预期):
- FPS ≥ 30
- 风动画流畅
- 多次运行稳定

**优秀标准** (超出预期):
- FPS ≥ 50
- 所有功能完美
- 确定性验证通过

---

## ⏱️ 预计测试时间

- 快速测试: 2-3 分钟
- A/B 对比: 5-10 分钟
- 详细验证: 15-20 分钟

---

**准备好开始测试了吗？** 运行上面的命令并告诉我结果！ 🚀

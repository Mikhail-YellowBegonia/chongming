# 外部高拟真场景集成计划

## 目标场景

| 项目                           | 来源                                      | 特点                     | 预期用途                       |
| ------------------------------ | ----------------------------------------- | ------------------------ | ------------------------------ |
| Realistic Environments         | github:itzMeerkat/Godot-RealisticEnvironments | 高质量天空与光照系统     | 测试云/雾对观测的干扰          |
| Meadows Environment Pack       | polyartstudios.itch.io/meadows-godot      | 大尺度地形、植被和水系统 | 大场景录制与遮挡测试           |
| Outskirts Environment Demo     | jedenjenda23.itch.io/outskirts-godot-environment-demo | 18世纪小镇郊区建筑群     | 建筑物遮挡与城镇环境录制       |

## 技术挑战与解决方案

### 1. 高渲染压力与进度反馈

#### 问题分析
- 顶级画质场景在标准相机分辨率（主 1920×1080、辅 640×480）下可能接近实时渲染极限
- 当前 `chongming-scene` 录制过程无进度指示，用户无法判断是否卡死
- 外部场景可能提供质量降级接口（LOD 距离、阴影质量、后处理）但须逐项探查

#### 解决方案

**A. 进度条实现**
- 在 `chongming/composition/evidence.py` 的主循环添加进度报告
- 使用 `tqdm` 或终端序列显示：`[已完成帧数/总帧数] 当前时间 | 预计剩余时间 | 平均帧耗时`
- 对于 `--no-video` 模式，报告逻辑步骤进度而非像素帧进度
- manifest 记录每 N 帧的墙钟检查点，用于诊断渲染瓶颈

**B. 质量降级开关**
- 场景配置新增可选字段 `rendering.external_scene_quality`：`"ultra"` | `"high"` | `"medium"` | `"low"`
- Godot 端在加载外部场景后，尝试调用场景根节点的标准化接口：
  ```gdscript
  if scene_root.has_method("set_quality_preset"):
      scene_root.set_quality_preset(quality_level)
  ```
- 若外部场景未提供该方法，记录警告但继续使用默认画质
- 降级策略包括：阴影分辨率、SSAO 强度、反射探针、植被密度、后处理效果

**C. 渲染超时保护**
- 为单帧渲染设置可配置超时（默认 60 秒），超时后标记该帧为 `skipped: render_timeout`
- 连续 3 帧超时则终止录制并报告失败，防止长时间卡死

### 2. 游戏逻辑冲突

#### 问题分析
- 外部场景可能包含：物理模拟、脚本驱动的动画、输入处理、场景切换逻辑
- 当前 `godot.py` 通过禁用自动渲染循环并接管 `_process()` 实现帧精确控制
- 外部脚本若修改全局状态或响应帧事件，可能与 chongming 的确定性时间推进冲突

#### 解决方案

**A. 场景隔离加载**
- 在 Godot 端创建新的 `ExternalSceneContainer` 节点
- 加载外部场景为子场景，并立即执行清理：
  ```gdscript
  func _sanitize_external_scene(scene_root: Node) -> void:
      # 禁用所有脚本但保留视觉效果
      for node in scene_root.get_children_recursive():
          if node.get_script():
              node.set_process(false)
              node.set_physics_process(false)
              node.set_process_input(false)
          
          # 冻结物理体
          if node is RigidBody3D or node is CharacterBody3D:
              node.freeze = true
  ```

**B. 确定性状态重置**
- 每次实验开始时，重新加载外部场景实例或调用可选的 `reset_to_initial_state()` 方法
- 记录场景节点树哈希（节点名称与类型），用于检测意外的场景修改

**C. 冲突检测与报告**
- 在录制过程中监控：
  - Godot 错误日志中的脚本异常
  - 场景树非预期变化（节点数量、根节点位置）
  - 渲染请求耗时突变（可能由脚本争抢资源导致）
- 检测到冲突时记录诊断信息，由用户决定是否需要专项处理

**D. 白名单机制（远期）**
- 若某些外部逻辑对视觉效果必需（如程序化天气、昼夜循环），提供白名单配置
- 场景配置新增：
  ```json
  "external_scene": {
      "allow_scripts": ["res://weather_system.gd"],
      "time_control_node": "WorldEnvironment/DayNightCycle"
  }
  ```

## 集成步骤

### 阶段 1：Nix Flake 集成（1-2 工作日）

```nix
# flake.nix 新增 inputs
inputs.godot-realistic-environments = {
  url = "github:itzMeerkat/Godot-RealisticEnvironments/<commit-hash>";
  flake = false;
};
inputs.meadows-environment = {
  url = "git+https://example.com/meadows-pack.git?ref=<tag>";  # 需从 itch.io 手动获取
  flake = false;
};
inputs.outskirts-demo = {
  url = "git+https://example.com/outskirts-demo.git?ref=<tag>";
  flake = false;
};

# outputs 段新增环境变量
CHONGMING_EXTERNAL_REALISTIC_ENV = "${godot-realistic-environments}";
CHONGMING_EXTERNAL_MEADOWS = "${meadows-environment}";
CHONGMING_EXTERNAL_OUTSKIRTS = "${outskirts-demo}";
```

**注意**：商业资源包（Meadows、Outskirts）须先确认许可证，可能需要：
- 购买后手动下载到本地，通过 `path:` 引用
- 检查是否允许团队内部共享和 CI 使用
- 确认录制输出的再分发权限

### 阶段 2：Godot 端适配（2-3 工作日）

修改 `src/chongming/rendering/godot/tools/render.gd`：

1. 新增外部场景加载函数
2. 实现 `_sanitize_external_scene()`
3. 添加质量预设调用逻辑
4. 场景状态哈希计算

### 阶段 3：Python 集成（2-3 工作日）

1. `contracts/scene.py`：新增 `ExternalSceneConfig` 数据类
2. `scenarios/load.py`：编译器支持 `external_scene` 字段验证
3. `rendering/godot.py`：传递外部场景路径和质量参数到 Godot
4. `composition/evidence.py`：添加进度报告和超时保护

### 阶段 4：场景配置与测试（3-5 工作日）

为每个外部场景创建配置文件：

```json
// scenarios/realistic_env_foggy.json
{
  "format": "scenario-authoring-v2",
  "scenario_id": "realistic-env-foggy-test",
  "duration_s": 60,
  "fps": 30,
  "external_scene": {
    "project_env": "CHONGMING_EXTERNAL_REALISTIC_ENV",
    "scene_path": "res://scenes/main_environment.tscn",
    "quality": "high",
    "time_of_day": 14.5,
    "weather": "fog_heavy"
  },
  "occlusion_mode": "unknown",
  "stations": [/* ... */],
  "targets": [/* ... */]
}
```

测试矩阵：
- 每个场景 × 3 种质量级别 × 2 种天气条件 = 18 个配置
- 录制 30 秒片段，验证确定性、帧完整性和真值一致性
- 记录墙钟耗时和内存峰值

### 阶段 5：文档更新（1 工作日）

- `docs/user-guide.md`：外部场景配置章节
- `docs/design.md`：外部场景集成边界说明
- `scenarios/README.md`：示例配置清单
- `AGENTS.md`：更新资产许可与署名要求

## 验收标准

- [ ] 三个外部场景均可在 `chongming-scene` 下完整录制
- [ ] 进度条正确显示帧进度和预计剩余时间
- [ ] 质量降级开关有效减少渲染耗时（至少 30%）
- [ ] 无游戏逻辑冲突导致的确定性破坏
- [ ] 连续运行两次生成相同真值序列（允许 PNG 像素因浮点累积有微小差异）
- [ ] 文档完整记录配置方法、许可要求和已知限制

## 风险与备选方案

### 风险 1：许可证不兼容
- Meadows/Outskirts 可能禁止录制输出的公开分发
- **备选**：仅内部使用，文档说明如何自行购买和集成

### 风险 2：Godot 4.7 版本不兼容
- 外部场景可能使用 4.3 或更早版本，存在 API 破坏性变更
- **备选**：使用 Godot 的场景升级工具 `--upgrade` 参数，或保留多版本 Godot

### 风险 3：性能无法接受
- 即使降低质量，单帧仍需 5 秒以上
- **备选**：使用 `--render-every 10` 抽帧录制，或文档明确标注"仅用于静态场景验证"

## 开发成本估算

- **工程时间**：10-15 工作日（含测试与文档）
- **测试机要求**：支持 Vulkan/Metal 的独立 GPU，16GB+ 内存
- **资源采购**：约 $50-100（若 Meadows 和 Outskirts 需要购买）

## 后续优化方向

1. **并行录制**：为不同外部场景启动独立 Godot 进程，避免场景切换开销
2. **分布式渲染**：在多台机器上并行录制不同时间段
3. **统一质量基准**：开发标准化的画质评估工具，量化不同场景的视觉复杂度
4. **自定义融合场景**：借鉴三个场景的优点，制作单个"全覆盖测试场景"（远期目标）

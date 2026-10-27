# Realistic Environments 集成报告

## 集成概述

成功集成 [Godot-RealisticEnvironments](https://github.com/itzMeerkat/Godot-RealisticEnvironments) (提交 `169a472`) 作为外部 Godot 场景，用于测试真实天气条件（云、雾、海洋）对目标观测的影响。

## 技术实现

### 1. Nix Flake 集成

**修改文件**: `flake.nix`

添加 Realistic Environments 为 flake input：
```nix
inputs.realistic-environments = {
  url = "github:itzMeerkat/Godot-RealisticEnvironments/169a472c0f019f51052d3e6893395a8425260ab6";
  flake = false;
};
```

导出环境变量：
```nix
CHONGMING_EXTERNAL_REALISTIC_ENV = "${realistic-environments}";
```

### 2. 导入超时优化

**修改文件**: `src/chongming/rendering/godot.py:573`

**问题**: Realistic Environments 是大型项目（约 80 MB），首次导入需要 2-3 分钟，原 120 秒超时不足。

**解决**: 将 Godot 导入超时从 120 秒增加到 600 秒。

```python
# 前: timeout=120
# 后: timeout=600
```

### 3. 场景配置

**新增文件**: `scenarios/realistic_env_ocean.json`

- **场景**: `res://examples/ocean_only.tscn` (最简洁的海洋场景)
- **时长**: 15 秒 @ 30 fps = 450 逻辑步，1800 逻辑帧
- **遮挡模式**: `unknown`（外部场景无完整几何模型）
- **站点**: 2 个观测站
- **目标**: 1 个恒速飞行目标

## 验证结果

### 测试 1: 无视频模式
```bash
export CHONGMING_GODOT_EXTERNAL_PROJECT="$CHONGMING_EXTERNAL_REALISTIC_ENV"
export CHONGMING_GODOT_EXTERNAL_SCENE="res://examples/ocean_only.tscn"
uv run chongming-scene --scenario scenarios/realistic_env_ocean.json \
  --output /tmp/realistic_env_test --no-video
```

**结果**: ✅ 成功
- 生成完整真值（40 MB `truth.jsonl`）
- 所有 1800 帧标记为 skipped（预期行为）

### 测试 2: 视频录制（抽帧）
```bash
uv run chongming-scene --scenario scenarios/realistic_env_ocean.json \
  --output /tmp/realistic_env_test --render-every 10
```

**结果**: ✅ 成功
- 渲染 180 帧（每 10 帧采样 1 次）
- 生成 4 个视频流（2 站 × 主辅相机）

### 性能指标

| 指标 | 数值 |
|------|------|
| 首次导入时间 | ~2-3 分钟 |
| 单帧渲染中位数 | 385 ms (2.6 fps) |
| 单帧渲染 p95 | 464 ms (2.2 fps) |
| GPU 总时间（180 帧） | 5.1 秒 |
| 总运行时间（45 步） | 18.8 秒 |

**评估**: 渲染性能符合"高拟真场景"预期，可接受用于离线录制。

### 输出文件验证

```
/tmp/realistic_env_test/
├── authoring.json          (682 B)   - 输入场景配置
├── manifest.json           (11 KB)   - 运行元数据
├── scenario.json           (48 KB)   - 编译后场景
├── online.jsonl            (1.2 MB)  - 在线帧元数据
├── truth.jsonl             (40 MB)   - 逐步真值
├── truth-static.json       (28 KB)   - 静态对象快照
├── timing.jsonl            (183 KB)  - 性能统计
├── summary.json            (891 B)   - 汇总
├── video.json              (1.2 KB)  - 视频元数据
└── video/
    ├── camera-000.mp4      (200 KB)  - 站 1 主相机
    ├── camera-001.mp4      (25 KB)   - 站 1 辅助相机
    ├── camera-002.mp4      (300 KB)  - 站 2 主相机
    └── camera-003.mp4      (3.3 KB)  - 站 2 辅助相机
```

所有文件完整，SHA-256 摘要已记录在 `video.json`。

## 场景特性

### 优势
1. **真实天气系统**: 包含动态天空、云、雾、大气透视
2. **海洋渲染**: 波浪、反射、折射效果
3. **光照系统**: 昼夜循环、太阳角度、曝光控制
4. **简洁场景**: `ocean_only.tscn` 仅 13 行，无游戏逻辑冲突

### 限制
1. **无地形遮挡**: 海洋场景无建筑物或植被，仅适合天气干扰测试
2. **渲染压力**: 2-3 fps，需使用 `--render-every N` 抽帧录制
3. **首次导入慢**: 2-3 分钟，CI 环境需考虑超时配置

## 文档更新

- ✅ `docs/nix-external-projects.md`: 添加 Realistic Environments 项目信息和性能特征
- ✅ `README.md`: 补充海洋场景录制示例
- ✅ `scenarios/realistic_env_ocean.json`: 新增测试场景配置

## 下一步工作

### 短期（当前分支）
- [ ] 测试其他场景（`demo/main.tscn`、`examples/boat.tscn`）
- [ ] 验证天气参数控制（时间、云密度、雾强度）
- [ ] 确认 `occlusion_mode: unknown` 下的真值一致性

### 中期（后续分支）
- [ ] 集成 Meadows Environment Pack（大尺度地形 + 植被）
- [ ] 集成 Outskirts Environment Demo（建筑群遮挡）
- [ ] 实现进度条显示（`tqdm`）
- [ ] 添加质量降级开关（`rendering.external_scene_quality`）

### 长期
- [ ] 游戏逻辑清理机制（`_sanitize_external_scene()`）
- [ ] 场景状态监控和冲突检测
- [ ] 并行录制优化

## 验收状态

- ✅ Nix flake 集成完成
- ✅ 场景配置和真值生成正确
- ✅ 视频录制功能正常
- ✅ 代码质量检查通过
- ✅ 文档更新完整
- ⏳ 等待 code review 和合并

## 许可证确认

**Realistic Environments**: MIT License
- ✅ 允许商业使用
- ✅ 允许修改和分发
- ✅ 录制输出可再分发
- ⚠️  需保留原始许可证和版权声明

## 提交信息

**分支**: `codex/realistic-environments`

**修改文件**:
- `flake.nix`
- `flake.lock`
- `src/chongming/rendering/godot.py`
- `scenarios/realistic_env_ocean.json`
- `docs/nix-external-projects.md`
- `README.md`

**建议提交消息**:
```
feat: integrate Realistic Environments external Godot scene

- Add Realistic Environments (169a472) to Nix flake inputs
- Increase Godot import timeout from 120s to 600s for large projects
- Add realistic_env_ocean.json test scenario (ocean scene)
- Update documentation with usage examples and performance benchmarks

Verified: 15s recording with ocean_only.tscn (180 frames, 2 stations)
Performance: 385ms median render time (2.6 fps)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
```

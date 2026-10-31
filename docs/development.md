# 开发指南

## 环境与依赖

在源码根目录使用仓库提供的 Nix 和 uv：

```sh
nix develop --no-update-lock-file
uv sync --frozen
```

Nix 提供 Python 3.12、uv、Godot 4.7.2 stable、Ruff、treefmt 和 Git；Linux 另提供 Xvfb。`flake.lock` 固定 Nixpkgs，`uv.lock` 固定 Python 依赖。Python 包要求为 `>=3.12,<3.15`，开发环境使用 3.12。Hatchling 是构建后端，包位于 `src/chongming/`。

uv 将运行及开发依赖安装到 `.venv/`，默认以 editable 方式安装本项目。日常使用 `uv run --frozen …`，不重新解析或修改锁文件。Nix 设置 `UV_PYTHON_DOWNLOADS=never`，解释器由 Nix 提供。Ruff 使用 shell 中的 `ruff`，不要通过 `uv run ruff` 替换为可能无法在 NixOS 启动的 PyPI 二进制。

未经维护者授权，不执行依赖升级、`uv add`、`uv lock` 或 Nix 输入更新。获准变更时只调整必要依赖，审查锁文件差异并运行相关检查。环境故障应先检查解释器、动态库和图形驱动，不以升级依赖作为默认修复。

Nix shell 为 manylinux wheel 设置 `LD_LIBRARY_PATH`。在 shell 外直接使用 `.venv/bin/python`，可能因缺少 `libstdc++.so.6` 等动态库而失败。首次准备环境及构建隔离环境可能联网；锁文件不等于离线依赖缓存。

Flake 为 `x86_64-linux`、`aarch64-linux`、`aarch64-darwin` 提供 shell、formatter、预览包装入口及固定测例资产下载，不提供应用构建包或 `nix flake check` 检查项。`nix run . -- --scenario scenarios/review.json` 是预览的 uv 包装入口，须在源码目录运行。

## 图形环境

实际运行使用 Godot 4.7.2 stable 标准版和 Compatibility 后端，需要图形会话及 OpenGL 3.3 驱动。`CHONGMING_GODOT` 指定可执行文件；Nix shell 已设置该变量。Godot `--headless` 使用 dummy renderer，只能用于逻辑／协议测试，不能替代像素验证。`chongming-center` 不加 `--ui` 仍需要渲染器；视频和成像的区别见[算法运行](user-guide.md#算法运行)。

Linux 无桌面环境可使用软件渲染：

```sh
nix develop --no-update-lock-file .#software --command xvfb-run -a uv run --frozen chongming-scene \
  --scenario scenarios/empty.json --output results/software-example --no-video
```

`software` shell 配置 Mesa/llvmpipe，不安装宿主显卡驱动。非 NixOS 的 GPU 驱动接入需要按宿主环境配置。软件渲染验证不能替代真实 GPU 性能测量或人工视觉检查；Flake 的平台声明不保证宿主图形环境可用。

## 检查

先执行与改动相关的测试，涉及公共契约、运行或打包时再运行完整回归。在开发 shell 内：

```sh
ruff check src tests scripts
ruff format --check src tests scripts
uv run --frozen mypy src tests
uv run --frozen python -m unittest tests.test_architecture -v
for scenario in scenarios/*.json; do
  uv run --frozen chongming-center --scenario "$scenario" --check
done
```

完整测试需要图形环境和 Nix shell（提供 Godot）：

```sh
nix develop --no-update-lock-file --command uv run --frozen python -m unittest discover -s tests -v
```

无桌面 Linux 可从源码根目录执行：

```sh
nix develop --no-update-lock-file .#software --command xvfb-run -a uv run --frozen python \
  -m unittest discover -s tests -v
```

检查失败时区分本轮引入的问题与原有问题，并报告原因及未验证项。不要通过放宽超时、弱化断言或返回假结果使检查通过。

PR 前执行上述静态检查、完整回归、全部发行场景校验和格式化，并记录命令与结果。几何、渲染或资产行为变更需要覆盖对应路径，场景 `--check` 通过不能代替真实运行。

### 中心回归

纯中心组件和虚拟误差测试不需要图形会话，在开发 shell 内执行：

```sh
uv run --frozen python -m unittest \
  tests.test_fusion_ingress tests.test_fusion_geometry tests.test_fusion_matching \
  tests.test_fusion_motion tests.test_fusion_uncertainty tests.test_fusion_tracking \
  tests.test_ray_errors tests.test_architecture -v
```

`tests.test_reference_fusion` 覆盖在线插件、真实 CLI、多目标交叉与漏检、异步站点、乱序／延迟、静默恢复和带噪校准状态。它包含引擎启动测试，需要图形环境：

```sh
nix develop --no-update-lock-file .#software --command xvfb-run -a uv run --frozen python \
  -m unittest tests.test_reference_fusion -v
```

组件测试还校验解析加权解、精确／奇异约束、搜索截断、稳健降权，以及独立光心／角度误差的 Monte Carlo 协方差。测试场景使用真值作离线断言，插件注册和在线输入不得携带正确对应、目标 ID 或生成参数。

### 设备与可视化回归

组件测试校验检测／跟踪状态、校准历史、反投影与独立误差、可视化适配及原始投票：

```sh
uv run --frozen python -m unittest \
  tests.test_detection tests.test_device_pipeline tests.test_device_rays \
  tests.test_visualization tests.test_voxel_diagnostics -v
```

真实图形回归需要图形会话；无桌面 Linux 使用：

```sh
nix develop --no-update-lock-file .#software --command xvfb-run -a uv run --frozen python \
  -m unittest tests.test_godot_visualization tests.test_godot_visualization_layers \
  tests.test_godot_evaluation tests.test_godot_voxels tests.test_center_ui \
  tests.test_edge_runtime tests.test_visualization_integration -v
```

这些测试覆盖逐步确认、精确整数、重复更新、准确参考时刻、空输出、原始体素、支持选择、跨模式清空、断连留证，以及所有调试图层前后的传感器像素不变。断言针对实际 Godot 计算和真实图像链路，不以 Python 重算或虚拟射线替代。

`tests.test_edge_runtime` 的 `real_scene()`／`policies()` 是显式合成 fixture：0.81 s、5 Hz、关闭植被／风／云，球从 `[-65,62,20]` 以 `[35,0,0]` 进入预热后的视野；已知像素误差、零角残差均为测试假设。这些 fixture 不是设备运行默认配置，也不代表原始 `center_clean.json` 在动态背景中有相同效果。站点相位差 25 ms、无运动先验的负例要求保留对齐失败，不放宽中心门限。

### 已知检查失败

模式 2–4 实现验收（代码归入 `5e101b7`）中，完整回归 483 项有 480 项通过、2 项失败、1 项超时；三项在改动前 HEAD 的独立副本复现：

| 测试                                                                                   | 当时结果                           |
| -------------------------------------------------------------------------------------- | ---------------------------------- |
| `TrackingTests.test_kalman_update_matches_known_scalar_gain_and_joseph_covariance`     | Kalman 状态与标量增益断言不符      |
| `GodotWorkspaceTests.test_read_only_package_project_uses_a_separate_working_directory` | 只读包工作目录路径断言失败         |
| `CenterDevelopmentTests.test_cli_can_reset_after_finite_end_and_closes_both_sessions`  | CLI 子进程超过测试原有的 15 s 超时 |

全仓 Ruff 格式检查另有 `tests/test_fusion_tracking.py` 的既有排版差异。以上不是豁免项，后续检查须重新报告实际结果。完整隔离发行安装及 `--render` 检查已通过，不能将工作目录单测失败等同于安装验收失败。真实 GPU、桌面人工交互等未验证项集中见[能力边界](design.md#验证与能力边界)。

### 格式化

统一入口为 `nix fmt`，由 `treefmt.toml` 调度 nixfmt、Ruff 和 Prettier：

```sh
nix fmt
nix fmt -- docs/development.md
nix fmt -- --no-cache --fail-on-change
```

最后一条命令仍会改写不合规文件，并以失败状态报告改动，不是只读检查。不要用 `--excludes` 临时追加规则，它会覆盖配置中的全局排除项；选择文件时直接传路径。

格式化涵盖 Python、Nix、JSON、Web 文件及 Markdown，不执行 lint 自动修复。缓存、输出、锁文件及不匹配的原始 OSM/HGT、Godot 文件不参与格式化。JSON 资产排版不会改变内容摘要；具体规则见[用户指南](user-guide.md#json-资产)。

## 外部资产管理

地形、模型、纹理和外部 Godot 工程须先准备为本地固定快照，运行时不依赖在线地图或资产下载。记录来源、版本或日期、原始文件摘要、处理命令及许可；关键资产经授权后可用 Git LFS 或固定 Nix 输入管理，不能将本地资源自动加入仓库或发行包。

Hannover 固定预设只保存配置与内容身份，不代表仓库已收录真实地图或 DEM。准备资产时保留原始输入、导入报告和独立备份；在线重新下载不保证相同内容，不以修改预设摘要绕过校验。JSON 内容摘要与文件字节摘要的用途见[用户指南](user-guide.md#json-资产)。

外部场景的 `--check` 只验证 Python 配置，不检查资源导入或 Godot 渲染。正式录制前应单独验证场景加载，保存外部工程提交与资源摘要，并保留各素材许可。Truck Town 的挂载方法见[用户指南](user-guide.md#外部-godot-场景)。

## 中心算法接入

`ports.development.CenterAlgorithm` 定义同步插件边界，JSON 不加载可执行代码。工厂由 `--algorithm module:factory` 显式指定，不带参数地创建算法对象：

| 方法                  | 输入与职责                                                                                                             |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `start(registration)` | `CenterRegistration` 含 `stations`、`center_session_id`、`policy`、`clock_mapping` 和 `started_at`，没有场景定义或真值 |
| `process(step)`       | `CenterInputStep` 含校准解、普通到达事件、主相机完成进度、处理时刻和有限结束标记；返回 `CenterAlgorithmResult`         |
| `close()`             | 释放算法资源；参考实现逆序停止已启动模块，重复关闭安全                                                                 |

`CenterAlgorithmResult` 的 `measurements`、`tracks` 为展开结果，`batches` 保留完整 `FusionBatchResult`，包括准入、水位、候选、关联和几何诊断。仅返回二元元组不符合接口。工具先交付校准解，再交付引用它的事件和进度，有限末尾另有 `stream_ended` 步。

同一插件可用于模式 3／4：前者使用普通虚拟事件，后者使用设备实际事件；模式 2 拒绝插件。加 `--ui` 后共用 `visualization.py` 的顶层结果适配和 Godot 诊断，不要求插件实现另一套传输或评分。体素导出目前仅适配 `ReferenceFusionAlgorithm`，不从插件候选框推造体素。更新配对、真值隔离和显示口径见[设计说明](design.md#godot-可视化与误差诊断)。

`reference.create_algorithm()` 构造参考实现；已有 `ports.fusion.FusionEngine` 可用 `reference.FusionEngineAdapter` 适配。`composition/center.py` 只根据在线策略构造依赖，不读取场景或启动业务；插件负责启动和关闭。准入与窗口在 `fusion_ingress.py`，候选与细化在 `fusion_geometry.py`，关联与排他搜索在 `fusion_association.py`／`fusion_matching.py`，数值估计和对齐在 `fusion_estimation.py`／`fusion_motion.py`，跟踪及编排在 `fusion_tracking.py`／`fusion_engine.py`，均位于 `modules/`。

`reference_policy()` 提供参考阈值；开发工具的窗口宽度取最小配置相机曝光周期。调整稳健损失、准入、水位或跟踪门限需要 Python 装配，authoring JSON 的 `search` 只配置搜索范围与步长，CLI 不提供任意策略覆盖。自定义驱动或插件应保存实际使用的完整策略与时钟映射，不能修改内部策略后仍把原注册配置当作复现实验记录。算法语义见[设计说明](design.md#中心融合与三维跟踪)，默认值和输出读取方法见[用户指南](user-guide.md#参考融合与跟踪)。

## 源码与修改边界

| 路径                                                                     | 职责                               |
| ------------------------------------------------------------------------ | ---------------------------------- |
| `src/chongming/contracts/`                                               | 不可变数据结构、单位、坐标与状态   |
| `src/chongming/ports/`                                                   | 算法和生命周期接口                 |
| `src/chongming/modules/`、`composition/`                                 | 模块实现、架构桩和依赖装配         |
| `src/chongming/scenarios/`                                               | 场景编译、资产、OSM 导入、运行证据 |
| `src/chongming/rendering/`、`web/`                                       | Godot 资源、渲染适配与预览页面     |
| `src/chongming/calibration.py`、`joint_calibration.py`                   | 辅助视觉标定与独立真值评估         |
| `src/chongming/development.py`、`edge_runtime.py`                        | 模式 2–4 驱动、真实设备输入与证据  |
| `src/chongming/center_ui.py`、`visualization.py`、`voxel_diagnostics.py` | 结果发布、只读适配与原始投票导出   |
| `scenarios/`                                                             | 可编辑示例与小型固定资产           |
| `tests/`、`scripts/`                                                     | 回归测试、实验与发行工具           |

优先实现当前功能，保持简单调用关系和明确的数据所有权。不为假想需求新增框架、兼容层或兜底，不重复实现已有边界校验。性能优化以测量为依据；原生后端只替换热点，不改变契约。协作约束见 [AGENTS.md](../AGENTS.md)。

实验结果写入 `output/` 或 `results/`，临时验证放入 `tmp/`；不得覆盖已有结果或将它们纳入发行包。源码文件引用均以上表中的源码树为准；用户归档内的源码位于随附 sdist，需解压后查看。

## 本地发行

工具只构建和验证本地文件，不上传、打标签或创建远程发布。在开发 shell 内使用独立输出目录：

```sh
uv run --frozen python scripts/distribute.py build --output dist/release
uv run --frozen python scripts/distribute.py check --output dist/release --render
```

无图形环境时可省略 `--render` 完成安装与预览 HTML 检查，但这不验证 Godot 运行。Linux 软件渲染的完整检查：

```sh
nix develop --no-update-lock-file .#software --command xvfb-run -a uv run --frozen python \
  scripts/distribute.py check --output dist/release --render
```

按 `pyproject.toml` 的项目版本生成五个文件：

| 文件                                   | 内容                                                                             |
| -------------------------------------- | -------------------------------------------------------------------------------- |
| `chongming-<version>.tar.gz`           | sdist，含源码、测试、脚本、场景、指南、`AGENTS.md`、锁文件、构建配置及 `COPYING` |
| `chongming-<version>-py3-none-any.whl` | 从 sdist 构建的包、Web/Godot 资源、命令入口和许可证，不含根目录场景及指南        |
| `chongming-<version>-user.tar.gz`      | 所有 Markdown、场景及资产、`COPYING`、sdist、wheel 和 `requirements.txt`         |
| `requirements.txt`                     | 从 sdist 的锁文件导出的运行依赖及哈希，不含项目本身与开发依赖                    |
| `SHA256SUMS`                           | 以上四个文件的字节摘要                                                           |

`SHA256SUMS` 位于输出目录，不在用户归档内；分发用户归档时应同时提供其校验值。摘要只能检查完整性，不证明发布者身份。版本号不代表已经公开发布。

构建可分为 `--stage sdist`、`wheel`、`bundle`；后两步分别要求已有 sdist、已有 sdist 与 wheel。重试时使用同一源码快照，源码改变后从 sdist 重新构建。输出目录只能包含本版本预期的五个普通文件；工具拒绝其他内容，不自动清理目录。

检查可选 `--stage contents` 或 `install`，默认 `all` 另从 sdist 重建 wheel 并逐文件比较。完整检查覆盖：

- 归档路径、清单、校验和、许可证和命令入口；强制要求 `ui.gd`、`visualization.gd`、`evaluation.gd`，源包与 wheel 同时漏文件也拒绝。
- 临时环境中按哈希安装锁定依赖和普通 wheel，执行 `uv pip check`。
- 独立工作目录、隔离解释器 `-I` 下的导入、全部发行场景和预览 HTML；`--render` 另检查真实传感器绘图、有限证据和模式 3 中心 UI 的连续确认、测量与非空原始体素。
- 运行前后包文件摘要一致，不依赖源码目录或向安装包写入缓存。

`--stage contents` 仅检查内容，不等价于完整安装／渲染检查。模式 2／4 的真实图像、设备输出与中心输入一致性由上述集成回归覆盖；发行探针不会擅自配置设备参数来替代该验收。

发行工具按 POSIX 的 `bin/python` 布局执行安装检查；Windows 原生发行验证尚未支持。文档或许可证改名时，同步更新 `pyproject.toml`、`scripts/distribute.py` 和 `tests/test_distribution.py`。发布前确认当前检查结果、许可与资源收录范围。

## 实验复查

在开发 shell 和图形会话中运行，输出目录应按实验分别命名：

```sh
uv run --frozen python scripts/review_photography.py --output output/photography-review --samples 6
uv run --frozen python scripts/review_vegetation.py --output output/vegetation-review --samples 12
uv run --frozen python scripts/review_osm.py --scenario scenarios/osm_landscape.json --output output/osm-review --samples 3
uv run --frozen python scripts/run_hannover_calibration.py --scenario scenarios/center_clean.json \
  --position-offset 0.005 0 0 --max-steps 3 --output output/calibration-review/report.json
```

最后一个脚本也可评估合成场景，不要求 Hannover 数据。它记录普通辅助图像的识别、姿态估计、状态机、墙钟及进程 CPU 耗时，估计完成后才读取真值计算误差。采样不足、没有有效估计与误差为零必须区分。

记录场景、分辨率、硬件、驱动、代码及依赖版本、首次请求和暖机样本。`gpu_ms` 是 Godot 内更新、同步、绘图及回读的墙钟时间；`gpu_submissions` 是像素请求数，不是 GPU 硬件计时或驱动提交数。自动测试检查几何、时间和状态关系，不维护跨 GPU 像素 golden。

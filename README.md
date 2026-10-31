# chongming

`chongming` 是白天可见光条件下的多摄像站小型空中目标算法开发与测试项目。主相机观测目标，辅助相机观测固定参照，设备端生成世界射线，本地 PC 执行三维定位与跟踪。

项目包含虚拟场景、参考业务算法和 Godot 可视化。当前验证限于指定合成场景，不提供目标类别识别，也不承诺探测距离、部署精度、硬件成本或外场闭环能力。

## 系统架构与数据流

```text
主辅图像 → 设备检测、校准与射线生成 → 中心融合、三维定位与跟踪
                                       ↑
                              独立测试时可改用虚拟射线

算法结果 ─┐
独立真值 ─┴→ Godot 自由视角与误差诊断
```

Python 编译场景、维护实验时钟、生成图像请求与真值、执行算法并保存证据。Godot 渲染传感器图像，显示算法结果，计算对应时刻的匹配误差；真值不进入在线算法。预览和算法运行共用 Godot 窗口，预览另保留浏览器工作台。

| 模式        | 输入与处理                                           | 入口                        |
| ----------- | ---------------------------------------------------- | --------------------------- |
| 1：预览     | 预览、编辑场景，不运行设备与中心算法；可检查辅助校准 | `chongming-preview`         |
| 2：设备测试 | 实际渲染图像 → 设备算法 → 射线与诊断，不启动中心     | `chongming-center --mode 2` |
| 3：中心测试 | 普通虚拟射线 → 中心算法 → 测量、轨迹与诊断           | `chongming-center --mode 3` |
| 4：集成测试 | 实际渲染图像 → 设备算法 → 中心算法                   | `chongming-center --mode 4` |

模式 2–4 加 `--ui` 显示 Godot 结果与误差诊断。模式 2／4 必须显式提供逐站设备策略，不以虚拟射线替代失败的图像处理。算法默认值和命令见[用户指南](docs/user-guide.md#算法运行)。

## 当前实现

- 独立主辅采样、统一实验时间、Godot 4.7.2 渲染、预览编辑及有限证据输出。
- MOG2 背景检测、连通域质心、站内关联；OpenCV 棋盘校准、历史查询、状态机与世界射线生成。
- 中心准入、曝光窗口、体素候选、跨站关联、加权定位和恒速跟踪；已有速度先验可用于异步对齐。
- 真值、射线、测量、轨迹和原始体素图层，缓存支持射线高亮，以及门限内匹配样本的指向／定位误差。
- 本地 OSM／高度网格、JSON 资产校验、卡片植被和简化摄影环境。

直接几何候选、通用传输与系统编排仍有未实现模块。误差诊断不提供完整检出率、召回率、轨迹评分或综合分数；真实场景效果及性能边界见[设计说明](docs/design.md#验证与能力边界)。

## 源码快速开始

需要 Nix，并启用 `nix-command` 和 `flakes`。在源码根目录执行：

```sh
nix develop --no-update-lock-file
uv sync --frozen
uv run --frozen chongming-center --scenario scenarios/center_clean.json --check
uv run --frozen chongming-preview --scenario scenarios/review.json
```

关闭预览后，可运行带 Godot 诊断的模式 3；该入口默认使用参考中心算法：

```sh
uv run --frozen chongming-center --scenario scenarios/center_clean.json \
  --output results/center-ui --ui
```

首次准备环境需要联网。实际运行需要图形会话和 OpenGL 3.3；关闭视频不免除引擎初始化要求。无桌面 Linux 见[图形环境](docs/development.md#图形环境)，普通 wheel 安装见[安装](docs/user-guide.md#安装)。

## 文档

- [用户指南](docs/user-guide.md)：安装、模式、设备策略、图层、场景、资产与输出。
- [开发指南](docs/development.md)：环境、检查、算法接入及本地发行。
- [设计说明](docs/design.md)：模块边界、数据契约、显示与误差口径、能力限制。
- [Agent 工作约定](AGENTS.md)：信息导航及协作规则。

## 许可与使用责任

代码以 GNU Affero General Public License v3.0 发布，许可证全文见 [COPYING](COPYING)。第三方依赖和素材适用各自许可证；OSM、DEM 与合成资产的署名要求见[数据许可](docs/user-guide.md#数据许可)。

植被使用 Shapespark Low-Poly Exterior Plants Kit，适用 CC0 1.0 Universal；来源、随包素材及修改说明见[素材说明](src/chongming/rendering/godot/assets/README.md)。

维护者不主动将本项目用于军事活动、武器系统集成、大规模监视或侵犯个人隐私，也不鼓励上述用途。这是项目立场，不是 AGPLv3 的附加许可条款。设计默认原始视频不离开设备端，但设备仍可能采集、缓存或导出视频；部署者须遵守适用法律并取得必要授权。

项目使用 OpenAI ChatGPT，经 Codex/Pi 辅助开发与编写文档，开发者承担审查责任。项目运行时不使用生成式 AI 或神经网络。相关参考与上游实现的许可边界见[设计说明](docs/design.md#参考与许可)。

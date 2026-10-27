# chongming

`chongming` 研究白天可见光条件下的多摄像站小型空中目标检测、三维定位与时序跟踪。主相机观测目标，辅助相机观测固定参照，设备端生成世界射线，本地 PC 执行中心融合。项目不提供目标类别识别，也不承诺探测距离、精度、硬件成本或外场闭环能力。

## 当前实现

- 可重复的虚拟场景、独立主辅相机采样、Godot 4.7.2 渲染、外部 Godot 场景挂载、预览编辑、视频录制与真值输出。
- 本地 OSM 地景和高度网格资产、JSON 内容摘要校验、卡片植被及简化摄影环境。
- OpenCV 辅助棋盘识别、姿态估计、校准历史与冻结/恢复状态机；预览及联合评估工具可运行该路径。
- 中心端分步开发工具和算法接入端口。默认 `InputInspector` 只检查输入，不执行三维定位或跟踪。

主相机检测、完整设备端射线链路、中心融合及通用系统编排仍有未实现部分。模块声明或初始化成功不代表算法已完成，详见[设计说明](docs/design.md#实现范围)。

## 源码快速开始

需要 Nix，并启用 `nix-command` 和 `flakes`。在源码根目录执行：

```sh
nix develop
uv sync --frozen
uv run --frozen chongming-center --scenario scenarios/center_clean.json --check
uv run --frozen chongming-preview --scenario scenarios/review.json
```

首次准备环境需要联网。预览需要图形会话和 OpenGL 3.3 驱动,默认地址为 <http://127.0.0.1:8765/>。`--no-video` 仍启动 Godot；无桌面 Linux 的运行方法见[开发指南](docs/development.md#图形环境)。

外部 Godot 项目通过 Nix 管理，录制外部场景需设置环境变量：

```sh
# Truck Town 示例
export CHONGMING_GODOT_EXTERNAL_PROJECT="$CHONGMING_EXTERNAL_TRUCK_TOWN"
export CHONGMING_GODOT_EXTERNAL_SCENE="res://town/model/town_model.gltf"
uv run chongming-scene --scenario scenarios/truck_town.json --output recordings/truck_town

# Realistic Environments 海洋场景
export CHONGMING_GODOT_EXTERNAL_PROJECT="$CHONGMING_EXTERNAL_REALISTIC_ENV"
export CHONGMING_GODOT_EXTERNAL_SCENE="res://examples/ocean_only.tscn"
uv run chongming-scene --scenario scenarios/realistic_env_ocean.json --output recordings/ocean
```

可用外部项目及性能特征见 [Nix 外部项目集成](docs/nix-external-projects.md)。

从用户发行归档安装普通 wheel，见[用户指南](docs/user-guide.md#安装)。

## 文档

- [用户指南](docs/user-guide.md)：安装、命令、场景配置、资产与输出。
- [开发指南](docs/development.md)：环境、检查、源码结构及本地发行。
- [设计说明](docs/design.md)：模块边界、契约、实现限制与参考。
- [Agent 工作约定](AGENTS.md)：信息导航及协作规则。

## 许可与使用责任

代码以 GNU Affero General Public License v3.0 发布，许可证全文见 [COPYING](COPYING)。第三方依赖和素材适用各自许可证；OSM、DEM 与合成资产的署名要求见[数据许可](docs/user-guide.md#数据许可)。

植被使用 Shapespark Low-Poly Exterior Plants Kit，适用 CC0 1.0 Universal；来源、随包素材及修改说明见[素材说明](src/chongming/rendering/godot/assets/README.md)。

维护者不主动将本项目用于军事活动、武器系统集成、大规模监视或侵犯个人隐私，也不鼓励上述用途。这是项目立场，不是 AGPLv3 的附加许可条款。设计默认原始视频不离开设备端，但设备仍可能采集、缓存或导出视频；部署者须遵守适用法律并取得必要授权。

项目使用 OpenAI ChatGPT，经 Codex/Pi 辅助开发与编写文档，开发者承担审查责任。项目运行时不使用生成式 AI 或神经网络。相关参考与上游实现的许可边界见[设计说明](docs/design.md#参考与许可)。

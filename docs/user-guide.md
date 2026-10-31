# 用户指南

## 安装

源码使用仓库的 Nix／uv 环境，见[开发指南](development.md#环境与依赖)。本指南的源码命令在开发 shell 和源码根目录执行。

用户发行归档包含 wheel、sdist、带哈希的运行依赖清单、场景及文档。先在发布文件所在目录执行 `sha256sum -c SHA256SUMS`，再解压用户归档；此完整检查需要校验清单中的全部文件。仅下载用户归档时，应将其 SHA-256 与可信发布渠道提供的值核对。

在解压后的用户目录中安装普通 wheel。以下为 POSIX shell 命令，需要 uv 及 Python 3.12 至 3.14：

```sh
uv venv --python python3 .venv
uv pip install --python .venv/bin/python --require-hashes --no-deps --requirements requirements.txt
uv pip install --python .venv/bin/python --no-deps ./chongming-*-py3-none-any.whl
uv pip check --python .venv/bin/python
source .venv/bin/activate
chongming-center --scenario scenarios/center_clean.json --check
```

普通安装不使用 `uv run` 自动同步源码项目。用户目录没有开发脚本和 Flake，需要开发环境时先解压随附 sdist。NixOS 用户应在源码的 Nix shell 内安装和运行，以取得 Python wheel 所需的动态库。依赖安装可能联网，实验运行不下载地景数据。

实际运行另需 Godot 4.7.2 stable 标准版、图形会话及 OpenGL 3.3。设置 `CHONGMING_GODOT` 指向可执行文件，或在 PATH 提供 `godot4`／`godot`；录制与回放另需 FFmpeg，可用 `CHONGMING_FFMPEG` 指定。Nix shell 已配置两者。Windows 的环境路径与激活方式不同，本指南的安装命令和发行检查以 POSIX 环境为准。

下文采用源码形式 `uv run --frozen <command>`；普通安装激活环境后直接执行 `<command>`。场景路径相对当前目录，资产路径默认相对场景文件目录，包内资源不依赖工作目录。

## 命令

| 命令                | 用途                                    |
| ------------------- | --------------------------------------- |
| `chongming-preview` | Godot 预览窗口与浏览器工作台            |
| `chongming-center`  | 场景检查、模式 2–4 的同步分步或图形运行 |
| `chongming-scene`   | 有限虚拟场景及证据导出                  |
| `chongming-osm`     | 本地 OSM XML 清洗与地景导入             |

各命令的完整参数见 `--help`。`chongming-center --check` 不启动渲染、不创建输出；实际运行即使关闭视频也会启动 Godot。运行输出目录必须尚不存在。

### 预览与编辑

```sh
uv run --frozen chongming-preview --scenario scenarios/review.json
```

默认服务地址为 <http://127.0.0.1:8765/>，仅监听本机。`--port 8766` 更换端口，`--no-browser` 不自动打开浏览器，Ctrl+C 关闭服务和渲染进程。`--render-fps 8` 限制墙钟生产节拍并降低像素请求频率，不改变实验时间戳；`--no-video` 停止像素生产，保留逻辑与事件。

从 authoring JSON 启动后，编辑模式取得独占 token 并暂停实验。面板可编辑站点、目标及地景选择；只有提交才原子写回场景文件。提交前检查磁盘字节摘要，文件被外部修改时拒绝覆盖。撤销重新读取磁盘，关闭服务放弃未提交草稿。

启动后同时打开 Godot 预览窗口；关闭窗口与 Ctrl+C 等效。工具栏提供播放/暂停、单步、回到起点、站点选择和真值叠加开关；在时间框输入秒数后按“跳转”或回车，逻辑层从起点重启并快进到该时刻，跳转进行中或编辑期间不接受新的跳转。左侧标签页显示所选站点的主、辅相机画面及自由视角，右侧为信息、编辑和日志面板。日志面板列出最近 200 条 `chongming` 日志及 Python 异常栈，有新警告或错误时标签显示圆点。

自由视角在窗口内的标签页中显示：右键在画面上拖动调整视线，按住右键时 WASD 平移、Q/E 升降、Shift 加速。编辑期间连续绘制草稿，但实验保持暂停；非编辑期间随离散场景步更新。窗口刷新和浏览器轮询不推进实验时间，视锥和标签不混入传感器图像。

省略 `--scenario` 和 `--recording` 可查看内置演示，但不能持久化编辑或使用 `--landscape-asset`。预览会评估已生成的辅助图像并显示标定结果，不调用中心工具的算法插件。

### 算法运行

`chongming-center` 的 `--mode 2|3|4` 同时适用于图形与 CLI，省略时为 3。`--ui` 只选择 Godot 界面，不改变模式；启动后直接运行，有限结束或关闭窗口后退出，不启动浏览器服务。

| 模式 | 输入来源                     | 默认中心算法                                  | 策略与插件                                       |
| ---- | ---------------------------- | --------------------------------------------- | ------------------------------------------------ |
| 2    | 实际渲染主辅图像             | 不启动中心                                    | 必填 `--device-policy`；拒绝 `--algorithm`       |
| 3    | 普通虚拟射线                 | 加 `--ui` 为参考算法；CLI 为 `InputInspector` | 允许 `--algorithm`；拒绝无用的 `--device-policy` |
| 4    | 实际渲染图像，经设备产生事件 | 参考算法                                      | 必填 `--device-policy`；允许 `--algorithm`       |

模式 3 的图形示例：

```sh
uv run --frozen chongming-center --scenario scenarios/center_clean.json \
  --output results/center-ui --ui
```

模式 2／4 需先准备与场景全部站点匹配的[设备策略文件](#设备策略文件)。以下路径须替换为实际文件，各输出目录须尚不存在：

```sh
uv run --frozen chongming-center --mode 2 --scenario scenarios/center_clean.json \
  --device-policy /path/to/device-policy.json --check
uv run --frozen chongming-center --mode 2 --scenario scenarios/center_clean.json \
  --device-policy /path/to/device-policy.json --output results/edge-ui --ui
uv run --frozen chongming-center --mode 4 --scenario scenarios/center_clean.json \
  --device-policy /path/to/device-policy.json --output results/integrated-ui --ui
```

默认关闭传感器显示和证据录像；`--video` 同时开启两者。模式 2／4 无论是否加该参数都生成全部应采样图像并执行设备算法；失败保留诊断，绝不改用虚拟射线。模式 3 的虚拟输入不冒充边缘输出，因此其边缘射线统计为零。

不加 `--ui` 时默认暂停，可交互分步，或加 `--run --tick-hz 10` 连续运行。`--tick-hz` 范围为 `(0, 240]`，只限制墙钟节拍，不改变曝光时刻或保证实际吞吐。交互命令如下：

| 命令                     | 行为                                          |
| ------------------------ | --------------------------------------------- |
| `step`                   | 完成一个曝光组及本模式算法调用                |
| `run 10` / `run`         | 连续十步／至有限终点；Ctrl+C 在完整步骤后暂停 |
| `video on` / `video off` | 改变显示／录像；模式 2／4 的算法成像不受影响  |
| `status`                 | 查看状态                                      |
| `reset`                  | 关闭旧会话，在新目录创建新源与算法实例        |
| `quit`                   | 关闭资源并退出                                |

`InputInspector` 只检查输入，不计算位置。受信任的 Python 工厂通过 `--algorithm my_center:factory` 接入，见[中心算法接入](development.md#中心算法接入)；JSON 不加载代码，在线算法不得读取真值或完整场景配置。

### 设备策略文件

`--device-policy` 是站点 ID 到完整 `DevicePolicy` 的 JSON 对象。字段由 [contracts/device.py](../src/chongming/contracts/device.py) 定义；缺站、多站、重复键、字段缺失／未知、非法类型或不支持策略在创建输出和分配 worker 前拒绝。`--check` 也验证所选模式的策略，但不检查图像质量或算法效果。

下面只展示一个站点的完整结构。数值来自合成组件测试，不是建议参数或部署配置；复制时须改为场景的实际站点 ID，并为每站提供完整对象。例中的未知误差保留 `null`，因此即使检测和校准成功，也不会生成具备必要不确定度的射线。

```json
{
  "example-station": {
    "detection": {
      "algorithm_id": "mog2-components-v1",
      "score_definition": {
        "definition_id": "foreground-component-fill-ratio-v1",
        "kind": "ranking",
        "minimum": 0.0,
        "maximum": 1.0,
        "cross_station_comparison_group": null
      },
      "minimum_component_area_px2": 1.0,
      "maximum_component_area_px2": null,
      "background_warmup_frame_count": 1,
      "background_history_frame_count": 20,
      "background_variance_threshold": 16.0,
      "background_learning_rate": 0.0,
      "background_detect_shadows": false
    },
    "tracking": {
      "algorithm_id": "constant-velocity-nearest-neighbor-v1",
      "association_gate_px": 6.0,
      "confirmation_frame_count": 2,
      "maximum_missing_duration_ns": 500000000,
      "quality_definition": {
        "definition_id": "consecutive-detection-fraction-v1",
        "kind": "ranking",
        "minimum": 0.0,
        "maximum": 1.0,
        "cross_station_comparison_group": null
      }
    },
    "calibration": {
      "update_period_ns": 0,
      "stale_after_ns": 2000000000,
      "recovery_success_count": 1,
      "minimum_reference_count": 4,
      "maximum_reprojection_rmse_px": 2.0,
      "maximum_rotation_only_parallax_px": null,
      "estimator_id": "opencv-checkerboard-v1"
    },
    "rays": {
      "uncertainty_model_id": "tangent_gaussian_v1",
      "fallback_centroid_covariance_px2": null,
      "unmodelled_high_frequency_jitter_covariance_rad2": null,
      "unmodelled_relative_flex_covariance_rad2": null,
      "residual_independence_basis": null
    }
  }
}
```

所有 `*_ns` 字段以纳秒计。背景参数全部显式提供：预热为非负整数，history 为正的 32 位有符号整数，variance threshold 为正有限数，learning rate 为 `[0,1]`（不接受自动值 `-1`），阴影开关为布尔值。跟踪关联门限、确认帧数及最长失联时长均须为正。校准更新周期与过期时长非负，恢复次数为正，最少参照点不少于 4，RMSE 上限为正；可选视差上限非负。检测与跟踪的算法 ID、分数定义须与实现一致。

协方差使用 2×2 对称半正定矩阵，像素项单位 px²，角残差单位 rad²；`null` 表示未知，零矩阵表示显式零误差假设，两者不可互换。当前检测器不给出质心协方差，要产生射线须另提供有依据的像素 fallback 与两项已知残差。非零残差还须填写彼此及与像素误差独立的 `residual_independence_basis`；未经实测的开发假设须明确注明，系统不自动生成依据。模型与校准历史限制见[设备检测与射线](design.md#设备检测与射线)。

### 图层与诊断面板

模式 2–4 的窗口保留站点选择、主辅图像检查（需 `--video`）、自由视角和退出，禁用场景编辑、播放／单步／重启及时间跳转。重新运行或切换模式须退出旧会话，再用新命令启动；不复用旧状态。

“图层”页可分别开关真值、射线、测量、轨迹和体素，查看站点校准状态与姿态差。在下拉框选择测量后，面板显示参考时刻及可显示的支持射线数，并以青色高亮；缺失或过期射线不重建。模式 3 不缓存虚拟输入作为边缘射线，因此可能有测量而无可高亮支持；体素仅支持参考中心的无先验原始投票。颜色、尺寸、保留期限与最后窗口语义见[图形口径](design.md#图形口径)及[原始体素](design.md#原始体素)。这些图层不进入传感器像素，开关与选择不改变算法或累计统计。

“误差诊断”页列出接收、可评估、匹配、未匹配、无法评估数量及匹配样本的均值、RMSE、最大误差，同时显示开发门限。无样本显示“不可计算”，不代表零误差；低匹配样本误差也不代表整体性能较高。匹配与统计口径见[设计说明](design.md#匹配与统计)。窗口内的统计和选择不生成独立评分文件，原始证据仍是复查依据。

### 参考融合与跟踪

CLI 模式 3 需显式启用参考算法；模式 3 的 `--ui` 和模式 4 默认已启用：

```sh
uv run --frozen chongming-center --scenario scenarios/center_clean.json \
  --algorithm chongming.reference:create_algorithm \
  --output results/center-reference --run --tick-hz 240
```

该示例为 30 秒双站单目标实验。`--tick-hz 240` 仅缩短墙钟等待，不改变曝光时刻，也不保证实际运行速率；省略 `--run` 可用 `step`／`run N` 检查阶段结果。无桌面 Linux 的图形环境见[开发指南](development.md#图形环境)。

参考中心执行事件准入、封口窗口、体素候选、多目标关联、加权定位及恒速跟踪。已有有效速度先验可对齐异步站点，新目标仍须具备足够窄的多站曝光支持；异步并不保证能够初始化目标。运动、误差和失败语义见[设计说明](design.md#中心融合与三维跟踪)。

`composition/center.py::reference_policy()` 的主要默认设置如下。这些是实验策略，不是部署精度或性能承诺；自定义策略需要 Python 装配，不能通过修改 authoring JSON 任意覆盖。

| 策略       | 默认设置                                                                                                                            |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| 窗口       | 宽度取最小配置相机曝光周期，允许迟到 0；首次等待及空闲阈值 1 s，离线阈值 3 s                                                        |
| 候选       | `coarse_voxel`，至少 2 个独立站；独立误差扩展系数 3，每射线不确定度扩展及其中心遍历预算 100000                                      |
| 关联       | 时间门限为一个窗口，空间门限 10 m，预测门限 2 m；假设预算 4096，每冲突分量搜索节点 20000                                            |
| 几何       | 交会角门限 0.01 rad，条件数上限 `1e6`，逐射线残差上限 1 m；最多 50 次迭代，位置收敛容差 `1e-8` m                                    |
| 损失与校准 | 默认 `least_squares`；可选 `huber`，尺度 1.5 标准差；VALID／FROZEN／STALE 独立精度乘数为 1／0.25／0.1                               |
| 对齐       | `track_prediction` 保留无先验搜索；新目标曝光中点跨度上限 1 ms                                                                      |
| 跟踪       | 至少 2 次测量确认，确认间隔上限 1 s；已确认轨迹失联 1 s 后进入 `coasting`、3 s 后删除；平方 Mahalanobis 门限 9，初始化空间门限 10 m |

`center-output.jsonl` 每行对应一次同步调用，包含 `step_no`、处理时间、`measurements`、`tracks`、`batches` 和 `center_ms`。顶层测量／轨迹是阶段批次的展开视图，统计时选一层，不能重复计数。`center_ms` 只计插件调用，不包括场景生成和证据写入。

| 结果                                    | 读取方式                                                                                                                |
| --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `measurements[]`                        | 以 `status == "located"` 确认有效位置；读取 `reference_time_ns`、支持站／事件和条件位置协方差                           |
| `tracks[]`                              | 按中心会话内 `track_id` 读取 `tentative`、`confirmed`、`coasting`、`deleted`；首个位置的速度和完整状态协方差可为 `null` |
| `batches[].admissions`、`window_update` | 查看隔离、拒绝、重复、迟到、水位和封口窗口；隔离事件可在引用补齐后重试，迟到证据不重开窗口                              |
| `batches[].candidates`、`associations`  | 查看支持站、未分配射线、竞争假设和搜索截断；空候选不等于全世界没有目标                                                  |
| `batches[].geometry[].results`          | 查看 `ray_weights`、`normalized_residuals`、`robust_weights`、迭代数、收敛与失败原因；残差为 `null` 可表示违反精确约束  |

算法可能因站数不足、关联歧义、搜索截断、近共线、精确约束冲突或求解失败而没有有效位置。先查阶段状态和原因，再判断是否可用；`solver_converged` 本身不代表通过质量门限。条件协方差不包含共享安装／校准误差，零条件协方差也不等于零绝对误差。

运行状态与全系统能力声明分别记录，见[有限导出与输出](#有限导出与输出)。

### 有限导出与输出

```sh
uv run --frozen chongming-scene --scenario scenarios/empty.json --output results/scene-test --no-video
```

省略 `--no-video` 输出高质量 H.264 视频流；`--render-every N` 按各相机独立曝光计数抽取像素，默认每帧绘制。所有逻辑曝光仍被访问，跳过的帧记录 ID 和原因。录制需要 Nix 环境提供的 FFmpeg。

| 文件                           | 内容                                                                          |
| ------------------------------ | ----------------------------------------------------------------------------- |
| `authoring.json`               | 输入的可编辑场景，输入为 authoring 格式时保存                                 |
| `scenario.json`                | 有限导出的编译场景，用于诊断                                                  |
| `manifest.json`                | 运行范围、配置、会话、渲染策略及来源记录；通过 `scenario_path` 和摘要引用场景 |
| `online.jsonl`                 | 普通帧元数据、事件及像素跳过记录                                              |
| `truth-static.json`            | 运行期间不变的特权场景对象快照，仅供评估                                      |
| `truth.jsonl`                  | 逐步真实状态及事件到目标的对应；静态对象通过摘要引用，仅供评估                |
| `timing.jsonl`、`summary.json` | 逐步耗时、计数、结束或失败状态                                                |
| `video/camera-*.mp4`           | 按流编号保存的高质量 H.264 视频，不以用户 ID 构造路径                         |
| `video.json`                   | 视频编码、尺寸、帧率、帧数和文件摘要                                          |

模式 3／4 另保存 `center-registration.json`、`center-input.jsonl` 和 `center-output.jsonl`；模式 2 不创建这些中心文件。模式 2／4 的 `edge-output.jsonl` 逐项保存输入帧元数据及像素 SHA-256、实际校准／主帧结果和步骤完成／失败记录；未开启录像时不保存原始像素。无曝光控制诊断原样留存，不伪造曝光时间。

开发运行 manifest 的 `execution_mode`、`input_source`、`virtual_events_enabled`、`sensor_rendering_required`、`center_algorithm_started` 区分真实路径；实际逐站策略保存在 `run_spec` 中。`algorithms_complete` 保持 `false`，不代表本次设备流程没有执行；`summary.json` 的 `state: "complete"` 只表示有限运行结束，不证明算法输出有效。完整 manifest 与真值不是在线输入。设备、中心或 UI 后续失败均保留已刷新证据，不自动清理目录。

`truth.jsonl` 使用 `truth-jsonl-v2` 布局。每条记录的 `logical_scene_static_ref` 指向同目录的 `truth-static.json`；评估读取器会校验摘要并将静态 `world_objects` 恢复到内存中的完整场景快照。运行期间若静态对象发生变化，运行会失败而不会静默生成错误真值。

manifest 的 `installation` 区分 `editable` 与 `installed`；`package_digests`／`package_digest` 描述实际加载的包文件。Git 提交、工作区状态和 `uv.lock` 摘要仅在可获取的 editable 源码环境记录，普通 wheel 安装对应字段为 `null`。包摘要不是完整源码溯源，发行包的校验和与源码快照应另行保留。

### 录制回放

使用有限导出生成 H.264 证据目录，不加 `--no-video`：

```sh
uv run --frozen chongming-scene --scenario scenarios/empty.json --output results/recording-demo
uv run --frozen chongming-preview --recording results/recording-demo --playback-fps 30
```

回放需要保留 `online.jsonl`、`truth.jsonl`、`truth-static.json`、`timing.jsonl`、`video.json`、`video/` 和 `authoring.json`；旧 PNG 序列不属于当前视频格式。仅有编译后的 `scenario.json` 无法加载，外部资产须按作者配置中的路径和摘要保持可用。

这是实验性预览入口，未恢复普通事件及完整评估数据；到达录制末尾后会转入实时场景源，不保证循环原录像。它仍初始化 Godot，不能作为不启动渲染器的完整回放方案。具体边界见[设计说明](design.md#证据回放)。

`--recording` 与 `--scenario`、`--camera-position-offset` 互斥，不支持场景编辑；`chongming-center` 尚不支持 `--recording`。`--playback-fps` 仅与录制配合使用且须为正数，限制场景步速率，不保证相机或浏览器帧率。省略时回放源不主动等待，预览仍受 `--render-fps` 限制。

## 场景配置

人工编辑格式为 `scenario-authoring-v2`。`load_scenario()` 编译该格式；`scenario-json-v1` 是诊断用运行图，由 `load_compiled_scenario()` 显式读取。二者用途不同。

示例均位于发行包或源码的 `scenarios/`：

| 场景                                         | 用途                                   |
| -------------------------------------------- | -------------------------------------- |
| `center_clean.json`                          | 双站单目标、30 秒中心输入              |
| `constant_velocity.json`、`empty.json`       | 交叉直线运动、无目标                   |
| `heightfield.json`、`heightfield_asset.json` | 内嵌与外部山脊网格                     |
| `review.json`、`photography.json`            | 三类形状、摄影环境与相机抖动           |
| `osm.json`、`osm_landscape.json`             | 合成建筑及完整地景，不是真实地图       |
| `truck_town.json`                            | 本地外部 Godot 城市，需另行准备工程    |
| `aircraft_model_test.json`                   | 飞行器模型配置检查，不代表完整运行验收 |

坐标为 ENU 东／北／上，距离单位米，时间单位秒。相机系为右／下／前。基础字段如下，未知字段或非法值会报错：

| 字段                                                  | 约束或用途                                                                                                               |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `format`、`scenario_id`                               | 格式及场景标识                                                                                                           |
| `random_seed`                                         | 固定为 42，可省略                                                                                                        |
| `duration_s`、`fps`                                   | 正时长，默认 24 秒；采样率默认 30 Hz，每站支持 1 至 60 Hz                                                                |
| `stations[]`                                          | 1 至 4 站，唯一 `id`、`position_enu_m`；可选 `main_yaw_deg`、`main_elevation_deg`、`auxiliary_yaw_deg`、`fps`、`start_s` |
| `targets[]`                                           | 唯一 ID、`geometry`、`motion`；`[]` 表示无目标                                                                           |
| `geometry`                                            | `sphere` + `radius_m`、`box` + `half_extents_m`，或 `aircraft` + 可选 `model`，运行边界见下文                            |
| `motion`                                              | `constant_velocity` + 位置/速度，或 `orbit` + 中心/半径/周期，可选相位及垂直振幅                                         |
| `world_objects[]`                                     | 静态物体；`[]` 清空内置地物，仍保留各站 A4 标志物                                                                        |
| `terrain`                                             | `flat` 或 `heightfield`；网格使用原点、间距、内嵌高度或外部资产                                                          |
| `vegetation[]`、`wind`                                | 植被类型及尺寸、平均风和湍流；`[]` 清空植被，`null` 关闭风                                                               |
| `search`                                              | `min_enu_m`、`max_enu_m`、`step_enu_m`；搜索盒外目标仍可产生射线                                                         |
| `environment`、`camera_jitter`、`clouds`、`rendering` | 可选摄影、抖动、云及渲染效果                                                                                             |
| `occlusion_mode`                                      | `known`（默认，按规范化几何计算遮挡）或 `unknown`（不判定遮挡，仅按目标是否在视场内生成候选）                            |
| `camera_position_offset_enu_m`                        | 对真实主辅光心共同施加位移，不修改在线安装配置，用于视差评估                                                             |
| `osm`                                                 | 本地 `asset` 引用与可选 `vegetation` 策略                                                                                |

省略地物、目标、植被或风字段时可能保留演示默认值；空场景应显式使用空数组及 `wind: null`。主相机为 1920×1080、对角 FOV 60°；辅助相机为 640×480、水平 FOV 2.6°，标称水平朝向 10 m 外与站点 ID 绑定的横向 A4 棋盘。方位角 0° 向北，正向东；显式抖动可使瞬时辅助姿态偏离水平。输出为无 alpha 的 sRGB RGB8。

### 几何类型

作者格式接受以下 `geometry.type`：

| 类型       | 参数                                                                            |
| ---------- | ------------------------------------------------------------------------------- |
| `sphere`   | `radius_m`，正半径，米                                                          |
| `box`      | `half_extents_m`，`[x, y, z]` 正半宽，米                                        |
| `aircraft` | 不指定 `model` 时使用内置三角网格；`model: "f86"` 或 `"la11"` 选择随包 GLB 模型 |

例如目标的几何字段可写为：

```json
{ "type": "aircraft", "model": "f86" }
```

`rendering/geometry.py` 读取 GLB 网格，应用网格节点的旋转、缩放和平移，并转换到机体系（x 右、y 机头、z 上，与内置网格一致）后，同一份顶点同时用于 Python 求交、包围盒、投影和 Godot 渲染；Godot 不再自行加载 GLB。Godot 以目标的 `appearance` 覆盖 GLB 自带材质，贴图不显示。

`model` 不是任意文件路径，作者格式不支持 `geometry.asset`。

内部 `CylinderGeometry(radius_m, height_m)` 用于植被等圆柱代理，不是作者格式可选的 `geometry.type`。

### 外部 Godot 场景

Truck Town 示例使用 [Godot demo projects 的 `3d/truck_town`](https://github.com/godotengine/godot-demo-projects/tree/master/3d/truck_town)。先在本地准备并固定上游提交，不将外部工程复制进本仓库。以下命令只挂载静态城市场景，相机和飞行物仍由 Python 实验时钟控制：

```sh
CHONGMING_GODOT_EXTERNAL_PROJECT=/path/to/godot-demo-projects/3d/truck_town \
CHONGMING_GODOT_EXTERNAL_SCENE=res://town/model/town_model.gltf \
uv run --frozen chongming-scene --scenario scenarios/truck_town.json \
  --output results/truck-town
```

两个环境变量必须同时设置，场景路径须以 `res://` 开头且位于工程资源中。渲染器复制外部资源到临时工程，再覆盖本项目 worker，忽略外部 `project.godot` 和导入缓存，并重新导入资源。输入工程不被修改；车辆控制、物理和 UI 不属于该示例的验证范围。

`truck_town.json` 使用 `occlusion_mode: "unknown"`，外部网格不提供 Python 遮挡真值。录制可保留相机、目标状态和图像，只能在同一固定录像内比较系统，不能据此声称已校正遮挡或保证绝对准确率。录像读取沿用[录制回放](#录制回放)的限制。

Godot demo 仓库采用 MIT 许可，模型和纹理仍须核对各自 `license.txt`。正式录制应另存工程提交、资源摘要和许可说明，不能仅依赖本项目包摘要复现外部场景。

### 摄影与植被

`photography.json` 提供可运行配置。太阳方向 `sun_direction_enu_unit` 在编译时归一化；天空色为 0 至 1 的线性 RGB，照度非负。正值 `atmospheric_visibility_m` 启用大气透视；`sun_angular_diameter_deg` 范围 0 至 5，默认 0；`glare_strength` 范围 0 至 2，默认 0。

`camera_jitter` 使用 `angular_amplitude_deg`（右／下／前轴）、`pixel_amplitude_px`（水平／竖直）、正值 `frequency_hz` 和 `roles`（`main`／`auxiliary`）。角幅度与按焦距换算的像素幅度叠加，直接改变物理姿态，不以图像平移模拟。`camera_position_offset_enu_m` 与旋转抖动分别表达平移和旋转。

`clouds` 最多两层，各有正值 `layer_altitude_m`、0 至 1 的 `optical_density` 和 `drift_velocity_enu_mps`。太阳、大气和云是未标定的视觉近似，不改变普通射线的几何可见性。雨、气象雾、通信扰动及滚动快门尚未实现。

`rendering` 可配置地表细节、SSAO、边缘抗锯齿和光束：`terrain_detail_strength` 为 0 至 1，默认 0.35；`ssao_intensity` 为 0 至 4，`god_rays_strength` 为 0 至 2，二者默认 0。`fxaa_enabled` 为布尔值，默认关闭；当前抗锯齿为四邻域边缘混合，不是 Godot 原生 FXAA。

植被公共字段为 `id`、`position_enu_m`，可选外观和遮挡开关：

| `type`         | 尺寸及参数                                                            |
| -------------- | --------------------------------------------------------------------- |
| `tree`（默认） | `height_m`、`crown_radius_m`，可选摆幅和频率                          |
| `shrub`        | `height_m`、`radius_m`                                                |
| `grass`        | `size_m`，可选 `blade_height_m`、`density_per_m2`；默认不遮挡逻辑视线 |

植被使用随包分发的 CC0 卡片资产，包括 12 个乔木、6 个灌木／绿篱和 12 个草／地被变体，无需运行时下载。品种按世界坐标噪声与确定性哈希选择，逻辑遮挡仍使用圆柱代理。

乔木按投影高度切换 LOD：约 45 px 以下使用无阴影、无动画的交叉 billboard；较大尺寸使用完整卡片网格，阴影和风摆分别在约 75 px、60 px 以下关闭。灌木也随屏幕尺寸关闭阴影和风摆；乔木与灌木最终在 `vegetation_lod_far_m` 后剔除，草在 `vegetation_lod_near_m` 后剔除，要求 `far > near > 0`。切换不渐变，未实现全局合批；每个草 patch 最多 256 个草簇。

近景树冠、灌木和草按实验时间、平均风及确定性湍流做整体平移／缩放，树干静止；不模拟枝叶级形变或真实风场。

## JSON 资产

外部高度网格及 OSM 地景均使用以下引用形式：

```json
{
  "uri": "assets/ridge.json",
  "content_sha256": "6744c8b5dd9c1494b5828de16938651560686bd7b9e155a1a3fd73acfce4bd03"
}
```

URI 支持本地相对路径、绝对路径和不含主机的 `file:` URI，不接受远程地址、查询或片段。默认以场景文件目录为资产根；Python API 可显式指定 `asset_root`。

高度资产形如 `{"heights_m": [[0, 1], [2, 3]]}`，至少为 2×2 矩形，数值有限且非负。`origin_enu_m`、正值 `spacing_m` 留在场景中；行从南到北，列从西到东，高度相对基础高程，网格外使用基础平面。

在源码目录计算内容摘要：

```sh
uv run --frozen python scripts/hash_json.py scenarios/assets/ridge.json
uv run --frozen chongming-center --scenario scenarios/heightfield_asset.json --check
```

真实内容修改后，将第一条命令输出的摘要更新到引用并校验场景；仅排版无需更新。普通 wheel 用户可使用公开函数 `content_sha256(parse_json(path.read_bytes()))`，二者位于 `chongming.scenarios.json_content`。

内容身份采用项目自定义规则，不是 RFC 8785/JCS：

- 规范字节为紧凑 UTF-8 JSON，无 BOM、尾换行；对象键按 Unicode 码点排序，使用 Python `ensure_ascii=False` 转义规则。
- 数组顺序和字符串码点保持不变，不执行 Unicode 归一化。
- `1`、`1.0`、`1e0` 等价，所有带符号零归为 `0`；所有整数值限于 `±(2**53 - 1)`。
- 数字转为 binary64 及其最短十进制写法后必须保持原十进制值；拒绝精度损失、溢出和下溢，不静默舍入。
- 拒绝重复键、非有限数、孤立代理码点及非法 JSON/UTF-8。摘要为规范字节的 64 位小写十六进制 SHA-256。

加载器单次读取、解析并验证，消费者使用同一份已验证值。源码/下载文件、发行包、图像和编辑并发检测仍使用字节摘要；不能用 `sha256sum` 的文件摘要替代 `content_sha256`。

## OSM 地景

导入仅读取固定本地 OSM XML 0.6，输出 `osm-landscape-v2`，导入器为 `chongming-osm-2`。`--source-url` 仅记录来源，不发起网络请求。PBF 如需使用，应先在准备阶段转换并保存原文件摘要及转换命令。

合成 fixture 的离线导入示例：

```sh
uv run --frozen chongming-osm scenarios/assets/osm_demo.osm \
  --config scenarios/assets/osm_import.json \
  --source-url 'repository:scenarios/assets/osm_demo.osm (synthetic fixture)' \
  --data-date 2026-10-20 --license CC0-1.0 \
  --attribution 'Synthetic fixture, CC0-1.0; not an OpenStreetMap extract' \
  --output output/osm/demo.buildings.json --cleaned-output output/osm/demo.cleaned.osm
```

报告默认将输出文件的扩展名替换为 `.report.json`，如 `demo.buildings.report.json`，含 `asset_content_sha256`、源 XML 与清洗 XML 的字节摘要、配置、诊断、统计和耗时。将资产路径及报告的内容摘要写入 `osm.asset`；完整合成地景也可由 `scripts/prepare_osm_demo.py` 生成，其 stdout 提供 `content_sha256`。

配置明确 WGS84 锚点、基准历元、椭球高、west/south/east/north 包围框及高度策略。导入结果经 ECEF 转为局部 ENU；运行场景与站点使用同一锚点，手写坐标不自动重投影。

- 建筑支持闭合、无自交的单外环和平顶挤出。高度依次取 `height`、`building:levels × level_height_m`、配置默认值，并记录回退。跨界建筑、关系建筑、内洞和 building parts 不生成任意补面。
- 道路按中心线和标签/类别宽度生成路面，不生成交通图；桥、隧道和非零抬升层拒绝并记录诊断。
- 树点、森林、绿地和水体可导入；自然地表支持有效 multipolygon 及内洞。缺失关系成员整体拒绝，道路和地表裁剪到选区。
- 清洗 XML 是本导入器的重导入输入，不是完整 OSM 区域备份；保留原始下载文件及其摘要。

可选 DEM 参数包括 `--dem-hgt`、`--dem-tile`、`--dem-source-url`、`--dem-data-date`、`--dem-license`、`--dem-attribution`、`--dem-spacing`。支持本地 1201²／3601² 大端 HGT 或 gzip，按 EGM96 正高处理；相对锚点 `reference_height_m` 生成地形，默认间距 60 m。范围外、void 和超过 250000 样本的网格拒绝。复合 DEM 不等于裸地测量，贴地水体不代表真实水位。

`osm.vegetation` 控制额外乔木、灌木及草 patch。森林基础树及 OSM 树点计入目标密度，不重复种植；新增实例排除建筑、道路、水体、已有树冠和 DEM 陡变。主要默认值为：森林／绿地乔木 200／12 株每公顷，灌木 55／30，草 patch 35／40；`seed` 为 42，尺寸、团簇及预算字段见源码 `OSMVegetationConfig`。预算仅约束新增实例，策略属于场景，不改变 OSM 资产内容摘要。

候选排序叠加低频密度噪声形成斑块，约 16% 的新增乔木按确定性哈希缩小为幼树／下木；相同资产与配置的生成结果可重复，不依赖墙钟。

### Hannover 固定预设

仓库只保存选区配置 `scenarios/assets/hannover_500m_import.json`，不分发真实样区 JSON 或下载文件。启用该编辑预设时，必须显式提供已经准备的资产：

```sh
uv run --frozen chongming-preview --scenario scenarios/review.json \
  --landscape-asset /path/to/hannover/landscape-dem.json
```

该选项只接受固定 Hannover 内容，不是任意地景入口；启动及选用时校验。内容摘要为：

```text
d02d6a1e8437374b4842f91c66a04b0cfb3da522c3e7747e8e5b298d5eb61388
```

选区中心为 `(52.38 N, 9.77 E)`，约 1×1 km，配置位于 `scenarios/assets/hannover_500m_import.json`。来源、日期和原始文件字节摘要保存在资产中。在线数据会变化，重新下载或导入不保证命中固定预设。任意合法本地地景应通过场景的 `osm.asset` 引用；没有匹配资产时不要修改预设摘要绕过校验。

### 测例资产

真实地景等测例资产不进入 Git 历史，以确定性归档附加到 GitHub prerelease（tag 前缀 `assets-`）。`flake.nix` 以 `packages.<system>.<name>` 登记下载地址与字节摘要，Nix 负责下载和校验。

| 资产            | 场景                               | 解包位置                          |
| --------------- | ---------------------------------- | --------------------------------- |
| `hannover-500m` | `scenarios/external/hannover.json` | `scenarios/assets/hannover-500m/` |

从源码根目录获取：

```sh
nix build .#hannover-500m --out-link ~/.local/share/chongming/gcroots/hannover-500m
tar -xzf ~/.local/share/chongming/gcroots/hannover-500m -C scenarios/assets
```

out-link 同时是 Nix GC root。解包目录已加入 `.gitignore`，可删除后重新解包。`scenarios/external/` 下的场景依赖这些资产，不参与 `scenarios/*.json` 的仓库测试；缺少资产时加载失败并报告路径。

归档内 `README.md` 记录来源 URL、下载时间、字节摘要、内容摘要及重新导入命令。不分发测例录像；录像由场景在本地生成，`truth.jsonl` 应可逐字段复现，图像随 GPU 和驱动存在差异。

制作新资产时，原始下载与导入结果保存在 `~/.local/share/chongming/assets/<name>/`，不要放在 `/tmp`、`output/` 等会被清理的目录。用 `scripts/pack_local_asset.py` 打包，经维护者确认许可后上传：

```sh
uv run --frozen python scripts/pack_local_asset.py ~/.local/share/chongming/assets/<name> \
  ~/.local/share/chongming/archives/<name>-<date>.tar.gz
gh release create assets-<name>-<date> ~/.local/share/chongming/archives/<name>-<date>.tar.gz \
  --prerelease --latest=false
```

脚本输出的摘要写入 `flake.nix`。

### 数据许可

仓库的 `osm_demo.osm`、`osm_landscape_demo.osm` 及其合成资产标注为 CC0-1.0，不是真实 OSM 提取。真实 OSM 数据通常适用 [ODbL-1.0](https://opendatacommons.org/licenses/odbl/1-0/)，需保留 [OpenStreetMap contributors 署名](https://www.openstreetmap.org/copyright)，并遵守 [OSMF attribution guidelines](https://osmfoundation.org/wiki/Licence/Attribution_Guidelines)。转换为网格不自动消除来源许可义务。

Mapzen/Skadi 的 HGT 格式及来源条件见 [格式说明](https://github.com/tilezen/joerd/blob/master/docs/formats.md) 和 [署名说明](https://github.com/tilezen/joerd/blob/master/docs/attribution.md)。Hannover DEM 保留以下提供方署名，不能将复合来源统一标记为 SRTM 公有领域：

> Mapzen terrain tiles; Europe terrain produced using Copernicus data and information funded by the European Union - EU-DEM layers; global GMTED2010 and SRTM courtesy of USGS.

展示或分发地景时保留资产中的署名与许可；再分发前按实际数据来源及发布方式核对条件。`hannover-500m` 测例资产以 ODbL-1.0 分发 OSM 衍生数据，DEM 保留上述提供方署名；仓库 AGPL-3.0 许可不适用于这些数据。其他未经授权的本地输入不得进入仓库、发行包或测例资产。

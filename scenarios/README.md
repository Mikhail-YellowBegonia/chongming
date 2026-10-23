# 场景 JSON 与算法开发

JSON 描述每次重新生成的世界、相机与目标；它不是运行时 `ScenarioSpec` 转储，也不包含中心算法或正确关联。志愿者可让 Agent 复制/编辑 JSON、校验后运行，无需改 Python。支持摄影环境与确定性相机抖动；干净场景提供几何一致的普通射线，带扰动场景按下述误差与校准语义输出。图形编辑器后置。

## 运行

```sh
uv sync
uv run chongming-center --scenario scenarios/center_clean.json --check
uv run chongming-center --scenario scenarios/center_clean.json --output results/center-test
```

工具默认无视频、暂停，不启动 HTTP 服务。输入 `step` 完成一个曝光中点组及中心调用；`run 10` 连续十步，`run` 到有限终点，Ctrl+C 在完整步骤后暂停；`status` 查看状态，`video on|off` 在步间恢复/停止像素，`reset` 新建输出目录、会话和算法实例，`quit` 关闭所有资源。所有运行均启动 Godot 4.7.2 stable，并通过本地 socket 通信；需要图形会话，保持最小化窗口，关闭视频时不绘图。配置见 [运行说明](../docs/虚拟场景预览.md)。按主 1920×1080、辅 640×480 sRGB RGB8 输出，中心工具将视频保存为 PNG。

`--run --tick-hz 10` 可直接连续运行；节拍只控制墙钟速度，不改变曝光时间。输出目录必须不存在。每次启动完整装配与模块生命周期；尚未实现的业务在 manifest 中标明 `algorithm_unimplemented`。默认 `InputInspector` 只检查输入，不计算定位/跟踪，实际图像检测/标定与完整算法链路仍未实现。

端侧入口为 `uv run chongming-device --scenario scenarios/center_clean.json --output output/device-probe`，默认启用视频、暂停，接入方法与可复制插件见 [端侧开发工作台](../docs/端侧开发工作台.md)。主辅帧独立采样派发，没有可信校准时默认探针输出诊断；视频关闭不会关闭插件。两条工作台可分别用于开发，Godot runtime 不可同时多开。

## 首轮干净基线与误差语义

`center_clean.json` 是首轮端侧/中心算法开发基线：双站单目标、30 秒，无相机抖动，检测概率为 1、虚警率为 0，独立角度/起点协方差均为零。全部普通射线的起点与受保护真值相同，方向在浮点精度内一致；自动测试覆盖整个场景时段。普通出口没有真值 ID，事件到目标的对应只写入独立真值证据。

程序化 `ScenarioSpec.virtual_observations` 支持 `independent_angular_covariance_rad2`（切平面二维弧度平方）与 `independent_origin_covariance_m2`（ENU 三维米平方）。协方差必须有限、对称、半正定，允许零矩阵和奇异矩阵，非法配置在生成前拒绝。方向误差从切平面高斯采样后经球面指数映射旋转，切向基随之平行移动；起点误差直接加在 ENU 中。零协方差严格不加扰动，受保护真值不变。具名 `ray-angle` / `ray-origin` 随机流由种子、场景、站点、相机角色、曝光区间和站内轨迹标识派生，与渲染顺序、视频开关和墙钟无关。当前 JSON 编辑格式未开放这两个协方差字段；程序化实验使用 `dataclasses.replace` 修改编译后的 spec。切平面高斯模型适合小角误差，统计测试覆盖该范围，不代表已验证任意大角分布或实际设备精度。

主相机存在非零抖动时，虚拟校准只在每个配置的可信校准区间首次采样处建立快照，此后不会读取真实瞬时姿态来刷新在线解。校准状态与射线 usage 标为 `frozen`，携带 `virtual_untracked_camera_jitter`、`virtual_calibration_snapshot_held` 和 `virtual_conservative_angular_envelope` 原因码。抖动误差放在共享校准解的 `correction_covariance_enu_rad2` 中，独立射线协方差仍只表示独立采样误差。状态、解和普通射线提供给中心，实际姿态与抖动配置只属于场景/评估侧。

共享项使用保守各向同性二阶矩包络。设相机各轴总幅度为 `a`（角幅度加上像素幅度除以焦距的反正切）、区间校准偏差范数为 `b`，在原共享协方差上加 `(2‖a‖ + b)² I`。这是相对所持快照的角度上界包络，不是已标定的零均值高斯或完整时序联合协方差；同一解下的多条射线不能把它当成独立噪声平均掉。抖动关闭或只作用于辅助相机时，不会因此降级主相机普通射线。该能力可用于降级状态与误差行为实验，覆盖率、鲁棒性和性能结论仍需针对具体算法另行验证。

## 可编辑字段

格式为 `scenario-authoring-v2`，坐标是 ENU 东/北/上、米，时间是秒。不支持格式兼容。

| 字段 | 用途 |
| --- | --- |
| `scenario_id / random_seed / duration_s / fps` | 标识、固定种子 42（可省略，其余值拒绝）、有限时长、1–60 Hz 实验采样；默认 24 秒、30 Hz |
| `stations[]` | 1–4 站，各有唯一 `id`、`position_enu_m`；可选 `main_yaw_deg`（0 向北、正向东）、`main_elevation_deg`、`auxiliary_yaw_deg`；辅助始终水平 |
| `stations[].fps / start_s` | 每站独立主辅采样率与首个曝光起点，默认继承全局 fps、从 0 开始 |
| `targets[]` | 任意数量，空数组表示无目标；每个目标有唯一 ID、geometry 与 motion；可选 `model_name` 为 `la11triangled` / `f86triangled`（或别名 `la11` / `f86`）以加载仓库内白模飞机 |
| `geometry` | sphere + radius_m，box + half_extents_m，或 aircraft；目标和地物共用 |
| `motion` | constant_velocity + position_enu_m/velocity_enu_mps；或 orbit + center_enu_m/radius_m/period_s，可选 phase_rad/vertical_amplitude_m，支持多周期 |
| `world_objects[]` | 覆盖内置地物，`[]` 清空建筑，始终保留每站 A4 标志物；每项含 id、position_enu_m、geometry，可选 display_name、appearance、occludes_sight_lines |
| `vegetation[]` | 可省略以使用演示林地，或显式设 `[]` 清空；树含 `id`、`position_enu_m`、`height_m`、`crown_radius_m`，可选 `sway_amplitude_m` / `sway_frequency_hz` |
| `wind` | 可省略使用演示风，设为 `null` 关闭，或配置平均 ENU 风速和可选湍流标准差；风和摆动仅由实验时间/固定种子决定 |
| `environment` | 可选摄影环境：太阳方向/照度、天空色、大气透视、太阳角直径与光学退化，见下文；省略或 `null` 保留基础照明和纯色天空 |
| `camera_jitter` | 可选物理旋转抖动：角幅度、等效像素幅度、基频及相机 roles；省略或 `null` 关闭，见下文 |
| `clouds[]` | 0–2 层程序云；每层配置正海拔、0–1 光学密度和 ENU 漂移速度，运动只依赖实验时间 |
| `rendering` | 可选程序地表、SSAO、轻量边缘 AA、屏幕空间光束和植被 LOD 距离，见下文 |
| `terrain` | flat 或 heightfield + origin_enu_m/spacing_m；内嵌 heights_m 或外部 asset 二选一，高度行从南到北、每行从西到东，网格外为平面 |
| `search` | min_enu_m、max_enu_m、step_enu_m 三个向量，控制中心搜索盒；范围外目标仍可发射线 |
| `appearance` | 可选 linear_rgb 三个 0–1 数和 diffuse_reflectance；只影响外观 |

未写 `world_objects` 时保留内置建筑、球形地物和每站标志物；相机朝向未指定时采用演示朝向。主相机对角线 FOV 60°，辅助相机水平 FOV 2.6°、俯仰角 0，朝向 10 m 外自己的横向 A4 棋盘；标志物 ID 为 `<station-id>-a4`，角点与站配置绑定。仅在主视场内且中心视线未被地形、地物或其他飞行器挡住时发包。JSON 编译出的虚拟校准区间覆盖全部配置时长，无主相机抖动时为理想有效状态；非零主相机抖动时使用前述冻结语义。虚拟出口的解只服务中心输入开发，不是端侧视觉校准成功。

默认树木由 Godot 程序化生成枝干、细枝与批量独立叶片，无需外部素材；OSM 点树也走同一程序化树生成器，不使用简单圆柱加椭球。树干静止，树冠按平均风和确定性湍流摆动。同一离散姿态驱动视觉网格与逻辑圆柱遮挡代理，逻辑不逐叶求交。当前树种仍是通用近似，LOD 为无渐变的距离硬切换；草只绘制近景批实例。湿润叶面和枝叶级形变尚未实现，本轮没有修改风场模型。

可对 Agent 说：“复制 center_clean.json，改成三站、两个交叉运动目标、运行 60 秒，清空地物，搜索盒缩小到使第二个目标在盒外；只编辑 JSON，然后执行 --check。”未知字段和非法值会报错，JSON 不加载 Python 代码。

现成场景：`center_clean.json` 双站单目标 30 秒；`constant_velocity.json` 双站交叉；`empty.json` 无目标；`heightfield.json` 山脊遮挡；`heightfield_asset.json` 同一山脊的外部资产版本；`review.json` 三类形状绕行；`photography.json` 逆光飞行器、近远建筑与轻微主相机抖动。

## 摄影环境与相机抖动

在 JSON 顶层添加以下字段即可开启首版近似；既有场景默认关闭：

```json
{
  "environment": {
    "sun_direction_enu_unit": [0.18, 1, 0.24],
    "illuminance_lux": 85000,
    "sky_linear_rgb": [0.42, 0.63, 0.86],
    "atmospheric_visibility_m": 6000,
    "sun_angular_diameter_deg": 0.53,
    "glare_strength": 0.35
  },
  "camera_jitter": {
    "angular_amplitude_deg": [0.006, 0.008, 0.004],
    "pixel_amplitude_px": [0.35, 0.25],
    "frequency_hz": 7,
    "roles": ["main"]
  }
}
```

太阳方向是从场景指向太阳的 ENU 向量，非零即可，编译时归一化。照度非负；天空色为线性 RGB、各通道 0–1。`atmospheric_visibility_m` 为正数，省略或 `null` 关闭大气透视；Godot 用指数深度雾近似随距离衰减，不能把该数值视作经实测标定的气象能见度。`sun_angular_diameter_deg` 为 0–5°，默认 0（无可见光盘），0.53° 接近日常太阳视直径；太阳位于地平线以下时不显示光盘。配置透视或光盘时启用随仰角和太阳方向变化的天空渐变。

`glare_strength` 为 0–2，默认 0。正值开启无历史曝光压制、局部亮部扩散与太阳附近暖色幕光。光盘属于天空，会被实际几何遮住；后处理采样实际图像中的亮源，暗遮挡物挡住太阳后，太阳引起的压制/幕光也消退。白色遮挡物仍可能被当作亮部。它是 Compatibility 后端上的低成本 LDR 近似，不提供经过辐射或真实镜头标定的 HDR 曝光、鬼影与散射仿真。

抖动角幅度依次对应相机坐标的右/下/前轴，单位为度；等效像素幅度依次为水平/竖直，按各相机焦距换算为角幅度并叠加。幅度均非负，基频为正数、默认 7 Hz；三轴使用不同频率比例及固定种子派生相位。`roles` 可选 `main`、`auxiliary`，省略表示两类相机；上例只扰动主相机，以保留辅助相机水平指向定标板的基线。辅助相机若显式开启抖动，实际瞬时姿态也会偏离水平。相同实验时间必定得到同一姿态，与墙钟、步号、相机绘制顺序无关。

抖动由 Python 修改实际外参，投影、遮挡、受保护真值和 Godot 使用该外参；伪装射线则把实际观测方向通过所持校准快照转换，因此会随抖动产生误差，并按前述规则冻结和声明共享不确定度。大气透视、曝光与炫光只降低视频观测质量，不据此删除普通射线或改变受保护几何真值。气象雾/雨及云层硬遮挡判据仍未实现；配置雾/雨运行时扰动会明确报错，下面的程序云只作用于像素。

## 云、地表与低成本后处理

`clouds` 最多两层；每层使用三层 Perlin 噪声、一个 Voronoi 轮廓和低频 UV 扭曲形成非体积天空云。`drift_velocity_enu_mps` 乘实验时间平移采样坐标，相同时间点与步号、墙钟、渲染顺序无关。云会遮弱太阳光盘和方向光，并产生暖色银边；它只影响像素，不作为伪装发包的硬遮挡物。云层的真实高度只是确定天空投影与视差的语义参数，不做体积积分。

`rendering` 示例：

```json
{
  "rendering": {
    "terrain_detail_strength": 0.8,
    "ssao_intensity": 0.7,
    "fxaa_enabled": true,
    "god_rays_strength": 0.28,
    "vegetation_lod_near_m": 130,
    "vegetation_lod_far_m": 700
  }
}
```

地表细节为世界坐标多尺度值噪声，不依赖外部贴图；高频层随屏幕导数衰减以减少远景闪烁。SSAO 使用 Godot 4.7 Compatibility 的简化原生实现。`fxaa_enabled` 名称表示快速近似抗锯齿需求，但 Compatibility 不支持 Godot 原生 FXAA，因此当前使用一次后处理中的四邻域定向边缘混合，只模糊高对比边缘，不做昂贵高斯卷积。Screen-Space God Rays 用 8 个朝向太阳的固定样本，只从可见亮部积累；离屏太阳或被暗物体遮挡时不会生成独立光束。所有强度默认 0，AA 默认关闭。

植被现在支持 `tree`（默认）、`shrub` 和 `grass`。灌木使用少量程序枝条和一个 420 叶 MultiMesh，逻辑层用圆柱近似遮挡；草地为一个共享叶片网格的 MultiMesh，默认不参与中心视线遮挡。示例：

```json
{
  "vegetation": [
    {"id": "hedge", "type": "shrub", "position_enu_m": [4, 30, 0],
     "height_m": 2.2, "radius_m": 2.8},
    {"id": "meadow", "type": "grass", "position_enu_m": [0, 25, 0],
     "size_m": [16, 9], "blade_height_m": 0.55, "density_per_m2": 13}
  ]
}
```

每棵树/灌木近距离绘制详细枝叶，越过 `vegetation_lod_near_m` 后切到低面数树干/树冠代理，越过 `vegetation_lod_far_m` 后剔除；草在 near 距离直接剔除。当前使用 Godot 几何实例的距离范围，切换没有交叉淡入，阈值附近可能出现轻微 pop。草单 patch 最多 4096 片，较大草地应拆成多个 patch，既能分块剔除也避免一个巨型 AABB 常驻视锥。

OSM 地景支持固定本地建筑、道路、树点、森林/绿地/水体和可选 HGT DEM；示例为 `scenarios/osm_landscape.json`（CC0 合成 fixture，非真实地图）。用 `uv run chongming-preview --scenario scenarios/osm_landscape.json` 复查；用 `chongming-osm` 离线清洗/导入 XML，再将 `osm.asset.uri` 和 SHA-256 写入场景。真实 Hannover 样区的配置、来源和复现命令见 [OSM 地景整合说明](../docs/OSM地景整合说明.md)，本地复查产物写入被忽略的 `output/` 目录。选区使用 WGS84 包围框，预览回调和固定 Hannover 地景切换已经接入；任意选区 UI 尚未实现。

较大的地形网格放在独立 JSON 中，场景只保留位置、间距和 `"asset": {"uri": "assets/ridge.json", "sha256": "…"}`。资产只含 `{"heights_m": [[0, 1], [2, 3]]}`：至少 2×2 的矩形有限非负高度，单位米、相对基础高程。原点和间距留在场景中，同一网格可复用。URI 相对场景文件目录解析，也支持本地绝对路径/file URI；不下载远程资产。Python 调用 `load_scenario` 可用 `asset_root` 指定素材目录。

编辑资产后执行 `shasum -a 256 scenarios/assets/ridge.json`，将完整的 64 位小写摘要填回场景，再用 `chongming-center --scenario scenarios/heightfield_asset.json --check` 校验。摘要不匹配、未知字段、非法网格在启动前报错；证据保留引用、摘要及编译后的几何。小网格仍可直接写 heights_m，不强制拆文件。

## 接入中心算法与答案参考

用受信任 Python 工厂接入：`--algorithm my_center:factory`，工厂返回 `ports.development.CenterAlgorithm`，实现 `start(registration)`、`process(step)` 和 `close()`。`process` 同步返回 `(measurements, tracks)`；工具等待返回后才接受下一步。`registration` 有在线站点配置、搜索盒、时钟与中心会话；`step` 有新校准解、普通事件/到达时刻、每个实际采样主流的完成进度。先注册解，再按顺序消费事件、进度；有限末尾还有单独的 stream_ended 步。已有 FusionEngine 可由工厂返回 `FusionEngineAdapter(engine)`。

`center-registration.json`、`center-input.jsonl` 是中心可见输入；`center-output.jsonl` 保存算法测量、轨迹和实际处理耗时。`truth.jsonl` 独立保存真实目标、理想射线和事件到目标的对应，供开发者/评估对答案，求解器不能读取它。manifest/authoring 同样只给编排和审计；无自动关联评分或定位算法。manifest 保存 Godot 完整版本、初始目标/相机元数据与场景配置。默认无视频运行的绘图请求为零，timing 仍记录每个逻辑组。历史字段 `gpu_ms` 是 Godot 内场景更新、绘图与回读墙钟耗时，`gpu_submissions` 是绘图请求数。

只查看逻辑总览可用 `uv run chongming-preview --scenario scenarios/center_clean.json --no-video --port 8878 --no-browser`，完成后 Ctrl+C 关闭。预览不调用志愿者中心算法，中心分步调试使用上面的工具；预览的“单步到下一可显示帧”与中心工具的实验组单步不同。

独立导出场景证据可用 `uv run chongming-scene --scenario scenarios/center_clean.json --output results/scene-test --no-video`；省略 --no-video 则输出完整标准图像。所有自动检查以几何、时间、事件、答案隔离和状态为主，不维护像素 golden。

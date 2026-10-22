# OpenStreetMap 地景整合说明

状态：本阶段已完成离线地景导入、清洗、场景资产接入、原生 freeview 和预览回调，并完成汉诺威约 1×1 km 样区评估；3 km 范围仍未验证。本文不新增主规范之外的总体系统需求；场景功能以根目录[虚拟场景需求清单](../虚拟场景需求清单.md)为准。

## 决策摘要

- 使用 OpenStreetMap (OSM) 的固定本地数据，运行时不联网；导入后保留建筑、道路、树点、森林/绿地/水体，并可选贴入本地 HGT 数字高程。
- 借鉴 `godot-openstreetmap` 的 OSM 几何分类、瓦片/缓存思路和多边形挤出做法，不把它作为运行时插件或在线数据服务直接接入。
- 不采用 OSM2World/GLB 作为中转管线。导入与简化由本仓库掌控，保持数据可审计、可复现，并适配当前 Python 逻辑层单向控制 Godot 渲染层的边界。
- OSM 视觉建筑和 Python 遮挡代理必须从同一份固定输入及同一套变换参数派生；Godot 的三角网格不成为场景真值的权威来源。

仓库提供 `chongming-osm` 离线工具和合成 fixture。地图选区使用显式 WGS84 包围框，原始 XML 与清洗结果均可本地保存；实验启动只读本地文件。当前编辑面板提供已校验 Hannover 资产切换，尚不提供任意包围框绘制或更大范围分块。

当前格式为 `osm-landscape-v2`（导入器 `chongming-osm-2`），仍兼容无新要素的 `osm-buildings-v1`。道路桥、隧道和非零 `layer` 会记录诊断并拒绝；树点有高度/冠幅回退；森林按固定间距、哈希和预算生成稀疏树，不把 OSM 面误称为完整树木清单。森林、绿地和水体支持多边形洞、关系拼接和选区裁剪。实时预览仍只需调用 `prepare_osm(..., preview=callback)`，回调传递完整 `OSMImport`，渲染层接口不变。

DEM 使用本地 1201²/3601² 大端 HGT，按 EGM96 正高解释，在锚点处转为局部相对高程；地表、道路和水体按同一三角形地形网格贴地。HGT 可能是 Copernicus EU-DEM 等复合地形，不等于裸地测量，资产必须记录提供方署名、许可和 SHA-256。范围外 DEM、void 样本和超过 250000 样本的网格会拒绝。

## 当前用法与接口

输入为本地 `.osm` XML 0.6；PBF 需在准备阶段用外部工具（如 osmium）离线转换，并另存原始 PBF 来源、摘要和转换命令。导入工具不会下载数据，`--source-url` 仅用于记载来源。示例 fixture 为自行编写的 CC0 合成数据，不是真实地图：

```sh
uv run chongming-osm scenarios/assets/osm_demo.osm \
  --config scenarios/assets/osm_import.json \
  --source-url 'repository:scenarios/assets/osm_demo.osm (synthetic fixture)' \
  --data-date 2026-10-20 --license CC0-1.0 \
  --attribution 'Synthetic fixture, CC0-1.0; not an OpenStreetMap extract' \
  --output output/osm/demo.buildings.json \
  --cleaned-output output/osm/demo.cleaned.osm
```

产物为规范化 `osm-landscape-v2` JSON、可选精简 XML，以及默认的 `demo.buildings.report.json`（可用 `--report` 指定）。报告关联源/清洗/资产三份 SHA-256、导入器版本、来源日期、许可、完整配置、各类要素数、DEM 元数据、诊断、大小和导入墙钟耗时。规范化资产不写入运行耗时，同源同配置重复导入字节一致。真实 OSM 默认署名为 OpenStreetMap contributors、许可为 ODbL-1.0；参数覆盖只用于确有不同来源许可的数据。旧 `osm-buildings-v1` 资产仍可加载。

配置示例在 `scenarios/assets/osm_import.json`：锚点必须为带 ID、基准历元和椭球高的 WGS84，`bounds_wgs84` 按 west/south/east/north 排列。ENU 东/北坐标通过 WGS84→ECEF→ENU 得到，保留微米量级数值舍入；无 DEM 时使用显式 `base_elevation_m`。有 DEM 时，建筑基础取轮廓顶点最低地面高程，树立于采样地面，地表面片与地形共享三角剖分。不把局部切平面当作大区域地图投影，不支持跨日期变更线选区。

当前支持策略：

- 支持 `building` 闭合、无自交的单外环，包括凹多边形；确定性耳切生成平顶和墙面。轮廓统一为逆时针、固定起点，ID 为 `osm/way/{id}`。
- 高度依次取有效 `height`（米、`m` 或 `ft`）、有效 `building:levels × level_height_m`、`default_height_m`，并受 `maximum_height_m` 限制。层高、默认高度与基础高程均须显式配置。异常高度/楼层和默认回退写入诊断。
- 只保留完整位于包围框内的建筑；跨越或包围选区的轮廓报 `boundary_crossing`，不裁断补面。完全在外的报 `outside_bounds`。
- 拒绝关系建筑、其成员建筑、building parts、非零/非法抬升标签、缺点、未闭合、自交和重复点。关系/内洞不尝试修复，避免误填庭院。连续共线点可确定性移除。
- 精简 XML 保留支持类别及其节点/成员依赖、几何/分类/高度/宽度标签，去掉未知 tag、无关 POI 和节点。拒绝候选与相关关系仍保留诊断所需记录，因此它是本导入器的精简重导入文件，不是通用、引用完整的 OSM 区域备份。清洗报告保留原始文件摘要；应同时留存原始下载文件。
- 道路支持 motorway/trunk/primary/secondary/tertiary 及 link、residential/unclassified/service/living_street、pedestrian/track/footway/path/cycleway/steps；按中心线缓冲成简单路面，宽度取有效 `width`（0.3–40 m）或类别默认值（1.5–12 m）。不生成交通图、车道、桥梁和隧道；`lanes` 不用于推断宽度。
- 树点支持 `natural=tree`，高度限 1–60 m，缺失回退 10 m；冠幅取有效 `diameter_crown`，否则冠半径取高度的 1/4。逻辑沿用树干/树冠圆柱代理，视觉沿用既有 Godot 语义树木。
- 森林为 `natural=wood` / `landuse=forest`；绿地支持 grass/meadow/recreation_ground/village_green、natural grassland、park/garden；水体支持 `natural=water` / `waterway=riverbank`。闭合 way 和可完整拼接的 multipolygon 支持内洞，缺成员或破损关系整体拒绝。道路及地表裁到选区，建筑边界策略仍为整栋保留/拒绝。
- 森林树按 `forest_spacing_m`（至少 10 m，默认 24 m）、`maximum_forest_trees`（默认 512，上限 2000）确定性分布，排除洞、建筑、水体、道路及已标树冠。OSM 点树和森林/绿地派生树都使用 Godot 自制程序化树，近景为枝干、细枝和批量叶片，远景才切换到简化代理；不会把点树降级成简单圆柱加椭球。OSM 绿地没有逐株清单，因此草地、林地和公园/花园面均按固定哈希网格派生三类植被：树、灌木和草；派生树的 `osm/area-tree/...`、森林树的 `osm/forest-tree/...` 与 OSM 点树的 `osm/tree/...` 命名空间独立。它们是有预算的视觉/遮挡近似，不声称为真实测绘位置；树冠、草和灌木接入确定性风扰动，树干保持静止。水体是随粗 DEM 贴地的颜色面，不反演水平水位或湖底。建筑基础没有地基开挖/坡地基础拟合。

飞行物可在 authoring JSON 的目标上设置 `model_name`：`la11triangled` 或 `f86triangled`（别名 `la11`、`f86`）。资源分别位于 `src/chongming/rendering/godot/assets/aircraft/`，当前按 GLB 原始比例实例化，在 Godot 侧按模型导出坐标轴做固定视觉校正、居中到真值位置，并覆盖为白/灰色 `StandardMaterial3D`；未识别或缺失模型会回退到原有程序化几何。模型只影响视觉实体，Python 的飞机几何仍作为真值/遮挡代理。

场景入口示例见 `scenarios/osm.json`：

```json
{"osm": {"asset": {"uri": "assets/osm_demo.buildings.json", "sha256": "<64位小写摘要>"}}}
```

资产加载在 JSON 解析前校验 SHA-256，拒绝远程 URI；编译结果中的建筑网格、来源和锚点必须与资产一致。场景和所有站点采用同一个资产锚点，站点配置摘要同步更新；已有手写 ENU 物体/站点坐标由作者保证与此锚点相符，不自动重投影。manifest 通过现有完整场景序列化保存来源、配置、诊断与规范几何。

实时预览使用 Python 接口 `prepare_osm(content, config, *, source_url, data_date, elevation=None, preview=callback)`。每次本地配置/选区变更后重新调用，成功校验后调用 `callback(OSMImport)`；`landscape_objects(imported)`、`landscape_vegetation(imported)` 与 `terrain_from_elevation(...)` 返回不可变场景描述，交给现有 Godot 快照路径。`attach_landscape` 已组合这三步。消费者替换上一批 `osm-*` 物体与 `osm/` 植被，并展示 OSM/DEM 署名和诊断。回调同步运行；界面可自行在后台调度导入并忽略过期请求。本接口不启动新渲染器、不控制实验时钟，也不修改正在运行的实验。正式实验仍使用固定摘要资产。

Python 和 Godot 共用同一 footprint 派生的闭合挤出网格与同一 DEM 三角网格，Python 仍为遮挡真值权威。闭合网格有内部点判定，开放地表不当作实心体；道路/颜色地表不增加独立视线遮挡。视线求交先以保守局部包围盒排除不相交线段，再保留原三角求交/包含判定。渲染沿用已有网格缓存和稳定 ID，未增加另一套几何推导。更大样区的轻量代理、合批或瓦片策略需要单独测量后再决定。

```sh
CHONGMING_GODOT=/path/to/godot uv run python scripts/review_osm.py --samples 6
uv run chongming-preview --scenario scenarios/osm.json
```

复查脚本产生署名截图、对比图和报告，记录建筑/顶点/三角形数、加载耗时（含 Python 内存跟踪开销）、Godot 启动、首次请求、暖机帧、逻辑采样与进程 RSS；RSS 为进程整体瞬时值，不是建筑专用或 GPU 内存。首尾离散时刻分别演示共线遮挡与移出遮挡。自动化测试覆盖投影控制点、轮廓/遍历顺序、清洗复导入、回退/拒绝、凹屋顶/缺口、资产校验、预览回调、场景往返及实际 Godot 像素行为；合成 fixture 验证不能替代真实样区几何抽查和规模验收。

2026-10-20 本机 macOS / Godot 4.7.2 Compatibility 的汉诺威样区：原始 XML 2.49 MB、清洗后约 650 KB；257 建筑、497 道路、135 树点、35 绿地、11 森林、11 水体，19×19 DEM 网格（60 m），规范资产约 340 KB，导入约 0.33 s。地景/地形约 4.63 万三角形、491 个植被代理（树 390 个部件、灌木 47、草 54）。加入静态场景保守 BVH 后，近地复查的逻辑采样中位约 8–53 ms；资源增量协议下 Godot 暖机约 13–17 ms（首个请求包含资源创建约 1.1 s）。这是同一台机器、1024×576、3 次暖机样本的墙钟测量，不是纯 GPU 时间，也不能推断城市规模上限。诊断中的回退、关系和抬升要素拒绝不代表 OSM 覆盖完整性。定向 Hannover 运行已验证建筑、植被和飞行物同帧可见，2 秒风场运行生成主/辅相机视频帧并保持真值/事件完整；复查产物按命令生成到被忽略的 `output/` 目录。

对外展示真实 OSM 资产时，现有预览消费者应使用资产中的署名；离线复查导出已经附带署名。当前 UI 仅切换仓库认可的本地资产；若以后增加任意选区，仍须保留来源、许可和摘要信息。

## 汉诺威样区与复现（2026-10-20）

按用户允许缩小范围，选取中心 `(52.38 N, 9.77 E)` 周边约 1×1 km 矩形，边界 `9.76265,52.3755,9.77735,52.3845`；配置固定在 `scenarios/assets/hannover_500m_import.json`。最初约 6×6 km 的 Overpass 请求未取得可靠统计，Geofabrik Niedersachsen PBF 约 508 MB，未为本次测试下载；不能据此判定 3 km 请求一定被拒绝。缩小后 OSM map API 返回 HTTP 200，固定原始 XML 2,486,289 bytes，删除 7291 个无关 tag 后为 649,704 bytes。map API 不保证跨边界关系的成员闭包；本次 3 个自然地表关系缺失/不支持成员，整体拒绝，另有关系建筑、跨界建筑与抬升道路诊断，故结果不是完整城市复刻。

本地文件保留在 `output/osm/hannover-500m/`（git 忽略），源码仅纳入小型合成 fixture 和选区配置。原始数据获取地址：

- OSM：<https://api.openstreetmap.org/api/0.6/map?bbox=9.76265,52.3755,9.77735,52.3845>，获取日期 2026-10-20。
- DEM：<https://s3.amazonaws.com/elevation-tiles-prod/skadi/N52/N52E009.hgt.gz>，5,136,432 bytes，获取日期 2026-10-20，HTTP Last-Modified 为 2016-04-23；资产 `data_date` 记载的是该版本日期，不是测量日期。
- DEM 格式与提供方条件：[Skadi/HGT 格式](https://github.com/tilezen/joerd/blob/master/docs/formats.md)、[Mapzen/来源署名与许可](https://github.com/tilezen/joerd/blob/master/docs/attribution.md)。欧洲地形署名包含 “Europe terrain data produced using Copernicus data and information funded by the European Union - EU-DEM layers”，全球 GMTED2010/SRTM 来源为 USGS；不能将整个复合数据简单标记为 SRTM 公有领域。

| 本地固定输入/产物 | SHA-256 |
| --- | --- |
| `source.osm` | `e02f68cae94a23fc7d7e5bd1fb51c4d28a3aa1d36efafb19b703b6ba569f4cae` |
| `cleaned.osm` | `6133f320f2d4c17076289bcc387635e44ea463d592525fc04932a26c553fe1db` |
| `N52E009.hgt.gz` | `08855845b2ee413a8f5e5c219b804fe34de000c9657d9affc06f1d66d988d3fa` |
| `landscape-dem.json` | `85a4f6bd9a4b0df848f96dcf2e003cddbf510c06f1c85cc9a6fcbea0fd4b9d78` |

OSM 在线地址随时间变化，只有保存的文件及摘要能复现实验。已有本地文件可断网重新导入：

```sh
uv run chongming-osm output/osm/hannover-500m/source.osm \
  --config scenarios/assets/hannover_500m_import.json \
  --source-url 'https://api.openstreetmap.org/api/0.6/map?bbox=9.76265,52.3755,9.77735,52.3845' \
  --data-date 2026-10-20 \
  --dem-hgt output/osm/hannover-500m/N52E009.hgt.gz --dem-tile N52E009 \
  --dem-source-url 'https://s3.amazonaws.com/elevation-tiles-prod/skadi/N52/N52E009.hgt.gz' \
  --dem-data-date 2016-04-23 \
  --dem-license 'Provider terms: https://github.com/tilezen/joerd/blob/master/docs/attribution.md' \
  --dem-attribution 'Mapzen terrain tiles; Europe terrain produced using Copernicus data and information funded by the European Union - EU-DEM layers; global GMTED2010 and SRTM courtesy of USGS.' \
  --output output/osm/hannover-500m/landscape-dem.json \
  --cleaned-output output/osm/hannover-500m/cleaned.osm
CHONGMING_GODOT=/path/to/godot uv run python scripts/review_osm.py \
  --scenario output/osm/hannover-500m/review.json \
  --output output/osm/hannover-500m/review --samples 3
uv run chongming-preview --scenario output/osm/hannover-500m/review.json
```

HGT 以锚点 EGM96 正高 54 m 为参考，场景锚点相对地面为 0；重采样网格相对高程约 −2.94 至 19.08 m。该 60 m 网格只验证简单地形链路，不能用于水位、林下裸地或地基精度判断。复查相机在 `(-620,-620,850)` 米，方位 45°、俯仰 −44°，植被 LOD 为 180/2000 m，用于查看全区低面代理；另用近地机位验证建筑、草木与飞行物的联合画面。静态复查的逻辑计时包含定标点视线，不能代表多目标运行成本。

本机 1024×576、每视图 3 次暖机采样，首个请求约 1.1 s（包括几何、材质和植被资源创建），后续中位约 13–17 ms；逻辑从此前的数十毫秒量级降到约 0.75 ms，原因是静态世界/植被先用保守包围盒 BVH 排除，再执行原有精确网格相交。Python 每帧发送稳定资源令牌和位姿，Godot 只在资源首次出现或语义改变时创建网格/材质。精确相交规则、树木等效圆柱和地形命中规则均未改变；动态飞机不使静态城市索引失效。网格统计不含 Godot 按 LOD 生成的树木视觉三角形。此结果证明小选区可离线准备和绘制；没有硬性实时指标，也没有引入 Vulkan/GPU 遮挡查询。

不需要真实下载数据时，可直接预览 `scenarios/osm_landscape.json`。其 CC0 合成 XML、带洞森林、道路、树点、绿地、水体与 DEM 均随仓库保存；`uv run python scripts/prepare_osm_demo.py` 可重建资产，脚本内容变化会改变 DEM 来源摘要，需将打印的新资产 SHA-256 更新到场景文件。

## 参考资料

### godot-openstreetmap

参考仓库：[RodZill4/godot-openstreetmap](https://github.com/RodZill4/godot-openstreetmap)，本次检查固定在 `master` 提交 [`a3a9ca5c46ddcc956a7d4798ee835d749d711e42`](https://github.com/RodZill4/godot-openstreetmap/commit/a3a9ca5c46ddcc956a7d4798ee835d749d711e42)。仓库附带 [MIT License](https://github.com/RodZill4/godot-openstreetmap/blob/a3a9ca5c46ddcc956a7d4798ee835d749d711e42/LICENSE)。其 README 说明 simple demo 可运行但不稳定，其他 demo 有过时问题；它是实现参考，不是可直接依赖的生产组件。

| 入口 | 可借鉴内容 | 不直接照搬的部分 |
| --- | --- | --- |
| [`global/osm.gd`](https://github.com/RodZill4/godot-openstreetmap/blob/a3a9ca5c46ddcc956a7d4798ee835d749d711e42/addons/openstreetmap/global/osm.gd) | Web Mercator 瓦片坐标换算；解析 OSM XML 的 node/way/tag 并按建筑、道路、水体、草地等分类；将解析结果写入缓存 | Godot 3 时代的 API、隐式全局状态、随机扰动几何，以及未经本仓库验证的分类/默认高度规则 |
| [`scripts/map.gd`](https://github.com/RodZill4/godot-openstreetmap/blob/a3a9ca5c46ddcc956a7d4798ee835d749d711e42/addons/openstreetmap/scripts/map.gd) | 地图区域按 tile 组织、中心变化时复用 tile、把地表纹理生成拆为单独工作 | 面向持续移动游戏的动态瓦片地图；当前实验是固定场景，不需要在线流式地图 |
| [`scripts/tile3d.gd`](https://github.com/RodZill4/godot-openstreetmap/blob/a3a9ca5c46ddcc956a7d4798ee835d749d711e42/addons/openstreetmap/scripts/tile3d.gd) | 解析缓存格式、按建筑/道路/水体分派场景对象的组织方式 | 缓存缺失时会请求 OSM API，并使用项目自定义 TWM 二进制格式；这会引入联网、缓存状态和额外格式维护，不适合直接进入实验启动链路 |
| [`buildings.gd`](https://github.com/RodZill4/godot-openstreetmap/blob/a3a9ca5c46ddcc956a7d4798ee835d749d711e42/addons/openstreetmap/buildings.gd) | 根据 footprint 生成墙面、平屋顶或简化屋顶网格 | 复杂屋顶和修复策略并非首期所需；视觉网格不能替代逻辑遮挡代理 |

项目采用“OSM 输入 -> 解析/分类 -> 自有 tile 缓存 -> Godot 网格”的流程。值得借鉴的是数据流分层和建筑挤出，而不是它的运行时网络请求、自有缓存格式或游戏式连续瓦片加载。

### OSM 数据与许可

- [OpenStreetMap Copyright and License](https://www.openstreetmap.org/copyright)：数据归属与 ODbL 说明。
- [OpenStreetMap Foundation Attribution Guidelines](https://osmfoundation.org/wiki/Licence/Attribution_Guidelines)：产品界面、图像和其他输出中的署名指导。
- [Open Data Commons Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/)：ODbL 正文。
- [Geofabrik 下载服务](https://download.geofabrik.de/)：可选的固定 PBF 区域提取来源之一；具体使用区域、下载日期和源文件摘要需随实验资产记录。

若仓库或发布包分发 OSM 数据、改编数据库或由其生成的数据库，应在分发前按 ODbL 评估义务。对外展示含 OSM 衍生地景的画面时，应保留适当的 OpenStreetMap attribution。具体数据集和发布方式的许可判断需要随实际来源确认，不能仅凭转换成几何文件就假定许可义务消失。

## 数据与实现边界

### 固定输入与复现

1. 实验运行不下载 OSM 数据，不调用 OSM API、Overpass 或瓦片服务。在线获取只能是开发者主动执行的离线准备步骤。
2. 每个 OSM 场景固定数据文件或固定提取版本，并记录来源 URL/提供方、数据日期、WGS84 区域边界、源文件 SHA-256、导入器版本、tag 映射/高度回退配置及坐标锚点。
3. 相同的输入摘要、配置和导入器版本必须产出相同的规范化几何与稳定 ID；不得依赖输入遍历顺序或未固定种子产生几何抖动。
4. 导入结果只允许仓库认可的本地资产 URI，并在解析前校验摘要。规范化资产格式为 `osm-landscape-v2`；清洗 XML 是可选的离线中间产物，不得让场景 JSON 隐式引用会变化的远程地址。

### 首期地景范围

- 从 OSM building footprint 生成简化建筑墙体与平顶，先支持无自交、可稳定闭合的外轮廓；复杂关系、多内环、自交或损坏几何须确定性拒绝并在导入报告列明，不能静默生成任意网格。
- 高度优先使用可解释的 OSM 高度标签；缺失或异常值使用显式配置的确定性回退，并在资产/manifest 中记录回退策略。楼层数换算的层高不得沿用参考项目未经校准的默认值。
- 坐标由选定 WGS84 锚点投影到局部 ENU 米制坐标。投影、轴向、原点、裁剪边界及边界相交建筑的处理需固定并测试，避免渲染与逻辑的方向或尺度不一致。
- 首期不要求道路交通网、完整自然地表分类、POI、完整地形高程、屋顶细节、运行时在线瓦片、任意大区域流式加载或照片级建筑材质。
- 建筑来源和导入统计应可追溯；缺失建筑或标签并不代表现实中不存在该建筑，不能用 OSM 覆盖完整性做场景真值声明。

### Python / Godot 职责边界

建议数据路径：固定 OSM 文件 -> 离线导入/校验 -> 带版本和摘要的规范化建筑数据 -> Python 场景编译、ENU 几何与遮挡代理 -> 语义/网格快照单向发送给 Godot -> 渲染输出。

Python 持有已导入 footprint、稳定对象 ID、确定的高度/姿态和简化遮挡几何，并为真值可见性查询负责；Godot 只消费这些描述并绘制视觉建筑。不得由 Godot 独立重新查询 OSM、独立推导建筑高度或向 Python 回传另一套几何真值。视觉优化可以改变网格精度，不得改变用于真值的规范几何。

现有场景契约、Python 可见性求交和 Godot 渲染请求支持通用三角网格；首版增加了 OSM 规范表示与挤出实现。真实样区 spike 仍应比较共享网格与轻量垂直棱柱/凸分解代理的成本，不能只把网格传给 Godot 而省略逻辑侧遮挡。

## 预期影响面

| 区域 | 预期工作 | 影响边界 |
| --- | --- | --- |
| 场景入口 | 在 `src/chongming/scenarios/authoring.py` 增加 OSM 资产/锚点等语义配置；在 `serialization.py` 校验字段、版本与摘要 | 保持 JSON 是简单场景入口；无远程 URL 和任意代码入口 |
| 资产加载 | 复用并按需扩展 `src/chongming/scenarios/assets.py` 的本地 URI 与 SHA-256 校验；新增导入器/格式版本及诊断报告 | 离线准备可以使用单独工具；运行时只读已固定资产 |
| 几何与真值 | 在 `contracts/scene.py` 表达建筑来源/稳定 ID 和遮挡代理；在 `modules/scene.py` 的可见性路径加入经验证的多边形代理 | 这是 OSM 影响逻辑层的主要边界；摄影、时钟、三路出口语义不变 |
| 渲染 | 在 `rendering/godot.py` 传递建筑实体，`rendering/godot/worker.gd` 构建或复用网格与材质 | 渲染实现不能联网，也不能回写场景/实验时间 |
| 证据与可复现 | 在运行 manifest/场景证据中记录源摘要、边界、导入器和 tag/高度策略 | 让图像、真值与 OSM 来源能够关联复核 |
| 测试与示例 | 增加极小、许可清楚的固定 fixture；覆盖投影、闭合轮廓、失败几何、高度回退、遮挡及 Godot 绘制一致性 | 不把真实大城市数据集纳入普通单元测试或依赖网络 |

预期主要改动限制在 `src/chongming/scenarios/`、`src/chongming/contracts/scene.py`、`src/chongming/modules/scene.py`、`src/chongming/rendering/`、`tests/` 和一个专用离线导入工具/小型 fixture。预计不触及中心融合算法、设备端视觉算法、离散时钟规则和输出协议。若实现需要修改这些边界，应先说明原因并增加对应架构决策记录。

## 阶段交付验收

本阶段以小面积、建筑标注有代表性的固定 OSM 样区完成以下检查；结果作为后续扩展范围的基线：

1. 输入边界、来源、日期、许可证说明和 SHA-256 可从配置/manifest 查到；断网重复运行能加载同一结果。
2. 投影后的建筑位置、朝向和米制尺寸通过已知控制点/手工抽样核验；相同输入重复导入产生相同摘要或规范化内容。
3. 常见闭合建筑轮廓正确挤出；不支持的几何、缺失高度和异常标签都有稳定、可解释的报告及回退结果。
4. 至少设置一个确定的相机—建筑—目标共线用例：Python 遮挡真值与渲染建筑位置一致；建筑以外的标签/视觉细节不会改变伪装包和真值规则。
5. 测量导入耗时、首次启动耗时、单帧耗时、网格数量和内存；据此决定是否需要瓦片或建筑合批，而不是预先照搬游戏式动态瓦片系统。
6. OSM 衍生资产及展示的 attribution/ODbL 处置已记录；未确认的再分发条件不随意打包原始数据。

## 当前边界与风险

OSM 建筑覆盖率和高度标签因地点而异；多边形洞、关系、边界截断和坐标基准容易导致几何错误；整批建筑的绘制和可见性求交可能影响运行成本；不同数据提取日期会造成外观变化；派生数据库的 ODbL 条件需结合分发方式判断。这些风险通过小区域验收、确定性拒绝/回退规则、输入摘要和共享几何来源控制，不以完整复刻某个现实地点为验收目标。

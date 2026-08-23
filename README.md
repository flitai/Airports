# Airports · 全球机场分布地图

一个**单文件、可离线、免安装**的网页地图，收录 [OurAirports](https://ourairports.com/)
全量 **85,860 个机场**（72,470 个在用 + 13,390 个已关闭）。按缩放级分级显示、
多维筛选、点选查看跑道与通信频率明细，美国机场还能直接在页面里翻 FAA 航图。

没有构建步骤，没有 npm，没有后端。克隆下来双击 `index.html` 就能用。

<img src="https://cdn.mathpix.com/snip/images/qiXAqzlQf1a-u_ztbAbdBchuBNbm3dweDqLLRaXMEEU.original.fullsize.png" />

## 快速开始

    git clone <仓库地址> && cd Airports

然后二选一：

| 方式 | 底图 | 说明 |
|---|---|---|
| **双击 `index.html`** | Natural Earth（粗略）/ 在线 OpenFreeMap | 无需服务器，开箱即用 |
| **双击 `start.command`**（macOS）<br>**双击 `start.bat`**（Windows） | 追加本地街道级 PMTiles + 山体阴影 | 起 Range 服务器并开浏览器，需先[下载离线底图](OFFLINE.md) |

机场数据（`data/*.js`，16 MB）已随仓库提交，**第一种方式不需要任何额外下载**。
街道级瓦片是 100+ GB 的可选数据集，不在仓库里。

> ⚠️ 不要用 `python3 -m http.server` 起服务——它不支持 Range 请求，PMTiles 会失效
> （实测：发 `Range: bytes=100-199` 它返回 200 和整个文件）。项目自带的 `serve.py`
> 就是为此写的。

## 功能

- **分级显示** — z<4 仅大型机场 → z≥4 加中型与有定期航班的 → z≥7 加小型 → z≥10 加直升机坪/水上机场
- **筛选** — 类型、定期航班、硬质道面、最长跑道长度、国家、已关闭机场（按需加载）
- **搜索** — ICAO / IATA / 名称 / 城市
- **详情** — 跑道明细（道面归一 + 原始写法）、通信频率、坐标海拔、维基/官网外链
- **航图** — FAA d-TPP 覆盖的 2,397 个机场直接在页面内嵌查看，按综合/地面/离场/进场/进近分组；
  其余机场给 [ChartFox](https://chartfox.org/) 外链
- **底图** — 自动 / 在线 / 完全离线三档
- **山体阴影** — 本地 DEM（terrarium 编码），叠在道路之下
- **实时航班**（可选，需联网+服务器模式）— 按视口拉取空中航班，按航向旋转、按高度着色；
  点击飞机可查该呼号的历史航线并画出大圆航路

## 底图的三种模式

侧栏「底图」可切换：

| 模式 | 说明 |
|---|---|
| **自动**（默认） | 有网走在线 OpenFreeMap；`navigator.onLine` 为假直接读本地，或在线 8 秒加载不出来自动切到本地 |
| **在线** | OpenFreeMap 矢量瓦片，需联网 |
| **离线** | **完全不联网**。按 `PMTILES_CANDIDATES` 预排顺序取第一个存在的本地包（planet → global-z9 → test-yrd），都没有才回落 Natural Earth 1:50m |

随仓库提供的 Natural Earth 底图（`data/basemap.js`，2.8 MB）含国界、湖泊、河流、
一级行政区界和 1212 个城市点，覆盖全球；文字字形内嵌在 `vendor/glyphs.js`，
所以断网时城市名和机场代码照样显示。但它是 1:50m 比例尺，z8 以上只剩海岸线和
国界——看全球分布够用，看单个机场周边环境要用在线或 PMTiles 底图。

装了 `planet.pmtiles` 后离线一档用的是手写的 `protomapsStyle()`，**60 层，比在线的
positron（55 层）还细**：七级道路分级带描边、跑道与滑行道、机场场区、水体/道路/
POI/地名分层标注。各地实测标注数量（离线 vs 在线）：弗吉尼亚 z10 是 13 : 14，
苏黎世 z12 是 85 : 57，上海 z15 是 139 : 16，巴黎 z13 是 132 : 33。

注意 `file://` 下浏览器禁止 `fetch`，探测不到本地瓦片，双击 `index.html` 时离线一档
只能是 Natural Earth——要用 PMTiles 必须走启动器。

## 实时航班（可选）

默认**关闭**。勾选侧栏「实时航班」后，按当前视口拉取空中的航班，图标按航向旋转、
按气压高度着色（低空暖色、高空冷色），悬停显示呼号/机型/注册号/高度/地速。

数据来自 [adsb.fi](https://adsb.fi/)，失败自动回退 [adsb.lol](https://adsb.lol/)，
两者都是社区众包的 ADS-B 聚合服务。

**视口覆盖**：上游单次查询的半径上限是 250 nm（实测 500 直接 400），所以视野一大
就盖不住整个窗口。代理端会按需把视口切成最多 4 个圆分别查询、按 ICAO24 去重合并；
再大就只覆盖中心区域，并在提示行明说「视口过大，放大可看全」，不假装全都拿到了。

**点击飞机看航线**：会画出起降机场之间的大圆航路（球面插值，跨 180 度经线已解卷绕），
面板列出航空公司、起降机场，两端可点进机场详情。机场按 ICAO 码在本地 8.6 万条库里
反查，所以名称与全站一致。

⚠️ 航线是**按呼号查历史记录**得来的，不是本架次提交的飞行计划——ADS-B 报文里根本
不含起降机场。同一呼号不同日期会飞不同城市对，实测 adsbdb 与 hexdb 对同一呼号能给出
两个矛盾答案。所以页面会拿飞机的实际位置做偏航校验：偏离该航路超过 200 nm 就明确
提示「这条多半不是今天这一班」。通航、包机、军机通常查不到任何记录。
航线数据来自 [adsbdb](https://www.adsbdb.com/)，回退 [hexdb.io](https://hexdb.io/)。

⚠️ **只在服务器模式下可用**。这两个数据源都不发 `Access-Control-Allow-Origin`
响应头，浏览器直连一律被同源策略拦下（`file://` 的 `null` 源和 `http://localhost`
都实测过），所以请求必须经 `serve.py` 的 `/api/flights` 代理转发。双击
`index.html` 时页面会自动禁用这个入口并说明原因。

这是个纯粹的联网增强项，**不勾选就一个请求都不发**，离线仍是这个项目的默认形态。
代理端对免费社区服务做了几重克制：同一视口 5 秒内复用缓存、轮询间隔 10 秒、
标签页切到后台就停、半径上限 250 nm、连续 3 次失败自动关闭。上游一条记录有 30+
字段，代理只透传页面用得上的 9 个并压成定长数组，实测体积降到 12%。

## 项目结构

```
index.html      单页应用（HTML+CSS+JS 全内联，72 KB）
serve.py        支持 Range 的静态服务器，一个进程服务全部资源
start.command   macOS 双击启动器      start.bat  Windows 双击启动器
data/           机场数据 + FAA 航图索引（.js 格式，file:// 下也能加载）
vendor/         maplibre-gl.js · pmtiles.js · glyphs.js（内嵌拉丁字形）
tiles/          可选的离线瓦片，不在仓库里（见 OFFLINE.md）
build_*.py      数据编译脚本
fetch_*.py      按需下载矢量底图（见 TILES.md）
```

数据输出为 `.js`（`window.X = JSON.parse('...')`）而非 `.json`，因为 `fetch`/XHR
在 `file://` 下被浏览器阻止，而经典 `<script src>` 标签不受限——这是"双击打开"
能成立的前提。同理，离线底图也编译成 `.js` 而不用 PMTiles/MBTiles。

中日韩地名不靠字形服务器，用 MapLibre 的 `localIdeographFontFamily` 交给系统字体
在客户端渲染；本地只内嵌了拉丁字形（Noto Sans Regular 0-255，100 KB）。

## 离线底图（可选）

想在完全断网时看到街道级底图，有两条路：

| 文档 | 脚本 | 适合 |
|---|---|---|
| **[OFFLINE.md](OFFLINE.md)** | `download_offline_maps.py` | 整份拉全球 z0-15（128 GB）+ 全球高程（6.9 GB）。自包含，只依赖 Python 标准库 |
| **[TILES.md](TILES.md)** | `fetch_tiles.py` / `fetch_levels.py` | 只要部分区域或部分缩放级。基于 `pmtiles extract` 按需抓取，需要 pmtiles CLI |

全球累计体积（实测，build 20260817）：z0-12 **17.6 GB** · z0-13 **35.6 GB** ·
z0-14 **67.6 GB** · z0-15 **128 GB**。z12 够看清城市路网，z14 能看清单条跑道和滑行道。

## 重新生成数据

    python3 build_data.py                    # 读同目录 CSV，输出 data/*.js
    python3 build_basemap.py <NE_geojson目录>  # 重新编译离线底图
    python3 build_faa.py                     # 下载 FAA 航图索引，输出 data/faa.js

`data/*.js` 已随仓库提交，克隆下来直接就能打开页面。只有要重新编译时才需要 CSV
原始数据——它们体积大、上游每日更新，故不入 git，用下面这行拉到项目根目录：

    for f in airports airport-comments airport-frequencies runways navaids countries regions; do \
      curl -O "https://davidmegginson.github.io/ourairports-data/$f.csv"; done

`build_data.py` 负责：道面材质归一（565 种原始写法 → 9 类）、按机场聚合跑道与频率、
拆分已关闭机场、字典编码国家/地区、预计算 zoom 分级用的 rank。

`build_faa.py` 需要联网，自动探测当前生效的 AIRAC 周期。航图数据每 28 天换一版，
FAA 只把最近三个周期留在服务器上，过期的 PDF 会 404。

**这件事已经自动化了**：用 `start.command` / `start.bat` 启动时，`serve.py` 会读
`data/faa.js` 里存的周期截止日，过期就在后台重跑 `build_faa.py`（约 2 分钟，下载
16 MB 元数据）。服务器不等它，立即就能用；更新完成后控制台会提示刷新页面。
写文件是「先写 `.tmp` 再原子改名」，所以后台更新不会让正在加载的页面读到半截数据。

没联网、超时、脚本报错都只打印警告，不影响启动——页面会照旧在航图区提示可能失效。
加 `--no-update` 可跳过这项检查。

⚠️ 双击 `index.html` 的 `file://` 模式下没有 Python 进程，**无法自动更新**，
只能靠页面提示后手动重跑 `build_faa.py`。

## 已知限制

- Web Mercator 在 ±85.05° 外不可表示，2 个南极点位（含 NZSP 南极点站）被钳制到
  地图边缘渲染，详情面板显示的仍是真实坐标。
- 跑道两端坐标仅 32% 的记录有，因此未绘制跑道几何线，只在详情面板列表格。
- 内嵌航图只覆盖 FAA d-TPP（2,397 个机场）。其他国家的 AIP 要么需要登录，要么带
  `X-Frame-Options` 禁止跨源内嵌（ChartFox 就是 `SAMEORIGIN`），只能跳外链。
- 看航图需要联网，`file://` 双击模式下也一样——本地只有索引，PDF 在 FAA 服务器上。
- `airport-comments.csv`（16,365 条社区评论）与 `navaids.csv` 本版未使用。
- 在线底图看过的瓦片会进浏览器 HTTP 磁盘缓存，同一区域断网也能显示；但换区域或
  缩放级就是空白，且缓存会被回收。要可靠离线请用离线底图模式。

## 数据来源

- 机场数据 [OurAirports](https://ourairports.com/)（公共领域）
- 离线底图 [Natural Earth](https://www.naturalearthdata.com/) 1:50m（公共领域），
  取自 [nvkelso/natural-earth-vector](https://github.com/nvkelso/natural-earth-vector)
- 在线底图 [OpenFreeMap](https://openfreemap.org/) / [OpenStreetMap](https://www.openstreetmap.org/copyright)
- 矢量瓦片 [Protomaps](https://protomaps.com/)（OpenStreetMap 数据，ODbL）
- 航图 [FAA d-TPP](https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/dtpp/)（公共领域，
  覆盖美国及其属地、密克罗尼西亚/马绍尔/帕劳）；其余机场外链 [ChartFox](https://chartfox.org/)（需 VATSIM 账号）。
  **仅供仿真使用，真实飞行请用官方现行有效版本。**
- 实时航班 [adsb.fi](https://adsb.fi/) / [adsb.lol](https://adsb.lol/)（社区众包 ADS-B）
- 航线查询 [adsbdb](https://www.adsbdb.com/) / [hexdb.io](https://hexdb.io/)
- 地图引擎 [MapLibre GL JS](https://maplibre.org/) 5.6.1（BSD-3）
- 字形 Noto Sans Regular（SIL OFL）

## 许可证

Copyright (C) 2026 FlitAI

本项目以 **GNU General Public License v3.0** 授权，完整条款见 [LICENSE](LICENSE)。

这意味着你可以自由使用、修改和分发本项目，但**任何分发出去的衍生作品必须同样
以 GPL-3.0 开源**，并附带源码。

`vendor/` 下的第三方组件（MapLibre GL JS、PMTiles JS、Noto Sans 字形）不受此
许可证约束，各自沿用 BSD-3-Clause / SIL OFL；`data/` 下的数据来自公共领域数据集。
详见 **[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)**。

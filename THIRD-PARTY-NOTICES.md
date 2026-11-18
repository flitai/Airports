# 第三方组件声明

本项目自身的代码以 GPL-3.0 授权（见 [LICENSE](LICENSE)）。`vendor/` 下的文件
是第三方作品，**不受本项目许可证约束**，各自沿用下列原始授权。分发本项目时
必须一并保留这些声明。

## vendor/maplibre-gl.js · vendor/maplibre-gl.css

MapLibre GL JS 5.6.1 — **BSD-3-Clause**

Copyright (c) 2020, MapLibre contributors
Copyright (c) 2020, Mapbox Inc.

完整条款：https://github.com/maplibre/maplibre-gl-js/blob/v5.6.1/LICENSE.txt

`maplibre-gl.js` 文件头部保留了原始的 `@license` 声明。

## vendor/pmtiles.js

PMTiles JS — **BSD-3-Clause**

Copyright (c) 2021 Protomaps LLC

完整条款：https://github.com/protomaps/PMTiles/blob/main/LICENSE

> ⚠️ 该文件经过压缩，构建工具剥掉了原始的许可证头注释。此处的声明即为对
> BSD-3-Clause「保留版权声明」条款的履行。

## vendor/glyphs.js

内嵌的 SDF 字形图集，由 **Noto Sans Regular** 生成（拉丁字符 0-255）——
**SIL Open Font License 1.1**

Copyright (c) The Noto Project Authors (https://github.com/notofonts/latin-greek-cyrillic)

完整条款：https://openfontlicense.org/

OFL 允许字体随任意许可证的软件一同分发，与本项目的 GPL-3.0 不冲突；
但字形数据本身仍属 OFL，衍生字体作品需遵守 OFL 的同名条款。

## sdr/（本地接收机，可选）

`sdr/` 下的代码属于本项目，同样以 GPL-3.0 授权。其中部分模块移植自 SDR Lab，
逐文件的来源见 [sdr/NOTICE.md](sdr/NOTICE.md)。

运行时调用的两个外部程序**不随本仓库分发**，由使用者自行安装，各自沿用原始授权：

- [librtlsdr](https://osmocom.org/projects/rtl-sdr/wiki) — GPL-2.0-or-later。通过动态库加载调用。
- [readsb](https://github.com/wiedehopf/readsb) — GPL-3.0-or-later。作为独立子进程运行，经管道交换数据。

Python 依赖 aiohttp（Apache-2.0）与 NumPy（BSD-3-Clause）由 `uv` 安装到 `sdr/.venv/`，
同样不在仓库中。

---

# 数据授权

数据不是软件，不受 GPL-3.0 约束，各自沿用来源授权。

| 数据 | 位于 | 授权 |
|---|---|---|
| 机场数据 [OurAirports](https://ourairports.com/) | `data/airports.js` `closed.js` `details.js` `meta.js` | 公共领域 |
| [Natural Earth](https://www.naturalearthdata.com/) 1:50m | `data/basemap.js` | 公共领域 |
| [FAA d-TPP](https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/dtpp/) 航图索引 | `data/faa.js` | 公共领域（美国政府作品） |
| [Protomaps](https://protomaps.com/) 矢量瓦片（[OpenStreetMap](https://www.openstreetmap.org/copyright) 数据） | `tiles/`，**不在仓库中** | ODbL 1.0 |
| AWS Terrain Tiles 高程 | `tiles/dem/`，**不在仓库中** | 见 [数据源声明](https://registry.opendata.aws/terrain-tiles/) |

用到 Protomaps / OSM 瓦片时，ODbL 要求标注「© OpenStreetMap contributors」——
页面已在地图归属控件中显示。

航图**仅供仿真使用，真实飞行请使用官方现行有效版本**。

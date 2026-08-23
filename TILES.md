# 矢量底图下载指南

按需下载 Protomaps 全球矢量底图（OpenStreetMap 数据，`.pmtiles` 格式）。

完整的 `planet.pmtiles` 是 z0-15、**128 GB**，其中 z15 一个级就占了将近一半。
多数项目用不到那么细，这两个脚本用来只取需要的部分——都基于 `pmtiles extract`
的 HTTP Range 抓取，按 Hilbert 序只拉需要的字节，不用先下整份。

## 目录

- [先装 pmtiles CLI](#先装-pmtiles-cli)
- [两个脚本怎么选](#两个脚本怎么选)
- [各级实测体积](#各级实测体积)
- [fetch_tiles.py：一次下一整份](#fetch_tilespy一次下一整份)
- [fetch_levels.py：分级下载、按需拼装](#fetch_levelspy分级下载按需拼装)
- [事后补层级](#事后补层级)
- [构建版本要对齐](#构建版本要对齐)
- [下完怎么用](#下完怎么用)
- [坑与注意事项](#坑与注意事项)

---

## 先装 pmtiles CLI

**这两个脚本都依赖 pmtiles CLI，没装会直接报「找不到 pmtiles 命令」。**

注意和 `download_offline_maps.py` 的区别：那个是自包含的，只用 Python 标准库
直接 HTTP Range 拖整份 128 GB 的 planet.pmtiles，**不需要 CLI**。
而「只下一部分」必须靠 `pmtiles extract` 按 Hilbert 序做区间抓取，绕不开 CLI。

### macOS

    brew install pmtiles

### Windows

1. 打开 https://github.com/protomaps/go-pmtiles/releases
2. 在最新版的 Assets 里下 `go-pmtiles_<版本>_Windows_x86_64.zip`
   （Surface / 骁龙本等 ARM 机器选 `Windows_arm64`）
3. 解压出 `pmtiles.exe`
4. 让脚本能找到它，两种办法：
   - **省事**：把 `pmtiles.exe` 直接丢到和脚本同一个文件夹，在那个文件夹里跑
     （Windows 下 Python 找命令时会先看当前目录）
   - **一劳永逸**：放到比如 `C:\Tools\`，把这个路径加进 PATH
     （设置 → 系统 → 系统信息 → 高级系统设置 → 环境变量 → Path → 新建）

装好后验证：

    pmtiles --help

能打印出 `Commands: show / extract / merge / ...` 就对了。改了 PATH 要**重开
PowerShell 窗口**才生效。

### Windows 上的写法差异

本文命令一律写 `python3`，Windows 上通常是 `python`：

    python fetch_tiles.py --maxzoom 9
    python fetch_levels.py --plan

多行命令的续行符 `\` 是 macOS/Linux 的写法，PowerShell 里换成反引号 `` ` ``，
或者干脆写成一行。

产物落在**脚本所在目录**下的 `tiles\`，不是你当前所在的目录。

### 都不想装怎么办

那就只能整份下：`python download_offline_maps.py --planet`，128 GB，
支持断点续传，只依赖 Python 标准库。

脚本本身只用 Python 3.8+ 标准库，不需要 pip 装任何东西。

## 两个脚本怎么选

| 脚本 | 做什么 | 什么时候用 |
|---|---|---|
| `fetch_tiles.py` | 一次下一整份（某区域 z0-N），下完即成品 | 只做一个项目、参数已经定了、体积不大 |
| `fetch_levels.py` | 每个缩放级单独存一份「零件」，之后本地拼 | 多个项目要不同粒度，想下一次用多次 |

一句话：**下一次就够了用 `fetch_tiles.py`，要反复用不同粒度用 `fetch_levels.py`。**

体积超过 10 GB 左右也建议用 `fetch_levels.py`——`pmtiles extract` 不支持断点续传，
`fetch_levels.py` 会切块下，断了只补没完成的块。

## 各级实测体积

`pmtiles --dry-run` 实测（build 20260817，全球，SI 单位，和 pmtiles 自己的输出一致）：

| 级别 | 单级体积 | 累计 z0-N | 瓦片条目数 |
|---|---:|---:|---:|
| z0-8（底座） | 551 MB | 551 MB | 33,313 |
| z9 | 1.0 GB | 1.6 GB | 145,762 |
| z10 | 2.2 GB | 3.8 GB | 478,393 |
| z11 | 4.2 GB | 8.0 GB | 1,424,189 |
| z12 | 9.6 GB | 17.6 GB | 4,225,275 |
| z13 | 18 GB | 35.6 GB | 12,921,650 |
| z14 | 32 GB | 67.6 GB | 39,331,727 |
| z15 | ≈60 GB | 128 GB | — |

怎么选：

- **z12** 够看清城市路网和街区，17.6 GB
- **z13** 能看清单条街道、大型建筑轮廓，35.6 GB
- **z14** 能看清单条跑道和滑行道，67.6 GB
- **z15** 只在需要看清建筑门牌级别时才值，代价是体积翻倍

z0-8 这 9 级加起来才 551 MB，拆开毫无意义，`fetch_levels.py` 一律把它们合成
单个「底座」档案。

---

## fetch_tiles.py：一次下一整份

给定区域 + 缩放上限，下完直接就是能用的 `.pmtiles`，不用拼装。

### 最常用的三条

    python3 fetch_tiles.py --maxzoom 12 --estimate        # 先估体积，不下载
    python3 fetch_tiles.py --maxzoom 12                   # 全球 z0-12（17.6 GB）
    python3 fetch_tiles.py --region china --maxzoom 14    # 中国 z0-14

自定义范围：

    python3 fetch_tiles.py --bbox 118,29.5,123,33 --maxzoom 14

预设区域（`--list-regions` 查看边界值）：

| 名字 | 范围 | 名字 | 范围 |
|---|---|---|---|
| `china` | 中国 | `japan` | 日本 |
| `yrd` | 长三角 | `sea` | 东南亚 |
| `prd` | 珠三角 | `europe` | 欧洲 |
| `jingjinji` | 京津冀 | `usa` | 美国本土 |

### 它跑起来会做什么

1. 自动找当前可用的 Protomaps 构建（按周往前试，最多 12 周）
2. 跑一次 `--dry-run` **精确**算体积——不是估算，是真实字节数
3. 打印体积、瓦片数、按 10 / 30 / 100 MB/s 各要多久
4. 问你 `[y/N]`，确认才开下（`-y` 跳过）
5. 下完校验：PMTiles 文件头 + 随机抽 48 处查是否全零 + `pmtiles show` 摘要

输出默认落在 `tiles/{区域}-z{maxzoom}.pmtiles`，`-o` 可改名。

### 参数

| 参数 | 用处 |
|---|---|
| `--maxzoom N` | **必填**，缩放上限 |
| `--minzoom N` | 只要高层次（默认 0）。脚本会警告：这份缺低缩放层，单独用缩小时会空白 |
| `--region NAME` | 预设区域 |
| `--bbox W,S,E,N` | 自定义矩形范围 |
| `--region-file x.geojson` | 按任意多边形形状裁剪，比矩形省体积。**`fetch_levels.py` 不支持这个**，是本脚本独有 |
| `-o FILE` | 输出文件名 |
| `--threads N` | 下载线程，默认 4 |
| `--build YYYYMMDD` | 钉死构建日期，保证可复现 |
| `--estimate` | 只估体积不下载 |
| `-y` | 跳过确认 |

---

## fetch_levels.py：分级下载、按需拼装

把每个缩放级单独存成一个「零件」，之后本地 merge 拼成品。
z14 只下一次，A 项目要 z0-12、B 项目要 z0-14，都只是本地拼装，不重下。

### 典型流程

    python3 fetch_levels.py --plan          # 查体积表，不联网，秒出
    python3 fetch_levels.py --levels 9-14   # 下 z9…z14，每级一个档案
    python3 fetch_levels.py --compose 0-12  # 拼出 tiles/global-z0-12.pmtiles
    python3 fetch_levels.py --compose 0-14  # 同一批零件再拼一份完整的
    python3 fetch_levels.py --status        # 看本地有哪些零件和成品

层级写法：`9-14`（连续）、`9,11,13`（离散）、`0-14`（含底座）。
`0-8` 之内的任何写法都折叠成一个底座档案，所以 `--levels 0-14` 产出 **7 个文件**：
底座 z0-8，加 z9…z14 各一个。

零件落在 `tiles/levels/`，成品落在 `tiles/`。
拼装是本地 merge（1 GB/s 量级），35 GB 大概一分多钟。

### 混合粒度：全球粗、重点区域细（有坑，先读完）

想「全球 z0-12 打底，中国境内补到 z14」，技术上 merge 是能合的：

    python3 fetch_levels.py --levels 0-12
    python3 fetch_levels.py --levels 13-14 --region china
    pmtiles merge tiles/levels/global-z0-8.pmtiles \
                  tiles/levels/global-z9.pmtiles \
                  tiles/levels/global-z10.pmtiles \
                  tiles/levels/global-z11.pmtiles \
                  tiles/levels/global-z12.pmtiles \
                  tiles/levels/china-z13.pmtiles \
                  tiles/levels/china-z14.pmtiles \
                  tiles/mixed.pmtiles

体积很香：全球 z0-12 是 17.6 GB，中国 z13-z14 只有几 GB，
远小于全球 z0-14 的 67.6 GB。

**但合出来的档案在中国境外放大到 z13 以上会一片空白。**
原因在 `vendor/pmtiles.js` 里能直接看到：矢量档案取不到瓦片时它返回的是
`{data: new Uint8Array}`——一个**空瓦片**，不是 404。MapLibre 把空瓦片当成
「这块确实没东西」正常渲染掉，不会回退去拉伸父级瓦片。而 merge 后档案头部的
maxzoom 变成 14，MapLibre 就不再对 z12 做 overzoom，转而真去请求 z13/z14，
境外自然全空。

所以：

- **应用只在该区域内使用** → 混合粒度可以用，省一大半体积
- **要在全球任意位置都能放大** → 别混，老老实实全球下到同一个 maxzoom。
  纯 z0-12 的档案在 z13+ 会自动 overzoom 拉伸 z12 瓦片，路网建筑都还在，
  只是不长出新细节——这比空白好得多

同样的道理，`fetch_tiles.py --minzoom 9` 下出来的档案单独用会在**缩小**时空白，
脚本本身也会提示这一点。

### 参数

| 参数 | 用处 |
|---|---|
| `--levels SPEC` | 要下载的层级，如 `9-14` 或 `9,11,13` |
| `--compose SPEC` | 把已有层级拼成一个成品，如 `0-12` |
| `--plan` | 只看体积表；加 `--exact` 联网实算而非查内置实测表 |
| `--status` | 列出本地零件和成品，含每份的 zoom 范围和瓦片数 |
| `--region` / `--bbox` | 限定范围（默认全球）；`--scope-name` 给 bbox 起名用于文件命名 |
| `--chunk-size N` | 每块目标体积 GB，默认 4。调小＝断线损失小、请求多 |
| `--keep-parts` | 合并后保留分块文件 |
| `--build YYYYMMDD` | 钉死构建日期 |
| `-y` | 跳过逐级确认 |

### 分块下载

`pmtiles extract` **不支持断点续传**，32 GB 的 z14 断在 30 GB 就白下了。
所以大的层级会先按经纬度切成若干块分别下，中断后重跑只补没完成的块，
全部下完再 merge 成一个档案。默认每块目标 4 GB 时的切法：

| 级别 | 体积 | 切块 |
|---|---:|---|
| z0-8 ～ z10 | ≤2.2 GB | 1 块 |
| z11 | 4.2 GB | 2×1 = 2 块 |
| z12 | 9.6 GB | 3×1 = 3 块 |
| z13 | 18 GB | 3×2 = 6 块 |
| z14 | 32 GB | 4×2 = 8 块 |

块大小**不均匀**——海洋块小、欧亚块大，实际能差好几倍。4 GB 是目标不是保证。
分块写入用「先写 `.tmp` 再改名」，中断不会留下半截文件被误判为完成。

---

## 事后补层级

**已经有 z0-12 的成品，现在想补 z13——必须合并才能用。**

单独放着的 z13 文件不会被用到：MapLibre 的一个 source 只能指向一个 `.pmtiles`，
两个文件就是两个 source，Protomaps 的样式没法这么用。

不合并的话 z0-12 自己是完全可用的——放大超过 z12 时 MapLibre 会把 z12 的瓦片
拉伸显示（overzoom），路网、建筑都还在，只是不会多出 z13 才有的细节。
那份没合并的 z13 就是纯占硬盘。

### 零件还在

`tiles/levels/` 下的 global-z0-8 … global-z12 都没删：

    python3 fetch_levels.py --levels 13 --build 20260817
    python3 fetch_levels.py --compose 0-13      # → tiles/global-z0-13.pmtiles

### 零件已删、只剩成品

直接拿成品当一个输入 merge——按缩放级天然不重叠：

    pmtiles merge tiles/global-z0-12.pmtiles \
                  tiles/levels/global-z13.pmtiles \
                  tiles/global-z0-13.pmtiles

合并后档案头部的 min/max zoom 会自动更新为 z0-13，metadata 取自第一个输入。

**注意磁盘**：merge 不能原地做，输出是新文件。峰值要同时放下
17.6 + 18 + 35.6 ≈ **71 GB**，合完确认没问题再删旧的。

## 构建版本要对齐

两个脚本每次跑都会自动找**当前最新**的 Protomaps 构建（Protomaps 只保留最近若干个）。
如果 z0-12 是上个月下的、z13 是今天下的，两者可能来自不同批次的 OSM 数据——
merge 不会报错（它只保留第一个输入的 metadata），但可能出现「某条路 z13 有、
z12 没有」这种不一致。

所以补下高层次时最好钉死构建：

    python3 fetch_levels.py --levels 13 --build 20260817

想确认已有文件是哪批数据，看 metadata 里的 `planetiler:buildtime` 和
`planetiler:osm:osmosisreplicationseq`：

    pmtiles show --metadata tiles/global-z0-12.pmtiles

两份对得上就放心合。构建日期列表见 https://build.protomaps.com/

## 下完怎么用

1. 把文件名加进 `index.html` 的 `PMTILES_CANDIDATES`（约第 692 行），
   或直接改名成 `tiles/planet.pmtiles`——这个名字已经在列表里
2. 用 `python3 serve.py` 起服务

**不能用 `python3 -m http.server`**：它完全忽略 Range 请求头，对
`Range: bytes=100-199` 会返回 200 和整个文件。PMTiles 靠 Range 从单个大文件里
取瓦片，用它服务几十 GB 的档案会导致每取一个瓦片都尝试传输整个文件。
项目自带的 `serve.py` 实现了 Range（返回 206 + Content-Range）。

跨机器拷贝用 `rsync -avP`（可断点续传）：

    rsync -avP tiles/global-z0-13.pmtiles 用户@目标机:<项目目录>/tiles/

## 坑与注意事项

**1. `pmtiles extract` 不支持断点续传。**
中断就得从头再来。体积大的用 `fetch_levels.py` 的分块下载。

**2. 相邻分块的 bbox 会重叠，merge 会直接拒绝。**
实测拿瓦片边界当 bbox 时，`pmtiles merge` 报
`27 overlapping tiles, starting with 12 3413 1663. Inputs must be disjoint`——
边界那一列瓦片被两块都包含了。`fetch_levels.py` 的分块 bbox 取的是**首末瓦片的
中心点**而不是瓦片边界，规避了这个问题。手写 `pmtiles extract --bbox` 拼多块时
要注意同样的坑。

**3. 分级拼装的产物和一次性下载完全等价。**
已验证：长三角 z0-12，分 5 个零件下载再拼装 vs 一次性 `pmtiles extract`，
两者都是 **3800 个瓦片、z0-12**，体积差 673 字节（目录布局差异）。

**4. `--dry-run` 是免费的。**
只读档案的目录结构，不下瓦片数据。全球 z9 约 7 秒，z14 约 23 秒。
下大东西之前先跑一次心里有底。

**5. 单位。**
`pmtiles` 自己输出的是 SI 单位（GB = 10⁹），本文体积表跟它一致。
两个脚本的进度显示用的是二进制单位（GB = 2³⁰），所以脚本里看到的数字会比
本表小约 7%，是同一个字节数的两种写法。

**6. Windows 上进度条刷屏闪烁（已修）。**
pmtiles 自带的进度条按终端宽度画满一整行，还用了 █（U+2588）。中文 Windows 的
PowerShell 默认 GBK 代码页，这个字符的实际显示宽度和 pmtiles 以为的对不上，
于是每次重绘都超出行宽触发自动换行——它每秒重绘十几次，就变成不停滚屏刷空行、
疯狂闪烁（实测一次 30 秒的下载会发出 433 次重绘）。

两个脚本现在把 pmtiles 的输出接过来，只画一行 78 字符的纯 ASCII，每 0.5 秒刷
一次，从根上避开换行：

    fetching chunks     63%  34/54 MB, 3.6 MB/s  [12s:7s]

输出重定向到文件时自动改成 15 秒打一整行，不刷屏。想要 pmtiles 原来那个花哨的
进度条，加 `--raw-progress`。

**7. 缺瓦片 = 空白，不是自动降级。**
矢量档案里没有的瓦片，pmtiles.js 返回空瓦片而非 404，MapLibre 会当成「这里
本来就没东西」渲染掉，不会回退到父级瓦片。所以档案的 zoom 范围内必须是**满**的，
残缺的范围会直接表现为空白。详见上面「混合粒度」一节。

**8. 磁盘峰值。**
分块下载的合并阶段要同时放下所有分块和成品，z14 峰值约 64 GB
（合并完自动删分块，加 `--keep-parts` 保留）。脚本在开下之前会检查可用空间
并拒绝启动。

## 数据来源与授权

- 矢量瓦片 [Protomaps](https://protomaps.com) basemap，数据来自
  [OpenStreetMap](https://www.openstreetmap.org/copyright)（ODbL）
- 构建列表 https://build.protomaps.com/
- 用在自己的地图上时要保留 OSM 署名

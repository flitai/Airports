# 全球离线底图下载指南

把 Protomaps 全球矢量底图和 AWS 全球高程整份拉到本地，让项目在**完全断网**的
情况下也能看到街道级底图和山体阴影。

这是可选的重型数据集（合计约 135 GB），不下载不影响项目使用——默认的
Natural Earth 底图已随仓库提供。

> 只想要部分缩放级、不想要整份 128 GB？看 **[TILES.md](TILES.md)**，
> 那里的 `fetch_tiles.py` / `fetch_levels.py` 基于 `pmtiles extract` 按需抓取。
> 本文讲的 `download_offline_maps.py` 是**自包含**下载器，只依赖 Python 3.8+
> 标准库、不需要 pmtiles CLI，可拷到任意一台网络好的机器单独运行，下完再把
> 产物拷回项目。

## 最终目录结构

数据全部放在项目下的 `tiles/`（该目录已在 `.gitignore` 中，不进版本库）：

```
<项目目录>/
  tiles/
    planet.pmtiles          128 GB   Protomaps 全球矢量底图 z0-15（含建筑高度）
    dem/{z}/{x}/{y}.png     6.9 GB   AWS 全球高程 z0-8，87,381 个文件
```

DEM 用松散文件而非打包格式，是因为 MapLibre 的 `raster-dem` 源直接吃
`tiles/dem/{z}/{x}/{y}.png` 这个 URL 模板，不需要额外的协议层，也不需要
服务器支持 Range。（脚本也支持 `--layout mbtiles` 打成单个 SQLite 文件。）

## 在下载机上

    python3 download_offline_maps.py --all       # 两个都下，支持断点续传

DEM 是 8.7 万次小请求，速度受并发数支配。实测（每档 120 个瓦片，正反序各跑一遍）：

| 并发 | 速率 | 87,381 瓦片耗时 | 可复现性 |
|---:|---:|---:|---|
| 16 | 10.6 瓦片/秒 | 2.3 小时 | 稳定 |
| **48** | **27.4 瓦片/秒** | **0.9 小时** | 稳定（默认值） |
| 96 | 2.3 ~ 33 瓦片/秒 | 0.7 ~ 10.6 小时 | 不稳定，会触发限流 |

96 并发在"新鲜"时能到 33 瓦片/秒，但累积负载后崩到 2.3——只比 48 快 21%
却极不稳定，所以默认取 48。planet 是单个大文件的顺序下载，不受此影响。

下完打包 DEM 便于传输：

    tar -cf dem.tar -C tiles dem

**打包不要加 `-z`**：png 已经是压缩格式，实测 gzip 只省 3.9%（89.7→86.2 MB）
却多花 2.5 倍时间。打包的意义是把 8 万次小文件传输变成一次大文件传输。

## 拷回项目机

把下面的 `<用户>@<项目机>` 和 `<项目目录>` 换成实际值：

    rsync -avP tiles/planet.pmtiles  <用户>@<项目机>:<项目目录>/tiles/
    rsync -avP dem.tar               <用户>@<项目机>:~/

在项目机上解包并自检：

    mkdir -p <项目目录>/tiles
    tar -xf ~/dem.tar -C <项目目录>/tiles/
    cd <项目目录> && python3 download_offline_maps.py --check

## 断点续传

两者都支持，中断后重跑同一条命令即可：planet 用 HTTP Range 续传（已逐字节
校验拼接正确），DEM 跳过已存在的文件（写入用「先写 `.part` 再改名」，中断
不会留下半截文件被误判为完成）。

## DEM 为什么只到 z8

地形是平滑的，z8 overzoom 到高缩放级别做山体阴影几乎无损。而 DEM 每个瓦片
都有数据（海洋是水深），没有空瓦片可省，体积增长极陡：z0-9 要 28 GB、
z0-10 要 99 GB、z0-12 要 645 GB。

## 必须用支持 Range 的服务器

**`python3 -m http.server` 不支持 Range 请求**（实测：发 `Range: bytes=100-199`
它返回 200 和整个文件）。PMTiles 完全依赖 Range 读单文件，用它服务 128 GB 的
archive 会导致每取一个瓦片都尝试传输整个文件。

用项目自带的 `serve.py`（`start.command` / `start.bat` 会自动调它），或者
`pmtiles serve`。DEM 是松散 png，不受此限制。

另外注意 `file://` 双击模式下浏览器禁止 `fetch`，探测不到本地瓦片，
**离线一档只能回落到 Natural Earth**——要用 PMTiles 必须走启动器。

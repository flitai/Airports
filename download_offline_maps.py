#!/usr/bin/env python3
# Airports · 全球机场分布地图
# Copyright (C) 2026 FlitAI
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-only
"""离线地图下载器 —— 可在任意机器独立运行，只依赖 Python 3.8+ 标准库。

产出两个文件，拷回 Airports 项目的 tiles/ 目录即可：

    planet.pmtiles   约 128 GB   Protomaps 全球矢量底图 z0-15（含建筑高度）
    dem.mbtiles      约 6.9 GB   AWS Terrain Tiles 全球高程 z0-8（terrarium 编码）

用法：
    python3 download_offline_maps.py --all            # 两个都下
    python3 download_offline_maps.py --planet         # 只下矢量底图
    python3 download_offline_maps.py --dem            # 只下高程
    python3 download_offline_maps.py --dem --maxzoom 9  # 高程下到 z9（28 GB）
    python3 download_offline_maps.py --check          # 只校验已下载的完整性

两者都支持断点续传：中断后重跑同一条命令即可，已下载的部分不会重来。

为什么 DEM 存成单个 .mbtiles 而不是 87,381 个 png：
跨机器拷贝几万个小文件比拷贝同等体积的单文件慢一到两个数量级。
MBTiles 就是个 SQLite 文件，拷回来后可用 `pmtiles convert dem.mbtiles dem.pmtiles`
转成 PMTiles，或让应用直接读 MBTiles。
"""
import argparse, os, sqlite3, sys, threading, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(HERE, "tiles")

PLANET_BUILD = "20260817"          # 已验证存在；失效时脚本会自动往前找
PLANET_URL = "https://build.protomaps.com/{build}.pmtiles"
DEM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
VERSION = "1.3"          # 用 --version 查看；1.0 有致命 bug，务必用 >=1.1
UA = {"User-Agent": "airports-offline-maps/1.0"}


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.2f} {u}"
        n /= 1024


def head_size(url):
    try:
        req = urllib.request.Request(url, method="HEAD", headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            return int(r.headers.get("Content-Length", 0))
    except Exception:
        return 0


# --------------------------------------------------------------------------
# 1) Protomaps planet：单个大文件，HTTP Range 断点续传
# --------------------------------------------------------------------------
def find_build():
    """按周向前找可用的构建（Protomaps 只保留最近若干个）。"""
    import datetime
    d = datetime.date(int(PLANET_BUILD[:4]), int(PLANET_BUILD[4:6]), int(PLANET_BUILD[6:]))
    for _ in range(12):
        url = PLANET_URL.format(build=d.strftime("%Y%m%d"))
        n = head_size(url)
        if n:
            return url, n
        d -= datetime.timedelta(days=7)
    return None, 0


def download_planet():
    url, total = find_build()
    if not url:
        print("找不到可用的 Protomaps 构建，请到 https://build.protomaps.com/ 查看最新日期，"
              "改脚本顶部的 PLANET_BUILD", file=sys.stderr)
        return False
    dest = os.path.join(OUTDIR, "planet.pmtiles")
    os.makedirs(OUTDIR, exist_ok=True)
    have = os.path.getsize(dest) if os.path.exists(dest) else 0

    print(f"\n=== Protomaps planet ===")
    print(f"来源  {url}")
    print(f"目标  {dest}")
    print(f"总量  {human(total)}" + (f"，已有 {human(have)}，续传" if have else ""))
    if have >= total > 0:
        print("已完整，跳过")
        return True

    t0, start_have = time.time(), have
    while have < total:
        req = urllib.request.Request(url, headers={**UA, "Range": f"bytes={have}-"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r, open(dest, "ab") as f:
                while True:
                    chunk = r.read(8 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    have += len(chunk)
                    el = time.time() - t0
                    spd = (have - start_have) / el if el else 0
                    eta = (total - have) / spd if spd else 0
                    print(f"\r  {human(have)} / {human(total)}  "
                          f"({have/total:6.2%})  {human(spd)}/s  "
                          f"剩余 {eta/3600:.1f} 小时   ", end="", flush=True)
        except Exception as e:
            print(f"\n  中断（{type(e).__name__}），5 秒后续传…", flush=True)
            time.sleep(5)
            have = os.path.getsize(dest) if os.path.exists(dest) else 0
            t0, start_have = time.time(), have
    print(f"\n完成：{human(os.path.getsize(dest))}")
    return True


# --------------------------------------------------------------------------
# 2) DEM：大量小瓦片 -> 单个 MBTiles(SQLite)
# --------------------------------------------------------------------------
def open_mbtiles(path, maxzoom):
    fresh = not os.path.exists(path)
    # writer 线程要用这个连接，必须关掉同线程检查（写入已由单个 writer 串行化）
    db = sqlite3.connect(path, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    if fresh:
        db.execute("CREATE TABLE metadata (name text, value text)")
        db.execute("CREATE TABLE tiles (zoom_level integer, tile_column integer, "
                   "tile_row integer, tile_data blob)")
        db.execute("CREATE UNIQUE INDEX tile_index ON tiles "
                   "(zoom_level, tile_column, tile_row)")
        db.executemany("INSERT INTO metadata VALUES (?,?)", [
            ("name", "AWS Terrain Tiles (terrarium)"),
            ("format", "png"), ("type", "baselayer"), ("version", "1"),
            ("minzoom", "0"), ("maxzoom", str(maxzoom)),
            ("bounds", "-180.0,-85.0511,180.0,85.0511"),
            ("description", "Global DEM, terrarium encoding: "
                            "elevation_m = (R*256 + G + B/256) - 32768"),
            ("attribution", "AWS Terrain Tiles / Mapzen"),
        ])
        db.commit()
    return db


def download_dem_files(maxzoom, workers):
    """松散文件布局：tiles/dem/{z}/{x}/{y}.png
    MapLibre 的 raster-dem 源可直接用这个 URL 模板，不需要 Range 支持。"""
    root = os.path.join(OUTDIR, "dem")
    def target(z, x, y):
        return os.path.join(root, str(z), str(x), f"{y}.png")

    jobs = [(z, x, y) for z in range(maxzoom + 1)
            for x in range(2 ** z) for y in range(2 ** z)
            if not os.path.exists(target(z, x, y))]
    total = sum(4 ** z for z in range(maxzoom + 1))
    print(f"\n=== AWS Terrain Tiles (DEM) z0-{maxzoom} · 松散文件 ===")
    print(f"目标  {root}/{{z}}/{{x}}/{{y}}.png")
    print(f"总量  {total:,} 个瓦片，待下载 {len(jobs):,}")
    if not jobs:
        print("已完整，跳过"); return True

    t0, done, failed = time.time(), [0], []
    def grab(t):
        z, x, y = t
        p = target(z, x, y)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        for attempt in range(4):
            try:
                req = urllib.request.Request(DEM_URL.format(z=z, x=x, y=y), headers=UA)
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                if data:
                    tmp = p + ".part"                 # 先写临时文件再改名，
                    with open(tmp, "wb") as f:        # 避免中断留下半截文件被误判为已完成
                        f.write(data)
                    os.replace(tmp, p)
                break
            except urllib.error.HTTPError as e:
                if e.code in (403, 404):
                    break
                time.sleep(1 + attempt)
            except Exception:
                time.sleep(1 + attempt)
        else:
            failed.append(t)
        done[0] += 1
        if done[0] % 2000 == 0:
            el = time.time() - t0; rate = done[0] / el if el else 0
            eta = (len(jobs) - done[0]) / rate if rate else 0
            print(f"\r  {done[0]:,}/{len(jobs):,}  {rate:.0f} 瓦片/秒  "
                  f"剩余 {eta/60:.0f} 分钟   ", end="", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(grab, jobs))
    n = sum(len(fs) for _, _, fs in os.walk(root))
    sz = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(root) for f in fs)
    print(f"\n完成：{n:,} 个文件，{human(sz)}，失败 {len(failed)} 个")
    return not failed


def download_dem(maxzoom, workers):
    os.makedirs(OUTDIR, exist_ok=True)
    path = os.path.join(OUTDIR, "dem.mbtiles")
    db = open_mbtiles(path, maxzoom)

    have = set(db.execute("SELECT zoom_level, tile_column, tile_row FROM tiles"))
    jobs = []
    for z in range(maxzoom + 1):
        n = 2 ** z
        for x in range(n):
            for y in range(n):
                if (z, x, n - 1 - y) not in have:      # MBTiles 用 TMS，y 轴翻转
                    jobs.append((z, x, y))
    total = sum(4 ** z for z in range(maxzoom + 1))
    print(f"\n=== AWS Terrain Tiles (DEM) z0-{maxzoom} ===")
    print(f"目标  {path}")
    print(f"总量  {total:,} 个瓦片，已有 {len(have):,}，待下载 {len(jobs):,}")
    if not jobs:
        print("已完整，跳过")
        return True

    q, t0, done, failed = Queue(maxsize=2000), time.time(), [0], []

    werr = []
    def writer():
        buf = []
        try:
            while True:
                item = q.get()
                if item is None:
                    break
                buf.append(item)
                if len(buf) >= 256:
                    db.executemany("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)", buf)
                    db.commit(); buf = []
            if buf:
                db.executemany("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)", buf)
                db.commit()
        except Exception as e:
            werr.append(e)
            print(f"\n写入线程出错: {type(e).__name__}: {e}", file=sys.stderr, flush=True)

    wt = threading.Thread(target=writer, daemon=True); wt.start()

    def grab(t):
        z, x, y = t
        for attempt in range(4):
            try:
                req = urllib.request.Request(DEM_URL.format(z=z, x=x, y=y), headers=UA)
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                if data:
                    q.put((z, x, (2 ** z) - 1 - y, sqlite3.Binary(data)))
                break
            except urllib.error.HTTPError as e:
                if e.code in (403, 404):
                    break                       # 该瓦片本就不存在
                time.sleep(1 + attempt)
            except Exception:
                time.sleep(1 + attempt)
        else:
            failed.append(t)
        done[0] += 1
        if done[0] % 2000 == 0:
            el = time.time() - t0
            rate = done[0] / el if el else 0
            eta = (len(jobs) - done[0]) / rate if rate else 0
            sz = os.path.getsize(path)
            print(f"\r  {done[0]:,}/{len(jobs):,}  {rate:.0f} 瓦片/秒  "
                  f"{human(sz)}  剩余 {eta/60:.0f} 分钟   ", end="", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(grab, jobs))
    q.put(None); wt.join()
    if werr:
        print("下载已中止：写入失败", file=sys.stderr); return False

    n = db.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()
    print(f"\n完成：{n:,} 个瓦片，{human(os.path.getsize(path))}，失败 {len(failed)} 个")
    if failed:
        print("重跑本命令即可补齐失败的瓦片")
    return not failed


# --------------------------------------------------------------------------
def check():
    print("\n=== 完整性校验 ===")
    p = os.path.join(OUTDIR, "planet.pmtiles")
    if os.path.exists(p):
        local = os.path.getsize(p)
        # 断网时不必逐个日期重试，直接跳过远端核对
        remote = 0
        if "--offline" not in sys.argv:
            _, remote = find_build()
        problems = []
        if remote and local != remote:
            problems.append(f"大小不符：{human(local)} ≠ 应为 {human(remote)}")

        with open(p, "rb") as f:
            magic = f.read(7)
        if magic != b"PMTiles":
            problems.append(f"文件头不是 PMTiles，读到 {magic!r}")

        # 只比大小会被「全零文件」骗过去：传输中断、介质故障、预分配后失败，
        # 都会留下大小完全正确但内容全空的文件。必须抽样看内容。
        import random as _rnd
        _rnd.seed(0)
        zero = 0
        with open(p, "rb") as f:
            for _ in range(64):
                f.seek(_rnd.randrange(0, max(1, local - 4096)))
                if not any(f.read(4096)):
                    zero += 1
        if zero > 48:
            problems.append(f"内容抽样 64 处有 {zero} 处全零，文件基本是空的")

        print(f"planet.pmtiles  {human(local)}"
              + (f" / 应为 {human(remote)}" if remote else "  （未联网核对远端大小）")
              + ("   ✅ 完整" if not problems else "   ❌ 损坏"))
        for w in problems:
            print(f"    ✗ {w}")
        if problems:
            print("    → 必须重新传输。重传前先在源机器上用同样方式核对源文件")
    else:
        print("planet.pmtiles  未下载")

    root = os.path.join(OUTDIR, "dem")
    if os.path.isdir(root):
        n = sum(len(fs) for _, _, fs in os.walk(root))
        sz = sum(os.path.getsize(os.path.join(dp, f))
                 for dp, _, fs in os.walk(root) for f in fs)
        mz = max((int(d) for d in os.listdir(root) if d.isdigit()), default=-1)
        expect = sum(4 ** z for z in range(mz + 1))
        print(f"dem/ (松散文件)  {human(sz)}  {n:,}/{expect:,} 个瓦片 (z0-{mz})  "
              f"{'✅ 完整' if n >= expect else '⚠️ 不完整，重跑 --dem 续传'}")

    d = os.path.join(OUTDIR, "dem.mbtiles")
    if os.path.exists(d):
        db = sqlite3.connect(d)
        n = db.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
        mz = int(db.execute("SELECT value FROM metadata WHERE name='maxzoom'").fetchone()[0])
        expect = sum(4 ** z for z in range(mz + 1))
        db.close()
        print(f"dem.mbtiles     {human(os.path.getsize(d))}  {n:,}/{expect:,} 个瓦片  "
              f"{'✅ 完整' if n >= expect * 0.999 else '⚠️ 不完整，重跑 --dem 续传'}")
    elif not os.path.isdir(root):
        print("dem             未下载")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="下载全球离线地图数据")
    ap.add_argument("--planet", action="store_true", help="下载 Protomaps 矢量底图（128 GB）")
    ap.add_argument("--dem", action="store_true", help="下载 AWS 高程数据")
    ap.add_argument("--all", action="store_true", help="两个都下")
    ap.add_argument("--check", action="store_true", help="只校验已下载内容")
    ap.add_argument("--version", action="version", version=f"download_offline_maps {VERSION}")
    ap.add_argument("--offline", action="store_true",
                    help="校验时跳过与远端核对文件大小（无网络时用）")
    ap.add_argument("--maxzoom", type=int, default=8, help="DEM 最大缩放级（默认 8，约 6.9 GB；9 约 28 GB）")
    ap.add_argument("--workers", type=int, default=48,
                    help="DEM 并发数（默认 48）。实测 16→10.7 瓦片/秒、48→27.4、96 不稳定："
                         "新鲜时 33 但累积负载后崩到 2.3，且只比 48 快 21%%，不建议超过 48")
    ap.add_argument("--layout", choices=("files", "mbtiles"), default="files",
                    help="DEM 存储布局：files=松散 {z}/{x}/{y}.png（默认，MapLibre 可直接用）；"
                         "mbtiles=单个 SQLite 文件（便于拷贝）")
    a = ap.parse_args()

    if a.check:
        check(); sys.exit(0)
    if not (a.planet or a.dem or a.all):
        ap.print_help(); sys.exit(1)

    if a.planet or a.all:
        download_planet()
    if a.dem or a.all:
        (download_dem_files if a.layout == "files" else download_dem)(a.maxzoom, a.workers)
    check()

    dem_files = os.path.isdir(os.path.join(OUTDIR, "dem"))
    pack = ("tar -cf dem.tar -C tiles dem        # 打包 DEM（不加 -z：png 已压缩，"
            "gzip 只省 4% 却多花 2.5 倍时间）") if dem_files else ""
    unpack = "tar -xf dem.tar -C ~/CC/Airports/tiles/" if dem_files else \
             "mv dem.mbtiles ~/CC/Airports/tiles/"
    print(f"""
下载完成。产物在 {OUTDIR}

在这台机器上打包：
    {pack}

拷到目标机器（大文件建议 rsync -avP，可断点续传）：
    rsync -avP tiles/planet.pmtiles  用户@目标机:~/CC/Airports/tiles/
    rsync -avP dem.tar               用户@目标机:~/

在目标机器上就位：
    mkdir -p ~/CC/Airports/tiles
    {unpack}
    cd ~/CC/Airports && python3 download_offline_maps.py --check
""")

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
"""按需下载 Protomaps 全球矢量底图的任意缩放层次 / 任意区域，产出 .pmtiles。

底层用 pmtiles CLI 的 extract：它按 Hilbert 序对远端 planet 文件做 HTTP Range
抓取，只拉需要的字节，不用下整个 128 GB。

用法示例
    python3 fetch_tiles.py --maxzoom 12                     全球 z0-12
    python3 fetch_tiles.py --region china --maxzoom 14      中国 z0-14
    python3 fetch_tiles.py --bbox 118,29.5,123,33 --maxzoom 14
    python3 fetch_tiles.py --minzoom 9 --maxzoom 14 --region china   只要高层次
    python3 fetch_tiles.py --maxzoom 12 --estimate          只估体积不下载
    python3 fetch_tiles.py --list-regions

体积会先用 --dry-run 精确算出来并让你确认，不会闷头下几十 GB。

依赖 pmtiles CLI：
    macOS    brew install pmtiles
    Windows  https://github.com/protomaps/go-pmtiles/releases 下载解压后放进 PATH
"""
import argparse, os, re, shutil, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(HERE, "tiles")
BUILD_URL = "https://build.protomaps.com/{build}.pmtiles"
DEFAULT_BUILD = "20260817"

# 预设区域：name -> (west, south, east, north)
REGIONS = {
    "global":     None,
    "china":      (73.5, 18.0, 135.1, 53.6),
    "yrd":        (118.0, 29.5, 123.0, 33.0),      # 长三角
    "prd":        (112.0, 21.5, 115.5, 24.0),      # 珠三角
    "jingjinji":  (113.4, 36.0, 119.9, 42.7),      # 京津冀
    "japan":      (129.0, 30.5, 146.0, 45.6),
    "sea":        (92.0, -11.0, 141.0, 29.0),      # 东南亚
    "europe":     (-10.0, 35.0, 40.0, 60.0),
    "usa":        (-125.0, 24.5, -66.9, 49.4),     # 美国本土
}
REGION_ZH = {
    "global": "全球", "china": "中国", "yrd": "长三角", "prd": "珠三角",
    "jingjinji": "京津冀", "japan": "日本", "sea": "东南亚",
    "europe": "欧洲", "usa": "美国本土",
}


def die(msg):
    print(f"\n错误：{msg}", file=sys.stderr)
    sys.exit(1)


def need_pmtiles():
    exe = shutil.which("pmtiles")
    if not exe:
        die("找不到 pmtiles 命令。\n"
            "  macOS    brew install pmtiles\n"
            "  Windows  https://github.com/protomaps/go-pmtiles/releases\n"
            "           下载解压后把 pmtiles.exe 放进 PATH")
    return exe


def head_size(url):
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "fetch-tiles/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return int(r.headers.get("Content-Length", 0))
    except Exception:
        return 0


def find_build(pinned=None):
    """Protomaps 只保留最近若干个构建，按周向前找可用的。"""
    import datetime
    b = pinned or DEFAULT_BUILD
    d = datetime.date(int(b[:4]), int(b[4:6]), int(b[6:]))
    for _ in range(12):
        url = BUILD_URL.format(build=d.strftime("%Y%m%d"))
        if head_size(url):
            return url
        if pinned:
            die(f"指定的构建 {pinned} 不存在")
        d -= datetime.timedelta(days=7)
    die("找不到可用的 Protomaps 构建，去 https://build.protomaps.com/ 查最新日期，用 --build 指定")


# --------------------------------------------------------------------------
# 进度显示
# --------------------------------------------------------------------------
BAR_RE = re.compile(r"^(.*?)\s*(\d+)%\s*\|[^|]*\|\s*\(([^)]*)\)(?:\s*\[([^\]]*)\])?")
SPLIT_RE = re.compile(rb"[\r\n]")


def run_pmtiles(cmd, raw=False):
    """跑 pmtiles，把它自带的进度条换成一行纯 ASCII。返回 (返回码, 末尾日志)。

    pmtiles 的进度条按终端宽度画满一整行，还用了 █（U+2588）。中文 Windows 的
    PowerShell 默认 GBK 代码页，这个字符的实际显示宽度和 pmtiles 以为的对不上，
    于是每次重绘都超出行宽触发自动换行——每秒十几次重绘就变成不停滚屏刷空行、
    疯狂闪烁。这里把输出接过来，只画一行短的纯 ASCII，从根上避开换行。
    """
    if raw:
        return subprocess.run(cmd).returncode, ""

    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    tty = sys.stdout.isatty()
    buf, last, live, tail = b"", 0.0, False, []
    try:
        while True:
            chunk = os.read(p.stdout.fileno(), 4096)
            if not chunk:
                break
            segs = SPLIT_RE.split(buf + chunk)
            buf = segs.pop()                      # 最后一段可能不完整，留到下一轮
            for seg in segs:
                s = seg.decode("utf-8", "replace").strip()
                if not s:
                    continue
                m = BAR_RE.search(s)
                if not m:                          # 普通日志行，原样打
                    if live:
                        print(); live = False
                    print("  " + s)
                    tail.append(s)
                    del tail[:-12]
                    continue
                pct = m.group(2)
                now = time.time()
                # TTY 上每 0.5 秒刷一次；重定向到文件时 15 秒一行，免得刷屏
                if now - last < (0.5 if tty else 15.0) and pct != "100":
                    continue
                last = now
                line = f"  {m.group(1).strip()[:18]:<18s} {pct:>3s}%  {m.group(3)}"
                if m.group(4):
                    line += f"  [{m.group(4)}]"
                line = line[:76]
                if tty:
                    print("\r" + line.ljust(78), end="", flush=True)
                    live = True
                else:
                    print(line, flush=True)
    except KeyboardInterrupt:
        p.terminate(); p.wait()
        raise
    finally:
        p.stdout.close()
    if live:
        print()
    return p.wait(), "\n".join(tail)


SIZE_RE = re.compile(r"archive size of ([\d.]+)\s*([kMGT]?B)")
TILES_RE = re.compile(r"result tile entries (\d+)")
UNIT = {"B": 1, "kB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}


def run_extract(exe, url, out, args, dry):
    cmd = [exe, "extract", url, out] + args + (["--dry-run"] if dry else [])
    p = subprocess.run(cmd, capture_output=True, text=True)
    log = p.stdout + p.stderr
    if p.returncode != 0:
        die(f"pmtiles extract 失败：\n{log.strip()[-800:]}")
    return log


def parse_estimate(log):
    m, t = SIZE_RE.search(log), TILES_RE.search(log)
    size = float(m.group(1)) * UNIT.get(m.group(2), 1) if m else 0
    return size, int(t.group(1)) if t else 0


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.2f} {u}"
        n /= 1024


def verify(path):
    """大小对不代表内容对：传输/写入故障会留下大小正确但全零的文件。"""
    print("\n=== 校验产物 ===")
    sz = os.path.getsize(path)
    problems = []
    with open(path, "rb") as f:
        if f.read(7) != b"PMTiles":
            problems.append("文件头不是 PMTiles")
    import random
    random.seed(0)
    zero = 0
    with open(path, "rb") as f:
        for _ in range(48):
            f.seek(random.randrange(0, max(1, sz - 4096)))
            if not any(f.read(4096)):
                zero += 1
    if zero > 36:
        problems.append(f"内容抽样 48 处有 {zero} 处全零")
    print(f"  {os.path.relpath(path, HERE)}  {human(sz)}  "
          + ("✅ 完整" if not problems else "❌ 损坏"))
    for w in problems:
        print(f"    ✗ {w}")
    if not problems:
        exe = shutil.which("pmtiles")
        out = subprocess.run([exe, "show", path], capture_output=True, text=True)
        for ln in (out.stdout + out.stderr).splitlines():
            if re.match(r"\s*(tile type|min zoom|max zoom|addressed tiles)", ln):
                print("    " + ln.strip())
    return not problems


def main():
    ap = argparse.ArgumentParser(
        description="按缩放层次 / 区域下载 Protomaps 全球矢量底图",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="区域预设见 --list-regions；也可直接用 --bbox 指定任意范围")
    ap.add_argument("--minzoom", type=int, default=0, help="最小缩放级（默认 0）")
    ap.add_argument("--maxzoom", type=int, help="最大缩放级（必填，除非 --list-regions）")
    ap.add_argument("--region", choices=sorted(REGIONS), help="预设区域")
    ap.add_argument("--bbox", help="自定义范围 W,S,E,N（如 118,29.5,123,33）")
    ap.add_argument("--region-file", help="GeoJSON Polygon/MultiPolygon 文件，按其形状裁剪")
    ap.add_argument("-o", "--out", help="输出文件名（默认按区域和层次自动命名）")
    ap.add_argument("--build", help="指定 Protomaps 构建日期 YYYYMMDD")
    ap.add_argument("--threads", type=int, default=4, help="下载线程数（默认 4）")
    ap.add_argument("--estimate", action="store_true", help="只估算体积，不下载")
    ap.add_argument("-y", "--yes", action="store_true", help="跳过确认直接下载")
    ap.add_argument("--raw-progress", action="store_true",
                    help="用 pmtiles 自带的进度条（Windows 上可能刷屏闪烁）")
    ap.add_argument("--list-regions", action="store_true", help="列出预设区域")
    a = ap.parse_args()

    if a.list_regions:
        print("\n预设区域：")
        for k in sorted(REGIONS):
            bb = REGIONS[k]
            print(f"  {k:11s} {REGION_ZH[k]:6s} " + ("全球范围" if bb is None else
                  f"{bb[0]},{bb[1]},{bb[2]},{bb[3]}"))
        print("\n也可用 --bbox W,S,E,N 指定任意范围，或 --region-file 指定 GeoJSON 形状")
        return

    if a.maxzoom is None:
        ap.error("必须指定 --maxzoom（或用 --list-regions 查看区域）")
    if a.minzoom > a.maxzoom:
        die("minzoom 不能大于 maxzoom")

    exe = need_pmtiles()
    url = find_build(a.build)

    # 组装范围参数
    extra, scope = [], "global"
    if a.region_file:
        if not os.path.exists(a.region_file):
            die(f"找不到 {a.region_file}")
        extra += [f"--region={a.region_file}"]
        scope = os.path.splitext(os.path.basename(a.region_file))[0]
    elif a.bbox:
        if len(a.bbox.split(",")) != 4:
            die("--bbox 格式应为 W,S,E,N")
        extra += [f"--bbox={a.bbox}"]
        scope = "bbox"
    elif a.region and a.region != "global":
        w, s, e, n = REGIONS[a.region]
        extra += [f"--bbox={w},{s},{e},{n}"]
        scope = a.region
    extra += [f"--minzoom={a.minzoom}", f"--maxzoom={a.maxzoom}",
              f"--download-threads={a.threads}"]

    out = a.out or os.path.join(
        OUTDIR, f"{scope}-z{a.minzoom}-{a.maxzoom}.pmtiles"
        if a.minzoom else f"{scope}-z{a.maxzoom}.pmtiles")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

    label = REGION_ZH.get(scope, scope)
    print(f"\n=== 下载计划 ===")
    print(f"  来源    {url}")
    print(f"  范围    {label}" + (f"  {a.bbox}" if a.bbox else ""))
    print(f"  层次    z{a.minzoom}-{a.maxzoom}")
    print(f"  输出    {out}")
    print("\n  正在精确计算体积（--dry-run，不下载数据）…")

    size, ntiles = parse_estimate(run_extract(exe, url, out, extra, dry=True))
    print(f"  体积    {human(size)}   瓦片 {ntiles:,} 个")
    for mbps in (10, 30, 100):
        print(f"          按 {mbps:>3} MB/s 约 {size/(mbps*1e6)/60:6.1f} 分钟")

    if a.minzoom > 0:
        print(f"\n  ⚠️  minzoom={a.minzoom}：这个档案缺少低缩放层，单独用会在缩小时空白。")
        print(f"      需配一份 z0-{a.minzoom-1} 的全球档案，再用 pmtiles merge 合并：")
        print(f"      pmtiles merge base.pmtiles {os.path.basename(out)} merged.pmtiles")

    if a.estimate:
        print("\n  （--estimate 模式，未下载）")
        return

    if not a.yes:
        try:
            if input("\n  开始下载？[y/N] ").strip().lower() not in ("y", "yes"):
                print("  已取消"); return
        except (EOFError, KeyboardInterrupt):
            print("\n  已取消"); return

    print(f"\n=== 下载中 ===")
    cmd = [exe, "extract", url, out] + extra
    try:
        rc, log = run_pmtiles(cmd, a.raw_progress)
        if rc != 0:
            die(f"下载失败，重跑本命令即可（pmtiles extract 会重新抓取）\n{log}")
    except KeyboardInterrupt:
        print("\n  已中断。注意：pmtiles extract 不支持断点续传，重跑会从头开始。")
        sys.exit(1)

    if verify(out):
        print(f"\n完成。放到 Airports/tiles/ 下即可被页面自动识别，")
        print(f"或在 index.html 的 PMTILES_CANDIDATES 里加上文件名。")


if __name__ == "__main__":
    main()

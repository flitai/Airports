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
"""分级下载 Protomaps 全球矢量底图：一级一档，按需拼装。

和 fetch_tiles.py 的分工：
    fetch_tiles.py   一次下一整份（某区域 z0-N），下完就是成品
    fetch_levels.py  把每个缩放级单独存一份「零件」，以后按项目需要本地拼

好处是 z14 只用下一次。之后 A 项目要 z0-12 的轻量版、B 项目要 z0-14 的完整版，
都只是本地 merge（1 GB/s 量级），不用重新下载。

用法示例
    python3 fetch_levels.py --plan                        看各级体积表（不联网）
    python3 fetch_levels.py --levels 9-14                 下载 z9…z14，每级一个档案
    python3 fetch_levels.py --levels 0-14                 连 z0-8 底座一起下
    python3 fetch_levels.py --levels 13-14 --region china  高层次只要中国
    python3 fetch_levels.py --compose 0-12 -o tiles/planet-z0-12.pmtiles
    python3 fetch_levels.py --levels 0-12 --compose 0-12  下完顺手拼一份成品
    python3 fetch_levels.py --status                      看本地已有哪些零件

关于 z0-8：这 9 级加起来才 551 MB，拆开毫无意义，脚本一律合成单个「底座」档案。
所以 --levels 0-14 产出的是 7 个文件：底座 z0-8，加 z9…z14 各一个。

断点：pmtiles extract 本身不支持续传，所以大的层级会先切成若干空间分块分别下载，
中断后重跑只重下没完成的那一块（默认每块约 4 GB，用 --chunk-size 调）。

依赖 pmtiles CLI：
    macOS    brew install pmtiles
    Windows  https://github.com/protomaps/go-pmtiles/releases 下载解压后放进 PATH
"""
import argparse, math, os, re, shutil, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(HERE, "tiles")
LEVELDIR = os.path.join(OUTDIR, "levels")
PARTDIR = os.path.join(LEVELDIR, ".parts")

BUILD_URL = "https://build.protomaps.com/{build}.pmtiles"
DEFAULT_BUILD = "20260817"
BASE_MAX = 8            # z0..BASE_MAX 合成一个底座档案，不逐级拆
MAX_LEVEL = 15          # Protomaps planet 只到 z15

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

# 全球各级实测体积（build 20260817，pmtiles --dry-run 报的 archive size，单位字节）。
# 键 8 代表 z0-8 底座（累计），9 及以上代表单独一级。换构建会有几个百分点出入。
MEASURED = {8: 551e6, 9: 1.0e9, 10: 2.2e9, 11: 4.2e9,
            12: 9.6e9, 13: 18e9, 14: 32e9, 15: 60e9}   # z15 由 planet 全档 128 GB 反推


def die(msg):
    print(f"\n错误：{msg}", file=sys.stderr)
    sys.exit(1)


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.2f} {u}"
        n /= 1024


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
                                     headers={"User-Agent": "fetch-levels/1.0"})
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
# 层级 / 命名
# --------------------------------------------------------------------------
def parse_levels(spec):
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part.lstrip("-"):
                a, b = part.split("-", 1)
                out.update(range(int(a), int(b) + 1))
            else:
                out.add(int(part))
        except ValueError:
            die(f"看不懂的层级写法：{part}（应形如 9-14 或 9,11,13）")
    bad = [z for z in out if not 0 <= z <= MAX_LEVEL]
    if bad:
        die(f"层级超出范围 0-{MAX_LEVEL}：{sorted(bad)}")
    if not out:
        die("--levels 不能为空")
    # z0..BASE_MAX 统一折叠成底座
    return sorted({BASE_MAX if z <= BASE_MAX else z for z in out})


def level_file(scope, z):
    return os.path.join(LEVELDIR, f"{scope}-z0-{BASE_MAX}.pmtiles" if z <= BASE_MAX
                        else f"{scope}-z{z}.pmtiles")


def level_label(z):
    return f"z0-{BASE_MAX} 底座" if z <= BASE_MAX else f"z{z}"


def level_zoom_args(z):
    return (0, BASE_MAX) if z <= BASE_MAX else (z, z)


# --------------------------------------------------------------------------
# 瓦片坐标 / 分块
# --------------------------------------------------------------------------
def lon2x(lon, z):
    return min(2 ** z - 1, max(0, int((lon + 180.0) / 360.0 * 2 ** z)))


def lat2y(lat, z):
    lat = max(-85.0511, min(85.0511, lat))
    r = math.radians(lat)
    y = (1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * 2 ** z
    return min(2 ** z - 1, max(0, int(y)))


def x2lon(x, z):
    return x / 2 ** z * 360.0 - 180.0


def y2lat(y, z):
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / 2 ** z))))


def cell_bbox(z, x0, x1, y0, y1):
    """用「首末瓦片的中心点」当 bbox 边界。

    直接拿瓦片边界当 bbox 会让相邻两块都包含边界那一列瓦片，
    pmtiles merge 会因为 overlapping tiles 直接拒绝合并（实测如此）。
    取中心点后每块严格覆盖 x0..x1 / y0..y1，块间无交集。"""
    return "%.9f,%.9f,%.9f,%.9f" % (x2lon(x0 + 0.5, z), y2lat(y1 + 0.5, z),
                                    x2lon(x1 + 0.5, z), y2lat(y0 + 0.5, z))


def split_range(a, b, n):
    """把整数区间 [a,b] 尽量均匀切成 n 段"""
    total = b - a + 1
    n = max(1, min(n, total))
    out, s = [], a
    for i in range(n):
        cnt = total // n + (1 if i < total % n else 0)
        out.append((s, s + cnt - 1))
        s += cnt
    return out


def plan_grid(size, chunk_bytes, nx, ny):
    """按体积决定切几块。行数取 2/4 是因为陆地集中在北半球，
    只按经度切会让欧亚那一条特别大。"""
    if size <= chunk_bytes:
        return 1, 1
    cells = math.ceil(size / chunk_bytes)
    rows = 4 if cells >= 16 else 2 if cells >= 4 else 1
    return min(nx, math.ceil(cells / rows)), min(ny, rows)


# --------------------------------------------------------------------------
# pmtiles 调用
# --------------------------------------------------------------------------
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


def extract_args(zmin, zmax, bbox, threads, overfetch):
    a = [f"--minzoom={zmin}", f"--maxzoom={zmax}", f"--download-threads={threads}"]
    if bbox:
        a.append(f"--bbox={bbox}")
    if overfetch is not None:
        a.append(f"--overfetch={overfetch}")
    return a


def dry_run(exe, url, args):
    """只算不下。返回 (字节数, 瓦片条目数)。"""
    p = subprocess.run([exe, "extract", url, os.devnull] + args + ["--dry-run"],
                       capture_output=True, text=True)
    log = p.stdout + p.stderr
    if p.returncode != 0:
        die(f"pmtiles extract --dry-run 失败：\n{log.strip()[-800:]}")
    m, t = SIZE_RE.search(log), TILES_RE.search(log)
    return (float(m.group(1)) * UNIT.get(m.group(2), 1) if m else 0,
            int(t.group(1)) if t else 0)


def sane(path):
    """大小对不代表内容对：传输/写入故障会留下大小正确但全零的文件。"""
    try:
        if os.path.getsize(path) < 128:
            return False
        with open(path, "rb") as f:
            if f.read(7) != b"PMTiles":
                return False
    except OSError:
        return False
    return True


def run_extract(exe, url, out, args, raw=False):
    """真下。写临时文件再改名，避免中断留下半截文件被当成已完成。"""
    tmp = out + ".tmp"
    for p in (tmp, out + ".part"):
        if os.path.exists(p):
            os.remove(p)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    rc, _ = run_pmtiles([exe, "extract", url, tmp] + args, raw)
    if rc != 0 or not sane(tmp):
        if os.path.exists(tmp):
            os.remove(tmp)
        return False
    os.replace(tmp, out)
    return True


def merge(exe, inputs, out, raw=False):
    if len(inputs) == 1:
        shutil.copyfile(inputs[0], out + ".tmp")
    else:
        tmp = out + ".tmp"
        if os.path.exists(tmp):
            os.remove(tmp)
        rc, log = run_pmtiles([exe, "merge"] + inputs + [tmp], raw)
        if rc != 0 or not sane(tmp):
            if os.path.exists(tmp):
                os.remove(tmp)
            die(f"pmtiles merge 失败：\n{log.strip()[-800:]}")
    os.replace(out + ".tmp", out)


def describe(exe, path):
    out = subprocess.run([exe, "show", path], capture_output=True, text=True)
    info = {}
    for ln in (out.stdout + out.stderr).splitlines():
        m = re.match(r"\s*(min zoom|max zoom|addressed tiles count):\s*(.+)", ln)
        if m:
            info[m.group(1)] = m.group(2).strip()
    return info


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def resolve_scope(a):
    """返回 (scope 名, bbox 字符串或 None, (w,s,e,n) 或 None)"""
    if a.region_file:
        if not os.path.exists(a.region_file):
            die(f"找不到 {a.region_file}")
        die("--region-file 用不了分块下载（形状裁剪无法按块拆分），请改用 fetch_tiles.py")
    if a.bbox:
        parts = a.bbox.split(",")
        if len(parts) != 4:
            die("--bbox 格式应为 W,S,E,N")
        try:
            w, s, e, n = map(float, parts)
        except ValueError:
            die("--bbox 四个值必须是数字")
        if w >= e or s >= n:
            die("--bbox 应满足 W<E 且 S<N（不支持跨 180° 经线，请拆成两次）")
        return (a.scope_name or "bbox"), a.bbox, (w, s, e, n)
    if a.region and a.region != "global":
        w, s, e, n = REGIONS[a.region]
        return a.region, f"{w},{s},{e},{n}", (w, s, e, n)
    return "global", None, None


def tile_range(box, z):
    if box is None:
        return 0, 2 ** z - 1, 0, 2 ** z - 1
    w, s, e, n = box
    return lon2x(w, z), lon2x(e, z), lat2y(n, z), lat2y(s, z)


def print_plan(scope, box, levels, exe=None, url=None, threads=4):
    """全球用实测表，秒出；区域必须联网实算。"""
    print(f"\n=== 体积表 · {REGION_ZH.get(scope, scope)} ===")
    rows, total = [], 0
    for z in levels:
        if box is None and exe is None:
            size = MEASURED.get(z, 0)
            mark = "≈"
        else:
            zmin, zmax = level_zoom_args(z)
            size, _ = dry_run(exe, url, extract_args(
                zmin, zmax, cell_bbox(z, *tile_range(box, z)) if box else None,
                threads, None))
            mark = " "
        total += size
        rows.append((z, size, mark))
    for z, size, mark in rows:
        print(f"  {level_label(z):<12s} {mark}{human(size):>10s}")
    print(f"  {'合计':<12s}  {human(total):>10s}"
          + (f"   （按 30 MB/s 约 {total/30e6/3600:.1f} 小时）" if total else ""))
    if box is None and exe is None:
        print(f"\n  ≈ 为 build {DEFAULT_BUILD} 的实测值，换构建有几个百分点出入；"
              f"加 --exact 联网实算")
    return total


def download_level(exe, url, z, scope, box, a):
    dest = level_file(scope, z)
    if os.path.exists(dest) and sane(dest):
        print(f"\n=== {level_label(z)} ===  已有 {human(os.path.getsize(dest))}，跳过")
        return dest
    zmin, zmax = level_zoom_args(z)
    gz = zmax                                   # 分块用该级自身的瓦片网格
    x0, x1, y0, y1 = tile_range(box, gz)
    full = cell_bbox(gz, x0, x1, y0, y1) if box else None

    print(f"\n=== {level_label(z)} · {REGION_ZH.get(scope, scope)} ===")
    print("  正在精确计算体积（--dry-run，不下载数据）…")
    size, ntiles = dry_run(exe, url, extract_args(zmin, zmax, full, a.threads, a.overfetch))
    cols, rows = plan_grid(size, a.chunk_size * 1e9, x1 - x0 + 1, y1 - y0 + 1)
    nchunk = cols * rows
    print(f"  体积 {human(size)}   瓦片条目 {ntiles:,}   切 {cols}×{rows}={nchunk} 块")

    need = size * (2 if nchunk > 1 else 1)      # 合并阶段要同时放下分块和成品
    free = shutil.disk_usage(OUTDIR if os.path.isdir(OUTDIR) else HERE).free
    if free < need * 1.05:
        die(f"磁盘空间不够：需要约 {human(need)}"
            + ("（分块 + 合并成品，合并完会删掉分块）" if nchunk > 1 else "")
            + f"，可用 {human(free)}")

    if not a.yes:
        try:
            if input(f"  下载 {level_label(z)}？[y/N] ").strip().lower() not in ("y", "yes"):
                print("  已跳过")
                return None
        except (EOFError, KeyboardInterrupt):
            print("\n  已取消")
            sys.exit(1)

    os.makedirs(PARTDIR, exist_ok=True)
    parts, cells = [], [(cx, cy) for cy in split_range(y0, y1, rows)
                        for cx in split_range(x0, x1, cols)]
    for i, ((cx0, cx1), (cy0, cy1)) in enumerate(cells, 1):
        tag = f"{scope}-z{z}" if z > BASE_MAX else f"{scope}-z0-{BASE_MAX}"
        p = os.path.join(PARTDIR, f"{tag}-{i:02d}of{nchunk:02d}.pmtiles")
        parts.append(p)
        if os.path.exists(p) and sane(p):
            print(f"  [{i}/{nchunk}] 已有 {human(os.path.getsize(p))}，跳过")
            continue
        bbox = cell_bbox(gz, cx0, cx1, cy0, cy1) if nchunk > 1 or box else None
        print(f"  [{i}/{nchunk}] x{cx0}-{cx1} y{cy0}-{cy1}"
              + (f"  bbox {bbox}" if bbox else "  全球"))
        try:
            ok = run_extract(exe, url, p, extract_args(zmin, zmax, bbox,
                                                       a.threads, a.overfetch),
                             a.raw_progress)
        except KeyboardInterrupt:
            print("\n  已中断。重跑同一条命令即可，完成的块不会重下。")
            sys.exit(1)
        if not ok:
            die(f"第 {i} 块下载失败。重跑同一条命令即可，完成的块不会重下。")

    print(f"  合并 {nchunk} 块 → {os.path.relpath(dest, HERE)}")
    merge(exe, parts, dest, a.raw_progress)
    if not a.keep_parts:
        for p in parts:
            os.remove(p)
    print(f"  完成 {human(os.path.getsize(dest))}")
    return dest


def compose(exe, scope, levels, out, raw=False):
    files, missing = [], []
    for z in levels:
        p = level_file(scope, z)
        (files if os.path.exists(p) and sane(p) else missing).append(p)
    if missing:
        die("缺少这些层级档案，先下载：\n  "
            + "\n  ".join(os.path.relpath(m, HERE) for m in missing)
            + f"\n\n  python3 fetch_levels.py --levels {levels[0]}-{levels[-1]}"
            + (f" --region {scope}" if scope != "global" else ""))
    lo, hi = (0 if levels[0] <= BASE_MAX else levels[0]), levels[-1]
    out = out or os.path.join(OUTDIR, f"{scope}-z{lo}-{hi}.pmtiles")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    total = sum(os.path.getsize(f) for f in files)
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(out))).free
    if free < total * 1.05:
        die(f"磁盘空间不够：成品约 {human(total)}，可用 {human(free)}")
    print(f"\n=== 拼装 z{lo}-{hi} ===")
    for f in files:
        print(f"  + {os.path.relpath(f, HERE):<40s} {human(os.path.getsize(f)):>10s}")
    merge(exe, files, out, raw)
    info = describe(exe, out)
    print(f"  → {os.path.relpath(out, HERE)}  {human(os.path.getsize(out))}"
          f"  z{info.get('min zoom','?')}-{info.get('max zoom','?')}"
          f"  {info.get('addressed tiles count','?')} 个瓦片")
    print(f"\n放在 tiles/ 下的成品，在 index.html 的 PMTILES_CANDIDATES 里加上文件名即可。")
    return out


def status(exe):
    print(f"\n=== 本地零件 · {os.path.relpath(LEVELDIR, HERE)} ===")
    if not os.path.isdir(LEVELDIR):
        print("  （空）还没下载过任何层级")
        return
    def zkey(f):                       # 按层级排序，不然 z10 会排在 z9 前面
        m = re.search(r"-z(\d+)", f)
        return (int(m.group(1)) if m else 99, f)
    files = sorted((f for f in os.listdir(LEVELDIR) if f.endswith(".pmtiles")), key=zkey)
    if not files:
        print("  （空）还没下载过任何层级")
    total = 0
    for f in files:
        p = os.path.join(LEVELDIR, f)
        sz = os.path.getsize(p)
        total += sz
        info = describe(exe, p) if sane(p) else {}
        print(f"  {f:<32s} {human(sz):>10s}  "
              + (f"z{info.get('min zoom','?')}-{info.get('max zoom','?')}  "
                 f"{info.get('addressed tiles count','?')} 个瓦片" if info else "❌ 损坏，删掉重下"))
    if files:
        print(f"  {'合计':<32s} {human(total):>10s}")

    if os.path.isdir(PARTDIR):
        ps = [f for f in os.listdir(PARTDIR) if f.endswith(".pmtiles")]
        if ps:
            sz = sum(os.path.getsize(os.path.join(PARTDIR, f)) for f in ps)
            print(f"\n  未合并的分块 {len(ps)} 个，{human(sz)}"
                  f"（重跑对应的 --levels 会接着下）")

    done = sorted(os.path.join(OUTDIR, f) for f in os.listdir(OUTDIR)
                  if f.endswith(".pmtiles")) if os.path.isdir(OUTDIR) else []
    if done:
        print(f"\n=== 成品 · {os.path.relpath(OUTDIR, HERE)} ===")
        for p in done:
            info = describe(exe, p) if sane(p) else {}
            print(f"  {os.path.basename(p):<32s} {human(os.path.getsize(p)):>10s}  "
                  + (f"z{info.get('min zoom','?')}-{info.get('max zoom','?')}" if info
                     else "❌ 损坏"))


def main():
    ap = argparse.ArgumentParser(
        description="分级下载 Protomaps 全球矢量底图，一级一档、按需拼装",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="典型流程：--plan 看体积 → --levels 9-14 下零件 → --compose 0-12 拼成品")
    ap.add_argument("--levels", help="要下载的层级，如 9-14 或 9,11,13（0-8 折叠为一个底座档案）")
    ap.add_argument("--compose", help="把已有层级拼成一个成品，如 0-12")
    ap.add_argument("--plan", action="store_true", help="只看体积表，不下载")
    ap.add_argument("--exact", action="store_true", help="--plan 时联网实算而非查实测表")
    ap.add_argument("--status", action="store_true", help="列出本地已有的零件和成品")
    ap.add_argument("--list-regions", action="store_true", help="列出预设区域")
    ap.add_argument("--region", choices=sorted(REGIONS), help="预设区域（默认全球）")
    ap.add_argument("--bbox", help="自定义范围 W,S,E,N（如 118,29.5,123,33）")
    ap.add_argument("--region-file", help="（分块下载不支持，请用 fetch_tiles.py）")
    ap.add_argument("--scope-name", help="给 --bbox 起个名字，用于文件命名（默认 bbox）")
    ap.add_argument("-o", "--out", help="--compose 的输出文件名")
    ap.add_argument("--build", help="指定 Protomaps 构建日期 YYYYMMDD")
    ap.add_argument("--threads", type=int, default=4, help="下载线程数（默认 4）")
    ap.add_argument("--overfetch", type=float, help="多抓比例，默认 0.05；调大减少请求数")
    ap.add_argument("--chunk-size", type=float, default=4.0,
                    help="每个分块的目标体积 GB（默认 4）。只是目标不是保证——"
                         "海洋块小、欧亚块大，实际能差好几倍。调小=断线损失小、请求多")
    ap.add_argument("--keep-parts", action="store_true", help="合并后保留分块文件")
    ap.add_argument("--raw-progress", action="store_true",
                    help="用 pmtiles 自带的进度条（Windows 上可能刷屏闪烁）")
    ap.add_argument("-y", "--yes", action="store_true", help="跳过逐级确认")
    a = ap.parse_args()

    if a.list_regions:
        print("\n预设区域：")
        for k in sorted(REGIONS):
            bb = REGIONS[k]
            print(f"  {k:11s} {REGION_ZH[k]:6s} "
                  + ("全球范围" if bb is None else f"{bb[0]},{bb[1]},{bb[2]},{bb[3]}"))
        print("\n也可用 --bbox W,S,E,N 指定任意范围（配 --scope-name 命名）")
        return

    exe = need_pmtiles()
    if a.status:
        status(exe)
        return
    if not (a.levels or a.compose or a.plan):
        ap.print_help()
        return

    scope, _, box = resolve_scope(a)
    os.makedirs(LEVELDIR, exist_ok=True)

    if a.plan:
        levels = parse_levels(a.levels) if a.levels else list(range(BASE_MAX, MAX_LEVEL))
        if box is not None or a.exact:
            print_plan(scope, box, levels, exe, find_build(a.build), a.threads)
        else:
            print_plan(scope, box, levels)
        return

    if a.levels:
        levels = parse_levels(a.levels)
        url = find_build(a.build)
        print(f"\n来源  {url}")
        print(f"范围  {REGION_ZH.get(scope, scope)}"
              + (f"  {a.bbox}" if a.bbox else ""))
        print(f"层级  " + "、".join(level_label(z) for z in levels))
        print(f"零件  {os.path.relpath(LEVELDIR, HERE)}/")
        for z in levels:
            download_level(exe, url, z, scope, box, a)

    if a.compose:
        compose(exe, scope, parse_levels(a.compose), a.out, a.raw_progress)
    elif a.levels:
        lv = parse_levels(a.levels)
        lo = 0 if lv[0] <= BASE_MAX else lv[0]
        print(f"\n零件已就位。拼成品：")
        print(f"    python3 fetch_levels.py --compose {lo}-{lv[-1]}"
              + (f" --region {scope}" if scope != "global" else ""))


if __name__ == "__main__":
    main()

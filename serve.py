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
"""支持 HTTP Range 的静态服务器。

为什么不能用 `python3 -m http.server`：它完全忽略 Range 请求头，对
`Range: bytes=100-199` 会返回 200 和整个文件。PMTiles 靠 Range 从单个大文件里
读取指定字节区间，用 http.server 服务 128 GB 的 planet.pmtiles 会导致每取一个
瓦片都尝试传输整个文件。

这个服务器一个进程同时服务：
    index.html / data/*.js / vendor/*     普通静态文件
    tiles/planet.pmtiles                  Range 请求（浏览器端 pmtiles.js 消费）
    tiles/dem/{z}/{x}/{y}.png             松散 DEM 瓦片

启动时检查 FAA 航图索引是否过期，过期就在后台重跑 build_faa.py（见 check_faa）。

用法：
    python3 serve.py [端口]        默认 8000
    python3 serve.py --no-update   跳过航图过期检查，直接起服务
"""
import os, re, sys, mimetypes, socketserver
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")

mimetypes.add_type("application/octet-stream", ".pmtiles")
mimetypes.add_type("application/vnd.mapbox-vector-tile", ".mvt")
mimetypes.add_type("application/javascript", ".js")


class RangeHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"          # Range + keep-alive 都需要 HTTP/1.1

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *args):
        if "--verbose" in sys.argv:
            super().log_message(fmt, *args)

    def end_headers(self):
        # 声明支持 Range，浏览器/pmtiles.js 会据此发字节区间请求
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_head(self):
        rng = self.headers.get("Range")
        if not rng:
            return super().send_head()

        path = self.translate_path(self.path)
        if os.path.isdir(path) or not os.path.exists(path):
            return super().send_head()

        m = RANGE_RE.match(rng.strip())
        if not m:
            self.send_error(400, "Malformed Range header")
            return None

        size = os.path.getsize(path)
        first, last = m.group(1), m.group(2)
        if first == "":                     # bytes=-N 取末尾 N 字节
            if last == "":
                self.send_error(400, "Malformed Range header")
                return None
            length = min(int(last), size)
            start = size - length
            end = size - 1
        else:
            start = int(first)
            end = int(last) if last else size - 1
            end = min(end, size - 1)

        if start >= size or start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None

        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        ctype = self.guess_type(path)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        # copyfile 会读到 EOF，这里只允许读区间内的字节
        return _Slice(f, end - start + 1)


class _Slice:
    """把文件对象限制在 N 字节内，供 SimpleHTTPRequestHandler.copyfile 使用。"""
    def __init__(self, f, remaining):
        self.f, self.remaining = f, remaining

    def read(self, n=-1):
        if self.remaining <= 0:
            return b""
        if n < 0 or n > self.remaining:
            n = self.remaining
        b = self.f.read(n)
        self.remaining -= len(b)
        return b

    def close(self):
        self.f.close()


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def dem_levels():
    """DEM 只报「有哪几个缩放级」，不算体积。

    算体积要把 87,381 个瓦片全 stat 一遍，实测 160 ms，换来的只是一行 MB 数；
    而这行输出要回答的问题只是「数据在不在」。瓦片放在会休眠的外置盘上时，
    这一遍遍历还会强制唤醒盘片，代价更不成比例。
    要精确校验完整性用 download_offline_maps.py --check。
    """
    try:
        zs = sorted(int(d) for d in os.listdir(os.path.join(ROOT, "tiles", "dem"))
                    if d.isdigit())
    except OSError:                         # 目录不存在或读不了
        return None
    if not zs:
        return None
    if zs == list(range(zs[0], zs[-1] + 1)):
        return f"z{zs[0]}-{zs[-1]} 就位"
    return "z" + ",".join(map(str, zs)) + " 就位"   # 有缺级就逐个列出


def status():
    def size(p):
        p = os.path.join(ROOT, p)
        if os.path.isfile(p):
            return os.path.getsize(p)
        if os.path.isdir(p):
            return sum(os.path.getsize(os.path.join(dp, f))
                       for dp, _, fs in os.walk(p) for f in fs)
        return None

    def mb(n):
        return f"{n/1048576:8.1f} MB" if n else "  未就位"

    rows = [("planet.pmtiles", mb(size("tiles/planet.pmtiles"))),
            ("测试底图 (长三角)", mb(size("tiles/test-yrd.pmtiles"))),
            ("DEM 瓦片", dem_levels() or "  未就位"),
            ("机场数据", mb(size("data")))]
    print("  数据就位情况:")
    for label, val in rows:
        print(f"    {label:22s} {val}")


FAA_JS = os.path.join(ROOT, "data", "faa.js")
FAA_RE = re.compile(r'"cycle":"(\d+)","from":"([\d-]+)","to":"([\d-]+)"')
UPDATE_TIMEOUT = 600            # 只是防挂死的兜底，后台跑不占启动时间


def faa_cycle():
    """从 data/faa.js 读出 (周期号, 生效日, 截止日)。整个文件 750 KB，但这三个
    字段就在最前面，读 200 字节足够——启动路径上不值得解析全文。"""
    try:
        with open(FAA_JS, encoding="utf-8") as f:
            m = FAA_RE.search(f.read(200))
    except OSError:
        return None
    return m.groups() if m else None


def _run_build_faa(old_cycle):
    """后台跑 build_faa.py。任何失败都只警告，不影响已经起来的服务器。"""
    import subprocess
    try:
        rc = subprocess.run([sys.executable, os.path.join(ROOT, "build_faa.py")],
                            cwd=ROOT, timeout=UPDATE_TIMEOUT,
                            capture_output=True, text=True).returncode
    except subprocess.TimeoutExpired:
        print(f"\n  ⚠ 航图更新超时（>{UPDATE_TIMEOUT}s），仍在用周期 {old_cycle}")
        return
    except OSError as e:
        print(f"\n  ⚠ 起不了 build_faa.py：{e}")
        return

    if rc != 0:
        print(f"\n  ⚠ 航图更新失败（多半是没联网），仍在用周期 {old_cycle}。"
              "\n    联网后重跑 python3 build_faa.py 即可\n")
        return
    new = faa_cycle()
    if new and new[0] != old_cycle:
        print(f"\n  ✓ 航图已更新到周期 {new[0]}，有效期至 {new[2]}（刷新页面生效）\n")
    else:
        print("\n  ✓ 航图索引已刷新\n")


def check_faa():
    """检查 FAA 航图索引是否过期，过期就在后台自动重跑 build_faa.py。

    to 是下一周期的生效日（from + 28 天），所以 today >= to 就已经过期了。

    为什么放后台：build_faa.py 要下 16 MB 元数据，实测约 150 秒，卡在启动路径
    上没法接受。build_faa.py 的输出是先写 .tmp 再 os.replace，替换是原子的，
    所以后台改文件不会让正在加载 faa.js 的页面读到半截内容。

    任何一步失败都只警告、不阻断：离线可用是这个项目的底线，没网时照样得起得来
    服务。航图本来就需要联网才能看，索引旧一点只是可能 404，页面自己会给出提示。
    """
    cur = faa_cycle()
    if cur is None:
        if os.path.isfile(FAA_JS):
            print("  FAA 航图  data/faa.js 格式异常，重跑 build_faa.py 可修复")
        else:
            print("  FAA 航图  未生成（可选功能，跑 build_faa.py 启用）")
        return

    cycle, _frm, to = cur
    if datetime.now(timezone.utc).date().isoformat() < to:
        print(f"  FAA 航图  周期 {cycle}，有效期至 {to}")
        return

    print(f"  FAA 航图  周期 {cycle} 已于 {to} 过期，正在后台更新（约 2 分钟）…")
    import threading
    threading.Thread(target=_run_build_faa, args=(cycle,), daemon=True).start()


if __name__ == "__main__":
    port = 8000
    for a in sys.argv[1:]:
        if a.isdigit():
            port = int(a)
    status()
    if "--no-update" not in sys.argv:
        check_faa()
    httpd = None
    for p in range(port, port + 20):        # 端口被占用就顺延，免得启动器失败
        try:
            httpd = Server(("127.0.0.1", p), RangeHandler); port = p; break
        except OSError:
            continue
    if not httpd:
        print(f"  {port}-{port+19} 端口都被占用", file=sys.stderr); sys.exit(1)
    print(f"\n  服务目录 {ROOT}")
    print(f"  打开 http://localhost:{port}/\n  Ctrl-C 停止\n")
    if "--open" in sys.argv:
        import webbrowser, threading
        threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{port}/")).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()

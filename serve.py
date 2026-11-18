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
    python3 serve.py --sdr         同时拉起本地接收机守护进程（需要 RTL-SDR，见 README）
"""
import os, re, sys, json, time, shutil, signal, threading, mimetypes, socketserver
import urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")

mimetypes.add_type("application/octet-stream", ".pmtiles")
mimetypes.add_type("application/vnd.mapbox-vector-tile", ".mvt")
mimetypes.add_type("application/javascript", ".js")


# ---------- 实时航班代理 ----------
# 为什么要代理：adsb.fi / adsb.lol 都不发 Access-Control-Allow-Origin，浏览器
# 直连一律被同源策略拦下（file:// 的 null 源和 http://localhost 都试过）。
# 所以只有服务器模式能用这个功能，双击 index.html 时前端会自己隐藏入口。
FLIGHT_SOURCES = [
    ("adsb.fi",  "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{dist}", "aircraft"),
    ("adsb.lol", "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{dist}",         "ac"),
]
FLIGHT_TTL = 5.0                # 同一视口 5 秒内复用缓存，别拿免费社区服务当压测靶子
FLIGHT_MAX_DIST = 250           # nm；250 nm 已经是 1400 架 / 800 KB
_flight_lock = threading.Lock()
_flight_cache = {}              # key -> (取回时刻, 已编码的响应体)


FLIGHT_MAX_TILES = 4            # 视口大到一片盖不住就分片，但最多 4 片：这是别人白送的服务


def tile_bbox(w, sth, e, n):
    """把视口切成若干个半径 <=250 nm 的查询圆。返回 (圆心列表, 是否全覆盖)。

    上游对 dist 的硬上限就是 250 nm（实测 500 直接 400），所以视野一大就只能
    分片查。但分片数要封顶：对着免费社区服务扇出几十个请求是不礼貌的，超了
    宁可只覆盖中心区域，并如实告诉前端 covered=False。
    """
    import math
    clat = (sth + n) / 2.0
    # 一个半径 R 的圆能内接边长 R*sqrt(2) 的正方形，按这个步长铺
    step = FLIGHT_MAX_DIST * 1.414
    h_nm = (n - sth) * 60.0
    w_nm = (e - w) * 60.0 * max(0.05, math.cos(math.radians(clat)))
    nx = max(1, math.ceil(w_nm / step))
    ny = max(1, math.ceil(h_nm / step))
    if nx * ny > FLIGHT_MAX_TILES:              # 盖不全就只取中心一片
        return [((sth + n) / 2.0, (w + e) / 2.0, FLIGHT_MAX_DIST)], False
    pts = []
    for iy in range(ny):
        for ix in range(nx):
            pts.append((sth + (n - sth) * (iy + 0.5) / ny,
                        w + (e - w) * (ix + 0.5) / nx,
                        FLIGHT_MAX_DIST))
    return pts, True


def fetch_flights(lat, lon, dist):
    """依次试各数据源，返回瘦身后的 JSON。整条链路失败就抛异常给调用方。

    上游一条记录有 30+ 字段，1400 架就是 800 KB。页面只用得上 10 个字段，
    压成定长数组后体积降一个量级——和 data/*.js 用定长数组是同一个理由。

    第 10 个字段是「这个位置是什么时候测到的」（Unix 秒）。页面要拿它在多个
    来源之间挑最新的位置：本地接收机每秒一报，联网数据 10 秒一取还带缓存，
    没有这个时刻就分不清谁新谁旧，来源一切换飞机就会往回跳。
    """
    last = None
    for name, tpl, key in FLIGHT_SOURCES:
        url = tpl.format(lat=f"{lat:.3f}", lon=f"{lon:.3f}", dist=dist)
        req = urllib.request.Request(url, headers={
            "User-Agent": "airports-map/1.0 (+https://github.com/flitai/Airports)"})
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                raw = json.loads(r.read())
        except Exception as e:                  # 网络/解析/HTTP 全算失败，换下一家
            last = f"{name}: {e}"
            continue
        # 位置时刻 = 上游的 now 减去该机的 seen_pos。两家的 now 单位不一样：
        # 实测 adsb.fi 给秒（1794983108.001），adsb.lol 给毫秒（1794983111002）。
        # 用上游的钟而不是本机的钟，是因为 seen_pos 是相对上游 now 计的。
        try:
            now = float(raw.get("now"))
            if now > 1e11:
                now /= 1000.0
        except (TypeError, ValueError):
            now = time.time()
        out = []
        for a in raw.get(key) or []:
            la, lo = a.get("lat"), a.get("lon")
            if la is None or lo is None:        # 只有 MLAT/TIS-B 无位置的记录
                continue
            alt = a.get("alt_baro")
            try:
                age = float(a.get("seen_pos") or 0)
            except (TypeError, ValueError):
                age = 0.0
            out.append([
                a.get("hex", ""),
                (a.get("flight") or "").strip(),
                a.get("r") or "",
                a.get("t") or "",
                round(la, 5), round(lo, 5),
                None if alt in ("ground", None) else alt,
                a.get("gs"), a.get("track"),
                round(now - age, 1),
            ])
        return {"src": name, "t": int(time.time()), "ac": out}
    raise RuntimeError(last or "no source")


def flights_response(qs):
    """解析查询串 → 取数（带缓存）→ 返回 (状态码, 响应体)。

    支持两种入参：bbox=w,s,e,n（视口，需要时自动分片）或 lat/lon/dist（单个圆）。
    """
    try:
        q = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        if "bbox" in q:
            w, sth, e, n = [float(x) for x in q["bbox"].split(",")]
            if not (-90 <= sth < n <= 90) or not (-180 <= w <= 180 and -180 <= e <= 180):
                return 400, json.dumps({"error": "bad bbox"}).encode()
            if e < w:                            # 视口跨了 180 度经线，退回中心单片
                w, e = -180.0, 180.0
            spots, covered = tile_bbox(w, sth, e, n)
        else:
            lat, lon = float(q["lat"]), float(q["lon"])
            dist = max(1, min(FLIGHT_MAX_DIST, int(float(q.get("dist", 100)))))
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                return 400, json.dumps({"error": "lat/lon out of range"}).encode()
            spots, covered = [(lat, lon, dist)], True
    except (KeyError, ValueError):
        return 400, json.dumps({"error": "need bbox=w,s,e,n or lat,lon[,dist]"}).encode()

    # 缓存键按 0.1° 取整：地图微动不该触发新的上游请求
    key = (tuple((round(a, 1), round(b, 1), c) for a, b, c in spots), covered)
    now = time.time()
    with _flight_lock:
        hit = _flight_cache.get(key)
        if hit and now - hit[0] < FLIGHT_TTL:
            return 200, hit[1]
    try:
        seen, out, src = set(), [], None
        for la, lo, di in spots:
            r = fetch_flights(la, lo, di)
            src = src or r["src"]
            for a in r["ac"]:                    # 相邻分片会重叠，按 ICAO24 去重
                if a[0] in seen:
                    continue
                seen.add(a[0]); out.append(a)
        body = json.dumps({"src": src, "t": int(time.time()), "ac": out,
                           "covered": covered, "tiles": len(spots)},
                          separators=(",", ":")).encode()
    except Exception as e:
        return 502, json.dumps({"error": str(e)[:200]}).encode()
    with _flight_lock:
        _flight_cache[key] = (now, body)
        if len(_flight_cache) > 64:             # 漫游久了别让字典无限涨
            for k in sorted(_flight_cache, key=lambda k: _flight_cache[k][0])[:32]:
                del _flight_cache[k]
    return 200, body


# ---------- 航线查询 ----------
# 注意这里查到的是「该呼号历史上飞过的航线」，不是本架次提交的飞行计划。
# ADS-B 报文根本不含起降机场，只能靠呼号去第三方库查表，而同一呼号不同日期
# 会飞不同城市对——实测 adsbdb 与 hexdb 对 RYR8FY 就给出两个不同答案。
# 所以前端拿到后要用飞机当前位置做偏航校验，别把它当权威航路展示。
ROUTE_TTL = 3600.0              # 航线近乎静态，缓存久一点；查不到的也缓存，免得通航机反复打上游
_route_lock = threading.Lock()
_route_cache = {}


def _get_json(url, timeout=10):
    req = urllib.request.Request(url, headers={
        "User-Agent": "airports-map/1.0 (+https://github.com/flitai/Airports)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def lookup_route(cs):
    """呼号 → {from,to}。adsbdb 为主（带机场名和坐标），hexdb 兜底（只给 ICAO 对）。"""
    try:
        d = _get_json("https://api.adsbdb.com/v0/callsign/" + cs)
        fr = (d.get("response") or {}).get("flightroute") or {}
        o, t = fr.get("origin"), fr.get("destination")
        if o and t:
            def one(a):
                return {"icao": a.get("icao_code") or "", "iata": a.get("iata_code") or "",
                        "name": a.get("name") or "", "lat": a.get("latitude"),
                        "lon": a.get("longitude")}
            return {"cs": cs, "src": "adsbdb",
                    "airline": (fr.get("airline") or {}).get("name") or "",
                    "from": one(o), "to": one(t)}
    except Exception:
        pass
    try:                                    # hexdb 只返回 "LGIR-EBCI" 这样的字符串
        d = _get_json("https://hexdb.io/api/v1/route/icao/" + cs)
        parts = (d.get("route") or "").split("-")
        if len(parts) == 2 and all(len(x) == 4 for x in parts):
            return {"cs": cs, "src": "hexdb", "airline": "",
                    "from": {"icao": parts[0], "iata": "", "name": "", "lat": None, "lon": None},
                    "to":   {"icao": parts[1], "iata": "", "name": "", "lat": None, "lon": None}}
    except Exception:
        pass
    return {"cs": cs, "none": True}


def route_response(qs):
    try:
        q = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        cs = re.sub(r"[^A-Z0-9]", "", urllib.parse.unquote(q["cs"]).upper())[:12]
    except (KeyError, ValueError):
        return 400, json.dumps({"error": "need cs"}).encode()
    if not cs:
        return 400, json.dumps({"error": "empty callsign"}).encode()

    now = time.time()
    with _route_lock:
        hit = _route_cache.get(cs)
        if hit and now - hit[0] < ROUTE_TTL:
            return 200, hit[1]
    try:
        body = json.dumps(lookup_route(cs), separators=(",", ":")).encode()
    except Exception as e:
        return 502, json.dumps({"error": str(e)[:200]}).encode()
    with _route_lock:
        _route_cache[cs] = (now, body)
        if len(_route_cache) > 512:
            for k in sorted(_route_cache, key=lambda k: _route_cache[k][0])[:256]:
                del _route_cache[k]
    return 200, body


# ---------- 本地接收机（可选）----------
# 接收机守护进程 sdr/sdrd 是另一个进程：它要用 numpy 和 WebSocket，而这个文件
# 坚持只用标准库——不用接收机的人不该为它装任何东西。这里只管三件事：
# 把它拉起来、告诉页面它在哪个端口、自己退出时把它带走。
#
# 「带走」靠的是一根管道而不是信号：守护进程的标准输入连着我们，我们一死
# （哪怕是 kill -9，来不及发任何信号）管道就断，它读到文件结束便自行退出，
# 再用同样的办法带走它的解码子进程。整条链上没有谁会变成占着 USB 的孤儿。
SDR_DIR = os.path.join(ROOT, "sdr")
# 环境变量只给自动化验收用：它要改接收机位置，不能动使用者自己的设置
RUNTIME = os.environ.get("AIRPORTS_RUNTIME") or os.path.join(ROOT, ".runtime")
_sdr = {"enabled": False, "state": "off", "port": None, "reason": "", "proc": None}


def _which(name):
    # 从访达或 Electron 启动时拿不到终端里的 PATH，Homebrew 和 uv 的目录要自己找
    hit = shutil.which(name)
    if hit:
        return hit
    for d in ("/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin")):
        if os.path.exists(os.path.join(d, name)):
            return os.path.join(d, name)
    return None


def _sdr_python():
    """找一个装好依赖的解释器。返回 (路径, None) 或 (None, 原因)。"""
    import subprocess
    venv = os.path.join(SDR_DIR, ".venv", "bin", "python")
    if not os.path.exists(venv):
        uv = _which("uv")
        if uv:
            # 一次性装依赖。不长期托管 `uv run`：多一层进程，生死跟随就多一处会断
            _sdr["state"] = "installing"
            cmd = [uv, "sync", "--project", SDR_DIR]
            if os.path.exists(os.path.join(SDR_DIR, "uv.lock")):
                cmd.append("--frozen")
            try:
                subprocess.run(cmd, cwd=SDR_DIR, timeout=600, capture_output=True)
            except (OSError, subprocess.TimeoutExpired):
                pass
    if os.path.exists(venv):
        return venv, None
    try:                                        # 没有 uv：当前解释器恰好装了依赖也行
        ok = subprocess.run([sys.executable, "-c", "import aiohttp, numpy"],
                            capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if ok:
        return sys.executable, None
    return None, "缺少依赖：装好 uv 后重试，或 pip install aiohttp numpy"


def _sdr_run(http_port):
    import subprocess
    py, why = _sdr_python()
    if not py:
        _sdr.update(state="failed", reason=why)
        print("\n  ⚠ 本地接收机起不来：" + why)
        return
    os.makedirs(RUNTIME, exist_ok=True)
    cmd = [py, "-u", "-m", "sdrd", "--parent-pipe", "--runtime-dir", RUNTIME,
           # 只放行本页面的来源：别的网站哪怕知道端口也连不上接收机
           "--allow-origin", "http://localhost:%d" % http_port,
           "--allow-origin", "http://127.0.0.1:%d" % http_port]
    try:
        log = open(os.path.join(RUNTIME, "sdrd.log"), "ab")
        proc = subprocess.Popen(cmd, cwd=SDR_DIR, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=log)
    except OSError as e:
        _sdr.update(state="failed", reason=str(e))
        return
    _sdr.update(proc=proc, state="starting")
    try:
        ready = json.loads(proc.stdout.readline() or b"{}")
    except ValueError:
        ready = {}
    if ready.get("ready"):
        _sdr.update(state="running", port=ready["port"], reason="")
        print("\n  ✓ 本地接收机已启动（端口 %d）" % ready["port"])
    else:
        _sdr.update(state="failed", reason=ready.get("error") or "守护进程没有就绪，见 .runtime/sdrd.log")
        print("\n  ⚠ 本地接收机起不来：" + _sdr["reason"])
    proc.wait()
    if _sdr["state"] == "running":
        _sdr.update(state="exited", port=None, reason="守护进程已退出，见 .runtime/sdrd.log")


def sdr_start(http_port):
    _sdr.update(enabled=True, state="starting")
    threading.Thread(target=_sdr_run, args=(http_port,), daemon=True).start()


def sdr_stop():
    proc = _sdr.get("proc")
    if not proc or proc.poll() is not None:
        return
    import subprocess
    try:
        proc.stdin.close()                      # 管道一关，它自己收场：关设备、关 Bias-T、收解码进程
        proc.wait(6)
    except (OSError, subprocess.TimeoutExpired):
        proc.terminate()
        try:
            proc.wait(3)
        except subprocess.TimeoutExpired:
            proc.kill()


def sdr_response():
    if _sdr["enabled"]:
        body = {k: _sdr[k] for k in ("enabled", "state", "port", "reason")}
    else:
        # 没用 --sdr，但也许有人手动起了守护进程（开发时常这么干）
        body = {"enabled": False}
        try:
            with open(os.path.join(RUNTIME, "sdrd.json"), encoding="utf-8") as f:
                info = json.load(f)
            os.kill(int(info["pid"]), 0)        # 进程不在了会抛异常，旧文件就不算数
            body = {"enabled": True, "state": "running", "port": int(info["port"]), "reason": ""}
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return 200, json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()


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

    def do_GET(self):
        if self.path.startswith("/api/route"):
            _, _, qs = self.path.partition("?")
            code, body = route_response(qs)
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/flights"):
            _, _, qs = self.path.partition("?")
            code, body = flights_response(qs)
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.split("?", 1)[0] == "/api/sdr":
            code, body = sdr_response()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def send_head(self):
        # 点开头的路径一律不给。这个服务器把整个仓库目录当静态文件送出，
        # 其中 .git/ 是全部历史，.runtime/sdr.json 里有接收机的经纬度——也就是
        # 使用者的住址。虽然只监听本机，也不该让任何一个本机网页读得到。
        clean = urllib.parse.unquote(self.path.split("?", 1)[0].split("#", 1)[0])
        if any(seg.startswith(".") for seg in clean.split("/") if seg):
            self.send_error(403, "Forbidden")
            return None
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
    if "--sdr" in sys.argv:
        print("  本地接收机  启动中…（首次会安装依赖，约一分钟）")
        sdr_start(port)                     # 端口定下来之后才能起：它要拿端口去限定页面来源

    def _term(*_):                          # 以前只认 Ctrl-C；被 kill 时也得走 finally 把子进程带走
        signal.signal(signal.SIGTERM, signal.SIG_IGN)   # 只认第一次：收尾途中再来一个不能把收尾打断
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)

    if "--parent-pipe" in sys.argv:
        # 给 Electron 外壳用：就绪后报一行端口，此后标准输入一断就退出。
        # 和守护进程跟随我们是同一个办法，外壳被强杀时整条链照样收得干净。
        print(json.dumps({"ready": True, "port": port}), flush=True)

        def _lifeline():
            try:
                while sys.stdin.buffer.read(4096):
                    pass
            except (OSError, ValueError):
                pass
            httpd.shutdown()
        threading.Thread(target=_lifeline, daemon=True).start()

    if "--open" in sys.argv:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{port}/")).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        sdr_stop()
        httpd.server_close()

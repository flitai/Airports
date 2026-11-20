"""ADS-B 解码：监管一个 readsb 子进程，把原始采样经标准输入喂给它。

取代 SDR Lab 的 modes.py / aircraft.py。原版用纯 Python 自己解，只认 DF17/18、
没有纠错、只算空中位置；readsb 全格式解码、带纠错、带地面位置和信号强度统计。

接入方式是「我们持有接收机，readsb 读管道」，而不是让 readsb 自己开设备：
  * 管道一关 readsb 就读到文件结束、自行退出（实测 1.9 秒内）。它从不碰 USB，
    所以无论我们怎么死，都不会留下一个占着接收机的孤儿进程。
  * 同一条管道可以喂回放和合成信号，不插接收机也能测全链路。
  * 以后频谱要和 ADS-B 同时看，采样在我们手里才分得出去。
代价是用不了 readsb 自带的自动增益，由引擎那边按削顶比例自己调。

readsb 在这种模式下拿采样点数当时钟（2.4 MS/s 写死在它的源码里），JSON 里的
now 不是墙上时间，只能用 seen / seen_pos 这类相对量。
"""
import itertools
import json
import os
import queue
import shutil
import subprocess
import threading
import time

RATE = 2_400_000
CENTER = 1_090_000_000
_serial = itertools.count()


def find_readsb():
    # 从访达或 Electron 启动的进程不继承终端的 PATH，Homebrew 的目录得自己补上
    return (os.environ.get("SDR_READSB") or shutil.which("readsb")
            or next((p for p in ("/opt/homebrew/bin/readsb", "/usr/local/bin/readsb")
                     if os.path.exists(p)), None))


class ReadsbDecoder:
    def __init__(self, run_dir, home=None, binary=None):
        # 每个实例一个输出目录。旧实例收尾要 3 秒左右，这期间新实例可能已经起来了；
        # 共用目录的话，旧的临走前写下的最后一份 JSON 会被新的当成自己的数据读走。
        self.base = os.path.join(run_dir, "readsb")
        self.dir = os.path.join(self.base, "%d-%d" % (os.getpid(), next(_serial)))
        self.home = home
        self.binary = binary or find_readsb()
        self.proc = None
        self.q = None
        self.writer = None
        self.dropped = 0                 # 管道写不动时丢掉的字节数：readsb 卡住了才会涨
        self.started = 0.0
        self._mtime = 0
        self._prev = None                # (now, messages)，用来差分出报文速率
        self.msg_rate = 0.0

    @property
    def available(self):
        return bool(self.binary)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        os.makedirs(self.dir, exist_ok=True)
        mine = "%d-" % os.getpid()
        for name in os.listdir(self.base):                   # 上次被强杀时没来得及清的目录
            if not name.startswith(mine) and os.path.isdir(os.path.join(self.base, name)):
                shutil.rmtree(os.path.join(self.base, name), ignore_errors=True)
        # --device-type 必须排在最前面，readsb 按它决定后面哪些选项合法
        cmd = [self.binary, "--device-type", "ifile", "--ifile", "-", "--iformat", "UC8",
               "--write-json", self.dir, "--write-json-every", "1",
               "--max-range", "300", "--quiet"]
        if self.home:
            cmd += ["--lat", "%.5f" % self.home[0], "--lon", "%.5f" % self.home[1]]
        self.log = open(os.path.join(self.base, "readsb.log"), "ab")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=self.log,
                                     stderr=subprocess.STDOUT)
        self.q = queue.Queue(maxsize=64)                     # 64 块约 0.9 秒
        self.dropped, self._prev, self.msg_rate, self._mtime = 0, None, 0.0, 0
        self.started = time.time()
        self.writer = threading.Thread(target=self._write, args=(self.proc, self.q),
                                       name="adsb-pipe", daemon=True)
        self.writer.start()

    def _write(self, proc, q):
        # 写管道单独占一个线程：readsb 万一卡住，被堵的是这里，不是 USB 回调
        while True:
            block = q.get()
            if block is None:
                break
            try:
                proc.stdin.write(block)
            except (BrokenPipeError, ValueError, OSError):
                break

    def feed(self, block):
        """在 USB 回调线程里调用，绝不阻塞。"""
        q = self.q
        if q is None:
            return
        try:
            q.put_nowait(block)
        except queue.Full:
            self.dropped += len(block)

    def stop(self, wait=True):
        """停掉解码进程。wait=False 时立即返回，收尾放到后台线程。

        readsb 在管道模式下从读到文件结束到真正退出固定要 3 秒左右（发 SIGTERM
        也一样，实测过）。切换模式时不能让引擎线程陪它等这 3 秒。
        """
        proc, q, log, out = self.proc, self.q, getattr(self, "log", None), self.dir
        self.proc = self.q = None
        if q is not None:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass
        if proc is None:
            return
        try:
            proc.stdin.close()                               # 文件结束 → readsb 自己退出
        except OSError:
            pass

        def reap():
            for step in (proc.terminate, proc.kill):         # 信号只是它不肯走时的后手
                try:
                    proc.wait(6)
                    break
                except subprocess.TimeoutExpired:
                    step()
            proc.wait()                                      # 无论怎么走的都要回收，别留僵尸进程
            if log:
                log.close()
            shutil.rmtree(out, ignore_errors=True)

        if wait:
            reap()
        else:
            threading.Thread(target=reap, name="adsb-reap", daemon=True).start()

    def snapshot(self):
        """读最新的 aircraft.json。文件没变就返回 None。"""
        path = os.path.join(self.dir, "aircraft.json")
        try:
            mtime = os.stat(path).st_mtime_ns
            if mtime == self._mtime:
                return None
            with open(path, encoding="utf-8") as f:
                data = json.load(f)                          # readsb 是先写临时文件再改名，读不到半截
        except (OSError, ValueError):
            return None
        self._mtime = mtime
        now, messages = data.get("now") or 0, data.get("messages") or 0
        if self._prev and now > self._prev[0]:
            self.msg_rate = max(0.0, (messages - self._prev[1]) / (now - self._prev[0]))
        self._prev = (now, messages)
        return data


def extras(aircraft):
    """readsb 还解出了些什么 → 给航班详情看的那几样。没收到的不带，所以多数时候是个很小的字典。

    sq 应答机编码，em 紧急状态（没有紧急情况时不带），vr 升降率 ft/min，sel 选定高度 ft，
    ias 指示空速 kt，mach 马赫数，wd / ws 飞机算出的风向和风速，oat 外界温度 ℃，
    cat 飞机类别（A1–C3），gnd 在地面。
    位置精度（NIC、NACp、SIL 这些）和报文计数不带：那是调试接收机用的，不是给看图的人的。
    """
    a, x = aircraft, {}
    for src, key in (("squawk", "sq"), ("ias", "ias"), ("mach", "mach"), ("wd", "wd"), ("ws", "ws"),
                     ("oat", "oat"), ("category", "cat")):
        if a.get(src) is not None:
            x[key] = a[src]
    if a.get("emergency") not in (None, "none"):
        x["em"] = a["emergency"]
    rate = a.get("baro_rate", a.get("geom_rate"))            # 气压升降率优先，没有就用几何的
    if rate is not None:
        x["vr"] = rate
    sel = a.get("nav_altitude_mcp", a.get("nav_altitude_fms"))   # 面板上拨的优先，其次是飞行管理计算机里的
    if sel is not None:
        x["sel"] = sel
    if a.get("alt_baro") == "ground":
        x["gnd"] = True
    return x


def slim(aircraft, wall):
    """readsb 的一架飞机 → 发给页面的定长数组。

    [hex, 呼号, lat, lon, 气压高度, 地速, 航向, 位置时刻, 信号强度 dBFS, 距上次收到任何报文的秒数, 附加信息]

    位置时刻用「本机墙上时间 − seen_pos」换算成绝对时间，页面拿它和联网数据的
    位置时刻比新旧。没解出位置的飞机 lat/lon 为 null，照样发：它算在架数里。
    附加信息是 extras() 给的字典，排在最后：不认识它的旧页面照样能用前十项。
    """
    alt = aircraft.get("alt_baro")
    has_pos = "lat" in aircraft and "lon" in aircraft
    return [aircraft.get("hex", ""), (aircraft.get("flight") or "").strip(),
            round(aircraft["lat"], 5) if has_pos else None,
            round(aircraft["lon"], 5) if has_pos else None,
            None if alt in ("ground", None) else alt,
            aircraft.get("gs"), aircraft.get("track"),
            round(wall - aircraft.get("seen_pos", 0), 1) if has_pos else None,
            aircraft.get("rssi"), aircraft.get("seen"), extras(aircraft)]

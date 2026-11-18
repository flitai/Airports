"""接收机引擎：唯一持有采样源的地方，一个模式状态机。

对应 SDR Lab 的 session.py，行为保留、结构重来。原版的毛病是：
  * 换模式要「停止 → 选预设 → 启动」。这里模式只是一个期望值，随时可改，
    引擎线程负责把现实拉到期望上，拉不上（没插、被占用）就隔两秒再试。
  * 150 行 if/elif 分发命令。这里是一张表。
  * 一个线程既排空 USB 又干所有重活。这里 USB 回调只转交数据块。

线程：
  rtl-usb / iq-*   采样源的回调，只调 _on_block
  adsb-pipe        往 readsb 写管道
  sdr-engine       本文件的 run()：处理命令、开关设备、每秒读一次解码结果
页面永远只看 snapshot() 给出的完整快照，界面状态全部由它推出来。
"""
import queue
import threading
import time
from concurrent.futures import Future

import numpy as np

from . import adsb
from .device import DeviceError
from .geo import RangeOutline
from .sources import make_source

MODES = ("off", "adsb")              # air / scan 在后续里程碑加入
RETRY_S = 2.0
FRESH_S = 60                         # 超过这么久没收到任何报文的飞机不再上报
CLIP_HIGH, CLIP_LOW = 0.005, 0.0002  # 削顶比例：高于前者降一档增益，低于后者升一档
ZERO = {"ac": 0, "ac_pos": 0, "msg_rate": 0}


class Engine:
    def __init__(self, cfg, source_spec, run_dir, notify=None):
        self.cfg, self.source_spec, self.run_dir = cfg, source_spec, run_dir
        self.notify = notify or (lambda kinds: None)
        self.cmds = queue.Queue()
        self.thread = None
        self._quit = threading.Event()
        self._lock = threading.Lock()

        self.mode = cfg.get("mode") if cfg.get("mode") in MODES else "adsb"
        self.home = tuple(cfg.get("home")) if cfg.get("home") else None
        saved = cfg.get("outline") or {}
        same = self.home and saved.get("home") and tuple(saved["home"]) == self.home
        self.outline = RangeOutline(self.home, saved.get("nm") if same else None)

        self.source = None
        self.decoder = None
        self.device = {"state": "released", "model": "", "tuner": "", "serial": "", "v4": False}
        self.error = None                # 最近一次打不开设备的原因，给界面显示
        self._retry_at = 0.0
        self._lost = False
        self._blocks = 0
        self._clip_sum = self._clip_n = 0
        self._last_block = 0.0
        self._gain_at = 0.0
        self._decoder_retry = 1.0
        self._decoder_at = 0.0
        self._clip = 0.0
        self._adsb = dict(ZERO, state="starting")    # 两次 JSON 更新之间沿用上一次的计数
        self._snap = {"aircraft": {"t": "aircraft", "list": []},
                      "outline": dict(self.outline.to_json(), t="outline")}
        self._snap["status"] = self._status({})

    # ---- 对外接口（任何线程都可以调）----
    def start(self):
        self.thread = threading.Thread(target=self.run, name="sdr-engine", daemon=True)
        self.thread.start()

    def stop(self):
        self._quit.set()
        if self.thread:
            self.thread.join(10)

    def command(self, op):
        """提交一条命令，返回 Future，结果是 {"ok":bool, "code":..., "msg":...}。"""
        fut = Future()
        self.cmds.put((op, fut))
        return fut

    def snapshot(self, kind):
        with self._lock:
            return self._snap[kind]

    # ---- 命令表 ----
    def _op_mode(self, op):
        mode = op.get("mode")
        if mode not in MODES:
            return {"ok": False, "code": "bad_mode", "msg": "这个模式尚未实现"}
        self.mode = mode
        self.cfg.set(mode=mode)
        self._retry_at = 0.0             # 用户刚点过，别让他等重试间隔
        return {"ok": True}

    def _op_home(self, op):
        lat, lon = op.get("lat"), op.get("lon")
        if lat is None or lon is None:
            self.home = None
        elif isinstance(lat, (int, float)) and isinstance(lon, (int, float)) \
                and -90 <= lat <= 90 and -180 <= lon <= 180:
            self.home = (round(float(lat), 5), round(float(lon), 5))
        else:
            return {"ok": False, "code": "bad_home", "msg": "经纬度超出范围"}
        self.outline = RangeOutline(self.home)       # 换了位置，旧轮廓就不成立了
        self.cfg.set(home=list(self.home) if self.home else None, outline=self.outline.to_json())
        self._publish(outline=dict(self.outline.to_json(), t="outline"))
        if self.decoder and self.decoder.alive():    # 接收机位置是 readsb 的启动参数
            self.decoder.stop(wait=False)
            self._start_decoder()
        return {"ok": True}

    def _op_set(self, op):
        if "gain_db" in op:
            g = op["gain_db"]
            if g != "auto" and not (isinstance(g, (int, float)) and 0 <= g <= 60):
                return {"ok": False, "code": "bad_gain", "msg": "增益应为 auto 或 0–60 dB"}
            self.cfg.set(gain=g)
            if self.source and g != "auto":
                self.source.set_gain(float(g))
        return {"ok": True}

    OPS = {"mode": _op_mode, "home": _op_home, "set": _op_set}

    # ---- 采样块入口（采样源线程）----
    def _on_block(self, block):
        self._blocks += 1
        self._last_block = time.monotonic()
        dec = self.decoder
        if dec is not None:
            dec.feed(block)
        if self._blocks & 7 == 0:        # 每 8 块抽一块算削顶，约 9 次/秒，开销可忽略
            a = np.frombuffer(block, np.uint8)
            self._clip_sum += float(np.mean((a == 0) | (a == 255)))
            self._clip_n += 1

    def _on_lost(self):
        self._lost = True

    # ---- 把现实拉到期望上 ----
    def _open(self):
        self.device = dict(self.device, state="opening")
        src = make_source(self.source_spec, ppm=self.cfg.get("ppm") or 0)
        try:
            src.open()
            gain = self.cfg.get("gain")
            start_gain = max(src.gains) if src.gains and gain == "auto" else (gain if gain != "auto" else 0)
            src.configure(adsb.CENTER, adsb.RATE, float(start_gain))
        except DeviceError as e:
            src.close()
            self.device = dict(self.device, state=e.code)
            self.error = str(e)
            self._retry_at = time.monotonic() + RETRY_S
            return
        info = src.info()
        self.device = {"state": "ready", "model": info["model"], "tuner": info["tuner"],
                       "serial": info["serial"], "v4": info["v4"],
                       "simulated": bool(info.get("simulated")), "range_hz": info["range_hz"],
                       "gains": info.get("gains", [])}
        self.error, self._lost = None, False
        self.source = src
        src.on_lost = self._on_lost
        self._blocks = self._clip_sum = self._clip_n = 0
        self._last_block = time.monotonic()
        self._start_decoder()
        src.start(self._on_block)

    def _start_decoder(self):
        self.decoder = adsb.ReadsbDecoder(self.run_dir, self.home)
        self._adsb = dict(ZERO, state="starting")
        if self.decoder.available:
            self.decoder.start()

    def _close(self, state="released", wait=False):
        if self.source:
            self.source.close()          # 先停采样再关解码，免得往已关的管道里写
            self.source = None
        if self.decoder:
            self.decoder.stop(wait)      # 不等它收尾，见 adsb.ReadsbDecoder.stop
            self.decoder = None
        self.device = dict(self.device, state=state)
        self._publish(aircraft={"t": "aircraft", "list": []})

    def _auto_gain(self, now):
        """替代 readsb 自带的自动增益（管道模式下它摸不到设备）。"""
        if now < self._gain_at or not self._clip_n:
            return
        clip, self._clip_sum, self._clip_n = self._clip_sum / self._clip_n, 0, 0
        self._gain_at = now + 5
        self._clip = clip
        src = self.source
        if self.cfg.get("gain") != "auto" or not src or not src.gains or src.gain is None:
            return
        i = min(range(len(src.gains)), key=lambda k: abs(src.gains[k] - src.gain))
        if clip > CLIP_HIGH and i > 0:
            src.set_gain(src.gains[i - 1])
        elif clip < CLIP_LOW and i < len(src.gains) - 1:
            src.set_gain(src.gains[i + 1])

    def _poll_decoder(self, now):
        dec = self.decoder
        if not dec or not dec.available:
            return {}
        if not dec.alive():              # readsb 自己死了：指数退避重启，别原地打转
            if now >= self._decoder_at:
                self._decoder_at = now + self._decoder_retry
                self._decoder_retry = min(30.0, self._decoder_retry * 2)
                dec.stop(wait=False)
                self._start_decoder()
                return {}
            return {"state": "failed"}
        data = dec.snapshot()
        if data is None:
            return {}
        if time.time() - dec.started > 10:
            self._decoder_retry = 1.0    # 稳定跑了一阵，退避归零
        wall = time.time()
        rows, grew, with_pos = [], False, 0
        for a in data.get("aircraft", []):
            if a.get("seen", 999) > FRESH_S:
                continue
            row = adsb.slim(a, wall)
            rows.append(row)
            if row[2] is not None:
                with_pos += 1
                if a.get("seen_pos", 99) < 5 and self.outline.update(row[2], row[3]):
                    grew = True
        self._publish(aircraft={"t": "aircraft", "list": rows})
        if grew:
            self.cfg.set(outline=self.outline.to_json())
            self._publish(outline=dict(self.outline.to_json(), t="outline"))
        return {"state": "running", "ac": len(rows), "ac_pos": with_pos,
                "msg_rate": round(dec.msg_rate, 1)}

    def _status(self, adsb_part):
        src, dec = self.source, self.decoder
        if self.mode != "adsb" or not src:
            a = dict(ZERO, state="paused")
        elif not dec or not dec.available:
            a = dict(ZERO, state="no_readsb")            # 没装 readsb：界面给出安装提示
        else:
            a = self._adsb = dict(self._adsb, **adsb_part)
        a = dict(a, max_nm=self.outline.max_nm, pipe_drops=dec.dropped if dec else 0)
        warnings = []
        if src and self._clip > CLIP_HIGH and self.cfg.get("gain") != "auto":
            warnings.append({"code": "clip"})            # 自动增益时它自己会降，不用打扰人
        if src and time.monotonic() - self._last_block > 2:
            warnings.append({"code": "stalled"})         # 设备开着但没有数据来
        rf = None
        if src:
            rf = {"center_hz": src.center, "rate_hz": adsb.RATE, "gain_db": src.gain,
                  "auto": self.cfg.get("gain") == "auto", "clip": round(self._clip, 5)}
        return {"t": "status", "mode": self.mode, "device": dict(self.device), "error": self.error,
                "rf": rf, "adsb": a, "home": list(self.home) if self.home else None,
                "warnings": warnings}

    def _publish(self, **kinds):
        with self._lock:
            self._snap.update(kinds)
        self.notify(list(kinds))

    # ---- 引擎线程 ----
    def run(self):
        next_tick = 0.0
        try:
            while not self._quit.is_set():
                try:
                    op, fut = self.cmds.get(timeout=0.2)
                    handler = self.OPS.get(op.get("op"))
                    try:
                        fut.set_result(handler(self, op) if handler
                                       else {"ok": False, "code": "bad_op", "msg": "不认识的命令"})
                    except Exception as e:           # 一条坏命令不该带走整个引擎
                        fut.set_result({"ok": False, "code": "error", "msg": str(e)})
                    next_tick = 0.0                  # 命令之后立刻出一次状态，界面不用等
                except queue.Empty:
                    pass
                now = time.monotonic()
                if now < next_tick:
                    continue
                next_tick = now + 1.0
                if self._lost and self.source:       # 被拔掉了
                    self._close("absent")
                    self.error = "接收机断开了"
                    self._retry_at = now + RETRY_S
                if self.mode == "off":
                    if self.source:
                        self._close("released")
                    # 用户选了关闭：之前「没插上」「被占用」之类的报错都不再相干
                    self.device = dict(self.device, state="released")
                    self.error = None
                elif self.mode != "off" and not self.source and now >= self._retry_at:
                    self._open()
                part = {}
                if self.source:
                    self._auto_gain(now)
                    part = self._poll_decoder(now)
                self._publish(status=self._status(part))
        finally:
            # 退出时一定关设备，Bias-T 也在这里关掉。解码进程不等它收尾（要 3 秒）：
            # 管道一断它必然自己走，而且它不碰 USB。我们早走一步，文件锁和接收机
            # 就早一步让出来——用户退出后立刻重开，新的守护进程才不会撞上「已在运行」。
            self._close("released", wait=False)

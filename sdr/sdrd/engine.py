"""接收机引擎：唯一持有采样源的地方，一个模式状态机。

对应 SDR Lab 的 session.py，行为保留、结构重来。原版的毛病是：
  * 换模式要「停止 → 选预设 → 启动」。这里模式只是一个期望值，随时可改，
    引擎线程负责把现实拉到期望上，拉不上（没插、被占用）就隔两秒再试。
  * 150 行 if/elif 分发命令。这里是一张表。
  * 一个线程既排空 USB 又干所有重活。这里 USB 回调只转交数据块。

线程：
  rtl-usb / iq-*   采样源的回调，只调 _on_block
  adsb-pipe        往 readsb 写管道
  sdr-dsp          话音模式下的频谱、静噪和解调（见 air.Monitor）
  sdr-engine       本文件的 run()：处理命令、开关设备、调谐、轮巡、每秒读一次解码结果

采样率全程 2.4 MS/s。模式之间、窗口之间的切换都只是重调谐，设备不关、异步读取
不停，所以切换是几十毫秒的事。
页面永远只看 snapshot() 给出的完整快照，界面状态全部由它推出来。
"""
import queue
import threading
import time
from concurrent.futures import Future

import numpy as np

from . import adsb
from .air import Monitor
from .device import DeviceError
from .dsp import channels as chan
from .geo import RangeOutline
from .sources import make_source

MODES = ("off", "adsb", "air")       # scan 在后续里程碑加入
RESTING = ("off", "adsb")            # 能写进设置、重启后接着干的模式；话音要有人在听才有意义
RETRY_S = 2.0
DWELL_S = 1.5                        # 轮巡：每个窗口至少看这么久
HANG_S = 3.0                         # 轮巡：通话结束后再等这么久，回话多半就在这几秒里
SILENT_S = 4.0                       # 正在听的频道载波亮了这么久却没有话音：同样当常驻载波
STUCK_S = 20.0                       # 一个通话频道连续亮了这么久，就当它是常驻载波（干扰，或卡住的话筒）
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

        self.mode = cfg.get("mode") if cfg.get("mode") in RESTING else "adsb"
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

        # ---- 话音 ----
        self.air = None                  # 当前的频道表和窗口划分，见 _set_plan
        self.monitor = None              # 当前窗口上的 Monitor；它在 sdr-dsp 线程里干活
        self.on_audio = None             # function(bytes)：一帧音频，由 sdr-dsp 线程调用
        self.listeners = 0               # 有几个页面在出声，由服务端维护
        self.auto_return_s = 60          # 没人听了，过这么久自动回到进话音之前的模式
        self._rf = None                  # 接收机此刻实际在干什么：None / "adsb" / "air"
        self._gen = 0                    # 每次重调谐加一，采样块带着它排队
        self._dspq = queue.Queue(maxsize=32)         # 32 块约 0.44 秒
        self._dsp_drops = 0
        self._frames = 0
        self._dsp_error = None
        self._return_to = self.mode
        self._idle_since = None
        self._idle_left = None
        self._tuned_at = 0.0
        self._hold_until = 0.0
        self._listen_at = 0.0            # 当前这个频道是什么时候开始解调的
        self._manual_id = 10_000

        self._snap = {"aircraft": {"t": "aircraft", "list": []},
                      "channels": {"t": "channels", "win": 0, "listen": None, "open": False,
                                   "voice": False, "level": {}, "active": [], "steady": []},
                      "outline": dict(self.outline.to_json(), t="outline")}
        self._snap["status"] = self._status({})

    # ---- 对外接口（任何线程都可以调）----
    def start(self):
        self.thread = threading.Thread(target=self.run, name="sdr-engine", daemon=True)
        self.thread.start()
        threading.Thread(target=self._dsp_run, name="sdr-dsp", daemon=True).start()

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
        if mode == "air" and not self.air:
            return {"ok": False, "code": "no_channels",
                    "msg": "还没选频道：打开一个机场，在频率表里点「收听」"}
        self._set_mode(mode)
        return {"ok": True}

    def _set_mode(self, mode):
        if mode == "air":
            if self.mode != "air":
                self._return_to = self.mode
            self._idle_since = self._idle_left = None
        else:
            self.cfg.set(mode=mode)
        self.mode = mode
        self._retry_at = 0.0             # 用户刚点过，别让他等重试间隔

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
        if "squelch_db" in op:
            q = op["squelch_db"]
            if isinstance(q, bool) or not isinstance(q, (int, float)) or not 0 <= q <= 40:
                return {"ok": False, "code": "bad_squelch", "msg": "静噪门限应为 0–40 dB"}
            self.cfg.set(squelch_db=float(q))
            if self.monitor:
                self.monitor.set_squelch(float(q))
        if "volume" in op:
            v = op["volume"]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
                return {"ok": False, "code": "bad_volume", "msg": "音量应为 0–1"}
            self.cfg.set(volume=round(float(v), 2))
        if "voice_only" in op:
            self.cfg.set(voice_only=bool(op["voice_only"]))
            if self.monitor:
                self.monitor.voice_only = bool(op["voice_only"])
        if "patrol" in op:
            if not self.air:
                return {"ok": False, "code": "no_channels", "msg": "还没选频道"}
            self.air["patrol"] = bool(op["patrol"])
            self._hold_until = 0.0
        return {"ok": True}

    # ---- 话音：频道表、窗口、收听 ----
    def _set_plan(self, channels, pick, label="", airport=None, skipped=0):
        """channels 是 dsp.channels.build 的结果，pick 是其中一个的 id（或 None）。"""
        at = next((k for k, c in enumerate(channels) if c["id"] == pick), None)
        windows = chan.plan(channels, at)
        win = channels[at]["win"] if at is not None else 0
        steady = self.air["steady"] if self.air else set()
        self.air = {"label": label, "airport": airport, "channels": channels, "windows": windows,
                    "win": win, "listen": pick, "patrol": False, "skipped": skipped,
                    # 常驻载波的频率。记在这儿而不是 Monitor 里：换窗口回来不必重新认一遍
                    "steady": {c["hz"] for c in channels if c["hz"] in steady}}
        if pick is None:
            self.air["listen"] = self._best_in(win)
        self._set_mode("air")
        if self._rf == "air":            # 已经在话音模式里：直接换过去，不必等下一拍
            self._tune_window(win)

    def _channel(self, ident):
        return next((c for c in self.air["channels"] if c["id"] == ident), None) if self.air else None

    def _best_in(self, win):
        """这个窗口里默认听哪个：此刻有信号的优先，其次按权重，连续播发的排最后。"""
        ids = self.air["windows"][win]["ch"]
        mon = self.monitor if self.monitor and self.air["win"] == win else None
        live = lambda i: bool(mon and i in mon.squelch and mon.squelch[i].open)
        chs = [self._channel(i) for i in ids]
        return max(chs, key=lambda c: (live(c["id"]) and not c["cont"], not c["cont"], c["weight"], -c["hz"]))["id"]

    def _op_channels(self, op):
        rows = op.get("list")
        if not isinstance(rows, list) or len(rows) > 400:
            return {"ok": False, "code": "bad_channels", "msg": "频率表格式不对"}
        channels = chan.build(rows)
        if not channels:
            return {"ok": False, "code": "no_channels", "msg": "这个机场没有可收听的民航话音频率"}
        pick = next((c["id"] for c in channels if op.get("pick") in c["rows"]), None)
        airport = op.get("airport") if isinstance(op.get("airport"), (int, str)) else None
        self._set_plan(channels, pick, chan.label_of(op.get("label")), airport, len(rows) - sum(len(c["rows"]) for c in channels))
        return {"ok": True}

    def _op_listen(self, op):
        ch = self._channel(op.get("ch"))
        if not ch:
            return {"ok": False, "code": "bad_channel", "msg": "没有这个频道"}
        self.air["patrol"] = False       # 明确点了一个频道：就听它，不再自动跳
        self.air["listen"] = ch["id"]
        self._set_mode("air")
        if self._rf == "air":
            if ch["win"] != self.air["win"] or not self.monitor:
                self._tune_window(ch["win"])
            else:
                self.monitor.listen(ch["id"])
                self._listen_at = time.monotonic()
        else:
            self.air["win"] = ch["win"]
        return {"ok": True}

    def _op_window(self, op):
        i = op.get("i")
        if not self.air or isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(self.air["windows"]):
            return {"ok": False, "code": "bad_window", "msg": "没有这个窗口"}
        self.air["patrol"] = False
        self.air["win"] = i
        self.air["listen"] = self._best_in(i)
        self._set_mode("air")
        if self._rf == "air":
            self._tune_window(i)
        return {"ok": True}

    def _op_tune(self, op):
        """按步进调谐：从正在听的频率挪一格（25 kHz，fine 时 8.33 kHz），或直接给 hz。"""
        cur = self._channel(self.air["listen"]) if self.air else None
        if isinstance(op.get("hz"), (int, float)) and not isinstance(op.get("hz"), bool):
            hz = float(op["hz"])
        elif op.get("step") in (1, -1) and cur:
            grid = chan.THIRD if op.get("fine") else 25_000
            hz = (round(cur["hz"] / grid) + op["step"]) * grid
        else:
            return {"ok": False, "code": "bad_tune", "msg": "先选一个频道，再用方向键步进"}
        if not chan.AIR_LO <= hz <= chan.AIR_HI:
            return {"ok": False, "code": "bad_tune", "msg": "超出民航话音波段（118–137 MHz）"}
        self._manual_id += 1
        kept = [c for c in self.air["channels"] if not c.get("manual")] if self.air else []
        new = chan.manual(hz, self._manual_id)
        same = next((c for c in kept if c["hz"] == new["hz"]), None)     # 挪到了表里已有的频道上
        air = self.air or {}
        self._set_plan(kept if same else kept + [new], (same or new)["id"],
                       air.get("label") or "手动调谐", air.get("airport"), air.get("skipped", 0))
        return {"ok": True}

    def _tune_window(self, win):
        """把接收机调到第 win 个窗口，并为它建一个新的 Monitor。"""
        air, src = self.air, self.source
        w = air["windows"][win]
        self._gen += 1                   # 先作废排队中的旧块，再动硬件
        self.monitor = None
        try:
            src.tune(w["center_hz"])
        except DeviceError as e:
            self.error, self._lost = str(e), True
            return
        mon = Monitor(w["center_hz"], [self._channel(i) for i in w["ch"]],
                      self.cfg.get("squelch_db"), self._audio_out, bool(self.cfg.get("voice_only")))
        mon.gen = self._gen
        if air["listen"] not in w["ch"]:
            air["listen"] = self._best_in(win)
        mon.listen(air["listen"])
        air["win"] = win
        self.monitor = mon
        self._tuned_at = self._listen_at = time.monotonic()
        self._hold_until = 0.0

    def _audio_out(self, frame):
        self._frames += 1
        if self.on_audio:
            self.on_audio(frame)

    def _patrol(self, now, mon):
        """轮巡：有人说话就停在那个频道上听，没人说话就去下一个窗口。"""
        air = self.air
        holding = [c for c in mon.channels
                   if mon.squelch[c["id"]].open and not c["cont"] and c["hz"] not in air["steady"]]
        if holding:
            self._hold_until = now + HANG_S
            if air["listen"] not in [c["id"] for c in holding]:
                best = max(holding, key=lambda c: (c["weight"], mon.squelch[c["id"]].level or 0))
                air["listen"] = best["id"]
                mon.listen(best["id"])
                self._listen_at = now
        elif now >= self._hold_until and now - self._tuned_at >= DWELL_S:
            # 只有通播这类连续播发频道的窗口不去：到了那儿必然有声，等于每一圈都被它打断一次
            n = len(air["windows"])
            worth = [w for w in range(n) if any(not self._channel(i)["cont"] for i in air["windows"][w]["ch"])]
            later = [w for w in worth if w > air["win"]] or worth
            if later and later[0] != air["win"]:
                air["listen"] = None
                self._tune_window(later[0])

    def _air_tick(self, now):
        """每 0.2 秒一次：轮巡，并把各频道的电平推给页面。"""
        mon = self.monitor
        if self._rf != "air" or not mon:
            return
        # 常驻载波：一直亮着不灭的。室内常见，是电脑、电源、网口时钟的杂散，不是通话。
        # 灭过一次就摘掉这个标记。通播本来就该一直亮，不算。
        # 正在解调的那个频道还多一条更快的证据：载波亮了几秒，却始终没有话音。
        steady = self.air["steady"]
        for c in mon.channels:
            if c["cont"]:
                continue
            lit = now - mon.since.get(c["id"], now)
            if not mon.squelch[c["id"]].open:
                if now - self._tuned_at >= 1.0:      # 刚调过来的头一秒静噪还没来得及开，不算「灭了」
                    steady.discard(c["hz"])
            elif lit >= STUCK_S:
                steady.add(c["hz"])
            elif c["id"] == self.air["listen"] and min(lit, now - self._listen_at) >= SILENT_S \
                    and (mon.voice_at is None or now - mon.voice_at >= SILENT_S):
                steady.add(c["hz"])
        if self.air["patrol"]:
            self._patrol(now, mon)
            mon = self.monitor or mon
        self._publish(channels=dict(mon.snapshot(), t="channels", win=self.air["win"],
                                    listen=self.air["listen"],
                                    steady=[c["id"] for c in self.air["channels"] if c["hz"] in steady]))

    def _dsp_run(self):
        while not self._quit.is_set():
            try:
                gen, block = self._dspq.get(timeout=0.5)
            except queue.Empty:
                continue
            mon = self.monitor
            if mon is None or mon.gen != gen:
                continue                 # 重调谐之前排进来的，频率已经不对了
            try:
                mon.feed(block)
            except Exception as e:       # 解调出错不该带走线程；记下来给状态条显示
                self._dsp_error = "%s: %s" % (type(e).__name__, e)

    OPS = {"mode": _op_mode, "home": _op_home, "set": _op_set, "channels": _op_channels,
           "listen": _op_listen, "window": _op_window, "tune": _op_tune}

    # ---- 采样块入口（采样源线程）----
    def _on_block(self, block):
        self._blocks += 1
        self._last_block = time.monotonic()
        rf = self._rf
        if rf == "adsb":
            dec = self.decoder
            if dec is not None:
                dec.feed(block)
        elif rf == "air":
            try:
                self._dspq.put_nowait((self._gen, block))
            except queue.Full:           # 解调跟不上了：丢块并如实上报，绝不堵 USB 回调
                self._dsp_drops += 1
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
        self._rf = None                  # 先开流，干什么由 _sync_rf 决定
        src.start(self._on_block)

    def _sync_rf(self):
        """让接收机实际在干的事（_rf）跟上期望的模式。只重调谐，不关设备。"""
        want = self.mode
        if want == self._rf or want == "off":
            return
        if self._rf == "adsb" and self.decoder:
            self.decoder.stop(wait=False)
            self.decoder = None
            self._publish(aircraft={"t": "aircraft", "list": []})
        self._rf, self.monitor = None, None
        if want == "adsb":
            try:
                self.source.tune(adsb.CENTER)
            except DeviceError as e:
                self.error, self._lost = str(e), True
                return
            self._start_decoder()
            self._rf = "adsb"
        else:
            self._rf = "air"
            self._tune_window(self.air["win"])

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
        self._rf, self.monitor = None, None
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
        if self._dsp_error:
            warnings.append({"code": "dsp", "msg": self._dsp_error})
        air = None
        if self.air:
            air = {k: self.air[k] for k in ("label", "airport", "windows", "win", "listen", "patrol", "skipped")}
            air["channels"] = [{k: c[k] for k in ("id", "type", "desc", "label", "hz", "spacing", "win", "rows", "cont")}
                               for c in self.air["channels"]]
        rf = None
        if src:
            rf = {"center_hz": src.center, "rate_hz": adsb.RATE, "gain_db": src.gain,
                  "auto": self.cfg.get("gain") == "auto", "clip": round(self._clip, 5)}
        return {"t": "status", "mode": self.mode, "device": dict(self.device), "error": self.error,
                "rf": rf, "adsb": a, "home": list(self.home) if self.home else None,
                "air": air, "squelch_db": self.cfg.get("squelch_db"), "volume": self.cfg.get("volume"),
                "voice_only": bool(self.cfg.get("voice_only")),
                "audio": {"listeners": self.listeners, "frames": self._frames,
                          "dsp_drops": self._dsp_drops},
                "return_to": self._return_to,
                "auto_return_s": self._idle_left if self.mode == "air" else None,
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
                    self._air_tick(now)
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
                if self.mode == "air":               # 没人听了就倒计时，到点回去接着收 ADS-B
                    if self.listeners > 0:
                        self._idle_since = self._idle_left = None
                    else:
                        self._idle_since = self._idle_since or now
                        self._idle_left = max(0, round(self.auto_return_s - (now - self._idle_since)))
                        if now - self._idle_since >= self.auto_return_s:
                            self._set_mode(self._return_to)
                part = {}
                if self.source:
                    self._sync_rf()
                    self._auto_gain(now)
                    if self._rf == "adsb":
                        part = self._poll_decoder(now)
                    self._air_tick(now)
                self._publish(status=self._status(part))
        finally:
            # 退出时一定关设备，Bias-T 也在这里关掉。解码进程不等它收尾（要 3 秒）：
            # 管道一断它必然自己走，而且它不碰 USB。我们早走一步，文件锁和接收机
            # 就早一步让出来——用户退出后立刻重开，新的守护进程才不会撞上「已在运行」。
            self._close("released", wait=False)

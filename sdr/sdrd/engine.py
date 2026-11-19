"""接收机引擎：唯一持有采样源的地方，一个模式状态机。

对应 SDR Lab 的 session.py，行为保留、结构重来。原版的毛病是：
  * 换模式要「停止 → 选预设 → 启动」。这里模式只是一个期望值，随时可改，
    引擎线程负责把现实拉到期望上，拉不上（没插、被占用）就隔两秒再试。
  * 150 行 if/elif 分发命令。这里是一张表。
  * 一个线程既排空 USB 又干所有重活。这里 USB 回调只转交数据块。

线程：
  rtl-usb / iq-*   采样源的回调，只调 _on_block
  adsb-pipe        往 readsb 写管道
  sdr-dsp          话音模式下的频谱、静噪和解调（见 air.Monitor）；两种模式下瀑布图的行
  sdr-engine       本文件的 run()：处理命令、开关设备、调谐、轮巡、每秒读一次解码结果
  sdr-rec*         录音和原始 I/Q 的写盘、删除（见 record.Store / record.IqStore）

采样率全程 2.4 MS/s。模式之间、窗口之间的切换都只是重调谐，设备不关、异步读取
不停，所以切换是几十毫秒的事。
页面永远只看 snapshot() 给出的完整快照，界面状态全部由它推出来。
"""
import collections
import os
import queue
import threading
import time
from concurrent.futures import Future

import numpy as np

from . import adsb
from .air import Monitor
from .device import DeviceError
from .dsp import candidates as cand
from .dsp import channels as chan
from .dsp import to_complex
from .dsp.spectrum import ROW_BLOCKS, ROW_S, Rows, Spectrum, spectrum_frame
from .geo import RangeOutline
from .record import IqStore, Store
from .sources import ReplaySource, make_source

MODES = ("off", "adsb", "air")       # scan 在后续里程碑加入
RESTING = ("off", "adsb")            # 能写进设置、重启后接着干的模式；话音要有人在听才有意义
RETRY_S = 2.0
DWELL_S = 1.5                        # 轮巡：每个窗口至少看这么久
HANG_S = 3.0                         # 轮巡：通话结束后再等这么久，回话多半就在这几秒里
SILENT_S = 4.0                       # 正在听的频道载波亮了这么久却没有话音：同样当常驻载波
STUCK_S = 20.0                       # 一个通话频道连续亮了这么久，就当它是常驻载波（干扰，或卡住的话筒）
FRESH_S = 60                         # 超过这么久没收到任何报文的飞机不再上报
CLIP_HIGH, CLIP_LOW = 0.005, 0.0002  # 削顶比例：高于前者降一档增益，低于后者升一档
SETTLE_S = 0.08                      # 重调谐后这么久之内的采样不拿来画频谱，锁相环还没稳住
RING_BLOCKS = 110                    # 原始采样的环形缓冲：110 块 = 1.5 秒、7.2 MB。录 I/Q 时放在最前面
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

        # ---- 录音 ----
        self.clip_s = None               # 一段最长多少秒；None 用 record.MAX_S。测试里会改小
        self.store = Store(os.path.join(run_dir, "recordings"),
                           float(cfg.get("rec_quota_mb") or 200) * 1e6, self._recordings_changed)

        # ---- 原始 I/Q 的录制与回放 ----
        # 环形缓冲一直留着最近 1.5 秒的采样（只是引用，不拷贝）。按下「立即录 I/Q」或话音触发时
        # 把它放在最前面：等人反应过来去按的时候，想录的那一下往往已经过去了。
        self.iq = IqStore(os.path.join(run_dir, "iq"), float(cfg.get("iq_quota_mb") or 2000) * 1e6,
                          self._iqs_changed)
        self._ring = collections.deque(maxlen=RING_BLOCKS)       # (调谐代号, 采样块)
        self._iq_lock = threading.Lock()                         # USB 回调与开 / 停录制之间
        self._iq_clip = False            # 话音触发的这段 I/Q，对应的话音片段存下来了没有
        self._replay = None              # 正在回放的那一段（IqStore 的条目加上路径）；实时接收时为 None
        self._replay_prev = self.mode    # 回放结束后回到哪个模式
        self._replay_alone = None        # 回放着却一个页面都没连着，是从什么时候开始的
        self.pages = 0                   # 连着几个页面，由服务端维护

        # ---- 频谱 ----
        # 频谱不是一种模式：话音和 ADS-B 下都有，有页面在看（watchers）才往外发。
        # 话音模式下谱是解调本来就要算的，白拿；ADS-B 模式下没人看就一格都不算。
        self.on_spectrum = None          # function(bytes)：一行频谱，由 sdr-dsp 线程调用
        self.watchers = 0                # 有几个页面开着瀑布图，由服务端维护
        self.tracker = cand.Tracker()    # 候选信号。跨窗口保留，所以放在这儿而不是 Monitor 里
        self._rows = Rows()
        self._plain = Spectrum()         # ADS-B 模式下画瀑布图用的谱
        self._plain_rows = Rows(blocks=1)    # 那边是 6 块里抽 1 块，抽到的每块就是一行
        self._row_seq = 0

        self._snap = {"aircraft": {"t": "aircraft", "list": []},
                      "channels": {"t": "channels", "win": 0, "listen": None, "open": False,
                                   "voice": False, "level": {}, "active": [], "steady": []},
                      "candidates": {"t": "candidates", "list": []},
                      "recordings": dict(self.store.snapshot(), t="recordings"),
                      "iqs": dict(self.iq.snapshot(), t="iqs"),
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
        if mode == "off" and self._replay:   # 关闭就是关闭：回放也一并结束
            self._replay_stop()
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
        elif not self._replay:           # 回放时切的模式只是为了看那段录制，不是重启后要接着干的事
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
        if "iq_with_voice" in op:
            self.cfg.set(iq_with_voice=bool(op["iq_with_voice"]))
        if "record" in op:
            if self.cfg.get("record") and not op["record"] and self.monitor:
                self.monitor.end_clip("stopped")     # 正在录的这段先存下来，再关
            self.cfg.set(record=bool(op["record"]))
        if self.monitor:
            self.monitor.record = self._triggering()
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

    def _op_drop(self, op):
        """删掉手动调出来的频道。它不来自任何频率表，不删就一直留在频道条上。"""
        ch = self._channel(op.get("ch"))
        if not ch or not ch.get("manual"):
            return {"ok": False, "code": "bad_channel", "msg": "只有手动调出来的频道可以删除"}
        air = self.air
        kept = [c for c in air["channels"] if not c.get("manual")]
        if not kept:                     # 频道表里只有它：没什么可听的了，回到进话音之前的模式
            self.air = None
            if self.mode == "air":
                self._set_mode(self._return_to)
            return {"ok": True}
        mode, patrol = self.mode, air["patrol"]
        # 正在听的不是它就接着听原来的；正在听的就是它，则由 _set_plan 另挑一个
        self._set_plan(kept, air["listen"] if air["listen"] != ch["id"] else None,
                       air["label"], air["airport"], air["skipped"])
        self.air["patrol"] = patrol      # 删一个频道不该顺手把轮巡关了
        if mode != "air":                # 不在话音模式时只是整理频道表，不借机切进去
            self._set_mode(mode)
        return {"ok": True}

    def _tune_window(self, win):
        """把接收机调到第 win 个窗口，并为它建一个新的 Monitor。"""
        air, src = self.air, self.source
        w = air["windows"][win]
        self._gen += 1                   # 先作废排队中的旧块，再动硬件
        self._drop_monitor()
        self._iq_stop("retune")          # 一段 I/Q 只有一个中心频率
        try:
            src.tune(w["center_hz"])
        except DeviceError as e:
            self.error, self._lost = str(e), True
            return
        mon = Monitor(w["center_hz"], [self._channel(i) for i in w["ch"]],
                      self.cfg.get("squelch_db"), self._audio_out, bool(self.cfg.get("voice_only")),
                      self._clip_out, self.clip_s)
        mon.record = self._triggering()
        mon.gen = self._gen
        if air["listen"] not in w["ch"]:
            air["listen"] = self._best_in(win)
        mon.listen(air["listen"])
        air["win"] = win
        self.monitor = mon
        self._tuned_at = self._listen_at = time.monotonic()
        self._hold_until = 0.0

    def _drop_monitor(self):
        """不再用当前这个 Monitor 了。它正在录的那段先收尾交出去，别丢。"""
        mon, self.monitor = self.monitor, None
        if mon:
            mon.end_clip()

    def _triggering(self):
        """话音触发要不要跑。「录音」和「录 I/Q」各自独立，开着任何一个都要：两者共用同一个触发。"""
        return bool(self.cfg.get("record") or self.cfg.get("iq_with_voice"))

    def _clip_out(self, ch, clip):
        """sdr-dsp 或引擎线程：一次通话结束了。开着「录音」就把这段话音交给写盘线程。

        没开「录音」、只开了「录 I/Q」时也会走到这里：这一段话音本身不存，但它算数——
        说明刚才那次触发是一次真的通话，对应的那段 I/Q 该留下。
        """
        cur = self.iq.active()
        if cur and cur["kind"] == "voice":
            self._iq_clip = True
        if not self.cfg.get("record"):
            return
        air = self.air or {}
        meta = {"hz": ch["hz"], "label": ch["label"], "type": ch["type"], "desc": ch["desc"],
                "spacing": ch["spacing"], "airport": air.get("airport"), "airport_label": air.get("label") or ""}
        if cur and cur["kind"] == "voice":           # 同时在录这一段的 I/Q：话音片段指过去
            meta["iq"] = cur["name"]
        if self._replay:
            meta["replay_of"] = self._replay["name"]   # 这段话音是从回放里录的，不是当时现场的
        self.store.add(clip, meta)

    def _recordings_changed(self):
        self._publish(recordings=dict(self.store.snapshot(), t="recordings"))

    def _op_record(self, op):
        """删一段，或者全部清空。开关录音走 set（它是一项设置，和静噪门限一样要存盘）。"""
        if op.get("clear") is True:
            self.store.clear()
        elif self.store.path_of(op.get("delete")):
            self.store.delete(op["delete"])
        else:
            return {"ok": False, "code": "bad_recording", "msg": "没有这段录音"}
        return {"ok": True}

    # ---- 原始 I/Q：录 ----
    def _iqs_changed(self):
        self._publish(iqs=dict(self.iq.snapshot(), t="iqs"))

    def _iq_start(self, kind):
        """开录当前整窗的 I/Q。kind：manual（按了按钮）或 voice（跟着话音触发）。"""
        src = self.source
        if not src or self._replay or self._rf not in ("adsb", "air") or self.iq.active():
            return False
        air = self.air if self._rf == "air" else None
        meta = {"kind": kind, "mode": self._rf, "center_hz": src.center, "rate_hz": adsb.RATE, "gain_db": src.gain,
                "device": self.device.get("model", "")}
        if air:
            # 回放时要靠这些把当时的频道表原样摆回来
            listen = self._channel(air["listen"])
            # 每个频道连同它对应频率表的哪几行一起记下：页面靠行号把指示灯点回频率表上
            meta.update(airport=air["airport"], airport_label=air["label"], listen=listen and listen["label"],
                        channels=[[c["type"], c["desc"], c["label"], c["rows"]] for c in air["channels"]])
        with self._iq_lock:
            pre = [b for g, b in self._ring if g == self._gen]   # 只要这次调谐之后的：之前的是别的频率
            self._iq_clip = False
            self.iq.begin(meta, pre, time.time() - len(pre) * len(pre[0]) / (2 * adsb.RATE) if pre else time.time())
        return True

    def _iq_stop(self, reason, keep=True):
        with self._iq_lock:
            self.iq.end(reason, keep)

    def _iq_tick(self, now):
        """每 0.2 秒一次：手动的到时长就停；话音触发的跟着话音片段开和停。"""
        cur = self.iq.active()
        mon = self.monitor
        tuned = mon.tuned if mon else None
        talking = bool(tuned and tuned[3].active)
        if cur:
            if cur["kind"] == "manual":
                if cur["s"] >= float(self.cfg.get("iq_max_s") or 60) + RING_BLOCKS * 65536 / (2 * adsb.RATE):
                    self._iq_stop("limit")
            elif not talking:
                # 话音片段结束了。它要是被当成噪声丢掉了，这段 I/Q 也不留
                self._iq_stop("clip", keep=self._iq_clip)
        elif talking and self.cfg.get("iq_with_voice") and self._rf == "air":
            self._iq_start("voice")

    def _op_iq(self, op):
        """手动开 / 停一段 I/Q，删一段，或者全部清空。"""
        if op.get("clear") is True:
            if self._replay:
                self._replay_stop()
            self._iq_stop("cleared", keep=False)
            self.iq.clear()
            return {"ok": True}
        if "delete" in op:
            name = op["delete"]
            if not self.iq.path_of(name):
                return {"ok": False, "code": "bad_recording", "msg": "没有这段 I/Q"}
            if self._replay and self._replay["name"] == name:
                self._replay_stop()                  # 正在放的这段要删：先回到实时
            self.iq.delete(name)
            return {"ok": True}
        if op.get("on"):
            if self._replay:
                return {"ok": False, "code": "replaying", "msg": "正在回放，录下来的只会是同一段。先返回实时接收"}
            cur = self.iq.active()
            if cur:                                  # 话音触发的那段正录着：接过来，改成手动的，由人来停
                self.iq.cur["meta"]["kind"] = "manual"
                return {"ok": True}
            if not self._iq_start("manual"):
                return {"ok": False, "code": "not_receiving", "msg": "接收机还没在收，没有东西可录"}
        else:
            self._iq_stop("stopped")
        return {"ok": True}

    # ---- 原始 I/Q：回放 ----
    def _op_replay(self, op):
        if op.get("off") is True:
            if self._replay:
                self._replay_stop()
            return {"ok": True}
        name = op.get("name")
        path, item = self.iq.path_of(name), self.iq.item(name)
        if not path or not item:
            return {"ok": False, "code": "bad_recording", "msg": "没有这段 I/Q"}
        if not self._replay:                         # 从一段换到另一段时，「回到哪儿」仍是最初那个模式
            self._replay_prev = self.mode if self.mode in RESTING else self._return_to
        self._close("released")                      # 放开真接收机；正在录的 I/Q 也就此收尾
        self._replay = dict(item, path=path)
        self.mode = "adsb"
        saved = item.get("channels") or []
        channels = chan.build(saved) if item.get("mode") == "air" else []
        if len(channels) == len(saved):              # 一一对应（存的时候就是一个载波一条）：把行号摆回去
            for c, row in zip(channels, saved):
                c["rows"] = list(row[3]) if len(row) > 3 and isinstance(row[3], list) else []
                if not c["rows"]:
                    c["manual"] = True               # 没有行号的是当时手动调出来的
        if channels:
            pick = next((c["id"] for c in channels if c["label"] == item.get("listen")), None)
            self._set_plan(channels, pick, item.get("airport_label") or "", item.get("airport"))
            self._return_to = "adsb"
        elif item.get("mode") == "air":              # 说明里没有频道表（不该发生）：调到中心旁边的格点上
            self.air = None
            self._op_tune({"hz": item["center_hz"] + chan.HALF_GRID})
            self._return_to = "adsb"
        self._retry_at = 0.0
        return {"ok": True}

    def _replay_stop(self):
        """回到实时接收：放开回放文件，按原来的模式重新打开真接收机。"""
        self._close("released")
        self._replay = self._replay_alone = None
        self.mode = self._replay_prev
        self._idle_since = self._idle_left = None
        self._retry_at = 0.0

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
                rf, gen, block = self._dspq.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if rf == "air":
                    mon = self.monitor
                    if mon is None or mon.gen != gen:
                        continue         # 重调谐之前排进来的，频率已经不对了
                    if mon.feed(block):
                        self._row(mon.center, self._rows.add(mon.center, mon.spectrum.power), True)
                elif self._rf == "adsb" and gen == self._gen and time.monotonic() - self._tuned_at > SETTLE_S:
                    power = self._plain.update(to_complex(block))
                    self._row(adsb.CENTER, self._plain_rows.add(adsb.CENTER, power), False)
            except Exception as e:       # 解调出错不该带走线程；记下来给状态条显示
                self._dsp_error = "%s: %s" % (type(e).__name__, e)

    def _row(self, center, row, air):
        """sdr-dsp 线程：攒够了一行频谱。话音模式下顺便找候选信号；有人在看才发帧。"""
        if row is None:
            return
        if air:
            # 门限就是静噪门限：候选的意思是「强到能开静噪的载波」，与指示灯一个标准
            self.tracker.update(cand.detect(row, center, self.cfg.get("squelch_db")), time.monotonic(), ROW_S)
        if self.watchers and self.on_spectrum:
            self._row_seq += 1
            self.on_spectrum(spectrum_frame(self._row_seq, center, row))

    def _candidates(self, now):
        """候选列表，并标出哪些其实是频道表里的频道（落在它的频率跟踪范围内）。"""
        rows = self.tracker.snapshot(now)
        chs = self.air["channels"] if self.air else []
        for r in rows:
            near = [c for c in chs if abs(r["hz"] - c["hz"]) <= (2_500 if c["spacing"] == "8.33" else 9_000)]
            r["ch"] = min(near, key=lambda c: abs(r["hz"] - c["hz"]))["id"] if near else None
        return rows

    OPS = {"mode": _op_mode, "home": _op_home, "set": _op_set, "channels": _op_channels,
           "listen": _op_listen, "window": _op_window, "tune": _op_tune, "drop": _op_drop,
           "record": _op_record, "iq": _op_iq, "replay": _op_replay}

    # ---- 采样块入口（采样源线程）----
    def _on_block(self, block):
        self._blocks += 1
        self._last_block = time.monotonic()
        with self._iq_lock:              # 不争用时只是几十纳秒；写盘在别的线程里
            self._ring.append((self._gen, block))
            self.iq.feed(block)
        rf = self._rf
        if rf == "adsb":
            dec = self.decoder
            if dec is not None:
                dec.feed(block)
            # 有人开着瀑布图才抽块去算频谱，6 块抽 1 块，与话音模式的行速率一致
            if self.watchers and self._blocks % ROW_BLOCKS == 0:
                try:
                    self._dspq.put_nowait((rf, self._gen, block))
                except queue.Full:       # 只是少画一行，不值得上报
                    pass
        elif rf == "air":
            try:
                self._dspq.put_nowait((rf, self._gen, block))
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
        rp = self._replay
        src = ReplaySource(rp["path"], rp["center_hz"], rp["name"]) if rp \
            else make_source(self.source_spec, ppm=self.cfg.get("ppm") or 0)
        try:
            src.open()
            gain = self.cfg.get("gain")
            start_gain = max(src.gains) if src.gains and gain == "auto" else (gain if gain != "auto" else 0)
            src.configure(adsb.CENTER, adsb.RATE, float(start_gain))
        except DeviceError as e:
            src.close()
            if rp:                       # 回放文件打不开：别在这儿反复重试，回到实时接收
                self._replay, self.mode, self.error = None, self._replay_prev, str(e)
                return
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
        self._rf = None
        self._drop_monitor()
        self._iq_stop("retune")
        if want == "adsb":
            self._gen += 1               # 作废排队中的旧块，见 _tune_window
            try:
                self.source.tune(adsb.CENTER)
            except DeviceError as e:
                self.error, self._lost = str(e), True
                return
            self._tuned_at = time.monotonic()
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
        self._iq_stop("closed")
        if self.source:
            self.source.close()          # 先停采样再关解码，免得往已关的管道里写
            self.source = None
        if self.decoder:
            self.decoder.stop(wait)      # 不等它收尾，见 adsb.ReadsbDecoder.stop
            self.decoder = None
        self._rf = None
        self._drop_monitor()
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
                # 回放出来的位置是过去的，不能算进「这台接收机最远收到哪儿」
                if a.get("seen_pos", 99) < 5 and not self._replay and self.outline.update(row[2], row[3]):
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
                "spectrum": {"watchers": self.watchers, "rows": self._row_seq},
                "rec": dict(self.store.stats(), on=bool(self.cfg.get("record"))),
                "iq": dict(self.iq.stats(), with_voice=bool(self.cfg.get("iq_with_voice")),
                           max_s=float(self.cfg.get("iq_max_s") or 60), active=self.iq.active()),
                "replay": {k: self._replay.get(k) for k in ("name", "center_hz", "duration_s", "mode", "airport_label",
                                                            "start")} if self._replay else None,
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
                    self._iq_tick(now)
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
                    if self.listeners > 0 or self._triggering():         # 开着自动录制就是有事在做，不算没人听
                        self._idle_since = self._idle_left = None
                    else:
                        self._idle_since = self._idle_since or now
                        self._idle_left = max(0, round(self.auto_return_s - (now - self._idle_since)))
                        if now - self._idle_since >= self.auto_return_s:
                            if self._replay:         # 没人听的回放没有意义，还占着接收机回不来
                                self._replay_stop()
                            else:
                                self._set_mode(self._return_to)
                if self._replay:                     # 回放是给人看的。页面都走了还放着，真接收机就一直回不来
                    self._replay_alone = None if self.pages else (self._replay_alone or now)
                    if self._replay_alone and now - self._replay_alone >= self.auto_return_s:
                        self._replay_stop()
                part = {}
                if self.source:
                    self._sync_rf()
                    self._auto_gain(now)
                    if self._rf == "adsb":
                        part = self._poll_decoder(now)
                    self._air_tick(now)
                    if self._rf == "air":
                        self._publish(candidates={"t": "candidates", "list": self._candidates(now)})
                self._publish(status=self._status(part))
        finally:
            # 退出时一定关设备，Bias-T 也在这里关掉。解码进程不等它收尾（要 3 秒）：
            # 管道一断它必然自己走，而且它不碰 USB。我们早走一步，文件锁和接收机
            # 就早一步让出来——用户退出后立刻重开，新的守护进程才不会撞上「已在运行」。
            self._close("released", wait=False)
            self.store.close()                       # 最后一段写完再走
            self.iq.close()

"""话音监听：一个射频窗口上的全部信号处理。

Monitor 活在 sdr-dsp 线程里，每来一块采样做三件事：
  1. 算一次整窗功率谱，给窗口里每个频道判一次「有没有载波」。活动指示灯和静噪
     用的是同一个量——灯亮就意味着切过去能听到东西，不会灯亮了却没声。
  2. 只对被收听的那一个频道跑解调。同窗口的其他频道只看不听，所以频率表再长
     开销也不涨。
  3. 把音频切成 20 毫秒一帧交出去。

引擎线程通过 listen() / set_squelch() 改它，靠的是整体替换不可变的小对象，
两个线程之间没有锁。
"""
import collections
import struct
import time

import numpy as np

from .dsp import AUDIO_RATE, to_complex
from .dsp.am import AmDemod, VoiceDetector
from .dsp.spectrum import Spectrum, Squelch

FRAME = AUDIO_RATE // 50                 # 20 毫秒 = 320 个采样点
AUDIO_FRAME = 1                          # 二进制帧的类型字节
LOOKBACK = 4                             # 「无话音时静音」开着时音频延后的块数，约 55 毫秒，见 feed
SETTLE_BLOCKS = 6                        # 重调谐后丢掉的块数，约 80 毫秒，等锁相环稳住


def audio_frame(seq, start, pcm):
    """帧头 4 字节：类型、标志（bit0 = 一次通话的第一帧）、序号。后面是 int16 小端。"""
    return struct.pack("<BBH", AUDIO_FRAME, 1 if start else 0, seq & 0xFFFF) + pcm.astype("<i2").tobytes()


class Monitor:
    def __init__(self, center_hz, channels, squelch_db, on_audio, voice_only=True):
        self.center = center_hz
        self.channels = [dict(c) for c in channels]
        self.on_audio = on_audio
        self.spectrum = Spectrum()
        self.squelch = {c["id"]: Squelch(squelch_db) for c in self.channels}
        self.offset = {c["id"]: c["hz"] - center_hz for c in self.channels}
        self.since = {}                  # 频道 id → 这次活动是什么时候开始的
        self.last_active = {}            # 频道 id → 最近一次有载波的时刻
        self.tuned = None                # (频道 id, AmDemod, VoiceDetector)，整体替换
        self.voice_only = voice_only     # 只在载波上有话音时出声
        self.voice = False               # 被收听的频道此刻有没有话音
        self.voice_at = None             # 最近一次判为有话音的时刻
        self.held = collections.deque()
        self.settle = SETTLE_BLOCKS
        self.gen = 0                     # 引擎给的调谐代号，用来认出重调谐之前排队的旧块
        self.seq = 0
        self.frames = 0
        self.pending = np.zeros(0, np.int16)
        self.talking = None              # 正在出声的频道 id
        self.first = False               # 下一帧是不是一次通话的第一帧

    # ---- 引擎线程 ----
    def listen(self, ident):
        if self.tuned and self.tuned[0] == ident:
            return
        ch = next((c for c in self.channels if c["id"] == ident), None)
        self.tuned = (ident, AmDemod(ch["hz"] - self.center, ch["spacing"]), VoiceDetector()) if ch else None

    def set_squelch(self, db):
        for s in self.squelch.values():
            s.open_db, s.close_db = db, db - 3.0
            # 开着的静噪本来要等电平掉到关门限以下才关。但用户把门限拖到当前电平之上，
            # 意思就是「这个强度的我不要听」，不该让回差替他留着
            if s.open and s.level is not None and s.level < db:
                s.open, s.lock, s.run = False, None, 0

    def snapshot(self):
        tuned = self.tuned
        ident, demod = tuned[:2] if tuned else (None, None)
        sq = self.squelch.get(ident)
        is_open = bool(sq and sq.open)
        return {"level": {str(i): None if s.level is None else round(float(s.level), 1)
                          for i, s in self.squelch.items()},
                "active": [i for i, s in self.squelch.items() if s.open],
                "open": is_open,
                "voice": bool(is_open and self.voice),
                "afc_hz": round(demod.afc_hz) if demod and is_open else None,
                "carrier_dbfs": round(demod.carrier_dbfs(), 1) if demod and is_open else None,
                "floor_dbfs": None if self.spectrum.floor_db() is None else round(self.spectrum.floor_db(), 1)}

    # ---- sdr-dsp 线程 ----
    def feed(self, block):
        if self.settle > 0:
            self.settle -= 1
            return
        x = to_complex(block)
        if self.spectrum.update(x) is None:
            return
        now = time.monotonic()
        tuned = self.tuned
        gate = False
        for c in self.channels:
            i = c["id"]
            sq = self.squelch[i]
            r = self.spectrum.channel(self.offset[i], c["spacing"], sq.lock)
            if r is None:
                continue
            was = sq.open
            is_open, present = sq.update(*r)
            if is_open:
                self.last_active[i] = now
                if not was:
                    self.since[i] = now
            if tuned and i == tuned[0]:
                gate = is_open and present
        if not tuned:
            return
        ident, demod, detector = tuned
        pcm = demod.process(x, gate)
        self.voice = detector.update(demod.ratio)
        if self.voice:
            self.voice_at = now
        wanted = self.squelch[ident].open
        if self.voice_only:
            # 判出「有话音」要几十毫秒，等判出来再放就吃掉了头一个音。所以音频统一延后
            # LOOKBACK 块：判定落在第 n 块时，放出去的是第 n−4 块，开头就保住了。
            wanted = wanted and self.voice
            self.held.append(pcm)
            pcm = self.held.popleft() if len(self.held) > LOOKBACK else None
        elif self.held:
            self.held.clear()
        if self.talking is not None and (self.talking != ident or not wanted):
            self._flush()                # 通话结束，或者换了频道
        if wanted and pcm is not None:
            if self.talking is None:
                self.talking, self.first = ident, True
            self._emit(pcm)

    def _emit(self, pcm):
        buf = np.concatenate([self.pending, pcm])
        while len(buf) >= FRAME:
            self._send(buf[:FRAME])
            buf = buf[FRAME:]
        self.pending = buf

    def _flush(self):
        """把不满一帧的尾巴补零发掉，下一次通话从整帧开始。"""
        if len(self.pending):
            buf = np.zeros(FRAME, np.int16)
            buf[:len(self.pending)] = self.pending
            self._send(buf)
        self.pending = np.zeros(0, np.int16)
        self.talking = None

    def _send(self, pcm):
        self.on_audio(audio_frame(self.seq, self.first, pcm))
        self.seq += 1
        self.frames += 1
        self.first = False

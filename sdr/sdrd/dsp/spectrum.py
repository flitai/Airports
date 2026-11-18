"""整窗功率谱，以及据此判断每个频道上有没有载波。

对应 SDR Lab 的 spectral.py 和 trigger.py:19-34 的 channel_excess。保留的是
「拿频道内的功率比上旁边的底噪」这个量。改动：
  * 分子取最强的一根谱线（载波），分母取整个信道带宽里的噪声，得到的是载噪比。
    它直接对应解调出来的话音有多干净：7 dB 勉强能听，15 dB 以上清楚。
    实测过另一种定义——载波比单独一格（586 Hz）的底噪高多少——在室内行不通：
    电脑和电源的杂散在一格里能高出 7–12 dB，一大片频道的指示灯会常亮，而这种
    强度的「信号」解调出来只有噪声。
  * 底噪取附近 ±58 kHz 内的中位数。中位数不怕里面混着几个邻道信号。
  * 最强谱线的位置顺带给出载波的实际偏差。

一次 FFT 4096 点，每块 32768 个采样点正好切成 8 段取平均：分辨率 586 Hz，
每块（13.65 毫秒）出一份。静噪的时序就建立在「块」这个单位上。

瀑布图用的是同一份谱：Rows 把 6 块攒成一行（每秒约 12 行），spectrum_frame 把一行
量化成 8 位发给页面。不为了画图另算一遍 FFT。
"""
import struct

import numpy as np

from . import RATE

NFFT = 4096
BIN_HZ = RATE / NFFT
# 瀑布图只画中间 80%：两边是抗混叠滤波器的滚降区，底噪在那里塌下去，画出来像是
# 「这一段很安静」，其实是接收机看不见。与 channels.USABLE（1.92 MHz）是同一个范围。
ROW_BINS = 3276
ROW_LO = NFFT // 2 - ROW_BINS // 2
ROW_SPAN_HZ = round(ROW_BINS * BIN_HZ)
ROW_BLOCKS = 6                           # 6 块一行：73 块/秒 ÷ 6 ≈ 12.2 行/秒
ROW_S = ROW_BLOCKS * 32768 / RATE        # 一行代表多长时间
SPECTRUM_FRAME = 2                       # 二进制帧的类型字节（1 是音频，见 air.py）
DB_MIN, DB_STEP = -120.0, 0.5            # 8 位量化：0 = −120 dBFS，每级 0.5 dB，到 +7.5 dBFS
_REF_BINS = 100                          # 底噪参考：两侧各 100 格，约 ±58.6 kHz
_ENBW_HZ = 1.5 * BIN_HZ                  # 汉宁窗下一格的等效噪声带宽
# 解调用的信道带宽（见 am.CHANNEL_BW_HZ，7.6 kHz）比一格宽多少 dB。写死是为了不反过来依赖 am
_CHANNEL_DB = 10 * np.log10(7_600 / _ENBW_HZ)


class Spectrum:
    def __init__(self):
        w = np.hanning(NFFT).astype(np.float32)
        self.window = w
        self.norm = 1.0 / float(w.sum()) ** 2            # 满幅正弦读作 0 dB
        self.power = None                                # 最近一块的功率谱，零频在正中

    def update(self, x):
        segs = len(x) // NFFT
        if not segs:
            return None
        f = np.fft.fft(x[:segs * NFFT].reshape(segs, NFFT) * self.window, axis=1)
        p = (f.real ** 2 + f.imag ** 2).mean(axis=0) * self.norm
        self.power = np.fft.fftshift(p)
        return self.power

    def channel(self, offset_hz, spacing, lock_hz=None):
        """→ (载噪比 dB, 载波相对标称频率的偏差 Hz)。频道不在窗口里返回 None。

        没锁定时在频率跟踪允许的整个范围里找最强的谱线：25 kHz 频道 ±9 kHz（欧洲的
        偏置载波最多偏 ±7.5 kHz，再给晶振误差留一点），8.33 kHz 频道 ±2.5 kHz。
        给了 lock_hz（上次找到的偏差）就只看它左右各两格。范围越宽，纯噪声里碰巧
        冒出一根高谱线的机会越大。
        """
        p = self.power
        k = int(round(offset_hz / BIN_HZ)) + NFFT // 2
        if p is None or k - _REF_BINS < 0 or k + _REF_BINS >= NFFT:
            return None
        if lock_hz is None:
            reach = int((2_500 if spacing == "8.33" else 9_000) / BIN_HZ)
            lo = k - reach
        else:
            reach = 2
            lo = int(round((offset_hz + lock_hz) / BIN_HZ)) + NFFT // 2 - reach
        band = p[lo:lo + 2 * reach + 1]
        i = int(band.argmax())
        floor = float(np.median(p[k - _REF_BINS:k + _REF_BINS + 1]))
        db = 10 * np.log10(max(float(band[i]), 1e-20) / max(floor, 1e-20)) - _CHANNEL_DB
        return db, (lo + i - NFFT // 2) * BIN_HZ - offset_hz

    def floor_db(self):
        """整窗底噪（dBFS/格），给界面显示接收状况用。"""
        return None if self.power is None else float(10 * np.log10(max(float(np.median(self.power)), 1e-20)))


class Rows:
    """把逐块的功率谱攒成瀑布图的行：按线性功率取平均，只留可用的中间一段。

    取平均而不是取最大：一次通话持续几秒，平均不会漏掉它，而噪声被压平之后
    弱信号才从底噪里显出来。中心频率变了，攒到一半的那行就不要了——前后两半
    不是同一段频谱。
    """
    def __init__(self, blocks=ROW_BLOCKS):
        self.blocks = blocks
        self.center = None
        self.acc, self.n = None, 0

    def add(self, center_hz, power):
        """power 是 Spectrum.power（4096 格）。攒够了返回一行（ROW_BINS 格），否则 None。"""
        if center_hz != self.center:
            self.center, self.acc, self.n = center_hz, None, 0
        part = power[ROW_LO:ROW_LO + ROW_BINS]
        if self.acc is None:
            self.acc = part.astype(np.float64)
        else:
            self.acc += part
        self.n += 1
        if self.n < self.blocks:
            return None
        row, self.acc, self.n = self.acc / self.n, None, 0
        return row


def quantize(row):
    db = 10 * np.log10(np.maximum(row, 1e-20))
    return np.clip(np.rint((db - DB_MIN) / DB_STEP), 0, 255).astype(np.uint8)


def spectrum_frame(seq, center_hz, row):
    """一行频谱 → 二进制帧。帧头 16 字节（小端）：

        0      类型 = 2
        1      标志（保留）
        2–3    行序号
        4–7    中心频率 Hz
        8–11   跨度 Hz
        12     整行底噪（中位数），与数据同一量化
        13     保留
        14–15  格数
        16…    每格一字节，dBFS = 值 × DB_STEP + DB_MIN

    每行自带中心频率和跨度：重调谐前后排在路上的行，页面都知道该画在哪儿。
    """
    q = quantize(row)
    return struct.pack("<BBHIIBBH", SPECTRUM_FRAME, 0, seq & 0xFFFF, int(center_hz), ROW_SPAN_HZ,
                       int(np.median(q)), 0, len(q)) + q.tobytes()


class Squelch:
    """一个频道的静噪。喂给它的是 Spectrum.channel 每块的结果。

    开：连续 open_blocks 块高于开门限。默认 3 块 ≈ 41 毫秒，开得快是为了不吃掉
        通话的头一个字。开的同时记下载波的位置（lock），之后只盯着那里看。
    关：累计 close_blocks 块没有载波。默认 30 块 ≈ 410 毫秒，关得慢是为了话音间隙
        里载波瞬时衰落时不把一句话切碎。
    update 的第二个返回值是「这一块有没有载波」，解调那边按它逐块静音：静噪挂着
    的那 400 毫秒里不能把噪声放出来。单独一块越过关门限不算数，要连着两块。
    """
    def __init__(self, open_db=7.0, hysteresis_db=3.0, open_blocks=3, close_blocks=30):
        self.open_db, self.close_db = open_db, open_db - hysteresis_db
        self.open_blocks, self.close_blocks = open_blocks, close_blocks
        self.open = False
        self.lock = None                 # 载波相对标称频率的偏差，开着时才有
        self.run = self.hits = self.misses = 0
        self.level = None                # 平滑过的电平，只用于显示

    def update(self, db, deviation_hz):
        """返回 (静噪是否开着, 这一块有没有载波)。"""
        self.level = db if self.level is None else self.level + 0.3 * (db - self.level)
        if not self.open:
            self.run = self.run + 1 if db >= self.open_db else 0
            if self.run >= self.open_blocks:
                self.open, self.lock = True, deviation_hz
                self.run, self.hits, self.misses = 0, 2, 0
            return self.open, self.open
        if db >= self.close_db:
            self.hits += 1
            self.lock = deviation_hz     # 跟着载波慢慢漂
        else:
            self.hits = 0
        present = self.hits >= 2
        self.misses = 0 if present else self.misses + 1
        if self.misses >= self.close_blocks:
            self.open, self.lock = False, None
        return self.open, present

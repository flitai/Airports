"""有理数倍重采样：2.4 MS/s → 2.1 MS/s（×7/8），给 VDL2 解码器用。

dumpvdl2 只吃 105 kHz 整数倍的采样率（10500 波特 × 每符号 10 点 × 过采样倍数），
2.4 MS/s 不是。2.1 MS/s = 105 kHz × 20 是离得最近的一档。

多相结构：概念上先插 6 个零升到 16.8 MS/s、低通、再 8 取 1；实际只算留下来的那些
输出。每 8 个输入出 7 个输出，第 r 个输出用原型滤波器的第 r 相：

    y[7q + r] = 7 · Σ_t h[r + 7t] · x[8q + r − t]

7 个输出用到的输入挤在同一段 T+6 点的窗口里，所以整块可以写成「每隔 8 点取一个
窗口，乘一个 7 行的矩阵」，一次矩阵乘法出一整块。

通带只保 ±0.6 MHz：VDL2 的窗口中心是固定的（见 channels.VDL2_CENTER），数据链
频道都在这个范围里。阻带从 2.1 MHz − 0.6 MHz 起算——再往里的混叠落在通带之外，
不碍事。过渡带因此很宽，每相只要十个抽头。
"""
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from . import RATE, lowpass, taps_for

S16_SCALE = 16384.0      # 满幅的一半。削顶的输入经滤波会过冲到 1.25 倍，乘 32767 就回绕了


class Resampler:
    """带状态的多相重采样器。块怎么切，输出都一样（浮点舍入的最后一位除外）。"""

    def __init__(self, up=7, down=8, rate=RATE, pass_hz=600_000, atten_db=60.0):
        self.up, self.down = up, down
        self.out_rate = rate * up // down
        stop_hz = self.out_rate - pass_hz
        n = taps_for(stop_hz - pass_hz, rate * up, atten_db)
        self.per = -(-n // up)                           # 每相几个抽头
        h = lowpass(self.per * up, (pass_hz + stop_hz) / 2, rate * up, atten_db).astype(np.float64) * up
        # 第 r 行是第 r 个输出要乘的系数，摆在 per + up − 1 点的窗口里。窗口第 w 点对应 t = r + per − 1 − w
        self.width = self.per + up - 1
        H = np.zeros((up, self.width), np.float64)
        for r in range(up):
            for t in range(self.per):
                H[r, r + self.per - 1 - t] = h[r + up * t]
        self.H = np.ascontiguousarray(H.T.astype(np.float32))    # (窗口宽, 相数)
        self.tail = np.zeros((self.per - 1, 2), np.float32)      # 上一块留下的尾巴，I、Q 两列

    def process(self, x):
        """complex64 → complex64，采样率乘以 up/down。"""
        buf = np.concatenate([self.tail, np.ascontiguousarray(x, np.complex64).view(np.float32).reshape(-1, 2)])
        k = (len(buf) - self.width) // self.down + 1 if len(buf) >= self.width else 0
        if k <= 0:
            self.tail = buf
            return np.zeros(0, np.complex64)
        # 窗口：(组数, 2, 窗口宽)。乘出来是 (组数, 2, 相数)，换成 (组数, 相数, 2) 就是按时间排好的 I/Q
        win = sliding_window_view(buf, self.width, axis=0)[::self.down][:k]
        y = np.ascontiguousarray((win @ self.H).transpose(0, 2, 1))
        self.tail = buf[k * self.down:].copy()
        return y.reshape(-1).view(np.complex64)


def to_s16(y):
    """complex64（满幅 ±1）→ 16 位小端交错 I/Q 的字节串。"""
    a = np.ascontiguousarray(y, np.complex64).view(np.float32) * S16_SCALE
    return np.clip(np.rint(a), -32768, 32767).astype("<i2").tobytes()

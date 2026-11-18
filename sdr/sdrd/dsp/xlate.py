"""移频 + 低通 + 抽取，一步做完。

SDR Lab 的做法（processors.py:129-133）是先用全速率的复指数把整块采样搬到零频，
再滤波、再抽取：2.4 MS/s 下每秒 240 万次复数乘法和一条 8 阶 IIR，绝大部分算出来
的点随后就被抽取扔掉了。

这里把搬移折进滤波器的抽头里（抽头乘上复指数，低通变成以目标频率为中心的带通），
并且只在要留下的那些时刻求值——每 50 个输入点算一个输出。搬移剩下的那部分是每个
输出点乘一个相位因子，量只有原来的五十分之一。
"""
import numpy as np

from . import RATE, lowpass, taps_for


class Xlate:
    def __init__(self, offset_hz, decim=50, rate=RATE, pass_hz=14_000, atten_db=75.0):
        self.decim, self.rate = decim, rate
        out_rate = rate / decim
        # 通带 ±14 kHz = 频率跟踪的 ±9 kHz 加信道滤波的 ±5 kHz。
        # 抽取后落回通带的最近镜像在 out_rate − pass_hz，过渡带就夹在这两者之间
        stop_hz = out_rate - pass_hz
        n = taps_for(stop_hz - pass_hz, rate, atten_db)
        h = lowpass(n, (pass_hz + stop_hz) / 2, rate, atten_db)
        self.w = 2 * np.pi * offset_hz / rate
        m = np.arange(n)
        # 滑窗是按时间正序取的，卷积要的是逆序；低通抽头对称，所以只需把复指数配成正序
        self.taps = (h * np.exp(-1j * self.w * m)).astype(np.complex64)
        self.tail = np.zeros(0, np.complex64)
        self.phase = 0.0                 # tail[0] 这个采样点对应的搬移相位

    def process(self, x):
        """x: 2.4 MS/s 的 complex64 → 抽取后的 complex64，目标频率落在零频。"""
        buf = np.concatenate([self.tail, x])
        n, d = len(self.taps), self.decim
        count = (len(buf) - n) // d + 1 if len(buf) >= n else 0
        if count <= 0:
            self.tail = buf
            return np.zeros(0, np.complex64)
        win = np.lib.stride_tricks.sliding_window_view(buf, n)[::d][:count]
        y = np.dot(win, self.taps)                       # 对这种带步长的视图，dot 比 @ 快三倍
        step = self.w * d
        y *= np.exp(-1j * (self.phase + step * np.arange(count))).astype(np.complex64)
        self.phase = float((self.phase + step * count) % (2 * np.pi))
        self.tail = buf[count * d:]
        return y

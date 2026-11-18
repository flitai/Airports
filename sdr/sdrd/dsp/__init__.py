"""信号处理：只依赖 numpy，全部是带状态、可按任意块长喂入的流式处理。

全程固定 2.4 MS/s。换模式、换窗口都只是重调谐，采样率不动：ADS-B 解码器把这个
数写死在源码里，话音这边 2.4 MS/s ÷ 50 正好是 48 kHz，再 ÷ 3 是 16 kHz。
"""
import numpy as np

RATE = 2_400_000
AUDIO_RATE = 16_000


def to_complex(block):
    """接收机给的 8 位无符号 I/Q 字节 → complex64，满幅约为 ±1。"""
    a = np.frombuffer(block, np.uint8).astype(np.float32)
    a -= 127.5
    a *= 1 / 127.5
    return a.view(np.complex64)


def lowpass(ntaps, cutoff_hz, rate, atten_db=60.0):
    """凯泽窗 sinc 低通，直流增益为 1。cutoff 是 −6 dB 点。"""
    beta = 0.1102 * (atten_db - 8.7) if atten_db > 50 else \
        0.5842 * (atten_db - 21) ** 0.4 + 0.07886 * (atten_db - 21)
    n = np.arange(ntaps) - (ntaps - 1) / 2
    h = np.sinc(2 * cutoff_hz / rate * n) * np.kaiser(ntaps, beta)
    return (h / h.sum()).astype(np.float32)


def taps_for(transition_hz, rate, atten_db=60.0):
    """达到给定阻带衰减和过渡带宽所需的抽头数（凯泽公式），取奇数。"""
    n = int(np.ceil((atten_db - 8) / (2.285 * 2 * np.pi * transition_hz / rate))) + 1
    return n | 1


class Fir:
    """带状态的 FIR，可选整数抽取。块怎么切，输出都和一次喂完逐点相同。"""

    def __init__(self, taps, decim=1):
        self.taps = np.asarray(taps)
        self.decim = decim
        self.tail = None
        self.skip = 0                    # 下一块里第一个要留下的输出的下标

    def process(self, x):
        n = len(self.taps) - 1
        if self.tail is None:
            self.tail = np.zeros(n, x.dtype)
        buf = np.concatenate([self.tail, x])
        self.tail = buf[len(buf) - n:]
        y = np.convolve(buf, self.taps, "valid")         # 长度正好是 len(x)
        if self.decim == 1:
            return y
        out = y[self.skip::self.decim]
        self.skip = (self.skip - len(y)) % self.decim
        return out

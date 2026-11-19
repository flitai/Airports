"""2.4 → 2.1 MS/s 重采样器。"""
import time
import unittest

import numpy as np

from sdrd.dsp import RATE, to_complex
from sdrd.dsp.resample import S16_SCALE, Resampler, to_s16

OUT = 2_100_000
BLOCK = 32768


def tone(hz, n, amp=0.5, rate=RATE):
    return (amp * np.exp(2j * np.pi * hz * np.arange(n) / rate)).astype(np.complex64)


def peak(y, rate=OUT):
    """→ (最强谱线的频率, 它的幅度)。加窗后按相干增益归一，幅度就是单音的幅度。"""
    w = np.hanning(len(y))
    spec = np.abs(np.fft.fft(y * w)) / w.sum()
    k = int(np.argmax(spec))
    return np.fft.fftfreq(len(y), 1 / rate)[k], spec[k]


def level_at(y, hz, rate=OUT):
    w = np.hanning(len(y))
    spec = np.abs(np.fft.fft(y * w)) / w.sum()
    f = np.fft.fftfreq(len(y), 1 / rate)
    return spec[np.abs(f - hz) < 3 * rate / len(y)].max()


class Shape(unittest.TestCase):
    def test_rate_and_count(self):
        r = Resampler()
        self.assertEqual(r.out_rate, OUT)
        y = r.process(tone(100_000, BLOCK))
        self.assertEqual(len(y), BLOCK * 7 // 8)
        self.assertEqual(len(r.process(tone(100_000, BLOCK))), BLOCK * 7 // 8)

    def test_odd_lengths_lose_nothing(self):
        r, x = Resampler(), tone(50_000, 8 * 5000)
        got = sum(len(r.process(x[a:b])) for a, b in ((0, 3), (3, 4), (4, 1001), (1001, 20_000), (20_000, 40_000)))
        # 最后不满一个窗口的几点留在状态里，等下一块来了再出
        self.assertLessEqual(len(x) * 7 // 8 - got, 7 * 2)

    def test_split_invariant(self):
        rng = np.random.default_rng(1)
        x = (rng.normal(0, .2, 4 * BLOCK) + 1j * rng.normal(0, .2, 4 * BLOCK)).astype(np.complex64)
        whole = Resampler().process(x)
        r, parts = Resampler(), []
        for a, b in ((0, 7), (7, 5000), (5000, 5001), (5001, 70_000), (70_000, 4 * BLOCK)):
            parts.append(r.process(x[a:b]))
        got = np.concatenate(parts)
        # 矩阵乘法走 BLAS，块长不同舍入的最后一位可能不同，所以不要求逐位相同；
        # 要求的是量化成 16 位之后最多差一个最低位
        self.assertEqual(len(got), len(whole))
        a = np.frombuffer(to_s16(whole), "<i2").astype(np.int32)
        b = np.frombuffer(to_s16(got), "<i2").astype(np.int32)
        self.assertLessEqual(int(np.abs(a - b).max()), 1)


class Response(unittest.TestCase):
    def run_tone(self, hz, n=16 * BLOCK):
        y = Resampler().process(tone(hz, n))
        return y[2000:]                                  # 丢掉滤波器起振的那一小段

    def test_passband_keeps_frequency_and_level(self):
        # VDL2 的数据链频道离窗口中心 +312.5 到 +562.5 kHz
        for hz in (-600_000, -312_500, 0, 150_000, 312_500, 437_500, 562_500, 600_000):
            y = self.run_tone(hz)
            f, _ = peak(y)
            a = float(np.sqrt(np.mean(np.abs(y) ** 2)))  # 单音的幅度；不用谱峰，免得栅栏效应掺进来
            self.assertLess(abs(f - hz), 2 * OUT / (16 * BLOCK * 7 // 8), hz)
            self.assertLess(abs(20 * np.log10(a / 0.5)), 0.2, hz)       # 起伏不超过 0.2 dB

    def test_images_and_aliases_rejected(self):
        # 输入里 ±1.05 MHz 以外的东西在新采样率下会折回来。折进通带的那些要压到 45 dB 以下
        for hz in (1_150_000, -1_150_000, 1_190_000):
            y = self.run_tone(hz)
            alias = hz - np.sign(hz) * OUT               # 折回来落在的频率
            self.assertLessEqual(abs(alias), 1_000_000)
            if abs(alias) <= 600_000:                    # 只在乎落进通带的
                self.assertLess(20 * np.log10(level_at(y, alias) / 0.5 + 1e-12), -45, hz)
        # 落在通带里的单音，别处不该冒出镜像
        y = self.run_tone(437_500)
        w = np.hanning(len(y))
        spec = np.abs(np.fft.fft(y * w)) / w.sum()
        f = np.fft.fftfreq(len(y), 1 / OUT)
        spur = spec[np.abs(f - 437_500) > 20_000].max()
        self.assertLess(20 * np.log10(spur / 0.5), -45)

    def test_stopband_alias_into_passband(self):
        # 2.1 MHz − 0.6 MHz = 1.5 MHz 起是阻带：这里的东西正好折进通带，必须压住。
        # 2.4 MS/s 的输入本身装不下 1.5 MHz，所以这一条在插零后的速率上直接看原型滤波器
        r = Resampler()
        h = np.zeros(r.per * r.up)
        for p in range(r.up):
            for t in range(r.per):
                h[p + r.up * t] = r.H[p + r.per - 1 - t, p]
        spec = np.abs(np.fft.rfft(h, 1 << 16)) / r.up
        f = np.fft.rfftfreq(1 << 16, 1 / (RATE * r.up))
        self.assertLess(20 * np.log10(spec[f >= 1_500_000].max()), -55)
        self.assertLess(abs(20 * np.log10(spec[f <= 600_000].min())), 0.2)


class Output(unittest.TestCase):
    def test_full_scale_does_not_wrap(self):
        # 削顶的输入：方波一样在两个极值之间跳，滤波后会过冲
        raw = np.tile(np.array([255, 255, 255, 255, 0, 0, 0, 0], np.uint8), BLOCK // 4).tobytes()
        y = Resampler().process(to_complex(raw))
        s = np.frombuffer(to_s16(y), "<i2")
        self.assertEqual(len(s), 2 * len(y))
        # 没回绕：相邻两点不会从正的极大突然跳到负的极大
        self.assertLess(int(np.abs(np.diff(s[0::2].astype(np.int32))).max()), 60_000)
        self.assertLessEqual(int(np.abs(s.astype(np.int32)).max()), 32768)

    def test_scale(self):
        y = Resampler().process(tone(0, 4 * BLOCK, amp=0.5))
        s = np.frombuffer(to_s16(y), "<i2")[0::2][1000:]
        self.assertLess(abs(float(np.median(s)) - 0.5 * S16_SCALE), 2)

    def test_faster_than_real_time(self):
        rng = np.random.default_rng(2)
        raw = rng.integers(0, 256, 2 * BLOCK, np.uint8).tobytes()
        r = Resampler()
        for _ in range(5):
            to_s16(r.process(to_complex(raw)))
        n, t0 = 200, time.perf_counter()
        for _ in range(n):
            to_s16(r.process(to_complex(raw)))
        spent = time.perf_counter() - t0
        self.assertGreater(n * BLOCK / RATE / spent, 3)  # 至少 3 倍实时


if __name__ == "__main__":
    unittest.main()

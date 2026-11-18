"""解调链：全部用合成信号，不需要接收机。"""
import time
import unittest

import numpy as np

from sdrd.dsp import AUDIO_RATE, RATE, Fir, lowpass, to_complex
from sdrd.dsp.am import AmDemod, VoiceDetector
from sdrd.dsp.spectrum import Spectrum, Squelch
from sdrd.dsp.xlate import Xlate
from sdrd.synth import AirBand, Station

BLOCK = 32768                            # 采样点；接收机一次回调给的就是这么多


def am(offset_hz, tone_hz, seconds, amp=0.05, depth=0.6, noise=0.004, extra=(), seed=1):
    """浮点的调幅信号：载波在 offset_hz，另可叠加 (偏移, 单音, 幅度) 的邻道。"""
    n = int(RATE * seconds)
    t = np.arange(n) / RATE
    x = amp * (1 + depth * np.cos(2 * np.pi * tone_hz * t)) * np.exp(2j * np.pi * offset_hz * t)
    for off, tone, a in extra:
        x = x + a * (1 + 0.6 * np.cos(2 * np.pi * tone * t)) * np.exp(2j * np.pi * off * t)
    rng = np.random.default_rng(seed)
    return (x + noise * (rng.normal(size=n) + 1j * rng.normal(size=n))).astype(np.complex64)


def demodulate(x, offset_hz, spacing="25", block=BLOCK):
    """照引擎的做法跑一遍：频谱判静噪，静噪开着才出声。返回 (音频, 每块的记录)。"""
    demod, spec, sq, out, log = AmDemod(offset_hz, spacing), Spectrum(), Squelch(), [], []
    for i in range(0, len(x) - block + 1, block):
        seg = x[i:i + block]
        spec.update(seg)
        db, dev = spec.channel(offset_hz, spacing, sq.lock)
        is_open, present = sq.update(db, dev)
        out.append(demod.process(seg, is_open and present))
        log.append((db, is_open, demod.afc_hz))
    return np.concatenate(out), log


def strength(audio, hz):
    y = audio / 32767.0
    t = np.arange(len(y)) / AUDIO_RATE
    return abs(np.mean(y * np.exp(-2j * np.pi * hz * t)))


class Filters(unittest.TestCase):
    def test_fir_is_block_size_invariant(self):
        rng = np.random.default_rng(0)
        x = rng.normal(size=5000).astype(np.float32)
        taps = lowpass(63, 3000, 48000)
        whole = Fir(taps, 3).process(x)
        f = Fir(taps, 3)
        parts = np.concatenate([f.process(x[i:i + 677]) for i in range(0, len(x), 677)])
        np.testing.assert_allclose(parts, whole, atol=1e-6)

    def test_xlate_is_block_size_invariant_and_moves_the_carrier_to_dc(self):
        x = am(300_000, 1000, 0.1, depth=0)
        whole = Xlate(300_000).process(x)
        f = Xlate(300_000)
        parts = np.concatenate([f.process(x[i:i + 10007]) for i in range(0, len(x), 10007)])
        np.testing.assert_allclose(parts, whole, atol=1e-5)
        settled = whole[50:]
        self.assertAlmostEqual(float(np.abs(settled.mean())), 0.05, delta=0.002)   # 幅度不变，频率归零

    def test_xlate_rejects_what_would_alias_into_the_channel(self):
        # 抽取到 48 kHz 后，偏 48 kHz 的信号会正好叠到零频上：必须在抽取前滤掉
        for off in (48_000, -48_000, 96_000, 36_000, -62_000):
            y = Xlate(300_000).process(am(300_000 + off, 1000, 0.05, amp=0.5, noise=0))[50:]
            self.assertLess(20 * np.log10(np.abs(y).max() / 0.5), -65, off)


class Demod(unittest.TestCase):
    def test_tone_comes_out_at_1khz_despite_7khz_carrier_error(self):
        audio, log = demodulate(am(300_000 - 7_000, 1000, 1.5), 300_000)
        self.assertAlmostEqual(len(audio) / AUDIO_RATE, 1.5, delta=0.02)
        tail = audio[AUDIO_RATE // 2:].astype(float)
        peak = np.argmax(np.abs(np.fft.rfft(tail * np.hanning(len(tail))))) * AUDIO_RATE / len(tail)
        self.assertAlmostEqual(peak, 1000, delta=5)
        self.assertAlmostEqual(log[-1][2], -7000, delta=20)               # 频率跟踪找到了真实载波
        # 调制度 60% 的单音：有效值约 0.6/√2，不随载波强弱变化
        self.assertAlmostEqual(float(np.sqrt(np.mean((tail / 32767) ** 2))), 0.42, delta=0.04)

    def test_loudness_does_not_depend_on_signal_strength(self):
        rms = []
        for amp in (0.2, 0.02):
            audio, _ = demodulate(am(-450_000, 1000, 0.8, amp=amp, noise=0.0005), -450_000)
            rms.append(np.sqrt(np.mean((audio[AUDIO_RATE // 4:] / 32767.0) ** 2)))
        self.assertAlmostEqual(rms[0], rms[1], delta=0.02)

    def test_squelch_timing(self):
        # 0.3 秒噪声，0.6 秒载波，0.8 秒噪声
        x = np.concatenate([am(0, 0, 0.3, amp=0), am(200_000, 1000, 0.6), am(0, 0, 0.8, amp=0, seed=3)])
        audio, log = demodulate(x, 200_000)
        opened = [i for i, (_, is_open, _) in enumerate(log) if is_open]
        block_s = BLOCK / RATE
        self.assertAlmostEqual(opened[0] * block_s, 0.3 + 0.041, delta=0.03)      # 约 40 毫秒后开
        self.assertAlmostEqual(opened[-1] * block_s, 0.9 + 0.41, delta=0.05)      # 约 400 毫秒后关
        self.assertTrue(all(db < 7 for db, _, _ in log[:20]))                      # 纯噪声够不着门限
        before = audio[:int(0.28 * AUDIO_RATE)]
        after = audio[int(0.95 * AUDIO_RATE):]
        self.assertEqual(int(np.abs(before).max()), 0)
        # 载波消失后静噪还要挂 400 毫秒，但这段不能把噪声放出来
        self.assertLess(int(np.abs(after).max()), 300)

    def test_noise_alone_never_opens(self):
        _, log = demodulate(am(0, 0, 3.0, amp=0), 123_456)
        self.assertFalse(any(is_open for _, is_open, _ in log))

    def test_adjacent_channel_40db_stronger_does_not_capture(self):
        # 由 SDR Lab 的 test_am_selectivity 移植：邻道强 40 dB，听到的仍应是本频道的 1 kHz
        x = am(100_000, 1000, 1.0, amp=0.003, noise=0.0001,
               extra=[(125_000, 1700, 0.3), (75_000, 1700, 0.3)])
        audio, _ = demodulate(x, 100_000)
        tail = audio[AUDIO_RATE // 2:]
        self.assertGreater(strength(tail, 1000), 0.2)
        self.assertLess(strength(tail, 1700) / strength(tail, 1000), 0.02)

    def test_833_channel_rejects_its_neighbour(self):
        x = am(-200_000 + 1_500, 1000, 1.0, amp=0.02, extra=[(-200_000 + 8_333, 1700, 0.2)])
        audio, log = demodulate(x, -200_000, "8.33")
        tail = audio[AUDIO_RATE // 2:]
        self.assertAlmostEqual(log[-1][2], 1500, delta=20)
        self.assertLess(strength(tail, 1700) / strength(tail, 1000), 0.02)

    def voiced_fraction(self, x, offset_hz):
        """跑一遍解调，返回「判为有话音」的块占多少（跳过开头半秒的建立过程）。"""
        demod, det, flags = AmDemod(offset_hz), VoiceDetector(hang_blocks=1), []
        for i in range(0, len(x) - BLOCK + 1, BLOCK):
            demod.process(x[i:i + BLOCK], True)
            flags.append(det.update(demod.ratio))
        return float(np.mean(flags[36:]))

    def test_voice_detector_tells_modulation_from_bare_carrier(self):
        # 载噪比约 9 dB：和室内实测到的真实信号一个量级
        weak = dict(amp=0.02, noise=0.0035)
        self.assertGreater(self.voiced_fraction(am(150_000, 1000, 2.0, depth=0.6, **weak), 150_000), 0.95)
        self.assertEqual(self.voiced_fraction(am(150_000, 1000, 2.0, depth=0.0, **weak), 150_000), 0.0)
        # 强的空载波同样不算；纯噪声也不算
        self.assertEqual(self.voiced_fraction(am(150_000, 1000, 2.0, depth=0.0), 150_000), 0.0)
        self.assertEqual(self.voiced_fraction(am(0, 0, 2.0, amp=0), 150_000), 0.0)

    def test_voice_detector_ignores_a_carrier_that_wobbles(self):
        # 开关电源、时钟这类杂散的频率是抖的（实测 125.000 上那一根抖 ±500 Hz）。
        # 载波一抖，同相和正交两路一起乱，不能被当成话音。
        n = int(RATE * 2.0)
        t = np.arange(n) / RATE
        phase = 2 * np.pi * 150_000 * t + (500 / 40) * np.sin(2 * np.pi * 40 * t)
        rng = np.random.default_rng(5)
        x = (0.02 * np.exp(1j * phase) + 0.0035 * (rng.normal(size=n) + 1j * rng.normal(size=n))).astype(np.complex64)
        self.assertLess(self.voiced_fraction(x, 150_000), 0.05)

    def test_voice_detector_holds_through_pauses_between_words(self):
        det = VoiceDetector(hang_blocks=73)
        for _ in range(10):
            det.update(4.0)
        self.assertTrue(det.voice)
        self.assertTrue(all(det.update(1.0) for _ in range(60)))          # 停顿 0.8 秒：不掐
        self.assertFalse(any([det.update(1.0) for _ in range(40)][-5:]))  # 再往后才算说完
        det.update(4.0)
        self.assertTrue(det.update(0.0))                                   # 载波瞬时衰落：不当成说完了

    def test_chain_runs_at_least_five_times_real_time(self):
        x = am(300_000, 1000, 2.0)
        demodulate(x[:BLOCK * 4], 300_000)                                # 预热，别把首次导入算进去
        t0 = time.perf_counter()
        demodulate(x, 300_000)
        self.assertGreater(2.0 / (time.perf_counter() - t0), 5)


class Synthetic(unittest.TestCase):
    def test_station_spec(self):
        s = Station.parse("118.5@-7000~4/2*700")
        self.assertEqual((s.hz, s.offset_hz, s.on_s, s.off_s, s.tone_hz), (118.5e6, -7000, 4, 2, 700))
        self.assertEqual(Station.parse("127.6").off_s, 0)
        self.assertEqual(Station.parse("125.0%0").depth, 0)
        with self.assertRaises(ValueError):
            Station.parse("tower")

    def test_8bit_synthetic_station_is_seen_where_it_should_be(self):
        band = AirBand([Station(118.5e6, offset_hz=-7000)])
        spec = Spectrum()
        spec.update(to_complex(band.block(BLOCK, 118_287_500)))
        db, dev = spec.channel(118_500_000 - 118_287_500, "25")
        self.assertGreater(db, 30)
        self.assertAlmostEqual(dev, -7000, delta=600)                     # 分辨率是一格 586 Hz
        self.assertLess(spec.channel(118_600_000 - 118_287_500, "25")[0], 7)


if __name__ == "__main__":
    unittest.main()

"""瀑布图的行和候选信号检测。全部用合成的话音台，不需要接收机。"""
import struct
import unittest

import numpy as np

from sdrd import synth
from sdrd.dsp import candidates as cand
from sdrd.dsp import to_complex
from sdrd.dsp.spectrum import (BIN_HZ, DB_MIN, DB_STEP, NFFT, ROW_BINS, ROW_BLOCKS, ROW_SPAN_HZ,
                               SPECTRUM_FRAME, Rows, Spectrum, quantize, spectrum_frame)

CENTER = 119_012_500                     # 希思罗塔台那个窗口的中心，落在半格上


def rows_of(stations, n=1, center=CENTER):
    """让这些合成台发射一阵，返回 n 行瀑布图（线性功率）。"""
    band = synth.AirBand(stations)
    spec, rows, out = Spectrum(), Rows(), []
    while len(out) < n:
        row = rows.add(center, spec.update(to_complex(band.block(32768, center))))
        if row is not None:
            out.append(row)
    return out


def st(text, amp=None):
    s = synth.Station.parse(text)
    if amp is not None:
        s.amp = amp
    return s


def hz_of(k, center=CENTER):
    return center + (k - ROW_BINS // 2) * BIN_HZ


class RowsTest(unittest.TestCase):
    def test_one_row_every_six_blocks_and_only_the_usable_span(self):
        rows, power = Rows(), np.arange(NFFT, dtype=np.float32)
        got = [rows.add(CENTER, power) for _ in range(2 * ROW_BLOCKS)]
        self.assertEqual([r is not None for r in got], ([False] * (ROW_BLOCKS - 1) + [True]) * 2)
        row = got[-1]
        self.assertEqual(len(row), ROW_BINS)
        self.assertEqual(row[ROW_BINS // 2], NFFT // 2)          # 行的正中就是窗口的正中
        self.assertAlmostEqual(ROW_SPAN_HZ, 0.8 * 2_400_000, delta=1000)

    def test_averages_power_linearly(self):
        rows = Rows(blocks=2)
        rows.add(CENTER, np.full(NFFT, 1.0, np.float32))
        self.assertAlmostEqual(float(rows.add(CENTER, np.full(NFFT, 3.0, np.float32))[0]), 2.0)

    def test_retune_discards_the_half_built_row(self):
        rows, power = Rows(), np.ones(NFFT, np.float32)
        for _ in range(ROW_BLOCKS - 1):
            rows.add(CENTER, power)
        got = [rows.add(CENTER + 2_000_000, power) for _ in range(ROW_BLOCKS)]
        self.assertEqual([r is not None for r in got], [False] * (ROW_BLOCKS - 1) + [True])


class Frame(unittest.TestCase):
    def test_header_and_quantization(self):
        db = np.linspace(-110, -5, ROW_BINS)
        frame = spectrum_frame(70_000, CENTER, 10 ** (db / 10))
        kind, flags, seq, center, span, floor, _, n = struct.unpack("<BBHIIBBH", frame[:16])
        self.assertEqual((kind, flags, seq, center, span, n),
                         (SPECTRUM_FRAME, 0, 70_000 & 0xFFFF, CENTER, ROW_SPAN_HZ, ROW_BINS))
        q = np.frombuffer(frame[16:], np.uint8)
        self.assertEqual(len(q), n)
        self.assertLessEqual(float(np.abs(q * DB_STEP + DB_MIN - db).max()), DB_STEP / 2 + 1e-6)
        self.assertAlmostEqual(floor * DB_STEP + DB_MIN, float(np.median(db)), delta=DB_STEP)

    def test_out_of_range_values_clip_instead_of_wrapping(self):
        q = quantize(np.array([0.0, 1e-30, 1.0, 1e6]))
        self.assertEqual(q.tolist(), [0, 0, 240, 255])

    def test_a_carrier_lands_on_the_right_bin(self):
        row = rows_of([st("118.5@-7000")])[0]
        q = np.frombuffer(spectrum_frame(1, CENTER, row)[16:], np.uint8)
        self.assertAlmostEqual(hz_of(int(q.argmax())), 118_493_000, delta=BIN_HZ)


class Detect(unittest.TestCase):
    def test_finds_carriers_and_reports_carrier_to_noise(self):
        row = rows_of([st("118.5@-7000"), st("119.725%0")])[0]
        found = cand.detect(row, CENTER, 7.0)
        self.assertEqual(len(found), 2)
        for (hz, bw, db), want in zip(found, (118_493_000, 119_725_000)):
            self.assertAlmostEqual(hz, want, delta=BIN_HZ)
            self.assertAlmostEqual(db, 40, delta=3)              # 幅度 12、噪声 1.5 的合成台就是这个数
            self.assertLess(bw, 12_000)

    def test_noise_alone_gives_nothing(self):
        for row in rows_of([], 20):
            self.assertEqual(cand.detect(row, CENTER, 7.0), [])

    def test_threshold_is_the_squelch_threshold(self):
        row = rows_of([st("118.5%0", amp=0.5)])[0]               # 载噪比约 12 dB
        (hz, _, db), = cand.detect(row, CENTER, 7.0)
        self.assertAlmostEqual(db, 12.4, delta=2)
        self.assertEqual(cand.detect(row, CENTER, 16.0), [])

    def test_dc_spike_at_window_center_is_not_a_signal(self):
        row = rows_of([st("%.4f%%0" % (CENTER / 1e6))])[0]       # 正好压在窗口中心上的载波
        self.assertEqual(cand.detect(row, CENTER, 7.0), [])

    def test_modulated_signal_is_one_candidate_not_three(self):
        # 3 kHz 单音调幅：载波两侧各一根边带，隔着 5 格。应并成一段，频率落在载波上
        row = rows_of([st("118.5*3000")])[0]
        (hz, bw, _), = cand.detect(row, CENTER, 7.0)
        self.assertAlmostEqual(hz, 118_500_000, delta=BIN_HZ)
        self.assertGreater(bw, 6_000)


class Track(unittest.TestCase):
    def test_merges_follows_and_times_a_run(self):
        t = cand.Tracker()
        for i in range(10):
            t.update([(118_500_000 + 40 * i, 3000, 20 + i)], 100 + 0.1 * i, 0.1)
        (c,) = t.snapshot(100.9)
        self.assertEqual((c["hz"], c["db"], c["peak_db"], c["age_s"]), (118_500_360, 29, 29, 0))
        self.assertAlmostEqual(c["run_s"], 0.9)
        self.assertAlmostEqual(c["seen_s"], 1.0)

    def test_neighbouring_833_channels_stay_separate(self):
        t = cand.Tracker()
        t.update([(118_000_000, 3000, 20), (118_008_333, 3000, 25)], 0, 0.1)
        self.assertEqual([c["hz"] for c in t.snapshot(0)], [118_000_000, 118_008_333])    # 按频率排，不按强度

    def test_gone_then_back_restarts_the_run(self):
        t = cand.Tracker()
        t.update([(118_500_000, 3000, 20)], 0, 0.1)
        t.update([], 5, 0.1)
        (c,) = t.snapshot(5)
        self.assertEqual((c["age_s"], c["run_s"]), (5, 0))        # 消失了：记着它，但不算在播
        t.update([(118_500_100, 3000, 22)], 6, 0.1)
        t.update([(118_500_100, 3000, 22)], 6.5, 0.1)
        (c,) = t.snapshot(6.5)
        self.assertEqual((c["age_s"], c["run_s"], c["id"]), (0, 0.5, 1))

    def test_live_ones_sort_before_gone_ones(self):
        t = cand.Tracker()
        t.update([(118_000_000, 3000, 40)], 0, 0.1)
        t.update([(119_000_000, 3000, 10)], 10, 0.1)
        self.assertEqual([c["hz"] for c in t.snapshot(10)], [119_000_000, 118_000_000])

    def test_forgets_old_ones_and_caps_the_list(self):
        t = cand.Tracker()
        t.update([(118_000_000, 3000, 20)], 0, 0.1)
        t.update([(119_000_000, 3000, 20)], cand.EXPIRE_S + 1, 0.1)
        self.assertEqual([c["hz"] for c in t.snapshot(cand.EXPIRE_S + 1)], [119_000_000])
        t.clear()
        for i in range(cand.LIMIT + 50):
            t.update([(118_000_000 + 8_333 * i, 3000, 20)], i * 0.01, 0.1)
        self.assertEqual(len(t.items), cand.LIMIT)
        self.assertEqual(min(c["hz"] for c in t.items), 118_000_000 + 8_333 * 50)        # 留下的是最近的
        self.assertEqual(len(t.snapshot(3)), 80)


if __name__ == "__main__":
    unittest.main()

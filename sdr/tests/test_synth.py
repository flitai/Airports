"""合成信号的编码对不对，拿公开文献里的实测报文来核。"""
import unittest

from sdrd import synth

# 出自《The 1090 Megahertz Riddle》的示例报文
IDENT = bytes.fromhex("8D4840D6202CC371C32CE0576098")          # 呼号 KLM1023
POS_EVEN = bytes.fromhex("8D40621D58C382D690C8AC2863A7")       # 52.2572N 3.91937E，38000 ft


class Crc(unittest.TestCase):
    def test_real_frames(self):
        for frame in (IDENT, POS_EVEN):
            self.assertEqual(synth.crc24(frame[:11]), int.from_bytes(frame[11:], "big"))


class Frames(unittest.TestCase):
    def test_ident_matches_real_frame(self):
        self.assertEqual(synth.ident_frame(0x4840D6, "KLM1023"), IDENT)

    def test_altitude_field(self):
        frame = synth.position_frame(0x40621D, 52.2572, 3.91937, 38000, 0)
        self.assertEqual(frame[:4], POS_EVEN[:4])
        self.assertEqual(frame[4:6], POS_EVEN[4:6])            # TC=11 加上 12 位高度码的前 8 位

    def test_cpr_close_to_real_frame(self):
        # 直接从实测报文里取出 CPR 码来比。文献给的经纬度只有 4–5 位小数，允许差两个最低位
        me = int.from_bytes(POS_EVEN[4:11], "big")
        want = ((me >> 17) & 0x1FFFF, me & 0x1FFFF)
        got = synth.cpr_encode(52.2572, 3.91937, 0)
        self.assertLessEqual(abs(got[0] - want[0]), 2)
        self.assertLessEqual(abs(got[1] - want[1]), 2)

    def test_unknown_characters_become_spaces(self):
        self.assertEqual(synth.ident_frame(1, "A-B"), synth.ident_frame(1, "A B"))


class Waveform(unittest.TestCase):
    def test_envelope_length_and_energy(self):
        env = synth.envelope(IDENT)
        self.assertEqual(len(env), 288)                        # 120 微秒 × 2.4 MS/s
        # 前导 4 个脉冲 + 112 位各一个脉冲，每个半微秒 = 1.2 个采样点
        self.assertAlmostEqual(float(env.sum()), 116 * 1.2, places=3)

    def test_block_is_cu8_and_frames_span_blocks(self):
        mod = synth.Modulator(synth.Traffic((10.0, 20.0), 2))
        blocks = [mod.block(4096) for _ in range(8)]
        self.assertTrue(all(len(b) == 8192 for b in blocks))
        self.assertEqual(mod.pos, 8 * 4096)

    def test_traffic_is_deterministic(self):
        t = synth.Traffic((10.0, 20.0), 1)
        self.assertEqual(t.state(t.craft[0], 12.5), t.state(t.craft[0], 12.5))
        self.assertEqual(len([f for f in t.frames_between(0, 2)]), 4 * 2 + 1)   # 4 组位置+速度，1 条呼号


if __name__ == "__main__":
    unittest.main()

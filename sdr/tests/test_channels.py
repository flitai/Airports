"""频道名换算和窗口规划。"""
import unittest

from sdrd.dsp import channels as C

ZBAA = [["APP", "BEIJING APP AREA 1", "119.6"], ["APP", "BEIJING APP AREA 4", "119.7"],
        ["APP", "BEIJING APP AREA 3", "120.6"], ["APP", "BEIJING APP AREA 6", "121.1"],
        ["APP", "BEIJING APP AREA 2", "126.1"], ["APP", "BEIJING APP AREA 5", "127.75"],
        ["ATIS", "BEIJING ATIS", "127.6"], ["CLD", "BEIJING DEL", "121.6"],
        ["GND", "GND EAST", "121.7"], ["GND", "BEIJING GND", "121.7"], ["GND", "GND WEST", "121.9"],
        ["TWR", "BEIJING EAST TWR", "118.5"], ["TWR", "RWY 01/19 TWR", "118.6"],
        ["TWR", "BEIJING  WEST TWR", "124.3"]]


class Carrier(unittest.TestCase):
    def test_25khz_names_are_their_own_carrier(self):
        for name, hz in (("118.5", 118_500_000), ("118.025", 118_025_000), ("136.975", 136_975_000)):
            self.assertEqual(C.carrier(C.parse_khz(name)), (hz, "25"), name)

    def test_833_names_map_to_real_carriers(self):
        # 一格 25 kHz 里的三个 8.33 kHz 频道，以及下一格的第一个
        for name, hz in (("118.005", 118_000_000), ("118.010", 118_008_333), ("118.015", 118_016_667),
                         ("118.030", 118_025_000), ("118.035", 118_033_333), ("118.040", 118_041_667)):
            self.assertEqual(C.carrier(C.parse_khz(name)), (hz, "8.33"), name)

    def test_names_as_found_in_the_airport_data(self):
        # 法兰克福的几条：频率表里不补零，照样要认出来
        self.assertEqual(C.carrier(C.parse_khz("118.03")), (118_025_000, "8.33"))
        self.assertEqual(C.carrier(C.parse_khz("121.805")), (121_800_000, "8.33"))
        self.assertEqual(C.carrier(C.parse_khz("122.035")), (122_033_333, "8.33"))
        self.assertEqual(C.carrier(C.parse_khz("135.78")), (135_775_000, "8.33"))

    def test_truncated_two_decimal_names(self):
        # 老写法把末位的 5 截掉：118.02 是 118.025，118.07 是 118.075
        self.assertEqual(C.carrier(C.parse_khz("118.02")), (118_025_000, "25"))
        self.assertEqual(C.carrier(C.parse_khz("118.07")), (118_075_000, "25"))

    def test_real_frequency_written_out_snaps_to_grid(self):
        self.assertEqual(C.carrier(C.parse_khz("118.008")), (118_008_333, "8.33"))

    def test_unlistenable_rows_say_why(self):
        self.assertIsNone(C.unlistenable(C.parse_khz("118.5")))
        self.assertIn("导航台", C.unlistenable(C.parse_khz("113.75")))
        self.assertIn("短波", C.unlistenable(C.parse_khz("8.9")))
        self.assertIn("UHF", C.unlistenable(C.parse_khz("257.8")))
        self.assertIn("无法识别", C.unlistenable(C.parse_khz("n/a")))


class Plan(unittest.TestCase):
    def check_windows(self, chans, windows):
        seen = []
        for w, win in enumerate(windows):
            self.assertEqual(win["center_hz"] % 25_000, 12_500)          # 直流毛刺落在两个频道之间
            for i in win["ch"]:
                c = next(c for c in chans if c["id"] == i)
                self.assertEqual(c["win"], w)
                self.assertLessEqual(abs(c["hz"] - win["center_hz"]), C.USABLE // 2 - 15_000)
                self.assertGreaterEqual(abs(c["hz"] - win["center_hz"]), 12_500)
                seen.append(i)
        self.assertEqual(sorted(seen), sorted(c["id"] for c in chans))   # 每个频道恰好进一个窗口

    def test_build_merges_duplicates_and_drops_unlistenable(self):
        chans = C.build(ZBAA + [["ATIS", "on a VOR", "113.75"], ["HF", "", "8.9"], ["X", "", "abc"]])
        self.assertEqual(len(chans), 13)                                  # 14 行里 121.7 重复一次
        gnd = next(c for c in chans if c["hz"] == 121_700_000)
        self.assertEqual(gnd["rows"], [8, 9])
        self.assertTrue(next(c for c in chans if c["label"] == "127.600")["cont"])
        self.assertFalse(gnd["cont"])

    def test_capital_airport_needs_four_windows(self):
        chans = C.build(ZBAA)
        windows = C.plan(chans)
        self.assertEqual(len(windows), 4)                                 # 118.5–127.75，一个窗口盖不全
        self.check_windows(chans, windows)

    def test_picked_row_gets_the_heaviest_window(self):
        chans = C.build(ZBAA)
        atis = next(c["id"] for c in chans if c["label"] == "127.600")
        windows = C.plan(chans, atis)
        self.check_windows(chans, windows)
        mine = windows[chans[atis]["win"]]
        labels = sorted(next(c for c in chans if c["id"] == i)["label"] for i in mine["ch"])
        # 不指定时通播只和 127.75 一组；点了它之后，窗口往下挪，把 126.1 的进近也盖进来
        self.assertEqual(labels, ["126.100", "127.600", "127.750"])

    def test_single_channel_sits_away_from_dc(self):
        chans = C.build([["ATIS", "", "127.6"]])
        (w,) = C.plan(chans, 0)
        self.assertGreaterEqual(abs(w["center_hz"] - 127_600_000), 100_000)
        self.check_windows(chans, [w])

    def test_wide_and_dense_tables(self):
        rows = [["TWR", "", "%.3f" % (118 + 0.025 * k)] for k in range(0, 760, 7)]
        chans = C.build(rows)
        windows = C.plan(chans, 40)
        self.check_windows(chans, windows)
        self.assertLessEqual(len(windows), 12)                            # 19 MHz / 1.86 MHz，多不过一两个

    def test_manual_channel(self):
        self.assertEqual(C.manual(121_500_000, 9)["spacing"], "25")
        m = C.manual(118_008_300, 9)
        self.assertEqual((m["hz"], m["spacing"]), (118_008_333, "8.33"))


if __name__ == "__main__":
    unittest.main()

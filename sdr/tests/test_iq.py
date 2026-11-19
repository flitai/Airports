"""原始 I/Q 的录制与回放。全部用合成信号，不需要接收机。"""
import json
import os
import tempfile
import time
import unittest

import numpy as np

from sdrd import adsb, synth
from sdrd.config import Config
from sdrd.device import BUF_LEN
from sdrd.dsp import to_complex
from sdrd.dsp.spectrum import BIN_HZ, NFFT, Spectrum
from sdrd.engine import Engine
from sdrd.record import IQ_BACKLOG, IQ_BYTES_PER_S, IqStore
from sdrd.sources import ReplaySource, shift_block

CENTER = 119_012_500
BLOCK_S = BUF_LEN / IQ_BYTES_PER_S


def wait_for(fn, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.02)
    return None


def peaks(raw, floor_db=20):
    """一块 cu8 里比底噪高 floor_db 以上的谱线，相对窗口中心的偏移（Hz），去掉直流那几格。"""
    p = Spectrum().update(to_complex(raw))
    hot = [int(k) for k in np.flatnonzero(p > np.median(p) * 10 ** (floor_db / 10)) if abs(int(k) - NFFT // 2) > 3]
    groups = []
    for k in hot:                                        # 一根载波占相邻的几格：并成一组，取最强的那格
        if groups and k - groups[-1][-1] <= 5:
            groups[-1].append(k)
        else:
            groups.append([k])
    return [(max(g, key=lambda k: p[k]) - NFFT // 2) * BIN_HZ for g in groups]


class Shift(unittest.TestCase):
    def setUp(self):
        # 窗口里两个台：一个在中心上方 100 kHz，一个贴着下沿（中心下方 1.1 MHz）
        band = synth.AirBand([synth.Station.parse("%.4f%%0" % ((CENTER + 100_000) / 1e6)),
                              synth.Station.parse("%.4f%%0" % ((CENTER - 1_100_000) / 1e6))])
        self.raw = band.block(BUF_LEN // 2, CENTER)
        self.rng = np.random.default_rng(1)

    def test_signals_move_by_the_tuning_difference(self):
        self.assertEqual([round(o / 1000) for o in peaks(self.raw)], [-1100, 100])
        out = shift_block(self.raw, 300_000, self.rng)                # 接收机调低了 300 kHz：信号都往高处移
        got = peaks(out)
        self.assertEqual(len(got), 2)
        self.assertAlmostEqual(got[0], -800_000, delta=BIN_HZ)
        self.assertAlmostEqual(got[1], 400_000, delta=BIN_HZ)

    def test_what_leaves_the_window_does_not_wrap_around(self):
        out = shift_block(self.raw, -300_000, self.rng)               # 下沿那个台移出去了
        got = peaks(out)
        self.assertEqual(len(got), 1)                                  # 没有从上沿绕回来
        self.assertAlmostEqual(got[0], -200_000, delta=BIN_HZ)

    def test_far_away_is_just_quiet_noise(self):
        out = shift_block(self.raw, 30_000_000, self.rng)
        self.assertEqual(peaks(out), [])
        a = np.frombuffer(out, np.uint8).astype(float)
        self.assertTrue(0.3 < a.std() < 1.5, a.std())                 # 有一点底噪，不是一片死值


class Replay(unittest.TestCase):
    def test_same_center_is_bit_exact_and_loops(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.cu8")
            blocks = [bytes([k]) * BUF_LEN for k in (1, 2, 3)]
            with open(path, "wb") as f:
                f.write(b"".join(blocks) + b"\x09" * 100)             # 末尾不满一块的尾巴
            src = ReplaySource(path, CENTER, "x.cu8")
            src.open()
            src.configure(CENTER, 2_400_000, 0)
            self.assertEqual([src._next_block() for _ in range(5)], blocks + blocks[:2])   # 到头从头再来，尾巴丢掉
            self.assertIn("回放", src.info()["model"])
            src.tune(CENTER + 250_000)
            self.assertNotEqual(src._next_block(), blocks[2])         # 调开了：不再是原样
            src.close()

    def test_missing_or_empty_file_is_reported(self):
        from sdrd.device import DeviceError
        with self.assertRaises(DeviceError):
            ReplaySource("/nonexistent/x.cu8", CENTER).open()


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "iq")
        self.changes = []
        self.store = IqStore(self.dir, 3e6, lambda: self.changes.append(1))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def settle(self, n):
        self.assertTrue(wait_for(lambda: len(self.changes) >= n, 5))
        return self.store.snapshot()

    def capture(self, blocks, pre=0, start=1_795_000_000, **kw):
        name = self.store.begin({"kind": "manual", "mode": "air", "center_hz": CENTER, "rate_hz": 2_400_000},
                                [bytes([200 + k]) * BUF_LEN for k in range(pre)], start)
        for k in range(blocks):
            self.store.feed(bytes([k % 200]) * BUF_LEN)
        self.store.end("stopped", **kw)
        return name

    def test_pre_roll_then_blocks_then_a_json(self):
        name = self.capture(10, pre=3)
        self.assertEqual(name, "20261118T110640Z_119.0125MHz.cu8")
        item, = self.settle(1)["list"]
        self.assertEqual((item["name"], item["bytes"], item["kind"], item["center_hz"], item["end"], item["dropped_blocks"]),
                         (name, 13 * BUF_LEN, "manual", CENTER, "stopped", 0))
        self.assertAlmostEqual(item["duration_s"], 13 * BLOCK_S, delta=0.01)
        with open(os.path.join(self.dir, name), "rb") as f:
            data = f.read()
        self.assertEqual([data[k * BUF_LEN] for k in range(13)], [200, 201, 202] + list(range(10)))   # 留着的那段在最前面
        with open(os.path.join(self.dir, name[:-4] + ".json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), item)
        self.assertEqual(self.store.path_of(name), os.path.join(self.dir, name))
        self.assertIsNone(self.store.path_of("../" + name))
        self.assertIsNone(self.store.active())

    def test_reports_progress_while_recording(self):
        self.store.begin({"kind": "voice", "center_hz": CENTER}, [], time.time())
        for _ in range(8):
            self.store.feed(b"\x7f" * BUF_LEN)
        a = self.store.active()
        self.assertEqual((a["kind"], a["bytes"], a["dropped"]), ("voice", 8 * BUF_LEN, 0))
        self.assertAlmostEqual(a["s"], 8 * BLOCK_S, delta=0.06)
        self.store.end("clip")

    def test_unwanted_and_tiny_captures_leave_nothing(self):
        self.capture(20, keep=False)                                   # 话音片段被当成噪声丢掉了：I/Q 也不留
        self.capture(2)                                                # 不到十分之一秒
        self.settle(2)
        self.assertEqual((self.store.snapshot()["count"], os.listdir(self.dir)), (0, []))

    def test_quota_drops_the_oldest(self):
        for k in range(4):                                             # 每段 20 块 = 1.3 MB，配额 3 MB
            self.capture(20, start=1_795_000_000 + 60 * k)
        snap = self.settle(4)
        self.assertEqual([i["start"] for i in snap["list"]], [1_795_000_000 + 60 * k for k in (3, 2)])
        self.assertEqual(len(os.listdir(self.dir)), 4)

    def test_a_stalled_disk_drops_blocks_instead_of_growing(self):
        gate = __import__("threading").Event()
        self.store.q.put(("delete", "nothing"))                        # 先让写盘线程卡在一件事上
        real, self.store._remove = self.store._remove, lambda items: (gate.wait(5), real(items))
        self.store.begin({"kind": "manual", "center_hz": CENTER}, [], time.time())
        for _ in range(IQ_BACKLOG + 50):
            self.store.feed(b"\x7f" * BUF_LEN)
        a = self.store.active()
        self.assertEqual((a["dropped"], a["bytes"]), (50, IQ_BACKLOG * BUF_LEN))    # 积压有上限，多出来的如实记为丢块
        gate.set()
        self.store.end("stopped")
        item = wait_for(lambda: self.store.snapshot()["list"], 10)[0]
        self.assertEqual((item["dropped_blocks"], item["bytes"]), (50, IQ_BACKLOG * BUF_LEN))


class EngineIq(unittest.TestCase):
    """塔台连续播发（偏 −7 kHz）；地面说 2 秒停 3 秒；125.0 是没有调制的干扰载波。"""
    SOURCE = "synth:adsb:48,11;air:118.5@-7000,121.9~2/3*700,125.0%0"
    ROWS = [["TWR", "Tower", "118.5"], ["GND", "Ground", "121.9"], ["TWR", "bare", "125.0"]]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(os.path.join(self.tmp.name, "sdr.json"))
        self.cfg.set(mode="off")
        self.eng = Engine(self.cfg, self.SOURCE, self.tmp.name)
        self.frames = []
        self.eng.on_audio = self.frames.append
        self.eng.listeners = self.eng.pages = 1
        self.eng.start()

    def tearDown(self):
        self.eng.stop()
        self.tmp.cleanup()

    def cmd(self, **op):
        return self.eng.command(op).result(5)

    def st(self):
        return self.eng.snapshot("status")

    def iqs(self):
        return self.eng.snapshot("iqs")

    def chan(self):
        return self.eng.snapshot("channels")

    def test_manual_capture_then_replay_in_place(self):
        self.assertEqual(self.cmd(op="iq", on=True)["code"], "not_receiving")       # 接收机关着：没有东西可录
        self.cmd(op="channels", label="Test Field", airport=7, list=self.ROWS, pick=0)
        self.assertTrue(wait_for(lambda: self.chan()["open"]))
        time.sleep(1.6)                                                             # 让环形缓冲攒满
        center = self.st()["rf"]["center_hz"]
        self.assertTrue(self.cmd(op="iq", on=True)["ok"])
        a = wait_for(lambda: self.st()["iq"]["active"])
        self.assertEqual(a["kind"], "manual")
        time.sleep(2.0)
        self.assertTrue(self.cmd(op="iq", on=False)["ok"])
        item = wait_for(lambda: self.iqs()["list"])[0]
        self.assertIsNone(self.st()["iq"]["active"])
        self.assertEqual((item["kind"], item["mode"], item["center_hz"], item["end"], item["dropped_blocks"]),
                         ("manual", "air", center, "stopped", 0))
        self.assertEqual((item["airport"], item["airport_label"], item["listen"]), (7, "Test Field", "118.500"))
        self.assertEqual(item["channels"], [["TWR", "Tower", "118.500", [0]], ["GND", "Ground", "121.900", [1]],
                                            ["TWR", "bare", "125.000", [2]]])
        self.assertTrue(3.2 <= item["duration_s"] <= 4.2, item["duration_s"])       # 按下之前的 1.5 秒 + 录的 2 秒
        path = os.path.join(self.tmp.name, "iq", item["name"])
        self.assertEqual(os.path.getsize(path), item["bytes"])
        self.assertEqual(item["bytes"] % BUF_LEN, 0)
        self.assertRegex(item["name"], r"^\d{8}T\d{6}Z_%.4fMHz\.cu8$" % (center / 1e6))

        # ---- 回放：换成读文件的采样源，页面看到的还是一台在收听塔台的接收机 ----
        live = self.eng.source
        del self.frames[:]
        self.assertTrue(self.cmd(op="replay", name=item["name"])["ok"])
        st = wait_for(lambda: self.st() if self.st()["replay"] and self.st()["device"]["state"] == "ready" else None)
        self.assertEqual((st["replay"]["name"], st["mode"], st["air"]["label"]), (item["name"], "air", "Test Field"))
        # 频道表原样摆回来，包括每个频道对应频率表的第几行
        self.assertEqual([(c["label"], c["rows"]) for c in st["air"]["channels"]],
                         [("118.500", [0]), ("121.900", [1]), ("125.000", [2])])
        self.assertIn("回放", st["device"]["model"])
        self.assertEqual(self.eng.source.kind, "replay")
        self.assertIsNot(self.eng.source, live)
        c = wait_for(lambda: self.chan() if self.chan()["open"] and self.chan()["afc_hz"] is not None else None)
        self.assertAlmostEqual(c["afc_hz"], -7000, delta=40)                        # 录下来的那个偏了 7 kHz 的塔台
        self.assertTrue(wait_for(lambda: len(self.frames) > 60))
        pcm = np.concatenate([np.frombuffer(f[4:], "<i2") for f in self.frames[20:60]]).astype(float)
        self.assertAlmostEqual(float(np.argmax(np.abs(np.fft.rfft(pcm * np.hanning(len(pcm))))) * 16000 / len(pcm)), 1000, delta=30)
        self.assertEqual(self.cmd(op="iq", on=True)["code"], "replaying")           # 回放时不能再录 I/Q
        # 地面在另一个窗口里，录的时候接收机没看着它：回放里那儿是空的
        self.cmd(op="listen", ch=1)
        self.assertTrue(wait_for(lambda: self.chan()["listen"] == 1 and self.chan()["win"] == 1))
        time.sleep(3.5)
        self.assertEqual((self.chan()["open"], self.chan()["active"]), (False, []))
        # 回来：换了窗口，回放按频点平移，塔台还在原来的频率上
        self.cmd(op="listen", ch=0)
        self.assertTrue(wait_for(lambda: self.chan()["listen"] == 0 and self.chan()["open"], 5))
        # ---- 返回实时 ----
        self.assertTrue(self.cmd(op="replay", off=True)["ok"])
        st = wait_for(lambda: self.st() if not self.st()["replay"] and self.st()["device"]["state"] == "released" else None)
        self.assertEqual(st["mode"], "off")                                         # 回到回放之前的模式
        self.assertIsNone(self.eng.source)
        self.assertEqual(Config(os.path.join(self.tmp.name, "sdr.json")).get("mode"), "off")
        self.assertEqual(self.cmd(op="replay", name="nope.cu8")["code"], "bad_recording")

    def test_capture_stops_at_the_limit_and_on_retune(self):
        self.cfg.set(iq_max_s=1)
        self.cmd(op="channels", list=self.ROWS, pick=0)
        self.assertTrue(wait_for(lambda: self.chan()["open"]))
        self.cmd(op="iq", on=True)
        item = wait_for(lambda: self.iqs()["list"], 8)[0]                           # 没人去停：到时长自己停
        self.assertEqual(item["end"], "limit")
        self.assertLess(item["duration_s"], 1 + 1.5 + 0.6)
        self.cmd(op="iq", on=True)
        self.assertTrue(wait_for(lambda: self.st()["iq"]["active"]))
        self.cmd(op="listen", ch=1)                                                 # 换窗口：一段 I/Q 只有一个中心频率
        self.assertTrue(wait_for(lambda: self.iqs()["count"] == 2, 5))
        self.assertEqual(self.iqs()["list"][0]["end"], "retune")
        self.assertTrue(self.cmd(op="iq", delete=self.iqs()["list"][0]["name"])["ok"])
        self.assertTrue(wait_for(lambda: self.iqs()["count"] == 1, 3))
        self.assertTrue(self.cmd(op="iq", clear=True)["ok"])
        self.assertTrue(wait_for(lambda: self.iqs()["count"] == 0, 3))
        self.assertEqual(os.listdir(os.path.join(self.tmp.name, "iq")), [])

    def test_voice_triggered_iq_follows_the_audio_clips(self):
        self.cmd(op="channels", label="Test Field", airport=7, list=self.ROWS, pick=1)   # 地面：说 2 秒停 3 秒
        recs = lambda: self.eng.snapshot("recordings")
        # 只开「录 I/Q」，不开「录音」：每次通话照样自动存一段 I/Q，话音一段也不存
        self.eng.listeners = 0
        self.eng.auto_return_s = 2
        self.assertTrue(self.cmd(op="set", iq_with_voice=True)["ok"])
        self.assertTrue(wait_for(lambda: self.iqs()["count"] >= 2, 20))
        alone = self.iqs()["list"][0]
        self.assertEqual((alone["kind"], alone["end"], alone["listen"]), ("voice", "clip", "121.900"))
        self.assertTrue(3.0 <= alone["duration_s"] <= 5.5, alone["duration_s"])     # 2 秒通话，前有留白后有收尾
        st = self.st()
        self.assertEqual((recs()["count"], st["rec"]["on"], st["iq"]["with_voice"]), (0, False, True))
        self.assertEqual((st["mode"], st["auto_return_s"]), ("air", None))          # 开着自动录 I/Q，同样不自动返回
        self.eng.listeners, self.eng.auto_return_s = 1, 999
        # 两个都开：话音片段指向同时录下的那段 I/Q
        self.cmd(op="iq", clear=True)
        self.assertTrue(self.cmd(op="set", record=True)["ok"])
        self.assertTrue(wait_for(lambda: recs()["count"] >= 2 and self.iqs()["count"] >= 2, 20))
        clip = recs()["list"][0]
        iq = next(i for i in self.iqs()["list"] if i["name"] == clip["iq"])        # 话音片段指向同时录下的那段 I/Q
        self.assertEqual((iq["kind"], iq["end"], iq["listen"], iq["mode"]), ("voice", "clip", "121.900", "air"))
        self.assertGreater(iq["duration_s"], clip["duration_s"] - 0.3)              # I/Q 至少盖住整段话音
        self.assertLess(iq["duration_s"], clip["duration_s"] + 2.0)
        self.assertLess(iq["start"], clip["start"] + 0.5)                           # 而且开头不晚于话音片段
        self.assertTrue(self.st()["iq"]["with_voice"])
        # 没有话音的干扰载波：话音不录，I/Q 也不录
        self.cmd(op="listen", ch=2)
        self.assertTrue(wait_for(lambda: self.chan()["listen"] == 2 and self.chan()["open"], 6))
        time.sleep(1.0)
        n = self.iqs()["count"]
        time.sleep(3.0)
        self.assertEqual((self.iqs()["count"], self.st()["iq"]["active"]), (n, None))

    def test_replay_ends_when_no_page_is_left(self):
        self.cmd(op="mode", mode="adsb")
        self.assertTrue(wait_for(lambda: self.eng._rf == "adsb"))
        self.cmd(op="iq", on=True)
        time.sleep(1.0)
        self.cmd(op="iq", on=False)
        item = wait_for(lambda: self.iqs()["list"])[0]
        self.assertEqual((item["mode"], item["center_hz"], "channels" in item), ("adsb", adsb.CENTER, False))
        self.cmd(op="replay", name=item["name"])
        st = wait_for(lambda: self.st() if self.st()["replay"] and self.st()["device"]["state"] == "ready" else None)
        self.assertEqual((st["mode"], st["rf"]["center_hz"]), ("adsb", adsb.CENTER))
        self.assertEqual(Config(os.path.join(self.tmp.name, "sdr.json")).get("mode"), "adsb")
        self.eng.auto_return_s = 1
        self.eng.pages = 0                                                          # 页面都关了
        st = wait_for(lambda: self.st() if not self.st()["replay"] and self.st()["device"].get("simulated") and
                      self.st()["device"]["state"] == "ready" and "回放" not in self.st()["device"]["model"] else None, 8)
        self.assertIsNotNone(st)                                                    # 自己回到实时接收
        self.assertEqual(st["mode"], "adsb")


if __name__ == "__main__":
    unittest.main()

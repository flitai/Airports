"""话音模式的引擎行为：用合成的话音台，不需要接收机。"""
import os
import tempfile
import time
import unittest

import numpy as np

from sdrd import adsb
from sdrd.air import AUDIO_FRAME, FRAME
from sdrd.config import Config
from sdrd.engine import Engine

ROWS = [["TWR", "Tower", "118.5"], ["GND", "Ground", "121.9"], ["ATIS", "Information", "127.6"],
        ["APP", "Approach", "119.7"], ["ATIS", "on a VOR", "113.75"]]
# 塔台偏 −7 kHz 连续发；地面说 1 秒停 6 秒；通播连续；进近没人说话
SOURCE = "synth:adsb:48,11;air:118.5@-7000,121.9~1/6*700,127.6*500"


def wait_for(fn, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.02)
    return None


def tone_of(frames):
    pcm = np.concatenate([np.frombuffer(f[4:], "<i2") for f in frames]).astype(float)
    return float(np.argmax(np.abs(np.fft.rfft(pcm * np.hanning(len(pcm))))) * 16000 / len(pcm))


class Air(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(os.path.join(self.tmp.name, "sdr.json"))
        self.cfg.set(mode="off")
        self.eng = Engine(self.cfg, SOURCE, self.tmp.name)
        self.frames = []
        self.eng.on_audio = self.frames.append
        self.eng.listeners = 1
        self.eng.start()

    def tearDown(self):
        self.eng.stop()
        self.tmp.cleanup()

    def cmd(self, **op):
        return self.eng.command(op).result(5)

    def chan(self):
        return self.eng.snapshot("channels")

    def air(self):
        return self.eng.snapshot("status")["air"]

    def test_listen_from_frequency_table(self):
        self.assertEqual(self.cmd(op="mode", mode="air")["code"], "no_channels")     # 没选频道进不了话音
        t0 = time.time()
        self.assertTrue(self.cmd(op="channels", label="Test Field", airport=7, list=ROWS, pick=0)["ok"])
        self.assertTrue(wait_for(lambda: self.chan()["open"]))
        self.assertLess(time.time() - t0, 3)                                         # 从关闭状态到出声
        st = self.eng.snapshot("status")
        air = st["air"]
        self.assertEqual((st["mode"], st["adsb"]["state"], st["return_to"]), ("air", "paused", "off"))
        self.assertEqual((air["label"], air["airport"], air["skipped"]), ("Test Field", 7, 1))
        self.assertEqual(len(air["windows"]), 3)                                     # 118.5+119.7 / 121.9 / 127.6
        self.assertEqual(st["rf"]["center_hz"], air["windows"][0]["center_hz"])
        c = wait_for(lambda: self.chan() if self.chan()["afc_hz"] is not None else None)
        self.assertEqual((c["listen"], c["win"], c["active"]), (0, 0, [0]))
        self.assertAlmostEqual(c["afc_hz"], -7000, delta=30)                         # 跟踪到偏掉的载波
        self.assertGreater(c["level"]["0"], 20)
        self.assertLess(c["level"]["3"], 7)                                          # 同窗口的进近是安静的
        wait_for(lambda: len(self.frames) > 60)
        first = self.frames[0]
        self.assertEqual((first[0], first[1], len(first)), (AUDIO_FRAME, 1, 4 + 2 * FRAME))
        self.assertEqual(self.frames[1][1], 0)                                       # 只有头一帧带「开始」标志
        self.assertAlmostEqual(tone_of(self.frames[20:60]), 1000, delta=30)

        # 换到另一个窗口里的通播：重调谐，不关设备
        n = len(self.frames)
        self.assertTrue(self.cmd(op="listen", ch=2)["ok"])
        c = wait_for(lambda: self.chan() if self.chan()["win"] == 2 and self.chan()["open"] else None)
        self.assertEqual(c["listen"], 2)
        self.assertEqual(self.eng.snapshot("status")["device"]["state"], "ready")
        wait_for(lambda: len(self.frames) > n + 60)
        self.assertEqual(self.frames[n][1], 1)
        self.assertAlmostEqual(tone_of(self.frames[n + 20:n + 60]), 500, delta=30)
        self.assertEqual(self.eng.snapshot("status")["audio"]["dsp_drops"], 0)

    def test_window_and_step_tuning(self):
        self.cmd(op="channels", list=ROWS, pick=0)
        self.assertTrue(self.cmd(op="window", i=1)["ok"])
        self.assertTrue(wait_for(lambda: self.chan()["win"] == 1))
        self.assertEqual(self.air()["listen"], 1)
        self.assertEqual(self.cmd(op="window", i=9)["code"], "bad_window")
        self.assertEqual(self.cmd(op="listen", ch=99)["code"], "bad_channel")
        # 方向键步进：从 121.900 上一格到 121.925，表里没有，成为一个手动频道
        self.assertTrue(self.cmd(op="tune", step=1)["ok"])
        air = self.air()
        m = next(c for c in air["channels"] if c["id"] == air["listen"])
        self.assertEqual((m["label"], m["type"], m["rows"]), ("121.925", "手动", []))
        # 再退一格回到 121.900：落在表里已有的频道上，手动频道随之消失
        self.assertTrue(self.cmd(op="tune", step=-1)["ok"])
        air = self.air()
        self.assertEqual(air["listen"], 1)
        self.assertEqual(len(air["channels"]), 4)
        self.assertEqual(self.cmd(op="tune", hz=150e6)["code"], "bad_tune")

    def test_patrol_follows_speech_and_skips_continuous_broadcasts(self):
        self.cmd(op="channels", list=ROWS[1:4], pick=0)              # 地面 / 通播 / 进近，三个窗口
        self.eng.auto_return_s = 999
        air = wait_for(self.air)                                      # 应答先到，快照随后一拍才更新
        win = {c["label"]: c["win"] for c in air["channels"]}
        self.assertEqual(len(air["windows"]), 3)
        self.assertTrue(self.cmd(op="set", patrol=True)["ok"])
        seen, held = set(), []
        end = time.time() + 14
        while time.time() < end:
            c = self.chan()
            seen.add(c["win"])
            if c["open"]:
                held.append(c["listen"])
            time.sleep(0.05)
        self.assertEqual(seen, {win["119.700"], win["121.900"]})      # 有人说话的两个窗口来回巡
        self.assertNotIn(win["127.600"], seen)                        # 只有通播的窗口不去：它一直在播
        self.assertEqual(set(held), {0})                              # 只在地面有人说话时出过声
        self.assertTrue(self.air()["patrol"])
        self.cmd(op="listen", ch=1)                                   # 明确点一个频道即退出轮巡
        self.assertFalse(self.air()["patrol"])

    def test_squelch_setting_takes_effect_and_persists(self):
        self.cmd(op="channels", list=ROWS, pick=0)
        self.assertTrue(wait_for(lambda: self.chan()["open"]))
        self.assertTrue(self.cmd(op="set", squelch_db=40)["ok"])     # 门限抬到信号够不着的高度
        self.assertEqual(self.cmd(op="set", squelch_db=99)["code"], "bad_squelch")
        self.assertEqual(self.cmd(op="set", volume=2)["code"], "bad_volume")
        self.assertTrue(self.cmd(op="set", volume=0.35)["ok"])
        self.assertEqual(self.eng.monitor.squelch[0].open_db, 40)     # 当前窗口立即生效
        self.assertTrue(wait_for(lambda: not self.chan()["open"], 3))   # 信号 38 dB，够不着新门限：立刻关
        self.cmd(op="listen", ch=1)                                   # 换窗口：新窗口沿用这个门限
        self.assertTrue(wait_for(lambda: self.chan()["win"] == 1))
        self.assertEqual(self.eng.monitor.squelch[1].open_db, 40)
        self.assertEqual(self.eng.snapshot("status")["squelch_db"], 40)
        saved = Config(os.path.join(self.tmp.name, "sdr.json"))
        self.assertEqual((saved.get("squelch_db"), saved.get("volume")), (40.0, 0.35))
        self.assertEqual(saved.get("mode"), "off")                    # 话音模式不写进设置

    def test_mutes_a_carrier_that_has_no_voice(self):
        # 125.0 上是一根不带调制的载波，扮演室内常见的干扰
        self.eng.stop()
        self.eng = Engine(self.cfg, SOURCE + ",125.0%0", self.tmp.name)
        self.eng.on_audio = self.frames.append
        self.eng.listeners = 1
        self.eng.start()
        self.cmd(op="channels", list=[["TWR", "bare carrier", "125.0"], ["TWR", "Tower", "118.5"]], pick=0)
        c = wait_for(lambda: self.chan() if self.chan()["open"] else None)
        self.assertEqual(c["listen"], 0)
        time.sleep(1.5)
        self.assertFalse(self.chan()["voice"])
        self.assertEqual(len(self.frames), 0)                         # 载波在，但没有话音：一帧都不送
        self.assertTrue(wait_for(lambda: 0 in self.chan()["steady"], 8))   # 几秒后认定为常驻载波
        # 关掉这个选项：按载波放行，听到的是噪声
        self.assertTrue(self.cmd(op="set", voice_only=False)["ok"])
        self.assertTrue(wait_for(lambda: len(self.frames) > 20, 3))
        self.assertFalse(Config(os.path.join(self.tmp.name, "sdr.json")).get("voice_only"))
        self.cmd(op="set", voice_only=True)
        # 换到有调制的塔台：照常出声，开头一帧带「开始」标志
        del self.frames[:]
        self.cmd(op="listen", ch=1)
        self.assertTrue(wait_for(lambda: len(self.frames) > 40, 5))
        self.assertTrue(self.chan()["voice"])
        self.assertEqual(self.frames[0][1], 1)
        self.assertAlmostEqual(tone_of(self.frames[10:40]), 1000, delta=30)

    def test_returns_when_nobody_listens(self):
        self.cmd(op="mode", mode="adsb")
        self.eng.auto_return_s = 2
        self.eng.listeners = 0
        self.cmd(op="channels", list=ROWS, pick=0)
        self.assertTrue(wait_for(lambda: self.eng.snapshot("status")["return_to"] == "adsb", 3))
        self.assertTrue(wait_for(lambda: self.eng.snapshot("status")["auto_return_s"] is not None, 3))
        self.assertTrue(wait_for(lambda: self.eng.snapshot("status")["mode"] == "adsb", 6))
        self.assertIsNotNone(self.air())                              # 频道表还留着，按 2 就能回来


@unittest.skipUnless(adsb.find_readsb(), "没装 readsb")
class HotSwitch(unittest.TestCase):
    def test_adsb_to_voice_and_back_without_closing_the_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = Engine(Config(os.path.join(tmp, "sdr.json")), SOURCE, tmp)
            eng.listeners = 1
            eng.start()
            try:
                self.assertTrue(wait_for(lambda: len(eng.snapshot("aircraft")["list"]) >= 3, 25))
                src = eng.source
                eng.command({"op": "channels", "list": ROWS, "pick": 0}).result(5)
                self.assertTrue(wait_for(lambda: eng.snapshot("channels")["open"], 5))
                self.assertEqual(eng.snapshot("aircraft")["list"], [])        # 本地 ADS-B 暂停，飞机撤下
                self.assertEqual(eng.snapshot("status")["adsb"]["state"], "paused")
                t0 = time.time()
                eng.command({"op": "mode", "mode": "adsb"}).result(5)
                self.assertTrue(wait_for(lambda: len(eng.snapshot("aircraft")["list"]) >= 3, 25))
                self.assertLess(time.time() - t0, 15)
                self.assertIs(eng.source, src)                                # 同一个设备句柄，没重开
                self.assertEqual(eng.snapshot("status")["rf"]["center_hz"], adsb.CENTER)
            finally:
                eng.stop()


if __name__ == "__main__":
    unittest.main()

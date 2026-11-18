"""话音模式的引擎行为：用合成的话音台，不需要接收机。"""
import os
import struct
import tempfile
import time
import unittest
import wave

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

    def air_when(self, ok):
        """等状态快照追上刚发的命令再读：应答先到，快照晚一拍。"""
        air = wait_for(lambda: self.air() if self.air() and ok(self.air()) else None, 5)
        self.assertIsNotNone(air, "5 秒内状态快照没有变成预期的样子")
        return air

    @staticmethod
    def manual_of(air):
        return next((c for c in air["channels"] if c["type"] == "手动"), None)

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
        # 新的一段以带「开始」标志的帧起头。它前面可能还夹着旧频道的一两帧：
        # 命令发出时解调线程手里正在处理的那一块
        starts = [i for i, f in enumerate(self.frames[n:n + 60]) if f[1] == 1]
        self.assertEqual(len(starts), 1)
        self.assertLessEqual(starts[0], 2)
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
        air = self.air_when(self.manual_of)
        m = next(c for c in air["channels"] if c["id"] == air["listen"])
        self.assertEqual((m["label"], m["type"], m["rows"]), ("121.925", "手动", []))
        # 再退一格回到 121.900：落在表里已有的频道上，手动频道随之消失
        self.assertTrue(self.cmd(op="tune", step=-1)["ok"])
        air = self.air_when(lambda a: not self.manual_of(a))
        self.assertEqual(air["listen"], 1)
        self.assertEqual(len(air["channels"]), 4)
        self.assertEqual(self.cmd(op="tune", hz=150e6)["code"], "bad_tune")
        # 手动频道可以删。正在听的就是它：删掉后改听同窗口里别的频道，仍在话音模式
        self.cmd(op="tune", step=1)
        manual = self.air_when(self.manual_of)["listen"]
        self.assertEqual(self.cmd(op="drop", ch=1)["code"], "bad_channel")        # 频率表里的频道不能删
        self.assertTrue(self.cmd(op="set", patrol=True)["ok"])
        self.assertTrue(self.cmd(op="drop", ch=manual)["ok"])
        air = self.air_when(lambda a: not self.manual_of(a))
        self.assertEqual((len(air["channels"]), air["patrol"]), (4, True))        # 轮巡没被顺手关掉
        self.assertNotEqual(air["listen"], manual)
        self.assertEqual(self.eng.snapshot("status")["mode"], "air")
        self.assertEqual(self.cmd(op="drop", ch=manual)["code"], "bad_channel")   # 已经没有了
        # 正在听的是别的频道：删手动频道不打断它
        self.cmd(op="set", patrol=False)
        self.cmd(op="tune", hz=118_950_000)
        manual = self.air_when(self.manual_of)["listen"]
        self.cmd(op="listen", ch=0)
        self.assertTrue(self.cmd(op="drop", ch=manual)["ok"])
        air = self.air_when(lambda a: not self.manual_of(a))
        self.assertEqual((air["listen"], len(air["channels"])), (0, 4))

    def test_dropping_the_only_channel_leaves_voice_mode(self):
        self.assertTrue(self.cmd(op="tune", hz=136_975_000)["ok"])                # 没选机场，直接敲了个频率
        air = wait_for(self.air)
        self.assertEqual(len(air["channels"]), 1)
        self.assertTrue(self.cmd(op="drop", ch=air["listen"])["ok"])
        st = wait_for(lambda: self.eng.snapshot("status") if self.eng.snapshot("status")["mode"] == "off" else None)
        self.assertIsNotNone(st)                                                  # 回到进话音之前的模式
        self.assertIsNone(st["air"])
        self.assertTrue(wait_for(lambda: self.eng.snapshot("status")["device"]["state"] == "released"))

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
        # 换到有调制的塔台：照常出声，开头一帧带「开始」标志。
        # 先等上一段收尾：不满一帧的尾巴是下一块采样到了才补发的，不等的话它会混进新的一段里
        time.sleep(0.3)
        del self.frames[:]
        self.cmd(op="listen", ch=1)
        self.assertTrue(wait_for(lambda: len(self.frames) > 40, 5))
        self.assertTrue(self.chan()["voice"])
        self.assertEqual(self.frames[0][1], 1)
        self.assertAlmostEqual(tone_of(self.frames[10:40]), 1000, delta=30)

    def head(self, frame):
        kind, _, seq, center, span, floor, _, n = struct.unpack("<BBHIIBBH", frame[:16])
        return {"kind": kind, "seq": seq, "center": center, "span": span, "floor": floor, "n": n}

    def test_waterfall_rows_follow_the_window(self):
        rows = []
        self.eng.on_spectrum = rows.append
        self.cmd(op="channels", list=ROWS, pick=0)
        self.assertTrue(wait_for(lambda: self.chan()["open"]))
        time.sleep(0.5)
        self.assertEqual(rows, [])                                    # 没有页面在看：一行都不发
        self.eng.watchers = 1
        self.assertTrue(wait_for(lambda: rows))
        n0, t0 = len(rows), time.time()
        time.sleep(3)
        rate = (len(rows) - n0) / (time.time() - t0)
        self.assertTrue(10 <= rate <= 15, rate)                       # 每秒 12 行上下
        air = self.air()
        h = self.head(rows[-1])
        self.assertEqual((h["kind"], h["center"], h["n"]), (2, air["windows"][0]["center_hz"], len(rows[-1]) - 16))
        q = np.frombuffer(rows[-1][16:], np.uint8)
        peak = h["center"] + (int(q.argmax()) - h["n"] / 2) * h["span"] / h["n"]
        self.assertAlmostEqual(peak, 118_500_000 - 7000, delta=600)   # 最亮的一格就是偏了 7 kHz 的塔台
        self.assertGreater(int(q.max()) - h["floor"], 60)             # 比底噪高 30 dB 以上（0.5 dB 一级）
        self.assertEqual((h["seq"] - self.head(rows[-2])["seq"]) & 0xFFFF, 1)
        st = wait_for(lambda: self.eng.snapshot("status") if self.eng.snapshot("status")["spectrum"]["rows"] > n0 else None)
        self.assertEqual(st["spectrum"]["watchers"], 1)
        # 换到另一个窗口：行里的中心频率跟着变，页面靠它知道该画在哪儿
        self.cmd(op="listen", ch=2)
        want = air["windows"][2]["center_hz"]
        self.assertTrue(wait_for(lambda: self.head(rows[-1])["center"] == want, 3))
        self.assertEqual(self.eng.snapshot("status")["audio"]["dsp_drops"], 0)

    def test_candidates_know_which_ones_are_in_the_table(self):
        # 119.3 上多一根载波，频率表里没有它；它和塔台、进近在同一个窗口里
        self.eng.stop()
        self.eng = Engine(self.cfg, SOURCE + ",119.3%0", self.tmp.name)
        self.eng.listeners = 1
        self.eng.start()
        self.assertEqual(self.eng.snapshot("candidates")["list"], [])
        self.cmd(op="channels", list=ROWS, pick=0)
        got = wait_for(lambda: self.eng.snapshot("candidates")["list"] if len(self.eng.snapshot("candidates")["list"]) >= 2 else None)
        self.assertIsNotNone(got)                                     # 不用有人开着瀑布图，候选照样在找
        by = {round(c["hz"] / 1e5): c for c in got}
        twr, stray = by[1185], by[1193]
        self.assertEqual((twr["ch"], stray["ch"]), (0, None))         # 塔台偏了 7 kHz 也认得出是表里的那个
        self.assertAlmostEqual(twr["hz"], 118_493_000, delta=600)
        self.assertAlmostEqual(stray["hz"], 119_300_000, delta=600)
        self.assertEqual((twr["age_s"], stray["age_s"]), (0, 0))
        self.assertGreater(twr["db"], 30)
        self.assertEqual(len(got), 2)                                 # 安静的进近不在里面
        run = wait_for(lambda: [c for c in self.eng.snapshot("candidates")["list"] if c["id"] == stray["id"] and c["run_s"] > 2], 6)
        self.assertTrue(run)                                          # 一直在：连续时长在涨
        # 换到别的窗口再看：之前见过的还记着，只是标成不在了
        self.cmd(op="listen", ch=2)
        gone = wait_for(lambda: [c for c in self.eng.snapshot("candidates")["list"] if c["id"] == stray["id"] and c["age_s"] >= 2], 6)
        self.assertTrue(gone)
        self.assertEqual(gone[0]["run_s"], 0)
        live = [c for c in self.eng.snapshot("candidates")["list"] if c["age_s"] == 0]
        self.assertEqual([c["ch"] for c in live], [2])                # 此刻在的排最前：这个窗口里的通播

    def test_adsb_mode_computes_spectrum_only_while_someone_watches(self):
        rows = []
        self.eng.on_spectrum = rows.append
        self.cmd(op="mode", mode="adsb")
        self.assertTrue(wait_for(lambda: self.eng._rf == "adsb"))
        time.sleep(1.0)
        self.assertEqual((rows, self.eng._dspq.qsize()), ([], 0))     # 没人看：不抽块、不算谱
        self.eng.watchers = 1
        self.assertTrue(wait_for(lambda: rows))
        n0, t0 = len(rows), time.time()
        time.sleep(2)
        rate = (len(rows) - n0) / (time.time() - t0)
        self.assertTrue(10 <= rate <= 15, rate)
        h = self.head(rows[-1])
        self.assertEqual((h["kind"], h["center"]), (2, adsb.CENTER))
        self.assertEqual(self.eng.snapshot("candidates")["list"], [])  # 1090 MHz 上不找候选
        self.eng.watchers = 0
        time.sleep(0.3)
        n1 = len(rows)
        time.sleep(0.7)
        self.assertEqual(len(rows), n1)

    def test_voice_triggered_recording(self):
        """没有任何页面在听：录音照样自己触发、自己存盘。"""
        # 地面说 3 秒停 3 秒；125.0 是一根不带调制的干扰载波；塔台连续播发。三个台各在一个窗口里
        self.eng.stop()
        self.eng = Engine(self.cfg, "synth:adsb:48,11;air:121.9~3/3*700,125.0%0,118.5", self.tmp.name)
        self.eng.listeners = 0
        self.eng.auto_return_s = 999
        self.eng.start()
        recs = lambda: self.eng.snapshot("recordings")
        folder = os.path.join(self.tmp.name, "recordings")
        table = [["GND", "Ground", "121.9"], ["TWR", "bare carrier", "125.0"], ["TWR", "Tower", "118.5"]]
        self.cmd(op="channels", label="Test Field", airport=7, list=table, pick=0)
        self.assertTrue(wait_for(lambda: self.chan()["voice"], 8))
        time.sleep(1.0)
        self.assertEqual((recs()["count"], self.eng.snapshot("status")["rec"]["on"]), (0, False))   # 没开录音就不录
        self.assertTrue(self.cmd(op="set", record=True)["ok"])
        self.assertTrue(wait_for(lambda: self.chan()["rec_s"], 8))               # 正在录的这段已经多长，给页面显示用
        self.assertTrue(wait_for(lambda: recs()["count"] >= 2, 16))              # 头一段可能是半句，看第二段
        item = recs()["list"][0]
        self.assertEqual((item["hz"], item["label"], item["type"], item["airport"], item["airport_label"], item["end"]),
                         (121_900_000, "121.900", "GND", 7, "Test Field", "closed"))
        self.assertAlmostEqual(item["speech_s"], 3.0, delta=0.4)
        self.assertAlmostEqual(item["pre_s"], 0.5, delta=0.05)
        self.assertTrue(3.5 <= item["duration_s"] <= 4.5, item["duration_s"])    # 留白 + 3 秒通话 + 静噪关闭前的尾巴
        self.assertGreater(item["peak_db"], 30)
        self.assertLess(abs(time.time() - item["start"] - item["duration_s"]), 3)
        with wave.open(os.path.join(folder, item["name"])) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, 16000))
            pcm = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(float)
        self.assertAlmostEqual(len(pcm) / 16000, item["duration_s"], delta=0.01)
        self.assertLess(np.abs(pcm[:4000]).max(), 200)                           # 开头四分之一秒是载波出现之前：安静
        body = pcm[16000:40000]
        self.assertGreater(np.sqrt(np.mean(body ** 2)), 3000)
        self.assertAlmostEqual(float(np.argmax(np.abs(np.fft.rfft(body * np.hanning(len(body))))) * 16000 / len(body)),
                               700, delta=20)                                    # 录下来的就是地面那个 700 Hz
        self.assertTrue(os.path.exists(os.path.join(folder, item["name"][:-4] + ".json")))

        # 持续的干扰载波：没有话音，一段也不录
        self.cmd(op="listen", ch=1)
        self.assertTrue(wait_for(lambda: self.chan()["listen"] == 1 and self.chan()["open"], 5))
        n = wait_for(lambda: recs()["count"], 1) and recs()["count"]             # 换频道时正在录的那段先收尾
        time.sleep(4)
        self.assertEqual((recs()["count"], self.chan()["rec_s"]), (n, None))

        # 连续播发的台：只录一段，到上限为止，不会一段接一段把磁盘写满
        self.eng.clip_s = 1.5
        self.cmd(op="listen", ch=2)
        self.assertTrue(wait_for(lambda: recs()["count"] == n + 1, 8))
        time.sleep(4)
        self.assertEqual(recs()["count"], n + 1)
        cap = recs()["list"][0]
        self.assertEqual((cap["label"], cap["end"]), ("118.500", "limit"))
        # 上限从开录那一刻算起，不含前置留白（刚调过来，留白还没攒满半秒）
        self.assertAlmostEqual(cap["duration_s"] - cap["pre_s"], 1.5, delta=0.1)

        # 开着录音就不算「没人听」：不倒计时，不自动回去
        self.eng.auto_return_s = 1
        time.sleep(3)
        st = self.eng.snapshot("status")
        self.assertEqual((st["mode"], st["auto_return_s"], st["rec"]["on"], st["rec"]["count"]), ("air", None, True, n + 1))
        self.assertTrue(Config(os.path.join(self.tmp.name, "sdr.json")).get("record"))

        # 删一段、清空
        self.assertEqual(self.cmd(op="record", delete="../sdr.json")["code"], "bad_recording")
        self.assertTrue(self.cmd(op="record", delete=cap["name"])["ok"])
        self.assertTrue(wait_for(lambda: recs()["count"] == n, 3))
        self.assertFalse(os.path.exists(os.path.join(folder, cap["name"])))
        self.assertTrue(self.cmd(op="record", clear=True)["ok"])
        self.assertTrue(wait_for(lambda: recs()["count"] == 0, 3))
        self.assertEqual(os.listdir(folder), [])
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "sdr.json")))   # 清空只动录音目录
        # 关掉录音：没人听，这回该回去了
        self.assertTrue(self.cmd(op="set", record=False)["ok"])
        self.assertTrue(wait_for(lambda: self.eng.snapshot("status")["mode"] == "off", 6))

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

"""数据链模式的引擎行为：合成的话音台加假的 dumpvdl2，不需要接收机，也不需要编译解码器。

要盯住的是模式之间怎么进出：进入数据链的路不止一条（启动时读到存下的模式、命令、
话音空闲后自动返回、回放结束），每一条都得把窗口调对、把解码器起起来；
同窗的话音能听，窗外的话音一听就离开，离开了还回得来。
"""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from sdrd import vdl2
from sdrd.config import Config
from sdrd.dsp import channels as chan
from sdrd.engine import Engine
from tests import fake_dumpvdl2 as fake

# 135.9 在数据链窗口里（135.4675–137.0 MHz），连续播发；塔台和地面在窗外
SOURCE = "synth:adsb:48,11;air:135.9*600,118.5*500,121.9~2/3*700"
ROWS = [["TWR", "Tower", "118.5"], ["GND", "Ground", "121.9"], ["APP", "Approach", "135.9"]]
DATA = chan.DATA_ID                      # 136.975 那个数据链频道的 id


def wait_for(fn, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.02)
    return None


class Base(unittest.TestCase):
    MODE = "off"
    FAKE = ""

    def setUp(self):
        # 引擎退出时不等解码器收尾（见 Engine.run 的 finally），假解码器可能还在往这儿写最后一笔
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.note = os.path.join(self.tmp.name, "fake.json")
        self.env = mock.patch.dict(os.environ, {"SDR_DUMPVDL2": fake.wrapper(self.tmp.name),
                                                "FAKE_VDL2_LOG": self.note, "FAKE_VDL2_MODE": self.FAKE})
        self.env.start()
        self.cfg = Config(os.path.join(self.tmp.name, "sdr.json"))
        self.cfg.set(mode=self.MODE)
        self.eng = self.engine()

    def engine(self):
        eng = Engine(self.cfg, SOURCE, self.tmp.name)
        self.frames = []
        eng.on_audio = self.frames.append
        eng.listeners = eng.pages = 1
        eng.start()
        return eng

    def tearDown(self):
        self.eng.stop()
        self.env.stop()
        self.tmp.cleanup()

    def cmd(self, **op):
        r = self.eng.command(op).result(5)
        self.assertTrue(self.eng.thread.is_alive(), "引擎线程死了")
        return r

    def st(self):
        return self.eng.snapshot("status")

    def when(self, ok, seconds=10, why="状态没有变成预期的样子"):
        """等状态快照追上刚发的命令再读：应答先到，快照晚一拍。"""
        st = wait_for(lambda: self.st() if ok(self.st()) else None, seconds)
        self.assertIsNotNone(st, "%s：%s" % (why, {k: self.st()[k] for k in ("mode", "vdl2", "return_to")}))
        self.assertTrue(self.eng.thread.is_alive(), "引擎线程死了")
        return st

    def running(self, seconds=10):
        return self.when(lambda s: s["mode"] == "vdl2" and s["vdl2"]["state"] == "running"
                         and s["rf"] and s["rf"]["center_hz"] == chan.VDL2_CENTER, seconds, "数据链没有起来")

    def fake_state(self):
        try:
            with open(self.note) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def msgs(self):
        return self.eng.snapshot("vdl2")["list"]


class Enter(Base):
    MODE = "vdl2"                        # 上次停在数据链模式：启动时频道表是空的

    def test_resumes_saved_mode_with_empty_channel_table(self):
        st = self.running()
        air = st["air"]
        self.assertEqual([c["hz"] for c in air["channels"]], list(chan.VDL2_FREQS))
        self.assertTrue(all(c["data"] == "vdl2" and c["win"] == 0 for c in air["channels"]))
        self.assertEqual((air["win"], air["listen"], air["patrol"]), (0, None, False))
        self.assertTrue(air["windows"][0]["data"])
        argv = wait_for(self.fake_state)["argv"]
        self.assertEqual(argv[argv.index("--centerfreq") + 1], str(chan.VDL2_CENTER))
        # 报文进快照，按飞机汇总，写进日志
        self.assertTrue(wait_for(lambda: len(self.msgs()) >= 2, 8))
        snap = self.eng.snapshot("vdl2")
        self.assertFalse(snap["replay"])
        self.assertEqual(snap["list"][0]["n"], snap["seq"])
        self.assertIn("a23721", [a["hex"] for a in snap["ac"]])
        st = self.when(lambda s: s["vdl2"]["total"] >= 2 and s["vdl2"]["log"]["bytes"] > 0)
        self.assertEqual(st["vdl2"]["freqs"][0]["hz"], 136_975_000)
        self.assertGreaterEqual(st["vdl2"]["freqs"][0]["n"], 1)
        self.assertEqual((st["vdl2"]["pipe_drops"], st["audio"]["dsp_drops"], st["auto_return_s"]), (0, 0, None))
        self.assertEqual(len(self.frames), 0)            # 默认不听任何话音
        self.assertTrue(any(n.endswith(".jsonl") for n in os.listdir(os.path.join(self.tmp.name, "vdl2"))))

    def test_messages_survive_a_restart(self):
        self.running()
        self.assertTrue(wait_for(lambda: len(self.msgs()) >= 2, 8))
        self.eng.stop()
        kept = self.eng.snapshot("vdl2")["seq"]
        self.cfg.set(mode="off")
        self.eng = self.engine()
        self.assertGreaterEqual(len(self.msgs()), min(kept, 2))              # 从日志尾部读回来的
        self.assertEqual(self.msgs()[0]["hz"] % 25_000, 0)
        self.assertTrue(self.cmd(op="vdl2", clear=True)["ok"])
        self.assertEqual(self.msgs(), [])
        self.assertEqual(self.when(lambda s: s["vdl2"]["total"] == 0)["vdl2"]["log"]["bytes"], 0)
        self.assertEqual(self.cmd(op="vdl2")["code"], "bad_op")

    def test_device_lost_and_reopened(self):
        self.running()
        first = wait_for(self.fake_state)["pid"]
        self.eng._on_lost()
        self.when(lambda s: s["vdl2"]["state"] != "running", 5, "设备丢了解码器还标着运行")
        self.running(15)
        self.assertNotEqual(wait_for(self.fake_state)["pid"], first)


class Switching(Base):
    def test_enter_from_off_and_persist(self):
        self.assertEqual(self.st()["vdl2"]["state"], "off")
        self.assertTrue(self.cmd(op="mode", mode="vdl2")["ok"])
        self.running()
        self.assertEqual(self.cfg.get("mode"), "vdl2")
        self.assertTrue(self.cmd(op="mode", mode="adsb")["ok"])
        st = self.when(lambda s: s["mode"] == "adsb" and s["vdl2"]["state"] == "off"
                       and s["rf"] and s["rf"]["center_hz"] == 1_090_000_000)
        self.assertIsNone(st["air"])                     # 只有数据链频道的表，离开时不留下
        self.assertTrue(self.cmd(op="mode", mode="vdl2")["ok"])
        self.running()

    def test_voice_in_the_same_window_keeps_the_data_link(self):
        self.cmd(op="mode", mode="vdl2")
        self.running()
        pid = wait_for(self.fake_state)["pid"]
        # 打开一个机场的频率表，点的是窗内的进近：仍在数据链，开始出声
        self.assertTrue(self.cmd(op="channels", label="Test", airport=7, list=ROWS, pick=2)["ok"])
        st = self.when(lambda s: s["air"] and s["air"]["listen"] == 2)
        self.assertEqual(st["mode"], "vdl2")
        air = st["air"]
        by = {c["label"]: c for c in air["channels"]}
        self.assertEqual((by["135.900"]["win"], by["136.975"]["win"]), (0, 0))
        self.assertNotEqual(by["118.500"]["win"], 0)
        self.assertEqual(len(air["windows"]), 3)         # 数据链窗口 + 塔台 + 地面
        self.assertTrue(wait_for(lambda: len(self.frames) > 40))
        self.assertTrue(wait_for(lambda: self.eng.snapshot("channels")["open"]))
        self.running()
        self.assertEqual(self.fake_state()["pid"], pid)  # 频道表变了，解码器没有重启
        n = len(self.msgs())
        self.assertTrue(wait_for(lambda: len(self.msgs()) > n, 6))           # 一边听话音，报文照收
        # 停止收听不等于离开
        self.assertTrue(self.cmd(op="listen", ch=None)["ok"])
        st = self.when(lambda s: s["air"]["listen"] is None)
        self.assertEqual(st["mode"], "vdl2")
        time.sleep(0.3)
        n = len(self.frames)
        time.sleep(0.6)
        self.assertLessEqual(len(self.frames) - n, 1)
        self.assertTrue(self.cmd(op="listen", ch=2)["ok"])
        self.assertTrue(wait_for(lambda: len(self.frames) > n + 20))
        self.assertEqual(self.st()["mode"], "vdl2")
        self.assertEqual(self.fake_state()["pid"], pid)

    def test_voice_outside_leaves_and_comes_back(self):
        self.eng.auto_return_s = 2
        self.cmd(op="mode", mode="vdl2")
        self.cmd(op="channels", list=ROWS, pick=2)
        self.running()
        first = wait_for(self.fake_state)["pid"]
        self.assertTrue(self.cmd(op="listen", ch=0)["ok"])                   # 塔台在窗外
        st = self.when(lambda s: s["mode"] == "air" and s["vdl2"]["state"] == "off"
                       and s["rf"]["center_hz"] != chan.VDL2_CENTER)
        self.assertEqual(st["return_to"], "vdl2")
        self.assertEqual(st["air"]["listen"], 0)
        self.assertFalse(any(c["data"] for c in st["air"]["channels"]))      # 话音模式的表里没有数据链频道
        self.assertEqual(self.cfg.get("mode"), "vdl2")                       # 话音不是能存下来的模式
        self.assertTrue(wait_for(lambda: self.eng.snapshot("channels")["open"]))
        self.assertTrue(wait_for(lambda: self.eng.vdl is None))
        # 没人听了：倒计时结束自动回到数据链，窗口和解码器都对
        self.eng.listeners = 0
        st = self.running(12)
        self.assertEqual((st["air"]["win"], st["air"]["listen"]), (0, None))
        self.assertEqual(len([c for c in st["air"]["channels"] if c["data"]]), len(chan.VDL2_FREQS))
        again = wait_for(lambda: (self.fake_state() or {}).get("pid") not in (None, first) and self.fake_state())
        self.assertEqual(again["argv"][again["argv"].index("--centerfreq") + 1], str(chan.VDL2_CENTER))

    def test_window_command(self):
        self.cmd(op="mode", mode="vdl2")
        self.cmd(op="channels", list=ROWS, pick=2)
        self.running()
        self.assertTrue(self.cmd(op="window", i=0)["ok"])                    # 数据链窗口：不动
        time.sleep(0.3)
        self.assertEqual(self.st()["mode"], "vdl2")
        self.assertEqual(self.cmd(op="window", i=9)["code"], "bad_window")
        tower = next(c for c in self.st()["air"]["channels"] if c["label"] == "118.500")
        self.assertTrue(self.cmd(op="window", i=tower["win"])["ok"])         # 别的窗口：去话音，听那个窗口里的
        st = self.when(lambda s: s["mode"] == "air")
        self.assertEqual((st["air"]["listen"], st["return_to"]), (0, "vdl2"))
        self.assertEqual(self.when(lambda s: s["rf"]["center_hz"] != chan.VDL2_CENTER)["vdl2"]["state"], "off")

    def test_data_channels_cannot_be_listened_to(self):
        self.cmd(op="mode", mode="vdl2")
        self.running()
        self.assertEqual(self.cmd(op="listen", ch=DATA)["code"], "data_channel")
        self.assertEqual(self.cmd(op="tune", hz=136_975_000)["code"], "data_channel")
        self.assertEqual(self.cmd(op="mode", mode="air")["code"], "no_channels")     # 表里只有数据链频道
        self.assertEqual(self.cmd(op="set", patrol=True)["code"], "no_patrol")
        self.assertEqual(self.cmd(op="tune", step=1)["code"], "bad_tune")            # 没在听任何频道，无从步进
        time.sleep(0.3)
        st = self.st()
        self.assertEqual((st["mode"], st["air"]["listen"], st["vdl2"]["state"]), ("vdl2", None, "running"))
        self.assertEqual(len(st["air"]["channels"]), len(chan.VDL2_FREQS))           # 没多出重复的标签

    def test_manual_tuning_and_dropping(self):
        self.cmd(op="mode", mode="vdl2")
        self.running()
        pid = wait_for(self.fake_state)["pid"]
        self.assertTrue(self.cmd(op="tune", hz=135_900_000)["ok"])           # 窗内：留在数据链，听它
        st = self.when(lambda s: s["air"]["listen"] is not None)
        manual = st["air"]["listen"]
        self.assertEqual(st["mode"], "vdl2")
        self.assertTrue(wait_for(lambda: len(self.frames) > 20))
        self.assertTrue(self.cmd(op="drop", ch=manual)["ok"])                # 删掉它：仍在数据链，只剩数据链频道
        st = self.when(lambda s: len(s["air"]["channels"]) == len(chan.VDL2_FREQS))
        self.assertEqual((st["mode"], st["air"]["listen"]), ("vdl2", None))
        self.assertEqual(self.cmd(op="drop", ch=DATA)["code"], "bad_channel")
        self.running()
        self.assertEqual(self.fake_state()["pid"], pid)
        # 窗外：去话音；删掉这唯一的手动频道，回到数据链
        self.assertTrue(self.cmd(op="tune", hz=118_500_000)["ok"])
        st = self.when(lambda s: s["mode"] == "air")
        self.assertEqual(st["return_to"], "vdl2")
        self.assertTrue(self.cmd(op="drop", ch=st["air"]["listen"])["ok"])
        self.running(12)

    def test_from_voice_mode_with_a_table(self):
        self.cmd(op="channels", list=ROWS, pick=0)                           # 话音模式，听塔台
        self.when(lambda s: s["mode"] == "air" and s["rf"])
        self.assertTrue(self.cmd(op="mode", mode="vdl2")["ok"])
        st = self.running()
        self.assertEqual(st["air"]["listen"], None)                          # 塔台不在数据链窗口里
        self.assertEqual(len(st["air"]["channels"]), 3 + len(chan.VDL2_FREQS))
        self.assertTrue(self.cmd(op="mode", mode="air")["ok"])               # 有话音频道：可以回话音
        st = self.when(lambda s: s["mode"] == "air" and s["vdl2"]["state"] == "off")
        self.assertFalse(any(c["data"] for c in st["air"]["channels"]))
        self.assertIsNotNone(st["air"]["listen"])


class NoDecoder(Base):
    def test_missing_binary_is_reported(self):
        os.environ["SDR_DUMPVDL2"] = "none"
        self.cmd(op="mode", mode="vdl2")
        st = self.when(lambda s: s["mode"] == "vdl2" and s["vdl2"]["state"] == "no_dumpvdl2")
        self.assertEqual(st["rf"]["center_hz"], chan.VDL2_CENTER)            # 窗口照样调过去，话音照样能听
        self.assertTrue(self.cmd(op="tune", hz=135_900_000)["ok"])
        self.assertTrue(wait_for(lambda: len(self.frames) > 20))
        self.assertEqual(self.st()["mode"], "vdl2")


class Dying(Base):
    FAKE = "exit1"

    def test_decoder_that_keeps_dying(self):
        self.cmd(op="mode", mode="vdl2")
        st = self.when(lambda s: s["vdl2"]["state"] == "failed" and s["vdl2"]["error"], 10)
        self.assertEqual(st["vdl2"]["error"], "fake: refusing to run")
        time.sleep(2.5)                                  # 退避着重启，引擎不受影响
        st = self.st()
        self.assertEqual((st["mode"], st["vdl2"]["state"]), ("vdl2", "failed"))
        self.assertTrue(self.eng.thread.is_alive())
        self.assertGreaterEqual(self.eng._vdl_retry, 2)


class Replay(Base):
    def record(self, seconds=1.2):
        self.assertTrue(self.cmd(op="iq", on=True)["ok"])
        time.sleep(seconds)
        self.cmd(op="iq", on=False)
        return wait_for(lambda: self.eng.snapshot("iqs")["list"], 10)[0]

    def test_recording_made_in_data_link_mode(self):
        self.cmd(op="mode", mode="vdl2")
        self.cmd(op="tune", hz=135_900_000)
        self.running()
        self.assertTrue(wait_for(lambda: len(self.msgs()) >= 1, 8))
        item = self.record()
        self.assertEqual((item["mode"], item["center_hz"]), ("vdl2", chan.VDL2_CENTER))
        self.assertEqual([r[2] for r in item["channels"]], ["135.900"])      # 只存话音频道
        self.cmd(op="mode", mode="adsb")
        self.when(lambda s: s["mode"] == "adsb" and s["vdl2"]["state"] == "off")
        live, logged = self.eng.snapshot("vdl2")["seq"], self.st()["vdl2"]["log"]["bytes"]
        self.assertTrue(self.cmd(op="replay", name=item["name"])["ok"])
        st = self.when(lambda s: s["replay"] and s["mode"] == "vdl2" and s["vdl2"]["state"] == "running", 12)
        self.assertEqual(st["rf"]["center_hz"], chan.VDL2_CENTER)
        self.assertEqual(st["air"]["listen"], next(c["id"] for c in st["air"]["channels"] if c["label"] == "135.900"))
        self.assertEqual(self.cfg.get("mode"), "adsb")                       # 回放时的模式不存
        snap = self.eng.snapshot("vdl2")
        self.assertTrue(snap["replay"])
        self.assertLess(snap["seq"], live + 1)                               # 回放另记一份，从头数
        self.assertTrue(wait_for(lambda: len(self.msgs()) >= 1, 8))
        self.assertTrue(all(m.get("replay") for m in self.msgs()))
        self.assertEqual(self.st()["vdl2"]["log"]["bytes"], logged)          # 回放的报文不进日志
        self.assertTrue(self.cmd(op="replay", off=True)["ok"])
        st = self.when(lambda s: not s["replay"] and s["mode"] == "adsb")
        snap = self.eng.snapshot("vdl2")
        self.assertFalse(snap["replay"])
        self.assertEqual(snap["seq"], live)                                  # 实时收到的那一份原样还在

    def test_recording_without_voice_channels_and_return_to_data_link(self):
        self.cmd(op="mode", mode="vdl2")
        self.running()
        item = self.record()
        self.assertEqual((item["mode"], item["channels"]), ("vdl2", []))
        self.assertTrue(self.cmd(op="replay", name=item["name"])["ok"])
        st = self.when(lambda s: s["replay"] and s["mode"] == "vdl2" and s["vdl2"]["state"] == "running", 12)
        self.assertEqual(st["rf"]["center_hz"], chan.VDL2_CENTER)            # 不会掉到 1090 MHz 去
        self.assertTrue(self.cmd(op="replay", off=True)["ok"])
        self.when(lambda s: not s["replay"], 12)
        st = self.running(12)                                                # 回放前就在数据链：回来接着收
        self.assertIsNone(st["replay"])
        self.assertFalse(self.eng.snapshot("vdl2")["replay"])
        self.assertEqual(self.cfg.get("mode"), "vdl2")

    def test_voice_recording_replayed_then_switched_to_data_link(self):
        # 第 0 步那种素材：话音模式下手动调到 136.975 录的，中心不是数据链窗口的中心
        self.cmd(op="tune", hz=135_900_000)
        st = self.when(lambda s: s["mode"] == "air" and s["rf"] and s["rf"]["center_hz"] != 1_090_000_000)
        self.assertNotEqual(st["rf"]["center_hz"], chan.VDL2_CENTER)
        item = self.record()
        self.assertEqual(item["mode"], "air")
        self.assertTrue(self.cmd(op="replay", name=item["name"])["ok"])
        self.when(lambda s: s["replay"] and s["mode"] == "air", 12)
        self.assertTrue(self.cmd(op="mode", mode="vdl2")["ok"])
        st = self.when(lambda s: s["replay"] and s["mode"] == "vdl2" and s["vdl2"]["state"] == "running", 12)
        self.assertEqual(st["rf"]["center_hz"], chan.VDL2_CENTER)
        self.assertNotEqual(self.cfg.get("mode"), "vdl2")


if __name__ == "__main__":
    unittest.main()

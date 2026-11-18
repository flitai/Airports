"""话音触发录音：状态机和录音目录。不需要接收机。"""
import json
import os
import tempfile
import time
import unittest
import wave

import numpy as np

from sdrd.record import MIN_SPEECH_S, PRE_S, Clipper, Store

BLOCK = 218                              # 一块采样对应的音频点数（13.65 毫秒 × 16 kHz）
BLOCK_S = BLOCK / 16000


def wait_for(fn, seconds=5):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.01)
    return None


def run(clipper, script, enabled=True):
    """script：[(块数, 静噪开着, 有话音)]。→ 录完的片段列表。每块的采样值就是它的序号。"""
    out, k = [], 0
    for blocks, is_open, voice in script:
        for _ in range(blocks):
            k += 1
            clip = clipper.feed(np.full(BLOCK, k, np.int16), is_open, voice, voice, enabled, 20.0, -7000.0)
            if clip:
                out.append(clip)
    return out


class ClipperTest(unittest.TestCase):
    def test_a_call_becomes_one_clip_with_pre_roll(self):
        # 一秒安静，三秒通话，静噪关闭
        clip, = run(Clipper(), [(73, False, False), (220, True, True), (1, False, False)])
        self.assertEqual(clip["end"], "closed")
        self.assertAlmostEqual(clip["pre_s"], PRE_S, delta=2 * BLOCK_S)          # 带上了开录之前的半秒
        self.assertAlmostEqual(clip["duration_s"], PRE_S + 221 * BLOCK_S, delta=3 * BLOCK_S)
        self.assertAlmostEqual(clip["speech_s"], 219 * BLOCK_S, delta=2 * BLOCK_S)
        self.assertEqual((clip["peak_db"], clip["afc_hz"]), (20.0, -7000))
        pcm = clip["pcm"]
        self.assertAlmostEqual(len(pcm) / 16000, clip["duration_s"], delta=0.006)   # 时长如实，只是四舍五入到百分之一秒
        self.assertEqual(int(pcm[-1]), 73 + 221)                                 # 一块不少，一直录到静噪关闭那一块
        self.assertEqual(int(pcm[0]), 75 - round(clip["pre_s"] / BLOCK_S))       # 开头是触发之前的音频，不是从触发那一块（第 74 块）起
        self.assertTrue(np.all(np.diff(pcm.astype(int)) >= 0))                   # 顺序没乱，中间没有接缝
        # 开始时刻 = 触发那一刻的墙上时间减去前置留白（这里是瞬间喂完的，所以约等于现在）
        self.assertAlmostEqual(clip["start"], time.time() - clip["pre_s"], delta=0.5)

    def test_carrier_without_voice_never_starts_a_clip(self):
        c = Clipper()
        self.assertEqual(run(c, [(2000, True, False)]), [])
        self.assertIsNone(c.active)
        self.assertLessEqual(len(c.ring), PRE_S / BLOCK_S + 2)                   # 留着的只有半秒，不会越攒越多

    def test_blips_are_discarded(self):
        short = int(MIN_SPEECH_S / BLOCK_S) - 3
        self.assertEqual(run(Clipper(), [(50, False, False), (short, True, True), (1, False, False)]), [])

    def test_voice_stops_but_carrier_stays(self):
        clips = run(Clipper(), [(100, True, True), (1, True, False), (300, True, False), (100, True, True), (1, False, False)])
        self.assertEqual([c["end"] for c in clips], ["silence", "closed"])       # 再有人说话是新的一段

    def test_continuous_broadcast_is_capped_and_latched(self):
        c = Clipper(max_s=2.0)
        clips = run(c, [(3000, True, True)])                                     # 41 秒不间断
        self.assertEqual(len(clips), 1)                                          # 只录一段，不是一段接一段
        self.assertEqual(clips[0]["end"], "limit")
        self.assertAlmostEqual(clips[0]["duration_s"] - clips[0]["pre_s"], 2.0, delta=2 * BLOCK_S)
        self.assertTrue(c.latched)
        clips = run(c, [(40, False, False), (100, True, True), (1, False, False)])
        self.assertEqual([x["end"] for x in clips], ["closed"])                  # 那次发射结束之后才能再触发

    def test_disabled_records_nothing_and_turning_off_ends_the_clip(self):
        self.assertEqual(run(Clipper(), [(200, True, True), (1, False, False)], enabled=False), [])
        c = Clipper()
        self.assertEqual(run(c, [(100, True, True)]), [])
        self.assertAlmostEqual(c.seconds(), 100 * BLOCK_S, delta=0.1)
        clip, = run(c, [(1, True, True)], enabled=False)
        self.assertEqual(clip["end"], "stopped")
        self.assertIsNone(c.seconds())

    def test_finish_from_another_thread(self):
        c = Clipper()
        run(c, [(100, True, True)])
        self.assertEqual(c.finish("retune")["end"], "retune")
        self.assertIsNone(c.finish())                                            # 没在录就没有东西可收尾


def clip(seconds=1.0, start=None, tone=700):
    t = np.arange(int(seconds * 16000)) / 16000
    return {"pcm": (8000 * np.sin(2 * np.pi * tone * t)).astype(np.int16), "start": start or time.time(),
            "pre_s": 0.5, "duration_s": seconds, "speech_s": seconds - 0.5, "peak_db": 21.5, "afc_hz": -7000, "end": "closed"}


META = {"hz": 121_900_000, "label": "121.900", "type": "GND", "desc": "Ground", "spacing": "25",
        "airport": 7, "airport_label": "Test Field"}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "recordings")
        self.changes = []
        self.store = Store(self.dir, 1e6, lambda: self.changes.append(1))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def settle(self, n):
        self.assertTrue(wait_for(lambda: len(self.changes) >= n))
        return self.store.snapshot()

    def test_writes_a_wav_and_a_json_side_by_side(self):
        self.store.add(clip(1.0, start=1_795_000_000), META)
        snap = self.settle(1)
        item, = snap["list"]
        self.assertEqual(item["name"], "20261118T110640Z_121.900.wav")           # 文件名是 UTC 时刻加频率
        self.assertEqual((item["label"], item["type"], item["airport"], item["end"]), ("121.900", "GND", 7, "closed"))
        self.assertEqual((item["duration_s"], item["pre_s"], item["peak_db"]), (1.0, 0.5, 21.5))
        self.assertEqual(item["start_utc"], "2026-11-18T11:06:40Z")
        with wave.open(os.path.join(self.dir, item["name"])) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()), (1, 2, 16000, 16000))
        with open(os.path.join(self.dir, item["name"][:-4] + ".json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), item)
        self.assertEqual((snap["count"], snap["bytes"]), (1, 32044))
        self.assertEqual(sorted(os.listdir(self.dir)), [item["name"][:-4] + ".json", item["name"]])   # 没留下临时文件

    def test_same_second_same_channel_gets_a_distinct_name(self):
        self.store.add(clip(start=1_795_000_000), META)
        self.store.add(clip(start=1_795_000_000), META)
        self.assertEqual(sorted(i["name"] for i in self.settle(2)["list"]),
                         ["20261118T110640Z_121.900-2.wav", "20261118T110640Z_121.900.wav"])

    def test_quota_drops_the_oldest(self):
        # 配额 1 MB，每段 10 秒 = 320 KB：放得下三段，第四段进来时最旧的让位
        for k in range(5):
            self.store.add(clip(10.0, start=1_795_000_000 + 60 * k), META)
        snap = self.settle(5)
        self.assertEqual(snap["count"], 3)
        self.assertLessEqual(snap["bytes"], 1e6)
        self.assertEqual([i["start"] for i in snap["list"]], [1_795_000_000 + 60 * k for k in (4, 3, 2)])   # 新的在前
        self.assertEqual(len(os.listdir(self.dir)), 6)                           # 被删的连文件一起没了

    def test_a_clip_larger_than_the_quota_is_still_kept(self):
        self.store.add(clip(40.0), META)                                         # 1.28 MB，比配额大
        self.assertEqual(self.settle(1)["count"], 1)                             # 刚录的这段不删，否则等于没录

    def test_delete_clear_and_rescan(self):
        for k in range(3):
            self.store.add(clip(start=1_795_000_000 + k), META)
        names = [i["name"] for i in self.settle(3)["list"]]
        self.store.delete(names[1])
        self.assertEqual([i["name"] for i in self.settle(4)["list"]], [names[0], names[2]])
        self.assertFalse(os.path.exists(os.path.join(self.dir, names[1])))
        # 重启：从目录里把已有的片段认回来；缺了 WAV 的那份 JSON 不算
        os.remove(os.path.join(self.dir, names[2]))
        again = Store(self.dir, 1e6)
        self.assertEqual([i["name"] for i in again.snapshot()["list"]], [names[0]])
        again.close()
        self.store.clear()
        self.assertEqual(self.settle(5)["count"], 0)
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".wav")], [])

    def test_path_of_only_knows_its_own_clips(self):
        self.store.add(clip(), META)
        name = self.settle(1)["list"][0]["name"]
        self.assertEqual(self.store.path_of(name), os.path.join(self.dir, name))
        with open(os.path.join(self.tmp.name, "secret.wav"), "w") as f:
            f.write("x")
        for bad in ("../secret.wav", "/etc/passwd", "..%2Fsecret.wav", name[:-4] + ".json", "nope.wav", "", None, 7):
            self.assertIsNone(self.store.path_of(bad), bad)


if __name__ == "__main__":
    unittest.main()

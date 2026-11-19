"""VDL2 数据链：报文解析、解码器监管（用假的 dumpvdl2）、报文表与日志。"""
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import numpy as np

from sdrd import vdl2
from sdrd.dsp import channels as chan
from tests import fake_dumpvdl2 as fake

BLOCK = bytes([128]) * 65536             # 一块静音：32768 个采样点
OUT_PER_BLOCK = 32768 * 7 // 8 * 4       # 重采样后是 28672 个点，每点 4 字节


def wait_for(fn, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.02)
    return None


def line(i):
    return json.dumps(fake.SAMPLES[i]()).encode()


class Parse(unittest.TestCase):
    def test_uplink_with_unknown_payload(self):
        m = vdl2.parse(line(0))
        self.assertEqual((m["hex"], m["dir"], m["kind"], m["hz"]), ("a23721", "up", "x25", 136_975_000))
        self.assertEqual(m["src"], {"a": "345678", "k": "gs", "st": "gnd"})
        self.assertEqual(m["dst"], {"a": "a23721", "k": "ac"})
        self.assertIn("METAR KROC", m["text"])           # 字节串是可打印的，就当文字给人看
        self.assertAlmostEqual(m["lvl"], -24.5 + vdl2.LEVEL_FIX, places=1)
        self.assertAlmostEqual(m["t"], time.time(), delta=5)

    def test_acars_downlink(self):
        m = vdl2.parse(line(1))
        self.assertEqual((m["hex"], m["dir"], m["kind"]), ("abcdef", "down", "acars"))      # 地址转成小写
        self.assertEqual((m["reg"], m["flight"], m["label"]), ("B-TEST", "TS1234", "H1"))   # 注册号前面的点去掉
        self.assertEqual(m["src"]["st"], "air")
        self.assertTrue(m["sum"].startswith("ACARS H1"))
        self.assertIn("FUEL 12.4", m["text"])

    def test_ground_station_broadcast(self):
        m = vdl2.parse(line(2))
        self.assertEqual((m["hex"], m["dir"], m["kind"], m["hz"]), (None, "up", "xid", 136_725_000))
        self.assertEqual(m["dst"]["k"], "all")
        self.assertIn("Ground Station Information Frame", m["sum"])
        self.assertIn("airport_coverage", m["text"])
        self.assertIn("lat: 40.1", m["text"])

    def test_bare_frame(self):
        m = vdl2.parse(line(3))
        self.assertEqual((m["hex"], m["dir"], m["kind"], m["sum"], m["text"]),
                         ("abcdef", "down", "avlc", "Receive Ready", ""))

    def test_garbage_never_raises(self):
        for raw in (b"", b"not json", b"\xff\xfe{", b"[1, 2]", b'{"vdl2": 3}', b'{"vdl2": {}}',
                    b'{"vdl2": {"avlc": {"src": 5, "acars": {"msg_text": 7}}}}', b"null", "字符串也行"):
            m = vdl2.parse(raw)
            self.assertTrue(m is None or isinstance(m, dict), raw)

    def test_long_text_is_capped(self):
        doc = fake.SAMPLES[1]()
        doc["vdl2"]["avlc"]["acars"]["msg_text"] = "X" * 5000
        self.assertEqual(len(vdl2.parse(json.dumps(doc))["text"]), vdl2.TEXT_MAX)

    def test_binary_payload_shown_as_hex(self):
        doc = fake.SAMPLES[0]()
        doc["vdl2"]["avlc"]["x25"]["unknown_proto"]["data"] = [0, 1, 2, 250, 255]
        self.assertIn("00 01 02 fa ff", vdl2.parse(json.dumps(doc))["text"])


class Decoder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = fake.wrapper(self.tmp.name)
        self.note = os.path.join(self.tmp.name, "fake.json")
        self.env = mock.patch.dict(os.environ, {"FAKE_VDL2_LOG": self.note, "FAKE_VDL2_MODE": ""})
        self.env.start()
        self.dec = None

    def tearDown(self):
        if self.dec:
            self.dec.stop(wait=True)
        self.env.stop()
        self.tmp.cleanup()

    def make(self, center=chan.VDL2_CENTER, freqs=chan.VDL2_FREQS):
        self.dec = vdl2.Dumpvdl2Decoder(self.tmp.name, center, freqs, self.bin)
        return self.dec

    def noted(self):
        try:
            with open(self.note) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def test_arguments_in_the_order_dumpvdl2_needs(self):
        dec = self.make()
        dec.start()
        argv = wait_for(self.noted)["argv"]
        # --oversample 和 --sample-format 写在 --iq-file 前面会被它悄悄重置
        self.assertLess(argv.index("--iq-file"), argv.index("--sample-format"))
        self.assertLess(argv.index("--iq-file"), argv.index("--oversample"))
        self.assertEqual(argv[argv.index("--iq-file") + 1], "-")
        self.assertEqual(argv[argv.index("--sample-format") + 1], "S16_LE")      # 不是帮助里印的 S16LE
        self.assertEqual(argv[argv.index("--oversample") + 1], "20")
        self.assertEqual(argv[argv.index("--centerfreq") + 1], str(chan.VDL2_CENTER))
        self.assertEqual(argv[argv.index("--output") + 1], "decoded:json:file:path=-")
        self.assertEqual(argv[-5:], [str(f) for f in chan.VDL2_FREQS])
        self.assertEqual(argv[-5], "136975000")

    def test_feeds_resampled_samples_and_reads_messages(self):
        dec = self.make()
        dec.start()
        proc = dec.proc
        n = 200                                          # 2.7 秒的采样，假解码器每秒吐一条
        for _ in range(n):
            dec.q.put(BLOCK)
        dec.stop(wait=True, drain=True)
        self.assertEqual(proc.returncode, 0)             # 读到文件结束自己退出的，没挨信号
        self.assertEqual(dec.fed, n * OUT_PER_BLOCK)
        self.assertEqual(dec.fed % 4, 0)
        self.assertEqual(self.noted()["bytes"], dec.fed)
        msgs = [vdl2.parse(r) for r in wait_for(lambda: dec.take() or None, 2)]
        self.assertEqual([m["kind"] for m in msgs], ["x25", "acars"])
        self.assertEqual(dec.dropped, 0)
        self.dec = None

    def test_exits_by_itself_when_the_pipe_closes(self):
        dec = self.make()
        dec.start()
        proc = dec.proc
        dec.feed(BLOCK)
        t0 = time.time()
        dec.stop(wait=True)
        self.assertEqual(proc.returncode, 0)
        self.assertLess(time.time() - t0, 3)
        self.assertFalse(dec.alive())
        self.dec = None

    def test_stalled_decoder_drops_and_still_stops(self):
        os.environ["FAKE_VDL2_MODE"] = "stall"
        dec = self.make()
        dec.start()
        proc = dec.proc
        t0 = time.time()
        for _ in range(300):
            dec.feed(BLOCK)                              # 不阻塞：这是 USB 回调线程里调的
        self.assertLess(time.time() - t0, 1)
        self.assertGreater(dec.dropped, 0)
        t0 = time.time()
        dec.stop(wait=True)                              # 写线程正堵在管道上，也得停得下来
        self.assertLess(time.time() - t0, 8)
        self.assertIsNotNone(proc.returncode)
        self.dec = None

    def test_flood_of_lines_does_not_block(self):
        os.environ["FAKE_VDL2_MODE"] = "flood"
        dec = self.make()
        dec.start()
        rows = []
        self.assertTrue(wait_for(lambda: rows.extend(dec.take()) or len(rows) >= 2000, 10))
        self.assertTrue(all(vdl2.parse(r) for r in rows))

    def test_bad_lines_are_skipped(self):
        os.environ["FAKE_VDL2_MODE"] = "junk"
        dec = self.make()
        dec.start()
        for _ in range(200):
            dec.q.put(BLOCK)
        dec.stop(wait=True, drain=True)
        rows = wait_for(lambda: dec.take() or None, 2)
        self.assertGreater(len(rows), 4)
        self.assertEqual(len([m for m in map(vdl2.parse, rows) if m]), 2)
        self.dec = None

    def test_dead_on_arrival_reports_its_last_words(self):
        os.environ["FAKE_VDL2_MODE"] = "exit1"
        dec = self.make()
        dec.start()
        self.assertTrue(wait_for(lambda: not dec.alive()))
        self.assertEqual(dec.proc.returncode, 1)
        self.assertEqual(wait_for(dec.last_error), "fake: refusing to run")

    def test_refuses_channels_outside_the_sampled_band(self):
        dec = self.make(center=135_000_000)
        with self.assertRaises(ValueError):
            dec.start()
        self.assertFalse(dec.alive())

    def test_missing_binary(self):
        with mock.patch.dict(os.environ, {"SDR_DUMPVDL2": "none"}):
            dec = vdl2.Dumpvdl2Decoder(self.tmp.name, chan.VDL2_CENTER, chan.VDL2_FREQS)
        self.assertFalse(dec.available)
        self.assertFalse(dec.alive())
        dec.feed(BLOCK)                                  # 都不该炸
        dec.stop()
        self.assertEqual(dec.take(), [])


class LogAndJournal(unittest.TestCase):
    def msg(self, i, t, hexa=None):
        m = vdl2.parse(line(i))
        m["t"] = t
        if hexa:
            m["hex"] = hexa
        return m

    def test_log_keeps_recent_and_summarises_by_aircraft(self):
        log = vdl2.Log(keep=5, ac_ttl=100)
        for k in range(8):
            log.add(self.msg(1, 1000 + k))
        log.add(self.msg(0, 1010))
        snap = log.snapshot(1011)
        self.assertEqual([m["n"] for m in snap["list"]], [9, 8, 7, 6, 5])    # 只留最近 5 条，新的在前
        self.assertEqual(snap["seq"], 9)
        ac = {a["hex"]: a for a in snap["ac"]}
        self.assertEqual((ac["abcdef"]["n"], ac["abcdef"]["first"], ac["abcdef"]["last"]), (8, 1000, 1007))
        self.assertEqual((ac["abcdef"]["reg"], ac["abcdef"]["flight"]), ("B-TEST", "TS1234"))
        self.assertEqual(ac["a23721"]["n"], 1)
        st = log.stats(1011)
        self.assertEqual((st["total"], st["per_min"], st["ac"]), (9, 9, 2))
        self.assertEqual(st["freqs"][136_975_000]["n"], 9)
        self.assertEqual(log.stats(1070)["per_min"], 1)                      # 一分钟以前的不算
        self.assertEqual(set(a["hex"] for a in log.snapshot(1109)["ac"]), {"a23721"})    # 100 秒没再出现的拿掉
        log.clear()
        self.assertEqual((log.snapshot(1109)["list"], log.stats(1109)["total"]), ([], 0))

    def test_ground_broadcast_has_no_aircraft(self):
        log = vdl2.Log()
        log.add(self.msg(2, 1000))
        self.assertEqual(log.snapshot(1001)["ac"], [])

    def test_journal_rolls_prunes_and_reads_back(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(vdl2, "PART_BYTES", 1000):
            j = vdl2.Journal(os.path.join(tmp, "vdl2"), 3500)
            self.assertEqual(j.stats(), {"bytes": 0, "quota_bytes": 3500, "files": 0})
            day = 1_795_000_000                          # 2026-11-18 UTC
            row = lambda k: json.dumps({"k": k, "pad": "x" * 280}).encode()
            for k in range(20):                          # 每次 300 字节：一个文件装 4 次
                j.write([row(k)], now=day + k)
            names = sorted(os.listdir(j.dir))
            self.assertTrue(all(n.startswith("20261118-") and n.endswith(".jsonl") for n in names), names)
            self.assertLessEqual(j.stats()["bytes"], 3500)
            self.assertEqual(j.stats()["files"], len(names))
            self.assertGreater(len(names), 1)
            self.assertNotIn("20261118-000.jsonl", names)                    # 最旧的被删了
            self.assertEqual([json.loads(r)["k"] for r in j.tail(3)], [17, 18, 19])
            j.write([row(99)], now=day + 86400)                              # 跨日另起一个文件
            self.assertIn("20261119-000.jsonl", os.listdir(j.dir))
            self.assertEqual(json.loads(j.tail(1)[0])["k"], 99)
            again = vdl2.Journal(j.dir, 3500)                                # 重启后大小接得上
            self.assertEqual(again.stats(), j.stats())
            with open(os.path.join(j.dir, "dumpvdl2.log"), "w") as f:
                f.write("keep me")
            j.clear()
            self.assertEqual(os.listdir(j.dir), ["dumpvdl2.log"])            # 只删日志，不碰别的
            self.assertEqual(j.stats()["bytes"], 0)

    def test_journal_off_when_quota_is_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = vdl2.Journal(os.path.join(tmp, "vdl2"), 0)
            j.write([b"{}"])
            self.assertFalse(os.path.exists(j.dir))
            self.assertEqual(j.tail(), [])


@unittest.skipUnless(vdl2.find_dumpvdl2() and os.path.exists(vdl2.MODEL_WAV),
                     "没有编译 dumpvdl2（sdr/tools/build-dumpvdl2.sh）")
class RealDecoder(unittest.TestCase):
    """真的 dumpvdl2 解真的 VDL2 波形：模型信号升到 2.4 MS/s、偏离窗口中心、加噪声、量化成 8 位，
    再走「平移 → 重采样 → 管道」。解得出来，说明重采样没把信号做坏。"""

    def capture(self, folder, rec_center, snr_db):
        from sdrd import synth
        wave = synth.Vdl2Model(vdl2.MODEL_WAV, amp=30.0).wave
        n = np.arange(len(wave), dtype=np.float64)
        y = wave * np.exp(2j * np.pi * ((136_975_000 - rec_center) / synth.RATE) * n)
        y = np.concatenate([np.zeros(synth.RATE // 2, np.complex128), y, np.zeros(synth.RATE // 2, np.complex128)])
        # 信噪比按 25 kHz 信道带宽算
        sigma = np.sqrt(np.mean(np.abs(wave) ** 2) / 10 ** (snr_db / 10) * (synth.RATE / 25e3) / 2)
        rng = np.random.default_rng(0)
        iq = np.empty(2 * len(y), np.float32)
        iq[0::2], iq[1::2] = y.real + rng.normal(0, sigma, len(y)), y.imag + rng.normal(0, sigma, len(y))
        path = os.path.join(folder, "model.cu8")
        np.clip(np.rint(iq[:len(iq) - len(iq) % 65536] + 127.5), 0, 255).astype(np.uint8).tofile(path)
        return path

    def test_decodes_model_signal_through_shift_and_resampler(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 136.8625 MHz 是话音模式下手动调到 136.975 时的窗口中心：在那儿录的 I/Q 回放时就是这么平移的
            path = self.capture(tmp, 136_862_500, 20)
            msgs, fed = vdl2.decode_file(path, center_hz=136_862_500, run_dir=tmp)
            self.assertEqual(fed % 4, 0)
            self.assertEqual(len(msgs), 2)
            self.assertEqual({m["hex"] for m in msgs}, {"a23721"})
            self.assertEqual({m["hz"] for m in msgs}, {136_975_000})
            self.assertTrue(any("METAR KROC" in m["text"] for m in msgs))

    def test_external_sample(self):
        path = os.environ.get("SDR_VDL2_SAMPLE")
        if not path:
            self.skipTest("SDR_VDL2_SAMPLE 没指向一段真实的 VDL2 录制")
        msgs, _ = vdl2.decode_file(path)
        self.assertGreater(len(msgs), 0)


if __name__ == "__main__":
    unittest.main()

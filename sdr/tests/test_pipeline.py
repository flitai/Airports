"""解码管道与引擎。用真 readsb 的测试在没装 readsb 时自动跳过。"""
import json
import os
import stat
import tempfile
import time
import unittest
from unittest import mock

from sdrd import adsb
from sdrd.config import Config
from sdrd.engine import Engine
from sdrd.geo import distance_bearing

HAVE_READSB = bool(adsb.find_readsb())

# 一个假 readsb：读标准输入，数字节，每收到一些就写一份 aircraft.json，读到文件结束就退出
FAKE = '''#!/usr/bin/env python3
import sys, json, os
out = sys.argv[sys.argv.index("--write-json") + 1]
total = 0
while True:
    b = sys.stdin.buffer.read(65536)
    if not b: break
    total += len(b)
    tmp = os.path.join(out, "aircraft.json.tmp")
    json.dump({"now": total / 4.8e6, "messages": total // 1000,
               "aircraft": [{"hex": "abc001", "flight": "FAKE1   ", "lat": 1.0, "lon": 2.0,
                             "alt_baro": 1000, "seen": 0.1, "seen_pos": 0.2, "rssi": -5.0}]}, open(tmp, "w"))
    os.replace(tmp, os.path.join(out, "aircraft.json"))
'''


def wait_for(fn, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return None


class Slim(unittest.TestCase):
    def test_positioned(self):
        row = adsb.slim({"hex": "abc", "flight": "CCA1  ", "lat": 1.234567, "lon": 2.0, "alt_baro": 9000,
                         "gs": 300, "track": 90, "seen": 0.5, "seen_pos": 2.0, "rssi": -20.1}, 1000.0)
        self.assertEqual(row, ["abc", "CCA1", 1.23457, 2.0, 9000, 300, 90, 998.0, -20.1, 0.5])

    def test_no_position_and_on_ground(self):
        row = adsb.slim({"hex": "abc", "alt_baro": "ground", "seen": 1}, 1000.0)
        self.assertEqual(row[2:5], [None, None, None])
        self.assertIsNone(row[7])


class FakeDecoder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = os.path.join(self.tmp.name, "fake-readsb")   # 不能叫 readsb：那是解码器输出目录的名字
        with open(self.fake, "w") as f:
            f.write(FAKE)
        os.chmod(self.fake, os.stat(self.fake).st_mode | stat.S_IXUSR)

    def tearDown(self):
        self.tmp.cleanup()

    def test_feed_snapshot_and_stop(self):
        d = adsb.ReadsbDecoder(self.tmp.name, home=(1.0, 2.0), binary=self.fake)
        d.start()
        self.assertTrue(d.alive())
        d.feed(b"\x7f" * 65536)
        snap = wait_for(d.snapshot)
        self.assertEqual(snap["aircraft"][0]["hex"], "abc001")
        self.assertIsNone(d.snapshot())                        # 文件没变就不重复返回
        proc = d.proc
        d.stop()
        self.assertIsNotNone(proc.poll())                      # stop 返回时子进程已经没了
        d.feed(b"x")                                           # 停了以后再喂不能抛异常

    def test_exits_on_eof_without_any_signal(self):
        # 我们猝死时不会有人调 stop()，子进程只能靠管道关闭自己走
        d = adsb.ReadsbDecoder(self.tmp.name, binary=self.fake)
        d.start()
        proc = d.proc
        d.q.put(None)
        d.writer.join(2)
        proc.stdin.close()
        self.assertEqual(proc.wait(5), 0)

    def test_missing_binary(self):
        with mock.patch("sdrd.adsb.find_readsb", return_value=None):
            self.assertFalse(adsb.ReadsbDecoder(self.tmp.name).available)


@unittest.skipUnless(HAVE_READSB, "没装 readsb")
class RealReadsb(unittest.TestCase):
    def test_engine_decodes_synthetic_traffic(self):
        """合成采样 → 引擎 → 真 readsb → 快照：位置要和我们造的对得上。"""
        center = (48.0, 11.0)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(os.path.join(tmp, "sdr.json"))
            eng = Engine(cfg, "synth:adsb:%s,%s" % center, tmp)
            eng.start()
            try:
                self.assertTrue(wait_for(lambda: eng.command({"op": "home", "lat": 48.0, "lon": 11.0}).result(5)["ok"]))
                rows = wait_for(lambda: [r for r in eng.snapshot("aircraft")["list"] if r[2] is not None and r[1]]
                                if len(eng.snapshot("aircraft")["list"]) >= 3 and
                                all(r[1] for r in eng.snapshot("aircraft")["list"]) else None, 25)
                self.assertIsNotNone(rows, "25 秒内没解出 3 架带呼号的飞机")
                self.assertEqual(sorted(r[0] for r in rows), ["abc001", "abc002", "abc003"])
                for r in rows:
                    d, _ = distance_bearing(center, (r[2], r[3]))
                    want = {"abc001": 12, "abc002": 21, "abc003": 30}[r[0]]
                    self.assertAlmostEqual(d, want, delta=0.5)           # 各自绕中心转的半径
                    self.assertLess(abs(time.time() - r[7]), 5)          # 位置时刻是墙上时间，不是 readsb 的合成钟
                st = eng.snapshot("status")
                self.assertEqual((st["mode"], st["device"]["state"], st["adsb"]["state"]), ("adsb", "ready", "running"))
                self.assertGreater(wait_for(lambda: eng.snapshot("status")["adsb"]["max_nm"]) or 0, 10)

                # 热切换：关 → 开，不需要重建引擎
                self.assertTrue(eng.command({"op": "mode", "mode": "off"}).result(5)["ok"])
                self.assertTrue(wait_for(lambda: eng.snapshot("status")["device"]["state"] == "released"))
                self.assertEqual(eng.snapshot("aircraft")["list"], [])
                self.assertFalse(eng.command({"op": "mode", "mode": "air"}).result(5)["ok"])
                self.assertTrue(eng.command({"op": "mode", "mode": "adsb"}).result(5)["ok"])
                self.assertTrue(wait_for(lambda: eng.snapshot("status")["adsb"]["state"] == "running", 15))
                self.assertEqual(Config(os.path.join(tmp, "sdr.json")).get("home"), [48.0, 11.0])
            finally:
                eng.stop()
            self.assertIsNone(eng.source)
            self.assertIsNone(eng.decoder)


class EngineCommands(unittest.TestCase):
    def test_rejects_bad_input_without_dying(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(os.path.join(tmp, "sdr.json")); cfg.set(mode="off")
            eng = Engine(cfg, "synth:adsb", tmp)
            eng.start()
            try:
                for op, code in (({"op": "nope"}, "bad_op"), ({"op": "home", "lat": 99, "lon": 0}, "bad_home"),
                                 ({"op": "set", "gain_db": "loud"}, "bad_gain"), ({"op": "mode"}, "bad_mode")):
                    self.assertEqual(eng.command(op).result(5).get("code"), code)
                self.assertTrue(eng.command({"op": "home", "lat": None, "lon": None}).result(5)["ok"])
                self.assertEqual(eng.snapshot("status")["mode"], "off")
            finally:
                eng.stop()


if __name__ == "__main__":
    unittest.main()


class DaemonShutdown(unittest.TestCase):
    def test_exits_promptly_while_a_page_is_still_connected(self):
        """回归：页面的 WebSocket 还连着时，守护进程也必须在管道关闭后很快退出。

        以前 aiohttp 收尾时会等这条长连接，结果退出拖到被父进程强杀，
        设备来不及正常关闭。
        """
        import asyncio
        import subprocess
        import sys
        import aiohttp

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(os.path.join(tmp, "sdr.json")); cfg.set(mode="off")
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            p = subprocess.Popen([sys.executable, "-u", "-m", "sdrd", "--source", "synth:adsb",
                                  "--runtime-dir", tmp, "--parent-pipe"], cwd=root,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                port = json.loads(p.stdout.readline())["port"]

                async def scenario():
                    async with aiohttp.ClientSession() as s:
                        ws = await s.ws_connect("http://127.0.0.1:%d/ws" % port)
                        self.assertEqual(json.loads((await ws.receive(timeout=5)).data)["t"], "hello")
                        p.stdin.close()                      # 父进程「没了」；页面这头故意不断开
                        t0 = time.time()
                        await asyncio.get_running_loop().run_in_executor(None, p.wait, 10)
                        return time.time() - t0

                took = asyncio.run(scenario())
                self.assertEqual(p.returncode, 0)
                self.assertLess(took, 3.0)
            finally:
                if p.poll() is None:
                    p.kill()
                p.stdout.close()

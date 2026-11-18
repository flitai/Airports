"""来源校验：这是挡住「任意网页操纵接收机」的唯一一道门，逐条钉死。"""
import asyncio
import json
import unittest

from sdrd.device import describe
from sdrd.server import AUDIO_BACKLOG, Client, PRIORITY, _host_ok, origin_allowed

PAGE = {"http://localhost:8000", "http://127.0.0.1:8000"}


class Origin(unittest.TestCase):
    def test_listed_origins_pass(self):
        for o in PAGE:
            self.assertTrue(origin_allowed(o, PAGE))

    def test_other_sites_and_other_ports_are_rejected(self):
        for o in ("http://evil.example", "https://localhost:8000", "http://localhost:9999",
                  "http://localhost.evil.example:8000"):
            self.assertFalse(origin_allowed(o, PAGE), o)

    def test_null_origin_is_always_rejected(self):
        self.assertFalse(origin_allowed("null", PAGE))
        self.assertFalse(origin_allowed("null", set()))

    def test_non_browser_clients_have_no_origin(self):
        self.assertTrue(origin_allowed(None, PAGE))

    def test_dev_mode_allows_any_local_origin_only(self):
        self.assertTrue(origin_allowed("http://localhost:5173", set()))
        self.assertTrue(origin_allowed("http://127.0.0.1:8001", set()))
        self.assertFalse(origin_allowed("http://evil.example", set()))
        self.assertFalse(origin_allowed("http://localhost.evil.example", set()))


class Host(unittest.TestCase):
    def test_local_names(self):
        for h in ("localhost:1234", "127.0.0.1:1234", "localhost", "[::1]:1234"):
            self.assertTrue(_host_ok(h), h)

    def test_rebinding_names(self):
        for h in ("evil.example:1234", "127.0.0.1.evil.example", "", None):
            self.assertFalse(_host_ok(h), h)


class FakeSocket:
    """记下发了什么。stuck 时每次发送都永远等下去，像一个被浏览器挂起的标签页。"""
    def __init__(self, stuck=False):
        self.closed, self.stuck, self.sent = False, stuck, []

    async def _send(self, kind, data):
        if self.stuck:
            await asyncio.Event().wait()
        self.sent.append((kind, data))

    async def send_bytes(self, data):
        await self._send("bin", data)

    async def send_str(self, data):
        await self._send("text", json.loads(data)["t"])


class Backpressure(unittest.TestCase):
    def test_audio_first_then_snapshots_then_only_the_newest_spectrum_row(self):
        async def go():
            c = Client(FakeSocket())
            for i in range(100):
                c.offer_spectrum(bytes([2, i]))
            for kind in ("candidates", "aircraft", "status", "channels"):
                c.offer(kind, {"t": kind})
            c.offer_audio(b"\x01a")
            c.offer_audio(b"\x01b")
            pump = asyncio.ensure_future(c.pump())
            await asyncio.sleep(0.05)
            pump.cancel()
            return c.ws.sent
        self.assertEqual(asyncio.run(go()),
                         [("bin", b"\x01a"), ("bin", b"\x01b"), ("text", "status"), ("text", "channels"),
                          ("text", "aircraft"), ("text", "candidates"), ("bin", bytes([2, 99]))])

    def test_a_suspended_tab_cannot_grow_the_daemon(self):
        """挂起的标签页不读数据，发送就卡在那儿。此后无论再来多少，积压都有上界。"""
        async def go():
            c = Client(FakeSocket(stuck=True))
            pump = asyncio.ensure_future(c.pump())
            for i in range(5000):
                c.offer_spectrum(bytes(3292))
                c.offer_audio(bytes(644))
                for kind in PRIORITY:
                    c.offer(kind, {"t": kind, "i": i})
                if i % 500 == 0:
                    await asyncio.sleep(0)
            await asyncio.sleep(0.05)
            pump.cancel()
            return c
        c = asyncio.run(go())
        self.assertEqual(c.ws.sent, [])
        self.assertLessEqual(len(c.audio), AUDIO_BACKLOG)             # 音频最多 500 毫秒
        self.assertLessEqual(len(c.pending), len(PRIORITY))           # 快照每类一份
        self.assertEqual(len(c.spectrum), 3292)                       # 频谱只有一行


class Model(unittest.TestCase):
    def test_v4_needs_exact_usb_strings(self):
        self.assertEqual(describe("RTLSDRBlog", "Blog V4", "R828D"),
                         ("RTL-SDR Blog V4", (500_000, 1_766_000_000), True))

    def test_everything_else_is_not_v4(self):
        # 实测：一只报 Realtek / RTL2838UHIDIR、调谐器 R820T 的设备不是 V4
        name, rng, v4 = describe("Realtek", "RTL2838UHIDIR", "R820T")
        self.assertFalse(v4)
        self.assertEqual(rng[0], 24_000_000)
        self.assertIn("R820T", name)
        self.assertFalse(describe("Realtek", "Blog V4", "R828D")[2])


if __name__ == "__main__":
    unittest.main()

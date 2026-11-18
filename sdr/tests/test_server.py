"""来源校验：这是挡住「任意网页操纵接收机」的唯一一道门，逐条钉死。"""
import unittest

from sdrd.device import describe
from sdrd.server import _host_ok, origin_allowed

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

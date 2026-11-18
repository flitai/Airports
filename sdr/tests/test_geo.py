import unittest

from sdrd.geo import RangeOutline, distance_bearing


class Distance(unittest.TestCase):
    def test_one_degree_of_longitude_on_equator(self):
        d, b = distance_bearing((0, 0), (0, 1))
        self.assertAlmostEqual(d, 60.04, places=1)
        self.assertAlmostEqual(b, 90.0, places=6)

    def test_same_point_has_no_bearing(self):
        self.assertEqual(distance_bearing((40, 116), (40, 116)), (0.0, None))


class Outline(unittest.TestCase):
    def test_needs_home(self):
        self.assertFalse(RangeOutline().update(1, 1))

    def test_grows_once_per_improvement(self):
        o = RangeOutline((0, 0))
        self.assertTrue(o.update(0.2004, 0))                   # 约 12.03 nm，存成 12.0
        # 回归：同一个距离再来一次不能又算「变大」，否则每秒都要写一次配置文件
        self.assertFalse(o.update(0.2004, 0))
        self.assertTrue(o.update(0.5, 0))
        self.assertEqual(o.max_nm, 30.0)

    def test_rejects_absurd_distance(self):
        self.assertFalse(RangeOutline((0, 0)).update(0, 20))   # 1200 nm，必是解错的位置

    def test_roundtrip_and_reset_on_new_home(self):
        o = RangeOutline((0, 0)); o.update(0.5, 0)
        again = RangeOutline((0, 0), o.to_json()["nm"])
        self.assertEqual(again.nm, o.nm)
        self.assertEqual(RangeOutline((1, 1)).max_nm, 0)


if __name__ == "__main__":
    unittest.main()

"""球面距离与方位，以及按方位分桶的「最远接收轮廓」。

distance_bearing 移植自 SDR Lab 的 sdrlab/positions.py，改为返回海里（全站统一
用海里，页面里的 nmBetween 也是）。原模块里的 CPR 解码没有搬过来：位置解算
现在交给 readsb，它同时处理空中、地面和本地解码三种情形。
"""
import math

EARTH_NM = 3440.065          # 与 index.html 的 nmBetween 用同一个半径，两边算出的距离才对得上


def distance_bearing(origin, target):
    """(lat, lon) → (lat, lon) 的大圆距离（海里）和初始真方位（度）。重合时方位为 None。"""
    lat1, lon1, lat2, lon2 = map(math.radians, (*origin, *target))
    delta = lon2 - lon1
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta / 2) ** 2
    distance = 2 * EARTH_NM * math.asin(math.sqrt(min(1.0, max(0.0, a))))
    if distance < 1e-9:
        return distance, None
    bearing = math.degrees(math.atan2(
        math.sin(delta) * math.cos(lat2),
        math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta))) % 360
    return distance, bearing


class RangeOutline:
    """每 5° 方位记一个历史最远距离，连起来就是这副天线在这个位置的实际视野。

    自己算而不用 readsb 的 outline.json：readsb 每次重启都从零开始，而我们切一次
    模式就会重启它一次，轮廓永远攒不起来。
    """
    STEP = 5
    MAX_NM = 400             # 超过这个数的多半是解错的位置，别让一个坏点把轮廓撑爆

    def __init__(self, home=None, nm=None):
        self.home = tuple(home) if home else None
        n = 360 // self.STEP
        self.nm = list(nm) if nm and len(nm) == n else [0.0] * n

    def update(self, lat, lon):
        """喂一个位置。轮廓因此变大了就返回 True。"""
        if not self.home:
            return False
        d, b = distance_bearing(self.home, (lat, lon))
        if b is None or d > self.MAX_NM:
            return False
        i = int(b // self.STEP) % len(self.nm)
        d = round(d, 1)                  # 先取整再比：否则 12.04 存成 12.0 后，下一个 12.04 又算「变大」
        if d > self.nm[i]:
            self.nm[i] = d
            return True
        return False

    @property
    def max_nm(self):
        return max(self.nm)

    def to_json(self):
        return {"home": list(self.home) if self.home else None, "step_deg": self.STEP, "nm": self.nm}

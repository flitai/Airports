"""频道名 → 真实载波；一个机场的频率表 → 若干个射频窗口。

由 SDR Lab 的 exploration.py:12-67 改写而来。保留的是「按频率聚成组、每组一个
中心频率、别让频道落在直流毛刺上」这个思路。改掉的：
  * 原版的频道表要人手工维护，这里直接吃机场数据里的频率表。
  * 原版不懂 8.33 kHz 频道名，「118.005」会被当成 118.005 MHz 去调——差了 5 kHz。
  * 原版最多 64 行、按 75% 采样率硬切，这里不设行数上限，分组保证窗口数最少，
    并让用户点的那一行所在的窗口优先照顾塔台、进近这些要紧的频道。

为什么窗口是一等概念：接收机一次只能看 1.92 MHz，而有两个以上频率的机场里只有
约 13% 能被一个窗口盖全，频率跨度的中位数约 6 MHz。
"""
import math
import re

from . import RATE

AIR_LO, AIR_HI = 117_975_000, 137_000_000    # 民航甚高频话音波段
THIRD = 25_000 / 3                           # 8.33 kHz 间隔的真实步长
USABLE = int(RATE * 0.8)                     # 抗混叠滤波器平坦的那一段：1.92 MHz
SPAN = 1_860_000                             # 一个窗口里最低和最高载波最多相距这么远
HALF_GRID = 12_500

# 分组时的权重：窗口放哪儿有余地时，优先把这些盖进用户点的那个窗口
WEIGHTS = (("TWR", 3), ("APP", 3), ("DIR", 3), ("DEP", 2), ("ARR", 2), ("GND", 2),
           ("ATIS", 2), ("CLD", 1.5), ("DEL", 1.5))
# 连续播发的频道：轮巡时不能因为它一直有信号就停在那儿不走
CONTINUOUS = ("ATIS", "AWOS", "ASOS", "VOLMET")


def parse_khz(text):
    """频率表里的字符串（单位 MHz）→ 整数 kHz；不是数字就返回 None。"""
    try:
        v = float(str(text).strip())
    except ValueError:
        return None
    return round(v * 1000) if math.isfinite(v) and 0 < v < 1e5 else None


def unlistenable(khz):
    """收不了的原因（给界面当提示语）；能收则返回 None。sdr.js 里有一份同样的判断。"""
    if khz is None:
        return "频率无法识别"
    hz = khz * 1000
    if AIR_LO <= hz <= AIR_HI:
        return None
    if hz < 30_000_000:
        return "短波频率，暂不支持"
    if 108_000_000 <= hz < AIR_LO:
        return "导航台频率，不是话音频道"
    if 225_000_000 <= hz <= 400_000_000:
        return "军用 UHF 话音，暂不支持"
    return "不在民航话音波段（118–137 MHz）"


def carrier(khz):
    """频道名（kHz）→ (载波 Hz, 间隔 "25" 或 "8.33")。

    每 25 kHz 一格。格内的名字按 ICAO 的约定：
        .x00  25 kHz 频道，载波就是它
        .x05  8.33 kHz 频道，载波在格子起点（和 .x00 同频，只是带宽窄）
        .x10  8.33 kHz 频道，载波 = 起点 + 8.33 kHz
        .x15  8.33 kHz 频道，载波 = 起点 + 16.67 kHz
    另有一种历史写法：只写两位小数、把末位的 5 截掉，118.02 其实是 118.025。
    频率表里有 92 条这样的。其余写法（直接写真实频率 118.008 之类）就近归到
    8.33 kHz 的格点上。
    """
    base, m = divmod(khz, 25)
    base *= 25_000
    if m == 0:
        return base, "25"
    if m in (5, 10, 15):
        return base + round((m - 5) // 5 * THIRD), "8.33"
    if m == 20:
        return base + 25_000, "25"
    k = round(khz * 1000 / THIRD)
    return round(k * THIRD), "25" if k % 3 == 0 else "8.33"


def _weight(kind):
    kind = kind.upper()
    return next((w for key, w in WEIGHTS if kind.startswith(key)), 1)


def build(rows):
    """频率表 [[类型, 说明, MHz 字符串], ...] → 频道列表。

    同一个载波在表里出现多次（首都机场的 121.7 有两行）只算一个频道，rows 记下
    它对应表里的哪几行，界面靠这个把活动指示灯点回每一行上。
    收不了的行不进列表。
    """
    by_hz, out = {}, []
    for i, row in enumerate(rows):
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        khz = parse_khz(row[2])
        if unlistenable(khz):
            continue
        hz, spacing = carrier(khz)
        kind, desc = str(row[0] or "")[:24], str(row[1] or "")[:80]
        ch = by_hz.get(hz)
        if ch:
            ch["rows"].append(i)
            ch["weight"] = max(ch["weight"], _weight(kind))
            if kind and kind not in ch["type"].split(" / "):
                ch["type"] += " / " + kind
            continue
        ch = by_hz[hz] = {"id": len(out), "type": kind, "desc": desc, "hz": hz, "spacing": spacing,
                          "label": "%.3f" % (khz / 1000), "rows": [i], "weight": _weight(kind),
                          "cont": kind.upper().startswith(CONTINUOUS), "win": 0}
        out.append(ch)
    return out


def manual(hz, ident):
    """方向键步进调出来的频道，不来自任何机场的频率表。"""
    k = round(hz / THIRD)
    hz = round(k * THIRD)
    return {"id": ident, "type": "手动", "desc": "", "hz": hz, "spacing": "25" if k % 3 == 0 else "8.33",
            "label": "%.3f" % (hz / 1e6), "rows": [], "weight": 1, "cont": False, "win": 0, "manual": True}


# ---- VDL2 数据链 ----
# 窗口中心是固定的，不跟着话音频道表走：五个数据链频率离中心 +312.5 到 +562.5 kHz，
# 远离中心的直流毛刺，也都在通带平坦处。让中心浮动的话，改一个话音频道解码器就得
# 重启一次，最坏会把数据链频道摆到离直流 37.5 kHz 的地方。
# 「同窗口」因此是一条算术判断：离中心不超过 VDL2_REACH 的话音频道，135.4675–137.0 MHz。
VDL2_CENTER = 136_412_500
VDL2_FREQS = (136_975_000, 136_725_000, 136_775_000, 136_825_000, 136_875_000)   # 第一个是公共信令频道
VDL2_REACH = USABLE // 2 - 15_000
DATA_ID = 1_000_000                          # 数据链频道的 id 从这儿起：频率表的从 0 起，手动的从 10001 起


def data(hz, ident):
    """数据链频道。进频道表是为了在频道条上占个位置、带上指示灯；它不能收听。"""
    return {"id": ident, "type": "VDL2", "desc": "数据链", "hz": hz, "spacing": "25",
            "label": "%.3f" % (hz / 1e6), "rows": [], "weight": 1, "cont": True, "win": 0, "data": "vdl2"}


def plan_vdl2(voice):
    """数据链模式的窗口划分 → (频道表, 窗口列表)。

    第 0 个窗口固定是数据链窗口：数据链频道，加上落在它里面的话音频道。其余话音频道
    照常分，窗口号顺延。正好压在数据链频率上的话音频道去掉——那儿没有话音可听。
    """
    voice = [c for c in voice if not c.get("data") and c["hz"] not in VDL2_FREQS]
    inside = [c for c in voice if abs(c["hz"] - VDL2_CENTER) <= VDL2_REACH]
    outside = [c for c in voice if abs(c["hz"] - VDL2_CENTER) > VDL2_REACH]
    rest = plan(outside)
    for c in outside:
        c["win"] += 1
    links = [data(hz, DATA_ID + i) for i, hz in enumerate(VDL2_FREQS)]
    members = sorted(inside + links, key=lambda c: c["hz"])
    for c in members:
        c["win"] = 0
    first = {"center_hz": VDL2_CENTER, "lo_hz": members[0]["hz"], "hi_hz": members[-1]["hz"],
             "ch": [c["id"] for c in members], "data": True}
    return voice + links, [first] + rest


def _center(freqs):
    """窗口中心：落在 25 kHz 网格的半格上，并尽量让所有载波都离它远一点。

    接收机的直流毛刺和它两侧的低频噪声都在中心。半格保证没有哪个 25 kHz 频道正好
    压在毛刺上；在盖得住全组的前提下，再挑离最近的载波最远的位置（100 kHz 封顶，
    更远没有额外好处），并列时取最居中的。
    """
    lo, hi = min(freqs), max(freqs)
    reach = USABLE // 2 - 15_000         # 载波离中心最多这么远，给频道自身的带宽留边
    first = math.ceil((hi - reach - HALF_GRID) / 25_000) * 25_000 + HALF_GRID
    mid, best, best_score = (lo + hi) / 2, None, None
    for c in range(first, lo + reach + 1, 25_000):
        score = (min(100_000, min(abs(c - f) for f in freqs)), -abs(c - mid))
        if best_score is None or score > best_score:
            best, best_score = c, score
    return best


def plan(channels, pick=None):
    """把频道分进最少的窗口，写回每个频道的 win，返回窗口列表。

    pick 是用户点的那个频道在 channels 里的下标。有它时，先为它挑一个窗口位置——
    在所有盖得住它的摆法里取权重和最大的——再把两侧剩下的频道贪心地分完。
    不给 pick 就整体从低到高贪心，那样窗口数是最少的。
    """
    if not channels:
        return []
    order = sorted(range(len(channels)), key=lambda i: channels[i]["hz"])
    hz = [channels[i]["hz"] for i in order]

    def sweep(idx, reverse=False):
        groups, seq = [], (idx[::-1] if reverse else idx)
        for k in seq:
            if groups and abs(hz[k] - hz[groups[-1][0]]) <= SPAN:
                groups[-1].append(k)
            else:
                groups.append([k])
        return [sorted(g) for g in groups][::-1] if reverse else groups

    everything = list(range(len(order)))
    if pick is None:
        groups = sweep(everything)
    else:
        p = order.index(pick)
        best, best_score = None, None
        for start in everything:
            if not hz[p] - SPAN <= hz[start] <= hz[p]:
                continue
            g = [k for k in everything if hz[start] <= hz[k] <= hz[start] + SPAN]
            score = (sum(channels[order[k]]["weight"] for k in g), len(g), -(hz[g[-1]] - hz[g[0]]))
            if best_score is None or score > best_score:
                best, best_score = g, score
        groups = (sweep([k for k in everything if k < best[0]], reverse=True) + [best]
                  + sweep([k for k in everything if k > best[-1]]))

    windows = []
    for w, g in enumerate(groups):
        ids = [order[k] for k in g]
        for i in ids:
            channels[i]["win"] = w
        lo, hi = hz[g[0]], hz[g[-1]]
        windows.append({"center_hz": _center([hz[k] for k in g]), "lo_hz": lo, "hi_hz": hi,
                        "ch": [channels[i]["id"] for i in ids]})
    return windows


def label_of(text):
    """机场名这类来自页面的字符串：去掉控制字符，限长。"""
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(text or ""))[:120]

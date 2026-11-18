"""候选信号：窗口里有哪些载波强到值得一听，不管它在不在频率表里。

由 SDR Lab 的 search.py（CandidateDetector）改写而来。保留的是「对平均功率谱做
能量检测，连成段，再按频率把逐次的检测结果并成一条条候选」这个做法，以及它的
态度：能量检测只说明这里有东西，不说明它是什么。改掉的：
  * 强度和门限改用载噪比，与静噪同一个量、同一个门限。于是候选的含义一句话就能
    说清：强到能开静噪的载波。原版拿「比单格底噪高 10 dB」当门限，室内实测行不通，
    电脑和电源的杂散会冒出一大片（见 spectrum.py 开头的说明）。
  * 频率取最强的那根谱线而不是功率加权的重心。调幅话音最强的就是载波，重心会被
    调制边带和相邻信号拉偏。
  * 不按频段猜调制方式。这里只有民航话音波段，页面拿附近机场的频率表去对。
  * 不依赖 scipy：原版用 ndimage 做平滑和闭运算，这里几行 numpy。
  * 候选跨窗口保留：换窗口、轮巡一圈回来，之前见过的还在。
原版的扫描与驻留策略（SearchPolicy）没有移植，全波段扫描留到以后。
"""
import threading

import numpy as np

from .spectrum import BIN_HZ, _CHANNEL_DB

_EDGE_DB = 6.0                           # 一段信号的边界：平滑后比底噪高这么多
_GAP_BINS = 3                            # 相隔不超过这么多格的两段算一段（调制边带之间的空隙）
_DC_BINS = 4                             # 窗口正中是接收机的直流毛刺，左右各这么多格不算信号
MERGE_HZ = 2_500                         # 两次检测相差在这以内算同一个候选
GAP_S = 1.0                              # 隔了这么久再出现，算新的一次，连续时长从头计
EXPIRE_S = 900.0                         # 这么久没再出现就忘掉
LIMIT = 200


def detect(row, center_hz, threshold_db):
    """row 是一行线性功率谱（Rows 的输出，窗口正中在 len // 2）。

    → [(载波频率 Hz, 带宽 Hz, 载噪比 dB)]，按频率从低到高。
    """
    n = len(row)
    floor = float(np.median(row))
    if floor <= 0:
        return []
    smooth = np.convolve(row, np.full(3, 1 / 3), "same")
    mask = smooth > floor * 10 ** (_EDGE_DB / 10)
    mid = n // 2
    mask[mid - _DC_BINS:mid + _DC_BINS + 1] = False
    edges = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    runs = []
    for a, b in zip(starts.tolist(), ends.tolist()):
        # 直流毛刺那几格是我们自己挖掉的，不是信号里的空隙，不能跨过它去合并
        if runs and a - runs[-1][1] <= _GAP_BINS and not runs[-1][1] <= mid < a:
            runs[-1][1] = b
        else:
            runs.append([a, b])
    need = threshold_db + _CHANNEL_DB
    out = []
    for a, b in runs:
        k = a + int(row[a:b].argmax())
        excess = 10 * np.log10(float(row[k]) / floor)
        if excess >= need:
            out.append((center_hz + (k - mid) * BIN_HZ, (b - a) * BIN_HZ, excess - _CHANNEL_DB))
    return out


class Tracker:
    """把逐行的检测结果并成候选条目。sdr-dsp 线程写，引擎线程读，所以带一把锁。"""

    def __init__(self):
        self.items = []
        self._lock = threading.Lock()
        self._next = 1

    def update(self, found, now, dt):
        """found 是 detect 的结果；now 是单调时钟；dt 是这一行代表的时长。"""
        with self._lock:
            for hz, bw, db in found:
                near = [c for c in self.items if abs(c["hz"] - hz) <= MERGE_HZ]
                if near:
                    c = min(near, key=lambda c: abs(c["hz"] - hz))
                    if now - c["last"] > GAP_S:
                        c["since"] = now
                    c.update(hz=hz, bw=bw, db=db, peak=max(c["peak"], db), last=now, seen=c["seen"] + dt)
                else:
                    self.items.append({"id": self._next, "hz": hz, "bw": bw, "db": db, "peak": db,
                                       "first": now, "last": now, "since": now, "seen": dt})
                    self._next += 1
            self.items = [c for c in self.items if now - c["last"] < EXPIRE_S]
            if len(self.items) > LIMIT:
                self.items = sorted(self.items, key=lambda c: c["last"])[-LIMIT:]

    def clear(self):
        with self._lock:
            self.items = []

    def snapshot(self, now, limit=80):
        """→ 给页面的列表：此刻还在的排前面，按频率从低到高；已经消失的按消失的先后。

        在的那些不按强度排：几个强度相近的信号每秒都在互换名次，列表跳来跳去没法点。
        按频率排是稳的，而且和瀑布图从左到右的顺序一致。

        age_s   距最近一次出现多久（0 就是此刻还在）
        run_s   这一次已经连续出现了多久；已经消失的记 0
        seen_s  累计出现了多久
        """
        with self._lock:
            items = [dict(c) for c in self.items]
        out = []
        for c in items:
            age = now - c["last"]
            live = age <= GAP_S
            out.append({"id": c["id"], "hz": round(c["hz"]), "bw_hz": round(c["bw"]),
                        "db": round(c["db"], 1), "peak_db": round(c["peak"], 1),
                        "age_s": 0 if live else round(age), "run_s": round(c["last"] - c["since"], 1) if live else 0,
                        "seen_s": round(c["seen"], 1)})
        out.sort(key=lambda c: (c["age_s"], c["hz"]))
        return out[:limit]

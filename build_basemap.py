#!/usr/bin/env python3
# Airports · 全球机场分布地图
# Copyright (C) 2026 FlitAI
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-only
"""把 Natural Earth 矢量数据编译成离线底图 data/basemap.js。

为什么不用 PMTiles/MBTiles：它们靠 HTTP Range 请求读单文件，而 fetch/XHR 在
file:// 下被浏览器阻止。要保住"双击打开"，底图数据只能和机场数据一样编译成
.js 由 <script> 标签加载。

用法：
    python3 build_basemap.py <natural_earth_geojson 目录>
数据来源 https://github.com/nvkelso/natural-earth-vector （public domain）
"""
import json, os, sys

SRC = sys.argv[1] if len(sys.argv) > 1 else "."
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PREC = 3          # 3 位小数 ≈ 110m，对 1:50m 比例尺的数据远超所需


def round_coords(c):
    """递归取整坐标，并去掉取整后重合的相邻点。空/退化几何返回 None。"""
    if not c:
        return None
    if isinstance(c[0], (int, float)):
        return [round(c[0], PREC), round(c[1], PREC)]
    out = [round_coords(x) for x in c]
    if out and isinstance(out[0][0], (int, float)):
        ded = [out[0]]
        for p in out[1:]:
            if p != ded[-1]:
                ded.append(p)
        # 环至少要 4 个点才合法，退化的就整段丢弃
        if len(ded) < 2 or (out[0] == out[-1] and len(ded) < 4):
            return None
        return ded
    return [x for x in out if x]


def slim(path, keep, name_field=None, where=None):
    """只保留需要的属性，坐标降精度。"""
    with open(path, encoding="utf-8") as f:
        fc = json.load(f)
    feats = []
    for ft in fc["features"]:
        p = ft.get("properties") or {}
        if where and not where(p):
            continue
        g = ft.get("geometry")
        if not g:
            continue
        gc = round_coords(g["coordinates"])
        if not gc:
            continue
        props = {}
        for src, dst in keep.items():
            v = p.get(src)
            if v not in (None, "", -99):
                props[dst] = v
        feats.append({"type": "Feature", "properties": props,
                      "geometry": {"type": g["type"], "coordinates": gc}})
    return {"type": "FeatureCollection", "features": feats}


LAYERS = [
    # 文件名, 保留属性映射, 过滤条件
    ("ne_50m_admin_0_countries",             {"NAME": "name"},                 None),
    ("ne_50m_lakes",                         {},                               None),
    ("ne_50m_rivers_lake_centerlines",       {},                               None),
    ("ne_50m_admin_1_states_provinces_lines",{},                               None),
    # 城市点只留较重要的，scalerank 越小越重要
    ("ne_50m_populated_places_simple",       {"name": "name", "scalerank": "r"},
     lambda p: (p.get("scalerank") or 99) <= 7),
]

out = {}
for fname, keep, where in LAYERS:
    path = os.path.join(SRC, fname + ".geojson")
    if not os.path.exists(path):
        print(f"  跳过（缺文件）: {fname}", file=sys.stderr)
        continue
    key = fname.replace("ne_50m_", "").replace("admin_0_", "") \
               .replace("admin_1_states_provinces_lines", "states") \
               .replace("rivers_lake_centerlines", "rivers") \
               .replace("populated_places_simple", "places")
    fc = slim(path, keep, where=where)
    out[key] = fc
    print(f"  {key:12s} {len(fc['features']):5d} 要素")

os.makedirs(OUT, exist_ok=True)
payload = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
esc = payload.replace("\\", "\\\\").replace("'", "\\'")
p = os.path.join(OUT, "basemap.js")
with open(p, "w", encoding="utf-8") as f:
    f.write(f"window.BASEMAP=JSON.parse('{esc}');\n")
print(f"\n  data/basemap.js  {os.path.getsize(p)/1048576:.2f} MB")

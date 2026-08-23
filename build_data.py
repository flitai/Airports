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
"""把 OurAirports 原始 CSV 编译成单页面直接消费的紧凑 JS 数据文件。

输出为 .js 而非 .json：单页面要支持 file:// 双击打开，而 fetch/XHR 在
file:// 下被浏览器阻止，经典 <script src> 标签则不受限。
"""
import csv, json, re, sys, collections, os

SRC = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(SRC, "data")
csv.field_size_limit(10_000_000)

# ---------- 道面归一 ----------
# 原始 565 种写法：含 -G/-F/-P/-E 状态后缀、复合材质、多语言。
SURFACES = ["asphalt", "concrete", "paved", "grass", "gravel", "dirt", "water", "snow", "unknown"]
SURF_IDX = {s: i for i, s in enumerate(SURFACES)}
SURF_ZH = {"asphalt": "沥青", "concrete": "混凝土", "paved": "其他硬质铺装", "grass": "草地",
           "gravel": "砾石", "dirt": "土/沙", "water": "水面", "snow": "雪/冰", "unknown": "未知"}

SURF_RULES = [
    ("water",    r"WATER|WAT\b|SEA|LAKE"),
    ("snow",     r"SNOW|ICE|SKI"),
    ("asphalt",  r"ASP|BIT|TAR|MACADAM|MAC\b|TARMAC"),
    ("concrete", r"CONC|CON\b|CEMENT"),
    ("grass",    r"TURF|GRAS|GRS|GRE\b|SOD|LAWN"),
    ("gravel",   r"GVL|GRVL|GRAVEL|GRV|CORAL|SHELL|PICARRA|PIÇARRA|LATERITE|STONE"),
    ("dirt",     r"DIRT|EARTH|SAND|CLAY|SOIL|GROUND|MUD|SILT|LOAM"),
    ("paved",    r"PAVED|PEM|MATS|METAL|MET\b|WOOD|STEEL|ALUMIN|BRICK|PSP|ROOF|DECK|PAD|COMPOSITE|RUBBER|TREATED"),
]
SURF_RE = [(k, re.compile(p)) for k, p in SURF_RULES]
_cond = re.compile(r"[-/ ]+(G|F|P|E|X|GOOD|FAIR|POOR|EXCELLENT)$")
# 单字母码含义不可靠（约 3.3%），宁可标未知也不猜
_ambiguous = {"UNK", "UNKNOWN", "U", "N/A", "NONE", "X", "N", "G", "C", "S", "B", "L"}


def norm_surface(raw):
    s = (raw or "").strip().upper()
    if not s or s in _ambiguous:
        return "unknown"
    s = _cond.sub("", s)
    for key, rx in SURF_RE:
        if rx.search(s):
            return key
    return "unknown"


HARD = {SURF_IDX["asphalt"], SURF_IDX["concrete"], SURF_IDX["paved"]}
WIKI_PREFIX = "https://en.wikipedia.org/wiki/"
TYPES = ["large_airport", "medium_airport", "small_airport",
         "heliport", "seaplane_base", "balloonport", "closed"]
TYPE_IDX = {t: i for i, t in enumerate(TYPES)}
CLOSED_T = TYPE_IDX["closed"]


def rank_of(type_idx, sched):
    """zoom 分级用的重要度：数字越小越早出现在地图上。"""
    if type_idx == 0:                      # large_airport
        return 0
    if type_idx == 1 or sched:             # medium_airport 或有定期航班
        return 1
    if type_idx == 2:                      # small_airport
        return 2
    return 3                               # heliport / seaplane_base / balloonport


def num(v, cast=float):
    try:
        return cast(v)
    except (TypeError, ValueError):
        return None


def read(name, **kw):
    with open(os.path.join(SRC, name), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, **kw))


print("reading csv ...", file=sys.stderr)
airports  = read("airports.csv")
runways   = read("runways.csv")
freqs     = read("airport-frequencies.csv")
countries = {r["code"]: r["name"] for r in read("countries.csv")}
regions   = {r["code"]: r["name"] for r in read("regions.csv")}

# airport-comments.csv 表头用 ", "（逗号+空格）分隔，不加 skipinitialspace 会得到
# ' "airportRef"' 这样的 key，取值恒为 None。本版详情面板不展示评论，故只做解析
# 校验、不输出；若日后要用，去掉下面的丢弃即可。
_comments = read("airport-comments.csv", skipinitialspace=True)
assert "airportRef" in _comments[0], "comments CSV 表头解析失败"
del _comments

# ---------- 按机场聚合明细 ----------
# 跑道存为定长数组而非对象，省掉 48k 份重复 key（实测 5.55MB -> 1.44MB）
# [le, he, length_ft, width_ft, surf_idx, surf_raw, lighted, closed]
rw_by = collections.defaultdict(list)
surf_stat = collections.Counter()
for r in runways:
    cls = norm_surface(r["surface"])
    surf_stat[cls] += 1
    rw_by[r["airport_ref"]].append([
        r["le_ident"], r["he_ident"],
        num(r["length_ft"], int), num(r["width_ft"], int),
        SURF_IDX[cls], r["surface"] or "",
        1 if r["lighted"] == "1" else 0,
        1 if r["closed"] == "1" else 0,
    ])

# [type, description, frequency_mhz]
fq_by = collections.defaultdict(list)
for r in freqs:
    fq_by[r["airport_ref"]].append([r["type"], r["description"], r["frequency_mhz"]])

# ---------- 字典编码 ----------
# country/region 两列高度重复，存整数索引 + 字典表（省约 0.8MB）
cty_list, cty_idx = [], {}
rgn_list, rgn_idx = [], {}


def intern(code, lst, idx):
    if code not in idx:
        idx[code] = len(lst)
        lst.append(code)
    return idx[code]


# ---------- 组装点位层 ----------
COLS = ["id", "ident", "name", "type", "rank", "lon", "lat", "elev",
        "cty", "rgn", "muni", "iata", "sched", "rwlen", "hard"]
open_rows, closed_rows, details = [], [], {}

for a in airports:
    aid = a["id"]
    ti = TYPE_IDX.get(a["type"], CLOSED_T)
    sched = a["scheduled_service"] == "yes"
    rws = rw_by.get(aid, [])
    lens = [r[2] for r in rws if r[2]]

    row = [
        int(aid), a["ident"], a["name"], ti, rank_of(ti, sched),
        round(float(a["longitude_deg"]), 5), round(float(a["latitude_deg"]), 5),
        num(a["elevation_ft"], int),
        intern(a["iso_country"], cty_list, cty_idx),
        intern(a["iso_region"], rgn_list, rgn_idx),
        a["municipality"], a["iata_code"],
        1 if sched else 0,
        max(lens) if lens else None,
        1 if any(r[4] in HARD for r in rws) else 0,
    ]
    (closed_rows if ti == CLOSED_T else open_rows).append(row)

    d = {}
    if rws:
        d["r"] = rws
    if aid in fq_by:
        d["f"] = fq_by[aid]
    # wiki 链接 16,730 条几乎全是同一前缀，剥掉由前端还原
    wiki = a["wikipedia_link"] or ""
    if wiki.startswith(WIKI_PREFIX):
        d["w"] = wiki[len(WIKI_PREFIX):]
    elif wiki:
        d["W"] = wiki
    if a["home_link"]:
        d["h"] = a["home_link"]
    # gps_code 85% / local_code 48% 与 ident 相同。相同的存哨兵 1（前端回退到
    # ident），不同的存原值，字段缺失则表示该机场确实没有这个代码——三态必须
    # 可区分，否则前端会把 ident 当成不存在的代码显示出来。
    if a["gps_code"]:
        d["g"] = 1 if a["gps_code"] == a["ident"] else a["gps_code"]
    if a["local_code"]:
        d["l"] = 1 if a["local_code"] == a["ident"] else a["local_code"]
    # ident 是 OurAirports 的主键，不等于 ICAO 代码：全库 784 条两者不一致，新机场
    # 往往先拿到 IN-0276 这样的伪码当 ident（诺伊达，真实 ICAO 是 VIND）。只存不一致
    # 的那些，相同的让前端回退到 ident。
    if a["icao_code"] and a["icao_code"] != a["ident"]:
        d["c"] = a["icao_code"]
    if d:
        details[int(aid)] = d

# ---------- 输出 ----------
os.makedirs(OUT, exist_ok=True)


def dump_js(obj, name, var):
    """写成 window.X = JSON.parse('...')：JSON.parse 解析字符串字面量比让 JS
    引擎解析同等大小的对象字面量快数倍。用单引号包裹是因为 JSON 结构里全是
    双引号、单引号极少，转义开销几乎为零（双引号包裹要多付 20% 体积）。"""
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    esc = payload.replace("\\", "\\\\").replace("'", "\\'")
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"window.{var}=JSON.parse('{esc}');\n")
    return os.path.getsize(path)


s_meta = dump_js({
    "cols": COLS, "types": TYPES,
    "surfaces": SURFACES, "surfaceZh": SURF_ZH,
    "wikiPrefix": WIKI_PREFIX,
    "ctyCodes": cty_list,
    "ctyNames": [countries.get(c, c) for c in cty_list],
    "rgnCodes": rgn_list,
    "rgnNames": [regions.get(c, c) for c in rgn_list],
}, "meta.js", "META")
s_ap  = dump_js(open_rows,   "airports.js", "AP")
s_cl  = dump_js(closed_rows, "closed.js",   "CLOSED")
s_dt  = dump_js(details,     "details.js",  "DT")

MB = 1048576
print(f"\n  meta.js      {s_meta/1024:7.1f} KB   {len(cty_list)} 国家 / {len(rgn_list)} 地区")
print(f"  airports.js  {s_ap/MB:7.2f} MB   {len(open_rows)} 个在用机场")
print(f"  closed.js    {s_cl/MB:7.2f} MB   {len(closed_rows)} 个已关闭机场")
print(f"  details.js   {s_dt/MB:7.2f} MB   {len(details)} 个机场有明细"
      f"（跑道 {len(rw_by)} / 频率 {len(fq_by)}）")
print(f"  {'首屏合计':<12} {(s_meta+s_ap+s_dt)/MB:7.2f} MB")

print("\n道面归一结果:")
tot = sum(surf_stat.values())
for k, v in surf_stat.most_common():
    print(f"   {k:10s} {SURF_ZH[k]:8s} {v:6d}  {v/tot:5.1%}")

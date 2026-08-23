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
"""
下载 FAA d-TPP 元数据，抽出「ICAO → 航图清单」，生成 data/faa.js。

美国的终端程序图（d-TPP）是公开免费的：PDF 无需登录，响应头里既没有
X-Frame-Options 也没有 CSP，可以直接 <iframe> 嵌进页面看。其他国家的
AIP 各有各的门槛，所以这一套只覆盖美国，别处仍走 ChartFox 外链。

    python3 build_faa.py            # 用当前生效的周期
    python3 build_faa.py --cycle 2609

⚠️ 需要每 28 天重跑一次。FAA 只把最近三个 AIRAC 周期留在服务器上（实测
2608/2607/2606 可取，2603 已经 404），过期后页面里的链接会集体失效。
前端会读 faa.js 里的 to 日期，过期时在界面上给出提示。
"""

import argparse, json, os, sys, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

SRC = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(SRC, "data")
BASE = "https://aeronav.faa.gov/d-tpp"
META_URL = BASE + "/{cycle}/xml_data/d-TPP_Metafile.xml"

# FAA 的 chart_code 归到与航图界通行的五个大类，前端按这个顺序分组显示。
# 未在表里的代码一律落到 0（综合），不会因为 FAA 新增代码而丢图。
GROUPS = ["综合", "地面", "离场", "进场", "进近"]
CODE_GROUP = {
    "APD": 1,   # Airport Diagram 机场图
    "HOT": 1,   # Hot Spot 热点图
    "DP":  2,   # Departure Procedure 离场程序
    "ODP": 2,   # Obstacle Departure Procedure 障碍物离场程序
    "STR": 3,   # Standard Terminal Arrival 标准进场
    "IAP": 4,   # Instrument Approach Procedure 仪表进近
    # MIN(起降最低标准) / LAH(land and hold short) / DAU 等归入「综合」
}


def human(n):
    return f"{n/1048576:.2f} MB" if n >= 1048576 else f"{n/1024:.1f} KB"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "airports-map/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def current_cycle():
    """FAA 不提供「当前周期」接口。从今天往前后各试几个周期号，取 XML 里
    from_edate/to_edate 覆盖今天的那个。周期号形如 YYNN，NN 是当年第几个。"""
    today = datetime.now(timezone.utc).date()
    yy = today.year % 100
    cands = [f"{yy:02d}{n:02d}" for n in range(1, 15)] + \
            [f"{yy-1:02d}{n:02d}" for n in range(11, 15)]
    for c in reversed(cands):
        try:
            head = fetch_head(c)
        except Exception:
            continue
        if head and head[0] <= today <= head[1]:
            return c
    raise SystemExit("找不到当前生效的 AIRAC 周期，请用 --cycle 手动指定")


def parse_edate(s):
    # 形如 "0901Z  08/06/26"
    try:
        return datetime.strptime(s.split()[-1], "%m/%d/%y").date()
    except Exception:
        return None


def fetch_head(cycle):
    """只取 XML 开头几 KB，读根节点上的周期起止日期，避免为了试探下载 16 MB。"""
    req = urllib.request.Request(META_URL.format(cycle=cycle),
                                 headers={"User-Agent": "airports-map/1.0",
                                          "Range": "bytes=0-2047"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            chunk = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError:
        return None
    i = chunk.find("<digital_tpp")
    if i < 0:
        return None
    seg = chunk[i:chunk.find(">", i)]
    f = t = None
    for key in ("from_edate", "to_edate"):
        j = seg.find(key + '="')
        if j < 0:
            return None
        v = seg[j + len(key) + 2: seg.find('"', j + len(key) + 2)]
        if key == "from_edate":
            f = parse_edate(v)
        else:
            t = parse_edate(v)
    return (f, t) if f and t else None


def dump_js(obj, name, var):
    """与 build_data.py 同一套：JSON.parse('...') 比对象字面量解析快数倍，
    单引号包裹省去双引号转义。"""
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    esc = payload.replace("\\", "\\\\").replace("'", "\\'")
    path = os.path.join(OUT, name)
    # 先写 .tmp 再原子改名：serve.py 启动时会自动调用本脚本，中途 Ctrl-C 或
    # 断网都不能留下半截的 faa.js —— 那会让整个航图功能挂掉。
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(f"window.{var}=JSON.parse('{esc}');\n")
    os.replace(tmp, path)
    return os.path.getsize(path)


def main():
    ap = argparse.ArgumentParser(description="生成美国 FAA 航图索引 data/faa.js")
    ap.add_argument("--cycle", help="AIRAC 周期号，形如 2608。不给就自动探测")
    args = ap.parse_args()

    cycle = args.cycle or current_cycle()
    span = fetch_head(cycle)
    if not span:
        raise SystemExit(f"周期 {cycle} 的元数据取不到（FAA 只保留最近三个周期）")
    frm, to = span
    print(f"AIRAC 周期 {cycle}   有效期 {frm} → {to}")

    print("下载 d-TPP 元数据 …")
    root = ET.fromstring(fetch(META_URL.format(cycle=cycle)))

    charts, n = {}, 0
    for apt in root.iter("airport_name"):
        icao = apt.get("icao_ident")
        if not icao:
            continue
        lst = []
        for rec in apt.iter("record"):
            pdf = (rec.findtext("pdf_name") or "").strip()
            name = (rec.findtext("chart_name") or "").strip()
            if not pdf or not name:
                continue
            code = (rec.findtext("chart_code") or "").strip()
            # 去掉 .PDF 后缀，前端拼回来——两万条省下 80 KB
            if pdf.upper().endswith(".PDF"):
                pdf = pdf[:-4]
            lst.append([CODE_GROUP.get(code, 0), name, pdf])
            n += 1
        if lst:
            charts[icao] = lst

    os.makedirs(OUT, exist_ok=True)
    size = dump_js({"cycle": cycle, "from": frm.isoformat(), "to": to.isoformat(),
                    "groups": GROUPS, "ap": charts}, "faa.js", "FAA")

    print(f"data/faa.js     {human(size)}   {len(charts):,} 个机场 / {n:,} 张航图")
    by = {}
    for lst in charts.values():
        for c in lst:
            by[GROUPS[c[0]]] = by.get(GROUPS[c[0]], 0) + 1
    print("  " + "  ".join(f"{k} {v:,}" for k, v in
                           sorted(by.items(), key=lambda kv: -kv[1])))
    if to <= datetime.now(timezone.utc).date():
        print("\n⚠️  这个周期已经过期，页面会提示航图可能失效。请用当前周期重跑。")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""假的 dumpvdl2：测试里顶替真解码器，用法与它一样（从标准输入读采样，往标准输出写 JSON）。

读法照着真的来：一次读 320000 字节，读不满就当文件结束、退出。每读够一秒的采样
吐一条报文，轮着用下面那几条样例。

环境变量：
  FAKE_VDL2_LOG   把启动参数和读到的字节数写进这个文件（JSON），测试靠它查
  FAKE_VDL2_MODE  stall  不读输入（扮演卡死的解码器）
                  exit1  一启动就以退出码 1 退出
                  flood  一启动先吐 2000 行
                  junk   正常报文之间夹几行坏的

样例报文的来历：X.25 那条是 dumpvdl2 2.7.0 解它自带的模型信号（test/vdl2_model_16b_1050kHz.wav）
的真实输出，地址是上游测试向量里的；ACARS 和 XID 两条是照 libacars / dumpvdl2 的
JSON 格式化代码拼的，地址、注册号、航班号都是编的。
"""
import json
import os
import sys
import time

BLOCK = 320000
PER_MSG = 8_400_000                      # 一秒的采样：2.1 MS/s × 4 字节


def frame(avlc, freq=136975000):
    now = time.time()
    return {"vdl2": {"app": {"name": "dumpvdl2", "ver": "2.7.0"},
                     "t": {"sec": int(now), "usec": int(now % 1 * 1e6)}, "freq": freq,
                     "burst_len_octets": 120, "hdr_bits_fixed": 0, "octets_corrected_by_fec": 0, "idx": 0,
                     "sig_level": -24.5, "noise_level": -46.1, "freq_skew": 0.2, "avlc": avlc}}


WEATHER = "METAR KROC 081354Z 03014KT 6SM -RA BR BKN004 OVC010 01/01 A2966\n"
SAMPLES = [
    # 地面站 → 飞机，上层协议没认出来，载荷是一段气象报文（真实输出，见文件头）
    lambda: frame({"src": {"addr": "345678", "type": "Ground station", "status": "On ground"},
                   "dst": {"addr": "A23721", "type": "Aircraft"}, "cr": "Command", "frame_type": "I",
                   "rseq": 2, "sseq": 1, "poll": True,
                   "x25": {"err": True, "unknown_proto": {"data": list(WEATHER.encode())}}}),
    # 飞机 → 地面站的 ACARS
    lambda: frame({"src": {"addr": "ABCDEF", "type": "Aircraft", "status": "Airborne"},
                   "dst": {"addr": "10AB2C", "type": "Ground station"}, "cr": "Command", "frame_type": "I",
                   "rseq": 0, "sseq": 3, "poll": False,
                   "acars": {"err": False, "crc_ok": True, "more": False, "reg": ".B-TEST", "mode": "2",
                             "label": "H1", "blk_id": "5", "ack": "!", "flight": "TS1234", "msg_num": "D03",
                             "msg_num_seq": "A", "sublabel": "DF",
                             "msg_text": "#DFB/POS N3959.2E11625.4,ALT 9800\r\nFUEL 12.4 ETA 0312"}}),
    # 地面站的广播（GSIF）
    lambda: frame({"src": {"addr": "10AB2C", "type": "Ground station", "status": "On ground"},
                   "dst": {"addr": "FFFFFF", "type": "All stations"}, "cr": "Command", "frame_type": "U",
                   "cmd": "XID", "pf": False,
                   "xid": {"err": False, "type": "GSIF", "type_descr": "Ground Station Information Frame",
                           "pub_params": [{"name": "param_set_id", "value": "8885:1993"}],
                           "vdl_params": [{"name": "airport_coverage", "value": ["ZZZZ"]},
                                          {"name": "gs_location", "value": {"lat": 40.1, "lon": 116.6}}]}},
          136725000),
    # 飞机回的确认帧，没有上层内容
    lambda: frame({"src": {"addr": "ABCDEF", "type": "Aircraft", "status": "Airborne"},
                   "dst": {"addr": "10AB2C", "type": "Ground station"}, "cr": "Response", "frame_type": "S",
                   "cmd": "Receive Ready", "pf": False, "rseq": 4}),
]


def wrapper(folder, python=sys.executable):
    """→ 一个可以直接当「dumpvdl2 的路径」用的可执行文件，用当前的解释器跑这个脚本。"""
    path = os.path.join(folder, "fake-vdl2")
    with open(path, "w") as f:
        f.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (python, os.path.abspath(__file__)))
    os.chmod(path, 0o755)
    return path


def main():
    mode, log = os.environ.get("FAKE_VDL2_MODE", ""), os.environ.get("FAKE_VDL2_LOG")
    out, state = sys.stdout, {"argv": sys.argv[1:], "bytes": 0, "blocks": 0, "pid": os.getpid()}

    def note():
        if log:
            with open(log + ".tmp", "w") as f:
                json.dump(state, f)
            os.replace(log + ".tmp", log)

    note()
    sys.stderr.write("fake dumpvdl2\n")
    if mode == "exit1":
        sys.stderr.write("fake: refusing to run\n")
        sys.exit(1)
    if mode == "stall":
        time.sleep(600)
        return
    if mode == "flood":
        for i in range(2000):
            out.write(json.dumps(SAMPLES[i % len(SAMPLES)]()) + "\n")
        out.flush()
    due, i = PER_MSG, 0
    while True:
        chunk = sys.stdin.buffer.read(BLOCK)
        state["bytes"] += len(chunk)
        state["blocks"] += 1
        if state["blocks"] % 8 == 0 or len(chunk) < BLOCK:
            note()
        if len(chunk) < BLOCK:
            break
        if state["bytes"] >= due:
            due += PER_MSG
            if mode == "junk":
                out.write("not json\n")
                out.flush()
                out.buffer.write(b"\xff\xfe{\n")
                out.buffer.flush()
                out.write('{"vdl2": 3}\n[1, 2]\n')
            out.write(json.dumps(SAMPLES[i % len(SAMPLES)]()) + "\n")
            out.flush()
            i += 1


if __name__ == "__main__":
    main()

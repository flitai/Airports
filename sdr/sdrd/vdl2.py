"""VDL2 数据链：监管一个 dumpvdl2 子进程，把重采样后的采样经标准输入喂给它。

接法与 adsb.ReadsbDecoder 是同一个路数——我们持有接收机，解码器读管道，管道一断
它读到文件结束自己退出，从不碰 USB。不同的地方：

  * dumpvdl2 只吃 105 kHz 整数倍的采样率，所以喂之前先从 2.4 MS/s 重采样到
    2.1 MS/s（dsp.resample），16 位样本。
  * 它的输出是标准输出上一行一条的 JSON，不是文件。所以多一个读线程：不读的话
    管道写满，解码器就卡在那儿了。
  * 它在解出第一条报文之前什么都不输出，没有「我还活着」的信号。进程在、采样
    喂得进去，就算运行中。

三个不踩一次想不到的坑，都钉在 argv() 里：
  * --oversample 和 --sample-format 必须写在 --iq-file 之后，否则被悄悄重置成默认值；
  * 16 位格式要写 S16_LE，帮助里印的 S16LE 会被拒绝；
  * 显式给了 --centerfreq 它就不检查频道是不是在采样带宽里，调错了照样「运行」，
    只是什么都解不出。所以这边自己查一遍。

离线入口：python -m sdrd.vdl2 <录制.cu8>，把一段 I/Q 录制不按实时节拍地过一遍，
打印解出的报文。验证整条链、排查「是没信号还是解不出」时用。
"""
import collections
import datetime
import json
import os
import queue
import shutil
import subprocess
import threading
import time

from .dsp import to_complex
from .dsp.resample import Resampler, to_s16

OVERSAMPLE = 20
OUT_RATE = 105_000 * OVERSAMPLE          # 2.1 MS/s
REACH = OUT_RATE // 2 - 21_000           # 频道离中心最多这么远，dumpvdl2 自己的界限（没给 --centerfreq 时它才查）
LEVEL_FIX = 6.0                          # 喂过去的样本是半幅的（见 resample.S16_SCALE），它报的电平要补回来
TEXT_MAX = 1024
KEEP = 500                               # 内存里留多少条报文
SHOWN = 200                              # 一份快照里带多少条
AC_TTL_S = 1800                          # 一架飞机这么久没再出现，就从汇总里拿掉
PART_BYTES = 5_000_000                   # 日志一个文件写到这么大就换下一个


PREFIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".vdl2")
# dumpvdl2 自带的模型信号，sdr/tools/build-dumpvdl2.sh 把它留在这儿。合成信号源用它（synth.Vdl2Model）
MODEL_WAV = os.path.join(PREFIX, "share", "vdl2_model_16b_1050kHz.wav")


def find_dumpvdl2():
    """→ 解码器的路径，没有则 None。SDR_DUMPVDL2=none 表示「当它不存在」，测试用。"""
    env = os.environ.get("SDR_DUMPVDL2")
    if env:
        return None if env == "none" else env
    # sdr/tools/build-dumpvdl2.sh 装到这里。Homebrew 没有这个包，所以它排在 PATH 前面
    local = os.path.join(PREFIX, "bin", "dumpvdl2")
    return next((p for p in (local, shutil.which("dumpvdl2"), "/opt/homebrew/bin/dumpvdl2",
                             "/usr/local/bin/dumpvdl2") if p and os.path.exists(p)), None)


class Dumpvdl2Decoder:
    def __init__(self, run_dir, center_hz, freqs, binary=None):
        self.dir = os.path.join(run_dir, "vdl2")
        self.center, self.freqs = int(center_hz), [int(f) for f in freqs]
        self.binary = binary or find_dumpvdl2()
        self.proc = None
        self.q = None
        self.halt = None
        self.log = None
        self.dropped = 0                 # 管道写不动时丢掉的字节数：解码器卡住了才会涨
        self.fed = 0                     # 已经写进管道的字节数
        self.started = 0.0
        self.lines = collections.deque(maxlen=4000)      # 读线程放、引擎取；满了丢最旧的，绝不阻塞

    @property
    def available(self):
        return bool(self.binary)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def argv(self):
        return [self.binary, "--iq-file", "-", "--sample-format", "S16_LE", "--oversample", str(OVERSAMPLE),
                "--centerfreq", str(self.center), "--output", "decoded:json:file:path=-"] \
            + [str(f) for f in self.freqs]

    def start(self):
        far = [f for f in self.freqs if abs(f - self.center) > REACH]
        if far or not self.freqs:
            raise ValueError("数据链频率 %s 不在采样带宽里（中心 %d）" % (far, self.center))
        os.makedirs(self.dir, exist_ok=True)
        self.log = open(os.path.join(self.dir, "dumpvdl2.log"), "ab")
        # 标准错误进文件，不接管道：没人读的管道写满了会把它卡住
        self.proc = subprocess.Popen(self.argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log)
        self.q = queue.Queue(maxsize=64)                 # 64 块约 0.9 秒
        self.halt = threading.Event()
        self.dropped = self.fed = 0
        self.started = time.time()
        threading.Thread(target=self._write, args=(self.proc, self.q, self.halt),
                         name="vdl2-pipe", daemon=True).start()
        threading.Thread(target=self._read, args=(self.proc,), name="vdl2-out", daemon=True).start()

    def _write(self, proc, q, halt):
        # 重采样和写管道都在这个线程里：解码器万一卡住，被堵的是这里，不是 USB 回调。
        # 标准输入只由这个线程关。一次写 11 万字节，比管道的缓冲大，所以它平时多半正
        # 卡在写里；别的线程这时去关，会等在缓冲写入器的锁上，解码器卡死就永远等下去。
        rs = Resampler()
        try:
            while not halt.is_set():
                try:
                    block = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                if block is None:
                    break
                data = to_s16(rs.process(to_complex(block)))
                proc.stdin.write(data)
                self.fed += len(data)
        except (BrokenPipeError, ValueError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()                       # 文件结束 → dumpvdl2 自己退出
            except OSError:
                pass

    def _read(self, proc):
        # 绑定的是自己这个进程：旧实例收尾时吐出的最后几行不会混进新实例里
        lines = self.lines
        try:
            for raw in proc.stdout:
                lines.append(raw)
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass

    def feed(self, block):
        """在 USB 回调线程里调用，绝不阻塞。"""
        q = self.q
        if q is None:
            return
        try:
            q.put_nowait(block)
        except queue.Full:
            self.dropped += len(block)

    def take(self):
        """→ 上次取走之后新到的原始 JSON 行（字节串）。"""
        out, lines = [], self.lines
        while True:
            try:
                out.append(lines.popleft())
            except IndexError:
                return out

    def last_error(self):
        """解码器临死前在标准错误里说的最后一句，给状态条显示。"""
        try:
            with open(os.path.join(self.dir, "dumpvdl2.log"), "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 600))
                rows = [r.strip() for r in f.read().decode("utf-8", "replace").splitlines() if r.strip()]
            return rows[-1][:200] if rows else None
        except OSError:
            return None

    def stop(self, wait=True, drain=False):
        """停掉解码进程。wait=False 时立即返回，收尾放到后台线程。

        drain=True：把队列里排着的采样喂完再关（离线过一段录制时用）。
        """
        proc, q, halt, log = self.proc, self.q, self.halt, self.log
        self.proc = self.q = self.halt = self.log = None
        if proc is None:
            return
        if drain:
            q.put(None)
        else:
            halt.set()
            try:
                q.put_nowait(None)
            except queue.Full:
                pass

        def reap():
            # 写线程看到通知就关管道，解码器读到文件结束自己走。信号只是它不肯走时的
            # 后手——解码器卡死的话，写线程正堵在写里，也要靠这一下把它放出来
            for step in (proc.terminate, proc.kill):
                try:
                    proc.wait(600 if drain else 3)
                    break
                except subprocess.TimeoutExpired:
                    step()
            proc.wait()                                  # 无论怎么走的都要回收，别留僵尸进程
            if log:
                log.close()

        if wait:
            reap()
        else:
            threading.Thread(target=reap, name="vdl2-reap", daemon=True).start()


# ---- 报文 ----
def _addr(a):
    if not isinstance(a, dict) or not a.get("addr"):
        return None
    kind = {"Aircraft": "ac", "Ground station": "gs"}.get(a.get("type"), "all")
    out = {"a": str(a["addr"]).lower(), "k": kind}       # 小写：地图上飞机的地址（readsb、联网数据）都是小写
    if a.get("status"):
        out["st"] = "air" if a["status"] == "Airborne" else "gnd"
    return out


def _level(v):
    return round(float(v) + LEVEL_FIX, 1) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _lines(obj, depth=0):
    """把一段认不出结构的 JSON 摊成「键: 值」的几行，给人看个大概。"""
    pad = "  " * depth
    if isinstance(obj, dict):
        if set(obj) == {"name", "value"}:                # XID 参数是 {"name":…, "value":…} 这个样子
            obj = {str(obj["name"]): obj["value"]}
        out = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                out.append("%s%s:" % (pad, k))
                out += _lines(v, depth + 1)
            else:
                out.append("%s%s: %s" % (pad, k, json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v))
        return out
    if isinstance(obj, list):
        if obj and all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 255 for v in obj):
            # 字节串。多半是可打印字符就当文字给人看（没认出来的上层协议里常是气象报文），否则给十六进制
            raw = bytes(obj)
            if sum(32 <= b < 127 or b in (9, 10, 13) for b in raw) >= 0.9 * len(raw):
                return [pad + r for r in raw.decode("ascii", "replace").splitlines() if r.strip()]
            return [pad + raw.hex(" ")]
        out = []
        for v in obj:
            out += _lines(v, depth) if isinstance(v, (dict, list)) else ["%s%s" % (pad, v)]
        return out
    return ["%s%s" % (pad, obj)]


AVLC_KEYS = {"src", "dst", "cr", "frame_type", "cmd", "pf", "rseq", "sseq", "poll"}
ACARS_KEYS = {"err", "crc_ok", "more", "reg", "mode", "label", "blk_id", "ack", "flight", "msg_num",
              "msg_num_seq", "sublabel", "mfi", "msg_text"}
NAMES = {"acars": "ACARS", "xid": "XID", "x25": "X.25"}


def parse(raw):
    """dumpvdl2 的一行 JSON → 精简报文；不是一条报文就返回 None。绝不抛异常。

    结果里的字段：t 时刻，hz 频率，lvl / nf 信号与底噪电平（dBFS），src / dst
    两端（a 地址，k 是 ac / gs / all，st 是 air / gnd），hex 这条报文属于哪架飞机，
    dir 是 down（飞机发的）或 up（地面站发的），kind 类别，sum 一行摘要，
    text 正文，reg / flight / label 是 ACARS 里带的注册号、航班号、标签。
    """
    try:
        doc = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw)
        v = doc.get("vdl2")
        avlc = v.get("avlc")
        if not isinstance(avlc, dict):
            return None
        t = v.get("t") or {}
        src, dst = _addr(avlc.get("src")), _addr(avlc.get("dst"))
        msg = {"t": round(float(t.get("sec", 0)) + float(t.get("usec", 0)) / 1e6, 3),
               "hz": int(v.get("freq") or 0), "lvl": _level(v.get("sig_level")), "nf": _level(v.get("noise_level")),
               "src": src, "dst": dst, "hex": None, "dir": None, "kind": "avlc", "sum": "", "text": ""}
        plane = next((a for a in (src, dst) if a and a["k"] == "ac"), None)
        if plane:
            msg["hex"] = plane["a"]
        if src:
            msg["dir"] = "down" if src["k"] == "ac" else "up" if src["k"] == "gs" else None

        layer = next((k for k in avlc if k not in AVLC_KEYS and isinstance(avlc[k], dict)), None)
        body = avlc.get(layer) if layer else None
        if layer == "acars":
            msg["kind"] = "acars"
            text = str(body.get("msg_text") or "")
            for key, name in (("reg", "reg"), ("flight", "flight"), ("label", "label")):
                val = str(body.get(key) or "").strip().lstrip(".")
                if val:
                    msg[name] = val
            apps = [k for k in body if k not in ACARS_KEYS and isinstance(body[k], (dict, list))]
            first = next((r.strip() for r in text.splitlines() if r.strip()), "")
            msg["sum"] = " ".join(x for x in ("ACARS", msg.get("label"), "/".join(apps), first[:80]) if x)
            extra = [r for k in apps for r in ["%s:" % k] + _lines(body[k], 1)]
            msg["text"] = "\n".join([text] * bool(text) + extra)
        elif layer == "xid":
            msg["kind"] = "xid"
            msg["sum"] = "XID " + str(body.get("type_descr") or body.get("type") or "")
            msg["text"] = "\n".join(_lines({k: body[k] for k in body if k not in ("err", "type", "type_descr")}))
        elif layer:
            msg["kind"] = layer
            inner = [k for k in body if isinstance(body[k], dict)]
            msg["sum"] = " ".join([NAMES.get(layer, layer.upper())]
                                  + ["未识别的上层协议" if k == "unknown_proto" else k.upper() for k in inner[:2]])
            rest = {k: v for k, v in body.items() if k not in ("err", "unknown_proto")}
            raw_part = body.get("unknown_proto")
            # 没认出来的上层协议只有一坨字节：直接给内容，不必再套两层键名
            payload = _lines(raw_part["data"]) if isinstance(raw_part, dict) and isinstance(raw_part.get("data"), list) \
                else (_lines(raw_part) if raw_part else [])
            msg["text"] = "\n".join(_lines(rest) + payload)
        else:
            kind = avlc.get("frame_type")
            msg["sum"] = " ".join(str(x) for x in (avlc.get("cmd") or ("I 帧" if kind == "I" else "帧"),) if x)
        msg["text"] = msg["text"][:TEXT_MAX]
        return msg
    except Exception:                    # 外部程序的输出，什么形状都可能有；一行坏的不该带走读它的人
        return None


class Log:
    """最近的报文，外加按飞机、按频率的汇总。只在引擎线程里用。"""

    def __init__(self, keep=KEEP, ac_ttl=AC_TTL_S):
        self.keep, self.ac_ttl = keep, ac_ttl
        self.clear()

    def clear(self):
        self.msgs = collections.deque(maxlen=self.keep)
        self.seq = self.total = 0
        self.ac = {}                     # 地址 → {hex, first, last, n, reg, flight}
        self.freq = {}                   # 频率 → [条数, 最近时刻]
        self.recent = collections.deque()    # 最近一分钟里各条报文的时刻

    def add(self, msg):
        self.seq += 1
        self.total += 1
        msg["n"] = self.seq
        self.msgs.append(msg)
        f = self.freq.setdefault(msg["hz"], [0, 0.0])
        f[0] += 1
        f[1] = max(f[1], msg["t"])
        self.recent.append(msg["t"])
        if msg.get("hex"):
            a = self.ac.setdefault(msg["hex"], {"hex": msg["hex"], "first": msg["t"], "last": msg["t"], "n": 0})
            a["n"] += 1
            a["last"] = max(a["last"], msg["t"])
            for k in ("reg", "flight"):
                if msg.get(k):
                    a[k] = msg[k]

    def _expire(self, now):
        for h in [h for h, a in self.ac.items() if now - a["last"] > self.ac_ttl]:
            del self.ac[h]
        while self.recent and now - self.recent[0] > 60:
            self.recent.popleft()

    def snapshot(self, now, shown=SHOWN):
        self._expire(now)
        rows = list(self.msgs)[-shown:][::-1]
        return {"seq": self.seq, "list": rows,
                "ac": sorted(self.ac.values(), key=lambda a: -a["last"])}

    def stats(self, now):
        self._expire(now)
        return {"total": self.total, "per_min": len(self.recent), "ac": len(self.ac),
                "freqs": {hz: {"n": n, "last": last} for hz, (n, last) in self.freq.items()}}


class Journal:
    """把解码器的原始 JSON 行按天写进文件，超过配额删最旧的。只在引擎线程里用。

    文件名 <UTC 日期>-<序号>.jsonl。一个文件写到 PART_BYTES 或跨日就换下一个，
    这样配额能按文件删，不必去截一个大文件的头。
    """

    def __init__(self, path, quota_bytes):
        self.dir, self.quota = path, int(quota_bytes)
        self._count()

    def _count(self):
        # 大小记在内存里。状态快照每秒要用它一次，那条路上不该有磁盘调用
        files = self._files()
        self._stats = {"bytes": sum(os.path.getsize(p) for p in files), "quota_bytes": self.quota,
                       "files": len(files)}

    def _files(self):
        try:
            names = sorted(n for n in os.listdir(self.dir) if n.endswith(".jsonl"))
        except OSError:
            return []
        return [os.path.join(self.dir, n) for n in names]

    def write(self, lines, now=None):
        if self.quota <= 0 or not lines:
            return
        os.makedirs(self.dir, exist_ok=True)
        day = datetime.datetime.fromtimestamp(now or time.time(), datetime.timezone.utc).strftime("%Y%m%d")
        today = [p for p in self._files() if os.path.basename(p).startswith(day + "-")]
        path = today[-1] if today else os.path.join(self.dir, "%s-%03d.jsonl" % (day, 0))
        if today and os.path.getsize(path) >= PART_BYTES:
            path = os.path.join(self.dir, "%s-%03d.jsonl" % (day, int(os.path.basename(path)[9:12]) + 1))
        with open(path, "ab") as f:
            for raw in lines:
                f.write(raw if raw.endswith(b"\n") else raw + b"\n")
        files = self._files()
        total = sum(os.path.getsize(p) for p in files)
        while total > self.quota and len(files) > 1:     # 正在写的那个留着
            old = files.pop(0)
            total -= os.path.getsize(old)
            os.remove(old)
        self._stats = {"bytes": total, "quota_bytes": self.quota, "files": len(files)}

    def tail(self, n=KEEP):
        """→ 最近 n 行原始 JSON，旧的在前。启动时用它把报文列表接回来。"""
        out = []
        for path in reversed(self._files()):
            try:
                with open(path, "rb") as f:
                    rows = f.read().splitlines()
            except OSError:
                continue
            out = rows[-(n - len(out)):] + out
            if len(out) >= n:
                break
        return out

    def stats(self):
        return dict(self._stats)

    def clear(self):
        for p in self._files():
            try:
                os.remove(p)
            except OSError:
                pass
        self._count()


# ---- 离线入口 ----
def decode_file(path, center_hz=None, freqs=None, target_hz=None, binary=None, run_dir=None):
    """把一段 cu8 录制不按实时节拍地过一遍解码器 → (报文列表, 喂进去的字节数)。

    录制的中心频率从同名的 JSON 里取（也可以直接给）。它多半不是数据链窗口的中心，
    所以先用回放的那套办法把频谱平移过去——与在界面里回放这段录制走的是同一条路。
    """
    import tempfile

    import numpy as np

    from .device import BUF_LEN
    from .dsp import channels as chan
    from .sources import shift_block

    if center_hz is None:
        with open(os.path.splitext(path)[0] + ".json", encoding="utf-8") as f:
            center_hz = json.load(f)["center_hz"]
    target = int(target_hz or chan.VDL2_CENTER)
    shift = int(center_hz) - target
    dec = Dumpvdl2Decoder(run_dir or tempfile.mkdtemp(prefix="vdl2-"), target, freqs or chan.VDL2_FREQS, binary)
    if not dec.available:
        raise FileNotFoundError("找不到 dumpvdl2：先运行 sdr/tools/build-dumpvdl2.sh")
    dec.start()
    rng = np.random.default_rng(3)
    with open(path, "rb") as f:
        while True:
            raw = f.read(BUF_LEN)
            if len(raw) < BUF_LEN:
                break
            dec.q.put(raw if shift == 0 else shift_block(raw, shift, rng))
    # dumpvdl2 一次读 320000 字节，读不满就当文件结束、把那不满的一截丢掉。垫几块静音把尾巴顶出去
    for _ in range(4):
        dec.q.put(bytes([128]) * BUF_LEN)
    dec.stop(wait=True, drain=True)
    time.sleep(0.2)                      # 读线程把最后几行收完
    return [m for m in (parse(r) for r in dec.take()) if m], dec.fed


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="python -m sdrd.vdl2", description="离线解一段 I/Q 录制里的 VDL2 报文")
    ap.add_argument("file", help="cu8 录制；同名的 .json 里要有 center_hz，或者用 --center 给")
    ap.add_argument("--center", type=float, help="录制的中心频率，Hz")
    args = ap.parse_args(argv)
    t0 = time.time()
    msgs, fed = decode_file(args.file, args.center)
    for m in msgs:
        who = m.get("flight") or m.get("reg") or m.get("hex") or "—"
        print("%.3f  %.3f MHz  %5s dBFS  %-4s %-8s %s" % (m["t"], m["hz"] / 1e6, m["lvl"], m["dir"] or "", who, m["sum"]))
        for row in m["text"].splitlines():
            print("      " + row)
    print("—— %d 条报文；喂了 %.1f 秒的采样，用时 %.1f 秒" % (len(msgs), fed / 4 / OUT_RATE, time.time() - t0))
    return 0 if msgs else 1


if __name__ == "__main__":
    raise SystemExit(main())

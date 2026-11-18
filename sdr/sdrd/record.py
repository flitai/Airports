"""话音触发录音：有人说话就自动存一段，一次通话一个文件。

借鉴 SDR Lab 的 trigger.py：「载波能量不算数，要有话音才开录」「开录时把此前
一小段也带上」「一段有上限，到了上限要等这次发射结束才能再触发」这三条规矩
原样保留。代码是新写的，差别在录什么：
  * 原版存整窗的原始 I/Q（每秒 4.8 MB），事后再解调导出。这里直接存解调好的
    16 kHz 单声道音频（每秒 32 KB），旁边放一份 JSON 说明是哪个频道、什么时候、
    信号多强。一分钟的通话不到 2 MB，放得住一整天。
  * 原版为了判断有没有人声要装一整套语音识别库。这里用解调链顺带算出的
    「载波上有没有调制」（见 dsp/am.py 的 VoiceDetector）。
  * 前置留白取自音频而不是原始采样：解调本来就一直在跑，留住最近半秒的输出
    就够了，不必为此再存一份每秒 4.8 MB 的环形缓冲。

Clipper 是状态机，活在 sdr-dsp 线程里，每块采样喂一次，攒在内存里；一段结束
才整段交给 Store。Store 有自己的线程，写盘、删文件、守配额都在那儿做——解调
线程绝不碰磁盘。

另有原始 I/Q 的录制（IqStore），用来回放：整窗的 8 位采样原样写成 .cu8，每秒
4.8 MB。它和音频片段分开放、分开算配额——一分钟的 I/Q 抵得上两个半小时的音频。
录的是接收机当时看到的整个窗口，所以回放时可以换着听窗口里的任何一个频道。
"""
import json
import os
import queue
import re
import threading
import time
import wave

import numpy as np

from .dsp import AUDIO_RATE, RATE

PRE_S = 0.5                              # 前置留白：开录时带上此前这么长的音频
MAX_S = 60.0                             # 一段最长这么久。通播这类连续播发的台，录一轮就够了
MIN_SPEECH_S = 0.3                       # 说话不足这么久的不存：多半是一下噪声碰巧像调制
IQ_BYTES_PER_S = 2 * RATE                # 8 位 I、8 位 Q
IQ_BACKLOG = 256                         # 等着写盘的块数上限（约 3.5 秒、16 MB）。盘跟不上就丢块并记数
_STEM = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,80}$")


class Clipper:
    """一个频道上的录音状态机。

    等待：留着最近 PRE_S 的音频。静噪开着且判出话音 → 开录，把留着的那段放在最前面。
    录制：一块一块往后接。静噪关了（这次发射结束）、话音停了一秒多（载波还在但没人
          说话了）、录音被关掉，都结束这一段；到了 MAX_S 也结束，并且**锁住**：
          这次发射不结束就不再触发。没有这一条，一个连续播发的台会一段接一段把
          磁盘写满。
    feed 的返回值是一段录完的内容（字典），没有则为 None。带锁，是因为换频道、
    换窗口时引擎线程要过来把正在录的那段收尾。
    """
    def __init__(self, max_s=MAX_S):
        self.max_n = int(max_s * AUDIO_RATE)
        self.pre_n = int(PRE_S * AUDIO_RATE)
        self.ring, self.ring_n = [], 0
        self.active = None
        self.latched = False
        self._lock = threading.Lock()

    def feed(self, pcm, is_open, voice, speaking, enabled, level=None, afc_hz=None):
        """pcm：这一块的 int16 音频。voice 带一秒的保持，speaking 是这一块本身有没有调制。"""
        with self._lock:
            a = self.active
            if a is None:
                self.ring.append(pcm)
                self.ring_n += len(pcm)
                while self.ring and self.ring_n - len(self.ring[0]) >= self.pre_n:
                    self.ring_n -= len(self.ring.pop(0))
                if self.latched:
                    self.latched = bool(is_open and voice)       # 这次发射结束了才解锁
                elif enabled and is_open and voice:
                    self.active = {"parts": self.ring, "n": self.ring_n, "pre_n": self.ring_n,
                                   "start": time.time() - self.ring_n / AUDIO_RATE, "speech_n": 0,
                                   "peak_db": level, "afc_hz": afc_hz}
                    self.ring, self.ring_n = [], 0
                return None
            a["parts"].append(pcm)
            a["n"] += len(pcm)
            if is_open and speaking:
                a["speech_n"] += len(pcm)
                a["afc_hz"] = afc_hz
                if level is not None and (a["peak_db"] is None or level > a["peak_db"]):
                    a["peak_db"] = level
            if not enabled:
                return self._end("stopped")
            if not is_open:
                return self._end("closed")
            if not voice:
                return self._end("silence")
            if a["n"] - a["pre_n"] >= self.max_n:
                self.latched = True
                return self._end("limit")
            return None

    def finish(self, reason="retune"):
        """别的线程调：把正在录的那段就此收尾（换了频道、换了窗口、离开话音模式）。"""
        with self._lock:
            return self._end(reason) if self.active else None

    def seconds(self):
        a = self.active
        return None if a is None else round(a["n"] / AUDIO_RATE, 1)

    def _end(self, reason):
        a, self.active = self.active, None
        if a["speech_n"] < MIN_SPEECH_S * AUDIO_RATE:
            return None
        return {"pcm": np.concatenate(a["parts"]), "start": a["start"], "pre_s": round(a["pre_n"] / AUDIO_RATE, 2),
                "duration_s": round(a["n"] / AUDIO_RATE, 2), "speech_s": round(a["speech_n"] / AUDIO_RATE, 2),
                "peak_db": None if a["peak_db"] is None else round(float(a["peak_db"]), 1),
                "afc_hz": None if a["afc_hz"] is None else round(float(a["afc_hz"])), "end": reason}


class _Folder:
    """一个录制目录：每段一个数据文件加一份同名的 JSON。写盘和删除都在自己的线程里。

    配额按总字节数算，超了就从最旧的删起。它保证的是「开着录走开一晚上，磁盘不会
    满」，不是档案馆——要留的请自己从目录里拷走。
    """
    EXT = ""                                             # 数据文件的扩展名，四个字符

    def __init__(self, folder, quota_bytes, on_change=None):
        self.folder, self.quota = folder, int(quota_bytes)
        self.on_change = on_change or (lambda: None)
        self._lock = threading.Lock()
        self.items = self._scan()                        # 新的在前
        self.failed = 0
        self.q = queue.Queue()
        self.thread = threading.Thread(target=self._run, name="sdr-rec" + self.EXT, daemon=True)
        self.thread.start()

    # ---- 任何线程 ----
    def delete(self, name):
        self.q.put(("delete", name))

    def clear(self):
        self.q.put(("clear",))

    def close(self):
        self.q.put(None)
        self.thread.join(5)

    def snapshot(self, limit=200):
        with self._lock:
            return dict(self._totals(), list=[dict(i) for i in self.items[:limit]])

    def stats(self):
        with self._lock:
            return self._totals()

    def _totals(self):
        return {"count": len(self.items), "bytes": sum(i["bytes"] for i in self.items),
                "quota_bytes": self.quota, "failed": self.failed}

    def item(self, name):
        with self._lock:
            return next((dict(i) for i in self.items if i["name"] == name), None)

    def path_of(self, name):
        """片段名 → 磁盘路径；不是我们自己存的片段就返回 None。

        名字来自页面，所以只认索引里有的那些，并且先过一遍字符白名单：不给
        「../」和绝对路径任何机会。
        """
        if not isinstance(name, str) or not name.endswith(self.EXT) or not _STEM.match(name[:-len(self.EXT)]):
            return None
        return os.path.join(self.folder, name) if self.item(name) else None

    # ---- 写盘线程 ----
    def _run(self):
        while True:
            job = self.q.get()
            if job is None:
                self._shutdown()
                return
            try:
                if job[0] == "delete":
                    self._remove([i for i in self.items if i["name"] == job[1]])
                elif job[0] == "clear":
                    self._remove(list(self.items))
                elif not self._job(job):
                    continue                             # 只是一块数据：不必通知页面
            except OSError:                              # 盘满、目录被删：记一笔，别带走线程
                self.failed += 1
            self.on_change()

    def _job(self, job):
        raise NotImplementedError

    def _shutdown(self):
        pass

    def _unique(self, base):
        name, k = base + self.EXT, 1
        while os.path.exists(os.path.join(self.folder, name)):
            k += 1
            name = "%s-%d%s" % (base, k, self.EXT)
        return name

    def _commit(self, item):
        """数据文件已经完整落盘：写 JSON，登记，守配额。扫目录时以 JSON 为准。"""
        path = os.path.join(self.folder, item["name"][:-4])
        with open(path + ".json.tmp", "w", encoding="utf-8") as f:
            json.dump(item, f, ensure_ascii=False, indent=1)
        os.replace(path + ".json.tmp", path + ".json")
        with self._lock:
            self.items.insert(0, item)
        over, total = [], sum(i["bytes"] for i in self.items)
        for i in reversed(self.items[1:]):               # 从最旧的删起；刚录的这段不删
            if total <= self.quota:
                break
            over.append(i)
            total -= i["bytes"]
        self._remove(over)

    def _remove(self, items):
        for i in items:
            for ext in (self.EXT, ".json"):
                try:
                    os.remove(os.path.join(self.folder, i["name"][:-4] + ext))
                except OSError:
                    pass
        gone = {i["name"] for i in items}
        with self._lock:
            self.items = [i for i in self.items if i["name"] not in gone]

    def _scan(self):
        """启动时把目录里已有的片段认回来。只认 JSON 和 WAV 都在的。"""
        out = []
        try:
            names = os.listdir(self.folder)
        except OSError:
            return out
        for n in names:
            if not n.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.folder, n), encoding="utf-8") as f:
                    item = json.load(f)
                data = n[:-5] + self.EXT
                if isinstance(item, dict) and item.get("name") == data and _STEM.match(n[:-5]) \
                        and isinstance(item.get("start"), (int, float)):
                    item["bytes"] = os.path.getsize(os.path.join(self.folder, data))
                    out.append(item)
            except (OSError, ValueError):
                continue
        out.sort(key=lambda i: i["start"], reverse=True)
        return out


class Store(_Folder):
    """话音片段：一段一个 16 kHz 单声道 WAV。"""
    EXT = ".wav"

    def add(self, clip, meta):
        self.q.put(("add", clip, meta))

    def _job(self, job):
        _, clip, meta = job
        os.makedirs(self.folder, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clip["start"]))
        name = self._unique("%s_%.3f" % (stamp, meta["hz"] / 1e6))
        path = os.path.join(self.folder, name)
        with wave.open(path + ".tmp", "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(AUDIO_RATE)
            w.writeframes(clip["pcm"].astype("<i2").tobytes())
        os.replace(path + ".tmp", path)
        item = dict(meta, name=name, bytes=os.path.getsize(path),
                    **{k: clip[k] for k in ("start", "pre_s", "duration_s", "speech_s", "peak_db", "afc_hz", "end")})
        item["start_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clip["start"]))
        self._commit(item)
        return True


class IqStore(_Folder):
    """原始 I/Q：一段一个 .cu8（rtl_sdr 直接存下来的那种格式），同一时刻只录一段。

    begin / feed / end 由引擎调。feed 在 USB 回调线程里，只做一件事：把这一块的
    引用放进队列。写盘在本类自己的线程里；盘跟不上时丢块并记数（JSON 里的
    dropped_blocks），绝不让队列无限长，更不堵 USB 回调。
    """
    EXT = ".cu8"

    def __init__(self, folder, quota_bytes, on_change=None):
        self.cur = None                                  # 正在录的那段：{"name", "meta", "bytes", "dropped", "start"}
        self.backlog = 0
        self._file = None
        super().__init__(folder, quota_bytes, on_change)

    def begin(self, meta, pre_blocks, start):
        """开一段。pre_blocks 是此前留着的采样块，原样放在最前面。→ 这一段的文件名。"""
        os.makedirs(self.folder, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(start))
        name = self._unique("%s_%.4fMHz" % (stamp, meta["center_hz"] / 1e6))
        open(os.path.join(self.folder, name), "wb").close()      # 先占住名字，免得下一段撞上
        self.q.put(("open", name))
        self.cur = {"name": name, "meta": dict(meta), "bytes": 0, "dropped": 0, "start": start}
        for block in pre_blocks:
            self.feed(block)
        return name

    def feed(self, block):
        cur = self.cur
        if cur is None:
            return
        if self.backlog >= IQ_BACKLOG:
            cur["dropped"] += 1
            return
        self.backlog += 1
        cur["bytes"] += len(block)
        self.q.put(block)

    def end(self, reason, keep=True, extra=None):
        cur, self.cur = self.cur, None
        if cur:
            self.q.put(("close", cur, reason, keep, extra or {}))

    def active(self):
        cur = self.cur
        return None if cur is None else {"name": cur["name"], "kind": cur["meta"].get("kind"), "bytes": cur["bytes"],
                                         "s": round(cur["bytes"] / IQ_BYTES_PER_S, 1), "dropped": cur["dropped"]}

    def _job(self, job):
        if isinstance(job, bytes):
            self.backlog -= 1
            if self._file:
                self._file.write(job)
            return False
        if job[0] == "open":
            self._file = open(os.path.join(self.folder, job[1]), "wb")
            return False
        _, cur, reason, keep, extra = job
        f, self._file = self._file, None
        if f:
            f.close()
        path = os.path.join(self.folder, cur["name"])
        size = os.path.getsize(path)
        if not keep or size < IQ_BYTES_PER_S // 10:      # 不要的，或者短得没法用的：连文件一起扔掉
            os.remove(path)
            return True
        self._commit(dict(cur["meta"], name=cur["name"], bytes=size, start=cur["start"],
                          start_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cur["start"])),
                          duration_s=round(size / IQ_BYTES_PER_S, 2), dropped_blocks=cur["dropped"],
                          end=reason, **extra))
        return True

    def _shutdown(self):
        if self._file:                                   # 进程要走了还有一段没收尾：文件留着，但没有 JSON，
            self._file.close()                           # 下次启动不会认它
            self._file = None

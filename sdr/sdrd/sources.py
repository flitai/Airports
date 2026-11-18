"""不需要硬件的采样源：文件回放和合成信号。

两者和 device.RtlSource 长得一样（open / info / configure / set_gain / start /
stop / close），引擎分不出区别。它们都按真实时间节拍出块——readsb 在管道那头
拿采样点数当时钟，喂得比实时快，它的时间就会跑到墙上时间前面去。
"""
import threading
import time

from . import synth
from .device import BUF_LEN, DeviceError


class _Paced:
    """按采样率定时出块的公共骨架。子类只管 _next_block()。"""
    kind = "paced"
    rate = synth.RATE

    def __init__(self):
        self.thread = None
        self._stop = threading.Event()
        self.on_lost = None
        self.center, self.gain, self.gains = 0, None, []

    def open(self):
        pass

    def configure(self, center, rate, gain):
        if int(rate) != self.rate:
            raise DeviceError("error", f"这个采样源只有 {self.rate} S/s")
        self.center = int(center)

    def set_gain(self, db):
        return None                  # 没有可调的增益；返回 None 让引擎别再试

    def start(self, on_block):
        self._stop.clear()

        def run():
            due = time.monotonic()
            step = (BUF_LEN // 2) / self.rate
            while not self._stop.is_set():
                block = self._next_block()
                if block is None:
                    break
                on_block(block)
                due += step
                delay = due - time.monotonic()
                if delay > 0:
                    self._stop.wait(delay)
                elif delay < -1:     # 落后太多（比如机器睡眠过）就重新对表，别狂追
                    due = time.monotonic()

        self.thread = threading.Thread(target=run, name="iq-" + self.kind, daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        if self.thread and threading.current_thread() is not self.thread:
            self.thread.join(3)
        self.thread = None

    def close(self):
        self.stop()


class FileReplaySource(_Paced):
    """循环回放一段 cu8 录音（rtl_sdr 直接存下来的那种）。"""
    kind = "file"

    def __init__(self, path):
        super().__init__()
        self.path, self.f = path, None

    def open(self):
        try:
            self.f = open(self.path, "rb")
        except OSError as e:
            raise DeviceError("absent", f"回放文件打不开：{e}")

    def info(self):
        return {"model": "文件回放", "tuner": "—", "v4": False, "serial": "",
                "range_hz": [0, 0], "gains": [], "simulated": True}

    def _next_block(self):
        block = self.f.read(BUF_LEN)
        if len(block) < BUF_LEN:     # 到头了就从头再来；丢掉不满一块的尾巴
            self.f.seek(0)
            block = self.f.read(BUF_LEN)
        return block or None

    def close(self):
        super().close()
        if self.f:
            self.f.close()
            self.f = None


class SynthAdsbSource(_Paced):
    """合成的 ADS-B 流量，绕 center 转圈。"""
    kind = "synth"

    def __init__(self, center=(0.0, 0.0), count=3):
        super().__init__()
        self.traffic = synth.Traffic(center, count)
        self.mod = synth.Modulator(self.traffic)

    def info(self):
        return {"model": "合成信号", "tuner": "—", "v4": False, "serial": "",
                "range_hz": [0, 0], "gains": [], "simulated": True}

    def _next_block(self):
        return self.mod.block(BUF_LEN // 2)


def make_source(spec, ppm=0):
    """命令行的 --source 取值 → 采样源。

    rtl                真实接收机（默认）
    file:<路径>         回放 cu8 录音
    synth:adsb[:纬,经]  合成流量
    """
    if spec == "rtl":
        from .device import RtlSource
        return RtlSource(ppm=ppm)
    if spec.startswith("file:"):
        return FileReplaySource(spec[5:])
    if spec.startswith("synth:adsb"):
        rest = spec[len("synth:adsb"):].lstrip(":")
        center = tuple(float(x) for x in rest.split(",")) if rest else (0.0, 0.0)
        return SynthAdsbSource(center)
    raise ValueError(f"不认识的采样源：{spec}")

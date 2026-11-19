"""不需要硬件的采样源：文件回放、合成信号，以及界面里选一段 I/Q 来回放。

两者和 device.RtlSource 长得一样（open / info / configure / set_gain / start /
stop / close），引擎分不出区别。它们都按真实时间节拍出块——readsb 在管道那头
拿采样点数当时钟，喂得比实时快，它的时间就会跑到墙上时间前面去。
"""
import os
import threading
import time

import numpy as np

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

    def tune(self, center):
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


class SynthSource(_Paced):
    """合成信号：调到 1090 MHz 附近给 ADS-B 流量，调到别处给话音台。

    两样合在一个采样源里，是因为要验的正是「同一只接收机在两种模式间来回切」。
    """
    kind = "synth"

    def __init__(self, center=(0.0, 0.0), count=3, stations=(), bursts=()):
        super().__init__()
        self.traffic = synth.Traffic(center, count)
        self.mod = synth.Modulator(self.traffic)
        self.air = synth.AirBand(stations, bursts=bursts)

    def info(self):
        return {"model": "合成信号", "tuner": "—", "v4": False, "serial": "",
                "range_hz": [0, 0], "gains": [], "simulated": True}

    def _next_block(self):
        if abs(self.center - 1_090_000_000) < self.rate // 2:
            return self.mod.block(BUF_LEN // 2)
        return self.air.block(BUF_LEN // 2, self.center)


def shift_block(raw, shift_hz, rng):
    """把一块 cu8 采样在频率上平移 shift_hz（正数往高处移），移出窗口的丢掉。

    做法是整块做一次 FFT、把频点挪若干格、再变回来。一块 32768 点，一格 73 Hz，
    所以平移量取整到 73 Hz——差的这点由解调的频率跟踪补上。挪的是整数格，块与块
    之间相位自然接得上。不能直接乘一个复指数了事：那样移出去的那一截会从窗口另
    一头绕回来，凭空多出一批假信号。
    空出来的地方垫一点噪声，看上去是一段安静的频谱，而不是一片死黑。
    """
    n = len(raw) // 2
    k = int(round(shift_hz / (synth.RATE / n)))
    out = np.zeros(n, np.complex128)
    if abs(k) < n:
        a = np.frombuffer(raw, np.uint8).astype(np.float32)
        a -= 127.5
        spec = np.fft.fftshift(np.fft.fft(a.view(np.complex64)))
        moved = np.zeros(n, np.complex128)
        if k >= 0:
            moved[k:] = spec[:n - k]
        else:
            moved[:n + k] = spec[-k:]
        out = np.fft.ifft(np.fft.ifftshift(moved))
    iq = np.empty(2 * n, np.float32)
    iq[0::2], iq[1::2] = out.real, out.imag
    iq += rng.normal(127.5, 0.6, 2 * n).astype(np.float32)
    return np.clip(np.rint(iq), 0, 255).astype(np.uint8).tobytes()


class ReplaySource(_Paced):
    """回放一段录下来的 I/Q，像一台只看得见那 2.4 MHz 的接收机。

    录的时候接收机调在 rec_center。回放时调到同一个中心，采样原样送出；调到别处，
    就按差值把频谱平移过去——还落在录制范围里的信号出现在它该在的位置，范围以外
    是空的。于是引擎里划窗口、换频道、画瀑布图的代码一行都不用为回放改。
    放到头就从头再来。
    """
    kind = "replay"

    def __init__(self, path, rec_center, label=""):
        super().__init__()
        self.path, self.rec_center, self.label = path, int(rec_center), label
        self.f = None
        self.rng = np.random.default_rng(3)

    def open(self):
        try:
            self.f = open(self.path, "rb")
            if os.path.getsize(self.path) < BUF_LEN:
                raise OSError("文件太短")
        except OSError as e:
            raise DeviceError("absent", f"这段 I/Q 打不开：{e}")

    def info(self):
        return {"model": "回放 · " + self.label, "tuner": "—", "v4": False, "serial": "",
                "range_hz": [0, 0], "gains": [], "simulated": True}

    def _next_block(self):
        raw = self.f.read(BUF_LEN)
        if len(raw) < BUF_LEN:           # 到头了就从头再来；丢掉不满一块的尾巴
            self.f.seek(0)
            raw = self.f.read(BUF_LEN)
        if not raw:
            return None
        shift = self.rec_center - self.center
        return raw if shift == 0 else shift_block(raw, shift, self.rng)

    def close(self):
        super().close()
        if self.f:
            self.f.close()
            self.f = None


def make_source(spec, ppm=0):
    """命令行的 --source 取值 → 采样源。

    rtl                真实接收机（默认）
    file:<路径>         回放 cu8 录音
    synth:adsb[:纬,经][;air:<台>,<台>…][;vdl2:<wav>[@周期秒]]
                       合成 ADS-B 流量；可再加几个合成话音台，写法见 synth.Station.parse；
                       再加 vdl2 就在 136.975 MHz 上周期地发一段真的 VDL2 波形
                       （synth.Vdl2Model；wav 写 model 表示编译脚本留下的那段模型信号）
    """
    if spec == "rtl":
        from .device import RtlSource
        return RtlSource(ppm=ppm)
    if spec.startswith("file:"):
        return FileReplaySource(spec[5:])
    if spec.startswith("synth:adsb"):
        spec, _, link = spec.partition(";vdl2:")
        rest, _, air = spec[len("synth:adsb"):].partition(";air:")
        rest = rest.lstrip(":")
        center = tuple(float(x) for x in rest.split(",")) if rest else (0.0, 0.0)
        stations = [synth.Station.parse(x) for x in air.split(",") if x.strip()]
        bursts = []
        if link:
            from . import vdl2
            wav, _, period = link.partition("@")
            bursts = [synth.Vdl2Model(vdl2.MODEL_WAV if wav == "model" else wav, period_s=float(period or 2))]
        return SynthSource(center, stations=stations, bursts=bursts)
    raise ValueError(f"不认识的采样源：{spec}")

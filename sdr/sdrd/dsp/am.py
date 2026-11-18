"""航空话音解调：2.4 MS/s 的整窗采样 → 16 kHz 的 16 位单声道音频。

由 SDR Lab 的 processors.py（AnalogDemod 的 AM 分支）和 audio.py（限幅）改写。
保留的：先窄带滤波、再检波、再语音带通的顺序——信道滤波必须在检波之前，否则
强邻道会经过检波器压进来；同步检波（原版的可选项）；「正常音量线性通过、只在
峰值处软限幅」。
改动：
  * 搬移和第一级抽取合成一步（见 xlate.py），不再做全速率混频。
  * 加了自动频率跟踪。原版靠人工记下每个台的实测频率。实测：一个用双载波
    偏置的通播台，收到的那一支在标称频率 +4974 Hz；一个三载波的扇区，收到的在 +7343 Hz。
  * 跟踪到载波之后，信道滤波只留 ±3 kHz——话音就这么宽。不必像原版那样按频道
    间隔留 ±5 kHz 甚至更宽：多出来的只有噪声，以及偏置载波系统里别的那几支载波。
  * 同步检波作为默认。室内天线下载噪比常常只有 5–10 dB，包络检波在这种信噪比下
    会把噪声也整流成声音；同步检波只取与载波同相的那一半，噪声少一半。
  * 同步检波顺带给出「有没有话音」：调幅的话音只出现在与载波同相的那一路，正交
    那一路只有噪声。两路在话音频带里的功率一比，就知道载波上有没有调制，不必
    像原版那样为了一个话音检测模型装一整套语音识别库。见 VoiceDetector。
  * 音量按载波电平归一：远台近台听起来一样响。原版用的是事后的慢速增益。
  * 滤波器全部换成线性相位 FIR，不依赖 scipy。

链路：
  2.4 MS/s ─ Xlate ÷50 ─ 48 kHz ─ 跟踪并搬到零频 ─ 信道滤波 ±3 kHz ─ 同步检波并除以
  载波幅度 ─ 低通 3 kHz ÷3 ─ 16 kHz ─ 高通 300 Hz ─ 软限幅 ─ int16
"""
import numpy as np

from . import AUDIO_RATE, RATE, Fir, lowpass, taps_for
from .xlate import Xlate

IF_RATE = 48_000
AFC_LIMIT = {"25": 9_000, "8.33": 2_500}     # 频率跟踪范围：载波可以偏离标称值多远
PASS_HZ, STOP_HZ = 3_000, 4_600              # 信道滤波：相对载波的单边通带和阻带起点
CHANNEL_BW_HZ = 2 * (PASS_HZ + STOP_HZ) / 2  # 等效噪声带宽，频谱那边算载噪比时用同一个数
_AFC_FFT = 2048
_CARRIER_MS = 5                              # 估计载波相位用的平均时长


class AmDemod:
    def __init__(self, offset_hz, spacing="25"):
        """offset_hz：频道的标称载波相对窗口中心的偏移。"""
        self.afc_limit = AFC_LIMIT[spacing]
        self.xlate = Xlate(offset_hz, RATE // IF_RATE)
        self.channel = Fir(lowpass(taps_for(STOP_HZ - PASS_HZ, IF_RATE), (PASS_HZ + STOP_HZ) / 2, IF_RATE))
        # 载波相位：两个 5 毫秒的滑动平均串起来，即三角窗。第一个零点在 200 Hz，
        # 300 Hz 以上的话音边带被压掉 27 dB 以上，剩下的就是载波本身。
        box = np.ones(IF_RATE * _CARRIER_MS // 1000, np.float32)
        tri = np.convolve(box, box)
        self.carrier = Fir((tri / tri.sum()).astype(np.float32))
        self.delay = Fir(np.eye(1, len(tri), len(tri) // 2, dtype=np.float32)[0])   # 让信号等一等载波估计
        self.audio_lp = Fir(lowpass(taps_for(1_000, IF_RATE), 3_400, IF_RATE), IF_RATE // AUDIO_RATE)
        lp = lowpass(taps_for(200, AUDIO_RATE, 40), 200, AUDIO_RATE, 40)
        hp = -lp
        hp[len(hp) // 2] += 1                            # 全通减低通 = 高通
        self.audio_hp = Fir(hp)
        # 正交那一路走一模一样的滤波器，只为量它的功率
        self.quad_lp = Fir(self.audio_lp.taps, IF_RATE // AUDIO_RATE)
        self.quad_hp = Fir(hp)
        self.ratio = 0.0                 # 这一块里 同相功率 ÷ 正交功率；没有载波时为 0
        self.afc_hz = 0.0                # 载波实际位置相对标称值的偏差
        self.locked = False
        self.phase = 0.0
        self.level = 0.0                 # 载波幅度（满幅为 1）
        self.was_on = False

    def process(self, x, carrier_present):
        """x：一块 2.4 MS/s 的 complex64。返回这一块对应的 int16 音频。

        carrier_present 为假时输出静音，但滤波器照常走，状态保持连续。这一条不是
        可有可无的：音量是按载波电平归一的，载波一消失，归一化会把纯噪声放大到
        满幅。静噪要再等 400 毫秒才关，这期间必须按块把声音掐掉。
        """
        y = self.xlate.process(x)
        if not len(y):
            return np.zeros(0, np.int16)
        if carrier_present:
            self._track(y)
        else:
            self.locked = False
        step = 2 * np.pi * self.afc_hz / IF_RATE
        y = y * np.exp(-1j * (self.phase + step * np.arange(len(y)))).astype(np.complex64)
        self.phase = float((self.phase + step * len(y)) % (2 * np.pi))
        y = self.channel.process(y)
        c = self.carrier.process(y)                      # 载波：幅度和相位都慢变
        y = self.delay.process(y)
        mag = np.abs(c)
        if carrier_present:
            mean = float(mag.mean())
            # 刚出现载波时直接取当前值，之后慢慢跟
            self.level = mean if not self.was_on else self.level + 0.2 * (mean - self.level)
            # 同相分量除以载波幅度：干净的调幅信号在这里是 1 + m(t)。深衰落的瞬间载波
            # 估计会很小，分母垫一个下限，别把那一瞬的噪声放大成爆音。
            floor = max(0.3 * self.level, 1e-9)
            z = y * np.conj(c) / (mag * np.maximum(mag, floor))
            audio, quad = z.real - 1.0, z.imag
        else:
            audio = quad = np.zeros(len(y), np.float32)
        if carrier_present and not self.was_on:          # 开头 2 毫秒渐入，免得咔哒一声
            n = min(len(audio), IF_RATE // 500)
            audio[:n] *= np.linspace(0, 1, n, dtype=np.float32)
        self.was_on = carrier_present
        audio = self.audio_hp.process(self.audio_lp.process(audio.astype(np.float32)))
        quad = self.quad_hp.process(self.quad_lp.process(quad.astype(np.float32)))
        self.ratio = float(np.mean(audio * audio) / max(float(np.mean(quad * quad)), 1e-12)) \
            if carrier_present and len(audio) else 0.0
        # 调制度 100% 的话音在这里是 ±1。0.75 以内原样通过，再往上平滑地压到 0.95 以内
        mag = np.abs(audio)
        audio = np.sign(audio) * np.where(mag <= 0.75, mag, 0.75 + 0.20 * np.tanh((mag - 0.75) / 0.20))
        return np.rint(audio * 32767).astype(np.int16)

    def _track(self, y):
        """在允许范围内找最强的那根谱线当载波。AM 的载波总比任何一个边带强。"""
        n = min(len(y), _AFC_FFT)
        p = np.abs(np.fft.fft(y[:n] * np.hanning(n), _AFC_FFT)) ** 2
        res = IF_RATE / _AFC_FFT
        reach = int(self.afc_limit / res)
        idx = np.arange(-reach, reach + 1)
        band = p[idx]                                    # 负下标正好取到 FFT 的负频率那一半
        i = int(band.argmax())
        est = float(idx[i]) * res
        if 0 < i < len(band) - 1:                        # 抛物线插值，把分辨率从 23 Hz 再细一档
            a, b, c = np.log(np.maximum(band[i - 1:i + 2], 1e-30))
            if a - 2 * b + c < 0:
                est += 0.5 * (a - c) / (a - 2 * b + c) * res
        self.afc_hz = est if not self.locked else self.afc_hz + 0.25 * (est - self.afc_hz)
        self.locked = True

    def carrier_dbfs(self):
        return float(20 * np.log10(max(self.level, 1e-9)))


class VoiceDetector:
    """载波上有没有话音。喂给它的是 AmDemod.ratio，每块一次。

    没有调制时同相和正交两路都只有噪声，比值在 1 上下；有话音时同相那一路多出
    话音的功率。两路经过同样的滤波、除以同一个载波幅度，所以这个比值不需要任何
    标定，信号强弱、增益高低都不影响。载波本身在抖的干扰（开关电源、时钟的杂散）
    两路一起乱，比值同样在 1 附近。

    平滑后超过 on_ratio 即认为有话音。1.6 相当于话音功率是噪声的 0.6 倍，再弱的
    话音听也听不清。之后保持 hang_blocks 块（默认约 1 秒）：字和字之间本来就有
    停顿，不能一停就掐。
    它分辨的是「有没有调制」，单音和数据也算有。要挡的是无调制的干扰载波和纯噪声。
    """
    def __init__(self, on_ratio=1.6, hang_blocks=73):
        self.on_ratio, self.hang_blocks = on_ratio, hang_blocks
        self.smooth = 1.0
        self.voice = False
        self.quiet = 0

    def update(self, ratio):
        if ratio <= 0:                   # 这一块没有载波：平滑值归位，但「刚才有话音」照常保持
            self.smooth = 1.0            # 一会儿——载波瞬时衰落不该把一句话判成两句
        else:
            self.smooth += 0.3 * (min(ratio, 10.0) - self.smooth)
        if self.smooth >= self.on_ratio:
            self.voice, self.quiet = True, 0
        elif self.voice:
            self.quiet += 1
            if self.quiet >= self.hang_blocks:
                self.voice = False
        return self.voice

"""合成信号：ADS-B 从报文编码一路做到 2.4 MS/s 的原始 I/Q；航空话音是带调制的载波。

为什么要有它：SDR Lab 的测试夹具里没有真实录音（只留了指向缺失文件的说明），
而自己录的回放文件含真实航班，不适合入库。有了合成信号，「原始采样 → readsb
→ 守护进程 → 页面」整条链路不插接收机也能测，而且位置是我们自己定的，能断言
解出来的经纬度对不对。

编码按 DO-260 的 1090ES 格式：DF17，24 位地址，56 位 ME，24 位校验。
"""
import math

import numpy as np

RATE = 2_400_000
_OVERSAMPLE = 5                       # 先在 12 MS/s 上造波形，再平均抽取到 2.4 MS/s
_HALF_US = 6                          # 12 MS/s 下半微秒 = 6 个采样点
_CHARSET = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######"


def crc24(data):
    """Mode S 的 24 位校验，生成多项式 0x1FFF409。"""
    crc = 0
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1FFF409
    return crc & 0xFFFFFF


def _frame(icao, me):
    body = bytes([0x8D]) + icao.to_bytes(3, "big") + me.to_bytes(7, "big")   # 0x8D = DF17, CA=5
    return body + crc24(body).to_bytes(3, "big")


def _nl(lat):
    """纬度带内的经度分区数（CPR 的 NL 函数）。"""
    lat = abs(lat)
    if lat < 1e-9:
        return 59
    if lat >= 87:
        return 2 if lat == 87 else 1
    a = 1 - math.cos(math.pi / 30)
    return math.floor(2 * math.pi / math.acos(1 - a / math.cos(math.radians(lat)) ** 2))


def cpr_encode(lat, lon, odd):
    """空中位置的 17 位 CPR 编码 → (纬度码, 经度码)。"""
    dlat = 360 / (60 - odd)
    yz = math.floor(131072 * ((lat % dlat) / dlat) + 0.5)
    rlat = dlat * (yz / 131072 + math.floor(lat / dlat))
    dlon = 360 / max(_nl(rlat) - odd, 1)
    xz = math.floor(131072 * ((lon % dlon) / dlon) + 0.5)
    return yz & 0x1FFFF, xz & 0x1FFFF


def position_frame(icao, lat, lon, alt_ft, odd):
    n = int((alt_ft + 1000) // 25)
    alt = ((n >> 4) << 5) | 0x10 | (n & 0xF)                 # Q 位置 1：25 英尺分辨率
    yz, xz = cpr_encode(lat, lon, odd)
    me = (11 << 51) | (alt << 36) | (int(odd) << 34) | (yz << 17) | xz    # TC=11 空中位置
    return _frame(icao, me)


def ident_frame(icao, callsign):
    me = 4 << 51                                             # TC=4 身份，类别留空
    for i, ch in enumerate(callsign.upper().ljust(8)[:8]):
        code = _CHARSET.find(ch)
        me |= (code if code > 0 else 32) << (42 - 6 * i)     # 字符集里没有的一律当空格
    return _frame(icao, me)


def velocity_frame(icao, gs_kt, track_deg):
    """TC=19 子类 1：对地速度，按东西 / 南北两个分量给出。"""
    ew = gs_kt * math.sin(math.radians(track_deg))
    ns = gs_kt * math.cos(math.radians(track_deg))
    me = (19 << 51) | (1 << 48)
    me |= (int(ew < 0) << 42) | ((min(1022, round(abs(ew))) + 1) << 32)
    me |= (int(ns < 0) << 31) | ((min(1022, round(abs(ns))) + 1) << 21)
    me |= 1 << 10                                            # 垂直速率 = 0
    return _frame(icao, me)


def envelope(frame, offset=0):
    """一帧 112 位报文 → 2.4 MS/s 的幅度包络（0..1）。

    1090ES 是脉位调制：前导 8 微秒里有 4 个半微秒脉冲（起点 0、1、3.5、4.5 微秒），
    随后每位占 1 微秒，前半高是 1、后半高是 0。2.4 MS/s 下半微秒只有 1.2 个采样点，
    不是整数，所以先在 12 MS/s 上画出方波再平均抽取，readsb 的 2.4 MS/s 解调器
    面对的就是和真实采样一样的分数相位波形。offset（0..4）用来变换采样相位。
    """
    slots = np.zeros(240, np.float32)                        # 120 微秒 = 240 个半微秒
    slots[[0, 2, 7, 9]] = 1
    bits = np.unpackbits(np.frombuffer(frame, np.uint8))
    slots[16 + 2 * np.arange(112) + (1 - bits)] = 1
    wave = np.repeat(slots, _HALF_US)
    wave = np.concatenate([np.zeros(offset, np.float32), wave])
    wave = np.pad(wave, (0, -len(wave) % _OVERSAMPLE))
    return wave.reshape(-1, _OVERSAMPLE).mean(axis=1)


class Traffic:
    """几架绕着一个中心点转圈的假飞机，状态是时间的确定函数，方便断言。"""

    def __init__(self, center=(0.0, 0.0), count=3):
        self.center = center
        self.craft = [{"icao": 0xABC001 + i, "cs": "SYN%03d" % (i + 1),
                       "radius_nm": 12 + 9 * i, "gs": 220 + 60 * i,
                       "alt": 6000 + 8000 * i, "phase": 2.1 * i} for i in range(count)]

    def state(self, craft, t):
        """t 秒时这架飞机的 (lat, lon, alt, gs, track)。"""
        theta = craft["phase"] + (craft["gs"] / 3600 / craft["radius_nm"]) * t
        lat = self.center[0] + craft["radius_nm"] / 60 * math.cos(theta)
        lon = self.center[1] + craft["radius_nm"] / 60 * math.sin(theta) / math.cos(math.radians(self.center[0]))
        track = math.degrees(math.atan2(math.cos(theta), -math.sin(theta))) % 360
        return lat, lon, craft["alt"], craft["gs"], track

    def frames_between(self, t0, t1):
        """[t0, t1) 秒内该发的报文 → [(时刻, 帧字节)]。

        每架每 0.5 秒一条位置（奇偶交替，这是全局 CPR 解算必需的一对）、一条速度，
        每 2 秒一条呼号，各架之间错开，免得撞在同一时刻互相盖住。
        """
        out = []
        for i, c in enumerate(self.craft):
            base = 0.013 * i
            k = math.ceil((t0 - base) / 0.5)
            while base + 0.5 * k < t1:
                t = base + 0.5 * k
                lat, lon, alt, gs, track = self.state(c, t)
                out.append((t, position_frame(c["icao"], lat, lon, alt, k % 2)))
                out.append((t + 0.004, velocity_frame(c["icao"], gs, track)))
                if k % 4 == 0:
                    out.append((t + 0.008, ident_frame(c["icao"], c["cs"])))
                k += 1
        return out


class Modulator:
    """把 Traffic 的报文流铺成连续的 cu8 采样块。帧可以跨块，尾巴带到下一块。"""
    AMP = 45.0                       # 脉冲幅度（量化单位）。噪声约 1.5，信噪比足够 readsb 稳定解出
    TAIL = 320                       # 一帧约 289 个采样点，留够跨块的余量

    def __init__(self, traffic, seed=1):
        self.traffic = traffic
        self.rng = np.random.default_rng(seed)
        self.pos = 0                 # 已产出的采样点数
        self.carry = np.zeros(self.TAIL, np.complex64)

    def block(self, nsamples):
        buf = np.zeros(nsamples + self.TAIL, np.complex64)
        buf[:self.TAIL] = self.carry
        t0, t1 = self.pos / RATE, (self.pos + nsamples) / RATE
        for t, frame in self.traffic.frames_between(t0, t1):
            start = int(t * RATE) - self.pos
            if not 0 <= start < nsamples:
                continue
            env = envelope(frame, int(self.rng.integers(_OVERSAMPLE)))
            phase = np.exp(1j * self.rng.uniform(0, 2 * np.pi))
            buf[start:start + len(env)] += (self.AMP * env * phase).astype(np.complex64)
        self.carry = buf[nsamples:].copy()
        self.pos += nsamples
        iq = np.empty(2 * nsamples, np.float32)
        iq[0::2], iq[1::2] = buf[:nsamples].real, buf[:nsamples].imag
        iq += self.rng.normal(127.5, 1.5, 2 * nsamples).astype(np.float32)
        return np.clip(np.rint(iq), 0, 255).astype(np.uint8).tobytes()


class Station:
    """一个合成的话音台：载波 + 单音调幅，可以按周期通断。

    offset_hz 是载波偏离标称频率的量，用来验证自动频率跟踪；on_s / off_s 模拟
    一问一答的通话节奏（off_s 为 0 即连续播发，像通播台）。
    """
    def __init__(self, hz, tone_hz=1000.0, offset_hz=0.0, on_s=1.0, off_s=0.0, amp=12.0, depth=0.6):
        self.hz, self.tone_hz, self.offset_hz = float(hz), float(tone_hz), float(offset_hz)
        self.on_s, self.off_s, self.amp, self.depth = float(on_s), float(off_s), float(amp), float(depth)

    @classmethod
    def parse(cls, text):
        """MHz[@偏差Hz][~开秒[/关秒]][*单音Hz][%调制度]。

        例如 118.5@-7000~4/2*1000；125.0%0 是不带调制的空载波，用来扮演干扰。
        """
        import re
        m = re.fullmatch(r"([\d.]+)(?:@(-?[\d.]+))?(?:~([\d.]+)(?:/([\d.]+))?)?(?:\*([\d.]+))?(?:%([\d.]+))?", text.strip())
        if not m:
            raise ValueError("看不懂的合成台：" + text)
        mhz, off, on, gap, tone, depth = m.groups()
        return cls(float(mhz) * 1e6, float(tone or 1000), float(off or 0), float(on or 1), float(gap or 0),
                   depth=float(depth) / 100 if depth else 0.6)


class AirBand:
    """把若干个 Station 铺成 cu8 采样块。只画落在当前窗口里的台。"""

    def __init__(self, stations, seed=2, noise=1.5):
        self.stations = list(stations)
        self.rng = np.random.default_rng(seed)
        self.noise = noise
        self.pos = 0

    def block(self, nsamples, center_hz):
        n = np.arange(self.pos, self.pos + nsamples, dtype=np.float64)
        t = n / RATE
        buf = np.zeros(nsamples, np.complex128)
        for s in self.stations:
            off = s.hz + s.offset_hz - center_hz
            if abs(off) > RATE / 2 - 20_000:
                continue
            period = s.on_s + s.off_s
            keyed = (t % period) < s.on_s if s.off_s > 0 else 1.0
            env = s.amp * (1 + s.depth * np.cos(2 * np.pi * s.tone_hz * t)) * keyed
            buf += env * np.exp(2j * np.pi * (off / RATE) * n)
        self.pos += nsamples
        iq = np.empty(2 * nsamples, np.float32)
        iq[0::2], iq[1::2] = buf.real, buf.imag
        iq += self.rng.normal(127.5, self.noise, 2 * nsamples).astype(np.float32)
        return np.clip(np.rint(iq), 0, 255).astype(np.uint8).tobytes()

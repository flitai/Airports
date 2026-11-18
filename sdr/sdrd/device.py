"""librtlsdr 的 ctypes 绑定。

由 SDR Lab 的 sdrlab/rtl.py 改写而来，保留了它最对的一条原则：USB 回调里只
拷贝、不做任何别的事。改掉的地方：

  * 不再调用直采。原版用 V3 的 Q 路直采收短波；V4 靠内置上变频，直采在 V4 上
    收不到东西。短波属于后续里程碑，到时按型号分别处理，这里先不碰。
  * 不再写死「设备 0 + 仓库里自编译的 dylib」，改为枚举设备并查找系统里的驱动。
  * 重调谐不再停掉异步读取再重启（原版每次调谐都要等首个回调，最长 5 秒）。
  * 型号靠 USB 字符串识别，和驱动自己的判据一致，并把调谐器型号一起报上去——
    外壳上印的字不算数，驱动认不认才算数。
"""
import ctypes as C
import ctypes.util
import os
import threading

TUNERS = {0: "未知", 1: "E4000", 2: "FC0012", 3: "FC0013", 4: "FC2580", 5: "R820T", 6: "R828D"}
_CALLBACK = C.CFUNCTYPE(None, C.POINTER(C.c_ubyte), C.c_uint32, C.c_void_p)
# 每个 USB 缓冲 65536 字节 = 2.4 MS/s 下 13.65 ms。原版用 262144（54.6 ms），
# 对 ADS-B 无所谓，但后面做话音时这就是凭空多出来的延迟。
BUF_LEN = 65536
BUF_NUM = 16


class DeviceError(RuntimeError):
    """code 给界面用来挑提示语：absent / busy / denied / no_driver / error。"""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


_lib = None


def load_library():
    """按顺序找驱动：环境变量 → Homebrew（Apple 芯片 / Intel）→ 系统搜索路径。

    不能只靠 find_library：uv 管理的 Python 不看 /opt/homebrew/lib。
    """
    global _lib
    if _lib is not None:
        return _lib
    names = [os.environ.get("SDR_LIBRTLSDR"),
             "/opt/homebrew/lib/librtlsdr.dylib", "/usr/local/lib/librtlsdr.dylib",
             ctypes.util.find_library("rtlsdr"), "librtlsdr.so.0", "librtlsdr.so"]
    last = None
    for name in filter(None, names):
        try:
            lib = C.CDLL(name)
            break
        except OSError as e:
            last = e
    else:
        raise DeviceError("no_driver", f"找不到 librtlsdr（{last}）。macOS 上执行 brew install librtlsdr")
    sig = {
        "get_device_count": ([], C.c_uint32),
        "get_device_name": ([C.c_uint32], C.c_char_p),
        "get_device_usb_strings": ([C.c_uint32, C.c_char_p, C.c_char_p, C.c_char_p], C.c_int),
        "open": ([C.POINTER(C.c_void_p), C.c_uint32], C.c_int),
        "close": ([C.c_void_p], C.c_int),
        "get_tuner_type": ([C.c_void_p], C.c_int),
        "set_freq_correction": ([C.c_void_p, C.c_int], C.c_int),
        "set_center_freq": ([C.c_void_p, C.c_uint32], C.c_int),
        "get_center_freq": ([C.c_void_p], C.c_uint32),
        "set_sample_rate": ([C.c_void_p, C.c_uint32], C.c_int),
        "get_sample_rate": ([C.c_void_p], C.c_uint32),
        "set_tuner_gain_mode": ([C.c_void_p, C.c_int], C.c_int),
        "set_tuner_gain": ([C.c_void_p, C.c_int], C.c_int),
        "get_tuner_gain": ([C.c_void_p], C.c_int),
        "get_tuner_gains": ([C.c_void_p, C.POINTER(C.c_int)], C.c_int),
        "set_agc_mode": ([C.c_void_p, C.c_int], C.c_int),
        "reset_buffer": ([C.c_void_p], C.c_int),
        "read_async": ([C.c_void_p, _CALLBACK, C.c_void_p, C.c_uint32, C.c_uint32], C.c_int),
        "cancel_async": ([C.c_void_p], C.c_int),
    }
    for name, (args, res) in sig.items():
        f = getattr(lib, "rtlsdr_" + name)
        f.argtypes, f.restype = args, res
    if hasattr(lib, "rtlsdr_set_bias_tee"):          # 老版本驱动没有这个函数
        lib.rtlsdr_set_bias_tee.argtypes, lib.rtlsdr_set_bias_tee.restype = [C.c_void_p, C.c_int], C.c_int
    _lib = lib
    return lib


def enumerate_devices():
    lib = load_library()
    out = []
    for i in range(lib.rtlsdr_get_device_count()):
        v, p, s = (C.create_string_buffer(256) for _ in range(3))
        lib.rtlsdr_get_device_usb_strings(i, v, p, s)
        out.append({"index": i,
                    "name": (lib.rtlsdr_get_device_name(i) or b"").decode("utf-8", "replace"),
                    "vendor": v.value.decode("utf-8", "replace"),
                    "product": p.value.decode("utf-8", "replace"),
                    "serial": s.value.decode("utf-8", "replace")})
    return out


def describe(vendor, product, tuner):
    """→ (型号名, 调谐范围 Hz, 是否 V4)。

    判据与驱动一字不差：厂商串 "RTLSDRBlog" 且产品串 "Blog V4"（或 "Blog V4L"）。
    只有这样驱动才会启用上变频和输入切换，才真的能调到 24 MHz 以下。
    """
    if vendor == "RTLSDRBlog" and product in ("Blog V4", "Blog V4L"):
        return "RTL-SDR " + product, (500_000, 1_766_000_000), True
    return f"RTL2832U · {tuner}", (24_000_000, 1_766_000_000), False


class RtlSource:
    kind = "rtl"

    def __init__(self, index=0, ppm=0):
        self.index, self.ppm = index, int(ppm)
        self.lib = load_library()
        self.dev = C.c_void_p()
        self.thread = None
        self._cb = None
        self._stopping = False
        self._lock = threading.Lock()            # 调谐和增益可能来自别的线程
        self.on_lost = None
        self.gains, self.gain = [], None
        self.center = self.rate = 0
        self._info = {}

    # ---- 打开与关闭 ----
    def open(self):
        devs = enumerate_devices()
        if not devs:
            raise DeviceError("absent", "未检测到接收机")
        d = devs[min(self.index, len(devs) - 1)]
        r = self.lib.rtlsdr_open(C.byref(self.dev), d["index"])
        if r < 0:
            self.dev = C.c_void_p()
            # 返回值是 libusb 的错误码：-6 被占用，-3 无权限
            if r == -6:
                raise DeviceError("busy", "接收机被别的程序占用")
            if r == -3:
                raise DeviceError("denied", "没有访问接收机的权限")
            raise DeviceError("error", f"打开接收机失败（{r}）")
        try:
            tuner = TUNERS.get(self.lib.rtlsdr_get_tuner_type(self.dev), "未知")
            model, rng, v4 = describe(d["vendor"], d["product"], tuner)
            n = self.lib.rtlsdr_get_tuner_gains(self.dev, None)
            if n > 0:
                arr = (C.c_int * n)()
                self.lib.rtlsdr_get_tuner_gains(self.dev, arr)
                self.gains = [g / 10 for g in arr]
            self._bias(False)                    # 进程崩溃后引脚状态会残留，打开时就显式关掉
            if self.ppm:
                self.lib.rtlsdr_set_freq_correction(self.dev, self.ppm)
            self._info = {"model": model, "tuner": tuner, "v4": v4, "range_hz": list(rng),
                          "vendor": d["vendor"], "product": d["product"], "serial": d["serial"],
                          "gains": self.gains}
        except BaseException:
            self.close()
            raise

    def info(self):
        return dict(self._info)

    def _bias(self, on):
        if self.dev and hasattr(self.lib, "rtlsdr_set_bias_tee"):
            self.lib.rtlsdr_set_bias_tee(self.dev, int(bool(on)))

    def _check(self, r, what):
        if r < 0:
            raise DeviceError("error", f"{what}失败（{r}）")

    def configure(self, center, rate, gain):
        with self._lock:
            self._check(self.lib.rtlsdr_set_sample_rate(self.dev, int(rate)), "设置采样率")
            self._check(self.lib.rtlsdr_set_agc_mode(self.dev, 0), "关闭数字 AGC")
            self._check(self.lib.rtlsdr_set_tuner_gain_mode(self.dev, 1), "切换手动增益")
            self._check(self.lib.rtlsdr_set_center_freq(self.dev, int(center)), "调谐")
            self._check(self.lib.rtlsdr_reset_buffer(self.dev), "复位缓冲")
            self.rate = int(self.lib.rtlsdr_get_sample_rate(self.dev))
            self.center = int(self.lib.rtlsdr_get_center_freq(self.dev))
        self.set_gain(gain)

    def tune(self, center):
        """换中心频率，异步读取不停。

        原版每次调谐都 cancel_async 再重开，要等首个回调，最长 5 秒。其实驱动的
        调谐只是几次 USB 控制传输，和批量读取走的是不同的端点，可以并行；
        rtl_tcp 就是这么做的。换完之后头几十毫秒的采样是锁相环还没稳住时的，
        由调用方丢弃。
        """
        with self._lock:
            self._check(self.lib.rtlsdr_set_center_freq(self.dev, int(center)), "调谐")
            self.center = int(self.lib.rtlsdr_get_center_freq(self.dev))

    def set_gain(self, db):
        """设到增益表里最近的一档，返回实际生效的值。"""
        if not self.gains:
            return None
        nearest = min(self.gains, key=lambda g: abs(g - db))
        with self._lock:
            self.lib.rtlsdr_set_tuner_gain(self.dev, int(round(nearest * 10)))
            self.gain = self.lib.rtlsdr_get_tuner_gain(self.dev) / 10
        return self.gain

    def start(self, on_block):
        self._stopping = False

        def receive(buf, length, _ctx):
            # 只拷贝、只转交。这是 ctypes 回调，每次进来都要拿 GIL，
            # 在这里多做一点事，USB 缓冲就多一分溢出的机会。
            on_block(C.string_at(buf, length))

        self._cb = _CALLBACK(receive)            # 必须留住引用，否则回调对象被回收后驱动会调进野地址

        def run():
            self.lib.rtlsdr_read_async(self.dev, self._cb, None, BUF_NUM, BUF_LEN)
            if not self._stopping and self.on_lost:
                self.on_lost()                   # 不是我们叫停的：设备被拔了或 USB 出错

        self.thread = threading.Thread(target=run, name="rtl-usb", daemon=True)
        self.thread.start()

    def stop(self):
        self._stopping = True
        if self.thread and self.thread.is_alive():
            self.lib.rtlsdr_cancel_async(self.dev)
            if threading.current_thread() is not self.thread:
                self.thread.join(5)
        self.thread = None

    def close(self):
        if self.dev:
            self.stop()
            self._bias(False)
            self.lib.rtlsdr_close(self.dev)
            self.dev = C.c_void_p()

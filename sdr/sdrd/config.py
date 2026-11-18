"""守护进程的持久设置：.runtime/sdr.json。

设置放在守护进程这边而不是浏览器的 localStorage：浏览器和 Electron 是两个
独立的存储，各存各的话，在一边设好的接收机位置到另一边就没了。

这个文件里有接收机的经纬度，也就是使用者的住址。.runtime/ 已在 .gitignore 里，
serve.py 也拒绝对外提供任何点开头的路径。
"""
import json
import os
import threading

DEFAULTS = {
    "mode": "adsb",          # 上次的模式，重启后接着干。用 --sdr 启动就是想收，所以默认开
    "home": None,            # [lat, lon]
    "gain": "auto",          # "auto" 或 dB 数值
    "ppm": 0,
    "outline": None,         # RangeOutline.to_json()
    "squelch_db": 7.0,       # 载噪比（载波比信道带宽内的噪声）高于多少 dB 才开静噪
    "voice_only": True,      # 无话音时静音：载波上没有调制（干扰、纯噪声）就不出声
    "volume": 0.8,           # 0–1。存在这边，浏览器和桌面外壳共用一个音量
}


class Config:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self.data = dict(DEFAULTS)
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data.update({k: loaded[k] for k in DEFAULTS if k in loaded})
        except (OSError, ValueError):
            pass                         # 没有或读坏了都当首次运行，不拦启动

    def get(self, key):
        return self.data.get(key)

    def set(self, **kw):
        with self._lock:
            self.data.update(kw)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self.path)   # 原子替换：写到一半断电也不会留下半截文件

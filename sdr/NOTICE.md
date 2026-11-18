# sdr/ 的来源声明

`sdr/` 是 Airports 的本地接收机部分，以 GPL-3.0 授权（见仓库根目录的 LICENSE）。

## 移植自 SDR Lab 的部分

SDR Lab 是一个基于 RTL-SDR Blog V3 的桌面信号探索工作台（Python / PySide6）。
经 superbug 同意，下列文件由它的对应模块移植、改写而来：

| 本仓库 | 来自 SDR Lab | 保留了什么，改了什么 |
|---|---|---|
| `sdrd/device.py` | `sdrlab/rtl.py` | 保留：ctypes 调 librtlsdr、USB 回调里只拷贝不处理。改动：去掉 V3 的 Q 路直采；按 USB 字符串识别型号；查找系统驱动而不是仓库内自编译的库；重调谐不再重启异步读取 |
| `sdrd/geo.py` 的 `distance_bearing` | `sdrlab/positions.py` | 公式不变，单位由千米改为海里 |
| `sdrd/config.py` | `sdrlab/preferences.py` | 只借了「原子替换写 JSON」的做法 |

`sdrd/engine.py` 对应 SDR Lab 的 `session.py`，行为相近但结构是重写的，没有沿用代码。
SDR Lab 的纯 Python ADS-B 解码（`modes.py` / `aircraft.py`）没有移植，改由 readsb 承担。

SDR Lab 作者：superbug。

## 其他来源

`sdrd/synth.py` 的报文编码依据 RTCA DO-260 的 1090ES 格式自行实现；
`tests/test_synth.py` 中的两条示例报文出自 Junzi Sun《The 1090 Megahertz Riddle》。

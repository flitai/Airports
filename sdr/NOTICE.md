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

| `sdrd/dsp/channels.py` | `sdrlab/exploration.py`（`validate_channels`、`channel_centers`） | 保留：按频率聚成组、每组一个中心频率、让频道避开直流毛刺。改动：直接吃机场频率表而不是手工维护的频道表；懂 8.33 kHz 频道名；去掉 64 行上限；分组保证窗口数最少，并按频道类型加权 |
| `sdrd/dsp/spectrum.py` | `sdrlab/spectral.py`、`sdrlab/trigger.py` 的 `channel_excess` | 保留：汉宁窗分段平均的功率谱、「频道功率比邻近底噪」并以中位数估底噪。改动：分子取载波谱线、分母折算到信道带宽，得到载噪比；顺带给出载波的频率偏差；瀑布图的行直接由解调用的同一份谱累加而成，量化成 8 位发给页面 |
| `sdrd/dsp/candidates.py` | `sdrlab/search.py` 的 `CandidateDetector` | 保留：对平均功率谱做能量检测、连成段、剔除中心的直流毛刺，再按频率把逐次检测并成候选条目；「能量检测只说明有东西，不说明是什么」的态度。改动：强度和门限改用载噪比，与静噪同一个标准；频率取最强谱线而不是功率重心；不按频段猜调制方式；候选跨窗口保留；不依赖 scipy。扫描与驻留策略（`SearchPolicy`）没有移植 |
| `sdrd/dsp/am.py` | `sdrlab/processors.py` 的 `AnalogDemod`（AM 分支）、`sdrlab/audio.py` 的限幅 | 保留：先信道滤波再检波再语音带通的顺序、同步检波、0.75 以上才软限幅。改动：加自动频率跟踪；信道滤波收窄到 ±3 kHz；按载波幅度归一；用同相 / 正交两路的功率比判断有没有话音（取代原版依赖语音识别库的做法）；滤波器全部改为 numpy 实现的线性相位 FIR，不依赖 scipy |

`sdrd/dsp/xlate.py`（移频、滤波、抽取合成一步）是新写的，取代 `processors.py` 里的全速率混频。
`sdrd/air.py` 的静噪状态机借鉴了 `sdrlab/trigger.py` 的开关门限加保持时间的思路，代码是新写的。

`sdrd/record.py`（话音触发录音）借鉴了 `sdrlab/trigger.py` 的三条规矩：要有话音才开录、开录时带上此前一小段、
到了单段上限要等这次发射结束才能再触发。代码是新写的：录的是解调后的音频而不是原始 I/Q，话音判定用解调链
自带的调制检测而不是语音识别库。同一文件里的原始 I/Q 录制沿用了 SDR Lab「录的时候把此前一小段原始采样也带上」
的做法（`trigger.py:149-154` 的缓存）；回放时按频点平移频谱（`sdrd/sources.py` 的 `ReplaySource`）是新写的。

`sdrd/engine.py` 对应 SDR Lab 的 `session.py`，行为相近但结构是重写的，没有沿用代码。
SDR Lab 的纯 Python ADS-B 解码（`modes.py` / `aircraft.py`）没有移植，改由 readsb 承担。

SDR Lab 作者：superbug。

## 其他来源

`sdrd/synth.py` 的报文编码依据 RTCA DO-260 的 1090ES 格式自行实现；
`tests/test_synth.py` 中的两条示例报文出自 Junzi Sun《The 1090 Megahertz Riddle》。

VDL2 数据链的解码由 [dumpvdl2](https://github.com/szpajder/dumpvdl2)（Tomasz Lemiech，GPL-3.0-or-later）承担，
它依赖同一作者的 [libacars](https://github.com/szpajder/libacars)（MIT）。两者都是外部程序：不随本仓库分发，
由 `tools/build-dumpvdl2.sh` 在使用者的机器上按固定版本取源码编译，守护进程把它当子进程来用（`sdrd/vdl2.py`）。
合成信号源里的 VDL2 波形（`sdrd/synth.py` 的 `Vdl2Model`）用的是 dumpvdl2 源码自带的模型信号
`test/vdl2_model_16b_1050kHz.wav`，同样只在编译时留一份在本机，不入库；`tests/fake_dumpvdl2.py` 里
X.25 那条样例报文是 dumpvdl2 解这段模型信号的输出。

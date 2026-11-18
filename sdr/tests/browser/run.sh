#!/bin/zsh
# 浏览器端验收：用无头 Chrome 驱动真实页面，经 window.__probe / __act 断言。
# 用法：sdr/tests/browser/run.sh [base|receiver|voice|spectrum|all]      （默认 all）
#
#   base      不启用接收机：联网航班、多来源合并、切底图；以及 file:// 模式
#   receiver  用合成 ADS-B 信号走完整链路：serve.py → sdrd → readsb → 页面
#   voice     用合成的话音台验话音监听：频率表里点收听 → 调谐 → 解调 → 页面出声
#   spectrum  频谱与瀑布图：两种模式下的行速率、点击调谐、信号列表
#
# receiver、voice 和 spectrum 会改接收机位置、静噪这些设置，所以各用一个临时的运行目录，
# 不碰 .runtime/sdr.json 里你自己的设置。
set -u
ROOT=${0:A:h:h:h:h}; HERE=${0:A:h}; WHAT=${1:-all}
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
TMP=$(mktemp -d); DBG=9333; RC=0
trap 'kill $CP $SP 2>/dev/null; wait 2>/dev/null; rm -rf "$TMP"' EXIT

chrome() {
  # 不加 swiftshader 这两个参数，无头模式下没有 WebGL，地图永远起不来。
  # autoplay-policy：脚本驱动的点击不算用户操作，不放开的话 AudioContext 起不来
  # mute-audio：无头浏览器照样会把声音送到扬声器。不静音的话，跑 voice 这一组时
  # 合成话音台的单音会真的响一分多钟。静音不影响验收，音频照常走完整条播放链路
  "$CHROME" --headless=new --remote-debugging-port=$DBG --user-data-dir="$TMP/chrome" --no-first-run \
    --enable-unsafe-swiftshader --use-angle=swiftshader --window-size=1400,900 \
    --autoplay-policy=no-user-gesture-required --mute-audio about:blank >"$TMP/chrome.log" 2>&1 &
  CP=$!; sleep 2
}
serve() {   # 其余参数原样传给 serve.py；端口被占会顺延，所以从输出里读实际端口
  (cd "$ROOT" && exec python3 -u serve.py 8931 --no-update "$@") >"$TMP/serve.log" 2>&1 &
  SP=$!; sleep 2
  PORT=$(grep -o 'localhost:[0-9]*' "$TMP/serve.log" | head -1 | cut -d: -f2)
}
stop() { kill $CP $SP 2>/dev/null; wait 2>/dev/null; sleep 3; }

if [[ $WHAT == base || $WHAT == all ]]; then
  # 也给它一个空的运行目录：不然你自己正开着接收机时，这个「不启用接收机」的页面
  # 会顺着 .runtime/sdrd.json 找到那个守护进程连上去，测的就不是基础页面了
  export AIRPORTS_RUNTIME="$TMP/runtime-base"
  echo "===== 基础页面 · 服务器模式 ====="; serve; chrome
  node "$HERE/base-page.mjs" $DBG "http://localhost:$PORT/" server || RC=1
  echo "===== 基础页面 · file:// 模式 ====="
  node "$HERE/base-page.mjs" $DBG "file://$ROOT/index.html" file || RC=1
  stop
fi
# 合成信号里除了 ADS-B 流量还有三个话音台，对着希思罗的频率表摆的：
#   118.5 塔台，载波偏 −7 kHz，连续 1 kHz 单音（验频率跟踪和音频主频）
#   121.9 地面，说 2 秒停 5 秒，700 Hz（验静噪和轮巡）
#   128.075 通播，连续，500 Hz（在另一个窗口里）
#   119.725 一根不带调制的空载波，扮演室内常见的干扰（验「无话音时静音」）
SYNTH="synth:adsb:51.47,-0.45;air:118.5@-7000,121.9~2/5*700,128.075*500,119.725%0"
if [[ $WHAT == receiver || $WHAT == all ]]; then
  echo "===== 接收机 · 合成信号 ====="
  export SDR_SOURCE="$SYNTH" AIRPORTS_RUNTIME="$TMP/runtime"
  serve --sdr; chrome
  node "$HERE/receiver.mjs" $DBG "http://localhost:$PORT/" || RC=1
  stop
fi
if [[ $WHAT == voice || $WHAT == all ]]; then
  echo "===== 话音监听 · 合成信号 ====="
  export SDR_SOURCE="$SYNTH" AIRPORTS_RUNTIME="$TMP/runtime-voice" SDR_AUTO_RETURN_S=8
  serve --sdr; chrome
  node "$HERE/voice.mjs" $DBG "http://localhost:$PORT/" || RC=1
  stop
fi
if [[ $WHAT == spectrum || $WHAT == all ]]; then
  echo "===== 频谱与瀑布图 · 合成信号 ====="
  # 多一个 119.3（说 3 秒停 3 秒，400 Hz）：它和塔台在同一个窗口里，但希思罗的频率表里没有它
  export SDR_SOURCE="$SYNTH,119.3~3/3*400" AIRPORTS_RUNTIME="$TMP/runtime-spectrum" SDR_AUTO_RETURN_S=600
  serve --sdr; chrome
  node "$HERE/spectrum.mjs" $DBG "http://localhost:$PORT/" || RC=1
  stop
fi
exit $RC

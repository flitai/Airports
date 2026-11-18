#!/bin/zsh
# 浏览器端验收：用无头 Chrome 驱动真实页面，经 window.__probe / __act 断言。
# 用法：sdr/tests/browser/run.sh [base|receiver|all]      （默认 all）
#
#   base      不启用接收机：联网航班、多来源合并、切底图；以及 file:// 模式
#   receiver  用合成 ADS-B 信号走完整链路：serve.py → sdrd → readsb → 页面
#
# receiver 会把接收机位置设到测试坐标，所以它用一个临时的运行目录，
# 不碰 .runtime/sdr.json 里你自己的设置。
set -u
ROOT=${0:A:h:h:h:h}; HERE=${0:A:h}; WHAT=${1:-all}
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
TMP=$(mktemp -d); DBG=9333; RC=0
trap 'kill $CP $SP 2>/dev/null; wait 2>/dev/null; rm -rf "$TMP"' EXIT

chrome() {
  # 不加 swiftshader 这两个参数，无头模式下没有 WebGL，地图永远起不来
  "$CHROME" --headless=new --remote-debugging-port=$DBG --user-data-dir="$TMP/chrome" --no-first-run \
    --enable-unsafe-swiftshader --use-angle=swiftshader --window-size=1400,900 about:blank >"$TMP/chrome.log" 2>&1 &
  CP=$!; sleep 2
}
serve() {   # 其余参数原样传给 serve.py；端口被占会顺延，所以从输出里读实际端口
  (cd "$ROOT" && exec python3 -u serve.py 8931 --no-update "$@") >"$TMP/serve.log" 2>&1 &
  SP=$!; sleep 2
  PORT=$(grep -o 'localhost:[0-9]*' "$TMP/serve.log" | head -1 | cut -d: -f2)
}
stop() { kill $CP $SP 2>/dev/null; wait 2>/dev/null; sleep 3; }

if [[ $WHAT == base || $WHAT == all ]]; then
  echo "===== 基础页面 · 服务器模式 ====="; serve; chrome
  node "$HERE/base-page.mjs" $DBG "http://localhost:$PORT/" server || RC=1
  echo "===== 基础页面 · file:// 模式 ====="
  node "$HERE/base-page.mjs" $DBG "file://$ROOT/index.html" file || RC=1
  stop
fi
if [[ $WHAT == receiver || $WHAT == all ]]; then
  echo "===== 接收机 · 合成信号 ====="
  export SDR_SOURCE="synth:adsb:51.47,-0.45" AIRPORTS_RUNTIME="$TMP/runtime"
  serve --sdr; chrome
  node "$HERE/receiver.mjs" $DBG "http://localhost:$PORT/" || RC=1
  stop
fi
exit $RC

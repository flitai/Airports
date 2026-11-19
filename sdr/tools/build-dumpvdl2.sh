#!/bin/sh
# 编译 VDL2 数据链解码器 dumpvdl2（和它依赖的 libacars），装进仓库里的私有目录
# sdr/.vdl2/。Homebrew 没有这两个包；这里不碰系统目录，删掉 sdr/.vdl2 就干净了。
#
#   sdr/tools/build-dumpvdl2.sh
#
# 需要：git、cmake、pkg-config、glib（brew install cmake pkg-config glib）。
# 装了 librtlsdr 的话顺带编进去——守护进程用不上（它走管道喂采样），但排查时
# 可以让 dumpvdl2 自己直接开接收机做个基线。
#
# 两个仓库都钉在发布标签对应的那次提交上，取下来的不是它就不编。
set -eu

LIBACARS_TAG=v2.2.1
LIBACARS_SHA=9af09a0121d4ec577339cbd4c7420d7519da48fa
DUMPVDL2_TAG=v2.7.0
DUMPVDL2_SHA=686e87f2294ef6275855ba7364bd4bb9a0f8f56a

HERE=$(cd "$(dirname "$0")" && pwd)
PREFIX=${SDR_VDL2_PREFIX:-$HERE/../.vdl2}
mkdir -p "$PREFIX"
PREFIX=$(cd "$PREFIX" && pwd)
SRC=$PREFIX/src
JOBS=$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 2)

for tool in git cmake pkg-config; do
  command -v "$tool" >/dev/null || { echo "缺少 $tool：brew install cmake pkg-config glib" >&2; exit 1; }
done
pkg-config --exists glib-2.0 || { echo "缺少 glib：brew install glib" >&2; exit 1; }

LOG=$PREFIX/build.log
: >"$LOG"
quiet() {    # 编译输出进日志（光是 ASN.1 那部分的警告就有几百行），出了错才把末尾打出来
  "$@" >>"$LOG" 2>&1 || { tail -40 "$LOG" >&2; echo "失败了，完整输出在 $LOG" >&2; exit 1; }
}

fetch() {    # 名字 地址 标签 提交
  rm -rf "$SRC/$1"
  git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$3" "$2" "$SRC/$1"
  got=$(git -C "$SRC/$1" rev-parse HEAD)
  [ "$got" = "$4" ] || { echo "$1 $3 取到的是 $got，不是预期的 $4，不编" >&2; exit 1; }
}

mkdir -p "$SRC"
echo "== libacars $LIBACARS_TAG"
fetch libacars https://github.com/szpajder/libacars "$LIBACARS_TAG" "$LIBACARS_SHA"
# JSON 输出是 libacars 自带的，不靠 jansson；libxml2 只管把 XML 排版得好看。都不要
quiet cmake -S "$SRC/libacars" -B "$SRC/libacars/build" -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_INSTALL_LIBDIR=lib \
      -DCMAKE_INSTALL_NAME_DIR="$PREFIX/lib" \
      -DLIBXML2=FALSE -DJANSSON=FALSE
quiet cmake --build "$SRC/libacars/build" -j "$JOBS"
quiet cmake --install "$SRC/libacars/build"

echo "== dumpvdl2 $DUMPVDL2_TAG"
fetch dumpvdl2 https://github.com/szpajder/dumpvdl2 "$DUMPVDL2_TAG" "$DUMPVDL2_SHA"
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
quiet cmake -S "$SRC/dumpvdl2" -B "$SRC/dumpvdl2/build" -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_INSTALL_RPATH="$PREFIX/lib" \
      -DMIRISDR=FALSE -DSDRPLAY=FALSE -DSDRPLAY3=FALSE -DSOAPYSDR=FALSE -DSQLITE=FALSE \
      -DETSY_STATSD=FALSE -DRAW_BINARY_FORMAT=FALSE -DZMQ=FALSE
quiet cmake --build "$SRC/dumpvdl2/build" -j "$JOBS"
quiet cmake --install "$SRC/dumpvdl2/build"

# dumpvdl2 自带的一段模型信号：1.05 MS/s 的 16 位 I/Q，里面是真的 VDL2 波形。
# 合成信号源和单元测试拿它当素材（sdrd/synth.py 的 Vdl2Model），不插接收机也能验整条链
mkdir -p "$PREFIX/share"
cp "$SRC/dumpvdl2/test/vdl2_model_16b_1050kHz.wav" "$PREFIX/share/"
rm -rf "$SRC"                            # 源码和中间产物有 50 多 MB，装完就不留了

BIN=$PREFIX/bin/dumpvdl2
"$BIN" --version
# 自检：把那段模型信号直接喂给它，应当解出两帧
N=$(tail -c +45 "$PREFIX/share/vdl2_model_16b_1050kHz.wav" | "$BIN" --iq-file - --sample-format S16_LE \
      --oversample 10 --centerfreq 136975000 --output decoded:json:file:path=- 136975000 2>/dev/null | grep -c '"vdl2"')
[ "$N" -ge 1 ] || { echo "编出来了，但解不出自带的模型信号（得到 $N 帧）" >&2; exit 1; }
echo "装好了：${BIN}，自检解出 ${N} 帧"

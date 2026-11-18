#!/bin/sh
# Airports · 把桌面外壳打成一个 macOS 应用
# SPDX-License-Identifier: GPL-3.0-only
#
# 用法：sh desktop/build-app.sh          （或在 desktop/ 里 npm run package）
# 产物：desktop/dist/Airports.app 和同目录下的 zip
#
# 做法是直接拿 node_modules 里那份 Electron.app 改：把外壳代码放进
# Resources/app，把页面、serve.py、数据和接收机守护进程放进 Resources/airports，
# 改 Info.plist，重新签名。没有用 electron-packager / electron-builder——它们做的
# 就是这几步，却要再从网上下一遍 Electron，而这个项目一直要求离线可用。
#
# 打出来的是「薄壳」，不是自带一切的应用。目标机器上仍然需要：
#   python3                         serve.py 用系统自带的即可
#   uv、librtlsdr、readsb            只有用本地接收机时才需要（brew install）
# 离线瓦片（一百多 GB）不在包里，启动后在菜单「底图 → 选择离线瓦片目录…」里指定。
#
# 没有开发者签名，只做了本机签名（ad-hoc）。在打包的这台机器上可以直接打开；
# 拷到别的 Mac 上，系统会因为「无法验证开发者」拦一次，需要右键 → 打开。
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
SRC=${ELECTRON_APP:-$HERE/node_modules/electron/dist/Electron.app}
VERSION=$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$HERE/package.json" | head -1)
ARCH=$(uname -m)
OUT=$HERE/dist
APP=$OUT/Airports.app
ZIP=$OUT/Airports-$VERSION-mac-$ARCH.zip

[ -d "$SRC" ] || { echo "找不到 Electron.app：先在 desktop/ 里 npm install，或用 ELECTRON_APP 指定" >&2; exit 1; }

mkdir -p "$OUT"
rm -rf "$OUT/Airports.app" "$ZIP"
cp -R "$SRC" "$APP"

RES=$APP/Contents/Resources
rm -f "$RES/default_app.asar"                 # Electron 自带的欢迎页，用不着
mkdir -p "$RES/app" "$RES/airports/sdr"

# 外壳本身
cp "$HERE/main.js" "$HERE/package.json" "$RES/app/"

# 页面、服务器、数据。测试、构建脚本、原始 CSV、瓦片下载工具都不带
cp "$ROOT/index.html" "$ROOT/serve.py" "$ROOT/build_faa.py" \
   "$ROOT/LICENSE" "$ROOT/THIRD-PARTY-NOTICES.md" "$ROOT/README.md" "$RES/airports/"
rsync -a --exclude __pycache__ --exclude .DS_Store "$ROOT/data" "$ROOT/vendor" "$RES/airports/"
cp "$ROOT/sdr/pyproject.toml" "$ROOT/sdr/uv.lock" "$ROOT/sdr/NOTICE.md" "$RES/airports/sdr/"
rsync -a --exclude __pycache__ --exclude .DS_Store "$ROOT/sdr/sdrd" "$ROOT/sdr/ui" "$RES/airports/sdr/"

# 名字和版本。主程序必须改名：Electron 靠主程序的文件名判断自己是不是「已打包」
# （app.isPackaged），仍叫 Electron 的话外壳会去错误的目录里找 serve.py。
# Frameworks 里的几个 Helper 不用改名，Electron 会先按「Electron Helper」去找。
mv "$APP/Contents/MacOS/Electron" "$APP/Contents/MacOS/Airports"
PLIST=$APP/Contents/Info.plist
plutil -replace CFBundleExecutable -string "Airports" "$PLIST"
plutil -replace CFBundleName -string "Airports" "$PLIST"
plutil -replace CFBundleDisplayName -string "Airports" "$PLIST"
plutil -replace CFBundleIdentifier -string "org.flitai.airports" "$PLIST"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$PLIST"
plutil -replace CFBundleVersion -string "$VERSION" "$PLIST"

# 改过内容之后原来的签名就对不上了；Apple 芯片上签名无效的程序根本起不来
codesign --force --deep --sign - "$APP" >/dev/null 2>&1
codesign --verify --deep "$APP"

ditto -c -k --keepParent "$APP" "$ZIP"
echo "应用  $APP"
echo "压缩包  $ZIP  ($(du -h "$ZIP" | cut -f1))"

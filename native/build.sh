#!/bin/bash
# D99：把原生外壳编成真正的 AgentOS.app。
#
# 为什么手搓 bundle 而不用 xcodeproj：本机只装了 CommandLineTools
# （实测 xcodebuild 不可用），而 swiftc 直编 + 手工 Info.plist 产出的
# 是同一个东西 —— Finder 双击能开、菜单栏能挂、Dock 有图标、能进
# 「键盘快捷键」设置面板，.app 该有的全有。
#
# 幂等：每次重建先清 Contents 再放，避免上一次残留混进来
#（用 find -delete，不依赖 rm）。
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NATIVE="$ROOT/native"
APP="$ROOT/AgentOS.app"
SRC="$NATIVE/Sources/AgentOSShell"

SWIFTC="$(command -v swiftc)" || { echo "FAIL no-swiftc"; exit 3; }

[ -d "$APP/Contents" ] && find "$APP/Contents" -depth -delete

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$NATIVE/Resources/Info.plist" "$APP/Contents/Info.plist"
if [ -f "$NATIVE/Resources/AppIcon.icns" ]; then
  cp "$NATIVE/Resources/AppIcon.icns" "$APP/Contents/Resources/"
fi

"$SWIFTC" -O -swift-version 5 \
  -framework AppKit -framework WebKit -framework Foundation \
  -o "$APP/Contents/MacOS/AgentOS" \
  "$SRC"/*.swift
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "BUILD_FAIL rc=$rc"
  exit "$rc"
fi

# 签名：ad-hoc 就够让本地 .app 被 Gatekeeper 认成本地构建产物，
# 免得每次双击都弹「无法验证开发者」。
codesign --force --sign - "$APP" 2>/dev/null || echo "WARN codesign-skipped"

echo "BUILD_OK $APP"
exit 0

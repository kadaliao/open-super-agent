#!/bin/sh
# App 图标：src-tauri/icons/AppIcon.icon（Icon Composer 做的，Mac 和 iPhone 共用）编译成 Mac 用的两个文件，改了图标后跑一次并提交：
#   src-tauri/icons/Assets.car  macOS 26 起按它画 Liquid Glass（tauri.conf.json 的 bundle.macOS.files 放进 App，Info.plist 的 CFBundleIconName 指向它）
#   src-tauri/icons/icon.icns   更早的 macOS 用的平面版，actool 按同一个图标画好
# 需要 Xcode 26 的 actool（CI 上没有，所以编好的文件提交进仓库）。iPhone 版由 Xcode 打包时自己编译同一个文件。
set -eu
ICONS="$(cd "$(dirname "$0")/../src-tauri/icons" && pwd)"
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
xcrun actool "$ICONS/AppIcon.icon" --compile "$OUT" --platform macosx --minimum-deployment-target 12.0 \
  --app-icon AppIcon --output-partial-info-plist "$OUT/partial.plist" --target-device mac >/dev/null
cp "$OUT/Assets.car" "$ICONS/Assets.car"
cp "$OUT/AppIcon.icns" "$ICONS/icon.icns"
echo "已更新 $ICONS/Assets.car 和 icon.icns"

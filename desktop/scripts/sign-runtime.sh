#!/usr/bin/env bash
# 发布前用 Developer ID 给 desktop/build/runtime 里的每个可执行文件和动态库签名（Tauri 不签 Resources 里的东西，公证要求全部签过）。
# 用法：desktop/scripts/sign-runtime.sh "Developer ID Application: 名字 (TEAMID)"
set -euo pipefail
IDENTITY="$1"
DESK="$(cd "$(dirname "$0")/.." && pwd)"
ENT="$DESK/src-tauri/runtime.entitlements"
n=0
while IFS= read -r -d '' f; do
  file -b "$f" | grep -q Mach-O || continue
  extra=()
  [ "$f" = "$DESK/build/runtime/bin/python3" ] && extra=(--entitlements "$ENT")
  codesign --force --options runtime --timestamp --sign "$IDENTITY" ${extra[@]+"${extra[@]}"} "$f"
  n=$((n + 1))
done < <(find "$DESK/build/runtime" -type f -print0)
echo "签好了 $n 个文件"

#!/bin/sh
# 在模拟器上点一下：mobile/uitest/run.sh <设备 UDID> <步骤> [要找的文字]
# 步骤：AllowNotifications（点权限框的“允许”）、Lock、Home、WaitNotification、TapNotification（点含这段文字的通知）、Dump
#       App 里的手势：EdgeSwipe（左边缘右滑）、EdgeSwipeShort（滑一点就松手）、Pinch、DoubleTap、DragPage（拖顶上的标题栏）、
#       Tap "x,y"、Drag "x0,y0,x1,y1"（0–1，相对屏幕）
# 第一次跑会先生成工程并编译（约 30 秒），之后每步几秒。
set -e
cd "$(dirname "$0")"
[ -d UIDriver.xcodeproj ] || xcodegen generate -q
run=$(ls build/Build/Products/*.xctestrun 2>/dev/null | head -1)
if [ -z "$run" ]; then
  xcodebuild build-for-testing -project UIDriver.xcodeproj -scheme Driver -destination "id=$1" -derivedDataPath build -quiet
  run=$(ls build/Build/Products/*.xctestrun | head -1)
fi
TEST_RUNNER_NOTIF_TEXT="${3:-}" xcodebuild test-without-building -xctestrun "$run" -destination "id=$1" \
  -only-testing:"Driver/DriverTests/test$2" 2>&1 | grep -E "Test Case.*(passed|failed)|FOUND:|TAPPING:|error:" || true

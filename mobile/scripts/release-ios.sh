#!/bin/sh
# 打 iOS 的 App Store 包（TestFlight 也用它）：mobile/scripts/release-ios.sh <构建号> [--upload]
#
# 1. 从钥匙串取 Apple Distribution 证书，放进一个临时钥匙串（codesign 不弹授权框），打完删掉
# 2. tauri ios build --archive-only：编 Rust、打 release 的 xcarchive（签名设置在 gen/apple/project.yml 的 release 配置里）
# 3. 用 ExportOptions-appstore.plist 导出 .ipa，检查 App 和通知扩展的权限、版本号
# 4. altool 先 validate；带 --upload 才上传到 App Store Connect
#
# 要先在本机装好两份 App Store 描述文件（“Open Super Agent App Store”“Open Super Agent NotificationService App Store”）。
# 钥匙串里要有：Open Super Agent Apple Distribution p12（base64）和 … p12 password；Open Super Agent notarization key（App Store Connect API key）。
# 打包时不要设 APPLE_API_* 环境变量：Tauri 会改用自动签名，通知扩展的权限会丢（tauri#15663）。只在上传那一步用 API key。
set -eu
BUILD="${1:?usage: release-ios.sh <build-number> [--upload]}"
UPLOAD="${2:-}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MOBILE="$ROOT/mobile"
OUT="$MOBILE/src-tauri/gen/apple/build/appstore"
API_KEY_ID=3MNW5ZF2R8
API_ISSUER=08aa7ad2-2c31-4653-8dd7-7314a4b981b6
VERSION=$(node -p "require('$MOBILE/package.json').version")
export PATH="$HOME/.cargo/bin:$PATH"
unset APPLE_API_KEY APPLE_API_ISSUER APPLE_API_KEY_PATH 2>/dev/null || true

TMP=$(mktemp -d)
KC="$TMP/osa-signing.keychain-db"
KCPASS=$(openssl rand -hex 16)
ORIG_KEYCHAINS=$(security list-keychains -d user | tr -d '"')
cleanup() {
  # shellcheck disable=SC2086
  security list-keychains -d user -s $ORIG_KEYCHAINS
  security delete-keychain "$KC" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT

echo "== 签名证书 → 临时钥匙串"
security find-generic-password -s "Open Super Agent Apple Distribution p12" -w | base64 -d > "$TMP/dist.p12"
security create-keychain -p "$KCPASS" "$KC"
security set-keychain-settings -lut 3600 "$KC"
security unlock-keychain -p "$KCPASS" "$KC"
security import "$TMP/dist.p12" -k "$KC" -P "$(security find-generic-password -s "Open Super Agent Apple Distribution p12 password" -w)" \
  -T /usr/bin/codesign -T /usr/bin/security >/dev/null
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KCPASS" "$KC" >/dev/null
rm -f "$TMP/dist.p12"
# shellcheck disable=SC2086
security list-keychains -d user -s "$KC" $ORIG_KEYCHAINS
security find-identity -v -p codesigning "$KC" | grep -q "Apple Distribution" || { echo "临时钥匙串里没有 Apple Distribution 证书"; exit 1; }

echo "== 打包 $VERSION ($BUILD)"
cd "$MOBILE"
# 构建号用 bundleVersion 直接写进 CFBundleVersion。--build-number 会接在版本号后面变成 0.7.0.1，App Store 只认最多三段数字
npx tauri ios build --archive-only -c "{\"bundle\":{\"iOS\":{\"bundleVersion\":\"$BUILD\"}}}"
ARCHIVE="$MOBILE/src-tauri/gen/apple/build/osa-mobile_iOS.xcarchive"

echo "== 导出 ipa"
rm -rf "$OUT"
xcodebuild -exportArchive -archivePath "$ARCHIVE" -exportPath "$OUT" -exportOptionsPlist "$MOBILE/scripts/ExportOptions-appstore.plist" -quiet
IPA=$(ls "$OUT"/*.ipa)

echo "== 检查权限和版本号"
unzip -q -o "$IPA" -d "$TMP/ipa"
APP=$(ls -d "$TMP"/ipa/Payload/*.app)
EXT="$APP/PlugIns/NotificationService.appex"
codesign -d --entitlements - --xml "$APP" 2>/dev/null > "$TMP/app.ent"
codesign -d --entitlements - --xml "$EXT" 2>/dev/null > "$TMP/ext.ent"
grep -q "<key>aps-environment</key><string>production</string>" "$TMP/app.ent" || { echo "App 没有 aps-environment=production"; exit 1; }
for f in app ext; do grep -q "group.com.kadaliao.opensuperagent" "$TMP/$f.ent" || { echo "$f 没有 App Group"; exit 1; }; done
for p in "$APP" "$EXT"; do
  v=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$p/Info.plist")
  b=$(/usr/libexec/PlistBuddy -c "Print :CFBundleVersion" "$p/Info.plist")
  [ "$v" = "$VERSION" ] && [ "$b" = "$BUILD" ] || { echo "$p 的版本是 $v ($b)，应该是 $VERSION ($BUILD)"; exit 1; }
done
echo "权限、版本号都对：$IPA"

echo "== altool validate"
mkdir -p "$TMP/keys"
security find-generic-password -s "Open Super Agent notarization key" -w | xxd -r -p > "$TMP/keys/AuthKey_$API_KEY_ID.p8"
export API_PRIVATE_KEYS_DIR="$TMP/keys"
xcrun altool --validate-app -f "$IPA" -t ios --apiKey "$API_KEY_ID" --apiIssuer "$API_ISSUER" --output-format normal
if [ "$UPLOAD" = "--upload" ]; then
  echo "== 上传到 App Store Connect"
  xcrun altool --upload-app -f "$IPA" -t ios --apiKey "$API_KEY_ID" --apiIssuer "$API_ISSUER" --output-format normal
fi

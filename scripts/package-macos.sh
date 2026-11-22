#!/bin/sh
# Builds "GeekBridge Phone.app" for this Mac (dist/). Ad-hoc signed; for other Macs the app
# must be signed with a Developer ID and notarised.
set -eu
cd "$(dirname "$0")/.."
cargo build --release -p phone-app
VERSION=$(cargo metadata --no-deps --format-version 1 | python3 -c 'import json,sys;print([p["version"] for p in json.load(sys.stdin)["packages"] if p["name"]=="phone-app"][0])')
APP="dist/GeekBridge Phone.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp target/release/geekbridge-phone "$APP/Contents/MacOS/"
# Mac voice mode is built into the app. Only the one-time root helper and its installer ship as files.
mkdir -p "$APP/Contents/Resources/macos"
cp packaging/macos/btmode packaging/macos/install-helper.sh "$APP/Contents/Resources/macos/"
chmod 755 "$APP/Contents/Resources/macos/btmode" "$APP/Contents/Resources/macos/install-helper.sh"
cat > "$APP/Contents/Resources/THIRD-PARTY-LICENSES.txt" <<LIC
GeekBridge Phone (macOS) bundles code under these licences.

== ai-caller native bridge (crates/phone-macos/native/ai-caller), MIT ==
$(cat crates/phone-macos/native/ai-caller/LICENSE)

== Rust dependencies ==
Statically linked crates (cpal: Apache-2.0, and others) keep their own licences; run
"cargo about" or "cargo license" in the source tree for the full list.
LIC
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>GeekBridge Phone</string>
<key>CFBundleIdentifier</key><string>dev.geekbridge.Phone</string>
<key>CFBundleExecutable</key><string>geekbridge-phone</string>
<key>CFBundleVersion</key><string>$VERSION</string>
<key>CFBundleShortVersionString</key><string>$VERSION</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>NSHighResolutionCapable</key><true/>
<key>NSMicrophoneUsageDescription</key><string>GeekBridge Phone uses the microphone so you can talk on phone calls through this Mac.</string>
<key>NSBluetoothAlwaysUsageDescription</key><string>GeekBridge Phone talks to your phone over Bluetooth for calls, texts and contacts.</string>
</dict></plist>
PLIST
ENT=$(mktemp)
cat > "$ENT" <<ENTS
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>com.apple.security.device.bluetooth</key><true/>
<key>com.apple.security.device.audio-input</key><true/>
</dict></plist>
ENTS
# Sign from the inside out: every Mach-O file in the bundle first, then the app. SIGN_IDENTITY is a
# Developer ID for distribution; the default "-" is ad hoc and runs on this Mac only.
IDENT="${SIGN_IDENTITY:--}"
OPTS=""
[ "$IDENT" != "-" ] && OPTS="--options runtime --timestamp"
# shellcheck disable=SC2086
codesign --force --sign "$IDENT" $OPTS --entitlements "$ENT" "$APP"
rm -f "$ENT"
echo "Built $APP"

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
# Mac voice mode helper: our code, the native bridge, a relocatable Python with the pinned packages,
# and the licences. Run helper/macos-btd/build.sh and make-runtime.sh first.
HELPER=helper/macos-btd
if [ ! -x "$HELPER/build/python/bin/python3" ] || [ ! -f "$HELPER/build/libacipc_bridge.dylib" ]; then
    echo "Run $HELPER/build.sh and $HELPER/make-runtime.sh first (native bridge and Python runtime)." >&2
    exit 1
fi
DEST="$APP/Contents/Resources/helper/macos-btd"
mkdir -p "$DEST/build"
rsync -a --exclude __pycache__ "$HELPER/btd.py" "$HELPER/gbp_btd" "$DEST/"
cp "$HELPER/build/libacipc_bridge.dylib" "$DEST/build/"
rsync -a --exclude __pycache__ "$HELPER/build/python" "$DEST/"
# Precompile now: the helper runs with -B and must not write into the signed bundle.
"$DEST/python/bin/python3" -I -m compileall -q "$DEST/python/lib" "$DEST/gbp_btd" "$DEST/btd.py"
"$DEST/python/bin/python3" -I "$HELPER/gen_licences.py" "$DEST/python/lib/python3.13/site-packages" \
    "$APP/Contents/Resources/THIRD-PARTY-LICENSES.txt" \
    "$HELPER/third_party/ai-caller/LICENSE" "$DEST/python/lib/python3.13/LICENSE.txt"
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
HELPER_ENT=$(mktemp)
cat > "$HELPER_ENT" <<ENTS
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>com.apple.security.cs.disable-library-validation</key><true/>
</dict></plist>
ENTS
find "$APP/Contents/Resources" -type f | while read -r f; do
    if file -b "$f" | grep -q "Mach-O"; then
        # shellcheck disable=SC2086
        codesign --force --sign "$IDENT" $OPTS --entitlements "$HELPER_ENT" "$f"
    fi
done
rm -f "$HELPER_ENT"
# shellcheck disable=SC2086
codesign --force --sign "$IDENT" $OPTS --entitlements "$ENT" "$APP"
rm -f "$ENT"
echo "Built $APP"

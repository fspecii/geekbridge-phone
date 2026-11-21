#!/bin/sh
# Builds the stage 2 pieces. Offline except for the one-time Bumble download. Runs nothing that
# touches Bluetooth.
set -eu
cd "$(dirname "$0")"
mkdir -p build

# Python environment with the pinned Bumble (the version the reference project used).
if [ ! -x .venv/bin/python ]; then
    /opt/homebrew/bin/python3 -m venv .venv
fi
.venv/bin/python -I -m pip install --disable-pip-version-check --quiet "bumble==0.0.236" "sounddevice==0.5.6" "audioop-lts==0.2.2"

# Native bridge, from the reference project's unmodified source (MIT, licence alongside it).
clang -dynamiclib -Wall -Wextra -Werror third_party/ai-caller/acipc_bridge.c \
    -o build/libacipc_bridge.dylib -framework CoreFoundation -framework IOKit

# Launcher in a signed app bundle: macOS aborts a bare process that touches Bluetooth power.
APP=build/GBBtLauncher.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
clang -Wall -Wextra -Werror -o "$APP/Contents/MacOS/launcher" launcher.c
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>GBBtLauncher</string>
<key>CFBundleIdentifier</key><string>dev.geekbridge.btlauncher</string>
<key>CFBundleExecutable</key><string>launcher</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSBackgroundOnly</key><true/>
<key>NSMicrophoneUsageDescription</key><string>GeekBridge Phone test: carries your voice on a phone call through this Mac.</string>
<key>NSBluetoothAlwaysUsageDescription</key><string>GeekBridge Phone test: uses the Mac's Bluetooth controller directly.</string>
</dict></plist>
PLIST
codesign --force --sign - "$APP"

# Stage 6: the small app that asks macOS to connect the phone (see connect.m).
CONNECT=build/GBBtConnect.app
rm -rf "$CONNECT"
mkdir -p "$CONNECT/Contents/MacOS"
clang -fobjc-arc -Wall -Werror -o "$CONNECT/Contents/MacOS/connect" connect.m -framework Foundation -framework IOBluetooth
cat > "$CONNECT/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>GBBtConnect</string>
<key>CFBundleIdentifier</key><string>dev.geekbridge.btconnect</string>
<key>CFBundleExecutable</key><string>connect</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSBackgroundOnly</key><true/>
<key>NSBluetoothAlwaysUsageDescription</key><string>GeekBridge Phone test: connects your phone so a call can be answered on this Mac.</string>
</dict></plist>
PLIST
codesign --force --sign - "$CONNECT"

# Test tone for stage 5b (headphones), when ffmpeg is here. Six seconds, the mandatory A2DP codec.
if command -v ffmpeg >/dev/null 2>&1; then
    ffmpeg -loglevel error -y -f lavfi -i "sine=frequency=440:duration=6" -af "volume=0.25" \
        -ac 2 -ar 44100 -c:a sbc -f sbc build/tone.sbc
    ffmpeg -loglevel error -y -f lavfi -i "sine=frequency=440:duration=120" -af "volume=0.15" \
        -ac 2 -ar 44100 -c:a sbc -f sbc build/tone-long.sbc
fi

# Offline tests: the safety filter and the key store.
.venv/bin/python -I -m unittest discover -s tests
echo "built: build/libacipc_bridge.dylib and $APP"

#!/bin/sh
# Builds the macOS Bluetooth helper: Python environment (pinned Bumble), the native bridge, and the
# offline tests. Touches no Bluetooth.
set -eu
cd "$(dirname "$0")"
mkdir -p build
if [ ! -x .venv/bin/python ]; then
    /opt/homebrew/bin/python3 -m venv .venv
fi
.venv/bin/python -I -m pip install --disable-pip-version-check --quiet "bumble==0.0.236"
# The native bridge, from the reference project's unmodified source (MIT; licence alongside it).
clang -dynamiclib -Wall -Wextra -Werror third_party/ai-caller/acipc_bridge.c \
    -o build/libacipc_bridge.dylib -framework CoreFoundation -framework IOKit
.venv/bin/python -I -m unittest discover -s tests
echo "built: build/libacipc_bridge.dylib; tests passed"

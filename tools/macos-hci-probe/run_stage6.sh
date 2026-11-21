#!/bin/sh
# Stage 6: macOS's Bluetooth through our pass-through, for a bounded time. See docs/MACOS-COEXIST.md.
#
#   ./run_stage6.sh [seconds]                   6a: plain pass-through; default 90
#   ./run_stage6.sh <seconds> "<phone>"         6b: also keep that phone's voice link (silence, counted)
#   ./run_stage6.sh <seconds> "<phone>" sound   6c: the same, with the call's sound on the Mac's input and output
#
# With the helper installed (sudo ./install-helper.sh, once) it asks for no password and may run
# for up to twelve hours; without it, it asks once and runs for up to ten minutes. While it
# runs, use the mouse and headphones as usual. Everything is put back when it ends, by a timer
# if it does not end properly, and by a reboot in any case.
set -eu
cd "$(dirname "$0")"
D="$PWD"
TOTAL="${1:-90}"
PHONE_ARGS=""
if [ -n "${2:-}" ]; then
    PHONE_ARGS="--phone $(.venv/bin/python -I keys_import.py "$2" --print-address)"
    [ "${3:-}" = sound ] && PHONE_ARGS="$PHONE_ARGS --sound"
fi
mkdir -p logs
LOG="logs/stage6a-$(date +%Y%m%d-%H%M%S).log"
PORT_FILE="$(mktemp -t gbp-port)"
: > "$PORT_FILE"
HELPER=/usr/local/libexec/geekbridge/btmode
if [ -x "$HELPER" ] && sudo -n "$HELPER" status >/dev/null 2>&1; then
    GO_FILE=/var/run/geekbridge-btmode/go
    start_root() { sudo -n "$HELPER" start "$1" "$TOTAL"; }
    revert_root() { sudo -n "$HELPER" revert; }
    sudo -n "$HELPER" revert >/dev/null
else
    GO_FILE="$PORT_FILE.go"
    rm -f "$GO_FILE"
    start_root() { sudo "$D/stage6_root.sh" start "$1" "$TOTAL" "$GO_FILE"; }
    revert_root() { sudo -n "$D/stage6_root.sh" revert "$GO_FILE"; }
    sudo -v
fi
START="$(date '+%Y-%m-%d %H:%M:%S')"
.venv/bin/python -I stage6a_passthrough.py --seconds "$((TOTAL - 15))" --port-file "$PORT_FILE" --go-file "$GO_FILE" $PHONE_ARGS > "$LOG" 2>&1 &
PROXY=$!
# Ctrl-C ends the test early and still puts Bluetooth back.
trap 'kill "$PROXY" 2>/dev/null; wait "$PROXY" 2>/dev/null; revert_root >/dev/null 2>&1; exit 130' INT TERM
for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s "$PORT_FILE" ] && break; sleep 0.3; done
[ -s "$PORT_FILE" ] || { echo "the pass-through did not start" | tee -a "$LOG"; kill "$PROXY" 2>/dev/null; exit 1; }
start_root "$(cat "$PORT_FILE")" | tee -a "$LOG"
echo "running for $TOTAL seconds (Ctrl-C to end early): use the mouse and headphones as usual"
wait "$PROXY" || true
# Put Bluetooth back as soon as our side is done or has failed, not when the timer fires:
# the second live run failed at once and left the Mac without Bluetooth for five minutes.
revert_root 2>&1 | tee -a "$LOG" || echo "could not revert early; the timer will" | tee -a "$LOG"
sleep 6
{
    echo "--- what bluetoothd logged ---"
    /usr/bin/log show --start "$START" --style compact --predicate 'process == "bluetoothd"' 2>/dev/null \
        | grep -i -E "HCI Transport|BT_HCI_TRANSPORT|UART open|Searching for transport|FATAL|Invalid chipset|Stack initialization|SCO/eSCO reject" \
        | sed -E 's/([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}/<address>/g' | cut -c1-240 | head -40
    echo "--- Bluetooth now ---"
    /usr/sbin/system_profiler SPBluetoothDataType 2>/dev/null | grep -E "State:|Transport:|Chipset:"
} >> "$LOG" 2>&1
rm -f "$PORT_FILE"
sed -E 's/([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}/<address>/g' "$LOG" | cut -c1-300
echo "log saved: $D/$LOG"

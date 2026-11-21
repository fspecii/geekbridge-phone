#!/bin/sh
# The root step of stage 6. Run by run_stage6.sh through sudo; do not run it alone.
#
#   stage6_root.sh start <serial port> <seconds> <go file>
#   stage6_root.sh revert <go file>
#
# start: restarts macOS's Bluetooth daemon so it reaches the controller through <serial port>,
# and sets a timer that puts everything back after <seconds>. The timer is detached, so it
# fires even if the terminal closes. A reboot also puts everything back: the setting does not
# survive one.
# revert: puts everything back now (the setting cleared, the daemon restarted on its normal
# path) and tells the timer there is nothing left to do.
set -eu
[ "$(id -u)" = 0 ] || { echo "needs root" >&2; exit 77; }
MODE="${1:?start or revert}"
if [ "$MODE" = revert ]; then
    GO="${2:?go file}"
    : > "$GO.reverted"
    /bin/launchctl unsetenv BT_HCI_TRANSPORT
    /usr/bin/killall bluetoothd || true
    echo "Bluetooth is back on its normal path"
    exit 0
fi
[ "$MODE" = start ] || { echo "unknown mode: $MODE" >&2; exit 64; }
PORT="${2:?serial port}"
SECONDS_ON="${3:?seconds}"
GO="${4:?go file}"
case "$PORT" in /dev/ttys[0-9]*) ;; *) echo "not a pseudo-terminal: $PORT" >&2; exit 64 ;; esac
case "$SECONDS_ON" in ''|*[!0-9]*) echo "seconds must be a number" >&2; exit 64 ;; esac
[ "$SECONDS_ON" -ge 20 ] && [ "$SECONDS_ON" -le 600 ] || { echo "seconds must be 20 to 600" >&2; exit 64; }

nohup sh -c "sleep $SECONDS_ON; [ -e '$GO.reverted' ] || { /bin/launchctl unsetenv BT_HCI_TRANSPORT; /usr/bin/killall bluetoothd; }" >/dev/null 2>&1 &
echo "timer set: Bluetooth returns to its normal path in $SECONDS_ON seconds at the latest"
/bin/launchctl setenv BT_HCI_TRANSPORT "H4:POSIX:$PORT,115200"
: > "$GO"
chmod 644 "$GO"
/usr/bin/killall bluetoothd || true
echo "bluetoothd restarted on the serial path"

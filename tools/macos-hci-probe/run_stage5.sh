#!/bin/sh
# Runs one stage 5 probe on a paired device, named by part of its name or by address.
#
#   ./run_stage5.sh import "Magic Mouse" [--reversed]   copy its key from macOS (administrator approval)
#   ./run_stage5.sh 5a "Magic Mouse" [--inject]         mouse probe; switches Bluetooth off for up to 90 s
#   ./run_stage5.sh 5b "AirPods"                        headphones probe; switches Bluetooth off for up to 60 s
#   ./run_stage5.sh 5c "Fold5" "Magic Mouse" "AirPods"  a real call with both beside it; up to 200 s
#
# The launcher always switches Bluetooth back on. Every run's output is saved under logs/.
# See docs/MACOS-COEXIST.md.
set -eu
cd "$(dirname "$0")"
D="$PWD"
STEP="${1:?step: import, 5a, 5b or 5c}"
DEVICE="${2:?device name or address}"
shift 2
mkdir -p logs
LOG="logs/stage5-$STEP-$(date +%Y%m%d-%H%M%S).log"
case "$STEP" in
import)
    .venv/bin/python -I keys_import.py "$DEVICE" "$@" 2>&1 | tee "$LOG"
    echo "log saved: $D/$LOG"
    exit 0 ;;
5a) SCRIPT=stage5a_mouse.py; LIMIT=90 ;;
5b) SCRIPT=stage5b_headphones.py; LIMIT=90 ;;
5c)
    # ./run_stage5.sh 5c "<phone>" "<mouse>" "<headphones>": a real call with both beside it, up to 200 s.
    MOUSE="$(.venv/bin/python -I keys_import.py "${1:?mouse name}" --print-address)"
    BUDS="$(.venv/bin/python -I keys_import.py "${2:?headphones name}" --print-address)"
    shift 2
    mkdir -p -m 700 "$HOME/.local/share/geekbridge-phone/mac-bt"
    printf '{"mouse": "%s", "headphones": "%s"}\n' "$MOUSE" "$BUDS" > "$HOME/.local/share/geekbridge-phone/mac-bt/stage5c.json"
    SCRIPT=stage5c_all.py; LIMIT=200 ;;
*) echo "unknown step: $STEP" >&2; exit 64 ;;
esac
ADDRESS="$(.venv/bin/python -I keys_import.py "$DEVICE" --print-address)"
# Read the key again right before the run: AirPods change theirs between sessions (seen live:
# a key that worked at 21:33 was refused at 21:34 and the keychain held a different one).
for NAME in "$DEVICE" ${MOUSE:+"$MOUSE"} ${BUDS:+"$BUDS"}; do
    .venv/bin/python -I keys_import.py "$NAME" | head -1
done
OUT="$D/$LOG.raw"
: > "$OUT"
open -W --stdout "$OUT" --stderr "$OUT" build/GBBtLauncher.app \
    --args "$D/.venv/bin/python" "$D/$SCRIPT" "$ADDRESS" "$LIMIT" "$@" || true
# The saved log has device addresses masked, so it can be shared as it is.
sed -E 's/([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}/<address>/g' "$OUT" | tee "$LOG"
rm -f "$OUT"
echo "log saved: $D/$LOG"

#!/bin/sh
# Installs (or removes) the one root piece of the macOS pass-through, so it no longer asks for a
# password each time. The app runs it for you once, behind the system's administrator prompt;
# you can also run it yourself:
#
#   sudo ./install-helper.sh                  install for the user who ran sudo
#   ./install-helper.sh --user NAME           install for NAME (when already running as root)
#   sudo ./install-helper.sh remove           take it out again
#
# It copies `btmode` (next to this script) to /usr/local/libexec/geekbridge/btmode, owned by root
# and not writable by anyone else, and adds one sudo rule that lets that one user run that one
# file as root without a password. Read `btmode` first: it says what this allows.
set -eu
[ "$(id -u)" = 0 ] || { echo "run it with sudo" >&2; exit 77; }
DEST=/usr/local/libexec/geekbridge
RULE=/etc/sudoers.d/geekbridge-btmode
OWNER="${SUDO_USER:-}"
ACTION=install
while [ $# -gt 0 ]; do
    case "$1" in
        remove) ACTION=remove ;;
        --user) shift; OWNER="${1:-}" ;;
        *) echo "unknown argument: $1" >&2; exit 64 ;;
    esac
    shift
done
if [ "$ACTION" = remove ]; then
    [ -x "$DEST/btmode" ] && "$DEST/btmode" revert || true
    rm -f "$RULE" "$DEST/btmode"
    rmdir "$DEST" 2>/dev/null || true
    echo "removed"
    exit 0
fi
case "$OWNER" in ''|root) echo "name the user: --user NAME (or run it with sudo from your own account)" >&2; exit 64 ;; esac
case "$OWNER" in *[!A-Za-z0-9._-]*) echo "unexpected user name" >&2; exit 64 ;; esac
id "$OWNER" >/dev/null 2>&1 || { echo "no such user" >&2; exit 64; }
cd "$(dirname "$0")"
[ -f btmode ] || { echo "btmode is not next to this script" >&2; exit 66; }
mkdir -p "$DEST"
chown root:wheel "$DEST"
chmod 755 "$DEST"
install -o root -g wheel -m 755 btmode "$DEST/btmode"
TMP="$(mktemp)"
printf '%s ALL=(root) NOPASSWD: %s\n' "$OWNER" "$DEST/btmode" > "$TMP"
# A broken sudo rule can lock sudo out, so it is checked before it goes in.
/usr/sbin/visudo -cf "$TMP" >/dev/null || { rm -f "$TMP"; echo "the sudo rule did not validate; nothing installed" >&2; exit 1; }
install -o root -g wheel -m 440 "$TMP" "$RULE"
rm -f "$TMP"
echo "installed for $OWNER: $DEST/btmode"

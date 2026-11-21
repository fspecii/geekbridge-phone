#!/bin/sh
# Installs (or removes) the one root piece of the pass-through, so it no longer asks for a
# password each time. Run it yourself, once:
#
#   sudo ./install-helper.sh            install for the user who ran sudo
#   sudo ./install-helper.sh remove     take it out again
#
# It copies helper/btmode to /usr/local/libexec/geekbridge/btmode, owned by root and not
# writable by anyone else, and adds one sudo rule that lets that one user run that one file
# as root without a password. Read helper/btmode first: it says what this allows.
set -eu
[ "$(id -u)" = 0 ] || { echo "run it with sudo" >&2; exit 77; }
DEST=/usr/local/libexec/geekbridge
RULE=/etc/sudoers.d/geekbridge-btmode
if [ "${1:-}" = remove ]; then
    [ -x "$DEST/btmode" ] && "$DEST/btmode" revert || true
    rm -f "$RULE" "$DEST/btmode"
    rmdir "$DEST" 2>/dev/null || true
    echo "removed"
    exit 0
fi
OWNER="${SUDO_USER:-}"
case "$OWNER" in ''|root) echo "run it with sudo from your own account" >&2; exit 64 ;; esac
case "$OWNER" in *[!A-Za-z0-9._-]*) echo "unexpected user name" >&2; exit 64 ;; esac
cd "$(dirname "$0")"
mkdir -p "$DEST"
chown root:wheel "$DEST"
chmod 755 "$DEST"
install -o root -g wheel -m 755 helper/btmode "$DEST/btmode"
TMP="$(mktemp)"
printf '%s ALL=(root) NOPASSWD: %s\n' "$OWNER" "$DEST/btmode" > "$TMP"
# A broken sudo rule can lock sudo out, so it is checked before it goes in.
/usr/sbin/visudo -cf "$TMP" >/dev/null || { rm -f "$TMP"; echo "the sudo rule did not validate; nothing installed" >&2; exit 1; }
install -o root -g wheel -m 440 "$TMP" "$RULE"
rm -f "$TMP"
echo "installed for $OWNER: $DEST/btmode"

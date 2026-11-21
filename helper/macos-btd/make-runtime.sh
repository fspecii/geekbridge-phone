#!/bin/sh
# Builds build/python: a relocatable Python with only the helper's pinned packages (no Homebrew,
# no virtual environment). Needs the runtime tarball in runtime-cache/dl/py.tar.gz (see README);
# the checksum below is checked before anything is unpacked. Touches no Bluetooth.
set -eu
cd "$(dirname "$0")"
SHA=8392b6ea51d94391befb2c31ebe75463f045d0b69dfcf5e1bb3234e1f5fa3405
URL="https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.13.18%2B20261026-aarch64-apple-darwin-install_only_stripped.tar.gz"
TAR=runtime-cache/dl/py.tar.gz
mkdir -p runtime-cache/dl build
if [ ! -f "$TAR" ]; then
    curl -sSL -o "$TAR" "$URL"
fi
echo "$SHA  $TAR" | shasum -a 256 -c - >/dev/null || { echo "runtime checksum mismatch" >&2; rm -f "$TAR"; exit 1; }
rm -rf runtime-cache/x build/python
mkdir -p runtime-cache/x
tar -xzf "$TAR" -C runtime-cache/x
mv runtime-cache/x/python build/python
rmdir runtime-cache/x
P=build/python
# Install the pinned packages, then remove pip itself.
$P/bin/python3 -I -m pip install -q --disable-pip-version-check --no-deps -r requirements-bundle.txt
$P/bin/python3 -I -m pip uninstall -q -y pip
# Trim what a helper never uses.
rm -rf $P/include $P/share $P/bin/idle3* $P/bin/pip* $P/bin/pydoc3* $P/bin/python3*-config \
       $P/lib/tcl* $P/lib/tk* $P/lib/itcl* $P/lib/thread* $P/lib/pkgconfig $P/lib/libtcl* $P/lib/libtk* \
       $P/lib/python3.13/idlelib $P/lib/python3.13/tkinter $P/lib/python3.13/turtledemo \
       $P/lib/python3.13/test $P/lib/python3.13/lib2to3 $P/lib/python3.13/ensurepip \
       $P/lib/python3.13/lib-dynload/_tkinter*
find $P -name __pycache__ -type d -prune -exec rm -rf {} +
"$P/bin/python3" -I -c "import bumble, cryptography, pyee; print('runtime ok', bumble.__file__.split('/')[-3])"
du -sh $P

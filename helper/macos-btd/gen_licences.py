# SPDX-License-Identifier: GPL-3.0-or-later
"""Writes THIRD-PARTY-LICENSES.txt from the installed packages' own metadata and licence files.

Usage: python -I gen_licences.py SITE_PACKAGES OUT [EXTRA_FILE ...]
Each EXTRA_FILE (a licence text, such as the native bridge's) is added after the packages.
"""
import sys
from email.parser import Parser
from pathlib import Path

LICENCE_NAMES = ("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS")


def packages(site):
    for info in sorted(Path(site).glob("*.dist-info"), key=lambda p: p.name.lower()):
        meta = Parser().parsestr((info / "METADATA").read_text(errors="replace"))
        name, version = meta.get("Name", info.name), meta.get("Version", "")
        if name.lower() in ("pip", "setuptools", "wheel"):
            continue
        files = [f for f in sorted(info.rglob("*")) if f.is_file() and f.name.upper().startswith(LICENCE_NAMES)]
        yield name, version, meta.get("License-Expression") or meta.get("License") or "", meta.get("Home-page") or "", files


def main(argv):
    site, out, extras = argv[1], argv[2], argv[3:]
    parts = ["Third-party software bundled with GeekBridge Phone's Mac voice mode helper.\n"]
    missing = []
    for name, version, licence, home, files in packages(site):
        parts.append("=" * 78 + f"\n{name} {version}\nLicence: {licence.splitlines()[0] if licence else 'see below'}\n{home}\n" + "=" * 78 + "\n")
        if not files:
            missing.append(name)
        for f in files:
            parts.append(f"--- {f.name} ---\n{f.read_text(errors='replace').strip()}\n")
    for extra in extras:
        path = Path(extra)
        parts.append("=" * 78 + f"\n{path.parent.name}/{path.name}\n" + "=" * 78 + "\n" + path.read_text(errors="replace").strip() + "\n")
    Path(out).write_text("\n".join(parts))
    if missing:
        print("no licence file found for:", ", ".join(missing), file=sys.stderr)
    print(f"wrote {out}: {len(parts)} sections")


if __name__ == "__main__":
    main(sys.argv)

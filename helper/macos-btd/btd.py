# SPDX-License-Identifier: GPL-3.0-or-later
"""Entry point the app starts: python -I btd.py --dir DIR [--stack simulated] ...

`-I` (isolated mode) keeps the user's environment and site folders out of the helper, so this
script adds its own folder to the import path and nothing else.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from gbp_btd.__main__ import main

raise SystemExit(main())

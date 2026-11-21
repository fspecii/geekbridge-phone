# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage 3b: a real call with sound. The Mac answers, the caller is heard in the Mac's speakers,
and the Mac's microphone is heard by the caller.

Run by launcher.c with system Bluetooth switched off. One attempt, no loops. Same flow and
safety rules as stage 3a (see stage3a_call.py); this adds the sound devices. Use wired
headphones: Bluetooth is off, and laptop speakers would feed the call back into the microphone.
"""
import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audio_io
import stage3a_call
from gbp_acipc import emit


def main(argv=None):
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--listen", type=int, default=75, help="seconds to wait for the call")
    parser.add_argument("--hold", type=int, default=30, help="seconds to keep the call before hanging up")
    parser.add_argument("--input-device", default=None, help="microphone name or number (default: system default)")
    parser.add_argument("--output-device", default=None, help="speaker or headphone name or number (default: system default)")
    parser.add_argument("--allow-speakers", action="store_true", help="use the built-in speakers (feedback is likely)")
    parser.add_argument("--keys-path", default=None)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", args.target):
        emit("stage3b-failed", reason="target is not a Bluetooth address")
        return 2
    if not 5 <= args.listen <= 90 or not 10 <= args.hold <= 60:
        emit("stage3b-failed", reason="listen must be 5-90 and hold 10-60 seconds")
        return 2

    def number(value):
        return int(value) if value is not None and re.fullmatch(r"\d+", value) else value

    def audio_factory():
        import sounddevice as sd
        return audio_io.open_streams(sd, number(args.input_device), number(args.output_device),
                                     allow_speakers=args.allow_speakers,
                                     half_duplex=args.allow_speakers)

    limit = args.listen + args.hold + 70
    try:
        asyncio.run(asyncio.wait_for(
            stage3a_call.run(args.target, args.listen, args.hold, args.keys_path,
                             audio_factory=audio_factory, label="stage3b"), limit))
    except BaseException:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

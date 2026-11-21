# SPDX-License-Identifier: GPL-3.0-or-later
"""python -I -m gbp_btd --dir DIR [--keys-path FILE] [--grace SECONDS]

Runs the helper until the app asks it to stop, or has been gone for the grace period, or a
signal arrives. The app (or the test script) must already have switched system Bluetooth off.
"""
import argparse
import asyncio
import logging
import signal
import sys

from .app import Helper


def main(argv=None):
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(prog="gbp_btd")
    parser.add_argument("--dir", required=True, help="private folder for the sockets (mode 700)")
    parser.add_argument("--keys-path", default=None, help="pairing key file (default: the app's private folder)")
    parser.add_argument("--stack", choices=("bumble", "simulated"), default="bumble",
                        help="simulated: a pretend phone, no Bluetooth (for development)")
    parser.add_argument("--name", default="Mac", help="the name phones see for this Mac")
    parser.add_argument("--library", default=None, help="path to libacipc_bridge.dylib")
    parser.add_argument("--grace", type=int, default=15, help="seconds to wait for the app to come back")
    parser.add_argument("--idle", type=int, default=60, help="seconds to wait for the app to connect at all")
    args = parser.parse_args(argv)

    async def run():
        if args.stack == "simulated":
            from .simulated import SimulatedStack
            stack = SimulatedStack()
        else:
            from .bumble_stack import BumbleStack  # imported late: needs the native bridge and Bumble
            stack = BumbleStack(keys_path=args.keys_path, library=args.library, name=args.name)
        helper = Helper(args.dir, stack, grace=args.grace, idle=args.idle,
                        log=lambda *a: print(*a, file=sys.stderr, flush=True))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            loop.add_signal_handler(sig, helper.stop_event.set)
        await helper.run()

    try:
        asyncio.run(run())
    except Exception as error:
        print(f"helper failed: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

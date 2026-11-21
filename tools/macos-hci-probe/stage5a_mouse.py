# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage 5a: serve the user's own Bluetooth mouse from our stack, with the key macOS holds.

Run by launcher.c with system Bluetooth switched off. One attempt, no loops, no pairing.
The question it answers: with the same controller address and the pairing key imported from
macOS (keys_import.py), does the mouse reconnect to our stack as if it were macOS, and do its
reports arrive? With --inject the reports also move the Mac's pointer.

Move or click the mouse after the Ping: a sleeping mouse reconnects by itself when touched.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bumble import hci, pairing
from bumble.device import Device, DeviceConfiguration
from bumble.hid import Host as HidHost, Message

from gbp_acipc import NoFirmwareHost, emit
from gbp_companions import CompanionKeyStore, CompanionTransport, MagicMouse

RUN_SECONDS = 45
CONNECT_WAIT = 4  # a mouse in use calls at once; otherwise we call it (the live run waited 25 s for nothing)
INPUT_HEADER = 0xA1  # DATA | input report, first byte of every interrupt-channel message


class RefusePairing(pairing.PairingDelegate):
    """A missing or wrong key is a failure here, never a new pairing: that would replace macOS's key."""

    def __init__(self):
        super().__init__(io_capability=self.NO_OUTPUT_NO_INPUT)

    async def confirm(self, auto=False):
        return False

    async def compare_numbers(self, number, digits):
        return False


class MouseSession:
    """Counts and decodes the mouse's reports. Takes its parts as arguments so it runs in tests."""

    def __init__(self, pointer=None, say=emit):
        self.mouse = MagicMouse()
        self.pointer, self.say = pointer, say
        self.reports = self.decoded = self.moves = self.clicks = self.scrolls = self.other = 0
        self.first = asyncio.Event()
        self.kinds = {}  # "report id/length" -> count: says what an unknown mouse sends, without its content

    def on_interrupt(self, pdu):
        pdu = bytes(pdu)
        if len(pdu) < 2 or pdu[0] != INPUT_HEADER:
            self.other += 1
            return
        self.reports += 1
        kind = f"0x{pdu[1]:02x}/{len(pdu) - 1}"
        self.kinds[kind] = self.kinds.get(kind, 0) + 1
        self.first.set()
        report = MagicMouse.decode(pdu[1:])
        if report is None:
            self.other += 1
            return
        self.decoded += 1
        lines = self.mouse.scroll_lines(report)
        self.moves += bool(report.dx or report.dy)
        self.clicks += bool(report.left or report.right)
        self.scrolls += bool(lines)
        if self.pointer:
            self.pointer.apply(report, lines)

    def summary(self):
        return {"reports": self.reports, "decoded": self.decoded, "with_movement": self.moves,
                "with_button": self.clicks, "with_scroll": self.scrolls, "not_understood": self.other,
                "pointer_events": self.pointer.posted if self.pointer else None, "report_kinds": self.kinds}


async def cue():
    for command in (("/usr/bin/say", "Ready. Move the mouse now."), ("/usr/bin/afplay", "/System/Library/Sounds/Ping.aiff")):
        try:
            await asyncio.create_subprocess_exec(*command)
            return
        except Exception:
            continue


def build_device(transport):
    host = NoFirmwareHost(controller_source=transport, controller_sink=transport)
    config = DeviceConfiguration(
        name="Mac", le_enabled=False, classic_enabled=True, classic_smp_enabled=False,
        classic_accept_any=True, connectable=True, discoverable=False, keystore=None)
    device = Device(config=config, host=host)
    device.command_timeout = 3
    device.pairing_config_factory = lambda connection: pairing.PairingConfig(
        sc=True, mitm=False, bonding=True, delegate=RefusePairing())
    return device


async def serve(device, transport, target, session, keys_path=None, run_seconds=RUN_SECONDS,
                connect_wait=CONNECT_WAIT, ready=cue):
    """The whole test on a started transport: key check, link, channels, multitouch, count. Returns the summary."""
    linked = asyncio.Event()
    await transport.guarded(device.power_on(), 15)
    device.keystore = CompanionKeyStore(device.public_address, companions=[target], path=keys_path)
    if not await device.keystore.get(str(target)):
        raise RuntimeError("no imported key for the mouse: run keys_import.py first")
    hid = HidHost(device)
    hid.on(hid.EVENT_INTERRUPT_DATA, session.on_interrupt)
    device.on(device.EVENT_CONNECTION, lambda connection: linked.set())
    emit("waiting-for-mouse")
    await ready()
    try:
        await transport.guarded(linked.wait(), connect_wait)
        emit("mouse-connected", by="mouse")
    except TimeoutError:
        emit("mouse-did-not-call", action="calling the mouse")
        connection = await transport.guarded(device.connect_classic(target, timeout=8), 10)
        await transport.guarded(connection.authenticate(), 10)
        await transport.guarded(connection.encrypt(), 5)
        await transport.guarded(hid.connect_control_channel(), 5)
        await transport.guarded(hid.connect_interrupt_channel(), 5)
        emit("mouse-connected", by="us")
    for _ in range(50):  # the mouse opens its two channels right after the link
        if hid.l2cap_ctrl_channel and hid.l2cap_intr_channel:
            break
        await asyncio.sleep(0.1)
    if not hid.l2cap_ctrl_channel:
        raise RuntimeError("the mouse linked but opened no control channel")
    answers = []
    hid.on(hid.EVENT_HANDSHAKE, lambda code: answers.append(int(code)))
    # Asked more than once, as the Linux driver does: a mouse that has just linked may not be ready.
    # The first live run got 8-byte reports only, so the single request had not taken effect.
    for attempt in range(3):
        hid.set_report(Message.ReportType.FEATURE_REPORT, MagicMouse.ENABLE_MULTITOUCH)
        await asyncio.sleep(1)
    emit("multitouch-requested", times=3, answers=answers)
    await transport.guarded(asyncio.sleep(run_seconds), run_seconds + 2)
    if device.keystore.failure:
        raise device.keystore.failure
    return session.summary()


async def run(target_text, inject, keys_path=None):
    target = hci.Address(target_text, hci.Address.PUBLIC_DEVICE_ADDRESS)
    transport = CompanionTransport(companions=[target])
    device = build_device(transport)
    pointer = None
    if inject:
        from pointer import Pointer
        pointer = Pointer()
        if not pointer.trusted(ask=True):
            emit("pointer-not-allowed", hint="allow GBBtLauncher under Privacy & Security > Accessibility")
            pointer = None
    session = MouseSession(pointer)
    await transport.start()
    try:
        result = await serve(device, transport, target, session, keys_path)
        emit("mouse-result", passed=result["decoded"] > 0, **result)
        return 0 if result["decoded"] > 0 else 12
    finally:
        try:
            for connection in list(device.connections.values()):
                await asyncio.wait_for(connection.disconnect(), 3)
        except Exception as error:
            emit("disconnect-problem", error=type(error).__name__)
        await transport.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--inject", action="store_true")
    args = parser.parse_args()
    try:
        code = asyncio.run(run(args.target, args.inject))
    except Exception as error:
        emit("failed", error=type(error).__name__, detail=str(error))
        code = 11
    sys.exit(code)


if __name__ == "__main__":
    main()

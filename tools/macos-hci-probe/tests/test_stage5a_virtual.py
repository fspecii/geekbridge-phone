# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage 5a against a simulated mouse on Bumble's virtual controllers, through the companion filter.

No radio. The pretend mouse calls us the way a mouse that was touched calls its host, opens its
two HID channels and sends Magic Mouse reports. The virtual controller always starts a new
pairing when asked to authenticate, so the stored-key handshake itself is not covered here:
that is what the live run is for. The key store's own rules are in test_companions.py.
"""
import asyncio
import logging
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bumble import hci
from bumble.controller import Controller
from bumble.device import Device, DeviceConfiguration
from bumble.hid import Device as HidDevice
from bumble.host import Host
from bumble.keys import PairingKeys
from bumble.link import LocalLink

import keys_import
import stage5a_mouse
from gbp_acipc import ScopeError
from gbp_companions import CompanionScope
from test_companions import mouse_report

logging.disable(logging.CRITICAL)

OURS = "F0:F1:F2:F3:F4:F5"
MOUSE = "AA:BB:CC:00:00:02"
STRANGER = "11:22:33:44:55:66"
KEY = bytes(range(16, 32))


class VirtualCompanionTransport:
    """CompanionTransport's behaviour on a virtual controller: same scope, same refusal of strangers."""

    def __init__(self, controller, companions):
        self.controller = controller
        self.filter = CompanionScope(None, companions)
        self.failure = None
        self.refused = 0

    def set_packet_sink(self, sink):
        transport = self

        class Checked:
            def on_packet(self, packet):
                try:
                    refusal = transport.filter.reject_for(packet)
                    if refusal is not None:
                        transport.refused += 1
                        transport.on_packet(refusal)
                        return
                    transport.filter.check_inbound(packet)
                except ScopeError as error:
                    transport.failure = error
                    raise
                sink.on_packet(packet)

        self.controller.set_packet_sink(Checked())

    def on_packet(self, packet):
        try:
            self.filter.check_outbound(packet)
        except ScopeError as error:
            self.failure = error
            raise
        self.controller.on_packet(packet)

    async def guarded(self, awaitable, timeout):
        task = asyncio.ensure_future(awaitable)
        try:
            end = asyncio.get_running_loop().time() + timeout
            while not task.done():
                if self.failure:
                    raise self.failure
                if asyncio.get_running_loop().time() >= end:
                    raise TimeoutError("operation exceeded its time limit")
                await asyncio.sleep(0.01)
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


class OneKey:
    """The pretend device's key store: one key, for our address."""

    def __init__(self, key):
        self.key = key
        self.updated = False

    async def get(self, name):
        return PairingKeys(link_key=PairingKeys.Key(self.key, False), link_key_type=4) if self.key else None

    async def update(self, name, keys):
        self.updated = True

    async def delete(self, name):
        pass

    async def get_all(self):
        return []

    async def get_resolving_keys(self):
        return []


async def peer(link, address, key):
    controller = Controller("peer", link=link, public_address=hci.Address(address, hci.Address.PUBLIC_DEVICE_ADDRESS))
    host = Host(controller_source=controller, controller_sink=controller)
    device = Device(config=DeviceConfiguration(name="Mouse", le_enabled=False, classic_enabled=True,
                                               classic_smp_enabled=False, keystore=None), host=host)
    await device.power_on()
    device.keystore = OneKey(key)
    return device


async def quiet():
    pass


class Stage5aVirtual(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.keys = Path(self.dir.name) / "keys.json"
        self.link = LocalLink()
        self.controller = Controller("ours", link=self.link,
                                     public_address=hci.Address(OURS, hci.Address.PUBLIC_DEVICE_ADDRESS))
        self.target = hci.Address(MOUSE, hci.Address.PUBLIC_DEVICE_ADDRESS)
        self.transport = VirtualCompanionTransport(self.controller, [self.target])
        self.device = stage5a_mouse.build_device(self.transport)
        self.session = stage5a_mouse.MouseSession(say=lambda *a, **k: None)
        stage5a_mouse.emit = lambda *a, **k: None

    async def asyncTearDown(self):
        self.dir.cleanup()

    async def test_the_mouse_calls_us_and_its_reports_arrive(self):
        keys_import.store(OURS, MOUSE, KEY, 4, self.keys)
        mouse = await peer(self.link, MOUSE, KEY)
        hid = HidDevice(mouse)
        features = []
        hid.register_set_report_cb(lambda report_id, report_type, size, data: (
            features.append((report_type, bytes([report_id]) + bytes(data))), hid.GetSetStatus(status=hid.GetSetReturn.SUCCESS))[1])

        async def mouse_side():
            await asyncio.sleep(0.2)
            connection = await mouse.connect_classic(hci.Address(OURS, hci.Address.PUBLIC_DEVICE_ADDRESS))
            await hid.connect_control_channel()
            await hid.connect_interrupt_channel()
            await asyncio.sleep(0.8)
            for report in (mouse_report(dx=7, dy=-2), mouse_report(buttons=1), mouse_report(dx=1)):
                hid.send_data(report)
            return connection

        side = asyncio.ensure_future(mouse_side())
        summary = await stage5a_mouse.serve(self.device, self.transport, self.target, self.session,
                                            keys_path=self.keys, run_seconds=2, connect_wait=5, ready=quiet)
        connection = await side
        self.assertEqual((summary["decoded"], summary["with_movement"], summary["with_button"]), (3, 2, 1))
        self.assertEqual(features, [(3, stage5a_mouse.MagicMouse.ENABLE_MULTITOUCH)] * 3)
        self.assertFalse(mouse.keystore.updated)
        self.assertIsNone(self.transport.failure)

    async def test_without_an_imported_key_nothing_is_attempted(self):
        with self.assertRaises(RuntimeError):
            await stage5a_mouse.serve(self.device, self.transport, self.target, self.session,
                                      keys_path=self.keys, run_seconds=1, connect_wait=1, ready=quiet)

    async def test_a_stranger_calling_is_refused_and_never_linked(self):
        keys_import.store(OURS, MOUSE, KEY, 4, self.keys)
        stranger = await peer(self.link, STRANGER, None)

        async def stranger_side():
            await asyncio.sleep(0.2)
            try:
                await stranger.connect_classic(hci.Address(OURS, hci.Address.PUBLIC_DEVICE_ADDRESS), timeout=2)
                return "linked"
            except Exception as error:
                return type(error).__name__

        side = asyncio.ensure_future(stranger_side())
        with self.assertRaises(Exception):
            await stage5a_mouse.serve(self.device, self.transport, self.target, self.session,
                                      keys_path=self.keys, run_seconds=1, connect_wait=1.5, ready=quiet)
        self.assertNotEqual(await side, "linked")
        self.assertEqual(self.transport.refused, 1)
        self.assertEqual(self.transport.filter.links, {})


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline tests for stage 5: the multi-device filter, imported keys, the mouse decoder. No Bluetooth."""
import asyncio
import json
import plistlib
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bumble import hci
from bumble.keys import PairingKeys

import keys_import
import pointer
from gbp_acipc import ScopeError
from gbp_companions import CompanionKeyStore, CompanionScope, CompanionTransport, MagicMouse, MouseReport
from stage5a_mouse import MouseSession

PHONE = hci.Address("6C:AC:C2:00:00:01", hci.Address.PUBLIC_DEVICE_ADDRESS)
MOUSE = hci.Address("AA:BB:CC:00:00:02", hci.Address.PUBLIC_DEVICE_ADDRESS)
BUDS = hci.Address("AA:BB:CC:00:00:03", hci.Address.PUBLIC_DEVICE_ADDRESS)
STRANGER = hci.Address("11:22:33:44:55:66", hci.Address.PUBLIC_DEVICE_ADDRESS)
MAC = hci.Address("9C:58:84:00:00:02", hci.Address.PUBLIC_DEVICE_ADDRESS)
KEY = bytes(range(16))


def command(opcode, params=b""):
    return bytes([1]) + opcode.to_bytes(2, "little") + bytes([len(params)]) + params


def event(code, params=b""):
    return bytes([4, code, len(params)]) + params


def connected(handle, address, link_type=1, status=0):
    return event(hci.HCI_CONNECTION_COMPLETE_EVENT,
                 bytes([status]) + handle.to_bytes(2, "little") + bytes(address) + bytes([link_type, 0]))


def request(address, link_type=1):
    return event(hci.HCI_CONNECTION_REQUEST_EVENT, bytes(address) + b"\x80\x25\x00" + bytes([link_type]))


def voice_open(handle, address):
    return event(hci.HCI_SYNCHRONOUS_CONNECTION_COMPLETE_EVENT,
                 bytes([0]) + handle.to_bytes(2, "little") + bytes(address) + bytes([2]) + bytes(7))


def acl(handle, payload=b"\x01\x00\x01\x00\x00"):
    return bytes([2]) + handle.to_bytes(2, "little") + len(payload).to_bytes(2, "little") + payload


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.f = CompanionScope(PHONE, [MOUSE, BUDS])

    def test_the_one_phone_rules_still_hold(self):
        self.f.check_outbound(command(0x1001))
        for refused in (command(0xFC01), command(hci.HCI_WRITE_STORED_LINK_KEY_COMMAND),
                        command(hci.HCI_INQUIRY_COMMAND, b"\x33\x8b\x9e\x04\x00"),
                        command(hci.HCI_LE_ENABLE_ENCRYPTION_COMMAND)):
            with self.assertRaises(ScopeError):
                self.f.check_outbound(refused)

    def test_links_may_open_to_the_phone_and_each_companion_only(self):
        for address in (PHONE, MOUSE, BUDS):
            self.f.check_outbound(command(hci.HCI_CREATE_CONNECTION_COMMAND, bytes(address) + bytes(7)))
            self.f.check_outbound(command(hci.HCI_LINK_KEY_REQUEST_REPLY_COMMAND, bytes(address) + KEY))
        with self.assertRaises(ScopeError):
            self.f.check_outbound(command(hci.HCI_CREATE_CONNECTION_COMMAND, bytes(STRANGER) + bytes(7)))
        with self.assertRaises(ScopeError):
            self.f.check_outbound(command(hci.HCI_LINK_KEY_REQUEST_REPLY_COMMAND, bytes(STRANGER) + KEY))

    def test_data_flows_only_on_links_the_controller_reported_for_our_devices(self):
        with self.assertRaises(ScopeError):
            self.f.check_outbound(acl(0x0B))
        self.f.check_inbound(connected(0x0B, MOUSE))
        self.f.check_inbound(connected(0x0C, PHONE))
        self.f.check_outbound(acl(0x0B))
        self.f.check_outbound(acl(0x0C))
        with self.assertRaises(ScopeError):
            self.f.check_outbound(acl(0x0D))
        self.assertEqual(self.f.handle_of(MOUSE), 0x0B)

    def test_a_link_to_a_stranger_stops_the_run(self):
        with self.assertRaises(ScopeError):
            self.f.check_inbound(connected(0x0B, STRANGER))

    def test_a_failed_link_is_not_remembered(self):
        self.f.check_inbound(connected(0x0B, MOUSE, status=4))
        with self.assertRaises(ScopeError):
            self.f.check_outbound(acl(0x0B))

    def test_a_closed_link_stops_carrying_data(self):
        self.f.check_inbound(connected(0x0B, MOUSE))
        self.f.check_inbound(event(hci.HCI_DISCONNECTION_COMPLETE_EVENT, bytes([0, 0x0B, 0, 0x13])))
        with self.assertRaises(ScopeError):
            self.f.check_outbound(acl(0x0B))

    def test_link_commands_need_one_of_our_links(self):
        self.f.check_inbound(connected(0x0B, MOUSE))
        self.f.check_outbound(command(hci.HCI_AUTHENTICATION_REQUESTED_COMMAND, (0x0B).to_bytes(2, "little")))
        with self.assertRaises(ScopeError):
            self.f.check_outbound(command(hci.HCI_AUTHENTICATION_REQUESTED_COMMAND, (0x0E).to_bytes(2, "little")))

    def test_voice_is_only_ever_with_the_phone(self):
        self.f.check_inbound(connected(0x0B, BUDS))
        self.f.check_inbound(connected(0x0C, PHONE))
        accept = hci.HCI_ENHANCED_ACCEPT_SYNCHRONOUS_CONNECTION_REQUEST_COMMAND
        self.f.check_outbound(command(accept, bytes(PHONE) + bytes(50)))
        with self.assertRaises(ScopeError):
            self.f.check_outbound(command(accept, bytes(BUDS) + bytes(50)))
        setup = hci.HCI_SETUP_SYNCHRONOUS_CONNECTION_COMMAND
        self.f.check_outbound(command(setup, (0x0C).to_bytes(2, "little") + bytes(15)))
        with self.assertRaises(ScopeError):
            self.f.check_outbound(command(setup, (0x0B).to_bytes(2, "little") + bytes(15)))
        with self.assertRaises(ScopeError):
            self.f.check_inbound(voice_open(0x20, BUDS))

    def test_one_voice_link_and_voice_data_only_on_it(self):
        self.f.check_inbound(connected(0x0C, PHONE))
        with self.assertRaises(ScopeError):
            self.f.check_outbound(bytes([3, 0x20, 0, 2, 0, 0]))
        self.f.check_inbound(voice_open(0x20, PHONE))
        self.f.check_outbound(bytes([3, 0x20, 0, 2, 0, 0]))
        with self.assertRaises(ScopeError):
            self.f.check_inbound(voice_open(0x21, PHONE))
        self.f.check_outbound(command(hci.HCI_DISCONNECT_COMMAND, (0x20).to_bytes(2, "little") + b"\x13"))
        self.f.check_inbound(event(hci.HCI_DISCONNECTION_COMPLETE_EVENT, bytes([0, 0x20, 0, 0x16])))
        self.f.check_inbound(voice_open(0x21, PHONE))

    def test_a_plain_sco_link_counts_as_the_voice_link(self):
        self.f.check_inbound(connected(0x0C, PHONE))
        self.f.check_inbound(connected(0x20, PHONE, link_type=0))
        self.f.check_outbound(bytes([3, 0x20, 0, 1, 0]))
        with self.assertRaises(ScopeError):
            self.f.check_inbound(connected(0x21, BUDS, link_type=0))

    def test_a_stranger_is_refused_and_our_devices_are_not(self):
        refusal = self.f.reject_for(request(STRANGER))
        self.assertEqual(refusal[:4], command(hci.HCI_REJECT_CONNECTION_REQUEST_COMMAND, bytes(7))[:4])
        self.assertEqual(refusal[4:10], bytes(STRANGER))
        self.f.check_outbound(command(hci.HCI_ACCEPT_CONNECTION_REQUEST_COMMAND, bytes(MOUSE) + b"\x01"))
        for address in (PHONE, MOUSE, BUDS):
            self.assertIsNone(self.f.reject_for(request(address)))

    def test_a_voice_request_from_headphones_is_refused(self):
        self.assertIsNone(self.f.reject_for(request(PHONE, link_type=2)))
        refusal = self.f.reject_for(request(BUDS, link_type=2))
        self.assertEqual(int.from_bytes(refusal[1:3], "little"), hci.HCI_REJECT_SYNCHRONOUS_CONNECTION_REQUEST_COMMAND)
        self.f.check_inbound(connected(0x0C, PHONE))
        self.f.check_inbound(voice_open(0x20, PHONE))
        self.assertIsNotNone(self.f.reject_for(request(PHONE, link_type=2)))

    def test_pairing_events_for_a_stranger_stop_the_run(self):
        self.f.check_inbound(event(hci.HCI_LINK_KEY_REQUEST_EVENT, bytes(MOUSE)))
        with self.assertRaises(ScopeError):
            self.f.check_inbound(event(hci.HCI_LINK_KEY_REQUEST_EVENT, bytes(STRANGER)))
        with self.assertRaises(ScopeError):
            self.f.check_inbound(event(hci.HCI_PIN_CODE_REQUEST_EVENT, bytes(MOUSE)))

    def test_a_session_key_refresh_on_our_link_is_normal(self):
        refresh = hci.HCI_ENCRYPTION_KEY_REFRESH_COMPLETE_EVENT
        with self.assertRaises(ScopeError):
            self.f.check_inbound(event(refresh, bytes([0, 0x0B, 0])))
        self.f.check_inbound(connected(0x0B, MOUSE))
        self.f.check_inbound(event(refresh, bytes([0, 0x0B, 0])))
        with self.assertRaises(ScopeError):
            self.f.check_inbound(event(refresh, bytes([0, 0x0E, 0])))

    def test_no_phone_means_no_voice_at_all(self):
        scope = CompanionScope(None, [MOUSE])
        self.assertIsNotNone(scope.reject_for(request(MOUSE, link_type=2)))
        with self.assertRaises(ScopeError):
            scope.check_inbound(voice_open(0x20, MOUSE))


class TransportTests(unittest.TestCase):
    def test_a_stranger_never_reaches_the_stack(self):
        sent, seen = [], []

        class Probe(CompanionTransport):
            def on_packet(self, packet):
                self.filter.check_outbound(packet)
                sent.append(bytes(packet))

        class Sink:
            def on_packet(self, packet):
                seen.append(bytes(packet))

        transport = Probe(companions=[MOUSE])
        transport.set_packet_sink(Sink())
        self.assertFalse(transport.deliver(request(STRANGER)))
        self.assertTrue(transport.deliver(request(MOUSE)))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][4:10], bytes(STRANGER))
        self.assertEqual(seen, [request(MOUSE)])


class KeyStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "keys.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_an_imported_key_is_found_by_the_stacks_own_name(self):
        keys_import.store(str(MAC).split("/")[0], str(MOUSE).split("/")[0], KEY, 4, self.path)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        store = CompanionKeyStore(MAC, companions=[MOUSE], path=self.path)
        keys = asyncio.run(store.get(str(MOUSE)))
        self.assertEqual(keys.link_key.value, KEY)
        self.assertFalse(keys.link_key.authenticated)
        self.assertIsNone(asyncio.run(store.get(str(STRANGER))))
        self.assertEqual([name for name, _ in asyncio.run(store.get_all())], [str(MOUSE)])

    def test_a_companion_can_never_get_a_new_key(self):
        keys_import.store(str(MAC).split("/")[0], str(MOUSE).split("/")[0], KEY, 4, self.path)
        before = self.path.read_text()
        store = CompanionKeyStore(MAC, companions=[MOUSE], path=self.path)
        fresh = PairingKeys(link_key=PairingKeys.Key(bytes(16), True), link_key_type=8)
        with self.assertRaises(RuntimeError):
            asyncio.run(store.update(str(MOUSE), fresh))
        asyncio.run(store.delete(str(MOUSE)))
        asyncio.run(store.delete_all())
        self.assertEqual(self.path.read_text(), before)
        self.assertIsNotNone(store.failure)

    def test_a_damaged_import_is_an_error(self):
        self.path.write_text(json.dumps({f"{MAC}/{MOUSE}": {"type": 99, "key": KEY.hex()}}))
        store = CompanionKeyStore(MAC, companions=[MOUSE], path=self.path)
        with self.assertRaises(RuntimeError):
            asyncio.run(store.get(str(MOUSE)))

    def test_the_phone_keeps_its_own_rules(self):
        store = CompanionKeyStore(MAC, phone=PHONE, companions=[MOUSE], path=self.path)
        asyncio.run(store.update(str(PHONE), PairingKeys(link_key=PairingKeys.Key(KEY, True), link_key_type=8)))
        self.assertEqual(asyncio.run(store.get(str(PHONE))).link_key.value, KEY)


class KeyImportTests(unittest.TestCase):
    def test_shapes_the_keychain_item_may_have(self):
        self.assertEqual(keys_import.parse_secret(KEY)[0], KEY)
        self.assertEqual(keys_import.parse_secret(KEY.hex().encode() + b"\n")[0], KEY)
        binary = plistlib.dumps({"LinkKey": KEY, "LinkKeyType": 5, "Name": "Mouse"}, fmt=plistlib.FMT_BINARY)
        self.assertEqual(keys_import.parse_secret(binary)[:2], (KEY, 5))
        self.assertEqual(keys_import.parse_secret(binary.hex().encode())[:2], (KEY, 5))
        xml = plistlib.dumps({"device": {"Key": KEY}})
        self.assertEqual(keys_import.parse_secret(xml)[:2], (KEY, keys_import.DEFAULT_TYPE))

    def test_the_shape_macos_27_writes(self):
        text = "-".join(f"{byte:02X}" for byte in KEY)
        for word, number in (("Authenticated", 5), ("UnAuthenticated", 4)):
            item = plistlib.dumps({"LinkKey": text, "LinkKeyType": word, "LocalAddress": "9c-58-84-00-00-02"})
            key, kind, shape = keys_import.parse_secret(item)
            self.assertEqual((key, kind), (KEY, number))
            self.assertNotIn(text, shape)

    def test_the_shape_report_never_holds_the_key(self):
        binary = plistlib.dumps({"LinkKey": KEY, "Other": b"x" * 16}, fmt=plistlib.FMT_BINARY)
        _, _, shape = keys_import.parse_secret(binary)
        self.assertNotIn(KEY.hex(), shape)
        self.assertNotIn(repr(KEY), shape)

    def test_a_device_is_found_by_part_of_its_name(self):
        listing = ("      Connected:\n          Eu's Magic Mouse:\n              Address: aa:bb:cc:00:00:02\n"
                   "          Some AirPods:\n              Address: AA:BB:CC:00:00:03\n"
                   "      Not Connected:\n          Other AirPods:\n              Address: AA:BB:CC:00:00:04\n")
        self.assertEqual(keys_import.find_device("magic mouse", listing), "AA:BB:CC:00:00:02")
        with self.assertRaises(keys_import.ImportError_):
            keys_import.find_device("airpods", listing)
        with self.assertRaises(keys_import.ImportError_):
            keys_import.find_device("keyboard", listing)

    def test_an_item_without_one_clear_key_is_refused(self):
        with self.assertRaises(keys_import.ImportError_):
            keys_import.parse_secret(plistlib.dumps({"A": KEY, "B": KEY[::-1]}))
        with self.assertRaises(keys_import.ImportError_):
            keys_import.parse_secret(b"not a key at all, just text")


def mouse_report(dx=0, dy=0, buttons=0, touches=()):
    data = bytearray([0x12, buttons]) + dx.to_bytes(2, "little", signed=True) + dy.to_bytes(2, "little", signed=True)
    if not touches:
        return bytes(data + bytes(2))
    data += bytes(8)
    for identifier, x, y, down in touches:
        raw_y = (-y) & 0xFFF
        data += bytes([x & 0xFF, ((x >> 8) & 0x0F) | ((raw_y & 0x0F) << 4), raw_y >> 4, 0, 0,
                       (identifier & 3) << 6, identifier >> 2, 0x40 if down else 0])
    return bytes(data)


class MagicMouseTests(unittest.TestCase):
    def test_movement_and_buttons(self):
        report = MagicMouse.decode(mouse_report(dx=5, dy=-300, buttons=1))
        self.assertEqual((report.dx, report.dy, report.left, report.right), (5, -300, True, False))
        self.assertTrue(MagicMouse.decode(mouse_report(buttons=2)).right)

    def test_touches(self):
        report = MagicMouse.decode(mouse_report(touches=[(3, -700, 250, True), (9, 12, -40, False)]))
        self.assertEqual([(t.id, t.x, t.y, t.down) for t in report.touches],
                         [(3, -700, 250, True), (9, 12, -40, False)])

    def test_other_reports_and_bad_sizes_are_not_ours(self):
        self.assertIsNone(MagicMouse.decode(b""))
        self.assertIsNone(MagicMouse.decode(bytes([0x29]) + bytes(7)))
        self.assertIsNone(MagicMouse.decode(bytes([0x12]) + bytes(10)))
        self.assertIsNone(MagicMouse.decode(bytes([0x12]) + bytes(16)))

    def test_one_finger_sliding_scrolls_and_two_do_not(self):
        mouse = MagicMouse()
        lines = [mouse.scroll_lines(MagicMouse.decode(mouse_report(touches=[(1, 0, y, True)])))
                 for y in (0, 40, 80, 200, 200)]
        self.assertEqual(lines, [0, 0, 1, 2, 0])
        mouse = MagicMouse()
        two = [mouse.scroll_lines(MagicMouse.decode(mouse_report(touches=[(1, 0, y, True), (2, 50, y, True)])))
               for y in (0, 500)]
        self.assertEqual(two, [0, 0])

    def test_lifting_the_finger_forgets_where_it_was(self):
        mouse = MagicMouse()
        mouse.scroll_lines(MagicMouse.decode(mouse_report(touches=[(1, 0, 0, True)])))
        mouse.scroll_lines(MagicMouse.decode(mouse_report()))
        self.assertEqual(mouse.scroll_lines(MagicMouse.decode(mouse_report(touches=[(1, 0, 900, True)]))), 0)


class PointerPlanTests(unittest.TestCase):
    def test_moves_presses_releases_and_drags(self):
        self.assertEqual(pointer.plan((False, False), MouseReport(dx=3)), [(pointer.MOVED, 0)])
        self.assertEqual(pointer.plan((False, False), MouseReport(left=True)), [(pointer.LEFT_DOWN, 0)])
        self.assertEqual(pointer.plan((True, False), MouseReport(dx=1, left=True)), [(pointer.LEFT_DRAGGED, 0)])
        self.assertEqual(pointer.plan((True, False), MouseReport()), [(pointer.LEFT_UP, 0)])
        self.assertEqual(pointer.plan((False, True), MouseReport(dy=2, right=True)), [(pointer.RIGHT_DRAGGED, 1)])
        self.assertEqual(pointer.plan((False, False), MouseReport()), [])


class MouseSessionTests(unittest.TestCase):
    def test_counts_what_arrives_and_drives_the_pointer(self):
        applied = []

        class FakePointer:
            posted = 0

            def apply(self, report, lines):
                applied.append((report.dx, report.dy, lines))

        async def scenario():
            session = MouseSession(FakePointer(), say=lambda *a, **k: None)
            session.on_interrupt(bytes([0xA1]) + mouse_report(dx=4, dy=1))
            session.on_interrupt(bytes([0xA1]) + mouse_report(buttons=1))
            session.on_interrupt(bytes([0xA1, 0x90, 0, 0]))
            session.on_interrupt(b"\x00")
            return session

        session = asyncio.run(scenario())
        summary = session.summary()
        self.assertEqual((summary["reports"], summary["decoded"], summary["with_movement"],
                          summary["with_button"], summary["not_understood"]), (3, 2, 1, 1, 2))
        self.assertEqual(applied, [(4, 1, 0), (0, 0, 0)])
        self.assertTrue(session.first.is_set())


if __name__ == "__main__":
    unittest.main()

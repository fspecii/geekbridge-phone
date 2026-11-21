# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline tests: the safety filter and the key store. No Bluetooth, no hardware."""
import asyncio
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bumble import hci
from bumble.keys import PairingKeys

from gbp_acipc import ScopeError, ScopeFilter, TargetKeyStore

PHONE = hci.Address("6C:AC:C2:00:00:01", hci.Address.PUBLIC_DEVICE_ADDRESS)
OTHER = hci.Address("11:22:33:44:55:66", hci.Address.PUBLIC_DEVICE_ADDRESS)


def command(opcode, params=b""):
    return bytes([1]) + opcode.to_bytes(2, "little") + bytes([len(params)]) + params


def event(code, params=b""):
    return bytes([4, code, len(params)]) + params


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.f = ScopeFilter(PHONE)

    def test_plain_commands_pass(self):
        self.f.check_outbound(command(0x1001))  # read local version

    def test_vendor_commands_are_refused(self):
        with self.assertRaises(ScopeError):
            self.f.check_outbound(command(0xFC01))
        with self.assertRaises(ScopeError):
            self.f.check_outbound(command(0xFE6E, b"\0" * 5))

    def test_stored_key_commands_are_refused(self):
        for op in (hci.HCI_READ_STORED_LINK_KEY_COMMAND, hci.HCI_WRITE_STORED_LINK_KEY_COMMAND,
                   hci.HCI_DELETE_STORED_LINK_KEY_COMMAND):
            with self.assertRaises(ScopeError):
                self.f.check_outbound(command(op))

    def test_inquiry_is_refused(self):
        with self.assertRaises(ScopeError):
            self.f.check_outbound(command(hci.HCI_INQUIRY_COMMAND, b"\x33\x8b\x9e\x04\x00"))

    def test_connection_only_to_the_target(self):
        self.f.check_outbound(command(hci.HCI_CREATE_CONNECTION_COMMAND, bytes(PHONE) + b"\0" * 7))
        with self.assertRaises(ScopeError):
            self.f.check_outbound(command(hci.HCI_CREATE_CONNECTION_COMMAND, bytes(OTHER) + b"\0" * 7))

    def test_pairing_replies_only_for_the_target(self):
        for op in (hci.HCI_USER_CONFIRMATION_REQUEST_REPLY_COMMAND, hci.HCI_LINK_KEY_REQUEST_NEGATIVE_REPLY_COMMAND):
            self.f.check_outbound(command(op, bytes(PHONE)))
            with self.assertRaises(ScopeError):
                self.f.check_outbound(command(op, bytes(OTHER)))

    def test_authentication_needs_the_known_link(self):
        request = command(hci.HCI_AUTHENTICATION_REQUESTED_COMMAND, (0x0041).to_bytes(2, "little"))
        with self.assertRaises(ScopeError):  # no link known yet
            self.f.check_outbound(request)
        self.f.handle = 0x0041
        self.f.check_outbound(request)
        self.f.handle = 0x0042
        with self.assertRaises(ScopeError):
            self.f.check_outbound(request)

    def test_unknown_packet_types_are_refused(self):
        with self.assertRaises(ScopeError):
            self.f.check_outbound(bytes([9, 0, 0]))

    def test_pairing_events_for_the_target_pass(self):
        self.f.handle = 0x0041
        self.f.check_inbound(event(hci.HCI_USER_CONFIRMATION_REQUEST_EVENT, bytes(PHONE) + b"\0\0\0\0"))
        self.f.check_inbound(event(hci.HCI_SIMPLE_PAIRING_COMPLETE_EVENT, b"\0" + bytes(PHONE)))
        self.f.check_inbound(event(hci.HCI_AUTHENTICATION_COMPLETE_EVENT, b"\0" + (0x0041).to_bytes(2, "little")))

    def test_pairing_events_for_other_devices_stop_the_run(self):
        self.f.handle = 0x0041
        with self.assertRaises(ScopeError):
            self.f.check_inbound(event(hci.HCI_USER_CONFIRMATION_REQUEST_EVENT, bytes(OTHER) + b"\0\0\0\0"))
        with self.assertRaises(ScopeError):
            self.f.check_inbound(event(hci.HCI_SIMPLE_PAIRING_COMPLETE_EVENT, b"\0" + bytes(OTHER)))
        with self.assertRaises(ScopeError):
            self.f.check_inbound(event(hci.HCI_AUTHENTICATION_COMPLETE_EVENT, b"\0" + (0x0099).to_bytes(2, "little")))

    def test_unexpected_pairing_events_stop_the_run(self):
        with self.assertRaises(ScopeError):
            self.f.check_inbound(event(hci.HCI_PIN_CODE_REQUEST_EVENT, bytes(PHONE)))

    def test_other_events_pass(self):
        self.f.check_inbound(event(0x0E, b"\x01\x01\x10\x00"))  # command complete


class KeyStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "keys" / "pair-keys.json"
        self.store = TargetKeyStore("9C:58:84:00:00:02", PHONE, self.path)

    def tearDown(self):
        self.dir.cleanup()

    def keys(self, authenticated=True, key_type=8, value=b"\x07" * 16):
        return PairingKeys(link_key=PairingKeys.Key(value, authenticated), link_key_type=key_type)

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_round_trip_and_private_file(self):
        self.run_async(self.store.update(str(PHONE), self.keys()))
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.path.parent).st_mode), 0o700)
        got = self.run_async(self.store.get(str(PHONE)))
        self.assertEqual(bytes(got.link_key.value), b"\x07" * 16)
        self.assertEqual(got.link_key_type, 8)

    def test_other_addresses_are_refused(self):
        with self.assertRaises(RuntimeError):
            self.run_async(self.store.update(str(OTHER), self.keys()))
        self.assertIsNone(self.run_async(self.store.get(str(OTHER))))

    def test_unverified_or_odd_keys_are_refused(self):
        with self.assertRaises(RuntimeError):
            self.run_async(self.store.update(str(PHONE), self.keys(authenticated=False)))
        with self.assertRaises(RuntimeError):
            self.run_async(self.store.update(str(PHONE), self.keys(key_type=4)))
        with self.assertRaises(RuntimeError):
            self.run_async(self.store.update(str(PHONE), self.keys(value=b"\x01" * 8)))
        self.assertFalse(self.path.exists())

    def test_delete_removes_only_our_entry(self):
        self.run_async(self.store.update(str(PHONE), self.keys()))
        other = TargetKeyStore("AA:AA:AA:AA:AA:AA", PHONE, self.path)
        self.run_async(other.update(str(PHONE), self.keys(value=b"\x09" * 16)))
        self.run_async(self.store.delete(str(PHONE)))
        self.assertIsNone(self.run_async(self.store.get(str(PHONE))))
        self.assertIsNotNone(self.run_async(other.get(str(PHONE))))

    def test_the_key_is_not_in_the_log_line(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.run_async(self.store.update(str(PHONE), self.keys()))
        self.assertNotIn("07" * 16, out.getvalue())
        self.assertIn("pairing-key-saved", out.getvalue())


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline tests: the filter rules for the voice link and for data packets."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bumble import hci

from gbp_acipc import MAX_SCO_PAYLOAD, ScopeError, ScopeFilter

PHONE = hci.Address("6C:AC:C2:00:00:01", hci.Address.PUBLIC_DEVICE_ADDRESS)
OTHER = hci.Address("11:22:33:44:55:66", hci.Address.PUBLIC_DEVICE_ADDRESS)
ACL = 0x0041
SCO = 0x0042


def command(opcode, params=b""):
    return bytes([1]) + opcode.to_bytes(2, "little") + bytes([len(params)]) + params


def event(code, params=b""):
    return bytes([4, code, len(params)]) + params


def sync_complete(address, handle=SCO, status=0):
    # status, handle(2), address(6), link type, tx interval, retransmission window, rx/tx lengths, air mode
    return event(hci.HCI_SYNCHRONOUS_CONNECTION_COMPLETE_EVENT,
                 bytes([status]) + handle.to_bytes(2, "little") + bytes(address) + bytes([2, 6, 2]) + bytes(6))


def sco_packet(handle, payload):
    return bytes([3]) + handle.to_bytes(2, "little") + bytes([len(payload)]) + payload


def acl_packet(handle, payload):
    return bytes([2]) + handle.to_bytes(2, "little") + len(payload).to_bytes(2, "little") + payload


class VoiceFilterTests(unittest.TestCase):
    def setUp(self):
        self.f = ScopeFilter(PHONE)
        self.f.handle = ACL

    def test_accepting_the_voice_link_only_for_the_target(self):
        for op in (hci.HCI_ENHANCED_ACCEPT_SYNCHRONOUS_CONNECTION_REQUEST_COMMAND,
                   hci.HCI_ACCEPT_SYNCHRONOUS_CONNECTION_REQUEST_COMMAND,
                   hci.HCI_REJECT_SYNCHRONOUS_CONNECTION_REQUEST_COMMAND):
            self.f.check_outbound(command(op, bytes(PHONE) + b"\0" * 8))
            with self.assertRaises(ScopeError):
                self.f.check_outbound(command(op, bytes(OTHER) + b"\0" * 8))

    def test_setting_up_a_voice_link_only_on_the_target_acl_link(self):
        op = hci.HCI_ENHANCED_SETUP_SYNCHRONOUS_CONNECTION_COMMAND
        self.f.check_outbound(command(op, ACL.to_bytes(2, "little") + b"\0" * 8))
        with self.assertRaises(ScopeError):
            self.f.check_outbound(command(op, (0x0099).to_bytes(2, "little") + b"\0" * 8))

    def test_disconnect_allowed_for_the_acl_link_and_the_voice_link_only(self):
        self.f.check_outbound(command(hci.HCI_DISCONNECT_COMMAND, ACL.to_bytes(2, "little") + b"\x13"))
        with self.assertRaises(ScopeError):  # no voice link yet
            self.f.check_outbound(command(hci.HCI_DISCONNECT_COMMAND, SCO.to_bytes(2, "little") + b"\x13"))
        self.f.check_inbound(sync_complete(PHONE))
        self.f.check_outbound(command(hci.HCI_DISCONNECT_COMMAND, SCO.to_bytes(2, "little") + b"\x13"))
        with self.assertRaises(ScopeError):
            self.f.check_outbound(command(hci.HCI_DISCONNECT_COMMAND, (0x0099).to_bytes(2, "little") + b"\x13"))

    def test_voice_link_for_another_address_stops_the_run(self):
        with self.assertRaises(ScopeError):
            self.f.check_inbound(sync_complete(OTHER))

    def test_a_second_voice_link_is_refused(self):
        self.f.check_inbound(sync_complete(PHONE, handle=SCO))
        with self.assertRaises(ScopeError):
            self.f.check_inbound(sync_complete(PHONE, handle=0x0043))

    def test_a_failed_voice_link_sets_nothing(self):
        self.f.check_inbound(sync_complete(PHONE, status=0x0D))
        self.assertIsNone(self.f.sco_handle)

    def test_voice_data_needs_the_voice_link(self):
        with self.assertRaises(ScopeError):
            self.f.check_outbound(sco_packet(SCO, bytes(60)))  # no voice link yet
        self.f.check_inbound(sync_complete(PHONE))
        self.f.check_outbound(sco_packet(SCO, bytes(60)))
        with self.assertRaises(ScopeError):
            self.f.check_outbound(sco_packet(0x0077, bytes(60)))

    def test_voice_packet_length_must_be_consistent_and_small(self):
        self.f.check_inbound(sync_complete(PHONE))
        bad = bytearray(sco_packet(SCO, bytes(60)))
        bad[3] = 59  # header says 59, packet carries 60
        with self.assertRaises(ScopeError):
            self.f.check_outbound(bytes(bad))
        with self.assertRaises(ScopeError):
            self.f.check_outbound(sco_packet(SCO, bytes(MAX_SCO_PAYLOAD + 1)))  # 255 does not fit the controller buffer
        self.f.check_outbound(sco_packet(SCO, bytes(MAX_SCO_PAYLOAD)))

    def test_voice_link_is_forgotten_when_it_disconnects(self):
        self.f.check_inbound(sync_complete(PHONE))
        self.f.check_inbound(event(hci.HCI_DISCONNECTION_COMPLETE_EVENT,
                                   b"\0" + SCO.to_bytes(2, "little") + b"\x16"))
        self.assertIsNone(self.f.sco_handle)
        self.f.check_inbound(sync_complete(PHONE, handle=0x0050))  # a new link is fine afterwards

    def test_acl_data_only_on_the_known_link(self):
        self.f.check_outbound(acl_packet(ACL, b"data"))
        with self.assertRaises(ScopeError):
            self.f.check_outbound(acl_packet(0x0099, b"data"))
        fresh = ScopeFilter(PHONE)
        with self.assertRaises(ScopeError):
            fresh.check_outbound(acl_packet(ACL, b"data"))  # link not known yet

    def test_flag_bits_in_the_handle_field_are_ignored(self):
        self.f.check_outbound(acl_packet(ACL | 0x2000, b"data"))  # packet boundary flags


if __name__ == "__main__":
    unittest.main()

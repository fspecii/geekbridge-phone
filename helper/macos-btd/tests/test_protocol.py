# SPDX-License-Identifier: GPL-3.0-or-later
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gbp_btd import protocol
from gbp_btd.protocol import ProtocolError, parse_request


def req(**kw):
    return json.dumps(kw).encode()


class ParseTests(unittest.TestCase):
    def test_good_requests(self):
        self.assertEqual(parse_request(req(id=1, op="status")), (1, "status", {}))
        self.assertEqual(parse_request(req(id=2, op="dial", number="+441234567")), (2, "dial", {"number": "+441234567"}))
        self.assertEqual(parse_request(req(id=3, op="answer")), (3, "answer", {"call_id": None}))
        self.assertEqual(parse_request(req(id=4, op="open_service", service="map"))[2], {"service": "map"})
        self.assertEqual(parse_request(req(id=5, op="pair", address="6c:ac:c2:00:00:01"))[2],
                         {"address": "6C:AC:C2:00:00:01"})
        self.assertEqual(parse_request(req(id=6, op="scan", seconds=10))[2], {"seconds": 10})

    def bad(self, code, **kw):
        with self.assertRaises(ProtocolError) as caught:
            parse_request(req(**kw))
        self.assertEqual(caught.exception.code, code)

    def test_unknown_and_missing_operations(self):
        self.bad("unknown_op", id=1, op="format_disk")
        self.bad("unknown_op", id=1)

    def test_ids_must_be_whole_numbers(self):
        self.bad("bad_request", id="1", op="status")
        self.bad("bad_request", id=-1, op="status")
        self.bad("bad_request", id=True, op="status")
        self.bad("bad_request", id=1.5, op="status")
        self.bad("bad_request", op="status")

    def test_numbers_cannot_smuggle_commands(self):
        for number in ("1;AT+CHUP", "12\r\nATA", "1 2", "", "x" * 40, "+44(0)1", 12345):
            self.bad("bad_request", id=1, op="dial", number=number)
        self.assertEqual(parse_request(req(id=1, op="dial", number="*31#+4412;"))[2]["number"], "*31#+4412;")

    def test_tones(self):
        self.bad("bad_request", id=1, op="tones", tones="12E")
        self.bad("bad_request", id=1, op="tones", tones="")
        self.assertEqual(parse_request(req(id=1, op="tones", tones="12#*A"))[2]["tones"], "12#*A")

    def test_addresses(self):
        for address in ("nope", "6C:AC:C2:5C:46", "6C:AC:C2:00:00:01:00", "ZZ:AC:C2:5C:46:DA", None, 5):
            self.bad("bad_request", id=1, op="pair", address=address)

    def test_scan_seconds_and_services(self):
        for seconds in (2, 31, "10", 10.5, True):
            self.bad("bad_request", id=1, op="scan", seconds=seconds)
        self.bad("bad_request", id=1, op="open_service", service="hfp")

    def test_unexpected_and_missing_fields(self):
        self.bad("bad_request", id=1, op="status", extra=1)
        self.bad("bad_request", id=1, op="dial")
        self.bad("bad_request", id=1, op="answer", call_id="../etc")

    def test_not_json_or_not_an_object(self):
        for raw in (b"{", b"[]", b"7", b"\xff\xfe", b""):
            with self.assertRaises(ProtocolError):
                parse_request(raw)

    def test_oversize_message(self):
        with self.assertRaises(ProtocolError) as caught:
            parse_request(b" " * (protocol.MAX_LINE + 1))
        self.assertEqual(caught.exception.code, "too_large")


class EncodeTests(unittest.TestCase):
    def test_replies_and_events_are_one_line(self):
        for message in (protocol.ok(1, calls=[]), protocol.error(2, "busy", "later"), protocol.event("fault", reason="x\ny")):
            data = protocol.encode(message)
            self.assertEqual(data.count(b"\n"), 1)
            self.assertTrue(data.endswith(b"\n"))
            json.loads(data)

    def test_an_event_field_called_name_does_not_collide_with_the_event_name(self):
        message = protocol.event("pairing_done", name="Pixel", address="AA:BB:CC:DD:EE:FF")
        self.assertEqual(message, {"event": "pairing_done", "name": "Pixel", "address": "AA:BB:CC:DD:EE:FF"})

    def test_error_shape(self):
        self.assertEqual(protocol.error(3, "busy", "m"), {"id": 3, "ok": False, "error": {"code": "busy", "message": "m"}})

    def test_oversize_reply_is_refused(self):
        with self.assertRaises(ProtocolError):
            protocol.encode(protocol.ok(1, blob="x" * protocol.MAX_LINE))


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: GPL-3.0-or-later
"""The live test program's driver, run offline against the helper with the fake stack, so a bug in it
cannot waste a real run."""
import asyncio
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeStack, until

import session_test
from gbp_btd.stack import CallRow
from gbp_btd.voice import UP, encode_frame

TARGET = "6C:AC:C2:00:00:01"


class Driver(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(prefix="gbps", dir="/tmp")
        self.stack = FakeStack()
        # A phone that answers an OBEX Connect with Success, like a real one.
        self.stack.channel_reply = lambda data: b"\xa0\x00\x07\x10\x00\x20\x00" if data[:1] == b"\x80" else None

    async def asyncTearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    async def run_driver(self, ring=True, stream=None, services_only=False):
        loop = asyncio.get_running_loop()
        started = asyncio.Event()

        def cue():
            loop.call_soon_threadsafe(started.set)

        async def phone():
            await asyncio.wait_for(started.wait(), 10)
            await until(lambda: self.stack.links and self.stack.links[-1].session)
            session = self.stack.links[-1].session
            if ring:
                session.calls_rows = [CallRow(number="+441234567")]
                session.on_indicator("callsetup", 1)
                await until(lambda: any(c == "answer" for c in session.commands), 15)
                session.on_indicator("call", 1)
                session.on_indicator("callsetup", 0)
                # the phone streams voice packets for a while
                for _ in range(400):
                    session.on_voice(b"\x02\x00" * 30, True)
                    await asyncio.sleep(0.005)
                await until(lambda: "hang_up" in session.commands, 15)
                session.on_indicator("call", 0)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            feeder = asyncio.ensure_future(phone()) if ring else None
            result = await session_test.run_session(self.stack, self.dir, TARGET, listen=10 if ring else 2, hold=3, cue=cue,
                                                    services_only=services_only)
            if feeder:
                await asyncio.gather(feeder, return_exceptions=True)
        lines = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]
        return result, lines

    async def test_a_full_driven_session_passes(self):
        result, lines = await self.run_driver()
        stages = [line["stage"] for line in lines]
        self.assertTrue(result["ok"], lines)
        for expected in ("helper-ready", "phones", "phone-connected", "service-pbap", "service-map",
                         "waiting-for-call", "incoming-call", "answered", "voice", "status"):
            self.assertIn(expected, stages)
        voice = next(line for line in lines if line["stage"] == "voice")
        self.assertGreater(voice["frames"], 100)
        self.assertGreaterEqual(voice["seconds_with_frames"], 1)

    async def test_services_only_checks_contacts_and_messages_and_never_waits_for_a_call(self):
        result, lines = await self.run_driver(ring=False, services_only=True)
        stages = [line["stage"] for line in lines]
        self.assertTrue(result["ok"], lines)
        self.assertIn("service-pbap", stages)
        self.assertIn("service-map", stages)
        self.assertNotIn("waiting-for-call", stages)
        pbap = next(line for line in lines if line["stage"] == "service-pbap")
        self.assertIn("open_seconds", pbap)
        self.assertIn("answer_seconds", pbap)

    async def test_services_only_fails_when_the_phone_stays_silent(self):
        self.stack.channel_reply = lambda data: None  # never answers
        original = session_test.probe_service
        session_test.probe_service = lambda app, kind, wait=1: original(app, kind, wait=1)  # keep the test short
        try:
            result, lines = await self.run_driver(ring=False, services_only=True)
        finally:
            session_test.probe_service = original
        self.assertFalse(result["ok"])
        silent = next(line for line in lines if line["stage"] == "service-pbap")
        self.assertEqual(silent["error"], "TimeoutError")
        self.assertIn("waited_seconds", silent)

    async def test_nothing_secret_is_printed(self):
        _, lines = await self.run_driver()
        text = json.dumps(lines)
        self.assertNotIn("+441234567", text)  # the number is shown only as its last three digits
        self.assertIn('"number_tail": "567"', text)

    async def test_no_call_fails_cleanly_without_a_hang(self):
        result, lines = await self.run_driver(ring=False)
        self.assertFalse(result["ok"])
        self.assertIn("session-failed", [line["stage"] for line in lines])

    async def test_an_unpaired_target_is_reported(self):
        self.stack.known.clear()
        result, lines = await self.run_driver(ring=False)
        self.assertFalse(result["ok"])
        self.assertIn("not paired", json.dumps(lines))

    async def test_a_phone_that_refuses_obex_fails_the_check(self):
        self.stack.channel_reply = lambda data: b"\xc3\x00\x03"  # Forbidden
        result, lines = await self.run_driver()
        self.assertFalse(result["ok"])
        service = next(line for line in lines if line["stage"] == "service-pbap")
        self.assertFalse(service["ok"])

    def test_the_obex_connect_packet_is_well_formed(self):
        packet = session_test.obex_connect("pbap")
        self.assertEqual(packet[0], 0x80)
        self.assertEqual(int.from_bytes(packet[1:3], "big"), len(packet))
        self.assertEqual(packet[3:7], b"\x10\x00\x20\x00")
        self.assertEqual(packet[7], 0x46)
        self.assertIn(bytes.fromhex(session_test.TARGETS["pbap"]), packet)


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: GPL-3.0-or-later
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeStack, settle, until

from gbp_btd.phone_manager import PhoneManager
from gbp_btd.server import HandlerError
from gbp_btd.stack import CallRow, StackError
from gbp_btd.voice import VoicePort

ADDRESS = "6C:AC:C2:00:00:01"


class Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = FakeStack()
        self.events = []
        self.pauses = []

        async def emit(event_name, /, **fields):
            self.events.append((event_name, fields))

        async def sleep(seconds):
            self.pauses.append(seconds)
            await asyncio.sleep(0)

        self.down = []
        self.port = VoicePort(send=lambda frame: self.down.append(frame))
        self.mgr = PhoneManager(self.stack, emit, self.port, sleep=sleep, backoff=(3, 6, 12))

    async def asyncTearDown(self):
        await self.mgr.stop()

    def names(self):
        return [n for n, _ in self.events]

    async def connect(self):
        await self.mgr.select(ADDRESS)
        await until(lambda: self.mgr.state == "connected")
        return self.stack.links[-1], self.stack.links[-1].session


class ConnectionTests(Case):
    async def test_select_connects_and_reports(self):
        link, _ = await self.connect()
        self.assertEqual(self.mgr.selected, ADDRESS)
        self.assertIn("phone_connected", self.names())
        self.assertEqual(self.stack.connects, 1)

    async def test_selecting_the_same_phone_again_does_not_reconnect(self):
        await self.connect()
        await self.mgr.select(ADDRESS)
        self.assertEqual(self.stack.connects, 1)

    async def test_a_lost_link_is_reconnected(self):
        link, _ = await self.connect()
        link.lose()
        await until(lambda: self.stack.connects == 2 and self.mgr.state == "connected")
        self.assertIn("phone_disconnected", self.names())
        self.assertEqual(self.names().count("phone_connected"), 2)

    async def test_failures_retry_with_growing_pauses_and_report_once(self):
        self.stack.connect_failures = [StackError("link_failed"), StackError("link_failed"), StackError("link_failed")]
        await self.mgr.select(ADDRESS)
        await until(lambda: self.mgr.state == "connected")
        self.assertEqual(self.pauses[:3], [3, 6, 12])
        self.assertEqual(self.names().count("phone_error"), 1)  # the same error is not repeated

    async def test_the_pause_stops_growing_at_the_last_step(self):
        self.stack.connect_failures = [StackError("timeout")] * 5
        await self.mgr.select(ADDRESS)
        await until(lambda: self.mgr.state == "connected")
        self.assertEqual(self.pauses[:5], [3, 6, 12, 12, 12])

    async def test_a_hands_free_failure_is_retried_too(self):
        self.stack.hfp_failures = [StackError("no_service")]
        await self.mgr.select(ADDRESS)
        await until(lambda: self.mgr.state == "connected")
        self.assertEqual(self.stack.connects, 2)

    async def test_a_controller_fault_is_not_retried(self):
        self.stack.connect_failures = [StackError("fault", "controller crashed")]
        await self.mgr.select(ADDRESS)
        await until(lambda: self.mgr.state == "fault")
        await settle(20)
        self.assertEqual(self.stack.connects, 1)
        self.assertIn(("fault", {"code": "fault", "message": "controller crashed"}), self.events)
        self.assertEqual(self.mgr.snapshot()["fault"]["code"], "fault")

    async def test_stop_closes_the_session_and_the_link(self):
        link, session = await self.connect()
        await self.mgr.stop()
        self.assertTrue(session.closed and link.closed)
        self.assertEqual(self.mgr.state, "idle")

    async def test_selecting_another_phone_replaces_the_connection(self):
        link, _ = await self.connect()
        await self.mgr.select("11:22:33:44:55:66")
        await until(lambda: self.stack.connects == 2 and self.mgr.state == "connected")
        self.assertTrue(link.closed)

    async def test_paused_drops_the_link_and_comes_back(self):
        link, _ = await self.connect()
        async with self.mgr.paused():
            self.assertTrue(link.closed)
            self.assertEqual(self.mgr.state, "idle")
        await until(lambda: self.mgr.state == "connected")
        self.assertEqual(self.stack.connects, 2)

    async def test_paused_does_nothing_when_no_phone_was_selected(self):
        async with self.mgr.paused():
            pass
        await settle()
        self.assertEqual(self.stack.connects, 0)


class CallTests(Case):
    async def ring(self, session, number="+441234567"):
        session.calls_rows = [CallRow(number=number)]
        session.on_indicator("callsetup", 1)
        await until(lambda: any(n == "call_state" for n in self.names()))

    async def test_an_incoming_call_is_reported_with_its_number(self):
        _, session = await self.connect()
        await self.ring(session)
        self.assertIn(("call_state", {"id": "call-1", "state": "incoming", "number": "+441234567"}), self.events)
        self.assertEqual(self.mgr.calls(), [{"id": "call-1", "state": "incoming", "number": "+441234567", "name": ""}])

    async def test_a_ring_without_a_number_is_still_reported(self):
        _, session = await self.connect()
        session.calls_rows = []
        session.on_indicator("callsetup", 1)
        await until(lambda: any(n == "call_state" for n in self.names()))
        self.assertEqual(self.mgr.calls()[0]["number"], "")

    async def test_answer_then_hang_up(self):
        _, session = await self.connect()
        await self.ring(session)
        await self.mgr.answer()
        session.on_indicator("call", 1)
        session.on_indicator("callsetup", 0)
        await until(lambda: self.mgr.calls() and self.mgr.calls()[0]["state"] == "active")
        await self.mgr.hang_up()
        self.assertEqual(session.commands, ["answer", "hang_up"])

    async def test_hanging_up_a_ringing_call_declines_it(self):
        _, session = await self.connect()
        await self.ring(session)
        await self.mgr.hang_up()
        self.assertEqual(session.commands, ["reject"])

    async def test_the_end_of_a_call_is_reported(self):
        _, session = await self.connect()
        await self.ring(session)
        session.on_indicator("call", 1)
        session.on_indicator("callsetup", 0)
        session.on_indicator("call", 0)
        await until(lambda: any(f.get("state") == "ended" for n, f in self.events if n == "call_state"))
        self.assertEqual(self.mgr.calls(), [])

    async def test_dialling_and_tones(self):
        _, session = await self.connect()
        await self.mgr.dial("+441234567")
        await self.mgr.tones("12#")
        self.assertEqual(session.commands, [("dial", "+441234567"), ("tones", "12#")])

    async def test_commands_without_a_phone_fail_clearly(self):
        for action in (self.mgr.answer, self.mgr.hang_up):
            with self.assertRaises(HandlerError) as caught:
                await action()
            self.assertEqual(caught.exception.code, "no_phone")
        with self.assertRaises(HandlerError):
            await self.mgr.dial("123")

    async def test_a_lost_link_during_a_call_reports_it_ended(self):
        link, session = await self.connect()
        await self.ring(session)
        session.on_indicator("call", 1)
        link.lose()
        await until(lambda: any(f.get("state") == "ended" for n, f in self.events if n == "call_state"))


class VoiceTests(Case):
    async def test_phone_audio_goes_to_the_app_and_app_audio_goes_back(self):
        from gbp_btd.voice import UP, FrameReader, encode_frame
        _, session = await self.connect()
        self.port.on_frame(UP, 1, b"\x09\x00" * 30)
        session.on_voice(b"\x02\x00" * 30, True)
        self.assertEqual(session.sent_voice, [b"\x09\x00" * 30])
        frames = FrameReader().feed(b"".join(self.down))
        self.assertEqual(frames[0][2], b"\x02\x00" * 30)

    async def test_with_no_app_audio_the_phone_gets_silence_of_the_right_length(self):
        _, session = await self.connect()
        session.on_voice(b"\x02\x00" * 30, True)
        self.assertEqual(session.sent_voice, [bytes(60)])

    async def test_damaged_audio_is_silence_both_ways(self):
        from gbp_btd.voice import FrameReader
        _, session = await self.connect()
        session.on_voice(b"\x02\x00" * 30, False)
        self.assertEqual(session.sent_voice, [bytes(60)])
        self.assertEqual(FrameReader().feed(b"".join(self.down))[0][2], bytes(60))

    async def test_a_bad_packet_is_a_reported_fault_and_stops_the_echo(self):
        _, session = await self.connect()
        session.on_voice(bytes(61), True)  # odd length
        await until(lambda: any(n == "fault" for n in self.names()))
        session.on_voice(bytes(60), True)
        self.assertEqual(session.sent_voice, [])


if __name__ == "__main__":
    unittest.main()

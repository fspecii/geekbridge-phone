# SPDX-License-Identifier: GPL-3.0-or-later
import asyncio
import os
import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeStack, until

from gbp_btd.handler import Handler
from gbp_btd.pairing import PairingManager
from gbp_btd.phone_manager import PhoneManager
from gbp_btd.server import HandlerError, Reply
from gbp_btd.services import ServiceManager
from gbp_btd.stack import StackError
from gbp_btd.voice import VoicePort

ADDRESS = "6C:AC:C2:00:00:01"


class Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = FakeStack()
        self.events = []

        async def emit(event_name, /, **fields):
            self.events.append((event_name, fields))

        async def sleep(seconds):
            await asyncio.sleep(0)

        self.port = VoicePort(send=lambda f: None)
        self.phones = PhoneManager(self.stack, emit, self.port, sleep=sleep, backoff=(1,))
        self.services = ServiceManager(self.phones)
        self.pairing = PairingManager(self.stack, self.phones, emit)
        self.stopped = []
        self.handler = Handler(self.stack, self.phones, self.services, self.pairing, self.port,
                               request_stop=lambda: self.stopped.append(1), grace=0.05)

    async def asyncTearDown(self):
        await self.services.close_all()
        await self.phones.stop()

    async def connect(self):
        await self.phones.select(ADDRESS)
        await until(lambda: self.phones.state == "connected")
        return self.stack.links[-1]


class ServiceTests(Case):
    async def open(self, kind="map"):
        fd = await self.services.open(kind)
        sock = socket.socket(fileno=fd)
        sock.setblocking(False)
        return sock

    async def read(self, sock, timeout=2):
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(loop.sock_recv(sock, 4096), timeout)

    async def test_phone_bytes_reach_the_app_and_app_bytes_reach_the_phone(self):
        link = await self.connect()
        app = await self.open("pbap")
        kind, channel = link.channels[0]
        self.assertEqual(kind, "pbap")
        channel.feed(b"phonebook data")
        self.assertEqual(await self.read(app), b"phonebook data")
        await asyncio.get_running_loop().sock_sendall(app, b"GET")
        await until(lambda: channel.written)
        self.assertEqual(channel.written, [b"GET"])
        app.close()

    async def test_the_app_closing_its_end_closes_the_channel(self):
        link = await self.connect()
        app = await self.open()
        _, channel = link.channels[0]
        app.close()
        await until(lambda: channel.closed)

    async def test_the_phone_closing_the_channel_closes_the_app_end(self):
        link = await self.connect()
        app = await self.open()
        _, channel = link.channels[0]
        await channel.close()
        self.assertEqual(await self.read(app), b"")  # end of stream
        self.assertNotIn("map", self.services.open_channels)
        app.close()

    async def test_opening_again_replaces_the_old_channel(self):
        link = await self.connect()
        first = await self.open("map")
        old = link.channels[0][1]
        second = await self.open("map")
        self.assertTrue(old.closed)
        self.assertEqual(len(link.channels), 2)
        first.close()
        second.close()

    async def test_the_two_services_are_independent(self):
        link = await self.connect()
        a, b = await self.open("map"), await self.open("pbap")
        self.assertEqual(len(self.services.open_channels), 2)
        a.close()
        b.close()

    async def test_no_phone_is_a_clear_error(self):
        with self.assertRaises(HandlerError) as caught:
            await self.services.open("map")
        self.assertEqual(caught.exception.code, "no_phone")

    async def test_a_missing_service_is_reported(self):
        await self.connect()
        self.stack.missing_services = {"map"}
        with self.assertRaises(HandlerError) as caught:
            await self.services.open("map")
        self.assertEqual(caught.exception.code, "no_service")

    async def test_a_lost_link_closes_every_channel_when_the_app_asks(self):
        link = await self.connect()
        app = await self.open("map")
        await self.services.close_all()
        self.assertTrue(link.channels[0][1].closed)
        app.close()


class PairingTests(Case):
    async def test_scan_pauses_the_phone_and_lists_what_it_finds(self):
        link = await self.connect()
        result = await self.pairing.scan(10)
        self.assertEqual(result, {"found": [{"id": "11:22:33:44:55:66", "name": "Pixel 9", "paired": False}]})
        self.assertTrue(link.closed)
        await until(lambda: self.phones.state == "connected")  # reconnected afterwards

    async def test_pairing_shows_the_code_and_reports_the_result(self):
        result = await self.pairing.pair("11:22:33:44:55:66")
        self.assertEqual(result["name"], "Paired Phone")
        self.assertIn(("pairing_code", {"address": "11:22:33:44:55:66", "code": "123456"}), self.events)
        self.assertIn("pairing_done", [n for n, _ in self.events])
        self.assertIn("11:22:33:44:55:66", dict(self.stack.known_phones()))

    async def test_failures_become_clear_errors(self):
        self.stack.scan_error = StackError("timeout", "nothing answered")
        with self.assertRaises(HandlerError) as caught:
            await self.pairing.scan(5)
        self.assertEqual(caught.exception.code, "timeout")
        self.stack.pair_error = StackError("auth_failed", "the phone refused")
        with self.assertRaises(HandlerError) as caught:
            await self.pairing.pair("11:22:33:44:55:66")
        self.assertEqual(caught.exception.code, "auth_failed")

    async def test_only_one_scan_or_pairing_at_a_time(self):
        gate = asyncio.Event()
        original = self.stack.scan

        async def slow(seconds):
            await gate.wait()
            return []

        self.stack.scan = slow
        first = asyncio.ensure_future(self.pairing.scan(5))
        await asyncio.sleep(0.01)
        with self.assertRaises(HandlerError) as caught:
            await self.pairing.pair("11:22:33:44:55:66")
        self.assertEqual(caught.exception.code, "busy")
        gate.set()
        await first

    async def test_unpair_forgets_the_phone_and_stops_using_it(self):
        await self.connect()
        await self.pairing.unpair(ADDRESS)
        self.assertEqual(self.stack.forgotten, [ADDRESS])
        self.assertEqual(self.phones.state, "idle")
        self.assertIsNone(self.phones.selected)


class HandlerTests(Case):
    async def test_hello_and_status(self):
        hello = await self.handler.handle("hello", {})
        self.assertEqual(hello["protocol"], 1)
        status = await self.handler.handle("status", {})
        self.assertEqual(status["state"], "idle")
        self.assertIn("voice_port", status)

    async def test_phones_lists_paired_phones_with_connection_state(self):
        await self.handler.handle("select_phone", {"address": ADDRESS})
        await until(lambda: self.phones.state == "connected")
        rows = (await self.handler.handle("phones", {}))["phones"]
        self.assertEqual(rows, [{"address": ADDRESS, "name": "Sam's Z Fold5", "connected": True, "battery": None, "apple": False}])

    async def test_an_unknown_phone_cannot_be_selected(self):
        with self.assertRaises(HandlerError) as caught:
            await self.handler.handle("select_phone", {"address": "11:22:33:44:55:66"})
        self.assertEqual(caught.exception.code, "unknown_phone")

    async def test_open_service_returns_a_descriptor(self):
        await self.connect()
        reply = await self.handler.handle("open_service", {"service": "map"})
        self.assertIsInstance(reply, Reply)
        self.assertEqual(reply.fields, {"service": "map"})
        os.close(reply.fd)

    async def test_call_commands_reach_the_phone(self):
        link = await self.connect()
        await self.handler.handle("dial", {"number": "123"})
        await self.handler.handle("tones", {"tones": "5"})
        await self.handler.handle("answer", {"call_id": None})
        await self.handler.handle("hang_up_all", {})
        self.assertEqual(link.session.commands, [("dial", "123"), ("tones", "5"), "answer", "hang_up"])

    async def test_shutdown_asks_to_stop(self):
        await self.handler.handle("shutdown", {})
        await asyncio.sleep(0.01)
        self.assertEqual(self.stopped, [1])

    async def test_the_helper_stops_when_the_app_does_not_come_back(self):
        await self.handler.client_gone()
        await asyncio.sleep(0.15)
        self.assertEqual(self.stopped, [1])

    async def test_the_app_coming_back_cancels_the_stop(self):
        await self.handler.client_gone()
        await self.handler.handle("status", {})  # the app is back
        await asyncio.sleep(0.15)
        self.assertEqual(self.stopped, [])


if __name__ == "__main__":
    unittest.main()

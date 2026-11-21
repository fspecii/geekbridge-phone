# SPDX-License-Identifier: GPL-3.0-or-later
"""The whole helper, assembled as it runs, driven through its real sockets with a fake phone."""
import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeStack, until

from gbp_btd.app import Helper
from gbp_btd.stack import CallRow
from gbp_btd.voice import DOWN, HEADER, UP, FrameReader, encode_frame

ADDRESS = "6C:AC:C2:00:00:01"


class App:
    """A stand-in for the Rust app: the control socket and the voice socket."""

    def __init__(self, directory):
        self.control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.control.settimeout(3)
        self.control.connect(os.path.join(directory, "btd.sock"))
        self.buffer, self.fds, self.next_id = b"", [], 0

    def send(self, op, **params):
        self.next_id += 1
        self.control.sendall((json.dumps({"id": self.next_id, "op": op, **params}) + "\n").encode())
        return self.next_id

    def line(self):
        while b"\n" not in self.buffer:
            data, fds, _, _ = socket.recv_fds(self.control, 65536, 4)
            if not data:
                return None
            self.buffer += data
            self.fds += fds
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def reply(self, request_id):
        """The reply to a request, setting aside any events that arrive first."""
        self.events = getattr(self, "events", [])
        while True:
            message = self.line()
            if message is None:
                raise AssertionError("connection closed")
            if message.get("id") == request_id:
                return message
            self.events.append(message)

    def call(self, op, **params):
        return self.reply(self.send(op, **params))

    def wait_event(self, name):
        self.events = getattr(self, "events", [])
        for i, event in enumerate(self.events):
            if event.get("event") == name:
                return self.events.pop(i)
        while True:
            message = self.line()
            if message.get("event") == name:
                return message
            self.events.append(message)

    def close(self):
        self.control.close()


class EndToEnd(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(prefix="gbpe", dir="/tmp")
        self.stack = FakeStack()

        async def sleep(seconds):
            await asyncio.sleep(0)

        self.helper = Helper(self.dir, self.stack, grace=30, sleep=sleep, backoff=(1,))
        await self.helper.start()

    async def asyncTearDown(self):
        await self.helper.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def t(self, fn, *args, **kw):
        return await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*args, **kw))

    async def test_a_full_session(self):
        app = await self.t(App, self.dir)
        hello = await self.t(app.call, "hello")
        self.assertEqual(hello["protocol"], 1)
        self.assertEqual(hello["stack"], {"controller": "fake", "stack": "fake"})

        phones = (await self.t(app.call, "phones"))["phones"]
        self.assertEqual(phones[0]["address"], ADDRESS)
        self.assertFalse(phones[0]["connected"])

        self.assertTrue((await self.t(app.call, "select_phone", address=ADDRESS))["ok"])
        await self.t(app.wait_event, "phone_connected")
        self.assertTrue((await self.t(app.call, "phones"))["phones"][0]["connected"])

        # An incoming call: the number arrives, the app sees the event and the call list.
        session = self.stack.links[-1].session
        session.calls_rows = [CallRow(number="+441234567")]
        session.on_indicator("callsetup", 1)
        event = await self.t(app.wait_event, "call_state")
        self.assertEqual((event["state"], event["number"]), ("incoming", "+441234567"))
        calls = (await self.t(app.call, "calls"))["calls"]
        self.assertEqual(calls[0]["state"], "incoming")

        # Answer, and voice flows both ways over the voice socket.
        self.assertTrue((await self.t(app.call, "answer"))["ok"])
        session.on_indicator("call", 1)
        session.on_indicator("callsetup", 0)
        reader, writer = await asyncio.open_unix_connection(os.path.join(self.dir, "voice.sock"))
        await asyncio.sleep(0.05)
        writer.write(encode_frame(UP, 1, b"\x05\x00" * 30))
        await writer.drain()
        await until(lambda: self.helper.port.up_frames == 1)
        session.on_voice(b"\x02\x00" * 30, True)  # a voice packet from the phone
        self.assertEqual(session.sent_voice[-1], b"\x05\x00" * 30)  # the app's audio went to the phone
        frame = await asyncio.wait_for(reader.readexactly(HEADER.size + 60), 2)
        self.assertEqual(FrameReader().feed(frame)[0][2], b"\x02\x00" * 30)  # and the phone's to the app
        writer.close()

        # Contacts: a PBAP channel arrives as a descriptor and carries bytes both ways.
        reply = await self.t(app.call, "open_service", service="pbap")
        self.assertTrue(reply["ok"])
        self.assertEqual(len(app.fds), 1)
        mine = socket.socket(fileno=app.fds.pop())
        mine.settimeout(2)
        channel = self.stack.links[-1].channels[-1][1]
        channel.feed(b"BEGIN:VCARD")
        self.assertEqual(await self.t(mine.recv, 100), b"BEGIN:VCARD")
        mine.sendall(b"GET")
        await until(lambda: channel.written == [b"GET"])
        mine.close()

        # Hang up, then shutdown: the helper stops itself.
        self.assertTrue((await self.t(app.call, "hang_up"))["ok"])
        self.assertTrue((await self.t(app.call, "shutdown"))["ok"])
        await asyncio.wait_for(self.helper.server.stopped.wait(), 2)
        app.close()

    async def test_scan_and_pairing_through_the_socket(self):
        app = await self.t(App, self.dir)
        found = (await self.t(app.call, "scan", seconds=5))["found"]
        self.assertEqual(found[0]["name"], "Pixel 9")
        reply = await self.t(app.call, "pair", address="11:22:33:44:55:66")
        self.assertTrue(reply["ok"])
        code = await self.t(app.wait_event, "pairing_code")
        self.assertEqual(code["code"], "123456")
        done = await self.t(app.wait_event, "pairing_done")
        self.assertEqual(done["phone_name"], "Paired Phone")
        app.close()

    async def test_errors_reach_the_app_with_clear_codes(self):
        app = await self.t(App, self.dir)
        self.assertEqual((await self.t(app.call, "dial", number="123"))["error"]["code"], "no_phone")
        self.assertEqual((await self.t(app.call, "open_service", service="map"))["error"]["code"], "no_phone")
        self.assertEqual((await self.t(app.call, "select_phone", address="11:22:33:44:55:66"))["error"]["code"], "unknown_phone")
        app.close()

    async def test_the_helper_stops_everything_in_order(self):
        app = await self.t(App, self.dir)
        await self.t(app.call, "select_phone", address=ADDRESS)
        await self.t(app.wait_event, "phone_connected")
        link = self.stack.links[-1]
        app.close()
        await self.helper.stop()
        self.assertTrue(link.session.closed and link.closed)  # the phone link is closed
        self.assertTrue(self.stack.stopped)                    # the controller is released last
        self.assertFalse(os.path.lexists(os.path.join(self.dir, "btd.sock")))
        self.assertFalse(os.path.lexists(os.path.join(self.dir, "voice.sock")))


class RunTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_returns_after_a_shutdown_request(self):
        directory = tempfile.mkdtemp(prefix="gbpe", dir="/tmp")
        stack = FakeStack()
        helper = Helper(directory, stack, grace=30)
        task = asyncio.ensure_future(helper.run())
        await until(lambda: helper.server is not None and helper.server.path and os.path.exists(helper.server.path))
        app = await asyncio.get_running_loop().run_in_executor(None, App, directory)
        await asyncio.get_running_loop().run_in_executor(None, lambda: app.call("shutdown"))
        await asyncio.wait_for(task, 3)
        self.assertTrue(stack.started and stack.stopped)
        app.close()
        shutil.rmtree(directory, ignore_errors=True)

    async def test_run_returns_when_the_app_never_comes_back(self):
        directory = tempfile.mkdtemp(prefix="gbpe", dir="/tmp")
        helper = Helper(directory, FakeStack(), grace=0.1)
        task = asyncio.ensure_future(helper.run())
        await until(lambda: helper.server is not None and helper.server.path and os.path.exists(helper.server.path))
        app = await asyncio.get_running_loop().run_in_executor(None, App, directory)
        await asyncio.get_running_loop().run_in_executor(None, lambda: app.call("status"))
        app.close()  # the app vanishes
        await asyncio.wait_for(task, 3)  # the helper stops itself after the grace period
        shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

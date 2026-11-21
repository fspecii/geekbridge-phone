# SPDX-License-Identifier: GPL-3.0-or-later
import asyncio
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gbp_btd import protocol
from gbp_btd.server import HandlerError, Reply, Server, prepare_directory


class TestClient:
    """A blocking client, run in a thread so it does not stall the server's event loop."""

    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(3)
        self.sock.connect(path)
        self.buffer = b""
        self.fds = []

    def send(self, message):
        data = message if isinstance(message, bytes) else (json.dumps(message) + "\n").encode()
        self.sock.sendall(data)

    def line(self):
        while b"\n" not in self.buffer:
            data, fds, _, _ = socket.recv_fds(self.sock, 65536, 4)
            if not data:
                return None
            self.buffer += data
            self.fds += fds
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def close(self):
        self.sock.close()


class Handler:
    def __init__(self):
        self.gone = 0
        self.seen = []
        self.release = None

    async def handle(self, op, params):
        self.seen.append((op, params))
        if op == "status":
            return {"state": "idle"}
        if op == "dial":
            raise HandlerError("no_phone", "no phone is connected")
        if op == "open_service":
            a, b = socket.socketpair()
            self.peer = a  # the helper's end
            return Reply({"service": params["service"]}, fd=b.detach())
        if op == "scan":
            await self.release.wait()
            return {"found": []}
        if op == "phones":
            raise ValueError("boom")
        return {}

    async def client_gone(self):
        self.gone += 1


class ServerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(prefix="gbpt", dir="/tmp")
        self.handler = Handler()
        self.handler.release = asyncio.Event()
        self.server = Server(self.dir, self.handler, log=lambda *a: None)
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def in_thread(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    async def client(self):
        return await self.in_thread(TestClient, self.server.path)

    async def ask(self, client, **message):
        await self.in_thread(client.send, message)
        return await self.in_thread(client.line)


class BasicTests(ServerCase):
    async def test_a_request_gets_a_reply(self):
        client = await self.client()
        reply = await self.ask(client, id=1, op="status")
        self.assertEqual(reply, {"id": 1, "ok": True, "state": "idle"})
        client.close()

    async def test_handler_errors_become_error_replies(self):
        client = await self.client()
        reply = await self.ask(client, id=2, op="dial", number="123")
        self.assertEqual(reply, {"id": 2, "ok": False, "error": {"code": "no_phone", "message": "no phone is connected"}})
        client.close()

    async def test_unexpected_errors_do_not_leak_details(self):
        client = await self.client()
        reply = await self.ask(client, id=3, op="phones")
        self.assertEqual(reply["error"]["code"], "internal")
        self.assertNotIn("boom", json.dumps(reply))
        client.close()

    async def test_bad_requests_get_errors_and_the_connection_stays_up(self):
        client = await self.client()
        await self.in_thread(client.send, b"not json\n")
        self.assertEqual((await self.in_thread(client.line))["error"]["code"], "bad_json")
        reply = await self.ask(client, id=4, op="dial", number="1;ATA")
        self.assertEqual(reply["error"]["code"], "bad_request")
        self.assertEqual((await self.ask(client, id=5, op="status"))["ok"], True)
        client.close()

    async def test_validated_parameters_reach_the_handler(self):
        client = await self.client()
        await self.ask(client, id=6, op="pair", address="6c:ac:c2:00:00:01")
        self.assertEqual(self.handler.seen[-1], ("pair", {"address": "6C:AC:C2:00:00:01"}))
        client.close()

    async def test_a_runaway_line_drops_the_app(self):
        client = await self.client()
        await self.in_thread(client.send, b"x" * (protocol.MAX_LINE + 10))
        reply = await self.in_thread(client.line)
        self.assertEqual(reply["error"]["code"], "too_large")
        client.close()

    async def test_requests_are_answered_as_they_finish(self):
        client = await self.client()
        await self.in_thread(client.send, {"id": 10, "op": "scan", "seconds": 5})  # slow: waits for release
        fast = await self.ask(client, id=11, op="status")
        self.assertEqual(fast["id"], 11)
        self.handler.release.set()
        slow = await self.in_thread(client.line)
        self.assertEqual(slow["id"], 10)
        client.close()


class DescriptorTests(ServerCase):
    async def test_a_service_channel_arrives_as_a_working_descriptor(self):
        client = await self.client()
        reply = await self.ask(client, id=1, op="open_service", service="pbap")
        self.assertEqual(reply, {"id": 1, "ok": True, "service": "pbap", "fd": True})
        self.assertEqual(len(client.fds), 1)
        mine = socket.socket(fileno=client.fds[0])
        mine.settimeout(2)
        self.handler.peer.sendall(b"hello app")
        self.assertEqual(await self.in_thread(mine.recv, 100), b"hello app")
        mine.sendall(b"hello helper")
        self.assertEqual(await self.in_thread(self.handler.peer.recv, 100), b"hello helper")
        mine.close()
        self.handler.peer.close()
        client.close()

    async def test_the_helper_does_not_keep_the_passed_descriptor(self):
        client = await self.client()
        await self.ask(client, id=9, op="status")  # the server has accepted us: its end of the connection exists
        before = len(os.listdir("/dev/fd"))
        await self.ask(client, id=1, op="open_service", service="map")
        await asyncio.sleep(0.05)
        for fd in client.fds:  # the test client shares this process: drop the app's copy
            os.close(fd)
        client.fds.clear()
        # only the helper's own end (peer) remains: the server kept no copy of the app's end
        self.assertLessEqual(len(os.listdir("/dev/fd")), before + 1)
        client.close()


class ClientTests(ServerCase):
    async def test_a_second_app_is_turned_away_while_one_is_connected(self):
        first = await self.client()
        await self.ask(first, id=1, op="status")
        second = await self.client()
        reply = await self.in_thread(second.line)
        self.assertEqual(reply["error"]["code"], "busy")
        first.close()
        second.close()

    async def test_the_app_can_reconnect_and_the_handler_is_told(self):
        first = await self.client()
        await self.ask(first, id=1, op="status")
        first.close()
        for _ in range(100):
            if self.handler.gone:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(self.handler.gone, 1)
        second = await self.client()
        self.assertEqual((await self.ask(second, id=1, op="status"))["ok"], True)
        second.close()

    async def test_events_reach_the_connected_app_and_are_dropped_without_one(self):
        await self.server.broadcast("fault", reason="nobody listening")  # no error
        client = await self.client()
        await self.ask(client, id=1, op="status")
        await self.server.broadcast("call_state", state="ringing")
        self.assertEqual(await self.in_thread(client.line), {"event": "call_state", "state": "ringing"})
        client.close()

    async def test_shutdown_sets_the_stop_signal_after_replying(self):
        client = await self.client()
        reply = await self.ask(client, id=1, op="shutdown")
        self.assertTrue(reply["ok"])
        await asyncio.wait_for(self.server.stopped.wait(), 2)
        client.close()


class FilesystemTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_socket_is_private_and_removed_on_stop(self):
        directory = tempfile.mkdtemp(prefix="gbpt", dir="/tmp")
        server = Server(directory, Handler(), log=lambda *a: None)
        await server.start()
        self.assertEqual(stat.S_IMODE(os.stat(server.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
        await server.stop()
        self.assertFalse(os.path.lexists(server.path))
        shutil.rmtree(directory)

    def test_a_folder_open_to_others_is_refused(self):
        directory = tempfile.mkdtemp(prefix="gbpt", dir="/tmp")
        os.chmod(directory, 0o755)
        with self.assertRaises(RuntimeError):
            prepare_directory(directory)
        shutil.rmtree(directory)

    def test_a_link_instead_of_a_folder_is_refused(self):
        directory = tempfile.mkdtemp(prefix="gbpt", dir="/tmp")
        link = directory + "-link"
        os.symlink(directory, link)
        with self.assertRaises(RuntimeError):
            prepare_directory(link)
        os.unlink(link)
        shutil.rmtree(directory)

    async def test_a_path_too_long_for_a_unix_socket_is_refused(self):
        directory = tempfile.mkdtemp(prefix="gbpt", dir="/tmp")
        deep = os.path.join(directory, "a" * 90)
        server = Server(deep, Handler(), log=lambda *a: None)
        with self.assertRaises(RuntimeError):
            await server.start()
        shutil.rmtree(directory)

    async def test_a_stale_socket_file_is_replaced(self):
        directory = tempfile.mkdtemp(prefix="gbpt", dir="/tmp")
        os.chmod(directory, 0o700)
        stale = os.path.join(directory, "btd.sock")
        open(stale, "w").close()
        server = Server(directory, Handler(), log=lambda *a: None)
        await server.start()
        await server.stop()
        shutil.rmtree(directory)


if __name__ == "__main__":
    unittest.main()

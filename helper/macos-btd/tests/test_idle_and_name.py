# SPDX-License-Identifier: GPL-3.0-or-later
"""A helper nobody connects to stops by itself; a phone paired before names were kept learns its name."""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeStack

from gbp_btd.acipc import TargetKeyStore
from gbp_btd.app import Helper
from gbp_btd.bumble_stack import BumbleStack
from test_end_to_end import App

ADDRESS = "6C:AC:C2:00:00:01"


class IdleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gbpi", dir="/tmp")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    async def test_a_helper_that_no_app_connects_to_stops_by_itself(self):
        helper = Helper(self.dir, FakeStack(), idle=0.3)
        await asyncio.wait_for(helper.run(), 5)  # returns, rather than waiting for ever

    async def test_a_helper_with_an_app_keeps_running_past_the_idle_time(self):
        helper = Helper(self.dir, FakeStack(), idle=0.3, grace=30)
        task = asyncio.ensure_future(helper.run())
        for _ in range(100):
            if helper.server and helper.server.handler and (Path(self.dir) / "btd.sock").exists():
                break
            await asyncio.sleep(0.02)
        app = await asyncio.to_thread(App, self.dir)
        await asyncio.to_thread(app.call, "hello")
        await asyncio.sleep(0.8)
        self.assertFalse(task.done(), "stopped although the app had connected")
        await asyncio.to_thread(app.call, "shutdown")
        await asyncio.wait_for(task, 5)
        app.close()


class NameTests(unittest.IsolatedAsyncioTestCase):
    def stack(self, store, remote_name):
        stack = BumbleStack.__new__(BumbleStack)  # only the name lookup is under test
        stack.keystore = store

        class Device:
            calls = 0

            async def request_remote_name(self, connection):
                Device.calls += 1
                if isinstance(remote_name, Exception):
                    raise remote_name
                return remote_name

        async def guarded(coro, seconds):
            return await coro

        stack.device = Device()
        stack.guarded = guarded
        return stack, Device

    async def test_a_phone_without_a_name_is_asked_once_and_the_name_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            store = TargetKeyStore("00:00:00:00:00:01", ADDRESS, Path(d) / "keys.json")
            stack, device = self.stack(store, "Sam's Z Fold5")
            await stack._learn_name(object(), ADDRESS)
            self.assertEqual(store.names()[ADDRESS], "Sam's Z Fold5")
            await stack._learn_name(object(), ADDRESS)
            self.assertEqual(device.calls, 1, "a known name is not asked for again")

    async def test_a_phone_that_does_not_answer_leaves_the_name_empty(self):
        with tempfile.TemporaryDirectory() as d:
            store = TargetKeyStore("00:00:00:00:00:01", ADDRESS, Path(d) / "keys.json")
            for answer in ("", TimeoutError(), RuntimeError("x")):
                stack, _ = self.stack(store, answer)
                await stack._learn_name(object(), ADDRESS)
            self.assertEqual(store.names(), {})


if __name__ == "__main__":
    unittest.main()

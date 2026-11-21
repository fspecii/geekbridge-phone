# SPDX-License-Identifier: GPL-3.0-or-later
import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import until

from gbp_btd.simulated import ADDRESS, OBEX_SUCCESS, SimulatedStack
from test_end_to_end import App

ROOT = Path(__file__).resolve().parent.parent


class SimulatedStackTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_ring_answer_voice_and_hang_up(self):
        stack = SimulatedStack()
        stack.ring_after = 0.05
        link = await stack.connect(ADDRESS)
        session = await link.open_hfp()
        seen, voice = [], []
        session.on_indicator = lambda n, v: seen.append((n, v))
        session.on_voice = lambda data, ok: voice.append(data)
        await until(lambda: ("callsetup", 1) in seen)
        await session.answer()
        await until(lambda: len(voice) > 10)
        await session.hang_up()
        self.assertIn(("call", 0), seen)
        await session.close()
        await link.close()

    async def test_the_channel_answers_obex_connect(self):
        link = await SimulatedStack().connect(ADDRESS)
        channel = await link.open_channel("pbap")
        await channel.write(b"\x80\x00\x1a" + bytes(23))
        self.assertEqual(await channel.read(), OBEX_SUCCESS)


class RealProcessTests(unittest.TestCase):
    """The helper as the app will start it: a separate process, over its sockets."""

    def test_the_helper_runs_as_a_process_and_stops_on_request(self):
        directory = tempfile.mkdtemp(prefix="gbpp", dir="/tmp")
        env = dict(os.environ, GBP_SIM_RING_AFTER="0.2")
        proc = subprocess.Popen([sys.executable, "-I", str(ROOT / "btd.py"), "--stack", "simulated", "--dir", directory, "--grace", "5"],
                                cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if os.path.exists(os.path.join(directory, "btd.sock")):
                    break
                time.sleep(0.05)
            app = App(directory)
            self.assertEqual(app.call("hello")["stack"]["stack"], "simulated")
            self.assertTrue(app.call("select_phone", address=ADDRESS)["ok"])
            self.assertEqual(app.wait_event("phone_connected")["address"], ADDRESS)
            self.assertEqual(app.wait_event("call_state")["state"], "incoming")
            reply = app.call("open_service", service="map")
            self.assertTrue(reply["ok"] and reply["fd"])
            self.assertTrue(app.call("shutdown")["ok"])
            self.assertEqual(proc.wait(timeout=10), 0)
            self.assertEqual(os.listdir(directory), [])  # sockets removed
            app.close()
        finally:
            if proc.poll() is None:
                proc.kill()
            shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

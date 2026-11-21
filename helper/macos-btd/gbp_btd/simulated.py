# SPDX-License-Identifier: GPL-3.0-or-later
"""A pretend phone, for developing and testing the app side without any Bluetooth.

`python -I -m gbp_btd --stack simulated --dir DIR` runs the whole helper against this stack. It
knows one phone, connects at once, answers OBEX Connect on both services, and can ring once
after a delay (environment variable GBP_SIM_RING_AFTER, in seconds). Never used by the real app.
"""
import asyncio
import os

from .stack import CallRow, Channel, HfpSession, Link, Nearby, Stack, StackError

ADDRESS = "6C:AC:C2:00:00:01"
NAME = "Simulated Phone"
NUMBER = "+441234567"
OBEX_SUCCESS = b"\xa0\x00\x07\x10\x00\x20\x00"


class SimChannel(Channel):
    def __init__(self):
        self.queue = asyncio.Queue()
        self.closed = False

    async def read(self):
        return await self.queue.get()

    async def write(self, data):
        # An OBEX Connect (0x80) is answered with Success; anything else is echoed back.
        self.queue.put_nowait(OBEX_SUCCESS if data[:1] == b"\x80" else bytes(data))

    async def close(self):
        if not self.closed:
            self.closed = True
            self.queue.put_nowait(b"")


class SimSession(HfpSession):
    def __init__(self, ring_after):
        self.commands = []
        self.active = False
        self.voice_task = None
        self.ring_task = asyncio.ensure_future(self._ring(ring_after)) if ring_after is not None else None

    async def _ring(self, delay):
        await asyncio.sleep(delay)
        if self.on_indicator:
            self.on_indicator("callsetup", 1)

    async def _stream_voice(self):
        while self.active:
            if self.on_voice:
                self.on_voice(b"\x01\x00" * 30, True)
            await asyncio.sleep(0.004)

    async def answer(self):
        self.commands.append("answer")
        self.active = True
        if self.on_indicator:
            self.on_indicator("call", 1)
            self.on_indicator("callsetup", 0)
        self.voice_task = asyncio.ensure_future(self._stream_voice())

    async def _end(self):
        self.active = False
        if self.on_indicator:
            self.on_indicator("call", 0)
            self.on_indicator("callsetup", 0)

    async def reject(self):
        self.commands.append("reject")
        await self._end()

    async def hang_up(self):
        self.commands.append("hang_up")
        await self._end()

    async def dial(self, number):
        self.commands.append(("dial", number))
        if self.on_indicator:
            self.on_indicator("callsetup", 2)

    async def send_tones(self, tones):
        self.commands.append(("tones", tones))

    async def query_calls(self):
        return [CallRow(number=NUMBER)]

    def send_voice(self, payload):
        pass

    async def close(self):
        self.active = False
        for task in (self.ring_task, self.voice_task):
            if task:
                task.cancel()


class SimLink(Link):
    def __init__(self, address, ring_after):
        self.address, self.ring_after = address, ring_after
        self.channels = []

    async def open_hfp(self):
        return SimSession(self.ring_after)

    async def open_channel(self, kind):
        channel = SimChannel()
        self.channels.append(channel)
        return channel

    async def close(self):
        for channel in self.channels:
            await channel.close()


class SimulatedStack(Stack):
    def __init__(self):
        value = os.environ.get("GBP_SIM_RING_AFTER")
        self.ring_after = float(value) if value else None
        self.known = {ADDRESS: NAME}

    async def start(self):
        pass

    async def stop(self):
        pass

    def known_phones(self):
        return list(self.known.items())

    async def connect(self, address):
        if address not in self.known:
            raise StackError("link_failed", "unknown phone")
        return SimLink(address, self.ring_after)

    async def scan(self, seconds):
        return [Nearby("11:22:33:44:55:66", "Pixel 9", False)]

    async def pair(self, address, confirm):
        if not await confirm("123456"):
            raise StackError("refused", "the code was not confirmed")
        self.known[address] = "Paired Phone"
        return "Paired Phone"

    async def forget(self, address):
        self.known.pop(address, None)

    def info(self):
        return {"stack": "simulated", "controller_address": "00:00:00:00:00:00", "hci_version": 0}

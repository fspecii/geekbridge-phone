# SPDX-License-Identifier: GPL-3.0-or-later
"""Stand-ins for the Bluetooth stack and a phone, for the helper's offline tests."""
import asyncio

from gbp_btd.stack import CallRow, Channel, HfpSession, Link, Nearby, Stack, StackError


class FakeSession(HfpSession):
    def __init__(self):
        self.commands = []
        self.sent_voice = []
        self.calls_rows = []
        self.closed = False

    async def answer(self):
        self.commands.append("answer")

    async def reject(self):
        self.commands.append("reject")

    async def hang_up(self):
        self.commands.append("hang_up")

    async def dial(self, number):
        self.commands.append(("dial", number))

    async def send_tones(self, tones):
        self.commands.append(("tones", tones))

    async def query_calls(self):
        return list(self.calls_rows)

    def send_voice(self, payload):
        self.sent_voice.append(payload)

    async def close(self):
        self.closed = True


class FakeChannel(Channel):
    def __init__(self, auto_reply=None):
        self.incoming = asyncio.Queue()
        self.written = []
        self.closed = False
        self.auto_reply = auto_reply  # function(bytes written) -> bytes to feed back, or None

    def feed(self, data):
        self.incoming.put_nowait(data)

    async def read(self):
        return await self.incoming.get()

    async def write(self, data):
        self.written.append(data)
        if self.auto_reply:
            reply = self.auto_reply(data)
            if reply:
                self.incoming.put_nowait(reply)

    async def close(self):
        if not self.closed:
            self.closed = True
            self.incoming.put_nowait(b"")


class FakeLink(Link):
    def __init__(self, address, stack):
        self.address, self.stack = address, stack
        self.session = FakeSession()
        self.channels = []
        self.closed = False

    async def open_hfp(self):
        if self.stack.hfp_failures:
            raise self.stack.hfp_failures.pop(0)
        return self.session

    async def open_channel(self, kind):
        if kind in self.stack.missing_services:
            raise StackError("no_service", f"no {kind} service")
        channel = FakeChannel(self.stack.channel_reply)
        self.channels.append((kind, channel))
        return channel

    async def close(self):
        self.closed = True

    def lose(self):
        if self.on_lost:
            self.on_lost()


class FakeStack(Stack):
    def __init__(self, known=None):
        self.known = dict(known or {"6C:AC:C2:00:00:01": "Sam's Z Fold5"})
        self.connect_failures = []
        self.hfp_failures = []
        self.missing_services = set()
        self.links = []
        self.connects = 0
        self.started = self.stopped = False
        self.nearby = [Nearby("11:22:33:44:55:66", "Pixel 9", False)]
        self.scan_error = None
        self.pair_error = None
        self.pair_code = "123456"
        self.forgotten = []
        self.channel_reply = None

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    def known_phones(self):
        return list(self.known.items())

    async def connect(self, address):
        self.connects += 1
        if self.connect_failures:
            raise self.connect_failures.pop(0)
        link = FakeLink(address, self)
        self.links.append(link)
        return link

    async def scan(self, seconds):
        if self.scan_error:
            raise self.scan_error
        return list(self.nearby)

    async def pair(self, address, confirm):
        if self.pair_error:
            raise self.pair_error
        if not await confirm(self.pair_code):
            raise StackError("refused", "the code was not confirmed")
        self.known[address] = "Paired Phone"
        return "Paired Phone"

    async def forget(self, address):
        self.known.pop(address, None)
        self.forgotten.append(address)

    def info(self):
        return {"controller": "fake", "stack": "fake"}


async def settle(times=5):
    """Lets queued tasks and callbacks run."""
    for _ in range(times):
        await asyncio.sleep(0)


async def until(condition, timeout=2.0):
    end = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)

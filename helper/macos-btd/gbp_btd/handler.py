# SPDX-License-Identifier: GPL-3.0-or-later
"""Turns validated requests into actions. The server calls `handle`; nothing here touches a socket."""
import asyncio

from .server import HandlerError, Reply

PROTOCOL_VERSION = 1
HELPER_VERSION = "0.1.0"


class Handler:
    def __init__(self, stack, phones, services, pairing, port, request_stop, grace=15, log=None):
        self.stack, self.phones, self.services, self.pairing, self.port = stack, phones, services, pairing, port
        self.request_stop = request_stop  # called when the app has gone for `grace` seconds, or on shutdown
        self.grace = grace
        self.log = log or (lambda *a: None)
        self.gone_task = None
        self.seen_app = False  # becomes true at the first request from the app

    def _known(self):
        rows = []
        for address, name in self.stack.known_phones():
            connected = self.phones.selected == address and self.phones.state == "connected"
            rows.append({"address": address, "name": name or address, "connected": connected,
                         "battery": None, "apple": "iphone" in (name or "").lower()})
        return rows

    async def handle(self, op, params):
        self.seen_app = True
        if self.gone_task:
            self.gone_task.cancel()
            self.gone_task = None
        if op == "hello":
            return {"protocol": PROTOCOL_VERSION, "helper": HELPER_VERSION, "stack": self.stack.info()}
        if op == "status":
            return {**self.phones.snapshot(), "stack": self.stack.info(), "voice_port": self.port.stats()}
        if op == "phones":
            return {"phones": self._known()}
        if op == "select_phone":
            if params["address"] not in [a for a, _ in self.stack.known_phones()]:
                raise HandlerError("unknown_phone", "that phone is not paired with this app")
            await self.phones.select(params["address"])
            return {}
        if op == "calls":
            return {"calls": self.phones.calls()}
        if op == "dial":
            await self.phones.dial(params["number"])
            return {}
        if op == "answer":
            await self.phones.answer()
            return {}
        if op in ("hang_up", "hang_up_all"):
            await self.phones.hang_up()
            return {}
        if op == "tones":
            await self.phones.tones(params["tones"])
            return {}
        if op == "open_service":
            fd = await self.services.open(params["service"])
            return Reply({"service": params["service"]}, fd=fd)
        if op == "scan":
            return await self.pairing.scan(params["seconds"])
        if op == "pair":
            return await self.pairing.pair(params["address"])
        if op == "unpair":
            await self.pairing.unpair(params["address"])
            return {}
        if op == "shutdown":
            asyncio.get_running_loop().call_soon(self.request_stop)
            return {}
        raise HandlerError("unknown_op", op)

    async def client_gone(self):
        """The app disconnected. If it does not come back soon, stop: the guard then restores Bluetooth."""
        async def wait():
            await asyncio.sleep(self.grace)
            self.log("the app is gone: stopping")
            self.request_stop()

        self.gone_task = asyncio.ensure_future(wait())

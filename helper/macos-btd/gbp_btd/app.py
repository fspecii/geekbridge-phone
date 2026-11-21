# SPDX-License-Identifier: GPL-3.0-or-later
"""Assembles the helper: server, voice socket, managers, handler. Used by main.py and by the tests."""
import asyncio

from .handler import Handler
from .pairing import PairingManager
from .phone_manager import PhoneManager
from .server import Server
from .services import ServiceManager
from .voice import VoicePort, VoiceServer


class Helper:
    def __init__(self, directory, stack, grace=15, idle=60, log=None, backoff=(3, 6, 12, 24, 48), sleep=asyncio.sleep):
        self.log = log or (lambda *a: None)
        self.stop_event = asyncio.Event()
        self.stack = stack
        self.port = VoicePort(send=lambda frame: None)
        self.server = None  # created in start(), after the event loop exists
        self.directory, self.grace, self.backoff, self.sleep = directory, grace, backoff, sleep
        self.idle = idle  # seconds to wait for the app's first request before giving up

    async def start(self):
        self.stack_started = False
        self.server = Server(self.directory, handler=None, log=self.log)
        self.voice = VoiceServer(self.directory, self.port)
        self.phones = PhoneManager(self.stack, self.server.broadcast, self.port, sleep=self.sleep,
                                   backoff=self.backoff, log=self.log)
        self.services = ServiceManager(self.phones, self.log)
        self.pairing = PairingManager(self.stack, self.phones, self.server.broadcast)
        self.server.handler = Handler(self.stack, self.phones, self.services, self.pairing, self.port,
                                      request_stop=self.stop_event.set, grace=self.grace, log=self.log)
        self.stack.on_fault = lambda error: asyncio.ensure_future(self.phones.fault_from_outside(error))
        if hasattr(self.stack, "log"):
            self.stack.log = self.log
        await self.stack.start()
        self.stack_started = True
        await self.voice.start()
        await self.server.start()

    async def run(self):
        await self.start()
        try:
            stopper = asyncio.ensure_future(self.server.stopped.wait())
            requested = asyncio.ensure_future(self.stop_event.wait())
            watchdog = asyncio.ensure_future(self._no_app_came())
            await asyncio.wait({stopper, requested}, return_when=asyncio.FIRST_COMPLETED)
            stopper.cancel()
            requested.cancel()
            watchdog.cancel()
        finally:
            await self.stop()

    async def _no_app_came(self):
        """A helper nobody talks to would hold the controller for ever: stop if the app never shows up."""
        await asyncio.sleep(self.idle)
        if not self.server.handler.seen_app:
            self.log("no app connected: stopping")
            self.stop_event.set()

    async def stop(self):
        """Closes everything in an order that leaves the controller free for the app to give back."""
        if getattr(self, "phones", None):
            await self.services.close_all()
            await self.phones.stop()
        if self.server:
            await self.server.stop()
        if getattr(self, "voice", None):
            await self.voice.stop()
        if getattr(self, "stack_started", False):
            await self.stack.stop()
            self.stack_started = False

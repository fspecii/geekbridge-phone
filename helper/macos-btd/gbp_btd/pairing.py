# SPDX-License-Identifier: GPL-3.0-or-later
"""Finding phones and pairing with one. The numeric code goes to the app, which shows it; our side
confirms and the user compares on the phone, as on Windows."""
from .server import HandlerError
from .stack import StackError


class PairingManager:
    def __init__(self, stack, phones, emit):
        self.stack, self.phones, self.emit = stack, phones, emit
        self.busy = False

    async def scan(self, seconds):
        async with self._exclusive():
            async with self.phones.paused():
                try:
                    found = await self.stack.scan(seconds)
                except StackError as error:
                    raise HandlerError(error.code, error.message) from None
        return {"found": [{"id": n.address, "name": n.name, "paired": n.paired} for n in found]}

    async def pair(self, address):
        async with self._exclusive():
            async with self.phones.paused():
                async def show(code):
                    await self.emit("pairing_code", address=address, code=code)
                    return True

                try:
                    name = await self.stack.pair(address, show)
                except StackError as error:
                    raise HandlerError(error.code, error.message) from None
        await self.emit("pairing_done", address=address, phone_name=name or "")
        return {"address": address, "name": name or ""}

    async def unpair(self, address):
        if self.phones.selected == address:
            await self.phones.stop()
            self.phones.selected = None
        await self.stack.forget(address)

    def _exclusive(self):
        manager = self

        class Guard:
            async def __aenter__(self):
                if manager.busy:
                    raise HandlerError("busy", "a scan or pairing is already running")
                manager.busy = True

            async def __aexit__(self, *exc):
                manager.busy = False

        return Guard()

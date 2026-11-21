# SPDX-License-Identifier: GPL-3.0-or-later
"""MAP and PBAP channels for the app: a service channel on the phone, handed over as a socket descriptor.

The app's existing OBEX code reads and writes the descriptor unchanged. One channel per service
at a time (phones allow one message session at a time): opening again replaces the old one.
"""
import asyncio
import contextlib
import socket

from .server import HandlerError
from .stack import StackError

CHUNK = 4096


class ServiceManager:
    def __init__(self, phones, log=None):
        self.phones = phones
        self.log = log or (lambda *a: None)
        self.open_channels = {}  # kind -> (channel, helper_socket, tasks)

    async def open(self, kind):
        link = self.phones.link
        if link is None:
            raise HandlerError("no_phone", "no phone is connected")
        await self.close(kind)
        try:
            channel = await asyncio.wait_for(link.open_channel(kind), 20)
        except StackError as error:
            raise HandlerError(error.code if error.code in ("no_service", "timeout", "busy") else "link_failed",
                               error.message) from None
        except asyncio.TimeoutError:
            raise HandlerError("timeout", "the phone did not open the service") from None
        helper_end, app_end = socket.socketpair()
        helper_end.setblocking(False)
        loop = asyncio.get_running_loop()
        tasks = [asyncio.ensure_future(self._to_app(channel, helper_end, loop, kind)),
                 asyncio.ensure_future(self._to_phone(channel, helper_end, loop, kind))]
        self.open_channels[kind] = (channel, helper_end, tasks)
        return app_end.detach()  # the server passes it on and then closes it

    async def _to_app(self, channel, sock, loop, kind):
        try:
            while True:
                data = await channel.read()
                if not data:
                    break
                await loop.sock_sendall(sock, data)
        except (OSError, StackError):
            pass
        finally:
            await self._finished(kind, channel)

    async def _to_phone(self, channel, sock, loop, kind):
        try:
            while True:
                data = await loop.sock_recv(sock, CHUNK)
                if not data:
                    break
                await channel.write(data)
        except (OSError, StackError):
            pass
        finally:
            await self._finished(kind, channel)

    async def _finished(self, kind, channel):
        current = self.open_channels.get(kind)
        if current and current[0] is channel:
            await self.close(kind)

    async def close(self, kind):
        entry = self.open_channels.pop(kind, None)
        if not entry:
            return
        channel, sock, tasks = entry
        me = asyncio.current_task()
        for task in tasks:
            if task is not me:
                task.cancel()
        with contextlib.suppress(OSError):
            sock.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(channel.close(), 3)

    async def close_all(self):
        for kind in list(self.open_channels):
            await self.close(kind)

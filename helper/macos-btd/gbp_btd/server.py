# SPDX-License-Identifier: GPL-3.0-or-later
"""The helper's socket server: one app connected at a time, requests answered as they finish.

Unix socket in a private folder (mode 700, socket mode 600). Requests are checked by
`protocol.parse_request`; a bad request gets an error reply and the connection stays up.
A handler may return a file descriptor with its reply: it is passed over the socket
(SCM_RIGHTS) in the same message, which is how MAP and PBAP channels reach the app.
"""
import asyncio
import os
import socket
import stat
import sys

from . import protocol
from .protocol import ProtocolError

MAX_IN_FLIGHT = 8
SOCKET_PATH_LIMIT = 100  # macOS allows 104 bytes in a Unix socket path


class HandlerError(Exception):
    """A request that failed for a reason the app should see."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class Reply:
    """What a handler returns: fields for the reply, and optionally a descriptor to pass."""

    def __init__(self, fields=None, fd=None):
        self.fields, self.fd = fields or {}, fd


def prepare_directory(path):
    """Creates the folder (mode 700) or checks an existing one: ours, not a link, no access for others."""
    path = os.path.abspath(path)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"{path} is not a plain folder")
    if info.st_uid != os.getuid():
        raise RuntimeError(f"{path} belongs to another user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError(f"{path} is accessible to others (mode {oct(stat.S_IMODE(info.st_mode))})")
    return path


class Connection:
    """A raw non-blocking socket with line reading and descriptor sending."""

    def __init__(self, sock, loop):
        self.sock, self.loop = sock, loop
        self.buffer = bytearray()
        self.send_lock = asyncio.Lock()
        self.closed = False

    async def read_line(self):
        """The next line (without the newline), or None when the app has gone."""
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self.buffer[:newline])
                del self.buffer[:newline + 1]
                return line
            if len(self.buffer) > protocol.MAX_LINE:
                raise ProtocolError("too_large", "message is too long")
            try:
                chunk = await self.loop.sock_recv(self.sock, 65536)
            except OSError:
                return None
            if not chunk:
                return None
            self.buffer += chunk

    async def send(self, message, fd=None):
        data = protocol.encode(message)
        async with self.send_lock:
            if self.closed:
                return
            try:
                if fd is None:
                    await self.loop.sock_sendall(self.sock, data)
                else:
                    # The descriptor travels with the first bytes of the reply.
                    while True:
                        try:
                            sent = socket.send_fds(self.sock, [data], [fd])
                            break
                        except BlockingIOError:
                            await asyncio.sleep(0.001)
                    if sent < len(data):
                        await self.loop.sock_sendall(self.sock, data[sent:])
            except OSError:
                self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.sock.close()
            except OSError:
                pass


class Server:
    def __init__(self, directory, handler, name="btd.sock", log=None):
        self.directory, self.handler, self.name = directory, handler, name
        self.log = log or (lambda *a: print(*a, file=sys.stderr, flush=True))
        self.listener = None
        self.client = None
        self.accept_task = None
        self.client_task = None
        self.stopped = asyncio.Event()
        self.path = None

    async def start(self):
        directory = prepare_directory(self.directory)
        self.path = os.path.join(directory, self.name)
        if len(self.path.encode()) > SOCKET_PATH_LIMIT:
            raise RuntimeError("the socket path is too long; use a shorter folder")
        if os.path.lexists(self.path):
            os.unlink(self.path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self.path)
        os.chmod(self.path, 0o600)
        listener.listen(1)
        listener.setblocking(False)
        self.listener = listener
        self.accept_task = asyncio.ensure_future(self._accept_loop())

    async def _accept_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            try:
                sock, _ = await loop.sock_accept(self.listener)
            except (OSError, asyncio.CancelledError):
                return
            sock.setblocking(False)
            connection = Connection(sock, loop)
            if self.client is not None and not self.client.closed:
                await connection.send(protocol.error(0, "busy", "another app is already connected"))
                connection.close()
                continue
            self.client = connection
            self.client_task = asyncio.ensure_future(self._serve(connection))

    async def _serve(self, connection):
        in_flight = set()
        try:
            while True:
                try:
                    line = await connection.read_line()
                except ProtocolError as error:
                    await connection.send(protocol.error(0, error.code, error.message))
                    return  # a runaway line: drop the app, it can reconnect
                if line is None:
                    return
                if not line.strip():
                    continue
                try:
                    request_id, op, params = protocol.parse_request(line)
                except ProtocolError as error:
                    await connection.send(protocol.error(0, error.code, error.message))
                    continue
                if len(in_flight) >= MAX_IN_FLIGHT:
                    await connection.send(protocol.error(request_id, "busy", "too many requests at once"))
                    continue
                task = asyncio.ensure_future(self._run(connection, request_id, op, params))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
        finally:
            for task in tuple(in_flight):
                task.cancel()
            await asyncio.gather(*in_flight, return_exceptions=True)
            connection.close()
            if self.client is connection:
                self.client = None
            hook = getattr(self.handler, "client_gone", None)
            if hook:
                try:
                    await hook()
                except Exception as error:
                    self.log("client_gone failed:", type(error).__name__)

    async def _run(self, connection, request_id, op, params):
        fd = None
        try:
            result = await self.handler.handle(op, params)
            fields = result.fields if isinstance(result, Reply) else (result or {})
            fd = result.fd if isinstance(result, Reply) else None
            if fd is not None:
                fields = {**fields, "fd": True}  # tells the app a descriptor travels with this reply
            await connection.send(protocol.ok(request_id, **fields), fd=fd)
            if op == "shutdown":
                self.stopped.set()
        except HandlerError as error:
            await connection.send(protocol.error(request_id, error.code, error.message))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.log("request failed:", op, type(error).__name__)
            await connection.send(protocol.error(request_id, "internal", "the helper hit an unexpected error"))
        finally:
            if fd is not None:
                try:
                    os.close(fd)  # the app has its own copy now
                except OSError:
                    pass

    async def broadcast(self, event_name, /, **fields):
        """Sends an event to the app, if one is connected."""
        connection = self.client
        if connection is not None and not connection.closed:
            await connection.send(protocol.event(event_name, **fields))

    async def stop(self):
        if self.accept_task:
            self.accept_task.cancel()
            await asyncio.gather(self.accept_task, return_exceptions=True)
        if self.client:
            self.client.close()
        if self.client_task:
            self.client_task.cancel()
            await asyncio.gather(self.client_task, return_exceptions=True)
        if self.listener:
            self.listener.close()
        if self.path and os.path.lexists(self.path):
            os.unlink(self.path)

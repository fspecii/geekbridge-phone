# SPDX-License-Identifier: GPL-3.0-or-later
"""What the helper needs from a Bluetooth stack. The real one is in bumble_stack.py; tests use a fake.

Keeping this small is what lets the managers be tested without any radio.
"""
from dataclasses import dataclass


class StackError(Exception):
    """A stack failure. `code` is one of: link_failed, auth_failed, no_service, timeout, fault, busy, refused."""

    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code, self.message = code, message or code


@dataclass(frozen=True)
class Nearby:
    address: str
    name: str
    paired: bool = False


@dataclass(frozen=True)
class CallRow:
    number: str = ""
    name: str = ""


class HfpSession:
    """One phone's hands-free link. The manager sets the three callbacks."""

    on_indicator = None  # (name: str, value: int)
    on_caller = None     # (number: str)
    on_voice = None      # (data: bytes, ok: bool) for each voice packet received

    async def answer(self): raise NotImplementedError
    async def reject(self): raise NotImplementedError
    async def hang_up(self): raise NotImplementedError
    async def dial(self, number): raise NotImplementedError
    async def send_tones(self, tones): raise NotImplementedError
    async def query_calls(self): raise NotImplementedError  # -> list[CallRow]
    def send_voice(self, payload): raise NotImplementedError
    async def close(self): raise NotImplementedError


class Channel:
    """A byte channel to a service on the phone (MAP or PBAP over RFCOMM)."""

    async def read(self): raise NotImplementedError   # b"" when closed
    async def write(self, data): raise NotImplementedError
    async def close(self): raise NotImplementedError


class Link:
    """A connected, authenticated, encrypted link to one phone."""

    address = ""
    on_lost = None  # callback, no arguments, set by the manager

    async def open_hfp(self): raise NotImplementedError      # -> HfpSession
    async def open_channel(self, kind): raise NotImplementedError  # kind "map" | "pbap" -> Channel
    async def close(self): raise NotImplementedError


class Stack:
    on_fault = None  # called with a StackError when the controller fails on its own

    async def start(self): raise NotImplementedError
    async def stop(self): raise NotImplementedError
    def known_phones(self): raise NotImplementedError   # -> list[(address, name)] with a stored key
    async def connect(self, address): raise NotImplementedError   # -> Link
    async def scan(self, seconds): raise NotImplementedError      # -> list[Nearby]
    async def pair(self, address, confirm): raise NotImplementedError  # confirm(code) awaited; -> name
    async def forget(self, address): raise NotImplementedError
    def info(self): raise NotImplementedError           # dict for `status`

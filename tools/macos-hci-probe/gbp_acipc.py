# SPDX-License-Identifier: GPL-3.0-or-later
"""Bumble transport over Apple's built-in Bluetooth controller, with a safety filter.

The native bridge (third_party/ai-caller/acipc_bridge.c, MIT) opens Apple's HCI, ACL and SCO
channels. This module puts a Bluetooth host stack on top of them and makes sure the stack can
only act on one phone: the target address given at start-up. The design follows the reference
project lzsnolimit/ai-caller (MIT); the code here is our own.

Safe to import without any hardware: the filter and the key store are plain Python.
"""
import asyncio
import ctypes
import json
import os
import tempfile
from pathlib import Path

from bumble import hci
from bumble.host import Host
from bumble.keys import KeyStore, PairingKeys

NATIVE_DIR = Path(os.environ.get("GBP_NATIVE_DIR", str(Path(__file__).resolve().parent / "build")))


def emit(stage, **values):
    """One JSON line per event, flushed, so a log shows how far a run got."""
    print(json.dumps({"stage": stage, **values}), flush=True)


class ScopeError(RuntimeError):
    """A packet would act outside the one selected phone."""


# Commands whose first parameter is a BD_ADDR: allowed only for the target.
ADDRESS_COMMANDS = {
    hci.HCI_CREATE_CONNECTION_COMMAND,
    hci.HCI_ACCEPT_CONNECTION_REQUEST_COMMAND,
    hci.HCI_REJECT_CONNECTION_REQUEST_COMMAND,
    hci.HCI_REMOTE_NAME_REQUEST_COMMAND,
    hci.HCI_LINK_KEY_REQUEST_REPLY_COMMAND,
    hci.HCI_LINK_KEY_REQUEST_NEGATIVE_REPLY_COMMAND,
    hci.HCI_IO_CAPABILITY_REQUEST_REPLY_COMMAND,
    hci.HCI_IO_CAPABILITY_REQUEST_NEGATIVE_REPLY_COMMAND,
    hci.HCI_USER_CONFIRMATION_REQUEST_REPLY_COMMAND,
    hci.HCI_USER_CONFIRMATION_REQUEST_NEGATIVE_REPLY_COMMAND,
    # Voice link (stage 3): accepting or refusing the phone's request names its address.
    hci.HCI_ACCEPT_SYNCHRONOUS_CONNECTION_REQUEST_COMMAND,
    hci.HCI_ENHANCED_ACCEPT_SYNCHRONOUS_CONNECTION_REQUEST_COMMAND,
    hci.HCI_REJECT_SYNCHRONOUS_CONNECTION_REQUEST_COMMAND,
}
# Commands whose first parameter is a connection handle: allowed only for the target's link.
HANDLE_COMMANDS = {
    hci.HCI_AUTHENTICATION_REQUESTED_COMMAND,
    hci.HCI_SET_CONNECTION_ENCRYPTION_COMMAND,
    hci.HCI_SETUP_SYNCHRONOUS_CONNECTION_COMMAND,
    hci.HCI_ENHANCED_SETUP_SYNCHRONOUS_CONNECTION_COMMAND,
    hci.HCI_DISCONNECT_COMMAND,
}
# Largest voice packet the controller accepts (its SCO buffer size, read in stage 1).
MAX_SCO_PAYLOAD = 254
# Never sent in stage 2: keys, PINs and pairing paths we do not use, controller-stored keys,
# out-of-band data, LE security, and discovery.
FORBIDDEN_COMMANDS = {
    hci.HCI_PIN_CODE_REQUEST_REPLY_COMMAND,
    hci.HCI_PIN_CODE_REQUEST_NEGATIVE_REPLY_COMMAND,
    hci.HCI_CHANGE_CONNECTION_LINK_KEY_COMMAND,
    hci.HCI_LINK_KEY_SELECTION_COMMAND,
    hci.HCI_USER_PASSKEY_REQUEST_REPLY_COMMAND,
    hci.HCI_USER_PASSKEY_REQUEST_NEGATIVE_REPLY_COMMAND,
    hci.HCI_REMOTE_OOB_DATA_REQUEST_REPLY_COMMAND,
    hci.HCI_REMOTE_OOB_DATA_REQUEST_NEGATIVE_REPLY_COMMAND,
    hci.HCI_REMOTE_OOB_EXTENDED_DATA_REQUEST_REPLY_COMMAND,
    hci.HCI_READ_STORED_LINK_KEY_COMMAND,
    hci.HCI_WRITE_STORED_LINK_KEY_COMMAND,
    hci.HCI_DELETE_STORED_LINK_KEY_COMMAND,
    hci.HCI_WRITE_AUTHENTICATION_ENABLE_COMMAND,
    hci.HCI_REFRESH_ENCRYPTION_KEY_COMMAND,
    hci.HCI_SEND_KEYPRESS_NOTIFICATION_COMMAND,
    hci.HCI_READ_LOCAL_OOB_DATA_COMMAND,
    hci.HCI_READ_LOCAL_OOB_EXTENDED_DATA_COMMAND,
    hci.HCI_WRITE_SIMPLE_PAIRING_DEBUG_MODE_COMMAND,
    hci.HCI_LE_ENABLE_ENCRYPTION_COMMAND,
    hci.HCI_LE_LONG_TERM_KEY_REQUEST_REPLY_COMMAND,
    hci.HCI_LE_LONG_TERM_KEY_REQUEST_NEGATIVE_REPLY_COMMAND,
    hci.HCI_LE_READ_LOCAL_P_256_PUBLIC_KEY_COMMAND,
    hci.HCI_LE_GENERATE_DHKEY_COMMAND,
    hci.HCI_LE_GENERATE_DHKEY_V2_COMMAND,
    hci.HCI_INQUIRY_COMMAND,
}

# Events that belong to one address (the address starts at this offset in the packet).
ADDRESS_EVENTS = {
    hci.HCI_LINK_KEY_REQUEST_EVENT: 3,
    hci.HCI_LINK_KEY_NOTIFICATION_EVENT: 3,
    hci.HCI_IO_CAPABILITY_REQUEST_EVENT: 3,
    hci.HCI_IO_CAPABILITY_RESPONSE_EVENT: 3,
    hci.HCI_USER_CONFIRMATION_REQUEST_EVENT: 3,
    hci.HCI_SIMPLE_PAIRING_COMPLETE_EVENT: 4,  # a status byte comes first
}
# Events that belong to one connection handle (the handle sits after a status byte).
HANDLE_EVENTS = {
    hci.HCI_AUTHENTICATION_COMPLETE_EVENT,
    hci.HCI_ENCRYPTION_CHANGE_EVENT,
    hci.HCI_ENCRYPTION_CHANGE_V2_EVENT,
}
# Pairing events we never expect: they stop the run.
UNEXPECTED_EVENTS = {
    hci.HCI_PIN_CODE_REQUEST_EVENT,
    hci.HCI_CHANGE_CONNECTION_LINK_KEY_COMPLETE_EVENT,
    hci.HCI_LINK_KEY_TYPE_CHANGED_EVENT,
    hci.HCI_RETURN_LINK_KEYS_EVENT,
    hci.HCI_ENCRYPTION_KEY_REFRESH_COMPLETE_EVENT,
    hci.HCI_REMOTE_OOB_DATA_REQUEST_EVENT,
    hci.HCI_USER_PASSKEY_REQUEST_EVENT,
    hci.HCI_USER_PASSKEY_NOTIFICATION_EVENT,
    hci.HCI_KEYPRESS_NOTIFICATION_EVENT,
}


class ScopeFilter:
    """Decides which packets may pass. Pure logic: no hardware."""

    def __init__(self, target):
        self.target = bytes(target)  # little-endian BD_ADDR bytes, as Bumble keeps them
        self.handle = None  # the target's ACL link
        self.sco_handle = None  # the target's voice link, once it exists

    def check_outbound(self, raw):
        raw = bytes(raw)
        if not raw or raw[0] not in (1, 2, 3):
            raise ScopeError("unsupported outbound packet type")
        if raw[0] == 2:
            if len(raw) < 5 or self.handle is None:
                raise ScopeError("ACL data before the link is known")
            if (int.from_bytes(raw[1:3], "little") & 0x0FFF) != self.handle:
                raise ScopeError("ACL data for another link")
            return
        if raw[0] == 3:
            if len(raw) < 4 or self.sco_handle is None:
                raise ScopeError("voice data without a voice link")
            if (int.from_bytes(raw[1:3], "little") & 0x0FFF) != self.sco_handle:
                raise ScopeError("voice data for another link")
            if raw[3] != len(raw) - 4 or raw[3] > MAX_SCO_PAYLOAD:
                raise ScopeError("voice packet length is not valid")
            return
        if len(raw) < 4:
            raise ScopeError("truncated command")
        opcode = int.from_bytes(raw[1:3], "little")
        params = raw[4:]
        if (opcode >> 10) == 0x3F:
            raise ScopeError("vendor command refused")
        if opcode in FORBIDDEN_COMMANDS:
            raise ScopeError(f"command 0x{opcode:04x} not allowed in this stage")
        if opcode in ADDRESS_COMMANDS:
            if params[:6] != self.target:
                raise ScopeError(f"command 0x{opcode:04x} names another address")
        elif opcode in HANDLE_COMMANDS:
            handle = int.from_bytes(params[:2], "little") & 0x0FFF
            allowed = {h for h in (self.handle, self.sco_handle) if h is not None}
            if opcode != hci.HCI_DISCONNECT_COMMAND:
                allowed = {self.handle} if self.handle is not None else set()
            if handle not in allowed:
                raise ScopeError(f"command 0x{opcode:04x} names another link")

    def check_inbound(self, packet):
        packet = bytes(packet)
        if not packet or packet[0] != 4 or len(packet) < 3:
            return
        event = packet[1]
        if event in UNEXPECTED_EVENTS:
            raise ScopeError(f"unexpected pairing event 0x{event:02x}")
        if event == hci.HCI_SYNCHRONOUS_CONNECTION_COMPLETE_EVENT:
            # status, handle(2), address(6), link type, ...
            if len(packet) < 13 or packet[6:12] != self.target:
                raise ScopeError("voice link for another address")
            if packet[3] == 0:
                if self.sco_handle is not None:
                    raise ScopeError("a second voice link is not allowed")
                self.sco_handle = int.from_bytes(packet[4:6], "little") & 0x0FFF
            return
        if event == hci.HCI_DISCONNECTION_COMPLETE_EVENT and len(packet) >= 7:
            if (int.from_bytes(packet[4:6], "little") & 0x0FFF) == self.sco_handle:
                self.sco_handle = None
            return
        if event in ADDRESS_EVENTS:
            start = ADDRESS_EVENTS[event]
            if packet[start:start + 6] != self.target:
                raise ScopeError(f"pairing event 0x{event:02x} for another address")
        elif event in HANDLE_EVENTS:
            if self.handle is None or int.from_bytes(packet[4:6], "little") != self.handle:
                raise ScopeError(f"event 0x{event:02x} for another link")


class TargetKeyStore(KeyStore):
    """Pairing key for the one target phone, in a private file (mode 600) outside the repo."""

    def __init__(self, controller, target, path=None):
        self.target = str(target)
        self.account = f"{controller}/{self.target}"
        self.path = Path(path or Path.home() / ".local/share/geekbridge-phone/mac-bt/pair-keys.json")
        self.failure = None

    def _entries(self):
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}

    def _save(self, entries):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".keys-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(entries, handle)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    async def get(self, name):
        if str(name) != self.target:
            return None
        entry = self._entries().get(self.account)
        if not entry:
            return None
        value = bytes.fromhex(entry["key"])
        if entry["type"] not in (5, 8) or len(value) != 16:
            raise RuntimeError("stored pairing key is not valid")
        return PairingKeys(link_key=PairingKeys.Key(value, True), link_key_type=entry["type"])

    async def update(self, name, keys):
        try:
            if str(name) != self.target:
                raise RuntimeError("refusing a pairing key for another address")
            link = keys.link_key
            if not link or not link.authenticated:
                raise RuntimeError("refusing a key that was not compared by the user")
            if keys.link_key_type not in (5, 8) or len(link.value) != 16:
                raise RuntimeError("refusing an unexpected key type")
            entries = self._entries()
            entries[self.account] = {"type": int(keys.link_key_type), "key": bytes(link.value).hex()}
            self._save(entries)
            emit("pairing-key-saved", authenticated=True)  # the key itself is never logged
        except Exception as error:
            self.failure = error
            raise

    async def delete(self, name):
        if str(name) != self.target:
            return
        entries = self._entries()
        if entries.pop(self.account, None) is not None:
            self._save(entries)

    async def get_all(self):
        keys = await self.get(self.target)
        return [(self.target, keys)] if keys else []

    async def delete_all(self):
        await self.delete(self.target)


class NoFirmwareHost(Host):
    """Standard initialisation only: no vendor driver, no firmware loading."""

    async def reset(self, driver_factory=None):
        await super().reset(driver_factory=None)


class AcipcTransport:
    """Bumble packet source and sink over the native bridge, behind a ScopeFilter."""

    def __init__(self, target, library=None):
        path = library or str(NATIVE_DIR / "libacipc_bridge.dylib")
        self.lib = ctypes.CDLL(path)
        self.lib.acipc_open.argtypes = []
        self.lib.acipc_open.restype = ctypes.c_int
        self.lib.acipc_close.argtypes = []
        self.lib.acipc_close.restype = None
        self.lib.acipc_send.argtypes = [ctypes.c_uint8, ctypes.c_void_p, ctypes.c_size_t]
        self.lib.acipc_send.restype = ctypes.c_int
        self.lib.acipc_recv.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_void_p,
                                       ctypes.c_size_t, ctypes.c_int]
        self.lib.acipc_recv.restype = ctypes.c_int
        self.filter = ScopeFilter(target)
        self.sink = None
        self.running = False
        self.failure = None
        self.pump_task = None
        self.sent = 0
        self.received = 0

    # Bumble transport source and sink protocol.
    def set_packet_sink(self, sink):
        self.sink = sink

    def on_packet(self, packet):
        raw = bytes(packet)
        try:
            self.filter.check_outbound(raw)
        except ScopeError as error:
            self.failure = error
            raise
        payload = ctypes.create_string_buffer(raw[1:])
        status = self.lib.acipc_send(raw[0], payload, len(raw) - 1)
        if status != 0:
            self.failure = RuntimeError(f"native send failed: {status}")
            raise self.failure
        self.sent += 1

    def set_handle(self, handle):
        self.filter.handle = handle

    def _read_one(self):
        kind = ctypes.c_uint8()
        buf = ctypes.create_string_buffer(4096)
        count = self.lib.acipc_recv(ctypes.byref(kind), buf, len(buf), 10)
        if count < 0:
            raise RuntimeError(f"native receive failed: {count}")
        return bytes([kind.value]) + buf.raw[:count] if count else None

    async def _pump(self):
        try:
            while self.running:
                packet = await asyncio.to_thread(self._read_one)
                if packet is None:
                    continue
                self.filter.check_inbound(packet)
                if packet[0] == 4 and packet[1] == hci.HCI_DISCONNECTION_COMPLETE_EVENT and len(packet) >= 7:
                    emit("link-disconnected", status=packet[3], reason=packet[6])
                self.received += 1
                if self.sink:
                    self.sink.on_packet(packet)
        except Exception as error:
            self.failure = error
            self.running = False

    async def start(self):
        status = self.lib.acipc_open()
        if status != 0:
            raise RuntimeError(f"native open failed: {status}")
        self.running = True
        self.pump_task = asyncio.create_task(self._pump())

    async def stop(self):
        self.running = False
        if self.pump_task:
            # The channels must not close while a native read is still running.
            await asyncio.gather(self.pump_task, return_exceptions=True)
        self.lib.acipc_close()
        emit("transport-closed", sent=self.sent, received=self.received)
        if self.failure:
            raise self.failure

    async def guarded(self, awaitable, timeout):
        """Wait for `awaitable`, but stop at once if the transport fails or time runs out."""
        task = asyncio.ensure_future(awaitable)
        try:
            end = asyncio.get_running_loop().time() + timeout
            while not task.done():
                if self.failure:
                    raise self.failure
                if asyncio.get_running_loop().time() >= end:
                    raise TimeoutError("operation exceeded its time limit")
                await asyncio.sleep(0.01)
            if self.failure:
                raise self.failure
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

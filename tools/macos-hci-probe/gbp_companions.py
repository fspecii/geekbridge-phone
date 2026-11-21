# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage 5: the stack hosts the user's own Bluetooth devices while it owns the controller.

While our stack holds the Mac's controller for a call, macOS cannot serve the mouse or the
headphones. So the stack serves them itself, for the length of the call, using the pairing keys
macOS already has (same controller address, same key: the device sees the host it knows).

This module holds the parts that need no hardware:

- CompanionScope: the safety filter for one phone plus a fixed list of companion devices.
  It keeps every rule of the one-phone filter (gbp_acipc.ScopeFilter) and adds the companions:
  links and pairing replies only for listed addresses, a voice link only with the phone,
  and a refusal for any stranger that tries to connect.
- CompanionKeyStore: imported keys are read-only. A companion that asks for a new pairing is
  refused, so a run can never replace the key macOS holds.
- MagicMouse: decodes the Magic Mouse 2 report (layout from the Linux driver hid-magicmouse.c).
"""
import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

from bumble import hci
from bumble.keys import KeyStore, PairingKeys

from gbp_acipc import (ADDRESS_COMMANDS, ADDRESS_EVENTS, FORBIDDEN_COMMANDS, HANDLE_COMMANDS, HANDLE_EVENTS,
                       MAX_SCO_PAYLOAD, UNEXPECTED_EVENTS, AcipcTransport, ScopeError, TargetKeyStore, emit)

VOICE_ACCEPT_COMMANDS = {
    hci.HCI_ACCEPT_SYNCHRONOUS_CONNECTION_REQUEST_COMMAND,
    hci.HCI_ENHANCED_ACCEPT_SYNCHRONOUS_CONNECTION_REQUEST_COMMAND,
}
VOICE_SETUP_COMMANDS = {
    hci.HCI_SETUP_SYNCHRONOUS_CONNECTION_COMMAND,
    hci.HCI_ENHANCED_SETUP_SYNCHRONOUS_CONNECTION_COMMAND,
}
# Saying no is safe toward any address; it is how a stranger is turned away.
REFUSAL_COMMANDS = {
    hci.HCI_REJECT_CONNECTION_REQUEST_COMMAND,
    hci.HCI_REJECT_SYNCHRONOUS_CONNECTION_REQUEST_COMMAND,
}
ACL_LINK = 1
REJECT_UNACCEPTABLE_ADDRESS = 0x0F
# Combination keys macOS may hold for a mouse or headphones: legacy (0), unauthenticated and
# authenticated P-192 (4, 5) and P-256 (7, 8).
IMPORTED_KEY_TYPES = (0, 4, 5, 7, 8)


def _command(opcode, params):
    return bytes([1]) + opcode.to_bytes(2, "little") + bytes([len(params)]) + params


class CompanionScope:
    """Decides which packets may pass, for one phone and a fixed set of companions. Pure logic."""

    def __init__(self, phone=None, companions=()):
        self.phone = bytes(phone) if phone is not None else None
        self.companions = {bytes(address) for address in companions}
        self.links = {}  # ACL handle -> address
        self.sco_handle = None
        self.scanning = False

    def allowed(self, address):
        address = bytes(address)
        return address == self.phone or address in self.companions

    def handle_of(self, address):
        address = bytes(address)
        return next((handle for handle, owner in self.links.items() if owner == address), None)

    def check_outbound(self, raw):
        raw = bytes(raw)
        if not raw or raw[0] not in (1, 2, 3):
            raise ScopeError("unsupported outbound packet type")
        if raw[0] == 2:
            if len(raw) < 5 or (int.from_bytes(raw[1:3], "little") & 0x0FFF) not in self.links:
                raise ScopeError("ACL data for a link that is not ours")
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
            raise ScopeError(f"command 0x{opcode:04x} is not allowed")
        if opcode in (hci.HCI_INQUIRY_COMMAND, hci.HCI_INQUIRY_CANCEL_COMMAND):
            if not self.scanning:
                raise ScopeError(f"command 0x{opcode:04x} is only allowed during a scan")
            return
        if opcode == hci.HCI_REMOTE_NAME_REQUEST_COMMAND:
            if not (self.scanning or self.allowed(params[:6])):
                raise ScopeError(f"command 0x{opcode:04x} names another address outside a scan")
            return
        if opcode in REFUSAL_COMMANDS:
            return
        if opcode in VOICE_ACCEPT_COMMANDS:
            if self.phone is None or params[:6] != self.phone:
                raise ScopeError("a voice link is only accepted from the phone")
            return
        if opcode in ADDRESS_COMMANDS:
            if not self.allowed(params[:6]):
                raise ScopeError(f"command 0x{opcode:04x} names another address")
            return
        if opcode in HANDLE_COMMANDS:
            handle = int.from_bytes(params[:2], "little") & 0x0FFF
            if opcode == hci.HCI_DISCONNECT_COMMAND and handle == self.sco_handle and handle is not None:
                return
            if handle not in self.links:
                raise ScopeError(f"command 0x{opcode:04x} names another link")
            if opcode in VOICE_SETUP_COMMANDS and self.links[handle] != self.phone:
                raise ScopeError("a voice link is only set up with the phone")

    def reject_for(self, packet):
        """A stranger asked to connect: returns the command that refuses it, or None.

        The caller sends the command and does not show the request to the stack, which would
        otherwise try to accept or refuse it by rules of its own."""
        packet = bytes(packet)
        if len(packet) < 13 or packet[0] != 4 or packet[1] != hci.HCI_CONNECTION_REQUEST_EVENT:
            return None
        address, link_type = packet[3:9], packet[12]
        if link_type == ACL_LINK:
            if self.allowed(address):
                return None
            return _command(hci.HCI_REJECT_CONNECTION_REQUEST_COMMAND, address + bytes([REJECT_UNACCEPTABLE_ADDRESS]))
        if self.phone is not None and address == self.phone and self.sco_handle is None:
            return None
        return _command(hci.HCI_REJECT_SYNCHRONOUS_CONNECTION_REQUEST_COMMAND,
                        address + bytes([REJECT_UNACCEPTABLE_ADDRESS]))

    def check_inbound(self, packet):
        packet = bytes(packet)
        if not packet or packet[0] != 4 or len(packet) < 3:
            return
        event = packet[1]
        if event == hci.HCI_ENCRYPTION_KEY_REFRESH_COMPLETE_EVENT:
            # A device that leads its link (a mouse does) renews the session's encryption key now
            # and then. The pairing key does not change. Seen on the first live mouse run.
            if len(packet) < 6 or (int.from_bytes(packet[4:6], "little") & 0x0FFF) not in self.links:
                raise ScopeError("encryption key refresh for another link")
            return
        if event in UNEXPECTED_EVENTS:
            raise ScopeError(f"unexpected pairing event 0x{event:02x}")
        if event == hci.HCI_CONNECTION_COMPLETE_EVENT and len(packet) >= 13:
            # status, handle(2), address(6), link type, encryption
            if packet[3] != 0:
                return
            handle = int.from_bytes(packet[4:6], "little") & 0x0FFF
            if packet[12] == ACL_LINK:
                if not self.allowed(packet[6:12]):
                    raise ScopeError("a link opened to an address that is not ours")
                self.links[handle] = packet[6:12]
            else:  # a plain SCO voice link reports here, not in the synchronous event
                if self.phone is None or packet[6:12] != self.phone:
                    raise ScopeError("voice link for an address that is not the phone")
                if self.sco_handle is not None:
                    raise ScopeError("a second voice link is not allowed")
                self.sco_handle = handle
            return
        if event == hci.HCI_SYNCHRONOUS_CONNECTION_COMPLETE_EVENT:
            if len(packet) < 13 or self.phone is None or packet[6:12] != self.phone:
                raise ScopeError("voice link for an address that is not the phone")
            if packet[3] == 0:
                if self.sco_handle is not None:
                    raise ScopeError("a second voice link is not allowed")
                self.sco_handle = int.from_bytes(packet[4:6], "little") & 0x0FFF
            return
        if event == hci.HCI_DISCONNECTION_COMPLETE_EVENT and len(packet) >= 7:
            handle = int.from_bytes(packet[4:6], "little") & 0x0FFF
            if handle == self.sco_handle:
                self.sco_handle = None
            self.links.pop(handle, None)
            return
        if event in ADDRESS_EVENTS:
            start = ADDRESS_EVENTS[event]
            if not self.allowed(packet[start:start + 6]):
                raise ScopeError(f"pairing event 0x{event:02x} for another address")
        elif event in HANDLE_EVENTS:
            if (int.from_bytes(packet[4:6], "little") & 0x0FFF) not in self.links:
                raise ScopeError(f"event 0x{event:02x} for another link")


class CompanionTransport(AcipcTransport):
    """The native transport behind a CompanionScope. A stranger's connection request is refused
    here and never reaches the stack."""

    def __init__(self, phone=None, companions=(), library=None):
        super().__init__(phone if phone is not None else bytes(6), library)
        self.filter = CompanionScope(phone, companions)
        self.refused = 0

    def set_handle(self, handle):
        """The scope learns links from the controller's own events; nothing to set."""

    def deliver(self, packet):
        """One packet from the controller: refuse, check, pass on. Returns True if the stack saw it."""
        refusal = self.filter.reject_for(packet)
        if refusal is not None:
            self.refused += 1
            emit("stranger-refused", count=self.refused)
            self.on_packet(refusal)
            return False
        self.filter.check_inbound(packet)
        self.received += 1
        if self.sink:
            self.sink.on_packet(packet)
        return True

    async def _pump(self):
        try:
            while self.running:
                packet = await asyncio.to_thread(self._read_one)
                if packet is not None:
                    self.deliver(packet)
        except Exception as error:
            self.failure = error
            self.running = False


class CompanionKeyStore(KeyStore):
    """Keys for the phone (paired by us, as before) and imported keys for the companions.

    Imported keys are never written, replaced or deleted here: they belong to macOS."""

    def __init__(self, controller, phone=None, companions=(), path=None):
        self.controller = str(controller)
        self.companions = {str(address) for address in companions}
        self.path = Path(path or Path.home() / ".local/share/geekbridge-phone/mac-bt/pair-keys.json")
        self.phone_store = TargetKeyStore(controller, phone, path) if phone is not None else None
        self.failure = None

    def _companion_entry(self, name):
        try:
            entries = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        return entries.get(f"{self.controller}/{name}")

    async def get(self, name):
        name = str(name)
        if self.phone_store and name == self.phone_store.target:
            return await self.phone_store.get(name)
        if name not in self.companions:
            return None
        entry = self._companion_entry(name)
        if not entry:
            return None
        value = bytes.fromhex(entry["key"])
        if entry.get("type") not in IMPORTED_KEY_TYPES or len(value) != 16:
            raise RuntimeError("imported pairing key is not valid")
        return PairingKeys(link_key=PairingKeys.Key(value, entry["type"] in (5, 8)), link_key_type=entry["type"])

    async def update(self, name, keys):
        if self.phone_store and str(name) == self.phone_store.target:
            return await self.phone_store.update(name, keys)
        self.failure = RuntimeError("refusing a new pairing key for a device macOS is paired with")
        raise self.failure

    async def delete(self, name):
        if self.phone_store and str(name) == self.phone_store.target:
            await self.phone_store.delete(name)

    async def get_all(self):
        rows = []
        for name in ([self.phone_store.target] if self.phone_store else []) + sorted(self.companions):
            keys = await self.get(name)
            if keys:
                rows.append((name, keys))
        return rows

    async def delete_all(self):
        if self.phone_store:
            await self.phone_store.delete_all()


def _s16(low, high):
    value = low | (high << 8)
    return value - 0x10000 if value & 0x8000 else value


def _s12(value):
    value &= 0xFFF
    return value - 0x1000 if value & 0x800 else value


@dataclass
class Touch:
    id: int
    x: int
    y: int
    down: bool


@dataclass
class MouseReport:
    dx: int = 0
    dy: int = 0
    left: bool = False
    right: bool = False
    touches: list = field(default_factory=list)


class MagicMouse:
    """Magic Mouse 2 and USB-C (products 0x0269, 0x0323) over classic HID.

    Until the host sends ENABLE_MULTITOUCH the mouse reports movement and clicks only. With it,
    report 0x12 also carries the finger positions on the shell, which is where scrolling comes
    from: the mouse has no wheel."""

    REPORT_ID = 0x12
    ENABLE_MULTITOUCH = bytes([0xF1, 0x02, 0x01])  # feature report, sent with SET_REPORT
    TOUCH_STATE_MASK = 0xF0
    SCROLL_STEP = 64  # shell units of finger travel per scroll line

    def __init__(self):
        self.last_y = {}
        self.scroll_remainder = 0

    @classmethod
    def decode(cls, report):
        """`report` starts at the report id. Returns a MouseReport, or None if it is not one of ours."""
        report = bytes(report)
        size = len(report)
        if not report or report[0] != cls.REPORT_ID:
            return None
        if size != 8 and (size < 14 or (size - 14) % 8):
            return None
        out = MouseReport(dx=_s16(report[2], report[3]), dy=_s16(report[4], report[5]),
                          left=bool(report[1] & 1), right=bool(report[1] & 2))
        for offset in range(14, size, 8):
            t = report[offset:offset + 8]
            out.touches.append(Touch(
                id=((t[6] << 2) | (t[5] >> 6)) & 0xF,
                x=_s12(((t[1] & 0x0F) << 8) | t[0]),
                y=-_s12((t[2] << 4) | (t[1] >> 4)),
                down=bool(t[7] & cls.TOUCH_STATE_MASK)))
        return out

    def scroll_lines(self, report):
        """Vertical scroll lines from one finger sliding on the shell; 0 for anything else."""
        down = [touch for touch in report.touches if touch.down]
        if len(down) != 1:
            self.last_y = {touch.id: touch.y for touch in down}
            self.scroll_remainder = 0
            return 0
        touch = down[0]
        previous = self.last_y.get(touch.id)
        self.last_y = {touch.id: touch.y}
        if previous is None:
            return 0
        self.scroll_remainder += touch.y - previous
        lines = int(self.scroll_remainder / self.SCROLL_STEP)
        self.scroll_remainder -= lines * self.SCROLL_STEP
        return lines

# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage 6: macOS keeps every Bluetooth device, and we sit between it and the chip.

`bluetoothd` can be started with BT_HCI_TRANSPORT naming a serial port (see
docs/MACOS-COEXIST.md). We give it a pseudo-terminal and pass every HCI packet between that
port and the real controller, which we hold through Apple's channels. macOS then serves the
mouse, the headphones and the rest with its own stack; later stages keep back only the
phone's voice link.

This module is the part with no hardware in it: the serial (H4) framing, and the policy that
decides what happens to each packet. Counters record packet kinds and opcodes, never content.
"""
import collections
import re
import time

COMMAND, ACL, SCO, EVENT = 1, 2, 3, 4
HEADER = {COMMAND: (3, 2, 1), ACL: (4, 2, 2), SCO: (3, 2, 1), EVENT: (2, 1, 1)}  # header size, length offset, length size

# Vendor commands that only make sense while booting a chip over a serial line. Apple's own
# driver has already loaded this controller's firmware and it sits on PCIe, so these are
# answered with success here and never reach it: a second firmware load could crash it.
SERIAL_BOOT_COMMANDS = {
    0xFC18: "update baud rate",
    0xFC2E: "download minidriver",
    0xFC45: "write UART clock setting",
    0xFC4C: "write RAM",
    0xFC4E: "launch RAM",
}


class FramingError(Exception):
    pass


class H4Parser:
    """Cuts a serial byte stream into HCI packets. Feed bytes, get whole packets back."""

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer += data
        packets = []
        while self.buffer:
            kind = self.buffer[0]
            if kind not in HEADER:
                raise FramingError(f"unknown packet type 0x{kind:02x}")
            header, offset, size = HEADER[kind]
            if len(self.buffer) < 1 + header:
                break
            length = int.from_bytes(self.buffer[1 + offset:1 + offset + size], "little")
            total = 1 + header + length
            if len(self.buffer) < total:
                break
            packets.append(bytes(self.buffer[:total]))
            del self.buffer[:total]
        return packets


def command_complete(opcode, status=0):
    """The event a controller sends when a command finished at once."""
    return bytes([EVENT, 0x0E, 4, 1]) + opcode.to_bytes(2, "little") + bytes([status])


# Hands-free signalling is plain text inside the data packets: commands from the Mac ("AT...")
# and answers from the phone ("+CIEV: ...", "OK"). Reading it shows why a phone does or does not
# hand its call audio over. Quoted text and runs of digits are masked, so no phone number or contact name is kept.
AT_LINE = re.compile(rb"(AT[+A-Z][ -~]{0,40}|\+[A-Z]{3,5}[:=][ -~]{0,40}|RING|OK|ERROR)")
DIGITS = re.compile(r"\d{5,}")
QUOTED = re.compile(r'"[^"]*"')  # numbers and contact names both arrive in quotes

CONNECTION_REQUEST, CONNECTION_COMPLETE, DISCONNECTION_COMPLETE = 0x04, 0x03, 0x05
COMMAND_STATUS, SYNCHRONOUS_COMPLETE = 0x0F, 0x2C
ACL_LINK_TYPE = 1
VOICE_ACCEPT_OPCODES = (0x0429, 0x043E)  # accept, and enhanced accept, synchronous connection request


class Policy:
    """What to do with each packet.

    Returns (action, packet): "forward" passes it on, "answer" sends `packet` back to the sender,
    "send" sends `packet` to the chip in place of the one received, "tap" is the phone's voice
    for us, and "drop" is ours to see and not the host's.

    With `phone` set (six address bytes, as they appear in HCI) the policy keeps that phone's
    voice link away from macOS: macOS's daemon would decline the request, so we accept it
    with the command `voice_accept(address, link_type)` builds, and nothing about that link
    is shown to the daemon. Everything else, the phone's other traffic included, passes."""

    def __init__(self, phone=None, voice_accept=None):
        self.to_chip = collections.Counter()
        self.to_host = collections.Counter()
        self.opcodes = collections.Counter()
        self.answered = collections.Counter()
        self.phone = bytes(phone) if phone is not None else None
        self.voice_accept = voice_accept
        self.voice_pending = False   # we accepted a request and wait for the link
        self.voice_handle = None
        self.voice = collections.Counter()
        # When things happened, in seconds since the start: the first live call got eleven voice
        # packets and then none, and the counts alone could not say why or when.
        self.clock = time.monotonic
        self.started = None
        self.timeline = []
        self.voice_seconds = set()
        self.other_voice = {}        # handle -> [packets from chip, first second, last second]
        self.hardware_errors = 0
        self.disconnect_reasons = collections.Counter()
        self.phone_link = None       # the phone's data link, learned from its connection event
        self.signalling = []         # [seconds, "mac" or "phone", line]

    def _signalling(self, packet, sender):
        if self.phone is None or self.phone_link is None or len(packet) < 5 or len(self.signalling) >= 300:
            return
        if (int.from_bytes(packet[1:3], "little") & 0x0FFF) != self.phone_link:
            return
        for match in AT_LINE.finditer(packet[5:]):
            line = DIGITS.sub("#", QUOTED.sub('"#"', match.group(0).decode("ascii", "replace").strip()))
            self.signalling.append([self._now(), sender, line])

    def _now(self):
        now = self.clock()
        if self.started is None:
            self.started = now
        return round(now - self.started, 2)

    def _note(self, what):
        if len(self.timeline) < 60:
            self.timeline.append([self._now(), what])

    def from_host(self, packet):
        kind = packet[0]
        if kind == COMMAND:
            opcode = int.from_bytes(packet[1:3], "little")
            self.opcodes[opcode] += 1
            if opcode in SERIAL_BOOT_COMMANDS:
                self.answered[opcode] += 1
                return "answer", command_complete(opcode)
        if kind == ACL:
            self._signalling(packet, "mac")
        self.to_chip[kind] += 1
        return "forward", packet

    def from_chip(self, packet):
        mine = self._voice_from_chip(packet) if self.phone is not None else None
        if mine is not None:
            return mine
        self._watch(packet)
        self.to_host[packet[0]] += 1
        return "forward", packet

    def _watch(self, packet):
        """Counts what the host's own links do, so a stalled call can be read against them."""
        kind = packet[0]
        if kind == ACL:
            self._signalling(packet, "phone")
        elif kind == EVENT and len(packet) >= 13 and packet[1] == CONNECTION_COMPLETE:
            # Every classic link is noted, the phone's or not: the app once showed the phone as
            # connected in a run where no link from the phone's address had come up here.
            mine = self.phone is not None and packet[6:12] == self.phone
            who = "phone" if mine else "another device"
            self._note(f"classic link to {who}: type {packet[12]}, status 0x{packet[3]:02x}")
            if mine and packet[3] == 0 and packet[12] == ACL_LINK_TYPE:
                self.phone_link = int.from_bytes(packet[4:6], "little") & 0x0FFF
        elif kind == EVENT and len(packet) >= 5 and packet[1] == 0x3E and packet[3] in (0x01, 0x0A) and packet[4] == 0:
            self.le_links = getattr(self, "le_links", 0) + 1
        if kind == SCO and len(packet) >= 4:
            handle = int.from_bytes(packet[1:3], "little") & 0x0FFF
            now = self._now()
            row = self.other_voice.setdefault(handle, [0, now, now])
            row[0] += 1
            row[2] = now
        elif kind == EVENT and len(packet) >= 3:
            if packet[1] == 0x10:
                self.hardware_errors += 1
                self._note("hardware error event")
            elif packet[1] == DISCONNECTION_COMPLETE and len(packet) >= 7:
                self.disconnect_reasons[f"0x{packet[6]:02x}"] += 1
            elif packet[1] == SYNCHRONOUS_COMPLETE and len(packet) >= 6:
                self._note(f"host voice link {'opened' if packet[3] == 0 else 'failed'}")

    def _voice_from_chip(self, packet):
        kind = packet[0]
        if kind == SCO and self.voice_handle is not None and len(packet) >= 4:
            if (int.from_bytes(packet[1:3], "little") & 0x0FFF) == self.voice_handle:
                self.voice["packets_in"] += 1
                if self.voice["packets_in"] == 1:
                    self._note("first phone voice packet")
                self.voice_seconds.add(int(self._now()))
                self.last_voice = self._now()
                return "tap", packet
            return None
        if kind != EVENT or len(packet) < 3:
            return None
        event = packet[1]
        if event == CONNECTION_REQUEST and len(packet) >= 13:
            if packet[3:9] == self.phone and packet[12] != ACL_LINK_TYPE:
                self.voice["requests"] += 1
                if self.voice_handle is not None or self.voice_pending:
                    return None  # a second voice link is the daemon's to decline
                self.voice_pending = True
                self._note(f"phone asked for voice (link type {packet[12]}); accepting")
                return "send", self.voice_accept(self.phone, packet[12])
        elif event == COMMAND_STATUS and len(packet) >= 7:
            if self.voice_pending and int.from_bytes(packet[5:7], "little") in VOICE_ACCEPT_OPCODES:
                if packet[3] != 0:
                    self.voice_pending = False
                    self.voice["accept_refused"] += 1
                self._note(f"accept answered with status 0x{packet[3]:02x}")
                return "drop", packet
        elif event in (SYNCHRONOUS_COMPLETE, CONNECTION_COMPLETE) and len(packet) >= 13:
            is_voice = event == SYNCHRONOUS_COMPLETE or packet[12] != ACL_LINK_TYPE
            if self.voice_pending and is_voice and packet[6:12] == self.phone:
                self.voice_pending = False
                if packet[3] == 0:
                    self.voice_handle = int.from_bytes(packet[4:6], "little") & 0x0FFF
                    self.voice["links_opened"] += 1
                    self._note("phone voice link open")
                else:
                    self.voice["links_failed"] += 1
                    self._note(f"phone voice link failed, status 0x{packet[3]:02x}")
                return "drop", packet
        elif event == DISCONNECTION_COMPLETE and len(packet) >= 7:
            if self.voice_handle is not None and (int.from_bytes(packet[4:6], "little") & 0x0FFF) == self.voice_handle:
                self.voice_handle = None
                self.voice["links_closed"] += 1
                self._note(f"phone voice link closed, reason 0x{packet[6]:02x}")
                return "drop", packet
        return None

    def voice_reply(self, packet):
        """Silence of the same size, for the same link: one packet back for each packet in."""
        self.voice["packets_out"] += 1
        return packet[:3] + bytes([packet[3]]) + bytes(packet[3])

    def summary(self):
        names = {COMMAND: "commands", ACL: "data", SCO: "voice", EVENT: "events"}
        vendor = sum(count for opcode, count in self.opcodes.items() if opcode >> 10 == 0x3F)
        return {
            "to_chip": {names[k]: v for k, v in sorted(self.to_chip.items())},
            "to_host": {names[k]: v for k, v in sorted(self.to_host.items())},
            "distinct_commands": len(self.opcodes),
            "vendor_commands": vendor,
            "answered_here": {f"0x{opcode:04x}": count for opcode, count in sorted(self.answered.items())},
            "first_commands": [f"0x{opcode:04x}" for opcode in list(self.opcodes)[:12]],
            "phone_voice": dict(self.voice),
            "phone_voice_seconds": len(self.voice_seconds),
            "phone_voice_last_packet_at": getattr(self, "last_voice", None),
            "host_voice_links": {f"0x{handle:03x}": {"packets": row[0], "from": row[1], "to": row[2]}
                                 for handle, row in self.other_voice.items()},
            "hardware_errors": self.hardware_errors,
            "disconnect_reasons": dict(self.disconnect_reasons),
            "timeline": self.timeline,
            "low_energy_links_opened": getattr(self, "le_links", 0),
            "hands_free_signalling": self.signalling,
            "ran_seconds": self._now(),
        }

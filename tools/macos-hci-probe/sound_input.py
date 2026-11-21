# SPDX-License-Identifier: GPL-3.0-or-later
"""Keeps the Mac's sound input off Bluetooth headsets while a call can come in.

macOS makes a newly connected headset the default input, and anything listening on the default
input then opens a voice link to the headset. The controller carries one voice link well, and
the phone's call needs it (see docs/MACOS-COEXIST.md). So while the pass-through runs, the
default input is moved to a microphone that is not on Bluetooth, if there is one, and put back
afterwards. Seen live: with the headset as input the phone kept the call's sound to itself.
"""
import ctypes
import ctypes.util
import struct

SYSTEM_OBJECT = 1
BLUETOOTH = {b"blue", b"blea"}


def fourcc(text):
    return struct.unpack(">I", text.encode())[0]


class Address(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


def choose(current, devices):
    """Pure logic. `devices` is a list of (id, transport bytes, input channels). Returns the device
    to switch the input to, or None to leave it alone."""
    by_id = {device: (transport, channels) for device, transport, channels in devices}
    if current not in by_id or by_id[current][0] not in BLUETOOTH:
        return None
    others = [device for device, transport, channels in devices if channels > 0 and transport not in BLUETOOTH]
    return others[0] if others else None


class Inputs:
    def __init__(self):
        self.ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
        self.original = None
        self.switches = 0

    def _get(self, obj, selector, scope, kind, count=1):
        address = Address(fourcc(selector), fourcc(scope), 0)
        size = ctypes.c_uint32(0)
        if self.ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(address), 0, None, ctypes.byref(size)):
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if self.ca.AudioObjectGetPropertyData(obj, ctypes.byref(address), 0, None, ctypes.byref(size), buffer):
            return None
        return buffer.raw[:size.value]

    def default_input(self):
        raw = self._get(SYSTEM_OBJECT, "dIn ", "glob", None)
        return struct.unpack("I", raw[:4])[0] if raw else None

    def devices(self):
        raw = self._get(SYSTEM_OBJECT, "dev#", "glob", None) or b""
        rows = []
        for (device,) in struct.iter_unpack("I", raw[:len(raw) // 4 * 4]):
            transport = self._get(device, "tran", "glob", None)
            config = self._get(device, "slay", "inpt", None) or b""
            # AudioBufferList: a count, then per buffer: channels (u32), size (u32), pointer (8 bytes).
            channels = 0
            if len(config) >= 4:
                buffers = struct.unpack("I", config[:4])[0]
                for index in range(buffers):
                    offset = 8 + index * 16
                    if offset + 4 <= len(config):
                        channels += struct.unpack("I", config[offset:offset + 4])[0]
            rows.append((device, struct.pack(">I", struct.unpack("I", transport[:4])[0]) if transport else b"", channels))
        return rows

    def set_default(self, device):
        address = Address(fourcc("dIn "), fourcc("glob"), 0)
        value = ctypes.c_uint32(device)
        return self.ca.AudioObjectSetPropertyData(SYSTEM_OBJECT, ctypes.byref(address), 0, None, 4, ctypes.byref(value)) == 0

    def keep_off_bluetooth(self):
        """Call now and then. Returns True if it just moved the input."""
        current = self.default_input()
        target = choose(current, self.devices())
        if target is None:
            return False
        if self.original is None:
            self.original = current
        if self.set_default(target):
            self.switches += 1
            return True
        return False

    def restore(self):
        if self.original is not None:
            self.set_default(self.original)
            self.original = None

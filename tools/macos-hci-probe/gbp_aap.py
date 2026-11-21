# SPDX-License-Identifier: GPL-3.0-or-later
"""AirPods microphone over Apple's accessory channel (AAP), as far as it is publicly understood.

AirPods can send their microphone as AAC-ELD access units over the AAP control channel
(L2CAP PSM 0x1001) while A2DP keeps playing. That gives a headset microphone with no voice
(SCO) link, which matters here: the controller's one voice link is taken by the phone call.

The byte layouts come from LibrePods pull request 655 and the airpods-helper project, both
taken from packet captures and confirmed there on AirPods Pro 3 only. Most header fields are
not understood; this module relies only on the ones every known implementation checks.
Pure logic: no Bluetooth.
"""
PSM = 0x1001
HEADER = bytes([0x04, 0x00, 0x04, 0x00])
HANDSHAKE = bytes([0x00, 0x00, 0x04, 0x00, 0x01, 0x00, 0x02, 0x00]) + bytes(8)
SET_FEATURES = HEADER + bytes([0x4D, 0x00, 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
SUBSCRIBE = HEADER + bytes([0x0F, 0x00, 0xFF, 0xFF, 0xFF, 0xFF])
MIC_OPCODE = 0x58
MIC_START = HEADER + bytes([MIC_OPCODE, 0x00, 0x00, 0x00, 0x09, 0x00, 0x00, 0x01, 0x82, 0x00, 0x00, 0x00, 0x04, 0x96, 0x00])
MIC_STOP = HEADER + bytes([MIC_OPCODE, 0x00, 0x00, 0x00, 0x02, 0x00, 0x03, 0x01])
AUDIO_SUBTYPE = 0x0001
AUDIO_HEADER = 22  # bytes before the first access unit; only the first 8 are understood
UNIT_PREFIX = 5    # timestamp (u32 LE) and length (u8) before each access unit
# AAC-ELD stream description for a decoder: object type 39, mono, 480-sample frames.
ELD_CONFIG = bytes([0xF8, 0xE6, 0x30, 0x00])
ELD_FRAME_SAMPLES = 480


def is_audio(sdu):
    sdu = bytes(sdu)
    return (len(sdu) >= 8 and sdu[:4] == HEADER and int.from_bytes(sdu[4:6], "little") == MIC_OPCODE
            and int.from_bytes(sdu[6:8], "little") == AUDIO_SUBTYPE)


def access_units(sdu):
    """The AAC-ELD access units in one audio message, in order. A cut-off last unit is dropped."""
    sdu = bytes(sdu)
    units, offset = [], AUDIO_HEADER
    while offset + UNIT_PREFIX <= len(sdu):
        start = offset + UNIT_PREFIX
        end = start + sdu[offset + 4]
        if end > len(sdu):
            break
        units.append(sdu[start:end])
        offset = end
    return units


def build_audio(units):
    """An audio message the way the AirPods send it; `units` is (timestamp, bytes) pairs. For tests."""
    sdu = bytearray(AUDIO_HEADER)
    sdu[0:4] = HEADER
    sdu[4:6] = MIC_OPCODE.to_bytes(2, "little")
    sdu[6:8] = AUDIO_SUBTYPE.to_bytes(2, "little")
    for timestamp, unit in units:
        if len(unit) > 255:
            raise ValueError("an access unit holds at most 255 bytes")
        sdu += timestamp.to_bytes(4, "little") + bytes([len(unit)]) + bytes(unit)
    return bytes(sdu)


class MicCounter:
    """What a run saw on the microphone stream: enough to tell a working stream from silence."""

    def __init__(self):
        self.messages = self.units = self.bytes = self.other = 0
        self.sizes = set()

    def on_sdu(self, sdu):
        if not is_audio(sdu):
            self.other += 1
            return
        self.messages += 1
        for unit in access_units(sdu):
            self.units += 1
            self.bytes += len(unit)
            self.sizes.add(len(unit))

    def summary(self, seconds):
        return {"audio_messages": self.messages, "access_units": self.units, "audio_bytes": self.bytes,
                "other_messages": self.other, "distinct_unit_sizes": len(self.sizes),
                "audio_seconds": round(self.units * ELD_FRAME_SAMPLES / 48000, 2), "listened_seconds": seconds}

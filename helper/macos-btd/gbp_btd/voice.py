# SPDX-License-Identifier: GPL-3.0-or-later
"""Voice between the helper and the app: 8 kHz 16-bit mono frames on a Unix socket.

Frame: magic(1) kind(1) length(2) sequence(4), then `length` bytes. Kind 1 is phone-to-Mac
audio (downlink), kind 2 is Mac-to-phone audio (uplink). The voice link itself (SCO) paces
everything: the helper sends one voice packet back for every one it receives, so the uplink
ring is read once per downlink packet.
"""
import asyncio
import os
import struct

from .ring import ByteRing
from .server import prepare_directory

MAGIC = 0xB7
DOWN, UP = 1, 2
HEADER = struct.Struct("!BBHI")
MAX_PAYLOAD = 512
SAMPLE_RATE = 8000


class FrameError(Exception):
    pass


def encode_frame(kind, sequence, payload):
    if kind not in (DOWN, UP) or len(payload) > MAX_PAYLOAD or len(payload) % 2:
        raise FrameError("frame not valid")
    return HEADER.pack(MAGIC, kind, len(payload), sequence & 0xFFFFFFFF) + payload


class FrameReader:
    """Feeds on socket bytes, yields whole frames. A bad header ends the stream: it cannot be resynchronised."""

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer += data
        frames = []
        while len(self.buffer) >= HEADER.size:
            magic, kind, length, sequence = HEADER.unpack_from(self.buffer)
            if magic != MAGIC or kind not in (DOWN, UP) or length > MAX_PAYLOAD or length % 2:
                raise FrameError("frame header not valid")
            if len(self.buffer) < HEADER.size + length:
                break
            frames.append((kind, sequence, bytes(self.buffer[HEADER.size:HEADER.size + length])))
            del self.buffer[:HEADER.size + length]
        return frames


class VoicePort:
    """Sits between the voice link and the app's audio socket."""

    def __init__(self, send, capacity_ms=200):
        self.send = send  # send(frame_bytes); may drop when no app is connected
        self.uplink_ring = ByteRing(SAMPLE_RATE * 2 * capacity_ms // 1000)
        self.sequence = 0
        self.down_frames = self.up_frames = self.up_bad = 0
        self.last_up_sequence = None
        self.up_gaps = 0

    def downlink(self, data):
        """A voice packet from the phone, for the Mac's speakers."""
        self.sequence += 1
        self.down_frames += 1
        try:
            self.send(encode_frame(DOWN, self.sequence, data))
        except FrameError:
            pass  # a malformed packet is dropped, not forwarded

    def uplink(self, length):
        """The next voice packet for the phone: microphone audio, or silence when there is none."""
        return self.uplink_ring.pull(length)

    def on_frame(self, kind, sequence, payload):
        if kind != UP:
            self.up_bad += 1
            return
        if self.last_up_sequence is not None and sequence != ((self.last_up_sequence + 1) & 0xFFFFFFFF):
            self.up_gaps += 1
        self.last_up_sequence = sequence
        self.up_frames += 1
        self.uplink_ring.push(payload)

    def reset(self):
        self.uplink_ring.clear()
        self.last_up_sequence = None

    def stats(self):
        return {"down_frames": self.down_frames, "up_frames": self.up_frames, "up_gaps": self.up_gaps,
                "up_bad": self.up_bad, "uplink_dropped_bytes": self.uplink_ring.dropped,
                "uplink_padded_bytes": self.uplink_ring.padded}


class VoiceServer:
    """The audio socket: one app at a time, frames in both directions."""

    def __init__(self, directory, port, name="voice.sock"):
        self.directory, self.port, self.name = directory, port, name
        self.path = None
        self.listener = None
        self.writer = None
        self.task = None
        port.send = self._send

    async def start(self):
        directory = prepare_directory(self.directory)
        self.path = os.path.join(directory, self.name)
        if os.path.lexists(self.path):
            os.unlink(self.path)
        self.listener = await asyncio.start_unix_server(self._client, path=self.path)
        os.chmod(self.path, 0o600)

    def _send(self, frame):
        writer = self.writer
        if writer is None or writer.is_closing():
            return
        # Never queue behind a stalled app: a late voice frame is worse than a dropped one.
        if writer.transport.get_write_buffer_size() > 16 * 1024:
            return
        writer.write(frame)

    async def _client(self, reader, writer):
        if self.writer is not None and not self.writer.is_closing():
            writer.close()
            return
        self.writer = writer
        self.port.reset()
        frames = FrameReader()
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                for kind, sequence, payload in frames.feed(data):
                    self.port.on_frame(kind, sequence, payload)
        except (FrameError, ConnectionError):
            pass
        finally:
            self.writer = None
            writer.close()

    async def stop(self):
        if self.writer is not None:
            self.writer.close()
        if self.listener:
            self.listener.close()
            await self.listener.wait_closed()
        if self.path and os.path.lexists(self.path):
            os.unlink(self.path)

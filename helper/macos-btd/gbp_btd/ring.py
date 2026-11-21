# SPDX-License-Identifier: GPL-3.0-or-later
"""A bounded byte FIFO for audio: overflow drops the oldest whole samples, a short read is padded with silence."""
import threading

SAMPLE = 2


class ByteRing:
    def __init__(self, capacity):
        self.capacity = capacity
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.dropped = 0
        self.padded = 0

    def push(self, data):
        with self.lock:
            self.buf += data
            over = len(self.buf) - self.capacity
            if over > 0:
                over += over % SAMPLE
                del self.buf[:over]
                self.dropped += over

    def pull(self, count):
        with self.lock:
            out = bytes(self.buf[:count])
            del self.buf[:count]
        if len(out) < count:
            self.padded += count - len(out)
            out += bytes(count - len(out))
        return out

    def size(self):
        with self.lock:
            return len(self.buf)

    def clear(self):
        with self.lock:
            self.buf.clear()

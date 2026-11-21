# SPDX-License-Identifier: GPL-3.0-or-later
"""Voice link (SCO) packet handling. Pure logic: no Bluetooth, no Bumble.

The controller has no flow control for voice, so the host sends exactly one packet back for
every packet it receives. That keeps our sending locked to the controller's own clock. The
design follows the reference project lzsnolimit/ai-caller (MIT); the code is ours.
"""
import time

MAX_PAYLOAD = 254  # the controller's SCO buffer size


class ScoStats:
    """Counts packets and records, second by second, whether traffic arrived."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.started = None
        self.rx = self.tx = self.bad_status = self.empty = self.odd = self.too_long = 0
        self.rx_by_second = {}

    def mark_rx(self):
        now = self.clock()
        if self.started is None:
            self.started = now
        second = int(now - self.started)
        self.rx_by_second[second] = self.rx_by_second.get(second, 0) + 1
        self.rx += 1

    def seconds_with_traffic(self):
        return sorted(s for s, n in self.rx_by_second.items() if n > 0)

    def gaps(self):
        """Seconds without a received packet between the first and the last one."""
        seen = self.seconds_with_traffic()
        if not seen:
            return []
        return [s for s in range(seen[0], seen[-1] + 1) if s not in self.rx_by_second]

    def summary(self):
        return {
            "rx": self.rx, "tx": self.tx, "bad_status": self.bad_status,
            "empty": self.empty, "odd_length": self.odd, "too_long": self.too_long,
            "seconds_with_traffic": len(self.seconds_with_traffic()), "gap_seconds": len(self.gaps()),
        }


class VoiceEcho:
    """For each received voice packet, send one packet of the same length back.

    `source(length)` supplies the outgoing audio (silence by default). In stage 3b it will
    return microphone audio. A packet that arrived damaged is answered with silence.
    """

    def __init__(self, send, source=None, clock=time.monotonic, sink=None):
        self.send = send  # send(handle, payload)
        self.source = source or (lambda length: bytes(length))
        self.sink = sink  # sink(payload): what the phone sent, for the speakers
        self.stats = ScoStats(clock)
        self.failure = None

    def on_packet(self, handle, data, status_ok=True):
        if self.failure:
            return
        self.stats.mark_rx()
        if not data:
            self.stats.empty += 1
            return
        if len(data) % 2:
            self.stats.odd += 1  # 16-bit samples: an odd length is not audio
            self.failure = ValueError("voice packet with an odd length")
            return
        if len(data) > MAX_PAYLOAD:
            self.stats.too_long += 1
            self.failure = ValueError("voice packet longer than the controller buffer")
            return
        if not status_ok:
            self.stats.bad_status += 1
        if self.sink:
            self.sink(data if status_ok else bytes(len(data)))
        out = self.source(len(data)) if status_ok else bytes(len(data))
        if len(out) != len(data):
            out = (bytes(out) + bytes(len(data)))[: len(data)]  # never send a different length
        self.send(handle, bytes(out))
        self.stats.tx += 1

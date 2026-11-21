# SPDX-License-Identifier: GPL-3.0-or-later
"""Sound for stage 3b: voice packets (8 kHz, 16-bit, mono) to and from the Mac's speakers and microphone.

Everything here except `open_streams` is plain logic and is tested without sound hardware. The
sound library is passed in, so tests use a stand-in.

Directions:
  downlink  phone -> Mac speakers: `downlink(data)` takes the packet payload, `speaker_callback` fills the speaker buffer.
  uplink    Mac microphone -> phone: `mic_callback` takes microphone samples, `uplink(length)` gives the next packet payload.
Buffers are bounded (about 200 ms). When one overflows, the oldest sound is dropped; when one
runs dry, silence is played. Both are counted and reported, never hidden.
"""
import audioop
import threading
import time

SCO_RATE = 8000
SAMPLE = 2  # bytes per 16-bit sample


class ByteRing:
    """A bounded byte FIFO. Overflow drops the oldest bytes; a short read is padded with silence."""

    def __init__(self, capacity):
        self.capacity = capacity
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.dropped = 0  # bytes lost to overflow
        self.padded = 0   # bytes of silence added because the buffer ran dry

    def push(self, data):
        with self.lock:
            self.buf += data
            over = len(self.buf) - self.capacity
            if over > 0:
                over += over % SAMPLE  # keep whole samples
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


class Resampler:
    """Mono 16-bit sample-rate conversion with state kept between calls.

    Going down by a whole factor (48000 to 8000) first averages each group of samples, which
    removes most of what would otherwise fold back into the speech band. Other ratios use
    audioop's converter.
    """

    def __init__(self, src_rate, dst_rate):
        self.src, self.dst = src_rate, dst_rate
        self.state = None
        self.factor = src_rate // dst_rate if src_rate % dst_rate == 0 and src_rate > dst_rate else 0
        self.carry = b""

    def process(self, data):
        if self.src == self.dst:
            return data
        if self.factor:
            data = self.carry + data
            group = self.factor * SAMPLE
            usable = len(data) - len(data) % group
            self.carry = data[usable:]
            samples = memoryview(data[:usable]).cast("h")
            out = bytearray()
            f = self.factor
            for i in range(0, len(samples), f):
                out += int(sum(samples[i:i + f]) / f).to_bytes(2, "little", signed=True)
            return bytes(out)
        out, self.state = audioop.ratecv(data, SAMPLE, 1, self.src, self.dst, self.state)
        return out


class Levels:
    """Loudness per direction, to see that sound really flows (0 to 32767)."""

    def __init__(self):
        self.peak_rms = 0
        self.samples = 0
        self.silent_blocks = 0
        self.blocks = 0

    def add(self, data):
        if not data:
            return
        rms = audioop.rms(data, SAMPLE)
        self.blocks += 1
        self.samples += len(data) // SAMPLE
        self.peak_rms = max(self.peak_rms, rms)
        if rms == 0:
            self.silent_blocks += 1


class AudioBridge:
    def __init__(self, in_rate=48000, out_rate=48000, out_channels=2, capacity_ms=200, prime_ms=60,
                 half_duplex=False, gate_rms=400, gate_hold=0.25, clock=time.monotonic):
        self.half_duplex, self.gate_rms, self.gate_hold, self.clock = half_duplex, gate_rms, gate_hold, clock
        self.gate_until = 0.0
        self.gated_packets = 0
        sco_capacity = SCO_RATE * SAMPLE * capacity_ms // 1000
        self.out_channels = out_channels
        self.to_speaker = ByteRing(out_rate * SAMPLE * capacity_ms // 1000)
        self.from_mic = ByteRing(sco_capacity)
        self.down_resampler = Resampler(SCO_RATE, out_rate)
        self.up_resampler = Resampler(in_rate, SCO_RATE)
        self.prime_bytes = out_rate * SAMPLE * prime_ms // 1000
        self.playing = False
        self.speaker_underruns = 0
        self.speaker_callbacks = 0
        self.packets_up = 0
        self.packets_down = 0
        self.down_levels, self.up_levels = Levels(), Levels()
        self.stream_errors = []
        self.started = False

    # Voice link side (event loop thread).
    def downlink(self, data):
        self.packets_down += 1
        self.down_levels.add(data)
        if self.half_duplex and data and audioop.rms(data, SAMPLE) >= self.gate_rms:
            self.gate_until = self.clock() + self.gate_hold  # the speakers are loud: mute the microphone
        self.to_speaker.push(self.down_resampler.process(data))

    def uplink(self, length):
        self.packets_up += 1
        out = self.from_mic.pull(length)  # always drained, so the buffer does not back up
        if self.half_duplex and self.clock() < self.gate_until:
            self.gated_packets += 1
            out = bytes(length)
        self.up_levels.add(out)
        return out

    # Sound device side (audio threads).
    def speaker_callback(self, frame_count):
        self.speaker_callbacks += 1
        need = frame_count * SAMPLE
        if not self.playing:
            if self.to_speaker.size() < self.prime_bytes:
                return bytes(need * self.out_channels)
            self.playing = True
        before = self.to_speaker.padded
        mono = self.to_speaker.pull(need)
        if self.to_speaker.padded != before:
            self.speaker_underruns += 1
        if self.out_channels == 2:
            return audioop.tostereo(mono, SAMPLE, 1, 1)
        return mono

    def mic_callback(self, data):
        self.from_mic.push(self.up_resampler.process(data))

    def stats(self):
        return {
            "packets_down": self.packets_down, "packets_up": self.packets_up,
            "speaker_underruns": self.speaker_underruns, "speaker_callbacks": self.speaker_callbacks,
            "speaker_dropped_bytes": self.to_speaker.dropped,
            "mic_dropped_bytes": self.from_mic.dropped,
            "mic_padded_bytes": self.from_mic.padded,
            "downlink_peak_rms": self.down_levels.peak_rms,
            "uplink_peak_rms": self.up_levels.peak_rms,
            "uplink_silent_packets": self.up_levels.silent_blocks,
            "stream_errors": len(self.stream_errors), "half_duplex": self.half_duplex, "gated_packets": self.gated_packets,
        }


class Streams:
    """The open sound streams and what they are called, so a run can report and close them."""

    def __init__(self, input_stream, output_stream, info):
        self.input_stream, self.output_stream, self.info = input_stream, output_stream, info

    def close(self):
        for stream in (self.input_stream, self.output_stream):
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass


def open_streams(sd, input_device=None, output_device=None, capacity_ms=200, prime_ms=60, allow_speakers=False,
                 half_duplex=False):
    """Opens the microphone and speaker, returns (AudioBridge, Streams). `sd` is the sounddevice module.

    The built-in speakers are refused unless `allow_speakers` is set: with Bluetooth off, they
    would feed the call back into the microphone.
    """
    in_info = sd.query_devices(input_device, "input")
    out_info = sd.query_devices(output_device, "output")
    if not allow_speakers and "speaker" in out_info["name"].lower():
        raise RuntimeError(f"the output is '{out_info['name']}': plug in wired headphones, or pass --allow-speakers")
    in_rate, out_rate = int(in_info["default_samplerate"]), int(out_info["default_samplerate"])
    channels = 2 if out_info["max_output_channels"] >= 2 else 1
    bridge = AudioBridge(in_rate, out_rate, channels, capacity_ms, prime_ms, half_duplex=half_duplex)

    def note(status):
        if status:
            bridge.stream_errors.append(str(status))

    def on_input(indata, frames, time_info, status):
        note(status)
        bridge.mic_callback(bytes(indata))

    def on_output(outdata, frames, time_info, status):
        note(status)
        outdata[:] = bridge.speaker_callback(frames)

    input_stream = sd.RawInputStream(samplerate=in_rate, channels=1, dtype="int16",
                                     device=input_device, latency="low", callback=on_input)
    output_stream = sd.RawOutputStream(samplerate=out_rate, channels=channels, dtype="int16",
                                       device=output_device, latency="low", callback=on_output)
    try:
        input_stream.start()
        output_stream.start()
    except BaseException:
        Streams(input_stream, output_stream, {}).close()
        raise
    bridge.started = True
    info = {"input": in_info["name"], "input_rate": in_rate,
            "output": out_info["name"], "output_rate": out_rate, "output_channels": channels,
            "half_duplex": half_duplex}
    return bridge, Streams(input_stream, output_stream, info)

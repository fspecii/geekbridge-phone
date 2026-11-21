# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline tests: the sound side of stage 3b, with a stand-in for the sound library."""
import math
import struct
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audioop

import audio_io
from audio_io import AudioBridge, ByteRing, Levels, Resampler, open_streams


def tone(rate, seconds, freq=440, amp=8000):
    n = int(rate * seconds)
    return b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / rate))) for i in range(n))


class ByteRingTests(unittest.TestCase):
    def test_fifo_order_and_exact_reads(self):
        ring = ByteRing(100)
        ring.push(b"\x01\x00" * 10)
        self.assertEqual(ring.pull(4), b"\x01\x00\x01\x00")
        self.assertEqual(ring.size(), 16)

    def test_overflow_drops_the_oldest_whole_samples_and_counts_them(self):
        ring = ByteRing(10)
        ring.push(bytes(range(14)))
        self.assertEqual(ring.size(), 10)
        self.assertEqual(ring.dropped, 4)
        self.assertEqual(ring.pull(2), bytes([4, 5]))  # the oldest four bytes are gone

    def test_dropping_old_audio_never_splits_a_sample(self):
        ring = ByteRing(11)  # an odd capacity, with whole samples going in
        ring.push(bytes(12))
        self.assertEqual(ring.size() % 2, 0)
        self.assertEqual(ring.dropped, 2)
        ring.push(bytes(6))
        self.assertEqual(ring.size() % 2, 0)

    def test_a_short_read_is_padded_with_silence_and_counted(self):
        ring = ByteRing(100)
        ring.push(b"\x07\x00" * 2)
        out = ring.pull(10)
        self.assertEqual(out, b"\x07\x00\x07\x00" + bytes(6))
        self.assertEqual(ring.padded, 6)

    def test_threads_do_not_lose_bytes(self):
        ring = ByteRing(10_000_000)

        def writer():
            for _ in range(2000):
                ring.push(b"\x01\x00" * 10)

        threads = [threading.Thread(target=writer) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(ring.size(), 4 * 2000 * 20)


class ResamplerTests(unittest.TestCase):
    def rms(self, data):
        return audioop.rms(data, 2)

    def test_48k_to_8k_gives_one_sixth_of_the_samples(self):
        out = Resampler(48000, 8000).process(tone(48000, 0.5))
        self.assertEqual(len(out) // 2, 24000 // 6)

    def test_chunk_boundaries_do_not_lose_samples(self):
        data = tone(48000, 0.5)
        whole = Resampler(48000, 8000).process(data)
        r = Resampler(48000, 8000)
        pieces = b"".join(r.process(data[i:i + 1000]) for i in range(0, len(data), 1000))  # not a multiple of 12
        self.assertEqual(len(pieces), len(whole))
        self.assertEqual(pieces, whole)

    def test_a_steady_level_survives_down_and_up(self):
        flat = struct.pack("<h", 1000) * 4800
        down = Resampler(48000, 8000).process(flat)
        self.assertTrue(all(abs(v - 1000) <= 1 for v in struct.unpack("<%dh" % (len(down) // 2), down)))

    def test_speech_band_tone_keeps_its_loudness(self):
        down = Resampler(48000, 8000).process(tone(48000, 0.5, freq=440))
        self.assertGreater(self.rms(down), 0.85 * self.rms(tone(8000, 0.5, freq=440)))

    def test_a_tone_above_the_speech_band_is_reduced_not_folded_in_loudly(self):
        # 7 kHz at 48 kHz: simple averaging attenuates it strongly instead of aliasing it in full
        down = Resampler(48000, 8000).process(tone(48000, 0.5, freq=7000))
        self.assertLess(self.rms(down), 0.5 * self.rms(tone(8000, 0.5, freq=440)))

    def test_8k_to_48k_gives_six_times_the_samples(self):
        out = Resampler(8000, 48000).process(tone(8000, 0.5))
        self.assertAlmostEqual(len(out) // 2, 24000, delta=12)

    def test_44100_uses_the_general_converter(self):
        out = Resampler(44100, 8000).process(tone(44100, 0.5))
        self.assertAlmostEqual(len(out) // 2, 4000, delta=20)

    def test_same_rate_is_a_pass_through(self):
        data = tone(8000, 0.1)
        self.assertEqual(Resampler(8000, 8000).process(data), data)


class BridgeTests(unittest.TestCase):
    def test_nothing_plays_until_the_buffer_is_primed(self):
        bridge = AudioBridge(prime_ms=60)
        bridge.downlink(b"\x10\x00" * 30)  # about 4 ms of phone audio
        self.assertEqual(bridge.speaker_callback(480), bytes(480 * 2 * 2))
        self.assertFalse(bridge.playing)

    def test_phone_audio_reaches_both_speaker_channels(self):
        bridge = AudioBridge(out_channels=2, prime_ms=10)
        for _ in range(20):
            bridge.downlink(b"\xe8\x03" * 30)  # constant 1000
        out = bridge.speaker_callback(100)
        samples = struct.unpack("<%dh" % (len(out) // 2), out)
        left, right = samples[0::2], samples[1::2]
        self.assertEqual(left, right)
        self.assertTrue(any(abs(v - 1000) < 5 for v in left))
        self.assertTrue(bridge.playing)

    def test_mono_output_has_no_channel_doubling(self):
        bridge = AudioBridge(out_channels=1, prime_ms=10)
        for _ in range(20):
            bridge.downlink(b"\xe8\x03" * 30)
        self.assertEqual(len(bridge.speaker_callback(100)), 200)

    def test_running_dry_after_playing_counts_an_underrun_and_plays_silence(self):
        bridge = AudioBridge(prime_ms=10)
        for _ in range(20):
            bridge.downlink(b"\xe8\x03" * 30)
        bridge.speaker_callback(1000)  # drains most of it
        bridge.speaker_callback(20000)  # asks for far more than is left
        self.assertGreaterEqual(bridge.speaker_underruns, 1)

    def test_microphone_audio_comes_out_at_the_packet_rate(self):
        bridge = AudioBridge(in_rate=48000)
        bridge.mic_callback(tone(48000, 0.1))  # 4800 samples in, 800 samples at 8 kHz
        payload = bridge.uplink(60)
        self.assertEqual(len(payload), 60)
        self.assertNotEqual(payload, bytes(60))
        self.assertGreater(bridge.stats()["uplink_peak_rms"], 0)

    def test_an_empty_microphone_buffer_gives_silence_of_the_right_length(self):
        bridge = AudioBridge()
        self.assertEqual(bridge.uplink(64), bytes(64))
        self.assertEqual(bridge.stats()["mic_padded_bytes"], 64)
        self.assertEqual(bridge.stats()["uplink_silent_packets"], 1)

    def test_a_flood_of_phone_audio_is_bounded(self):
        bridge = AudioBridge(capacity_ms=200)
        for _ in range(500):
            bridge.downlink(b"\x01\x00" * 30)
        self.assertLessEqual(bridge.to_speaker.size(), 48000 * 2 * 200 // 1000)
        self.assertGreater(bridge.stats()["speaker_dropped_bytes"], 0)

    def test_levels_see_loudness_and_silence(self):
        levels = Levels()
        levels.add(bytes(60))
        levels.add(tone(8000, 0.01))
        self.assertEqual(levels.blocks, 2)
        self.assertEqual(levels.silent_blocks, 1)
        self.assertGreater(levels.peak_rms, 1000)


class HalfDuplexTests(unittest.TestCase):
    class Clock:
        now = 100.0

        def __call__(self):
            return self.now

    def make(self, **kw):
        self.clock = self.Clock()
        bridge = AudioBridge(in_rate=48000, half_duplex=True, clock=self.clock, **kw)
        bridge.mic_callback(tone(48000, 0.2))  # the microphone has sound ready
        return bridge

    def test_loud_caller_audio_mutes_the_microphone_for_a_moment(self):
        bridge = self.make()
        bridge.downlink(tone(8000, 0.01))  # loud, 80 samples
        self.assertEqual(bridge.uplink(60), bytes(60))
        self.assertEqual(bridge.stats()["gated_packets"], 1)

    def test_the_microphone_comes_back_after_the_hold_time(self):
        bridge = self.make()
        bridge.downlink(tone(8000, 0.01))
        self.clock.now += 0.3
        self.assertNotEqual(bridge.uplink(60), bytes(60))

    def test_quiet_caller_audio_does_not_gate(self):
        bridge = self.make()
        bridge.downlink(b"\x01\x00" * 80)  # barely audible
        self.assertNotEqual(bridge.uplink(60), bytes(60))
        self.assertEqual(bridge.stats()["gated_packets"], 0)

    def test_gated_microphone_audio_is_still_drained(self):
        bridge = self.make()
        before = bridge.from_mic.size()
        bridge.downlink(tone(8000, 0.01))
        bridge.uplink(60)
        self.assertEqual(bridge.from_mic.size(), before - 60)

    def test_without_half_duplex_nothing_is_gated(self):
        bridge = AudioBridge(in_rate=48000, half_duplex=False)
        bridge.mic_callback(tone(48000, 0.2))
        bridge.downlink(tone(8000, 0.01))
        self.assertNotEqual(bridge.uplink(60), bytes(60))

    def test_open_streams_can_turn_it_on(self):
        bridge, streams = open_streams(FakeSD(), half_duplex=True)
        self.assertTrue(bridge.half_duplex)
        self.assertTrue(streams.info["half_duplex"])


class FakeStream:
    def __init__(self, kind, **kw):
        self.kind, self.kw, self.started, self.closed = kind, kw, False, False

    def start(self):
        if self.kw.get("device") == "bad":
            raise RuntimeError("cannot open device")
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


class FakeSD:
    def __init__(self):
        self.streams = []

    def query_devices(self, device=None, kind=None):
        if kind == "input":
            return {"name": "Fake Microphone", "default_samplerate": 48000.0, "max_input_channels": 1, "max_output_channels": 0}
        return {"name": "Fake Headphones", "default_samplerate": 44100.0, "max_input_channels": 0, "max_output_channels": 2}

    def RawInputStream(self, **kw):
        self.streams.append(FakeStream("in", **kw))
        return self.streams[-1]

    def RawOutputStream(self, **kw):
        self.streams.append(FakeStream("out", **kw))
        return self.streams[-1]


class OpenStreamsTests(unittest.TestCase):
    def test_streams_use_the_device_rates_and_16_bit_samples(self):
        sd = FakeSD()
        bridge, streams = open_streams(sd)
        mic, speaker = sd.streams
        self.assertEqual((mic.kw["samplerate"], mic.kw["channels"], mic.kw["dtype"]), (48000, 1, "int16"))
        self.assertEqual((speaker.kw["samplerate"], speaker.kw["channels"], speaker.kw["dtype"]), (44100, 2, "int16"))
        self.assertTrue(mic.started and speaker.started)
        self.assertTrue(bridge.started)
        self.assertEqual(streams.info["output"], "Fake Headphones")

    def test_microphone_and_speaker_callbacks_are_wired_to_the_bridge(self):
        sd = FakeSD()
        bridge, _ = open_streams(sd)
        mic, speaker = sd.streams
        mic.kw["callback"](tone(48000, 0.05), 2400, None, None)
        self.assertNotEqual(bridge.uplink(60), bytes(60))
        outdata = bytearray(400)
        speaker.kw["callback"](outdata, 100, None, None)
        self.assertEqual(len(outdata), 400)
        self.assertEqual(bridge.speaker_callbacks, 1)

    def test_stream_status_problems_are_recorded(self):
        sd = FakeSD()
        bridge, _ = open_streams(sd)
        sd.streams[0].kw["callback"](bytes(100), 50, None, "input overflow")
        self.assertEqual(bridge.stats()["stream_errors"], 1)

    def test_a_failed_start_closes_everything_and_raises(self):
        sd = FakeSD()
        with self.assertRaises(RuntimeError):
            open_streams(sd, input_device="bad")
        self.assertTrue(all(s.closed for s in sd.streams))

    def test_the_built_in_speakers_are_refused_unless_allowed(self):
        class SpeakerSD(FakeSD):
            def query_devices(self, device=None, kind=None):
                info = super().query_devices(device, kind)
                if kind == "output":
                    info["name"] = "MacBook Air Speakers"
                return info

        sd = SpeakerSD()
        with self.assertRaises(RuntimeError) as caught:
            open_streams(sd)
        self.assertIn("wired headphones", str(caught.exception))
        self.assertEqual(sd.streams, [])  # nothing was opened
        bridge, _ = open_streams(SpeakerSD(), allow_speakers=True)
        self.assertTrue(bridge.started)

    def test_close_stops_both_streams(self):
        sd = FakeSD()
        _, streams = open_streams(sd)
        streams.close()
        self.assertTrue(all(s.closed and not s.started for s in sd.streams))


if __name__ == "__main__":
    unittest.main()

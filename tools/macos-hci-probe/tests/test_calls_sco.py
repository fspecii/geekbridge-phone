# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline tests: call state and voice packet handling."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calls import CallTracker, Phase, last_digits
from sco import MAX_PAYLOAD, ScoStats, VoiceEcho


class CallTrackerTests(unittest.TestCase):
    def test_incoming_call_answered_then_ended(self):
        t = CallTracker()
        self.assertEqual(t.on_indicator("callsetup", 1), ["ringing"])
        self.assertEqual(t.phase, Phase.RINGING)
        self.assertEqual(t.on_indicator("call", 1), ["active"])
        self.assertEqual(t.on_indicator("callsetup", 0), [])  # still active
        self.assertEqual(t.on_indicator("call", 0), ["ended"])
        self.assertEqual(t.phase, Phase.IDLE)

    def test_unanswered_call_is_missed(self):
        t = CallTracker()
        t.on_indicator("callsetup", 1)
        self.assertEqual(t.on_indicator("callsetup", 0), ["missed"])

    def test_outgoing_call(self):
        t = CallTracker()
        self.assertEqual(t.on_indicator("callsetup", 2), ["dialing"])
        self.assertEqual(t.on_indicator("callsetup", 3), ["alerting"])
        self.assertEqual(t.on_indicator("call", 1), ["active"])
        self.assertEqual(t.on_indicator("callsetup", 0), [])
        self.assertEqual(t.on_indicator("call", 0), ["ended"])

    def test_other_indicators_and_repeats_do_nothing(self):
        t = CallTracker()
        self.assertEqual(t.on_indicator("signal", 4), [])
        t.on_indicator("callsetup", 1)
        self.assertEqual(t.on_indicator("callsetup", 1), [])

    def test_number_is_kept_for_the_call_only(self):
        t = CallTracker()
        t.on_indicator("callsetup", 1)
        t.on_number("+441234567")
        self.assertEqual(t.number, "+441234567")
        t.on_indicator("call", 1)
        t.on_indicator("callsetup", 0)
        t.on_indicator("call", 0)
        self.assertIsNone(t.number)

    def test_log_tail_hides_most_of_the_number(self):
        self.assertEqual(last_digits("+44 1234 567"), "567")
        self.assertEqual(last_digits(None), "")


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class VoiceEchoTests(unittest.TestCase):
    def make(self, source=None):
        self.sent = []
        self.clock = Clock()
        return VoiceEcho(lambda h, d: self.sent.append((h, d)), source, self.clock)

    def test_one_packet_back_for_each_received(self):
        echo = self.make()
        for _ in range(5):
            echo.on_packet(7, bytes(60))
        self.assertEqual(echo.stats.rx, 5)
        self.assertEqual(echo.stats.tx, 5)
        self.assertTrue(all(h == 7 and len(d) == 60 for h, d in self.sent))

    def test_default_reply_is_silence(self):
        echo = self.make()
        echo.on_packet(7, b"\x01\x02" * 30)
        self.assertEqual(self.sent[0][1], bytes(60))

    def test_source_audio_is_sent_with_the_same_length(self):
        echo = self.make(source=lambda n: b"\x05\x00" * (n // 2))
        echo.on_packet(7, bytes(60))
        self.assertEqual(self.sent[0][1], b"\x05\x00" * 30)

    def test_short_source_is_padded_with_silence_never_a_different_length(self):
        echo = self.make(source=lambda n: b"\x05\x00" * 5)
        echo.on_packet(7, bytes(60))
        self.assertEqual(len(self.sent[0][1]), 60)
        self.assertEqual(self.sent[0][1][:10], b"\x05\x00" * 5)
        self.assertEqual(self.sent[0][1][10:], bytes(50))

    def test_damaged_packets_are_answered_with_silence_and_counted(self):
        echo = self.make(source=lambda n: b"\x09\x00" * (n // 2))
        echo.on_packet(7, bytes(60), status_ok=False)
        self.assertEqual(self.sent[0][1], bytes(60))
        self.assertEqual(echo.stats.bad_status, 1)

    def test_odd_or_oversize_packets_stop_the_echo(self):
        echo = self.make()
        echo.on_packet(7, bytes(61))
        self.assertIsInstance(echo.failure, ValueError)
        echo.on_packet(7, bytes(60))
        self.assertEqual(self.sent, [])  # nothing more is sent after a failure
        echo2 = self.make()
        echo2.on_packet(7, bytes(MAX_PAYLOAD + 2))
        self.assertIsInstance(echo2.failure, ValueError)

    def test_empty_packets_are_counted_not_answered(self):
        echo = self.make()
        echo.on_packet(7, b"")
        self.assertEqual(echo.stats.empty, 1)
        self.assertEqual(self.sent, [])


class VoiceSinkTests(unittest.TestCase):
    def test_received_audio_goes_to_the_sink_and_the_source_audio_goes_back(self):
        sent, heard = [], []
        echo = VoiceEcho(lambda h, d: sent.append(d), source=lambda n: b"\x05\x00" * (n // 2), sink=heard.append)
        echo.on_packet(7, b"\x03\x00" * 30)
        self.assertEqual(heard, [b"\x03\x00" * 30])
        self.assertEqual(sent, [b"\x05\x00" * 30])

    def test_damaged_audio_reaches_the_sink_as_silence(self):
        heard = []
        echo = VoiceEcho(lambda h, d: None, sink=heard.append)
        echo.on_packet(7, b"\x03\x00" * 30, status_ok=False)
        self.assertEqual(heard, [bytes(60)])

    def test_nothing_reaches_the_sink_after_a_failure(self):
        heard = []
        echo = VoiceEcho(lambda h, d: None, sink=heard.append)
        echo.on_packet(7, bytes(61))  # odd length
        echo.on_packet(7, bytes(60))
        self.assertEqual(heard, [])


class StatsTests(unittest.TestCase):
    def test_traffic_every_second_has_no_gaps(self):
        clock = Clock()
        stats = ScoStats(clock)
        for second in range(10):
            clock.now = 100.0 + second + 0.1
            stats.mark_rx()
            clock.now += 0.4
            stats.mark_rx()
        self.assertEqual(len(stats.seconds_with_traffic()), 10)
        self.assertEqual(stats.gaps(), [])
        self.assertEqual(stats.summary()["rx"], 20)

    def test_a_silent_second_is_reported(self):
        clock = Clock()
        stats = ScoStats(clock)
        for second in (0, 1, 3, 4):
            clock.now = 100.0 + second + 0.1
            stats.mark_rx()
        self.assertEqual(stats.gaps(), [2])
        self.assertEqual(stats.summary()["gap_seconds"], 1)


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline tests for the AirPods microphone framing. No Bluetooth."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gbp_aap


class AapTests(unittest.TestCase):
    def test_control_packets_have_the_captured_bytes(self):
        self.assertEqual(gbp_aap.HANDSHAKE.hex(), "00000400010002000000000000000000")
        self.assertEqual(gbp_aap.SET_FEATURES.hex(), "040004004d00ff00000000000000")
        self.assertEqual(gbp_aap.SUBSCRIBE.hex(), "040004000f00ffffffff")
        self.assertEqual(gbp_aap.MIC_START.hex(), "04000400580000000900000182000000049600")
        self.assertEqual(gbp_aap.MIC_STOP.hex(), "040004005800000002000301")

    def test_audio_messages_are_told_from_control_messages(self):
        self.assertTrue(gbp_aap.is_audio(gbp_aap.build_audio([(1, b"abc")])))
        self.assertFalse(gbp_aap.is_audio(gbp_aap.MIC_START))
        self.assertFalse(gbp_aap.is_audio(gbp_aap.HEADER + bytes([0x04, 0, 1, 0])))
        self.assertFalse(gbp_aap.is_audio(b"\x04\x00"))

    def test_access_units_come_out_in_order(self):
        sdu = gbp_aap.build_audio([(10, b"one"), (20, b"second"), (30, b"")])
        self.assertEqual(gbp_aap.access_units(sdu), [b"one", b"second", b""])

    def test_a_cut_off_unit_is_dropped_not_read_past_the_end(self):
        sdu = gbp_aap.build_audio([(10, b"one"), (20, b"second")])
        self.assertEqual(gbp_aap.access_units(sdu[:-2]), [b"one"])
        self.assertEqual(gbp_aap.access_units(sdu[:gbp_aap.AUDIO_HEADER + 3]), [])

    def test_the_counter_reports_a_working_stream(self):
        counter = gbp_aap.MicCounter()
        for index in range(100):
            counter.on_sdu(gbp_aap.build_audio([(index, bytes(60 + index % 5))]))
        counter.on_sdu(gbp_aap.SUBSCRIBE)
        summary = counter.summary(8)
        self.assertEqual((summary["audio_messages"], summary["access_units"], summary["other_messages"]),
                         (100, 100, 1))
        self.assertEqual(summary["distinct_unit_sizes"], 5)
        self.assertEqual(summary["audio_seconds"], 1.0)


if __name__ == "__main__":
    unittest.main()

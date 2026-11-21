# SPDX-License-Identifier: GPL-3.0-or-later
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gbp_btd.voice import (DOWN, HEADER, MAGIC, MAX_PAYLOAD, UP, FrameError, FrameReader, VoicePort, VoiceServer,
                           encode_frame)


class CodecTests(unittest.TestCase):
    def test_round_trip(self):
        frames = FrameReader().feed(encode_frame(DOWN, 7, b"\x01\x00" * 30))
        self.assertEqual(frames, [(DOWN, 7, b"\x01\x00" * 30)])

    def test_frames_split_across_reads_and_joined_in_one_read(self):
        data = encode_frame(UP, 1, bytes(60)) + encode_frame(UP, 2, bytes(60))
        reader = FrameReader()
        got = []
        for i in range(0, len(data), 7):
            got += reader.feed(data[i:i + 7])
        self.assertEqual([f[1] for f in got], [1, 2])
        self.assertEqual(len(FrameReader().feed(data)), 2)

    def test_bad_frames_are_refused(self):
        for bad in (lambda: encode_frame(3, 1, b""), lambda: encode_frame(UP, 1, bytes(3)),
                    lambda: encode_frame(UP, 1, bytes(MAX_PAYLOAD + 2))):
            with self.assertRaises(FrameError):
                bad()

    def test_a_bad_header_ends_the_stream(self):
        for header in (HEADER.pack(0x00, UP, 4, 1), HEADER.pack(MAGIC, 9, 4, 1),
                       HEADER.pack(MAGIC, UP, MAX_PAYLOAD + 2, 1), HEADER.pack(MAGIC, UP, 3, 1)):
            with self.assertRaises(FrameError):
                FrameReader().feed(header + bytes(8))

    def test_the_sequence_number_wraps(self):
        frames = FrameReader().feed(encode_frame(DOWN, 2 ** 32 + 5, bytes(2)))
        self.assertEqual(frames[0][1], 5)


class PortTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.port = VoicePort(send=self.sent.append)

    def test_downlink_frames_are_numbered(self):
        self.port.downlink(bytes(60))
        self.port.downlink(bytes(60))
        frames = FrameReader().feed(b"".join(self.sent))
        self.assertEqual([f[1] for f in frames], [1, 2])
        self.assertEqual(self.port.stats()["down_frames"], 2)

    def test_a_bad_downlink_packet_is_dropped_not_forwarded(self):
        self.port.downlink(bytes(61))
        self.assertEqual(self.sent, [])

    def test_uplink_reads_what_the_app_sent_and_pads_with_silence(self):
        self.port.on_frame(UP, 1, b"\x05\x00" * 10)
        self.assertEqual(self.port.uplink(20), b"\x05\x00" * 10)
        self.assertEqual(self.port.uplink(20), bytes(20))
        self.assertEqual(self.port.stats()["uplink_padded_bytes"], 20)

    def test_sequence_gaps_from_the_app_are_counted(self):
        self.port.on_frame(UP, 1, bytes(60))
        self.port.on_frame(UP, 3, bytes(60))
        self.assertEqual(self.port.stats()["up_gaps"], 1)

    def test_downlink_kind_from_the_app_is_counted_as_bad(self):
        self.port.on_frame(DOWN, 1, bytes(60))
        self.assertEqual(self.port.stats()["up_bad"], 1)

    def test_a_flood_from_the_app_is_bounded(self):
        for i in range(200):
            self.port.on_frame(UP, i, bytes(60))
        self.assertLessEqual(self.port.uplink_ring.size(), 8000 * 2 * 200 // 1000)
        self.assertGreater(self.port.stats()["uplink_dropped_bytes"], 0)


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(prefix="gbpv", dir="/tmp")
        self.port = VoicePort(send=lambda f: None)
        self.server = VoiceServer(self.dir, self.port)
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def test_frames_flow_both_ways_over_the_socket(self):
        reader, writer = await asyncio.open_unix_connection(self.server.path)
        await asyncio.sleep(0.05)
        writer.write(encode_frame(UP, 1, b"\x07\x00" * 30))
        await writer.drain()
        for _ in range(100):
            if self.port.up_frames:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.port.uplink(60), b"\x07\x00" * 30)
        self.port.downlink(b"\x01\x00" * 30)
        data = await asyncio.wait_for(reader.readexactly(HEADER.size + 60), 2)
        self.assertEqual(FrameReader().feed(data)[0][0], DOWN)
        writer.close()

    async def test_a_corrupt_stream_drops_the_app_not_the_helper(self):
        reader, writer = await asyncio.open_unix_connection(self.server.path)
        writer.write(b"garbage that is not a frame header at all")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(10), 2), b"")  # closed by the helper
        reader2, writer2 = await asyncio.open_unix_connection(self.server.path)  # still accepting
        writer2.close()

    async def test_a_second_app_is_turned_away(self):
        _, first = await asyncio.open_unix_connection(self.server.path)
        await asyncio.sleep(0.05)
        reader, second = await asyncio.open_unix_connection(self.server.path)
        self.assertEqual(await asyncio.wait_for(reader.read(10), 2), b"")
        first.close()
        second.close()

    async def test_frames_are_dropped_not_queued_when_no_app_is_connected(self):
        self.port.downlink(bytes(60))  # no client: nothing happens, nothing queued
        self.assertIsNone(self.server.writer)

    async def test_the_socket_is_private(self):
        import stat
        self.assertEqual(stat.S_IMODE(os.stat(self.server.path).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()

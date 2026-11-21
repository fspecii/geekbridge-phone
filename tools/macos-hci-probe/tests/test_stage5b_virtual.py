# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage 5b against simulated headphones on Bumble's virtual controllers, through the companion filter.

No radio. The pretend headphones offer an SBC sink and an AAP channel that answers a
microphone request with audio messages, the way AirPods are reported to. As in the mouse
test, the stored-key handshake is not modelled by the virtual controller and is skipped.
"""
import asyncio
import logging
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bumble import hci, l2cap
from bumble.a2dp import A2DP_SBC_CODEC_TYPE, SbcMediaCodecInformation, make_audio_sink_service_sdp_records
from bumble.avdtp import AVDTP_AUDIO_MEDIA_TYPE, Listener, MediaCodecCapabilities
from bumble.controller import Controller
from bumble.link import LocalLink

import gbp_aap
import keys_import
import stage5a_mouse
import stage5b_headphones
from test_stage5a_virtual import KEY, OURS, VirtualCompanionTransport, peer

logging.disable(logging.CRITICAL)

BUDS = "AA:BB:CC:00:00:03"
TONE = Path(__file__).resolve().parent.parent / "build" / "tone.sbc"


def sink_capabilities():
    I = SbcMediaCodecInformation
    return MediaCodecCapabilities(
        media_type=AVDTP_AUDIO_MEDIA_TYPE, media_codec_type=A2DP_SBC_CODEC_TYPE,
        media_codec_information=I(
            sampling_frequency=I.SamplingFrequency.SF_44100 | I.SamplingFrequency.SF_48000,
            channel_mode=I.ChannelMode.MONO | I.ChannelMode.DUAL_CHANNEL | I.ChannelMode.STEREO | I.ChannelMode.JOINT_STEREO,
            block_length=I.BlockLength.BL_4 | I.BlockLength.BL_8 | I.BlockLength.BL_12 | I.BlockLength.BL_16,
            subbands=I.Subbands.S_4 | I.Subbands.S_8,
            allocation_method=I.AllocationMethod.LOUDNESS | I.AllocationMethod.SNR,
            minimum_bitpool_value=2, maximum_bitpool_value=53))


class Headphones:
    """A2DP sink that counts media packets, and optionally an AAP channel with a microphone."""

    def __init__(self, device, with_aap):
        self.device, self.media_packets, self.requests, self.tasks = device, 0, [], []
        device.sdp_service_records = {0x00010001: make_audio_sink_service_sdp_records(0x00010001)}
        self.listener = Listener.for_device(device)
        self.listener.on("connection", self._avdtp)
        if with_aap:
            device.create_l2cap_server(l2cap.ClassicChannelSpec(gbp_aap.PSM), self._aap)

    def _avdtp(self, protocol):
        sink = protocol.add_sink(sink_capabilities())
        sink.on("rtp_packet", self._media)

    def _media(self, packet):
        self.media_packets += 1

    def _aap(self, channel):
        def on_sdu(sdu):
            sdu = bytes(sdu)
            self.requests.append(sdu)
            if sdu == gbp_aap.MIC_START:
                self.tasks.append(asyncio.ensure_future(self._stream(channel)))
            if sdu == gbp_aap.MIC_STOP:
                for task in self.tasks:
                    task.cancel()

        channel.sink = on_sdu

    async def _stream(self, channel):
        stamp = 0
        while True:
            channel.write(gbp_aap.build_audio([(stamp, bytes(70)), (stamp + 480, bytes(72))]))
            stamp += 960
            await asyncio.sleep(0.03)


@unittest.skipUnless(TONE.exists(), "build/tone.sbc is made by build.sh when ffmpeg is installed")
class Stage5bVirtual(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.keys = Path(self.dir.name) / "keys.json"
        keys_import.store(OURS, BUDS, KEY, 4, self.keys)
        self.link = LocalLink()
        controller = Controller("ours", link=self.link, public_address=hci.Address(OURS, hci.Address.PUBLIC_DEVICE_ADDRESS))
        self.target = hci.Address(BUDS, hci.Address.PUBLIC_DEVICE_ADDRESS)
        self.transport = VirtualCompanionTransport(controller, [self.target])
        self.device = stage5a_mouse.build_device(self.transport)
        stage5b_headphones.emit = lambda *a, **k: None

    async def asyncTearDown(self):
        self.dir.cleanup()

    async def _run(self, with_aap):
        headphones = Headphones(await peer(self.link, BUDS, KEY), with_aap)
        counter = gbp_aap.MicCounter()
        connection = await stage5b_headphones.open_link(self.device, self.transport, self.target, self.keys,
                                                        secure=False)
        stream, pump, handle = await stage5b_headphones.play_tone(self.transport, connection, TONE)
        try:
            await asyncio.sleep(0.5)
            has_aap = await stage5b_headphones.listen_to_mic(self.transport, connection, 1, counter)
        finally:
            handle.close()
        self.assertIsNone(self.transport.failure)
        return headphones, counter, has_aap

    async def test_the_tone_plays_and_the_microphone_streams_beside_it(self):
        headphones, counter, has_aap = await self._run(with_aap=True)
        self.assertTrue(has_aap)
        self.assertGreater(headphones.media_packets, 10)
        self.assertEqual(headphones.requests[:4],
                         [gbp_aap.HANDSHAKE, gbp_aap.SET_FEATURES, gbp_aap.SUBSCRIBE, gbp_aap.MIC_START])
        self.assertEqual(headphones.requests[-1], gbp_aap.MIC_STOP)
        summary = counter.summary(1)
        self.assertGreater(summary["access_units"], 20)
        self.assertEqual(summary["access_units"], summary["audio_messages"] * 2)

    async def test_headphones_without_the_apple_channel_still_play(self):
        headphones, counter, has_aap = await self._run(with_aap=False)
        self.assertFalse(has_aap)
        self.assertGreater(headphones.media_packets, 10)
        self.assertEqual(counter.summary(1)["access_units"], 0)


if __name__ == "__main__":
    unittest.main()

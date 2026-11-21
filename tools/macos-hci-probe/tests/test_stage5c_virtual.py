# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage 5c's companion side on virtual controllers: the mouse and the headphones together,
through a filter that also has a phone configured. The call itself is covered by the stage 3
tests; what is new here is the two devices at once and the verdict. No radio."""
import asyncio
import logging
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bumble import hci
from bumble.controller import Controller
from bumble.hid import Device as HidDevice
from bumble.link import LocalLink

import gbp_aap
import stage5a_mouse
import stage5c_all
from gbp_companions import CompanionScope
from test_companions import mouse_report
from test_stage5a_virtual import KEY, MOUSE, OURS, VirtualCompanionTransport, peer
from test_stage5b_virtual import BUDS, TONE, Headphones

logging.disable(logging.CRITICAL)
PHONE = "6C:AC:C2:00:00:01"


class VerdictTests(unittest.TestCase):
    VOICE = {"rx": 500, "tx": 500, "gap_seconds": 0, "odd_length": 0}

    def test_clean_voice_and_a_moving_mouse_pass(self):
        result = stage5c_all.verdict(self.VOICE, {"decoded": 40}, {"access_units": 300}, True)
        self.assertEqual(result, {"voice_ok": True, "mouse_ok": True, "microphone_ok": True, "passed": True})

    def test_headphones_without_the_apple_channel_do_not_fail_the_run(self):
        result = stage5c_all.verdict(self.VOICE, {"decoded": 40}, {"access_units": 0}, False)
        self.assertIsNone(result["microphone_ok"])
        self.assertTrue(result["passed"])

    def test_broken_voice_or_a_silent_mouse_fail(self):
        gaps = dict(self.VOICE, gap_seconds=2)
        self.assertFalse(stage5c_all.verdict(gaps, {"decoded": 40}, {"access_units": 300}, True)["passed"])
        self.assertFalse(stage5c_all.verdict(self.VOICE, {"decoded": 0}, {"access_units": 300}, True)["passed"])

    def test_a_silent_apple_microphone_is_reported(self):
        self.assertFalse(stage5c_all.verdict(self.VOICE, {"decoded": 4}, {"access_units": 0}, True)["microphone_ok"])


@unittest.skipUnless(TONE.exists(), "build/tone.sbc is made by build.sh when ffmpeg is installed")
class CompanionsTogether(unittest.IsolatedAsyncioTestCase):
    async def test_mouse_and_headphones_are_served_at_once(self):
        stage5c_all.emit = lambda *a, **k: None
        link = LocalLink()
        controller = Controller("ours", link=link, public_address=hci.Address(OURS, hci.Address.PUBLIC_DEVICE_ADDRESS))
        mouse, buds = stage5c_all.address(MOUSE), stage5c_all.address(BUDS)
        transport = VirtualCompanionTransport(controller, [])
        transport.filter = CompanionScope(stage5c_all.address(PHONE), [mouse, buds])
        device = stage5a_mouse.build_device(transport)
        await device.power_on()
        headphones = Headphones(await peer(link, BUDS, KEY), with_aap=True)
        pretend_mouse = await peer(link, MOUSE, KEY)
        mouse_hid = HidDevice(pretend_mouse)
        features = []
        mouse_hid.register_set_report_cb(lambda report_id, report_type, size, data: (
            features.append(report_id), mouse_hid.GetSetStatus(status=mouse_hid.GetSetReturn.SUCCESS))[1])
        session, counter = stage5a_mouse.MouseSession(say=lambda *a, **k: None), gbp_aap.MicCounter()

        hid, handle, buds_link = await stage5c_all.start_companions(
            device, transport, mouse, buds, TONE, session, secure=False)
        try:
            await pretend_mouse.connect_classic(hci.Address(OURS, hci.Address.PUBLIC_DEVICE_ADDRESS))
            await mouse_hid.connect_control_channel()
            await mouse_hid.connect_interrupt_channel()
            await asyncio.sleep(0.8)
            for _ in range(5):
                mouse_hid.send_data(mouse_report(dx=2, dy=1))
            self.assertTrue(await stage5c_all.listen_to_mic(transport, buds_link, 1, counter))
        finally:
            handle.close()
        self.assertIsNone(transport.failure)
        self.assertEqual(session.summary()["decoded"], 5)
        self.assertEqual(features, [0xF1])
        self.assertGreater(headphones.media_packets, 10)
        self.assertGreater(counter.summary(1)["access_units"], 20)
        self.assertEqual(len(transport.filter.links), 2)


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline test of the stage 3a call flow, driven by stand-ins for the phone and the controller."""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bumble import hci, hfp

import stage3a_call
from stage3a_call import CallSession

PHONE = hci.Address("6C:AC:C2:00:00:01", hci.Address.PUBLIC_DEVICE_ADDRESS)
GOOD = hci.HCI_SynchronousDataPacket.Status.CORRECTLY_RECEIVED_DATA


class Emitter:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event, *args):
        for handler in list(self.handlers.get(event, [])):
            handler(*args)


def indicator(name, value):
    return SimpleNamespace(indicator=SimpleNamespace(value=name), current_status=value)


class FakeProtocol(Emitter):
    EVENT_AG_INDICATOR = "ag_indicator"
    EVENT_CLI_NOTIFICATION = "cli"

    def __init__(self, connection, number="+441234567"):
        super().__init__()
        self.connection, self.number = connection, number

    async def query_current_calls(self):
        return [SimpleNamespace(number=self.number)]

    async def answer_incoming_call(self):
        self.emit(self.EVENT_AG_INDICATOR, indicator("call", 1))
        self.emit(self.EVENT_AG_INDICATOR, indicator("callsetup", 0))
        self.connection.emit(self.connection.EVENT_SCO_REQUEST, 2)  # the phone asks for the voice link

    async def terminate_call(self):
        self.emit(self.EVENT_AG_INDICATOR, indicator("call", 0))


class FakeConnection(Emitter):
    EVENT_SCO_REQUEST = "sco_request"
    EVENT_SCO_CONNECTION = "sco_connection"
    EVENT_SCO_CONNECTION_FAILURE = "sco_failure"
    peer_address = PHONE


class FakeDevice:
    EVENT_SCO_REQUEST = "sco_request"
    EVENT_SCO_CONNECTION = "sco_connection"
    EVENT_SCO_CONNECTION_FAILURE = "sco_failure"

    def __init__(self, connection, fail_accept=False):
        self.connection, self.fail_accept = connection, fail_accept
        self.commands, self.sent = [], []
        self.host = SimpleNamespace(send_sco_sdu=lambda handle, payload: self.sent.append((handle, payload)))

    async def send_async_command(self, command):
        if self.fail_accept:
            raise RuntimeError("controller refused")
        self.commands.append(command)
        link = SimpleNamespace(handle=7, sink=None)
        asyncio.get_running_loop().call_soon(
            lambda: self.connection.emit(self.connection.EVENT_SCO_CONNECTION, link))


class FakeTransport:
    failure = None

    async def guarded(self, awaitable, timeout):
        return await asyncio.wait_for(awaitable, timeout)


class CallSessionTests(unittest.IsolatedAsyncioTestCase):
    def build(self, **kw):
        self.said = []
        self.connection = FakeConnection()
        self.device = FakeDevice(self.connection, **kw)
        self.protocol = FakeProtocol(self.connection)
        self.session = CallSession(self.device, self.connection, self.protocol, FakeTransport(),
                                   say=lambda stage, **v: self.said.append((stage, v)))

    def stages(self):
        return [s for s, _ in self.said]

    async def ring(self):
        self.protocol.emit("ag_indicator", indicator("callsetup", 1))
        self.protocol.emit("cli", SimpleNamespace(number="+441234567"))

    async def test_full_call_flow(self):
        self.build()
        await self.ring()
        await self.session.wait_for_ring(1)
        incoming = dict(self.said)["incoming-call"]
        self.assertTrue(incoming["number_known"])
        self.assertEqual(incoming["number_tail"], "567")

        async def feed():  # the controller delivers voice packets while the link is open
            while self.session.link is None:
                await asyncio.sleep(0.01)
            for _ in range(30):
                self.session.link.sink(SimpleNamespace(packet_status=GOOD, data=bytes(60)))
                await asyncio.sleep(0.01)

        feeder = asyncio.ensure_future(feed())
        summary = await self.session.answer_and_hold(0.4)
        await feeder
        self.assertEqual(summary["rx"], 30)
        self.assertEqual(summary["tx"], 30)
        self.assertEqual(summary["gap_seconds"], 0)
        self.assertEqual(len(self.device.sent), 30)
        self.assertTrue(all(h == 7 and p == bytes(60) for h, p in self.device.sent))

        await self.session.hang_up()
        self.assertIn("hung-up", self.stages())
        for stage in ("call-ringing", "call-active", "voice-link-requested", "voice-link-open", "call-ended"):
            self.assertIn(stage, self.stages())

    async def test_the_voice_link_is_accepted_with_the_standard_cvsd_parameters(self):
        self.build()
        await self.ring()
        await self.session.wait_for_ring(1)
        await self.session.answer_and_hold(0.05)
        self.assertEqual(len(self.device.commands), 1)
        command = self.device.commands[0]
        self.assertIsInstance(command, hci.HCI_Enhanced_Accept_Synchronous_Connection_Request_Command)
        self.assertEqual(bytes(command.bd_addr), bytes(PHONE))
        expected = hfp.ESCO_PARAMETERS[hfp.DefaultCodecParameters.ESCO_CVSD_S1].asdict()
        self.assertEqual(command.transmit_bandwidth, expected["transmit_bandwidth"])
        self.assertEqual(command.max_latency, expected["max_latency"])

    async def test_plain_sco_request_uses_the_sco_parameters(self):
        self.build()
        self.connection.emit("sco_request", 0)
        await asyncio.sleep(0.05)
        expected = hfp.ESCO_PARAMETERS[hfp.DefaultCodecParameters.SCO_CVSD_D1].asdict()
        self.assertEqual(self.device.commands[0].packet_type, expected["packet_type"])

    async def test_a_second_voice_link_request_is_a_failure(self):
        self.build()
        await self.ring()
        await self.session.wait_for_ring(1)
        await self.session.answer_and_hold(0.05)
        self.connection.emit("sco_request", 2)
        self.assertIsNotNone(self.session.sco_failure)

    async def test_a_refused_accept_is_reported(self):
        self.build(fail_accept=True)
        await self.ring()
        await self.session.wait_for_ring(1)
        with self.assertRaises(RuntimeError):
            await self.session.answer_and_hold(0.05)

    async def test_no_ring_times_out(self):
        self.build()
        with self.assertRaises(asyncio.TimeoutError):
            await self.session.wait_for_ring(0.05)

    async def test_a_bad_voice_packet_is_a_failure(self):
        self.build()
        await self.ring()
        await self.session.wait_for_ring(1)

        async def feed():
            while self.session.link is None:
                await asyncio.sleep(0.01)
            self.session.link.sink(SimpleNamespace(packet_status=GOOD, data=bytes(61)))  # odd length

        feeder = asyncio.ensure_future(feed())
        with self.assertRaises(ValueError):
            await self.session.answer_and_hold(0.2)
        await feeder


class RecordingAudio:
    def __init__(self):
        self.heard, self.asked = [], []

    def downlink(self, data):
        self.heard.append(data)

    def uplink(self, length):
        self.asked.append(length)
        return b"\x09\x00" * (length // 2)


class CallSessionAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_phone_audio_goes_to_the_speakers_and_microphone_audio_goes_back(self):
        said = []
        connection = FakeConnection()
        device = FakeDevice(connection)
        protocol = FakeProtocol(connection)
        audio = RecordingAudio()
        session = CallSession(device, connection, protocol, FakeTransport(),
                              say=lambda stage, **v: said.append(stage), audio=audio)
        protocol.emit("ag_indicator", indicator("callsetup", 1))
        await session.wait_for_ring(1)

        async def feed():
            while session.link is None:
                await asyncio.sleep(0.01)
            for _ in range(10):
                session.link.sink(SimpleNamespace(packet_status=GOOD, data=b"\x02\x00" * 30))
            session.link.sink(SimpleNamespace(packet_status=hci.HCI_SynchronousDataPacket.Status.NO_DATA_RECEIVED
                                              if hasattr(hci.HCI_SynchronousDataPacket.Status, "NO_DATA_RECEIVED")
                                              else 1, data=b"\x02\x00" * 30))

        feeder = asyncio.ensure_future(feed())
        summary = await session.answer_and_hold(0.2)
        await feeder
        self.assertEqual(len(audio.heard), 11)
        self.assertEqual(audio.heard[:10], [b"\x02\x00" * 30] * 10)
        self.assertEqual(audio.heard[10], bytes(60))  # the damaged packet reached the speakers as silence
        self.assertEqual(audio.asked, [60] * 10)  # the damaged one is answered with silence, no microphone read
        self.assertEqual([p for _, p in device.sent[:10]], [b"\x09\x00" * 30] * 10)
        self.assertEqual(device.sent[10][1], bytes(60))
        self.assertEqual(summary["bad_status"], 1)


class ArgumentTests(unittest.TestCase):
    def test_bad_target_is_refused(self):
        sys.argv = ["x", "--target", "nope"]
        self.assertEqual(stage3a_call.main(), 2)

    def test_3b_arguments_are_checked(self):
        import stage3b_voice
        self.assertEqual(stage3b_voice.main(["--target", "nope"]), 2)
        self.assertEqual(stage3b_voice.main(["--target", "6C:AC:C2:00:00:01", "--hold", "3"]), 2)
        self.assertEqual(stage3b_voice.main(["--target", "6C:AC:C2:00:00:01", "--hold", "300"]), 2)

    def test_limits_are_enforced(self):
        sys.argv = ["x", "--target", "6C:AC:C2:00:00:01", "--listen", "500"]
        self.assertEqual(stage3a_call.main(), 2)


if __name__ == "__main__":
    unittest.main()

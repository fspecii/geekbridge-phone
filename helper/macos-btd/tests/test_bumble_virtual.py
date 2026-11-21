# SPDX-License-Identifier: GPL-3.0-or-later
"""The real Bumble stack against a simulated phone, on Bumble's virtual controllers.

No radio and no Apple channels: both devices sit on an in-process link. The safety filter is in
the loop on our side, so this also shows that it lets real Bumble traffic through.
"""
import asyncio
import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bumble import core, hfp, rfcomm, sdp
from bumble.controller import Controller
from bumble.device import Device, DeviceConfiguration
from bumble.host import Host
from bumble.link import LocalLink

from fakes import until

from gbp_btd.acipc import ScopeError, ScopeFilter
from gbp_btd.bumble_stack import BumbleStack
from gbp_btd.stack import StackError

logging.disable(logging.CRITICAL)  # Bumble's virtual controller logs warnings for commands it does not model

OURS = "F0:F1:F2:F3:F4:F5"
PHONE = "6C:AC:C2:00:00:01"


class VirtualTransport:
    """What AcipcTransport is to the real controller, for a Bumble virtual controller. Same filter."""

    def __init__(self, controller):
        self.controller = controller
        self.filter = ScopeFilter()
        self.failure = None
        self.running = False

    def set_packet_sink(self, sink):
        transport = self

        class Checked:
            def on_packet(self, packet):
                try:
                    transport.filter.check_inbound(packet)
                except ScopeError as error:
                    transport.failure = error
                    raise
                sink.on_packet(packet)

        self.controller.set_packet_sink(Checked())

    def on_packet(self, packet):
        try:
            self.filter.check_outbound(packet)
        except ScopeError as error:
            self.failure = error
            raise
        self.controller.on_packet(packet)

    def set_handle(self, handle):
        self.filter.handle = handle

    def set_target(self, target):
        self.filter.set_target(target)

    def set_scanning(self, on):
        self.filter.scanning = bool(on)

    async def start(self):
        self.running = True

    async def stop(self):
        self.running = False

    async def guarded(self, awaitable, timeout):
        task = asyncio.ensure_future(awaitable)
        try:
            end = asyncio.get_running_loop().time() + timeout
            while not task.done():
                if self.failure:
                    raise self.failure
                if asyncio.get_running_loop().time() >= end:
                    raise TimeoutError("operation exceeded its time limit")
                await asyncio.sleep(0.01)
            if self.failure:
                raise self.failure
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


def ag_configuration():
    I = hfp.AgIndicator
    return hfp.AgConfiguration(
        supported_ag_features=[hfp.AgFeature.ENHANCED_CALL_STATUS, hfp.AgFeature.REJECT_CALL],
        supported_ag_indicators=[
            hfp.AgIndicatorState.service(), hfp.AgIndicatorState.call(),
            hfp.AgIndicatorState(indicator=I.CALL_SETUP, supported_values={0, 1, 2, 3}, current_status=0),
            hfp.AgIndicatorState(indicator=I.CALL_HELD, supported_values={0, 1, 2}, current_status=0),
            hfp.AgIndicatorState.signal(), hfp.AgIndicatorState.roam(),
            hfp.AgIndicatorState(indicator=I.BATTERY_CHARGE, supported_values=set(range(6)), current_status=5),
        ],
        supported_hf_indicators=[],
        supported_ag_call_hold_operations=[],
        supported_audio_codecs=[hfp.AudioCodec.CVSD],
    )


class SimulatedPhone:
    def __init__(self, controller):
        config = DeviceConfiguration(name="Simulated Phone", le_enabled=False, classic_enabled=True,
                                     classic_smp_enabled=False, classic_accept_any=True, connectable=True,
                                     discoverable=True, keystore=None)
        self.device = Device(config=config, host=Host(controller_source=controller, controller_sink=controller))
        self.ag = None
        self.commands = []
        self.map_data = []
        self.map_dlcs = []

    async def start(self):
        await self.device.power_on()
        self.config = ag_configuration()
        server = rfcomm.Server(self.device)
        hfp_channel = server.listen(self._hfp)
        # Two message server instances, like a real phone: email on the lower channel, SMS on the higher.
        self.mas_hits = []
        email_channel = server.listen(self._mas("email"))
        sms_channel = server.listen(self._mas("sms"))
        pbap_channel = server.listen(self._map)
        self.sms_channel, self.email_channel = sms_channel, email_channel

        def mas(handle, channel, name, types):
            return rfcomm.make_service_sdp_records(handle, channel, core.UUID.from_16_bits(0x1132)) + [
                sdp.ServiceAttribute(0x0100, sdp.DataElement.text_string(name)),
                sdp.ServiceAttribute(0x0316, sdp.DataElement.unsigned_integer_8(types)),
            ]

        self.device.sdp_service_records = {
            1: hfp.make_ag_sdp_records(1, hfp_channel, self.config),
            2: mas(2, email_channel, b"Email", 0x01),
            3: mas(3, sms_channel, b"SMS/MMS", 0x02),
            4: rfcomm.make_service_sdp_records(4, pbap_channel, core.UUID.from_16_bits(0x112F)),
        }

    def _hfp(self, dlc):
        self.ag = hfp.AgProtocol(dlc, self.config)
        for event, name in ((self.ag.EVENT_ANSWER, "answer"), (self.ag.EVENT_HANG_UP, "hang_up"), (self.ag.EVENT_DIAL, "dial")):
            def handler(*args, _name=name):
                self.commands.append((_name,) + args)
                self.ag.send_ok()
            self.ag.on(event, handler)

    def _mas(self, name):
        def acceptor(dlc):
            self.mas_hits.append(name)
            self._map(dlc)
        return acceptor

    def _map(self, dlc):
        self.map_dlcs.append(dlc)

        def sink(data):
            self.map_data.append(bytes(data))
            dlc.write(b"echo:" + bytes(data))

        dlc.sink = sink

    def ring(self, number="+441234567"):
        self.ag.update_ag_indicator(hfp.AgIndicator.CALL_SETUP, 1)  # 1 = incoming call
        self.ag.send_ring()
        self.ag.send_cli_notification(hfp.CallLineIdentification(number=number, type=145))


class VirtualCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(prefix="gbpb", dir="/tmp")
        link = LocalLink()
        ours = Controller("ours", link=link, public_address=OURS)
        theirs = Controller("phone", link=link, public_address=PHONE)
        self.transport = VirtualTransport(ours)
        self.phone = SimulatedPhone(theirs)
        await self.phone.start()
        self.stack = BumbleStack(keys_path=str(Path(self.dir) / "keys.json"), name="Test Mac",
                                 transport_factory=lambda: self.transport, skip_security=True)
        await self.stack.start()

    async def asyncTearDown(self):
        await self.stack.stop()
        shutil.rmtree(self.dir, ignore_errors=True)


class StackTests(VirtualCase):
    async def test_the_stack_starts_and_reports_its_controller(self):
        info = self.stack.info()
        self.assertEqual(info["stack"], "bumble")
        self.assertEqual(info["controller_address"], OURS)
        self.assertIsNotNone(info["hci_version"])
        self.assertIsNone(self.transport.failure)

    async def test_connect_and_open_the_hands_free_link(self):
        link = await self.stack.connect(PHONE)
        session = await link.open_hfp()
        self.assertIsNotNone(self.phone.ag)
        await link.close()
        self.assertIsNone(self.transport.failure)  # the safety filter let all of it through

    async def test_the_phone_ringing_reaches_the_session(self):
        link = await self.stack.connect(PHONE)
        session = await link.open_hfp()
        seen = []
        session.on_indicator = lambda name, value: seen.append((name, value))
        callers = []
        session.on_caller = callers.append
        self.phone.ring()
        await until(lambda: ("callsetup", 1) in seen and callers)
        self.assertEqual(callers[0], "+441234567")
        await link.close()

    async def test_answering_and_hanging_up_reach_the_phone(self):
        link = await self.stack.connect(PHONE)
        session = await link.open_hfp()
        await session.answer()
        await session.hang_up()
        names = [c[0] for c in self.phone.commands]
        self.assertEqual(names, ["answer", "hang_up"])
        await link.close()

    async def test_dialling_reaches_the_phone_with_the_number(self):
        link = await self.stack.connect(PHONE)
        session = await link.open_hfp()
        await session.dial("+441234567")
        self.assertEqual(self.phone.commands[0][0], "dial")
        self.assertIn("+441234567", str(self.phone.commands[0]))
        await link.close()

    async def test_a_message_channel_carries_bytes_both_ways(self):
        link = await self.stack.connect(PHONE)
        channel = await link.open_channel("map")
        await channel.write(b"OBEX CONNECT")
        self.assertEqual(await asyncio.wait_for(channel.read(), 3), b"echo:OBEX CONNECT")
        self.assertEqual(self.phone.map_data, [b"OBEX CONNECT"])
        await link.close()

    async def test_the_messages_service_uses_the_sms_instance_not_the_lowest_channel(self):
        self.assertLess(self.phone.email_channel, self.phone.sms_channel)  # the trap: email is lower
        link = await self.stack.connect(PHONE)
        channel = await link.open_channel("map")
        await channel.write(b"hi")
        self.assertEqual(await asyncio.wait_for(channel.read(), 3), b"echo:hi")
        self.assertEqual(self.phone.mas_hits, ["sms"])
        await link.close()

    async def test_the_diagnostics_hold_sizes_never_content(self):
        lines = []
        self.stack.log = lambda *a: lines.append(" ".join(str(x) for x in a))
        link = await self.stack.connect(PHONE)
        channel = await link.open_channel("pbap")
        await channel.write(b"SECRET-CONTACT-DATA")
        await asyncio.wait_for(channel.read(), 3)
        text = "\n".join(lines)
        self.assertIn("service-channel write 19 bytes", text)
        self.assertIn("service-channel received", text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("echo", text)
        await link.close()

    async def test_a_phonebook_channel_opens_too_and_both_can_be_open_at_once(self):
        link = await self.stack.connect(PHONE)
        messages = await link.open_channel("map")
        contacts = await link.open_channel("pbap")
        await contacts.write(b"GET")
        self.assertEqual(await asyncio.wait_for(contacts.read(), 3), b"echo:GET")
        await messages.close()
        await contacts.close()
        await link.close()

    async def test_the_hands_free_link_and_a_service_share_one_connection(self):
        link = await self.stack.connect(PHONE)
        await link.open_hfp()
        channel = await link.open_channel("pbap")
        await channel.write(b"x")
        self.assertEqual(await asyncio.wait_for(channel.read(), 3), b"echo:x")
        await link.close()

    async def test_a_lost_link_is_reported(self):
        link = await self.stack.connect(PHONE)
        lost = asyncio.Event()
        link.on_lost = lost.set
        await self.phone.device.disconnect(next(iter(self.phone.device.connections.values())), 0x13)
        await asyncio.wait_for(lost.wait(), 3)

    async def test_connecting_to_an_address_nothing_answers_is_a_clean_error(self):
        with self.assertRaises(StackError) as caught:
            await self.stack.connect("11:22:33:44:55:66")
        self.assertIn(caught.exception.code, ("link_failed", "timeout"))
        self.assertIsNone(self.transport.failure)


if __name__ == "__main__":
    unittest.main()


class CandidateParsingTests(unittest.TestCase):
    def test_service_names_lose_their_trailing_nul_and_types_are_read(self):
        from bumble import sdp
        from gbp_btd.bumble_stack import candidates
        record = rfcomm.make_service_sdp_records(1, 7, core.UUID.from_16_bits(0x1132)) + [
            sdp.ServiceAttribute(0x0100, sdp.DataElement.text_string(b"SMS/MMS\x00")),
            sdp.ServiceAttribute(0x0316, sdp.DataElement.unsigned_integer_8(14)),
        ]
        self.assertEqual(candidates([record]), [{"channel": 7, "name": "SMS/MMS", "types": 14}])


class ChannelChoiceTests(unittest.TestCase):
    def test_choose_the_sms_instance(self):
        from gbp_btd.bumble_stack import choose_channel
        found = [{"channel": 3, "name": "Email", "types": 0x01}, {"channel": 9, "name": "SMS/MMS", "types": 0x0A}]
        self.assertEqual(choose_channel(found, prefer_sms=True)["channel"], 9)
        self.assertEqual(choose_channel(found, prefer_sms=False)["channel"], 3)

    def test_without_message_types_the_lowest_valid_channel_is_used(self):
        from gbp_btd.bumble_stack import choose_channel
        found = [{"channel": 12, "name": "", "types": None}, {"channel": 5, "name": "", "types": None}]
        self.assertEqual(choose_channel(found, prefer_sms=True)["channel"], 5)

    def test_invalid_channels_are_never_chosen(self):
        from gbp_btd.bumble_stack import choose_channel
        for found in ([], [{"channel": 0, "name": "", "types": None}], [{"channel": 31, "name": "", "types": None}],
                      [{"channel": "x", "name": "", "types": None}]):
            with self.assertRaises(StackError):
                choose_channel(found)
        self.assertEqual(choose_channel([{"channel": 99, "name": "", "types": 2}, {"channel": 7, "name": "", "types": None}])["channel"], 7)

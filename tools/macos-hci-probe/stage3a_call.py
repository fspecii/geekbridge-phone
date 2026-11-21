# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage 3a: answer one real call from the Mac and carry its voice link with silence.

Run by launcher.c with system Bluetooth switched off. One attempt, no loops. No sound device
yet: the Mac answers, accepts the phone's voice link, counts packets both ways for a few
seconds while sending silence, then hangs up. The structure follows the reference project
lzsnolimit/ai-caller (MIT); the code is ours.

The phone must already be paired with our stack (stage 2). A missing key is a failure here,
never a new pairing.
"""
import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bumble import core, hci, hfp, pairing, rfcomm, sdp
from bumble.device import Device, DeviceConfiguration

from calls import CallTracker, Phase, last_digits
from gbp_acipc import AcipcTransport, NoFirmwareHost, TargetKeyStore, emit
from sco import VoiceEcho

HFP_AG = 0x111F
RUN_LIMIT = 140  # seconds for the whole run


class RefusePairing(pairing.PairingDelegate):
    """This stage never pairs: a missing key is a failure, not a prompt."""

    def __init__(self):
        super().__init__(io_capability=self.NO_OUTPUT_NO_INPUT)

    async def confirm(self, auto=False):
        return False

    async def compare_numbers(self, number, digits):
        return False


class CallSession:
    """Call control and the voice link for one phone. Takes the Bumble objects it needs, so the
    flow can be tested with stand-ins."""

    def __init__(self, device, connection, protocol, transport, say=emit, audio=None):
        self.device, self.connection, self.protocol, self.transport = device, connection, protocol, transport
        self.say = say
        self.tracker = CallTracker()
        self.ring = asyncio.Event()
        self.ended = asyncio.Event()
        self.sco_ready = asyncio.Event()
        self.link = None
        self.sco_failure = None
        self.accept_tasks = set()
        self.echo = VoiceEcho(
            lambda handle, payload: self.device.host.send_sco_sdu(handle, payload),
            source=audio.uplink if audio else None,
            sink=audio.downlink if audio else None)
        protocol.on(protocol.EVENT_AG_INDICATOR, self._indicator)
        protocol.on(protocol.EVENT_CLI_NOTIFICATION, self._caller)
        connection.on(device.EVENT_SCO_REQUEST, self._sco_requested)
        connection.on(device.EVENT_SCO_CONNECTION, self._sco_connected)
        connection.on(device.EVENT_SCO_CONNECTION_FAILURE, self._sco_failed)

    # Hands-free events.
    def _indicator(self, state):
        name = getattr(state.indicator, "value", str(state.indicator))
        for event in self.tracker.on_indicator(name, state.current_status):
            self.say("call-" + event, number_tail=last_digits(self.tracker.number))
            if event == "ringing":
                self.ring.set()
            if event in ("ended", "missed"):
                self.ended.set()

    def _caller(self, line):
        self.tracker.on_number(getattr(line, "number", None))

    # Voice link events.
    def _sco_requested(self, link_type):
        if self.link is not None:
            self.sco_failure = RuntimeError("a second voice link was requested")
            self.sco_ready.set()
            return
        kind = hfp.DefaultCodecParameters.SCO_CVSD_D1 if link_type == 0 else hfp.DefaultCodecParameters.ESCO_CVSD_S1
        self.say("voice-link-requested", link_type=int(link_type))

        async def accept():
            try:
                await self.transport.guarded(self.device.send_async_command(
                    hci.HCI_Enhanced_Accept_Synchronous_Connection_Request_Command(
                        bd_addr=self.connection.peer_address,
                        **hfp.ESCO_PARAMETERS[kind].asdict())), 5)
            except Exception as error:
                self.sco_failure = error
                self.sco_ready.set()

        task = asyncio.ensure_future(accept())
        self.accept_tasks.add(task)
        task.add_done_callback(self.accept_tasks.discard)

    def _sco_connected(self, link):
        self.link = link
        link.sink = self._voice_packet
        self.say("voice-link-open", handle=int(link.handle))
        self.sco_ready.set()

    def _sco_failed(self, status):
        if self.link is None:
            self.sco_failure = RuntimeError(f"voice link failed (status {status})")
            self.sco_ready.set()

    def _voice_packet(self, packet):
        ok = packet.packet_status == hci.HCI_SynchronousDataPacket.Status.CORRECTLY_RECEIVED_DATA
        self.echo.on_packet(self.link.handle, packet.data, ok)

    # Steps.
    async def wait_for_ring(self, seconds):
        await asyncio.wait_for(self.ring.wait(), seconds)
        calls = await self.transport.guarded(self.protocol.query_current_calls(), 4)
        for call in calls:
            self.tracker.on_number(call.number)
        self.say("incoming-call", calls=len(calls), number_known=bool(self.tracker.number),
                 number_tail=last_digits(self.tracker.number))

    async def answer_and_hold(self, hold_seconds):
        await self.transport.guarded(self.protocol.answer_incoming_call(), 5)
        self.say("answered")
        await self.transport.guarded(self.sco_ready.wait(), 8)
        if self.sco_failure:
            raise self.sco_failure
        await asyncio.sleep(hold_seconds)
        if self.echo.failure:
            raise self.echo.failure
        return self.echo.stats.summary()

    async def hang_up(self):
        await self.transport.guarded(self.protocol.terminate_call(), 5)
        await self.transport.guarded(self.ended.wait(), 6)
        self.say("hung-up")

    async def close(self):
        for task in tuple(self.accept_tasks):
            task.cancel()
        await asyncio.gather(*self.accept_tasks, return_exceptions=True)
        if self.link is not None:
            self.link.sink = None


def rfcomm_channels(records):
    channels = set()
    for record in records:
        for attribute in record:
            for descriptor in attribute.value.value:
                elements = descriptor.value
                if len(elements) >= 2 and elements[0].value == core.BT_RFCOMM_PROTOCOL_ID:
                    channels.add(elements[1].value)
    return channels


async def cue():
    """A spoken cue, so you know the Mac is ready for the call. Best effort, falls back to a ping."""
    for command in (("/usr/bin/say", "Ready. Call the phone now."),
                    ("/usr/bin/afplay", "/System/Library/Sounds/Ping.aiff")):
        try:
            await asyncio.create_subprocess_exec(*command)
            return
        except Exception:
            continue


async def run(target_text, listen_seconds, hold_seconds, keys_path=None, audio_factory=None, label="stage3a"):
    target = hci.Address(target_text, hci.Address.PUBLIC_DEVICE_ADDRESS)
    transport = AcipcTransport(target)
    host = NoFirmwareHost(controller_source=transport, controller_sink=transport)
    config = DeviceConfiguration(
        name="Mac", le_enabled=False, classic_enabled=True, classic_smp_enabled=False,
        classic_accept_any=False, connectable=False, discoverable=False, keystore=None)
    device = Device(config=config, host=host)
    device.command_timeout = 3
    device.pairing_config_factory = lambda connection: pairing.PairingConfig(
        sc=True, mitm=True, bonding=True, delegate=RefusePairing())
    connection = sdp_client = rfcomm_client = session = loop_task = streams = bridge = None
    primary = None
    link_closed = asyncio.Event()
    await transport.start()
    try:
        await transport.guarded(device.power_on(), 15)
        device.keystore = TargetKeyStore(device.public_address, target, keys_path)
        if not await device.keystore.get(str(target)):
            raise RuntimeError("no stored pairing key: run stage 2 first")
        connection = await transport.guarded(device.connect_classic(target, timeout=8), 10)
        connection.on(connection.EVENT_DISCONNECTION, lambda *_: link_closed.set())
        transport.set_handle(connection.handle)
        await transport.guarded(connection.authenticate(), 15)
        await transport.guarded(connection.encrypt(), 5)
        emit("phone-connected", encrypted=bool(connection.is_encrypted))

        sdp_client = sdp.Client(connection)
        await transport.guarded(sdp_client.connect(), 5)
        records = await transport.guarded(sdp_client.search_attributes(
            [core.UUID.from_16_bits(HFP_AG)], [sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID]), 5)
        channels = rfcomm_channels(records)
        if len(channels) != 1:
            raise RuntimeError("the hands-free channel is missing or ambiguous")
        channel = channels.pop()
        await transport.guarded(sdp_client.disconnect(), 2)
        rfcomm_client = rfcomm.Client(connection)
        mux = await transport.guarded(rfcomm_client.start(), 5)
        dlc = await transport.guarded(mux.open_dlc(channel), 5)
        protocol = hfp.HfProtocol(dlc, hfp.HfConfiguration([], [], [hfp.AudioCodec.CVSD]))
        await transport.guarded(protocol.initiate_slc(), 8)
        loop_task = asyncio.ensure_future(protocol.run())  # unsolicited results: indicators, ring
        if audio_factory:
            # Open the sound devices before the call, so macOS asks for microphone access now.
            bridge, streams = audio_factory()
            emit("audio-open", **streams.info)
        session = CallSession(device, connection, protocol, transport, audio=bridge)

        emit("waiting-for-call", seconds=listen_seconds)
        await cue()
        await session.wait_for_ring(listen_seconds)
        await asyncio.sleep(1)  # let the ring settle before answering
        summary = await session.answer_and_hold(hold_seconds)
        await session.hang_up()
        gaps = summary["gap_seconds"]
        ok = summary["rx"] > 0 and summary["tx"] > 0 and gaps == 0 and not summary["odd_length"]
        if bridge:
            summary.update(bridge.stats())
            callbacks = max(1, summary["speaker_callbacks"])
            ok = ok and bridge.started and summary["stream_errors"] == 0 \
                and summary["speaker_underruns"] / callbacks <= 0.05
        emit(label + ("-pass" if ok else "-failed"), **summary)
        if not ok:
            raise RuntimeError("voice traffic was missing or irregular")
    except BaseException as error:
        primary = error
        emit(label + "-failed", error_type=type(error).__name__, reason=str(error) or "no detail")
        raise
    finally:
        cleanup = None
        if loop_task:
            loop_task.cancel()
            await asyncio.gather(loop_task, return_exceptions=True)
        if session:
            await session.close()
        if streams:
            streams.close()
        for operation in (
            rfcomm_client.shutdown if rfcomm_client and rfcomm_client.multiplexer else None,
            sdp_client.disconnect if sdp_client and sdp_client.channel else None,
            connection.disconnect if connection else None,
        ):
            if operation and not transport.failure and not link_closed.is_set():
                try:
                    await transport.guarded(operation(), 3)
                except BaseException as error:
                    cleanup = error
                    emit("cleanup-failed", error_type=type(error).__name__)
        try:
            await transport.stop()
        except BaseException:
            if primary is None:
                raise
        if cleanup and primary is None:
            raise RuntimeError("cleanup did not finish") from cleanup


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--listen", type=int, default=60, help="seconds to wait for the call")
    parser.add_argument("--hold", type=int, default=10, help="seconds to hold the call before hanging up")
    parser.add_argument("--keys-path", default=None)
    args = parser.parse_args()
    if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", args.target):
        emit("stage3a-failed", reason="target is not a Bluetooth address")
        return 2
    if not 5 <= args.listen <= 90 or not 3 <= args.hold <= 30:
        emit("stage3a-failed", reason="listen must be 5-90 and hold 3-30 seconds")
        return 2
    try:
        asyncio.run(asyncio.wait_for(run(args.target, args.listen, args.hold, args.keys_path), RUN_LIMIT))
    except BaseException:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

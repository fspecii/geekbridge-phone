# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage 5b: serve the user's own Bluetooth headphones from our stack, with the key macOS holds.

Run by launcher.c with system Bluetooth switched off. One attempt, no loops, no pairing.
Two questions:

1. Sound out: with the imported key, do the headphones accept our stack as their Mac and play
   a test tone over A2DP?
2. Microphone in, without a voice link (AirPods only): while the tone plays, does the AAP
   microphone stream deliver audio? Other headphones answer "no AAP channel", which is a
   result, not a failure.

Needs build/tone.sbc (build.sh makes it when ffmpeg is installed). Wear the headphones.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bumble import hci, l2cap
from bumble.a2dp import A2DP_SBC_CODEC_TYPE, SbcPacketSource
from bumble.apps.player.player import sbc_codec_capabilities
from bumble.avdtp import AVDTP_AUDIO_MEDIA_TYPE, MediaPacketPump, Protocol, find_avdtp_service_with_connection

import gbp_aap
from gbp_acipc import emit
from gbp_companions import CompanionKeyStore, CompanionTransport
from stage5a_mouse import build_device

TONE = Path(__file__).resolve().parent / "build" / "tone.sbc"
MIC_SECONDS = 8


async def open_link(device, transport, target, keys_path=None, secure=True):
    await transport.guarded(device.power_on(), 15)
    device.keystore = CompanionKeyStore(device.public_address, companions=[target], path=keys_path)
    if not await device.keystore.get(str(target)):
        raise RuntimeError("no imported key for the headphones: run keys_import.py first")
    connection = await transport.guarded(device.connect_classic(target, timeout=10), 12)
    if secure:
        await transport.guarded(connection.authenticate(), 10)
        await transport.guarded(connection.encrypt(), 5)
    emit("headphones-connected", encrypted=bool(connection.is_encrypted))
    return connection


async def play_tone(transport, connection, tone_path):
    """Starts the tone and returns (stream, pump, file). The caller stops them."""
    version = await transport.guarded(find_avdtp_service_with_connection(connection), 8)
    if not version:
        raise RuntimeError("the headphones list no A2DP service")
    protocol = await transport.guarded(Protocol.connect(connection, version), 8)
    await transport.guarded(protocol.discover_remote_endpoints(), 8)
    sink = protocol.find_remote_sink_by_codec(AVDTP_AUDIO_MEDIA_TYPE, A2DP_SBC_CODEC_TYPE)
    if sink is None:
        raise RuntimeError("the headphones offer no SBC sink")
    handle = open(tone_path, "rb")

    async def read(count):
        return handle.read(count)

    capabilities = await sbc_codec_capabilities(read)
    handle.seek(0)
    pump = MediaPacketPump(SbcPacketSource(read, protocol.l2cap_channel.peer_mtu).packets)
    source = protocol.add_source(capabilities, pump)
    stream = await transport.guarded(protocol.create_stream(source, sink), 8)
    await transport.guarded(stream.start(), 8)
    emit("tone-playing")
    return stream, pump, handle


AAP_TRIES = 3
AAP_WAIT = 10  # the first live run got no answer within 6 s, and one arrived after we had given up


async def open_aap(transport, connection, wait=AAP_WAIT):
    """Opens Apple's accessory channel and introduces us. None if the headphones have no such channel."""
    channel = None
    for attempt in range(1, AAP_TRIES + 1):
        try:
            channel = await transport.guarded(
                connection.create_l2cap_channel(l2cap.ClassicChannelSpec(gbp_aap.PSM)), wait)
            break
        except Exception as error:
            # The reason matters: "PSM not supported" closes the route, "security block" or
            # "no resources" may only mean the headphones were not ready yet.
            emit("no-aap-channel", attempt=attempt, error=type(error).__name__, reason=str(error)[:160])
            if transport.failure:
                raise
            await asyncio.sleep(2)
    if channel is None:
        return None
    emit("aap-channel-open", attempt=attempt)
    for message in (gbp_aap.HANDSHAKE, gbp_aap.SET_FEATURES, gbp_aap.SUBSCRIBE):
        channel.write(message)
        await asyncio.sleep(0.3)
    return channel


async def listen_to_mic(transport, connection, seconds, counter, channel=None, wait=AAP_WAIT):
    """Asks for the microphone and counts what arrives. False if there is no AAP channel."""
    channel = channel or await open_aap(transport, connection, wait)
    if channel is None:
        return False
    channel.sink = counter.on_sdu
    channel.write(gbp_aap.MIC_START)
    emit("microphone-requested")
    await transport.guarded(asyncio.sleep(seconds), seconds + 2)
    channel.write(gbp_aap.MIC_STOP)
    await asyncio.sleep(0.3)
    return True


async def run(target_text, keys_path=None):
    if not TONE.exists():
        raise RuntimeError("build/tone.sbc is missing: install ffmpeg and run build.sh")
    target = hci.Address(target_text, hci.Address.PUBLIC_DEVICE_ADDRESS)
    transport = CompanionTransport(companions=[target])
    device = build_device(transport)
    counter = gbp_aap.MicCounter()
    handle = None
    await transport.start()
    try:
        connection = await open_link(device, transport, target, keys_path)
        # The accessory channel first: AirPods expect their host to open it right after the link.
        channel = await open_aap(transport, connection)
        if channel is not None:
            channel.sink = counter.on_sdu
        stream, pump, handle = await play_tone(transport, connection, TONE)
        await asyncio.sleep(2)
        has_aap = await listen_to_mic(transport, connection, MIC_SECONDS, counter, channel=channel) \
            if channel is not None else False
        await asyncio.sleep(1)
        if device.keystore.failure:
            raise device.keystore.failure
        result = counter.summary(MIC_SECONDS)
        emit("headphones-result", tone_sent=True, aap_channel=has_aap,
             microphone_passed=result["access_units"] > 0, **result)
        return 0
    finally:
        if handle:
            handle.close()
        try:
            for connection in list(device.connections.values()):
                await asyncio.wait_for(connection.disconnect(), 3)
        except Exception as error:
            emit("disconnect-problem", error=type(error).__name__)
        await transport.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    try:
        code = asyncio.run(run(args.target))
    except Exception as error:
        emit("failed", error=type(error).__name__, detail=str(error))
        code = 11
    sys.exit(code)


if __name__ == "__main__":
    main()

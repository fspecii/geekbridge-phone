# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage 5c: a real call, the mouse and the headphones, all served by our stack at once.

Run by launcher.c with system Bluetooth switched off. One attempt, no loops, no pairing.
The question is whether the controller carries all of it together: the phone's one voice
link, the mouse's HID traffic, A2DP to the headphones and, for AirPods, the AAP microphone
stream, with no firmware crash. Sound is not bridged yet: the call carries silence (as in
stage 3a), the headphones play the test tone, and the microphone stream is only counted.

All three keys come from macOS (keys_import.py). The mouse and headphones are read from
stage5c.json beside the key file; run_stage5.sh writes it. Ring the phone after the cue and
do not answer on the phone; move the mouse during the call.
"""
import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bumble import core, hci, hfp, rfcomm, sdp
from bumble.hid import Host as HidHost, Message

import gbp_aap
from gbp_acipc import emit
from gbp_companions import CompanionKeyStore, CompanionTransport, MagicMouse
from stage3a_call import HFP_AG, CallSession, cue, rfcomm_channels
from stage5a_mouse import MouseSession, build_device
from stage5b_headphones import listen_to_mic, play_tone

CONFIG = Path.home() / ".local/share/geekbridge-phone/mac-bt/stage5c.json"
TONE = Path(__file__).resolve().parent / "build" / "tone-long.sbc"
LISTEN_SECONDS = 60
HOLD_SECONDS = 15
MIC_SECONDS = 8
RUN_LIMIT = 190


def address(text):
    return hci.Address(text, hci.Address.PUBLIC_DEVICE_ADDRESS)


async def start_companions(device, transport, mouse, headphones, tone, mouse_session, secure=True):
    """Brings up the mouse host and the headphones' tone. Returns (hid, tone file)."""
    hid = HidHost(device)
    hid.on(hid.EVENT_INTERRUPT_DATA, mouse_session.on_interrupt)

    def enable_multitouch(connection):
        async def later():
            for _ in range(50):
                if hid.l2cap_ctrl_channel:
                    hid.set_report(Message.ReportType.FEATURE_REPORT, MagicMouse.ENABLE_MULTITOUCH)
                    emit("mouse-connected")
                    return
                await asyncio.sleep(0.1)

        if connection.peer_address == mouse:
            asyncio.ensure_future(later())

    device.on(device.EVENT_CONNECTION, enable_multitouch)
    link = await transport.guarded(device.connect_classic(headphones, timeout=10), 12)
    if secure:
        await transport.guarded(link.authenticate(), 10)
        await transport.guarded(link.encrypt(), 5)
    emit("headphones-connected", encrypted=bool(link.is_encrypted))
    _, _, handle = await play_tone(transport, link, tone)
    return hid, handle, link


async def open_phone(device, transport, phone):
    connection = await transport.guarded(device.connect_classic(phone, timeout=8), 10)
    await transport.guarded(connection.authenticate(), 15)
    await transport.guarded(connection.encrypt(), 5)
    emit("phone-connected", encrypted=bool(connection.is_encrypted))
    client = sdp.Client(connection)
    await transport.guarded(client.connect(), 5)
    records = await transport.guarded(client.search_attributes(
        [core.UUID.from_16_bits(HFP_AG)], [sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID]), 5)
    channels = rfcomm_channels(records)
    if len(channels) != 1:
        raise RuntimeError("the hands-free channel is missing or ambiguous")
    await transport.guarded(client.disconnect(), 2)
    mux = await transport.guarded(rfcomm.Client(connection).start(), 5)
    dlc = await transport.guarded(mux.open_dlc(channels.pop()), 5)
    protocol = hfp.HfProtocol(dlc, hfp.HfConfiguration([], [], [hfp.AudioCodec.CVSD]))
    await transport.guarded(protocol.initiate_slc(), 8)
    return connection, protocol


def verdict(voice, mouse, mic, has_aap):
    """Pure logic: what the run showed. Voice must be clean; the companions are reported one by one."""
    voice_ok = voice["rx"] > 0 and voice["tx"] > 0 and voice["gap_seconds"] == 0 and not voice["odd_length"]
    return {"voice_ok": voice_ok, "mouse_ok": mouse["decoded"] > 0,
            "microphone_ok": mic["access_units"] > 0 if has_aap else None,
            "passed": voice_ok and mouse["decoded"] > 0}


async def run(phone_text, config_path=CONFIG, keys_path=None):
    if not TONE.exists():
        raise RuntimeError("build/tone-long.sbc is missing: install ffmpeg and run build.sh")
    config = json.loads(Path(config_path).read_text())
    phone, mouse, headphones = address(phone_text), address(config["mouse"]), address(config["headphones"])
    transport = CompanionTransport(phone, [mouse, headphones])
    device = build_device(transport)
    mouse_session, counter = MouseSession(), gbp_aap.MicCounter()
    handle = loop_task = session = None
    await transport.start()
    try:
        await transport.guarded(device.power_on(), 15)
        device.keystore = CompanionKeyStore(device.public_address, companions=[phone, mouse, headphones], path=keys_path)
        for name, who in (("phone", phone), ("mouse", mouse), ("headphones", headphones)):
            if not await device.keystore.get(str(who)):
                raise RuntimeError(f"no imported key for the {name}: run keys_import.py first")
        connection, protocol = await open_phone(device, transport, phone)
        loop_task = asyncio.ensure_future(protocol.run())
        session = CallSession(device, connection, protocol, transport)
        _, handle, buds_link = await start_companions(device, transport, mouse, headphones, TONE, mouse_session)
        emit("waiting-for-call", seconds=LISTEN_SECONDS)
        await cue()
        await session.wait_for_ring(LISTEN_SECONDS)
        await asyncio.sleep(1)
        mic = asyncio.ensure_future(_mic_during_call(transport, buds_link, session, counter))
        voice = await session.answer_and_hold(HOLD_SECONDS)
        has_aap = await mic
        await session.hang_up()
        if device.keystore.failure:
            raise device.keystore.failure
        mouse_result, mic_result = mouse_session.summary(), counter.summary(MIC_SECONDS)
        result = verdict(voice, mouse_result, mic_result, has_aap)
        emit("stage5c-pass" if result["passed"] else "stage5c-failed", **result,
             voice=voice, mouse=mouse_result, microphone=mic_result)
        return 0 if result["passed"] else 12
    finally:
        if loop_task:
            loop_task.cancel()
            await asyncio.gather(loop_task, return_exceptions=True)
        if session:
            await session.close()
        if handle:
            handle.close()
        try:
            for link in list(device.connections.values()):
                await asyncio.wait_for(link.disconnect(), 3)
        except Exception as error:
            emit("disconnect-problem", error=type(error).__name__)
        await transport.stop()


async def _mic_during_call(transport, link, session, counter):
    """Starts the microphone request once the voice link is up, so both run together."""
    await session.sco_ready.wait()
    return await listen_to_mic(transport, link, MIC_SECONDS, counter)


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    try:
        code = asyncio.run(asyncio.wait_for(run(args.target), RUN_LIMIT))
    except BaseException as error:
        emit("stage5c-failed", error=type(error).__name__, detail=str(error))
        code = 11
    sys.exit(code)


if __name__ == "__main__":
    main()

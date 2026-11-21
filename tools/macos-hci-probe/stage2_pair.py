# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage 2: pair one phone with our own Bluetooth stack and open its hands-free channel.

Run by launcher.c with system Bluetooth switched off. One attempt, no loops. No calls, no audio.
Each step prints one JSON line. The structure follows the reference project lzsnolimit/ai-caller
(MIT); the code is ours.
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

from gbp_acipc import AcipcTransport, NoFirmwareHost, TargetKeyStore, emit

HFP_AG = 0x111F
PBAP_PSE = 0x112F
MAP_MAS = 0x1132
RUN_LIMIT = 85  # seconds for the whole run


async def same_number_on_phone(code, wait=40):
    """Ask on the Mac whether the phone shows the same number. Anything but a clear yes is a no."""
    if not re.fullmatch(r"\d{6}", code):
        return False
    script = (
        f'display dialog "Pairing code: {code}\\n\\nDoes the phone show the same number?" '
        f'with title "GeekBridge Phone" buttons {{"No", "Yes, same number"}} '
        f'default button "No" giving up after {wait}'
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate(), wait + 5)
    except Exception:
        return False
    text = out.decode(errors="replace")
    return "button returned:Yes, same number" in text and "gave up:false" in text


class DialogDelegate(pairing.PairingDelegate):
    def __init__(self):
        super().__init__(io_capability=self.DISPLAY_OUTPUT_AND_YES_NO_INPUT)

    async def confirm(self, auto=False):
        return False

    async def compare_numbers(self, number, digits):
        code = f"{number:0{digits}d}"
        emit("numeric-comparison-required", digits=code)
        started = asyncio.get_running_loop().time()
        matched = await same_number_on_phone(code)
        emit("numeric-comparison-result", matched=matched,
             seconds=round(asyncio.get_running_loop().time() - started, 1))
        return matched


def rfcomm_channels(records):
    channels = set()
    for record in records:
        for attribute in record:
            for descriptor in attribute.value.value:
                elements = descriptor.value
                if len(elements) >= 2 and elements[0].value == core.BT_RFCOMM_PROTOCOL_ID:
                    channels.add(elements[1].value)
    return channels


async def run(target_text, keys_path=None):
    target = hci.Address(target_text, hci.Address.PUBLIC_DEVICE_ADDRESS)
    transport = AcipcTransport(target)
    host = NoFirmwareHost(controller_source=transport, controller_sink=transport)
    config = DeviceConfiguration(
        name="Mac", le_enabled=False, classic_enabled=True, classic_smp_enabled=False,
        classic_accept_any=False, connectable=False, discoverable=False, keystore=None)
    device = Device(config=config, host=host)
    device.command_timeout = 3
    device.pairing_config_factory = lambda connection: pairing.PairingConfig(
        sc=True, mitm=True, bonding=True, delegate=DialogDelegate())
    connection = sdp_client = rfcomm_client = None
    primary = None
    link_closed = asyncio.Event()
    await transport.start()
    try:
        await transport.guarded(device.power_on(), 15)
        if host.local_version is None or host.local_version.status:
            raise RuntimeError("controller gave no valid version")
        emit("stack-ready", hci_version=int(host.local_version.hci_version))
        device.keystore = TargetKeyStore(device.public_address, target, keys_path)

        connection = await transport.guarded(device.connect_classic(target, timeout=8), 10)
        connection.on(connection.EVENT_DISCONNECTION, lambda *_: link_closed.set())
        transport.set_handle(connection.handle)
        emit("phone-connected")

        await transport.guarded(connection.authenticate(), 50)
        await transport.guarded(connection.encrypt(), 5)
        await asyncio.sleep(0.1)  # the key update is scheduled by the device
        if device.keystore.failure:
            raise device.keystore.failure
        keys = await device.keystore.get(str(target))
        if not keys or not keys.link_key.authenticated:
            raise RuntimeError("no authenticated key was stored")
        emit("phone-paired", encrypted=bool(connection.is_encrypted), key_saved=True)

        sdp_client = sdp.Client(connection)
        await transport.guarded(sdp_client.connect(), 5)
        found = {}
        for name, uuid in (("hands_free_gateway", HFP_AG), ("phonebook", PBAP_PSE), ("messages", MAP_MAS)):
            handles = await transport.guarded(
                sdp_client.search_services([core.UUID.from_16_bits(uuid)]), 5)
            found[name] = len(handles)
        emit("phone-services", **found)
        if not found["hands_free_gateway"]:
            raise RuntimeError("the phone offers no hands-free gateway service")
        records = await transport.guarded(sdp_client.search_attributes(
            [core.UUID.from_16_bits(HFP_AG)], [sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID]), 5)
        channels = rfcomm_channels(records)
        if len(channels) != 1:
            raise RuntimeError("the hands-free channel is missing or ambiguous")
        channel = channels.pop()
        if not isinstance(channel, int) or not 1 <= channel <= 30:
            raise RuntimeError("the hands-free channel number is not valid")
        await transport.guarded(sdp_client.disconnect(), 2)

        rfcomm_client = rfcomm.Client(connection)
        mux = await transport.guarded(rfcomm_client.start(), 5)
        dlc = await transport.guarded(mux.open_dlc(channel), 5)
        emit("hands-free-channel-open")
        protocol = hfp.HfProtocol(dlc, hfp.HfConfiguration([], [], [hfp.AudioCodec.CVSD]))
        # Only the opening exchange of features and indicators. No run(), no audio, no calls.
        await transport.guarded(protocol.initiate_slc(), 8)
        emit("stage2-pass", indicators=len(protocol.ag_indicators), audio_started=False, call_started=False)
    except BaseException as error:
        primary = error
        emit("stage2-failed", error_type=type(error).__name__, reason=str(error))
        raise
    finally:
        cleanup = None
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
    parser.add_argument("--target", required=True, help="BD_ADDR of the one phone, AA:BB:CC:DD:EE:FF")
    parser.add_argument("--keys-path", default=None)
    args = parser.parse_args()
    if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", args.target):
        emit("stage2-failed", reason="target is not a Bluetooth address")
        return 2
    try:
        asyncio.run(asyncio.wait_for(run(args.target, args.keys_path), RUN_LIMIT))
    except BaseException:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

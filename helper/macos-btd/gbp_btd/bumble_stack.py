# SPDX-License-Identifier: GPL-3.0-or-later
"""The real Bluetooth stack: Google's Bumble over the Mac's built-in controller.

This is the code that passed stages 2 and 3 as test programs (tools/macos-hci-probe), reshaped
behind the interface in stack.py. The design follows the reference project lzsnolimit/ai-caller
(MIT); the code here is ours. Everything goes through the safety filter in acipc.py.

Imports Bumble and loads the native bridge only when used.
"""
import asyncio
import contextlib

from bumble import core, hci, hfp, pairing, rfcomm, sdp
from bumble.device import Device, DeviceConfiguration

from .acipc import AcipcTransport, NoFirmwareHost, ScopeError, TargetKeyStore
from .stack import CallRow, Channel, HfpSession, Link, Nearby, Stack, StackError

HFP_AG, PBAP_PSE, MAP_MAS = 0x111F, 0x112F, 0x1132
SERVICE_UUIDS = {"map": MAP_MAS, "pbap": PBAP_PSE}
# Class of device: audio service, audio/video major class, car audio minor class. The phone then
# treats the Mac as a car kit: Android offers "Phone calls", an iPhone offers its car options.
CLASS_OF_DEVICE = 0x200408
GOOD = hci.HCI_SynchronousDataPacket.Status.CORRECTLY_RECEIVED_DATA


class ConfirmDelegate(pairing.PairingDelegate):
    """Pairing with a code: we show it through `confirm`, the user compares it on the phone."""

    def __init__(self, confirm=None):
        super().__init__(io_capability=self.DISPLAY_OUTPUT_AND_YES_NO_INPUT if confirm else self.NO_OUTPUT_NO_INPUT)
        self._confirm = confirm

    async def confirm(self, auto=False):
        return False

    async def compare_numbers(self, number, digits):
        if self._confirm is None:
            return False  # a normal connection never pairs: a missing key is a failure
        return bool(await self._confirm(f"{number:0{digits}d}"))


def _channels(records):
    found = set()
    for record in records:
        for attribute in record:
            for descriptor in attribute.value.value:
                elements = descriptor.value
                if len(elements) >= 2 and elements[0].value == core.BT_RFCOMM_PROTOCOL_ID:
                    found.add(elements[1].value)
    return found


SERVICE_NAME_ID = 0x0100
SUPPORTED_MESSAGE_TYPES_ID = 0x0316  # MAP: bit 0x02 SMS (GSM), 0x04 SMS (CDMA); 0x01 is email
SMS_BITS = 0x06


def candidates(records):
    """Every RFCOMM server channel in the SDP records, with the record's name and, for MAP, its message types."""
    found = []
    for record in records:
        attributes = {attribute.id: attribute.value for attribute in record}
        channels = set()
        descriptors = attributes.get(sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID)
        for descriptor in (descriptors.value if descriptors is not None else []):
            elements = descriptor.value
            if len(elements) >= 2 and elements[0].value == core.BT_RFCOMM_PROTOCOL_ID:
                channels.add(elements[1].value)
        name = attributes.get(SERVICE_NAME_ID)
        text = ""
        if name is not None:
            text = name.value.decode(errors="replace") if isinstance(name.value, bytes) else str(name.value)
            text = text.rstrip("\x00")  # SDP text strings often end with a NUL
        types = attributes.get(SUPPORTED_MESSAGE_TYPES_ID)
        message_types = types.value if types is not None and isinstance(types.value, int) else None
        found.extend({"channel": c, "name": text[:40], "types": message_types} for c in channels)
    return found


def choose_channel(found, prefer_sms=False):
    """The channel to use. For messages, the instance that carries SMS when the phone says which one does."""
    valid = [c for c in found if isinstance(c["channel"], int) and 1 <= c["channel"] <= 30]
    if not valid:
        raise StackError("no_service", "the phone does not offer that service")
    if prefer_sms:
        sms = [c for c in valid if c["types"] is not None and c["types"] & SMS_BITS]
        if sms:
            return min(sms, key=lambda c: c["channel"])
    return min(valid, key=lambda c: c["channel"])


class BumbleChannel(Channel):
    def __init__(self, dlc, log=None):
        self.dlc = dlc
        self.log = log or (lambda *a: None)
        self.queue = asyncio.Queue()
        self.read_chunks = self.written_chunks = 0
        dlc.sink = self._data
        dlc.on(dlc.EVENT_CLOSE, self._closed)

    def _data(self, data):
        self.read_chunks += 1
        if self.read_chunks <= 5:
            self.log("service-channel received", len(data), "bytes")
        self.queue.put_nowait(bytes(data))

    def _closed(self, *args):
        self.log("service-channel closed by the phone or the link")
        self.queue.put_nowait(b"")

    async def read(self):
        return await self.queue.get()

    async def write(self, data):
        self.written_chunks += 1
        if self.written_chunks <= 5:
            self.log("service-channel write", len(data), "bytes; credits to send:", self.dlc.tx_credits)
        self.dlc.write(bytes(data))
        try:
            await asyncio.wait_for(self.dlc.drain(), 12)
        except asyncio.TimeoutError:
            self.log("service-channel write was not sent in 12 s; credits to send:", self.dlc.tx_credits)
            raise
        if self.written_chunks <= 5:
            self.log("service-channel write sent")

    async def close(self):
        with contextlib.suppress(Exception):
            await self.dlc.disconnect()
        self.queue.put_nowait(b"")


class BumbleHfp(HfpSession):
    def __init__(self, stack, link, protocol):
        self.stack, self.link, self.protocol = stack, link, protocol
        self.device, self.connection = stack.device, link.connection
        self.sco_link = None
        self.accept_tasks = set()
        protocol.on(protocol.EVENT_AG_INDICATOR, self._indicator)
        protocol.on(protocol.EVENT_CLI_NOTIFICATION, self._caller)
        self.connection.on(self.device.EVENT_SCO_REQUEST, self._sco_requested)
        self.connection.on(self.device.EVENT_SCO_CONNECTION, self._sco_connected)
        self.connection.on(self.device.EVENT_SCO_CONNECTION_FAILURE, self._sco_failed)
        self.loop_task = asyncio.ensure_future(protocol.run())  # unsolicited results: indicators, ring

    def _indicator(self, state):
        if self.on_indicator:
            self.on_indicator(getattr(state.indicator, "value", str(state.indicator)), state.current_status)

    def _caller(self, line):
        if self.on_caller and getattr(line, "number", None):
            self.on_caller(line.number)

    # Voice link: the phone asks, we accept (one link only; the filter refuses a second one).
    def _sco_requested(self, link_type):
        kind = hfp.DefaultCodecParameters.SCO_CVSD_D1 if link_type == 0 else hfp.DefaultCodecParameters.ESCO_CVSD_S1

        async def accept():
            try:
                await self.stack.guarded(self.device.send_async_command(
                    hci.HCI_Enhanced_Accept_Synchronous_Connection_Request_Command(
                        bd_addr=self.connection.peer_address, **hfp.ESCO_PARAMETERS[kind].asdict())), 5)
            except Exception:
                pass  # the phone reports a failed voice link by itself

        task = asyncio.ensure_future(accept())
        self.accept_tasks.add(task)
        task.add_done_callback(self.accept_tasks.discard)

    def _sco_connected(self, link):
        self.sco_link = link
        link.sink = self._packet
        link.on(link.EVENT_DISCONNECTION, lambda *a: setattr(self, "sco_link", None))

    def _sco_failed(self, status):
        pass

    def _packet(self, packet):
        if self.on_voice:
            self.on_voice(packet.data, packet.packet_status == GOOD)

    def send_voice(self, payload):
        link = self.sco_link
        if link is not None:
            self.device.host.send_sco_sdu(link.handle, payload)

    # Commands.
    async def _run(self, coro, seconds=5):
        try:
            return await self.stack.guarded(coro, seconds)
        except TimeoutError:
            raise StackError("timeout", "the phone did not answer in time") from None
        except StackError:
            raise
        except Exception as error:
            raise StackError("refused", f"{type(error).__name__}") from None

    async def answer(self):
        await self._run(self.protocol.answer_incoming_call())

    async def reject(self):
        await self._run(self.protocol.terminate_call())

    async def hang_up(self):
        await self._run(self.protocol.terminate_call())

    async def dial(self, number):
        await self._run(self.protocol.execute_command(f"ATD{number};", timeout=5))

    async def send_tones(self, tones):
        for tone in tones:
            await self._run(self.protocol.execute_command(f"AT+VTS={tone}", timeout=3))

    async def query_calls(self):
        calls = await self._run(self.protocol.query_current_calls(), 4)
        return [CallRow(number=call.number or "") for call in calls]

    async def close(self):
        self.loop_task.cancel()
        await asyncio.gather(self.loop_task, *self.accept_tasks, return_exceptions=True)
        for task in tuple(self.accept_tasks):
            task.cancel()
        self.on_voice = self.on_indicator = self.on_caller = None


class BumbleLink(Link):
    def __init__(self, stack, connection, address):
        self.stack, self.connection, self.address = stack, connection, address
        self.mux = None
        self.rfcomm_client = None
        self.session = None
        self.closed = False
        connection.on(connection.EVENT_DISCONNECTION, lambda *a: self._lost())

    def _lost(self):
        if not self.closed and self.on_lost:
            self.on_lost()

    async def _multiplexer(self):
        if self.mux is None:
            self.rfcomm_client = rfcomm.Client(self.connection)
            self.mux = await self.stack.guarded(self.rfcomm_client.start(), 5)
        return self.mux

    async def _server_channel(self, uuid, prefer_sms=False):
        client = sdp.Client(self.connection)
        await self.stack.guarded(client.connect(), 5)
        try:
            records = await self.stack.guarded(client.search_attributes(
                [core.UUID.from_16_bits(uuid)],
                [sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID, SERVICE_NAME_ID, SUPPORTED_MESSAGE_TYPES_ID]), 5)
        finally:
            with contextlib.suppress(Exception):
                await self.stack.guarded(client.disconnect(), 2)
        found = candidates(records)
        self.stack.log("sdp", hex(uuid), "instances:", [(c["channel"], c["name"], c["types"]) for c in found])
        return choose_channel(found, prefer_sms)["channel"]

    async def open_hfp(self):
        channel = await self._server_channel(HFP_AG)
        mux = await self._multiplexer()
        dlc = await self.stack.guarded(mux.open_dlc(channel), 5)
        protocol = hfp.HfProtocol(dlc, hfp.HfConfiguration([], [], [hfp.AudioCodec.CVSD]))
        await self.stack.guarded(protocol.initiate_slc(), 8)
        self.session = BumbleHfp(self.stack, self, protocol)
        return self.session

    async def open_channel(self, kind):
        channel = await self._server_channel(SERVICE_UUIDS[kind], prefer_sms=(kind == "map"))
        mux = await self._multiplexer()
        started = asyncio.get_running_loop().time()
        dlc = await self.stack.guarded(mux.open_dlc(channel), 8)
        self.stack.log(kind, "channel", channel, "open after", round(asyncio.get_running_loop().time() - started, 2),
                       "s; credits to send:", dlc.tx_credits, "to receive:", dlc.rx_credits, "frame size:", dlc.mtu)
        return BumbleChannel(dlc, self.stack.log)

    async def close(self):
        self.closed = True
        if self.rfcomm_client and self.rfcomm_client.multiplexer:
            with contextlib.suppress(Exception):
                await self.stack.guarded(self.rfcomm_client.shutdown(), 3)
        with contextlib.suppress(Exception):
            await self.stack.guarded(self.connection.disconnect(), 3)
        self.stack.transport.set_handle(None)


class BumbleStack(Stack):
    def __init__(self, keys_path=None, library=None, name="Mac", transport_factory=None, skip_security=False):
        self.keys_path, self.library, self.name = keys_path, library, name
        self.transport_factory = transport_factory  # tests pass a virtual controller here
        self.skip_security = skip_security          # tests only: a virtual link has no pairing
        self.transport = self.device = self.keystore = self.watcher = None
        self.log = lambda *a: None  # set by the helper; sizes and timings only, never content

    async def start(self):
        self.transport = self.transport_factory() if self.transport_factory else AcipcTransport(None, self.library)
        host = NoFirmwareHost(controller_source=self.transport, controller_sink=self.transport)
        config = DeviceConfiguration(
            name=self.name, le_enabled=False, classic_enabled=True, classic_smp_enabled=False,
            classic_accept_any=False, connectable=False, discoverable=False, keystore=None,
            class_of_device=CLASS_OF_DEVICE)
        self.device = Device(config=config, host=host)
        self.device.command_timeout = 3
        await self.transport.start()
        try:
            await self.guarded(self.device.power_on(), 15)
            version = host.local_version
            if version is None or version.status:
                raise StackError("fault", "the controller gave no valid version")
        except BaseException:
            await self.transport.stop()
            raise
        self.keystore = TargetKeyStore(self.device.public_address, None, self.keys_path)
        self.device.keystore = self.keystore
        self.watcher = asyncio.ensure_future(self._watch())

    async def stop(self):
        if self.watcher:
            self.watcher.cancel()
            await asyncio.gather(self.watcher, return_exceptions=True)
        if self.transport:
            with contextlib.suppress(Exception):
                await self.transport.stop()

    async def _watch(self):
        """Reports a controller or bridge failure that happens between requests."""
        while True:
            await asyncio.sleep(0.25)
            if self.transport.failure:
                error = StackError("fault", f"{type(self.transport.failure).__name__}: {self.transport.failure}")
                if self.on_fault:
                    self.on_fault(error)
                return

    async def guarded(self, awaitable, seconds):
        try:
            return await self.transport.guarded(awaitable, seconds)
        except (ScopeError, RuntimeError) as error:
            raise StackError("fault", f"{type(error).__name__}: {error}") from None
        except TimeoutError:
            raise StackError("timeout", "no answer in time") from None

    def _scope(self, address):
        target = hci.Address(address, hci.Address.PUBLIC_DEVICE_ADDRESS) if address else None
        self.transport.set_target(target)
        self.keystore.set_target(address)
        return target

    def known_phones(self):
        if not self.keystore:
            return []
        names = self.keystore.names()
        return [(address, names.get(address, "")) for address in self.keystore.known_addresses()]

    async def connect(self, address):
        target = self._scope(address)
        self.device.pairing_config_factory = lambda connection: pairing.PairingConfig(
            sc=True, mitm=True, bonding=True, delegate=ConfirmDelegate(None))
        try:
            connection = await self.guarded(self.device.connect_classic(target, timeout=8), 10)
        except StackError:
            raise
        except Exception as error:
            raise StackError("link_failed", type(error).__name__) from None
        self.transport.set_handle(connection.handle)
        try:
            if not self.skip_security:
                await self.guarded(connection.authenticate(), 15)
                await self.guarded(connection.encrypt(), 5)
        except StackError as error:
            await self._drop(connection)
            raise StackError("auth_failed" if error.code != "fault" else "fault", error.message) from None
        except Exception as error:
            await self._drop(connection)
            raise StackError("auth_failed", type(error).__name__) from None
        await self._learn_name(connection, address)
        return BumbleLink(self, connection, address)

    async def _learn_name(self, connection, address):
        """A phone paired before names were kept shows as its address; ask it once for its name."""
        if not self.keystore or self.keystore.names().get(self.keystore.plain(address)):
            return
        with contextlib.suppress(Exception):
            name = await self.guarded(self.device.request_remote_name(connection), 4)
            if name:
                self.keystore.remember_name(address, name)

    async def _drop(self, connection):
        with contextlib.suppress(Exception):
            await self.guarded(connection.disconnect(), 3)
        self.transport.set_handle(None)

    async def scan(self, seconds):
        found = {}
        self._scope(None)
        self.transport.set_scanning(True)

        def result(address, class_of_device, eir, rssi):
            name = ""
            with contextlib.suppress(Exception):
                name = eir.get(core.AdvertisingData.COMPLETE_LOCAL_NAME) or eir.get(core.AdvertisingData.SHORTENED_LOCAL_NAME) or ""
                if isinstance(name, bytes):
                    name = name.decode(errors="replace")
            found[str(address)] = (address, class_of_device, name)

        self.device.on(self.device.EVENT_INQUIRY_RESULT, result)
        try:
            await self.guarded(self.device.start_discovery(auto_restart=False), 5)
            await asyncio.sleep(seconds)
            with contextlib.suppress(Exception):
                await self.guarded(self.device.stop_discovery(), 5)
            known = {a for a, _ in self.known_phones()}
            phones = []
            for key, (address, cod, name) in list(found.items())[:12]:
                major = (cod >> 8) & 0x1F
                if major not in (0, 2):  # uncategorised or phone
                    continue
                if not name:
                    with contextlib.suppress(Exception):
                        name = await self.guarded(self.device.request_remote_name(address), 4)
                if name:
                    text = str(address).replace("/P", "")
                    phones.append(Nearby(text, name, text in known))
            return phones
        finally:
            self.transport.set_scanning(False)
            with contextlib.suppress(Exception):
                self.device.remove_listener(self.device.EVENT_INQUIRY_RESULT, result)

    async def pair(self, address, confirm):
        target = self._scope(address)
        self.device.pairing_config_factory = lambda connection: pairing.PairingConfig(
            sc=True, mitm=True, bonding=True, delegate=ConfirmDelegate(confirm))
        connection = None
        try:
            connection = await self.guarded(self.device.connect_classic(target, timeout=8), 10)
            self.transport.set_handle(connection.handle)
            await self.guarded(connection.authenticate(), 50)
            await self.guarded(connection.encrypt(), 5)
            await asyncio.sleep(0.1)  # the key update is scheduled by the device
            if self.keystore.failure:
                raise StackError("auth_failed", "the key could not be stored")
            keys = await self.keystore.get(address)
            if not keys or not keys.link_key.authenticated:
                raise StackError("auth_failed", "no authenticated key was stored")
            name = ""
            with contextlib.suppress(Exception):
                name = await self.guarded(self.device.request_remote_name(connection), 4)
            if name:
                self.keystore.remember_name(address, name)
            return name
        except StackError:
            raise
        except Exception as error:
            raise StackError("auth_failed", type(error).__name__) from None
        finally:
            if connection is not None:
                await self._drop(connection)

    async def forget(self, address):
        await self.keystore.delete(address)

    def info(self):
        import bumble
        version = self.device.host.local_version if self.device else None
        return {
            "stack": "bumble", "bumble": getattr(bumble, "__version__", "unknown"),
            "controller_address": str(self.device.public_address).replace("/P", "") if self.device else "",
            "hci_version": int(version.hci_version) if version else None,
        }

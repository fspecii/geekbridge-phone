# SPDX-License-Identifier: GPL-3.0-or-later
"""One phone at a time: connect, keep the hands-free link up, track the call, carry the voice.

Reconnects with growing pauses. A controller fault is not retried: it is reported and the
manager stops, so the app can restore Bluetooth. Voice follows the call automatically.
"""
import asyncio
import contextlib

from .calls import CallTracker, Phase
from .sco import VoiceEcho
from .server import HandlerError
from .stack import StackError

CALL_ID = "call-1"
STATE_NAMES = {Phase.RINGING: "incoming", Phase.DIALING: "dialing", Phase.ALERTING: "alerting", Phase.ACTIVE: "active"}


class PhoneManager:
    def __init__(self, stack, emit, voice_port, sleep=asyncio.sleep, backoff=(3, 6, 12, 24, 48), log=None):
        self.stack, self.emit, self.port = stack, emit, voice_port
        self.sleep, self.backoff = sleep, backoff
        self.log = log or (lambda *a: None)
        self.state = "idle"  # idle, connecting, connected, fault
        self.selected = None
        self.link = None
        self.session = None
        self.fault = None
        self.tracker = CallTracker()
        self.echo = None
        self.task = None
        self.lost = None
        self.paused_count = 0
        self.last_error = None

    # Selection and lifecycle.
    async def select(self, address):
        if self.selected == address and self.task and not self.task.done():
            return
        await self.stop()
        self.selected = address
        self.fault = None
        self.task = asyncio.ensure_future(self._run(address))

    async def stop(self):
        task, self.task = self.task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._teardown()
        if self.state != "fault":
            self.state = "idle"

    @contextlib.asynccontextmanager
    async def paused(self):
        """Drops the link while something else (scan, pairing) needs the controller, then reconnects."""
        address = self.selected
        had_task = self.task is not None and not self.task.done()
        await self.stop()
        try:
            yield
        finally:
            if had_task and address and self.state != "fault":
                self.selected = None
                await self.select(address)

    async def _teardown(self):
        session, link = self.session, self.link
        self.session = self.link = None
        self.echo = None
        was_active = self.tracker.phase != Phase.IDLE
        self.tracker = CallTracker()
        self.port.reset()
        for closer in (session and session.close, link and link.close):
            if closer:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(closer(), 5)
        if was_active:
            await self.emit("call_state", id=CALL_ID, state="ended", number="")

    async def _run(self, address):
        attempt = 0
        while True:
            self.state = "connecting"
            try:
                link = await self.stack.connect(address)
                session = await link.open_hfp()
            except StackError as error:
                if error.code == "fault":
                    await self._enter_fault(error)
                    return
                if self.last_error != error.code:
                    self.last_error = error.code
                    await self.emit("phone_error", address=address, code=error.code, message=error.message)
                await self.sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
                attempt += 1
                continue
            attempt = 0
            self.last_error = None
            self.link, self.session = link, session
            self.lost = asyncio.Event()
            link.on_lost = self.lost.set
            self._wire(session)
            self.state = "connected"
            await self.emit("phone_connected", address=address)
            await self.lost.wait()
            await self._teardown()
            await self.emit("phone_disconnected", address=address)
            await self.sleep(2)

    async def _enter_fault(self, error):
        self.fault = {"code": error.code, "message": error.message}
        self.state = "fault"
        await self._teardown()
        await self.emit("fault", code=error.code, message=error.message)

    # Calls.
    def _wire(self, session):
        self.tracker = CallTracker()
        self.echo = VoiceEcho(lambda handle, payload: session.send_voice(payload),
                              source=self.port.uplink, sink=self.port.downlink)
        session.on_indicator = self._indicator
        session.on_caller = self.tracker.on_number
        session.on_voice = self._voice

    def _voice(self, data, ok):
        echo = self.echo
        if echo is None:
            return
        echo.on_packet(0, data, ok)
        if echo.failure:
            failure, self.echo = echo.failure, None
            asyncio.ensure_future(self.emit("fault", code="voice", message=str(failure)))

    def _indicator(self, name, value):
        for event in self.tracker.on_indicator(name, value):
            asyncio.ensure_future(self._call_event(event))

    async def _call_event(self, event):
        if event == "ringing" and self.session and not self.tracker.number:
            try:
                rows = await asyncio.wait_for(self.session.query_calls(), 3)
                for row in rows:
                    self.tracker.on_number(row.number)
            except Exception:
                pass  # the ring is reported without a number
        state = STATE_NAMES.get(self.tracker.phase, "ended")
        await self.emit("call_state", id=CALL_ID, state=state, number=self.tracker.number or "")

    def calls(self):
        phase = self.tracker.phase
        if phase == Phase.IDLE:
            return []
        return [{"id": CALL_ID, "state": STATE_NAMES[phase], "number": self.tracker.number or "", "name": ""}]

    def _need_session(self):
        if self.session is None:
            raise HandlerError("no_phone", "no phone is connected")
        return self.session

    async def _command(self, coro):
        try:
            return await coro
        except StackError as error:
            raise HandlerError(error.code, error.message) from None

    async def dial(self, number):
        await self._command(self._need_session().dial(number))

    async def answer(self):
        await self._command(self._need_session().answer())

    async def hang_up(self):
        session = self._need_session()
        if self.tracker.phase == Phase.RINGING:
            await self._command(session.reject())
        else:
            await self._command(session.hang_up())

    async def tones(self, tones):
        await self._command(self._need_session().send_tones(tones))

    async def fault_from_outside(self, error):
        """The controller failed while no request was running (the stack reports it by itself)."""
        task, self.task = self.task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._enter_fault(error)

    def snapshot(self):
        return {"state": self.state, "selected": self.selected, "fault": self.fault,
                "calls": self.calls(), "voice": self.echo.stats.summary() if self.echo else None}

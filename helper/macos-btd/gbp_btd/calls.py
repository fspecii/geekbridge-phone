# SPDX-License-Identifier: GPL-3.0-or-later
"""Call state from the hands-free indicators. Pure logic: no Bluetooth, no Bumble.

The audio gateway reports two indicators that matter: `call` (0 or 1) and `callsetup`
(0 none, 1 incoming, 2 outgoing, 3 outgoing and ringing at the other end).
"""
from enum import Enum


class Phase(Enum):
    IDLE = "idle"
    RINGING = "ringing"
    DIALING = "dialing"
    ALERTING = "alerting"
    ACTIVE = "active"


class CallTracker:
    """Turns indicator changes into named events: ringing, dialing, alerting, active, ended, missed."""

    def __init__(self):
        self.call = 0
        self.setup = 0
        self.phase = Phase.IDLE
        self.number = None
        self.was_active = False

    def on_number(self, number):
        if number:
            self.number = str(number)

    def on_indicator(self, name, value):
        """Returns the list of events this change caused (usually zero or one)."""
        value = int(value)
        if name == "call":
            self.call = value
        elif name == "callsetup":
            self.setup = value
        else:
            return []
        before = self.phase
        if self.call == 1:
            self.phase, self.was_active = Phase.ACTIVE, True
        elif self.setup == 1:
            self.phase = Phase.RINGING
        elif self.setup == 2:
            self.phase = Phase.DIALING
        elif self.setup == 3:
            self.phase = Phase.ALERTING
        else:
            self.phase = Phase.IDLE
        if self.phase == before:
            return []
        if self.phase == Phase.IDLE:
            event = "ended" if self.was_active else ("missed" if before == Phase.RINGING else "ended")
            self.was_active = False
            self.number = None
            return [event]
        return [self.phase.value]


def last_digits(number, count=3):
    """A short tail of a number, for logs that must not hold the whole number."""
    digits = "".join(c for c in str(number or "") if c.isdigit())
    return digits[-count:] if digits else ""

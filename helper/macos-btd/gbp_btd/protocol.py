# SPDX-License-Identifier: GPL-3.0-or-later
"""The message format between the app and the helper. Pure logic: no sockets, no Bluetooth.

One JSON object per line, UTF-8, at most MAX_LINE bytes.

  request   {"id": 7, "op": "dial", "number": "+441234567"}
  response  {"id": 7, "ok": true, ...}   or   {"id": 7, "ok": false, "error": {"code": "...", "message": "..."}}
  event     {"event": "call_state", ...}   (no id; the helper sends these on its own)

Every request is checked here before anything acts on it.
"""
import json
import re

MAX_LINE = 64 * 1024

ADDRESS = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
NUMBER = re.compile(r"^[0-9+*#,;]{1,32}$")
TONES = re.compile(r"^[0-9A-D*#]{1,32}$")
SERVICES = ("map", "pbap")


class ProtocolError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _address(value):
    if not isinstance(value, str) or not ADDRESS.match(value.upper()):
        raise ProtocolError("bad_request", "address must look like AA:BB:CC:DD:EE:FF")
    return value.upper()


def _number(value):
    if not isinstance(value, str) or not NUMBER.match(value):
        raise ProtocolError("bad_request", "number may hold digits and + * # , ; only, up to 32 characters")
    return value


def _tones(value):
    if not isinstance(value, str) or not TONES.match(value):
        raise ProtocolError("bad_request", "tones may hold 0-9 A-D * # only, up to 32 characters")
    return value


def _seconds(value):
    if not isinstance(value, int) or isinstance(value, bool) or not 3 <= value <= 30:
        raise ProtocolError("bad_request", "seconds must be a whole number from 3 to 30")
    return value


def _service(value):
    if value not in SERVICES:
        raise ProtocolError("bad_request", "service must be 'map' or 'pbap'")
    return value


def _optional_call_id(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value):
        raise ProtocolError("bad_request", "call_id is not valid")
    return value


# op -> {field: validator}; a field is required unless its validator accepts None and it is listed in OPTIONAL.
OPS = {
    "hello": {},
    "status": {},
    "phones": {},
    "calls": {},
    "dial": {"number": _number},
    "answer": {"call_id": _optional_call_id},
    "hang_up": {"call_id": _optional_call_id},
    "hang_up_all": {},
    "tones": {"tones": _tones},
    "open_service": {"service": _service},
    "select_phone": {"address": _address},
    "scan": {"seconds": _seconds},
    "pair": {"address": _address},
    "unpair": {"address": _address},
    "shutdown": {},
}
OPTIONAL = {("answer", "call_id"), ("hang_up", "call_id")}


def parse_request(line):
    """Returns (id, op, params) or raises ProtocolError. `line` is bytes or str, one message."""
    if isinstance(line, str):
        line = line.encode()
    if len(line) > MAX_LINE:
        raise ProtocolError("too_large", "message is too long")
    try:
        message = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        raise ProtocolError("bad_json", "message is not valid JSON") from None
    if not isinstance(message, dict):
        raise ProtocolError("bad_request", "message must be a JSON object")
    request_id = message.get("id")
    if not isinstance(request_id, int) or isinstance(request_id, bool) or request_id < 0:
        raise ProtocolError("bad_request", "id must be a non-negative whole number")
    op = message.get("op")
    if op not in OPS:
        raise ProtocolError("unknown_op", f"unknown operation {op!r}" if isinstance(op, str) else "op is missing")
    spec = OPS[op]
    unknown = set(message) - set(spec) - {"id", "op"}
    if unknown:
        raise ProtocolError("bad_request", f"unexpected field {sorted(unknown)[0]!r}")
    params = {}
    for name, check in spec.items():
        if name not in message:
            if (op, name) in OPTIONAL:
                params[name] = None
                continue
            raise ProtocolError("bad_request", f"{name} is required")
        params[name] = check(message[name])
    return request_id, op, params


def encode(message):
    data = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
    if len(data) > MAX_LINE:
        raise ProtocolError("too_large", "reply is too long")
    return data


def ok(request_id, **fields):
    return {"id": request_id, "ok": True, **fields}


def error(request_id, code, message):
    return {"id": request_id, "ok": False, "error": {"code": code, "message": message}}


def event(event_name, /, **fields):
    return {"event": event_name, **fields}

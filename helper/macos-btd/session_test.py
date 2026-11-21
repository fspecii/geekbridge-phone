# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage 4a live test: the helper, driven through its sockets the way the app will drive it.

Run by the launcher (tools/macos-hci-probe/launcher.c) with system Bluetooth switched off. One
attempt, no loops. The helper runs in this process with the real stack; a blocking client plays
the app. It checks: hello, the phone list, connecting to the selected phone, an OBEX handshake on
the contacts and messages services, then (with a call from a second phone) ring, answer, voice
frames, hang up. Pairing keys are never printed.
"""
import argparse
import asyncio
import faulthandler
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gbp_btd.app import Helper
from gbp_btd.voice import DOWN, FrameReader

# OBEX targets: the service identifiers the phone expects in the Connect request.
TARGETS = {"pbap": "796135f0f0c511d809660800200c9a66", "map": "bb582b40420c11dbb0de0800200c9a66"}


def say(stage, **values):
    print(json.dumps({"stage": stage, **values}), flush=True)


def obex_connect(kind):
    uuid = bytes.fromhex(TARGETS[kind])
    headers = b"\x46" + (3 + len(uuid)).to_bytes(2, "big") + uuid
    body = b"\x10\x00\x20\x00" + headers  # version 1.0, no flags, 8192-byte packets, then the Target header
    return b"\x80" + (3 + len(body)).to_bytes(2, "big") + body


class AppClient:
    """A blocking stand-in for the app: control socket, events set aside while waiting for replies."""

    def __init__(self, directory):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(os.path.join(directory, "btd.sock"))
        self.buffer, self.fds, self.events, self.next_id = b"", [], [], 0

    def _line(self, timeout):
        self.sock.settimeout(timeout)
        while b"\n" not in self.buffer:
            data, fds, _, _ = socket.recv_fds(self.sock, 65536, 4)
            if not data:
                raise ConnectionError("the helper closed the connection")
            self.buffer += data
            self.fds += fds
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def call(self, op, timeout=30, **params):
        self.next_id += 1
        request_id = self.next_id
        self.sock.sendall((json.dumps({"id": request_id, "op": op, **params}) + "\n").encode())
        end = time.monotonic() + timeout
        while True:
            message = self._line(max(0.1, end - time.monotonic()))
            if message.get("id") == request_id:
                return message
            if "event" in message:
                self.events.append(message)

    def event(self, name, timeout):
        end = time.monotonic() + timeout
        while True:
            for i, event in enumerate(self.events):
                if event.get("event") == name:
                    return self.events.pop(i)
            if time.monotonic() > end:
                raise TimeoutError(f"no {name} event in {timeout} s")
            try:
                message = self._line(max(0.1, min(1.0, end - time.monotonic())))
            except socket.timeout:
                continue
            if "event" in message:
                self.events.append(message)

    def close(self):
        self.sock.close()


def probe_service(app, kind, wait=20):
    began = time.monotonic()
    reply = app.call("open_service", service=kind, timeout=40)
    opened = round(time.monotonic() - began, 2)
    if not reply.get("ok"):
        return {"ok": False, "error": reply.get("error"), "open_seconds": opened}
    channel = socket.socket(fileno=app.fds.pop(0))
    channel.settimeout(wait)
    try:
        sent = time.monotonic()
        channel.sendall(obex_connect(kind))
        answer = channel.recv(4096)
        return {"ok": bool(answer) and answer[0] == 0xA0, "response_code": f"0x{answer[0]:02x}" if answer else None,
                "bytes": len(answer), "open_seconds": opened, "answer_seconds": round(time.monotonic() - sent, 2)}
    except OSError as error:
        return {"ok": False, "error": type(error).__name__, "open_seconds": opened,
                "waited_seconds": round(time.monotonic() - sent, 2)}
    finally:
        channel.close()


def drive(directory, target, listen, hold, cue, services_only=False):
    """The app's side of the session. Returns a result dict; `ok` says whether every step passed."""
    result = {"ok": False}
    app = AppClient(directory)
    try:
        hello = app.call("hello")
        result["protocol"] = hello.get("protocol")
        say("helper-ready", protocol=hello.get("protocol"), stack=hello.get("stack", {}).get("stack"))
        phones = app.call("phones")["phones"]
        say("phones", count=len(phones), target_known=any(p["address"] == target for p in phones))
        if not any(p["address"] == target for p in phones):
            say("session-failed", reason="the target phone is not paired with this app")
            return result
        reply = app.call("select_phone", address=target)
        if not reply.get("ok"):
            say("session-failed", reason="select_phone", error=reply.get("error"))
            return result
        app.event("phone_connected", 40)
        say("phone-connected")

        for kind in ("pbap", "map"):
            outcome = probe_service(app, kind)
            say(f"service-{kind}", **outcome)
            result[kind] = outcome["ok"]

        if services_only:
            result["ok"] = bool(result.get("pbap") and result.get("map"))
            app.call("shutdown")
            return result
        say("waiting-for-call", seconds=listen)
        cue()
        ring = app.event("call_state", listen)
        while ring.get("state") != "incoming":
            ring = app.event("call_state", listen)
        say("incoming-call", number_known=bool(ring.get("number")), number_tail=re.sub(r"\D", "", ring.get("number", ""))[-3:])
        time.sleep(1)

        voice = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        voice.settimeout(2)
        voice.connect(os.path.join(directory, "voice.sock"))
        answered = app.call("answer")
        if not answered.get("ok"):
            say("session-failed", reason="answer", error=answered.get("error"))
            return result
        say("answered")
        reader, frames, seconds = FrameReader(), 0, set()
        start = time.monotonic()
        while time.monotonic() - start < hold:
            try:
                data = voice.recv(8192)
            except socket.timeout:
                continue
            if not data:
                break
            for kind, sequence, payload in reader.feed(data):
                if kind == DOWN:
                    frames += 1
                    seconds.add(int(time.monotonic() - start))
        voice.close()
        say("voice", frames=frames, seconds_with_frames=len(seconds), hold=hold)
        app.call("hang_up")
        end = time.monotonic() + 15
        while app.event("call_state", max(0.1, end - time.monotonic())).get("state") != "ended":
            pass  # earlier call events (such as "active") may still be queued; wait for the end
        status = app.call("status")
        say("status", voice_port=status.get("voice_port"), fault=status.get("fault"))
        result["voice_ok"] = frames > 0 and len(seconds) >= max(1, hold - 2)
        result["ok"] = bool(result.get("pbap") and result.get("map") and result["voice_ok"] and not status.get("fault"))
        app.call("shutdown")
        return result
    except Exception as error:
        say("session-failed", error_type=type(error).__name__, reason=str(error))
        return result
    finally:
        app.close()


async def run_session(stack, directory, target, listen=75, hold=10, cue=lambda: None, services_only=False):
    helper = Helper(directory, stack, grace=30, log=lambda *a: say("helper-log", message=" ".join(map(str, a))))
    say("helper-starting")
    await helper.start()
    say("helper-started")
    try:
        return await asyncio.get_running_loop().run_in_executor(None, drive, directory, target, listen, hold, cue, services_only)
    finally:
        await helper.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--listen", type=int, default=75)
    parser.add_argument("--hold", type=int, default=10)
    parser.add_argument("--name", default="Mac")
    parser.add_argument("--services-only", action="store_true", help="check contacts and messages only; no call")
    args = parser.parse_args()
    if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", args.target):
        say("session-failed", reason="target is not a Bluetooth address")
        return 2
    from gbp_btd.bumble_stack import BumbleStack
    directory = f"/tmp/gbp-btd-{os.getuid()}"

    def cue():
        try:
            subprocess.Popen(["/usr/bin/say", "Ready. Call the phone now."])
        except OSError:
            pass

    say("session-start", services_only=args.services_only)
    faulthandler.dump_traceback_later(60, exit=False)  # if the run stalls, show where every thread is stuck
    try:
        result = asyncio.run(asyncio.wait_for(
            run_session(BumbleStack(name=args.name), directory, args.target.upper(), args.listen, args.hold, cue, args.services_only),
            args.listen + args.hold + 90))
    except BaseException as error:
        faulthandler.cancel_dump_traceback_later()
        say("session-failed", error_type=type(error).__name__, reason=str(error) or "no detail")
        return 1
    faulthandler.cancel_dump_traceback_later()
    say("session-pass" if result.get("ok") else "session-failed-checks", **{k: v for k, v in result.items() if k != "ok"})
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)  # a stuck native thread must not keep the process, and Bluetooth, waiting

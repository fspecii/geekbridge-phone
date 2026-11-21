# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage 6a: does macOS's own Bluetooth work through us?

Started by run_stage6.sh as the logged-in user. It makes a pseudo-terminal, writes its path
to a file for the root step, then waits for `bluetoothd` to let go of the controller (the
root step restarts it on the serial path), opens the controller and passes packets both ways
for a fixed time. The question: do the mouse and headphones come back and work as usual,
with macOS as their host and every packet going through this program?

It changes nothing itself. It closes the controller before its time is up, so the root
step's timer can hand the controller back to macOS's normal path.
"""
import argparse
import ctypes
import json
import os
import select
import sys
import termios
import time
import tty
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gbp_proxy import FramingError, H4Parser, Policy

NATIVE = Path(__file__).resolve().parent / "build" / "libacipc_bridge.dylib"


def say(stage, **values):
    print(json.dumps({"stage": stage, **values}), flush=True)


class Chip:
    """The controller through the native bridge: whole packets in, whole packets out."""

    def __init__(self):
        lib = ctypes.CDLL(str(NATIVE))
        lib.acipc_open.restype = ctypes.c_int
        lib.acipc_close.restype = None
        lib.acipc_send.argtypes = [ctypes.c_uint8, ctypes.c_void_p, ctypes.c_size_t]
        lib.acipc_send.restype = ctypes.c_int
        lib.acipc_recv.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        lib.acipc_recv.restype = ctypes.c_int
        self.lib, self.buffer, self.kind = lib, ctypes.create_string_buffer(4096), ctypes.c_uint8()

    def open(self, wait_seconds):
        end = time.monotonic() + wait_seconds
        tries = 0
        while True:
            tries += 1
            status = self.lib.acipc_open()
            if status == 0:
                return tries
            if time.monotonic() >= end:
                raise RuntimeError(f"the controller did not open (last status {status}, {tries} tries)")
            # Fast: the controller is free only from the old daemon's exit until the new one
            # resets the chip, a few hundred milliseconds. The second live run polled every
            # 250 ms, missed that gap, and the chip then refused to open for good (status -12).
            time.sleep(0.005)

    def send(self, packet):
        payload = ctypes.create_string_buffer(packet[1:])
        status = self.lib.acipc_send(packet[0], payload, len(packet) - 1)
        if status != 0:
            raise RuntimeError(f"native send failed: {status}")

    def receive(self, wait_ms):
        count = self.lib.acipc_recv(ctypes.byref(self.kind), self.buffer, len(self.buffer), wait_ms)
        if count < 0:
            raise RuntimeError(f"native receive failed: {count}")
        return bytes([self.kind.value]) + self.buffer.raw[:count] if count else None

    def close(self):
        self.lib.acipc_close()


def speak(text):
    """A spoken cue, so the person testing knows the state without reading a log. Best effort."""
    try:
        import subprocess
        subprocess.Popen(["/usr/bin/say", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


class PhoneLink:
    """Gets the phone's link up after the daemon restart, and says so. Nothing else starts it."""

    FIRST_TRY, BETWEEN, TRIES = 8, 12, 6

    def __init__(self, address, started, runner=None, others=()):
        import subprocess
        self.address, self.next_try, self.tries = address, started + self.FIRST_TRY, 0
        self.others = list(others)   # headphones do not always come back by themselves after the restart
        self.helper = None
        self.announced = False
        app = str(Path(__file__).resolve().parent / "build" / "GBBtConnect.app")
        script = 'f=$(mktemp); /usr/bin/open -W -g --stdout "$f" --stderr "$f" "$0" --args "$1"; cat "$f"; rm -f "$f"'
        self.runner = runner or (lambda: subprocess.Popen(
            ["/bin/sh", "-c", script, app, self.address.replace(":", "-")],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True))

    APP = Path(__file__).resolve().parent.parent.parent / "dist" / "GeekBridge Phone (dev).app"

    def restart_app(self):
        """Starts the app afresh once the phone is linked. An app that was already open holds a
        session from before the daemon restart: it shows the phone as connected and sends
        nothing (seen live twice: no call banner, messages timing out, and not one hands-free
        line on the link in five minutes)."""
        import subprocess
        if not self.APP.exists():
            return False
        subprocess.run(["/usr/bin/pkill", "-x", "geekbridge-phone"], check=False)
        time.sleep(1)
        subprocess.Popen(["/usr/bin/open", str(self.APP)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True

    def connect_others(self):
        import subprocess
        app = str(Path(__file__).resolve().parent / "build" / "GBBtConnect.app")
        for address in self.others:
            subprocess.Popen(["/usr/bin/open", "-g", app, "--args", address.replace(":", "-")],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if self.others:
            say("reconnecting-other-devices", count=len(self.others))

    def tick(self, now, policy):
        if policy.phone_link is not None:
            if not self.announced:
                self.announced = True
                self.connect_others()
                restarted = self.restart_app()
                say("phone-linked", after_tries=self.tries, app_restarted=restarted)
                speak("Phone connected. Starting the app. You can call in ten seconds.")
            return
        self.announced = False
        if self.helper is not None:
            if self.helper.poll() is None:
                return
            say("phone-connect-attempt", attempt=self.tries, exit=self.helper.returncode,
                said=(self.helper.stdout.read() or "").strip()[:80])
            self.helper = None
        if now >= self.next_try and self.tries < self.TRIES:
            self.tries += 1
            self.next_try = now + self.BETWEEN
            self.helper = self.runner()


class Sound:
    """The call's sound: the caller to the Mac's current output, the Mac's current input to the caller.

    macOS is still the host for the headphones on this route, so "current output" is whatever
    the user has selected, Bluetooth headphones included. Opened when the phone's voice link
    opens and closed with it."""

    def __init__(self, opener=None, inputs=None):
        self.inputs = inputs
        self.opener = opener or self._open_default
        self.bridge = self.streams = None
        self.failure = None
        self.calls = []

    def _open_default(self):
        import sounddevice as sd

        import audio_io
        # A call plays on Bluetooth headphones when any are connected: the proxy flags the
        # headphone link high priority (0xFC57) so the chip keeps scheduling it through the
        # call. With no headphones connected, the output is left alone (the Mac's speakers).
        if self.inputs is not None and self.inputs.prefer_bluetooth_output():
            say("sound-output-moved-to-headphones")
        # The sound library reads the Mac's devices once; read them again so this call uses the
        # output and input chosen now, not the ones from the last call.
        sd._terminate()
        sd._initialize()
        speakers = "speaker" in sd.query_devices(None, "output")["name"].lower()
        # Through loudspeakers the caller would hear themselves, so only one side talks at a time.
        return audio_io.open_streams(sd, allow_speakers=True, half_duplex=speakers)

    def open(self):
        try:
            self.bridge, self.streams = self.opener()
            say("sound-open", **self.streams.info)
        except Exception as error:  # the call still runs, in silence, and the log says why
            self.failure = f"{type(error).__name__}: {str(error)[:160]}"
            say("sound-failed", detail=self.failure)

    def exchange(self, payload):
        """One packet of the caller's voice in, one packet of ours out, the same size."""
        if self.bridge is None:
            return bytes(len(payload))
        self.bridge.downlink(payload)
        out = self.bridge.uplink(len(payload))
        return (bytes(out) + bytes(len(payload)))[:len(payload)]

    def close(self):
        if self.bridge is not None:
            self.calls.append(self.bridge.stats())
            say("sound-closed", **self.calls[-1])
        if self.streams is not None:
            self.streams.close()
        self.bridge = self.streams = None
        if self.inputs is not None:
            self.inputs.restore_output()


def pump(host_fd, chip, policy, seconds, clock=time.monotonic, sound=None, inputs=None, phone_link=None):
    """Passes packets until the time is up. Returns when done; raises on a broken stream."""
    parser = H4Parser()
    end = clock() + seconds
    in_call = False
    next_input_check = 0
    while clock() < end:
        if phone_link is not None and clock() >= next_input_check:
            phone_link.tick(clock(), policy)
        if inputs is not None and clock() >= next_input_check:
            next_input_check = clock() + 1
            if inputs.keep_off_bluetooth():
                say("sound-input-moved-off-bluetooth", times=inputs.switches)
        if (policy.voice_handle is not None) != in_call:
            in_call = not in_call
            if phone_link is not None:
                speak("Call audio is on the Mac." if in_call else "Call ended.")
            if sound is not None:
                sound.open() if in_call else sound.close()
        ready, _, _ = select.select([host_fd], [], [], 0)
        if ready:
            for packet in parser.feed(os.read(host_fd, 4096)):
                action, out = policy.from_host(packet)
                if action == "forward":
                    chip.send(out)
                else:
                    os.write(host_fd, out)
        while policy.outbox:
            chip.send(policy.outbox.pop(0))
        packet = chip.receive(2)
        if packet is not None:
            action, out = policy.from_chip(packet)
            if action == "forward":
                os.write(host_fd, out)
            elif action == "send":
                chip.send(out)
            elif action == "tap":
                reply = policy.voice_reply(out)
                if sound is not None:
                    reply = reply[:4] + sound.exchange(out[4:])
                chip.send(reply)
    if sound is not None and in_call:
        sound.close()


def build_policy(phone_text):
    """Stage 6a: plain pass-through. Stage 6b: also keep the phone's voice link (silence both ways, counted)."""
    if not phone_text:
        return Policy()
    from bumble import hci, hfp

    def accept(address, link_type):
        # S4, not S1: S1 sends a voice packet every 6 slots, which leaves no room on the air for
        # the 5-slot packets that carry music to headphones (seen live: every music packet
        # flushed by the chip for the length of a call). S4 sends twice as much half as often.
        kind = hfp.DefaultCodecParameters.SCO_CVSD_D1 if link_type == 0 else hfp.DefaultCodecParameters.ESCO_CVSD_S4
        command = hci.HCI_Enhanced_Accept_Synchronous_Connection_Request_Command(
            bd_addr=hci.Address(phone_text, hci.Address.PUBLIC_DEVICE_ADDRESS), **hfp.ESCO_PARAMETERS[kind].asdict())
        return bytes(command)

    return Policy(phone=bytes(hci.Address(phone_text, hci.Address.PUBLIC_DEVICE_ADDRESS)), voice_accept=accept,
                  lead_other_links=True)


def main():
    import signal
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, required=True)
    parser.add_argument("--port-file", required=True)
    parser.add_argument("--go-file", required=True, help="appears when the root step has restarted bluetoothd")
    parser.add_argument("--phone", default=None, help="stage 6b: keep this phone's voice link for ourselves")
    parser.add_argument("--sound", action="store_true", help="stage 6c: carry the call's sound, not silence")
    parser.add_argument("--also-connect", action="append", default=[],
                        help="another paired device to reconnect once the pass-through is up (headphones)")
    args = parser.parse_args()
    if not 20 <= args.seconds <= 43200:
        sys.exit("seconds must be 20 to 43200")
    host_fd, device_fd = os.openpty()
    tty.setraw(device_fd)
    attributes = termios.tcgetattr(device_fd)
    attributes[2] |= termios.CLOCAL
    termios.tcsetattr(device_fd, termios.TCSANOW, attributes)
    port = os.ttyname(device_fd)
    os.chmod(port, 0o666)
    Path(args.port_file).write_text(port)
    say("port-ready", port=port)
    chip, policy, code = Chip(), build_policy(args.phone), 0
    inputs = None
    opened = False
    try:
        # Not before the root step has restarted bluetoothd: until then the running one owns the
        # controller, and reaching for it could disturb the Bluetooth the user is working with.
        for _ in range(12000):
            if Path(args.go_file).exists():
                break
            time.sleep(0.005)
        else:
            raise RuntimeError("the root step never ran; nothing was touched")
        tries = chip.open(wait_seconds=8)
        opened = True
        say("controller-open", tries=tries)
        with_sound = bool(args.sound and args.phone)
        if with_sound:
            from sound_input import Inputs
            inputs = Inputs()
        link = PhoneLink(args.phone, time.monotonic(), others=args.also_connect) if args.phone else None
        if link is not None:
            speak("Pass-through is up. Connecting the phone.")
        pump(host_fd, chip, policy, args.seconds, sound=Sound(inputs=inputs) if with_sound else None, inputs=inputs,
             phone_link=link)
        say("time-up")
        if link is not None:
            speak("Test finished. Bluetooth is going back to normal.")
    except KeyboardInterrupt:
        say("ended-early")
    except (RuntimeError, FramingError, OSError) as error:
        say("failed", error=type(error).__name__, detail=str(error)[:200])
        code = 11
    finally:
        if inputs is not None:
            inputs.restore()
        if opened:
            chip.close()
        say("passthrough-result", **policy.summary())
    sys.exit(code)


if __name__ == "__main__":
    main()

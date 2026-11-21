# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline tests for stage 6: serial framing, the packet policy and the pump. No Bluetooth."""
import os
import sys
import tty
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gbp_proxy
import stage6a_passthrough
from gbp_proxy import ACL, COMMAND, EVENT, SCO, FramingError, H4Parser, Policy

RESET = bytes([COMMAND, 0x03, 0x0C, 0])
DATA = bytes([ACL, 0x0B, 0x20, 3, 0, 1, 2, 3])
VOICE = bytes([SCO, 0x20, 0x00, 2, 9, 9])
DONE = bytes([EVENT, 0x0E, 4, 1, 0x03, 0x0C, 0])


class FramingTests(unittest.TestCase):
    def test_packets_come_out_whole_however_the_bytes_arrive(self):
        stream = RESET + DATA + VOICE + DONE
        self.assertEqual(H4Parser().feed(stream), [RESET, DATA, VOICE, DONE])
        parser, out = H4Parser(), []
        for byte in stream:
            out += parser.feed(bytes([byte]))
        self.assertEqual(out, [RESET, DATA, VOICE, DONE])

    def test_a_partial_packet_waits_for_the_rest(self):
        parser = H4Parser()
        self.assertEqual(parser.feed(DATA[:5]), [])
        self.assertEqual(parser.feed(DATA[5:]), [DATA])

    def test_a_long_data_packet_uses_its_two_byte_length(self):
        big = bytes([ACL, 0x0B, 0x20]) + (700).to_bytes(2, "little") + bytes(700)
        self.assertEqual(H4Parser().feed(big + RESET), [big, RESET])

    def test_a_stream_that_is_not_hci_stops_the_run(self):
        with self.assertRaises(FramingError):
            H4Parser().feed(b"\x7fnot hci")


class PolicyTests(unittest.TestCase):
    def test_ordinary_traffic_passes_both_ways(self):
        policy = Policy()
        for packet in (RESET, DATA, VOICE):
            self.assertEqual(policy.from_host(packet), ("forward", packet))
        self.assertEqual(policy.from_chip(DONE), ("forward", DONE))

    def test_vendor_commands_macos_normally_sends_still_pass(self):
        command = bytes([COMMAND, 0x6E, 0xFE, 5, 0, 0, 0, 0, 0])
        self.assertEqual(Policy().from_host(command), ("forward", command))

    def test_serial_boot_commands_are_answered_here_and_never_reach_the_chip(self):
        policy = Policy()
        for opcode in gbp_proxy.SERIAL_BOOT_COMMANDS:
            command = bytes([COMMAND]) + opcode.to_bytes(2, "little") + bytes([2, 0, 0])
            action, answer = policy.from_host(command)
            self.assertEqual(action, "answer")
            self.assertEqual(answer, bytes([EVENT, 0x0E, 4, 1]) + opcode.to_bytes(2, "little") + b"\x00")
        self.assertEqual(sum(policy.to_chip.values()), 0)

    def test_the_summary_counts_kinds_and_holds_no_content(self):
        policy = Policy()
        policy.from_host(RESET)
        policy.from_host(DATA)
        policy.from_host(bytes([COMMAND, 0x2E, 0xFC, 0]))
        policy.from_chip(DONE)
        summary = policy.summary()
        self.assertEqual(summary["to_chip"], {"commands": 1, "data": 1})
        self.assertEqual(summary["to_host"], {"events": 1})
        self.assertEqual((summary["vendor_commands"], summary["answered_here"]), (1, {"0xfc2e": 1}))
        self.assertEqual(summary["first_commands"], ["0x0c03", "0xfc2e"])


PHONE = bytes([1, 0, 0, 0xC2, 0xAC, 0x6C])
BUDS = bytes([3, 0, 0, 0xCC, 0xBB, 0xAA])
ACCEPT = bytes([COMMAND, 0x3E, 0x04, 6]) + PHONE


def voice_policy():
    return Policy(phone=PHONE, voice_accept=lambda address, link_type: ACCEPT)


def request(address, link_type):
    return bytes([EVENT, 0x04, 10]) + address + b"\x0c\x02\x5a" + bytes([link_type])


def status_for(opcode, status=0):
    return bytes([EVENT, 0x0F, 4, status, 1]) + opcode.to_bytes(2, "little")


def voice_open(address, handle, status=0):
    return bytes([EVENT, 0x2C, 17, status]) + handle.to_bytes(2, "little") + address + bytes([2]) + bytes(7)


def voice_packet(handle, size=60):
    return bytes([SCO]) + handle.to_bytes(2, "little") + bytes([size]) + bytes(range(size))


def closed(handle):
    return bytes([EVENT, 0x05, 4, 0]) + handle.to_bytes(2, "little") + b"\x13"


class PhoneVoiceTests(unittest.TestCase):
    def open_link(self, policy):
        self.assertEqual(policy.from_chip(request(PHONE, 2)), ("send", ACCEPT))
        self.assertEqual(policy.from_chip(status_for(0x043E))[0], "drop")
        self.assertEqual(policy.from_chip(voice_open(PHONE, 0x20))[0], "drop")

    def test_the_phones_voice_request_is_accepted_by_us_and_hidden_from_macos(self):
        policy = voice_policy()
        self.open_link(policy)
        self.assertEqual(policy.voice_handle, 0x20)
        self.assertEqual(sum(policy.to_host.values()), 0)

    def test_the_phones_voice_is_tapped_and_answered_with_silence_of_the_same_size(self):
        policy = voice_policy()
        self.open_link(policy)
        packet = voice_packet(0x20)
        self.assertEqual(policy.from_chip(packet), ("tap", packet))
        reply = policy.voice_reply(packet)
        self.assertEqual((reply[:4], reply[4:]), (packet[:4], bytes(60)))
        self.assertEqual((policy.voice["packets_in"], policy.voice["packets_out"]), (1, 1))

    def test_everything_else_still_reaches_macos(self):
        policy = voice_policy()
        self.open_link(policy)
        for packet in (request(PHONE, 1), request(BUDS, 2), voice_open(BUDS, 0x21), voice_packet(0x21),
                       status_for(0x0405), closed(0x0B), DONE, DATA):
            self.assertEqual(policy.from_chip(packet), ("forward", packet))

    def test_a_headset_voice_link_is_never_touched(self):
        policy = voice_policy()
        for packet in (request(BUDS, 2), status_for(0x043E), voice_open(BUDS, 0x21), voice_packet(0x21)):
            self.assertEqual(policy.from_chip(packet), ("forward", packet))
        self.assertIsNone(policy.voice_handle)

    def test_when_the_link_closes_the_tap_ends_and_a_new_call_can_open_one(self):
        policy = voice_policy()
        self.open_link(policy)
        self.assertEqual(policy.from_chip(closed(0x20))[0], "drop")
        packet = voice_packet(0x20)
        self.assertEqual(policy.from_chip(packet), ("forward", packet))
        self.open_link(policy)

    def test_a_second_request_while_one_is_open_is_left_to_macos_to_decline(self):
        policy = voice_policy()
        self.open_link(policy)
        again = request(PHONE, 2)
        self.assertEqual(policy.from_chip(again), ("forward", again))

    def test_a_refused_or_failed_link_leaves_nothing_behind(self):
        policy = voice_policy()
        policy.from_chip(request(PHONE, 2))
        self.assertEqual(policy.from_chip(status_for(0x043E, status=0x0C))[0], "drop")
        self.assertFalse(policy.voice_pending)
        policy.from_chip(request(PHONE, 2))
        policy.from_chip(status_for(0x043E))
        self.assertEqual(policy.from_chip(voice_open(PHONE, 0x20, status=0x1F))[0], "drop")
        self.assertIsNone(policy.voice_handle)
        self.assertEqual(policy.summary()["phone_voice"]["links_failed"], 1)

    def test_the_summary_says_when_things_happened_and_what_the_hosts_links_did(self):
        policy = voice_policy()
        ticks = iter(x / 2 for x in range(100))
        policy.clock = lambda: next(ticks)
        self.open_link(policy)
        policy.from_chip(voice_packet(0x20))
        policy.from_chip(voice_packet(0x21))
        policy.from_chip(voice_packet(0x21))
        policy.from_chip(bytes([EVENT, 0x10, 1, 0]))
        policy.from_chip(closed(0x20))
        summary = policy.summary()
        self.assertEqual([what for _, what in summary["timeline"]],
                         ["phone asked for voice (link type 2); accepting", "accept answered with status 0x00",
                          "phone voice link open: every 0 slots, retry window 0, 0 bytes in, 0 bytes out",
                          "first phone voice packet", "hardware error event",
                          "phone voice link closed, reason 0x13"])
        self.assertEqual(summary["host_voice_links"]["0x021"]["packets"], 2)
        self.assertEqual((summary["hardware_errors"], summary["phone_voice_seconds"]), (1, 1))

    def test_hands_free_signalling_is_read_from_the_phones_link_with_numbers_masked(self):
        policy = voice_policy()
        linked = bytes([EVENT, 0x03, 11, 0, 0x0C, 0x00]) + PHONE + bytes([1, 0])
        self.assertEqual(policy.from_chip(linked), ("forward", linked))

        def data(handle, text):
            body = b"\x09\x00\x40\x00" + b"\x0b\xef\x1b" + text
            return bytes([ACL]) + handle.to_bytes(2, "little") + len(body).to_bytes(2, "little") + body

        policy.from_host(data(0x0C, b"AT+BRSF=959\r"))
        policy.from_chip(data(0x0C, b"\r\n+BRSF: 3943\r\n\r\nOK\r\n"))
        policy.from_chip(data(0x0C, b'\r\n+CLIP: "+40712345678",145,,,"Ana"\r\n'))
        policy.from_host(data(0x0B, b"AT+SECRET\r"))
        lines = [(who, line) for _, who, line in policy.summary()["hands_free_signalling"]]
        self.assertEqual(lines, [("mac", "AT+BRSF=959"), ("phone", "+BRSF: 3943"), ("phone", "OK"),
                                 ("phone", '+CLIP: "#",145,,,"#"')])
        other = bytes([EVENT, 0x03, 11, 0, 0x0D, 0x00]) + BUDS + bytes([1, 0])
        policy.from_chip(other)
        notes = [what for _, what in policy.summary()["timeline"]]
        self.assertEqual(notes, ["classic link to phone: type 1, status 0x00",
                                 "classic link to another device: type 1, status 0x00"])

    def test_without_a_phone_nothing_is_kept(self):
        policy = Policy()
        for packet in (request(PHONE, 2), voice_open(PHONE, 0x20), voice_packet(0x20)):
            self.assertEqual(policy.from_chip(packet), ("forward", packet))


class FakeChip:
    def __init__(self, incoming):
        self.incoming, self.sent = list(incoming), []

    def send(self, packet):
        self.sent.append(packet)

    def receive(self, wait_ms):
        return self.incoming.pop(0) if self.incoming else None


class PumpTests(unittest.TestCase):
    def test_packets_cross_in_both_directions_and_boot_commands_are_answered(self):
        ours, theirs = os.openpty()
        chip, policy = FakeChip([DONE, DATA]), Policy()
        boot = bytes([COMMAND, 0x2E, 0xFC, 0])
        tty.setraw(theirs)  # before any byte is written: setting it later waits for the port to drain
        os.write(theirs, RESET + boot + DATA)
        ticks = iter(range(100))
        stage6a_passthrough.pump(ours, chip, policy, seconds=8, clock=lambda: next(ticks))
        self.assertEqual(chip.sent, [RESET, DATA])
        seen = H4Parser().feed(os.read(theirs, 4096))
        self.assertCountEqual(seen, [DONE, DATA, gbp_proxy.command_complete(0xFC2E)])
        os.close(ours)
        os.close(theirs)


class PumpVoiceTests(unittest.TestCase):
    def test_the_pump_accepts_the_phones_voice_and_sends_silence_back(self):
        ours, theirs = os.openpty()
        tty.setraw(theirs)
        packet = voice_packet(0x20)
        chip = FakeChip([request(PHONE, 2), status_for(0x043E), voice_open(PHONE, 0x20), packet, DONE])
        policy = voice_policy()
        ticks = iter(range(100))
        stage6a_passthrough.pump(ours, chip, policy, seconds=8, clock=lambda: next(ticks))
        self.assertEqual(chip.sent, [ACCEPT, packet[:4] + bytes(60)])
        self.assertEqual(H4Parser().feed(os.read(theirs, 4096)), [DONE])
        os.close(ours)
        os.close(theirs)


class SoundInputTests(unittest.TestCase):
    def test_a_bluetooth_input_is_moved_to_another_microphone_and_nothing_else_is_touched(self):
        import sound_input
        webcam, headset, speakers = (40, b"usb ", 1), (50, b"blue", 1), (60, b"bltn", 0)
        self.assertEqual(sound_input.choose(50, [speakers, headset, webcam]), 40)
        self.assertIsNone(sound_input.choose(40, [speakers, headset, webcam]))
        self.assertIsNone(sound_input.choose(50, [speakers, headset]))
        self.assertIsNone(sound_input.choose(99, [webcam]))
        self.assertEqual(sound_input.choose(51, [(51, b"blea", 1), webcam]), 40)


class PhoneLinkTests(unittest.TestCase):
    def setUp(self):
        stage6a_passthrough.say = lambda *a, **k: None
        stage6a_passthrough.speak = lambda *a, **k: None

    def test_the_app_is_started_afresh_once_when_the_phone_links(self):
        restarts = []
        link = stage6a_passthrough.PhoneLink("AA:BB:CC:00:00:01", 0, runner=lambda: None)
        link.restart_app = lambda: restarts.append(1) or True
        link.connect_others = lambda: None
        policy = voice_policy()
        policy.phone_link = 0x0C
        for now in (1, 2, 3):
            link.tick(now, policy)
        self.assertEqual(restarts, [1])

    def test_it_keeps_asking_until_the_phone_links_and_then_stops(self):
        class Helper:
            returncode = 4
            stdout = __import__("io").StringIO("open-connection-result 0x4")

            def poll(self):
                return self.returncode

        started = []
        link = stage6a_passthrough.PhoneLink("AA:BB:CC:00:00:01", 0, runner=lambda: started.append(1) or Helper())
        link.restart_app = lambda: False
        policy = voice_policy()
        for now in (1, 9, 15, 22, 30):
            link.tick(now, policy)
        self.assertEqual(len(started), 2)
        policy.phone_link = 0x0C
        for now in (40, 60, 90):
            link.tick(now, policy)
        self.assertEqual(len(started), 2)
        self.assertTrue(link.announced)

    def test_it_gives_up_after_a_fixed_number_of_tries(self):
        class Helper:
            returncode = 4
            stdout = __import__("io").StringIO("")

            def poll(self):
                return self.returncode

        started = []
        link = stage6a_passthrough.PhoneLink("AA:BB:CC:00:00:01", 0, runner=lambda: started.append(1) or Helper())
        for now in range(0, 600, 5):
            link.tick(now, voice_policy())
        self.assertEqual(len(started), stage6a_passthrough.PhoneLink.TRIES)


class LinkRoleTests(unittest.TestCase):
    def accept(self, address, role):
        return bytes([COMMAND, 0x09, 0x04, 7]) + address + bytes([role])

    def test_roles_are_recorded_and_left_alone_by_default(self):
        policy = voice_policy()
        command = self.accept(BUDS, 1)
        self.assertEqual(policy.from_host(command), ("forward", command))
        policy.from_chip(bytes([EVENT, 0x12, 8, 0]) + BUDS + bytes([0]))
        notes = [(who, what) for _, who, what in policy.summary()["link_roles"]]
        self.assertEqual(notes, [("another device", "accepted, host asked to follow"),
                                 ("another device", "role now lead, status 0x00")])

    def test_the_experiment_makes_the_mac_lead_other_links_but_never_touches_the_phones(self):
        policy = Policy(phone=PHONE, voice_accept=lambda a, t: ACCEPT, lead_other_links=True)
        self.assertEqual(policy.from_host(self.accept(BUDS, 1)), ("forward", self.accept(BUDS, 0)))
        self.assertEqual(policy.from_host(self.accept(PHONE, 1)), ("forward", self.accept(PHONE, 1)))
        self.assertEqual(policy.from_host(self.accept(BUDS, 0)), ("forward", self.accept(BUDS, 0)))

    def complete(self, opcode, status=0):
        return bytes([EVENT, 0x0E, 4, 1]) + opcode.to_bytes(2, "little") + bytes([status])

    def test_the_headphone_link_is_set_high_priority_for_the_call_and_the_answers_are_hidden(self):
        policy = Policy(phone=PHONE, voice_accept=lambda a, t: ACCEPT, lead_other_links=True)
        policy.from_chip(bytes([EVENT, 0x03, 11, 0, 0x0D, 0x00]) + BUDS + bytes([1, 0]))
        policy.from_chip(request(PHONE, 2))
        policy.from_chip(status_for(0x043E))
        policy.from_chip(voice_open(PHONE, 0x20))
        self.assertEqual(policy.outbox, [bytes([COMMAND, 0x57, 0xFC, 3, 0x0D, 0x00, 0x01])])
        policy.outbox.clear()
        self.assertEqual(policy.from_chip(self.complete(0xFC57))[0], "drop")
        policy.from_chip(closed(0x20))
        self.assertEqual(policy.outbox, [bytes([COMMAND, 0x57, 0xFC, 3, 0x0D, 0x00, 0x00])])
        self.assertEqual(policy.from_chip(self.complete(0xFC57))[0], "drop")
        later = self.complete(0xFC57)
        self.assertEqual(policy.from_chip(later), ("forward", later))

    def test_without_the_experiment_no_priority_commands_are_sent(self):
        policy = voice_policy()
        policy.from_chip(bytes([EVENT, 0x03, 11, 0, 0x0D, 0x00]) + BUDS + bytes([1, 0]))
        policy.from_chip(request(PHONE, 2))
        policy.from_chip(status_for(0x043E))
        policy.from_chip(voice_open(PHONE, 0x20))
        self.assertEqual(policy.outbox, [])

    def test_packets_the_chip_gave_up_on_are_counted(self):
        policy = voice_policy()
        policy.from_chip(bytes([EVENT, 0x11, 2, 0x0D, 0x00]))
        self.assertEqual(policy.summary()["chip_flushes"], 1)


class SoundOutputTests(unittest.TestCase):
    def test_a_call_falls_back_to_the_built_in_speakers_when_the_output_is_bluetooth(self):
        import sound_input
        speakers, music, hdmi = (75, b"bltn", 0, 2), (112, b"blue", 0, 2), (95, b"hdmi", 0, 2)
        self.assertEqual(sound_input.choose_wired_output(112, [hdmi, music, speakers]), 75)
        self.assertIsNone(sound_input.choose_wired_output(75, [hdmi, music, speakers]))
        self.assertIsNone(sound_input.choose_wired_output(112, [hdmi, music]))

    def test_connected_headphones_become_the_output_and_their_music_side_is_preferred(self):
        import sound_input
        speakers, webcam = (75, b"bltn", 0, 2), (91, b"usb ", 2, 0)
        music, headset = (112, b"blue", 0, 2), (118, b"blue", 1, 1)
        self.assertEqual(sound_input.choose_output(75, [speakers, webcam, headset, music]), 112)
        self.assertEqual(sound_input.choose_output(75, [speakers, headset]), 118)
        self.assertIsNone(sound_input.choose_output(112, [speakers, music]))
        self.assertIsNone(sound_input.choose_output(75, [speakers, webcam]))


class FakeBridge:
    def __init__(self):
        self.heard = []

    def downlink(self, data):
        self.heard.append(bytes(data))

    def uplink(self, length):
        return bytes([7]) * length

    def stats(self):
        return {"packets": len(self.heard)}


class FakeStreams:
    info = {"input": "Mic", "output": "Headphones"}
    closed = False

    def close(self):
        self.closed = True


class SoundTests(unittest.TestCase):
    def setUp(self):
        stage6a_passthrough.say = lambda *a, **k: None

    def run_call(self, sound):
        ours, theirs = os.openpty()
        tty.setraw(theirs)
        packet = voice_packet(0x20)
        chip = FakeChip([request(PHONE, 2), status_for(0x043E), voice_open(PHONE, 0x20), packet, packet,
                         closed(0x20), DONE])
        ticks = iter(range(100))
        stage6a_passthrough.pump(ours, chip, voice_policy(), seconds=12, clock=lambda: next(ticks), sound=sound)
        os.close(ours)
        os.close(theirs)
        return chip, packet

    def test_the_callers_voice_reaches_the_output_and_ours_goes_back(self):
        bridge, streams = FakeBridge(), FakeStreams()
        sound = stage6a_passthrough.Sound(opener=lambda: (bridge, streams))
        chip, packet = self.run_call(sound)
        self.assertEqual(bridge.heard, [packet[4:], packet[4:]])
        self.assertEqual(chip.sent[1:], [packet[:4] + bytes([7]) * 60] * 2)
        self.assertTrue(streams.closed)
        self.assertEqual(sound.calls, [{"packets": 2}])

    def test_a_sound_device_that_will_not_open_leaves_the_call_running_in_silence(self):
        def broken():
            raise RuntimeError("no microphone permission")

        sound = stage6a_passthrough.Sound(opener=broken)
        chip, packet = self.run_call(sound)
        self.assertEqual(chip.sent[1:], [packet[:4] + bytes(60)] * 2)
        self.assertIn("no microphone permission", sound.failure)


if __name__ == "__main__":
    unittest.main()

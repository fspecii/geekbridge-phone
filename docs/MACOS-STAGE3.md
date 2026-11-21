# Stage 3 plan: calls and voice through the Mac

Part of [MACOS-HEADUNIT-ROUTE.md](MACOS-HEADUNIT-ROUTE.md). Stage 2 paired the Fold5 with our own Bluetooth stack on the Mac's built-in chip and opened its hands-free channel. Stage 3 makes calls work: ring, answer, hang up, dial, and the caller's voice through the Mac's speakers and microphone.

Nothing in this document has been run. It rests on the reference project (`lzsnolimit/ai-caller`, MIT), which reports full two-way voice for one call at a time on the same chip family, and on Bumble 0.0.236 as installed.

## Status, 2026-11-09 16:50: 3a built and tested offline, not run

Written in `tools/macos-hci-probe/`: `calls.py`, `sco.py`, `stage3a_call.py`, the extended safety filter, and tests. 52 offline tests pass:

- The filter allows accepting the voice link only for the target address, setting one up only on the target's link, voice packets only on the single voice link and only up to 254 bytes with a consistent length, ACL data only on the known link, and disconnects only for the two known links. A voice link for another address, or a second voice link, stops the run.
- Call state: ring, answer, hang up, missed, outgoing, with numbers kept only for the length of the call. Logs hold only the last three digits.
- Voice handling: one packet back per packet received, odd or oversize packets stop the echo, damaged packets are answered with silence, silent seconds are reported.
- The whole flow runs against stand-ins for the phone and controller: ring, caller number, answer, the exact `Enhanced Accept Synchronous Connection Request` command (checked for address and the CVSD parameters), voice link open, packets echoed, hang up, plus refused accept, second link and bad packet failures.

The call script never pairs: a missing key is a failure. A Ping on the Mac marks the moment to ring the phone.

Not yet run on Bluetooth. Next: the 3a run (with your go).

## Stage 3a result, 2026-11-09 16:28: passed on the first run

One run through `launcher`, 26 seconds from start to Bluetooth restored. A second phone rang the Fold5; nothing was answered on the phone.

| Step | Result |
| --- | --- |
| Reconnect | Our stack reconnected to the Fold5 with the stage 2 key, link encrypted, no new pairing |
| Ring | Reported at once; the caller's number arrived (ending `701`), read with the call list |
| Voice link | The phone requested an eSCO link, we accepted with the standard CVSD parameters, the link opened (handle 6) |
| Answer | Answered from the Mac; the phone's call indicator went active |
| Voice traffic | 2952 packets received and 2952 sent over 12 seconds (about 246 a second, roughly 65 bytes each, matching 8 kHz 16-bit audio); 0 bad, 0 empty, 0 odd length; traffic in every second, 0 gaps |
| Hang up | Hung up from the Mac; the voice link and the call ended cleanly |
| Restore | Bluetooth switched on again and checked; no firmware crash in the `bluetoothd` log |

So the Mac, using only its built-in Bluetooth chip and our own stack, answers a real call, receives the caller's voice packets and sends packets back at the controller's pace without upsetting the firmware. That is the hard part of call voice on macOS.

Bumble prints a "only for testing" warning for its voice-link functions. It is harmless and expected for this version.

Not yet shown: sound. The packets carried silence. Stage 3b connects them to the speakers and microphone.

## Status, 2026-11-09 17:00: 3b built and tested offline, not run

Written: `audio_io.py`, `stage3b_voice.py`, the audio hook in `sco.py` and `stage3a_call.py`, microphone permission text in the launcher bundle, pinned `sounddevice==0.5.6` and `audioop-lts==0.2.2`. 83 offline tests pass. What they cover:

- Buffers: bounded at 200 ms, oldest sound dropped on overflow, silence added when a buffer runs dry, both counted; sample alignment kept; safe across threads.
- Resampling: 48 kHz to 8 kHz averages groups of six samples first (a 7 kHz tone comes out well below a 440 Hz tone), 8 kHz to 48 kHz and 44.1 kHz use the general converter, results are the same whatever the chunk sizes.
- Bridge: nothing plays until 60 ms are buffered, speaker output is doubled to both channels, a microphone read is always the exact packet length, an empty microphone gives silence and is counted, loudness is measured both ways.
- Devices: streams open at each device's own rate with 16-bit samples, callbacks reach the bridge, status problems are recorded, a failed start closes both streams.
- Call flow: phone audio reaches the speaker side, microphone audio goes back one packet per packet, a damaged packet reaches the speakers and the phone as silence.

The run opens the sound devices before the call, so macOS asks for microphone access while you wait. Pass rule: the 3a rules, plus no sound stream errors and at most 5% of speaker callbacks short of audio. Whether it sounds right is for you to judge.

Not yet run on Bluetooth or sound.

## Stage 3b result, 2026-11-09 16:36: passed its mechanical checks

Run on the Mac's built-in speakers and microphone (your choice), with the half-duplex gate on, so the microphone is muted for 0.25 s after each stretch of loud caller audio. A second phone rang the Fold5; the Mac answered, kept the call about 32 seconds, and hung up.

| Measure | Value |
| --- | --- |
| Voice packets | 8274 received, 8274 sent, traffic in all 32 seconds, 0 gaps; 0 bad, 0 empty, 0 odd |
| Sound streams | microphone and speakers opened at 48 kHz (speakers in stereo), 0 stream errors |
| Caller to speakers | peak loudness 6622 of 32767: the caller's voice arrived loud and clear in level; 8555 packets played |
| Microphone to caller | peak loudness 3441: the microphone was live and microphone permission was granted |
| Speaker underruns | 38 of 33264 speaker callbacks (0.11%), far under the 5% limit |
| Half-duplex gate | muted 2658 of 8555 outgoing packets (31%) |
| Controller | no firmware crash in the `bluetoothd` log; Bluetooth restored and checked |

One counter is misleading: `mic_dropped_bytes` (193860) counts microphone sound dropped while the Mac was waiting for the call, before any voice link existed. It should reset when the link opens.

Heard on the call (reported by the user, 2026-11-09): the caller was heard through the Mac's speakers, and the caller heard the user through the Mac's microphone. Both directions worked, with the half-duplex gate on. So call voice now works on the Mac's built-in Bluetooth chip with no extra hardware, no driver and no change to SIP.

## Stage 3b first attempt, 2026-11-09 16:33: no call arrived, stopped safely

The run connected and encrypted the link and opened the sound devices (microphone and speakers at 48 kHz), then waited 60 seconds for a ring. None was reported to the Mac, so it timed out, disconnected cleanly, and Bluetooth came back on (checked). No call was made, no voice was carried, nothing was heard.

Two problems on our side showed up in the log and are fixed, with 84 offline tests passing:

- The active output was the built-in speakers, not headphones. `open_streams` now refuses the built-in speakers unless `--allow-speakers` is given.
- The cue was a short Ping. It is now a spoken "Ready. Call the phone now." (the Ping stays as a fallback), and the default wait is 75 seconds.
- The failure line said "stage3a" for 3b. It now uses the right label.

Open question for the next attempt: whether the Fold5 actually rang during that window. A missed cue and a ring that never reached the Mac are different problems.

## How voice works here

1. The phone is the audio gateway. When a call is answered on the Mac side, the phone asks for a voice link (an eSCO link) and Bumble raises `EVENT_SCO_REQUEST`.
2. Our side accepts it with one standard command, `Enhanced Accept Synchronous Connection Request`, with the CVSD "S1" parameters from `hfp.ESCO_PARAMETERS`. Bumble then raises `EVENT_SCO_CONNECTION` with a link object.
3. Voice travels as ordinary HCI SCO packets over Apple's `sco` channel: 8 kHz, 16-bit mono PCM (CVSD codec). Incoming packets arrive on the link's sink. We send audio with `host.send_sco_sdu(handle, bytes)`.
4. This controller has no flow control for SCO, so the reference project sends exactly one packet back for each packet received. That ties our sending to the controller's own clock.
5. The reference project found no vendor commands necessary for one call. Our filter keeps refusing them.

One call at a time. Two simultaneous voice links crash the controller firmware, so the stack never sets up a second one.

## Steps

### 3a. Call control and voice plumbing, no sound device

Goal: prove the link and the packet flow with silence, before any audio hardware is involved.

1. Our stack connects to the already paired Fold5 and reopens the hands-free channel (the key from stage 2 is stored, so no pairing).
2. You call the Fold5 from another phone. Our stack reports the ring and the caller's number (`EVENT_RING`, `EVENT_CLI_NOTIFICATION`, `query_current_calls`).
3. The program answers the call from the Mac. It accepts the voice link, counts SCO packets in both directions for 10 seconds while sending silence back, then hangs up.

Pass: ring and number reported, answer confirmed by the phone, received SCO packets increase every second, our packets match them, no error from the native bridge, no firmware crash, Bluetooth restored.

### 3b. Voice through the Mac

1. Same as 3a, with sound: the downlink (phone to Mac) goes to the Mac's speakers and the Mac's microphone feeds the uplink.
2. Audio parts: PortAudio through the `sounddevice` package, 16-bit mono, opened at the device's native rate and resampled to and from 8 kHz with `audioop` (`audioop-lts` on Python 3.13). Bounded buffers of about 200 ms each way; any overflow or underflow is counted and reported, never hidden.
3. The Mac needs microphone permission for the launcher app. The bundle gets `NSMicrophoneUsageDescription`, and macOS asks you once.
4. Use wired headphones for the first test. Bluetooth is off during the run, so AirPods will not work, and the laptop speakers would feed the call back into the microphone.

Pass: for at least 30 seconds you hear the caller and the caller hears you, the underrun and overrun counts stay small, and the run ends cleanly.

### 3c. Dialling out

The Mac dials your other phone (`ATD<number>;` through `execute_command`), you answer it there, and the same voice test runs. Hang up from the Mac. The call is placed from the Fold5 and uses its plan.

## What changes in the code

| Change | Why |
| --- | --- |
| Filter: allow `Enhanced Accept`, `Accept` and `Reject Synchronous Connection Request` only for the target's address; allow `Setup Synchronous Connection` only for the target's link | The voice link must be accepted, and nothing else may be |
| Filter: track the voice link's handle from the `Synchronous Connection Complete` event; outbound SCO data is allowed only for that handle and only up to the controller's 254-byte buffer | Keeps voice packets on one link |
| `calls.py`: the hands-free protocol loop (`run()`), call state from `EVENT_AG_INDICATOR`, caller number, answer, reject, hang up, dial | Call control |
| `sco.py`: accept, link events, one packet back per packet received, counters | Voice plumbing |
| `audio_io.py`: sound input and output, resampling, bounded buffers, counters | Sound |
| Launcher bundle: microphone usage text, longer time limit for calls (up to 150 seconds) | macOS permission and call length |
| Offline tests: new filter rules, buffer overflow and underflow behaviour, resampler round trip, call event parsing against recorded stand-ins | Everything that can be tested without a phone |

New packages: `sounddevice` (its macOS wheel includes PortAudio) and, on Python 3.13, `audioop-lts`. Nothing else.

## What you do

Before each run:

1. Phone: Bluetooth on, screen on, and "Phone calls" switched on for the Mac's entry in the phone's Bluetooth settings, as before. After the stage 2 pairing it may have reset, so check it.
2. Another phone ready to call the Fold5 (3a, 3b) or to receive a call (3c).
3. Wired headphones for 3b and 3c.

During a run: ring the Fold5 when I tell you the run has started. In 3b, allow microphone access the first time.

## Abort rules

- Stop and restore if the power state is not on at the start.
- Stop on any native bridge error other than a timeout, on a firmware crash line in the `bluetoothd` log, on a second voice link request, or when the time limit passes.
- One attempt per run. No retries, no loops.
- If a call is still active when the run ends, hang it up on the phone.

## Risks

- A firmware crash can leave `bluetoothd` restarting for about 13 seconds. The launcher waits up to 40 seconds for Bluetooth to come back and reports the state.
- Real calls: 3a and 3b receive a call (no cost to you if the other phone is yours). 3c places a call from the Fold5's plan.
- Audio may glitch before the buffers are tuned. That is not a failure of the route; the counters tell us what to adjust.
- macOS 27.0 is a pre-release and the reference project tested 26.4 on another Mac. Stage 1 and 2 passed here, which is the best evidence so far.
- While system Bluetooth is off, keyboards, mice and AirPods on the Mac disconnect.

## After stage 3

If 3b passes, the Mac carries call voice through its built-in Bluetooth with our own stack. Stage 4 then puts this behind the app's `Platform` interface as a helper process, adds MAP and PBAP (messages and contacts) over our RFCOMM so one stack serves everything, and adds a "Mac voice mode" switch that says plainly that system Bluetooth is off while it runs.

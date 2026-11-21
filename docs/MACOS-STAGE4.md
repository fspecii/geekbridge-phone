# Stage 4 plan: Mac voice mode inside the app

Part of [MACOS-HEADUNIT-ROUTE.md](MACOS-HEADUNIT-ROUTE.md). Stages 1 to 3 showed, on this Mac, that our own Bluetooth stack running over Apple's internal controller channels can pair an Android phone, answer a real call, and carry its voice through the Mac's speakers and microphone. The pieces live in `tools/macos-hci-probe/` as test programs. Stage 4 turns them into a feature of the app.

Nothing in this document has been built. It names real files and interfaces from the repository, and marks the open questions.

## Status, 2026-11-09 18:30: 4a built and tested offline, not yet run live

The helper is in `helper/macos-btd/` (see its README). 148 offline tests pass:

- Protocol: every request checked before it acts; numbers, tones, addresses, durations and service names validated; unknown fields refused; oversize and malformed messages answered with errors, never a crash.
- Server: one app at a time, requests answered as they finish, a private folder and socket (mode 700 and 600), descriptors passed with `SCM_RIGHTS` and not kept by the helper, a stale socket replaced, the app coming back cancelling the stop timer.
- Managers: connect and reconnect with growing pauses, one error report per distinct failure, a controller fault stops retrying and is reported, call tracking with numbers, answer, decline, hang up, dial and tones, voice both ways with silence for damaged packets, scan and pairing with the code shown to the app, MAP and PBAP channels as descriptors (one per service, replaced on reopen).
- Real stack against a simulated phone on Bumble's virtual controllers, through the real safety filter: connecting, the hands-free link, ring and caller number, answer, hang up, dial, a messages and a contacts channel open at once on one connection, a lost link, an unreachable phone. The filter blocked nothing legitimate.
- The live session driver, run offline against the helper with a fake phone.

Two bugs were found by these tests before any live run: an event field called `name` collided with the event name (fixed by making the event name positional-only), and the key store looked keys up in a different form than Bumble and the stage 2 file use (fixed; the real stage 2 key is now found by the helper's own code).

Not yet verified live: scanning (the virtual controller cannot simulate it), and everything on the real chip through the helper's sockets. The live session test is `helper/macos-btd/session_test.py`.

## Stage 4a live session, 2026-11-09 16:54: most of it passed

Run once through the launcher: helper in-process with the real stack on the built-in controller, a blocking client playing the app. 40 seconds from start to Bluetooth restored.

| Step | Result |
| --- | --- |
| Helper start | Passed: hello answered (protocol 1, Bumble stack) |
| Phone list | Passed: the Fold5 listed from the stored stage 2 key (this also confirms the key lookup fix) |
| Connect | Passed: the helper connected to the Fold5, link encrypted, hands-free link open |
| Contacts (PBAP) and messages (MAP) | **Not passed:** both channels timed out waiting for the phone's answer to an OBEX Connect |
| Ring | Passed: incoming call with the caller's number |
| Answer | Passed: answered through the socket interface |
| Voice | Passed: 2673 voice frames reached the app side in 10 seconds, traffic in 11 of 11 seconds; no fault |
| Hang up | Passed: call ended, voice link closed |
| Shutdown | Passed: helper stopped, no firmware crash in the log, Bluetooth switched on again and checked, no socket files left |

The contacts and messages timeout is most likely the phone waiting for the user to allow access: Android asks the first time a newly paired device opens contacts or messages, and the pairing made with our own stack is new to the phone. This is unconfirmed. If it is a permission prompt, nothing needs fixing in the helper; the app already tells the user to allow access on the phone, as on the other platforms. If no prompt appeared, the next suspect is the data path, which the offline tests cover only on the virtual link.

## Stage 4a, contacts and messages, 2026-11-09 17:03: passed

A second, shorter live run (`--services-only`, no call), 12 seconds from start to Bluetooth restored:

| Step | Result |
| --- | --- |
| Helper start, phone list, connect | Passed |
| Contacts (PBAP) | The phone listed one server, channel 19, "OBEX Phonebook Access Server". The channel opened in 0.03 s with 7 credits each way and a 990-byte frame size. Our 26-byte OBEX Connect was sent, and the phone answered with Success (`0xA0`, 31 bytes) after 5.7 s |
| Messages (MAP) | The phone listed one instance, channel 7, "SMS/MMS", message types 14 (SMS GSM, SMS CDMA, MMS). Opened in 0.06 s, same credits. OBEX Connect answered with Success after 1.2 s |
| Stop | Clean: helper folder empty, no firmware crash, Bluetooth on again |

Together with the call run, every part of the helper's interface has now worked on the real chip and the real phone: connect, ring and caller number, answer, voice frames, hang up, contacts and messages channels. Not yet shown live: scanning and pairing through the helper, dialling out, and reading actual contacts and messages through the existing Rust OBEX code.

Why the first attempt failed is not proven. Both services had timed out at 10 seconds. This run's answers took 5.7 s and 1.2 s, so the likely cause is that the phone is slow to answer the first OBEX Connect after a fresh pairing and the earlier wait was too short. No prompt appeared. The instance choice was not the cause: the phone lists only one SMS instance and no email instance. Consequence for the app: give the first OBEX Connect in voice mode at least 30 seconds, with a "waiting for the phone" message, instead of the 12 s used elsewhere.

The first run also stalled once before printing anything (a later identical run did not), cause unknown; the native open now runs in a thread with a 20 s limit and the program dumps its stacks after 60 s if it stalls. Watch for it.

## Goal

A "Mac voice mode" in GeekBridge Phone. With it on, the app on a Mac does what the Linux and Windows versions do: ring banners with Answer and Decline, calls with voice through the Mac's speakers and microphone, dialling, contacts, texts. With it off, the app behaves as it does today (through macOS's own Bluetooth: contacts and texts, call control, no voice).

The cost is stated to the user, not hidden: while voice mode is on, the Mac's own Bluetooth is off, so keyboards, mice and AirPods connected to the Mac disconnect.

## Design rules

1. **One interface.** Voice mode is a second implementation of `phone_core::platform::Platform`. Nothing above it (service, views, banners) learns that there are two.
2. **The user switches it on and off.** No automatic power changes without a visible switch and a visible state.
3. **Bluetooth is always given back.** Crash, kill, power loss, macOS update: the next start of the app finds and fixes it, and a separate guard restores it if the app dies while voice mode is on.
4. **One phone, one call.** Two simultaneous voice links crash the controller firmware, so the stack refuses a second one (already enforced by the safety filter).
5. **The stack can only act on the selected phone.** The safety filter from stage 2 and 3 stays in the product.
6. **Every private-API risk has a visible failure path:** a self-test at switch-on, a clear message, and a way back to normal mode.

## Architecture

```
GeekBridge Phone.app (Rust, the existing app)
  phone-core / phone-app                       unchanged above the Platform interface
  phone-macos
    IOBluetooth backend   (today)              voice mode OFF: system Bluetooth, no voice
    VoiceModePlatform     (new)                voice mode ON: talks to the helper
    power.rs / power.m    (new)                switch system Bluetooth off and on; the guard
    voice_audio           (new, later)         speakers + microphone with echo cancellation
        |  control: Unix socket, one JSON line per message (private folder, mode 600)
        |  services: file descriptors passed over that socket (MAP, PBAP)
        |  voice: a second socket with 8 kHz 16-bit frames
        v
  geekbridge-btd (helper, Python for now)      child of the app, one per voice mode session
    gbp_acipc.py   Bumble transport + safety filter           (exists, stage 2)
    calls.py, sco.py                                            (exist, stage 3)
    new: server, phone manager, services, scan and pairing
        |  Apple's hci / acl / sco channels (libacipc_bridge.dylib, MIT, unmodified)
        v
  the Mac's built-in Bluetooth controller
```

### Why a helper process and not code in the app

- The Bluetooth stack is Bumble (Python, Apache-2.0). There is no mature classic Bluetooth host stack with hands-free in Rust. Porting one is possible later; it is not the way to the first working version.
- A separate process isolates crashes: the stack dying does not take the app down, and the app can restart it.
- It matches how the stage 2 and 3 programs already run.

### Switch-on sequence (the app does this)

1. Check the preconditions: macOS build known or accepted, free of a leftover marker, a paired phone selected, the Bluetooth power state on.
2. Write a marker file ("voice mode active, Bluetooth was on") and start the guard.
3. Switch system Bluetooth off (the app is itself an app bundle with a Bluetooth usage description, so it may call the power function; the test programs needed a launcher for that reason).
4. Start the helper. It runs a self-test: open the channels, read the controller version and address, compare with the address macOS reports. Any failure is a clean stop with a plain message.
5. The helper connects to the selected phone with the stored key, opens the hands-free channel, and reports state.
6. The app swaps its `Platform` to `VoiceModePlatform`.

### Switch-off, and recovery

- Off, normal: stop the helper first (channels closed), then switch Bluetooth on, wait for the state, remove the marker, tell the app to swap back.
- App crash or kill: the guard (a tiny detached process, the app's own binary started with a `--guard` argument) sees the app's process end, stops the helper if it is still running, switches Bluetooth on, removes the marker.
- Start-up: if the marker exists and no voice mode session is running, restore Bluetooth and tell the user it was restored.
- Controller trouble: if the helper reports a firmware crash, the app does not retry on its own. It stops, restores Bluetooth, and shows the reason. `bluetoothd` can need about 13 seconds to come back; the restore waits for it.

## Interface between the app and the helper

Control channel: a Unix socket in the app's private data folder, created by the app, one JSON object per line. Requests carry an id and get one reply. The helper also sends events.

| Request | Meaning |
| --- | --- |
| `phones` | Paired phones: name, address, connected, whether we hold a key |
| `calls` | Current calls: id, state, number, name |
| `dial`, `answer`, `hang_up`, `hang_up_all`, `tones` | Call control (`ATD<number>;`, `ATA`, `AT+CHUP`, `AT+VTS`) |
| `open_service` (`map` or `pbap`) | Opens an RFCOMM channel to the phone's service and returns it as a file descriptor passed over the socket (`SCM_RIGHTS`); the existing Rust OBEX code reads and writes it unchanged |
| `scan`, `pair`, `unpair` | Finding phones, pairing with a code shown in the app, removing a key |
| `status` | Controller state, link state, counters, versions |
| `shutdown` | Clean stop |

Events from the helper: `call_state`, `phone_connected`, `phone_disconnected`, `pairing_code`, `fault` (with a plain-text reason).

Voice channel: a second Unix socket carrying fixed-size frames of 8 kHz 16-bit mono PCM in both directions, plus a small header with a sequence number so gaps can be counted.

Mapping to `Platform` (all in the new `VoiceModePlatform`):

| `Platform` method | Voice mode |
| --- | --- |
| `phones` | `phones` request; `computer_name` from macOS as today |
| `open_map`, `open_pbap` | `open_service`; the returned descriptor becomes the `Stream` (with the same read and write timeouts the Linux code sets) |
| `calls`, `dial`, `answer`, `hang_up`, `hang_up_all`, `send_tones` | the requests above |
| `activate_audio` | not needed: voice follows the call automatically (default implementation) |
| `block_media_audio` | not needed: calls only |
| `can_pair`, `scan_nearby`, `pair`, `unpair` | `scan`, `pair`, `unpair`; the existing pairing window works as it does on Windows |
| `start_recording` | later, from the voice frames |

## Helper work (stage 4a)

New code in the helper, reusing what exists:

- `server.py`: the socket server and the message loop.
- `phone_manager.py`: one phone at a time. Connect with the stored key, authenticate and encrypt, open the hands-free channel, keep it alive, reconnect with backoff (not tight loops), report state.
- `services.py`: SDP lookup for MAP (0x1132) and PBAP (0x112F), RFCOMM channel open, byte pump to a passed descriptor, one open service per type at a time (phones allow one message session at a time).
- `pairing.py`: inquiry (with names from extended inquiry data or a remote name request) and Secure Simple Pairing. The numeric code goes to the app, which shows it in the pairing window. Our side confirms and the user compares on the phone, as on Windows. The safety filter needs a scoped exception for inquiry, allowed only while a scan the user started is running.
- Voice: the stage 3 voice link code, with the audio now going to the voice socket instead of the helper's own sound devices.
- Device identity: set the class of device to a car hands-free unit and publish a hands-free (HF) service record, so Android shows its "Phone calls" switch and iPhone treats the Mac as a car (Sync Contacts, Show Notifications). Decide whether to stay connect-only (we always initiate, like the reference project) or also accept connections from the phone. Connect-only is the first version.
- Tests: the same offline style as stages 2 and 3, with stand-ins for the controller and for the phone: message protocol, state machine, descriptor passing, reconnect behaviour, refusal paths. Bumble's local virtual controller may allow a simulated phone for the hands-free and RFCOMM parts; try it, do not depend on it.

Acceptance: the helper, started by a test script, passes the same checks as stages 2 and 3 through the socket interface, and a stand-in client can read a phonebook through a passed descriptor.

## App work (stage 4b)

In `crates/phone-macos` and `crates/phone-app`:

- `VoiceModePlatform` implementing `Platform` over the control socket, with a Rust fake helper in tests.
- `power`: the power functions (as in `launcher.c`) and the guard process.
- Helper lifecycle: start, health check, restart rules, shutdown.
- A `mac_voice_mode: bool` setting in `crates/phone-core/src/settings.rs` (default off), and the platform choice in `crates/phone-app/src/boot.rs` (`live_platform`).
- Settings screen: a "Mac voice mode" row, an explanation of the cost, a Turn on and Turn off button, and a persistent banner in the window while it is on ("Mac Bluetooth is off while voice mode is on").
- Pairing in voice mode goes through the helper, so the existing pairing window works; the first switch-on explains that the phone will be paired with the app, not with macOS.
- Startup recovery from the marker file.
- Tests: a fake helper process, marker and guard behaviour with the power functions stubbed, platform swap.

Acceptance: with a fake helper, the app switches modes, restores state after a simulated crash, and shows the right banners. With the real helper (4a), the full flow on the real phone.

## Voice with echo cancellation (stage 4c)

Stage 3b ran on the Mac's speakers with a half-duplex gate (microphone muted while the caller is loud). That is a test setup. For the product:

- Use the Mac's voice-processing audio unit (`kAudioUnitSubType_VoiceProcessingIO`), which gives echo cancellation, noise suppression and automatic gain, on the built-in speakers and microphone, so no headphones are needed.
- It lives in the app (Objective-C through the existing build.rs pattern), fed by the voice socket, resampling between 8 kHz and the unit's rate.
- Keep the half-duplex gate as a fallback if the unit cannot start.
- Keep call recording on the Platform interface in mind (the voice frames are the natural source).

Acceptance: a call on the Mac speakers with both people talking and no echo reported by the caller; counters (underruns, dropped frames) stay under the thresholds used in stage 3b.

## Packaging (stage 4d)

- Ship a Python runtime and the pinned wheels (`bumble`, its dependencies, `sounddevice` only if the helper keeps its own audio) inside the app bundle, with the helper scripts and `libacipc_bridge.dylib`. A standalone, relocatable Python build is the likely source. Estimated size: tens of megabytes. **Open:** accept that size, or port the stack to Rust later.
- Licence notices for everything shipped: ai-caller (MIT), Bumble (Apache-2.0), Python, PortAudio if used, and the dependencies' own licences.
- Signing: all the executables and libraries signed with the same identity. Distribution outside this Mac needs a Developer ID and notarisation; the private channel functions need no entitlement, but re-check with the hardened runtime on.
- `scripts/package-macos.sh` builds all of it; a test checks the bundle's contents and signatures.
- Updating: the app's self-updater must treat the helper and Python bundle as part of the update.

## Fragility and safety work (runs through all steps)

- **macOS changes.** The channel functions are not public Apple API. The helper checks at switch-on that the functions exist and the self-test passes. The app records the macOS build in its diagnostics and shows a plain message if the self-test fails after a macOS update, with voice mode disabled and normal mode untouched. Keep a short list of macOS builds known to work.
- **Controller crashes.** One voice link only; no vendor commands; no retries after a crash; restore Bluetooth and report.
- **Pairing keys.** One private file, mode 600, in the app's data folder, never logged. Moving from the test location is a one-time migration with a prompt. Removing a phone deletes its key.
- **Logs.** The helper writes a small rotating log with no keys, no message text, no full phone numbers.
- **Kill switch.** A command-line flag and a Settings button that restore Bluetooth and clear the marker, usable when the app is stuck.
- **Privacy.** Voice frames stay on the machine: no network, nothing stored unless the user turns on call recording.

## Order of work and checkpoints

| Step | Content | Checkpoint |
| --- | --- | --- |
| 4a | Helper server, phone manager, services, pairing, class of device and hands-free record | Offline tests; real run through the socket: pair, ring, answer, voice frames counted, phonebook read |
| 4b | `VoiceModePlatform`, power and guard, settings, banners, recovery | Tests with a fake helper; real run: switch on, call banner, answer, contacts and texts, switch off, Bluetooth back |
| 4c | Echo-cancelled audio in the app | Real call on the Mac speakers, both talking, no echo |
| 4d | Packaging and licences | Clean build, bundle check, run from the bundle on this Mac |
| 4e | iPhone, dial out, polish | Real runs with an iPhone and with an outgoing call |
| 5 (idea) | Switch to voice mode only when a call arrives | Not planned: the switch takes about 10 seconds, so the ring would be missed |

Each real run follows the rules used so far: one attempt per run, started only on the user's go, Bluetooth restored and checked afterwards.

## Decisions needed from the user

1. Accept a bundled Python runtime for the first release (larger app, fastest route), with a Rust port as a later project?
2. Accept an explicit "Mac voice mode" switch with the stated cost, as the shipped design?
3. Support Android first and iPhone after, as in the plan?
4. Is this for personal use on your Macs, or to be distributed to others? Distribution needs Developer ID signing and notarisation, and a support story for macOS updates.

## Out of scope

Two phones at once, two simultaneous calls, conference calls, HD voice (mSBC), media audio, Macs without this controller family (other models are untested), Intel Macs.

## 4b status, 2026-11-09: app side built and tested offline, not run against Bluetooth

Built in `crates/phone-macos/src/voice/`:

- `client.rs`, `fdpass.rs`: control connection to the helper, replies matched by id, descriptors received with `SCM_RIGHTS`, events to listeners, every waiting request fails when the helper goes.
- `platform.rs`: `VoiceModePlatform`, the helper behind the app's `Platform` trait (phones, calls, dial, answer, hang up, tones, scan, pair with code events, unpair, MAP and PBAP as real descriptors with 30 s stream timeouts). Dialled numbers are cleaned and refused if they hold anything but digits and `+ * # , ;`. After a controller fault no further request is sent.
- `lifecycle.rs`: `VoiceMode::enable` records the Bluetooth power state, writes a marker file, starts a guard process, switches system Bluetooth off, starts the helper and says hello; any failure restores Bluetooth first. `disable` stops the helper and restores. `recover_on_start` fixes a run that died. The guard (`app --macos-btguard <pid> <marker>`) restores if the app dies.

In the app: setting `mac_voice_mode` (default off), chosen in `boot::live_platform`, helper found in `Contents/Resources/helper/macos-btd` or `GBP_HELPER_DIR`, `boot::shutdown()` on exit, a Settings row that states the cost, and a note when voice mode could not start.

Tests: 40 in phone-macos, including the real Python helper in simulated mode (power off then on, ring, answer, a PBAP descriptor with the OBEX reply, restore). Helper suite 159, phone-core 161, app 11.

Not done: persistent banner while voice mode is on, switching without restart, echo cancellation (4c), bundled Python (4d), iPhone and dial-out live (4e). No live Bluetooth run yet.

## 4c status, 2026-11-09: call audio built and tested offline, the real audio unit not yet run

Built in `crates/phone-macos/src/voice/audio/`:

- `pcm.rs`: streaming linear resampler (chunk-independent, no drift over long runs) and a bounded playback buffer (200 ms, waits for 30 ms before playing, silence on running dry, counters for both).
- `bridge.rs`: `CallAudio` joins the helper's voice socket to an `AudioDevice`. Phone frames are resampled to the device rate and played; microphone audio is resampled to 8 kHz and sent as 15 ms frames with rising sequence numbers. A device that does not cancel echo gets the half-duplex gate (microphone dropped while the speaker is loud); one that does is never gated. A bad frame header ends phone audio without a crash; frames split across reads are joined.
- `controller.rs`: starts audio when the helper reports a call `active` and stops it on `ended`, so the microphone is open only during calls. An end that arrives before the start finishes leaves nothing running. A failed start is kept as a reason and the next call tries again.
- `vpio.rs` with `native/vpio.m`: the real device, the system voice-processing unit (echo cancellation, noise suppression, gain) on the built-in speakers and microphone. It tries 8, 16, 24 then 48 kHz, mono 16-bit, and reports the rate it got.
- `VoiceModeConfig.audio` and `boot.rs` wire it in; `VoiceMode::disable` stops audio first.

Tests: 64 in phone-macos. One runs the real Python helper in simulated mode with a stand-in device: ring (no audio), answer (phone frames reach the device), microphone frames reach the helper, hang up (audio stops), Bluetooth restored.

Not shown yet: that the voice-processing unit starts and cancels echo on this Mac (needs the signed app bundle and the microphone permission), and the sound quality. That is the live run for 4c: a real call on the Mac speakers with both sides talking.

## Live run 4b/4c: plan (packaged 2026-11-09, nothing run)

`scripts/package-macos.sh` now copies the helper into `GeekBridge Phone.app/Contents/Resources/helper/macos-btd`, with the development Python environment and the native bridge. That copy works on this Mac only; 4d replaces it. Checked offline: the bundled Python starts the bundled helper in simulated mode and answers hello over its socket; the signature carries the Bluetooth and microphone entitlements; the helper refuses a socket folder that others can read.

State before the run: the Fold5 is paired with our stack (key file present), macOS has no pairing for it, Bluetooth is on, the app is not running, no settings file exists (voice mode off).

One attempt, started on the user's go:

1. Write `mac_voice_mode: true` into the settings file, open the app bundle once. The first start asks for Bluetooth and microphone permission for the app; accept both.
2. Expect: Bluetooth off, helper starts, the Fold5 connects (up to about a minute), the app shows the phone as connected.
3. User rings the Fold5. Expect: the banner with Answer and Decline; answer on the Mac; talk both ways on the Mac speakers and microphone, no headphones; listen for echo.
4. Check contacts and texts load (this is the first run of the OBEX path through the Rust app), then hang up.
5. Quit the app. Expect Bluetooth back on within a few seconds; marker file gone. If the app is killed instead, the guard restores it.

Record: connect time, echo (none, some, bad), levels, drops (`AudioStats`), contacts and texts result, Bluetooth restored.

## Live run 4b/4c result, 2026-11-09 18:12 to 18:23: worked

One run of the packaged app with voice mode on.

| Step | Result |
| --- | --- |
| Start | System Bluetooth switched off, helper and guard started, Fold5 connected within about 30 s, app showed "Phone connected" |
| Incoming call | Banner showed the right number and name |
| Voice | Answered on the Mac; talk both ways on the Mac speakers and microphone, no headphones; user: "is good" (echo cancellation in the voice-processing unit, no gate) |
| Contacts and texts | Loaded through the Rust OBEX path; user: "works" |
| Quit | Bluetooth back on, marker file gone, `/tmp/gbp-501` folders removed |

Found:

1. The phone shows as its Bluetooth address, not its name: the stage 2 pairing never stored a name, and the helper only records names when it pairs. Fix: store the name at pairing, and read it from the phone for existing pairings.
2. Cmd+Q only hides the window to the tray (setting "keep running in tray" is on by default), so system Bluetooth stays off until the tray's Quit is used. Fix: say so in the Settings text, and show a banner while voice mode is on.
3. After the quit a second helper process (started 18:22, parent 1, no app) was left running with Bluetooth on. It was stopped by hand with SIGTERM; Bluetooth and files were clean afterwards. A helper that never had an app waits for ever (the grace period counts only after an app has left). Cause of the extra start not yet known. Fix: the helper exits if no app connects within 60 s.
4. Not measured: audio counters (`AudioStats`) and echo level; judged by ear only.

## Fixes after the live run, 2026-11-09 (offline; repackaged, not run again)

1. Phone name: the helper's HCI filter refused the remote-name request outside a scan, so pairing never stored a name (the call was wrapped in a silent try). The filter now allows it for the target phone at any time and for any other address only during a scan. After each connect the helper asks once for the name if none is stored (`_learn_name`). Existing pairings get their name on the next connect.
2. Tray: while voice mode runs the app shows an info banner "Mac voice mode is on" saying Bluetooth is off until the app quits and that closing the window only hides it, with a "Quit app" button. The Settings text for the tray option says the same.
3. Orphan helper: the helper exits if the app sends no request within 60 s (`--idle`). The helper suite has tests for both cases (no app: stops; app connected: keeps running).

Tests: helper 164, phone-macos 64, phone-core 161, app 11. The cause of the second helper start after the quit is still unknown; the guard and the idle stop now limit the harm.

## Second live run, 2026-11-09 18:30: fixes 1 and 2 confirmed, quit path found faulty

- Phone name shows ("Sam's Z Fold5") and the voice-mode banner shows.
- "Quit app" closed the window and Bluetooth came back, but the helper was still alive 5 s later and left an empty `/tmp/gbp-501`. Cause: the app's `on_exit` never called `boot::shutdown()`, so only the guard restored Bluetooth and the helper stopped on its 15 s grace timer. This is probably also where the earlier orphan helper came from. Fix: `on_exit` now stops the helper and restores Bluetooth itself (repackaged; not yet confirmed live). The guard stays as the crash backup.
- The first attempt of this run did not launch the app at all (a background `open -W` in a subshell); the second, direct `open` worked.

## Third live run, 2026-11-09 18:45: quit path confirmed

Open, phone connected, "Quit app" on the banner. Afterwards: Bluetooth on, no app, guard or helper process, no `/tmp/gbp-*` folders, marker file gone, no errors in the log. The app now stops the helper and restores Bluetooth itself; the guard is only the crash backup.

Status of stage 4: 4a to 4c done and confirmed live (connect, name, caller banner, answer, two-way voice on the Mac speakers, contacts and texts, clean quit). Open: 4d (bundled Python, licences, signing, update path), 4e (iPhone, dial-out, scanning and pairing through the helper live), audio measurements, the unexplained second helper start in the first run.

## 4d plan and progress, 2026-11-09

Findings:

- Disk: 4.6 GiB free. The bundle is about 90 MB, so this is fine.
- No Developer ID certificate is installed (`security find-identity` finds none), so signing stays ad hoc and notarisation is not possible yet. The script signs every Mach-O file inside the bundle from the inside out so that a certificate can be dropped in later (`SIGN_IDENTITY`).
- The helper needs only Bumble (installed with `--no-deps`), cryptography, cffi, pycparser, pyee and typing_extensions. The full helper suite (164 tests) passes in an environment holding just those. Dropping the rest removes libusb (LGPL), pyusb, pyserial, websockets and prompt_toolkit. All remaining licences are Apache-2.0, MIT, MIT-0, BSD-3-Clause or PSF. Pinned in `helper/macos-btd/requirements-bundle.txt`; licence text is generated by `gen_licences.py` from the packages' own metadata, plus the native bridge's MIT licence.
- Runtime: the Homebrew Python is not relocatable (absolute `/opt/homebrew` paths), so a standalone runtime is needed. Candidate: python-build-standalone release 20261026, `cpython-3.13.18+20261026-aarch64-apple-darwin-install_only_stripped.tar.gz`, 25,265,774 bytes, sha256 `8392b6ea51d94391befb2c31ebe75463f045d0b69dfcf5e1bb3234e1f5fa3405` (digest as listed by GitHub). Not yet downloaded.
- Apple silicon only: the native bridge and the controller path were tested on an M3 only; an Intel build is not planned.

## 4d status, 2026-11-09: self-contained bundle built and checked offline

Downloaded with the user's yes: `cpython-3.13.18+20261026-aarch64-apple-darwin-install_only_stripped.tar.gz` from astral-sh/python-build-standalone (25,265,774 bytes), sha256 checked against GitHub's listed digest (`8392b6ea...a3405`) before use; unpacked in its own folder.

- `helper/macos-btd/make-runtime.sh` builds `build/python`: verifies the checksum, unpacks, installs the pinned packages (`requirements-bundle.txt`, `--no-deps`), removes pip, Tk, tests and docs. 61 MB, links only system libraries, no `/opt/homebrew` reference.
- The helper suite (164 tests) passes on that runtime in a clean environment (`env -i`). One test, the descriptor-leak check, raced with the server's accept on this runtime; fixed by a round trip before the baseline (no leak existed).
- `scripts/package-macos.sh` now copies our helper, the bridge and the runtime (no virtual environment), precompiles bytecode, writes `Resources/THIRD-PARTY-LICENSES.txt` (packages' own licence files, the bridge's MIT licence, Python's licence), and signs every Mach-O file inside the bundle before the app. `SIGN_IDENTITY` takes a Developer ID (hardened runtime and timestamp are added then); the default is ad hoc. Bundle: 76 MB.
- The helper is started with `-B`: a run that wrote `__pycache__` into the bundle broke the signature seal (`a sealed resource is missing or invalid`). With `-B` and precompiled bytecode, `codesign --verify --strict --deep` passes before and after a run.
- Protocol check: the app refuses a helper whose `hello` reports another protocol version, so a partial update fails with a clear message.
- Update path: the app's updater has no macOS handling yet, so an update means replacing the whole app. Pairing keys (`~/.local/share/geekbridge-phone/mac-bt`), settings and the cache live outside the bundle and survive it.

Not done: a Developer ID certificate and notarisation (none on this Mac; needed before the app runs on other Macs without a Gatekeeper warning), an installer or disk image, a macOS update channel, an Intel build (not planned), and a live run of the new bundle (the bundled runtime has only run the simulated helper).

## Live run of the self-contained bundle, 2026-11-09: worked

The 93 MB app with its own Python runtime, run once with voice mode on: the helper started from `Resources/helper/macos-btd/python`, the Fold5 connected under its name, an incoming call was answered on the Mac with voice both ways, contacts loaded, and "Quit app" left Bluetooth on, no process, no `/tmp/gbp-*` folder and no marker file. `codesign --verify --strict --deep` passed while the app ran and after it quit (no bytecode written into the bundle). 4d is confirmed live for this Mac.

## Dial-out live, 2026-11-09: worked

One run of the bundled app: a number dialled from the keypad placed the call through the helper (`ATD`); user: "calling from the dial worked too". Quit left Bluetooth on, no process, no `/tmp/gbp-*`, no marker; seal valid; log empty. Whether voice was checked both ways on the outgoing call was not stated; incoming calls were confirmed two-way earlier.

Scope change: iPhone support is dropped from stage 4 (iPhones use the native macOS path). Left in 4e: scanning and pairing a new Android phone through the helper, live.

## Pairing check through the helper: plan, 2026-11-09 (nothing run)

Goal: pair a phone from inside the app with no System Settings and no stage 2 script: remove the Fold5, scan, pair, and see it connect. This is how a new Android user will start.

Offline prerequisites (done): the helper's scan, pair and forget run in simulated mode through the real Rust platform (`removing_a_phone_then_scanning_and_pairing_works_through_the_real_helper`): list empties after removal, scan lists only unpaired phones, the code reaches the app while pairing runs, the new phone is listed under its name and selected without a restart. The current key file and name file are copied (mode 600) to `~/.local/share/geekbridge-phone/mac-bt/backup-before-pair-check/`.

How the helper pairs: the Mac's side confirms automatically and the app shows the six-digit code; the user compares it with the phone and taps Pair there (as on Windows). Discovery runs only during the user's scan; the filter allows no inquiry otherwise and no other address.

One attempt, started on the user's go:

1. Open the packaged app with voice mode on; the Fold5 connects (as in the last runs).
2. Settings, remove "Sam's Z Fold5" (the helper forgets its key). The pairing window opens by itself.
3. On the Fold5: forget "Alex's MacBook Air" (Settings, Connected devices, previously connected devices), then open Pair new device and leave the screen on and unlocked.
4. In the app's pairing window press Scan (about 10 s). Expect the Fold5 in the list. Press it once.
5. Compare the code in the app with the one on the phone; tap Pair on the phone.
6. Expect: pairing done, name shown, the phone connects without a restart; contacts load. Then Quit app.

Abort rules: one scan and one pair attempt; if the phone is not listed, stop and say what the phone shows; if pairing fails, no retry, report the error text. Afterwards: Bluetooth on, no process, key file mode 600 and no key bytes in logs, no firmware crash in the `bluetoothd` log.

Risks: the Fold5 keeps its old "Mac" entry if step 3 is skipped, and pairing then fails on a stale key (fix: forget it on the phone). If pairing fails after the removal, the phone has no working pairing until this succeeds or the user pairs it through System Settings (the older IOBluetooth path, voice mode off).

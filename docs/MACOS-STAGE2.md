# Stage 2 plan: pair the Android phone with our own stack

Part of [MACOS-HEADUNIT-ROUTE.md](MACOS-HEADUNIT-ROUTE.md). Stage 1 showed that a normal process can open Apple's HCI, ACL and SCO channels on this Mac and talk to the controller. Stage 2 adds a Bluetooth host stack on top and brings up one real link to the Fold5.

Nothing in this document has been run. It follows the shape of the reference project (`lzsnolimit/ai-caller`, MIT), which reaches "authentication and hands-free service level connection, no calls or audio yet" as its own stopping point before calls.

## Goal

With system Bluetooth off, our stack:

1. connects to the Fold5 (`6C:AC:C2:00:00:01`), and only to it,
2. pairs with it using Secure Simple Pairing and a numeric comparison,
3. reads its service list over SDP,
4. opens an RFCOMM channel to its hands-free audio gateway service and completes the service level connection (the opening AT command exchange),
5. disconnects cleanly, and system Bluetooth comes back on.

No calls, no audio, no messages in this stage.

Pass: all five happen in one run, the pairing key is saved, and Bluetooth is on again afterwards. Fail: any step fails, the controller reports a firmware crash, or Bluetooth does not come back.

## Preparation status, 2026-11-09 16:20: done, nothing run against Bluetooth

Built and checked offline in `tools/macos-hci-probe/` (see its README):

- Python environment with `bumble==0.0.236`; every Bumble name used was checked to exist in that version.
- Native bridge compiled from the unmodified reference source (`-Wall -Wextra -Werror`).
- `gbp_acipc.py`, `stage2_pair.py` and `launcher.c` written; launcher built into a signed app bundle.
- 17 offline tests pass: the filter refuses vendor commands, stored-key commands, inquiry, and anything naming another address or link; the key store accepts only the target's authenticated key, writes mode 600, and never logs key bytes.
- Sanity runs without Bluetooth: the launcher and the program refuse bad input, the library loads without being opened, and Bluetooth stayed on.
- The filter is tighter than the reference project's: connection commands may only name the target, and discovery is refused.

Confirmation of the pairing code is a real numeric comparison: a dialog on the Mac shows the six digits and asks whether the phone shows the same number; any answer except a clear yes, or no answer in 40 seconds, rejects the pairing. You then tap Pair on the phone.

## What gets built

All under `tools/macos-hci-probe/`, none of it in the app yet.

| File | Role |
| --- | --- |
| `launcher.c` | The only part that touches system Bluetooth power. Runs inside a signed app bundle (macOS aborts a bare process that reads or sets the power state). Remembers the power state, switches Bluetooth off, starts the Python program as a child, waits with a watchdog, switches Bluetooth on again, and checks it. Restores on every exit path, including signals. |
| `libacipc_bridge.dylib` | Built from the reference project's `acipc_bridge.c`, copied unmodified with its licence (already in `third_party/ai-caller/`). |
| `gbp_acipc.py` | Our Bumble transport over the bridge, with a safety filter. |
| `stage2_pair.py` | Pairs, reads SDP, opens the hands-free channel, does the service level connection, disconnects. |
| `build.sh` | Builds the dylib and the launcher app, signs it ad hoc, creates the Python environment. |

Python environment: a virtual environment inside the project, ignored by git, with `bumble==0.0.236` (the version the reference project pins; newer versions are untested here). Python 3.13 is installed. The reference project adds `audioop-lts` on 3.13 for audio, which stage 3 needs and stage 2 does not.

### Safety filter in the transport

Copied in spirit from the reference project, because it is what keeps a bug from touching your other devices:

- Vendor HCI commands (opcode group `0x3F`) are refused. That includes firmware loading.
- Authentication, encryption, link key and pairing commands are allowed only when they name the Fold5's address or its connection handle.
- Any pairing event for another address stops the run.
- Stored link keys on the controller are never read, written or deleted.

### Key storage

One private file, `~/.local/share/geekbridge-phone/mac-bt/pair-keys.json`, mode 600, outside the repository. The key is never printed or logged. Only the Fold5's key is accepted.

## What you will do

Before the run:

1. On the Fold5, forget "Alex's MacBook Air" (Settings > Connected devices). This removes the phone's old key, which our stack does not have.
2. On the Mac, forget "Sam's Z Fold5" in System Settings > Bluetooth. This removes macOS's key. It is reversible: pairing through System Settings again, as you did before, brings it back.
3. Open the pairing screen on the phone (Settings > Connected devices > Pair new device) and leave it on, screen on, unlocked.

During the run: a pairing request appears on the phone with a six-digit code. Compare it with the code I report from the log and tap Pair. Our side confirms automatically; the check is yours, on the phone.

After the run: nothing, unless you want the Fold5 back with macOS. Then repeat your System Settings pairing.

## Steps

1. Offline preparation, no Bluetooth: create the environment, install `bumble==0.0.236`, build the dylib and launcher. Run the transport's own unit tests with a stand-in for the bridge, as the reference project does.
2. Record a baseline: `system_profiler SPBluetoothDataType`, the Bluetooth power state, and a marker for the `bluetoothd` log.
3. You do the three steps above.
4. I start the launcher once. It runs the sequence in Goal, with a 90 second limit.
5. I read the launcher's output and the `bluetoothd` log, check that Bluetooth is on, and write the result into the docs.

## Abort rules

- Stop and restore if the power state is not on at the start.
- Stop if the controller reports a firmware crash, or any channel call returns an error other than a timeout.
- Stop if more than one pairing attempt would be needed: one attempt per run, no loops.
- If the Fold5 does not show a pairing request within 30 seconds, stop and ask you what the phone shows.
- Never run any other device through this stack.

## Risks

- A firmware crash can leave `bluetoothd` in a restart loop of about 13 seconds, during which Bluetooth cannot be switched on. The reference project saw it with two simultaneous calls, not with one link.
- macOS 27.0 is a pre-release build and the reference project was tested on 26.4. The channel functions are not public API.
- While system Bluetooth is off, keyboards, mice and AirPods on the Mac disconnect. The built-in keyboard and trackpad are not Bluetooth.
- Pairing with our stack replaces the Fold5's pairing with macOS until you pair it again in System Settings.
- The Python environment adds a package download (`bumble` and its dependencies) from PyPI. Nothing else is downloaded.

## Stage 2 result, 2026-11-09 16:21: passed on the first run

One run through `launcher`, 13 seconds from start to Bluetooth restored. Log lines, in order:

| Step | Result |
| --- | --- |
| Stack start | Our Bumble stack started on the built-in controller (HCI version 12, Bluetooth 5.3), no firmware loaded |
| Connection | Link to the Fold5 (`6C:AC:C2:00:00:01`) established; no other address touched |
| Code check | Dialog on the Mac showed `437873`; you confirmed it matched the phone after 6.4 seconds and tapped Pair |
| Pairing | Authenticated key saved, link encrypted |
| Services | The phone lists one hands-free gateway, one phonebook (PBAP) and one messages (MAP) service |
| Hands-free | RFCOMM channel opened, service level connection completed, 7 AG indicators read |
| Close | Clean disconnect (status 0, reason 22, ended by us), channels closed after 79 packets sent and 141 received |
| Restore | Launcher switched system Bluetooth on again; it reports on, system_profiler agrees |

Checks afterwards: no firmware crash in the `bluetoothd` log; the key file is mode 600 in a mode 700 folder outside the repository; no key bytes in the log.

What this settles: our own stack, on the Mac's built-in chip with no driver, pairs with an Android phone, reads its services and holds a hands-free control channel. The three services the app needs (calls, contacts, messages) are all offered.

Still not shown: ringing and call control through this stack, SCO voice, and MAP and PBAP data through RFCOMM.

State left on your devices: the Fold5 is now paired with our stack, not with macOS, and macOS has no pairing for it. To get it back under macOS, forget "Alex's MacBook Air" on the phone and pair it again in System Settings.

## After stage 2

If it passes: stage 3, hands-free calls (ring, answer, dial, hang up, caller number) and the SCO voice path to the Mac's speakers and microphone. If the pairing step fails but the controller works, the usual causes are a stale key on either side or the phone's pairing screen not being open, and the log shows which.

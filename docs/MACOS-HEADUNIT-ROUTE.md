# macOS call voice: the head-unit route (found 2026-11-09)

This replaces the driver route in [MACOS-DRIVER-SPIKE.md](MACOS-DRIVER-SPIKE.md) and [MACOS-DRIVER-GATE3.md](MACOS-DRIVER-GATE3.md) as the first thing to try. Those two documents stay as the fallback.

## What changed

Earlier notes in this repository said call voice needs a custom driver or a second Bluetooth adapter. That was too pessimistic. A public project already carries two-way call voice on a Mac's built-in Bluetooth, with no driver, no kernel extension and no change to SIP:

- Project: [lzsnolimit/ai-caller](https://github.com/lzsnolimit/ai-caller), MIT licence (copyright 2026 ai-caller contributors). Reading, learning from and reusing its code is allowed with the licence notice kept.
- Its README reports "HFP dialing, two-way SCO voice and AI calls passed tests" on macOS 26.4 (Mac16,7) with a BCM_4388C2 built-in controller and an Android phone. An iPhone also passed calls according to its README. Only that one Mac model is tested.
- Its research log, `docs/bluetooth-multiplexing-exploration.md`, says the same stack gives full two-way audio for one call at a time, and that two simultaneous calls crash the controller firmware.

## How it works

The Mac acts as a car head unit with its own Bluetooth host stack in user space:

1. Apple's driver has already booted the chip. Nothing is loaded and no driver is replaced.
2. The native bridge `server/mac/native/acipc_bridge.c` finds three Apple channels in the IORegistry. They are the `AppleConvergedIPCRTIInterface` nodes whose `ACIPCInterfaceProtocol` is `hci`, `acl` and `sco`. Each carries an `IOSkywalkNexusUUID`.
3. It opens them with the system's `os_channel_*` functions (`os_channel_create_extended` and others, found with `dlsym`). Packets move through ring slots, 64 bytes per slot descriptor.
4. Google's [Bumble](https://github.com/google/bumble) Bluetooth stack runs on top: HCI, L2CAP, SDP, RFCOMM, pairing, and the hands-free profile with SCO.
5. System Bluetooth is turned off for as long as this runs, so the built-in controller is free. At exit it is turned on again. The project uses `blueutil` for that.
6. The phone is paired with this stack, not with macOS. The new pairing can replace the phone's pairing with macOS.

No sudo, entitlements or SIP change appear anywhere in the project's documents or code.

## Costs and limits

- System Bluetooth is off while it runs: keyboards, mice and AirPods on the Mac disconnect.
- One call with audio at a time.
- The gateway starts the connection to the phone. The phone cannot start it.
- Only one Mac model and one macOS version were tested by the project. This Mac (MacBook Air M3, BCM4388, firmware 24.1.584.4716, macOS 27.0 build 26A5425a, a pre-release) differs in all three.
- The `os_channel` functions are not public Apple API. A macOS update can break them. The project's author found that a controller firmware crash can leave `bluetoothd` in a restart loop of about 13 seconds.
- SCO voice in that project is 8 kHz PCM.

## Checked on this Mac, read-only

- All 14 `os_channel_*` functions the bridge uses are present.
- The IORegistry shows the three channels with their nexus IDs: `hci` `7876BC83-E653-45A1-8C00-0EDBEB6317D8`, `sco` `4D891B35-1D54-4B6F-9A1F-7C073D3AA02C`, `acl` `0E5C7775-69E2-44DE-92B6-B8A1B2F4ED15`. There are also `tsi` and `iso` channels.
- Missing tools: `blueutil` and Python's `bumble`. Free disk space was 6.6 GiB.

Not checked: whether a normal process can actually open those channels on this macOS build. That is stage 1.

## Stage 1 result, 2026-11-09 16:12: passed

Program: `tools/macos-hci-probe/stage1.c`, built with the Command Line Tools, run once inside a small signed app bundle (a bare binary is aborted by macOS for lack of a Bluetooth usage description, even to read the power state). No root, no entitlements, no SIP change. It uses the project's `acipc_bridge.c`, copied unmodified with its licence into `tools/macos-hci-probe/third_party/ai-caller/`.

- System Bluetooth was switched off, then the three channels opened (`acipc_open` returned 0) 3 seconds later.
- The controller answered three read-only HCI commands, all with status 0:
  - Local version: HCI and LMP version 12 (Bluetooth 5.3), HCI revision 0x826c, manufacturer 0x000F (Broadcom), LMP subversion 0x0248.
  - Address `9C:58:84:00:00:02`, identical to the address system_profiler shows for this Mac.
  - Buffers: ACL 2764 bytes x 8, SCO 254 bytes x 1. This matches the project's log.
- Bluetooth was switched on again about a second after the channels closed, and system_profiler shows it on. No firmware crash was logged.

So on this Mac (MacBook Air M3, macOS 27.0 pre-release build 26A5425a, firmware 24.1.584.4716) a normal process can open the Apple channels and talk HCI to the built-in controller. The project's own results cover only macOS 26.4 on another model.

Not yet shown: pairing, any link to a phone, hands-free signalling, SCO voice.

## Plan

1. **Stage 1, controller test (done, above).** In a private folder: build the bridge with the Command Line Tools, install `bumble` in a virtual environment, power system Bluetooth off, open the three channels, send HCI Reset and read the local version and address, close, power system Bluetooth on. No pairing, no phone. Proves the channels open as a normal user here.
2. **Stage 2, pairing and link (done 2026-11-09, passed; see MACOS-STAGE2.md).** Pair the Android phone with the new stack and open an RFCOMM link.
3. **Stage 3, calls (3a and 3b done 2026-11-09: a real call answered on the Mac, voice heard both ways through the Mac's speakers and microphone; see MACOS-STAGE3.md).** Hands-free profile: ring, answer, dial, hang up, caller number, and SCO voice through the Mac speakers and microphone with CoreAudio.
4. **Stage 4, product.** Run the stack as a helper process behind the same `Platform` interface the app already uses, so messages and contacts (MAP, PBAP over RFCOMM) and calls all come from one place. Add a clear "Mac voice mode" switch that says system Bluetooth is off while it is on. Keep the IOBluetooth backend as the default.
5. **Later.** Replace the Python helper with Rust if the dependency is too heavy to ship.

## Safety rules for the tests

- One attempt per step, started only when the user says go. No loops, no retries.
- Turn system Bluetooth off only for the length of a step and always turn it on again, then confirm the power state.
- Never run the downloaded reference code as it is. Read it, then build our own bridge from it with the licence notice kept.

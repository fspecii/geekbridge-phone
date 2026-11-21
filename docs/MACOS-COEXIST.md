# macOS call voice without losing the mouse and headphones (stage 5)

Part of [MACOS-HEADUNIT-ROUTE.md](MACOS-HEADUNIT-ROUTE.md). Stages 1 to 4 carry a call's voice by running our own Bluetooth stack on the Mac's built-in controller, with system Bluetooth off. The cost was that everything else on Bluetooth dropped: mouse, keyboard, headphones. On a Mac with no built-in trackpad or microphone that is not usable.

This document records why the obvious fixes are closed, the route that is left, and the tests that decide it. Requirements, set by the project owner: no extra Bluetooth adapter, nothing installed on the phone, and no "use a wired mouse or wired headphones" answer.

**Status, 2026-11-21, end of day.** Two routes were tried live on a Mac mini (Mac16,10), BCM_4388C2, firmware 24.2.241.1930, macOS 27.2 (26B123).

- **Route A, our stack serves the devices itself** (the first half of this document): the mouse worked, headphones linked but played nothing audible, and the owner rejected it as unreliable. Kept as a record; not pursued.
- **Route B, macOS keeps every device and we sit between it and the chip** (from "A simpler route to test first" onward): works. Two real calls were answered on the Mac with sound both ways while macOS served its own Bluetooth devices. It is a test rig in `tools/macos-hci-probe`, not yet part of the app.

Read route B first if you only want what works.

## What is closed, and how it was checked

All of this was read from the running system. Nothing was changed.

| Idea | Finding |
| --- | --- |
| Send our own commands to the chip while macOS keeps Bluetooth | `BluetoothHCISendRawCommand`, `BluetoothHCIVendorCommand`, `BluetoothHCIRequestCreate`, the synchronous-connection calls and `IOBluetoothAddSCOAudioDevice` in IOBluetooth are two instructions long: load 0, return. |
| Switch on a hands-free role inside `bluetoothd` | The override exists: `StackSettings` / `HFServiceEnable` in `com.apple.MobileBluetooth.debug`, read with plain `CFPreferencesCopyAppValue`. The code path only runs for device class 6. This Mac's `DeviceClassNumber` is 9, and class 9 already has that service bit set: it is the Mac's audio-gateway role toward headsets. `bluetoothd` holds only gateway-side AT strings (`+BRSF:`, `+CIEV:`); there is no hands-free-unit code in it. |
| Get `bluetoothd` to accept the phone's voice link | The connect-request handler asks the SCO application registered for that link, or the default one. With none, or a no, it rejects: "SCO/eSCO reject (HOST_REJ_RESOURCES) ... SCO connect-accept declined". That is the line PhoneBT users see once a second. `bluetoothd` has no XPC handler for SCO or raw HCI. |
| Open the chip's channels beside `bluetoothd` | Skywalk channels have one owner per port (XNU `channel.c`; read through a summary, not line by line). |
| Another transport for call audio | No public isochronous (LE Audio) API on macOS. Android sends call audio only to SCO, LE Audio or hearing aids. |
| Two voice links from our own stack (phone + headset microphone) | The reference project found that two host-sent voice streams crash this chip's firmware, with every setting they could vary. |

So call voice needs our stack to own the controller. What can change is how long it owns it, and what it does for the other devices meanwhile.

## The route: own the radio only for the call, and serve the other devices from our stack

1. **Idle: system Bluetooth stays on.** The app uses the IOBluetooth backend as it does today: ring banner, texts, contacts, call control. Mouse and headphones are macOS's.
2. **A call is answered or dialled: the radio changes hands.** The app switches system Bluetooth off, starts the helper, and the helper connects the phone, the mouse and the headphones.
3. **During the call the helper is the host for all three:**
   - Phone: the hands-free link and the one voice link (stage 3 and 4, unchanged).
   - Mouse: classic HID host; reports become pointer events (Quartz events, Accessibility permission).
   - Headphones, sound out: the caller's voice goes to them over A2DP. That is ordinary data, not a voice link.
   - Headphones, microphone in: for AirPods, the microphone stream of Apple's accessory channel (AAP, L2CAP PSM 0x1001, AAC-ELD). Also ordinary data. For other headphones there is no way to get the microphone without a second voice link, so the Mac's own microphone is used.
4. **The call ends: the radio goes back.** The helper stops, Bluetooth is switched on, and the devices return to macOS on their own.

### Why nothing has to be paired again

Our stack runs on the same controller, so it has the same Bluetooth address as macOS. A device recognises its host by address and pairing key. If our stack answers with the key macOS holds, the device cannot tell the difference, and macOS's own pairing is never touched.

macOS keeps classic pairing keys in the System keychain: service `MobileBluetooth`, account = the device address. On this Mac the Magic Mouse, the AirPods and the FreeBuds each have one. Reading an item makes macOS ask for an administrator's approval. `tools/macos-hci-probe/keys_import.py` copies one key into our private key file (mode 600, outside the repository) and never prints it.

The same applies to the phone: with its macOS key imported, one pairing serves both modes. That removes the stage 2 problem where pairing with our stack replaced the phone's pairing with macOS.

Imported keys are read-only in our stack. If a device asks for a new pairing, the stack refuses and the run fails, so a run can never replace the key macOS holds.

### Safety filter for several devices

The one-phone filter becomes `CompanionScope` (`gbp_companions.py`): one phone plus a fixed list of companions.

- Every old rule stays: no vendor commands, no stored-key commands, no discovery outside a user scan.
- Links, pairing replies and data only for the listed addresses. Links are learned from the controller's own connection events, not set by the stack.
- A voice link is accepted or set up only with the phone, and only one. A voice request from headphones is refused.
- A stranger's connection request is refused in the transport and never reaches the stack. (The stack must be connectable now, because a mouse that is touched calls its host.)

## Limits, stated up front

- **The handover is visible.** Mouse and headphones drop for a few seconds when the call starts and again when it ends. How many seconds is what gate 5d measures.
- **Headphone microphone is AirPods-only**, and the AAP microphone is confirmed publicly on AirPods Pro 3 only. Gate 5b shows what these AirPods (product 0x2019) do.
- **Only call audio reaches the headphones during a call.** Music or other Mac sound is not bridged in that window.
- **A2DP adds delay** to the caller's voice (the codec and the headphones' buffer), more than a voice link would.
- **Bluetooth Low Energy devices are not covered yet.** The second mouse here (X11mouse2) is BLE. It needs the LE keys (keychain service `BluetoothLE`), an HID-over-GATT host, and LE rules in the filter.
- **Scrolling on a Magic Mouse** is rebuilt from finger positions (layout from Linux `hid-magicmouse.c`). Momentum and gestures are not.
- All of it still rests on Apple's private controller channels, as stages 1 to 4 do.

## Gates

One attempt per step, started only when the owner says go. Each run switches system Bluetooth off for a bounded time and the launcher always switches it back on. On a Mac with only a Bluetooth mouse, the pointer is gone for that time unless `--inject` works.

Before any gate, once per device: `keys_import.py <address>` (administrator approval).

| Gate | Question | Tool | Pass |
| --- | --- | --- | --- |
| 5a | Does the mouse reconnect to our stack with macOS's key, and do its reports arrive and move the pointer? | `stage5a_mouse.py` | `mouse-result` with decoded reports; with `--inject`, the pointer moves |
| 5b | Do the headphones accept our stack and play a tone? Do AirPods stream their microphone over AAP while it plays? | `stage5b_headphones.py` | the tone is heard; `microphone_passed` true for AirPods |
| 5c | Do phone call, mouse and headphones work at once: one voice link, HID and A2DP beside it, no firmware crash? | `stage5c_all.py` (sound not bridged yet: silence in the call, a tone in the headphones, the microphone counted) | `stage5c-pass`: clean voice traffic and mouse reports; `microphone_ok` for AirPods |
| 5d | How long is the handover, both ways? | to write | seconds from Answer to voice, and from hang-up to macOS having the devices back |

Known unknowns the gates settle:

- **Key byte order.** Stacks disagree on the order of a link key at rest. If 5a fails authentication, import again with `--reversed` and repeat once. A wrong key makes the device refuse the link; it is not expected to make it forget macOS, but that is unverified.
- **Whether a refused authentication disturbs macOS's pairing.** Check that the mouse reconnects to macOS after a failed run before trying anything else.
- **AAP on these AirPods**, and whether the stream needs A2DP suspended and resumed around the start (LibrePods found it sometimes does).

If 5a fails with both key orders, the route is closed for mice and this document says so. If only 5b's microphone fails, AirPods get sound out and the Mac's microphone, like other headphones.

## Live results, 2026-11-21 (Mac mini, Magic Mouse USB-C, AirPods product 0x2019)

Bluetooth came back on cleanly after every run.

| Run | Result |
| --- | --- |
| 5b, 21:33 | AirPods linked to our stack with the imported key, in the byte order macOS stores it; link encrypted; A2DP stream started and the tone was sent. The AAP channel did not open within 6 s; an answer arrived after the test had given up. Whether the tone was heard is not recorded. |
| 5a, 21:33 | The mouse reconnected to our stack by itself. The run stopped on event 0x30 (encryption key refresh complete), which the filter listed as an unexpected pairing event. It is routine for a device that leads its link; the filter now allows it on our own links. |
| 5b, 21:34 | Authentication failure with the key that had worked a minute earlier. |
| 5a, 21:34 | The mouse did not call within 25 s; our stack called it and authentication and encryption passed. Two reports in 45 s, neither in the expected format. Not known whether the mouse was being moved. |

| 5a, 21:43 | Passed. Our stack called the mouse; 825 reports in 45 s, 823 decoded, 798 with movement, 19 with a button. All were 8 bytes: the single multitouch request had not taken effect, so no finger data and no scrolling. The pointer was not driven: Accessibility was not yet granted. |
| 5a, 21:45 | **Passed, with the pointer driven by our stack.** 3103 reports, 3101 decoded, 1665 with movement, 113 with a button, 29 producing scroll lines; 1714 pointer events posted. The multitouch request was answered 2 (refused) the first time and 0 (accepted) on the second and third: asking again after a second is needed. Reports then came in 22, 30 and 38 bytes (one, two and three fingers). |
| 5b, 21:47 | With the key read from the keychain right before the run: linked, encrypted, tone stream started. That confirms the key rotation and the fix. The AAP channel was refused with an L2CAP error (not a timeout this time); the reason was not logged. The probe now logs it and tries three times. |

Findings:

- **Gate 5a is passed** on this Magic Mouse: macOS's key works as stored, our stack can call the mouse or be called by it, its reports decode with the Linux layout, and Quartz events move the pointer once `GBBtLauncher` is allowed under Accessibility. How the scrolling feels is not judged yet.
- **A mouse that is in use does not always call the host.** In three runs it called by itself once. The stack should call the device at once and not wait.
- **macOS 27 keychain format.** The item is a property list with `LinkKey` as text (sixteen hex pairs joined by dashes), `LinkKeyType` as the word `Authenticated` or `UnAuthenticated`, and `LocalAddress`. AirPods items also carry `MagicAccEncKey`, `MagicAccHint`, `MagicAccIRK`, `MagicAccKey` and `MagicAccRachet`.
- **AirPods change their key between sessions.** Ten minutes after the import the keychain held a different key for the AirPods (compared by fingerprint, never printed); the mouse's key was unchanged. So the key must be read from the keychain right before each handover, not imported once. `run_stage5.sh` now does that. Not yet known: whether a session with our stack makes the AirPods and macOS disagree afterwards. They reconnected to macOS after the 21:33 run.
- **Reading a key showed a macOS dialog on some reads and not on others.** The app will need its own "always allow" on those items.

## After the gates

1. Helper: `CompanionScope` and the companion key store replace the one-phone versions; a companion manager for the mouse and the headphones; the pointer driver; an audio path that sends the call's downlink to A2DP and takes the uplink from AAP. A2DP needs an encoder (AAC through AudioToolbox, or SBC) and AAP needs an AAC-ELD decoder (AudioToolbox has one).
2. App: voice mode becomes "during calls" instead of "while switched on". `VoiceMode::enable` runs at Answer or Dial and `disable` at hang-up; the IOBluetooth backend serves everything else. The guard that restores Bluetooth stays.
3. Settings: a list of the user's devices to carry through a call, with a one-time key import per device.
4. BLE mice and keyboards.

## Sources

- Reference stack and the two-voice-link result: [lzsnolimit/ai-caller](https://github.com/lzsnolimit/ai-caller), `docs/bluetooth-multiplexing-exploration.md` (MIT).
- AAP microphone: [LibrePods pull request 655](https://github.com/librepods-org/librepods/pull/655) and [superninjv/airpods-helper](https://github.com/superninjv/airpods-helper) (`aap/src/mic.rs`, `aap/src/commands.rs`). Only the byte layouts were taken, as facts about the protocol.
- Magic Mouse report layout: Linux `drivers/hid/hid-magicmouse.c`.
- The `bluetoothd` findings: disassembly of `/usr/sbin/bluetoothd` (arm64e) from macOS 27.2 (26B123), read only.

## A simpler route to test first: macOS keeps every device, we sit between it and the chip (found 2026-11-21, 21:55)

The owner's verdict on the route above after the live runs: no tone was heard in the headphones, a password dialog appeared for key reads, and serving devices ourselves does not feel reliable. The ask: all devices connected like normal.

`bluetoothd` chooses how it reaches the controller at start-up. Read from the binary (macOS 27.2), not yet tried:

- It first reads the environment variable `BT_HCI_TRANSPORT`, split on `:` into transport, driver and parameters. Only if it is unset does it pick by hardware.
- Transport names in its table: `USB`, `H4DS`, `H4BC`, `H4`, `H5`, `APPLEBT`, `VIRT`, `PCIE`, `PCIESW`, `PCIESUN`, `APPLEPCIE`. Driver names: `USB`, `POSIX`, `APPLEBT`, `VIRT`, `PCIE`, `PCIESW`, `PCIESUN`, `APPLEPCIE`.
- The serial (UART) path opens a device given in the parameters, split on `,` into path and speed, defaulting to `/dev/cu.BLTH`.

So `bluetoothd` can be told to talk plain serial HCI (H4) to a port we name. The idea:

1. Our helper opens the real controller through Apple's channels, as stages 1 to 4 do.
2. It offers `bluetoothd` a pseudo-terminal and passes every packet through, both ways. macOS stays the host for the mouse, the headphones and everything else, with its own pairing and its own drivers. Nothing is imported, nothing is injected.
3. The helper keeps one thing for itself: the phone's voice link. It answers the phone's voice request, which `bluetoothd` would decline, carries the voice packets, and hides that link from `bluetoothd`.
4. Call control, texts and contacts stay on the IOBluetooth backend, unchanged.

What is not known:

- Whether the variable can be set for `bluetoothd` on a normal Mac. It is a system launch daemon; `launchctl setenv` as root and a restart of the daemon is the candidate. It would not survive a reboot, which is also the way back if it goes wrong.
- Whether the serial path still works in this build, what the `POSIX` driver sends at start-up (a firmware download would have to be answered by the helper, since Apple's driver has already loaded the firmware), and whether the chip accepts the daemon's vendor commands arriving this way.
- Whether macOS's own headset voice and the phone's voice link can run together. The reference project found two host-sent voice streams crash the firmware under its own configuration.
- It needs an administrator once per boot, or a small privileged helper installed once.

Risk: if `bluetoothd` starts on the serial path and the helper is not answering, the Mac has no Bluetooth until the variable is cleared and the daemon restarted, or the Mac is rebooted. On a Mac whose only pointer is Bluetooth, the first test needs a keyboard-only way back or a timer that reverts by itself.

### Stage 6a live result, 2026-11-21 22:00: passed

One run of `run_stage6.sh 90` on the Mac mini (macOS 27.2).

- `launchctl setenv BT_HCI_TRANSPORT "H4:POSIX:/dev/ttysNNN,115200"` as root, then `killall bluetoothd`, is enough. The new daemon logged `Searching for transport: "H4"`, `UART open(/dev/ttys006) took 0 milliseconds for the port to appear`, `Stack initialization complete`, and the stack reported `PoweredOn`.
- Our program opened the controller on the first try, as a plain process started from a terminal (no app bundle).
- In about 80 seconds it passed 10,578 commands, 199 data packets and 15,280 voice packets to the chip, and 10,990 events, 2,482 data packets and 7,639 voice packets back. 67 distinct commands, none of them vendor commands, and none of the serial-boot commands appeared, so nothing was answered locally.
- The daemon logged `Invalid chipset 0` and `enhancedFilterAcceptListSupported? 0`: on the serial path it does not know which chip it has, so it sends no Apple vendor commands. Features that depend on them are presumably off in this mode; which ones matter is not yet known.
- It also logged `Function Level Reset (FLR) of Bluetooth PCIe transport -- Completed` while booting, so it still resets the PCIe chip itself before using the serial path.
- After the timer: Bluetooth on, transport PCIe, the Magic Mouse, the AirPods and the BLE mouse connected to macOS again, the variable cleared.

Not recorded by the log: how the mouse and headphones behaved for the user during the run. The traffic shows devices were connected and active (voice packets flowed, so some headset link carried audio).

Next, stage 6b: keep the phone's voice link for ourselves. In the pass-through, when a voice request arrives from the phone's address, answer it ourselves, do not show it to the daemon, and carry that link's voice packets to our audio. Needs a phone paired with macOS and the app's IOBluetooth hands-free session running beside it.

### Stage 6a, runs two to four, 2026-11-21 22:06 to 22:11: a race found and fixed

- **22:06, failed.** Our program polled for the controller every 250 ms and never got it: status -12 on all 80 tries. The Mac had no Bluetooth until the five-minute timer fired.
- **22:09, failed.** Started while the Mac was still in that state, so the controller could not open (status -12, 1,200 tries). The new early revert put Bluetooth back within seconds.
- **22:11, passed.** From a normal state: the controller opened on the seventh try at 5 ms spacing, 6,304 commands and 149 data packets went to the chip, 6,593 events and 2,240 data packets came back, and the script put Bluetooth back when its time was up. Mouse and AirPods were connected to macOS afterwards.

What the status codes mean, checked directly:

- **-16 (busy):** macOS's daemon holds the controller. Seen with Bluetooth in its normal state.
- **-12:** the daemon on the serial path has already reset the PCIe chip (`Function Level Reset (FLR) of Bluetooth PCIe transport`) and nothing has brought it up again. It stays that way until the daemon is restarted on its normal path.

So the controller is free only from the old daemon's exit until the new one's reset, a few hundred milliseconds, and our program must take it in that gap. Once it holds the controller, the reset does no harm (both passing runs logged it). The program now starts polling before the daemon is restarted, every 5 ms. Two of two runs from a normal state passed; that is too few to call it reliable.

Rules added to the scripts: the go signal is written before the daemon is killed; when our side ends or fails, Bluetooth is put back at once; the timer remains as a backstop and does nothing if the early revert already ran.

**22:13, passed (third pass, 285 seconds).** Plain pass-through, no phone named. Controller open on the seventh try; 38,631 commands, 220 data and 41,832 voice packets to the chip; 39,673 events, 306 data and 21,300 voice packets back. A headset voice link ran through the pass-through for most of the run. The script put Bluetooth back when the time was up.

### Stage 6b, first live call, 2026-11-21 22:21: the phone's voice link opened beside macOS's devices

`run_stage6.sh 300 "Z Fold5"` with the Android phone paired to macOS in the ordinary way, and the app (IOBluetooth backend) running. The owner reports the phone connected and the incoming-call banner showed.

- The phone asked for its voice link once. Our program accepted it, the link opened, and macOS's daemon was not shown any of it: `"phone_voice": {"requests": 1, "links_opened": 1, "packets_in": 11, "packets_out": 11}`.
- Eleven voice packets came in and eleven went back, then none. No close event for that link was seen.
- macOS's own traffic kept flowing in the same run: 9,372 voice packets to the chip and 4,691 back on a headset voice link, 5,904 commands, 2,097 data packets in.

So the route does what it was built for up to the first packets: macOS keeps its devices and we take the phone's voice. What is not known is why the phone's voice stopped after eleven packets. Two candidates: the run's time ran out just then, or the phone's link starved once a headset voice link was also sending. The reference project saw one of two voice links stop after tens of packets on this chip. The first log had no times, so it cannot say which. The program now records when each step happened, per-link voice counts, hardware-error events and disconnect reasons.

### Stage 6b, second live call, 2026-11-21 22:28: a whole call's voice link, with macOS serving its devices beside it

Same command, with timestamps in the log. No headset voice link was open on macOS's side during this run (`"host_voice_links": {}`).

- The phone asked for its voice link; our program accepted it; the link was open 0.05 s later and the first voice packet arrived 0.03 s after that.
- The link carried voice for the whole call: 8,585 packets in and 8,585 back over 32.28 s, with packets in every one of 33 seconds. That is the full rate for this link type.
- It closed with reason 0x13, the phone ending the call. No hardware-error event, no other disconnects.
- macOS's own traffic ran through the same program throughout the 214 s: 39,920 commands and 801 data packets to the chip, 41,392 events and 5,400 data packets back. The data packets are the mouse and other devices macOS was serving.

So on the pass-through route a real call's voice link runs for the length of the call while macOS keeps its Bluetooth devices, with no injection, no key import and nothing re-paired. The voice was silence both ways in this probe: carrying real sound is the next piece.

Still open:

- **The first call's stall (11 packets).** That run had a headset voice link open on macOS's side at the same time; this one did not. A run with the headphones in use during the call decides whether two voice links can run together under Apple's configuration.
- **How it looked to the user**: whether the pointer and headphones behaved normally during the call is not in the log.

### Stage 6, runs at 22:34 and 22:41: no call, and a finding about headphones on this route

- **22:34.** No voice request from the phone. The app had been opened before the daemon was restarted and showed the phone as not connected. Order matters: start the pass-through, then open the app.
- **22:41, with `sound`.** Again no voice request from the phone (`"phone_voice": {}`); whether a call was placed is not known. Other headphones were in use (the AirPods' battery had run out).

The finding: in every pass-through run with headphones connected, macOS opened a voice link to them at once and kept it for the whole run. At 22:41 it opened 0.24 s into the run and carried 34,718 packets in and 69,380 out over 260 s (`host_voice_links`). The 22:00 and 22:13 runs show the same volume of voice packets; the 22:28 run, with no headphones, shows none.

Why macOS does this on the serial path is not known. Candidates: something is listening on the headset microphone (the Mac mini has no built-in one), or with the chip unidentified (`Invalid chipset 0`) the daemon cannot use the chip features it normally relies on for headphones and falls back to a plain voice link.

Why it matters:

- Headphones held in voice mode play sound at call quality, so music would sound worse for as long as the pass-through is on.
- It makes the phone's voice link the second one on the chip. The one call made in that situation (22:21) stopped after eleven packets.

So the route is proven for the phone's voice with macOS serving the mouse, and not yet for headphones. Next to find out: what holds the headset voice link open, and whether giving the daemon the chip's identity on this path changes it.

### What holds the headphones in voice mode (read from the daemon's log of the 22:41 run)

- On the serial path the daemon reports the chip as `THIRD_PARTY_DONGLE`. A2DP still works: it selected AAC with the headphones. For calls it logs `WBS / mSBC for HFP disabled`, `UWBS / AAC-ELD for HFP disabled`, `localAoS 0` and picks `CSVD`, 8 kHz. So headset call audio on this path is plain narrowband voice over a voice link, without Apple's better codecs or its audio-over-data path.
- The headphones connected at 22:41:48. At 22:41:50.59 the daemon logged `Sco data source selected for HCI` and `RegisterScoChannel`, at the moment CoreAudio started input on the headset. Nothing in our program had opened a microphone.

So the voice link is opened because something on the Mac starts recording from the headset's microphone as soon as it connects: macOS makes a newly connected headset the default input, and some process that listens on the default input follows it. Which process is not identified (a Sound Recognition daemon and a browser both had Bluetooth sessions open). This is ordinary macOS behaviour, not something the pass-through causes; what the pass-through changes is that the fallback is a narrowband voice link.

Consequence for the design: while a call runs, the Mac's input must not be the Bluetooth headset, or there are two voice links on the chip. With another microphone present (here a webcam), keeping the input on it is enough. On a Mac whose only microphone is the headset, two voice links are unavoidable, and whether they can run together is still the open question.

### Stage 6c, 22:50, with `sound` and the automatic input move

- The program moved the Mac's sound input off the Bluetooth headset once. macOS's headset voice link, opened at the start, was closed by macOS itself (reason 0x16) and carried no packets afterwards; 11,466 data packets went to the chip, which is what headphones playing over A2DP look like. So the input move does keep the headphones out of voice mode.
- The phone sent no voice request in this run either (`"phone_voice": {}`), so no call sound was carried. Whether a call was answered on the Mac in this run is the owner's to say. If one was, the phone is now keeping the call's sound although the chip's voice link is free, which was not the case at 22:21 and 22:28 and needs its own explanation (the phone's choice of active audio device, or the state of the app's hands-free session).

### Stage 6c, 23:29 and 23:37: the phone was never linked, and the app said it was

With every classic link now logged, both runs show two links (mouse and headphones) and none from the phone, by classic Bluetooth or Low Energy, and no failed attempts either. The owner had answered the call on the Mac and the sound stayed on the phone. So in these runs the phone had no link through the pass-through when it mattered: after the daemon restart, mice and headphones reconnect by themselves, a phone does not, and nothing started its link. The app's "connected" label is not evidence of a link.

Changes: the pass-through now asks macOS to open the phone's link itself (`connect.m`, a small app bundle, because macOS aborts a bare process that touches Bluetooth; checked: exit 134 as a script, fine as a bundle), retries a few times, and speaks its state aloud: pass-through up, phone connected, call audio on the Mac, call ended, test finished.

### Stage 6c, 23:47: two real calls with sound, system Bluetooth fully on

`run_stage6.sh 300 "Z Fold5" sound`, with the automatic phone connect and spoken cues. The owner's report: "worked now".

- The pass-through connected the phone by itself on the first try (`phone-linked`, `after_tries: 1`).
- The app showed no call banner at first: it had been opened before the daemon restart and held a dead session. Quitting and reopening it fixed that. The app must reconnect its hands-free session when the daemon restarts.
- **Call 1:** 6,825 voice packets each way (about 26 s). Caller's voice peak level 3,490, microphone peak 1,795, no speaker underruns, no stream errors.
- **Call 2:** 8,098 packets each way (about 30 s), peaks 2,262 and 1,574, no underruns, no errors.
- Sound went out on the Mac's current output, which at that moment was "Mac mini Speakers" (so half-duplex was used), and came in from the webcam microphone. The program moved the sound input off the Bluetooth headset each time macOS switched to it.
- macOS served its own Bluetooth devices through the same program for the whole run.

So on this route a call is answered on the Mac and its sound runs through the Mac, both ways, with macOS keeping the mouse and the headphones as its own devices. Not yet shown: the caller's voice in Bluetooth headphones (the output was the built-in speakers in these two calls; with headphones selected as output macOS plays to them over A2DP, which the 22:50 run showed stays up), and a headset microphone as the call's microphone.

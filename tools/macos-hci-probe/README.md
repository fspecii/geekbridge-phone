# macOS Bluetooth head-unit probes

Experiments for call voice on macOS through the Mac's built-in Bluetooth controller. Background, results and plan are in `docs/MACOS-HEADUNIT-ROUTE.md` and `docs/MACOS-STAGE2.md`.

Credit: the native bridge (`third_party/ai-caller/`) is the unmodified work of the ai-caller contributors, MIT licence. The design of the Python side follows their project; the code here is ours.

| Path | What it is |
| --- | --- |
| `stage1.c` | Stage 1: opens the three controller channels, reads three HCI values, restores Bluetooth. Passed on 2026-11-09. |
| `launcher.c` | Stage 2 guard: switches system Bluetooth off, runs the Python program, always switches it back on. |
| `gbp_acipc.py` | Bumble transport over the bridge, with a filter that confines the stack to one phone, and a one-phone key store. |
| `stage2_pair.py` | Pairs one phone, reads its services, opens its hands-free channel. No calls, no audio. |
| `calls.py`, `sco.py` | Call state from the hands-free indicators, and one-packet-back voice handling. Pure logic, fully tested offline. |
| `stage3a_call.py` | Stage 3a: answers one real call from the Mac, accepts the voice link, counts packets for a few seconds while sending silence, hangs up. No sound device. Needs the stage 2 pairing. |
| `audio_io.py` | Sound for stage 3b: bounded buffers, resampling between 8 kHz voice and the device rate, level meters, opening the microphone and speakers. Tested with a stand-in for the sound library. |
| `stage3b_voice.py` | Stage 3b: a real call with sound. Same flow as 3a, with the caller in the speakers and the microphone in the call. |
| `gbp_companions.py` | Stage 5: the filter and key store for one phone plus the user's own devices, the transport that refuses strangers, and the Magic Mouse decoder. See `docs/MACOS-COEXIST.md`. |
| `keys_import.py` | Copies one device's pairing key from macOS (System keychain, administrator approval) into our private key file. Never prints the key. Touches no Bluetooth. |
| `pointer.py` | Turns mouse reports into pointer events (needs Accessibility permission). |
| `stage5a_mouse.py` | Stage 5a: serves the user's Bluetooth mouse from our stack with the imported key. Not yet run live. |
| `gbp_aap.py` | The AirPods microphone stream over Apple's accessory channel: packet bytes and parsing. |
| `stage5b_headphones.py` | Stage 5b: plays a tone on the user's headphones over A2DP and asks AirPods for their microphone. Not yet run live. |
| `stage5c_all.py` | Stage 5c: one real call with the mouse and the headphones served beside it. Not yet run live. |
| `run_stage5.sh` | Runs a stage 5 step on a device named as System Settings shows it, and saves the output under `logs/`. |
| `tests/` | Offline tests: the filters, the key stores, the decoders, and stages 5a and 5b against simulated devices. |
| `build.sh` | Builds everything and runs the tests. Touches no Bluetooth. |

## Build and test (safe)

```sh
./build.sh
```

## Run stage 2 (switches Mac Bluetooth off for up to 90 seconds)

Only after the steps in `docs/MACOS-STAGE2.md` ("What you will do"). One attempt per run.

```sh
D="$PWD"; open -W --stdout /tmp/stage2.out --stderr /tmp/stage2.out build/GBBtLauncher.app \
  --args "$D/.venv/bin/python" "$D/stage2_pair.py" 6C:AC:C2:00:00:01 90; cat /tmp/stage2.out
```

The launcher must run inside its app bundle: macOS aborts a bare process that reads or sets the Bluetooth power state.

## Run stage 3a (switches Mac Bluetooth off for up to 150 seconds)

Needs the stage 2 pairing, "Phone calls" switched on for the Mac on the phone, and a second phone to ring the Fold5. A Ping sound on the Mac means it is ready: ring the Fold5 within 60 seconds and do not answer on the phone. The Mac answers, holds the call for 10 seconds and hangs up.

```sh
D="$PWD"; open -W --stdout /tmp/stage3a.out --stderr /tmp/stage3a.out build/GBBtLauncher.app \
  --args "$D/.venv/bin/python" "$D/stage3a_call.py" 6C:AC:C2:00:00:01 150; cat /tmp/stage3a.out
```

## Run stage 3b (switches Mac Bluetooth off for up to 200 seconds)

Everything from stage 3a, plus wired headphones (Bluetooth is off during the run, and laptop speakers would feed the call back into the microphone). macOS asks once for microphone access when the sound devices open, before the Ping. After the Ping, ring the Fold5 within 60 seconds and do not answer on the phone. The Mac answers and keeps the call for 30 seconds: talk, listen, then it hangs up.

```sh
D="$PWD"; open -W --stdout /tmp/stage3b.out --stderr /tmp/stage3b.out build/GBBtLauncher.app \
  --args "$D/.venv/bin/python" "$D/stage3b_voice.py" 6C:AC:C2:00:00:01 200; cat /tmp/stage3b.out
```

## Run stage 5 (each probe switches Mac Bluetooth off for a bounded time)

Read `docs/MACOS-COEXIST.md` first. `run_stage5.sh` takes a device by part of its name, as System Settings shows it, or by address. Nothing here has run live yet.

Every run's output is saved under `logs/` (ignored by git), with device addresses masked. Run the commands from this folder.

Once per device, copy its pairing key from macOS (macOS asks for an administrator; the key is never shown):

```sh
./run_stage5.sh import "Magic Mouse"
```

Stage 5a, the mouse (up to 90 seconds). While it runs the mouse is not macOS's. With `--inject` our stack moves the pointer instead, once `GBBtLauncher` is allowed under System Settings > Privacy & Security > Accessibility. After the spoken cue, move and click the mouse.

```sh
./run_stage5.sh 5a "Magic Mouse" --inject
```

If the result is an authentication failure, check that the mouse works with macOS again, run the import once more with `--reversed`, and repeat once.

Stage 5b, the headphones (up to 60 seconds). Wear them. A 440 Hz tone plays for a few seconds; for AirPods the result line also says whether the microphone stream arrived.

```sh
./run_stage5.sh import "AirPods"
```

```sh
./run_stage5.sh 5b "AirPods"
```

Stage 5c, a real call with the mouse and headphones beside it (up to 200 seconds). Needs an Android phone paired with this Mac in System Settings and its key imported the same way. After the cue, ring the phone from another phone, do not answer on the phone, and move the mouse during the call.

```sh
./run_stage5.sh 5c "Fold5" "Magic Mouse" "AirPods"
```

## Run stage 6 (calls on the Mac while macOS keeps every Bluetooth device)

Read the "simpler route" sections of `docs/MACOS-COEXIST.md` first. Verified live on one Mac mini (macOS 27.2): the pass-through, a phone's voice link kept for a whole call, and two calls with sound. The phone is paired with macOS in the ordinary way, in System Settings.

```sh
./run_stage6.sh 90                        # 6a: plain pass-through
./run_stage6.sh 300 "Z Fold5"             # 6b: also keep that phone's voice link (silence, counted)
./run_stage6.sh 300 "Z Fold5" sound       # 6c: the call's sound on the Mac's current output and input
```

What it does: restarts macOS's Bluetooth daemon so it reaches the controller through our program, connects the phone, and speaks its state aloud ("Phone connected. You can call now.", "Call audio is on the Mac."). Open the GeekBridge app after the spoken "pass-through is up": an app opened earlier holds a dead session. Answer calls in the app. Everything is put back when the run ends, by a timer if it does not end properly, and by a reboot in any case.

Without the helper it asks for an administrator password once per run. To install the one root piece, once:

```sh
sudo ./install-helper.sh
```

Read `helper/btmode` first. It lets programs running as your user route this Mac's Bluetooth through a program of their own, which is what the pass-through does and also what malware running as you could do. `sudo ./install-helper.sh remove` takes it out.

Limits known so far: the call's microphone must not be a Bluetooth headset (the program moves the Mac's sound input to another microphone while it runs); on this path macOS treats the chip as a generic one and sends none of Apple's chip commands.

| Path | What it is |
| --- | --- |
| `gbp_proxy.py` | Serial (H4) framing, and the policy that passes packets, keeps the phone's voice link, and reads the hands-free signalling with numbers and names masked. |
| `stage6a_passthrough.py` | Holds the controller, passes packets, connects the phone, carries the call's sound, speaks its state. |
| `sound_input.py` | Keeps the Mac's sound input off Bluetooth headsets while the pass-through runs. |
| `connect.m` | The small app that asks macOS to open the phone's link. |
| `helper/btmode`, `install-helper.sh` | The root piece and its installer. |
| `stage6_root.sh` | The same root step for use without the helper. |
| `run_stage6.sh` | Runs it all and saves the log, with what the daemon logged. |

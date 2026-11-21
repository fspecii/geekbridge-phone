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
| `tests/` | Offline tests for the filter and the key store. |
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

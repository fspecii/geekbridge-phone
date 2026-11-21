# macOS Bluetooth helper (`gbp_btd`)

The process behind GeekBridge Phone's "Mac voice mode". It runs a Bluetooth stack (Google's Bumble) on the Mac's built-in controller through Apple's internal channels, so calls can carry voice through the Mac's speakers and microphone. See `docs/MACOS-STAGE4.md` for the design and `docs/MACOS-HEADUNIT-ROUTE.md` for how the route was found.

**It must only run while system Bluetooth is off.** The app switches it off, starts the helper, and switches it on again afterwards. For tests, use the launcher in `tools/macos-hci-probe`, which does exactly that and always restores Bluetooth.

## What it speaks

A Unix socket `btd.sock` for requests and events (one JSON object per line; see `gbp_btd/protocol.py`), file descriptors for the contacts and messages channels, and a second socket `voice.sock` with 8 kHz 16-bit voice frames. Both sockets live in a private folder (mode 700).

| Module | Role |
| --- | --- |
| `protocol.py` | Message format and request checking |
| `server.py` | Socket server, descriptor passing |
| `handler.py` | Requests to actions; stops the helper if the app stays away |
| `phone_manager.py` | One phone: connect, reconnect, calls, voice |
| `services.py` | MAP and PBAP channels handed to the app as descriptors |
| `pairing.py` | Scan and pair |
| `voice.py` | Voice frames and the uplink buffer |
| `stack.py` | The interface to the Bluetooth stack |
| `bumble_stack.py` | The real stack, on Bumble |
| `acipc.py` | The native transport, the safety filter, the key store |
| `calls.py`, `sco.py` | Call state, voice packet handling |
| `app.py`, `__main__.py` | Assembly and entry point |
| `btd.py` | The script the app starts: `python -I btd.py --dir DIR` (`--stack simulated` runs a pretend phone, no Bluetooth) |
| `simulated.py` | The pretend phone, for developing the app side without Bluetooth |

## Safety

The transport filter confines the stack to the one selected phone: no vendor commands, no stored-key commands, no discovery except during a scan the user started, connection and pairing commands only for the target, voice data only on the single voice link. Pairing keys live in one private file (mode 600) and are never logged.

## Build and test (no Bluetooth)

```sh
./build.sh
```

## Live session test (switches Mac Bluetooth off for up to 200 seconds)

Needs the stage 2 pairing and a second phone to ring the Fold5. The Mac says "Ready. Call the phone now." Do not answer on the phone.

```sh
cd ../../tools/macos-hci-probe && ./build.sh   # builds the launcher bundle
H="$(cd ../../helper/macos-btd && pwd)"
open -W --stdout /tmp/btd-session.out --stderr /tmp/btd-session.out build/GBBtLauncher.app \
  --args "$H/.venv/bin/python" "$H/session_test.py" 6C:AC:C2:00:00:01 200; cat /tmp/btd-session.out
```

Credit: `third_party/ai-caller/` holds the native bridge of the ai-caller project, unmodified, MIT licence. Bumble is Apache-2.0.

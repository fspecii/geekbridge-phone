# macOS call voice: driver feasibility spike

Written 2026-11-09 on a MacBook Air M3 (Mac15,12), macOS 27. Everything here was read from the running system. Nothing was installed, loaded or changed.

## Why a driver

Call voice travels over a Bluetooth SCO link. On Windows and Linux the app gets that link from the operating system. On macOS it cannot:

- `IOBluetoothHandsFreeDevice.connectSCO` fails with `0xE00002C7` (unsupported). Other projects report the same on Apple Silicon.
- Disassembling IOBluetooth from the dyld cache shows `IOBluetoothAddSCOAudioDevice`, `IOBluetoothRemoveSCOAudioDevice` and the legacy `BluetoothHCI*SynchronousConnection` calls are stubs that only return 0. The neighbouring PAN calls are real code.
- Apple's `BTAudioHALPlugin` only has roles where the Mac is the audio source for a headset. It has no role for the Mac acting as a hands-free unit.
- Letting the phone start the link does not help: macOS logs no SCO event and creates no audio device.
- Microphone permission and signing entitlements make no difference.

So no public or exported API reaches call voice. The remaining way is below the API, at the chip.

## What the hardware looks like

| Item | Finding |
| --- | --- |
| Chip | Broadcom BCM4388 (system_profiler reports `BCM_4388`), firmware 24.1.584.4716 |
| Attachment | PCIe, node `bluetooth-pcie@0,1`, ID `pci14e4,5f72` |
| Memory windows | BAR0 32 KiB (registers), BAR2 16 MiB (shared memory) |
| Apple driver | `AppleConvergedPCI`, probe score 1000, matches `0x5fa0 0x5f69 0x5f31 0x5f71 0x5f72 0x5f83` (vendor 14e4) |
| Layers above it | `AppleConvergedIPCOLYBTControl`, `AppleBluetoothModule`, `AppleBTM`, then `IOBluetoothFamily` and `IOBluetoothHCIController` |
| Security | SIP and authenticated root are on. No third-party kexts are loaded. |
| Firmware on disk | Main images in `/usr/share/firmware/bluetooth` (about 815 KB each, one per board variant), calibration files in `/usr/share/firmware/wpan` and `/usr/share/firmware/bluetooth`. See gate 2. |

The Linux kernel driver `hci_bcm4377` supports the same PCI IDs, including `0x5f72`, and has dedicated host SCO rings. That is strong evidence the chip hands SCO data to the host. Apple's driver matches the same ID family, so it is the macOS counterpart of that driver. Whether this M3's chip uses exactly the protocol the Linux driver implements is not proven.

## What a custom driver has to do

1. Take over the PCI function from `AppleConvergedPCI`, with a higher probe score and the same match string.
2. Load the chip's firmware from the Mac's own system volume at run time (see gate 2). It is proprietary, so it must never be copied into the project or shipped with it.
3. Set up the shared-memory rings and speak HCI over them, as `hci_bcm4377` does.
4. Hand everything except SCO to Apple's stack, so keyboards, AirPods and the rest keep working, and take the call voice packets for the app. Intel Bluetooth kexts for macOS show that a third-party transport can sit under `IOBluetoothFamily`, but those drive USB chips, not this one.
5. Answer the phone's SCO connection request and move audio in both directions between the SCO rings and the Mac's speakers and microphone.

## What it costs and risks

- **Loading it.** A kernel extension on Apple Silicon needs reduced boot security, set in Recovery, plus user approval. The alternative is Apple's PCI DriverKit permission, which Apple grants rarely. Ordinary users cannot do either, so this cannot ship as a normal app.
- **Crashes.** A bug in a driver for the only Bluetooth chip can panic the Mac or leave Bluetooth dead until the driver is removed. It needs a spare Mac, and ideally a second one for kernel debugging.
- **Size.** Months of work, with no guarantee the M3 chip matches the Linux protocol.
- **Firmware.** Present on the system volume, but proprietary. The driver reads it at run time and the project never carries a copy.

## Gate 1 result (read-only protocol check): passed

Apple's own driver description for this chip (`AppleConvergedIPCOLYBTControl`, read from the IORegistry) uses the same vocabulary and the same ring table as the Linux driver `hci_bcm4377`:

| Linux driver | Apple, this M3 |
| --- | --- |
| BOOTSTAGE, RTI_CONTROL, RTI_STATUS, RTI windows | `bootstage`, `RTIDevice`, `RTIDevice_V2`, `RTIPipe` |
| HCI_H2D ring 1, doorbell 1 | pipe 1: hci, direction 1, doorbell index 1 |
| HCI_D2H ring 2, doorbell 2, virtual | pipe 2: hci, direction 2, doorbell index 2, virtual |
| SCO_H2D ring 3, doorbell 6, sync, payload in footer | pipe 3: sco, direction 1, doorbell index 6, sync, footer size 255 |
| SCO_D2H ring 4, doorbell 6, virtual, sync | pipe 4: sco, direction 2, doorbell index 6, virtual, sync |
| ACL_H2D ring 5, doorbell 3 | pipe 5: acl, direction 1, doorbell index 3 |
| ACL_D2H ring 6, doorbell 4 | pipe 6: acl, direction 2, doorbell index 4 |
| 128 and 256 entry rings, 0x10-byte entries | completion rings of 128 and 256 entries |
| OTP read, MSI interrupts | `read_otp` (macOS: yes), MSI range 0 to 9 |

So the M3 chip speaks the same ring protocol as the Linux driver supports, and the SCO rings exist and are enabled (bootstage 2 on every pipe, control stage 3, running).

Two further findings:

- Apple exposes each pipe, SCO included, as a Skywalk channel (`ACIPCInterfaceTransport = skywalk`, `skywalkBufferSize = 1020` for SCO). Opening such a channel from a user process needs private entitlements, which a normal app cannot get.
- The refusal to carry call voice for a hands-free unit is therefore a software policy above the hardware, not a missing hardware path.

Unverified: the exact on-wire entry layouts and the firmware boot steps on this M3, which Linux documents only for the earlier chips.

## Gate 2 result (find the firmware): passed

The earlier note that the main firmware was missing was wrong: the search had looked only in `/usr/share/firmware/wpan`.

- The running firmware is `24.1.584.4716` (system_profiler).
- The matching image is `/usr/share/firmware/bluetooth/BCM4388C0_24.1.584.4716_PCIE_macOS_Willamette_Dnieper_CLPC_OS_USI_20260506.bin`, 815,136 bytes, SHA-256 `713987384ae13df9489f5d5b428369f03d579a08833fd46ac5f4ff8c1e5a547d`.
- Its embedded build string reads `BCM4388C0 Olympic PCIe macOS_Dnieper_CLPC_OS USI [Version: 24.1.0584.4716]`, an exact match for the running chip.
- Calibration files for the same board (`Dnieper`, USI module) are present: `BCM4388C0_Dnieper_154_FINALv1_OS_USI_20230921.ptb` and the `.txcb` in `/usr/share/firmware/wpan`, and `BCM4388C0_Final_PCIE_macOS_Willamette_Dnieper_CLPC_OS_USI_K_R_20231009.ptb` in `/usr/share/firmware/bluetooth`.
- The image starts with the bytes `5a b8 de 3e`, followed by a table of section offsets and sizes. The format is not documented here and is not yet decoded. Linux's loader hands the whole file to the chip unchanged, which suggests the chip parses it itself.
- The board variant is chosen by the module on the Mac (here `Dnieper`, supplier `USI`). A driver has to pick the right one the way Apple's driver does, from the chip's OTP data (Linux reads the vendor and stepping tags, then builds the file name).

Legal and practical rule: the firmware belongs to Broadcom and Apple. The driver reads it from the user's own system at run time. It is never copied into the repository or shipped.

## Decision gates

1. Read-only protocol check: confirm from the shared-memory layout and Apple's driver strings that the M3 chip uses the `hci_bcm4377` ring protocol. Cheap, safe.
2. Obtain the firmware image from install media and confirm it matches the chip's revision.
3. A do-nothing kext that only matches the PCI function and logs, tested on a spare Mac in reduced security. Proves out-matching Apple's driver works at all.
4. HCI over rings up to a working inquiry. Proves the protocol port.
5. SCO rings and a loopback audio test.

Stop at any gate where the answer is no.

## Other routes found in the research

- A second USB Bluetooth adapter driven from user space (for example Google Bumble) with the Mac's own radio left alone. Confirmed by other projects, needs hardware, rejected for this product.
- Android developer tools (scrcpy) can capture some audio sources over USB debugging. Not checked for call audio, and it needs developer options on the phone.

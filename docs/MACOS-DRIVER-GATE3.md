# Gate 3 plan: take over the Bluetooth chip with a do-nothing driver

Companion to [MACOS-DRIVER-SPIKE.md](MACOS-DRIVER-SPIKE.md). Gates 1 and 2 passed: the chip's ring protocol matches the Linux driver and the firmware is on the system volume. This gate asks the first question that needs a driver: can a third-party driver attach to the Bluetooth chip in place of Apple's, and read its registers?

Nothing in this document has been run. Items marked **verify** come from forum posts or single reports and need checking against Apple's documentation before anyone relies on them.

## What gate 3 proves

1. A driver of ours can be loaded on this class of Mac at all.
2. It can win the match for PCI device `14e4:5f72` over Apple's `AppleConvergedPCI` (probe score 1000, match string `0x5fa014e4 0x5f6914e4 0x5f3114e4 0x5f7114e4 0x5f7214e4 0x5f8314e4`).
3. Once attached, it can map BAR0 (32 KiB) and BAR2 (16 MiB), read PCI configuration space, and read the `BOOTSTAGE` and `RTI_STATUS` registers.

It does not load firmware, send HCI, or touch audio. Those are gates 4 and 5.

Pass: the log shows our driver attached, the register values read, and Apple's driver absent. Fail: any of the three does not happen, or the Mac needs more than a reboot and a removal to recover.

## Expected side effect

When our driver wins the match, Apple's Bluetooth stack has no controller. Bluetooth is off on that Mac until our driver is removed and the Mac is restarted. Wi-Fi is a separate PCIe function (`14e4:4434`) and is not affected. The built-in keyboard and trackpad on a MacBook are not Bluetooth.

## The design question gate 3 cannot answer

A DriverKit extension runs in user space. It cannot subclass the kernel classes that `IOBluetoothFamily` expects from a transport, so it cannot sit under Apple's stack and pass most traffic through. Apple's own `AppleBluetoothModule` and `IOBluetoothHCIController` are kernel drivers. That leaves two shapes for the finished product:

| | A. Kernel extension under Apple's stack | B. DriverKit extension plus our own Bluetooth stack |
| --- | --- | --- |
| System Bluetooth (AirPods, keyboards) | Keeps working | Off while our stack owns the chip |
| Call voice | We take the SCO packets ourselves | Our stack runs the whole link, including voice |
| Safety | A bug can panic the Mac | A bug cannot panic the Mac (user space) |
| Install | Reduced security plus user approval; deprecated for PCI devices | Apple's PCI entitlement, or developer mode with SIP off |
| Headers | Not public; would be reverse-engineered, as the Intel Bluetooth kexts did | Public (PCIDriverKit) |
| Work | Very large | Large: a Bluetooth host stack with hands-free and SCO |

Shape B is the more realistic one, with a real cost for users: while the voice mode is on, the Mac's other Bluetooth devices do not work. A reboot or an unload brings them back. Decide between A and B after gate 3, with its results in hand.

## What gets built

A small DriverKit PCI extension and a host app that installs it.

- **Personality** (in the extension's `Info.plist`): `IOProviderClass` `IOPCIDevice`, `IOPCIMatch` `0x5f7214e4`, an `IOProbeScore` above 1000 (start with 2000), `IOUserClass` for our class, `IOUserServerName` for the extension. **Verify** that probe score alone is enough: forum reports say PCI matching can tie, and that a built-in driver attached at boot may win over a DriverKit extension.
- **Entitlements:** `com.apple.developer.driverkit`, `com.apple.developer.driverkit.transport.pci` with the match dictionary above. Apple grants the PCI entitlement on request. In developer mode on a test Mac it may not be needed. **Verify.**
- **Driver behaviour:** in `Start`, open the device, log vendor, device and subsystem IDs from config space, map BAR0 and BAR2, and read these registers (offsets from the Linux driver, BAR2 offset `0x200000` for the BCM4388): BAR2 `0x200454` (`BOOTSTAGE`), `0x20045c` (`RTI_STATUS`). No writes anywhere. Log through `os_log` under a subsystem of our own.
- **Host app:** submits an `OSSystemExtensionRequest` to install and remove the extension, and nothing else.

## Test system without a spare Mac

On Apple Silicon the boot security level and SIP are set per macOS installation, not per Mac. A second macOS installed on an external drive can run with SIP off and reduced security while the main macOS on the internal disk stays at full security and is never changed. Booting the main macOS again brings back Apple's driver, because nothing on the internal disk was touched. The chip's firmware lives in RAM and is loaded again on every boot, so the test leaves no state behind in the chip.

Requirements, checked on this Mac on 2026-11-09:

- An external SSD of at least 64 GB (128 GB is more comfortable) on USB-C or Thunderbolt. A hard disk works but is slow. **None was connected.**
- The internal disk has only 7 GiB free of 228 GiB, so a second macOS cannot go on an internal volume.
- This Mac runs macOS 27.0 (build 26A5425a, a pre-release build). Installing a matching second system needs an installer for that same build. **Verify** that one is available before buying or formatting anything.
- A Developer account for signing, unless developer mode proves enough. **Verify.**

Keep the rest of this section as the fallback if a spare Mac turns up.

### Spare Mac, if one exists

Use a spare Apple Silicon Mac, not this one. The list of paired devices on this Mac included a Mac mini, which would suit if it is Apple Silicon: send its model and chip (`system_profiler SPBluetoothDataType` shows `Chipset` and the PCI ID in `ioreg`). If its chip is not the BCM4388, the match string changes and the test shows the approach but not this exact chip. A Mac with the same chip is better.

Preconditions on the test Mac:

- Backed up, with nothing on it that matters.
- An Apple Developer account for signing, and a way to disable SIP (`csrutil disable` in Recovery).
- Time to put it back afterwards.

## Steps on the test Mac

1. Record a baseline before changing anything: `system_profiler SPBluetoothDataType`, `ioreg -r -n bluetooth-pcie@0,1 -l -w0`, `csrutil status`, and the macOS build.
2. In Recovery: disable SIP, and lower the boot security setting if the dev flow requires it. **Verify** the exact settings against Apple's DriverKit testing notes; one report says Reduced Security and SIP are separate switches.
3. Back in macOS: `sudo systemextensionsctl developer on`, then run the host app to install the extension. Approve it in System Settings > Privacy & Security if asked.
4. Restart, because a built-in driver attached at boot can beat the extension on the first match.
5. Read the log: `log show --last 5m --predicate 'subsystem == "<ours>"'`. Check `ioreg -r -n bluetooth-pcie@0,1 -l -w0` for which driver is attached.
6. Record the results against the pass and fail rules above.

## Recovery

1. Run the host app's remove command, or `systemextensionsctl uninstall <team id> <bundle id>`.
2. In Recovery: `csrutil enable`, and set boot security back to Full Security in Startup Security Utility.
3. Restart. Apple's driver should attach again and Bluetooth should return. Compare `ioreg` and `system_profiler` with the baseline.
4. If Bluetooth does not return, reinstall macOS over the top from Recovery. That keeps user data. It is the worst case and not expected.

## Abort rules

- Stop and restore if the extension does not load after the steps above, or if loading it needs a change the plan did not name.
- Stop if the test Mac panics on boot with the extension installed. A DriverKit extension should not cause that, so a panic means something else is wrong.
- Do not run any of this on the Mac that holds your work.

## After gate 3

If it passes, write the architecture decision (shape A or B above), then plan gate 4: firmware download and HCI over the rings, using the sequence documented for the Linux driver and the Apple ring table recorded in the spike report.

If it fails because Apple's driver keeps winning the match, the next test is whether the extension can claim the device when Apple's driver is prevented from matching, or the route is closed.

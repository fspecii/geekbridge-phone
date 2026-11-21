// SPDX-License-Identifier: GPL-3.0-or-later
// Asks macOS to open its Bluetooth link to one paired device: connect <AA-BB-CC-DD-EE-FF>
//
// After the daemon restarts on the pass-through, mice and headphones come back by themselves
// but a phone does not, and nothing else starts its link. This is the same as clicking the
// device in the Bluetooth menu. It lives in its own small app bundle because macOS ends a bare
// process that touches Bluetooth, and that must not take the pass-through down with it.
#import <Foundation/Foundation.h>
#import <IOBluetooth/IOBluetooth.h>

int main(int argc, char **argv) {
    @autoreleasepool {
        if (argc < 2) { printf("usage\n"); return 64; }
        IOBluetoothDevice *device = [IOBluetoothDevice deviceWithAddressString:@(argv[1])];
        if (!device) { printf("no-such-device\n"); return 3; }
        if ([device isConnected]) { printf("already-connected\n"); return 0; }
        IOReturn result = [device openConnection];
        printf("open-connection-result %#x\n", result);
        return result == kIOReturnSuccess ? 0 : 4;
    }
}

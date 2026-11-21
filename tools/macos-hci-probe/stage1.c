// SPDX-License-Identifier: GPL-3.0-or-later
// Stage 1 of docs/MACOS-HEADUNIT-ROUTE.md: can a normal process open Apple's HCI/ACL/SCO
// channels on this Mac, and does the controller answer? No pairing, no phone, no audio.
//
// Sequence: remember that Bluetooth is on, switch it off, open the three channels, send three
// read-only HCI commands, close, switch Bluetooth on again and check. Bluetooth is restored on
// every exit path (normal end, error, signal, timeout).
#include "third_party/ai-caller/acipc_bridge.h"
#include <dlfcn.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

// A bare binary needs its Bluetooth usage text inside it, or macOS aborts it on first use.
__attribute__((used, section("__TEXT,__info_plist")))
static const char info_plist[] =
    "<?xml version=\"1.0\" encoding=\"UTF-8\"?><!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
    "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\"><plist version=\"1.0\"><dict>"
    "<key>CFBundleIdentifier</key><string>dev.geekbridge.hciprobe</string>"
    "<key>NSBluetoothAlwaysUsageDescription</key><string>Stage 1 test: talks to the Mac's Bluetooth controller.</string>"
    "</dict></plist>";

static int (*get_power)(void);
static void (*set_power)(int);
static int initial_on;
static int restored;

static void say(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void say(const char *fmt, ...) {
    time_t t = time(NULL); struct tm m; localtime_r(&t, &m);
    printf("[%02d:%02d:%02d] ", m.tm_hour, m.tm_min, m.tm_sec);
    va_list ap; __builtin_va_start(ap, fmt); vprintf(fmt, ap); __builtin_va_end(ap);
    printf("\n"); fflush(stdout);
}

static int wait_power(int want, int seconds) {
    for (int i = 0; i < seconds * 4; ++i) {
        if (get_power() == want) return 1;
        usleep(250000);
    }
    return get_power() == want;
}

static void restore(void) {
    if (restored || !initial_on) return;
    restored = 1;
    acipc_close();
    say("restoring system Bluetooth");
    set_power(1);
    say("power state after restore: %d (1 = on)", wait_power(1, 30) ? 1 : get_power());
}
static void on_signal(int s) { (void)s; restore(); _exit(3); }

static void *watchdog(void *arg) {
    (void)arg;
    sleep(60);
    say("watchdog: 60 s passed, restoring Bluetooth and exiting");
    restore();
    _exit(4);
}

// Sends one read-only HCI command and prints the Command Complete that answers it.
static int command(const char *name, uint16_t opcode) {
    uint8_t pkt[3] = {opcode & 0xff, opcode >> 8, 0};
    int rc = acipc_send(1, pkt, sizeof pkt);
    if (rc) { say("%s: send failed (%d)", name, rc); return rc; }
    for (int tries = 0; tries < 300; ++tries) {
        uint8_t type, buf[1024];
        int n = acipc_recv(&type, buf, sizeof buf, 10);
        if (n < 0) { say("%s: receive failed (%d)", name, n); return n; }
        if (n == 0) continue;
        if (type == 4 && n >= 6 && buf[0] == 0x0e && (buf[3] | buf[4] << 8) == opcode) {
            say("%s: status 0x%02x, %d parameter bytes", name, buf[5], n - 6);
            printf("          raw:");
            for (int i = 6; i < n && i < 6 + 16; ++i) printf(" %02x", buf[i]);
            printf("\n"); fflush(stdout);
            return 0;
        }
        say("(other packet: type %d, %d bytes, first byte 0x%02x)", type, n, buf[0]);
    }
    say("%s: no answer in 3 s", name);
    return -1;
}

int main(void) {
    void *h = dlopen("/System/Library/Frameworks/IOBluetooth.framework/IOBluetooth", RTLD_NOW);
    if (!h) { say("cannot load IOBluetooth"); return 1; }
    *(void **)&get_power = dlsym(h, "IOBluetoothPreferenceGetControllerPowerState");
    *(void **)&set_power = dlsym(h, "IOBluetoothPreferenceSetControllerPowerState");
    if (!get_power || !set_power) { say("power functions missing"); return 1; }

    int state = get_power();
    say("system Bluetooth power state: %d (1 = on)", state);
    if (state != 1) { say("Bluetooth is not on: stopping without changing anything"); return 2; }
    initial_on = 1;
    signal(SIGINT, on_signal); signal(SIGTERM, on_signal); signal(SIGHUP, on_signal);
    atexit(restore);
    pthread_t wd; pthread_create(&wd, NULL, watchdog, NULL); pthread_detach(wd);

    say("switching system Bluetooth off so the controller is free");
    set_power(0);
    if (!wait_power(0, 20)) { say("Bluetooth did not switch off; stopping"); return 5; }
    sleep(3);

    int rc = acipc_open();
    say("acipc_open: %d (0 = the three channels opened)", rc);
    if (rc == 0) {
        command("Read Local Version Information", 0x1001);
        command("Read BD_ADDR", 0x1009);
        command("Read Buffer Size", 0x1005);
        acipc_close();
        say("channels closed");
    }
    restore();
    return rc ? 6 : 0;
}

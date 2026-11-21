// SPDX-License-Identifier: GPL-3.0-or-later
// Launcher for the stage 2 test. It is the only part that changes system Bluetooth power.
//
//   launcher <python> <script> <target BD_ADDR> [seconds] [--allow-speakers] [--services-only] [--inject]
//
// It remembers that Bluetooth is on, switches it off, runs `python -I <script> --target <addr>`
// as a child, waits (with a time limit), makes sure the child is gone, switches Bluetooth on
// again and checks the result. Bluetooth is restored on every exit path: normal end, error,
// signal or timeout.
//
// It must run inside a signed app bundle with a Bluetooth usage description: macOS aborts a
// bare process that reads or sets the Bluetooth power state.
#include <ctype.h>
#include <dlfcn.h>
#include <signal.h>
#include <spawn.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

extern char **environ;

__attribute__((used, section("__TEXT,__info_plist")))
static const char info_plist[] =
    "<?xml version=\"1.0\" encoding=\"UTF-8\"?><!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
    "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\"><plist version=\"1.0\"><dict>"
    "<key>CFBundleIdentifier</key><string>dev.geekbridge.btlauncher</string>"
    "<key>NSMicrophoneUsageDescription</key><string>GeekBridge Phone test: carries your voice on a phone call through this Mac.</string>"
    "<key>NSBluetoothAlwaysUsageDescription</key><string>GeekBridge Phone test: uses the Mac's Bluetooth controller directly.</string>"
    "</dict></plist>";

static int (*get_power)(void);
static void (*set_power)(int);
static int initial_on;
static int restored;
static pid_t child;
static volatile sig_atomic_t stop_requested;

static void say(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void say(const char *fmt, ...) {
    time_t t = time(NULL); struct tm m; localtime_r(&t, &m);
    printf("[%02d:%02d:%02d] ", m.tm_hour, m.tm_min, m.tm_sec);
    va_list ap; va_start(ap, fmt); vprintf(fmt, ap); va_end(ap);
    printf("\n"); fflush(stdout);
}

static int wait_power(int want, int seconds) {
    for (int i = 0; i < seconds * 4; ++i) {
        if (get_power() == want) return 1;
        usleep(250000);
    }
    return get_power() == want;
}

static void stop_child(void) {
    if (child <= 0) return;
    int status;
    if (waitpid(child, &status, WNOHANG) == child) { child = 0; return; }
    say("stopping the test program");
    kill(child, SIGTERM);
    for (int i = 0; i < 50; ++i) {  // up to 5 s
        if (waitpid(child, &status, WNOHANG) == child) { child = 0; return; }
        usleep(100000);
    }
    kill(child, SIGKILL);
    waitpid(child, &status, 0);
    child = 0;
}

static void restore(void) {
    if (restored || !initial_on) return;
    restored = 1;
    stop_child();  // the controller channels must be closed before Bluetooth comes back
    say("switching system Bluetooth back on");
    set_power(1);
    say("power state after restore: %d (1 = on)", wait_power(1, 40) ? 1 : get_power());
}

static void on_signal(int s) { (void)s; stop_requested = 1; }

static int valid_address(const char *a) {
    if (strlen(a) != 17) return 0;
    for (int i = 0; i < 17; ++i)
        if (i % 3 == 2 ? a[i] != ':' : !isxdigit((unsigned char)a[i])) return 0;
    return 1;
}

int main(int argc, char **argv) {
    if (argc < 4) { say("usage: launcher <python> <script> <BD_ADDR> [seconds]"); return 64; }
    const char *python = argv[1], *script = argv[2], *target = argv[3];
    int limit = argc > 4 ? atoi(argv[4]) : 90;
    int allow_speakers = 0, services_only = 0, inject = 0;
    for (int i = 5; i < argc; ++i) {  // the only extra arguments that are passed on
        if (strcmp(argv[i], "--allow-speakers") == 0) allow_speakers = 1;
        else if (strcmp(argv[i], "--services-only") == 0) services_only = 1;
        else if (strcmp(argv[i], "--inject") == 0) inject = 1;
        else { say("unknown argument: %s", argv[i]); return 64; }
    }
    if (limit < 10 || limit > 300) limit = 90;
    if (!valid_address(target)) { say("the target is not a Bluetooth address"); return 64; }
    if (access(python, X_OK) || access(script, R_OK)) { say("python or script not found"); return 66; }

    void *h = dlopen("/System/Library/Frameworks/IOBluetooth.framework/IOBluetooth", RTLD_NOW);
    if (!h) { say("cannot load IOBluetooth"); return 1; }
    *(void **)&get_power = dlsym(h, "IOBluetoothPreferenceGetControllerPowerState");
    *(void **)&set_power = dlsym(h, "IOBluetoothPreferenceSetControllerPowerState");
    if (!get_power || !set_power) { say("power functions missing"); return 1; }

    int state = get_power();
    say("system Bluetooth power state: %d (1 = on)", state);
    if (state != 1) { say("Bluetooth is not on: stopping without changing anything"); return 2; }
    initial_on = 1;
    struct sigaction sa = {.sa_handler = on_signal};
    sigaction(SIGINT, &sa, NULL); sigaction(SIGTERM, &sa, NULL); sigaction(SIGHUP, &sa, NULL);
    atexit(restore);

    say("switching system Bluetooth off so the controller is free");
    set_power(0);
    if (!wait_power(0, 20)) { say("Bluetooth did not switch off; stopping"); return 5; }
    sleep(3);

    char *args[9] = {(char *)python, "-I", (char *)script, "--target", (char *)target, NULL};
    int next = 5;
    if (allow_speakers) args[next++] = "--allow-speakers";
    if (services_only) args[next++] = "--services-only";
    if (inject) args[next++] = "--inject";
    args[next] = NULL;
    int rc = posix_spawn(&child, python, NULL, NULL, args, environ);
    if (rc) { say("could not start the test program (%d)", rc); child = 0; restore(); return 7; }
    say("test program started (limit %d s)", limit);

    int exit_code = 8;
    time_t deadline = time(NULL) + limit;
    for (;;) {
        int status;
        pid_t done = waitpid(child, &status, WNOHANG);
        if (done == child) {
            child = 0;
            exit_code = WIFEXITED(status) ? WEXITSTATUS(status) : 9;
            say("test program ended with code %d", exit_code);
            break;
        }
        if (stop_requested) { say("signal received"); exit_code = 3; break; }
        if (time(NULL) >= deadline) { say("time limit reached"); exit_code = 4; break; }
        usleep(100000);
    }
    restore();
    return exit_code;
}

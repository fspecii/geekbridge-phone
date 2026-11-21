// SPDX-License-Identifier: GPL-3.0-or-later
// IOBluetooth bridge: paired phones, RFCOMM streams (as socket fds) and hands-free calls.
// Every IOBluetooth call runs on one helper thread that owns a run loop, because the
// framework delivers its callbacks on the run loop of the thread that started the work.

#import <Foundation/Foundation.h>
#import <IOBluetooth/IOBluetooth.h>
#include <sys/socket.h>
#include <unistd.h>
#include <pthread.h>
#include <string.h>
#include <time.h>
static int dbg_on(void) { static int v = -1; if (v < 0) v = getenv("GBP_DEBUG") != NULL; return v; }
static void ts(void) { time_t t = time(NULL); struct tm m; localtime_r(&t, &m); fprintf(stderr, "[%02d:%02d:%02d] ", m.tm_hour, m.tm_min, m.tm_sec); }
#define DBG(...) do { if (dbg_on()) { ts(); fprintf(stderr, __VA_ARGS__); } } while (0)

// A bare binary needs its Bluetooth usage text inside it, or macOS aborts it on first use.
__attribute__((used, section("__TEXT,__info_plist")))
static const unsigned char gbp_info_plist[] = {60,63,120,109,108,32,118,101,114,115,105,111,110,61,34,49,46,48,34,32,101,110,99,111,100,105,110,103,61,34,85,84,70,45,56,34,63,62,60,33,68,79,67,84,89,80,69,32,112,108,105,115,116,32,80,85,66,76,73,67,32,34,45,47,47,65,112,112,108,101,47,47,68,84,68,32,80,76,73,83,84,32,49,46,48,47,47,69,78,34,32,34,104,116,116,112,58,47,47,119,119,119,46,97,112,112,108,101,46,99,111,109,47,68,84,68,115,47,80,114,111,112,101,114,116,121,76,105,115,116,45,49,46,48,46,100,116,100,34,62,60,112,108,105,115,116,32,118,101,114,115,105,111,110,61,34,49,46,48,34,62,60,100,105,99,116,62,60,107,101,121,62,67,70,66,117,110,100,108,101,73,100,101,110,116,105,102,105,101,114,60,47,107,101,121,62,60,115,116,114,105,110,103,62,100,101,118,46,103,101,101,107,98,114,105,100,103,101,46,80,104,111,110,101,60,47,115,116,114,105,110,103,62,60,107,101,121,62,67,70,66,117,110,100,108,101,78,97,109,101,60,47,107,101,121,62,60,115,116,114,105,110,103,62,71,101,101,107,66,114,105,100,103,101,32,80,104,111,110,101,60,47,115,116,114,105,110,103,62,60,107,101,121,62,78,83,66,108,117,101,116,111,111,116,104,65,108,119,97,121,115,85,115,97,103,101,68,101,115,99,114,105,112,116,105,111,110,60,47,107,101,121,62,60,115,116,114,105,110,103,62,71,101,101,107,66,114,105,100,103,101,32,80,104,111,110,101,32,116,97,108,107,115,32,116,111,32,121,111,117,114,32,112,104,111,110,101,32,111,118,101,114,32,66,108,117,101,116,111,111,116,104,46,60,47,115,116,114,105,110,103,62,60,107,101,121,62,78,83,66,108,117,101,116,111,111,116,104,80,101,114,105,112,104,101,114,97,108,85,115,97,103,101,68,101,115,99,114,105,112,116,105,111,110,60,47,107,101,121,62,60,115,116,114,105,110,103,62,71,101,101,107,66,114,105,100,103,101,32,80,104,111,110,101,32,116,97,108,107,115,32,116,111,32,121,111,117,114,32,112,104,111,110,101,32,111,118,101,114,32,66,108,117,101,116,111,111,116,104,46,60,47,115,116,114,105,110,103,62,60,47,100,105,99,116,62,60,47,112,108,105,115,116,62,0};

// Runs `b` on the main thread (IOBluetooth delivers its callbacks on the main run loop, which
// the app's window loop services) and waits for it to return, not for callbacks. Must never be
// called from the main thread itself.
static void on_bt(dispatch_block_t b) {
    dispatch_semaphore_t s = dispatch_semaphore_create(0);
    dispatch_async(dispatch_get_main_queue(), ^{ @autoreleasepool { b(); } dispatch_semaphore_signal(s); });
    dispatch_semaphore_wait(s, DISPATCH_TIME_FOREVER);
}

// For programs without a window loop (the probe example): services the main run loop.
void gbp_run_main_loop(void) { [[NSRunLoop mainRunLoop] run]; }

static NSString *norm_addr(NSString *a) {
    return [[a stringByReplacingOccurrencesOfString:@"-" withString:@":"] uppercaseString];
}

static IOBluetoothDevice *device_for(const char *addr) {
    NSString *want = norm_addr([NSString stringWithUTF8String:addr]);
    for (IOBluetoothDevice *d in [IOBluetoothDevice pairedDevices] ?: @[])
        if ([norm_addr(d.addressString) isEqualToString:want]) return d;   // the object macOS tracks
    return [IOBluetoothDevice deviceWithAddressString:[want stringByReplacingOccurrencesOfString:@":" withString:@"-"]];
}

static void put(char *buf, size_t n, NSString *s) {
    if (n == 0) return;
    strlcpy(buf, s.UTF8String ?: "", n);
}

// ---- paired phones -------------------------------------------------------------------------
// One phone per line: ADDRESS \t NAME \t CONNECTED(0/1) \t CLASS_MAJOR
int gbp_paired(char *out, size_t n) {
    __block NSMutableString *r = [NSMutableString string];
    on_bt(^{
        for (IOBluetoothDevice *d in [IOBluetoothDevice pairedDevices] ?: @[]) {
            NSString *name = d.name ?: d.addressString;
            [r appendFormat:@"%@\t%@\t%d\t%u\n", norm_addr(d.addressString), name,
                d.isConnected ? 1 : 0, (unsigned)d.deviceClassMajor];
        }
    });
    put(out, n, r);
    return 0;
}

// ---- SDP + RFCOMM ----------------------------------------------------------------------------
@interface GBPLink : NSObject <IOBluetoothRFCOMMChannelDelegate>
@property (strong) IOBluetoothDevice *dev;
@property (strong) IOBluetoothRFCOMMChannel *chan;
@property (strong) dispatch_semaphore_t sem;
@property IOReturn status;
@property int fd;          // our end of the socketpair
@property BOOL closing;
@end
@implementation GBPLink
- (void)connectionComplete:(IOBluetoothDevice *)d status:(IOReturn)s { DBG("gbp: connectionComplete=0x%x\n", s); self.status = s; dispatch_semaphore_signal(self.sem); }
- (void)sdpQueryComplete:(IOBluetoothDevice *)d status:(IOReturn)s { DBG("gbp: sdpQueryComplete=0x%x\n", s); self.status = s; dispatch_semaphore_signal(self.sem); }
- (void)rfcommChannelOpenComplete:(IOBluetoothRFCOMMChannel *)c status:(IOReturn)s { DBG("gbp: ch%u open complete=0x%x mtu=%u\n", (unsigned)c.getChannelID, s, (unsigned)c.getMTU); self.status = s; dispatch_semaphore_signal(self.sem); }
- (void)rfcommChannelData:(IOBluetoothRFCOMMChannel *)c data:(void *)p length:(size_t)len {
    DBG("gbp: ch%u rx %zu bytes\n", (unsigned)c.getChannelID, len);
    const char *b = p; size_t off = 0;
    while (off < len && !self.closing && self.fd >= 0) {
        ssize_t w = write(self.fd, b + off, len - off);
        if (w <= 0) { self.closing = YES; [c closeChannel]; return; }
        off += (size_t)w;
    }
}
- (void)rfcommChannelWriteComplete:(IOBluetoothRFCOMMChannel *)c refcon:(void *)refcon status:(IOReturn)e {
    if (e != kIOReturnSuccess) DBG("gbp: write failed 0x%x\n", e);
    free(refcon);
}
- (void)rfcommChannelClosed:(IOBluetoothRFCOMMChannel *)c {
    DBG("gbp: ch%u closed by phone/stack\n", (unsigned)c.getChannelID);
    self.closing = YES;
    if (self.fd >= 0) shutdown(self.fd, SHUT_RDWR);   // Rust side sees EOF
}
@end

static NSMutableSet *gLinks;

// IOBluetooth does not retain its delegates, and it can call them after the call that made
// them has returned (after a timeout, say). Every delegate therefore lives for the whole run.
static NSMutableArray *gKeep;
static void keep(id o) {
    static dispatch_once_t once;
    dispatch_once(&once, ^{ gKeep = [NSMutableArray array]; });
    @synchronized (gKeep) { [gKeep addObject:o]; }
}

// Writes from the phone-bound side: socketpair -> RFCOMM.
static void *pump_out(void *arg) {
    GBPLink *l = (__bridge_transfer GBPLink *)arg;
    uint8_t buf[1024];
    for (;;) {
        ssize_t n = read(l.fd, buf, sizeof buf);
        if (n <= 0) break;
        __block IOReturn rc = kIOReturnSuccess;
        NSData *d = [NSData dataWithBytes:buf length:(NSUInteger)n];
        on_bt(^{
            if (l.closing || !l.chan.isOpen) { rc = kIOReturnNotOpen; return; }
            // Queue the data without waiting: a blocking write on this thread would stop the
            // phone's replies (also handled here) from being read, and both would wait forever.
            NSUInteger off = 0, mtu = MAX(1, (NSUInteger)l.chan.getMTU);
            while (off < d.length && rc == kIOReturnSuccess) {
                NSUInteger k = MIN(mtu, d.length - off);
                void *copy = malloc(k);
                memcpy(copy, (const uint8_t *)d.bytes + off, k);
                rc = [l.chan writeAsync:copy length:(UInt16)k refcon:copy];   // freed on completion
                DBG("gbp: ch%u tx %lu bytes rc=0x%x\n", (unsigned)l.chan.getChannelID, (unsigned long)k, rc);
                if (rc != kIOReturnSuccess) free(copy);
                off += k;
            }
        });
        if (rc != kIOReturnSuccess) break;
    }
    __block int fd = -1;
    on_bt(^{ l.closing = YES; [l.chan closeChannel]; fd = l.fd; l.fd = -1; });
    if (fd >= 0) close(fd);
    return NULL;
}

// Opens the service with the 16-bit class `uuid16` on the phone. Returns a socket fd, or -1
// with a message in `err`; `*no_service` is set when the phone has no such service.
int gbp_open_service(const char *addr, unsigned uuid16, int *no_service, char *err, size_t en) {
    *no_service = 0;
    __block GBPLink *l = [GBPLink new];
    keep(l);
    l.sem = dispatch_semaphore_create(0);
    l.dev = device_for(addr);
    if (!l.dev) { put(err, en, @"unknown device"); return -1; }
    int svv[2];
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, svv) != 0) { put(err, en, @"socketpair failed"); return -1; }
    const int sv0 = svv[0], sv1 = svv[1];

    // 0. a cached record answers without any radio traffic
    __block BluetoothRFCOMMChannelID cached = 0;
    on_bt(^{
        DBG("gbp: cached services=%lu\n", (unsigned long)l.dev.services.count);
        for (IOBluetoothSDPServiceRecord *r in l.dev.services ?: @[]) DBG("gbp:   %s\n", r.getServiceName.UTF8String ?: "?");
        IOBluetoothSDPServiceRecord *rec = [l.dev getServiceRecordForUUID:[IOBluetoothSDPUUID uuid16:(UInt16)uuid16]];
        if (rec) { BluetoothRFCOMMChannelID c = 0; if ([rec getRFCOMMChannelID:&c] == kIOReturnSuccess) cached = c; }
    });
    if (cached == 0) {
    // 1. bring the baseband link up (the phone is usually paired but idle), then read the
    //    service records fresh (macOS caches them across phones' changes).
    // Opening the channel brings the link up. The Rust side never calls this for Apple devices
    // or unpaired ones, so a stale pairing cannot make the phone show prompts.
    on_bt(^{
        l.status = kIOReturnSuccess;
        IOReturn r = [l.dev performSDPQuery:l];
        DBG("gbp: performSDPQuery=0x%x\n", r);
        if (r != kIOReturnSuccess) { l.status = r; dispatch_semaphore_signal(l.sem); }
    });
    if (dispatch_semaphore_wait(l.sem, dispatch_time(DISPATCH_TIME_NOW, 20 * NSEC_PER_SEC)) != 0) {
        close(sv0); close(sv1); put(err, en, @"Bluetooth service lookup timed out"); return -1;
    }
    if (l.status != kIOReturnSuccess) {
        close(sv0); close(sv1);
        put(err, en, [NSString stringWithFormat:@"service lookup failed (0x%x)", l.status]); return -1;
    }
    }
    __block BluetoothRFCOMMChannelID ch = cached;
    if (!ch) on_bt(^{
        IOBluetoothSDPServiceRecord *rec = [l.dev getServiceRecordForUUID:[IOBluetoothSDPUUID uuid16:(UInt16)uuid16]];
        if (rec) { BluetoothRFCOMMChannelID c = 0; if ([rec getRFCOMMChannelID:&c] == kIOReturnSuccess) ch = c; }
    });
    if (ch == 0) { close(sv0); close(sv1); *no_service = 1; put(err, en, @"the phone does not offer that service"); return -1; }

    // 2. open the channel
    __block IOReturn r = kIOReturnSuccess;
    on_bt(^{
        IOBluetoothRFCOMMChannel *c = nil;
        r = [l.dev openRFCOMMChannelAsync:&c withChannelID:ch delegate:l];
        l.chan = c; l.fd = sv0;
        if (r != kIOReturnSuccess) dispatch_semaphore_signal(l.sem);
    });
    if (r == kIOReturnSuccess &&
        dispatch_semaphore_wait(l.sem, dispatch_time(DISPATCH_TIME_NOW, 15 * NSEC_PER_SEC)) != 0) {
        on_bt(^{ [l.chan closeChannel]; });
        close(sv0); close(sv1); put(err, en, @"Bluetooth connect timed out"); return -1;
    }
    if (r == kIOReturnSuccess) r = l.status;
    if (r != kIOReturnSuccess) {
        close(sv0); close(sv1);
        put(err, en, [NSString stringWithFormat:@"could not open the service (0x%x)", r]); return -1;
    }
    on_bt(^{ if (!gLinks) gLinks = [NSMutableSet set]; [gLinks addObject:l]; });
    pthread_t t;
    pthread_create(&t, NULL, pump_out, (__bridge_retained void *)l);
    pthread_detach(t);
    return sv1;
}

// ---- hands-free calls ----------------------------------------------------------------------
@interface GBPHF : NSObject <IOBluetoothHandsFreeDeviceDelegate>
@property (strong) IOBluetoothHandsFreeDevice *hf;
@property (strong) dispatch_semaphore_t up;
@property BOOL connected, active;
@property int setup, hold;
@property (copy) NSString *number;
@property (copy) NSString *dialed;
@property BOOL used;
@property (copy) NSString *callerName;
@property (strong) NSDate *setupAt;   // when the current ring began
@property BOOL wantAudio;   // the call was answered or placed from the app
@property (strong) NSDate *lastConnect;   // overlapping connect() calls crash IOBluetooth
@end
@implementation GBPHF
- (void)handsFree:(IOBluetoothHandsFree *)d connected:(NSNumber *)s { DBG("gbp: HF connected status=%d\n", s.intValue); self.connected = (s.intValue == 0); dispatch_semaphore_signal(self.up); }
- (void)handsFree:(IOBluetoothHandsFree *)d disconnected:(NSNumber *)s { DBG("gbp: HF disconnected status=%d\n", s.intValue); self.connected = NO; self.active = NO; self.setup = 0; }
- (void)handsFree:(IOBluetoothHandsFreeDevice *)d isServiceAvailable:(NSNumber *)v { DBG("gbp: HF service available=%d\n", v.intValue); }
- (void)handsFree:(IOBluetoothHandsFree *)d scoConnectionOpened:(NSNumber *)s { DBG("gbp: HF audio opened status=%d\n", s.intValue); }
- (void)handsFree:(IOBluetoothHandsFree *)d scoConnectionClosed:(NSNumber *)s { DBG("gbp: HF audio closed status=%d\n", s.intValue); }
- (void)handsFree:(IOBluetoothHandsFreeDevice *)d unhandledResultCode:(NSString *)c { DBG("gbp: HF unhandled %s\n", c.UTF8String); }
- (void)handsFree:(IOBluetoothHandsFreeDevice *)d isCallActive:(NSNumber *)v {
    DBG("gbp: HF call active=%d\n", v.intValue);
    self.active = v.boolValue;
    if (self.active && self.wantAudio && !d.isSCOConnected) {
        DBG("gbp: HF moving call audio to this Mac\n");
        [d connectSCO];
    }
    if (!self.active) self.wantAudio = NO;
}
- (void)handsFree:(IOBluetoothHandsFreeDevice *)d callSetupMode:(NSNumber *)v {
    DBG("gbp: HF call setup=%d\n", v.intValue);
    self.setup = v.intValue;
    self.setupAt = self.setup == 0 ? nil : (self.setupAt ?: [NSDate date]);
    if (self.setup == 0) { self.number = nil; self.callerName = nil; }
    else if (!self.number.length) {
        // The ring itself carries no number: ask the phone for its call list.
        IOBluetoothHandsFreeDevice *hf = d;
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 100 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{ [hf currentCallList]; });
    }
}
- (void)handsFree:(IOBluetoothHandsFreeDevice *)d currentCall:(NSDictionary *)c {
    NSString *n = c[IOBluetoothHandsFreeCallNumber];
    NSString *nm = c[IOBluetoothHandsFreeCallName];
    DBG("gbp: HF call list entry (number known=%d name known=%d)\n", n.length > 0, nm.length > 0);
    if (n.length) self.number = n;
    if (nm.length) self.callerName = nm;
}
- (void)handsFree:(IOBluetoothHandsFreeDevice *)d callHoldState:(NSNumber *)v { self.hold = v.intValue; }
- (void)handsFree:(IOBluetoothHandsFreeDevice *)d incomingCallFrom:(NSString *)n { DBG("gbp: HF incoming call (number known=%d)\n", n.length > 0); self.number = n; }
@end

static NSMutableDictionary<NSString *, GBPHF *> *gHF;

static GBPHF *hf_for(const char *addr, char *err, size_t en) {
    NSString *key = norm_addr([NSString stringWithUTF8String:addr]);
    __block GBPHF *g = nil;
    on_bt(^{
        if (!gHF) gHF = [NSMutableDictionary dictionary];
        g = gHF[key];
        if (!g) {
            IOBluetoothDevice *dev = device_for(addr);
            if (!dev) return;
            g = [GBPHF new];
            g.up = dispatch_semaphore_create(0);
            g.hf = [[IOBluetoothHandsFreeDevice alloc] initWithDevice:dev delegate:g];
            gHF[key] = g;
        }
        // One connect at a time: starting another while the first is still opening makes
        // IOBluetooth use a freed object and crash the app.
        if (!g.hf.isConnected && g.hf.device.isConnected && (!g.lastConnect || -[g.lastConnect timeIntervalSinceNow] > 90)) {
            g.lastConnect = [NSDate date];
            // A hands-free object that has already failed once crashes inside IOBluetooth when
            // reused (it keeps a half-sent command), so every attempt gets a fresh one. The old
            // one is kept alive, never freed, because IOBluetooth may still call back into it.
            if (g.used) {
                keep(g.hf);
                IOBluetoothDevice *dev = device_for(addr);
                g.hf = [[IOBluetoothHandsFreeDevice alloc] initWithDevice:dev delegate:g];
            }
            g.used = YES;
            DBG("gbp: HF connect() started\n");
            // Let the new object finish registering with the system before connecting.
            IOBluetoothHandsFreeDevice *hf = g.hf;
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 2 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{ [hf connect]; });
        }
    });
    if (!g) { put(err, en, @"unknown device"); return nil; }
    if (!g.connected) dispatch_semaphore_wait(g.up, dispatch_time(DISPATCH_TIME_NOW, 10 * NSEC_PER_SEC));
    if (!g.connected) { put(err, en, @"the phone's hands-free link is not up (turn on Calls for this Mac in the phone's Bluetooth settings)"); return nil; }
    return g;
}

// state: "active setup hold number"; returns 0 ok, -1 error.
int gbp_hf_state(const char *addr, char *out, size_t n, char *err, size_t en) {
    GBPHF *g = hf_for(addr, err, en);
    if (!g) return -1;
    // A ring is reported only once the caller is known (the phone sends the number a moment
    // after the ring), or after 1.5 s without one, so the banner can show who is calling.
    int setup = g.setup;
    if (setup == 1 && !g.number.length && g.setupAt && -[g.setupAt timeIntervalSinceNow] < 1.5) setup = 0;
    put(out, n, [NSString stringWithFormat:@"%d\t%d\t%d\t%@\t%@", g.active ? 1 : 0, setup, g.hold, g.number ?: g.dialed ?: @"", g.callerName ?: @""]);
    return 0;
}

// cmd: 'd' dial(arg) 'a' answer 'e' end 't' tones(arg) 's' audio to this Mac
int gbp_hf_cmd(const char *addr, char cmd, const char *arg, char *err, size_t en) {
    GBPHF *g = hf_for(addr, err, en);
    if (!g) return -1;
    NSString *a = arg ? [NSString stringWithUTF8String:arg] : @"";
    on_bt(^{
        switch (cmd) {
            case 'd': g.dialed = a; g.wantAudio = YES; [g.hf dialNumber:a]; break;
            case 'a': g.wantAudio = YES; [g.hf acceptCall]; break;
            case 'e': [g.hf endCall]; break;
            case 't': for (NSUInteger i = 0; i < a.length; i++) [g.hf sendDTMF:[a substringWithRange:NSMakeRange(i, 1)]]; break;
            case 's': [g.hf connectSCO]; break;
        }
    });
    return 0;
}

// Calls a connected phone's Bluetooth link up so SDP and RFCOMM work: not needed on macOS.

// ---- scanning and pairing: only ever started by a button press in the app ----------------------
@interface GBPScan : NSObject <IOBluetoothDeviceInquiryDelegate>
@property (strong) dispatch_semaphore_t done;
@property (strong) NSMutableString *out;
@property (strong) NSMutableSet *seen;
@end
@implementation GBPScan
- (void)take:(IOBluetoothDevice *)d {
    NSString *name = d.name;
    if (name.length == 0 || d.isPaired) return;
    NSString *a = norm_addr(d.addressString);
    if ([self.seen containsObject:a]) return;
    // Phones, and devices that do not say what they are.
    if (d.deviceClassMajor != kBluetoothDeviceClassMajorPhone && d.deviceClassMajor != kBluetoothDeviceClassMajorMiscellaneous) return;
    [self.seen addObject:a];
    [self.out appendFormat:@"%@\t%@\n", a, name];
}
- (void)deviceInquiryDeviceFound:(IOBluetoothDeviceInquiry *)i device:(IOBluetoothDevice *)d { [self take:d]; }
- (void)deviceInquiryDeviceNameUpdated:(IOBluetoothDeviceInquiry *)i device:(IOBluetoothDevice *)d devicesRemaining:(uint32_t)n { [self take:d]; }
- (void)deviceInquiryComplete:(IOBluetoothDeviceInquiry *)i error:(IOReturn)e aborted:(BOOL)a { dispatch_semaphore_signal(self.done); }
@end

// One inquiry of `seconds` seconds. Lines: ADDRESS \t NAME. Returns 0, or -1 with `err`.
int gbp_scan(unsigned seconds, char *out, size_t n, char *err, size_t en) {
    __block GBPScan *g = [GBPScan new];
    keep(g);
    g.done = dispatch_semaphore_create(0);
    g.out = [NSMutableString string];
    g.seen = [NSMutableSet set];
    __block IOBluetoothDeviceInquiry *inq = nil;
    __block IOReturn r = kIOReturnSuccess;
    on_bt(^{
        inq = [IOBluetoothDeviceInquiry inquiryWithDelegate:g];
        keep(inq);
        inq.inquiryLength = (uint8_t)MIN(seconds, 48);
        inq.updateNewDeviceNames = YES;
        r = [inq start];
    });
    if (r != kIOReturnSuccess) { put(err, en, [NSString stringWithFormat:@"could not start the search (0x%x); is Bluetooth on?", r]); return -1; }
    dispatch_semaphore_wait(g.done, dispatch_time(DISPATCH_TIME_NOW, (seconds + 3) * NSEC_PER_SEC));
    on_bt(^{ [inq stop]; });
    put(out, n, g.out);
    return 0;
}

@interface GBPPair : NSObject <IOBluetoothDevicePairDelegate>
@property (strong) dispatch_semaphore_t done;
@property IOReturn status;
@property (strong) IOBluetoothDevicePair *pair;
@property (assign) void (*cb)(void *, const char *);
@property (assign) void *user;
@end
@implementation GBPPair
- (void)devicePairingUserConfirmationRequest:(id)sender numericValue:(BluetoothNumericValue)v {
    char code[16]; snprintf(code, sizeof code, "%06u", (unsigned)v);
    if (self.cb) self.cb(self.user, code);
    [(IOBluetoothDevicePair *)sender replyUserConfirmation:YES];   // the user compares on the phone
}
- (void)devicePairingPINCodeRequest:(id)sender {
    BluetoothPINCode pin; memset(&pin, 0, sizeof pin); memcpy(pin.data, "0000", 4);
    [(IOBluetoothDevicePair *)sender replyPINCode:4 PINCode:&pin];
}
- (void)devicePairingFinished:(id)sender error:(IOReturn)e { self.status = e; dispatch_semaphore_signal(self.done); }
@end

// Pairs once. `cb(user, code)` gets the number the phone shows too. Returns 0 on success.
int gbp_pair(const char *addr, void (*cb)(void *, const char *), void *user, char *err, size_t en) {
    __block GBPPair *g = [GBPPair new];
    keep(g);
    g.done = dispatch_semaphore_create(0);
    g.cb = cb; g.user = user;
    __block IOReturn r = kIOReturnSuccess;
    on_bt(^{
        IOBluetoothDevice *dev = device_for(addr);
        if (!dev) { r = kIOReturnNoDevice; return; }
        g.pair = [IOBluetoothDevicePair pairWithDevice:dev];
        keep(g.pair);
        g.pair.delegate = g;
        r = [g.pair start];
    });
    if (r != kIOReturnSuccess) { put(err, en, [NSString stringWithFormat:@"could not start pairing (0x%x)", r]); return -1; }
    if (dispatch_semaphore_wait(g.done, dispatch_time(DISPATCH_TIME_NOW, 90 * NSEC_PER_SEC)) != 0) {
        on_bt(^{ [g.pair stop]; });
        put(err, en, @"pairing timed out: accept the request on the phone"); return -1;
    }
    if (g.status != kIOReturnSuccess) { put(err, en, [NSString stringWithFormat:@"pairing failed (0x%x). If it keeps failing, pair the phone in System Settings > Bluetooth instead; the app will then use it.", g.status]); return -1; }
    return 0;
}

// Removes the pairing (the same thing as "Forget This Device"). macOS has no public call for
// it, so this uses the `remove` selector that IOBluetoothDevice answers to, and says so when a
// future macOS does not. Only runs from the Unpair button.
int gbp_unpair(const char *addr, char *err, size_t en) {
    __block int rc = 0;
    on_bt(^{
        IOBluetoothDevice *dev = device_for(addr);
        SEL sel = NSSelectorFromString(@"remove");
        if (dev && [dev respondsToSelector:sel]) {
            IMP imp = [dev methodForSelector:sel];
            ((void (*)(id, SEL))imp)(dev, sel);
        } else rc = -1;
    });
    if (rc != 0) put(err, en, @"this macOS cannot remove it from here: open System Settings > Bluetooth, click the i next to the phone and choose Forget This Device");
    return rc;
}

// ---- connecting a paired phone ----------------------------------------------------------------
@interface GBPConn : NSObject
@end
@implementation GBPConn
- (void)connectionComplete:(IOBluetoothDevice *)d status:(IOReturn)s { DBG("gbp: link complete=0x%x\n", s); }
@end

// Starts the Bluetooth link to a paired phone and returns at once.
void gbp_connect(const char *addr) {
    NSString *a = [NSString stringWithUTF8String:addr];
    dispatch_async(dispatch_get_main_queue(), ^{
        static GBPConn *conn;
        if (!conn) conn = [GBPConn new];
        IOBluetoothDevice *dev = device_for(a.UTF8String);
        if (dev && !dev.isConnected) {
            IOReturn r = [dev openConnection:conn withPageTimeout:0x4000 authenticationRequired:NO];
            DBG("gbp: openConnection=0x%x\n", r);
        }
    });
}

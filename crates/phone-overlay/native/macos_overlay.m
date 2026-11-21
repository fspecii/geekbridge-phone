// SPDX-License-Identifier: GPL-3.0-or-later
// macOS banner window: one floating, non-activating panel in the top-right corner of the main
// screen. Rust draws the banners to a pixmap; this file only shows the pixels and reports
// mouse and key events back. All AppKit work happens on the main thread.

#import <Cocoa/Cocoa.h>

typedef void (*gbp_ov_cb)(int kind, float a, float b, const char *text);
// kinds: 1 move(x,y)  2 leave  3 down(x,y)  4 up(x,y)  5 key(text; b = 1 when option held)  6 keyboard lost

static gbp_ov_cb gCb;

@interface GBPOverlayPanel : NSPanel
@property BOOL wantKey;
@end
@implementation GBPOverlayPanel
- (BOOL)canBecomeKeyWindow { return self.wantKey; }
- (BOOL)canBecomeMainWindow { return NO; }
- (void)resignKeyWindow { [super resignKeyWindow]; if (gCb) gCb(6, 0, 0, NULL); }
@end

@interface GBPOverlayView : NSView
@property (strong) NSImage *image;
@end
@implementation GBPOverlayView
- (BOOL)isFlipped { return YES; }
- (BOOL)acceptsFirstMouse:(NSEvent *)e { return YES; }
- (BOOL)acceptsFirstResponder { return YES; }
- (void)updateTrackingAreas {
    for (NSTrackingArea *t in self.trackingAreas) [self removeTrackingArea:t];
    [self addTrackingArea:[[NSTrackingArea alloc] initWithRect:self.bounds
        options:NSTrackingMouseMoved | NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways | NSTrackingInVisibleRect
        owner:self userInfo:nil]];
    [super updateTrackingAreas];
}
- (void)drawRect:(NSRect)r {
    [[NSColor clearColor] set];
    NSRectFillUsingOperation(self.bounds, NSCompositingOperationClear);
    [self.image drawInRect:self.bounds fromRect:NSZeroRect operation:NSCompositingOperationSourceOver
                  fraction:1.0 respectFlipped:YES hints:nil];
}
- (void)report:(int)kind event:(NSEvent *)e {
    NSPoint p = [self convertPoint:e.locationInWindow fromView:nil];
    if (gCb) gCb(kind, (float)p.x, (float)p.y, NULL);
}
- (void)mouseMoved:(NSEvent *)e { [self report:1 event:e]; }
- (void)mouseDragged:(NSEvent *)e { [self report:1 event:e]; }
- (void)mouseExited:(NSEvent *)e { if (gCb) gCb(2, 0, 0, NULL); }
- (void)mouseDown:(NSEvent *)e { [self report:1 event:e]; [self report:3 event:e]; }
- (void)mouseUp:(NSEvent *)e { [self report:4 event:e]; }
- (void)keyDown:(NSEvent *)e {
    if (!gCb) return;
    BOOL opt = (e.modifierFlags & NSEventModifierFlagOption) != 0;
    const char *name = NULL;
    switch (e.keyCode) {
        case 51: name = "Backspace"; break;
        case 117: name = "Delete"; break;
        case 123: name = "Left"; break;
        case 124: name = "Right"; break;
        case 115: name = "Home"; break;
        case 119: name = "End"; break;
        case 36: case 76: name = "Enter"; break;
        case 53: name = "Escape"; break;
    }
    if (name) { gCb(5, 0, opt ? 1 : 0, name); return; }
    NSString *s = e.characters;
    if (s.length == 0 || (e.modifierFlags & NSEventModifierFlagCommand)) return;
    unichar c = [s characterAtIndex:0];
    if (c < 0x20 || c == 0x7f || (c >= 0xF700 && c <= 0xF8FF)) return;   // control / function keys
    // Text is sent with a "T:" prefix so it cannot be mistaken for a key name.
    NSString *t = [@"T:" stringByAppendingString:s];
    gCb(5, 0, 0, t.UTF8String);
}
@end

static GBPOverlayPanel *gPanel;
static GBPOverlayView *gView;

static void ensure_panel(void) {
    if (gPanel) return;
    gPanel = [[GBPOverlayPanel alloc] initWithContentRect:NSMakeRect(0, 0, 10, 10)
        styleMask:NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        backing:NSBackingStoreBuffered defer:NO];
    gPanel.level = NSStatusWindowLevel;
    gPanel.opaque = NO;
    gPanel.backgroundColor = [NSColor clearColor];
    gPanel.hasShadow = NO;            // the banners draw their own shadow
    gPanel.floatingPanel = YES;
    gPanel.hidesOnDeactivate = NO;
    gPanel.releasedWhenClosed = NO;
    gPanel.acceptsMouseMovedEvents = YES;
    gPanel.collectionBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces |
        NSWindowCollectionBehaviorFullScreenAuxiliary | NSWindowCollectionBehaviorStationary;
    gView = [[GBPOverlayView alloc] initWithFrame:NSMakeRect(0, 0, 10, 10)];
    gPanel.contentView = gView;
}

void gbp_ov_init(gbp_ov_cb cb) { gCb = cb; }

// `rgba` is premultiplied RGBA, `w` x `h` pixels, drawn at `scale` pixels per point.
void gbp_ov_present(const uint8_t *rgba, int w, int h, float scale) {
    NSData *data = [NSData dataWithBytes:rgba length:(NSUInteger)w * h * 4];
    dispatch_async(dispatch_get_main_queue(), ^{
        @autoreleasepool {
            ensure_panel();
            CGDataProviderRef prov = CGDataProviderCreateWithCFData((__bridge CFDataRef)data);
            CGColorSpaceRef cs = CGColorSpaceCreateDeviceRGB();
            CGImageRef img = CGImageCreate(w, h, 8, 32, (size_t)w * 4, cs,
                kCGImageAlphaPremultipliedLast | kCGBitmapByteOrderDefault, prov, NULL, false, kCGRenderingIntentDefault);
            CGColorSpaceRelease(cs);
            CGDataProviderRelease(prov);
            NSSize pts = NSMakeSize(w / scale, h / scale);
            gView.image = [[NSImage alloc] initWithCGImage:img size:pts];
            CGImageRelease(img);
            NSRect vf = [NSScreen mainScreen].visibleFrame;
            NSRect frame = NSMakeRect(NSMaxX(vf) - pts.width, NSMaxY(vf) - pts.height, pts.width, pts.height);
            [gPanel setFrame:frame display:YES];
            gView.frame = NSMakeRect(0, 0, pts.width, pts.height);
            [gView setNeedsDisplay:YES];
            if (!gPanel.isVisible) [gPanel orderFrontRegardless];
        }
    });
}

void gbp_ov_hide(void) {
    dispatch_async(dispatch_get_main_queue(), ^{ if (gPanel) [gPanel orderOut:nil]; });
}

// A reply field needs the keyboard: the panel takes it only while one is being edited.
void gbp_ov_want_keyboard(int on) {
    dispatch_async(dispatch_get_main_queue(), ^{
        ensure_panel();
        gPanel.wantKey = on != 0;
        if (on) { [gPanel makeKeyWindow]; [gPanel makeFirstResponder:gView]; }
        else if (gPanel.isKeyWindow) [gPanel resignKeyWindow];
    });
}

float gbp_ov_scale(void) {
    // Read directly: waiting for the main thread from here could deadlock with the app's start-up.
    return (float)[NSScreen mainScreen].backingScaleFactor;
}

int gbp_ov_is_dark(void) {
    NSString *s = [[NSUserDefaults standardUserDefaults] stringForKey:@"AppleInterfaceStyle"];
    return s != nil && [s caseInsensitiveCompare:@"Dark"] == NSOrderedSame;
}

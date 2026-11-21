# SPDX-License-Identifier: GPL-3.0-or-later
"""Moves the Mac's pointer from our own mouse reports, through Quartz events.

macOS only accepts these events from a process the user has allowed under
System Settings > Privacy & Security > Accessibility. Without that the calls succeed and
nothing moves, so `trusted()` is checked first.
"""
import ctypes
import ctypes.util


class Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class Rect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double), ("w", ctypes.c_double), ("h", ctypes.c_double)]


MOVED, LEFT_DOWN, LEFT_UP, RIGHT_DOWN, RIGHT_UP, LEFT_DRAGGED, RIGHT_DRAGGED = 5, 1, 2, 3, 4, 6, 7
HID_TAP = 0
SCROLL_UNIT_LINE = 1


def plan(buttons_before, report):
    """Pure logic: the events one report produces, as (kind, button) pairs, moves first."""
    left_before, right_before = buttons_before
    events = []
    if report.dx or report.dy:
        events.append((LEFT_DRAGGED if left_before else RIGHT_DRAGGED if right_before else MOVED,
                       1 if right_before and not left_before else 0))
    if report.left != left_before:
        events.append((LEFT_DOWN if report.left else LEFT_UP, 0))
    if report.right != right_before:
        events.append((RIGHT_DOWN if report.right else RIGHT_UP, 1))
    return events


class Pointer:
    def __init__(self):
        cg = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        self.cg, self.cf = cg, cf
        cg.AXIsProcessTrusted.restype = ctypes.c_bool
        cg.CGEventCreate.restype = ctypes.c_void_p
        cg.CGEventCreate.argtypes = [ctypes.c_void_p]
        cg.CGEventGetLocation.restype = Point
        cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, Point, ctypes.c_uint32]
        cg.CGEventCreateScrollWheelEvent2.restype = ctypes.c_void_p
        cg.CGEventCreateScrollWheelEvent2.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                                      ctypes.c_int32, ctypes.c_int32, ctypes.c_int32]
        cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        cg.CGDisplayBounds.restype = Rect
        cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        self.buttons = (False, False)
        self.posted = 0

    def trusted(self, ask=False):
        """With `ask`, macOS shows its own dialog that leads to the Accessibility list, once."""
        if not ask:
            return bool(self.cg.AXIsProcessTrusted())
        cg, cf = self.cg, self.cf
        cg.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        cg.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        cf.CFDictionaryCreate.restype = ctypes.c_void_p
        cf.CFDictionaryCreate.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                          ctypes.POINTER(ctypes.c_void_p), ctypes.c_long,
                                          ctypes.c_void_p, ctypes.c_void_p]
        key = ctypes.c_void_p.in_dll(cg, "kAXTrustedCheckOptionPrompt")
        yes = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
        options = cf.CFDictionaryCreate(None, ctypes.byref(key), ctypes.byref(yes), 1,
                                        ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")),
                                        ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks")))
        allowed = bool(cg.AXIsProcessTrustedWithOptions(options))
        cf.CFRelease(options)
        return allowed

    def _location(self):
        event = self.cg.CGEventCreate(None)
        point = self.cg.CGEventGetLocation(event)
        self.cf.CFRelease(event)
        return point

    def _post(self, event):
        if event:
            self.cg.CGEventPost(HID_TAP, event)
            self.cf.CFRelease(event)
            self.posted += 1

    def apply(self, report, scroll_lines=0):
        where = self._location()
        bounds = self.cg.CGDisplayBounds(self.cg.CGMainDisplayID())
        where.x = min(max(where.x + report.dx, bounds.x), bounds.x + bounds.w - 1)
        where.y = min(max(where.y + report.dy, bounds.y), bounds.y + bounds.h - 1)
        for kind, button in plan(self.buttons, report):
            self._post(self.cg.CGEventCreateMouseEvent(None, kind, where, button))
        self.buttons = (report.left, report.right)
        if scroll_lines:
            self._post(self.cg.CGEventCreateScrollWheelEvent2(None, SCROLL_UNIT_LINE, 1, scroll_lines, 0, 0))

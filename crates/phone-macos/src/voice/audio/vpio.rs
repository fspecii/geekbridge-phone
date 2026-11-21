// SPDX-License-Identifier: GPL-3.0-or-later
//! The real audio device: the system's voice-processing unit (`native/vpio.m`).

use super::bridge::{AudioDevice, CaptureFn, RenderFn, Running};
use std::ffi::{c_char, c_void};

type CaptureCb = extern "C" fn(*mut c_void, *const i16, u32);
type RenderCb = extern "C" fn(*mut c_void, *mut i16, u32);

extern "C" {
    fn gbp_vpio_start(capture: CaptureCb, render: RenderCb, user: *mut c_void, actual: *mut u32, err: *mut c_char, en: usize) -> *mut c_void;
    fn gbp_vpio_stop(handle: *mut c_void);
}

struct Callbacks {
    capture: CaptureFn,
    render: RenderFn,
}

extern "C" fn capture_trampoline(user: *mut c_void, samples: *const i16, count: u32) {
    // SAFETY: `user` is the boxed Callbacks, alive until the unit is stopped.
    let cb = unsafe { &mut *(user as *mut Callbacks) };
    let data = unsafe { std::slice::from_raw_parts(samples, count as usize) };
    (cb.capture)(data);
}

extern "C" fn render_trampoline(user: *mut c_void, samples: *mut i16, count: u32) {
    // SAFETY: as above; `samples` holds `count` writable samples.
    let cb = unsafe { &mut *(user as *mut Callbacks) };
    let data = unsafe { std::slice::from_raw_parts_mut(samples, count as usize) };
    (cb.render)(data);
}

struct Unit {
    handle: *mut c_void,
    callbacks: *mut Callbacks,
}

// SAFETY: the handle is only used to stop the unit; its callbacks are `Send`.
unsafe impl Send for Unit {}
impl Running for Unit {}

impl Drop for Unit {
    fn drop(&mut self) {
        // SAFETY: stopping first guarantees no callback runs while the callbacks are freed.
        unsafe {
            gbp_vpio_stop(self.handle);
            drop(Box::from_raw(self.callbacks));
        }
    }
}

pub struct VoiceProcessingDevice;

impl AudioDevice for VoiceProcessingDevice {
    fn start(&self, capture: CaptureFn, render: RenderFn) -> Result<(u32, bool, Box<dyn Running>), String> {
        let callbacks = Box::into_raw(Box::new(Callbacks { capture, render }));
        let mut rate = 0u32;
        let mut err = [0 as c_char; 160];
        // SAFETY: valid callbacks and buffers; the handle is freed by `Unit::drop`.
        let handle = unsafe { gbp_vpio_start(capture_trampoline, render_trampoline, callbacks.cast(), &mut rate, err.as_mut_ptr(), err.len()) };
        if handle.is_null() {
            // SAFETY: the unit did not start, so nothing else holds the callbacks.
            unsafe { drop(Box::from_raw(callbacks)) };
            let text = unsafe { std::ffi::CStr::from_ptr(err.as_ptr()) }.to_string_lossy().into_owned();
            return Err(text);
        }
        // The voice-processing unit always cancels echo.
        Ok((rate, true, Box::new(Unit { handle, callbacks })))
    }
}

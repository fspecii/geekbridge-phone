// SPDX-License-Identifier: GPL-3.0-or-later
//! Reading and setting the Mac's default sound devices (CoreAudio's hardware object).

use super::routing::{self, AudioDev, Kind};
use std::ffi::c_void;

#[repr(C)]
struct PropertyAddress {
    selector: u32,
    scope: u32,
    element: u32,
}

#[link(name = "CoreAudio", kind = "framework")]
extern "C" {
    fn AudioObjectGetPropertyDataSize(
        object: u32,
        address: *const PropertyAddress,
        qualifier_size: u32,
        qualifier: *const c_void,
        size: *mut u32,
    ) -> i32;
    fn AudioObjectGetPropertyData(
        object: u32,
        address: *const PropertyAddress,
        qualifier_size: u32,
        qualifier: *const c_void,
        size: *mut u32,
        data: *mut c_void,
    ) -> i32;
    fn AudioObjectSetPropertyData(
        object: u32,
        address: *const PropertyAddress,
        qualifier_size: u32,
        qualifier: *const c_void,
        size: u32,
        data: *const c_void,
    ) -> i32;
}

const SYSTEM: u32 = 1;
const fn code(text: &[u8; 4]) -> u32 {
    u32::from_be_bytes(*text)
}
const GLOBAL: u32 = code(b"glob");
const INPUT: u32 = code(b"inpt");
const OUTPUT: u32 = code(b"outp");
const DEFAULT_INPUT: u32 = code(b"dIn ");
const DEFAULT_OUTPUT: u32 = code(b"dOut");
const DEVICES: u32 = code(b"dev#");
const TRANSPORT: u32 = code(b"tran");
const STREAM_CONFIGURATION: u32 = code(b"slay");

fn read(object: u32, selector: u32, scope: u32) -> Option<Vec<u8>> {
    let address = PropertyAddress {
        selector,
        scope,
        element: 0,
    };
    let mut size = 0u32;
    // SAFETY: a valid address and out-pointer; CoreAudio fills `size`.
    if unsafe { AudioObjectGetPropertyDataSize(object, &address, 0, std::ptr::null(), &mut size) }
        != 0
    {
        return None;
    }
    let mut data = vec![0u8; size as usize];
    // SAFETY: `data` is valid for `size` bytes.
    if unsafe {
        AudioObjectGetPropertyData(
            object,
            &address,
            0,
            std::ptr::null(),
            &mut size,
            data.as_mut_ptr().cast(),
        )
    } != 0
    {
        return None;
    }
    data.truncate(size as usize);
    Some(data)
}

fn word(data: &[u8]) -> Option<u32> {
    Some(u32::from_ne_bytes(data.get(..4)?.try_into().ok()?))
}

/// Channels in an `AudioBufferList`: a count, then per buffer a channel count, a size and a pointer.
fn channels(object: u32, scope: u32) -> u32 {
    let Some(list) = read(object, STREAM_CONFIGURATION, scope) else {
        return 0;
    };
    let buffers = word(&list).unwrap_or(0) as usize;
    (0..buffers)
        .filter_map(|i| word(list.get(8 + i * 16..)?))
        .sum()
}

pub fn devices() -> Vec<AudioDev> {
    let Some(list) = read(SYSTEM, DEVICES, GLOBAL) else {
        return Vec::new();
    };
    list.chunks_exact(4)
        .filter_map(|c| {
            let id = word(c)?;
            let transport = read(id, TRANSPORT, GLOBAL)
                .and_then(|t| word(&t))
                .unwrap_or(0);
            Some(AudioDev {
                id,
                kind: Kind::from_transport(transport),
                inputs: channels(id, INPUT),
                outputs: channels(id, OUTPUT),
            })
        })
        .collect()
}

pub fn default_input() -> Option<u32> {
    word(&read(SYSTEM, DEFAULT_INPUT, GLOBAL)?)
}

pub fn default_output() -> Option<u32> {
    word(&read(SYSTEM, DEFAULT_OUTPUT, GLOBAL)?)
}

fn set(selector: u32, device: u32) -> bool {
    let address = PropertyAddress {
        selector,
        scope: GLOBAL,
        element: 0,
    };
    // SAFETY: a valid address and a 4-byte value.
    unsafe {
        AudioObjectSetPropertyData(
            SYSTEM,
            &address,
            0,
            std::ptr::null(),
            4,
            (&device as *const u32).cast(),
        ) == 0
    }
}

/// Moves the default sound input and output for the pass-through, and puts them back.
#[derive(Default)]
pub struct Routes {
    original_input: Option<u32>,
    original_output: Option<u32>,
}

impl Routes {
    /// Call about once a second while the pass-through runs. True if it moved the input.
    pub fn keep_input_off_bluetooth(&mut self) -> bool {
        let (Some(current), devices) = (default_input(), devices()) else {
            return false;
        };
        match routing::input_off_bluetooth(current, &devices) {
            Some(target) if set(DEFAULT_INPUT, target) => {
                self.original_input.get_or_insert(current);
                true
            }
            _ => false,
        }
    }

    /// For the length of a call: plays on connected Bluetooth headphones. True if it moved the output.
    pub fn use_headphones(&mut self) -> bool {
        let (Some(current), devices) = (default_output(), devices()) else {
            return false;
        };
        match routing::headphone_output(current, &devices) {
            Some(target) if set(DEFAULT_OUTPUT, target) => {
                self.original_output = Some(current);
                true
            }
            _ => false,
        }
    }

    pub fn restore_output(&mut self) {
        if let Some(original) = self.original_output.take() {
            set(DEFAULT_OUTPUT, original);
        }
    }

    pub fn restore(&mut self) {
        self.restore_output();
        if let Some(original) = self.original_input.take() {
            set(DEFAULT_INPUT, original);
        }
    }
}

impl Drop for Routes {
    fn drop(&mut self) {
        self.restore();
    }
}

impl super::sound::Routing for Routes {
    fn keep_input_off_bluetooth(&mut self) -> bool {
        Routes::keep_input_off_bluetooth(self)
    }
    fn use_headphones(&mut self) -> bool {
        Routes::use_headphones(self)
    }
    fn restore_output(&mut self) {
        Routes::restore_output(self)
    }
    fn restore(&mut self) {
        Routes::restore(self)
    }
}

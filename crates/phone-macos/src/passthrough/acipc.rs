// SPDX-License-Identifier: GPL-3.0-or-later
//! The built-in controller through Apple's internal channels (`native/ai-caller`, MIT, from
//! the ai-caller project). Three channels carry commands and events, data and voice.
//!
//! The channels have one owner at a time. While `bluetoothd` holds them, opening fails with
//! `EBUSY` (16). They are free for a moment between the old daemon exiting and the new one
//! resetting the chip, which is why [`Acipc::open_within`] polls so fast.

use super::controller::Controller;
use std::ffi::c_int;
use std::io;
use std::time::{Duration, Instant};

extern "C" {
    fn acipc_open() -> c_int;
    fn acipc_close();
    fn acipc_send(kind: u8, bytes: *const u8, length: usize) -> c_int;
    fn acipc_recv(kind: *mut u8, bytes: *mut u8, capacity: usize, timeout_ms: c_int) -> c_int;
}

pub struct Acipc {
    buffer: Vec<u8>,
}

impl Acipc {
    /// Opens the channels, trying every few milliseconds until `limit` is up. Returns the
    /// controller and how many tries it took, or the last error.
    pub fn open_within(limit: Duration, poll: Duration) -> Result<(Self, u32), io::Error> {
        let end = Instant::now() + limit;
        let mut tries = 0;
        loop {
            tries += 1;
            // SAFETY: no other thread uses the bridge; open is called before any send or recv.
            let status = unsafe { acipc_open() };
            if status == 0 {
                return Ok((
                    Self {
                        buffer: vec![0; 4096],
                    },
                    tries,
                ));
            }
            if Instant::now() >= end {
                return Err(io::Error::from_raw_os_error(-status));
            }
            std::thread::sleep(poll);
        }
    }
}

impl Controller for Acipc {
    fn send(&mut self, packet: &[u8]) -> io::Result<()> {
        let (&kind, rest) = packet
            .split_first()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "empty packet"))?;
        // SAFETY: `rest` is valid for its length; one sender thread.
        let status = unsafe { acipc_send(kind, rest.as_ptr(), rest.len()) };
        if status == 0 {
            Ok(())
        } else {
            Err(io::Error::from_raw_os_error(-status))
        }
    }

    fn recv(&mut self, wait_ms: i32) -> io::Result<Option<Vec<u8>>> {
        let mut kind = 0u8;
        // SAFETY: the buffer is valid for its length; one receiver thread.
        let count = unsafe {
            acipc_recv(
                &mut kind,
                self.buffer.as_mut_ptr(),
                self.buffer.len(),
                wait_ms,
            )
        };
        match count {
            0 => Ok(None),
            n if n < 0 => Err(io::Error::from_raw_os_error(-n)),
            n => {
                let mut packet = Vec::with_capacity(n as usize + 1);
                packet.push(kind);
                packet.extend_from_slice(&self.buffer[..n as usize]);
                Ok(Some(packet))
            }
        }
    }
}

impl Drop for Acipc {
    fn drop(&mut self) {
        // SAFETY: the pump has stopped; nothing uses the channels any more.
        unsafe { acipc_close() }
    }
}

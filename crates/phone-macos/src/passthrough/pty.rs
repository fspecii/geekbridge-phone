// SPDX-License-Identifier: GPL-3.0-or-later
//! The pseudo-terminal the daemon is pointed at.

use std::fs::File;
use std::io;
use std::os::fd::{FromRawFd, OwnedFd};

pub struct Pty {
    /// Our end: the pump reads what the daemon writes and writes what the controller sends.
    pub master: File,
    /// The daemon's end, kept open here so the port stays there until it opens it.
    pub _slave: OwnedFd,
    pub path: String,
}

pub fn open() -> io::Result<Pty> {
    let (mut master, mut slave) = (0, 0);
    // SAFETY: valid out-pointers; the null pointers ask for no name, settings or size.
    let status = unsafe {
        libc::openpty(
            &mut master,
            &mut slave,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    };
    if status != 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: both descriptors were just created and belong to nobody else.
    let (master, slave) = unsafe { (File::from_raw_fd(master), OwnedFd::from_raw_fd(slave)) };
    // Raw bytes, no echo, no line editing; and no waiting for a modem carrier.
    // SAFETY: `slave` is a terminal; the structure is filled by tcgetattr before use.
    unsafe {
        let mut attributes: libc::termios = std::mem::zeroed();
        use std::os::fd::AsRawFd;
        if libc::tcgetattr(slave.as_raw_fd(), &mut attributes) == 0 {
            libc::cfmakeraw(&mut attributes);
            attributes.c_cflag |= libc::CLOCAL;
            libc::tcsetattr(slave.as_raw_fd(), libc::TCSANOW, &attributes);
        }
    }
    let path = {
        use std::os::fd::AsRawFd;
        // SAFETY: ttyname returns a NUL-terminated string valid until the next call.
        let name = unsafe { libc::ttyname(slave.as_raw_fd()) };
        if name.is_null() {
            return Err(io::Error::last_os_error());
        }
        unsafe { std::ffi::CStr::from_ptr(name) }
            .to_string_lossy()
            .into_owned()
    };
    Ok(Pty {
        master,
        _slave: slave,
        path,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};

    #[test]
    fn a_port_is_made_and_bytes_pass_through_it_unchanged() {
        let pty = open().unwrap();
        assert!(pty.path.starts_with("/dev/tty"), "{}", pty.path);
        let mut master = pty.master;
        let mut slave = File::from(pty._slave);
        let packet = [1u8, 0x03, 0x0C, 0, 0x0A, 0x0D, 0x1B, 0x03];
        slave.write_all(&packet).unwrap();
        let mut got = [0u8; 8];
        master.read_exact(&mut got).unwrap();
        assert_eq!(
            got, packet,
            "no echo, no line editing: carriage returns and escapes survive"
        );
    }
}

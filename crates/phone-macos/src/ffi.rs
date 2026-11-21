// SPDX-License-Identifier: GPL-3.0-or-later
//! Safe wrappers over the C functions in `native/iobt.m`.

use phone_core::platform::{PlatformError, Result, Stream};
use std::ffi::{c_char, c_int, c_uint, CStr, CString};
use std::fs::File;
use std::os::fd::{FromRawFd, OwnedFd, RawFd};

extern "C" {
    fn gbp_connect(addr: *const c_char);
    fn gbp_unpair(addr: *const c_char, err: *mut c_char, en: usize) -> c_int;
    fn gbp_scan(seconds: c_uint, out: *mut c_char, n: usize, err: *mut c_char, en: usize) -> c_int;
    fn gbp_pair(
        addr: *const c_char,
        cb: extern "C" fn(*mut std::ffi::c_void, *const c_char),
        user: *mut std::ffi::c_void,
        err: *mut c_char,
        en: usize,
    ) -> c_int;
    fn gbp_paired(out: *mut c_char, n: usize) -> c_int;
    fn gbp_open_service(
        addr: *const c_char,
        uuid16: c_uint,
        no_service: *mut c_int,
        err: *mut c_char,
        en: usize,
    ) -> c_int;
    fn gbp_hf_state(
        addr: *const c_char,
        out: *mut c_char,
        n: usize,
        err: *mut c_char,
        en: usize,
    ) -> c_int;
    fn gbp_hf_cmd(
        addr: *const c_char,
        cmd: c_char,
        arg: *const c_char,
        err: *mut c_char,
        en: usize,
    ) -> c_int;
}

/// How long one read or write on a service stream may stall before the link counts as dead.
const IO_TIMEOUT_SECS: libc::time_t = 12;

fn text(buf: &[u8]) -> String {
    CStr::from_bytes_until_nul(buf)
        .map(|c| c.to_string_lossy().into_owned())
        .unwrap_or_default()
}

fn c(s: &str) -> Result<CString> {
    CString::new(s).map_err(|_| PlatformError::Other("text contains a NUL byte".into()))
}

pub fn paired() -> String {
    let mut buf = vec![0u8; 64 * 1024];
    // SAFETY: the buffer is valid for its length and the callee NUL-terminates within it.
    unsafe { gbp_paired(buf.as_mut_ptr().cast(), buf.len()) };
    text(&buf)
}

fn timeouts(fd: RawFd) -> std::io::Result<()> {
    let tv = libc::timeval {
        tv_sec: IO_TIMEOUT_SECS,
        tv_usec: 0,
    };
    for opt in [libc::SO_RCVTIMEO, libc::SO_SNDTIMEO] {
        // SAFETY: `fd` is an open socket and `tv` outlives the call.
        let r = unsafe {
            libc::setsockopt(
                fd,
                libc::SOL_SOCKET,
                opt,
                (&tv as *const libc::timeval).cast(),
                std::mem::size_of::<libc::timeval>() as libc::socklen_t,
            )
        };
        if r < 0 {
            return Err(std::io::Error::last_os_error());
        }
    }
    Ok(())
}

/// Looks up the service with class `uuid16` on the phone and opens it.
pub fn open_service(address: &str, uuid16: u16) -> Result<Box<dyn Stream>> {
    // One attempt only: repeated tries make the phone show a pairing prompt each time.
    open_service_once(address, uuid16)
}

fn open_service_once(address: &str, uuid16: u16) -> Result<Box<dyn Stream>> {
    let addr = c(address)?;
    let mut err = vec![0u8; 256];
    let mut no_service: c_int = 0;
    // SAFETY: all pointers are valid for the lengths given.
    let fd = unsafe {
        gbp_open_service(
            addr.as_ptr(),
            uuid16 as c_uint,
            &mut no_service,
            err.as_mut_ptr().cast(),
            err.len(),
        )
    };
    if fd < 0 {
        return Err(if no_service != 0 {
            PlatformError::NoService
        } else {
            PlatformError::Other(text(&err))
        });
    }
    // SAFETY: the shim hands over ownership of this socket fd.
    let owned = unsafe { OwnedFd::from_raw_fd(fd) };
    timeouts(fd)?;
    Ok(Box::new(File::from(owned)))
}

pub fn hf_state(address: &str) -> Result<String> {
    let addr = c(address)?;
    let (mut out, mut err) = (vec![0u8; 512], vec![0u8; 256]);
    // SAFETY: all pointers are valid for the lengths given.
    let r = unsafe {
        gbp_hf_state(
            addr.as_ptr(),
            out.as_mut_ptr().cast(),
            out.len(),
            err.as_mut_ptr().cast(),
            err.len(),
        )
    };
    if r < 0 {
        return Err(PlatformError::Other(text(&err)));
    }
    Ok(text(&out))
}

/// `cmd`: `d` dial, `a` answer, `e` end, `t` tones, `s` voice to this Mac.
pub fn hf_cmd(address: &str, cmd: u8, arg: &str) -> Result<()> {
    let (addr, arg) = (c(address)?, c(arg)?);
    let mut err = vec![0u8; 256];
    // SAFETY: all pointers are valid for the lengths given.
    let r = unsafe {
        gbp_hf_cmd(
            addr.as_ptr(),
            cmd as c_char,
            arg.as_ptr(),
            err.as_mut_ptr().cast(),
            err.len(),
        )
    };
    if r < 0 {
        return Err(PlatformError::Other(text(&err)));
    }
    Ok(())
}

/// One Bluetooth search of `seconds` seconds. Only called when the user presses Search.
pub fn scan(seconds: u32) -> Result<String> {
    let (mut out, mut err) = (vec![0u8; 16 * 1024], vec![0u8; 256]);
    // SAFETY: all pointers are valid for the lengths given.
    let r = unsafe {
        gbp_scan(
            seconds,
            out.as_mut_ptr().cast(),
            out.len(),
            err.as_mut_ptr().cast(),
            err.len(),
        )
    };
    if r < 0 {
        return Err(PlatformError::Other(text(&err)));
    }
    Ok(text(&out))
}

type ShowCode = std::sync::Arc<dyn Fn(Option<String>) + Send + Sync>;

extern "C" fn code_trampoline(user: *mut std::ffi::c_void, code: *const c_char) {
    // SAFETY: `user` is the `ShowCode` that `pair` keeps alive for the whole blocking call, and
    // `code` is a NUL-terminated string valid for this call.
    let show = unsafe { &*(user as *const ShowCode) };
    let code = unsafe { CStr::from_ptr(code) }.to_string_lossy().into_owned();
    show(Some(code));
}

/// Pairs once with the device at `address`; blocks until it ends.
pub fn pair(address: &str, show_code: ShowCode) -> Result<()> {
    let addr = c(address)?;
    let mut err = vec![0u8; 256];
    // SAFETY: `show_code` outlives the call, which does not return until pairing has ended.
    let r = unsafe {
        gbp_pair(
            addr.as_ptr(),
            code_trampoline,
            (&show_code as *const ShowCode).cast_mut().cast(),
            err.as_mut_ptr().cast(),
            err.len(),
        )
    };
    if r < 0 {
        return Err(PlatformError::Other(text(&err)));
    }
    Ok(())
}

/// Removes the pairing with the phone at `address`.
pub fn unpair(address: &str) -> Result<()> {
    let addr = c(address)?;
    let mut err = vec![0u8; 256];
    // SAFETY: all pointers are valid for the lengths given.
    let r = unsafe { gbp_unpair(addr.as_ptr(), err.as_mut_ptr().cast(), err.len()) };
    if r < 0 {
        return Err(PlatformError::Other(text(&err)));
    }
    Ok(())
}

/// Starts the Bluetooth link to a paired phone and returns at once.
pub fn connect(address: &str) {
    if let Ok(addr) = c(address) {
        // SAFETY: `addr` is a valid NUL-terminated string for the duration of the call.
        unsafe { gbp_connect(addr.as_ptr()) };
    }
}

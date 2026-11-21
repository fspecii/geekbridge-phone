// SPDX-License-Identifier: GPL-3.0-or-later
//! Receiving (and, for tests, sending) file descriptors over a Unix socket (SCM_RIGHTS).
//!
//! The helper hands the app its contacts and messages channels as descriptors; the app's
//! existing OBEX code reads and writes them as ordinary streams.

use std::io;
use std::os::unix::io::RawFd;

/// Room for several descriptors in one message.
const CONTROL_BYTES: usize = 128;

/// Reads from `fd` and returns the bytes read and any descriptors that arrived with them.
/// Received descriptors are marked close-on-exec. Blocks as the socket's own timeout allows.
pub fn recv_with_fds(fd: RawFd, buf: &mut [u8]) -> io::Result<(usize, Vec<RawFd>)> {
    let mut iov = libc::iovec {
        iov_base: buf.as_mut_ptr().cast(),
        iov_len: buf.len(),
    };
    // u64 keeps the control buffer aligned for cmsghdr.
    let mut control = [0u64; CONTROL_BYTES / 8];
    // SAFETY: zeroed msghdr is a valid empty message; the pointers set below outlive the call.
    let mut msg: libc::msghdr = unsafe { std::mem::zeroed() };
    msg.msg_iov = &mut iov;
    msg.msg_iovlen = 1;
    msg.msg_control = control.as_mut_ptr().cast();
    msg.msg_controllen = CONTROL_BYTES as _;
    // SAFETY: `msg` points at valid buffers for the whole call.
    let n = unsafe { libc::recvmsg(fd, &mut msg, 0) };
    if n < 0 {
        return Err(io::Error::last_os_error());
    }
    let mut fds = Vec::new();
    // SAFETY: walking the control messages the kernel just wrote into `control`.
    unsafe {
        let mut cmsg = libc::CMSG_FIRSTHDR(&msg);
        while !cmsg.is_null() {
            if (*cmsg).cmsg_level == libc::SOL_SOCKET && (*cmsg).cmsg_type == libc::SCM_RIGHTS {
                let data = libc::CMSG_DATA(cmsg) as *const RawFd;
                let bytes = (*cmsg).cmsg_len as usize - libc::CMSG_LEN(0) as usize;
                for i in 0..bytes / std::mem::size_of::<RawFd>() {
                    let received = std::ptr::read_unaligned(data.add(i));
                    libc::fcntl(received, libc::F_SETFD, libc::FD_CLOEXEC);
                    fds.push(received);
                }
            }
            cmsg = libc::CMSG_NXTHDR(&msg, cmsg);
        }
    }
    if msg.msg_flags & libc::MSG_CTRUNC != 0 {
        for fd in fds {
            // SAFETY: closing descriptors we just received.
            unsafe { libc::close(fd) };
        }
        return Err(io::Error::other("descriptor data was cut off"));
    }
    Ok((n as usize, fds))
}

/// Sends `data` on `fd` with one descriptor attached. Used by the tests' fake helper.
#[cfg(test)]
pub fn send_with_fd(fd: RawFd, data: &[u8], attached: RawFd) -> io::Result<()> {
    let mut iov = libc::iovec {
        iov_base: data.as_ptr() as *mut _,
        iov_len: data.len(),
    };
    let mut control = [0u64; CONTROL_BYTES / 8];
    // SAFETY: as in `recv_with_fds`; CMSG_* fill the control buffer for exactly one descriptor.
    unsafe {
        let mut msg: libc::msghdr = std::mem::zeroed();
        msg.msg_iov = &mut iov;
        msg.msg_iovlen = 1;
        msg.msg_control = control.as_mut_ptr().cast();
        msg.msg_controllen = libc::CMSG_SPACE(std::mem::size_of::<RawFd>() as u32) as _;
        let cmsg = libc::CMSG_FIRSTHDR(&msg);
        (*cmsg).cmsg_level = libc::SOL_SOCKET;
        (*cmsg).cmsg_type = libc::SCM_RIGHTS;
        (*cmsg).cmsg_len = libc::CMSG_LEN(std::mem::size_of::<RawFd>() as u32) as _;
        std::ptr::write_unaligned(libc::CMSG_DATA(cmsg) as *mut RawFd, attached);
        if libc::sendmsg(fd, &msg, 0) < 0 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::os::unix::io::{AsRawFd, FromRawFd, IntoRawFd};
    use std::os::unix::net::UnixStream;

    #[test]
    fn a_descriptor_travels_with_its_bytes_and_works_on_the_other_side() {
        let (a, b) = UnixStream::pair().unwrap();
        let (carried_far, mut carried_near) = UnixStream::pair().unwrap();
        send_with_fd(a.as_raw_fd(), b"hello\n", carried_far.as_raw_fd()).unwrap();
        drop(carried_far);
        let mut buf = [0u8; 64];
        let (n, fds) = recv_with_fds(b.as_raw_fd(), &mut buf).unwrap();
        assert_eq!(&buf[..n], b"hello\n");
        assert_eq!(fds.len(), 1);
        let mut received = unsafe { UnixStream::from_raw_fd(fds[0]) };
        received.write_all(b"ping").unwrap();
        let mut got = [0u8; 4];
        carried_near.read_exact(&mut got).unwrap();
        assert_eq!(&got, b"ping");
    }

    #[test]
    fn plain_bytes_arrive_with_no_descriptors() {
        let (mut a, b) = UnixStream::pair().unwrap();
        a.write_all(b"plain").unwrap();
        let mut buf = [0u8; 16];
        let (n, fds) = recv_with_fds(b.as_raw_fd(), &mut buf).unwrap();
        assert_eq!(&buf[..n], b"plain");
        assert!(fds.is_empty());
    }

    #[test]
    fn a_closed_socket_reads_zero_bytes() {
        let (a, b) = UnixStream::pair().unwrap();
        drop(a);
        let mut buf = [0u8; 16];
        assert_eq!(recv_with_fds(b.as_raw_fd(), &mut buf).unwrap().0, 0);
    }

    #[test]
    fn received_descriptors_are_close_on_exec() {
        let (a, b) = UnixStream::pair().unwrap();
        let (carried, _keep) = UnixStream::pair().unwrap();
        send_with_fd(a.as_raw_fd(), b"x", carried.as_raw_fd()).unwrap();
        let mut buf = [0u8; 4];
        let (_, fds) = recv_with_fds(b.as_raw_fd(), &mut buf).unwrap();
        let flags = unsafe { libc::fcntl(fds[0], libc::F_GETFD) };
        assert!(flags & libc::FD_CLOEXEC != 0);
        drop(unsafe { UnixStream::from_raw_fd(fds[0]) });
        let _ = carried.into_raw_fd();
    }
}

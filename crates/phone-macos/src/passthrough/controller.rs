// SPDX-License-Identifier: GPL-3.0-or-later
//! The real controller, as the pump sees it: whole HCI packets in and out.

use std::io;

/// A Bluetooth controller. A packet is its H4 type byte followed by the packet proper.
pub trait Controller {
    fn send(&mut self, packet: &[u8]) -> io::Result<()>;
    /// The next packet, or `None` if none arrived within `wait_ms`.
    fn recv(&mut self, wait_ms: i32) -> io::Result<Option<Vec<u8>>>;
}

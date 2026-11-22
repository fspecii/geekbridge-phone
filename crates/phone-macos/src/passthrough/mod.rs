// SPDX-License-Identifier: GPL-3.0-or-later
//! Calls on the Mac with system Bluetooth left on.
//!
//! `bluetoothd` can be told to reach the controller through a serial port. We give it a
//! pseudo-terminal and pass every packet between it and the real controller, which we hold
//! through Apple's internal channels. macOS stays the host for the mouse, the headphones and
//! everything else; we keep back only the phone's voice link, which the daemon would decline.

#[cfg(target_os = "macos")]
pub mod acipc;
#[cfg(target_os = "macos")]
pub mod audio;
pub mod controller;
#[cfg(target_os = "macos")]
pub mod coreaudio;
pub mod h4;
pub mod helper;
pub mod policy;
#[cfg(target_os = "macos")]
pub mod pty;
pub mod pump;
pub mod routing;
#[cfg(target_os = "macos")]
mod session;
pub mod sound;
#[cfg(target_os = "macos")]
pub use session::{Config, Passthrough, Status};
pub mod devices;

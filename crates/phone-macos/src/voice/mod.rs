// SPDX-License-Identifier: GPL-3.0-or-later
//! Mac voice mode: the app side of the helper described in `docs/MACOS-STAGE4.md`.
//!
//! The helper (`helper/macos-btd`) runs our own Bluetooth stack on the Mac's built-in controller.
//! This module talks to it over a private Unix socket and presents it to the rest of the app as a
//! [`phone_core::platform::Platform`]. Everything here is plain Unix code, so it is tested without
//! any Bluetooth.

pub mod audio;
mod client;
mod fdpass;
mod lifecycle;
mod platform;

pub use audio::{AudioDevice, AudioStats, CallAudio, CallAudioController};
pub use client::{ClientError, Event, HelperClient, Reply};
pub use platform::VoiceModePlatform;
pub use lifecycle::{guard_main, recover_on_start, run_guard, Power, SystemPower, VoiceMode, VoiceModeConfig, VoiceModeError};

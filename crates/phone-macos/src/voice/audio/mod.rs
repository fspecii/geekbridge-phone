// SPDX-License-Identifier: GPL-3.0-or-later
//! Call audio on the Mac: the helper's voice socket joined to the built-in speakers and
//! microphone, with echo cancellation from the system's voice-processing unit.

pub mod bridge;
pub mod controller;
pub mod pcm;
#[cfg(target_os = "macos")]
pub mod vpio;

pub use bridge::{AudioDevice, AudioStats, CallAudio};
pub use controller::CallAudioController;

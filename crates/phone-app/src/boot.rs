// SPDX-License-Identifier: GPL-3.0-or-later
//! Choosing the phone platform and starting the pieces: the service, the event thread and the
//! single-instance listener.

use phone_core::calls::CallSnapshot;
use phone_core::platform::{PhoneInfo, Platform, PlatformError, Result, Stream};
use std::sync::Arc;

/// The platform used when the real one cannot start (no Bluetooth service, no D-Bus). Every
/// call reports why, so the user sees one clear banner instead of a window that does nothing.
pub struct UnavailablePlatform {
    reason: String,
}

impl UnavailablePlatform {
    pub fn new(reason: impl Into<String>) -> Self {
        Self {
            reason: reason.into(),
        }
    }

    fn fail<T>(&self) -> Result<T> {
        Err(PlatformError::Other(self.reason.clone()))
    }
}

impl Platform for UnavailablePlatform {
    fn phones(&self) -> Result<Vec<PhoneInfo>> {
        self.fail()
    }
    fn open_map(&self, _: &str) -> Result<Box<dyn Stream>> {
        self.fail()
    }
    fn open_pbap(&self, _: &str) -> Result<Box<dyn Stream>> {
        self.fail()
    }
    fn calls(&self, _: &str) -> Result<Vec<CallSnapshot>> {
        self.fail()
    }
    fn dial(&self, _: &str, _: &str) -> Result<()> {
        self.fail()
    }
    fn answer(&self, _: &str, _: &str) -> Result<()> {
        self.fail()
    }
    fn hang_up(&self, _: &str, _: &str) -> Result<()> {
        self.fail()
    }
    fn hang_up_all(&self, _: &str) -> Result<()> {
        self.fail()
    }
    fn send_tones(&self, _: &str, _: &str) -> Result<()> {
        self.fail()
    }
}

/// The real phone platform for this operating system.
///
pub fn live_platform(
    paths: &phone_core::store::Paths,
    settings: &phone_core::settings::Settings,
) -> Arc<dyn Platform> {
    let _ = (paths, settings);
    #[cfg(target_os = "linux")]
    {
        match phone_linux::LinuxPlatform::new() {
            Ok(p) => Arc::new(p),
            Err(e) => {
                log::error!("Linux Bluetooth platform failed to start: {e}");
                Arc::new(UnavailablePlatform::new(format!(
                    "Bluetooth is not available: {e}"
                )))
            }
        }
    }
    #[cfg(target_os = "windows")]
    {
        Arc::new(phone_windows::WindowsPlatform)
    }
    #[cfg(target_os = "macos")]
    {
        macos_voice::platform(paths, settings)
    }
    #[cfg(not(any(target_os = "linux", target_os = "windows", target_os = "macos")))]
    {
        Arc::new(UnavailablePlatform::new(
            "This system is not supported yet.",
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_unavailable_platform_always_says_why() {
        let p = UnavailablePlatform::new("no bus");
        assert_eq!(p.phones().unwrap_err().to_string(), "no bus");
        assert_eq!(p.dial("a", "1").unwrap_err().to_string(), "no bus");
        assert!(p.open_map("a").is_err());
        assert!(p.calls("a").is_err());
    }
}

/// Stops Mac voice mode, if it runs, and puts system Bluetooth back. Call when the app exits.
pub fn shutdown() {
    #[cfg(target_os = "macos")]
    macos_voice::stop();
}

/// True while Mac voice mode runs (system Bluetooth is off).
pub fn voice_mode_active() -> bool {
    #[cfg(target_os = "macos")]
    {
        macos_voice::active()
    }
    #[cfg(not(target_os = "macos"))]
    {
        false
    }
}

/// Why voice mode is not running although it is switched on (shown in Settings), if so.
pub fn voice_mode_note() -> Option<String> {
    #[cfg(target_os = "macos")]
    {
        macos_voice::note()
    }
    #[cfg(not(target_os = "macos"))]
    {
        None
    }
}

#[cfg(target_os = "macos")]
mod macos_voice {
    use phone_core::platform::Platform;
    use phone_core::settings::Settings;
    use phone_core::store::Paths;
    use phone_macos::passthrough::{devices, helper, Config, Passthrough, Status};
    use std::process::Command;
    use std::sync::{Arc, Mutex, OnceLock};

    static MODE: Mutex<Option<Passthrough>> = Mutex::new(None);
    static NOTE: OnceLock<String> = OnceLock::new();

    pub fn active() -> bool {
        MODE.lock()
            .unwrap_or_else(|e| e.into_inner())
            .as_ref()
            .is_some_and(|p| p.status() == Status::Running)
    }

    pub fn note() -> Option<String> {
        NOTE.get().cloned()
    }

    pub fn stop() {
        if let Some(mut pass) = MODE.lock().unwrap_or_else(|e| e.into_inner()).take() {
            pass.stop();
        }
    }

    /// Starts Mac voice mode first when it is switched on, and only then builds the platform:
    /// this process must not talk to the Bluetooth daemon until the daemon has been restarted,
    /// or what it holds is a session with the old one.
    pub fn platform(_paths: &Paths, settings: &Settings) -> Arc<dyn Platform> {
        if settings.mac_voice_mode {
            match start(settings) {
                Ok(pass) => *MODE.lock().unwrap_or_else(|e| e.into_inner()) = Some(pass),
                Err(message) => {
                    log::error!("Mac voice mode did not start: {message}");
                    let _ = NOTE.set(message);
                }
            }
        }
        Arc::new(phone_macos::MacosPlatform::new())
    }

    /// The paired devices, read by a short-lived copy of this program so that this process stays
    /// away from the Bluetooth daemon until it has been restarted.
    fn paired_devices() -> Result<String, String> {
        let exe = std::env::current_exe().map_err(|e| e.to_string())?;
        let out = Command::new(exe)
            .arg("--macos-paired")
            .output()
            .map_err(|e| e.to_string())?;
        Ok(String::from_utf8_lossy(&out.stdout).into_owned())
    }

    fn start(settings: &Settings) -> Result<Passthrough, String> {
        let helper = helper::Helper::real();
        if helper.state() != helper::State::Ready {
            let installer = helper::installer_path()
                .ok_or("The call helper is not installed with this app.")?;
            helper::install(&installer)?; // macOS asks for an administrator's approval, once
            if helper.state() != helper::State::Ready {
                return Err("The call helper did not install.".into());
            }
        }
        let choice = devices::choose(&paired_devices()?, settings.preferred_phone.as_deref())?;
        Passthrough::start(
            Config {
                phone: choice.phone,
                reconnect: choice.reconnect,
            },
            helper,
        )
    }
}

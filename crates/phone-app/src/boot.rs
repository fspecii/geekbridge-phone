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
pub fn live_platform(paths: &phone_core::store::Paths, settings: &phone_core::settings::Settings) -> Arc<dyn Platform> {
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
    use phone_macos::voice::{recover_on_start, SystemPower, VoiceMode, VoiceModeConfig};
    use std::path::PathBuf;
    use std::sync::{Arc, Mutex, OnceLock};

    static MODE: Mutex<Option<VoiceMode>> = Mutex::new(None);
    static NOTE: OnceLock<String> = OnceLock::new();

    /// Where the helper lives: inside the app bundle when packaged, else `GBP_HELPER_DIR`.
    fn helper_dir() -> Option<PathBuf> {
        if let Some(dir) = std::env::var_os("GBP_HELPER_DIR") {
            return Some(PathBuf::from(dir));
        }
        let exe = std::env::current_exe().ok()?;
        let bundled = exe.parent()?.parent()?.join("Resources/helper/macos-btd");
        bundled.join("btd.py").exists().then_some(bundled)
    }

    fn python_in(helper: &std::path::Path) -> Option<PathBuf> {
        ["python/bin/python3", ".venv/bin/python"]
            .iter()
            .map(|p| helper.join(p))
            .find(|p| p.exists())
    }

    pub fn active() -> bool {
        MODE.lock().unwrap_or_else(|e| e.into_inner()).is_some()
    }

    pub fn note() -> Option<String> {
        NOTE.get().cloned()
    }

    pub fn stop() {
        if let Some(mut mode) = MODE.lock().unwrap_or_else(|e| e.into_inner()).take() {
            mode.disable();
        }
    }

    pub fn platform(paths: &Paths, settings: &Settings) -> Arc<dyn Platform> {
        let marker = paths.data.join("voice-mode-bluetooth-off");
        if recover_on_start(&marker, &SystemPower) {
            log::warn!("the last run ended with Bluetooth switched off; it is on again");
        }
        if settings.mac_voice_mode {
            match start(paths, marker) {
                Ok(platform) => return platform,
                Err(message) => {
                    log::error!("Mac voice mode did not start: {message}");
                    let _ = NOTE.set(message);
                }
            }
        }
        Arc::new(phone_macos::MacosPlatform::new())
    }

    fn start(paths: &Paths, marker: PathBuf) -> Result<Arc<dyn Platform>, String> {
        let helper = helper_dir().ok_or("The voice helper is not installed with this app.")?;
        let python = python_in(&helper).ok_or("The voice helper has no Python runtime.")?;
        let exe = std::env::current_exe().map_err(|e| e.to_string())?;
        let _ = &paths;
        let config = VoiceModeConfig {
            python,
            helper_script: helper.join("btd.py"),
            // Unix socket paths are short; keep this under /tmp and private to the user.
            dir: PathBuf::from(format!("/tmp/gbp-{}", unsafe { libc_uid() })),
            marker,
            extra_args: vec![],
            env: vec![],
            guard: Some(vec![exe.to_string_lossy().into_owned(), "--macos-btguard".into()]),
            power: Arc::new(SystemPower),
            audio: Some(Arc::new(phone_macos::voice::audio::vpio::VoiceProcessingDevice)),
        };
        let mode = VoiceMode::enable(config).map_err(|e| e.to_string())?;
        let platform: Arc<dyn Platform> = mode.platform();
        *MODE.lock().unwrap_or_else(|e| e.into_inner()) = Some(mode);
        Ok(platform)
    }

    extern "C" {
        #[link_name = "getuid"]
        fn libc_uid() -> u32;
    }
}

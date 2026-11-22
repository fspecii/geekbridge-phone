// SPDX-License-Identifier: GPL-3.0-or-later
//! The macOS [`Platform`].

use crate::{ffi, parse_calls, parse_phones};
use phone_core::calls::CallSnapshot;
use phone_core::platform::{NearbyDevice, PhoneInfo, Platform, PlatformError, Result, Stream};

/// Bluetooth service classes: Message Access Server and Phonebook Access Server.
const MAP_MAS: u16 = 0x1132;
const PBAP_PSE: u16 = 0x112F;

#[derive(Default)]
pub struct MacosPlatform {
    last_connect: std::sync::Mutex<std::collections::HashMap<String, std::time::Instant>>,
}

impl MacosPlatform {
    pub fn new() -> Self {
        Self::default()
    }

    /// True at most once a minute per phone.
    fn connect_due(&self, address: &str) -> bool {
        let mut m = self.last_connect.lock().unwrap_or_else(|e| e.into_inner());
        let now = std::time::Instant::now();
        match m.get(address) {
            Some(t) if now.duration_since(*t) < std::time::Duration::from_secs(60) => false,
            _ => {
                m.insert(address.to_string(), now);
                true
            }
        }
    }
}

impl MacosPlatform {
    /// Only paired, non-Apple phones may be connected by this app. macOS handles Apple devices
    /// itself (they must be connected in System Settings), and reaching out to a device whose
    /// pairing has gone stale makes it show pairing prompts.
    fn may_connect(&self, address: &str) -> Result<()> {
        let phones = parse_phones(&ffi::paired());
        match phones
            .iter()
            .find(|p| p.address.eq_ignore_ascii_case(address))
        {
            None => Err(PlatformError::Other(
                "that phone is not paired with this Mac".into(),
            )),
            Some(p) if p.apple && !p.connected => Err(PlatformError::Other(
                "connect this Apple device in System Settings > Bluetooth first".into(),
            )),
            Some(_) => Ok(()),
        }
    }
}

impl Platform for MacosPlatform {
    fn phones(&self) -> Result<Vec<PhoneInfo>> {
        let phones = parse_phones(&ffi::paired());
        // Android phones do not connect to a Mac by themselves, so bring the link up for them,
        // at most once a minute each. Apple devices are left to macOS.
        for p in phones.iter().filter(|p| !p.apple && !p.connected) {
            if self.connect_due(&p.address) {
                ffi::connect(&p.address);
            }
        }
        Ok(phones)
    }
    fn computer_name(&self) -> Option<String> {
        let out = std::process::Command::new("scutil")
            .args(["--get", "ComputerName"])
            .output()
            .ok()?;
        let name = String::from_utf8(out.stdout).ok()?.trim().to_string();
        (!name.is_empty()).then_some(name)
    }
    fn open_map(&self, address: &str) -> Result<Box<dyn Stream>> {
        self.may_connect(address)?;
        ffi::open_service(address, MAP_MAS)
    }
    fn open_pbap(&self, address: &str) -> Result<Box<dyn Stream>> {
        self.may_connect(address)?;
        ffi::open_service(address, PBAP_PSE)
    }
    fn calls(&self, address: &str) -> Result<Vec<CallSnapshot>> {
        self.may_connect(address)?;
        Ok(parse_calls(&ffi::hf_state(address)?))
    }
    fn dial(&self, address: &str, number: &str) -> Result<()> {
        self.may_connect(address)?;
        ffi::hf_cmd(address, b'd', number)
    }
    fn answer(&self, address: &str, _call_id: &str) -> Result<()> {
        self.may_connect(address)?;
        ffi::hf_cmd(address, b'a', "")
    }
    fn hang_up(&self, address: &str, _call_id: &str) -> Result<()> {
        ffi::hf_cmd(address, b'e', "")
    }
    fn hang_up_all(&self, address: &str) -> Result<()> {
        ffi::hf_cmd(address, b'e', "")
    }
    fn send_tones(&self, address: &str, tones: &str) -> Result<()> {
        self.may_connect(address)?;
        ffi::hf_cmd(address, b't', tones)
    }
    fn activate_audio(&self, address: &str) -> Result<()> {
        ffi::hf_cmd(address, b's', "")
    }
    fn can_pair(&self) -> bool {
        true
    }
    fn scan_nearby(&self, seconds: u32) -> Result<Vec<NearbyDevice>> {
        Ok(crate::parse_nearby(&ffi::scan(seconds)?))
    }
    fn pair(
        &self,
        id: &str,
        show_code: std::sync::Arc<dyn Fn(Option<String>) + Send + Sync>,
    ) -> Result<()> {
        ffi::pair(id, show_code)
    }
    fn unpair(&self, address: &str) -> Result<()> {
        ffi::unpair(address)
    }
}

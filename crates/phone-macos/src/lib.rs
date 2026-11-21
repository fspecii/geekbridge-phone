// SPDX-License-Identifier: GPL-3.0-or-later
//! macOS platform layer: paired phones, the message and phonebook services over RFCOMM, and
//! hands-free calls, all through IOBluetooth (see `native/iobt.m`).
//!
//! macOS plays the part of the car: it opens MAP and PBAP on the phone like any other
//! client, and runs the hands-free unit role for calls. Pairing is done in System Settings.

#[cfg(unix)]
pub mod voice;

#[cfg(target_os = "macos")]
mod ffi;
#[cfg(target_os = "macos")]
mod platform;

#[cfg(target_os = "macos")]
pub use platform::MacosPlatform;

/// Parses the line format `gbp_paired` writes (`ADDRESS \t NAME \t CONNECTED \t CLASS`).
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub(crate) fn parse_phones(text: &str) -> Vec<phone_core::platform::PhoneInfo> {
    // Bluetooth major device class 2 is "phone"; 0 is "miscellaneous", which iPhones can report.
    const MAJOR_PHONE: u32 = 2;
    let mut out: Vec<_> = text
        .lines()
        .filter_map(|line| {
            let mut f = line.split('\t');
            let address = f.next()?.trim().to_string();
            let name = f.next()?.to_string();
            let connected = f.next()? == "1";
            let major: u32 = f.next()?.trim().parse().ok()?;
            let looks_apple = {
                let n = name.to_ascii_lowercase();
                n.contains("iphone") || n.contains("ipad")
            };
            if major != MAJOR_PHONE && !looks_apple {
                return None;
            }
            Some(phone_core::platform::PhoneInfo {
                address,
                name,
                connected,
                battery: None,
                apple: looks_apple,
            })
        })
        .collect();
    // macOS can list one device twice (one record per transport), so keep one per address.
    let mut seen = std::collections::HashSet::new();
    out.retain(|p| seen.insert(p.address.to_ascii_uppercase()));
    // Connected first, then Android before Apple devices (macOS drives those itself), then by name.
    out.sort_by(|a, b| {
        b.connected
            .cmp(&a.connected)
            .then_with(|| a.apple.cmp(&b.apple))
            .then_with(|| a.name.cmp(&b.name))
    });
    out
}

/// Parses the search results (`ADDRESS \t NAME` per line).
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub(crate) fn parse_nearby(text: &str) -> Vec<phone_core::platform::NearbyDevice> {
    text.lines()
        .filter_map(|l| {
            let (id, name) = l.split_once('\t')?;
            Some(phone_core::platform::NearbyDevice {
                id: id.trim().to_string(),
                name: name.trim().to_string(),
            })
        })
        .collect()
}

/// Turns the hands-free state line (`ACTIVE \t SETUP \t HOLD \t NUMBER`) into calls.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub(crate) fn parse_calls(text: &str) -> Vec<phone_core::calls::CallSnapshot> {
    use phone_core::calls::{CallSnapshot, CallState};
    let mut f = text.split('\t');
    let active = f.next() == Some("1");
    let setup: u8 = f.next().and_then(|s| s.parse().ok()).unwrap_or(0);
    let hold: u8 = f.next().and_then(|s| s.parse().ok()).unwrap_or(0);
    let number = f.next().unwrap_or("").to_string();
    let name = f.next().filter(|n| !n.is_empty()).map(str::to_string);
    let snap = |id: &str, state| CallSnapshot {
        id: id.to_string(),
        state,
        number: number.clone(),
        name: name.clone(),
    };
    let mut calls = Vec::new();
    if active {
        // Hold state 2 is "all calls held"; 1 is "one held, one active".
        let state = if hold == 2 {
            CallState::Held
        } else {
            CallState::Active
        };
        calls.push(snap("hf-active", state));
    }
    match setup {
        1 => calls.push(snap(
            "hf-setup",
            if active {
                CallState::Waiting
            } else {
                CallState::Incoming
            },
        )),
        2 => calls.push(snap("hf-setup", CallState::Dialing)),
        3 => calls.push(snap("hf-setup", CallState::Alerting)),
        _ => {}
    }
    calls
}

#[cfg(test)]
mod tests {
    use super::*;
    use phone_core::calls::CallState;

    #[test]
    fn phones_are_filtered_and_connected_ones_come_first() {
        let t = "AA:BB:CC:00:00:01\tMy iPhone\t0\t0\n\
                 AA:BB:CC:00:00:02\tPixel\t1\t2\n\
                 AA:BB:CC:00:00:03\tHeadphones\t1\t4\n";
        let p = parse_phones(t);
        assert_eq!(p.len(), 2);
        assert_eq!(p[0].name, "Pixel");
        assert!(!p[0].apple);
        assert!(p[1].apple);
    }

    #[test]
    fn a_device_listed_twice_appears_once_and_android_comes_first() {
        let t = "F4:81:C4:00:00:03\tAlex's iPad\t0\t2\n\
                 6C:AC:C2:00:00:01\tSam's Z Fold5\t0\t2\n\
                 6C:AC:C2:00:00:01\tSam's Z Fold5\t0\t2\n";
        let p = parse_phones(t);
        assert_eq!(p.len(), 2);
        assert_eq!(p[0].name, "Sam's Z Fold5");
    }

    #[test]
    fn search_results_are_parsed() {
        let n = parse_nearby("AA:BB:CC:00:00:09\tPixel 9\nbad line\n");
        assert_eq!(n.len(), 1);
        assert_eq!(n[0].name, "Pixel 9");
    }

    #[test]
    fn hands_free_state_becomes_calls() {
        assert!(parse_calls("0\t0\t0\t").is_empty());
        let c = parse_calls("0\t1\t0\t+4412345");
        assert_eq!(c[0].state, CallState::Incoming);
        assert_eq!(c[0].number, "+4412345");
        assert_eq!(parse_calls("1\t0\t0\t")[0].state, CallState::Active);
        assert_eq!(parse_calls("1\t1\t0\t1")[1].state, CallState::Waiting);
        assert_eq!(parse_calls("0\t3\t0\t1")[0].state, CallState::Alerting);
    }
}

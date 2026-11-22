// SPDX-License-Identifier: GPL-3.0-or-later
//! Which paired devices the pass-through is about, from the paired-device list
//! (`ADDRESS \t NAME \t CONNECTED \t CLASS` per line, the format `gbp_paired` writes).

use crate::parse_phones;

/// Bluetooth major device class 4: audio and video (headphones, headsets, speakers).
const MAJOR_AUDIO: u32 = 4;

pub struct Choice {
    /// The Android phone whose call audio the pass-through keeps.
    pub phone: String,
    /// Audio devices that are connected now. They are brought back after the daemon restarts.
    pub reconnect: Vec<String>,
}

/// `preferred`: the phone the user chose in Settings, if any. Apple devices are never chosen:
/// macOS carries their calls itself.
pub fn choose(paired: &str, preferred: Option<&str>) -> Result<Choice, String> {
    let phones: Vec<_> = parse_phones(paired)
        .into_iter()
        .filter(|p| !p.apple)
        .collect();
    let phone = preferred
        .and_then(|want| phones.iter().find(|p| p.address.eq_ignore_ascii_case(want)))
        .or_else(|| phones.first())
        .ok_or("No Android phone is paired with this Mac. Pair one in System Settings > Bluetooth first.")?;
    let reconnect = paired
        .lines()
        .filter_map(|line| {
            let mut f = line.split('\t');
            let address = f.next()?.trim();
            let _name = f.next()?;
            let connected = f.next()? == "1";
            let major: u32 = f.next()?.trim().parse().ok()?;
            (connected && major == MAJOR_AUDIO && !address.eq_ignore_ascii_case(&phone.address))
                .then(|| address.to_string())
        })
        .collect();
    Ok(Choice {
        phone: phone.address.clone(),
        reconnect,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const LIST: &str = "AA:BB:CC:00:00:01\tMy iPhone\t1\t2\n\
                        6C:AC:C2:00:00:01\tSam's Z Fold5\t0\t2\n\
                        6C:AC:C2:00:00:09\tPixel 9\t1\t2\n\
                        AA:BB:CC:00:00:03\tFreeBuds\t1\t4\n\
                        AA:BB:CC:00:00:04\tOld headset\t0\t4\n\
                        AA:BB:CC:00:00:05\tMagic Mouse\t1\t5\n";

    #[test]
    fn the_connected_android_phone_is_chosen_and_apple_devices_never_are() {
        let c = choose(LIST, None).unwrap();
        assert_eq!(c.phone, "6C:AC:C2:00:00:09");
    }

    #[test]
    fn the_phone_chosen_in_settings_wins_when_it_is_paired() {
        assert_eq!(
            choose(LIST, Some("6c:ac:c2:00:00:01")).unwrap().phone,
            "6C:AC:C2:00:00:01"
        );
        assert_eq!(
            choose(LIST, Some("11:22:33:44:55:66")).unwrap().phone,
            "6C:AC:C2:00:00:09",
            "not paired: the default"
        );
    }

    #[test]
    fn only_connected_audio_devices_are_brought_back() {
        assert_eq!(
            choose(LIST, None).unwrap().reconnect,
            vec!["AA:BB:CC:00:00:03"]
        );
    }

    #[test]
    fn without_an_android_phone_it_says_what_to_do() {
        let only_iphone = "AA:BB:CC:00:00:01\tMy iPhone\t1\t2\n";
        let Err(message) = choose(only_iphone, None) else {
            panic!("an iPhone must not be chosen")
        };
        assert!(message.contains("Pair one in System Settings"));
        assert!(choose("", None).is_err());
    }
}

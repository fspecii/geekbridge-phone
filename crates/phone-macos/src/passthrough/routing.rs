// SPDX-License-Identifier: GPL-3.0-or-later
//! Which sound devices a call uses.
//!
//! Two rules, both learned on a real Mac:
//!
//! * **The sound input stays off Bluetooth headsets.** macOS makes a newly connected headset
//!   the default input, and anything that listens on the default input then makes macOS open a
//!   voice link to the headset. The controller carries one voice link well, and the phone's call
//!   needs it. So while the pass-through runs, the input is moved to a microphone that is not on
//!   Bluetooth, if there is one, and put back afterwards.
//! * **A call plays on Bluetooth headphones when any are connected**, on their music side
//!   (output only), which needs no voice link.
//!
//! The choosing is pure and tested; reading and setting the defaults is CoreAudio (macOS only).

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Kind {
    BuiltIn,
    Usb,
    Bluetooth,
    /// Virtual and aggregate devices: used only when nothing else will do.
    Virtual,
    Other,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct AudioDev {
    pub id: u32,
    pub kind: Kind,
    pub inputs: u32,
    pub outputs: u32,
}

impl Kind {
    /// From CoreAudio's transport type code.
    pub fn from_transport(code: u32) -> Kind {
        match &code.to_be_bytes() {
            b"bltn" => Kind::BuiltIn,
            b"usb " => Kind::Usb,
            b"blue" | b"blea" => Kind::Bluetooth,
            b"virt" | b"grup" => Kind::Virtual,
            _ => Kind::Other,
        }
    }

    fn rank(self) -> u8 {
        match self {
            Kind::BuiltIn => 0,
            Kind::Usb => 1,
            Kind::Other => 2,
            Kind::Virtual => 3,
            Kind::Bluetooth => 4,
        }
    }
}

/// The microphone to use instead of a Bluetooth one, or `None` to leave the input alone.
pub fn input_off_bluetooth(current: u32, devices: &[AudioDev]) -> Option<u32> {
    let now = devices.iter().find(|d| d.id == current)?;
    if now.kind != Kind::Bluetooth {
        return None;
    }
    devices
        .iter()
        .filter(|d| d.inputs > 0 && d.kind != Kind::Bluetooth)
        .min_by_key(|d| d.kind.rank())
        .map(|d| d.id)
}

/// The Bluetooth headphones to play a call on, or `None` to leave the output alone. A device
/// with no input is preferred: that is the headphones' music side.
pub fn headphone_output(current: u32, devices: &[AudioDev]) -> Option<u32> {
    if devices
        .iter()
        .any(|d| d.id == current && d.kind == Kind::Bluetooth)
    {
        return None;
    }
    devices
        .iter()
        .filter(|d| d.kind == Kind::Bluetooth && d.outputs > 0)
        .min_by_key(|d| d.inputs > 0)
        .map(|d| d.id)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dev(id: u32, kind: Kind, inputs: u32, outputs: u32) -> AudioDev {
        AudioDev {
            id,
            kind,
            inputs,
            outputs,
        }
    }

    #[test]
    fn transport_codes_map_to_kinds() {
        let code = |s: &[u8; 4]| u32::from_be_bytes(*s);
        assert_eq!(Kind::from_transport(code(b"blue")), Kind::Bluetooth);
        assert_eq!(Kind::from_transport(code(b"blea")), Kind::Bluetooth);
        assert_eq!(Kind::from_transport(code(b"usb ")), Kind::Usb);
        assert_eq!(Kind::from_transport(code(b"bltn")), Kind::BuiltIn);
        assert_eq!(Kind::from_transport(code(b"virt")), Kind::Virtual);
        assert_eq!(Kind::from_transport(code(b"hdmi")), Kind::Other);
    }

    #[test]
    fn a_bluetooth_input_moves_to_the_best_other_microphone() {
        let devices = [
            dev(95, Kind::Other, 0, 2),
            dev(118, Kind::Bluetooth, 1, 0),
            dev(81, Kind::Virtual, 1, 1),
            dev(91, Kind::Usb, 2, 0),
            dev(75, Kind::BuiltIn, 1, 2),
        ];
        assert_eq!(
            input_off_bluetooth(118, &devices),
            Some(75),
            "built in first"
        );
        assert_eq!(
            input_off_bluetooth(118, &devices[..4]),
            Some(91),
            "then USB, before virtual devices"
        );
        assert_eq!(
            input_off_bluetooth(118, &devices[..3]),
            Some(81),
            "virtual only as a last resort"
        );
    }

    #[test]
    fn the_input_is_left_alone_unless_it_is_bluetooth_and_something_else_exists() {
        let usb = dev(91, Kind::Usb, 2, 0);
        let headset = dev(118, Kind::Bluetooth, 1, 0);
        assert_eq!(input_off_bluetooth(91, &[usb, headset]), None);
        assert_eq!(input_off_bluetooth(118, &[headset]), None);
        assert_eq!(
            input_off_bluetooth(999, &[usb, headset]),
            None,
            "an unknown current device"
        );
    }

    #[test]
    fn calls_play_on_connected_headphones_on_their_music_side() {
        let speakers = dev(75, Kind::BuiltIn, 0, 2);
        let music = dev(112, Kind::Bluetooth, 0, 2);
        let headset = dev(118, Kind::Bluetooth, 1, 1);
        assert_eq!(headphone_output(75, &[speakers, headset, music]), Some(112));
        assert_eq!(headphone_output(75, &[speakers, headset]), Some(118));
        assert_eq!(
            headphone_output(112, &[speakers, music]),
            None,
            "already on them"
        );
        assert_eq!(headphone_output(75, &[speakers]), None, "none connected");
    }
}

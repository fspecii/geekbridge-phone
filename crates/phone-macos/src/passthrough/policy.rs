// SPDX-License-Identifier: GPL-3.0-or-later
//! What the pass-through does with each HCI packet.
//!
//! Almost everything passes untouched in both directions: macOS keeps the mouse, the
//! headphones and every other device as its own. Three things are ours:
//!
//! 1. **The phone's voice link.** macOS's daemon has no hands-free unit and declines the
//!    phone's request for call audio. We accept it ourselves and hide that link from the daemon;
//!    its audio goes to the Mac's sound and back.
//! 2. **High priority for the other links while that call runs.** The controller starves the
//!    headphones' audio link against a reserved voice link unless it is flagged high priority
//!    with the Broadcom vendor command 0xFC57. macOS sends it itself at A2DP start, but not on
//!    this route, where it does not recognise the chip.
//! 3. **Boot commands for a serial chip.** The daemon on a serial path may try to download
//!    firmware; the controller is already running, so those commands are answered locally and
//!    never reach it.
//!
//! Pure logic: no hardware, no clock. Tested without Bluetooth.

use super::h4::{command_complete, COMMAND, EVENT, SCO};
use std::collections::{BTreeSet, VecDeque};

const CONNECTION_COMPLETE: u8 = 0x03;
const DISCONNECTION_COMPLETE: u8 = 0x05;
const CONNECTION_REQUEST: u8 = 0x04;
const COMMAND_COMPLETE: u8 = 0x0E;
const COMMAND_STATUS: u8 = 0x0F;
const SYNCHRONOUS_COMPLETE: u8 = 0x2C;
const ACL_LINK: u8 = 1;
const OP_ACCEPT_SYNC: u16 = 0x0429;
const OP_ACCEPT_ENHANCED_SYNC: u16 = 0x043E;
/// Broadcom vendor command "high priority connection": a handle and an enable byte.
pub const BCM_HIGH_PRIORITY: u16 = 0xFC57;

/// Vendor commands that only make sense while booting a chip over a serial line.
const SERIAL_BOOT_COMMANDS: [u16; 5] = [0xFC18, 0xFC2E, 0xFC45, 0xFC4C, 0xFC4E];

/// A Bluetooth address as it appears in HCI packets (least significant byte first).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Address(pub [u8; 6]);

impl Address {
    /// Parses `AA:BB:CC:DD:EE:FF` (or with dashes).
    pub fn parse(text: &str) -> Option<Self> {
        let mut bytes = [0u8; 6];
        let mut parts = text.split([':', '-']);
        for slot in bytes.iter_mut().rev() {
            *slot = u8::from_str_radix(parts.next()?, 16).ok()?;
        }
        parts.next().is_none().then_some(Self(bytes))
    }
}

/// What to do with a packet the host (the daemon) sent toward the controller.
#[derive(Debug, PartialEq, Eq)]
pub enum FromHost {
    Forward,
    /// Do not send it on; answer the host with this event.
    Answer(Vec<u8>),
}

/// What to do with a packet that came from the controller.
#[derive(Debug, PartialEq, Eq)]
pub enum FromChip {
    Forward,
    /// The host never sees it.
    Drop,
    /// Send this command to the controller; the host never sees the request.
    Accept(Vec<u8>),
    /// A voice packet of the phone's call: its payload (header stripped).
    Voice(Vec<u8>),
}

/// Things worth logging or reacting to.
#[derive(Debug, PartialEq, Eq, Clone)]
pub enum Event {
    PhoneVoiceRequested {
        link_type: u8,
    },
    PhoneVoiceOpened {
        handle: u16,
    },
    PhoneVoiceFailed {
        status: u8,
    },
    PhoneVoiceClosed {
        reason: u8,
    },
    PriorityRequested {
        handle: u16,
        enable: bool,
    },
    PriorityAnswered {
        status: u8,
    },
    /// A classic link came up to this device (the phone, headphones, a mouse...).
    LinkUp {
        address: Address,
    },
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Stats {
    pub to_chip: u64,
    pub to_host: u64,
    pub voice_in: u64,
    pub voice_out: u64,
    pub calls: u64,
}

pub struct Policy {
    phone: Option<Address>,
    flag_other_links: bool,
    voice_pending: bool,
    voice_handle: Option<u16>,
    other_links: BTreeSet<u16>,
    flagged: BTreeSet<u16>,
    own_commands: u32,
    outbox: VecDeque<Vec<u8>>,
    events: VecDeque<Event>,
    pub stats: Stats,
}

impl Policy {
    /// `phone`: the one phone whose call audio we keep. `flag_other_links`: set the other
    /// links high priority while the call runs (always on in the product; off in tests that
    /// check the plain pass-through).
    pub fn new(phone: Option<Address>, flag_other_links: bool) -> Self {
        Self {
            phone,
            flag_other_links,
            voice_pending: false,
            voice_handle: None,
            other_links: BTreeSet::new(),
            flagged: BTreeSet::new(),
            own_commands: 0,
            outbox: VecDeque::new(),
            events: VecDeque::new(),
            stats: Stats::default(),
        }
    }

    pub fn in_call(&self) -> bool {
        self.voice_handle.is_some()
    }

    /// Commands of ours for the controller. The pump sends them between packets.
    pub fn take_outbox(&mut self) -> Vec<Vec<u8>> {
        self.outbox.drain(..).collect()
    }

    pub fn take_events(&mut self) -> Vec<Event> {
        self.events.drain(..).collect()
    }

    pub fn from_host(&mut self, packet: &[u8]) -> FromHost {
        if packet.first() == Some(&COMMAND) && packet.len() >= 3 {
            let opcode = u16::from_le_bytes([packet[1], packet[2]]);
            if SERIAL_BOOT_COMMANDS.contains(&opcode) {
                return FromHost::Answer(command_complete(opcode, 0));
            }
        }
        self.stats.to_chip += 1;
        FromHost::Forward
    }

    pub fn from_chip(&mut self, packet: &[u8]) -> FromChip {
        if self.phone.is_some() {
            if let Some(action) = self.phone_traffic(packet) {
                return action;
            }
        }
        self.watch_links(packet);
        self.stats.to_host += 1;
        FromChip::Forward
    }

    /// Our answer to one phone voice packet: silence of the same size, until the pump swaps in
    /// real sound.
    pub fn silence_for(&mut self, packet: &[u8]) -> Vec<u8> {
        self.stats.voice_out += 1;
        let mut reply = packet.to_vec();
        for byte in reply.iter_mut().skip(4) {
            *byte = 0;
        }
        reply
    }

    fn phone_traffic(&mut self, packet: &[u8]) -> Option<FromChip> {
        let phone = self.phone?;
        match packet.first().copied()? {
            SCO if packet.len() >= 4 => {
                let handle = u16::from_le_bytes([packet[1], packet[2]]) & 0x0FFF;
                if Some(handle) == self.voice_handle {
                    self.stats.voice_in += 1;
                    return Some(FromChip::Voice(packet[4..].to_vec()));
                }
                None
            }
            EVENT if packet.len() >= 3 => self.phone_event(packet, phone),
            _ => None,
        }
    }

    fn phone_event(&mut self, packet: &[u8], phone: Address) -> Option<FromChip> {
        match packet[1] {
            CONNECTION_REQUEST if packet.len() >= 13 => {
                let link_type = packet[12];
                if packet[3..9] != phone.0 || link_type == ACL_LINK {
                    return None;
                }
                if self.voice_handle.is_some() || self.voice_pending {
                    return None; // a second voice link is the daemon's to decline
                }
                self.voice_pending = true;
                self.events
                    .push_back(Event::PhoneVoiceRequested { link_type });
                Some(FromChip::Accept(accept_voice(&phone, link_type)))
            }
            COMMAND_STATUS if packet.len() >= 7 => {
                let opcode = u16::from_le_bytes([packet[5], packet[6]]);
                if self.own_commands > 0 && opcode == BCM_HIGH_PRIORITY {
                    self.own_commands -= 1;
                    self.events
                        .push_back(Event::PriorityAnswered { status: packet[3] });
                    return Some(FromChip::Drop);
                }
                if self.voice_pending
                    && (opcode == OP_ACCEPT_SYNC || opcode == OP_ACCEPT_ENHANCED_SYNC)
                {
                    if packet[3] != 0 {
                        self.voice_pending = false;
                        self.events
                            .push_back(Event::PhoneVoiceFailed { status: packet[3] });
                    }
                    return Some(FromChip::Drop);
                }
                None
            }
            COMMAND_COMPLETE if packet.len() >= 6 => {
                let opcode = u16::from_le_bytes([packet[4], packet[5]]);
                if self.own_commands > 0 && opcode == BCM_HIGH_PRIORITY {
                    self.own_commands -= 1;
                    let status = packet.get(6).copied().unwrap_or(0);
                    self.events.push_back(Event::PriorityAnswered { status });
                    return Some(FromChip::Drop);
                }
                None
            }
            SYNCHRONOUS_COMPLETE | CONNECTION_COMPLETE if packet.len() >= 13 => {
                let is_voice = packet[1] == SYNCHRONOUS_COMPLETE || packet[12] != ACL_LINK;
                if !(self.voice_pending && is_voice && packet[6..12] == phone.0) {
                    return None;
                }
                self.voice_pending = false;
                if packet[3] == 0 {
                    let handle = u16::from_le_bytes([packet[4], packet[5]]) & 0x0FFF;
                    self.voice_handle = Some(handle);
                    self.stats.calls += 1;
                    self.events.push_back(Event::PhoneVoiceOpened { handle });
                    self.flag_others(true);
                } else {
                    self.events
                        .push_back(Event::PhoneVoiceFailed { status: packet[3] });
                }
                Some(FromChip::Drop)
            }
            DISCONNECTION_COMPLETE if packet.len() >= 7 => {
                let handle = u16::from_le_bytes([packet[4], packet[5]]) & 0x0FFF;
                if Some(handle) != self.voice_handle {
                    return None;
                }
                self.voice_handle = None;
                self.events
                    .push_back(Event::PhoneVoiceClosed { reason: packet[6] });
                self.flag_others(false);
                Some(FromChip::Drop)
            }
            _ => None,
        }
    }

    /// Tracks the classic links that are not the phone's, so they can be flagged during a call.
    fn watch_links(&mut self, packet: &[u8]) {
        if packet.first() != Some(&EVENT) || packet.len() < 7 {
            return;
        }
        match packet[1] {
            CONNECTION_COMPLETE
                if packet.len() >= 13 && packet[3] == 0 && packet[12] == ACL_LINK =>
            {
                let mut bytes = [0u8; 6];
                bytes.copy_from_slice(&packet[6..12]);
                self.events.push_back(Event::LinkUp {
                    address: Address(bytes),
                });
                let mine = self.phone.is_some_and(|p| packet[6..12] == p.0);
                if mine {
                    return;
                }
                let handle = u16::from_le_bytes([packet[4], packet[5]]) & 0x0FFF;
                self.other_links.insert(handle);
                if self.in_call() {
                    self.flag(handle, true); // headphones that connect mid-call
                }
            }
            DISCONNECTION_COMPLETE => {
                let handle = u16::from_le_bytes([packet[4], packet[5]]) & 0x0FFF;
                self.other_links.remove(&handle);
                self.flagged.remove(&handle);
            }
            _ => {}
        }
    }

    fn flag_others(&mut self, enable: bool) {
        if !self.flag_other_links {
            return;
        }
        let handles: Vec<u16> = if enable {
            self.other_links.iter().copied().collect()
        } else {
            self.flagged.iter().copied().collect()
        };
        for handle in handles {
            self.flag(handle, enable);
        }
    }

    fn flag(&mut self, handle: u16, enable: bool) {
        if !self.flag_other_links {
            return;
        }
        if enable == self.flagged.contains(&handle) {
            return;
        }
        let [lo, hi] = handle.to_le_bytes();
        self.outbox
            .push_back(vec![COMMAND, 0x57, 0xFC, 3, lo, hi, enable as u8]);
        self.own_commands += 1;
        if enable {
            self.flagged.insert(handle);
        } else {
            self.flagged.remove(&handle);
        }
        self.events
            .push_back(Event::PriorityRequested { handle, enable });
    }
}

// "Enhanced Accept Synchronous Connection Request" for CVSD voice, as the Hands-Free profile
// defines its parameter sets. eSCO uses S4 (a voice packet every 12 slots with a retransmission
// window, which the phone agrees to); plain SCO uses D1 (HV3). The address goes in bytes 4..10.
const ESCO_S4: &str = "013e043fffeeddccbbaa401f0000401f0000020000000002000000003c003c00803e0000803e0000040000000004000000001000100002020000000000000c00880302";
const SCO_D1: &str = "013e043fffeeddccbbaa401f0000401f0000020000000002000000003c003c00803e0000803e000004000000000400000000100010000202000000000000ffff040000";

fn from_hex(text: &str) -> Vec<u8> {
    (0..text.len() / 2)
        .filter_map(|i| u8::from_str_radix(&text[2 * i..2 * i + 2], 16).ok())
        .collect()
}

pub fn accept_voice(phone: &Address, link_type: u8) -> Vec<u8> {
    let mut command = from_hex(if link_type == 0 { SCO_D1 } else { ESCO_S4 });
    command[4..10].copy_from_slice(&phone.0);
    command
}

#[cfg(test)]
mod tests {
    use super::super::h4::ACL;
    use super::*;

    const PHONE: Address = Address([1, 0, 0, 0xC2, 0xAC, 0x6C]);
    const BUDS: Address = Address([3, 0, 0, 0xCC, 0xBB, 0xAA]);
    const MOUSE: Address = Address([2, 0, 0, 0xCC, 0xBB, 0xAA]);

    fn request(address: &Address, link_type: u8) -> Vec<u8> {
        let mut p = vec![EVENT, 0x04, 10];
        p.extend_from_slice(&address.0);
        p.extend_from_slice(&[0x0c, 0x02, 0x5a, link_type]);
        p
    }

    fn status_for(opcode: u16, status: u8) -> Vec<u8> {
        let [lo, hi] = opcode.to_le_bytes();
        vec![EVENT, 0x0F, 4, status, 1, lo, hi]
    }

    fn connected(handle: u16, address: &Address, link_type: u8, status: u8) -> Vec<u8> {
        let [lo, hi] = handle.to_le_bytes();
        let mut p = vec![EVENT, 0x03, 11, status, lo, hi];
        p.extend_from_slice(&address.0);
        p.extend_from_slice(&[link_type, 0]);
        p
    }

    fn voice_open(address: &Address, handle: u16, status: u8) -> Vec<u8> {
        let [lo, hi] = handle.to_le_bytes();
        let mut p = vec![EVENT, 0x2C, 17, status, lo, hi];
        p.extend_from_slice(&address.0);
        p.push(2);
        p.extend_from_slice(&[0; 7]);
        p
    }

    fn closed(handle: u16, reason: u8) -> Vec<u8> {
        let [lo, hi] = handle.to_le_bytes();
        vec![EVENT, 0x05, 4, 0, lo, hi, reason]
    }

    fn voice_packet(handle: u16, size: usize) -> Vec<u8> {
        let [lo, hi] = handle.to_le_bytes();
        let mut p = vec![SCO, lo, hi, size as u8];
        p.extend((0..size).map(|i| i as u8));
        p
    }

    fn complete(opcode: u16) -> Vec<u8> {
        command_complete(opcode, 0)
    }

    fn open_call(policy: &mut Policy) {
        assert!(matches!(
            policy.from_chip(&request(&PHONE, 2)),
            FromChip::Accept(_)
        ));
        assert_eq!(
            policy.from_chip(&status_for(OP_ACCEPT_ENHANCED_SYNC, 0)),
            FromChip::Drop
        );
        assert_eq!(
            policy.from_chip(&voice_open(&PHONE, 0x20, 0)),
            FromChip::Drop
        );
    }

    #[test]
    fn addresses_parse_least_significant_byte_first() {
        assert_eq!(Address::parse("6C:AC:C2:00:00:01"), Some(PHONE));
        assert_eq!(Address::parse("6c-ac-c2-00-00-01"), Some(PHONE));
        assert_eq!(Address::parse("6C:AC:C2:00:00"), None);
        assert_eq!(Address::parse("6C:AC:C2:00:00:01:02"), None);
        assert_eq!(Address::parse("zz:AC:C2:00:00:01"), None);
    }

    #[test]
    fn the_accept_commands_are_the_standard_ones_with_the_phones_address() {
        let esco = accept_voice(&PHONE, 2);
        assert_eq!(
            (esco.len(), &esco[..4], &esco[4..10]),
            (67, &[1u8, 0x3e, 0x04, 0x3f][..], &PHONE.0[..])
        );
        assert_eq!(&esco[esco.len() - 5..], &[0x0c, 0x00, 0x88, 0x03, 0x02]);
        let sco = accept_voice(&PHONE, 0);
        assert_eq!((sco.len(), &sco[4..10]), (67, &PHONE.0[..]));
        assert_eq!(&sco[sco.len() - 5..], &[0xff, 0xff, 0x04, 0x00, 0x00]);
    }

    #[test]
    fn ordinary_traffic_passes_both_ways() {
        let mut policy = Policy::new(Some(PHONE), true);
        assert_eq!(
            policy.from_host(&[COMMAND, 0x03, 0x0C, 0]),
            FromHost::Forward
        );
        assert_eq!(policy.from_chip(&complete(0x0C03)), FromChip::Forward);
        assert_eq!(
            policy.from_chip(&[ACL, 0x0B, 0x20, 1, 0, 9]),
            FromChip::Forward
        );
    }

    #[test]
    fn vendor_commands_macos_sends_still_pass() {
        let mut policy = Policy::new(Some(PHONE), true);
        assert_eq!(
            policy.from_host(&[COMMAND, 0x6E, 0xFE, 5, 0, 0, 0, 0, 0]),
            FromHost::Forward
        );
    }

    #[test]
    fn serial_boot_commands_are_answered_here_and_never_reach_the_chip() {
        let mut policy = Policy::new(None, true);
        for opcode in SERIAL_BOOT_COMMANDS {
            let [lo, hi] = opcode.to_le_bytes();
            assert_eq!(
                policy.from_host(&[COMMAND, lo, hi, 0]),
                FromHost::Answer(complete(opcode))
            );
        }
        assert_eq!(policy.stats.to_chip, 0);
    }

    #[test]
    fn the_phones_voice_request_is_accepted_by_us_and_hidden_from_macos() {
        let mut policy = Policy::new(Some(PHONE), true);
        open_call(&mut policy);
        assert!(policy.in_call());
        assert_eq!(policy.stats.to_host, 0);
        assert_eq!(
            policy.take_events(),
            vec![
                Event::PhoneVoiceRequested { link_type: 2 },
                Event::PhoneVoiceOpened { handle: 0x20 }
            ]
        );
        policy.from_chip(&connected(0x0C, &PHONE, 1, 0));
        policy.from_chip(&connected(0x0D, &BUDS, 1, 0));
        policy.from_chip(&connected(0x0E, &MOUSE, 1, 4));
        assert_eq!(
            policy
                .take_events()
                .into_iter()
                .filter(|e| matches!(e, Event::LinkUp { .. }))
                .collect::<Vec<_>>(),
            vec![
                Event::LinkUp { address: PHONE },
                Event::LinkUp { address: BUDS }
            ],
            "a link that failed to come up is not reported"
        );
    }

    #[test]
    fn the_phones_voice_is_tapped_and_answered_with_silence_of_the_same_size() {
        let mut policy = Policy::new(Some(PHONE), true);
        open_call(&mut policy);
        let packet = voice_packet(0x20, 60);
        assert_eq!(
            policy.from_chip(&packet),
            FromChip::Voice(packet[4..].to_vec())
        );
        let reply = policy.silence_for(&packet);
        assert_eq!((&reply[..4], &reply[4..]), (&packet[..4], &[0u8; 60][..]));
        assert_eq!((policy.stats.voice_in, policy.stats.voice_out), (1, 1));
    }

    #[test]
    fn everything_else_still_reaches_macos() {
        let mut policy = Policy::new(Some(PHONE), false);
        open_call(&mut policy);
        for packet in [
            request(&PHONE, 1),
            request(&BUDS, 2),
            voice_open(&BUDS, 0x21, 0),
            voice_packet(0x21, 60),
            status_for(0x0405, 0),
            closed(0x0B, 0x13),
            complete(0x0C03),
        ] {
            assert_eq!(
                policy.from_chip(&packet),
                FromChip::Forward,
                "{packet:02x?}"
            );
        }
    }

    #[test]
    fn a_headset_voice_link_is_never_touched() {
        let mut policy = Policy::new(Some(PHONE), true);
        for packet in [
            request(&BUDS, 2),
            status_for(OP_ACCEPT_ENHANCED_SYNC, 0),
            voice_open(&BUDS, 0x21, 0),
            voice_packet(0x21, 60),
        ] {
            assert_eq!(policy.from_chip(&packet), FromChip::Forward);
        }
        assert!(!policy.in_call());
    }

    #[test]
    fn when_the_link_closes_the_tap_ends_and_a_new_call_can_open_one() {
        let mut policy = Policy::new(Some(PHONE), true);
        open_call(&mut policy);
        assert_eq!(policy.from_chip(&closed(0x20, 0x13)), FromChip::Drop);
        assert!(!policy.in_call());
        let packet = voice_packet(0x20, 60);
        assert_eq!(policy.from_chip(&packet), FromChip::Forward);
        open_call(&mut policy);
        assert_eq!(policy.stats.calls, 2);
    }

    #[test]
    fn a_second_request_while_one_is_open_is_left_to_macos_to_decline() {
        let mut policy = Policy::new(Some(PHONE), true);
        open_call(&mut policy);
        let again = request(&PHONE, 2);
        assert_eq!(policy.from_chip(&again), FromChip::Forward);
    }

    #[test]
    fn a_refused_or_failed_link_leaves_nothing_behind() {
        let mut policy = Policy::new(Some(PHONE), true);
        policy.from_chip(&request(&PHONE, 2));
        assert_eq!(
            policy.from_chip(&status_for(OP_ACCEPT_ENHANCED_SYNC, 0x0C)),
            FromChip::Drop
        );
        assert!(!policy.in_call());
        assert!(matches!(
            policy.from_chip(&request(&PHONE, 2)),
            FromChip::Accept(_)
        ));
        policy.from_chip(&status_for(OP_ACCEPT_ENHANCED_SYNC, 0));
        assert_eq!(
            policy.from_chip(&voice_open(&PHONE, 0x20, 0x1F)),
            FromChip::Drop
        );
        assert!(!policy.in_call());
        assert!(policy
            .take_events()
            .contains(&Event::PhoneVoiceFailed { status: 0x1F }));
    }

    #[test]
    fn without_a_phone_nothing_is_kept() {
        let mut policy = Policy::new(None, true);
        for packet in [
            request(&PHONE, 2),
            voice_open(&PHONE, 0x20, 0),
            voice_packet(0x20, 60),
        ] {
            assert_eq!(policy.from_chip(&packet), FromChip::Forward);
        }
    }

    #[test]
    fn the_other_links_are_flagged_high_priority_for_the_call_and_unflagged_after() {
        let mut policy = Policy::new(Some(PHONE), true);
        policy.from_chip(&connected(0x0C, &PHONE, 1, 0));
        policy.from_chip(&connected(0x0D, &BUDS, 1, 0));
        policy.from_chip(&connected(0x0E, &MOUSE, 1, 0));
        open_call(&mut policy);
        assert_eq!(
            policy.take_outbox(),
            vec![
                vec![COMMAND, 0x57, 0xFC, 3, 0x0D, 0x00, 1],
                vec![COMMAND, 0x57, 0xFC, 3, 0x0E, 0x00, 1]
            ]
        );
        assert_eq!(
            policy.from_chip(&complete(BCM_HIGH_PRIORITY)),
            FromChip::Drop
        );
        assert_eq!(
            policy.from_chip(&complete(BCM_HIGH_PRIORITY)),
            FromChip::Drop
        );
        policy.from_chip(&closed(0x20, 0x13));
        assert_eq!(
            policy.take_outbox(),
            vec![
                vec![COMMAND, 0x57, 0xFC, 3, 0x0D, 0x00, 0],
                vec![COMMAND, 0x57, 0xFC, 3, 0x0E, 0x00, 0]
            ]
        );
        policy.from_chip(&complete(BCM_HIGH_PRIORITY));
        policy.from_chip(&complete(BCM_HIGH_PRIORITY));
        let later = complete(BCM_HIGH_PRIORITY);
        assert_eq!(
            policy.from_chip(&later),
            FromChip::Forward,
            "only our own answers are hidden"
        );
    }

    #[test]
    fn headphones_that_connect_mid_call_are_flagged_and_a_link_that_ends_is_forgotten() {
        let mut policy = Policy::new(Some(PHONE), true);
        open_call(&mut policy);
        assert!(policy.take_outbox().is_empty());
        policy.from_chip(&connected(0x0D, &BUDS, 1, 0));
        assert_eq!(
            policy.take_outbox(),
            vec![vec![COMMAND, 0x57, 0xFC, 3, 0x0D, 0x00, 1]]
        );
        policy.from_chip(&closed(0x0D, 0x13));
        policy.from_chip(&closed(0x20, 0x13));
        assert!(
            policy.take_outbox().is_empty(),
            "a link that is gone needs no command"
        );
    }

    #[test]
    fn the_phones_own_link_and_failed_links_are_never_flagged() {
        let mut policy = Policy::new(Some(PHONE), true);
        policy.from_chip(&connected(0x0C, &PHONE, 1, 0));
        policy.from_chip(&connected(0x0D, &BUDS, 1, 4));
        open_call(&mut policy);
        assert!(policy.take_outbox().is_empty());
    }

    #[test]
    fn without_the_flag_no_priority_commands_are_sent() {
        let mut policy = Policy::new(Some(PHONE), false);
        policy.from_chip(&connected(0x0D, &BUDS, 1, 0));
        open_call(&mut policy);
        assert!(policy.take_outbox().is_empty());
    }
}

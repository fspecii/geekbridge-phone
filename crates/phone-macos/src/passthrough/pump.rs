// SPDX-License-Identifier: GPL-3.0-or-later
//! The loop: packets from the daemon's serial port to the controller and back, with the
//! policy deciding what is ours.

use super::controller::Controller;
use super::h4::Parser;
use super::policy::{Event, FromChip, FromHost, Policy};
use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

/// How long a receive waits for the controller before the serial side gets another look.
const RECV_WAIT_MS: i32 = 2;

/// The daemon's end of the serial line: a pseudo-terminal in the product, a socket in tests.
pub trait HostPort: Read + Write + AsRawFd {}
impl<T: Read + Write + AsRawFd> HostPort for T {}

/// What happens to a call's sound. The pump gives it each phone voice packet and takes one back.
pub trait Sound {
    /// Called about once a second while the pump runs.
    fn tick(&mut self) {}
    fn call_opened(&mut self);
    fn call_closed(&mut self);
    /// `down`: the payload of one voice packet from the phone. Returns the payload to send
    /// back, which must be the same length.
    fn exchange(&mut self, down: &[u8]) -> Vec<u8>;
}

/// No sound: the phone's voice is answered with silence.
pub struct Silent;

impl Sound for Silent {
    fn call_opened(&mut self) {}
    fn call_closed(&mut self) {}
    fn exchange(&mut self, down: &[u8]) -> Vec<u8> {
        vec![0; down.len()]
    }
}

pub trait Observer {
    fn event(&mut self, event: &Event);
}

fn readable(fd: i32) -> bool {
    let mut p = libc::pollfd {
        fd,
        events: libc::POLLIN,
        revents: 0,
    };
    // SAFETY: one valid pollfd; a zero timeout never blocks.
    let n = unsafe { libc::poll(&mut p, 1, 0) };
    n > 0 && p.revents & (libc::POLLIN | libc::POLLHUP) != 0
}

/// Runs until `stop` is set, the daemon closes its end, or something breaks.
pub fn run(
    host: &mut dyn HostPort,
    chip: &mut dyn Controller,
    policy: &mut Policy,
    sound: &mut dyn Sound,
    observer: &mut dyn Observer,
    stop: &AtomicBool,
) -> io::Result<()> {
    let mut parser = Parser::new();
    let mut scratch = [0u8; 4096];
    let fd = host.as_raw_fd();
    let mut next_tick = Instant::now();
    while !stop.load(Ordering::Relaxed) {
        if Instant::now() >= next_tick {
            sound.tick();
            next_tick = Instant::now() + Duration::from_secs(1);
        }
        if readable(fd) {
            let n = host.read(&mut scratch)?;
            if n == 0 {
                return Ok(()); // the daemon let go of the port
            }
            let packets = parser
                .feed(&scratch[..n])
                .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
            for packet in packets {
                match policy.from_host(&packet) {
                    FromHost::Forward => chip.send(&packet)?,
                    FromHost::Answer(reply) => host.write_all(&reply)?,
                }
            }
        }
        for command in policy.take_outbox() {
            chip.send(&command)?;
        }
        if let Some(packet) = chip.recv(RECV_WAIT_MS)? {
            match policy.from_chip(&packet) {
                FromChip::Forward => host.write_all(&packet)?,
                FromChip::Drop => {}
                FromChip::Accept(command) => chip.send(&command)?,
                FromChip::Voice(payload) => {
                    let reply = sound.exchange(&payload);
                    policy.stats.voice_out += 1;
                    let mut out = Vec::with_capacity(4 + payload.len());
                    out.extend_from_slice(&packet[..4]);
                    out.extend_from_slice(&fit(reply, payload.len()));
                    chip.send(&out)?;
                }
            }
        }
        for event in policy.take_events() {
            match &event {
                Event::PhoneVoiceOpened { .. } => sound.call_opened(),
                Event::PhoneVoiceClosed { .. } => sound.call_closed(),
                _ => {}
            }
            observer.event(&event);
        }
    }
    Ok(())
}

/// A reply is never a different length from what it answers: pad with silence or cut.
fn fit(mut reply: Vec<u8>, length: usize) -> Vec<u8> {
    reply.resize(length, 0);
    reply
}

#[cfg(test)]
mod tests {
    use super::super::h4::{command_complete, ACL, COMMAND, EVENT};
    use super::super::policy::Address;
    use super::*;
    use std::collections::VecDeque;
    use std::os::unix::net::UnixStream;
    use std::sync::Arc;

    const PHONE: Address = Address([1, 0, 0, 0xC2, 0xAC, 0x6C]);
    const BUDS: Address = Address([3, 0, 0, 0xCC, 0xBB, 0xAA]);

    /// A controller that plays a script, records what it is sent, and stops the pump when the
    /// script has run out.
    struct FakeChip {
        script: VecDeque<Vec<u8>>,
        sent: Vec<Vec<u8>>,
        stop: Arc<AtomicBool>,
    }

    impl Controller for FakeChip {
        fn send(&mut self, packet: &[u8]) -> io::Result<()> {
            self.sent.push(packet.to_vec());
            Ok(())
        }
        fn recv(&mut self, _wait_ms: i32) -> io::Result<Option<Vec<u8>>> {
            match self.script.pop_front() {
                Some(p) => Ok(Some(p)),
                None => {
                    self.stop.store(true, Ordering::Relaxed);
                    Ok(None)
                }
            }
        }
    }

    #[derive(Default)]
    struct RecordingSound {
        opened: u32,
        closed: u32,
        heard: Vec<Vec<u8>>,
    }

    impl Sound for RecordingSound {
        fn call_opened(&mut self) {
            self.opened += 1;
        }
        fn call_closed(&mut self) {
            self.closed += 1;
        }
        fn exchange(&mut self, down: &[u8]) -> Vec<u8> {
            self.heard.push(down.to_vec());
            vec![7; down.len()]
        }
    }

    #[derive(Default)]
    struct Events(Vec<Event>);

    impl Observer for Events {
        fn event(&mut self, event: &Event) {
            self.0.push(event.clone());
        }
    }

    fn run_script(
        phone: Option<Address>,
        from_daemon: &[u8],
        script: Vec<Vec<u8>>,
        sound: &mut RecordingSound,
    ) -> (Vec<Vec<u8>>, Vec<u8>, Vec<Event>) {
        let (ours, mut daemon) = UnixStream::pair().unwrap();
        daemon.write_all(from_daemon).unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let mut chip = FakeChip {
            script: script.into(),
            sent: Vec::new(),
            stop: stop.clone(),
        };
        let mut policy = Policy::new(phone, true);
        let mut events = Events::default();
        let mut host = ours;
        run(&mut host, &mut chip, &mut policy, sound, &mut events, &stop).unwrap();
        drop(host);
        let mut received = Vec::new();
        daemon.read_to_end(&mut received).unwrap();
        (chip.sent, received, events.0)
    }

    fn event_packet(code: u8, params: &[u8]) -> Vec<u8> {
        let mut p = vec![EVENT, code, params.len() as u8];
        p.extend_from_slice(params);
        p
    }

    #[test]
    fn ordinary_traffic_crosses_both_ways_and_boot_commands_are_answered_here() {
        let reset = [COMMAND, 0x03, 0x0C, 0];
        let boot = [COMMAND, 0x2E, 0xFC, 0];
        let data = [ACL, 0x0B, 0x20, 3, 0, 1, 2, 3];
        let from_daemon: Vec<u8> = [&reset[..], &boot, &data].concat();
        let done = command_complete(0x0C03, 0);
        let mut sound = RecordingSound::default();
        let (sent, received, _) = run_script(
            None,
            &from_daemon,
            vec![done.clone(), data.to_vec()],
            &mut sound,
        );
        assert_eq!(sent, vec![reset.to_vec(), data.to_vec()]);
        let mut want = command_complete(0xFC2E, 0);
        want.extend_from_slice(&done);
        want.extend_from_slice(&data);
        assert_eq!(received, want);
    }

    #[test]
    fn a_call_is_accepted_carried_and_closed_without_the_daemon_seeing_any_of_it() {
        let mut request = PHONE.0.to_vec();
        request.extend_from_slice(&[0x0c, 0x02, 0x5a, 2]);
        let mut open = vec![0];
        open.extend_from_slice(&0x20u16.to_le_bytes());
        open.extend_from_slice(&PHONE.0);
        open.push(2);
        open.extend_from_slice(&[0; 7]);
        let voice = vec![SCO_TYPE, 0x20, 0x00, 4, 1, 2, 3, 4];
        let script = vec![
            event_packet(0x04, &request),
            event_packet(0x0F, &[0, 1, 0x3E, 0x04]),
            event_packet(0x2C, &open),
            voice.clone(),
            voice.clone(),
            event_packet(0x05, &[0, 0x20, 0, 0x13]),
        ];
        let mut sound = RecordingSound::default();
        let (sent, received, events) = run_script(Some(PHONE), &[], script, &mut sound);
        assert!(received.is_empty(), "the daemon saw {received:02x?}");
        assert_eq!(
            sent.len(),
            3,
            "the accept command and one reply for each voice packet"
        );
        assert_eq!(&sent[0][..4], &[1, 0x3E, 0x04, 0x3F]);
        assert_eq!(&sent[0][4..10], &PHONE.0);
        assert_eq!(sent[1], vec![SCO_TYPE, 0x20, 0x00, 4, 7, 7, 7, 7]);
        assert_eq!(sent[2], sent[1]);
        assert_eq!((sound.opened, sound.closed), (1, 1));
        assert_eq!(sound.heard, vec![vec![1, 2, 3, 4]; 2]);
        assert!(events.contains(&Event::PhoneVoiceOpened { handle: 0x20 }));
        assert!(events.contains(&Event::PhoneVoiceClosed { reason: 0x13 }));
    }

    #[test]
    fn headphones_are_flagged_high_priority_for_the_call_and_the_answers_are_hidden() {
        let mut headphones = vec![0, 0x0D, 0x00];
        headphones.extend_from_slice(&BUDS.0);
        headphones.extend_from_slice(&[1, 0]);
        let mut request = PHONE.0.to_vec();
        request.extend_from_slice(&[0x0c, 0x02, 0x5a, 2]);
        let mut open = vec![0];
        open.extend_from_slice(&0x20u16.to_le_bytes());
        open.extend_from_slice(&PHONE.0);
        open.push(2);
        open.extend_from_slice(&[0; 7]);
        let script = vec![
            event_packet(0x03, &headphones),
            event_packet(0x04, &request),
            event_packet(0x0F, &[0, 1, 0x3E, 0x04]),
            event_packet(0x2C, &open),
            command_complete(0xFC57, 0),
            event_packet(0x05, &[0, 0x20, 0, 0x13]),
            command_complete(0xFC57, 0),
        ];
        let mut sound = RecordingSound::default();
        let (sent, received, _) = run_script(Some(PHONE), &[], script, &mut sound);
        assert!(sent.contains(&vec![COMMAND, 0x57, 0xFC, 3, 0x0D, 0x00, 1]));
        assert!(sent.contains(&vec![COMMAND, 0x57, 0xFC, 3, 0x0D, 0x00, 0]));
        let connection = event_packet(0x03, &headphones);
        assert_eq!(
            received, connection,
            "the daemon sees the headphones connect and nothing of ours"
        );
    }

    #[test]
    fn a_reply_is_never_a_different_length_from_what_it_answers() {
        assert_eq!(fit(vec![1, 2], 4), vec![1, 2, 0, 0]);
        assert_eq!(fit(vec![1, 2, 3, 4, 5], 4), vec![1, 2, 3, 4]);
    }

    #[test]
    fn silence_answers_with_zeros_of_the_same_length() {
        assert_eq!(Silent.exchange(&[9, 9, 9]), vec![0, 0, 0]);
    }

    const SCO_TYPE: u8 = super::super::h4::SCO;
}

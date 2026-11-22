// SPDX-License-Identifier: GPL-3.0-or-later
//! HCI over a serial line ("H4"): one type byte, then a header, then a payload.
//!
//! `bluetoothd` talks to the controller this way when pointed at a serial port, so the
//! pass-through has to cut the byte stream into whole packets before it can look at them.

pub const COMMAND: u8 = 1;
pub const ACL: u8 = 2;
pub const SCO: u8 = 3;
pub const EVENT: u8 = 4;

/// Header size, offset of the length field inside the header, and its size, per packet type.
fn layout(kind: u8) -> Option<(usize, usize, usize)> {
    match kind {
        COMMAND => Some((3, 2, 1)),
        ACL => Some((4, 2, 2)),
        SCO => Some((3, 2, 1)),
        EVENT => Some((2, 1, 1)),
        _ => None,
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum FramingError {
    /// The stream is not HCI: the pass-through stops rather than guess.
    UnknownType(u8),
}

impl std::fmt::Display for FramingError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let FramingError::UnknownType(t) = self;
        write!(f, "the serial stream is not HCI (packet type 0x{t:02x})")
    }
}

impl std::error::Error for FramingError {}

/// Cuts a byte stream into whole packets, however the bytes arrive.
#[derive(Default)]
pub struct Parser {
    buffer: Vec<u8>,
}

impl Parser {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn feed(&mut self, data: &[u8]) -> Result<Vec<Vec<u8>>, FramingError> {
        self.buffer.extend_from_slice(data);
        let mut packets = Vec::new();
        while let Some(&kind) = self.buffer.first() {
            let (header, offset, size) = layout(kind).ok_or(FramingError::UnknownType(kind))?;
            if self.buffer.len() < 1 + header {
                break;
            }
            let at = 1 + offset;
            let length = if size == 1 {
                self.buffer[at] as usize
            } else {
                u16::from_le_bytes([self.buffer[at], self.buffer[at + 1]]) as usize
            };
            let total = 1 + header + length;
            if self.buffer.len() < total {
                break;
            }
            packets.push(self.buffer.drain(..total).collect());
        }
        Ok(packets)
    }
}

/// The event a controller sends when a command finished at once.
pub fn command_complete(opcode: u16, status: u8) -> Vec<u8> {
    let [lo, hi] = opcode.to_le_bytes();
    vec![EVENT, 0x0E, 4, 1, lo, hi, status]
}

#[cfg(test)]
mod tests {
    use super::*;

    const RESET: [u8; 4] = [COMMAND, 0x03, 0x0C, 0];
    const DATA: [u8; 8] = [ACL, 0x0B, 0x20, 3, 0, 1, 2, 3];
    const VOICE: [u8; 6] = [SCO, 0x20, 0x00, 2, 9, 9];
    const DONE: [u8; 7] = [EVENT, 0x0E, 4, 1, 0x03, 0x0C, 0];

    #[test]
    fn packets_come_out_whole_however_the_bytes_arrive() {
        let stream: Vec<u8> = [&RESET[..], &DATA, &VOICE, &DONE].concat();
        let want = vec![RESET.to_vec(), DATA.to_vec(), VOICE.to_vec(), DONE.to_vec()];
        assert_eq!(Parser::new().feed(&stream).unwrap(), want);
        let mut parser = Parser::new();
        let mut got = Vec::new();
        for byte in &stream {
            got.extend(parser.feed(&[*byte]).unwrap());
        }
        assert_eq!(got, want);
    }

    #[test]
    fn a_partial_packet_waits_for_the_rest() {
        let mut parser = Parser::new();
        assert!(parser.feed(&DATA[..5]).unwrap().is_empty());
        assert_eq!(parser.feed(&DATA[5..]).unwrap(), vec![DATA.to_vec()]);
    }

    #[test]
    fn a_long_data_packet_uses_its_two_byte_length() {
        let mut big = vec![ACL, 0x0B, 0x20];
        big.extend_from_slice(&700u16.to_le_bytes());
        big.extend(std::iter::repeat_n(0, 700));
        let stream: Vec<u8> = [&big[..], &RESET].concat();
        assert_eq!(
            Parser::new().feed(&stream).unwrap(),
            vec![big, RESET.to_vec()]
        );
    }

    #[test]
    fn a_stream_that_is_not_hci_stops_the_run() {
        assert_eq!(
            Parser::new().feed(b"\x7fnot hci"),
            Err(FramingError::UnknownType(0x7f))
        );
    }

    #[test]
    fn command_complete_carries_the_opcode_and_status() {
        assert_eq!(
            command_complete(0xFC57, 0),
            vec![EVENT, 0x0E, 4, 1, 0x57, 0xFC, 0]
        );
    }
}

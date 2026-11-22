// SPDX-License-Identifier: GPL-3.0-or-later
//! The sound of a call: the caller's voice to the Mac's output, the Mac's input to the caller.
//!
//! A phone voice packet is 8 kHz, 16-bit, mono, a few milliseconds long. Each one that comes
//! in is resampled to the output's rate and queued for playback, and the controller wants one
//! packet of the same size back, taken from the microphone queue (silence when it is empty).
//! The queues are bounded: overflow drops the oldest sound, running dry plays silence.

use super::pump::Sound;
use crate::voice::audio::pcm::{PlaybackRing, Resampler};
use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

const PHONE_RATE: u32 = 8000;
/// Longest the queues may grow, in milliseconds of sound.
const QUEUE_MS: u32 = 200;
/// Playback starts once this much is queued.
const PRIME_MS: u32 = 40;

pub type Capture = Box<dyn FnMut(&[i16]) + Send>;
pub type Render = Box<dyn FnMut(&mut [i16]) + Send>;

/// What a started sound device reports.
pub struct Started {
    pub input_rate: u32,
    pub output_rate: u32,
    /// The output is loudspeakers, so the microphone would hear the call.
    pub echo_risk: bool,
    /// Dropping it stops the sound.
    pub handle: Box<dyn Send>,
}

/// The Mac's sound hardware. Mono 16-bit at each device's own rate; the device does the rest.
pub trait Io: Send + Sync {
    fn start(&self, capture: Capture, render: Render) -> Result<Started, String>;
}

/// Reading and setting the default sound devices.
pub trait Routing: Send {
    /// Called about once a second. True if it moved the input.
    fn keep_input_off_bluetooth(&mut self) -> bool;
    /// For the length of a call. True if it moved the output to Bluetooth headphones.
    fn use_headphones(&mut self) -> bool;
    fn restore_output(&mut self);
    fn restore(&mut self);
}

#[derive(Default)]
struct Queues {
    ring: Option<PlaybackRing>,
    mic: VecDeque<i16>,
    mic_cap: usize,
    /// How much of the mic went unused or was missing, for the log.
    mic_dropped: u64,
    mic_missing: u64,
    packets: u64,
}

fn lock(q: &Mutex<Queues>) -> std::sync::MutexGuard<'_, Queues> {
    q.lock().unwrap_or_else(|e| e.into_inner())
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct CallStats {
    pub packets: u64,
    pub playback_dropped: u64,
    pub underruns: u64,
    pub mic_dropped: u64,
    pub mic_missing: u64,
}

struct Session {
    queues: Arc<Mutex<Queues>>,
    down: Resampler,
    _device: Box<dyn Send>,
}

pub struct CallSound {
    io: Arc<dyn Io>,
    routing: Box<dyn Routing>,
    session: Option<Session>,
    last: CallStats,
    /// Why the last call had no sound, if so.
    pub failure: Option<String>,
}

impl CallSound {
    pub fn new(io: Arc<dyn Io>, routing: Box<dyn Routing>) -> Self {
        Self {
            io,
            routing,
            session: None,
            last: CallStats::default(),
            failure: None,
        }
    }

    pub fn last_call(&self) -> &CallStats {
        &self.last
    }

    fn stats(session: &Session) -> CallStats {
        let q = lock(&session.queues);
        let ring = q.ring.as_ref();
        CallStats {
            packets: q.packets,
            playback_dropped: ring.map_or(0, |r| r.dropped),
            underruns: ring.map_or(0, |r| r.underruns),
            mic_dropped: q.mic_dropped,
            mic_missing: q.mic_missing,
        }
    }
}

impl Sound for CallSound {
    fn tick(&mut self) {
        if self.routing.keep_input_off_bluetooth() {
            log::info!(
                "the sound input was on a Bluetooth headset; moved it to another microphone"
            );
        }
    }

    fn call_opened(&mut self) {
        self.failure = None;
        if self.routing.use_headphones() {
            log::info!("the call plays on the Bluetooth headphones");
        }
        let queues = Arc::new(Mutex::new(Queues::default()));
        // The rates are known only once the device has started; the queues are built then.
        let capture_queues = queues.clone();
        let render_queues = queues.clone();
        let mut up: Option<Resampler> = None;
        let mut scratch: Vec<i16> = Vec::new();
        let rates = Arc::new(Mutex::new((0u32, 0u32)));
        let capture_rates = rates.clone();
        let capture: Capture = Box::new(move |samples| {
            let input_rate = capture_rates.lock().unwrap_or_else(|e| e.into_inner()).0;
            if input_rate == 0 {
                return;
            }
            let resampler = up.get_or_insert_with(|| Resampler::new(input_rate, PHONE_RATE));
            scratch.clear();
            resampler.process(samples, &mut scratch);
            let mut q = lock(&capture_queues);
            // Without echo cancellation the microphone would hear the speaker: while the
            // output is loud the microphone is dropped (a no-op on headphones, which are quiet
            // at the microphone).
            if q.ring.as_ref().is_some_and(|r| r.last_loud && r.echo_risk) {
                return;
            }
            q.mic.extend(scratch.iter().copied());
            let over = q.mic.len().saturating_sub(q.mic_cap);
            if over > 0 {
                q.mic.drain(..over);
                q.mic_dropped += over as u64;
            }
        });
        let render: Render = Box::new(move |out| match lock(&render_queues).ring.as_mut() {
            Some(ring) => ring.pull(out),
            None => out.fill(0),
        });
        match self.io.start(capture, render) {
            Ok(started) => {
                *rates.lock().unwrap_or_else(|e| e.into_inner()) =
                    (started.input_rate, started.output_rate);
                {
                    let mut q = lock(&queues);
                    let mut ring = PlaybackRing::new(started.output_rate, QUEUE_MS, PRIME_MS);
                    ring.echo_risk = started.echo_risk;
                    q.ring = Some(ring);
                    q.mic_cap = (PHONE_RATE * QUEUE_MS / 1000) as usize;
                }
                self.session = Some(Session {
                    queues,
                    down: Resampler::new(PHONE_RATE, started.output_rate),
                    _device: started.handle,
                });
            }
            Err(e) => {
                log::warn!("the call has no sound: {e}");
                self.failure = Some(e);
            }
        }
    }

    fn exchange(&mut self, down: &[u8]) -> Vec<u8> {
        let Some(session) = self.session.as_mut() else {
            return vec![0; down.len()];
        };
        let samples: Vec<i16> = down
            .as_chunks::<2>()
            .0
            .iter()
            .map(|b| i16::from_le_bytes(*b))
            .collect();
        let mut resampled = Vec::with_capacity(samples.len() * 8);
        session.down.process(&samples, &mut resampled);
        let mut q = lock(&session.queues);
        q.packets += 1;
        if let Some(ring) = q.ring.as_mut() {
            ring.push(&resampled);
        }
        let mut out = Vec::with_capacity(down.len());
        for _ in 0..samples.len() {
            let sample = match q.mic.pop_front() {
                Some(s) => s,
                None => {
                    q.mic_missing += 1;
                    0
                }
            };
            out.extend_from_slice(&sample.to_le_bytes());
        }
        out
    }

    fn call_closed(&mut self) {
        if let Some(session) = self.session.take() {
            self.last = Self::stats(&session);
            log::info!(
                "call sound: {} packets, {} playback samples dropped, {} underruns, microphone: {} dropped, {} missing",
                self.last.packets,
                self.last.playback_dropped,
                self.last.underruns,
                self.last.mic_dropped,
                self.last.mic_missing
            );
        }
        self.routing.restore_output();
    }
}

impl Drop for CallSound {
    fn drop(&mut self) {
        self.session = None;
        self.routing.restore();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Default)]
    struct Taps {
        capture: Option<Capture>,
        render: Option<Render>,
        stopped: bool,
    }

    struct FakeIo {
        taps: Arc<Mutex<Taps>>,
        fail: bool,
        echo: bool,
    }

    struct Alive(Arc<Mutex<Taps>>);
    impl Drop for Alive {
        fn drop(&mut self) {
            self.0.lock().unwrap().stopped = true;
        }
    }

    impl Io for FakeIo {
        fn start(&self, capture: Capture, render: Render) -> Result<Started, String> {
            if self.fail {
                return Err("no microphone permission".into());
            }
            let mut taps = self.taps.lock().unwrap();
            taps.capture = Some(capture);
            taps.render = Some(render);
            Ok(Started {
                input_rate: 16000,
                output_rate: 16000,
                echo_risk: self.echo,
                handle: Box::new(Alive(self.taps.clone())),
            })
        }
    }

    #[derive(Default)]
    struct Moves(Arc<Mutex<Vec<&'static str>>>);

    impl Routing for Moves {
        fn keep_input_off_bluetooth(&mut self) -> bool {
            self.0.lock().unwrap().push("input");
            false
        }
        fn use_headphones(&mut self) -> bool {
            self.0.lock().unwrap().push("headphones");
            true
        }
        fn restore_output(&mut self) {
            self.0.lock().unwrap().push("restore output");
        }
        fn restore(&mut self) {
            self.0.lock().unwrap().push("restore");
        }
    }

    type Log = Arc<Mutex<Vec<&'static str>>>;

    fn sound(fail: bool, echo: bool) -> (CallSound, Arc<Mutex<Taps>>, Log) {
        let taps = Arc::new(Mutex::new(Taps::default()));
        let moves = Moves::default();
        let log = moves.0.clone();
        let io = FakeIo {
            taps: taps.clone(),
            fail,
            echo,
        };
        (CallSound::new(Arc::new(io), Box::new(moves)), taps, log)
    }

    fn bytes(samples: &[i16]) -> Vec<u8> {
        samples.iter().flat_map(|s| s.to_le_bytes()).collect()
    }

    #[test]
    fn before_a_call_and_without_a_device_the_answer_is_silence_of_the_same_size() {
        let (mut s, _, _) = sound(true, false);
        assert_eq!(s.exchange(&[1, 2, 3, 4]), vec![0; 4]);
        s.call_opened();
        assert_eq!(s.failure.as_deref(), Some("no microphone permission"));
        assert_eq!(s.exchange(&[1, 2, 3, 4]), vec![0; 4]);
    }

    #[test]
    fn the_callers_voice_reaches_the_speaker_and_the_microphone_comes_back() {
        let (mut s, taps, log) = sound(false, false);
        s.call_opened();
        assert_eq!(log.lock().unwrap().as_slice(), &["headphones"]);
        // The microphone speaks (16 kHz in, so half as many samples reach the phone).
        let mic = vec![1000i16; 640];
        (taps.lock().unwrap().capture.as_mut().unwrap())(&mic);
        // The phone sends 30 samples (3.75 ms) a packet; send enough to prime playback.
        let mut answers = Vec::new();
        for _ in 0..30 {
            answers.push(s.exchange(&bytes(&[500i16; 30])));
        }
        assert!(
            answers.iter().all(|a| a.len() == 60),
            "one answer of the same size for each packet"
        );
        let first: Vec<i16> = answers[0]
            .as_chunks::<2>()
            .0
            .iter()
            .map(|b| i16::from_le_bytes(*b))
            .collect();
        assert!(
            first.iter().all(|&x| (x - 1000).abs() <= 1),
            "the microphone's sound comes back: {first:?}"
        );
        let mut played = vec![0i16; 320];
        (taps.lock().unwrap().render.as_mut().unwrap())(&mut played);
        assert!(played.iter().any(|&x| x != 0), "the caller is heard");
        s.call_closed();
        assert!(
            taps.lock().unwrap().stopped,
            "the device is stopped when the call ends"
        );
        assert_eq!(
            log.lock().unwrap().as_slice(),
            &["headphones", "restore output"]
        );
        assert_eq!(s.last_call().packets, 30);
    }

    #[test]
    fn a_microphone_that_has_nothing_gives_silence_and_is_counted() {
        let (mut s, _, _) = sound(false, false);
        s.call_opened();
        assert_eq!(s.exchange(&bytes(&[1i16; 30])), vec![0; 60]);
        s.call_closed();
        assert_eq!(s.last_call().mic_missing, 30);
    }

    #[test]
    fn loudspeakers_mute_the_microphone_while_the_caller_is_loud() {
        let (mut s, taps, _) = sound(false, true);
        s.call_opened();
        for _ in 0..40 {
            s.exchange(&bytes(&[8000i16; 30]));
        }
        let mut out = vec![0i16; 160];
        (taps.lock().unwrap().render.as_mut().unwrap())(&mut out);
        (taps.lock().unwrap().capture.as_mut().unwrap())(&[1000i16; 320]);
        assert_eq!(
            s.exchange(&bytes(&[8000i16; 30])),
            vec![0; 60],
            "the speaker is loud: the microphone is dropped"
        );
    }

    #[test]
    fn the_tick_keeps_the_input_off_bluetooth_and_dropping_restores_everything() {
        let (mut s, _, log) = sound(false, false);
        s.tick();
        drop(s);
        assert_eq!(log.lock().unwrap().as_slice(), &["input", "restore"]);
    }
}

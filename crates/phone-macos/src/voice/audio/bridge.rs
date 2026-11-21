// SPDX-License-Identifier: GPL-3.0-or-later
//! Joins the helper's voice socket to the Mac's audio device during one call.
//!
//! Phone audio arrives as 8 kHz frames, is resampled to the device rate and played. Microphone
//! audio comes back from the device, is resampled to 8 kHz and sent as frames. The device is a
//! trait so this is tested without sound hardware; the real one is the voice-processing unit
//! in `vpio.rs`, which cancels the speaker's echo out of the microphone.

use super::pcm::{PlaybackRing, Resampler};
use std::io::{ErrorKind, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{sync_channel, RecvTimeoutError};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::Duration;

const MAGIC: u8 = 0xB7;
const DOWN: u8 = 1;
const UP: u8 = 2;
const HEADER: usize = 8;
const SCO_RATE: u32 = 8000;
/// Bytes of microphone audio per uplink frame (15 ms).
const UP_FRAME: usize = 240;

pub type CaptureFn = Box<dyn FnMut(&[i16]) + Send>;
pub type RenderFn = Box<dyn FnMut(&mut [i16]) + Send>;

/// A running audio device. Dropping it stops the sound.
pub trait Running: Send {}

pub trait AudioDevice: Send + Sync {
    /// Starts capture and playback. Returns the device's sample rate, whether it cancels echo,
    /// and the running handle.
    fn start(&self, capture: CaptureFn, render: RenderFn) -> Result<(u32, bool, Box<dyn Running>), String>;
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct AudioStats {
    pub down_frames: u64,
    pub up_frames: u64,
    pub up_dropped: u64,
    pub playback_dropped: u64,
    pub underruns: u64,
    pub echo_cancelled: bool,
    pub device_rate: u32,
}

#[derive(Default)]
struct Counters {
    down: AtomicU64,
    up: AtomicU64,
    up_dropped: AtomicU64,
}

pub struct CallAudio {
    stop: Arc<AtomicBool>,
    socket: UnixStream,
    threads: Vec<JoinHandle<()>>,
    device: Option<Box<dyn Running>>,
    ring: Arc<Mutex<PlaybackRing>>,
    counters: Arc<Counters>,
    echo_cancelled: bool,
    rate: u32,
}

fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

fn frame(kind: u8, sequence: u32, payload: &[u8]) -> Vec<u8> {
    let mut f = Vec::with_capacity(HEADER + payload.len());
    f.push(MAGIC);
    f.push(kind);
    f.extend_from_slice(&(payload.len() as u16).to_be_bytes());
    f.extend_from_slice(&sequence.to_be_bytes());
    f.extend_from_slice(payload);
    f
}

impl CallAudio {
    pub fn start(voice_socket: &Path, device: &dyn AudioDevice) -> Result<Self, String> {
        let socket = UnixStream::connect(voice_socket).map_err(|e| format!("cannot open the voice socket: {e}"))?;
        socket.set_read_timeout(Some(Duration::from_millis(200))).map_err(|e| e.to_string())?;
        let stop = Arc::new(AtomicBool::new(false));
        let counters = Arc::new(Counters::default());
        // The ring is created before the device so its rate is known: start with 8 kHz, then
        // replace the contents' rate by building the ring after the device answers.
        let ring = Arc::new(Mutex::new(PlaybackRing::new(SCO_RATE, 200, 30)));
        let (tx, rx) = sync_channel::<Vec<i16>>(64);

        let capture_ring = ring.clone();
        let capture_counters = counters.clone();
        let echo_flag = Arc::new(AtomicBool::new(false));
        let echo_for_capture = echo_flag.clone();
        let rate_cell = Arc::new(Mutex::new(SCO_RATE));
        let rate_for_capture = rate_cell.clone();
        let mut down_sampler: Option<Resampler> = None;
        let mut pending: Vec<i16> = Vec::new();
        let capture: CaptureFn = Box::new(move |samples| {
            let rate = *lock(&rate_for_capture);
            let sampler = down_sampler.get_or_insert_with(|| Resampler::new(rate, SCO_RATE));
            // Without echo cancellation, drop the microphone while the speaker is loud.
            if !echo_for_capture.load(Ordering::Relaxed) && lock(&capture_ring).last_loud {
                return;
            }
            sampler.process(samples, &mut pending);
            while pending.len() >= UP_FRAME / 2 {
                let chunk: Vec<i16> = pending.drain(..UP_FRAME / 2).collect();
                if tx.try_send(chunk).is_err() {
                    capture_counters.up_dropped.fetch_add(1, Ordering::Relaxed);
                }
            }
        });

        let render_ring = ring.clone();
        let render: RenderFn = Box::new(move |out| lock(&render_ring).pull(out));

        let (rate, echo_cancelled, running) = device.start(capture, render)?;
        *lock(&rate_cell) = rate;
        echo_flag.store(echo_cancelled, Ordering::Relaxed);
        // Now that the rate is known, the ring takes samples at the device rate.
        *lock(&ring) = PlaybackRing::new(rate, 200, 30);

        let mut threads = Vec::new();

        // Phone to speakers.
        {
            let mut socket = socket.try_clone().map_err(|e| e.to_string())?;
            let stop = stop.clone();
            let ring = ring.clone();
            let counters = counters.clone();
            threads.push(std::thread::spawn(move || {
                let mut up_sampler = Resampler::new(SCO_RATE, rate);
                let mut buffer: Vec<u8> = Vec::new();
                let mut chunk = [0u8; 4096];
                while !stop.load(Ordering::Relaxed) {
                    match socket.read(&mut chunk) {
                        Ok(0) => break,
                        Ok(n) => buffer.extend_from_slice(&chunk[..n]),
                        Err(e) if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut | ErrorKind::Interrupted) => continue,
                        Err(_) => break,
                    }
                    while buffer.len() >= HEADER {
                        let length = u16::from_be_bytes([buffer[2], buffer[3]]) as usize;
                        if buffer[0] != MAGIC || length > 512 || length % 2 == 1 || !matches!(buffer[1], DOWN | UP) {
                            return; // cannot resynchronise; the call carries on without sound
                        }
                        if buffer.len() < HEADER + length {
                            break;
                        }
                        if buffer[1] == DOWN {
                            let samples: Vec<i16> = buffer[HEADER..HEADER + length]
                                .chunks_exact(2)
                                .map(|b| i16::from_le_bytes([b[0], b[1]]))
                                .collect();
                            let mut out = Vec::with_capacity(samples.len() * 6);
                            up_sampler.process(&samples, &mut out);
                            lock(&ring).push(&out);
                            counters.down.fetch_add(1, Ordering::Relaxed);
                        }
                        buffer.drain(..HEADER + length);
                    }
                }
            }));
        }

        // Microphone to phone.
        {
            let mut socket = socket.try_clone().map_err(|e| e.to_string())?;
            let stop = stop.clone();
            let counters = counters.clone();
            threads.push(std::thread::spawn(move || {
                let mut sequence = 0u32;
                while !stop.load(Ordering::Relaxed) {
                    match rx.recv_timeout(Duration::from_millis(100)) {
                        Ok(samples) => {
                            sequence = sequence.wrapping_add(1);
                            let payload: Vec<u8> = samples.iter().flat_map(|s| s.to_le_bytes()).collect();
                            if socket.write_all(&frame(UP, sequence, &payload)).is_err() {
                                break;
                            }
                            counters.up.fetch_add(1, Ordering::Relaxed);
                        }
                        Err(RecvTimeoutError::Timeout) => {}
                        Err(RecvTimeoutError::Disconnected) => break,
                    }
                }
            }));
        }

        Ok(Self {
            stop,
            socket,
            threads,
            device: Some(running),
            ring,
            counters,
            echo_cancelled,
            rate,
        })
    }

    pub fn stats(&self) -> AudioStats {
        let ring = lock(&self.ring);
        AudioStats {
            down_frames: self.counters.down.load(Ordering::Relaxed),
            up_frames: self.counters.up.load(Ordering::Relaxed),
            up_dropped: self.counters.up_dropped.load(Ordering::Relaxed),
            playback_dropped: ring.dropped,
            underruns: ring.underruns,
            echo_cancelled: self.echo_cancelled,
            device_rate: self.rate,
        }
    }

    pub fn stop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        self.device.take(); // sound stops first
        let _ = self.socket.shutdown(std::net::Shutdown::Both);
        for t in self.threads.drain(..) {
            let _ = t.join();
        }
    }
}

impl Drop for CallAudio {
    fn drop(&mut self) {
        self.stop();
    }
}

/// Waits until `check` is true or the time is up.
#[cfg(test)]
pub(crate) fn until(limit: Duration, mut check: impl FnMut() -> bool) -> bool {
    use std::time::Instant;
    let end = Instant::now() + limit;
    while Instant::now() < end {
        if check() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    check()
}

#[cfg(test)]
pub(crate) mod fake {
    use super::*;

    /// A device that records what is played and lets the test feed the microphone.
    pub struct FakeDevice {
        pub rate: u32,
        pub echo: bool,
        pub fail: bool,
        pub capture: Arc<Mutex<Option<CaptureFn>>>,
        pub render: Arc<Mutex<Option<RenderFn>>>,
        pub started: Arc<AtomicU64>,
        pub stopped: Arc<AtomicU64>,
    }

    impl FakeDevice {
        pub fn new(rate: u32, echo: bool) -> Self {
            Self {
                rate,
                echo,
                fail: false,
                capture: Arc::new(Mutex::new(None)),
                render: Arc::new(Mutex::new(None)),
                started: Arc::new(AtomicU64::new(0)),
                stopped: Arc::new(AtomicU64::new(0)),
            }
        }
        pub fn mic(&self, samples: &[i16]) {
            if let Some(f) = lock(&self.capture).as_mut() {
                f(samples);
            }
        }
        pub fn speaker(&self, n: usize) -> Vec<i16> {
            let mut out = vec![0i16; n];
            if let Some(f) = lock(&self.render).as_mut() {
                f(&mut out);
            }
            out
        }
    }

    struct Handle(Arc<AtomicU64>);
    impl Running for Handle {}
    impl Drop for Handle {
        fn drop(&mut self) {
            self.0.fetch_add(1, Ordering::SeqCst);
        }
    }

    impl AudioDevice for FakeDevice {
        fn start(&self, capture: CaptureFn, render: RenderFn) -> Result<(u32, bool, Box<dyn Running>), String> {
            if self.fail {
                return Err("no audio unit".into());
            }
            self.started.fetch_add(1, Ordering::SeqCst);
            *lock(&self.capture) = Some(capture);
            *lock(&self.render) = Some(render);
            Ok((self.rate, self.echo, Box::new(Handle(self.stopped.clone()))))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::fake::FakeDevice;
    use super::*;
    use std::os::unix::net::UnixListener;

    fn listener(name: &str) -> (UnixListener, std::path::PathBuf) {
        let path = std::path::PathBuf::from(format!("/tmp/gba{}{}", std::process::id(), name));
        let _ = std::fs::remove_file(&path);
        (UnixListener::bind(&path).unwrap(), path)
    }

    fn send_down(peer: &mut UnixStream, seq: u32, samples: &[i16]) {
        let payload: Vec<u8> = samples.iter().flat_map(|s| s.to_le_bytes()).collect();
        peer.write_all(&frame(DOWN, seq, &payload)).unwrap();
    }

    fn read_frame(peer: &mut UnixStream) -> (u8, u32, Vec<u8>) {
        let mut h = [0u8; HEADER];
        peer.read_exact(&mut h).unwrap();
        assert_eq!(h[0], MAGIC);
        let len = u16::from_be_bytes([h[2], h[3]]) as usize;
        let mut payload = vec![0u8; len];
        peer.read_exact(&mut payload).unwrap();
        (h[1], u32::from_be_bytes([h[4], h[5], h[6], h[7]]), payload)
    }

    #[test]
    fn phone_audio_reaches_the_speaker_at_the_device_rate() {
        let (l, path) = listener("a");
        let device = FakeDevice::new(16000, true);
        let audio = CallAudio::start(&path, &device).unwrap();
        let (mut peer, _) = l.accept().unwrap();
        for i in 0..6 {
            send_down(&mut peer, i, &[4000; 120]); // 15 ms each
        }
        assert!(until(Duration::from_secs(2), || audio.stats().down_frames == 6));
        let played = device.speaker(1600); // 100 ms at 16 kHz
        assert!(played.iter().filter(|&&s| s == 4000).count() >= 1400, "most of 90 ms of audio is played");
        assert!(audio.stats().echo_cancelled);
        assert_eq!(audio.stats().device_rate, 16000);
    }

    #[test]
    fn microphone_audio_is_sent_as_8k_frames_with_rising_sequence() {
        let (l, path) = listener("b");
        let device = FakeDevice::new(16000, true);
        let _audio = CallAudio::start(&path, &device).unwrap();
        let (mut peer, _) = l.accept().unwrap();
        peer.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        for _ in 0..4 {
            device.mic(&[1234; 480]); // 30 ms at 16 kHz = 240 samples at 8 kHz = 2 frames
        }
        let mut sequences = Vec::new();
        for _ in 0..8 {
            let (kind, seq, payload) = read_frame(&mut peer);
            assert_eq!(kind, UP);
            assert_eq!(payload.len(), UP_FRAME);
            let first = i16::from_le_bytes([payload[0], payload[1]]);
            assert!((first - 1234).abs() <= 1 || sequences.is_empty(), "{first}");
            sequences.push(seq);
        }
        assert_eq!(sequences, (1..=8).collect::<Vec<u32>>());
    }

    #[test]
    fn without_echo_cancellation_the_microphone_is_muted_while_the_speaker_is_loud() {
        let (l, path) = listener("c");
        let device = FakeDevice::new(8000, false);
        let audio = CallAudio::start(&path, &device).unwrap();
        let (mut peer, _) = l.accept().unwrap();
        for i in 0..6 {
            send_down(&mut peer, i, &[6000; 120]);
        }
        assert!(until(Duration::from_secs(2), || audio.stats().down_frames == 6));
        device.speaker(240); // loud audio is playing
        device.mic(&[100; 120]);
        std::thread::sleep(Duration::from_millis(200));
        assert_eq!(audio.stats().up_frames, 0, "the microphone is gated while the caller is loud");
        device.speaker(2000); // the rest plays out
        device.speaker(100); // and the speaker is quiet
        device.mic(&[100; 120]);
        assert!(until(Duration::from_secs(2), || audio.stats().up_frames == 1));
    }

    #[test]
    fn with_echo_cancellation_the_microphone_is_never_gated() {
        let (l, path) = listener("d");
        let device = FakeDevice::new(8000, true);
        let audio = CallAudio::start(&path, &device).unwrap();
        let (mut peer, _) = l.accept().unwrap();
        for i in 0..6 {
            send_down(&mut peer, i, &[6000; 120]);
        }
        assert!(until(Duration::from_secs(2), || audio.stats().down_frames == 6));
        device.speaker(240);
        device.mic(&[100; 120]);
        assert!(until(Duration::from_secs(2), || audio.stats().up_frames == 1));
    }

    #[test]
    fn a_device_that_will_not_start_is_an_error_and_nothing_is_left_running() {
        let (_l, path) = listener("e");
        let mut device = FakeDevice::new(16000, true);
        device.fail = true;
        assert!(CallAudio::start(&path, &device).is_err());
    }

    #[test]
    fn a_missing_voice_socket_is_an_error() {
        let device = FakeDevice::new(16000, true);
        assert!(CallAudio::start(Path::new("/tmp/gba-none"), &device).is_err());
        assert_eq!(device.started.load(Ordering::SeqCst), 0, "the device is not started without a socket");
    }

    #[test]
    fn stopping_stops_the_device_and_the_threads_and_can_be_done_twice() {
        let (l, path) = listener("f");
        let device = FakeDevice::new(16000, true);
        let mut audio = CallAudio::start(&path, &device).unwrap();
        let (_peer, _) = l.accept().unwrap();
        audio.stop();
        audio.stop();
        assert_eq!(device.stopped.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn a_bad_frame_header_ends_phone_audio_without_a_crash() {
        let (l, path) = listener("g");
        let device = FakeDevice::new(8000, true);
        let audio = CallAudio::start(&path, &device).unwrap();
        let (mut peer, _) = l.accept().unwrap();
        peer.write_all(&[0u8; 16]).unwrap();
        std::thread::sleep(Duration::from_millis(300));
        assert_eq!(audio.stats().down_frames, 0);
    }

    #[test]
    fn frames_split_across_reads_are_put_together() {
        let (l, path) = listener("h");
        let device = FakeDevice::new(8000, true);
        let audio = CallAudio::start(&path, &device).unwrap();
        let (mut peer, _) = l.accept().unwrap();
        let payload: Vec<u8> = [2000i16; 120].iter().flat_map(|s| s.to_le_bytes()).collect();
        let whole = frame(DOWN, 1, &payload);
        peer.write_all(&whole[..5]).unwrap();
        std::thread::sleep(Duration::from_millis(50));
        peer.write_all(&whole[5..100]).unwrap();
        std::thread::sleep(Duration::from_millis(50));
        peer.write_all(&whole[100..]).unwrap();
        assert!(until(Duration::from_secs(2), || audio.stats().down_frames == 1));
    }
}

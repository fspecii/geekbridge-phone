// SPDX-License-Identifier: GPL-3.0-or-later
//! Sample-rate conversion and the playback buffer. Plain logic, no sound hardware.

use std::collections::VecDeque;

/// Streaming linear resampler for mono 16-bit audio. Keeps its position between calls, so
/// chunk boundaries make no click. Linear is enough for 8 kHz speech; the audio unit already
/// band-limits what it gives us.
pub struct Resampler {
    step: f64,
    pos: f64,
    last: i16,
    primed: bool,
}

impl Resampler {
    pub fn new(from: u32, to: u32) -> Self {
        Self { step: from as f64 / to as f64, pos: 0.0, last: 0, primed: false }
    }

    pub fn process(&mut self, input: &[i16], out: &mut Vec<i16>) {
        if input.is_empty() {
            return;
        }
        // Position 0 is the previous chunk's last sample; position k is input[k - 1].
        if !self.primed {
            self.last = input[0];
            self.primed = true;
        }
        let at = |i: usize| if i == 0 { self.last } else { input[i - 1] };
        while self.pos < input.len() as f64 {
            let i = self.pos.floor() as usize;
            let frac = self.pos - i as f64;
            let a = at(i) as f64;
            let b = at(i + 1) as f64;
            out.push((a + (b - a) * frac).round() as i16);
            self.pos += self.step;
        }
        self.pos -= input.len() as f64;
        self.last = *input.last().unwrap();
    }
}

/// Bounded playback FIFO. Overflow drops the oldest sound; running dry plays silence. Playback
/// starts only once a little sound is queued, and again after every dry spell.
pub struct PlaybackRing {
    samples: VecDeque<i16>,
    capacity: usize,
    prefill: usize,
    playing: bool,
    pub dropped: u64,
    pub underruns: u64,
    pub last_loud: bool,
}

impl PlaybackRing {
    pub fn new(rate: u32, capacity_ms: u32, prefill_ms: u32) -> Self {
        Self {
            samples: VecDeque::new(),
            capacity: (rate * capacity_ms / 1000) as usize,
            prefill: (rate * prefill_ms / 1000) as usize,
            playing: false,
            dropped: 0,
            underruns: 0,
            last_loud: false,
        }
    }

    pub fn push(&mut self, data: &[i16]) {
        self.samples.extend(data.iter().copied());
        if self.samples.len() > self.capacity {
            let over = self.samples.len() - self.capacity;
            self.samples.drain(..over);
            self.dropped += over as u64;
        }
    }

    pub fn pull(&mut self, out: &mut [i16]) {
        if !self.playing && self.samples.len() >= self.prefill {
            self.playing = true;
        }
        let mut peak = 0i32;
        for slot in out.iter_mut() {
            *slot = if self.playing {
                match self.samples.pop_front() {
                    Some(s) => s,
                    None => {
                        self.playing = false;
                        self.underruns += 1;
                        0
                    }
                }
            } else {
                0
            };
            peak = peak.max((*slot as i32).abs());
        }
        self.last_loud = peak > 800;
    }

    pub fn len(&self) -> usize {
        self.samples.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ramp(n: usize) -> Vec<i16> {
        (0..n).map(|i| (i * 100) as i16).collect()
    }

    #[test]
    fn upsampling_doubles_the_count_and_stays_between_neighbours() {
        let mut r = Resampler::new(8000, 16000);
        let mut out = Vec::new();
        r.process(&ramp(80), &mut out);
        assert_eq!(out.len(), 160);
        assert!(out.windows(2).all(|w| w[1] >= w[0]), "a rising ramp stays rising");
    }

    #[test]
    fn downsampling_halves_the_count() {
        let mut r = Resampler::new(16000, 8000);
        let mut out = Vec::new();
        r.process(&ramp(160), &mut out);
        assert_eq!(out.len(), 80);
    }

    #[test]
    fn chunking_does_not_change_the_result() {
        let input = ramp(480);
        for (from, to) in [(8000, 16000), (16000, 8000), (8000, 24000), (48000, 8000), (8000, 8000)] {
            let mut whole = Vec::new();
            Resampler::new(from, to).process(&input, &mut whole);
            let mut parts = Vec::new();
            let mut r = Resampler::new(from, to);
            for chunk in input.chunks(37) {
                r.process(chunk, &mut parts);
            }
            assert_eq!(whole, parts, "{from} to {to}");
        }
    }

    #[test]
    fn long_runs_keep_the_ratio_without_drift() {
        let mut r = Resampler::new(8000, 48000);
        let mut out = Vec::new();
        for _ in 0..1000 {
            r.process(&[1000; 120], &mut out);
        }
        assert_eq!(out.len(), 120 * 1000 * 6);
    }

    #[test]
    fn a_constant_stays_constant() {
        let mut r = Resampler::new(8000, 16000);
        let mut out = Vec::new();
        r.process(&[500; 50], &mut out);
        assert!(out.iter().all(|&s| s == 500));
    }

    #[test]
    fn the_ring_waits_for_its_prefill_then_plays_in_order() {
        let mut ring = PlaybackRing::new(8000, 200, 20); // prefill 160 samples
        ring.push(&[7; 100]);
        let mut out = [1i16; 50];
        ring.pull(&mut out);
        assert!(out.iter().all(|&s| s == 0), "silence until enough is queued");
        ring.push(&[9; 100]);
        ring.pull(&mut out);
        assert_eq!(&out[..], &[7; 50][..]);
    }

    #[test]
    fn the_ring_drops_the_oldest_when_full() {
        let mut ring = PlaybackRing::new(8000, 10, 0); // 80 samples
        ring.push(&(0..100).collect::<Vec<i16>>());
        assert_eq!(ring.len(), 80);
        assert_eq!(ring.dropped, 20);
        let mut out = [0i16; 1];
        ring.pull(&mut out);
        assert_eq!(out[0], 20);
    }

    #[test]
    fn running_dry_counts_one_underrun_and_waits_for_a_refill() {
        let mut ring = PlaybackRing::new(8000, 200, 10); // prefill 80
        ring.push(&[5; 80]);
        let mut out = [0i16; 100];
        ring.pull(&mut out);
        assert_eq!(ring.underruns, 1);
        assert_eq!(out[79], 5);
        assert_eq!(out[80], 0);
        ring.push(&[5; 20]);
        ring.pull(&mut out[..20]);
        assert!(out[..20].iter().all(|&s| s == 0), "not restarted until the prefill is back");
    }

    #[test]
    fn loudness_is_tracked_for_the_half_duplex_fallback() {
        let mut ring = PlaybackRing::new(8000, 200, 0);
        ring.push(&[3000; 10]);
        let mut out = [0i16; 10];
        ring.pull(&mut out);
        assert!(ring.last_loud);
        ring.pull(&mut out);
        assert!(!ring.last_loud);
    }
}

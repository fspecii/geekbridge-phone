// SPDX-License-Identifier: GPL-3.0-or-later
//! The Mac's microphone and speakers (or headphones) through `cpal`.
//!
//! The default input and output devices at the moment a call starts, each at its own sample
//! rate, mono 16-bit toward the rest of the pass-through. The streams are not `Send`, so they
//! live on a thread of their own that waits to be told to stop.

use super::sound::{Capture, Io, Render, Started};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::SampleFormat;
use std::sync::mpsc;

pub struct CpalIo;

/// Dropping it stops the streams.
struct Running {
    stop: Option<mpsc::Sender<()>>,
    thread: Option<std::thread::JoinHandle<()>>,
}

impl Drop for Running {
    fn drop(&mut self) {
        drop(self.stop.take());
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}

fn to_i16(x: f32) -> i16 {
    (x.clamp(-1.0, 1.0) * 32767.0) as i16
}

fn build(
    mut capture: Capture,
    mut render: Render,
) -> Result<(cpal::Stream, cpal::Stream, Started2), String> {
    let host = cpal::default_host();
    let input = host
        .default_input_device()
        .ok_or("this Mac has no sound input")?;
    let output = host
        .default_output_device()
        .ok_or("this Mac has no sound output")?;
    let input_config = input
        .default_input_config()
        .map_err(|e| format!("the sound input: {e}"))?;
    let output_config = output
        .default_output_config()
        .map_err(|e| format!("the sound output: {e}"))?;
    let (in_channels, out_channels) = (
        input_config.channels() as usize,
        output_config.channels() as usize,
    );
    let (input_rate, output_rate) = (input_config.sample_rate(), output_config.sample_rate());
    let echo_risk = output
        .description()
        .map(|d| d.name().to_lowercase().contains("speaker"))
        .unwrap_or(false);
    let on_error = |e: cpal::Error| log::warn!("call sound: {e}");

    let input_stream = match input_config.sample_format() {
        SampleFormat::F32 => input.build_input_stream(
            input_config.config(),
            move |data: &[f32], _: &cpal::InputCallbackInfo| {
                let mono: Vec<i16> = data.chunks(in_channels).map(|f| to_i16(f[0])).collect();
                capture(&mono);
            },
            on_error,
            None,
        ),
        SampleFormat::I16 => input.build_input_stream(
            input_config.config(),
            move |data: &[i16], _: &cpal::InputCallbackInfo| {
                let mono: Vec<i16> = data.chunks(in_channels).map(|f| f[0]).collect();
                capture(&mono);
            },
            on_error,
            None,
        ),
        other => {
            return Err(format!(
                "the sound input uses {other}, which is not supported"
            ))
        }
    }
    .map_err(|e| format!("the sound input would not start: {e}"))?;

    let mut mono: Vec<i16> = Vec::new();
    let output_stream = match output_config.sample_format() {
        SampleFormat::F32 => output.build_output_stream(
            output_config.config(),
            move |data: &mut [f32], _: &cpal::OutputCallbackInfo| {
                mono.resize(data.len() / out_channels, 0);
                render(&mut mono);
                for (frame, sample) in data.chunks_mut(out_channels).zip(&mono) {
                    frame.fill(*sample as f32 / 32768.0);
                }
            },
            on_error,
            None,
        ),
        SampleFormat::I16 => output.build_output_stream(
            output_config.config(),
            move |data: &mut [i16], _: &cpal::OutputCallbackInfo| {
                mono.resize(data.len() / out_channels, 0);
                render(&mut mono);
                for (frame, sample) in data.chunks_mut(out_channels).zip(&mono) {
                    frame.fill(*sample);
                }
            },
            on_error,
            None,
        ),
        other => {
            return Err(format!(
                "the sound output uses {other}, which is not supported"
            ))
        }
    }
    .map_err(|e| format!("the sound output would not start: {e}"))?;

    input_stream
        .play()
        .map_err(|e| format!("the sound input would not start: {e}"))?;
    output_stream
        .play()
        .map_err(|e| format!("the sound output would not start: {e}"))?;
    Ok((
        input_stream,
        output_stream,
        Started2 {
            input_rate,
            output_rate,
            echo_risk,
        },
    ))
}

struct Started2 {
    input_rate: u32,
    output_rate: u32,
    echo_risk: bool,
}

impl Io for CpalIo {
    fn start(&self, capture: Capture, render: Render) -> Result<Started, String> {
        let (ready_tx, ready_rx) = mpsc::channel::<Result<Started2, String>>();
        let (stop_tx, stop_rx) = mpsc::channel::<()>();
        let thread = std::thread::Builder::new()
            .name("gbp-call-sound".into())
            .spawn(move || match build(capture, render) {
                Ok((input, output, info)) => {
                    let _ = ready_tx.send(Ok(info));
                    let _ = stop_rx.recv(); // returns when the sender is dropped
                    drop((input, output));
                }
                Err(e) => {
                    let _ = ready_tx.send(Err(e));
                }
            })
            .map_err(|e| e.to_string())?;
        match ready_rx.recv() {
            Ok(Ok(info)) => Ok(Started {
                input_rate: info.input_rate,
                output_rate: info.output_rate,
                echo_risk: info.echo_risk,
                handle: Box::new(Running {
                    stop: Some(stop_tx),
                    thread: Some(thread),
                }),
            }),
            Ok(Err(e)) => {
                let _ = thread.join();
                Err(e)
            }
            Err(_) => Err("the sound thread ended before it started".into()),
        }
    }
}

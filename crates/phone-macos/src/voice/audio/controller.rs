// SPDX-License-Identifier: GPL-3.0-or-later
//! Starts the Mac's audio when a call becomes active and stops it when the call ends, so the
//! microphone is in use only during calls.

use super::bridge::{AudioDevice, CallAudio};
use crate::voice::client::HelperClient;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

#[derive(Default)]
struct Slot {
    audio: Option<CallAudio>,
    wanted: bool,
    error: Option<String>,
}

#[derive(Clone)]
pub struct CallAudioController {
    slot: Arc<Mutex<Slot>>,
}

fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

impl CallAudioController {
    /// Listens to the helper's call events. The returned controller stays valid for as long as
    /// the client does.
    pub fn attach(client: &HelperClient, voice_socket: PathBuf, device: Arc<dyn AudioDevice>) -> Self {
        let slot = Arc::new(Mutex::new(Slot::default()));
        let shared = slot.clone();
        client.subscribe(move |event| {
            if event.name != "call_state" {
                return;
            }
            let state = event.fields["state"].as_str().unwrap_or("");
            match state {
                "active" => {
                    lock(&shared).wanted = true;
                    let (shared, path, device) = (shared.clone(), voice_socket.clone(), device.clone());
                    std::thread::spawn(move || {
                        let mut slot = lock(&shared);
                        if !slot.wanted || slot.audio.is_some() {
                            return;
                        }
                        match CallAudio::start(&path, device.as_ref()) {
                            Ok(audio) => {
                                slot.error = None;
                                slot.audio = Some(audio);
                            }
                            Err(e) => {
                                log::error!("call audio did not start: {e}");
                                slot.error = Some(e);
                            }
                        }
                    });
                }
                "ended" | "idle" | "disconnected" | "none" => {
                    let mut slot = lock(&shared);
                    slot.wanted = false;
                    let audio = slot.audio.take();
                    drop(slot);
                    if let Some(mut audio) = audio {
                        std::thread::spawn(move || audio.stop());
                    }
                }
                _ => {}
            }
        });
        Self { slot }
    }

    pub fn is_running(&self) -> bool {
        lock(&self.slot).audio.is_some()
    }

    /// Why the last start failed, if it did.
    pub fn last_error(&self) -> Option<String> {
        lock(&self.slot).error.clone()
    }

    pub fn stats(&self) -> Option<super::AudioStats> {
        lock(&self.slot).audio.as_ref().map(|a| a.stats())
    }

    /// Stops any running audio now (used when voice mode is switched off).
    pub fn stop(&self) {
        let mut slot = lock(&self.slot);
        slot.wanted = false;
        if let Some(mut audio) = slot.audio.take() {
            drop(slot);
            audio.stop();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::super::bridge::fake::FakeDevice;
    use super::super::bridge::until;
    use super::*;
    use crate::voice::client::testing::FakeHelper;
    use serde_json::json;
    use std::os::unix::net::UnixListener;
    use std::sync::atomic::Ordering;
    use std::time::Duration;

    fn setup(tag: &str, fail: bool) -> (FakeHelper, HelperClient, CallAudioController, Arc<FakeDevice>, UnixListener) {
        let path = PathBuf::from(format!("/tmp/gbc{}{}", std::process::id(), tag));
        let _ = std::fs::remove_file(&path);
        let listener = UnixListener::bind(&path).unwrap();
        let helper = FakeHelper::start(Arc::new(|_| Some((json!({"ok": true}), None))));
        let client = HelperClient::connect(helper.socket()).unwrap();
        client.request("status", json!({}), Duration::from_secs(5)).unwrap(); // the fake has accepted us
        let mut device = FakeDevice::new(16000, true);
        device.fail = fail;
        let device = Arc::new(device);
        let controller = CallAudioController::attach(&client, path, device.clone());
        (helper, client, controller, device, listener)
    }

    fn state(helper: &FakeHelper, s: &str) {
        helper.send_event(json!({"event": "call_state", "id": "call-1", "state": s, "number": ""}));
    }

    #[test]
    fn audio_starts_when_the_call_is_active_and_stops_when_it_ends() {
        let (helper, _client, controller, device, _l) = setup("a", false);
        state(&helper, "incoming");
        std::thread::sleep(Duration::from_millis(150));
        assert!(!controller.is_running(), "ringing does not open the microphone");
        state(&helper, "active");
        assert!(until(Duration::from_secs(3), || controller.is_running()));
        assert_eq!(device.started.load(Ordering::SeqCst), 1);
        state(&helper, "ended");
        assert!(until(Duration::from_secs(3), || !controller.is_running() && device.stopped.load(Ordering::SeqCst) == 1));
    }

    #[test]
    fn repeated_active_events_start_one_session() {
        let (helper, _client, controller, device, _l) = setup("b", false);
        for _ in 0..3 {
            state(&helper, "active");
        }
        assert!(until(Duration::from_secs(3), || controller.is_running()));
        std::thread::sleep(Duration::from_millis(200));
        assert_eq!(device.started.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn a_device_failure_is_kept_for_the_user_and_the_next_call_tries_again() {
        let (helper, _client, controller, _device, _l) = setup("c", true);
        state(&helper, "active");
        assert!(until(Duration::from_secs(3), || controller.last_error().is_some()));
        assert!(!controller.is_running());
    }

    #[test]
    fn an_end_that_arrives_before_the_start_finishes_leaves_nothing_running() {
        let (helper, _client, controller, device, _l) = setup("d", false);
        state(&helper, "active");
        state(&helper, "ended");
        std::thread::sleep(Duration::from_millis(500));
        let started = device.started.load(Ordering::SeqCst);
        let stopped = device.stopped.load(Ordering::SeqCst);
        assert!(!controller.is_running(), "started {started} stopped {stopped}");
        assert_eq!(started, stopped);
    }

    #[test]
    fn stop_ends_a_running_session() {
        let (helper, _client, controller, device, _l) = setup("e", false);
        state(&helper, "active");
        assert!(until(Duration::from_secs(3), || controller.is_running()));
        controller.stop();
        assert!(!controller.is_running());
        assert_eq!(device.stopped.load(Ordering::SeqCst), 1);
    }
}

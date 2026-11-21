// SPDX-License-Identifier: GPL-3.0-or-later
//! One run of the pass-through: from pointing the daemon at it to putting everything back.

use super::acipc::Acipc;
use super::audio::CpalIo;
use super::coreaudio::Routes;
use super::helper::{self, Helper, Mode};
use super::policy::{Address, Event, Policy};
use super::pty;
use super::pump::{self, Observer};
use super::sound::CallSound;
use crate::ffi;
use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::process::{Child, Command};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

/// How long the daemon may take to point at the pass-through, and the controller to open.
const GO_WAIT: Duration = Duration::from_secs(20);
const OPEN_WAIT: Duration = Duration::from_secs(8);
/// Polled this fast on purpose: the controller is free only from the old daemon's exit to the
/// new one's reset of the chip, a few hundred milliseconds.
const OPEN_POLL: Duration = Duration::from_millis(5);
const READY_WAIT: Duration = Duration::from_secs(40);
/// The helper's own timer puts Bluetooth back this long after the last `extend`; the app extends
/// it every half hour, so a dead app is undone even if its guard is too.
const TTL_SECS: u32 = 3 * 3600;
const EXTEND_EVERY: Duration = Duration::from_secs(1800);

pub struct Config {
    /// The phone whose call audio is kept.
    pub phone: String,
    /// Other paired devices to reconnect after the daemon restarts (headphones), which do not
    /// all come back by themselves.
    pub reconnect: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Status {
    Starting,
    Running,
    Failed(String),
    Stopped,
}

pub struct Passthrough {
    stop: Arc<AtomicBool>,
    status: Arc<Mutex<Status>>,
    threads: Vec<JoinHandle<()>>,
    guard: Option<Child>,
    helper: Helper,
}

fn set(status: &Mutex<Status>, value: Status) {
    *status.lock().unwrap_or_else(|e| e.into_inner()) = value;
}

/// Which devices have had a link come up since the daemon restarted.
#[derive(Clone, Default)]
struct Seen(Arc<Mutex<HashSet<[u8; 6]>>>);

impl Observer for Seen {
    fn event(&mut self, event: &Event) {
        match event {
            Event::LinkUp { address } => {
                self.0
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .insert(address.0);
            }
            Event::PhoneVoiceRequested { link_type } => {
                log::info!("the phone asks for call audio (link type {link_type})")
            }
            Event::PhoneVoiceOpened { handle } => {
                log::info!("the phone's call audio is open on handle {handle:#x}")
            }
            Event::PhoneVoiceFailed { status } => {
                log::warn!("the phone's call audio failed to open (status {status:#x})")
            }
            Event::PhoneVoiceClosed { reason } => {
                log::info!("the phone's call audio closed (reason {reason:#x})")
            }
            Event::PriorityRequested { handle, enable } => {
                log::debug!("link {handle:#x} high priority: {enable}")
            }
            Event::PriorityAnswered { status } if *status != 0 => {
                log::warn!("the controller refused a priority change (status {status:#x})")
            }
            Event::PriorityAnswered { .. } => {}
        }
    }
}

fn wait_for_go(stop: &AtomicBool) -> Result<(), String> {
    let end = Instant::now() + GO_WAIT;
    while !Path::new(helper::GO_FILE).exists() {
        if stop.load(Ordering::Relaxed) || Instant::now() >= end {
            return Err("the Bluetooth daemon was never pointed at the pass-through".into());
        }
        std::thread::sleep(OPEN_POLL);
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn pump_thread(
    master: std::fs::File,
    phone: Address,
    helper: Helper,
    stop: Arc<AtomicBool>,
    status: Arc<Mutex<Status>>,
    ready: mpsc::Sender<Result<(), String>>,
    seen: Seen,
    slave_keepalive: std::os::fd::OwnedFd,
) {
    let outcome = (|| -> Result<(Acipc, u32), String> {
        wait_for_go(&stop)?;
        Acipc::open_within(OPEN_WAIT, OPEN_POLL)
            .map_err(|e| format!("the Bluetooth controller would not open ({e})"))
    })();
    let (mut chip, tries) = match outcome {
        Ok(v) => v,
        Err(e) => {
            let _ = helper.revert();
            set(&status, Status::Failed(e.clone()));
            let _ = ready.send(Err(e));
            return;
        }
    };
    log::info!("the pass-through holds the controller (opened on try {tries})");
    set(&status, Status::Running);
    let _ = ready.send(Ok(()));
    let mut host = master;
    let mut policy = Policy::new(Some(phone), true);
    let mut sound = CallSound::new(Arc::new(CpalIo), Box::new(Routes::default()));
    let mut observer = seen;
    let result = pump::run(
        &mut host,
        &mut chip,
        &mut policy,
        &mut sound,
        &mut observer,
        &stop,
    );
    // The controller is closed before Bluetooth goes back, or the new daemon cannot open it.
    drop(chip);
    drop(sound);
    drop(slave_keepalive);
    drop(host);
    let _ = helper.revert();
    match result {
        Ok(()) if stop.load(Ordering::Relaxed) => set(&status, Status::Stopped),
        Ok(()) => set(
            &status,
            Status::Failed("the Bluetooth daemon let go of the pass-through".into()),
        ),
        Err(e) => {
            log::error!("the pass-through stopped: {e}");
            set(&status, Status::Failed(e.to_string()));
        }
    }
}

/// Brings the phone and the headphones back after the daemon restart, and keeps the helper's
/// timer from running out.
fn housekeeping(stop: Arc<AtomicBool>, seen: Seen, targets: Vec<String>, helper: Helper) {
    let began = Instant::now();
    let mut tried: HashMap<String, Instant> = HashMap::new();
    let mut extended = Instant::now();
    while !stop.load(Ordering::Relaxed) {
        let age = began.elapsed();
        if age > Duration::from_secs(6) && age < Duration::from_secs(180) {
            let up = seen.0.lock().unwrap_or_else(|e| e.into_inner()).clone();
            for text in &targets {
                let is_up = Address::parse(text).is_some_and(|a| up.contains(&a.0));
                if !is_up
                    && tried
                        .get(text)
                        .is_none_or(|t| t.elapsed() > Duration::from_secs(12))
                {
                    tried.insert(text.clone(), Instant::now());
                    ffi::connect(text);
                }
            }
        }
        if extended.elapsed() >= EXTEND_EVERY {
            extended = Instant::now();
            if let Err(e) = helper.extend(TTL_SECS) {
                log::warn!("could not extend the helper's timer: {e}");
            }
        }
        std::thread::sleep(Duration::from_millis(500));
    }
}

impl Passthrough {
    /// Points the Bluetooth daemon at the pass-through and waits until it holds the controller.
    /// On any failure Bluetooth is back on its normal path when this returns.
    pub fn start(config: Config, helper: Helper) -> Result<Self, String> {
        let phone = Address::parse(&config.phone).ok_or("the phone's address is not valid")?;
        if helper.mode().is_ok_and(|m| m == Mode::Passthrough) {
            // A run that ended badly left the daemon pointed at nothing.
            let _ = helper.revert();
            std::thread::sleep(Duration::from_secs(6));
        }
        let pty = pty::open().map_err(|e| format!("could not make the serial port: {e}"))?;
        let stop = Arc::new(AtomicBool::new(false));
        let status = Arc::new(Mutex::new(Status::Starting));
        let seen = Seen::default();
        let (ready_tx, ready_rx) = mpsc::channel();
        let mut threads = Vec::new();
        {
            let (helper, stop, status, seen) =
                (helper.clone(), stop.clone(), status.clone(), seen.clone());
            let (master, slave) = (pty.master, pty._slave);
            threads.push(
                std::thread::Builder::new()
                    .name("gbp-passthrough".into())
                    .spawn(move || {
                        pump_thread(master, phone, helper, stop, status, ready_tx, seen, slave)
                    })
                    .map_err(|e| e.to_string())?,
            );
        }
        let this = Self {
            stop: stop.clone(),
            status: status.clone(),
            threads,
            guard: None,
            helper: helper.clone(),
        };
        // The pump thread polls for the go signal, so it is waiting before the daemon is touched.
        if let Err(e) = helper.start(&pty.path, TTL_SECS) {
            drop(this); // stops the thread, which reverts
            return Err(e);
        }
        match ready_rx.recv_timeout(READY_WAIT) {
            Ok(Ok(())) => {}
            Ok(Err(e)) => return Err(e), // `this` is dropped; the thread already reverted
            Err(_) => return Err("the pass-through did not start in time".into()),
        }
        let mut this = this;
        let mut targets = vec![config.phone.clone()];
        targets.extend(config.reconnect);
        let (h, s, st) = (helper.clone(), stop, seen);
        this.threads.push(
            std::thread::Builder::new()
                .name("gbp-housekeeping".into())
                .spawn(move || housekeeping(s, st, targets, h))
                .map_err(|e| e.to_string())?,
        );
        this.guard = std::env::current_exe().ok().and_then(|exe| {
            Command::new(exe)
                .arg("--macos-ptguard")
                .arg(std::process::id().to_string())
                .spawn()
                .ok()
        });
        Ok(this)
    }

    pub fn status(&self) -> Status {
        self.status
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }

    /// Stops the pass-through and puts Bluetooth back on its normal path.
    pub fn stop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        for t in self.threads.drain(..) {
            let _ = t.join();
        }
        let _ = self.helper.revert(); // harmless if the thread already did
        if let Some(mut guard) = self.guard.take() {
            let _ = guard.kill();
            let _ = guard.wait();
        }
    }
}

impl Drop for Passthrough {
    fn drop(&mut self) {
        self.stop();
    }
}

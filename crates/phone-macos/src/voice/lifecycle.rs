// SPDX-License-Identifier: GPL-3.0-or-later
//! Turning Mac voice mode on and off, and making sure system Bluetooth always comes back.
//!
//! Voice mode needs the built-in controller to itself, so system Bluetooth is switched off while
//! it runs. Three things keep that from stranding the user without Bluetooth:
//!
//! 1. a marker file, written before Bluetooth goes off and removed after it is back on, so the
//!    next start of the app knows to put things right after a crash or power cut;
//! 2. a guard process that watches the app and restores Bluetooth if the app dies;
//! 3. `disable`, which restores on the normal path, also when starting the helper fails.

use super::audio::{AudioDevice, CallAudioController};
use super::client::HelperClient;
use super::platform::VoiceModePlatform;
use serde_json::json;
use std::fs;
use std::io;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::time::{Duration, Instant};

/// How the system Bluetooth switch is read and set. Faked in tests.
pub trait Power: Send + Sync {
    fn is_on(&self) -> Option<bool>;
    fn set_on(&self, on: bool);
}

/// The real switch: the same call System Settings uses.
pub struct SystemPower;

type GetFn = unsafe extern "C" fn() -> libc::c_int;
type SetFn = unsafe extern "C" fn(libc::c_int);

impl SystemPower {
    fn symbols() -> Option<(GetFn, SetFn)> {
        // SAFETY: dlopen/dlsym on a system framework; the two functions have these signatures.
        unsafe {
            let path = std::ffi::CString::new(
                "/System/Library/Frameworks/IOBluetooth.framework/IOBluetooth",
            )
            .ok()?;
            let handle = libc::dlopen(path.as_ptr(), libc::RTLD_NOW);
            if handle.is_null() {
                return None;
            }
            let get = libc::dlsym(
                handle,
                c"IOBluetoothPreferenceGetControllerPowerState".as_ptr(),
            );
            let set = libc::dlsym(
                handle,
                c"IOBluetoothPreferenceSetControllerPowerState".as_ptr(),
            );
            if get.is_null() || set.is_null() {
                return None;
            }
            Some((
                std::mem::transmute::<*mut libc::c_void, GetFn>(get),
                std::mem::transmute::<*mut libc::c_void, SetFn>(set),
            ))
        }
    }
}

impl Power for SystemPower {
    fn is_on(&self) -> Option<bool> {
        let (get, _) = Self::symbols()?;
        // SAFETY: see `symbols`.
        Some(unsafe { get() } != 0)
    }
    fn set_on(&self, on: bool) {
        if let Some((_, set)) = Self::symbols() {
            // SAFETY: see `symbols`.
            unsafe { set(on as libc::c_int) }
        }
    }
}

#[derive(Debug)]
pub enum VoiceModeError {
    /// Bluetooth could not be switched off, or the helper did not start. Bluetooth is restored.
    Start(String),
}

impl std::fmt::Display for VoiceModeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let VoiceModeError::Start(m) = self;
        write!(f, "{m}")
    }
}

impl std::error::Error for VoiceModeError {}

#[derive(Clone)]
pub struct VoiceModeConfig {
    /// Python that runs the helper (the bundled one in the product).
    pub python: PathBuf,
    /// `btd.py`.
    pub helper_script: PathBuf,
    /// Private folder for the helper's sockets (kept short: Unix socket paths are limited).
    pub dir: PathBuf,
    /// Written while Bluetooth is off.
    pub marker: PathBuf,
    /// `--stack simulated` for tests; empty for the real stack.
    pub extra_args: Vec<String>,
    pub env: Vec<(String, String)>,
    /// Command that runs the guard (`app --macos-btguard`); `None` in tests.
    pub guard: Option<Vec<String>>,
    pub power: Arc<dyn Power>,
    /// The speakers and microphone for call voice; `None` leaves voice to someone else.
    pub audio: Option<Arc<dyn AudioDevice>>,
}

/// The helper protocol this app speaks (`PROTOCOL_VERSION` in the helper).
const HELPER_PROTOCOL: u64 = 1;
const SETTLE: Duration = Duration::from_secs(10);
const HELPER_START: Duration = Duration::from_secs(40);

fn wait_power(power: &dyn Power, on: bool) -> bool {
    let end = Instant::now() + SETTLE;
    while Instant::now() < end {
        if power.is_on() == Some(on) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    power.is_on() == Some(on)
}

/// Contents: what to restore (`on` or `off`).
fn write_marker(path: &Path, restore_on: bool) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(path, if restore_on { "on\n" } else { "off\n" })
}

fn read_marker(path: &Path) -> Option<bool> {
    fs::read_to_string(path).ok().map(|t| t.trim() != "off")
}

/// Called at app start. If the last run left Bluetooth off on purpose and died, put it back.
/// Returns true when it had to.
pub fn recover_on_start(marker: &Path, power: &dyn Power) -> bool {
    let Some(restore_on) = read_marker(marker) else {
        return false;
    };
    if restore_on {
        power.set_on(true);
        wait_power(power, true);
    }
    let _ = fs::remove_file(marker);
    true
}

fn pid_alive(pid: i32) -> bool {
    // SAFETY: signal 0 only checks that the process exists.
    unsafe { libc::kill(pid, 0) == 0 || *libc::__error() == libc::EPERM }
}

/// The guard process: waits for the app to go, then restores Bluetooth if the marker is still
/// there. Returns when the marker is gone (normal shutdown) or after restoring.
pub fn run_guard(parent: i32, marker: &Path, power: &dyn Power, poll: Duration) {
    loop {
        if !marker.exists() {
            return;
        }
        if !pid_alive(parent) {
            recover_on_start(marker, power);
            return;
        }
        std::thread::sleep(poll);
    }
}

/// Entry point of the guard process (`app --macos-btguard <parent pid> <marker>`).
pub fn guard_main(parent: i32, marker: &Path) {
    run_guard(parent, marker, &SystemPower, Duration::from_secs(1));
}

pub struct VoiceMode {
    config: VoiceModeConfig,
    helper: Option<Child>,
    guard: Option<Child>,
    client: Arc<HelperClient>,
    platform: Arc<VoiceModePlatform>,
    audio: Option<CallAudioController>,
    restore_on: bool,
    stopped: bool,
}

fn start_helper(config: &VoiceModeConfig) -> Result<(Child, Arc<HelperClient>), String> {
    fs::create_dir_all(&config.dir).map_err(|e| e.to_string())?;
    fs::set_permissions(&config.dir, fs::Permissions::from_mode(0o700))
        .map_err(|e| e.to_string())?;
    let mut command = Command::new(&config.python);
    command
        .arg("-I")
        // Never write bytecode: the helper lives inside the signed app bundle, and a new file there breaks the seal.
        .arg("-B")
        .arg(&config.helper_script)
        .arg("--dir")
        .arg(&config.dir)
        .args(&config.extra_args)
        .envs(config.env.iter().cloned())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    if let Some(parent) = config.helper_script.parent() {
        command.current_dir(parent);
    }
    let mut child = command
        .spawn()
        .map_err(|e| format!("could not start the Bluetooth helper: {e}"))?;
    let socket = config.dir.join("btd.sock");
    let end = Instant::now() + HELPER_START;
    loop {
        if let Ok(Some(status)) = child.try_wait() {
            return Err(format!("the Bluetooth helper stopped at start ({status})"));
        }
        if socket.exists() {
            if let Ok(client) = HelperClient::connect(&socket) {
                match client.request("hello", json!({}), Duration::from_secs(10)) {
                    Ok(reply) if reply.fields["protocol"] == json!(HELPER_PROTOCOL) => {
                        return Ok((child, Arc::new(client)))
                    }
                    Ok(reply) => {
                        let _ = child.kill();
                        let _ = child.wait();
                        return Err(format!(
                            "the Bluetooth helper speaks protocol {} but this app needs {HELPER_PROTOCOL}; reinstall the app",
                            reply.fields["protocol"]
                        ));
                    }
                    Err(e) => {
                        let _ = child.kill();
                        let _ = child.wait();
                        return Err(format!("the Bluetooth helper did not answer: {e}"));
                    }
                }
            }
        }
        if Instant::now() > end {
            let _ = child.kill();
            let _ = child.wait();
            return Err("the Bluetooth helper did not start in time".into());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

impl VoiceMode {
    /// Switches system Bluetooth off, starts the helper and connects to it. On any failure,
    /// Bluetooth is restored before the error returns.
    pub fn enable(config: VoiceModeConfig) -> Result<Self, VoiceModeError> {
        recover_on_start(&config.marker, config.power.as_ref());
        let restore_on = config.power.is_on().ok_or_else(|| {
            VoiceModeError::Start("Bluetooth is not available on this Mac.".into())
        })?;
        write_marker(&config.marker, restore_on)
            .map_err(|e| VoiceModeError::Start(e.to_string()))?;
        let guard = config.guard.as_ref().and_then(|cmd| {
            let (program, args) = cmd.split_first()?;
            Command::new(program)
                .args(args)
                .arg(std::process::id().to_string())
                .arg(&config.marker)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .ok()
        });
        let fail = |mut guard: Option<Child>, message: String| {
            if restore_on {
                config.power.set_on(true);
                wait_power(config.power.as_ref(), true);
            }
            let _ = fs::remove_file(&config.marker);
            if let Some(g) = guard.as_mut() {
                let _ = g.kill();
                let _ = g.wait();
            }
            VoiceModeError::Start(message)
        };
        if restore_on {
            config.power.set_on(false);
            if !wait_power(config.power.as_ref(), false) {
                return Err(fail(guard, "Bluetooth would not switch off.".into()));
            }
        }
        match start_helper(&config) {
            Ok((helper, client)) => {
                let platform = Arc::new(VoiceModePlatform::new(client.clone()));
                let audio = config.audio.clone().map(|device| {
                    CallAudioController::attach(&client, config.dir.join("voice.sock"), device)
                });
                Ok(Self {
                    audio,
                    config,
                    helper: Some(helper),
                    guard,
                    client,
                    platform,
                    restore_on,
                    stopped: false,
                })
            }
            Err(message) => Err(fail(guard, message)),
        }
    }

    pub fn platform(&self) -> Arc<VoiceModePlatform> {
        self.platform.clone()
    }

    /// The call audio controller, when the Mac's audio is in use.
    pub fn audio(&self) -> Option<&CallAudioController> {
        self.audio.as_ref()
    }

    /// True while the helper is running.
    pub fn is_running(&mut self) -> bool {
        !self.stopped
            && !self.client.is_closed()
            && self
                .helper
                .as_mut()
                .is_some_and(|h| matches!(h.try_wait(), Ok(None)))
    }

    /// Stops the helper, switches Bluetooth back on and removes the marker. Safe to call twice.
    pub fn disable(&mut self) {
        if self.stopped {
            return;
        }
        self.stopped = true;
        if let Some(audio) = &self.audio {
            audio.stop();
        }
        let _ = self
            .client
            .request("shutdown", json!({}), Duration::from_secs(5));
        self.client.close();
        if let Some(mut helper) = self.helper.take() {
            let end = Instant::now() + Duration::from_secs(10);
            while Instant::now() < end && matches!(helper.try_wait(), Ok(None)) {
                std::thread::sleep(Duration::from_millis(50));
            }
            if matches!(helper.try_wait(), Ok(None)) {
                let _ = helper.kill();
            }
            let _ = helper.wait();
        }
        if self.restore_on {
            self.config.power.set_on(true);
            wait_power(self.config.power.as_ref(), true);
        }
        let _ = fs::remove_file(&self.config.marker);
        if let Some(mut guard) = self.guard.take() {
            let _ = guard.kill();
            let _ = guard.wait();
        }
        let _ = fs::remove_dir_all(&self.config.dir);
    }
}

impl Drop for VoiceMode {
    fn drop(&mut self) {
        self.disable();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Mutex;

    #[derive(Default)]
    struct FakePower {
        on: AtomicBool,
        log: Mutex<Vec<bool>>,
        refuse_off: AtomicBool,
    }
    impl FakePower {
        fn new(on: bool) -> Arc<Self> {
            let p = Self::default();
            p.on.store(on, Ordering::SeqCst);
            Arc::new(p)
        }
    }
    impl Power for FakePower {
        fn is_on(&self) -> Option<bool> {
            Some(self.on.load(Ordering::SeqCst))
        }
        fn set_on(&self, on: bool) {
            self.log.lock().unwrap().push(on);
            if !on && self.refuse_off.load(Ordering::SeqCst) {
                return;
            }
            self.on.store(on, Ordering::SeqCst);
        }
    }

    fn root() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../helper/macos-btd")
    }

    fn python() -> Option<PathBuf> {
        let p = root().join(".venv/bin/python");
        p.exists().then_some(p)
    }

    fn temp(name: &str) -> PathBuf {
        let d = PathBuf::from(format!("/tmp/gbl{}{}", std::process::id(), name));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }

    fn config(power: Arc<FakePower>, tag: &str) -> (VoiceModeConfig, PathBuf) {
        let base = temp(tag);
        (
            VoiceModeConfig {
                python: python().unwrap_or_else(|| PathBuf::from("/nonexistent/python")),
                helper_script: root().join("btd.py"),
                dir: base.join("d"),
                marker: base.join("marker"),
                extra_args: vec![
                    "--stack".into(),
                    "simulated".into(),
                    "--grace".into(),
                    "5".into(),
                ],
                env: vec![],
                guard: None,
                power,
                audio: None,
            },
            base,
        )
    }

    #[test]
    fn the_marker_restores_bluetooth_after_a_crash() {
        let power = FakePower::new(false);
        let base = temp("a");
        let marker = base.join("marker");
        write_marker(&marker, true).unwrap();
        assert!(recover_on_start(&marker, power.as_ref()));
        assert_eq!(power.is_on(), Some(true));
        assert!(!marker.exists());
        assert!(
            !recover_on_start(&marker, power.as_ref()),
            "nothing to do the second time"
        );
    }

    #[test]
    fn a_marker_that_says_off_leaves_bluetooth_alone() {
        let power = FakePower::new(false);
        let base = temp("b");
        let marker = base.join("marker");
        write_marker(&marker, false).unwrap();
        recover_on_start(&marker, power.as_ref());
        assert_eq!(power.is_on(), Some(false));
        assert!(power.log.lock().unwrap().is_empty());
    }

    #[test]
    fn the_guard_restores_when_the_app_is_gone_and_quits_when_the_marker_is() {
        let power = FakePower::new(false);
        let base = temp("c");
        let marker = base.join("marker");
        write_marker(&marker, true).unwrap();
        // A process that has already exited stands in for a dead app.
        let mut dead = Command::new("true").spawn().unwrap();
        let pid = dead.id() as i32;
        dead.wait().unwrap();
        run_guard(pid, &marker, power.as_ref(), Duration::from_millis(20));
        assert_eq!(power.is_on(), Some(true));
        assert!(!marker.exists());
        // With the marker gone, the guard returns even though the "app" is alive.
        run_guard(
            std::process::id() as i32,
            &marker,
            power.as_ref(),
            Duration::from_millis(20),
        );
    }

    #[test]
    fn the_guard_waits_while_the_app_is_alive() {
        let power = FakePower::new(false);
        let base = temp("d");
        let marker = base.join("marker");
        write_marker(&marker, true).unwrap();
        let m = marker.clone();
        let p = power.clone();
        let handle = std::thread::spawn(move || {
            run_guard(
                std::process::id() as i32,
                &m,
                p.as_ref(),
                Duration::from_millis(20),
            )
        });
        std::thread::sleep(Duration::from_millis(200));
        assert_eq!(
            power.is_on(),
            Some(false),
            "nothing is touched while the app lives"
        );
        fs::remove_file(&marker).unwrap();
        handle.join().unwrap();
        assert_eq!(power.is_on(), Some(false));
    }

    #[test]
    fn a_helper_that_cannot_start_leaves_bluetooth_on() {
        let power = FakePower::new(true);
        let (mut cfg, base) = config(power.clone(), "e");
        cfg.python = PathBuf::from("/nonexistent/python");
        let err = VoiceMode::enable(cfg).err().expect("must fail");
        assert!(err.to_string().contains("helper"), "{err}");
        assert_eq!(power.is_on(), Some(true));
        assert!(!base.join("marker").exists());
        assert_eq!(*power.log.lock().unwrap(), vec![false, true]);
    }

    #[test]
    fn bluetooth_that_will_not_switch_off_is_reported_and_restored() {
        let power = FakePower::new(true);
        power.refuse_off.store(true, Ordering::SeqCst);
        let (cfg, base) = config(power.clone(), "f");
        let err = VoiceMode::enable(cfg).err().expect("must fail");
        assert!(err.to_string().contains("would not switch off"), "{err}");
        assert_eq!(power.is_on(), Some(true));
        assert!(!base.join("marker").exists());
    }

    #[test]
    fn the_real_helper_runs_end_to_end_and_bluetooth_comes_back() {
        let Some(_) = python() else {
            eprintln!("skipped: helper environment not built");
            return;
        };
        let power = FakePower::new(true);
        let (mut cfg, base) = config(power.clone(), "g");
        cfg.env = vec![("GBP_SIM_RING_AFTER".into(), "0.3".into())];
        let marker = base.join("marker");
        let mut mode = VoiceMode::enable(cfg).expect("start");
        assert_eq!(power.is_on(), Some(false), "off while voice mode runs");
        assert_eq!(read_marker(&marker), Some(true), "marker written while off");
        assert!(mode.is_running());
        let platform = mode.platform();
        use phone_core::platform::Platform;
        let phones = platform.phones().unwrap();
        assert_eq!(phones.len(), 1);
        let address = phones[0].address.clone();
        // The simulated phone rings; the call shows up and can be answered.
        let end = Instant::now() + Duration::from_secs(10);
        let mut seen = false;
        while Instant::now() < end {
            if platform
                .calls(&address)
                .unwrap()
                .iter()
                .any(|c| c.state.is_ringing())
            {
                seen = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        assert!(seen, "the simulated call never showed");
        platform.answer(&address, "call-1").unwrap();
        // Contacts and messages: a real descriptor with the simulated OBEX answer.
        {
            use std::io::{Read, Write};
            let mut stream = platform.open_pbap(&address).unwrap();
            stream.write_all(&[0x80, 0x00, 0x1a]).unwrap();
            stream.write_all(&[0u8; 23]).unwrap();
            let mut reply = [0u8; 7];
            stream.read_exact(&mut reply).unwrap();
            assert_eq!(reply, [0xa0, 0x00, 0x07, 0x10, 0x00, 0x20, 0x00]);
        }
        mode.disable();
        assert_eq!(power.is_on(), Some(true), "Bluetooth is back");
        assert!(!marker.exists());
        assert!(!mode.is_running());
        mode.disable(); // twice is fine
        assert_eq!(*power.log.lock().unwrap(), vec![false, true]);
    }

    #[test]
    fn call_audio_flows_through_the_real_helper_to_the_device_and_back() {
        use super::super::audio::bridge::fake::FakeDevice;
        let Some(_) = python() else {
            eprintln!("skipped: helper environment not built");
            return;
        };
        let power = FakePower::new(true);
        let (mut cfg, _base) = config(power.clone(), "h");
        cfg.env = vec![("GBP_SIM_RING_AFTER".into(), "0.3".into())];
        let device = Arc::new(FakeDevice::new(16000, true));
        cfg.audio = Some(device.clone());
        let mut mode = VoiceMode::enable(cfg).expect("start");
        let platform = mode.platform();
        use phone_core::platform::Platform;
        let address = platform.phones().unwrap()[0].address.clone();
        let end = Instant::now() + Duration::from_secs(10);
        while Instant::now() < end
            && !platform
                .calls(&address)
                .unwrap()
                .iter()
                .any(|c| c.state.is_ringing())
        {
            std::thread::sleep(Duration::from_millis(100));
        }
        assert!(
            mode.audio().unwrap().stats().is_none(),
            "no audio while it only rings"
        );
        platform.answer(&address, "call-1").unwrap();
        let audio = mode.audio().unwrap().clone();
        let end = Instant::now() + Duration::from_secs(10);
        while Instant::now() < end && audio.stats().map(|s| s.down_frames).unwrap_or(0) < 5 {
            std::thread::sleep(Duration::from_millis(50));
        }
        let stats = audio.stats().expect("audio running during the call");
        assert!(
            stats.down_frames >= 5,
            "phone audio reached the Mac: {stats:?}"
        );
        for _ in 0..10 {
            device.mic(&[500; 160]); // 10 ms at 16 kHz
        }
        let end = Instant::now() + Duration::from_secs(5);
        while Instant::now() < end && audio.stats().map(|s| s.up_frames).unwrap_or(0) < 3 {
            std::thread::sleep(Duration::from_millis(50));
        }
        assert!(
            audio.stats().unwrap().up_frames >= 3,
            "microphone audio left the Mac"
        );
        platform.hang_up(&address, "call-1").unwrap();
        let end = Instant::now() + Duration::from_secs(5);
        while Instant::now() < end && audio.is_running() {
            std::thread::sleep(Duration::from_millis(50));
        }
        assert!(!audio.is_running(), "audio stops with the call");
        mode.disable();
        assert_eq!(power.is_on(), Some(true));
    }

    #[test]
    fn removing_a_phone_then_scanning_and_pairing_works_through_the_real_helper() {
        let Some(_) = python() else {
            eprintln!("skipped: helper environment not built");
            return;
        };
        let power = FakePower::new(true);
        let (cfg, _base) = config(power.clone(), "i");
        let mut mode = VoiceMode::enable(cfg).expect("start");
        let platform = mode.platform();
        use phone_core::platform::Platform;
        let phones = platform.phones().unwrap();
        assert_eq!(phones.len(), 1);
        // Remove it: the list is empty, as the app's pairing window expects.
        platform.unpair(&phones[0].address).unwrap();
        assert!(platform.phones().unwrap().is_empty());
        // Scan finds the nearby phone that is not paired.
        let found = platform.scan_nearby(5).unwrap();
        assert_eq!(found.len(), 1);
        // Pair it: the code reaches the app while pairing runs.
        let codes = Arc::new(Mutex::new(Vec::new()));
        let seen = codes.clone();
        platform
            .pair(
                &found[0].id,
                Arc::new(move |code| seen.lock().unwrap().push(code)),
            )
            .unwrap();
        assert_eq!(*codes.lock().unwrap(), vec![Some("123456".to_string())]);
        // The new phone is listed and selected without a restart.
        let after = platform.phones().unwrap();
        assert_eq!(after.len(), 1);
        assert_eq!(after[0].address, found[0].id);
        assert_eq!(after[0].name, "Paired Phone");
        mode.disable();
        assert_eq!(power.is_on(), Some(true));
    }
}

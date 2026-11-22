// SPDX-License-Identifier: GPL-3.0-or-later
//! The one root piece: `btmode` (see `packaging/macos`), which points macOS's Bluetooth daemon
//! at the pass-through and back. Installed once behind the system's administrator prompt, then
//! run through `sudo -n` without a password.

use std::io;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Arc;

pub const HELPER_PATH: &str = "/usr/local/libexec/geekbridge/btmode";
/// What the installed helper's `version` must print. A different number means it is out of date.
pub const WANTED_VERSION: &str = "2";
/// Appears while the daemon is being pointed at the pass-through, before it is killed.
pub const GO_FILE: &str = "/var/run/geekbridge-btmode/go";

#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub enum State {
    /// Not installed, or not allowed through sudo for this user.
    Missing,
    /// Installed by an older version of the app.
    Outdated,
    Ready,
}

#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub enum Mode {
    Normal,
    Passthrough,
}

/// Runs `btmode` with arguments and returns its exit code and output. Faked in tests.
pub trait Runner: Send + Sync {
    fn run(&self, args: &[&str]) -> io::Result<(i32, String)>;
}

/// The real one: `sudo -n /usr/local/libexec/geekbridge/btmode ...`.
pub struct Sudo;

impl Runner for Sudo {
    fn run(&self, args: &[&str]) -> io::Result<(i32, String)> {
        let out = Command::new("/usr/bin/sudo")
            .arg("-n")
            .arg(HELPER_PATH)
            .args(args)
            .output()?;
        Ok((
            out.status.code().unwrap_or(-1),
            String::from_utf8_lossy(&out.stdout).trim().to_string(),
        ))
    }
}

#[derive(Clone)]
pub struct Helper {
    runner: Arc<dyn Runner>,
}

impl Helper {
    pub fn new(runner: Arc<dyn Runner>) -> Self {
        Self { runner }
    }

    pub fn real() -> Self {
        Self::new(Arc::new(Sudo))
    }

    pub fn state(&self) -> State {
        match self.runner.run(&["version"]) {
            Ok((0, v)) if v == WANTED_VERSION => State::Ready,
            // A helper from before `version` existed ran and answered with its usage text and
            // exit code 64; sudo refusing, or no helper at all, is a different failure.
            Ok((64, _)) | Ok((0, _)) => State::Outdated,
            _ => State::Missing,
        }
    }

    pub fn mode(&self) -> io::Result<Mode> {
        let (_, out) = self.runner.run(&["status"])?;
        Ok(if out == "pass-through" {
            Mode::Passthrough
        } else {
            Mode::Normal
        })
    }

    fn checked(&self, args: &[&str]) -> Result<(), String> {
        match self.runner.run(args) {
            Ok((0, _)) => Ok(()),
            Ok((code, out)) => Err(format!("btmode {} failed ({code}): {out}", args[0])),
            Err(e) => Err(format!("btmode {} did not run: {e}", args[0])),
        }
    }

    pub fn start(&self, port: &str, seconds: u32) -> Result<(), String> {
        self.checked(&["start", port, &seconds.to_string()])
    }

    pub fn extend(&self, seconds: u32) -> Result<(), String> {
        self.checked(&["extend", &seconds.to_string()])
    }

    /// Puts Bluetooth back on its normal path. Safe to call any number of times.
    pub fn revert(&self) -> Result<(), String> {
        self.checked(&["revert"])
    }
}

/// Quotes text for `/bin/sh`.
pub fn shell_quote(text: &str) -> String {
    format!("'{}'", text.replace('\'', "'\\''"))
}

/// Escapes text for the inside of an AppleScript double-quoted string.
pub fn applescript_escape(text: &str) -> String {
    text.replace('\\', "\\\\").replace('"', "\\\"")
}

/// The AppleScript that runs the installer behind the system's administrator prompt.
pub fn install_script(installer: &Path, user: &str) -> String {
    let command = format!(
        "/bin/sh {} --user {}",
        shell_quote(&installer.to_string_lossy()),
        shell_quote(user)
    );
    format!(
        "do shell script \"{}\" with prompt \"GeekBridge Phone wants to let your account switch this Mac's Bluetooth through its call bridge, so calls can be answered here while your mouse and headphones stay connected.\" with administrator privileges",
        applescript_escape(&command)
    )
}

/// Installs the helper. macOS asks for an administrator's password; that prompt is the consent.
pub fn install(installer: &Path) -> Result<(), String> {
    let user = Command::new("/usr/bin/id")
        .arg("-un")
        .output()
        .map_err(|e| e.to_string())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())?;
    if user.is_empty() || user == "root" {
        return Err("could not tell which account to allow".into());
    }
    let out = Command::new("/usr/bin/osascript")
        .arg("-e")
        .arg(install_script(installer, &user))
        .output()
        .map_err(|e| format!("could not ask for administrator approval: {e}"))?;
    if out.status.success() {
        Ok(())
    } else {
        let text = String::from_utf8_lossy(&out.stderr);
        if text.contains("-128") {
            Err("administrator approval was cancelled".into())
        } else {
            Err(format!("the helper did not install: {}", text.trim()))
        }
    }
}

/// Where the installer is: next to `btmode`, in the app bundle's resources, or `GBP_HELPER_DIR`.
pub fn installer_path() -> Option<PathBuf> {
    let mut candidates = Vec::new();
    if let Some(dir) = std::env::var_os("GBP_HELPER_DIR") {
        candidates.push(PathBuf::from(dir));
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(contents) = exe.parent().and_then(Path::parent) {
            candidates.push(contents.join("Resources/macos"));
        }
        // Running from a source checkout: target/{debug,release}/geekbridge-phone.
        if let Some(root) = exe.ancestors().nth(3) {
            candidates.push(root.join("packaging/macos"));
        }
    }
    candidates
        .into_iter()
        .map(|d| d.join("install-helper.sh"))
        .find(|p| p.exists() && p.with_file_name("btmode").exists())
}

/// The guard process: runs beside the app and puts Bluetooth back if the app dies while the
/// pass-through is on (a crash, a kill, a power cut of the app only). Without it the daemon
/// would be left talking to a serial port nobody reads.
pub fn guard_main(app_pid: i32, helper: &Helper) {
    loop {
        // SAFETY: signal 0 only checks that the process exists.
        let alive = unsafe { libc::kill(app_pid, 0) } == 0
            || io::Error::last_os_error().raw_os_error() == Some(libc::EPERM);
        if !alive {
            let _ = helper.revert();
            return;
        }
        std::thread::sleep(std::time::Duration::from_secs(1));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    struct Fake {
        calls: Mutex<Vec<Vec<String>>>,
        answers: Mutex<Vec<io::Result<(i32, String)>>>,
    }

    impl Fake {
        fn new(answers: Vec<io::Result<(i32, String)>>) -> Arc<Self> {
            Arc::new(Self {
                calls: Mutex::new(Vec::new()),
                answers: Mutex::new(answers),
            })
        }
    }

    impl Runner for Fake {
        fn run(&self, args: &[&str]) -> io::Result<(i32, String)> {
            self.calls
                .lock()
                .unwrap()
                .push(args.iter().map(|s| s.to_string()).collect());
            self.answers.lock().unwrap().remove(0)
        }
    }

    #[test]
    fn the_right_version_is_ready_and_an_error_is_missing() {
        assert_eq!(
            Helper::new(Fake::new(vec![Ok((0, "2".into()))])).state(),
            State::Ready
        );
        assert_eq!(
            Helper::new(Fake::new(vec![Ok((1, "a password is required".into()))])).state(),
            State::Missing
        );
        assert_eq!(
            Helper::new(Fake::new(vec![Ok((64, String::new()))])).state(),
            State::Outdated,
            "an older helper"
        );
        assert_eq!(
            Helper::new(Fake::new(vec![Ok((0, "1".into()))])).state(),
            State::Outdated,
            "a version this app does not know"
        );
        assert_eq!(
            Helper::new(Fake::new(vec![Err(io::Error::other("no sudo"))])).state(),
            State::Missing
        );
    }

    #[test]
    fn the_mode_reads_the_status_word() {
        assert_eq!(
            Helper::new(Fake::new(vec![Ok((0, "pass-through".into()))]))
                .mode()
                .unwrap(),
            Mode::Passthrough
        );
        assert_eq!(
            Helper::new(Fake::new(vec![Ok((0, "normal".into()))]))
                .mode()
                .unwrap(),
            Mode::Normal
        );
    }

    #[test]
    fn start_extend_and_revert_pass_their_arguments_and_report_failures() {
        let fake = Fake::new(vec![
            Ok((0, String::new())),
            Ok((0, String::new())),
            Ok((0, String::new())),
            Ok((65, "not on the pass-through".into())),
        ]);
        let helper = Helper::new(fake.clone());
        helper.start("/dev/ttys005", 10800).unwrap();
        helper.extend(10800).unwrap();
        helper.revert().unwrap();
        let err = helper.extend(10800).unwrap_err();
        assert!(
            err.contains("65") && err.contains("not on the pass-through"),
            "{err}"
        );
        let calls = fake.calls.lock().unwrap().clone();
        assert_eq!(calls[0], vec!["start", "/dev/ttys005", "10800"]);
        assert_eq!(calls[1], vec!["extend", "10800"]);
        assert_eq!(calls[2], vec!["revert"]);
    }

    #[test]
    fn quoting_survives_spaces_and_quotes() {
        assert_eq!(
            shell_quote("/Applications/GeekBridge Phone.app/x"),
            "'/Applications/GeekBridge Phone.app/x'"
        );
        assert_eq!(shell_quote("it's"), "'it'\\''s'");
        assert_eq!(applescript_escape(r#"a"b\c"#), r#"a\"b\\c"#);
    }

    #[test]
    fn the_install_script_asks_for_administrator_approval_for_one_installer_and_one_user() {
        let script = install_script(
            Path::new(
                "/Applications/GeekBridge Phone.app/Contents/Resources/macos/install-helper.sh",
            ),
            "sam",
        );
        assert!(
            script.starts_with("do shell script \"/bin/sh '/Applications/GeekBridge Phone.app/")
        );
        assert!(script.contains("--user 'sam'"));
        assert!(script.ends_with("with administrator privileges"));
        assert!(!script.contains("; "), "one command and nothing else");
    }
}

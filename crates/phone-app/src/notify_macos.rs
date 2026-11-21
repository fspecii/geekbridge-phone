// SPDX-License-Identifier: GPL-3.0-or-later
//! macOS notifications for calls and texts, through `osascript`'s `display notification`
//! (no extra dependencies, no signing identity needed).
//!
//! The text goes in as script arguments, never spliced into the script, so a message body
//! cannot change what runs. These notifications have no buttons: answering and declining are
//! done in the app window. A ringing alert cannot be withdrawn, so ending a call adds nothing.

use crate::notify::{
    CallEndedNotice, CallNotice, CallUpdate, Notifier, NotifierHost, NotifyId, TextNotice,
};
use std::process::{Command, Stdio};

pub struct MacosNotifier {
    _host: NotifierHost,
}

impl MacosNotifier {
    pub fn new(host: NotifierHost) -> Self {
        Self { _host: host }
    }
}

fn show(title: &str, body: &str, sound: bool) {
    let (title, body) = (title.to_string(), body.to_string());
    std::thread::spawn(move || {
        let mut script =
            String::from("display notification (item 2 of argv) with title (item 1 of argv)");
        if sound {
            script.push_str(" sound name \"Ping\"");
        }
        let status = Command::new("osascript")
            .args(["-e", "on run argv", "-e", &script, "-e", "end run", "--"])
            .arg(&title)
            .arg(&body)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
        if let Err(e) = status {
            log::warn!("could not show a notification: {e}");
        }
    });
}

fn who(name: &Option<String>, number: &str) -> String {
    match name {
        Some(n) if !n.is_empty() => n.clone(),
        _ if number.is_empty() => "Unknown number".to_string(),
        _ => number.to_string(),
    }
}

impl Notifier for MacosNotifier {
    fn incoming_call(&self, c: &CallNotice) {
        show("Incoming call", &who(&c.name, &c.number), true);
    }

    fn call_updated(&self, _: &CallUpdate) {}

    fn call_ended(&self, c: &CallEndedNotice) {
        if c.missed {
            show("Missed call", &who(&c.name, &c.number), false);
        }
    }

    fn text(&self, t: &TextNotice) {
        show(&who(&t.name, &t.number), &t.body, true);
    }

    fn clear(&self, _: &NotifyId) {}
}

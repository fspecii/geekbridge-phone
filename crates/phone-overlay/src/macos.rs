// SPDX-License-Identifier: GPL-3.0-or-later

//! macOS presenter: one floating, non-activating panel in the top-right corner of the main
//! screen (`native/macos_overlay.m`). The platform-neutral core draws the banners to a pixmap;
//! this module sends that pixmap to the panel and turns mouse and key events into core input.
//!
//! One thread owns the [`OverlayState`] and sleeps until a command, an event from the panel or
//! the next timed wake-up arrives; with nothing on screen the panel is hidden and nothing wakes.

use std::ffi::{c_char, c_float, c_int, CStr};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use crate::{
    render, Action, ButtonState, Command, Key, OverlayConfig, OverlayError, OverlayHandle,
    OverlayState, Theme, ThemePref, Wake,
};

type Callback = extern "C" fn(c_int, c_float, c_float, *const c_char);

extern "C" {
    fn gbp_ov_init(cb: Callback);
    fn gbp_ov_present(rgba: *const u8, w: c_int, h: c_int, scale: c_float);
    fn gbp_ov_hide();
    fn gbp_ov_want_keyboard(on: c_int);
    fn gbp_ov_scale() -> c_float;
    fn gbp_ov_is_dark() -> c_int;
}

/// What the panel and the app tell the presenter thread.
enum Msg {
    Cmd(Command),
    Move(f32, f32),
    Leave,
    Button(ButtonState, f32, f32),
    Key(Key),
    KeyboardLost,
}

static EVENTS: OnceLock<Mutex<Sender<Msg>>> = OnceLock::new();

fn push(m: Msg) {
    if let Some(tx) = EVENTS.get() {
        if let Ok(tx) = tx.lock() {
            let _ = tx.send(m);
        }
    }
}

extern "C" fn on_event(kind: c_int, a: c_float, b: c_float, text: *const c_char) {
    match kind {
        1 => push(Msg::Move(a, b)),
        2 => push(Msg::Leave),
        3 => push(Msg::Button(ButtonState::Down, a, b)),
        4 => push(Msg::Button(ButtonState::Up, a, b)),
        5 => {
            if text.is_null() {
                return;
            }
            // SAFETY: the panel passes a NUL-terminated string valid for this call.
            let t = unsafe { CStr::from_ptr(text) }
                .to_string_lossy()
                .into_owned();
            let word = b != 0.0;
            let key = match t.as_str() {
                "Backspace" if word => Key::BackspaceWord,
                "Backspace" => Key::Backspace,
                "Delete" => Key::Delete,
                "Left" if word => Key::WordLeft,
                "Left" => Key::Left,
                "Right" if word => Key::WordRight,
                "Right" => Key::Right,
                "Home" => Key::Home,
                "End" => Key::End,
                "Enter" => Key::Enter,
                "Escape" => Key::Escape,
                other => match other.strip_prefix("T:") {
                    Some(text) if !text.is_empty() => Key::Text(text.to_string()),
                    _ => return,
                },
            };
            push(Msg::Key(key));
        }
        6 => push(Msg::KeyboardLost),
        _ => {}
    }
}

struct App {
    state: OverlayState,
    actions: Sender<Action>,
    pref: ThemePref,
    visible: bool,
    keyboard_on: bool,
    dirty: bool,
    wake_at: Option<Instant>,
    animating: bool,
}

/// Redraw rate while something animates.
const FRAME: Duration = Duration::from_millis(16);

impl App {
    fn theme(&self) -> Theme {
        let dark = match self.pref {
            ThemePref::Dark => true,
            ThemePref::Light => false,
            // SAFETY: plain read of a user default.
            ThemePref::System => unsafe { gbp_ov_is_dark() != 0 },
        };
        if dark {
            Theme::Dark
        } else {
            Theme::Light
        }
    }

    fn send(&self, actions: Vec<Action>) {
        for a in actions {
            let _ = self.actions.send(a);
        }
    }

    fn handle(&mut self, m: Msg) {
        let now = Instant::now();
        match m {
            Msg::Cmd(c) => self.state.apply(c, now),
            Msg::Move(x, y) => self.state.pointer_motion(x, y, now),
            Msg::Leave => self.state.pointer_leave(now),
            Msg::Button(st, x, y) => {
                self.state.pointer_motion(x, y, now);
                let actions = self.state.pointer_button(st, now);
                self.send(actions);
            }
            Msg::Key(k) => {
                let actions = self.state.key(k, now);
                self.send(actions);
            }
            Msg::KeyboardLost => self.state.keyboard_focus_lost(now),
        }
        self.dirty = true;
    }

    /// Advance time, then draw if something changed.
    fn after_events(&mut self) {
        let now = Instant::now();
        let actions = self.state.tick(now);
        self.send(actions);
        if self.state.is_empty() {
            if self.visible {
                // SAFETY: hides the panel on the main thread.
                unsafe { gbp_ov_hide() };
                self.visible = false;
            }
            self.sync_keyboard();
            self.wake_at = None;
            self.animating = false;
            self.dirty = false;
            return;
        }
        let due = self.wake_at.is_some_and(|t| now >= t);
        if self.dirty || self.animating || due {
            self.draw();
        }
    }

    fn draw(&mut self) {
        self.dirty = false;
        // SAFETY: plain read of the main screen's scale.
        let scale = unsafe { gbp_ov_scale() }.max(1.0);
        let pm = render(&self.state, scale, self.theme());
        let (w, h) = (pm.width() as c_int, pm.height() as c_int);
        if w > 0 && h > 0 {
            // SAFETY: the pixmap holds w*h premultiplied RGBA pixels and is copied before return.
            unsafe { gbp_ov_present(pm.data().as_ptr(), w, h, scale) };
            self.visible = true;
        }
        self.sync_keyboard();
        match self.state.next_wake() {
            Wake::Frame => {
                self.animating = true;
                self.wake_at = None;
            }
            Wake::At(t) => {
                self.animating = false;
                self.wake_at = Some(t);
            }
            Wake::Idle => {
                self.animating = false;
                self.wake_at = None;
            }
        }
    }

    /// The panel takes keyboard focus only while a reply field is being edited.
    fn sync_keyboard(&mut self) {
        let want = self.state.wants_keyboard();
        if want != self.keyboard_on {
            self.keyboard_on = want;
            // SAFETY: asks the main thread to give or release the keyboard.
            unsafe { gbp_ov_want_keyboard(want as c_int) };
        }
    }

    fn timeout(&self) -> Option<Duration> {
        if self.animating {
            return Some(FRAME);
        }
        self.wake_at
            .map(|t| t.saturating_duration_since(Instant::now()))
    }
}

fn run(config: OverlayConfig, actions: Sender<Action>, events: Receiver<Msg>) {
    let mut app = App {
        state: OverlayState::new(&config, Instant::now()),
        actions,
        pref: config.theme,
        visible: false,
        keyboard_on: false,
        dirty: false,
        wake_at: None,
        animating: false,
    };
    loop {
        let first = match app.timeout() {
            Some(t) => events.recv_timeout(t),
            None => events.recv().map_err(|_| RecvTimeoutError::Disconnected),
        };
        match first {
            Ok(m) => {
                app.handle(m);
                while let Ok(m) = events.try_recv() {
                    app.handle(m);
                }
            }
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => break,
        }
        app.after_events();
    }
    // SAFETY: hides the panel on the main thread.
    unsafe { gbp_ov_hide() };
}

/// Start the macOS presenter.
///
/// # Errors
///
/// [`OverlayError::Presenter`] when the presenter thread cannot start.
pub fn spawn(config: OverlayConfig) -> Result<(OverlayHandle, Receiver<Action>), OverlayError> {
    // Font discovery takes a moment: do it in the background so the first banner is quick.
    let _ = std::thread::Builder::new()
        .name("phone-overlay-fonts".into())
        .spawn(crate::text::warm_up);
    let (act_tx, act_rx) = mpsc::channel();
    let (ev_tx, ev_rx) = mpsc::channel::<Msg>();
    if EVENTS.set(Mutex::new(ev_tx.clone())).is_err() {
        return Err(OverlayError::Presenter(
            "the macOS overlay is already running in this process".into(),
        ));
    }
    // SAFETY: registers the event callback once, before any panel exists.
    unsafe { gbp_ov_init(on_event) };
    std::thread::Builder::new()
        .name("phone-overlay".into())
        .spawn(move || run(config, act_tx, ev_rx))
        .map_err(|e| OverlayError::Presenter(format!("{e:?}")))?;
    let tx = Mutex::new(ev_tx);
    let handle = OverlayHandle::from_sink(move |cmd| {
        if let Ok(tx) = tx.lock() {
            let _ = tx.send(Msg::Cmd(cmd));
        }
    });
    Ok((handle, act_rx))
}

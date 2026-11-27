// SPDX-License-Identifier: GPL-3.0-or-later
//! `geekbridge-phone --macos-paired` reads the paired devices from `main()`, on the process's
//! main thread, with no run loop servicing it. That once deadlocked: the read was queued to the
//! main thread and then waited for there. This test runs without the test harness (see
//! Cargo.toml) so that it, too, calls from the real main thread.

#[cfg(target_os = "macos")]
fn main() {
    use std::time::Duration;
    std::thread::spawn(|| {
        std::thread::sleep(Duration::from_secs(10));
        eprintln!("paired_devices_text() did not return within 10 s on the main thread");
        std::process::exit(1);
    });
    // On a Mac without Bluetooth access the list is empty; returning at all is the point.
    let _ = phone_macos::paired_devices_text();
    println!("test paired_devices_text_returns_on_the_main_thread ... ok");
}

#[cfg(not(target_os = "macos"))]
fn main() {}

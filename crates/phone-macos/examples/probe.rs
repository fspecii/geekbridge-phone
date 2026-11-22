// Lists paired phones, then opens each connected phone's message and phonebook services.
// Run: cargo run -p phone-macos --example probe
#[cfg(target_os = "macos")]
extern "C" {
    fn gbp_run_main_loop();
}

#[cfg(target_os = "macos")]
fn main() {
    std::thread::spawn(work);
    // SAFETY: services the main run loop for IOBluetooth's callbacks; `work` exits the process.
    unsafe { gbp_run_main_loop() };
}

#[cfg(target_os = "macos")]
fn work() {
    use phone_core::platform::Platform;
    let p = phone_macos::MacosPlatform::new();
    println!("computer: {:?}", p.computer_name());
    let phones = p.phones().expect("phones");
    println!("{} phone(s)", phones.len());
    for ph in &phones {
        println!(
            "- {} {} connected={} apple={}",
            ph.address, ph.name, ph.connected, ph.apple
        );
        if ph.name.contains("iPhone") || ph.connected {
            for (what, r) in [
                ("MAP", p.open_map(&ph.address)),
                ("PBAP", p.open_pbap(&ph.address)),
            ] {
                println!(
                    "  {what}: {}",
                    r.map(|_| "opened".into()).unwrap_or_else(|e| e.to_string())
                );
            }
            println!(
                "  calls: {:?}",
                p.calls(&ph.address).map_err(|e| e.to_string())
            );
        }
    }
    std::process::exit(0);
}
#[cfg(not(target_os = "macos"))]
fn main() {}

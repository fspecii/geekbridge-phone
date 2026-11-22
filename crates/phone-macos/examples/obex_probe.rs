// Runs the real OBEX clients over the Mac's RFCOMM streams and prints only counts and sizes
// (no names, numbers or message text). Must run inside an app bundle with a Bluetooth usage
// description (see scripts/package-macos.sh). The phone must be unlocked and in range.
#[cfg(target_os = "macos")]
extern "C" {
    fn gbp_run_main_loop();
}

#[cfg(target_os = "macos")]
fn main() {
    std::thread::spawn(|| {
        work();
        std::process::exit(0);
    });
    // SAFETY: services the main run loop for IOBluetooth's callbacks.
    unsafe { gbp_run_main_loop() };
}

#[cfg(target_os = "macos")]
fn work() {
    use phone_core::platform::Platform;
    use phone_obex::{Book, MapClient, PbapClient};
    let p = phone_macos::MacosPlatform::new();
    let Some(ph) = p.phones().unwrap().into_iter().find(|x| x.apple) else {
        return println!("no Apple phone paired");
    };
    println!(
        "phone: {} (apple)",
        if ph.name.is_empty() { "?" } else { "paired" }
    );

    match p.open_pbap(&ph.address) {
        Err(e) => println!("PBAP open: {e}"),
        Ok(s) => match PbapClient::connect(s) {
            Err(e) => println!(
                "PBAP connect: {e} (access denied: {})",
                e.is_access_denied()
            ),
            Ok(mut pbap) => {
                for (n, b) in [
                    ("contacts", Book::Contacts),
                    ("received", Book::Received),
                    ("dialed", Book::Dialed),
                    ("missed", Book::Missed),
                ] {
                    match pbap.pull(b, 500) {
                        Ok(t) => println!(
                            "PBAP {n}: {} bytes, {} cards",
                            t.len(),
                            t.matches("BEGIN:VCARD").count()
                        ),
                        Err(e) => println!("PBAP {n}: failed: {e}"),
                    }
                }
            }
        },
    }
    match p.open_map(&ph.address) {
        Err(e) => println!("MAP open: {e}"),
        Ok(s) => match MapClient::connect(s) {
            Err(e) => println!("MAP connect: {e} (access denied: {})", e.is_access_denied()),
            Ok(mut map) => match map.list_folders() {
                Ok(f) => println!("MAP folders: {} ({:?})", f.len(), f),
                Err(e) => println!("MAP list_folders: failed: {e}"),
            },
        },
    }
    println!(
        "calls: {:?}",
        p.calls(&ph.address).map_err(|e| e.to_string())
    );
}
#[cfg(not(target_os = "macos"))]
fn main() {}

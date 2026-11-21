fn main() {
    #[cfg(target_os = "macos")]
    {
        cc::Build::new()
            .file("native/iobt.m")
            .flag("-fobjc-arc")
            .flag("-Wno-deprecated-declarations")
            .compile("gbp_iobt");
        cc::Build::new()
            .file("native/vpio.m")
            .flag("-fobjc-arc")
            .compile("gbp_vpio");
        println!("cargo:rustc-link-lib=framework=AudioToolbox");
        println!("cargo:rerun-if-changed=native/vpio.m");
        println!("cargo:rustc-link-lib=framework=IOBluetooth");
        println!("cargo:rustc-link-lib=framework=Foundation");
        println!("cargo:rerun-if-changed=native/iobt.m");
    }
}

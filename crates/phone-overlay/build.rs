fn main() {
    #[cfg(target_os = "macos")]
    {
        cc::Build::new()
            .file("native/macos_overlay.m")
            .flag("-fobjc-arc")
            .compile("gbp_macos_overlay");
        println!("cargo:rustc-link-lib=framework=Cocoa");
        println!("cargo:rerun-if-changed=native/macos_overlay.m");
    }
}

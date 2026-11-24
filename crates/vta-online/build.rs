// Picks up an optional, uncommitted development file (see lib.rs); it is only
// compiled into builds with debug assertions.
fn main() {
    println!("cargo::rustc-check-cfg=cfg(vta_dev_online)");
    let dev = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../.dev/online_dev.rs");
    println!("cargo::rerun-if-changed={}", dev.display());
    if dev.is_file() {
        println!("cargo::rustc-cfg=vta_dev_online");
    }
}

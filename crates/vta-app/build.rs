fn main() {
    let config = slint_build::CompilerConfiguration::new().with_style("fluent-dark".into());
    slint_build::compile_with_config("ui/app.slint", config).expect("Slint build failed");

    // Windows: the same version resource and icon v2's build stamped, so the
    // installers, the updater and Explorer read the exe exactly as before.
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        let version = current_version();
        let quad = version_quad(&version);
        let edition = if std::env::var("CARGO_FEATURE_ICON_AI").is_ok() { "ai" } else { "standard" };
        let mut res = winresource::WindowsResource::new();
        res.set_icon("../../Assets/app_icon.ico")
            .set("FileVersion", &quad)
            .set("ProductVersion", &quad)
            .set("CompanyName", "Slyfalco")
            .set("ProductName", "VitaTheme Architect")
            .set("FileDescription", &format!("VitaTheme Architect ({edition} edition)"))
            .set("OriginalFilename", "vitaThemeArchitect.exe")
            .set("InternalName", "vitaThemeArchitect")
            .set_version_info(winresource::VersionInfo::FILEVERSION, pack(&quad))
            .set_version_info(winresource::VersionInfo::PRODUCTVERSION, pack(&quad));
        res.compile().expect("Windows resource failed");
    }
    println!("cargo:rerun-if-changed=../vta-core/src/lib.rs");
    println!("cargo:rerun-if-env-changed=VTA_PACKAGED");
}

/// `CURRENT_VERSION` from vta-core (single source of truth for the app version).
fn current_version() -> String {
    let src = std::fs::read_to_string("../vta-core/src/lib.rs").expect("vta-core lib.rs");
    src.lines()
        .find_map(|l| l.trim().strip_prefix("pub const CURRENT_VERSION: &str = \""))
        .and_then(|rest| rest.split('"').next())
        .expect("CURRENT_VERSION")
        .to_string()
}

/// v2 `_version_quad`: dotted version -> a.b.c.d.
fn version_quad(v: &str) -> String {
    let mut parts: Vec<&str> = v.split(|c: char| !c.is_ascii_digit()).filter(|p| !p.is_empty()).collect();
    parts.resize(4, "0");
    parts.truncate(4);
    parts.join(".")
}

fn pack(quad: &str) -> u64 {
    quad.split('.').take(4).fold(0u64, |acc, p| (acc << 16) | p.parse::<u64>().unwrap_or(0).min(0xffff))
}

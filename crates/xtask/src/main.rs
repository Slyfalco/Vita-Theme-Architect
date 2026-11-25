//! Build + package tool for VitaTheme Architect v3 (replaces v2's setup_build.py).
//!
//!     cargo run -p xtask -- package [--edition ai|standard|both] [--no-package]
//!
//! For each edition: `cargo build --release` (AI = default features, Standard =
//! `--no-default-features`) with VTA_PACKAGED=1, stage the app folder
//! `dist/vitaThemeArchitect-<os>-<arch>/` (macOS: `.../VitaThemeArchitect.app`)
//! with the runtime files v2 shipped, embed the install-integrity manifest, run
//! the release hook when one is configured (VTA_RELEASE_SCRIPT, off-repo), and
//! package `dist/vitaThemeArchitect-<os>-<arch>-<edition>.zip|tar.gz` — the
//! names the in-app updater and installers look for.

use std::path::{Path, PathBuf};
use std::process::Command;

const APP_FOLDER: &str = "vitaThemeArchitect";
const EXE: &str = "vitaThemeArchitect";
const MAC_APP: &str = "VitaThemeArchitect.app";

fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).ancestors().nth(2).unwrap().to_path_buf()
}

/// `<os>-<arch>` of the build (mirrors the updater's platform tag). CI can
/// override it (`VTA_TAG_OVERRIDE`) when cross-building — e.g. the x64 mac
/// package produced on an arm64 runner via `VTA_BUILD_TARGET`.
fn build_tag() -> String {
    if let Ok(tag) = std::env::var("VTA_TAG_OVERRIDE") {
        if !tag.trim().is_empty() {
            return tag.trim().to_string();
        }
    }
    let os = if cfg!(windows) {
        "windows"
    } else if cfg!(target_os = "macos") {
        "macos"
    } else {
        "linux"
    };
    let arch = match std::env::consts::ARCH {
        "x86_64" => "x64",
        "aarch64" => "arm64",
        "x86" => "x86",
        a => a,
    };
    format!("{os}-{arch}")
}

fn run(cmd: &mut Command) -> Result<(), String> {
    let status = cmd.status().map_err(|e| format!("{cmd:?}: {e}"))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("{cmd:?} failed ({status})"))
    }
}

fn copy_tree(src: &Path, dst: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dst)?;
    for e in std::fs::read_dir(src)? {
        let e = e?;
        let to = dst.join(e.file_name());
        if e.file_type()?.is_dir() {
            copy_tree(&e.path(), &to)?;
        } else {
            std::fs::copy(e.path(), &to)?;
        }
    }
    Ok(())
}

/// The runtime data files v2 shipped next to the exe.
fn stage_data(root: &Path, dest: &Path, ai: bool) -> Result<(), String> {
    let assets = root.join("Assets");
    let e = |r: std::io::Result<()>| r.map_err(|e| e.to_string());
    for name in ["app_icon.ico", "app_icon.png"] {
        if assets.join(name).is_file() {
            std::fs::create_dir_all(dest.join("Assets")).map_err(|e| e.to_string())?;
            std::fs::copy(assets.join(name), dest.join("Assets").join(name)).map_err(|e| e.to_string())?;
        }
    }
    // Template icons ship in every build (the generator's input; defaults in the creator).
    e(copy_tree(&assets.join("IconTemplates"), &dest.join("Assets/IconTemplates")))?;
    if ai {
        // The worker runs in the downloaded AI runtime, so it ships as a file.
        std::fs::copy(root.join("icon_ai_worker.py"), dest.join("icon_ai_worker.py")).map_err(|e| e.to_string())?;
        e(copy_tree(&assets.join("IconExamples"), &dest.join("Assets/IconExamples")))?;
        if assets.join("icon_estimates.dat").is_file() {
            std::fs::copy(assets.join("icon_estimates.dat"), dest.join("Assets/icon_estimates.dat")).map_err(|e| e.to_string())?;
        }
    }
    // Licences of bundled third-party fonts/icons (OFL / Apache-2.0) and the app's own.
    let lic = dest.join("licenses");
    std::fs::create_dir_all(&lic).map_err(|e| e.to_string())?;
    let ui = root.join("crates/vta-app/ui");
    for (src, name) in [
        (ui.join("fonts/LICENSE-Inter.txt"), "Inter-OFL.txt"),
        (ui.join("fonts/LICENSE-JetBrainsMono.txt"), "JetBrainsMono-OFL.txt"),
        (ui.join("icons/LICENSE-MaterialSymbols.txt"), "MaterialSymbols-Apache-2.0.txt"),
        (root.join("LICENSE"), "VitaThemeArchitect-LICENSE.txt"),
        // at9codec's licence requires credit (an independent AT9 codec
        // implementation written by Slyfalco) in the notices that ship with any
        // program using it, plus its reference notices.
        (root.join("third_party/at9codec-LICENSE.txt"), "at9codec-LICENSE.txt"),
    ] {
        if src.is_file() {
            std::fs::copy(&src, lic.join(name)).map_err(|e| e.to_string())?;
        }
    }
    if root.join("EULA.txt").is_file() {
        std::fs::copy(root.join("EULA.txt"), dest.join("EULA.txt")).map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn version() -> String {
    let src = std::fs::read_to_string(root().join("crates/vta-core/src/lib.rs")).unwrap_or_default();
    src.lines()
        .find_map(|l| l.trim().strip_prefix("pub const CURRENT_VERSION: &str = \""))
        .and_then(|r| r.split('"').next())
        .unwrap_or("0.0")
        .to_string()
}

fn info_plist(version: &str) -> String {
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>{EXE}</string>
    <key>CFBundleIdentifier</key><string>com.slyfalco.vitathemearchitect</string>
    <key>CFBundleName</key><string>VitaThemeArchitect</string>
    <key>CFBundleDisplayName</key><string>VitaTheme Architect</string>
    <key>CFBundleIconFile</key><string>app_icon.icns</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>{version}</string>
    <key>CFBundleVersion</key><string>{version}</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict>
</plist>
"#
    )
}

/// Build + stage one edition. Returns the app folder (or .app) path.
fn build_edition(edition: &str) -> Result<PathBuf, String> {
    let root = root();
    let ai = edition == "ai";
    let target_dir = root.join(format!("target/package-{edition}"));
    println!("==> Building the {edition} edition");
    // CI cross-compile: a cargo target triple (e.g. x86_64-apple-darwin on an
    // arm64 mac runner). It nests the output under target/<triple>/release.
    let target = std::env::var("VTA_BUILD_TARGET").ok().filter(|t| !t.trim().is_empty());
    let mut cmd = Command::new(std::env::var("CARGO").unwrap_or_else(|_| "cargo".into()));
    cmd.current_dir(&root)
        .args(["build", "--release", "-p", "vta-app", "--bin", EXE])
        .arg("--target-dir")
        .arg(&target_dir)
        .env("VTA_PACKAGED", "1");
    if let Some(t) = &target {
        cmd.arg("--target").arg(t);
    }
    if !ai {
        cmd.arg("--no-default-features");
    }
    run(&mut cmd)?;
    let exe_name = if cfg!(windows) { format!("{EXE}.exe") } else { EXE.to_string() };
    let release_dir = match &target {
        Some(t) => target_dir.join(t).join("release"),
        None => target_dir.join("release"),
    };
    let built = release_dir.join(&exe_name);

    let tag = build_tag();
    // v3 output lives in its own folder so it never overwrites v2 build artifacts.
    let dist = root.join("dist").join("v3");
    let folder = dist.join(format!("{APP_FOLDER}-{tag}"));
    let _ = std::fs::remove_dir_all(&folder);
    let (app_root, exe_dir) = if cfg!(target_os = "macos") {
        let app = folder.join(MAC_APP);
        let macos = app.join("Contents/MacOS");
        std::fs::create_dir_all(app.join("Contents/Resources")).map_err(|e| e.to_string())?;
        std::fs::write(app.join("Contents/Info.plist"), info_plist(&version())).map_err(|e| e.to_string())?;
        let icns = root.join("Assets/app_icon.icns");
        if icns.is_file() {
            std::fs::copy(&icns, app.join("Contents/Resources/app_icon.icns")).map_err(|e| e.to_string())?;
        }
        (app, macos)
    } else {
        (folder.clone(), folder.clone())
    };
    std::fs::create_dir_all(&exe_dir).map_err(|e| e.to_string())?;
    let exe = exe_dir.join(&exe_name);
    std::fs::copy(&built, &exe).map_err(|e| format!("copy exe: {e}"))?;
    stage_data(&root, &exe_dir, ai)?;

    // Integrity manifest (Windows/Linux; macOS uses its code signature) — after
    // every shipped file is in place, before the release hook and signing.
    if !cfg!(target_os = "macos") {
        let n = vta_core::integrity::embed(&exe_dir, &exe).map_err(|e| e.to_string())?;
        println!("  Embedded integrity manifest ({n} files).");
    }

    // Release builds: the off-repo release hook (set VTA_RELEASE_SCRIPT).
    if let Ok(hook) = std::env::var("VTA_RELEASE_SCRIPT") {
        let hook = root.join(hook.trim());
        if hook.is_file() {
            println!("  Running the release hook");
            let mut c = if hook.extension().is_some_and(|e| e == "py") {
                let mut c = Command::new(if cfg!(windows) { "python" } else { "python3" });
                c.arg(&hook);
                c
            } else {
                Command::new(&hook)
            };
            run(c.arg("--bind").arg("--exe").arg(&exe).arg("--app-dir").arg(&exe_dir).env("VTA_BUILD_EXE", &exe).current_dir(&root))?;
        } else {
            println!("  WARNING: VTA_RELEASE_SCRIPT is set but the file was not found — building non-release.");
        }
    }
    let _ = app_root;
    Ok(folder)
}

fn package(folder: &Path, edition: &str) -> Result<PathBuf, String> {
    let dist = folder.parent().unwrap();
    let base = format!("{}-{edition}", folder.file_name().unwrap().to_string_lossy());
    let folder_name = folder.file_name().unwrap().to_string_lossy().into_owned();
    if cfg!(target_os = "linux") {
        let out = dist.join(format!("{base}.tar.gz"));
        let file = std::fs::File::create(&out).map_err(|e| e.to_string())?;
        let gz = flate2::write::GzEncoder::new(file, flate2::Compression::best());
        let mut tar = tar::Builder::new(gz);
        tar.append_dir_all(&folder_name, folder).map_err(|e| e.to_string())?;
        tar.into_inner().and_then(|gz| gz.finish()).map_err(|e| e.to_string())?;
        Ok(out)
    } else if cfg!(target_os = "macos") {
        let out = dist.join(format!("{base}.zip"));
        let _ = std::fs::remove_file(&out);
        run(Command::new("ditto").args(["-c", "-k", "--sequesterRsrc", "--keepParent"]).arg(folder.join(MAC_APP)).arg(&out))?;
        Ok(out)
    } else {
        let out = dist.join(format!("{base}.zip"));
        let file = std::fs::File::create(&out).map_err(|e| e.to_string())?;
        let mut zip = zip::ZipWriter::new(file);
        let opts = zip::write::SimpleFileOptions::default().compression_method(zip::CompressionMethod::Deflated);
        fn add(zip: &mut zip::ZipWriter<std::fs::File>, base: &Path, dir: &Path, prefix: &str, opts: zip::write::SimpleFileOptions) -> Result<(), String> {
            let mut entries: Vec<PathBuf> = std::fs::read_dir(dir).map_err(|e| e.to_string())?.flatten().map(|e| e.path()).collect();
            entries.sort();
            for p in entries {
                let rel = format!("{prefix}/{}", p.strip_prefix(base).unwrap().to_string_lossy().replace('\\', "/"));
                if p.is_dir() {
                    zip.add_directory(format!("{rel}/"), opts).map_err(|e| e.to_string())?;
                    add(zip, base, &p, prefix, opts)?;
                } else {
                    zip.start_file(rel, opts).map_err(|e| e.to_string())?;
                    std::io::copy(&mut std::fs::File::open(&p).map_err(|e| e.to_string())?, zip).map_err(|e| e.to_string())?;
                }
            }
            Ok(())
        }
        add(&mut zip, folder, folder, &folder_name, opts)?;
        zip.finish().map_err(|e| e.to_string())?;
        Ok(out)
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(String::as_str) != Some("package") {
        eprintln!("usage: cargo run -p xtask -- package [--edition ai|standard|both] [--no-package]");
        std::process::exit(2);
    }
    let edition = args.iter().position(|a| a == "--edition").and_then(|i| args.get(i + 1)).cloned().unwrap_or_else(|| "ai".into());
    let no_package = args.iter().any(|a| a == "--no-package") || std::env::var("VTA_PACKAGE").as_deref() == Ok("0");
    let editions: Vec<&str> = match edition.as_str() {
        "both" => vec!["standard", "ai"],
        "standard" => vec!["standard"],
        _ => vec!["ai"],
    };
    for ed in editions {
        let result = build_edition(ed).and_then(|folder| {
            if no_package {
                Ok(folder)
            } else {
                package(&folder, ed)
            }
        });
        match result {
            Ok(p) => println!("==> {ed}: {}", p.display()),
            Err(e) => {
                eprintln!("==> {ed} FAILED: {e}");
                std::process::exit(1);
            }
        }
    }
}

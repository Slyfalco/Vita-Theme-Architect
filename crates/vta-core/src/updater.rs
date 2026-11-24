//! Cross-platform in-app updater: a port of v2's `app_updater.py` with the same
//! external contract (GitHub releases API, `v<version>` tags, asset names
//! `vitaThemeArchitect-<os>-<arch>-<edition>.<ext>`, tuple version compare,
//! public no-auth downloads).
//!
//! Only the install folder is ever touched — never the app data folder — and a
//! normal update copies *over* the install folder without deleting anything. The
//! swap is done by a detached helper script that waits for the app to exit.

use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

pub const GITHUB_REPO: &str = "Slyfalco/Vita-Theme-Architect";
pub const RELEASES_URL: &str = "https://github.com/Slyfalco/Vita-Theme-Architect/releases/latest";
const API_LATEST: &str = "https://api.github.com/repos/Slyfalco/Vita-Theme-Architect/releases/latest";
const API_TAG: &str = "https://api.github.com/repos/Slyfalco/Vita-Theme-Architect/releases/tags/";
const ARCHIVE_EXTS: [&str; 5] = [".tar.gz", ".tgz", ".zip", ".dmg", ".tar"];

#[derive(Debug, Clone, Default)]
pub struct Asset {
    pub name: String,
    pub url: String,
    pub size: u64,
}

#[derive(Debug, Clone, Default)]
pub struct Release {
    pub tag: String,
    pub name: String,
    pub body: String,
    pub prerelease: bool,
    pub html_url: String,
    pub assets: Vec<Asset>,
}

/// (os, arch) of this machine, e.g. ("windows", "x64"); mirrors the build tag.
pub fn platform_tag() -> (&'static str, &'static str) {
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
        other => other,
    };
    (os, arch)
}

fn parse_version(text: &str) -> Vec<u64> {
    let nums: Vec<u64> = text
        .split(|c: char| !c.is_ascii_digit())
        .filter(|s| !s.is_empty())
        .filter_map(|s| s.parse().ok())
        .collect();
    if nums.is_empty() {
        vec![0]
    } else {
        nums
    }
}

/// True when tag `latest` is newer than `current` (tuple compare: 1.0.10 > 1.0.2).
pub fn is_newer(latest: &str, current: &str) -> bool {
    parse_version(latest) > parse_version(current)
}

fn agent(timeout: Duration) -> ureq::Agent {
    ureq::Agent::config_builder().timeout_global(Some(timeout)).build().into()
}

fn get_json(url: &str) -> Result<serde_json::Value, String> {
    let body = agent(Duration::from_secs(10))
        .get(url)
        .header("User-Agent", "VitaThemeArchitect-Updater")
        .header("Accept", "application/vnd.github+json")
        .call()
        .map_err(|e| e.to_string())?
        .body_mut()
        .read_to_string()
        .map_err(|e| e.to_string())?;
    serde_json::from_str(&body).map_err(|e| e.to_string())
}

fn release_from(v: &serde_json::Value) -> Release {
    let s = |k: &str| v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string();
    Release {
        tag: s("tag_name"),
        name: s("name"),
        body: s("body"),
        prerelease: v.get("prerelease").and_then(|x| x.as_bool()).unwrap_or(false),
        html_url: s("html_url"),
        assets: v
            .get("assets")
            .and_then(|a| a.as_array())
            .map(|arr| {
                arr.iter()
                    .map(|a| Asset {
                        name: a.get("name").and_then(|x| x.as_str()).unwrap_or("").to_string(),
                        url: a.get("browser_download_url").and_then(|x| x.as_str()).unwrap_or("").to_string(),
                        size: a.get("size").and_then(|x| x.as_u64()).unwrap_or(0),
                    })
                    .collect()
            })
            .unwrap_or_default(),
    }
}

pub fn fetch_latest() -> Result<Release, String> {
    get_json(API_LATEST).map(|v| release_from(&v))
}

/// A release by tag (trying both `x` and `vx`).
pub fn fetch_release_by_tag(tag: &str) -> Option<Release> {
    let tag = tag.trim();
    let tries = if let Some(rest) = tag.strip_prefix('v') { vec![tag.to_string(), rest.to_string()] } else { vec![tag.to_string(), format!("v{tag}")] };
    tries.iter().filter(|t| !t.is_empty()).find_map(|t| get_json(&format!("{API_TAG}{t}")).ok().map(|v| release_from(&v)))
}

/// A release's markdown body as a short plain summary (v2 `summarize_notes`).
pub fn summarize_notes(body: &str, max_lines: usize, max_chars: usize) -> String {
    if body.is_empty() {
        return String::new();
    }
    let comments = regex::Regex::new(r"(?s)<!--.*?-->").unwrap();
    let heading = regex::Regex::new(r"^#+\s*").unwrap();
    let bullet = regex::Regex::new(r"^[-*+]\s+").unwrap();
    let code = regex::Regex::new(r"`([^`]*)`").unwrap();
    let bold = regex::Regex::new(r"\*\*([^*]*)\*\*").unwrap();
    let italic = regex::Regex::new(r"(^|[^\w])\*([^*]+)\*($|[^\w])").unwrap();
    let skip_heads = ["whatsnew", "whatisnew", "whatschanged", "changes", "changelog", "releasenotes", "highlights", "notes"];
    let body = comments.replace_all(body, "");
    let mut lines: Vec<String> = Vec::new();
    for raw in body.lines() {
        let line = raw.trim();
        if line.is_empty() {
            continue;
        }
        let low = line.to_lowercase();
        if low.starts_with("**full changelog") || low.starts_with("full changelog") || (low.contains("github.com") && low.contains("compare")) {
            continue;
        }
        let line = heading.replace(line, "").to_string();
        let squashed: String = line
            .chars()
            .filter(|c| !c.is_whitespace() && !"*:._'’-".contains(*c))
            .collect::<String>()
            .to_lowercase();
        if skip_heads.contains(&squashed.as_str()) {
            continue;
        }
        let line = bullet.replace(&line, "• ").to_string();
        let line = code.replace_all(&line, "$1").to_string();
        let line = bold.replace_all(&line, "$1").to_string();
        let line = italic.replace_all(&line, "$1$2$3").to_string();
        lines.push(line);
        if lines.len() >= max_lines {
            lines.push("…".into());
            break;
        }
    }
    let summary = lines.join("\n").trim().to_string();
    if summary.chars().count() <= max_chars {
        return summary;
    }
    let cut: String = summary.chars().take(max_chars).collect();
    let boundary = cut.rfind([' ', '\n']).unwrap_or(0);
    let cut = if boundary as f64 > max_chars as f64 * 0.5 { &cut[..boundary] } else { &cut[..] };
    format!("{} …", cut.trim_end())
}

fn stem_tokens(name: &str) -> Vec<String> {
    let mut low = name.to_lowercase();
    for ext in ARCHIVE_EXTS {
        if let Some(stripped) = low.strip_suffix(ext) {
            low = stripped.to_string();
            break;
        }
    }
    low.split(|c: char| c == '-' || c == '_' || c == '.' || c.is_whitespace())
        .filter(|t| !t.is_empty())
        .map(str::to_string)
        .collect()
}

/// "ai" if the asset name carries the `ai` token, else "standard".
pub fn asset_edition(name: &str) -> &'static str {
    if stem_tokens(name).iter().any(|t| t == "ai") {
        "ai"
    } else {
        "standard"
    }
}

/// v2 `pick_asset`: the asset for this OS + arch (+ edition, preferred or required).
pub fn pick_asset<'a>(assets: &'a [Asset], edition: Option<&str>, require_edition: bool) -> Option<&'a Asset> {
    pick_asset_for(assets, edition, require_edition, platform_tag())
}

pub fn pick_asset_for<'a>(assets: &'a [Asset], edition: Option<&str>, require_edition: bool, (os, arch): (&str, &str)) -> Option<&'a Asset> {
    let pref: &[&str] = match os {
        "windows" => &[".zip"],
        "macos" => &[".dmg", ".zip"],
        "linux" => &[".tar.gz", ".tgz", ".zip"],
        _ => &[".zip"],
    };
    let mut best: Option<(&Asset, i32)> = None;
    for a in assets {
        if a.url.is_empty() {
            continue;
        }
        let toks = stem_tokens(&a.name);
        if !toks.iter().any(|t| t == os) {
            continue;
        }
        let ed = asset_edition(&a.name);
        let mut score = match edition {
            Some(e) if e != ed => {
                if require_edition {
                    continue;
                }
                -3
            }
            Some(_) => 2,
            None => 0,
        };
        if toks.iter().any(|t| t == arch) {
            score += 4;
        }
        let low = a.name.to_lowercase();
        if let Some(i) = pref.iter().position(|ext| low.ends_with(ext)) {
            score += (pref.len() - i) as i32;
        }
        if best.is_none_or(|(_, s)| score > s) {
            best = Some((a, score));
        }
    }
    best.map(|(a, _)| a)
}

// ----------------------------------------------------------------- download

/// Stream an asset into `dir`. `progress(done, total)`; `Ok(None)` if cancelled.
pub fn download_asset(asset: &Asset, dir: &Path, cancel: &AtomicBool, mut progress: impl FnMut(u64, u64)) -> Result<Option<PathBuf>, String> {
    std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    let name = if asset.name.is_empty() { "update.bin" } else { &asset.name };
    let path = dir.join(Path::new(name).file_name().unwrap_or(std::ffi::OsStr::new("update.bin")));
    let mut resp = agent(Duration::from_secs(600))
        .get(&asset.url)
        .header("User-Agent", "VitaThemeArchitect-Updater")
        .call()
        .map_err(|e| e.to_string())?;
    let total = resp
        .headers()
        .get("content-length")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse().ok())
        .unwrap_or(asset.size);
    let mut reader = resp.body_mut().with_config().limit(u64::MAX).reader();
    let mut file = std::fs::File::create(&path).map_err(|e| e.to_string())?;
    let mut buf = vec![0u8; 1 << 16];
    let mut done = 0u64;
    loop {
        if cancel.load(Ordering::SeqCst) {
            drop(file);
            let _ = std::fs::remove_file(&path);
            return Ok(None);
        }
        let n = reader.read(&mut buf).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        std::io::Write::write_all(&mut file, &buf[..n]).map_err(|e| e.to_string())?;
        done += n as u64;
        progress(done, total);
    }
    Ok(Some(path))
}

fn extract(archive: &Path, dest: &Path) -> Result<(), String> {
    std::fs::create_dir_all(dest).map_err(|e| e.to_string())?;
    let low = archive.to_string_lossy().to_lowercase();
    if low.ends_with(".zip") {
        crate::import::extract_zip(archive, dest)
    } else if low.ends_with(".tar.gz") || low.ends_with(".tgz") || low.ends_with(".tar") {
        let file = std::fs::File::open(archive).map_err(|e| e.to_string())?;
        let reader: Box<dyn Read> = if low.ends_with(".tar") { Box::new(file) } else { Box::new(flate2::read::GzDecoder::new(file)) };
        let mut tar = tar::Archive::new(reader);
        tar.set_preserve_permissions(true);
        tar.unpack(dest).map_err(|e| e.to_string())
    } else if low.ends_with(".dmg") {
        extract_dmg(archive, dest)
    } else {
        Err(format!("unsupported archive type: {}", archive.display()))
    }
}

fn extract_dmg(dmg: &Path, dest: &Path) -> Result<(), String> {
    let out = std::process::Command::new("hdiutil")
        .args(["attach", "-nobrowse", "-readonly"])
        .arg(dmg)
        .output()
        .map_err(|e| e.to_string())?;
    let text = String::from_utf8_lossy(&out.stdout);
    let mount = text
        .lines()
        .find_map(|l| l.find("/Volumes/").map(|i| l[i..].trim().to_string()))
        .ok_or("could not mount the .dmg")?;
    let result = (|| {
        let app = std::fs::read_dir(&mount)
            .map_err(|e| e.to_string())?
            .flatten()
            .map(|e| e.path())
            .find(|p| p.extension().is_some_and(|e| e == "app"))
            .ok_or("no .app inside the .dmg")?;
        let status = std::process::Command::new("ditto")
            .arg(&app)
            .arg(dest.join(app.file_name().unwrap()))
            .status()
            .map_err(|e| e.to_string())?;
        if status.success() {
            Ok(())
        } else {
            Err("copying the .app failed".to_string())
        }
    })();
    let _ = std::process::Command::new("hdiutil").args(["detach", &mount, "-quiet"]).status();
    result
}

/// Find the new app inside the extracted archive: the .app on macOS, else the
/// folder holding the executable (or the single top-level folder).
fn find_payload(extracted: &Path, exe_name: &str) -> PathBuf {
    fn walk(dir: &Path, f: &mut dyn FnMut(&Path) -> bool) -> Option<PathBuf> {
        if f(dir) {
            return Some(dir.to_path_buf());
        }
        let mut subs: Vec<PathBuf> = std::fs::read_dir(dir).ok()?.flatten().map(|e| e.path()).filter(|p| p.is_dir()).collect();
        subs.sort();
        subs.iter().find_map(|s| walk(s, f))
    }
    if cfg!(target_os = "macos") {
        if let Some(app) = walk(extracted, &mut |d| d.extension().is_some_and(|e| e == "app")) {
            return app;
        }
    }
    let has_exe = |d: &Path| {
        d.join(exe_name).is_file()
            || std::fs::read_dir(d)
                .map(|rd| rd.flatten().any(|e| e.path().extension().is_some_and(|x| x.eq_ignore_ascii_case("exe"))))
                .unwrap_or(false)
    };
    if let Some(dir) = walk(extracted, &mut |d| has_exe(d)) {
        return dir;
    }
    let dirs: Vec<PathBuf> = std::fs::read_dir(extracted)
        .map(|rd| rd.flatten().map(|e| e.path()).filter(|p| p.is_dir()).collect())
        .unwrap_or_default();
    if dirs.len() == 1 {
        dirs[0].clone()
    } else {
        extracted.to_path_buf()
    }
}

pub struct Staged {
    pub new_app: PathBuf,
    pub stage_root: PathBuf,
}

/// Download + extract into a fresh temp staging folder. `Ok(None)` if cancelled.
pub fn prepare_update(asset: &Asset, cancel: &AtomicBool, progress: impl FnMut(u64, u64)) -> Result<Option<Staged>, String> {
    let stage_root = std::env::temp_dir().join(format!(
        "vta_update_{}_{}",
        std::process::id(),
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0)
    ));
    let Some(archive) = download_asset(asset, &stage_root, cancel, progress)? else {
        let _ = std::fs::remove_dir_all(&stage_root);
        return Ok(None);
    };
    let payload = stage_root.join("payload");
    extract(&archive, &payload)?;
    let exe_name = std::env::current_exe().ok().and_then(|e| e.file_name().map(|n| n.to_string_lossy().into_owned())).unwrap_or_default();
    Ok(Some(Staged { new_app: find_payload(&payload, &exe_name), stage_root }))
}

// ----------------------------------------------------------------- apply

/// The install folder (Windows/Linux) or the `.app` bundle (macOS).
pub fn app_root() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let dir = exe.parent()?.to_path_buf();
    if cfg!(target_os = "macos") {
        let bundle = dir.parent()?.parent()?;
        if bundle.extension().is_some_and(|e| e == "app") {
            return Some(bundle.to_path_buf());
        }
    }
    Some(dir)
}

fn install_writable(path: &Path) -> bool {
    let probe = path.join(".vta_write_test");
    let ok = std::fs::write(&probe, b"").is_ok();
    let _ = std::fs::remove_file(&probe);
    ok
}

/// Launch the detached helper that waits for this process to exit, installs the
/// new build, relaunches and cleans up. Returns Ok(true) once it runs (the caller
/// must then quit), Ok(false) if needed elevation was declined.
///
/// `clean` mirrors the folder (edition switch); `preserve` is an in-folder data
/// dir (portable builds) that a mirror must keep.
pub fn apply_and_restart(staged: &Staged, clean: bool, preserve: Option<&Path>) -> Result<bool, String> {
    let install = app_root().ok_or("could not find the install folder")?;
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    // Safety: the target must hold the running exe and not be shallow.
    if !exe.starts_with(&install) || install.components().count() < 3 {
        return Err(format!("refusing to update an unexpected install path: {}", install.display()));
    }
    let preserve = preserve.filter(|p| p.starts_with(&install));
    let elevate = !install_writable(&install);
    let pid = std::process::id();
    let exe_name = exe.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    #[cfg(windows)]
    {
        apply_windows(staged, &install, &exe_name, pid, clean, preserve, elevate)
    }
    #[cfg(not(windows))]
    {
        apply_unix(staged, &install, &exe_name, pid, clean, preserve, elevate)
    }
}

#[cfg(windows)]
fn apply_windows(staged: &Staged, install: &Path, exe_name: &str, pid: u32, clean: bool, preserve: Option<&Path>, elevate: bool) -> Result<bool, String> {
    use std::os::windows::process::CommandExt;
    let exe = install.join(exe_name);
    let script = std::env::temp_dir().join(format!("vta_update_{pid}.bat"));
    let mode = if clean { "/MIR" } else { "/E" };
    let exclude = match (clean, preserve) {
        (true, Some(p)) => format!(" /XD \"{}\"", p.display()),
        _ => String::new(),
    };
    // Elevated: relaunch through explorer.exe so the app comes back non-elevated.
    let relaunch = if elevate { format!("explorer.exe \"{}\"", exe.display()) } else { format!("start \"\" \"{}\"", exe.display()) };
    let lines = [
        "@echo off".to_string(),
        "setlocal".into(),
        ":waitloop".into(),
        format!("tasklist /FI \"PID eq {pid}\" | find \"{pid}\" >nul 2>&1"),
        "if %errorlevel%==0 (".into(),
        "  ping -n 2 127.0.0.1 >nul 2>&1".into(),
        "  goto waitloop".into(),
        ")".into(),
        format!(
            "robocopy \"{}\" \"{}\" {mode}{exclude} /R:3 /W:1 /NFL /NDL /NJH /NJS /NC /NS /NP >nul",
            staged.new_app.display(),
            install.display()
        ),
        relaunch,
        format!("rmdir /s /q \"{}\" >nul 2>&1", staged.stage_root.display()),
        "del \"%~f0\"".into(),
    ];
    std::fs::write(&script, lines.join("\r\n")).map_err(|e| e.to_string())?;
    if elevate {
        use windows_sys::Win32::UI::Shell::ShellExecuteW;
        let wide = |s: &str| s.encode_utf16().chain(std::iter::once(0)).collect::<Vec<u16>>();
        let (verb, file, params) = (wide("runas"), wide("cmd.exe"), wide(&format!("/c \"{}\"", script.display())));
        // SAFETY: all pointers are valid NUL-terminated UTF-16 buffers for the call.
        let rc = unsafe { ShellExecuteW(std::ptr::null_mut(), verb.as_ptr(), file.as_ptr(), params.as_ptr(), std::ptr::null(), 0) };
        return Ok(rc as isize > 32);
    }
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
    std::process::Command::new("cmd")
        .arg("/c")
        .arg(&script)
        .creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
        .spawn()
        .map_err(|e| e.to_string())?;
    Ok(true)
}

#[cfg(not(windows))]
fn apply_unix(staged: &Staged, install: &Path, exe_name: &str, pid: u32, clean: bool, preserve: Option<&Path>, elevate: bool) -> Result<bool, String> {
    use std::os::unix::fs::PermissionsExt;
    let tmp = std::env::temp_dir();
    let script = tmp.join(format!("vta_update_{pid}.sh"));
    let (new_app, install_s, stage) = (staged.new_app.display().to_string(), install.display().to_string(), staged.stage_root.display().to_string());
    let relaunch = if cfg!(target_os = "macos") { format!("open \"{install_s}\"") } else { format!("\"{}\" &", install.join(exe_name).display()) };
    let install_cmds: Vec<String> = match (clean, preserve) {
        (true, Some(p)) => {
            let keep = format!("{}.vtakeep", install_s.trim_end_matches('/'));
            let p = p.display();
            vec![
                format!("rm -rf \"{keep}\""),
                format!("if [ -e \"{p}\" ]; then mv \"{p}\" \"{keep}\"; fi"),
                format!("rm -rf \"{install_s}\""),
                format!("mkdir -p \"{install_s}\""),
                format!("cp -R \"{new_app}/.\" \"{install_s}\""),
                format!("if [ -e \"{keep}\" ]; then rm -rf \"{p}\"; mv \"{keep}\" \"{p}\"; fi"),
            ]
        }
        (true, None) => vec![format!("rm -rf \"{install_s}\""), format!("mkdir -p \"{install_s}\""), format!("cp -R \"{new_app}/.\" \"{install_s}\"")],
        (false, _) => vec![format!("mkdir -p \"{install_s}\""), format!("cp -R \"{new_app}/.\" \"{install_s}\"")],
    };
    let cleanup = vec![format!("rm -rf \"{stage}\""), "rm -f \"$0\"".to_string()];
    let mut body: Vec<String>;
    if elevate {
        let copyscript = tmp.join(format!("vta_update_{pid}_copy.sh"));
        std::fs::write(&copyscript, format!("#!/bin/sh\n{}\n", install_cmds.join("\n"))).map_err(|e| e.to_string())?;
        let _ = std::fs::set_permissions(&copyscript, std::fs::Permissions::from_mode(0o755));
        let do_install = if cfg!(target_os = "macos") {
            format!("osascript -e 'do shell script \"/bin/sh \\\"{}\\\"\" with administrator privileges'", copyscript.display())
        } else {
            format!("pkexec /bin/sh \"{}\"", copyscript.display())
        };
        body = vec![do_install, relaunch];
        body.extend(cleanup);
        body.push(format!("rm -f \"{}\"", copyscript.display()));
    } else {
        body = install_cmds;
        body.push(relaunch);
        body.extend(cleanup);
    }
    let mut lines = vec!["#!/bin/sh".to_string(), format!("while kill -0 {pid} 2>/dev/null; do sleep 1; done")];
    lines.extend(body);
    std::fs::write(&script, lines.join("\n")).map_err(|e| e.to_string())?;
    let _ = std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755));
    use std::os::unix::process::CommandExt;
    std::process::Command::new("sh").arg(&script).process_group(0).spawn().map_err(|e| e.to_string())?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn a(name: &str) -> Asset {
        Asset { name: name.into(), url: format!("https://x/{name}"), size: 1 }
    }

    #[test]
    fn versions_compare_like_v2() {
        assert!(is_newer("v1.0.10", "1.0.2"));
        assert!(is_newer("v3.0", "2.0"));
        assert!(!is_newer("v2.0", "2.0"));
        assert!(!is_newer("garbage", "0"));
    }

    #[test]
    fn picks_matching_asset() {
        let assets = vec![
            a("vitaThemeArchitect-windows-x64-ai.zip"),
            a("vitaThemeArchitect-windows-x64-standard.zip"),
            a("vitaThemeArchitect-windows-arm64-ai.zip"),
            a("vitaThemeArchitect-linux-x64-ai.tar.gz"),
            a("vitaThemeArchitect-windows-x64-setup.exe"),
        ];
        let pick = |ed, req, tag| pick_asset_for(&assets, ed, req, tag).map(|x| x.name.as_str());
        assert_eq!(pick(Some("ai"), false, ("windows", "x64")), Some("vitaThemeArchitect-windows-x64-ai.zip"));
        assert_eq!(pick(Some("standard"), false, ("windows", "x64")), Some("vitaThemeArchitect-windows-x64-standard.zip"));
        assert_eq!(pick(Some("ai"), false, ("windows", "arm64")), Some("vitaThemeArchitect-windows-arm64-ai.zip"));
        // Arch is a preference (v2): Windows-ARM falls back to x64 (it runs under emulation).
        assert_eq!(pick(Some("standard"), true, ("windows", "arm64")), Some("vitaThemeArchitect-windows-x64-standard.zip"));
        assert_eq!(pick(Some("ai"), false, ("linux", "x64")), Some("vitaThemeArchitect-linux-x64-ai.tar.gz"));
        assert_eq!(pick(Some("ai"), false, ("macos", "arm64")), None);
    }

    #[test]
    fn summarizes_notes() {
        let body = "## What's new\n<!-- hidden -->\n- **Faster** `AT9` decode\n* *Italic* fix\n\n**Full Changelog**: https://github.com/a/b/compare/v1...v2";
        assert_eq!(summarize_notes(body, 14, 800), "• Faster AT9 decode\n• Italic fix");
    }
}

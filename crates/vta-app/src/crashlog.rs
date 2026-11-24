//! Crash log (v2 `main._crash_log`): panics are appended to crash.log — in
//! `%APPDATA%\VitaThemeArchitect` for Windows release builds (the app folder may
//! be read-only), next to the executable on other systems, and in the repository
//! for development runs. The log is trimmed to its last 500 lines past 512 KB.

use std::io::Write;
use std::path::PathBuf;

const MAX_BYTES: u64 = 512 * 1024;
const KEEP_LINES: usize = 500;

fn log_path() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    if vta_core::paths::is_dev_build(&exe) {
        return Some(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../crash.log"));
    }
    if cfg!(windows) {
        let base = std::env::var_os("APPDATA").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("."));
        let dir = base.join("VitaThemeArchitect");
        let _ = std::fs::create_dir_all(&dir);
        Some(dir.join("crash.log"))
    } else {
        Some(exe.parent()?.join("crash.log"))
    }
}

fn now_text() -> String {
    let secs = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
    let (days, rem) = (secs.div_euclid(86_400), secs.rem_euclid(86_400));
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    format!("{y:04}-{m:02}-{d:02} {:02}:{:02}:{:02} UTC", rem / 3600, rem % 3600 / 60, rem % 60)
}

fn rotate(path: &PathBuf) {
    if std::fs::metadata(path).map(|m| m.len() > MAX_BYTES).unwrap_or(false) {
        if let Ok(text) = std::fs::read_to_string(path) {
            let lines: Vec<&str> = text.lines().collect();
            let kept = &lines[lines.len().saturating_sub(KEEP_LINES)..];
            let _ = std::fs::write(path, format!("--- Log rotated at {} (kept last {} lines) ---\n{}\n", now_text(), kept.len(), kept.join("\n")));
        }
    }
}

/// Install the panic hook (call first thing in `main`).
pub fn install() {
    let default = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        if let Some(path) = log_path() {
            rotate(&path);
            if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&path) {
                let bt = std::backtrace::Backtrace::force_capture();
                let _ = write!(f, "\n--- {} ---\n{info}\n{bt}\n", now_text());
            }
        }
        default(info);
    }));
}

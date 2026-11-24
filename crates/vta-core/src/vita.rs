//! PS Vita transfers over FTP (VitaShell's FTP server): send a theme to
//! `ux0:customtheme/`, list and download themes from it. Port of v2's
//! `send_to_vita` (main_window.py) and `VitaImporterDialog` (vita_importer.py).
//!
//! The functions here are blocking; run them off the UI thread. Questions for the
//! user (overwrite? use another folder?) are handled by the caller between
//! [`probe_destination`] and [`upload_theme`].

use crate::theme::{parse_theme_xml_str, ThemeData};
use std::net::{SocketAddr, ToSocketAddrs};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;
use suppaftp::types::FileType;
use suppaftp::FtpStream;

pub const REMOTE_BASE: &str = "ux0:customtheme";
pub const DEFAULT_PORT: u16 = 1337;

pub type Result<T> = std::result::Result<T, String>;

fn err(e: impl std::fmt::Display) -> String {
    e.to_string()
}

/// Connect, log in anonymously (like Python's ftplib) and switch to binary mode.
pub fn connect(ip: &str, port: u16, timeout: Duration) -> Result<FtpStream> {
    let addr: SocketAddr = (ip.trim(), port)
        .to_socket_addrs()
        .map_err(|e| format!("Invalid PS Vita address {ip}:{port}: {e}"))?
        .next()
        .ok_or_else(|| format!("Invalid PS Vita address {ip}:{port}"))?;
    let mut ftp = FtpStream::connect_timeout(addr, timeout).map_err(|e| format!("Could not connect to {ip}:{port}: {e}"))?;
    let _ = ftp.get_ref().set_read_timeout(Some(timeout));
    ftp.login("anonymous", "anonymous@").map_err(|e| format!("FTP login failed: {e}"))?;
    ftp.transfer_type(FileType::Binary).map_err(err)?;
    Ok(ftp)
}

/// Entry names of a directory listing (v2 parsed `LIST` lines the same way:
/// 9 whitespace-separated fields, the last is the name).
fn list_names(ftp: &mut FtpStream) -> Result<Vec<String>> {
    let lines = ftp.list(None).map_err(err)?;
    Ok(lines
        .iter()
        .filter_map(|line| {
            if line.split_whitespace().count() < 9 {
                return None;
            }
            // The name is everything after the 8th field (it may contain spaces).
            let mut rest = line.trim_start();
            for _ in 0..8 {
                rest = rest[rest.find(char::is_whitespace).unwrap_or(rest.len())..].trim_start();
            }
            let name = rest.trim_end().to_string();
            (name != "." && name != ".." && !name.is_empty()).then_some(name)
        })
        .collect())
}

/// Test the connection: connect, open `ux0:customtheme`, count its folders.
pub fn test_connection(ip: &str, port: u16) -> Result<(usize, Duration)> {
    let start = std::time::Instant::now();
    let mut ftp = connect(ip, port, Duration::from_secs(10))?;
    let latency = start.elapsed();
    let count = match ftp.cwd(REMOTE_BASE) {
        Ok(()) => list_names(&mut ftp)?.len(),
        Err(_) => 0,
    };
    let _ = ftp.quit();
    Ok((count, latency))
}

/// What's at a candidate destination folder on the Vita.
#[derive(Debug, Clone, PartialEq)]
pub enum Destination {
    /// The folder doesn't exist: it will be created.
    New { folder: String },
    /// The same theme (same title) is there; `remote_version` compared to local.
    SameTheme { folder: String, remote_version: String, order: std::cmp::Ordering },
    /// The folder exists but its theme.xml couldn't be read.
    Unreadable { folder: String, error: String },
}

impl Destination {
    pub fn folder(&self) -> &str {
        match self {
            Destination::New { folder } | Destination::SameTheme { folder, .. } | Destination::Unreadable { folder, .. } => folder,
        }
    }
}

/// v2 compared versions as floats ("01.10" -> 1.1).
fn version_number(v: &str) -> f64 {
    v.trim().parse::<f64>().unwrap_or(0.0)
}

/// Folder name for slot `n`: `theme`, `theme1`, `theme2`, … (as v2).
pub fn slot_name(n: u32) -> String {
    if n == 0 {
        "theme".into()
    } else {
        format!("theme{n}")
    }
}

/// Find where to send a theme: walk `theme`, `theme1`, … from slot `start` and stop at
/// the first free folder, the first folder holding the same theme (by title), or the
/// first folder whose theme.xml can't be read. Returns the destination and its slot.
pub fn probe_destination(ip: &str, port: u16, local: &ThemeData, start: u32) -> Result<(Destination, u32)> {
    let mut ftp = connect(ip, port, Duration::from_secs(20))?;
    if ftp.cwd(REMOTE_BASE).is_err() {
        ftp.mkdir(REMOTE_BASE).map_err(err)?;
        ftp.cwd(REMOTE_BASE).map_err(err)?;
    }
    let existing = list_names(&mut ftp)?;
    let local_version = local.version.clone().unwrap_or_else(|| "00.00".into());
    let mut slot = start;
    let result = loop {
        let folder = slot_name(slot);
        if !existing.iter().any(|n| n == &folder) {
            break Destination::New { folder };
        }
        let read = ftp
            .cwd(&folder)
            .map_err(err)
            .and_then(|_| ftp.retr_as_buffer("theme.xml").map_err(err));
        let _ = ftp.cwd("..");
        match read {
            Ok(buf) => {
                let text = String::from_utf8_lossy(buf.get_ref()).into_owned();
                let remote = parse_theme_xml_str(&text).unwrap_or_else(crate::theme::invalid_theme);
                if remote.title == local.title {
                    let remote_version = remote.version.unwrap_or_else(|| "00.00".into());
                    let order = version_number(&local_version)
                        .partial_cmp(&version_number(&remote_version))
                        .unwrap_or(std::cmp::Ordering::Equal);
                    break Destination::SameTheme { folder, remote_version, order };
                }
            }
            Err(error) => break Destination::Unreadable { folder, error },
        }
        slot += 1;
    };
    let _ = ftp.quit();
    Ok((result, slot))
}

/// Upload a theme folder's files into `ux0:customtheme/<folder>`. With `overwrite`,
/// files already in the folder are deleted first. `progress(done, total, file)`;
/// returns `Ok(false)` if cancelled.
pub fn upload_theme(
    ip: &str,
    port: u16,
    local_dir: &Path,
    folder: &str,
    overwrite: bool,
    cancel: &AtomicBool,
    mut progress: impl FnMut(usize, usize, &str),
) -> Result<bool> {
    let mut files: Vec<_> = std::fs::read_dir(local_dir)
        .map_err(err)?
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.is_file())
        .collect();
    files.sort();
    let mut ftp = connect(ip, port, Duration::from_secs(20))?;
    if ftp.cwd(REMOTE_BASE).is_err() {
        ftp.mkdir(REMOTE_BASE).map_err(err)?;
        ftp.cwd(REMOTE_BASE).map_err(err)?;
    }
    if ftp.cwd(folder).is_err() {
        ftp.mkdir(folder).map_err(err)?;
        ftp.cwd(folder).map_err(err)?;
    } else if overwrite {
        for name in list_names(&mut ftp).unwrap_or_default() {
            let _ = ftp.rm(&name); // directories are left alone, as in v2
        }
    }
    let total = files.len();
    for (i, path) in files.iter().enumerate() {
        if cancel.load(Ordering::SeqCst) {
            let _ = ftp.quit();
            return Ok(false);
        }
        let name = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        progress(i, total, &name);
        let mut f = std::fs::File::open(path).map_err(err)?;
        ftp.put_file(&name, &mut f).map_err(|e| format!("Uploading {name} failed: {e}"))?;
    }
    progress(total, total, "");
    let _ = ftp.quit();
    Ok(true)
}

/// Theme folder names in `ux0:customtheme`.
pub fn list_remote_themes(ip: &str, port: u16) -> Result<Vec<String>> {
    let mut ftp = connect(ip, port, Duration::from_secs(10))?;
    ftp.cwd(REMOTE_BASE).map_err(|e| format!("Could not open {REMOTE_BASE} on the PS Vita: {e}"))?;
    let mut names = list_names(&mut ftp)?;
    names.sort();
    let _ = ftp.quit();
    Ok(names)
}

/// Download remote theme folders into new library folders (`dest_for(name)` picks
/// each local folder). `progress(done, total, label)`. Returns the folders imported
/// and per-theme errors; stops early if cancelled.
pub fn download_themes(
    ip: &str,
    port: u16,
    names: &[String],
    mut dest_for: impl FnMut(&str) -> std::path::PathBuf,
    cancel: &AtomicBool,
    mut progress: impl FnMut(usize, usize, &str),
) -> Result<(Vec<std::path::PathBuf>, Vec<String>)> {
    let mut ftp = connect(ip, port, Duration::from_secs(10))?;
    ftp.cwd(REMOTE_BASE).map_err(err)?;
    // Count files first for an accurate progress bar.
    let mut plan: Vec<(String, Vec<String>)> = Vec::new();
    for name in names {
        if ftp.cwd(name).is_ok() {
            plan.push((name.clone(), list_names(&mut ftp).unwrap_or_default()));
            let _ = ftp.cwd("..");
        } else {
            plan.push((name.clone(), Vec::new()));
        }
    }
    let total: usize = plan.iter().map(|(_, f)| f.len()).sum();
    let (mut done, mut imported, mut errors) = (0usize, Vec::new(), Vec::new());
    for (name, files) in plan {
        if cancel.load(Ordering::SeqCst) {
            break;
        }
        let local = dest_for(&name);
        let result: Result<()> = (|| {
            std::fs::create_dir_all(&local).map_err(err)?;
            ftp.cwd(&name).map_err(err)?;
            for file in &files {
                if cancel.load(Ordering::SeqCst) {
                    break;
                }
                progress(done, total, &format!("Importing '{name}'…"));
                let buf = ftp.retr_as_buffer(file).map_err(|e| format!("{file}: {e}"))?;
                std::fs::write(local.join(file), buf.into_inner()).map_err(err)?;
                done += 1;
            }
            ftp.cwd("..").map_err(err)?;
            Ok(())
        })();
        match result {
            Ok(()) => imported.push(local),
            Err(e) => {
                errors.push(format!("{name}: {e}"));
                let _ = ftp.cwd(REMOTE_BASE);
            }
        }
    }
    progress(total, total, "");
    let _ = ftp.quit();
    Ok((imported, errors))
}

#[cfg(test)]
mod tests {
    #[test]
    fn list_line_names() {
        let line = "drwxrwxrwx   1 0        0          0 Jan 01 00:00 My Theme 2";
        let mut rest = line.trim_start();
        for _ in 0..8 {
            rest = rest[rest.find(char::is_whitespace).unwrap_or(rest.len())..].trim_start();
        }
        assert_eq!(rest.trim_end(), "My Theme 2");
        assert_eq!(super::slot_name(0), "theme");
        assert_eq!(super::slot_name(3), "theme3");
    }
}

//! Install-integrity manifest (port of v2 `main._check_overlay_integrity` and
//! `setup_build.embed_integrity_in_exe`), same on-disk format so the build
//! tooling and the app agree.
//!
//! The manifest (SHA-256 of every shipped file) is appended to the END of the
//! executable as `<json><8-byte LE length><8-byte MAGIC "VTAINTEG">`, so it is
//! part of the exe rather than a loose editable file. The reader is signing-aware:
//! when the exe is Authenticode-signed after the build, the certificate table is
//! appended after the overlay, so the overlay ends where the certificate starts.
//! The check FAILS CLOSED (missing/corrupt manifest, or any changed/missing listed
//! file). Extra, unlisted files are ignored (e.g. v2 leftovers after an update).

use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

pub const MAGIC: &[u8; 8] = b"VTAINTEG";

/// Native-library folders that must stay user-replaceable (LGPL) and are not
/// listed. (v3 ships none of v2's — no Qt/VLC/libsndfile — but the names stay
/// excluded so a leftover v2 copy is never hashed.)
pub const REPLACEABLE_DIRS: [&str; 5] = ["PySide6", "shiboken6", "VLC", "libsndfile", "_soundfile_data"];

/// Data folders that live beside a portable build and must never be listed.
pub const DATA_DIRS: [&str; 1] = ["portable_data"];

fn sha256_file(path: &Path) -> std::io::Result<String> {
    let mut f = std::fs::File::open(path)?;
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    Ok(h.finalize().iter().map(|b| format!("{b:02x}")).collect())
}

/// Build the manifest for an app folder (every file except the exe itself,
/// `__pycache__`, the replaceable native-library dirs and portable data).
pub fn manifest(app_dir: &Path, exe_name: &str) -> BTreeMap<String, String> {
    fn walk(base: &Path, dir: &Path, exe_name: &str, out: &mut BTreeMap<String, String>) {
        let Ok(rd) = std::fs::read_dir(dir) else { return };
        for e in rd.flatten() {
            let p = e.path();
            let name = e.file_name().to_string_lossy().into_owned();
            if p.is_dir() {
                if name == "__pycache__" || REPLACEABLE_DIRS.contains(&name.as_str()) || (dir == base && DATA_DIRS.contains(&name.as_str())) {
                    continue;
                }
                walk(base, &p, exe_name, out);
            } else {
                if dir == base && name == exe_name {
                    continue;
                }
                if let Ok(hash) = sha256_file(&p) {
                    let rel = p.strip_prefix(base).unwrap_or(&p).to_string_lossy().replace('\\', "/");
                    out.insert(rel, hash);
                }
            }
        }
    }
    let mut out = BTreeMap::new();
    walk(app_dir, app_dir, exe_name, &mut out);
    out
}

/// Append the manifest to the exe (build time, before signing).
pub fn embed(app_dir: &Path, exe: &Path) -> std::io::Result<usize> {
    let exe_name = exe.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    let files = manifest(app_dir, &exe_name);
    let blob = serde_json::to_vec(&serde_json::json!({ "files": files }))?;
    let mut f = std::fs::OpenOptions::new().append(true).open(exe)?;
    use std::io::Write;
    f.write_all(&blob)?;
    f.write_all(&(blob.len() as u64).to_le_bytes())?;
    f.write_all(MAGIC)?;
    Ok(files.len())
}

/// Where the overlay ends: the PE certificate table offset when signed, else EOF.
fn overlay_end_offset(f: &mut std::fs::File, filesize: u64) -> u64 {
    let mut parse = || -> Option<u64> {
        let mut head = vec![0u8; 4096];
        f.seek(SeekFrom::Start(0)).ok()?;
        let n = f.read(&mut head).ok()?;
        head.truncate(n);
        if head.len() < 0x40 || &head[..2] != b"MZ" {
            return None;
        }
        let e_lfanew = u32::from_le_bytes(head[0x3c..0x40].try_into().ok()?) as usize;
        if head.len() < e_lfanew + 176 || &head[e_lfanew..e_lfanew + 4] != b"PE\0\0" {
            return None;
        }
        let opt = e_lfanew + 24;
        let magic = u16::from_le_bytes(head[opt..opt + 2].try_into().ok()?);
        let sec_dir = opt + if magic == 0x20b { 112 } else { 96 } + 4 * 8;
        let cert_off = u32::from_le_bytes(head.get(sec_dir..sec_dir + 4)?.try_into().ok()?) as u64;
        let cert_size = u32::from_le_bytes(head.get(sec_dir + 4..sec_dir + 8)?.try_into().ok()?) as u64;
        (cert_off != 0 && cert_size != 0 && (16..=filesize).contains(&cert_off)).then_some(cert_off)
    };
    parse().unwrap_or(filesize)
}

/// Read the manifest back from an exe.
pub fn read_manifest(exe: &Path) -> Result<BTreeMap<String, String>, String> {
    let mut f = std::fs::File::open(exe).map_err(|_| "integrity manifest unreadable".to_string())?;
    let filesize = f.seek(SeekFrom::End(0)).map_err(|_| "integrity manifest unreadable".to_string())?;
    let end = overlay_end_offset(&mut f, filesize);
    // Signing pads a few NUL bytes between our overlay and the certificate table.
    let pad_window = end.min(32);
    if pad_window < 16 {
        return Err("integrity manifest missing".into());
    }
    let mut tail = vec![0u8; pad_window as usize];
    f.seek(SeekFrom::Start(end - pad_window)).map_err(|_| "integrity manifest unreadable".to_string())?;
    f.read_exact(&mut tail).map_err(|_| "integrity manifest unreadable".to_string())?;
    let trailing_nuls = tail.iter().rev().take_while(|b| **b == 0).count() as u64;
    let ovl_end = end - trailing_nuls;
    if ovl_end < 16 {
        return Err("integrity manifest missing".into());
    }
    let mut trailer = [0u8; 16];
    f.seek(SeekFrom::Start(ovl_end - 16)).map_err(|_| "integrity manifest unreadable".to_string())?;
    f.read_exact(&mut trailer).map_err(|_| "integrity manifest unreadable".to_string())?;
    if &trailer[8..] != MAGIC {
        return Err("integrity manifest missing".into());
    }
    let length = u64::from_le_bytes(trailer[..8].try_into().unwrap());
    if length == 0 || length > 64 * 1024 * 1024 || ovl_end - 16 < length {
        return Err("integrity manifest invalid".into());
    }
    let mut blob = vec![0u8; length as usize];
    f.seek(SeekFrom::Start(ovl_end - 16 - length)).map_err(|_| "integrity manifest unreadable".to_string())?;
    f.read_exact(&mut blob).map_err(|_| "integrity manifest unreadable".to_string())?;
    let v: serde_json::Value = serde_json::from_slice(&blob).map_err(|_| "integrity manifest unreadable".to_string())?;
    let files = v.get("files").and_then(|f| f.as_object()).ok_or("integrity manifest unreadable")?;
    Ok(files.iter().map(|(k, v)| (k.clone(), v.as_str().unwrap_or("").to_string())).collect())
}

/// Verify the listed files. Returns the problems (empty = OK); at most 20.
pub fn check(exe: &Path) -> Vec<String> {
    let files = match read_manifest(exe) {
        Ok(f) => f,
        Err(e) => return vec![e],
    };
    let base = exe.parent().unwrap_or(Path::new("."));
    let mut problems = Vec::new();
    for (rel, expected) in files {
        let path = base.join(rel.replace('/', std::path::MAIN_SEPARATOR_STR));
        if !path.is_file() {
            problems.push(format!("missing: {rel}"));
        } else {
            match sha256_file(&path) {
                Ok(h) if h.eq_ignore_ascii_case(&expected) => {}
                Ok(_) => problems.push(format!("changed: {rel}")),
                Err(_) => problems.push(format!("unreadable: {rel}")),
            }
        }
        if problems.len() >= 20 {
            break;
        }
    }
    problems
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn embed_and_check() {
        let d = std::env::temp_dir().join(format!("vta-integ-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(d.join("Assets")).unwrap();
        std::fs::write(d.join("app.exe"), b"not really an exe").unwrap();
        std::fs::write(d.join("Assets/a.png"), b"png").unwrap();
        std::fs::create_dir_all(d.join("portable_data")).unwrap();
        std::fs::write(d.join("portable_data/settings.json"), b"{}").unwrap();
        assert_eq!(embed(&d, &d.join("app.exe")).unwrap(), 1);
        assert!(check(&d.join("app.exe")).is_empty());
        // Extra files (e.g. v2 leftovers) are fine; changed ones are not.
        std::fs::write(d.join("python312.dll"), b"leftover").unwrap();
        assert!(check(&d.join("app.exe")).is_empty());
        std::fs::write(d.join("Assets/a.png"), b"tampered").unwrap();
        assert_eq!(check(&d.join("app.exe")), vec!["changed: Assets/a.png".to_string()]);
        // No manifest at all fails closed.
        std::fs::write(d.join("plain.exe"), b"MZ").unwrap();
        assert_eq!(check(&d.join("plain.exe")), vec!["integrity manifest missing".to_string()]);
        std::fs::remove_dir_all(&d).ok();
    }
}

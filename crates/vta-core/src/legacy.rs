//! First-run cleanup of v2 leftovers after an in-place update to v3.
//!
//! Both shipped v2 update paths (the in-app updater and the online installer's
//! "Update to latest") copy the new build OVER the install folder without
//! deleting anything, so the v2 Python/Qt/VLC runtime is left beside the v3 exe.
//! v3 doesn't use any of it; this removes it.
//!
//! Deliberately conservative: only top-level entries that match a known v2
//! runtime pattern are removed, and never anything v3 itself ships (listed in the
//! exe's integrity manifest), portable data, or anything unrecognised. VC++
//! runtime DLLs are kept (harmless, and something else might rely on them).
//! Best-effort: an entry that can't be deleted (read-only install) is skipped.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

/// v2-only folders (Nuitka package dirs and bundled native tools).
const V2_DIRS: [&str; 18] = [
    "backports",
    "certifi",
    "charset_normalizer",
    "cryptography",
    "libsndfile",
    "_soundfile_data",
    "numpy",
    "numpy.libs",
    "PIL",
    "pillow.libs",
    "PySide6",
    "pythonnet",
    "shiboken6",
    "Tools",
    "VLC",
    "webview",
    "clr_loader",
    "soundfile",
];

/// Whether a top-level file name is part of v2's Python/Qt runtime.
fn is_v2_file(name: &str) -> bool {
    let n = name.to_ascii_lowercase();
    let ext_mod = n.ends_with(".pyd")
        || (n.ends_with(".so") && (n.contains(".cpython-3") || n.contains(".abi3")))
        // v3 still ships the AI worker script (macOS has no manifest to say so).
        || (n.ends_with(".py") && n != "icon_ai_worker.py");
    let prefixes = [
        "python3",
        "libpython3",
        "qt6",
        "libqt6",
        "pyside6",
        "libpyside6",
        "shiboken6",
        "libshiboken6",
        "libcrypto-3",
        "libssl-3",
        "libffi-",
    ];
    ext_mod || n == "qt.conf" || prefixes.iter().any(|p| n.starts_with(p))
}

/// The top-level names v3 itself ships (first path component of each manifest entry).
pub fn shipped_top_level<'a>(manifest_keys: impl IntoIterator<Item = &'a String>) -> BTreeSet<String> {
    manifest_keys.into_iter().map(|k| k.split('/').next().unwrap_or(k).to_string()).collect()
}

/// The leftovers that would be removed from `app_dir` (top level only).
pub fn leftovers(app_dir: &Path, exe_name: &str, shipped: &BTreeSet<String>) -> Vec<PathBuf> {
    let Ok(rd) = std::fs::read_dir(app_dir) else { return Vec::new() };
    let mut out = Vec::new();
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        if name == exe_name || shipped.contains(&name) || crate::integrity::DATA_DIRS.contains(&name.as_str()) {
            continue;
        }
        let p = e.path();
        let v2 = if p.is_dir() { V2_DIRS.contains(&name.as_str()) } else { is_v2_file(&name) };
        if v2 {
            out.push(p);
        }
    }
    out.sort();
    out
}

/// Remove v2 leftovers; returns how many entries were removed.
pub fn cleanup(app_dir: &Path, exe_name: &str, shipped: &BTreeSet<String>) -> usize {
    leftovers(app_dir, exe_name, shipped)
        .into_iter()
        .filter(|p| if p.is_dir() { std::fs::remove_dir_all(p).is_ok() } else { std::fs::remove_file(p).is_ok() })
        .count()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn removes_only_v2_runtime() {
        let dir = std::env::temp_dir().join(format!("vta_legacy_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        for d in ["PySide6", "VLC", "Assets", "portable_data", "MyStuff", "Tools"] {
            std::fs::create_dir_all(dir.join(d)).unwrap();
        }
        for f in [
            "vitaThemeArchitect.exe",
            "python312.dll",
            "qt6core.dll",
            "_ssl.pyd",
            "qt.conf",
            "icon_ai_worker.py",
            "secure_core.cp312-win_amd64.pyd",
            "vcruntime140.dll",
            "vtaonline.dll",
            "EULA.txt",
            "notes.txt",
        ] {
            std::fs::write(dir.join(f), b"x").unwrap();
        }
        // v3 ships Assets/, EULA.txt and its own worker script.
        let keys = vec!["Assets/IconTemplates/a.png".to_string(), "EULA.txt".to_string(), "icon_ai_worker.py".to_string()];
        let shipped = shipped_top_level(&keys);
        let names: Vec<String> =
            leftovers(&dir, "vitaThemeArchitect.exe", &shipped).iter().map(|p| p.file_name().unwrap().to_string_lossy().into_owned()).collect();
        assert_eq!(names, ["PySide6", "Tools", "VLC", "_ssl.pyd", "python312.dll", "qt.conf", "qt6core.dll", "secure_core.cp312-win_amd64.pyd"]);
        assert_eq!(cleanup(&dir, "vitaThemeArchitect.exe", &shipped), 8);
        for kept in ["Assets", "portable_data", "MyStuff", "vcruntime140.dll", "vtaonline.dll", "notes.txt", "icon_ai_worker.py", "vitaThemeArchitect.exe"] {
            assert!(dir.join(kept).exists(), "{kept} should be kept");
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
}

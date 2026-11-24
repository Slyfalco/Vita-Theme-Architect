//! Where the app keeps its data: an exact port of v2's `constants.py` logic.
//!
//! These rules are an external contract: the installers' uninstaller, the v2 → v3
//! in-app update and users' existing data all depend on v3 landing on the *same*
//! folders, so don't change them.
//!
//! Three modes, decided automatically at startup (no flag or marker):
//! * **Source** (a development run from a cargo `target` folder): the shared per-user base.
//! * **Installed** (placed by our installer, or under a standard program path): a
//!   per-install folder `installs/<key>/` under the base, `<key>` = first 16 hex
//!   digits of SHA-256 of the normalised install folder. Large shared downloads (AI
//!   models, examples) and the default libraries stay at the base.
//! * **Portable** (run from anywhere else, e.g. a USB stick): everything beside the
//!   app (`portable_data/`, or `VitaThemeArchitect-data` beside the `.app` on macOS);
//!   falls back to the per-user base on a read-only medium.

use sha2::{Digest, Sha256};
use std::path::{Component, Path, PathBuf};

pub const APP_DIR_NAME: &str = "VitaThemeArchitect";

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Mode {
    Source,
    Installed,
    Portable,
}

/// The resolved data locations.
#[derive(Debug, Clone)]
pub struct DataDirs {
    pub mode: Mode,
    /// Per-install (or portable, or source) folder: settings.json, `.data_version`,
    /// the AI runtime.
    pub app_support: PathBuf,
    /// Shared per-user base (== `app_support` when portable or from source): AI
    /// models, example sets, default theme/extras libraries.
    pub base_support: PathBuf,
    /// The install key (installed builds only).
    pub install_key: Option<String>,
    /// The normalised install folder (installed builds only).
    pub install_dir: Option<String>,
}

impl DataDirs {
    pub fn settings_file(&self) -> PathBuf {
        self.app_support.join("settings.json")
    }
    pub fn default_themes_path(&self) -> PathBuf {
        self.base_support.join("Themes")
    }
    pub fn default_extras_path(&self) -> PathBuf {
        self.base_support.join("Extras")
    }
    pub fn is_portable(&self) -> bool {
        self.mode == Mode::Portable
    }

    /// `store_path`: a path inside the base is saved relative (forward slashes), so a
    /// portable copy keeps finding it after a drive-letter change; others stay absolute.
    pub fn store_path(&self, p: &str) -> String {
        if p.is_empty() {
            return String::new();
        }
        let ap = normcase(&normpath(&absolute(Path::new(p))));
        let base = normcase(&normpath(&absolute(&self.base_support)));
        if ap == base {
            return ".".into();
        }
        let sep = std::path::MAIN_SEPARATOR;
        if let Some(rest) = ap.strip_prefix(&format!("{base}{sep}")) {
            // Take the original-case tail from the un-normcased path.
            let orig = normpath(&absolute(Path::new(p)));
            let tail = &orig[orig.len() - rest.len()..];
            return tail.replace(sep, "/");
        }
        p.to_string()
    }

    /// `load_path`: relative values resolve against the current base; absolute as-is;
    /// empty gives `default`.
    pub fn load_path(&self, p: &str, default: &Path) -> PathBuf {
        if p.is_empty() {
            return default.to_path_buf();
        }
        if is_abs(p) {
            return PathBuf::from(p);
        }
        PathBuf::from(normpath(&self.base_support.join(p)))
    }

    /// True when another installed build has its own data dir besides this one.
    pub fn other_per_install_dirs_exist(&self) -> bool {
        let root = self.base_support.join("installs");
        let Ok(entries) = std::fs::read_dir(&root) else { return false };
        entries.flatten().any(|e| {
            e.path().is_dir()
                && Some(e.file_name().to_string_lossy().as_ref()) != self.install_key.as_deref()
        })
    }
}

/// Python's `os.path.isabs` (on Windows, a rooted path like `\foo` also counts).
fn is_abs(p: &str) -> bool {
    let path = Path::new(p);
    path.is_absolute() || (cfg!(windows) && (p.starts_with('\\') || p.starts_with('/')))
}

fn absolute(p: &Path) -> PathBuf {
    if p.is_absolute() {
        p.to_path_buf()
    } else {
        std::env::current_dir().map(|d| d.join(p)).unwrap_or_else(|_| p.to_path_buf())
    }
}

/// Python's `os.path.normpath`: drop `.`, resolve `..`, collapse separators (and use
/// `\` on Windows). Purely lexical, like Python's.
pub fn normpath(p: &Path) -> String {
    let mut prefix = String::new();
    let mut rooted = false;
    let mut parts: Vec<String> = Vec::new();
    for c in p.components() {
        match c {
            Component::Prefix(pre) => prefix = pre.as_os_str().to_string_lossy().replace('/', "\\"),
            Component::RootDir => rooted = true,
            Component::CurDir => {}
            Component::ParentDir => {
                if parts.last().is_some_and(|l| l != "..") {
                    parts.pop();
                } else if !rooted {
                    parts.push("..".into());
                }
            }
            Component::Normal(s) => parts.push(s.to_string_lossy().into_owned()),
        }
    }
    let sep = std::path::MAIN_SEPARATOR.to_string();
    let mut out = prefix;
    if rooted {
        out.push_str(&sep);
    }
    out.push_str(&parts.join(&sep));
    if out.is_empty() {
        ".".into()
    } else {
        out
    }
}

/// Python's `os.path.normcase`: lower-case + backslashes on Windows, identity elsewhere.
pub fn normcase(p: &str) -> String {
    if cfg!(windows) {
        p.replace('/', "\\").to_lowercase()
    } else {
        p.to_string()
    }
}

/// The per-install key: first 16 hex digits of SHA-256 of the normalised install dir.
pub fn install_key(install_dir: &str) -> String {
    let digest = Sha256::digest(install_dir.as_bytes());
    digest.iter().take(8).map(|b| format!("{b:02x}")).collect()
}

/// `normcase(normpath(dirname(exe)))`.
pub fn normalised_install_dir(exe: &Path) -> Option<String> {
    let dir = absolute(exe).parent()?.to_path_buf();
    Some(normcase(&normpath(&dir)))
}

/// The shared per-OS base folder.
pub fn base_support_dir() -> PathBuf {
    let home = || {
        std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" })
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("."))
    };
    if cfg!(target_os = "macos") {
        home().join("Library").join("Application Support").join(APP_DIR_NAME)
    } else if cfg!(windows) {
        std::env::var_os("APPDATA").map(PathBuf::from).unwrap_or_else(home).join(APP_DIR_NAME)
    } else {
        let xdg = std::env::var_os("XDG_DATA_HOME")
            .filter(|v| !v.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| home().join(".local").join("share"));
        xdg.join(APP_DIR_NAME)
    }
}

fn is_writable_dir(d: &Path) -> bool {
    if std::fs::create_dir_all(d).is_err() {
        return false;
    }
    let probe = d.join(".vta_write_test");
    let ok = std::fs::write(&probe, b"").is_ok();
    let _ = std::fs::remove_file(&probe);
    ok
}

/// True when `folder` sits under a standard program-install path for the OS.
pub fn in_program_location(folder: &Path) -> bool {
    let h = normcase(&normpath(folder));
    let mut roots: Vec<PathBuf> = Vec::new();
    let home = std::env::var_os("HOME").map(PathBuf::from);
    if cfg!(windows) {
        for var in ["ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"] {
            if let Some(v) = std::env::var_os(var).filter(|v| !v.is_empty()) {
                roots.push(v.into());
            }
        }
        if let Some(la) = std::env::var_os("LOCALAPPDATA").filter(|v| !v.is_empty()) {
            roots.push(PathBuf::from(la).join("Programs"));
        }
    } else if cfg!(target_os = "macos") {
        roots.push("/Applications".into());
        if let Some(h) = &home {
            roots.push(h.join("Applications"));
        }
    } else {
        for r in ["/usr", "/opt", "/bin", "/sbin"] {
            roots.push(r.into());
        }
        if let Some(h) = &home {
            for sub in [".local/bin", ".local/share", ".local/lib"] {
                roots.push(h.join(sub));
            }
        }
    }
    let sep = std::path::MAIN_SEPARATOR;
    roots.iter().any(|r| {
        let rn = normcase(&normpath(r));
        h == rn || h.starts_with(&format!("{rn}{sep}"))
    })
}

/// True when the exe runs from a cargo build folder (`.../target/<profile>/`), i.e. a
/// development run: v3's equivalent of v2's "not frozen".
pub fn is_dev_build(exe: &Path) -> bool {
    exe.ancestors().any(|a| {
        a.file_name().is_some_and(|n| n == "target")
            && a.parent().is_some_and(|p| p.join("Cargo.toml").is_file())
    })
}

/// The portable data folder, or `None` for an installed build / read-only medium.
fn portable_base(exe: &Path) -> Option<PathBuf> {
    let exe = absolute(exe);
    let (app_folder, host, data_name) = if cfg!(target_os = "macos") {
        let macos = exe.parent()?.to_path_buf(); // .../Contents/MacOS
        let bundle = macos.parent().and_then(Path::parent).map(Path::to_path_buf);
        // Data goes BESIDE the .app, never inside it (that breaks the signature).
        let host = match bundle {
            Some(b) if b.to_string_lossy().ends_with(".app") => b.parent()?.to_path_buf(),
            _ => macos.clone(),
        };
        (macos, host, "VitaThemeArchitect-data")
    } else {
        let folder = exe.parent()?.to_path_buf();
        (folder.clone(), folder, "portable_data")
    };
    // Installed when our installer's uninstaller sits one level above the app
    // folder (Windows), or the app is in a standard program-install path.
    if cfg!(windows) && app_folder.parent().is_some_and(|p| p.join("unins000.exe").is_file()) {
        return None;
    }
    if in_program_location(&app_folder) {
        return None;
    }
    let data = host.join(data_name);
    is_writable_dir(&data).then_some(data)
}

/// Resolve (and create) the data folders for the running exe, doing the same
/// one-time adoption of the pre-per-install layout and `install_path.txt` marker as v2.
pub fn resolve(current_version: &str) -> DataDirs {
    let exe = std::env::current_exe().ok();
    let frozen = exe.as_deref().is_some_and(|e| !is_dev_build(e));
    resolve_for(exe.as_deref().filter(|_| frozen), base_support_dir(), current_version)
}

/// `resolve` with the exe (`None` = source run) and base injected, for tests.
pub fn resolve_for(exe: Option<&Path>, base: PathBuf, current_version: &str) -> DataDirs {
    let portable = exe.and_then(portable_base);
    let install_dir = if portable.is_none() { exe.and_then(normalised_install_dir) } else { None };

    let dirs = if let Some(p) = portable {
        DataDirs { mode: Mode::Portable, app_support: p.clone(), base_support: p, install_key: None, install_dir: None }
    } else if let Some(dir) = install_dir {
        let key = install_key(&dir);
        let installs_root = base.join("installs");
        let app_support = installs_root.join(&key);
        let first_install_here = !app_support.is_dir();
        let installs_had_entries = std::fs::read_dir(&installs_root)
            .map(|rd| rd.flatten().any(|e| e.path().is_dir()))
            .unwrap_or(false);
        let _ = std::fs::create_dir_all(&app_support);
        let legacy = base.join("settings.json");
        if first_install_here && !installs_had_entries && legacy.is_file() {
            for name in ["settings.json", ".data_version"] {
                let (src, dst) = (base.join(name), app_support.join(name));
                if src.is_file() && !dst.exists() {
                    let _ = std::fs::copy(&src, &dst);
                }
            }
        }
        let _ = std::fs::write(app_support.join("install_path.txt"), format!("{dir}\n{current_version}\n"));
        DataDirs { mode: Mode::Installed, app_support, base_support: base, install_key: Some(key), install_dir: Some(dir) }
    } else {
        DataDirs { mode: Mode::Source, app_support: base.clone(), base_support: base, install_key: None, install_dir: None }
    };
    let _ = std::fs::create_dir_all(&dirs.app_support);
    let _ = std::fs::create_dir_all(&dirs.base_support);
    dirs
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Reference keys computed by v2: sha256(normcase(normpath(d))).hexdigest()[:16].
    #[test]
    fn install_key_matches_v2() {
        assert_eq!(install_key(r"c:\program files\vitathemearchitect\app"), "8695f1c63918836d");
        assert_eq!(
            install_key(r"f:\vitathemearchitect\dist\vitathemearchitect-windows-x64"),
            "8ba796b6657fb208"
        );
        assert_eq!(install_key(r"c:\users\ünï\apps\vta"), "b9c0ef841360411b");
        assert_eq!(install_key(""), "e3b0c44298fc1c14");
    }

    #[cfg(windows)]
    #[test]
    fn install_dir_normalisation_matches_v2() {
        let n = |p: &str| normalised_install_dir(&Path::new(p).join("VitaThemeArchitect.exe")).unwrap();
        assert_eq!(n(r"C:\Program Files\VitaThemeArchitect\app"), r"c:\program files\vitathemearchitect\app");
        assert_eq!(n("C:/Users/Ünï/Apps/VTA"), r"c:\users\ünï\apps\vta");
        assert_eq!(install_key(&n("C:/Users/Ünï/Apps/./VTA")), "b9c0ef841360411b");
    }

    #[test]
    fn normpath_like_python() {
        let sep = std::path::MAIN_SEPARATOR;
        assert_eq!(normpath(Path::new("a/./b/../c")), format!("a{sep}c"));
        assert_eq!(normpath(Path::new("../x")), format!("..{sep}x"));
        assert_eq!(normpath(Path::new("")), ".");
    }

    #[test]
    fn store_and_load_paths() {
        let base = std::env::temp_dir().join("vta-paths-test");
        let d = DataDirs { mode: Mode::Portable, app_support: base.clone(), base_support: base.clone(), install_key: None, install_dir: None };
        let inside = base.join("Themes").join("x");
        assert_eq!(d.store_path(&inside.to_string_lossy()), "Themes/x");
        assert_eq!(d.load_path("Themes/x", Path::new("")), PathBuf::from(normpath(&inside)));
        let outside = if cfg!(windows) { r"Z:\elsewhere\Themes" } else { "/elsewhere/Themes" };
        assert_eq!(d.store_path(outside), outside);
        assert_eq!(d.load_path(outside, Path::new("")), PathBuf::from(outside));
        assert_eq!(d.load_path("", Path::new("dflt")), PathBuf::from("dflt"));
    }
}

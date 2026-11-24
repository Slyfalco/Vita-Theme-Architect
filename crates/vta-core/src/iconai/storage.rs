//! Disk use of the AI parts and removing them (v2 `MODEL_SETUPS`, `storage_report`,
//! `_setup_files`, `_snapshot_paths`, `_files_size`, `_folder_size`, `remove_flux2`,
//! `remove_model_setup`, `remove_runtime`, `runtime_disk_usage`,
//! `app_downloaded_ai_bytes`, `remove_app_downloaded_ai_data`, `_app_download_targets`).

use super::worker_files::{backend_label, flux2_repo, helper_model_files, model_files, RepoFile};
use super::IconAi;
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

/// (model key, with poses, label): a model's own files, or a pose add-on's.
pub const MODEL_SETUPS: &[(&str, bool, &str)] = &[
    ("sdxl", false, "High quality model (SDXL)"),
    ("sd15", false, "Standard model (SD 1.5)"),
    ("sdxl", true, "AI poses add-on for High quality"),
    ("sd15", true, "AI poses add-on for Standard"),
];

pub type FileSet = BTreeSet<RepoFile>;

/// v2 `_folder_size(folder)`: bytes used by a folder's files (symlinked files count
/// their target; symlinked folders aren't followed).
pub fn folder_size(folder: &Path) -> u64 {
    let mut total = 0;
    let mut folders = vec![folder.to_path_buf()];
    while let Some(dir) = folders.pop() {
        let Ok(rd) = std::fs::read_dir(&dir) else { continue };
        for e in rd.flatten() {
            let Ok(ft) = e.file_type() else { continue };
            if ft.is_dir() {
                folders.push(e.path());
            } else if let Ok(m) = std::fs::metadata(e.path()) {
                total += m.len();
            }
        }
    }
    total
}

#[derive(Debug, Clone, PartialEq)]
pub struct SetupUsage {
    pub model: String,
    pub poses: bool,
    pub label: String,
    pub bytes: u64,
    pub installed: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Flux2Usage {
    pub variant: String,
    pub label: String,
    pub bytes: u64,
    pub present: bool,
}

/// v2 `storage_report()` dict.
#[derive(Debug, Clone, PartialEq)]
pub struct StorageReport {
    pub total: u64,
    pub runtime: u64,
    pub helpers: u64,
    pub other: u64,
    pub setups: Vec<SetupUsage>,
    pub flux2: Vec<Flux2Usage>,
    /// A shared (user-chosen) model cache is in use: its models aren't removable.
    pub shared: bool,
    pub models_dir: PathBuf,
}

fn remove_path(p: &Path) {
    if p.is_dir() {
        let _ = std::fs::remove_dir_all(p);
    }
}

impl IconAi {
    fn repo_folder(&self, repo: &str) -> PathBuf {
        self.models_dir().join("hub").join(format!("models--{}", repo.replace('/', "--")))
    }

    /// v2 `_setup_files(model, poses)`: what a model needs beyond the helpers, or
    /// what its pose add-on needs beyond the model.
    pub fn setup_files(&self, model: &str, poses: bool) -> FileSet {
        let half = self.runtime_uses_half_precision();
        let helpers: FileSet = helper_model_files(true).into_iter().collect();
        let base: FileSet = model_files(half, true, false, model, false).into_iter().filter(|f| !helpers.contains(f)).collect();
        if poses {
            model_files(half, true, true, model, false)
                .into_iter()
                .filter(|f| !helpers.contains(f) && !base.contains(f))
                .collect()
        } else {
            base
        }
    }

    /// v2 `_snapshot_paths(repo, path)`: the file in every cached snapshot.
    pub fn snapshot_paths(&self, repo: &str, path: &str) -> Vec<PathBuf> {
        let snapshots = self.repo_folder(repo).join("snapshots");
        let Ok(rd) = std::fs::read_dir(&snapshots) else { return Vec::new() };
        rd.flatten().map(|rev| rev.path().join(path)).filter(|p| p.is_file()).collect()
    }

    /// v2 `_files_size(files)`.
    pub fn files_size<'a>(&self, files: impl IntoIterator<Item = &'a RepoFile>) -> u64 {
        files
            .into_iter()
            .flat_map(|(r, p)| self.snapshot_paths(r, p))
            .filter_map(|f| std::fs::metadata(f).ok().map(|m| m.len()))
            .sum()
    }

    /// v2 `_flux2_repo_folder(variant)`.
    pub fn flux2_repo_folder(&self, variant: &str) -> PathBuf {
        self.repo_folder(flux2_repo(variant))
    }

    /// v2 `storage_report()`: disk use per AI part. Files shared by both models
    /// count once (under the first setup).
    pub fn storage_report(&self) -> StorageReport {
        let shared = self.using_shared_cache();
        let icon_ai = self.icon_ai_dir();
        let runtime_dir = self.runtime_dir();
        let mut total = if icon_ai.is_dir() { folder_size(&icon_ai) } else { 0 };
        let runtime = if runtime_dir.is_dir() { folder_size(&runtime_dir) } else { 0 };
        let helpers_set: FileSet = helper_model_files(true).into_iter().collect();
        let helpers = self.files_size(&helpers_set);
        let mut counted = FileSet::new();
        let mut setups = Vec::new();
        for (model, poses, label) in MODEL_SETUPS {
            let files = self.setup_files(model, *poses);
            let installed = files.iter().all(|(r, p)| self.model_cached(r, p));
            let bytes = self.files_size(files.difference(&counted));
            setups.push(SetupUsage { model: model.to_string(), poses: *poses, label: label.to_string(), bytes, installed });
            counted.extend(files);
        }
        let flux2: Vec<Flux2Usage> = ["flux2", "flux2_full"]
            .iter()
            .map(|v| {
                let folder = self.flux2_repo_folder(v);
                Flux2Usage {
                    variant: v.to_string(),
                    label: backend_label(v),
                    bytes: if folder.is_dir() { folder_size(&folder) } else { 0 },
                    present: self.flux2_present(v),
                }
            })
            .collect();
        let flux2_total: u64 = flux2.iter().map(|f| f.bytes).sum();
        let setup_total: u64 = setups.iter().map(|s| s.bytes).sum();
        let other;
        if shared {
            other = 0;
            total = runtime + helpers + setup_total + flux2_total;
        } else {
            other = total.saturating_sub(runtime).saturating_sub(helpers).saturating_sub(setup_total).saturating_sub(flux2_total);
        }
        StorageReport { total, runtime, helpers, other, setups, flux2, shared, models_dir: self.models_dir() }
    }

    /// v2 `remove_flux2(variant)`: delete a FLUX.2 tier (refused with a shared cache).
    pub fn remove_flux2(&self, variant: &str) {
        if self.using_shared_cache() {
            return;
        }
        remove_path(&self.flux2_repo_folder(variant));
    }

    /// v2 `remove_model_setup(model, poses)`: delete one setup's files, keeping
    /// anything another installed setup or the helpers need. Removing a model also
    /// removes its pose add-on. Refused with a shared cache.
    pub fn remove_model_setup(&self, model: &str, poses: bool) {
        if self.using_shared_cache() {
            return;
        }
        let mut remove = self.setup_files(model, poses);
        if !poses {
            remove.extend(self.setup_files(model, true));
        }
        let mut keep: FileSet = helper_model_files(true).into_iter().collect();
        for (other_model, other_poses, _) in MODEL_SETUPS {
            if *other_model == model && (*other_poses == poses || !poses) {
                continue;
            }
            let files = self.setup_files(other_model, *other_poses);
            if files.iter().all(|(r, p)| self.model_cached(r, p)) {
                keep.extend(files);
            }
        }
        let mut repos = BTreeSet::new();
        for (repo, path) in remove.difference(&keep) {
            for f in self.snapshot_paths(repo, path) {
                let target = std::fs::canonicalize(&f).unwrap_or_else(|_| f.clone());
                let _ = std::fs::remove_file(&f);
                if target != f {
                    let _ = std::fs::remove_file(&target);
                }
            }
            repos.insert(repo.clone());
        }
        // A model folder with nothing left in it goes entirely.
        for repo in repos {
            let folder = self.repo_folder(&repo);
            let snapshots = folder.join("snapshots");
            if !(snapshots.is_dir() && has_files(&snapshots)) {
                let _ = std::fs::remove_dir_all(&folder);
            }
        }
    }

    /// v2 `_app_download_targets()`: the runtime always; shared models/examples only
    /// when no other install exists (and the models only when not a custom cache).
    pub fn app_download_targets(&self) -> Vec<PathBuf> {
        let mut t = vec![self.runtime_dir(), self.runtime_marker()];
        if !self.other_installs_exist() {
            t.push(self.examples_dir());
            if !self.using_shared_cache() {
                t.push(self.default_models_dir());
            }
        }
        t
    }

    /// v2 `remove_runtime()`: the whole per-install AI folder plus (if this is the
    /// only install) the shared downloads.
    pub fn remove_runtime(&self) {
        let icon_ai = self.icon_ai_dir();
        let _ = std::fs::remove_dir_all(&icon_ai);
        let prefix = icon_ai.to_string_lossy().to_string();
        for p in self.app_download_targets() {
            if !p.to_string_lossy().starts_with(&prefix) && p.is_dir() {
                let _ = std::fs::remove_dir_all(&p);
            }
        }
    }

    /// v2 `runtime_disk_usage()`.
    pub fn runtime_disk_usage(&self) -> u64 {
        folder_size(&self.icon_ai_dir())
    }

    /// v2 `app_downloaded_ai_bytes()`: size of the AI data the app downloaded itself.
    pub fn app_downloaded_ai_bytes(&self) -> u64 {
        let mut total = 0;
        for p in self.app_download_targets() {
            if p.is_file() {
                total += std::fs::metadata(&p).map(|m| m.len()).unwrap_or(0);
            } else if p.is_dir() {
                total += walk_size(&p);
            }
        }
        total
    }

    /// v2 `remove_app_downloaded_ai_data()`: delete only app-downloaded AI data
    /// (keeps custom models/caches, extra artwork, calibration + key). Returns the
    /// paths removed.
    pub fn remove_app_downloaded_ai_data(&self) -> Vec<PathBuf> {
        let mut removed = Vec::new();
        for p in self.app_download_targets() {
            if p.is_file() {
                if std::fs::remove_file(&p).is_ok() {
                    removed.push(p);
                }
            } else if p.is_dir() {
                let _ = std::fs::remove_dir_all(&p);
                removed.push(p);
            }
        }
        removed
    }
}

/// `os.walk` + `getsize` (follows file symlinks, doesn't descend linked folders).
fn walk_size(dir: &Path) -> u64 {
    folder_size(dir)
}

fn has_files(dir: &Path) -> bool {
    let Ok(rd) = std::fs::read_dir(dir) else { return false };
    rd.flatten().any(|e| match e.file_type() {
        Ok(t) if t.is_dir() => has_files(&e.path()),
        Ok(_) => true,
        Err(_) => false,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn put(ai: &IconAi, repo: &str, path: &str, size: usize) {
        let f = ai.default_models_dir().join("hub").join(format!("models--{}", repo.replace('/', "--"))).join("snapshots").join("abc").join(path);
        std::fs::create_dir_all(f.parent().unwrap()).unwrap();
        std::fs::write(f, vec![0u8; size]).unwrap();
    }

    #[test]
    fn report_and_remove() {
        let ai = crate::iconai::tests::temp_ai("storage");
        // Runtime: 100 bytes. Other: 7 bytes.
        std::fs::create_dir_all(ai.runtime_dir()).unwrap();
        std::fs::write(ai.runtime_dir().join("python.exe"), vec![0u8; 100]).unwrap();
        std::fs::write(ai.icon_ai_dir().join("calibration.dat"), vec![0u8; 7]).unwrap();
        // All SD 1.5 files (10 bytes each), helpers 1 byte each.
        for (r, p) in helper_model_files(true) {
            put(&ai, &r, &p, 1);
        }
        let sd15 = ai.setup_files("sd15", false);
        for (r, p) in &sd15 {
            put(&ai, r, p, 10);
        }
        let rep = ai.storage_report();
        assert!(!rep.shared);
        assert_eq!(rep.runtime, 100);
        assert_eq!(rep.helpers, 10);
        // The shared image encoder (2 files) counts under SDXL, listed first.
        let sdxl = &rep.setups[0];
        assert_eq!((sdxl.bytes, sdxl.installed), (20, false));
        let sd = &rep.setups[1];
        assert_eq!(sd.bytes, 10 * sd15.len() as u64 - 20);
        assert!(sd.installed);
        assert!(!rep.setups[3].installed);
        assert_eq!(rep.flux2.len(), 2);
        assert_eq!(rep.flux2[0].label, "Ultra (FLUX.2 4-bit, local)");
        assert_eq!(rep.total, 100 + 7 + 10 + 10 * sd15.len() as u64);
        assert_eq!(rep.other, 7);

        // Removing SD 1.5 keeps the helpers; the SD 1.5 repos go entirely.
        ai.remove_model_setup("sd15", false);
        assert!(!ai.repo_folder(super::super::worker_files::BASE_MODEL).exists());
        assert!(ai.model_cached(super::super::worker_files::HEAD_REPO, super::super::worker_files::HEAD_FILE));
        assert_eq!(ai.storage_report().setups[1].bytes, 0);

        // Portable/source: everything sits under icon_ai.
        assert!(ai.app_downloaded_ai_bytes() >= 100);
        let removed = ai.remove_app_downloaded_ai_data();
        assert!(removed.contains(&ai.runtime_dir()));
        assert!(ai.icon_ai_dir().join("calibration.dat").is_file());
        ai.remove_runtime();
        assert!(!ai.icon_ai_dir().exists());
    }

    #[test]
    fn folder_size_counts_files() {
        let ai = crate::iconai::tests::temp_ai("fsize");
        let d = ai.dirs.app_support.join("x").join("y");
        std::fs::create_dir_all(&d).unwrap();
        std::fs::write(d.join("a"), [0u8; 5]).unwrap();
        std::fs::write(ai.dirs.app_support.join("x").join("b"), [0u8; 6]).unwrap();
        assert_eq!(folder_size(&ai.dirs.app_support.join("x")), 11);
        assert_eq!(folder_size(Path::new("no/such/dir")), 0);
    }
}

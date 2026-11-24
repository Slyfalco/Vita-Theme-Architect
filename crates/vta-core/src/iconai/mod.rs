//! App-side logic of the AI icon generator: a UI-free port of v2
//! `icon_generator.py` lines 1-1606 plus `RuntimeInstallThread`.
//!
//! The heavy work (torch/diffusers) still runs in a separately downloaded Python
//! runtime executing `icon_ai_worker.py`; this module manages that runtime
//! ([`runtime`]), estimates downloads/time ([`estimates`]), probes the hardware
//! ([`hardware`]), keeps the encrypted performance records ([`calibration`]) and
//! reports/removes disk use ([`storage`]).
//!
//! Everything hangs off [`IconAi`], which holds the data folders (v2's module-level
//! `ICON_AI_DIR`, `MODELS_DIR`, ... constants). Settings are re-read from
//! `settings.json` on every call, like v2's `_settings()`.

pub mod calibration;
pub mod estimates;
pub mod fernet;
pub mod hardware;
pub mod proc;
pub mod py;
pub mod runtime;
pub mod storage;
pub mod worker_files;

use crate::paths::DataDirs;
use crate::settings::Settings;
use serde_json::{Map, Value};
use std::path::{Path, PathBuf};

/// Locations and settings access for the AI icon generator.
#[derive(Debug, Clone)]
pub struct IconAi {
    /// The resolved data folders (`APP_SUPPORT_DIR` / `BASE_SUPPORT_DIR`).
    pub dirs: DataDirs,
    /// v2 `_BASE_DIR`: the folder holding `Assets/` and `icon_ai_worker.py`.
    pub app_dir: PathBuf,
}

impl IconAi {
    pub fn new(dirs: &DataDirs, app_dir: &Path) -> Self {
        IconAi { dirs: dirs.clone(), app_dir: app_dir.to_path_buf() }
    }

    /// `ICON_AI_DIR` = APP_SUPPORT_DIR/icon_ai (per-install AI data).
    pub fn icon_ai_dir(&self) -> PathBuf {
        self.dirs.app_support.join("icon_ai")
    }
    /// `RUNTIME_DIR` = ICON_AI_DIR/python.
    pub fn runtime_dir(&self) -> PathBuf {
        self.icon_ai_dir().join("python")
    }
    /// `_SHARED_AI_DIR` = BASE_SUPPORT_DIR/icon_ai.
    pub fn shared_ai_dir(&self) -> PathBuf {
        self.dirs.base_support.join("icon_ai")
    }
    /// `MODELS_DIR`: the app's own (shared per-user) Hugging Face cache.
    pub fn default_models_dir(&self) -> PathBuf {
        self.shared_ai_dir().join("models")
    }
    /// `EXAMPLES_DIR`: fetched community icon sets.
    pub fn examples_dir(&self) -> PathBuf {
        self.shared_ai_dir().join("examples")
    }
    /// `RUNTIME_MARKER` = ICON_AI_DIR/runtime.json.
    pub fn runtime_marker(&self) -> PathBuf {
        self.icon_ai_dir().join("runtime.json")
    }
    /// `CALIBRATION_FILE` = ICON_AI_DIR/calibration.dat.
    pub fn calibration_file(&self) -> PathBuf {
        self.icon_ai_dir().join("calibration.dat")
    }
    /// `_LOCAL_KEY_FILE` = ICON_AI_DIR/.perf_key.
    pub fn local_key_file(&self) -> PathBuf {
        self.icon_ai_dir().join(".perf_key")
    }
    /// `EXTRA_ART_DIR` = ICON_AI_DIR/extra_artwork.
    pub fn extra_art_dir(&self) -> PathBuf {
        self.icon_ai_dir().join("extra_artwork")
    }
    /// `REFERENCE_DATA_FILE` = <app>/Assets/icon_estimates.dat.
    pub fn reference_data_file(&self) -> PathBuf {
        self.app_dir.join("Assets").join("icon_estimates.dat")
    }
    /// `_worker_script()` = <app>/icon_ai_worker.py.
    pub fn worker_script(&self) -> PathBuf {
        self.app_dir.join("icon_ai_worker.py")
    }
    /// `_templates_dir()`: Assets/IconTemplates, if present.
    pub fn templates_dir(&self) -> Option<PathBuf> {
        Some(self.app_dir.join("Assets").join("IconTemplates")).filter(|p| p.is_dir())
    }
    /// `_examples_dir()`: Assets/IconExamples, if present.
    pub fn builtin_examples_dir(&self) -> Option<PathBuf> {
        Some(self.app_dir.join("Assets").join("IconExamples")).filter(|p| p.is_dir())
    }

    /// v2 `_settings()`: settings.json read fresh (empty when missing/corrupt).
    pub fn settings(&self) -> Settings {
        Settings::load(&self.dirs.settings_file())
    }

    /// v2 `_models_dir()`: a user-chosen shared HF cache (`shared_model_cache`,
    /// stripped) or the app's own `MODELS_DIR`.
    pub fn models_dir(&self) -> PathBuf {
        let shared = self.settings().str("shared_model_cache").unwrap_or("").trim().to_string();
        if shared.is_empty() { self.default_models_dir() } else { PathBuf::from(shared) }
    }

    /// v2 `_using_shared_cache()`: `_models_dir() != MODELS_DIR` (string compare).
    pub fn using_shared_cache(&self) -> bool {
        self.models_dir().to_string_lossy() != self.default_models_dir().to_string_lossy()
    }

    /// v2 `constants.other_per_install_dirs_exist()`.
    pub fn other_installs_exist(&self) -> bool {
        self.dirs.other_per_install_dirs_exist()
    }

    /// Port of the dialog's `_apply_model_settings(job)`: folds the user's HF token,
    /// Minimal-VRAM flag and a matching custom base model (`custom_base_model`,
    /// `custom_base_arch`) into the job. Returns the log lines v2 appends.
    pub fn apply_model_settings(&self, job: &mut Map<String, Value>) -> Vec<String> {
        let settings = self.settings();
        let mut log = Vec::new();
        job.insert("hf_token".into(), Value::String(settings.str("hf_token").unwrap_or("").trim().to_string()));
        job.insert("min_memory".into(), Value::Bool(settings.flag("ai_min_memory", false)));
        let path = settings.str("custom_base_model").unwrap_or("").trim().to_string();
        if path.is_empty() || !Path::new(&path).exists() {
            return log;
        }
        let mut arch = settings.str("custom_base_arch").filter(|s| !s.is_empty()).unwrap_or("auto").to_string();
        if arch == "auto" {
            arch = worker_files::detect_base_arch(Path::new(&path)).to_string();
        }
        let model = job.get("model").and_then(Value::as_str).unwrap_or("").to_string();
        if job.get("model").is_some_and(|m| m.as_str() == Some(arch.as_str())) {
            job.insert("custom_base_model".into(), Value::String(path.clone()));
            log.push(format!("Using your custom {} model: {}", arch.to_uppercase(), py::basename(&path)));
        } else if !arch.is_empty() {
            log.push(format!(
                "Custom model looks like {} but the selected quality tier is {}; using the default model for that tier instead.",
                arch.to_uppercase(),
                model.to_uppercase()
            ));
        }
        log
    }
}

/// v2 `platform.system()`: "Windows", "Darwin" or "Linux".
pub fn system() -> &'static str {
    if cfg!(windows) {
        "Windows"
    } else if cfg!(target_os = "macos") {
        "Darwin"
    } else if cfg!(target_os = "linux") {
        "Linux"
    } else {
        std::env::consts::OS
    }
}

/// v2 `platform.machine()`: "AMD64"/"ARM64" on Windows (from the processor
/// environment variables, as Python's fallback does), the kernel's name elsewhere
/// ("x86_64", "arm64" on macOS, "aarch64" on Linux).
pub fn machine() -> String {
    if cfg!(windows) {
        for var in ["PROCESSOR_ARCHITEW6432", "PROCESSOR_ARCHITECTURE"] {
            if let Ok(v) = std::env::var(var) {
                if !v.is_empty() {
                    return v;
                }
            }
        }
        return match std::env::consts::ARCH {
            "x86_64" => "AMD64",
            "aarch64" => "ARM64",
            "x86" => "x86",
            other => other,
        }
        .to_string();
    }
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("macos", "aarch64") => "arm64".into(),
        (_, "x86") => "i686".into(),
        (_, arch) => arch.into(),
    }
}

/// Apple Silicon (`Darwin` + arm64/aarch64), as v2 checks it.
pub fn is_apple_silicon() -> bool {
    system() == "Darwin" && matches!(machine().to_lowercase().as_str(), "arm64" | "aarch64")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::paths::Mode;

    pub(crate) fn temp_ai(name: &str) -> IconAi {
        let base = std::env::temp_dir().join(format!("vta-iconai-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();
        let dirs = DataDirs { mode: Mode::Portable, app_support: base.clone(), base_support: base.clone(), install_key: None, install_dir: None };
        IconAi::new(&dirs, &base.join("app"))
    }

    #[test]
    fn paths_and_shared_cache() {
        let ai = temp_ai("paths");
        assert!(ai.runtime_marker().ends_with("icon_ai/runtime.json"));
        assert!(!ai.using_shared_cache());
        let mut s = ai.settings();
        s.set("shared_model_cache", "  ");
        s.save().unwrap();
        assert!(!ai.using_shared_cache());
        s.set("shared_model_cache", " D:/hf ");
        s.save().unwrap();
        assert!(ai.using_shared_cache());
        assert_eq!(ai.models_dir(), PathBuf::from("D:/hf"));
    }

    #[test]
    fn model_settings_into_job() {
        let ai = temp_ai("modelset");
        let model_dir = ai.dirs.app_support.join("mymodel");
        std::fs::create_dir_all(model_dir.join("text_encoder_2")).unwrap();
        let mut s = ai.settings();
        s.set("custom_base_model", model_dir.to_string_lossy().to_string());
        s.set("ai_min_memory", true);
        s.save().unwrap();
        let mut job = Map::new();
        job.insert("model".into(), "sdxl".into());
        let log = ai.apply_model_settings(&mut job);
        assert_eq!(job["min_memory"], Value::Bool(true));
        assert_eq!(job["hf_token"], "");
        assert!(job.contains_key("custom_base_model"));
        assert_eq!(log, vec!["Using your custom SDXL model: mymodel".to_string()]);
        let mut job = Map::new();
        job.insert("model".into(), "sd15".into());
        let log = ai.apply_model_settings(&mut job);
        assert!(!job.contains_key("custom_base_model"));
        assert_eq!(log[0], "Custom model looks like SDXL but the selected quality tier is SD15; using the default model for that tier instead.");
    }
}

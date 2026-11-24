//! The private Python runtime the worker runs in: detection, update check, the
//! pip command lines (torch index / CUDA 13 selection, Intel-mac pins), the
//! install procedure of v2's `RuntimeInstallThread`, trimming, the worker
//! subprocess environment, and the bitsandbytes add-on.

use super::estimates::{download_text, format_size};
use super::hardware::nvidia_smi;
use super::proc::{hide_window, run_capture, run_process};
use super::storage::folder_size;
use super::{machine, py, system, IconAi};
use serde_json::{json, Value};
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

/// Bump when the pip package set changes so existing installs get redone.
pub const RUNTIME_VERSION: i64 = 2;
pub const PYTHON_BUILD_TAG: &str = "20261013";
pub const PYTHON_VERSION: &str = "3.12.15";
/// `{tag}`, `{ver}`, `{triple}` placeholders, as v2 `PYTHON_URL`.
pub const PYTHON_URL: &str =
    "https://github.com/astral-sh/python-build-standalone/releases/download/{tag}/cpython-{ver}+{tag}-{triple}-install_only.tar.gz";
pub const TORCH_CUDA_INDEX: &str = "https://download.pytorch.org/whl/cu126";
/// CUDA 13 wheels: smaller, need an R580+ driver and a Turing-or-newer GPU.
pub const TORCH_CUDA13_INDEX: &str = "https://download.pytorch.org/whl/cu130";
pub const CUDA13_MIN_COMPUTE: [i64; 2] = [7, 5];
pub const TORCH_CPU_INDEX: &str = "https://download.pytorch.org/whl/cpu";
pub const AI_PACKAGES: &[&str] = &["diffusers", "transformers", "accelerate", "safetensors", "pillow", "onnxruntime"];
/// Intel Macs: the last PyTorch with x86_64 macOS wheels is 2.2.2.
pub const INTEL_MAC_PACKAGES: &[&str] = &[
    "torch==2.2.2", "numpy<2", "diffusers==0.30.3", "transformers==4.44.2",
    "accelerate==0.34.2", "safetensors", "pillow", "onnxruntime==1.19.2",
];
/// v2 `INSTALL_SIZE_MSG` without the folder line (callers append `icon_ai_dir`).
pub fn install_size_msg(icon_ai_dir: &Path) -> String {
    format!(
        "This downloads a private Python runtime and PyTorch (about 0.5 GB, or 2-2.5 GB with NVIDIA GPU support). \
         On the first generation, about 5.6 GB of AI models are downloaded (about 8.5 GB without a GPU).\n\n\
         Everything is stored in:\n{}\n\nYou can remove it again from this dialog at any time.",
        icon_ai_dir.display()
    )
}

/// v2 `CUDA13_MIN_DRIVER` for the current system.
pub fn cuda13_min_driver(system: &str) -> Option<[i64; 2]> {
    match system {
        "Windows" => Some([581, 0]),
        "Linux" => Some([580, 65]),
        _ => None,
    }
}

/// v2 `_use_app_python()`. Always false in v3: the app is no longer a Python
/// program, so (like a v2 compiled build) the worker always uses the private runtime.
pub const USE_APP_PYTHON: bool = false;

/// v2 `_python_triple()` for a (system, machine) pair.
pub fn python_triple_for(system: &str, machine: &str) -> Option<String> {
    let arch = match machine.to_lowercase().as_str() {
        "amd64" | "x86_64" => "x86_64",
        "arm64" | "aarch64" => "aarch64",
        _ => return None,
    };
    match system {
        "Windows" => Some(format!("{arch}-pc-windows-msvc")),
        "Darwin" => Some(format!("{arch}-apple-darwin")),
        "Linux" => Some(format!("{arch}-unknown-linux-gnu")),
        _ => None,
    }
}

/// v2 `_python_triple()`.
pub fn python_triple() -> Option<String> {
    python_triple_for(system(), &machine())
}

/// v2 `PYTHON_URL.format(...)` (`triple` None formats as "None", as in v2).
pub fn python_url(triple: Option<&str>) -> String {
    PYTHON_URL
        .replace("{tag}", PYTHON_BUILD_TAG)
        .replace("{ver}", PYTHON_VERSION)
        .replace("{triple}", triple.unwrap_or("None"))
}

/// v2 `_parse_version`: first two dot-separated integers.
pub fn parse_version(text: &str) -> Option<Vec<i64>> {
    text.trim().split('.').take(2).map(|p| p.trim().parse::<i64>().ok()).collect()
}

/// v2 `_supports_cuda13` on nvidia-smi's output (`driver_version,compute_cap` rows).
pub fn supports_cuda13_from(system: &str, code: i32, stdout: &str) -> bool {
    let rows: Vec<Vec<&str>> = stdout.trim().lines().filter(|l| !l.trim().is_empty()).map(|l| l.split(',').collect()).collect();
    if code != 0 || rows.is_empty() {
        return false;
    }
    let Some(min_driver) = cuda13_min_driver(system) else { return false };
    rows.iter().all(|row| {
        if row.len() != 2 {
            return false;
        }
        match (parse_version(row[0]), parse_version(row[1])) {
            (Some(d), Some(c)) => d.as_slice() >= min_driver.as_slice() && c.as_slice() >= CUDA13_MIN_COMPUTE.as_slice(),
            _ => false,
        }
    })
}

/// v2 `_supports_cuda13(smi)`: every GPU and the driver are confirmed CUDA 13 capable.
pub fn supports_cuda13(smi: &Path) -> bool {
    match run_capture(smi, &["--query-gpu=driver_version,compute_cap", "--format=csv,noheader"], Duration::from_secs(15), true) {
        Some(o) => supports_cuda13_from(system(), o.code, &o.stdout),
        None => false,
    }
}

/// v2 `_pip_install_commands()` for given facts (`cuda13` = `Some(..)` when an
/// NVIDIA GPU was found on x86_64).
pub fn pip_install_commands_for(system: &str, machine: &str, nvidia: Option<bool>) -> Vec<Vec<String>> {
    let s = |v: &[&str]| v.iter().map(|x| x.to_string()).collect::<Vec<_>>();
    let m = machine.to_lowercase();
    if system == "Darwin" {
        if m == "x86_64" || m == "amd64" {
            return vec![s(INTEL_MAC_PACKAGES)];
        }
        let mut v = vec!["torch".to_string()];
        v.extend(s(AI_PACKAGES));
        return vec![v];
    }
    let index = match nvidia {
        Some(true) => TORCH_CUDA13_INDEX,
        Some(false) => TORCH_CUDA_INDEX,
        None => TORCH_CPU_INDEX,
    };
    vec![s(&["torch", "--index-url", index]), s(AI_PACKAGES)]
}

/// v2 `_pip_install_commands()`: pip argument lists for this machine (runs
/// nvidia-smi each call, as v2 does).
pub fn pip_install_commands() -> Vec<Vec<String>> {
    let (sys, m) = (system(), machine());
    let x64 = matches!(m.to_lowercase().as_str(), "x86_64" | "amd64");
    let nvidia = if sys != "Darwin" && x64 { nvidia_smi().map(|smi| supports_cuda13(&smi)) } else { None };
    pip_install_commands_for(sys, &m, nvidia)
}

/// Events from [`IconAi::install_runtime`] (v2 signals `log` and `progress`).
#[derive(Debug, Clone, PartialEq)]
pub enum InstallEvent {
    Log(String),
    /// percent (-1 = busy), status text.
    Progress(i32, String),
}

fn fail(kind: &str, msg: impl std::fmt::Display) -> String {
    format!("{kind}: {msg}")
}

fn http_error(e: ureq::Error, url: &str) -> String {
    match e {
        ureq::Error::StatusCode(code) => {
            let reason = ureq::http::StatusCode::from_u16(code).ok().and_then(|c| c.canonical_reason()).unwrap_or("");
            let side = if code < 500 { "Client" } else { "Server" };
            fail("HTTPError", format!("{code} {side} Error: {reason} for url: {url}"))
        }
        other => fail("ConnectionError", other),
    }
}

impl IconAi {
    /// v2 `_runtime_python()`: the private runtime's interpreter.
    pub fn runtime_python(&self) -> PathBuf {
        if system() == "Windows" {
            self.runtime_dir().join("python.exe")
        } else {
            self.runtime_dir().join("bin").join("python3")
        }
    }

    /// v2 `is_runtime_installed()`: marker version matches and the interpreter exists.
    pub fn is_runtime_installed(&self) -> bool {
        let Ok(text) = std::fs::read(self.runtime_marker()) else { return false };
        let Ok(marker) = serde_json::from_slice::<Value>(&text) else { return false };
        let version_ok = marker.get("version").is_some_and(|v| py::eq(v, &json!(RUNTIME_VERSION)));
        version_ok && self.runtime_python().is_file()
    }

    /// v2 `runtime_needs_update()`: an older runtime exists (updating keeps PyTorch).
    pub fn runtime_needs_update(&self) -> bool {
        self.runtime_python().is_file() && !self.is_runtime_installed()
    }

    /// v2 `_runtime_uses_half_precision()`: the marker's packages came from a CUDA index.
    pub fn runtime_uses_half_precision(&self) -> bool {
        let Ok(text) = std::fs::read(self.runtime_marker()) else { return false };
        let Ok(marker) = serde_json::from_slice::<Value>(&text) else { return false };
        py::dumps(marker.get("packages").unwrap_or(&Value::Null)).contains("download.pytorch.org/whl/cu")
    }

    /// v2 `_child_env()`: the environment for runtime subprocesses: the current
    /// environment minus `PYTHON*`, `QT_*`, `DYLD_*`, `LD_LIBRARY_PATH`, plus
    /// `HF_HOME` (= models dir) and the HF/pip quiet flags. (On Windows the names
    /// are upper-cased, as Python's `os.environ` does.)
    pub fn child_env(&self) -> Vec<(OsString, OsString)> {
        let mut env: Vec<(OsString, OsString)> = std::env::vars_os()
            .filter_map(|(k, v)| {
                let name = k.to_string_lossy().to_string();
                let name = if cfg!(windows) { name.to_uppercase() } else { name };
                let drop = name.starts_with("PYTHON") || name.starts_with("QT_") || name.starts_with("DYLD_") || name == "LD_LIBRARY_PATH";
                (!drop).then(|| (OsString::from(name), v))
            })
            .collect();
        let mut set = |k: &str, v: OsString| {
            env.retain(|(name, _)| name != k);
            env.push((k.into(), v));
        };
        set("HF_HOME", self.models_dir().into_os_string());
        set("HF_HUB_DISABLE_TELEMETRY", "1".into());
        set("PIP_DISABLE_PIP_VERSION_CHECK", "1".into());
        set("HF_HUB_DISABLE_XET", "1".into());
        env
    }

    /// v2 `_package_installed(name)`: `name` importable in the AI runtime.
    pub fn package_installed(&self, name: &str) -> bool {
        let py_exe = self.runtime_python();
        if !py_exe.is_file() {
            return false;
        }
        let code = format!(
            "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec({}) else 1)",
            py_repr(name)
        );
        run_capture(&py_exe, &["-I", "-c", &code], Duration::from_secs(30), true).is_some_and(|o| o.code == 0)
    }

    /// v2 `_install_bitsandbytes()`: pip-install bitsandbytes (FLUX.2 4-bit). True on success.
    pub fn install_bitsandbytes(&self) -> bool {
        let py_exe = self.runtime_python();
        if !py_exe.is_file() {
            return false;
        }
        run_capture(&py_exe, &["-I", "-m", "pip", "install", "--progress-bar", "off", "bitsandbytes"], Duration::from_secs(900), true)
            .is_some_and(|o| o.code == 0)
    }

    /// v2 `_strip_runtime()`: delete runtime parts the worker never uses (tests,
    /// Tk/IDLE, 2to3, static libs). Returns the bytes freed.
    pub fn strip_runtime(&self) -> u64 {
        let rt = self.runtime_dir();
        let mut lib = Some(rt.join("Lib")).filter(|p| p.is_dir());
        if lib.is_none() {
            lib = std::fs::read_dir(rt.join("lib")).ok().and_then(|rd| {
                rd.flatten()
                    .find(|e| {
                        let n = e.file_name().to_string_lossy().to_string();
                        n.starts_with("python3.") && !n.starts_with('.')
                    })
                    .map(|e| e.path())
            });
        }
        let mut targets: Vec<PathBuf> = Vec::new();
        if let Some(lib) = &lib {
            for d in ["test", "tkinter", "idlelib", "turtledemo", "lib2to3", "pydoc_data", "ensurepip"] {
                targets.push(lib.join(d));
            }
        }
        targets.push(rt.join("tcl"));
        for f in ["_tkinter.pyd", "tcl86t.dll", "tk86t.dll"] {
            targets.push(rt.join("DLLs").join(f));
        }
        collect_static_libs(&rt, &mut targets);
        let mut freed = 0;
        for t in targets {
            if t.is_dir() {
                freed += folder_size(&t);
                let _ = std::fs::remove_dir_all(&t);
            } else if t.is_file() {
                if let Ok(m) = std::fs::metadata(&t) {
                    if std::fs::remove_file(&t).is_ok() {
                        freed += m.len();
                    }
                }
            }
        }
        freed
    }

    /// The work of v2's `RuntimeInstallThread.run()`: download + extract
    /// python-build-standalone (fresh installs), run the pip commands, write
    /// `runtime.json`. Blocking; `on_event` gets the log/progress the dialog
    /// showed. `cancel` aborts (terminating pip). Returns v2's `finished_ok`
    /// (success, message): "AI components installed.", "Installation cancelled.",
    /// or "<ErrorType>: <message>".
    pub fn install_runtime(&self, cancel: &AtomicBool, mut on_event: impl FnMut(InstallEvent)) -> (bool, String) {
        let mut fresh = false;
        match self.install_inner(cancel, &mut fresh, &mut on_event) {
            Ok(()) => (true, "AI components installed.".into()),
            Err(e) => {
                if cancel.load(Ordering::SeqCst) {
                    // Drop a half-installed fresh runtime (an update keeps the old one).
                    if fresh {
                        let _ = std::fs::remove_dir_all(self.runtime_dir());
                    }
                    (false, "Installation cancelled.".into())
                } else {
                    (false, e)
                }
            }
        }
    }

    fn install_inner(&self, cancel: &AtomicBool, fresh: &mut bool, on_event: &mut dyn FnMut(InstallEvent)) -> Result<(), String> {
        let Some(triple) = python_triple() else {
            return Err(fail("RuntimeError", format!("Unsupported platform: {} {}", system(), machine())));
        };
        let _ = std::fs::remove_file(self.runtime_marker());
        std::fs::create_dir_all(self.icon_ai_dir()).map_err(|e| fail("OSError", e))?;
        *fresh = !self.runtime_python().is_file();
        if *fresh {
            self.install_python(&triple, cancel, on_event)?;
        }
        let python = self.runtime_python();
        let env = self.child_env();
        for pkgs in pip_install_commands() {
            if cancel.load(Ordering::SeqCst) {
                return Err(fail("RuntimeError", "cancelled"));
            }
            let name = pkgs[0].split('=').next().unwrap_or("").split('<').next().unwrap_or("").to_string();
            let label = format!("Installing {name}{}", if pkgs.len() > 1 { " and dependencies" } else { "" });
            on_event(InstallEvent::Progress(-1, format!("{label}...")));
            let mut args: Vec<OsString> = ["-I", "-m", "pip", "install", "--no-warn-script-location", "--progress-bar", "raw"]
                .iter()
                .map(OsString::from)
                .collect();
            args.extend(pkgs.iter().map(OsString::from));
            let mut progress = PipProgress::new(&label);
            // Events are forwarded as they arrive.
            let code = {
                let ev = std::cell::RefCell::new(&mut *on_event);
                run_process(
                    &python,
                    &args,
                    &env,
                    cancel,
                    |line| match progress.parse(line) {
                        Some((pct, text)) => {
                            let mut g = ev.borrow_mut();
                            (*g)(InstallEvent::Progress(pct, text));
                            true
                        }
                        None => false,
                    },
                    |line| {
                        let mut g = ev.borrow_mut();
                        (*g)(InstallEvent::Log(line))
                    },
                )
                .map_err(|e| fail("OSError", e))?
            };
            if code != 0 {
                return Err(fail("RuntimeError", format!("pip install failed (exit code {code}). See the log for details.")));
            }
        }
        let marker = json!({"version": RUNTIME_VERSION, "python": PYTHON_VERSION, "packages": pip_install_commands()});
        std::fs::write(self.runtime_marker(), py::dumps_indent(&marker, 2)).map_err(|e| fail("OSError", e))?;
        Ok(())
    }

    /// v2 `RuntimeInstallThread._install_python(triple)`.
    fn install_python(&self, triple: &str, cancel: &AtomicBool, on_event: &mut dyn FnMut(InstallEvent)) -> Result<(), String> {
        use std::io::{Read, Write};
        let _ = std::fs::remove_dir_all(self.runtime_dir());
        let url = python_url(Some(triple));
        on_event(InstallEvent::Log(format!("Downloading {url}")));
        let archive = temp_archive_path();
        let result = (|| -> Result<(), String> {
            let agent: ureq::Agent = ureq::Agent::config_builder()
                .timeout_connect(Some(Duration::from_secs(60)))
                .timeout_recv_response(Some(Duration::from_secs(60)))
                .build()
                .into();
            let mut resp = agent.get(&url).call().map_err(|e| http_error(e, &url))?;
            let total: u64 = resp
                .headers()
                .get("content-length")
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.parse().ok())
                .unwrap_or(0);
            let mut reader = resp.body_mut().with_config().limit(u64::MAX).reader();
            let mut out = std::fs::File::create(&archive).map_err(|e| fail("OSError", e))?;
            let mut buf = vec![0u8; 1 << 16];
            let (mut done, start) = (0u64, Instant::now());
            loop {
                let n = reader.read(&mut buf).map_err(|e| fail("ChunkedEncodingError", e))?;
                if n == 0 {
                    break;
                }
                if cancel.load(Ordering::SeqCst) {
                    return Err(fail("RuntimeError", "cancelled"));
                }
                out.write_all(&buf[..n]).map_err(|e| fail("OSError", e))?;
                done += n as u64;
                if total > 0 {
                    let elapsed = start.elapsed().as_secs_f64();
                    let speed = (elapsed > 1.0).then(|| done as f64 / elapsed);
                    on_event(InstallEvent::Progress(
                        (done * 100 / total) as i32,
                        download_text("Downloading Python runtime", done as f64, total as f64, speed),
                    ));
                }
            }
            drop(out);
            on_event(InstallEvent::Progress(-1, "Extracting Python runtime...".into()));
            // The archive's top-level folder is "python/".
            let file = std::fs::File::open(&archive).map_err(|e| fail("OSError", e))?;
            let mut tar = tar::Archive::new(flate2::read::GzDecoder::new(file));
            tar.set_preserve_permissions(false);
            tar.unpack(self.icon_ai_dir()).map_err(|e| fail("ReadError", e))?;
            let freed = self.strip_runtime();
            if freed > 0 {
                on_event(InstallEvent::Log(format!("Trimmed {} of unused files from the Python runtime.", format_size(freed as f64))));
            }
            Ok(())
        })();
        let _ = std::fs::remove_file(&archive);
        result
    }
}

/// v2 `tempfile.mkstemp(prefix='vta_python_', suffix='.tar.gz')`.
fn temp_archive_path() -> PathBuf {
    let mut rnd = [0u8; 6];
    let _ = getrandom::getrandom(&mut rnd);
    let tag: String = rnd.iter().map(|b| format!("{b:02x}")).collect();
    std::env::temp_dir().join(format!("vta_python_{tag}.tar.gz"))
}

/// `repr(str)` for the simple names v2 passes to `_package_installed`.
fn py_repr(s: &str) -> String {
    format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'"))
}

/// `glob(RUNTIME_DIR/**/libpython*.a, recursive=True)` (hidden folders skipped, as glob does).
fn collect_static_libs(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().to_string();
        if name.starts_with('.') {
            continue;
        }
        let path = e.path();
        if e.file_type().is_ok_and(|t| t.is_dir()) {
            collect_static_libs(&path, out);
        } else if name.starts_with("libpython") && name.ends_with(".a") {
            out.push(path);
        }
    }
}

/// v2 `RuntimeInstallThread._pip_progress(label)`: turns pip's raw
/// "Progress X of Y" lines into (percent, text with time left).
pub struct PipProgress {
    label: String,
    total: Option<u64>,
    start: Instant,
    re: regex::Regex,
}

impl PipProgress {
    pub fn new(label: &str) -> Self {
        PipProgress {
            label: label.to_string(),
            total: None,
            start: Instant::now(),
            re: regex::Regex::new(r"^Progress (\d+) of (\d+)").expect("valid regex"),
        }
    }

    /// `Some((percent, text))` for a progress line (not logged), `None` otherwise.
    pub fn parse(&mut self, line: &str) -> Option<(i32, String)> {
        let c = self.re.captures(line)?;
        let done: u64 = c[1].parse().ok()?;
        let total: u64 = c[2].parse().ok()?;
        if Some(total) != self.total {
            self.total = Some(total);
            self.start = Instant::now();
        }
        let elapsed = self.start.elapsed().as_secs_f64();
        let speed = (done > 0 && elapsed > 1.0).then(|| done as f64 / elapsed);
        Some(((done * 100 / total.max(1)) as i32, download_text(&self.label, done as f64, total as f64, speed)))
    }
}

/// Start the worker-style subprocess without the streaming helper (used by
/// callers that manage their own I/O): v2's Popen flags (`stdin` closed, hidden
/// window, `_child_env()`).
pub fn worker_command(ai: &IconAi, args: &[OsString]) -> Command {
    let mut cmd = Command::new(ai.runtime_python());
    cmd.args(args).env_clear().envs(ai.child_env()).stdin(Stdio::null());
    hide_window(&mut cmd, true);
    cmd
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn triples_and_urls() {
        assert_eq!(python_triple_for("Windows", "AMD64").as_deref(), Some("x86_64-pc-windows-msvc"));
        assert_eq!(python_triple_for("Windows", "ARM64").as_deref(), Some("aarch64-pc-windows-msvc"));
        assert_eq!(python_triple_for("Darwin", "arm64").as_deref(), Some("aarch64-apple-darwin"));
        assert_eq!(python_triple_for("Linux", "x86_64").as_deref(), Some("x86_64-unknown-linux-gnu"));
        assert_eq!(python_triple_for("Linux", "i686"), None);
        assert_eq!(
            python_url(Some("x86_64-pc-windows-msvc")),
            "https://github.com/astral-sh/python-build-standalone/releases/download/20261013/cpython-3.12.15+20261013-x86_64-pc-windows-msvc-install_only.tar.gz"
        );
    }

    #[test]
    fn cuda13_detection() {
        assert!(supports_cuda13_from("Windows", 0, "581.15, 8.9\n"));
        assert!(!supports_cuda13_from("Windows", 0, "580.97, 8.9\n"));
        assert!(supports_cuda13_from("Linux", 0, "580.65.06, 7.5"));
        assert!(!supports_cuda13_from("Linux", 0, "590.1, 6.1"));
        assert!(!supports_cuda13_from("Windows", 0, "590.1, 8.9\n590.1, 6.1"));
        assert!(!supports_cuda13_from("Windows", 1, "590.1, 8.9"));
        assert!(!supports_cuda13_from("Windows", 0, ""));
        assert!(!supports_cuda13_from("Darwin", 0, "590.1, 8.9"));
        assert!(!supports_cuda13_from("Windows", 0, "581, 8.9")); // (581,) < (581, 0), as v2
    }

    #[test]
    fn pip_commands() {
        let c = pip_install_commands_for("Windows", "AMD64", Some(true));
        assert_eq!(c[0], vec!["torch", "--index-url", TORCH_CUDA13_INDEX]);
        assert_eq!(c[1], AI_PACKAGES.iter().map(|s| s.to_string()).collect::<Vec<_>>());
        assert_eq!(pip_install_commands_for("Linux", "x86_64", Some(false))[0][2], TORCH_CUDA_INDEX);
        assert_eq!(pip_install_commands_for("Linux", "aarch64", None)[0][2], TORCH_CPU_INDEX);
        assert_eq!(pip_install_commands_for("Darwin", "x86_64", None), vec![INTEL_MAC_PACKAGES.iter().map(|s| s.to_string()).collect::<Vec<_>>()]);
        let arm = pip_install_commands_for("Darwin", "arm64", None);
        assert_eq!(arm.len(), 1);
        assert_eq!(arm[0][0], "torch");
        assert_eq!(arm[0].len(), 7);
    }

    #[test]
    fn pip_progress_lines() {
        let mut p = PipProgress::new("Installing torch and dependencies");
        assert!(p.parse("Collecting torch").is_none());
        let (pct, text) = p.parse("Progress 1048576 of 4194304").unwrap();
        assert_eq!(pct, 25);
        assert_eq!(text, "Installing torch and dependencies: 1.0 MB of 4.0 MB");
    }

    #[test]
    fn marker_and_half_precision() {
        let ai = crate::iconai::tests::temp_ai("marker");
        std::fs::create_dir_all(ai.icon_ai_dir()).unwrap();
        assert!(!ai.is_runtime_installed());
        let marker = json!({"version": 2, "python": PYTHON_VERSION,
            "packages": pip_install_commands_for("Windows", "AMD64", Some(false))});
        std::fs::write(ai.runtime_marker(), py::dumps_indent(&marker, 2)).unwrap();
        assert!(ai.runtime_uses_half_precision());
        assert!(!ai.is_runtime_installed()); // no interpreter yet
        let py_exe = ai.runtime_python();
        std::fs::create_dir_all(py_exe.parent().unwrap()).unwrap();
        std::fs::write(&py_exe, b"").unwrap();
        assert!(ai.is_runtime_installed());
        assert!(!ai.runtime_needs_update());
        std::fs::write(ai.runtime_marker(), r#"{"version": 1}"#).unwrap();
        assert!(ai.runtime_needs_update());
        assert!(!ai.runtime_uses_half_precision());
    }

    #[test]
    fn strip_removes_unused_parts() {
        let ai = crate::iconai::tests::temp_ai("strip");
        let rt = ai.runtime_dir();
        let lib = if cfg!(windows) { rt.join("Lib") } else { rt.join("lib").join("python3.12") };
        std::fs::create_dir_all(lib.join("test")).unwrap();
        std::fs::write(lib.join("test").join("a.py"), vec![0u8; 100]).unwrap();
        std::fs::create_dir_all(lib.join("json")).unwrap();
        std::fs::write(lib.join("json").join("b.py"), vec![0u8; 10]).unwrap();
        std::fs::create_dir_all(lib.join("config")).unwrap();
        std::fs::write(lib.join("config").join("libpython3.12.a"), vec![0u8; 50]).unwrap();
        assert_eq!(ai.strip_runtime(), 150);
        assert!(lib.join("json").join("b.py").is_file());
        assert!(!lib.join("test").exists());
    }

    #[test]
    fn child_env_filters() {
        let ai = crate::iconai::tests::temp_ai("env");
        let env = ai.child_env();
        let get = |k: &str| env.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone());
        assert_eq!(get("HF_HOME"), Some(ai.default_models_dir().into_os_string()));
        assert_eq!(get("HF_HUB_DISABLE_XET"), Some("1".into()));
        assert!(env.iter().all(|(n, _)| !n.to_string_lossy().starts_with("PYTHON")));
    }
}

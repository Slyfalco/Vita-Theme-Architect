//! Download/time/memory estimates, requirement checks and text formatting
//! (v2 `MODEL_CHOICES` ... `estimate_models`, `format_*`, `download_text`).

use super::hardware::{gpu_speed, hardware_print, HwSource, LiveHw};
use super::runtime::{pip_install_commands, python_triple_for, python_url, python_triple, USE_APP_PYTHON};
use super::worker_files::{flux2_files, flux2_key_files, model_files};
use super::{machine, py, system, IconAi};
use serde_json::{json, Map, Value};
use std::io::Read;
use std::time::{Duration, Instant};

pub const SPEED_TEST_BYTES: u64 = 64 * 1024 * 1024;
pub const SPEED_TEST_SECONDS: f64 = 5.0;
pub const SPEED_WARMUP: f64 = 1.0;
/// Python runtime plus diffusers/transformers/numpy/etc. besides PyTorch.
pub const LIBRARIES_ESTIMATE: f64 = 180.0 * 1024.0 * 1024.0;
pub const UPDATE_ESTIMATE: f64 = 20.0 * 1024.0 * 1024.0;
/// Installed size vs download (v2 `UNPACK_FACTOR`).
pub const UNPACK_FACTOR_INSTALL: f64 = 2.2;
pub const UNPACK_FACTOR_MODELS: f64 = 1.05;
/// Keep 1 GB free on top (v2 `DISK_MARGIN`).
pub const DISK_MARGIN: f64 = 1024.0 * 1024.0 * 1024.0;
/// How much slower the minimal-VRAM path is per icon.
pub const MINMEM_SLOWDOWN: f64 = 4.0;

/// (label, worker model key, minimum VRAM in GB to suggest it by default).
pub const MODEL_CHOICES: &[(&str, &str, f64)] = &[
    ("High quality (SDXL anime, 8 GB GPU)", "sdxl", 7.5),
    ("Standard (SD 1.5, 4 GB GPU)", "sd15", 0.0),
];
/// FLUX.2 tiers: (label, key, minimum VRAM to offer it).
pub const FLUX2_CHOICES: &[(&str, &str, f64)] = &[
    ("Ultra (FLUX.2 4-bit — slower, for ~8–12 GB GPUs, large download)", "flux2", 7.5),
    ("Ultra+ (FLUX.2 full — faster, for high-end GPUs, very large download)", "flux2_full", 20.0),
];
pub fn is_flux2(key: &str) -> bool {
    FLUX2_CHOICES.iter().any(|c| c.1 == key)
}
/// Reduced VRAM requirement of the FLUX.2 tiers with 'Use minimum memory'.
pub const FLUX2_MIN_MEM_VRAM: &[(&str, f64)] = &[("flux2", 6.0), ("flux2_full", 8.0)];
pub const FLUX2_LICENSE_URL: &str = "https://huggingface.co/black-forest-labs/FLUX.2-dev/blob/main/LICENSE.md";
/// GB: (normal VRAM, Minimal-VRAM VRAM, Minimal-VRAM system RAM, normal system RAM).
pub const MODEL_MEMORY: &[(&str, [f64; 4])] = &[
    ("sd15", [4.0, 1.0, 8.0, 6.0]),
    ("sdxl", [8.0, 2.0, 16.0, 10.0]),
    ("flux2", [16.0, 6.0, 24.0, 16.0]),
    ("flux2_full", [40.0, 8.0, 32.0, 24.0]),
];
/// Roughly what each model loads from disk.
pub const MODEL_BYTES: &[(&str, f64)] = &[("sdxl", 7.4e9), ("sd15", 2.6e9), ("flux2", 16e9), ("flux2_full", 32e9)];
/// (start-up s, s per icon) until this PC has its own measurements.
pub const TIMING_DEFAULTS: &[(&str, (f64, f64))] = &[
    ("local:sdxl", (25.0, 15.0)), ("local:sdxl+poses", (45.0, 29.0)), ("local:sd15", (12.0, 6.0)),
    ("local:sd15+poses", (15.0, 8.0)), ("local:flux2", (60.0, 75.0)), ("local:flux2_full", (90.0, 110.0)),
    ("google", (5.0, 20.0)),
];
/// Minimum system RAM per model (GB).
pub const MIN_RAM_GB: &[(&str, f64)] = &[("sd15", 8.0), ("sdxl", 16.0), ("flux2", 32.0), ("flux2_full", 32.0)];

fn lookup<T: Copy>(table: &[(&str, T)], key: &str) -> Option<T> {
    table.iter().find(|(k, _)| *k == key).map(|x| x.1)
}

/// v2 `memory_estimate(model, min_mem)`: (VRAM GB, system RAM GB).
pub fn memory_estimate(model: &str, min_mem: bool) -> (f64, f64) {
    let [nv, mv, mr, nr] = lookup(MODEL_MEMORY, model).unwrap_or([6.0, 2.0, 12.0, 8.0]);
    if min_mem { (mv, mr) } else { (nv, nr) }
}

/// v2 `memory_note(model, min_mem)`.
pub fn memory_note(model: &str, min_mem: bool) -> String {
    let (vram, ram) = memory_estimate(model, min_mem);
    if min_mem {
        format!("~{} GB VRAM + ~{} GB system RAM (offloaded)", py::fmt_g(vram), py::fmt_g(ram))
    } else {
        format!("~{} GB VRAM", py::fmt_g(vram))
    }
}

/// v2 `format_size(n)`.
pub fn format_size(n: f64) -> String {
    let gb = 1024f64.powi(3);
    if n >= gb {
        format!("{} GB", py::fixed(n / gb, 1))
    } else {
        format!("{} MB", py::fixed(n / 1024f64.powi(2), 0))
    }
}

/// v2 `format_eta(seconds)`.
pub fn format_eta(seconds: Option<f64>) -> String {
    match seconds {
        None => "an unknown time".into(),
        Some(s) if s < 60.0 => "under a minute".into(),
        Some(s) if s < 3600.0 => format!("about {} min", py::round_int(s / 60.0)),
        Some(s) => format!("about {} hours", py::fixed(s / 3600.0, 1)),
    }
}

/// v2 `format_countdown(seconds)`: "3 min 12 s left" style.
pub fn format_countdown(seconds: f64) -> String {
    let s = py::round_int(seconds).max(0);
    if s >= 3600 {
        format!("{} h {} min left", s / 3600, s % 3600 / 60)
    } else if s >= 60 {
        format!("{} min {:02} s left", s / 60, s % 60)
    } else {
        format!("{s} s left")
    }
}

/// v2 `download_text` (= `widgets.download_progress_text`):
/// "label: 1.2 GB of 2.5 GB, 18.0 MB/s, 1 min 10 s left".
pub fn download_text(label: &str, done: f64, total: f64, speed: Option<f64>) -> String {
    fn size(n: f64) -> String {
        if n >= 1024f64.powi(3) {
            format!("{} GB", py::fixed(n / 1024f64.powi(3), 1))
        } else if n >= 1024f64.powi(2) {
            format!("{} MB", py::fixed(n / 1024f64.powi(2), 1))
        } else {
            format!("{} KB", py::fixed(n / 1024.0, 0))
        }
    }
    let mut text = format!("{label}: {}", size(done));
    if total != 0.0 {
        text += &format!(" of {}", size(total));
    }
    if let Some(speed) = speed.filter(|s| *s != 0.0) {
        text += &format!(", {}/s", size(speed));
        if total != 0.0 && total > done {
            let left = py::round_int((total - done) / speed);
            if left >= 60 {
                text += &format!(", {} min {:02} s left", left.div_euclid(60), left.rem_euclid(60));
            } else {
                text += &format!(", {left} s left");
            }
        }
    }
    text
}

/// v2 `timing_key(job, cloud)`.
pub fn timing_key(job: &Map<String, Value>, cloud: bool) -> String {
    if cloud {
        return "google".into();
    }
    let model = match job.get("model") {
        None => "sd15".to_string(),
        Some(v) => py::str_of(v),
    };
    let mut key = format!("local:{model}");
    if py::truthy(job.get("pose_variation")) && (model == "sd15" || model == "sdxl") {
        key += "+poses";
    }
    if py::truthy(job.get("min_memory")) {
        key += "+minmem";
    }
    key
}

/// v2 `_timed(total, net, disk, unpack)`: (download s, install s), both `None`
/// without a measured connection.
pub fn timed(total: f64, net: Option<f64>, disk: f64, unpack: f64) -> (Option<f64>, Option<f64>) {
    let Some(net) = net.filter(|n| *n != 0.0) else { return (None, None) };
    let download = (total / net).max(if disk != 0.0 { total / disk } else { 0.0 });
    let install = if disk != 0.0 && unpack > 1.0 { total * unpack / disk } else { 0.0 };
    (Some(download), Some(install))
}

/// An estimate dict of v2 (`estimate_install` / `estimate_models` / `estimate_flux2`).
/// `install_eta` is only set by the install estimate; `load_eta` only by the models ones.
#[derive(Debug, Clone, PartialEq)]
pub struct Estimate {
    pub total: f64,
    pub speed: Option<f64>,
    pub disk: f64,
    pub parts: Vec<(String, f64)>,
    pub download_eta: Option<f64>,
    pub install_eta: Option<f64>,
    pub load_eta: Option<f64>,
    pub eta: Option<f64>,
    pub disk_use: f64,
}

/// One row of [`check_requirements`]: (what, needed, this PC, status).
#[derive(Debug, Clone, PartialEq)]
pub struct Requirement {
    pub what: String,
    pub needed: String,
    pub this_pc: String,
    /// "ok", "warn" or "fail".
    pub status: &'static str,
}

/// Facts [`check_requirements_with`] needs.
pub struct ReqFacts {
    pub system: String,
    pub machine: String,
    pub vram_gb: f64,
    pub ram_gb: f64,
    pub free_disk: f64,
}

/// v2 `check_requirements(kind, est, model, poses, min_mem)` on given facts.
/// `kind` is "install" or "models". A 'fail' blocks the download, 'warn' allows it.
pub fn check_requirements_with(f: &ReqFacts, kind: &str, est: Option<&Estimate>, model: &str, poses: bool, min_mem: bool) -> Vec<Requirement> {
    let row = |what: &str, needed: String, this_pc: String, status| Requirement { what: what.into(), needed, this_pc, status };
    let mut rows = Vec::new();
    let sys_text = format!("{} {}", f.system, f.machine);
    let supported = python_triple_for(&f.system, &f.machine).is_some();
    rows.push(row("Supported system", "Windows, macOS or Linux (64-bit)".into(), sys_text, if supported { "ok" } else { "fail" }));
    let size = est.map(|e| e.total).unwrap_or(0.0);
    if size != 0.0 {
        let unpack = if kind == "install" { UNPACK_FACTOR_INSTALL } else { UNPACK_FACTOR_MODELS };
        let need = size * unpack + DISK_MARGIN;
        rows.push(row("Free disk space", format_size(need), format_size(f.free_disk), if f.free_disk >= need { "ok" } else { "fail" }));
    }
    let apple = f.system == "Darwin" && matches!(f.machine.to_lowercase().as_str(), "arm64" | "aarch64");
    let (want_vram, want_ram_model) = if lookup(MODEL_MEMORY, model).is_some() {
        let (v, r) = memory_estimate(model, min_mem);
        (v, Some(r))
    } else {
        (if model == "sdxl" { 7.5 } else { 4.0 }, None)
    };
    if apple {
        rows.push(row("Graphics (GPU)", "NVIDIA or Apple Silicon".into(), "Apple Silicon".into(), "ok"));
    } else if f.vram_gb != 0.0 {
        let note = if f.vram_gb >= want_vram { "ok" } else { "warn" };
        let gpu_note = if min_mem {
            " (Minimal VRAM mode, offloaded to RAM)"
        } else if poses && model == "sdxl" {
            " (poses use a memory-saving mode)"
        } else {
            ""
        };
        rows.push(row(
            "GPU memory",
            format!("{} GB recommended{gpu_note}", py::fmt_g(want_vram)),
            format!("{} GB (NVIDIA)", py::fixed(f.vram_gb, 0)),
            note,
        ));
    } else {
        rows.push(row("Graphics (GPU)", "NVIDIA or Apple Silicon recommended".into(), "none found: several minutes per icon".into(), "warn"));
    }
    if f.ram_gb != 0.0 {
        let want_ram = want_ram_model.unwrap_or_else(|| lookup(MIN_RAM_GB, model).unwrap_or(8.0));
        rows.push(row(
            "System memory",
            format!("{} GB recommended{}", py::fmt_g(want_ram), if min_mem { " (Minimal VRAM mode)" } else { "" }),
            format!("{} GB", py::fixed(f.ram_gb, 0)),
            if f.ram_gb >= want_ram * 0.9 { "ok" } else { "warn" },
        ));
    }
    rows
}

fn agent(timeout: Duration) -> ureq::Agent {
    ureq::Agent::config_builder()
        .timeout_connect(Some(timeout))
        .timeout_recv_response(Some(timeout))
        .build()
        .into()
}

fn agent_lenient(timeout: Duration) -> ureq::Agent {
    ureq::Agent::config_builder()
        .timeout_connect(Some(timeout))
        .timeout_recv_response(Some(timeout))
        .http_status_as_error(false)
        .build()
        .into()
}

fn get_text(url: &str, timeout: Duration) -> Result<String, String> {
    agent_lenient(timeout)
        .get(url)
        .call()
        .map_err(|e| e.to_string())?
        .body_mut()
        .with_config()
        .limit(64 * 1024 * 1024)
        .read_to_string()
        .map_err(|e| e.to_string())
}

/// v2 `measure_speed(url)`: bytes/s from a short ranged download (first second
/// ignored), `Ok(None)` when nothing arrived.
pub fn measure_speed(url: &str) -> Result<Option<f64>, String> {
    let start = Instant::now();
    let mut got: u64 = 0;
    let mut warm: Option<(f64, u64)> = None;
    let mut resp = agent(Duration::from_secs(15))
        .get(url)
        .header("Range", &format!("bytes=0-{}", SPEED_TEST_BYTES - 1))
        .call()
        .map_err(|e| e.to_string())?;
    let mut reader = resp.body_mut().with_config().limit(u64::MAX).reader();
    let mut buf = vec![0u8; 1 << 16];
    loop {
        let n = reader.read(&mut buf).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        got += n as u64;
        let now = start.elapsed().as_secs_f64();
        if warm.is_none() && now >= SPEED_WARMUP {
            warm = Some((now, got));
        }
        if got >= SPEED_TEST_BYTES || now > SPEED_TEST_SECONDS {
            break;
        }
    }
    let elapsed = start.elapsed().as_secs_f64();
    if let Some((t, g)) = warm {
        if elapsed > t {
            return Ok(Some((got - g) as f64 / (elapsed - t)));
        }
    }
    Ok((got > 0 && elapsed > 0.0).then(|| got as f64 / elapsed))
}

/// v2 `_version_key(name)`: the `torch-X.Y.Z` version as integers.
pub fn version_key(name: &str) -> Result<Vec<i64>, String> {
    let re = regex::Regex::new(r"torch-([0-9.]+)").expect("valid regex");
    match re.captures(name) {
        None => Ok(Vec::new()),
        Some(c) => c[1].trim_matches('.').split('.').map(|x| x.parse::<i64>().map_err(|e| format!("ValueError: {e}"))).collect(),
    }
}

/// The wheel choice of v2 `_torch_wheel_url` from an index page's HTML.
pub fn pick_torch_wheel(page: &str, index: &str, system: &str, machine: &str) -> Result<Option<String>, String> {
    let m = machine.to_lowercase();
    let plat = if system == "Windows" { "win_amd64" } else if m == "x86_64" || m == "amd64" { "x86_64" } else { "aarch64" };
    let re = regex::Regex::new(r#"href="([^"]+?\.whl)"#).expect("valid regex");
    let names: Vec<&str> = re
        .captures_iter(page)
        .map(|c| c.get(1).unwrap().as_str())
        .filter(|h| h.contains("cp312-cp312") && h.contains(plat) && (system != "Linux" || h.contains("linux")))
        .collect();
    let mut best: Option<(&str, Vec<i64>)> = None;
    for n in names {
        let k = version_key(n)?;
        if best.as_ref().is_none_or(|(_, bk)| k > *bk) {
            best = Some((n, k));
        }
    }
    let Some((best, _)) = best else { return Ok(None) };
    let base = format!("{index}/torch/");
    let href = best.split('#').next().unwrap_or("");
    Ok(Some(url_join(&base, href)))
}

/// `urllib.parse.urljoin` for the hrefs package indexes use.
pub fn url_join(base: &str, href: &str) -> String {
    match url::Url::parse(base).and_then(|b| b.join(href)) {
        Ok(u) => u.to_string(),
        Err(_) => format!("{base}{href}"),
    }
}

/// v2 `_torch_wheel_url()`: the PyTorch wheel pip will download for this machine.
pub fn torch_wheel_url() -> Result<Option<String>, String> {
    let cmds = pip_install_commands();
    let Some(torch_cmd) = cmds.iter().find(|c| c.iter().any(|a| a.starts_with("torch"))) else { return Ok(None) };
    let spec = torch_cmd.iter().find(|a| a.starts_with("torch")).cloned().unwrap_or_default();
    let (sys, m) = (system(), machine());
    if let Some(i) = torch_cmd.iter().position(|a| a == "--index-url") {
        let index = &torch_cmd[i + 1];
        let page = get_text(&format!("{index}/torch/"), Duration::from_secs(15))?;
        return pick_torch_wheel(&page, index, sys, &m);
    }
    let version = spec.split_once("==").map(|x| x.1.to_string());
    let url = format!("https://pypi.org/pypi/torch/{}json", version.map(|v| format!("{v}/")).unwrap_or_default());
    let info: Value = serde_json::from_str(&get_text(&url, Duration::from_secs(15))?).map_err(|e| format!("JSONDecodeError: {e}"))?;
    let arch = if matches!(m.to_lowercase().as_str(), "arm64" | "aarch64") { "arm64" } else { "x86_64" };
    for f in info.get("urls").and_then(Value::as_array).into_iter().flatten() {
        let name = f["filename"].as_str().unwrap_or("");
        if name.contains("cp312") && name.contains("macosx") && name.contains(arch) {
            return Ok(f["url"].as_str().map(str::to_string));
        }
    }
    Ok(None)
}

/// `requests.post(url, data={'paths': [...]})` + `raise_for_status()` + `.json()`.
fn paths_info(repo: &str, paths: &[String], token: Option<&str>, timeout: Duration) -> Result<Value, String> {
    let url = format!("https://huggingface.co/api/models/{repo}/paths-info/main");
    let mut req = agent(timeout).post(&url);
    if let Some(t) = token {
        req = req.header("Authorization", &format!("Bearer {t}"));
    }
    let form: Vec<(&str, &str)> = paths.iter().map(|p| ("paths", p.as_str())).collect();
    let text = req
        .send_form(form)
        .map_err(|e| e.to_string())?
        .body_mut()
        .with_config()
        .limit(64 * 1024 * 1024)
        .read_to_string()
        .map_err(|e| e.to_string())?;
    serde_json::from_str(&text).map_err(|e| e.to_string())
}

fn item_size(item: &Value) -> f64 {
    let lfs = item.get("lfs").map(|l| py::num_or0(l.get("size"))).unwrap_or(0.0);
    if lfs != 0.0 { lfs } else { py::num_or0(item.get("size")) }
}

impl IconAi {
    /// v2 `_model_cached(repo, path)`: the file is in any snapshot of the HF cache.
    pub fn model_cached(&self, repo: &str, path: &str) -> bool {
        let snapshots = self.models_dir().join("hub").join(format!("models--{}", repo.replace('/', "--"))).join("snapshots");
        let Ok(rd) = std::fs::read_dir(&snapshots) else { return false };
        rd.flatten().any(|s| s.path().join(path).is_file())
    }

    /// v2 `_flux2_present(variant)`: the variant's key files are cached.
    pub fn flux2_present(&self, variant: &str) -> bool {
        flux2_key_files(variant).iter().all(|(r, p)| self.model_cached(r, p))
    }

    /// v2 `_flux2_missing_size(variant)`: bytes still to download (0 when cached).
    /// Per-repo API failures count as 0, as in v2.
    pub fn flux2_missing_size(&self, variant: &str) -> f64 {
        if self.flux2_present(variant) {
            return 0.0;
        }
        let token = self.settings().str("hf_token").unwrap_or("").trim().to_string();
        let token = (!token.is_empty()).then_some(token);
        let mut total = 0.0;
        for (repo, patterns) in flux2_files(variant) {
            let r = (|| -> Result<f64, String> {
                let mut req = agent(Duration::from_secs(20)).get(&format!("https://huggingface.co/api/models/{repo}"));
                if let Some(t) = &token {
                    req = req.header("Authorization", &format!("Bearer {t}"));
                }
                let info: Value = serde_json::from_str(
                    &req.call().map_err(|e| e.to_string())?.body_mut().read_to_string().map_err(|e| e.to_string())?,
                )
                .map_err(|e| e.to_string())?;
                let files: Vec<String> = info
                    .get("siblings")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter_map(|s| s["rfilename"].as_str())
                    .filter(|f| patterns.iter().any(|p| py::fnmatch(f, p)))
                    .map(str::to_string)
                    .collect();
                let resp = paths_info(&repo, &files, token.as_deref(), Duration::from_secs(30))?;
                let mut sum = 0.0;
                for item in resp.as_array().ok_or("not a list")? {
                    sum += item_size(item);
                }
                Ok(sum)
            })();
            total += r.unwrap_or(0.0);
        }
        total
    }

    /// v2 `estimate_flux2(variant)`: download size/time (plus bitsandbytes for the
    /// 4-bit build), `Ok(None)` when nothing is needed.
    pub fn estimate_flux2(&self, variant: &str) -> Result<Option<Estimate>, String> {
        let mut size = self.flux2_missing_size(variant);
        let need_bnb = variant == "flux2" && !self.package_installed("bitsandbytes");
        if size == 0.0 && !need_bnb {
            return Ok(None);
        }
        if need_bnb {
            size += 300.0 * 1024.0 * 1024.0;
        }
        let speed = measure_speed("https://huggingface.co/black-forest-labs/FLUX.2-dev/resolve/main/model_index.json")?;
        let disk = self.disk_speed();
        let (download, _) = timed(size, speed, disk, 1.0);
        let load = (disk != 0.0).then(|| size / disk);
        Ok(Some(Estimate {
            total: size,
            speed,
            disk,
            parts: vec![("FLUX.2 model".into(), size)],
            download_eta: download,
            install_eta: None,
            load_eta: load,
            eta: download.map(|d| d + load.unwrap_or(0.0)),
            disk_use: size * UNPACK_FACTOR_MODELS,
        }))
    }

    /// v2 `estimate_models(segmenter, poses, model, skip_base)`: missing model
    /// download size and time, `Ok(None)` when everything is cached.
    pub fn estimate_models(&self, segmenter: bool, poses: bool, model: &str, skip_base: bool) -> Result<Option<Estimate>, String> {
        if is_flux2(model) {
            return self.estimate_flux2(model);
        }
        let files = model_files(self.runtime_uses_half_precision(), segmenter, poses, model, skip_base);
        let missing: Vec<(String, String)> = files.into_iter().filter(|(r, p)| !self.model_cached(r, p)).collect();
        if missing.is_empty() {
            return Ok(None);
        }
        let mut repos: Vec<&String> = Vec::new();
        for (r, _) in &missing {
            if !repos.contains(&r) {
                repos.push(r);
            }
        }
        let mut sizes: Vec<((String, String), f64)> = Vec::new();
        for repo in repos {
            let paths: Vec<String> = missing.iter().filter(|(r, _)| r == repo).map(|(_, p)| p.clone()).collect();
            let resp = paths_info(repo, &paths, None, Duration::from_secs(20))?;
            for item in resp.as_array().ok_or("TypeError: paths-info is not a list")? {
                let key = (repo.clone(), item["path"].as_str().unwrap_or("").to_string());
                let size = item_size(item);
                match sizes.iter_mut().find(|(k, _)| *k == key) {
                    Some(e) => e.1 = size,
                    None => sizes.push((key, size)),
                }
            }
        }
        let total: f64 = sizes.iter().map(|x| x.1).sum();
        let mut biggest: Option<&((String, String), f64)> = None;
        for s in &sizes {
            if biggest.is_none_or(|b| s.1 > b.1) {
                biggest = Some(s);
            }
        }
        let Some(((brepo, bpath), _)) = biggest else {
            return Err("ValueError: max() arg is an empty sequence".into());
        };
        let speed = measure_speed(&format!("https://huggingface.co/{brepo}/resolve/main/{bpath}"))?;
        let disk = self.disk_speed();
        let (download, _) = timed(total, speed, disk, 1.0);
        let load = (disk != 0.0).then(|| total / disk);
        Ok(Some(Estimate {
            total,
            speed,
            disk,
            parts: vec![("AI models".into(), total)],
            download_eta: download,
            install_eta: None,
            load_eta: load,
            eta: download.map(|d| d + load.unwrap_or(0.0)),
            disk_use: total * UNPACK_FACTOR_MODELS,
        }))
    }

    /// v2 `estimate_install()`: size/time to install (or update) the runtime, the
    /// speed measured from the PyTorch server itself.
    pub fn estimate_install(&self) -> Result<Estimate, String> {
        let py_url = python_url(python_triple().as_deref());
        let (parts, speed_url) = if self.runtime_needs_update() {
            (vec![("New libraries".to_string(), UPDATE_ESTIMATE)], py_url)
        } else {
            let url = torch_wheel_url()?;
            let size = match &url {
                Some(u) => agent_lenient(Duration::from_secs(15))
                    .head(u)
                    .call()
                    .map_err(|e| e.to_string())?
                    .headers()
                    .get("content-length")
                    .and_then(|v| v.to_str().ok())
                    .and_then(|v| v.parse::<f64>().ok())
                    .unwrap_or(0.0),
                None => 0.0,
            };
            let lib_label = if USE_APP_PYTHON { "AI libraries (reusing this app's Python)" } else { "Python and libraries" };
            let torch = if size != 0.0 { size } else { 2.0 * 1024f64.powi(3) };
            (vec![("PyTorch".to_string(), torch), (lib_label.to_string(), LIBRARIES_ESTIMATE)], url.unwrap_or(py_url))
        };
        let total: f64 = parts.iter().map(|p| p.1).sum();
        let speed = measure_speed(&speed_url)?;
        let disk = self.disk_speed();
        let (download, install) = timed(total, speed, disk, UNPACK_FACTOR_INSTALL);
        Ok(Estimate {
            total,
            speed,
            disk,
            parts,
            download_eta: download,
            install_eta: install,
            load_eta: None,
            eta: download.map(|d| d + install.unwrap_or(0.0)),
            disk_use: total * UNPACK_FACTOR_INSTALL,
        })
    }

    /// v2 `check_requirements(kind, est, model, poses, min_mem)` for this PC.
    pub fn check_requirements(&self, kind: &str, est: Option<&Estimate>, model: &str, poses: bool, min_mem: bool) -> Vec<Requirement> {
        let facts = ReqFacts {
            system: system().to_string(),
            machine: machine(),
            vram_gb: super::hardware::gpu_vram_gb(),
            ram_gb: super::hardware::total_ram_gb(),
            free_disk: if est.is_some_and(|e| e.total != 0.0) { self.free_disk_bytes() as f64 } else { 0.0 },
        };
        check_requirements_with(&facts, kind, est, model, poses, min_mem)
    }

    // ------------------------------------------------------------- timing

    fn saved_timing(&self, key: &str, hw: &dyn HwSource) -> Option<(f64, f64)> {
        let s = self.settings();
        let saved = s.values.get("icon_ai_timing")?.as_object()?.get(key)?.as_array()?;
        if saved.len() >= 3 && saved[2].as_str() == Some(hardware_print(hw).as_str()) {
            return Some((py::number(saved.first())?, py::number(saved.get(1))?));
        }
        None
    }

    /// v2 `timing_measured(key)`: this PC (as it is now) finished a run with this setup.
    pub fn timing_measured(&self, key: &str) -> bool {
        self.saved_timing(key, &LiveHw).is_some()
    }

    /// v2 `load_timing(key)`: (start-up s, s per icon) for this PC.
    pub fn load_timing(&self, key: &str) -> (f64, f64) {
        self.load_timing_with(key, &LiveHw)
    }

    /// [`load_timing`](Self::load_timing) on given hardware.
    pub fn load_timing_with(&self, key: &str, hw: &dyn HwSource) -> (f64, f64) {
        if let Some(saved) = self.saved_timing(key, hw) {
            return saved;
        }
        let minmem = key.ends_with("+minmem");
        let base_key = if minmem { &key[..key.len() - "+minmem".len()] } else { key };
        let (mut startup, mut per_icon) = lookup(TIMING_DEFAULTS, base_key).unwrap_or((20.0, 18.0));
        if key.starts_with("local") {
            let vram = hw.vram_gb();
            let apple = hw.system() == "Darwin" && matches!(hw.machine().to_lowercase().as_str(), "arm64" | "aarch64");
            let speed = gpu_speed(hw).filter(|s| *s != 0.0);
            let factor = if apple {
                speed.unwrap_or(0.4)
            } else if vram == 0.0 {
                0.1
            } else {
                speed.unwrap_or(if vram >= 10.0 { 1.0 } else if vram >= 6.0 { 0.6 } else { 0.35 })
            };
            if let Some((ref_start, ref_per)) = self.dataset_reference(base_key, minmem, hw) {
                if let Some(s) = ref_start.filter(|s| *s != 0.0) {
                    startup = s;
                }
                per_icon = ref_per / factor;
                if apple {
                    startup *= 1.5;
                } else if vram == 0.0 {
                    startup *= 3.0;
                }
            } else {
                per_icon /= factor;
                if apple {
                    startup *= 1.5;
                } else if vram == 0.0 {
                    startup *= 3.0;
                } else if minmem {
                    per_icon *= MINMEM_SLOWDOWN;
                    startup *= 1.2;
                } else if base_key.contains("sdxl") && !base_key.contains("+poses") && vram < 10.0 {
                    per_icon *= 1.8;
                }
            }
            let s = self.settings();
            if let Some(disk) = s.values.get("icon_ai_disk_speed").and_then(Value::as_array) {
                if !disk.is_empty() && py::truthy(disk.first()) {
                    let name = base_key.split(':').nth(1).unwrap_or("").split('+').next().unwrap_or("");
                    let model_bytes = lookup(MODEL_BYTES, name).unwrap_or(3e9) + if base_key.contains("+poses") { 3.4e9 } else { 0.0 };
                    if let Some(d) = py::number(disk.first()) {
                        startup = startup.max(10.0 + model_bytes / d);
                    }
                }
            }
        }
        (startup, per_icon)
    }

    /// v2 `save_timing(key, startup, per_icon)`: remember this run, blended with
    /// earlier runs on the same hardware.
    pub fn save_timing(&self, key: &str, startup: f64, per_icon: f64) {
        self.save_timing_with(key, startup, per_icon, &LiveHw)
    }

    pub fn save_timing_with(&self, key: &str, mut startup: f64, mut per_icon: f64, hw: &dyn HwSource) {
        let old = self.saved_timing(key, hw);
        let mut s = self.settings();
        if !s.values.get("icon_ai_timing").is_some_and(Value::is_object) {
            if s.contains("icon_ai_timing") {
                return; // v2 would fail on a non-dict value (TypeError)
            }
            s.set("icon_ai_timing", Value::Object(Map::new()));
        }
        if let Some((os, op)) = old {
            startup = (os + startup) / 2.0;
            per_icon = (op + per_icon) / 2.0;
        }
        if let Some(Value::Object(t)) = s.values.get_mut("icon_ai_timing") {
            t.insert(key.into(), json!([py::round(startup, 1), py::round(per_icon, 1), hardware_print(hw)]));
        }
        let _ = s.save();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::iconai::hardware::tests::{rtx4070, FakeHw};

    #[test]
    fn formatting_like_v2() {
        assert_eq!(format_size(1.5 * 1024f64.powi(3)), "1.5 GB");
        assert_eq!(format_size(180.0 * 1024.0 * 1024.0), "180 MB");
        assert_eq!(format_eta(None), "an unknown time");
        assert_eq!(format_eta(Some(59.0)), "under a minute");
        assert_eq!(format_eta(Some(150.0)), "about 2 min"); // round(2.5) = 2
        assert_eq!(format_eta(Some(5400.0)), "about 1.5 hours");
        assert_eq!(format_countdown(192.0), "3 min 12 s left");
        assert_eq!(format_countdown(3725.0), "1 h 2 min left");
        assert_eq!(format_countdown(-3.0), "0 s left");
        assert_eq!(format_countdown(42.4), "42 s left");
        let mb = 1024.0 * 1024.0;
        assert_eq!(download_text("Models", 512.0, 0.0, None), "Models: 0 KB");
        assert_eq!(
            download_text("Downloading Python runtime", 10.0 * mb, 100.0 * mb, Some(2.0 * mb)),
            "Downloading Python runtime: 10.0 MB of 100.0 MB, 2.0 MB/s, 45 s left"
        );
        assert_eq!(download_text("X", 0.0, 1024f64.powi(3) * 2.0, Some(mb)), "X: 0 KB of 2.0 GB, 1.0 MB/s, 34 min 08 s left");
        assert_eq!(memory_note("sdxl", false), "~8 GB VRAM");
        assert_eq!(memory_note("sdxl", true), "~2 GB VRAM + ~16 GB system RAM (offloaded)");
        assert_eq!(memory_estimate("other", false), (6.0, 8.0));
    }

    #[test]
    fn timing_keys() {
        let job = |v: Value| v.as_object().unwrap().clone();
        assert_eq!(timing_key(&job(json!({})), true), "google");
        assert_eq!(timing_key(&job(json!({})), false), "local:sd15");
        assert_eq!(timing_key(&job(json!({"model": "sdxl", "pose_variation": true, "min_memory": true})), false), "local:sdxl+poses+minmem");
        assert_eq!(timing_key(&job(json!({"model": "flux2", "pose_variation": true})), false), "local:flux2");
    }

    #[test]
    fn timed_and_requirements() {
        assert_eq!(timed(100.0, None, 10.0, 2.2), (None, None));
        assert_eq!(timed(100.0, Some(50.0), 10.0, 1.0), (Some(10.0), Some(0.0)));
        let (d, i) = timed(100.0, Some(50.0), 0.0, 2.2);
        assert_eq!((d, i), (Some(2.0), Some(0.0)));
        let f = ReqFacts { system: "Windows".into(), machine: "AMD64".into(), vram_gb: 6.0, ram_gb: 15.6, free_disk: 1e9 };
        let est = Estimate { total: 2e9, speed: None, disk: 0.0, parts: vec![], download_eta: None, install_eta: None, load_eta: None, eta: None, disk_use: 0.0 };
        let rows = check_requirements_with(&f, "install", Some(&est), "sdxl", true, false);
        assert_eq!(rows[0], Requirement { what: "Supported system".into(), needed: "Windows, macOS or Linux (64-bit)".into(), this_pc: "Windows AMD64".into(), status: "ok" });
        assert_eq!(rows[1].needed, "5.1 GB");
        assert_eq!(rows[1].status, "fail");
        assert_eq!(rows[2].needed, "8 GB recommended (poses use a memory-saving mode)");
        assert_eq!(rows[2].this_pc, "6 GB (NVIDIA)");
        assert_eq!(rows[2].status, "warn");
        assert_eq!(rows[3].needed, "10 GB recommended");
        assert_eq!(rows[3].this_pc, "16 GB");
        assert_eq!(rows[3].status, "ok");
        let f = ReqFacts { system: "Linux".into(), machine: "x86_64".into(), vram_gb: 0.0, ram_gb: 0.0, free_disk: 0.0 };
        let rows = check_requirements_with(&f, "models", None, "unknown", false, true);
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[1].this_pc, "none found: several minutes per icon");
    }

    #[test]
    fn torch_wheel_choice() {
        let page = r#"<a href="/whl/cpu/torch-2.8.0%2Bcpu-cp312-cp312-win_amd64.whl#sha256=aa">x</a>
            <a href="/whl/cpu/torch-2.10.0%2Bcpu-cp312-cp312-win_amd64.whl#sha256=bb">x</a>
            <a href="/whl/cpu/torch-2.11.0%2Bcpu-cp311-cp311-win_amd64.whl">x</a>
            <a href="/whl/cpu/torch-2.12.0%2Bcpu-cp312-cp312-manylinux_2_28_x86_64.whl">x</a>"#;
        assert_eq!(
            pick_torch_wheel(page, "https://download.pytorch.org/whl/cpu", "Windows", "AMD64").unwrap().as_deref(),
            Some("https://download.pytorch.org/whl/cpu/torch-2.10.0%2Bcpu-cp312-cp312-win_amd64.whl")
        );
        assert_eq!(
            pick_torch_wheel(page, "https://download.pytorch.org/whl/cpu", "Linux", "x86_64").unwrap().as_deref(),
            Some("https://download.pytorch.org/whl/cpu/torch-2.12.0%2Bcpu-cp312-cp312-manylinux_2_28_x86_64.whl")
        );
        assert_eq!(pick_torch_wheel(page, "https://x", "Linux", "aarch64").unwrap(), None);
    }

    #[test]
    fn load_and_save_timing() {
        let ai = crate::iconai::tests::temp_ai("timing");
        let hw = rtx4070();
        // Defaults scaled by the GPU factor (4070 = 1.0) — no data anywhere.
        assert_eq!(ai.load_timing_with("local:sdxl", &hw), (25.0, 15.0));
        assert_eq!(ai.load_timing_with("local:sdxl+minmem", &hw), (30.0, 60.0));
        assert_eq!(ai.load_timing_with("google", &hw), (5.0, 20.0));
        let small = FakeHw { gpu: "Some GPU", vram: 8.0, ..rtx4070() };
        let (s, p) = ai.load_timing_with("local:sdxl", &small);
        assert_eq!(s, 25.0);
        assert!((p - 15.0 / 0.6 * 1.8).abs() < 1e-9);
        let cpu = FakeHw { gpu: "", vram: 0.0, ..rtx4070() };
        assert_eq!(ai.load_timing_with("local:sd15", &cpu), (36.0, 60.0));
        // A slow disk raises start-up.
        let mut s = ai.settings();
        s.set("icon_ai_disk_speed", json!([100e6, 1.0]));
        s.save().unwrap();
        assert_eq!(ai.load_timing_with("local:sd15", &hw).0, 10.0 + 2.6e9 / 100e6);
        // Measured timings on this hardware win, blended on later runs.
        ai.save_timing_with("local:sd15", 20.0, 5.0, &hw);
        assert_eq!(ai.load_timing_with("local:sd15", &hw), (20.0, 5.0));
        ai.save_timing_with("local:sd15", 30.0, 6.0, &hw);
        assert_eq!(ai.load_timing_with("local:sd15", &hw), (25.0, 5.5));
        let saved = &ai.settings().values["icon_ai_timing"]["local:sd15"];
        assert_eq!(saved, &json!([25.0, 5.5, "NVIDIA GeForce RTX 4070|12|Windows|AMD64"]));
        // Different hardware: stale.
        assert_ne!(ai.load_timing_with("local:sd15", &small), (25.0, 5.5));
    }
}

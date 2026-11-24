//! Hardware probes (v2 `gpu_vram_gb`, `gpu_name`, `gpu_detail`, `_cpu_info`,
//! `ram_speed_mhz`, `total_ram_gb`, `free_disk_bytes`, `hardware_print`,
//! `hardware_profile`, `gpu_speed`, `disk_speed`). Each probe is best-effort, never
//! fails, and (like v2) is looked up once per app session.
//!
//! The estimate/calibration code reads hardware through [`HwSource`] so tests can
//! supply a fake machine; [`LiveHw`] is the real one.

use super::proc::{run_capture, which};
use super::{machine, py, system, IconAi};
use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use std::time::Duration;

/// Relative Stable Diffusion speed of common GPUs (RTX 4070 = 1.0). Longer names
/// first, so "4070 ti super" wins over "4070". (v2 `GPU_SPEED`.)
pub const GPU_SPEED: &[(&str, f64)] = &[
    ("5090", 3.0), ("5080", 1.9), ("5070 ti", 1.6), ("5070", 1.2), ("5060 ti", 0.85), ("5060", 0.7),
    ("4090", 2.2), ("4080", 1.6), ("4070 ti super", 1.35), ("4070 ti", 1.25), ("4070 super", 1.15), ("4070", 1.0),
    ("4060 ti", 0.75), ("4060", 0.6),
    ("3090 ti", 1.25), ("3090", 1.15), ("3080 ti", 1.1), ("3080", 1.0), ("3070 ti", 0.75), ("3070", 0.7),
    ("3060 ti", 0.62), ("3060", 0.5), ("3050", 0.33),
    ("2080 ti", 0.6), ("2080", 0.48), ("2070", 0.4), ("2060", 0.33),
    ("1080 ti", 0.3), ("1660", 0.2), ("1070", 0.22), ("1060", 0.16),
];
/// v2 `APPLE_SPEED` (dict order: m1, m2, m3, m4).
pub const APPLE_SPEED: &[(&str, f64)] = &[("m1", 0.15), ("m2", 0.2), ("m3", 0.25), ("m4", 0.3)];
/// v2 `APPLE_TIER` (dict order: ultra, max, pro).
pub const APPLE_TIER: &[(&str, f64)] = &[("ultra", 4.0), ("max", 2.5), ("pro", 1.6)];

pub const DISK_TEST_BYTES: usize = 128 * 1024 * 1024;
/// Re-measure the disk weekly.
pub const DISK_SPEED_MAX_AGE: f64 = 7.0 * 24.0 * 3600.0;

/// What the estimates and calibration need to know about the machine.
pub trait HwSource {
    fn system(&self) -> String;
    fn machine(&self) -> String;
    fn gpu_name(&self) -> String;
    fn vram_gb(&self) -> f64;
    /// v2 `hardware_profile()` (only fetched when a record needs it).
    fn profile(&self) -> Map<String, Value>;
}

/// The real machine (cached probes).
pub struct LiveHw;

impl HwSource for LiveHw {
    fn system(&self) -> String {
        system().to_string()
    }
    fn machine(&self) -> String {
        machine()
    }
    fn gpu_name(&self) -> String {
        gpu_name()
    }
    fn vram_gb(&self) -> f64 {
        gpu_vram_gb()
    }
    fn profile(&self) -> Map<String, Value> {
        hardware_profile()
    }
}

/// v2 `_nvidia_smi()`: `nvidia-smi` on PATH, else `%SystemRoot%\System32\nvidia-smi.exe`.
pub fn nvidia_smi() -> Option<PathBuf> {
    if let Some(p) = which("nvidia-smi") {
        return Some(p);
    }
    if cfg!(windows) {
        let root = std::env::var("SystemRoot").unwrap_or_else(|_| r"C:\Windows".into());
        let cand = Path::new(&root).join("System32").join("nvidia-smi.exe");
        if cand.is_file() {
            return Some(cand);
        }
    }
    None
}

/// v2 `gpu_vram_gb()`: total VRAM of the largest NVIDIA GPU in GB, 0 if unknown.
pub fn gpu_vram_gb() -> f64 {
    static V: OnceLock<f64> = OnceLock::new();
    *V.get_or_init(|| {
        let Some(smi) = nvidia_smi() else { return 0.0 };
        let Some(out) = run_capture(&smi, &["--query-gpu=memory.total", "--format=csv,noheader,nounits"], Duration::from_secs(15), true) else {
            return 0.0;
        };
        let vals: Option<Vec<f64>> = out.stdout.split_whitespace().map(|v| v.parse::<f64>().ok()).collect();
        match vals {
            Some(v) if !v.is_empty() => v.into_iter().fold(f64::MIN, f64::max) / 1024.0,
            _ => 0.0,
        }
    })
}

/// v2 `gpu_name()`: the NVIDIA card (or Apple chip via `machdep.cpu.brand_string`), "" if none.
pub fn gpu_name() -> String {
    static V: OnceLock<String> = OnceLock::new();
    V.get_or_init(|| {
        if system() == "Darwin" {
            return run_capture(Path::new("sysctl"), &["-n", "machdep.cpu.brand_string"], Duration::from_secs(10), false)
                .map(|o| o.stdout.trim().to_string())
                .unwrap_or_default();
        }
        if let Some(smi) = nvidia_smi() {
            return run_capture(&smi, &["--query-gpu=name", "--format=csv,noheader"], Duration::from_secs(15), true)
                .and_then(|o| o.stdout.trim().lines().next().map(str::to_string))
                .unwrap_or_default();
        }
        String::new()
    })
    .clone()
}

/// v2 `gpu_detail()`: (compute_cap, driver) of an NVIDIA card, "" if unknown.
pub fn gpu_detail() -> (String, String) {
    static V: OnceLock<(String, String)> = OnceLock::new();
    V.get_or_init(|| {
        let (mut cc, mut driver) = (String::new(), String::new());
        if let Some(smi) = nvidia_smi() {
            if let Some(o) = run_capture(&smi, &["--query-gpu=driver_version,compute_cap", "--format=csv,noheader"], Duration::from_secs(15), true) {
                if let Some(line) = o.stdout.trim().lines().next() {
                    let parts: Vec<&str> = line.split(',').collect();
                    driver = parts[0].trim().to_string();
                    if parts.len() > 1 {
                        cc = parts[1].trim().to_string();
                    }
                }
            }
        }
        (cc, driver)
    })
    .clone()
}

/// v2 `_cpu_info()`: (name, logical cores, base MHz).
#[derive(Debug, Clone, Default)]
pub struct CpuInfo {
    pub name: String,
    pub cores: i64,
    pub mhz: i64,
}

pub fn cpu_info() -> CpuInfo {
    static V: OnceLock<CpuInfo> = OnceLock::new();
    V.get_or_init(|| {
        let mut info = CpuInfo { name: String::new(), cores: cpu_count(), mhz: 0 };
        match system() {
            "Windows" => {
                #[cfg(windows)]
                {
                    let key = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0";
                    match win::reg_string(key, "ProcessorNameString") {
                        Some(name) => {
                            info.name = name.trim().to_string();
                            if let Some(mhz) = win::reg_dword(key, "~MHz") {
                                info.mhz = mhz as i64;
                            }
                        }
                        None => {}
                    }
                }
            }
            "Darwin" => {
                if let Some(o) = run_capture(Path::new("sysctl"), &["-n", "machdep.cpu.brand_string"], Duration::from_secs(10), false) {
                    info.name = o.stdout.trim().to_string();
                    if let Some(hz) = run_capture(Path::new("sysctl"), &["-n", "hw.cpufrequency_max"], Duration::from_secs(10), false) {
                        let hz = hz.stdout.trim().to_string();
                        if !hz.is_empty() && hz.chars().all(|c| c.is_ascii_digit()) {
                            info.mhz = hz.parse::<i64>().map(|v| v / 1_000_000).unwrap_or(0);
                        }
                    }
                }
            }
            _ => {
                if let Ok(text) = std::fs::read_to_string("/proc/cpuinfo") {
                    for line in text.lines() {
                        if line.starts_with("model name") && info.name.is_empty() {
                            info.name = line.split_once(':').map(|x| x.1.trim().to_string()).unwrap_or_default();
                        } else if line.starts_with("cpu MHz") && info.mhz == 0 {
                            match line.split_once(':').and_then(|x| x.1.trim().parse::<f64>().ok()) {
                                Some(v) => info.mhz = v.trunc() as i64,
                                None => break, // v2: ValueError ends the scan
                            }
                        }
                    }
                }
            }
        }
        info
    })
    .clone()
}

/// `os.cpu_count()`.
fn cpu_count() -> i64 {
    #[cfg(windows)]
    {
        let n = unsafe { windows_sys::Win32::System::Threading::GetActiveProcessorCount(0xffff) };
        if n > 0 {
            return n as i64;
        }
    }
    #[cfg(unix)]
    {
        let n = unsafe { libc::sysconf(libc::_SC_NPROCESSORS_ONLN) };
        if n > 0 {
            return n as i64;
        }
    }
    std::thread::available_parallelism().map(|n| n.get() as i64).unwrap_or(0)
}

/// v2 `ram_speed_mhz()`: fastest memory module's MHz (0 if unknown; Windows and macOS only).
pub fn ram_speed_mhz() -> i64 {
    static V: OnceLock<i64> = OnceLock::new();
    *V.get_or_init(|| match system() {
        "Windows" => run_capture(
            Path::new("powershell"),
            &["-NoProfile", "-Command", "(Get-CimInstance Win32_PhysicalMemory | Measure-Object -Property Speed -Maximum).Maximum"],
            Duration::from_secs(12),
            true,
        )
        .map(|o| o.stdout.trim().to_string())
        .filter(|s| !s.is_empty() && s.chars().all(|c| c.is_ascii_digit()))
        .and_then(|s| s.parse().ok())
        .unwrap_or(0),
        "Darwin" => run_capture(Path::new("system_profiler"), &["SPMemoryDataType"], Duration::from_secs(15), false)
            .and_then(|o| {
                regex::Regex::new(r"Speed:\s*([0-9]+)\s*MHz")
                    .ok()?
                    .captures(&o.stdout)
                    .and_then(|c| c[1].parse().ok())
            })
            .unwrap_or(0),
        _ => 0,
    })
}

/// v2 `total_ram_gb()`: installed memory in GB (0 if unknown). Not cached (v2 isn't).
pub fn total_ram_gb() -> f64 {
    #[cfg(windows)]
    {
        use windows_sys::Win32::System::SystemInformation::{GlobalMemoryStatusEx, MEMORYSTATUSEX};
        let mut st: MEMORYSTATUSEX = unsafe { std::mem::zeroed() };
        st.dwLength = std::mem::size_of::<MEMORYSTATUSEX>() as u32;
        unsafe { GlobalMemoryStatusEx(&mut st) };
        return st.ullTotalPhys as f64 / 1024f64.powi(3);
    }
    #[allow(unreachable_code)]
    if system() == "Darwin" {
        return run_capture(Path::new("sysctl"), &["-n", "hw.memsize"], Duration::from_secs(10), false)
            .and_then(|o| o.stdout.trim().parse::<i64>().ok())
            .map(|b| b as f64 / 1024f64.powi(3))
            .unwrap_or(0.0);
    }
    if let Ok(text) = std::fs::read_to_string("/proc/meminfo") {
        for line in text.lines() {
            if line.starts_with("MemTotal:") {
                return line.split_whitespace().nth(1).and_then(|v| v.parse::<i64>().ok()).map(|k| k as f64 / 1024f64.powi(2)).unwrap_or(0.0);
            }
        }
    }
    0.0
}

/// v2 `free_disk_bytes(path)`: free space on the drive `path` is on (walking up to
/// an existing folder; the home folder when none exists).
pub fn free_disk_bytes(path: &Path) -> u64 {
    let mut probe = Some(path.to_path_buf());
    while let Some(p) = &probe {
        if p.as_os_str().is_empty() || p.exists() {
            break;
        }
        probe = p.parent().map(Path::to_path_buf);
    }
    let probe = probe.filter(|p| !p.as_os_str().is_empty()).unwrap_or_else(|| {
        std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).map(PathBuf::from).unwrap_or_else(|| ".".into())
    });
    disk_free(&probe).unwrap_or(0)
}

#[cfg(windows)]
fn disk_free(p: &Path) -> Option<u64> {
    use std::os::windows::ffi::OsStrExt;
    let wide: Vec<u16> = p.as_os_str().encode_wide().chain(Some(0)).collect();
    let (mut avail, mut total, mut free) = (0u64, 0u64, 0u64);
    let ok = unsafe {
        windows_sys::Win32::Storage::FileSystem::GetDiskFreeSpaceExW(wide.as_ptr(), &mut avail, &mut total, &mut free)
    };
    (ok != 0).then_some(free)
}

#[cfg(unix)]
fn disk_free(p: &Path) -> Option<u64> {
    use std::os::unix::ffi::OsStrExt;
    let c = std::ffi::CString::new(p.as_os_str().as_bytes()).ok()?;
    let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
    if unsafe { libc::statvfs(c.as_ptr(), &mut st) } != 0 {
        return None;
    }
    Some(st.f_bavail as u64 * st.f_frsize as u64)
}

#[cfg(not(any(windows, unix)))]
fn disk_free(_p: &Path) -> Option<u64> {
    None
}

/// v2 `hardware_print()`: "gpu|round(vram)|system|machine" — saved timings only
/// count while this stays the same.
pub fn hardware_print(hw: &dyn HwSource) -> String {
    format!("{}|{}|{}|{}", hw.gpu_name(), py::round_int(hw.vram_gb()), hw.system(), hw.machine())
}

/// v2 `gpu_speed()`: GPU speed relative to an RTX 4070, `None` for an unlisted GPU.
pub fn gpu_speed(hw: &dyn HwSource) -> Option<f64> {
    let name = hw.gpu_name().to_lowercase();
    if hw.system() == "Darwin" {
        let base = APPLE_SPEED.iter().find(|(k, _)| name.contains(k))?.1;
        return Some(base * APPLE_TIER.iter().find(|(k, _)| name.contains(k)).map(|x| x.1).unwrap_or(1.0));
    }
    if name.is_empty() {
        return None;
    }
    GPU_SPEED.iter().find(|(k, _)| name.contains(k)).map(|x| x.1)
}

/// v2 `hardware_profile()`: the full machine context for a calibration record
/// (same keys and order as v2). `python` holds the AI runtime's Python version: v3
/// has no app-side Python (v2 stored the app interpreter's version, also 3.12.x).
pub fn hardware_profile() -> Map<String, Value> {
    static V: OnceLock<Map<String, Value>> = OnceLock::new();
    V.get_or_init(|| {
        let cpu = cpu_info();
        let (cc, driver) = gpu_detail();
        let speed = gpu_speed(&LiveHw);
        let v = json!({
            "gpu": gpu_name(), "vram_gb": py::round(gpu_vram_gb(), 1), "gpu_speed": speed,
            "compute_cap": cc, "driver": driver,
            "cpu": cpu.name, "cpu_cores": cpu.cores, "cpu_mhz": cpu.mhz,
            "ram_gb": py::round(total_ram_gb(), 1), "ram_mhz": ram_speed_mhz(),
            "os": system(), "arch": machine(), "python": super::runtime::PYTHON_VERSION,
        });
        match v {
            Value::Object(m) => m,
            _ => Map::new(),
        }
    })
    .clone()
}

fn now_secs() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

impl IconAi {
    /// v2 `free_disk_bytes()` default: the AI folder's drive.
    pub fn free_disk_bytes(&self) -> u64 {
        free_disk_bytes(&self.icon_ai_dir())
    }

    /// v2 `disk_speed()`: bytes/s the AI folder's drive writes (a one-off 128 MB
    /// test, fsynced, then deleted; remembered in settings `icon_ai_disk_speed` =
    /// [speed, time] for a week). 0 on failure.
    pub fn disk_speed(&self) -> f64 {
        let s = self.settings();
        if let Some(saved) = s.values.get("icon_ai_disk_speed").and_then(Value::as_array) {
            if !saved.is_empty() {
                if let (Some(speed), Some(t)) = (py::number(saved.first()), py::number(saved.get(1))) {
                    if now_secs() - t < DISK_SPEED_MAX_AGE {
                        return speed;
                    }
                }
            }
        }
        let dir = self.icon_ai_dir();
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join(".disk_speed_test");
        let mut block = vec![0u8; 4 * 1024 * 1024];
        let _ = getrandom::getrandom(&mut block);
        let result = (|| -> std::io::Result<f64> {
            use std::io::Write;
            let start = std::time::Instant::now();
            let mut f = std::fs::File::create(&path)?;
            for _ in 0..DISK_TEST_BYTES / block.len() {
                f.write_all(&block)?;
            }
            f.flush()?;
            f.sync_all()?;
            Ok(DISK_TEST_BYTES as f64 / start.elapsed().as_secs_f64().max(0.01))
        })();
        let _ = std::fs::remove_file(&path);
        let Ok(speed) = result else { return 0.0 };
        let mut s = self.settings();
        s.set("icon_ai_disk_speed", json!([speed, now_secs()]));
        let _ = s.save();
        speed
    }
}

#[cfg(windows)]
mod win {
    use windows_sys::Win32::System::Registry::{RegGetValueW, HKEY_LOCAL_MACHINE, RRF_RT_REG_DWORD, RRF_RT_REG_SZ};

    fn wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(Some(0)).collect()
    }

    pub fn reg_string(key: &str, value: &str) -> Option<String> {
        let (k, v) = (wide(key), wide(value));
        let mut buf = vec![0u16; 512];
        let mut size = (buf.len() * 2) as u32;
        let rc = unsafe {
            RegGetValueW(HKEY_LOCAL_MACHINE, k.as_ptr(), v.as_ptr(), RRF_RT_REG_SZ, std::ptr::null_mut(), buf.as_mut_ptr().cast(), &mut size)
        };
        if rc != 0 {
            return None;
        }
        let len = (size as usize / 2).saturating_sub(1);
        Some(String::from_utf16_lossy(&buf[..len.min(buf.len())]).trim_end_matches('\0').to_string())
    }

    pub fn reg_dword(key: &str, value: &str) -> Option<u32> {
        let (k, v) = (wide(key), wide(value));
        let mut out = 0u32;
        let mut size = 4u32;
        let rc = unsafe {
            RegGetValueW(HKEY_LOCAL_MACHINE, k.as_ptr(), v.as_ptr(), RRF_RT_REG_DWORD, std::ptr::null_mut(), (&mut out as *mut u32).cast(), &mut size)
        };
        (rc == 0).then_some(out)
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    /// A fake machine for tests.
    pub struct FakeHw {
        pub system: &'static str,
        pub machine: &'static str,
        pub gpu: &'static str,
        pub vram: f64,
        pub profile: Map<String, Value>,
    }
    impl HwSource for FakeHw {
        fn system(&self) -> String {
            self.system.into()
        }
        fn machine(&self) -> String {
            self.machine.into()
        }
        fn gpu_name(&self) -> String {
            self.gpu.into()
        }
        fn vram_gb(&self) -> f64 {
            self.vram
        }
        fn profile(&self) -> Map<String, Value> {
            self.profile.clone()
        }
    }

    pub fn rtx4070() -> FakeHw {
        let profile = match json!({"gpu": "NVIDIA GeForce RTX 4070", "vram_gb": 12.0, "gpu_speed": 1.0, "compute_cap": "8.9",
            "driver": "581.15", "cpu": "AMD Ryzen", "cpu_cores": 16, "cpu_mhz": 3600, "ram_gb": 31.9, "ram_mhz": 3200,
            "os": "Windows", "arch": "AMD64", "python": "3.12.15"}) {
            Value::Object(m) => m,
            _ => unreachable!(),
        };
        FakeHw { system: "Windows", machine: "AMD64", gpu: "NVIDIA GeForce RTX 4070", vram: 11.99, profile }
    }

    #[test]
    fn speeds_and_print() {
        let hw = rtx4070();
        assert_eq!(hardware_print(&hw), "NVIDIA GeForce RTX 4070|12|Windows|AMD64");
        assert_eq!(gpu_speed(&hw), Some(1.0));
        let ti = FakeHw { gpu: "NVIDIA GeForce RTX 4070 Ti SUPER", ..rtx4070() };
        assert_eq!(gpu_speed(&ti), Some(1.35));
        let mac = FakeHw { system: "Darwin", machine: "arm64", gpu: "Apple M2 Pro", vram: 0.0, profile: Map::new() };
        assert!((gpu_speed(&mac).unwrap() - 0.32).abs() < 1e-9);
        let none = FakeHw { gpu: "", ..rtx4070() };
        assert_eq!(gpu_speed(&none), None);
    }

    #[test]
    fn live_probes_do_not_panic() {
        let _ = free_disk_bytes(Path::new("this/does/not/exist"));
        assert!(total_ram_gb() >= 0.0);
        assert!(cpu_info().cores >= 0);
    }
}

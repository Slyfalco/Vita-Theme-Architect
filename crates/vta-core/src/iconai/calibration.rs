//! Performance records (v2 "calibration"): one de-duplicated JSON record per
//! (machine, model, mode, steps), stored encrypted in `icon_ai/calibration.dat`
//! with this copy's Fernet key in `icon_ai/.perf_key`, plus the read-only reference
//! data shipped as `Assets/icon_estimates.dat`. Byte-format compatible with v2 in
//! both directions (raw Fernet token bytes; JSON lines as Python's `json.dumps`).

use super::fernet::Fernet;
use super::hardware::{HwSource, LiveHw};
use super::{py, IconAi};
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::SystemTime;

pub type Record = Map<String, Value>;

/// v2 `_valid_calibration_record(row)`: has the fields and plausible values.
pub fn valid_record(row: &Value) -> bool {
    let Some(r) = row.as_object() else { return false };
    if !py::truthy(r.get("model")) || !r.get("hw").is_some_and(Value::is_object) {
        return false;
    }
    match py::number(r.get("per_icon_s")) {
        Some(per) if (0.1..=3600.0).contains(&per) => {}
        _ => return false,
    }
    for field in ["peak_vram_gb", "peak_ram_gb"] {
        match r.get(field) {
            None | Some(Value::Null) => {}
            v => match py::number(v) {
                Some(x) if (0.0..=2048.0).contains(&x) => {}
                _ => return false,
            },
        }
    }
    true
}

/// v2 `_hw_signature(hw)`: "the same machine" from the timing-relevant hardware.
pub fn hw_signature(hw: &Map<String, Value>) -> String {
    ["gpu", "vram_gb", "cpu", "cpu_cores", "cpu_mhz", "ram_gb", "ram_mhz", "os", "arch"]
        .iter()
        .map(|k| hw.get(*k).map(py::str_of).unwrap_or_default())
        .collect::<Vec<_>>()
        .join("|")
}

/// v2 `_group_key(...)`: one machine, one model, one mode.
#[derive(Debug, Clone)]
pub struct GroupKey {
    sig: String,
    model: Value,
    min_memory: bool,
    poses: bool,
    steps: Value,
    cloud: bool,
}

impl PartialEq for GroupKey {
    fn eq(&self, o: &Self) -> bool {
        self.sig == o.sig
            && py::eq(&self.model, &o.model)
            && self.min_memory == o.min_memory
            && self.poses == o.poses
            && py::eq(&self.steps, &o.steps)
            && self.cloud == o.cloud
    }
}

fn group_key_of(r: &Record) -> GroupKey {
    let empty = Map::new();
    GroupKey {
        sig: hw_signature(r.get("hw").and_then(Value::as_object).unwrap_or(&empty)),
        model: r.get("model").cloned().unwrap_or(Value::Null),
        min_memory: py::truthy(r.get("min_memory")),
        poses: py::truthy(r.get("poses")),
        steps: r.get("steps").cloned().unwrap_or(Value::Null),
        cloud: py::truthy(r.get("cloud")),
    }
}

/// v2 `_ref_cipher()`: the Fernet key of the shipped reference data (PBKDF2-SHA256,
/// 100k iterations, over a secret assembled from two XOR-ed arrays).
fn ref_cipher() -> &'static Fernet {
    static C: OnceLock<Fernet> = OnceLock::new();
    C.get_or_init(|| {
        const A: [u8; 28] = [80, 35, 178, 252, 165, 62, 0, 209, 176, 126, 23, 77, 234, 214, 12, 129,
                             104, 199, 129, 240, 145, 48, 247, 47, 128, 163, 129, 190];
        const B: [u8; 28] = [6, 87, 211, 172, 192, 76, 102, 131, 213, 24, 58, 127, 218, 228, 58, 172,
                             33, 164, 238, 158, 212, 67, 131, 70, 237, 194, 245, 219];
        let secret: Vec<u8> = A.iter().zip(B.iter()).map(|(x, y)| x ^ y).collect();
        let mut key = [0u8; 32];
        pbkdf2::pbkdf2_hmac::<sha2::Sha256>(&secret, b"vta-icon-perf-ref-v1", 100_000, &mut key);
        Fernet::from_raw(&key).expect("32-byte key")
    })
}

fn parse_lines(text: &str) -> Vec<Value> {
    text.lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .filter_map(|l| serde_json::from_str::<Value>(l).ok())
        .collect()
}

type Cache = Mutex<HashMap<PathBuf, (SystemTime, Vec<Record>)>>;

fn cached(cache: &'static OnceLock<Cache>, path: &Path, load: impl FnOnce() -> Vec<Record>) -> Vec<Record> {
    let Ok(mtime) = std::fs::metadata(path).and_then(|m| m.modified()) else { return Vec::new() };
    let cache = cache.get_or_init(|| Mutex::new(HashMap::new()));
    if let Some((t, rows)) = cache.lock().unwrap().get(path) {
        if *t == mtime {
            return rows.clone();
        }
    }
    let rows = load();
    cache.lock().unwrap().insert(path.to_path_buf(), (mtime, rows.clone()));
    rows
}

fn invalidate(cache: &'static OnceLock<Cache>, path: &Path) {
    if let Some(c) = cache.get() {
        c.lock().unwrap().remove(path);
    }
}

static CALIB_CACHE: OnceLock<Cache> = OnceLock::new();
static REF_CACHE: OnceLock<Cache> = OnceLock::new();
static LOCAL_KEYS: OnceLock<Mutex<HashMap<PathBuf, Vec<u8>>>> = OnceLock::new();

impl IconAi {
    /// v2 `_local_cipher()`: Fernet with this copy's key from `.perf_key`, generated
    /// (and saved) the first time. The key is kept for the session once read.
    /// `None` when the stored key is invalid (v2 raises `ValueError`).
    pub fn local_cipher(&self) -> std::io::Result<Option<Fernet>> {
        let file = self.local_key_file();
        let keys = LOCAL_KEYS.get_or_init(|| Mutex::new(HashMap::new()));
        let mut keys = keys.lock().unwrap();
        if !keys.contains_key(&file) {
            let mut key: Vec<u8> = std::fs::read(&file).unwrap_or_default();
            while key.last().is_some_and(|b| b.is_ascii_whitespace()) {
                key.pop();
            }
            let start = key.iter().position(|b| !b.is_ascii_whitespace()).unwrap_or(key.len());
            key.drain(..start);
            if key.is_empty() {
                key = Fernet::generate_key().into_bytes();
                if let Some(dir) = file.parent() {
                    std::fs::create_dir_all(dir)?;
                }
                std::fs::write(&file, &key)?;
            }
            keys.insert(file.clone(), key);
        }
        Ok(Fernet::new(&keys[&file]))
    }

    /// v2 `calibration_records()`: every recorded run, decrypted and parsed (cached
    /// until the file changes). Empty when missing, keyless or tampered.
    pub fn calibration_records(&self) -> Vec<Record> {
        let path = self.calibration_file();
        cached(&CALIB_CACHE, &path, || {
            let Ok(blob) = std::fs::read(&path) else { return Vec::new() };
            let Ok(Some(cipher)) = self.local_cipher() else { return Vec::new() };
            let Some(plain) = cipher.decrypt_raw(&blob) else { return Vec::new() };
            let Ok(text) = String::from_utf8(plain) else { return Vec::new() };
            parse_lines(&text).into_iter().filter_map(|v| v.as_object().cloned()).collect()
        })
    }

    /// v2 `calibration_count()`.
    pub fn calibration_count(&self) -> usize {
        self.calibration_records().len()
    }

    /// v2 `reference_records()`: the shipped (read-only, encrypted + gzipped)
    /// reference data, valid records only, each tagged `source: reference`.
    pub fn reference_records(&self) -> Vec<Record> {
        let path = self.reference_data_file();
        cached(&REF_CACHE, &path, || {
            let Ok(blob) = std::fs::read(&path) else { return Vec::new() };
            let Some(gz) = ref_cipher().decrypt_raw(&blob) else { return Vec::new() };
            let mut text = String::new();
            if flate2::read::MultiGzDecoder::new(gz.as_slice()).read_to_string(&mut text).is_err() {
                return Vec::new();
            }
            parse_lines(&text)
                .into_iter()
                .filter(valid_record)
                .filter_map(|v| match v {
                    Value::Object(mut m) => {
                        m.insert("source".into(), "reference".into());
                        Some(m)
                    }
                    _ => None,
                })
                .collect()
        })
    }

    /// v2 `_prediction_records()`: local records plus reference records for setups
    /// the local data doesn't cover.
    pub fn prediction_records(&self) -> Vec<Record> {
        let local = self.calibration_records();
        let seen: Vec<GroupKey> = local.iter().map(group_key_of).collect();
        let extra: Vec<Record> = self.reference_records().into_iter().filter(|r| !seen.contains(&group_key_of(r))).collect();
        local.into_iter().chain(extra).collect()
    }

    /// v2 `export_calibration(dest)`: write the user's own valid records (JSON
    /// Lines, '_' fields stripped). Returns how many were written.
    pub fn export_calibration(&self, dest: &Path) -> std::io::Result<usize> {
        let mut out = String::new();
        let mut n = 0;
        for row in self.calibration_records() {
            let source_local = row.get("source").map(|s| s.as_str() == Some("local")).unwrap_or(true);
            if !source_local || !valid_record(&Value::Object(row.clone())) {
                continue;
            }
            let clean: Map<String, Value> = row.into_iter().filter(|(k, _)| !k.starts_with('_')).collect();
            out.push_str(&py::dumps(&Value::Object(clean)));
            out.push('\n');
            n += 1;
        }
        std::fs::write(dest, out)?;
        Ok(n)
    }

    /// v2 `record_calibration(key, job, startup, per_icon, icons, peak_vram,
    /// peak_ram, cloud)`: fold one finished run into the local records. Best-effort.
    #[allow(clippy::too_many_arguments)]
    pub fn record_calibration(&self, key: &str, job: &Map<String, Value>, startup: Option<f64>, per_icon: f64,
                              icons: usize, peak_vram: Option<f64>, peak_ram: Option<f64>, cloud: bool) {
        self.record_calibration_with(key, job, startup, per_icon, icons, peak_vram, peak_ram, cloud, &LiveHw)
    }

    #[allow(clippy::too_many_arguments)]
    pub fn record_calibration_with(&self, key: &str, job: &Map<String, Value>, startup: Option<f64>, per_icon: f64,
                                   _icons: usize, peak_vram: Option<f64>, peak_ram: Option<f64>, cloud: bool,
                                   hw_src: &dyn HwSource) {
        let _ = self.record_inner(key, job, startup, per_icon, peak_vram, peak_ram, cloud, hw_src);
    }

    #[allow(clippy::too_many_arguments)]
    fn record_inner(&self, key: &str, job: &Map<String, Value>, startup: Option<f64>, per_icon: f64,
                    peak_vram: Option<f64>, peak_ram: Option<f64>, cloud: bool, hw_src: &dyn HwSource) -> Option<()> {
        let hw = hw_src.profile();
        let speed = py::num_or0(hw.get("gpu_speed"));
        let model = if cloud { Value::from("google") } else { job.get("model").cloned().unwrap_or(Value::Null) };
        let min_memory = py::truthy(job.get("min_memory"));
        let poses = py::truthy(job.get("pose_variation"));
        let steps = job.get("steps").cloned().unwrap_or(Value::Null);
        let gkey = GroupKey { sig: hw_signature(&hw), model: model.clone(), min_memory, poses, steps: steps.clone(), cloud };
        let refv = (speed != 0.0).then(|| py::round(per_icon * speed, 2));
        let ts = utc_timestamp();
        let num = |v: f64| json!(v);
        let pv = peak_vram.filter(|v| *v != 0.0);
        let pr = peak_ram.filter(|v| *v != 0.0);

        let mut rows: Vec<Record> = self
            .calibration_records()
            .into_iter()
            .filter(|r| r.get("source").map(|s| s.as_str() == Some("local")).unwrap_or(true))
            .collect();
        let mut merged = false;
        for agg in rows.iter_mut() {
            if group_key_of(agg) != gkey {
                continue;
            }
            let n = match agg.get("samples") {
                None => 1.0,
                v => py::number(v)?,
            };
            let old_per = match agg.get("per_icon_s") {
                None => per_icon,
                v => py::number(v)?,
            };
            agg.insert("per_icon_s".into(), num(py::round((old_per * n + per_icon) / (n + 1.0), 2)));
            if let Some(r) = refv {
                let rn = match agg.get("_ref_n") { None => 0.0, v => py::number(v)? };
                let old = py::num_or0(agg.get("per_icon_ref"));
                agg.insert("per_icon_ref".into(), num(py::round((old * rn + r) / (rn + 1.0), 2)));
                agg.insert("_ref_n".into(), int_like(rn + 1.0));
            }
            if let Some(s) = startup {
                let sn = match agg.get("_startup_n") { None => 0.0, v => py::number(v)? };
                let old = py::num_or0(agg.get("startup_s"));
                agg.insert("startup_s".into(), num(py::round((old * sn + s) / (sn + 1.0), 1)));
                agg.insert("_startup_n".into(), int_like(sn + 1.0));
            }
            if let Some(v) = pv {
                let old = py::num_or0(agg.get("peak_vram_gb"));
                agg.insert("peak_vram_gb".into(), if old >= v { agg["peak_vram_gb"].clone() } else { num(v) });
            }
            if let Some(v) = pr {
                let old = py::num_or0(agg.get("peak_ram_gb"));
                agg.insert("peak_ram_gb".into(), if old >= v { agg["peak_ram_gb"].clone() } else { num(v) });
            }
            agg.insert("samples".into(), int_like(n + 1.0));
            agg.insert("last_ts".into(), ts.clone().into());
            agg.insert("app".into(), crate::CURRENT_VERSION.into());
            agg.insert("hw".into(), Value::Object(hw.clone()));
            merged = true;
            break;
        }
        if !merged {
            let row = json!({
                "source": "local", "key": key, "cloud": cloud, "model": model,
                "min_memory": min_memory, "poses": poses, "steps": steps,
                "resolution": job.get("resolution").cloned().unwrap_or(Value::Null),
                "per_icon_s": py::round(per_icon, 2),
                "per_icon_ref": refv, "_ref_n": if refv.is_some() { 1 } else { 0 },
                "startup_s": startup.map(|s| py::round(s, 1)),
                "_startup_n": if startup.is_some() { 1 } else { 0 },
                "peak_vram_gb": peak_vram, "peak_ram_gb": peak_ram,
                "samples": 1, "first_ts": ts, "last_ts": ts, "app": crate::CURRENT_VERSION, "hw": hw,
            });
            rows.push(row.as_object()?.clone());
        }
        let file = self.calibration_file();
        std::fs::create_dir_all(file.parent()?).ok()?;
        let payload = rows.into_iter().map(|r| py::dumps(&Value::Object(r))).collect::<Vec<_>>().join("\n");
        let cipher = self.local_cipher().ok()??;
        let blob = cipher.encrypt_raw(payload.as_bytes());
        let mut tmp = file.clone().into_os_string();
        tmp.push(".tmp");
        std::fs::write(&tmp, blob).ok()?;
        std::fs::rename(&tmp, &file).ok()?;
        invalidate(&CALIB_CACHE, &file);
        Some(())
    }

    /// v2 `_hw_time_ratio(src_hw, minmem)`: how much slower (>1) this PC runs than
    /// the measuring machine, from RAM and CPU speed.
    pub fn hw_time_ratio(src_hw: &Map<String, Value>, here: &Map<String, Value>, minmem: bool) -> f64 {
        let mut ratio = 1.0;
        let (src_ram, my_ram) = (py::num_or0(src_hw.get("ram_mhz")), py::num_or0(here.get("ram_mhz")));
        if src_ram != 0.0 && my_ram != 0.0 {
            let w = if minmem { 0.6 } else { 0.05 };
            ratio *= (1.0 - w) + w * (src_ram / my_ram);
        }
        let (src_cpu, my_cpu) = (py::num_or0(src_hw.get("cpu_mhz")), py::num_or0(here.get("cpu_mhz")));
        if src_cpu != 0.0 && my_cpu != 0.0 {
            let w = if minmem { 0.15 } else { 0.1 };
            ratio *= (1.0 - w) + w * (src_cpu / my_cpu);
        }
        ratio
    }

    /// v2 `_dataset_reference(base_key, minmem)`: mean (start-up, reference-GPU
    /// per-icon) measured for this setup on any hardware, or `None`.
    pub fn dataset_reference(&self, base_key: &str, minmem: bool, hw: &dyn HwSource) -> Option<(Option<f64>, f64)> {
        let (_, rest) = base_key.split_once(':')?;
        let model = rest.split('+').next().unwrap_or("");
        let poses = base_key.contains("+poses");
        let (mut per, mut start) = (Vec::new(), Vec::new());
        let mut here: Option<Map<String, Value>> = None;
        for row in self.prediction_records() {
            if py::truthy(row.get("cloud")) || row.get("model").and_then(Value::as_str) != Some(model) {
                continue;
            }
            if py::truthy(row.get("poses")) != poses || py::truthy(row.get("min_memory")) != minmem {
                continue;
            }
            if py::truthy(row.get("per_icon_ref")) {
                let r = py::number(row.get("per_icon_ref")).unwrap_or(0.0);
                let empty = Map::new();
                let src = match row.get("hw") {
                    Some(Value::Object(m)) => m,
                    _ => &empty,
                };
                let here = here.get_or_insert_with(|| hw.profile());
                per.push(r * Self::hw_time_ratio(src, here, minmem));
                if py::truthy(row.get("startup_s")) {
                    start.push(py::number(row.get("startup_s")).unwrap_or(0.0));
                }
            }
        }
        if per.is_empty() {
            return None;
        }
        let s = (!start.is_empty()).then(|| start.iter().sum::<f64>() / start.len() as f64);
        Some((s, per.iter().sum::<f64>() / per.len() as f64))
    }
}

/// Counters are Python ints: keep them integral in JSON.
fn int_like(v: f64) -> Value {
    if v.fract() == 0.0 && v.abs() < 9e15 { json!(v as i64) } else { json!(v) }
}

/// `time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())`.
pub fn utc_timestamp() -> String {
    let secs = SystemTime::now().duration_since(SystemTime::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    // Civil-from-days (Howard Hinnant).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    format!("{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z", rem / 3600, rem % 3600 / 60, rem % 60)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::iconai::hardware::tests::rtx4070;
    use base64::Engine;

    fn vectors() -> Value {
        serde_json::from_str(include_str!("testdata/py_vectors.json")).unwrap()
    }
    fn b64(s: &str) -> Vec<u8> {
        base64::engine::general_purpose::STANDARD.decode(s).unwrap()
    }

    /// Reads calibration.dat + .perf_key exactly as written by v2 (Python
    /// `cryptography.fernet`), and the reference cipher matches v2's derivation.
    #[test]
    fn reads_python_written_files() {
        let v = vectors();
        let ai = crate::iconai::tests::temp_ai("pycompat");
        std::fs::create_dir_all(ai.icon_ai_dir()).unwrap();
        std::fs::write(ai.local_key_file(), v["local_key"].as_str().unwrap()).unwrap();
        std::fs::write(ai.calibration_file(), b64(v["local_blob_b64"].as_str().unwrap())).unwrap();
        let rows = ai.calibration_records();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0]["per_icon_s"], json!(14.25));
        assert_eq!(rows[1]["note"], json!("ünï"));
        // Our dumps reproduce Python's payload byte for byte.
        let payload = rows.iter().map(|r| py::dumps(&Value::Object(r.clone()))).collect::<Vec<_>>().join("\n");
        assert_eq!(payload, v["local_payload"].as_str().unwrap());

        let want = Fernet::new(v["ref_key"].as_str().unwrap().as_bytes()).unwrap();
        let blob = b64(v["ref_blob_b64"].as_str().unwrap());
        assert_eq!(want.decrypt_raw(&blob), ref_cipher().decrypt_raw(&blob));
        assert!(ref_cipher().decrypt_raw(&blob).is_some());
        std::fs::create_dir_all(ai.app_dir.join("Assets")).unwrap();
        std::fs::write(ai.reference_data_file(), &blob).unwrap();
        let refs = ai.reference_records();
        assert_eq!(refs.len(), 1); // the invalid row is dropped
        assert_eq!(refs[0]["source"], json!("reference"));
    }

    /// Optional: proves Python can read what v3 writes. Runs only when
    /// `VTA_TEST_PYTHON` points to a Python with `cryptography` installed.
    #[test]
    fn python_reads_v3_written_file() {
        let Ok(python) = std::env::var("VTA_TEST_PYTHON") else { return };
        let ai = crate::iconai::tests::temp_ai("py-reads");
        let job = json!({"model": "sdxl", "steps": 20, "resolution": 768}).as_object().unwrap().clone();
        ai.record_calibration_with("local:sdxl", &job, Some(30.0), 14.0, 17, Some(7.5), None, false, &rtx4070());
        let script = "import sys,json\nfrom cryptography.fernet import Fernet\nimport base64\n\
            key=open(sys.argv[1],'rb').read().strip()\ntok=base64.urlsafe_b64encode(open(sys.argv[2],'rb').read())\n\
            print(json.dumps([json.loads(l) for l in Fernet(key).decrypt(tok).decode().splitlines()]))";
        let out = std::process::Command::new(python)
            .args(["-I", "-c", script])
            .arg(ai.local_key_file())
            .arg(ai.calibration_file())
            .output()
            .unwrap();
        assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
        let rows: Value = serde_json::from_slice(&out.stdout).unwrap();
        assert_eq!(rows[0]["per_icon_s"], json!(14.0));
        assert_eq!(rows[0]["peak_vram_gb"], json!(7.5));
    }

    #[test]
    fn record_merge_export_and_predict() {
        let ai = crate::iconai::tests::temp_ai("record");
        let hw = rtx4070();
        let job = json!({"model": "sdxl", "steps": 20, "resolution": 768}).as_object().unwrap().clone();
        ai.record_calibration_with("local:sdxl", &job, Some(30.0), 14.0, 17, Some(7.5), Some(10.0), false, &hw);
        ai.record_calibration_with("local:sdxl", &job, None, 16.0, 17, Some(7.0), Some(12.0), false, &hw);
        let rows = ai.calibration_records();
        assert_eq!(rows.len(), 1);
        let r = &rows[0];
        assert_eq!(r["samples"], json!(2));
        assert_eq!(r["per_icon_s"], json!(15.0));
        assert_eq!(r["per_icon_ref"], json!(15.0));
        assert_eq!(r["_ref_n"], json!(2));
        assert_eq!(r["startup_s"], json!(30.0));
        assert_eq!(r["_startup_n"], json!(1));
        assert_eq!(r["peak_vram_gb"], json!(7.5));
        assert_eq!(r["peak_ram_gb"], json!(12.0));
        assert_eq!(r["app"], json!(crate::CURRENT_VERSION));
        let keys: Vec<&String> = r.keys().collect();
        assert_eq!(keys[..6], ["source", "key", "cloud", "model", "min_memory", "poses"]);
        // Another mode is a separate record.
        let mut job2 = job.clone();
        job2.insert("min_memory".into(), json!(true));
        ai.record_calibration_with("local:sdxl+minmem", &job2, Some(40.0), 50.0, 17, None, None, false, &hw);
        assert_eq!(ai.calibration_count(), 2);
        let dest = ai.dirs.app_support.join("export.jsonl");
        assert_eq!(ai.export_calibration(&dest).unwrap(), 2);
        let text = std::fs::read_to_string(&dest).unwrap();
        assert!(!text.contains("\"_ref_n\""));
        assert!(text.ends_with('\n'));
        // Predictions use the reference-GPU figure, adjusted for RAM/CPU (same machine: 1.0).
        assert_eq!(ai.dataset_reference("local:sdxl", false, &hw), Some((Some(30.0), 15.0)));
        assert_eq!(ai.dataset_reference("local:sd15", false, &hw), None);
        assert_eq!(ai.load_timing_with("local:sdxl", &hw), (30.0, 15.0));
        // A key file from another copy can't read it.
        std::fs::write(ai.calibration_file(), b"garbage").unwrap();
        assert_eq!(ai.calibration_count(), 0);
    }

    #[test]
    fn validity_and_ratio() {
        assert!(valid_record(&json!({"model": "sd15", "hw": {}, "per_icon_s": 5})));
        assert!(valid_record(&json!({"model": "sd15", "hw": {}, "per_icon_s": true}))); // bool is an int in Python
        assert!(!valid_record(&json!({"model": "", "hw": {}, "per_icon_s": 5})));
        assert!(!valid_record(&json!({"model": "sd15", "hw": [], "per_icon_s": 5})));
        assert!(!valid_record(&json!({"model": "sd15", "hw": {}, "per_icon_s": 0.05})));
        assert!(!valid_record(&json!({"model": "sd15", "hw": {}, "per_icon_s": 5, "peak_vram_gb": 4096})));
        let a = json!({"ram_mhz": 3200, "cpu_mhz": 3000}).as_object().unwrap().clone();
        let b = json!({"ram_mhz": 1600, "cpu_mhz": 3000}).as_object().unwrap().clone();
        assert!((IconAi::hw_time_ratio(&a, &b, true) - 1.6).abs() < 1e-12);
        assert_eq!(hw_signature(&json!({"gpu": "G", "vram_gb": 12.0, "cpu_cores": 8}).as_object().unwrap().clone()), "G|12.0||8|||||");
    }

    #[test]
    fn timestamp_shape() {
        let t = utc_timestamp();
        assert_eq!(t.len(), 20);
        assert!(t.ends_with('Z') && t.as_bytes()[10] == b'T');
    }
}

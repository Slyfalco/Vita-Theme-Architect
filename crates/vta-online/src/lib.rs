//! Access to the online theme list (the community repository).
//!
//! This crate is only the public interface: a page of themes at a time (`get`)
//! and the number of matches (`count`). Official release builds ship the online
//! module beside the executable and this crate calls into it; other builds have
//! no online access (`status()` explains why).

use serde_json::{Map, Value};
// HTTP client for the optional development file (see the end of this file).
#[cfg(not(all(debug_assertions, vta_dev_online)))]
use ureq as _;

pub const PAGE: usize = 50;
pub const UNAVAILABLE_MSG: &str = "Online features are only available in official release builds.";
pub const VM_MSG: &str = "Online features are turned off in virtual machines.";

/// Which field a search matches.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Field {
    Title,
    Author,
    Tags,
    TitleTags,
}

/// List order.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Sort {
    Newest,
    Oldest,
    Az,
    Za,
    Downloads,
}

/// Catalog filters (worked out by the online module).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Filter {
    All,
    Verified,
    TopRated,
    StaffPicks,
}

impl Filter {
    pub fn code(self) -> u32 {
        match self {
            Filter::All => 0,
            Filter::Verified => 1,
            Filter::TopRated => 2,
            Filter::StaffPicks => 3,
        }
    }
}

/// One theme as handed to the app: display fields plus the module's badges.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Entry(pub Map<String, Value>);

impl Entry {
    pub fn str(&self, key: &str) -> String {
        match self.0.get(key) {
            Some(Value::String(s)) => s.clone(),
            Some(Value::Null) | None => String::new(),
            Some(v) => v.to_string(),
        }
    }
    pub fn num(&self, key: &str) -> i64 {
        match self.0.get(key) {
            Some(Value::Number(n)) => n.as_i64().unwrap_or(0),
            Some(Value::String(s)) => s.trim().parse::<f64>().map(|f| f as i64).unwrap_or(0),
            _ => 0,
        }
    }
    fn flag(&self, key: &str) -> bool {
        matches!(self.0.get(key), Some(Value::Bool(true)))
    }
    /// Whether the theme has a rating at all.
    pub fn has_validation(&self) -> bool {
        self.flag("vta_rated")
    }
    /// Star rating, 0.0–5.0.
    pub fn stars(&self) -> f32 {
        match self.0.get("vta_stars") {
            Some(Value::Number(n)) => n.as_f64().unwrap_or(0.0) as f32,
            _ => 0.0,
        }
    }
    /// The "Verified" badge.
    pub fn verified(&self) -> bool {
        self.flag("vta_verified")
    }
    /// The "Vita Validated" badge.
    pub fn vita_validated(&self) -> bool {
        self.flag("vta_validated")
    }
    /// How many checks the rating is based on.
    pub fn validation_votes(&self) -> i64 {
        self.num("vta_checks")
    }
}

/// Whether online features work in this build: `Err(message)` explains why not.
pub fn status() -> Result<(), String> {
    if let Some(n) = native::get() {
        return n.status();
    }
    #[cfg(all(debug_assertions, vta_dev_online))]
    if dev::url().is_some() {
        return Ok(());
    }
    Err(UNAVAILABLE_MSG.into())
}

pub fn available() -> bool {
    status().is_ok()
}

/// One page (1-based) of themes matching `search` and `filter`, at most [`PAGE`] long.
pub fn get(search: &str, field: Field, filter: Filter, sort: Sort, page: usize, amount: usize, fresh: bool) -> Result<Vec<Entry>, String> {
    if let Some(n) = native::get() {
        return n.get(search, field, filter, sort, page, amount.clamp(1, PAGE), fresh);
    }
    #[cfg(all(debug_assertions, vta_dev_online))]
    return dev::get(search, field, filter, sort, page, amount.clamp(1, PAGE), fresh);
    #[allow(unreachable_code)]
    Err(UNAVAILABLE_MSG.into())
}

/// How many themes match `search` and `filter`.
pub fn count(search: &str, field: Field, filter: Filter, fresh: bool) -> Result<usize, String> {
    if let Some(n) = native::get() {
        return n.count(search, field, filter, fresh);
    }
    #[cfg(all(debug_assertions, vta_dev_online))]
    return dev::count(search, field, filter, fresh);
    #[allow(unreachable_code)]
    Err(UNAVAILABLE_MSG.into())
}

// Development builds run from source may include an uncommitted local file that
// provides the same interface; it is never part of the published source.
#[cfg(all(debug_assertions, vta_dev_online))]
include!(concat!(env!("CARGO_MANIFEST_DIR"), "/../../.dev/online_dev.rs"));

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn reads_badges_from_module_fields() {
        let mut m = Map::new();
        m.insert("vta_rated".into(), json!(true));
        m.insert("vta_stars".into(), json!(4.5));
        m.insert("vta_verified".into(), json!(false));
        m.insert("vta_validated".into(), json!(true));
        m.insert("vta_checks".into(), json!(88));
        let e = Entry(m);
        assert!(e.has_validation() && !e.verified() && e.vita_validated());
        assert_eq!((e.stars(), e.validation_votes()), (4.5, 88));
        assert!(!Entry(Map::new()).has_validation());
        assert_eq!(Entry(Map::new()).stars(), 0.0);
    }
}
impl Field {
    fn code(self) -> u32 {
        match self {
            Field::Title => 0,
            Field::Author => 1,
            Field::Tags => 2,
            Field::TitleTags => 3,
        }
    }
}

impl Sort {
    fn code(self) -> u32 {
        match self {
            Sort::Newest => 0,
            Sort::Oldest => 1,
            Sort::Az => 2,
            Sort::Za => 3,
            Sort::Downloads => 4,
        }
    }
}

/// Official builds: the online module shipped next to the executable (added by
/// the release tooling after the build). Interface (C ABI, UTF-8, the module
/// allocates returned strings and frees them with `vta_online_free`):
///
/// * `i32 vta_online_status(char **reason)` — 0 = available; otherwise `reason`.
/// * `i32 vta_online_count(const char *search, u32 field, u32 fresh, u64 *count, char **error)`
/// * `i32 vta_online_get(const char *search, u32 field, u32 sort, u64 page, u64 amount, u32 fresh, char **json, char **error)`
///   — `json` is an array of at most `amount` theme objects.
/// * `void vta_online_free(char *)`
mod native {
    use super::{Entry, Field, Filter, Sort};
    use std::ffi::{c_char, CStr, CString};
    use std::sync::OnceLock;

    type StatusFn = unsafe extern "C" fn(*mut *mut c_char) -> i32;
    type CountFn = unsafe extern "C" fn(*const c_char, u32, u32, *mut u64, *mut *mut c_char) -> i32;
    type GetFn = unsafe extern "C" fn(*const c_char, u32, u32, u64, u64, u32, *mut *mut c_char, *mut *mut c_char) -> i32;
    type FreeFn = unsafe extern "C" fn(*mut c_char);

    pub struct Native {
        _lib: libloading::Library,
        status: StatusFn,
        count: CountFn,
        get: GetFn,
        free: FreeFn,
    }

    fn lib_name() -> &'static str {
        if cfg!(windows) {
            "vtaonline.dll"
        } else if cfg!(target_os = "macos") {
            "libvtaonline.dylib"
        } else {
            "libvtaonline.so"
        }
    }

    pub fn get() -> Option<&'static Native> {
        static NATIVE: OnceLock<Option<Native>> = OnceLock::new();
        NATIVE
            .get_or_init(|| {
                let exe = std::env::current_exe().ok()?;
                let path = exe.parent()?.join(lib_name());
                if !path.is_file() {
                    return None;
                }
                // SAFETY: loading the official module shipped beside the executable;
                // symbols are resolved with the documented signatures above.
                unsafe {
                    let lib = libloading::Library::new(&path).ok()?;
                    let status = *lib.get::<StatusFn>(b"vta_online_status\0").ok()?;
                    let count = *lib.get::<CountFn>(b"vta_online_count\0").ok()?;
                    let get = *lib.get::<GetFn>(b"vta_online_get\0").ok()?;
                    let free = *lib.get::<FreeFn>(b"vta_online_free\0").ok()?;
                    Some(Native { _lib: lib, status, count, get, free })
                }
            })
            .as_ref()
    }

    impl Native {
        fn take(&self, p: *mut c_char) -> String {
            if p.is_null() {
                return String::new();
            }
            // SAFETY: a NUL-terminated string allocated by the module, freed by it.
            let s = unsafe { CStr::from_ptr(p) }.to_string_lossy().into_owned();
            unsafe { (self.free)(p) };
            s
        }

        pub fn status(&self) -> Result<(), String> {
            let mut reason = std::ptr::null_mut();
            // SAFETY: out-pointer is valid for the call.
            let rc = unsafe { (self.status)(&mut reason) };
            let reason = self.take(reason);
            if rc == 0 {
                Ok(())
            } else {
                Err(if reason.is_empty() { super::UNAVAILABLE_MSG.into() } else { reason })
            }
        }

        pub fn count(&self, search: &str, field: Field, filter: Filter, fresh: bool) -> Result<usize, String> {
            let s = CString::new(search.replace('\0', "")).unwrap_or_default();
            let (mut n, mut err) = (0u64, std::ptr::null_mut());
            // SAFETY: valid C string and out-pointers. The filter rides in the
            // field's high 16 bits (ABI-stable: no change to the function arity).
            let rc = unsafe { (self.count)(s.as_ptr(), field.code() | (filter.code() << 16), u32::from(fresh), &mut n, &mut err) };
            let err = self.take(err);
            if rc == 0 {
                Ok(n as usize)
            } else {
                Err(err)
            }
        }

        pub fn get(&self, search: &str, field: Field, filter: Filter, sort: Sort, page: usize, amount: usize, fresh: bool) -> Result<Vec<Entry>, String> {
            let s = CString::new(search.replace('\0', "")).unwrap_or_default();
            let (mut json, mut err) = (std::ptr::null_mut(), std::ptr::null_mut());
            // SAFETY: valid C string and out-pointers. Filter packed into field's high bits.
            let rc = unsafe { (self.get)(s.as_ptr(), field.code() | (filter.code() << 16), sort.code(), page as u64, amount as u64, u32::from(fresh), &mut json, &mut err) };
            let (json, err) = (self.take(json), self.take(err));
            if rc != 0 {
                return Err(err);
            }
            match serde_json::from_str::<serde_json::Value>(&json) {
                Ok(serde_json::Value::Array(items)) => Ok(items
                    .into_iter()
                    .filter_map(|v| match v {
                        serde_json::Value::Object(m) => Some(Entry(m)),
                        _ => None,
                    })
                    .collect()),
                _ => Err("The theme list was not valid.".into()),
            }
        }
    }
}

/// The theme host lists files as `http://`; the same files are served over https.
pub fn secure_url(url: &str) -> String {
    match url.get(..7) {
        Some(p) if p.eq_ignore_ascii_case("http://") => format!("https://{}", &url[7..]),
        _ => url.to_string(),
    }
}

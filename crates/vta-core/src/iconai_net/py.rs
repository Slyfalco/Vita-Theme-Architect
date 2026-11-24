//! Small Python-compatibility helpers, so the port produces the same strings,
//! requests and random choices as v2 (`icon_generator.py` runs on CPython 3.12).

use serde_json::Value;
use std::fmt;

/// A v2 exception, shown the way v2 shows it: `f"{type(e).__name__}: {e}"`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PyError {
    pub kind: String,
    pub message: String,
}

impl PyError {
    pub fn new(kind: &str, message: impl Into<String>) -> Self {
        PyError { kind: kind.to_string(), message: message.into() }
    }
}

impl fmt::Display for PyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.kind, self.message)
    }
}

impl std::error::Error for PyError {}

/// Python's type name of a JSON-decoded value.
pub fn type_name(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.is_f64() => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

/// Python truthiness of a JSON-decoded value.
pub fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().is_some_and(|f| f != 0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

fn number_str(n: &serde_json::Number) -> String {
    if let Some(i) = n.as_i64() {
        return i.to_string();
    }
    if let Some(u) = n.as_u64() {
        return u.to_string();
    }
    let f = n.as_f64().unwrap_or(0.0);
    if f.is_finite() && f.fract() == 0.0 && f.abs() < 1e16 {
        format!("{f:.1}")
    } else {
        f.to_string()
    }
}

/// `str(value)` of a JSON-decoded value.
pub fn str_of(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => repr(other),
    }
}

/// `str(d.get(key, default))`: `None` for a JSON null.
pub fn str_or(v: Option<&Value>, default: &str) -> String {
    v.map(str_of).unwrap_or_else(|| default.to_string())
}

/// `repr(value)` of a JSON-decoded value.
pub fn repr(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(n) => number_str(n),
        Value::String(s) => repr_str(s),
        Value::Array(a) => format!("[{}]", a.iter().map(repr).collect::<Vec<_>>().join(", ")),
        Value::Object(o) => format!(
            "{{{}}}",
            o.iter().map(|(k, v)| format!("{}: {}", repr_str(k), repr(v))).collect::<Vec<_>>().join(", ")
        ),
    }
}

/// `repr(str)`.
pub fn repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => out.push_str(&format!("\\x{:02x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// `json.dumps(value)` with the defaults `requests` and `json.dump` use:
/// `", "` / `": "` separators and `ensure_ascii=True`.
pub fn json_dumps(v: &Value) -> String {
    let mut out = String::new();
    dump(v, &mut out);
    out
}

fn dump(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&number_str(n)),
        Value::String(s) => dump_str(s, out),
        Value::Array(a) => {
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                dump(x, out);
            }
            out.push(']');
        }
        Value::Object(o) => {
            out.push('{');
            for (i, (k, x)) in o.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                dump_str(k, out);
                out.push_str(": ");
                dump(x, out);
            }
            out.push('}');
        }
    }
}

fn dump_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' => out.push(c),
            c => {
                let mut units = [0u16; 2];
                for u in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{u:04x}"));
                }
            }
        }
    }
    out.push('"');
}

/// `urllib.parse.quote_plus`.
pub fn quote_plus(s: &str) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' => out.push(b as char),
            b' ' => out.push('+'),
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// `url + '?' + urlencode(params)`, as `requests` builds a GET with `params=`.
pub fn url_with_params(url: &str, params: &[(&str, String)]) -> String {
    let query: Vec<String> = params.iter().map(|(k, v)| format!("{}={}", quote_plus(k), quote_plus(v))).collect();
    if query.is_empty() {
        url.to_string()
    } else {
        format!("{url}?{}", query.join("&"))
    }
}

/// `int(text)` (base 10): surrounding whitespace, a sign and single underscores
/// between digits are accepted, as in Python.
pub fn int(text: &str) -> Result<i64, PyError> {
    let bad = || PyError::new("ValueError", format!("invalid literal for int() with base 10: {}", repr_str(text)));
    let t = text.trim();
    let (neg, digits) = match t.as_bytes().first() {
        Some(b'-') => (true, &t[1..]),
        Some(b'+') => (false, &t[1..]),
        _ => (false, t),
    };
    if digits.is_empty() || digits.starts_with('_') || digits.ends_with('_') || digits.contains("__") {
        return Err(bad());
    }
    let clean: String = digits.chars().filter(|c| *c != '_').collect();
    if !clean.chars().all(|c| c.is_ascii_digit()) {
        return Err(bad());
    }
    let n: i64 = clean.parse().map_err(|_| bad())?;
    Ok(if neg { -n } else { n })
}

/// `s[:n]` on characters.
pub fn head_chars(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// `os.path.basename` of a string path (both separators on Windows, `/` elsewhere).
pub fn basename(p: &str) -> &str {
    let cut = if cfg!(windows) { p.rfind(['/', '\\']) } else { p.rfind('/') };
    match cut {
        Some(i) => &p[i + 1..],
        None => p,
    }
}

/// `os.path.splitext(name)[0]` for a bare file name.
pub fn splitext_stem(name: &str) -> &str {
    if let Some(dot) = name.rfind('.') {
        if name[..dot].chars().any(|c| c != '.') {
            return &name[..dot];
        }
    }
    name
}

// --------------------------------------------------------------------------
// random.Random (CPython's Mersenne Twister), for v2's seeded choices.

const N: usize = 624;
const M: usize = 397;

/// CPython's `random.Random`, giving the same sequence for the same seed.
/// Only what v2 uses here is ported: integer seeding, `getrandbits`,
/// `shuffle` and `sample`.
#[derive(Clone)]
pub struct PyRandom {
    mt: [u32; N],
    index: usize,
}

impl PyRandom {
    /// `random.Random(seed)` for an int seed.
    pub fn new(seed: i64) -> Self {
        let mut n = seed.unsigned_abs();
        let mut key = Vec::new();
        while n > 0 {
            key.push(n as u32);
            n >>= 32;
        }
        if key.is_empty() {
            key.push(0);
        }
        let mut r = PyRandom { mt: [0; N], index: N };
        r.init_by_array(&key);
        r
    }

    /// An unpredictable generator (v2's module-level `random`).
    pub fn from_time() -> Self {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as i64)
            .unwrap_or(0);
        PyRandom::new(nanos ^ (std::process::id() as i64) << 20)
    }

    fn init_genrand(&mut self, s: u32) {
        self.mt[0] = s;
        for i in 1..N {
            self.mt[i] = 1812433253u32.wrapping_mul(self.mt[i - 1] ^ (self.mt[i - 1] >> 30)).wrapping_add(i as u32);
        }
        self.index = N;
    }

    fn init_by_array(&mut self, key: &[u32]) {
        self.init_genrand(19650218);
        let (mut i, mut j) = (1usize, 0usize);
        for _ in 0..N.max(key.len()) {
            self.mt[i] = (self.mt[i] ^ (self.mt[i - 1] ^ (self.mt[i - 1] >> 30)).wrapping_mul(1664525))
                .wrapping_add(key[j])
                .wrapping_add(j as u32);
            i += 1;
            j += 1;
            if i >= N {
                self.mt[0] = self.mt[N - 1];
                i = 1;
            }
            if j >= key.len() {
                j = 0;
            }
        }
        for _ in 0..N - 1 {
            self.mt[i] = (self.mt[i] ^ (self.mt[i - 1] ^ (self.mt[i - 1] >> 30)).wrapping_mul(1566083941)).wrapping_sub(i as u32);
            i += 1;
            if i >= N {
                self.mt[0] = self.mt[N - 1];
                i = 1;
            }
        }
        self.mt[0] = 0x8000_0000;
    }

    pub fn genrand_u32(&mut self) -> u32 {
        const MAG01: [u32; 2] = [0, 0x9908_b0df];
        if self.index >= N {
            for kk in 0..N {
                let y = (self.mt[kk] & 0x8000_0000) | (self.mt[(kk + 1) % N] & 0x7fff_ffff);
                self.mt[kk] = self.mt[(kk + M) % N] ^ (y >> 1) ^ MAG01[(y & 1) as usize];
            }
            self.index = 0;
        }
        let mut y = self.mt[self.index];
        self.index += 1;
        y ^= y >> 11;
        y ^= (y << 7) & 0x9d2c_5680;
        y ^= (y << 15) & 0xefc6_0000;
        y ^= y >> 18;
        y
    }

    /// `getrandbits(k)` for k <= 64.
    pub fn getrandbits(&mut self, k: u32) -> u64 {
        if k == 0 {
            return 0;
        }
        if k <= 32 {
            return (self.genrand_u32() >> (32 - k)) as u64;
        }
        let lo = self.genrand_u32() as u64;
        let hi = (self.genrand_u32() >> (64 - k)) as u64;
        lo | (hi << 32)
    }

    /// `_randbelow(n)`, n >= 1.
    pub fn randbelow(&mut self, n: usize) -> usize {
        let k = usize::BITS - n.leading_zeros();
        let mut r = self.getrandbits(k) as usize;
        while r >= n {
            r = self.getrandbits(k) as usize;
        }
        r
    }

    /// `random.shuffle(x)`.
    pub fn shuffle<T>(&mut self, x: &mut [T]) {
        for i in (1..x.len()).rev() {
            let j = self.randbelow(i + 1);
            x.swap(i, j);
        }
    }

    /// `random.sample(range(n), k)`: the chosen indices, in sample order.
    pub fn sample_indices(&mut self, n: usize, k: usize) -> Vec<usize> {
        assert!(k <= n, "Sample larger than population");
        let mut setsize = 21usize;
        if k > 5 {
            setsize += 4usize.pow(((k * 3) as f64).log(4.0).ceil() as u32);
        }
        let mut result = Vec::with_capacity(k);
        if n <= setsize {
            let mut pool: Vec<usize> = (0..n).collect();
            for i in 0..k {
                let j = self.randbelow(n - i);
                result.push(pool[j]);
                pool[j] = pool[n - i - 1];
            }
        } else {
            let mut selected = std::collections::HashSet::new();
            for _ in 0..k {
                let mut j = self.randbelow(n);
                while selected.contains(&j) {
                    j = self.randbelow(n);
                }
                selected.insert(j);
                result.push(j);
            }
        }
        result
    }

    /// `random.sample(population, k)`.
    pub fn sample<T: Clone>(&mut self, population: &[T], k: usize) -> Vec<T> {
        self.sample_indices(population.len(), k).into_iter().map(|i| population[i].clone()).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Expected values from CPython 3.12 `random.Random`.
    #[test]
    fn mersenne_twister_matches_cpython() {
        let mut r = PyRandom::new(12345);
        let mut x: Vec<usize> = (0..10).collect();
        r.shuffle(&mut x);
        assert_eq!(x, vec![8, 7, 3, 5, 1, 2, 9, 4, 0, 6]);
        assert_eq!(r.sample_indices(30, 4), vec![5, 11, 3, 27]);
        assert_eq!(r.sample_indices(10, 3), vec![6, 4, 2]);
        let mut r = PyRandom::new(0);
        assert_eq!([1, 5, 32, 33, 64].map(|k| r.getrandbits(k)), [1, 12, 3255389356, 3823568514, 4776171008201404212]);
        let mut r = PyRandom::new(999999);
        let mut y: Vec<usize> = (0..5).collect();
        r.shuffle(&mut y);
        assert_eq!(y, vec![3, 0, 2, 4, 1]);
        assert_eq!(r.sample_indices(25, 4), vec![17, 24, 13, 18]);
        assert_eq!(r.sample_indices(3, 3), vec![1, 2, 0]);
        let mut r = PyRandom::new((1i64 << 40) + 7);
        assert_eq!([r.randbelow(1000), r.randbelow(1000), r.randbelow(1000)], [628, 765, 834]);
    }

    #[test]
    fn python_strings() {
        assert_eq!(repr_str("it's"), "\"it's\"");
        assert_eq!(repr_str("a\nb"), "'a\\nb'");
        assert_eq!(json_dumps(&serde_json::json!({"title": "Café \"x\"", "author": null})),
                   "{\"title\": \"Caf\\u00e9 \\\"x\\\"\", \"author\": null}");
        assert_eq!(quote_plus("lucy_(elfen lied) solo"), "lucy_%28elfen+lied%29+solo");
        assert_eq!(int(" 12 ").unwrap(), 12);
        assert_eq!(int("1_0").unwrap(), 10);
        assert_eq!(int("1.5").unwrap_err().to_string(), "ValueError: invalid literal for int() with base 10: '1.5'");
        assert_eq!(splitext_stem("br1.png"), "br1");
        assert_eq!(splitext_stem(".png"), ".png");
        assert_eq!(repr(&serde_json::json!({"a": [1, true, null, "x"]})), "{'a': [1, True, None, 'x']}");
    }
}

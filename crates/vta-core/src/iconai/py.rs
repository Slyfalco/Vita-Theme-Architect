//! Small helpers that reproduce Python semantics v2 relies on (rounding, `str()`,
//! `:g` formatting, truthiness, `json.dumps` defaults), so numbers, keys and files
//! come out exactly as v2 wrote them.

use serde_json::{Map, Value};

/// Python's `round(x, n)` (correctly rounded, ties to even on the exact binary value).
/// Rust's fixed-precision formatting is exact with the same tie rule, so formatting
/// and parsing back gives Python's result.
pub fn round(x: f64, ndigits: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    format!("{x:.ndigits$}").parse().unwrap_or(x)
}

/// Python's `round(x)` (no ndigits): nearest integer, ties to even.
pub fn round_int(x: f64) -> i64 {
    x.round_ties_even() as i64
}

/// Python's `f"{x:.Nf}"`.
pub fn fixed(x: f64, n: usize) -> String {
    let s = format!("{x:.n$}");
    // Python prints "-0" for tiny negatives too; Rust does as well. Keep as-is.
    s
}

/// Python's `repr(float)` / `str(float)`: shortest round-trip digits, `12.0` for
/// integral values, scientific notation below 1e-4 or from 1e16.
pub fn float_repr(x: f64) -> String {
    if x.is_nan() {
        return "nan".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf".into() } else { "-inf".into() };
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0.0".into() } else { "0.0".into() };
    }
    // Shortest digits + exponent from Rust's `{:e}` (same shortest round-trip rule).
    let e = format!("{:e}", x.abs());
    let (mant, exp) = e.split_once('e').unwrap();
    let exp: i32 = exp.parse().unwrap();
    let digits: String = mant.chars().filter(|c| *c != '.').collect();
    let sign = if x < 0.0 { "-" } else { "" };
    if (-4..16).contains(&exp) {
        let n = digits.len() as i32;
        let body = if exp >= 0 {
            let int_len = exp + 1;
            if n <= int_len {
                format!("{}{}.0", digits, "0".repeat((int_len - n) as usize))
            } else {
                format!("{}.{}", &digits[..int_len as usize], &digits[int_len as usize..])
            }
        } else {
            format!("0.{}{}", "0".repeat((-exp - 1) as usize), digits)
        };
        format!("{sign}{body}")
    } else {
        let m = if digits.len() > 1 { format!("{}.{}", &digits[..1], &digits[1..]) } else { digits };
        let es = if exp < 0 { "-" } else { "+" };
        format!("{sign}{m}e{es}{:02}", exp.abs())
    }
}

/// Python's `f"{x:g}"` (6 significant digits, trailing zeros removed).
pub fn fmt_g(x: f64) -> String {
    if !x.is_finite() {
        return float_repr(x);
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0".into() } else { "0".into() };
    }
    let exp = x.abs().log10().floor() as i32;
    // Round to 6 significant digits first; the exponent may move (9.999995 -> 10).
    let sci = format!("{:.5e}", x);
    let (mant, e) = sci.split_once('e').unwrap();
    let exp_r: i32 = e.parse().unwrap_or(exp);
    let trim = |s: &str| -> String {
        if s.contains('.') {
            s.trim_end_matches('0').trim_end_matches('.').to_string()
        } else {
            s.to_string()
        }
    };
    if (-4..6).contains(&exp_r) {
        let prec = (5 - exp_r).max(0) as usize;
        trim(&format!("{x:.prec$}"))
    } else {
        let es = if exp_r < 0 { "-" } else { "+" };
        format!("{}e{es}{:02}", trim(mant), exp_r.abs())
    }
}

/// Python's `str(value)` for a JSON-loaded value (used to build `_hw_signature`).
pub fn str_of(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True".into() } else { "False".into() },
        Value::Number(n) => {
            if n.is_f64() {
                float_repr(n.as_f64().unwrap_or(0.0))
            } else {
                n.to_string()
            }
        }
        Value::String(s) => s.clone(),
        Value::Array(a) => format!("[{}]", a.iter().map(repr_of).collect::<Vec<_>>().join(", ")),
        Value::Object(o) => format!(
            "{{{}}}",
            o.iter().map(|(k, v)| format!("{}: {}", repr_str(k), repr_of(v))).collect::<Vec<_>>().join(", ")
        ),
    }
}

fn repr_str(s: &str) -> String {
    if s.contains('\'') && !s.contains('"') {
        format!("\"{s}\"")
    } else {
        format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'"))
    }
}

fn repr_of(v: &Value) -> String {
    match v {
        Value::String(s) => repr_str(s),
        other => str_of(other),
    }
}

/// Python truthiness of a JSON value (`None`/`False`/0/""/[]/{} are false).
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

/// A Python number (`int`/`float`, and `bool`, which is an `int` subclass).
pub fn number(v: Option<&Value>) -> Option<f64> {
    match v? {
        Value::Number(n) => n.as_f64(),
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        _ => None,
    }
}

/// `x or 0` for a numeric field.
pub fn num_or0(v: Option<&Value>) -> f64 {
    if truthy(v) { number(v).unwrap_or(0.0) } else { 0.0 }
}

/// Python `==` between two JSON-loaded values (1 == 1.0 == True).
pub fn eq(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Number(_) | Value::Bool(_), Value::Number(_) | Value::Bool(_)) => {
            number(Some(a)) == number(Some(b))
        }
        (Value::Array(x), Value::Array(y)) => x.len() == y.len() && x.iter().zip(y).all(|(p, q)| eq(p, q)),
        (Value::Object(x), Value::Object(y)) => {
            x.len() == y.len() && x.iter().all(|(k, v)| y.get(k).is_some_and(|w| eq(v, w)))
        }
        _ => a == b,
    }
}

/// `json.dumps(value)` with Python's defaults: `", "` / `": "` separators and
/// non-ASCII escaped (`ensure_ascii`).
pub fn dumps(v: &Value) -> String {
    let mut out = String::new();
    write_value(v, &mut out);
    out
}

fn write_value(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => {
            if let Some(f) = n.as_f64().filter(|_| n.is_f64()) {
                out.push_str(&float_repr(f).replace("inf", "Infinity").replace("nan", "NaN"));
            } else {
                out.push_str(&n.to_string());
            }
        }
        Value::String(s) => write_str(s, out),
        Value::Array(a) => {
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_value(x, out);
            }
            out.push(']');
        }
        Value::Object(o) => write_object(o, out),
    }
}

fn write_object(o: &Map<String, Value>, out: &mut String) {
    out.push('{');
    for (i, (k, x)) in o.iter().enumerate() {
        if i > 0 {
            out.push_str(", ");
        }
        write_str(k, out);
        out.push_str(": ");
        write_value(x, out);
    }
    out.push('}');
}

fn write_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || !c.is_ascii() => {
                let mut units = [0u16; 2];
                for u in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{u:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// `json.dump(value, f, indent=N)` (ensure_ascii on): pretty JSON with Python's
/// `", "`-free item separator and `": "` key separator.
pub fn dumps_indent(v: &Value, indent: usize) -> String {
    let pad = " ".repeat(indent);
    let mut buf = Vec::new();
    let fmt = serde_json::ser::PrettyFormatter::with_indent(pad.as_bytes());
    let mut ser = serde_json::Serializer::with_formatter(&mut buf, fmt);
    serde::Serialize::serialize(v, &mut ser).expect("serialising JSON to memory");
    let text = String::from_utf8(buf).expect("serde_json writes UTF-8");
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        if c.is_ascii() {
            out.push(c);
        } else {
            let mut units = [0u16; 2];
            for u in c.encode_utf16(&mut units) {
                out.push_str(&format!("\\u{u:04x}"));
            }
        }
    }
    out
}

/// Python's `os.path.basename` (splits on both separators on Windows).
pub fn basename(p: &str) -> String {
    let cut = if cfg!(windows) { p.rfind(['/', '\\']) } else { p.rfind('/') };
    match cut {
        Some(i) => p[i + 1..].to_string(),
        None if cfg!(windows) && p.len() >= 2 && p.as_bytes()[1] == b':' => p[2..].to_string(),
        None => p.to_string(),
    }
}

/// `fnmatch.fnmatch` (case-insensitive on Windows, like `os.path.normcase`).
pub fn fnmatch(name: &str, pat: &str) -> bool {
    let (n, p) = if cfg!(windows) {
        (name.to_lowercase().replace('/', "\\"), pat.to_lowercase().replace('/', "\\"))
    } else {
        (name.to_string(), pat.to_string())
    };
    let n: Vec<char> = n.chars().collect();
    let p: Vec<char> = p.chars().collect();
    glob_match(&n, &p)
}

fn glob_match(n: &[char], p: &[char]) -> bool {
    match p.first() {
        None => n.is_empty(),
        Some('*') => (0..=n.len()).any(|i| glob_match(&n[i..], &p[1..])),
        Some('?') => !n.is_empty() && glob_match(&n[1..], &p[1..]),
        Some('[') => {
            let Some(close) = p.iter().skip(2).position(|c| *c == ']').map(|i| i + 2) else {
                return n.first() == Some(&'[') && glob_match(&n[1..], &p[1..]);
            };
            let Some(&c) = n.first() else { return false };
            let mut set = &p[1..close];
            let negate = set.first() == Some(&'!');
            if negate {
                set = &set[1..];
            }
            let mut hit = false;
            let mut i = 0;
            while i < set.len() {
                if i + 2 < set.len() && set[i + 1] == '-' {
                    hit |= set[i] <= c && c <= set[i + 2];
                    i += 3;
                } else {
                    hit |= set[i] == c;
                    i += 1;
                }
            }
            hit != negate && glob_match(&n[1..], &p[close + 1..])
        }
        Some(&c) => n.first() == Some(&c) && glob_match(&n[1..], &p[1..]),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn rounding_like_python() {
        assert_eq!(round(0.125, 2), 0.12);
        assert_eq!(round(2.675, 2), 2.67);
        assert_eq!(round(0.25, 1), 0.2);
        assert_eq!(round(14.256, 2), 14.26);
        assert_eq!(round_int(2.5), 2);
        assert_eq!(round_int(3.5), 4);
        assert_eq!(fixed(2.5, 0), "2");
        assert_eq!(fixed(0.25, 1), "0.2");
        assert_eq!(fixed(1.45, 1), "1.4"); // 1.45 is 1.4499999... in binary
    }

    #[test]
    fn reprs_like_python() {
        assert_eq!(float_repr(12.0), "12.0");
        assert_eq!(float_repr(0.1), "0.1");
        assert_eq!(float_repr(1e16), "1e+16");
        assert_eq!(float_repr(1.5e-5), "1.5e-05");
        assert_eq!(float_repr(0.0001), "0.0001");
        assert_eq!(float_repr(123456789.25), "123456789.25");
        assert_eq!(fmt_g(4.0), "4");
        assert_eq!(fmt_g(7.5), "7.5");
        assert_eq!(fmt_g(16.0), "16");
        assert_eq!(fmt_g(1234567.0), "1.23457e+06");
        assert_eq!(fmt_g(0.00001), "1e-05");
        assert_eq!(str_of(&json!(null)), "None");
        assert_eq!(str_of(&json!(12.0)), "12.0");
        assert_eq!(str_of(&json!(8)), "8");
        assert_eq!(str_of(&json!(true)), "True");
    }

    #[test]
    fn dumps_like_python() {
        let v = json!({"a": 1, "b": [1.0, null, "ü"], "c": {"d": false}});
        assert_eq!(dumps(&v), "{\"a\": 1, \"b\": [1.0, null, \"\\u00fc\"], \"c\": {\"d\": false}}");
        assert_eq!(dumps_indent(&json!({"version": 2, "p": [["a"], []]}), 2),
                   "{\n  \"version\": 2,\n  \"p\": [\n    [\n      \"a\"\n    ],\n    []\n  ]\n}");
    }

    #[test]
    fn fnmatch_and_basename() {
        assert!(fnmatch("transformer/diffusion_pytorch_model.safetensors", "transformer/*"));
        assert!(fnmatch("tokenizer_2/vocab.json", "tokenizer*/*"));
        assert!(!fnmatch("vae2/config.json", "vae/*"));
        assert!(fnmatch("model_index.json", "model_index.json"));
        assert_eq!(basename("a/b/model.safetensors"), "model.safetensors");
        assert_eq!(basename("a/b/"), "");
    }
}

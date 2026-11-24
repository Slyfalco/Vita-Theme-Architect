//! `settings.json`: read and written in the same format as v2 (a flat JSON object,
//! 4-space indent, non-ASCII escaped like Python's `json.dump`). Unknown keys are kept,
//! so v2 and v3 can share the file and nothing is lost across the upgrade.

use serde_json::{Map, Value};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Default)]
pub struct Settings {
    pub values: Map<String, Value>,
    path: PathBuf,
}

impl Settings {
    /// Load; a missing or unreadable file gives empty settings (like v2).
    pub fn load(path: &Path) -> Self {
        let values = std::fs::read(path)
            .ok()
            .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
            .and_then(|v| match v {
                Value::Object(m) => Some(m),
                _ => None,
            })
            .unwrap_or_default();
        Settings { values, path: path.to_path_buf() }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn contains(&self, key: &str) -> bool {
        self.values.contains_key(key)
    }

    pub fn str(&self, key: &str) -> Option<&str> {
        self.values.get(key).and_then(Value::as_str)
    }

    pub fn str_or(&self, key: &str, default: &str) -> String {
        self.str(key).unwrap_or(default).to_string()
    }

    /// Python truthiness of a stored value, `default` when absent.
    pub fn flag(&self, key: &str, default: bool) -> bool {
        match self.values.get(key) {
            None => default,
            Some(Value::Bool(b)) => *b,
            Some(Value::Null) => false,
            Some(Value::Number(n)) => n.as_f64().is_some_and(|f| f != 0.0),
            Some(Value::String(s)) => !s.is_empty(),
            Some(Value::Array(a)) => !a.is_empty(),
            Some(Value::Object(o)) => !o.is_empty(),
        }
    }

    pub fn set(&mut self, key: &str, value: impl Into<Value>) {
        self.values.insert(key.to_string(), value.into());
    }

    pub fn save(&self) -> std::io::Result<()> {
        if let Some(dir) = self.path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        std::fs::write(&self.path, to_python_json(&Value::Object(self.values.clone())))
    }
}

/// `json.dumps(value, indent=4)` (ensure_ascii on): pretty JSON, 4 spaces, non-ASCII
/// as `\uXXXX` escapes.
pub fn to_python_json(value: &Value) -> String {
    let mut buf = Vec::new();
    let fmt = serde_json::ser::PrettyFormatter::with_indent(b"    ");
    let mut ser = serde_json::Serializer::with_formatter(&mut buf, fmt);
    serde::Serialize::serialize(value, &mut ser).expect("serialising JSON to memory");
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_style_json() {
        let v: Value = serde_json::json!({"vita_ip": "192.168.0.2", "name": "Ünï 日", "n": 1337, "b": true});
        let s = to_python_json(&v);
        assert!(s.contains("    \"vita_ip\": \"192.168.0.2\""));
        assert!(s.contains("\\u00dcn\\u00ef \\u65e5"));
        let back: Value = serde_json::from_str(&s).unwrap();
        assert_eq!(back, v);
    }
}

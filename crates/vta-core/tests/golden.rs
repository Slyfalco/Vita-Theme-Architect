//! theme.xml parity: the Rust reader/writer must match the v2 (Python) app exactly.
//! The golden files were recorded by running the v2 code itself (a script on the
//! v2 `main` branch, `scripts/v3_golden.py`); they are fixed reference data now.

use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};
use vta_core::theme::{parse_theme_xml, theme_xml_string, write_theme_xml, ThemeData};

fn golden(sub: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/golden").join(sub)
}

fn cases(sub: &str) -> Vec<(String, PathBuf)> {
    let mut out: Vec<_> = std::fs::read_dir(golden(sub))
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|e| e == "xml"))
        .map(|p| (p.file_stem().unwrap().to_string_lossy().into_owned(), p))
        .collect();
    out.sort();
    assert!(!out.is_empty(), "no golden cases in {sub}");
    out
}

#[test]
fn writer_matches_v2_byte_for_byte() {
    for (name, xml) in cases("write") {
        let input: ThemeData =
            serde_json::from_str(&std::fs::read_to_string(xml.with_extension("json")).unwrap()).unwrap();
        let expected = std::fs::read_to_string(&xml).unwrap();
        let actual = theme_xml_string(&input);
        assert_eq!(actual, expected, "writer mismatch for case `{name}`");
    }
}

#[test]
fn written_file_uses_platform_newlines() {
    let dir = std::env::temp_dir().join(format!("vta-golden-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    write_theme_xml(&dir, &ThemeData::default()).unwrap();
    let bytes = std::fs::read(dir.join("theme.xml")).unwrap();
    let crlf = bytes.windows(2).filter(|w| w == b"\r\n").count();
    let lf = bytes.iter().filter(|b| **b == b'\n').count();
    if cfg!(windows) {
        assert_eq!(crlf, lf);
    } else {
        assert_eq!(crlf, 0);
    }
    std::fs::remove_dir_all(&dir).ok();
}

/// The v2 dict shape returned by parse_theme_xml.
fn as_python_dict(d: &ThemeData, invalid: bool) -> Value {
    let map = |m: &std::collections::BTreeMap<String, String>| {
        Value::Object(m.iter().map(|(k, v)| (k.clone(), json!(v))).collect::<Map<_, _>>())
    };
    let mut v = json!({
        "title": d.title, "author": d.author, "version": d.version,
        "assets": map(&d.assets), "colors": map(&d.colors),
        "page_colors": map(&d.page_colors), "page_waves": map(&d.page_waves),
    });
    if invalid {
        v["bgm_loop_start"] = json!("0");
    }
    v
}

#[test]
fn reader_matches_v2() {
    for (name, xml) in cases("parse") {
        let expected: Value =
            serde_json::from_str(&std::fs::read_to_string(xml.with_extension("json")).unwrap()).unwrap();
        let parsed = parse_theme_xml(&xml);
        let invalid = expected.get("bgm_loop_start").is_some();
        assert_eq!(as_python_dict(&parsed, invalid), expected, "reader mismatch for case `{name}`");
    }
}

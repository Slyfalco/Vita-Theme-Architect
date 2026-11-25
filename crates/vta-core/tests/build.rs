//! Theme build end to end: BGM encoding and a full build from a real theme.

use std::path::{Path, PathBuf};
use vta_core::build::{build_theme, convert_to_at9, validate, BuildInput};
use vta_core::library::find_theme_problem;
use vta_core::theme::parse_theme_xml;

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("vta-build-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

/// 3 s stereo 44.1 kHz sine as a 16-bit WAV.
fn write_wav(path: &Path) {
    let rate = 44_100u32;
    let mut samples = Vec::new();
    for i in 0..rate * 3 {
        let v = ((i as f32 * 440.0 * std::f32::consts::TAU / rate as f32).sin() * 12_000.0) as i16;
        samples.push(v);
        samples.push(v);
    }
    at9codec::wav::write_pcm16(std::fs::File::create(path).unwrap(), 2, rate, &samples).unwrap();
}

#[test]
fn encodes_bgm_with_loop() {
    let dir = tmp("bgm");
    write_wav(&dir.join("in.wav"));
    let warn = convert_to_at9(&dir.join("in.wav"), &dir.join("bgm.at9"), Some(22_050), &|_, _| {}).unwrap();
    assert!(warn.is_none());
    let buf = std::fs::read(dir.join("bgm.at9")).unwrap();
    let c = at9codec::parse(&buf).unwrap();
    assert!(c.loop_info.is_some(), "loop point written");
    let pcm = at9codec::decode_to_pcm16(&buf).unwrap();
    assert_eq!(pcm.channels, 2);
    assert!(pcm.frames() > 100_000);
    std::fs::remove_dir_all(&dir).ok();
}

/// Rebuild a real local theme (skipped when the dev library isn't present).
#[test]
fn rebuilds_real_theme() {
    let lib = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../Themes");
    let Some(src) = ["theme3", "theme", "theme1"].iter().map(|n| lib.join(n)).find(|p| p.join("theme.xml").is_file()) else {
        eprintln!("no local theme to rebuild; skipped");
        return;
    };
    let data = parse_theme_xml(&src.join("theme.xml"));
    let mut input = BuildInput {
        title: data.title.clone().unwrap_or_default(),
        author: data.author.clone().unwrap_or_default(),
        version: data.version.clone().unwrap_or_default(),
        use_global_font_color: true,
        ..Default::default()
    };
    for (id, rel) in &data.assets {
        if id == "bgm.at9" {
            input.bgm = src.join(rel).to_string_lossy().into_owned();
        } else if !id.starts_with("tbr") {
            input.sources.insert(id.clone(), src.join(rel).to_string_lossy().into_owned());
        }
    }
    input.colors = data.colors.clone();
    // Page 1's wave is the user's choice too (v2 always wrote 12).
    input.page_waves.insert(1, "5".into());
    assert!(validate(&input).is_empty(), "{:?}", validate(&input));
    let out = tmp("real");
    let warnings = build_theme(&out, &input, &|_, _, _| {}).unwrap();
    assert!(warnings.is_empty(), "{warnings:?}");
    let rebuilt = parse_theme_xml(&out.join("theme.xml"));
    assert_eq!(rebuilt.title, data.title);
    assert_eq!(rebuilt.page_waves.get("br1_wave").map(String::as_str), Some("5"));
    assert!(find_theme_problem(&out, &rebuilt).is_none());
    assert_eq!(rebuilt.assets.len(), data.assets.len(), "{:?} vs {:?}", rebuilt.assets.keys(), data.assets.keys());
    std::fs::remove_dir_all(&out).ok();
}

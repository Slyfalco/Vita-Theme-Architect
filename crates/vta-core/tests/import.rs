//! Import behaviour (v2 parity): folder, zip, invalid themes, bulk with nested zips.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;
use vta_core::import;
use vta_core::theme::{write_theme_xml, ThemeData};

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("vta-import-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

/// A minimal valid theme: required assets present.
fn make_theme(dir: &Path, title: &str) {
    std::fs::create_dir_all(dir).unwrap();
    let mut data = ThemeData { title: Some(title.into()), author: Some("T".into()), version: Some("01.00".into()), ..Default::default() };
    for a in ["preview_thumbnail.png", "preview_livearea.png", "preview_lockscreen.png", "br1.png", "lockpaper.png"] {
        data.assets.insert(a.into(), a.into());
        std::fs::write(dir.join(a), b"png").unwrap();
    }
    write_theme_xml(dir, &data).unwrap();
}

fn zip_dir(src: &Path, zip_path: &Path, prefix: &str) {
    let f = std::fs::File::create(zip_path).unwrap();
    let mut z = zip::ZipWriter::new(f);
    let opts = zip::write::SimpleFileOptions::default();
    for e in std::fs::read_dir(src).unwrap().flatten() {
        let name = format!("{prefix}{}", e.file_name().to_string_lossy());
        if e.path().is_file() {
            z.start_file(name, opts).unwrap();
            z.write_all(&std::fs::read(e.path()).unwrap()).unwrap();
        }
    }
    z.finish().unwrap();
}

#[test]
fn folder_and_zip_import() {
    let root = tmp("single");
    let lib = root.join("lib");
    std::fs::create_dir_all(&lib).unwrap();
    make_theme(&root.join("src"), "Alpha");
    assert_eq!(import::import_folder(&lib, &root.join("src")), Ok("Alpha".into()));
    assert!(lib.join("theme/theme.xml").is_file());

    zip_dir(&root.join("src"), &root.join("a.zip"), "");
    assert_eq!(import::import_zip(&lib, &root.join("a.zip")), Ok("Alpha".into()));
    assert!(lib.join("theme1/theme.xml").is_file());

    // Missing required asset: rejected and nothing left behind.
    std::fs::remove_file(root.join("src/lockpaper.png")).unwrap();
    zip_dir(&root.join("src"), &root.join("bad.zip"), "");
    let r = import::import_zip(&lib, &root.join("bad.zip"));
    assert!(matches!(r, Err((ref t, _)) if t == "Theme Required Assets Error"), "{r:?}");
    assert!(!lib.join("theme2").exists());
    std::fs::remove_dir_all(&root).ok();
}

#[test]
fn bulk_import_nested() {
    let root = tmp("bulk");
    let lib = root.join("lib");
    std::fs::create_dir_all(&lib).unwrap();
    let src = root.join("pack");
    make_theme(&src.join("one"), "One");
    make_theme(&root.join("two_src"), "Two");
    zip_dir(&root.join("two_src"), &src.join("two.zip"), "Two/");
    std::fs::create_dir_all(src.join("__MACOSX")).unwrap();
    std::fs::write(src.join("broken.zip"), b"not a zip").unwrap();

    let report = import::bulk_import(&lib, &src, &AtomicBool::new(false), |_, _| {});
    let mut imported = report.imported.clone();
    imported.sort();
    assert_eq!(imported, vec!["One".to_string(), "Two".to_string()]);
    assert_eq!(report.errors.len(), 1, "{:?}", report.errors);
    assert!(report.errors[0].starts_with("broken.zip: could not open zip"));
    assert!(lib.join("theme/theme.xml").is_file() && lib.join("theme1/theme.xml").is_file());
    std::fs::remove_dir_all(&root).ok();
}

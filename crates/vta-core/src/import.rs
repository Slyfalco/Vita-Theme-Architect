//! Importing themes into the library (v2 `import_zip_from_path`, `import_folder`,
//! `bulk_import`, `get_unique_theme_path`).

use crate::library::find_theme_problem;
use crate::theme::parse_theme_xml;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

/// v2 `get_unique_theme_path`: library folders are named `theme`, `theme1`, … (the
/// name the user gave is deliberately not used, like v2).
pub fn unique_theme_path(library: &Path) -> PathBuf {
    let base = library.join("theme");
    if !base.exists() {
        return base;
    }
    (1..)
        .map(|n| library.join(format!("theme{n}")))
        .find(|p| !p.exists())
        .expect("an unused folder name exists")
}

/// Extract a zip safely (no absolute paths or `..` escapes).
pub fn extract_zip(zip_path: &Path, dest: &Path) -> Result<(), String> {
    let file = std::fs::File::open(zip_path).map_err(|e| e.to_string())?;
    let mut archive = zip::ZipArchive::new(file).map_err(|e| e.to_string())?;
    std::fs::create_dir_all(dest).map_err(|e| e.to_string())?;
    for i in 0..archive.len() {
        let mut entry = archive.by_index(i).map_err(|e| e.to_string())?;
        let Some(rel) = entry.enclosed_name() else { continue };
        let out = dest.join(rel);
        if entry.is_dir() {
            std::fs::create_dir_all(&out).map_err(|e| e.to_string())?;
            continue;
        }
        if let Some(parent) = out.parent() {
            std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        let mut f = std::fs::File::create(&out).map_err(|e| e.to_string())?;
        std::io::copy(&mut entry, &mut f).map_err(|e| e.to_string())?;
    }
    Ok(())
}

pub fn copy_dir(src: &Path, dst: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dst)?;
    for entry in std::fs::read_dir(src)? {
        let entry = entry?;
        let to = dst.join(entry.file_name());
        if entry.file_type()?.is_dir() {
            copy_dir(&entry.path(), &to)?;
        } else {
            std::fs::copy(entry.path(), &to)?;
        }
    }
    Ok(())
}

/// Outcome of a single-theme import: the imported title, or (title, message) of the problem.
pub type ImportResult = Result<String, (String, String)>;

fn title_of(dir: &Path) -> String {
    parse_theme_xml(&dir.join("theme.xml"))
        .title
        .unwrap_or_else(|| dir.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default())
}

/// v2 `import_zip_from_path`: extract into a new library folder, then validate
/// (removing it again if invalid).
pub fn import_zip(library: &Path, zip: &Path) -> ImportResult {
    let target = unique_theme_path(library);
    if let Err(e) = extract_zip(zip, &target) {
        let _ = std::fs::remove_dir_all(&target);
        return Err(("Import Error".into(), format!("Failed to import zip: {e}")));
    }
    if !target.join("theme.xml").exists() {
        let _ = std::fs::remove_dir_all(&target);
        return Err(("Import Error".into(), "Theme is missing theme.xml.".into()));
    }
    let data = parse_theme_xml(&target.join("theme.xml"));
    if let Some(p) = find_theme_problem(&target, &data) {
        let _ = std::fs::remove_dir_all(&target);
        return Err((p.title, p.message));
    }
    Ok(title_of(&target))
}

/// v2 `import_folder`: validate in place, then copy into a new library folder.
pub fn import_folder(library: &Path, src: &Path) -> ImportResult {
    if !src.join("theme.xml").exists() {
        return Err(("Import Error".into(), "Theme is missing theme.xml.".into()));
    }
    let data = parse_theme_xml(&src.join("theme.xml"));
    if let Some(p) = find_theme_problem(src, &data) {
        return Err((p.title, p.message));
    }
    let target = unique_theme_path(library);
    copy_dir(src, &target).map_err(|e| ("Import Error".to_string(), format!("Failed to copy folder: {e}")))?;
    Ok(title_of(&target))
}

/// Find theme folders (holding theme.xml) under `root`, extracting zips found on
/// the way (up to two levels of zip-in-zip). Returns (theme dir, display label).
fn collect_theme_dirs(
    root: &Path,
    temp_root: &Path,
    errors: &mut Vec<String>,
    label_root: &str,
    depth: u32,
) -> Vec<(PathBuf, String)> {
    let mut out = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let rel = dir.strip_prefix(root).unwrap_or(Path::new("")).to_string_lossy().replace('\\', "/");
        let label = if rel.is_empty() {
            if label_root.is_empty() {
                root.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
            } else {
                label_root.to_string()
            }
        } else if label_root.is_empty() {
            rel.clone()
        } else {
            format!("{label_root}/{rel}")
        };
        if dir.join("theme.xml").is_file() {
            out.push((dir, label));
            continue; // don't descend into a theme
        }
        let Ok(entries) = std::fs::read_dir(&dir) else { continue };
        let mut entries: Vec<_> = entries.flatten().collect();
        entries.sort_by_key(|e| e.file_name());
        for entry in entries.iter().rev() {
            let name = entry.file_name().to_string_lossy().into_owned();
            let path = entry.path();
            if path.is_dir() {
                if name != "__MACOSX" && !name.starts_with('.') {
                    stack.push(path);
                }
            }
        }
        if depth >= 2 {
            continue;
        }
        for entry in &entries {
            let name = entry.file_name().to_string_lossy().into_owned();
            if !name.to_lowercase().ends_with(".zip") || name.starts_with("._") || !entry.path().is_file() {
                continue;
            }
            let zip_label = if rel.is_empty() && label_root.is_empty() { name.clone() } else { format!("{label}/{name}") };
            let extract = temp_root.join(format!("z{}", out.len() + errors.len() + stack.len() + depth as usize * 1000 + entries.len()));
            let extract = unique_dir(&extract);
            if let Err(e) = extract_zip(&entry.path(), &extract) {
                errors.push(format!("{zip_label}: could not open zip ({e})"));
                continue;
            }
            let found = collect_theme_dirs(&extract, temp_root, errors, &zip_label, depth + 1);
            if found.is_empty() {
                errors.push(format!("{zip_label}: no theme.xml found"));
            }
            out.extend(found);
        }
    }
    out
}

fn unique_dir(base: &Path) -> PathBuf {
    let mut p = base.to_path_buf();
    let mut n = 0;
    while p.exists() {
        n += 1;
        p = PathBuf::from(format!("{}_{n}", base.display()));
    }
    p
}

#[derive(Debug, Default)]
pub struct BulkReport {
    pub imported: Vec<String>,
    pub errors: Vec<String>,
    pub found_any: bool,
}

/// v2 `bulk_import`: import every theme found in a folder or zip (themes may be
/// folders or zips, nested up to two zip levels). `progress(done, total)`.
pub fn bulk_import(
    library: &Path,
    source: &Path,
    cancel: &AtomicBool,
    mut progress: impl FnMut(usize, usize),
) -> BulkReport {
    let temp_root = std::env::temp_dir().join(format!("vta_bulk_{}_{}", std::process::id(), std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0)));
    let _ = std::fs::create_dir_all(&temp_root);
    let mut report = BulkReport::default();
    let dirs = if source.is_dir() {
        collect_theme_dirs(source, &temp_root, &mut report.errors, "", 0)
    } else {
        // A single zip is treated like a folder holding that zip.
        let zip_dir = temp_root.join("source");
        let _ = std::fs::create_dir_all(&zip_dir);
        let copied = source.file_name().map(|n| zip_dir.join(n));
        match copied.and_then(|to| std::fs::copy(source, &to).ok()) {
            Some(_) => collect_theme_dirs(&zip_dir, &temp_root, &mut report.errors, "", 0),
            None => {
                report.errors.push(format!("{}: could not read zip", source.display()));
                Vec::new()
            }
        }
    };
    report.found_any = !dirs.is_empty();
    let total = dirs.len();
    for (i, (dir, label)) in dirs.iter().enumerate() {
        if cancel.load(Ordering::SeqCst) {
            report.errors.push("Import cancelled by user.".into());
            break;
        }
        progress(i, total);
        let data = parse_theme_xml(&dir.join("theme.xml"));
        if let Some(p) = find_theme_problem(dir, &data) {
            report.errors.push(format!("{label}: {}", p.message));
            continue;
        }
        match copy_dir(dir, &unique_theme_path(library)) {
            Ok(()) => report.imported.push(data.title.clone().unwrap_or_else(|| label.clone())),
            Err(e) => report.errors.push(format!("{label}: {e}")),
        }
    }
    progress(total, total);
    let _ = std::fs::remove_dir_all(&temp_root);
    report
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unique_names_follow_v2() {
        let dir = std::env::temp_dir().join(format!("vta-uniq-{}", std::process::id()));
        std::fs::create_dir_all(dir.join("theme")).unwrap();
        std::fs::create_dir_all(dir.join("theme1")).unwrap();
        assert_eq!(unique_theme_path(&dir), dir.join("theme2"));
        std::fs::remove_dir_all(&dir).ok();
    }
}

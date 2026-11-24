//! The local theme library: scanning, validity checks, sorting and filtering
//! (v2's `load_themes_from_library`, `find_theme_problem`, `sort_and_display_local_themes`).

use crate::theme::{parse_theme_xml, ThemeData};
use std::path::{Path, PathBuf};

/// One theme folder in the library.
#[derive(Debug, Clone)]
pub struct LibraryTheme {
    pub path: PathBuf,
    pub folder_name: String,
    pub data: ThemeData,
    /// Total size of the theme folder in bytes.
    pub size_bytes: u64,
}

impl LibraryTheme {
    pub fn title(&self) -> &str {
        self.data.title.as_deref().unwrap_or(&self.folder_name)
    }
    pub fn author(&self) -> &str {
        self.data.author.as_deref().unwrap_or("Unknown")
    }
    pub fn version(&self) -> &str {
        self.data.version.as_deref().unwrap_or("00.00")
    }
    /// Absolute path of an asset, if the theme lists it.
    pub fn asset_path(&self, id: &str) -> Option<PathBuf> {
        self.data.assets.get(id).filter(|p| !p.is_empty()).map(|p| self.path.join(p))
    }
    pub fn has_bgm(&self) -> bool {
        self.asset_path("bgm.at9").is_some_and(|p| p.is_file())
    }
    /// Number of home pages (br1..br10 present).
    pub fn page_count(&self) -> usize {
        (1..=10).filter(|i| self.data.assets.contains_key(&format!("br{i}.png"))).count()
    }
    /// Number of custom system icons.
    pub fn icon_count(&self) -> usize {
        crate::theme::ICONS.iter().filter(|(_, id)| self.data.assets.contains_key(*id)).count()
    }
}

/// Why a theme folder was left out of the library (v2 showed these as error dialogs).
#[derive(Debug, Clone, PartialEq)]
pub struct ThemeProblem {
    pub folder: PathBuf,
    pub title: String,
    pub message: String,
}

/// v2 `find_theme_problem`: required metadata/assets present and every listed asset exists.
pub fn find_theme_problem(theme_path: &Path, data: &ThemeData) -> Option<ThemeProblem> {
    let name = theme_path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    let problem = |title: &str, message: String| {
        Some(ThemeProblem { folder: theme_path.to_path_buf(), title: title.into(), message })
    };
    const REQUIRED: [&str; 5] =
        ["preview_thumbnail.png", "preview_livearea.png", "preview_lockscreen.png", "br1.png", "lockpaper.png"];
    let required: Vec<Option<&String>> = REQUIRED.iter().map(|id| data.assets.get(*id)).collect();
    if required.iter().any(Option::is_none)
        || data.title.is_none()
        || data.version.is_none()
        || data.author.is_none()
    {
        return problem("Theme XML Error", format!("The xml file in {name} is invalid."));
    }
    let missing: Vec<&str> = required
        .iter()
        .flatten()
        .filter(|f| !theme_path.join(f.as_str()).exists())
        .map(|f| f.as_str())
        .collect();
    if !missing.is_empty() {
        return problem(
            "Theme Required Assets Error",
            format!("Assets in theme xml for {name} are missing:\n{}", missing.join("\n ")),
        );
    }
    let missing: Vec<&str> = data
        .assets
        .values()
        .filter(|f| !theme_path.join(f.as_str()).exists())
        .map(String::as_str)
        .collect();
    if !missing.is_empty() {
        return problem(
            "Theme Assets Error",
            format!("Assets in theme xml for {name} are missing:\n{}", missing.join("\n ")),
        );
    }
    None
}

fn dir_size(path: &Path) -> u64 {
    let Ok(entries) = std::fs::read_dir(path) else { return 0 };
    entries
        .flatten()
        .map(|e| match e.metadata() {
            Ok(m) if m.is_dir() => dir_size(&e.path()),
            Ok(m) => m.len(),
            Err(_) => 0,
        })
        .sum()
}

/// Scan the library folder. Folders without a `theme.xml` are ignored; invalid
/// themes are left out and reported.
pub fn scan(library: &Path) -> (Vec<LibraryTheme>, Vec<ThemeProblem>) {
    let mut themes = Vec::new();
    let mut problems = Vec::new();
    let Ok(entries) = std::fs::read_dir(library) else { return (themes, problems) };
    let mut dirs: Vec<PathBuf> = entries.flatten().map(|e| e.path()).filter(|p| p.is_dir()).collect();
    dirs.sort();
    for path in dirs {
        let xml = path.join("theme.xml");
        if !xml.exists() {
            continue;
        }
        let data = parse_theme_xml(&xml);
        if let Some(p) = find_theme_problem(&path, &data) {
            problems.push(p);
            continue;
        }
        let folder_name = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        let size_bytes = dir_size(&path);
        themes.push(LibraryTheme { path, folder_name, data, size_bytes });
    }
    (themes, problems)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SortMode {
    TitleAsc,
    TitleDesc,
    AuthorAsc,
    AuthorDesc,
    VersionDesc,
    VersionAsc,
}

impl SortMode {
    pub const ALL: [SortMode; 6] = [
        SortMode::TitleAsc,
        SortMode::TitleDesc,
        SortMode::AuthorAsc,
        SortMode::AuthorDesc,
        SortMode::VersionDesc,
        SortMode::VersionAsc,
    ];
    /// The v2 combo-box labels.
    pub fn label(self) -> &'static str {
        match self {
            SortMode::TitleAsc => "Alphabetical (A-Z)",
            SortMode::TitleDesc => "Alphabetical (Z-A)",
            SortMode::AuthorAsc => "Author (A-Z)",
            SortMode::AuthorDesc => "Author (Z-A)",
            SortMode::VersionDesc => "Version (High-Low)",
            SortMode::VersionAsc => "Version (Low-High)",
        }
    }
}

/// v2 `version_sort_key`: integer parts, or `[0, 0]` if any part isn't a number.
fn version_key(v: &str) -> Vec<i64> {
    let parts: Result<Vec<i64>, _> = v.split('.').map(|p| p.trim().parse::<i64>()).collect();
    parts.unwrap_or_else(|_| vec![0, 0])
}

/// Stable sort, like Python's `list.sort` (reverse=True keeps equal items in order).
pub fn sort(themes: &mut [LibraryTheme], mode: SortMode) {
    let lower = |s: &str| s.to_lowercase();
    match mode {
        SortMode::TitleAsc => themes.sort_by_key(|t| lower(t.title())),
        SortMode::TitleDesc => themes.sort_by(|a, b| lower(b.title()).cmp(&lower(a.title()))),
        SortMode::AuthorAsc => themes.sort_by_key(|t| lower(t.author())),
        SortMode::AuthorDesc => themes.sort_by(|a, b| lower(b.author()).cmp(&lower(a.author()))),
        SortMode::VersionDesc => themes.sort_by(|a, b| version_key(b.version()).cmp(&version_key(a.version()))),
        SortMode::VersionAsc => themes.sort_by_key(|t| version_key(t.version())),
    }
}

/// Case-insensitive substring search on title and author.
pub fn matches(theme: &LibraryTheme, search: &str) -> bool {
    let s = search.trim().to_lowercase();
    s.is_empty() || theme.title().to_lowercase().contains(&s) || theme.author().to_lowercase().contains(&s)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_keys() {
        assert!(version_key("01.10") > version_key("01.02"));
        assert_eq!(version_key("x.1"), vec![0, 0]);
    }
}

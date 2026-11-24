//! The generator rules the Gemini path and the community downloads reuse.
//! Same values as v2 `icon_ai_worker.py` (`ICON_SUBJECTS`, `TEXT_REGIONS`,
//! `CHARACTER_ICONS`, `example_sets`) and `icon_generator.py`
//! (`THEME_XML_ICON_TAGS`, `ICON_NAMES`, the example/template locations).

use std::path::{Path, PathBuf};

/// v2 `icon_generator.ICON_NAMES`.
pub const ICON_NAMES: [&str; 17] = [
    "icon_web.png", "icon_calendar.png", "icon_photos.png", "icon_email.png", "icon_friend.png",
    "icon_cma.png", "icon_message.png", "icon_music.png", "icon_near.png", "icon_parental.png",
    "icon_party.png", "icon_power.png", "icon_ps3link.png", "icon_ps4link.png", "icon_settings.png",
    "icon_trophy.png", "icon_video.png",
];

/// v2 `icon_ai_worker.ICON_SUBJECTS`: what each icon is, in prompts.
pub const ICON_SUBJECTS: [(&str, &str); 17] = [
    ("icon_web.png", "web browser globe"),
    ("icon_calendar.png", "calendar"),
    ("icon_photos.png", "photo camera"),
    ("icon_email.png", "email envelope"),
    ("icon_friend.png", "friends list faces"),
    ("icon_cma.png", "content manager files"),
    ("icon_message.png", "chat message bubbles"),
    ("icon_music.png", "music note"),
    ("icon_near.png", "location radar"),
    ("icon_parental.png", "parental controls"),
    ("icon_party.png", "voice chat headset"),
    ("icon_power.png", "power button"),
    ("icon_ps3link.png", "remote play handheld console"),
    ("icon_ps4link.png", "remote play game controller"),
    ("icon_settings.png", "settings toolbox"),
    ("icon_trophy.png", "trophy cup"),
    ("icon_video.png", "video play button"),
];

/// `ICON_SUBJECTS.get(name, default)`.
pub fn icon_subject<'a>(name: &str, default: &'a str) -> &'a str {
    ICON_SUBJECTS.iter().find(|(n, _)| *n == name).map(|(_, s)| *s).unwrap_or(default)
}

/// A v2 `TEXT_REGIONS` entry: (box in 128px template coordinates, style, text).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TextRegion {
    pub rect: (u32, u32, u32, u32),
    pub style: &'static str,
    pub text: &'static str,
}

/// v2 `icon_ai_worker.TEXT_REGIONS`.
pub const TEXT_REGIONS: [(&str, TextRegion); 3] = [
    ("icon_web.png", TextRegion { rect: (18, 55, 110, 76), style: "label", text: "www" }),
    ("icon_calendar.png", TextRegion { rect: (34, 32, 96, 63), style: "panel", text: "31" }),
    ("icon_ps4link.png", TextRegion { rect: (44, 98, 86, 118), style: "label", text: "PS4" }),
];

pub fn text_region(name: &str) -> Option<&'static TextRegion> {
    TEXT_REGIONS.iter().find(|(n, _)| *n == name).map(|(_, r)| r)
}

/// v2 `icon_ai_worker.CHARACTER_ICONS`.
pub const CHARACTER_ICONS: [&str; 8] = [
    "icon_friend.png", "icon_party.png", "icon_message.png", "icon_near.png",
    "icon_music.png", "icon_photos.png", "icon_video.png", "icon_trophy.png",
];

pub fn is_character_icon(name: &str) -> bool {
    CHARACTER_ICONS.contains(&name)
}

/// v2 `THEME_XML_ICON_TAGS`: theme.xml HomeProperty tag -> icon asset name (in v2's order).
pub const THEME_XML_ICON_TAGS: [(&str, &str); 17] = [
    ("m_browser", "icon_web.png"), ("m_calendar", "icon_calendar.png"), ("m_camera", "icon_photos.png"),
    ("m_email", "icon_email.png"), ("m_friend", "icon_friend.png"), ("m_hostCollabo", "icon_cma.png"),
    ("m_message", "icon_message.png"), ("m_music", "icon_music.png"), ("m_near", "icon_near.png"),
    ("m_parental", "icon_parental.png"), ("m_party", "icon_party.png"), ("m_power", "icon_power.png"),
    ("m_ps3Link", "icon_ps3link.png"), ("m_ps4Link", "icon_ps4link.png"), ("m_settings", "icon_settings.png"),
    ("m_trophy", "icon_trophy.png"), ("m_video", "icon_video.png"),
];

/// v2 `COMMUNITY_EXAMPLE_TARGET`: community icon sets kept.
pub const COMMUNITY_EXAMPLE_TARGET: usize = 24;
/// v2 `COMMUNITY_EXAMPLES_PER_OPEN`: sets added per dialog opening.
pub const COMMUNITY_EXAMPLES_PER_OPEN: usize = 4;

/// v2 `_templates_dir()`: `<app dir>/Assets/IconTemplates`, if present.
pub fn templates_dir(app_dir: &Path) -> Option<PathBuf> {
    let p = app_dir.join("Assets").join("IconTemplates");
    p.is_dir().then_some(p)
}

/// v2 `_examples_dir()`: the built-in example sets, `<app dir>/Assets/IconExamples`, if present.
pub fn builtin_examples_dir(app_dir: &Path) -> Option<PathBuf> {
    let p = app_dir.join("Assets").join("IconExamples");
    p.is_dir().then_some(p)
}

/// v2 `EXAMPLES_DIR`: `<BASE_SUPPORT_DIR>/icon_ai/examples` (shared per user).
pub fn examples_dir(base_support: &Path) -> PathBuf {
    base_support.join("icon_ai").join("examples")
}

/// v2 `EXTRA_ART_DIR`: `<APP_SUPPORT_DIR>/icon_ai/extra_artwork`.
pub fn extra_art_dir(app_support: &Path) -> PathBuf {
    app_support.join("icon_ai").join("extra_artwork")
}

/// v2 `icon_ai_worker.example_sets`: (set name, folder) of every example icon
/// set, each set once; an earlier root (the built-in copy) wins.
pub fn example_sets(examples_dirs: &[Option<PathBuf>]) -> Vec<(String, PathBuf)> {
    let mut seen = std::collections::HashSet::new();
    let mut sets = Vec::new();
    for root in examples_dirs.iter().flatten() {
        let Ok(rd) = std::fs::read_dir(root) else { continue };
        let mut names: Vec<String> = rd.flatten().map(|e| e.file_name().to_string_lossy().into_owned()).collect();
        names.sort();
        for name in names {
            let dir = root.join(&name);
            if dir.is_dir() && seen.insert(name.clone()) {
                sets.push((name, dir));
            }
        }
    }
    sets
}

/// v2 `example_set_count()`: sets built into the app plus downloaded, each once.
pub fn example_set_count(builtin: Option<&Path>, examples: &Path) -> usize {
    example_sets(&[builtin.map(Path::to_path_buf), Some(examples.to_path_buf())]).len()
}

/// v2 job `examples_dirs`: `[d for d in (_examples_dir(), EXAMPLES_DIR) if d and isdir(d)]`.
pub fn job_examples_dirs(builtin: Option<&Path>, examples: &Path) -> Vec<PathBuf> {
    [builtin.map(Path::to_path_buf), Some(examples.to_path_buf())]
        .into_iter()
        .flatten()
        .filter(|d| d.is_dir())
        .collect()
}

/// v2 `fetch_examples` gate: how many sets to fetch on this dialog opening
/// (`min(missing, COMMUNITY_EXAMPLES_PER_OPEN)`), or 0 when nothing should run
/// (online features unavailable, or the target is reached).
pub fn examples_to_fetch(builtin: Option<&Path>, examples: &Path) -> usize {
    let have = example_set_count(builtin, examples);
    if !vta_online::available() || have >= COMMUNITY_EXAMPLE_TARGET {
        return 0;
    }
    (COMMUNITY_EXAMPLE_TARGET - have).min(COMMUNITY_EXAMPLES_PER_OPEN)
}

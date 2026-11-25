//! Community themes as icon examples and as art (v2 `ExampleDownloadThread`,
//! `matching_community_themes`, `community_theme_art`). Release builds only:
//! the online list comes from `vta_online`.

use super::http;
use super::images;
use super::py::{self, PyError, PyRandom};
use super::rules;
use serde_json::Value;
use std::collections::{BTreeSet, HashMap};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use vta_online::{Entry, Field, Sort};

/// v2 `COMMUNITY_ART_THEMES`: matching community themes used.
pub const COMMUNITY_ART_THEMES: usize = 3;
/// v2 `COMMUNITY_ART_PER_THEME`: wallpapers taken from each.
pub const COMMUNITY_ART_PER_THEME: usize = 6;
/// v2 `_TITLE_FILLER`: words that don't identify a theme.
pub const TITLE_FILLER: [&str; 23] = [
    "theme", "themes", "ps", "vita", "psvita", "download", "themedownload", "dynamic", "custom",
    "the", "a", "an", "of", "and", "by", "for", "hd", "edition", "version", "new", "my", "fixed", "test",
];

/// v2 `constants.theme_pages(search, field, sort, max_pages)`: the online list
/// a page at a time until a short page (or `max_pages`).
pub fn theme_pages(search: &str, field: Field, sort: Sort, max_pages: Option<usize>) -> Result<Vec<Entry>, String> {
    let mut out = Vec::new();
    let mut page = 1;
    while max_pages.is_none_or(|m| page <= m) {
        let found = vta_online::get(search, field, vta_online::Filter::All, sort, page, vta_online::PAGE, false)?;
        let n = found.len();
        out.extend(found);
        if n < vta_online::PAGE {
            break;
        }
        page += 1;
    }
    Ok(out)
}

/// v2 `_title_words(text)`: the identifying words of a name.
pub fn title_words(text: &str) -> BTreeSet<String> {
    static RE: std::sync::OnceLock<(regex::Regex, regex::Regex)> = std::sync::OnceLock::new();
    let (word, skip) = RE.get_or_init(|| (regex::Regex::new(r"[a-z0-9]+").unwrap(), regex::Regex::new(r"^(?:v[0-9]+|theme[0-9]*)$").unwrap()));
    let lower = text.to_lowercase();
    word.find_iter(&lower)
        .map(|m| m.as_str())
        .filter(|w| !TITLE_FILLER.contains(w) && w.len() > 1 && !w.bytes().all(|b| b.is_ascii_digit()) && !skip.is_match(w))
        .map(str::to_string)
        .collect()
}

fn is_word_char(c: char) -> bool {
    c.is_ascii_lowercase() || c.is_ascii_digit()
}

fn is_decimal(c: char) -> bool {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let re = RE.get_or_init(|| regex::Regex::new(r"^\d$").unwrap());
    let mut buf = [0u8; 4];
    re.is_match(c.encode_utf8(&mut buf))
}

/// v2 `_title_numbers(text)`: the numbers in a name (`(?<![a-z0-9])\d+(?![a-z0-9])`,
/// matched with Python's backtracking semantics).
pub fn title_numbers(text: &str) -> BTreeSet<String> {
    let chars: Vec<char> = text.to_lowercase().chars().collect();
    let mut out = BTreeSet::new();
    let mut i = 0;
    while i < chars.len() {
        if is_decimal(chars[i]) && (i == 0 || !is_word_char(chars[i - 1])) {
            let mut run = i;
            while run < chars.len() && is_decimal(chars[run]) {
                run += 1;
            }
            // Greedy, then shorter, until the next character isn't [a-z0-9].
            if let Some(end) = (i + 1..=run).rev().find(|&e| e == chars.len() || !is_word_char(chars[e])) {
                out.insert(chars[i..end].iter().collect());
                i = end;
                continue;
            }
        }
        i += 1;
    }
    out
}

fn downloads(e: &Entry) -> i64 {
    e.num("downloads")
}

fn has(e: &Entry, key: &str) -> bool {
    py::truthy(e.0.get(key))
}

/// v2 `matching_community_themes(title, themes, limit)`: community themes about
/// the same thing, best first. A one-word name needs a theme titled exactly
/// that word; longer names need two shared words (title or tags). Ranked by
/// shared numbers, then shared words, then downloads (stable on ties).
pub fn matching_community_themes(title: &str, themes: &[Entry], limit: usize) -> Vec<Entry> {
    let words = title_words(title);
    if words.is_empty() {
        return Vec::new();
    }
    let numbers = title_numbers(title);
    let mut scored: Vec<((usize, usize, i64), &Entry)> = Vec::new();
    for theme in themes {
        let t_title = theme.str("title");
        let shared: BTreeSet<String> = if words.len() == 1 {
            if title_words(&t_title) == words { words.clone() } else { BTreeSet::new() }
        } else {
            let mut theirs = title_words(&t_title);
            theirs.extend(title_words(&theme.str("tags")));
            let s: BTreeSet<String> = words.intersection(&theirs).cloned().collect();
            if s.len() < 2 { BTreeSet::new() } else { s }
        };
        let id_present = matches!(theme.0.get("id"), Some(v) if !v.is_null());
        if !shared.is_empty() && has(theme, "zip") && id_present {
            let mut theirs = title_numbers(&t_title);
            theirs.extend(title_numbers(&theme.str("tags")));
            let same_number = numbers.intersection(&theirs).count();
            scored.push(((same_number, shared.len(), downloads(theme)), theme));
        }
    }
    scored.sort_by(|a, b| b.0.cmp(&a.0));
    scored.into_iter().take(limit).map(|(_, t)| t.clone()).collect()
}

/// `str(theme['id'])` / `str(theme.get('id', ''))`.
fn id_str(e: &Entry) -> String {
    py::str_or(e.0.get("id"), "")
}

/// Download a theme zip into memory (`requests.get(secure_url(zip), timeout=60)`).
fn fetch_zip(e: &Entry) -> Result<zip::ZipArchive<std::io::Cursor<Vec<u8>>>, PyError> {
    let resp = http::get(&vta_online::secure_url(&e.str("zip")), &[], 60)?;
    resp.raise_for_status()?;
    zip::ZipArchive::new(std::io::Cursor::new(resp.body)).map_err(|e| PyError::new("BadZipFile", e.to_string()))
}

fn read_member(archive: &mut zip::ZipArchive<std::io::Cursor<Vec<u8>>>, name: &str) -> Result<Vec<u8>, PyError> {
    let mut f = archive.by_name(name).map_err(|e| PyError::new("BadZipFile", e.to_string()))?;
    let mut buf = Vec::new();
    f.read_to_end(&mut buf).map_err(|e| PyError::new("BadZipFile", e.to_string()))?;
    Ok(buf)
}

fn member_names(archive: &zip::ZipArchive<std::io::Cursor<Vec<u8>>>) -> Vec<String> {
    archive.file_names().filter_map(|n| n.ok().map(|n| n.into_owned())).collect()
}

/// v2 `json.dump({'title': ..., 'author': ...}, f)`.
fn write_source_json(dir: &Path, e: &Entry) -> std::io::Result<()> {
    let mut m = serde_json::Map::new();
    m.insert("title".into(), e.0.get("title").cloned().unwrap_or(Value::Null));
    m.insert("author".into(), e.0.get("author").cloned().unwrap_or(Value::Null));
    std::fs::write(dir.join("source.json"), py::json_dumps(&Value::Object(m)))
}

/// The custom icons of a theme zip (v2 `ExampleDownloadThread._download`, per
/// theme): the icons its theme.xml names, as 128x128 RGB, minus those that are
/// the stock template. `Ok(None)` when the zip has no usable theme.xml.
/// `Err` only when theme.xml can't be read from the zip (v2 stops the whole
/// download then).
pub fn custom_icons_in_zip(
    archive: &mut zip::ZipArchive<std::io::Cursor<Vec<u8>>>,
    templates_dir: &Path,
) -> Result<Option<Vec<(&'static str, image::RgbImage)>>, PyError> {
    let mut names: HashMap<String, String> = HashMap::new();
    for n in member_names(archive) {
        if !n.ends_with('/') {
            names.insert(py::basename(&n).to_lowercase(), n);
        }
    }
    let Some(xml_member) = names.get("theme.xml").cloned() else { return Ok(None) };
    let bytes = read_member(archive, &xml_member)?;
    let Some(text) = crate::theme::decode_xml(&bytes) else { return Ok(None) };
    let options = roxmltree::ParsingOptions { allow_dtd: true, ..Default::default() };
    let Ok(doc) = roxmltree::Document::parse_with_options(&text, options) else { return Ok(None) };
    fn kids<'a, 'i>(node: roxmltree::Node<'a, 'i>, name: &'static str) -> impl Iterator<Item = roxmltree::Node<'a, 'i>> {
        node.children().filter(move |c| c.is_element() && c.tag_name().namespace().is_none() && c.tag_name().name() == name)
    }
    let mut icons: Vec<(&'static str, image::RgbImage)> = Vec::new();
    for (tag, icon_name) in rules::THEME_XML_ICON_TAGS {
        // ElementTree's find('./HomeProperty/{tag}/m_iconFilePath'): the first match.
        let el = kids(doc.root_element(), "HomeProperty")
            .flat_map(|h| kids(h, tag))
            .flat_map(|t| kids(t, "m_iconFilePath"))
            .next();
        let Some(text) = el.and_then(|e| e.text()).filter(|t| !t.is_empty()) else { continue };
        let Some(member) = names.get(&py::basename(text).to_lowercase()).cloned() else { continue };
        let Ok(data) = read_member(archive, &member) else { continue };
        let Ok(img) = images::open_bytes(&data) else { continue };
        icons.push((icon_name, images::resize_lanczos(&img.to_rgb8(), 128, 128)));
    }
    // Only real custom sets: not the stock symbols.
    Ok(Some(icons.into_iter().filter(|(n, img)| !images::looks_like_template(img, &templates_dir.join(n))).collect()))
}

/// v2 `ExampleDownloadThread._download`: adds up to `wanted` community icon
/// sets (themes with at least 12 custom icons, from the most downloaded 80 of
/// the first two pages, in random order) to `examples_dir/<theme id>/` as
/// 32-colour PNGs plus `source.json`. Sets already built in or downloaded are
/// skipped. `progress(percent, text)` as v2's progress signal. `Ok(message)` /
/// `Err(message)` are v2's `finished_ok` (the dialog logs
/// "Community icon sets: {message}" either way).
pub fn download_examples(
    wanted: usize,
    builtin_examples: Option<&Path>,
    examples_dir: &Path,
    templates_dir: &Path,
    cancel: &AtomicBool,
    progress: &mut dyn FnMut(i32, String),
) -> Result<String, String> {
    download_examples_inner(wanted, builtin_examples, examples_dir, templates_dir, cancel, progress).map_err(|e| e.to_string())
}

fn download_examples_inner(
    wanted: usize,
    builtin_examples: Option<&Path>,
    examples_dir: &Path,
    templates_dir: &Path,
    cancel: &AtomicBool,
    progress: &mut dyn FnMut(i32, String),
) -> Result<String, PyError> {
    progress(0, "Getting the community theme list...".into());
    // The most downloaded themes (two pages of the online list).
    let themes = theme_pages("", Field::Title, Sort::Downloads, Some(2)).map_err(|e| PyError::new("AttributeError", e))?;
    // Sets already shipped with the app or downloaded before are skipped.
    let have: std::collections::HashSet<String> =
        rules::example_sets(&[builtin_examples.map(Path::to_path_buf), Some(examples_dir.to_path_buf())])
            .into_iter()
            .map(|(n, _)| n)
            .collect();
    // Popular themes first, shuffled within that pool for variety.
    let mut candidates = themes;
    candidates.sort_by_key(|t| std::cmp::Reverse(downloads(t)));
    candidates.truncate(80);
    PyRandom::from_time().shuffle(&mut candidates);
    let mut added = 0usize;
    for theme in &candidates {
        if cancel.load(Ordering::Relaxed) || added >= wanted {
            break;
        }
        let theme_id = id_str(theme);
        if theme_id.is_empty() || have.contains(&theme_id) || !has(theme, "zip") {
            continue;
        }
        progress((added * 100 / wanted.max(1)) as i32, format!("Checking \"{}\"...", py::str_or(theme.0.get("title"), "?")));
        let Ok(mut archive) = fetch_zip(theme) else { continue };
        let Some(custom) = custom_icons_in_zip(&mut archive, templates_dir)? else { continue };
        if custom.len() < 12 {
            continue;
        }
        let out_dir = examples_dir.join(&theme_id);
        let saved = (|| -> Result<(), PyError> {
            std::fs::create_dir_all(&out_dir).map_err(|e| PyError::new("OSError", e.to_string()))?;
            for (icon_name, img) in &custom {
                images::save_median_cut_png(img, 32, &out_dir.join(icon_name))?;
            }
            write_source_json(&out_dir, theme).map_err(|e| PyError::new("OSError", e.to_string()))
        })();
        if saved.is_err() {
            // One bad theme must not stop the rest; drop its partial folder.
            let _ = std::fs::remove_dir_all(&out_dir);
            continue;
        }
        added += 1;
    }
    Ok(if added > 0 { format!("Added {added} community example set(s).") } else { "No new community icon sets found.".into() })
}

/// The wallpapers of a theme zip into `folder` (v2 `community_theme_art`, per
/// theme): `lockpaper`/`brN` png/jpg members, the first 6 by name, as JPEGs
/// (quality 90, at most 1024 px) named `<stem>.jpg`, plus `source.json`.
pub fn save_theme_wallpapers(
    archive: &mut zip::ZipArchive<std::io::Cursor<Vec<u8>>>,
    folder: &Path,
    theme: &Entry,
) -> Result<(), PyError> {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let re = RE.get_or_init(|| regex::Regex::new(r"^(?:lockpaper|br\d+)\.(?:png|jpe?g)$").unwrap());
    let mut names: Vec<String> = member_names(archive).into_iter().filter(|n| re.is_match(&py::basename(n).to_lowercase())).collect();
    names.sort();
    std::fs::create_dir_all(folder).map_err(|e| PyError::new("OSError", e.to_string()))?;
    for n in names.iter().take(COMMUNITY_ART_PER_THEME) {
        let img = images::open_bytes(&read_member(archive, n)?)?;
        let img = images::thumbnail(image::DynamicImage::ImageRgb8(img.to_rgb8()), 1024).to_rgb8();
        let stem = py::splitext_stem(py::basename(n));
        images::save_jpeg(&img, &folder.join(format!("{stem}.jpg")), 90)?;
    }
    write_source_json(folder, theme).map_err(|e| PyError::new("OSError", e.to_string()))
}

/// v2 `community_theme_art(title, dest, cancelled, log)`: wallpapers of the
/// community themes matching this theme's name, saved under `dest/<id>/` (a
/// temporary folder the caller deletes after the generation). Empty when
/// online features are unavailable or nothing matches.
pub fn community_theme_art(title: &str, dest: &Path, cancelled: &dyn Fn() -> bool, log: &mut dyn FnMut(String)) -> Vec<PathBuf> {
    if !vta_online::available() || title_words(title).is_empty() {
        return Vec::new();
    }
    // Search the online list for each word of the name (the most downloaded
    // matches first); matching_community_themes then applies the strict rules.
    let words = title_words(title);
    let field = if words.len() == 1 { Field::Title } else { Field::TitleTags };
    let mut themes = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for word in &words {
        match theme_pages(word, field, Sort::Downloads, Some(2)) {
            Ok(found) => {
                for theme in found {
                    let id = theme.0.get("id").map(py::repr).unwrap_or_else(|| "None".into());
                    if seen.insert(id) {
                        themes.push(theme);
                    }
                }
            }
            Err(e) => {
                log(format!("Community theme list unavailable: {e}"));
                return Vec::new();
            }
        }
    }
    let mut paths = Vec::new();
    for theme in matching_community_themes(title, &themes, COMMUNITY_ART_THEMES) {
        if cancelled() {
            break;
        }
        let folder = dest.join(id_str(&theme));
        if !folder.is_dir() {
            let result = fetch_zip(&theme).and_then(|mut archive| save_theme_wallpapers(&mut archive, &folder, &theme));
            if let Err(e) = result {
                let _ = std::fs::remove_dir_all(&folder);
                log(format!("Couldn't use community theme \"{}\": {}", py::str_or(theme.0.get("title"), "None"), e.message));
                continue;
            }
        }
        let mut found: Vec<PathBuf> = std::fs::read_dir(&folder)
            .map(|rd| {
                rd.flatten()
                    .filter(|e| e.file_name().to_string_lossy().ends_with(".jpg"))
                    .map(|e| folder.join(e.file_name()))
                    .collect()
            })
            .unwrap_or_default();
        found.sort();
        log(format!(
            "Using art from the community theme \"{}\" ({} pictures).",
            py::str_or(theme.0.get("title"), "None"),
            found.len()
        ));
        paths.extend(found);
    }
    paths
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn entry(v: Value) -> Entry {
        match v {
            Value::Object(m) => Entry(m),
            _ => unreachable!(),
        }
    }

    fn set(items: &[&str]) -> BTreeSet<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn words_and_numbers() {
        assert_eq!(title_words("Persona 5 Royal PS Vita Theme v2"), set(&["persona", "royal"]));
        assert_eq!(title_words("The Legend of Zelda: HD theme3"), set(&["legend", "zelda"]));
        assert_eq!(title_numbers("Persona 5 Royal v2 2B 3"), set(&["5", "3"]));
        assert_eq!(title_numbers("FF7-Remake 15!"), set(&["15"]));
    }

    #[test]
    fn matching_rules() {
        let themes: Vec<Entry> = vec![
            entry(json!({"id": 1, "title": "Persona 3 Reload", "tags": "", "zip": "z", "downloads": 900})),
            entry(json!({"id": 2, "title": "Persona 5 Royal", "tags": "", "zip": "z", "downloads": 10})),
            entry(json!({"id": 3, "title": "Persona Royal", "tags": "p5", "zip": "z", "downloads": "50"})),
            entry(json!({"id": 4, "title": "Royal Persona", "tags": "", "zip": "", "downloads": 5000})),
            entry(json!({"id": 5, "title": "Snow Party", "tags": "persona royal", "zip": "z", "downloads": 1})),
        ];
        let ids = |r: Vec<Entry>| r.iter().map(|e| e.num("id")).collect::<Vec<_>>();
        assert_eq!(ids(matching_community_themes("Persona 5 Royal", &themes, 3)), vec![2, 3, 5]);
        let snow = vec![
            entry(json!({"id": 1, "title": "Snow", "zip": "z"})),
            entry(json!({"id": 2, "title": "Snow Miku", "zip": "z", "downloads": 99})),
            entry(json!({"id": 3, "title": "Snow theme", "zip": "z", "downloads": 5})),
        ];
        assert_eq!(ids(matching_community_themes("snow", &snow, 3)), vec![3, 1]);
        assert!(matching_community_themes("PS Vita Theme", &snow, 3).is_empty());
    }

    fn zip_with(files: &[(&str, Vec<u8>)]) -> zip::ZipArchive<std::io::Cursor<Vec<u8>>> {
        let mut buf = std::io::Cursor::new(Vec::new());
        {
            let mut w = zip::ZipWriter::new(&mut buf);
            for (name, data) in files {
                w.start_file(*name, zip::write::SimpleFileOptions::default()).unwrap();
                std::io::Write::write_all(&mut w, data).unwrap();
            }
            w.finish().unwrap();
        }
        zip::ZipArchive::new(std::io::Cursor::new(buf.into_inner())).unwrap()
    }

    fn png_bytes(w: u32, h: u32, f: impl Fn(u32, u32) -> [u8; 3]) -> Vec<u8> {
        let img = image::RgbImage::from_fn(w, h, |x, y| image::Rgb(f(x, y)));
        let mut out = Vec::new();
        img.write_to(&mut std::io::Cursor::new(&mut out), image::ImageFormat::Png).unwrap();
        out
    }

    #[test]
    fn icons_from_theme_zip() {
        let templates = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../Assets/IconTemplates");
        let stock = std::fs::read(templates.join("icon_power.png")).unwrap();
        let xml = r#"<?xml version="1.0" encoding="utf-8"?><theme><HomeProperty>
            <m_browser><m_iconFilePath>icons/Web.PNG</m_iconFilePath></m_browser>
            <m_power><m_iconFilePath>power.png</m_iconFilePath></m_power>
            <m_music><m_iconFilePath>missing.png</m_iconFilePath></m_music>
            </HomeProperty></theme>"#;
        let mut archive = zip_with(&[
            ("Theme/theme.xml", xml.as_bytes().to_vec()),
            ("Theme/icons/web.png", png_bytes(64, 64, |x, y| [(x * 4) as u8, (y * 4) as u8, 77])),
            ("Theme/power.png", stock),
        ]);
        let icons = custom_icons_in_zip(&mut archive, &templates).unwrap().unwrap();
        assert_eq!(icons.len(), 1);
        assert_eq!(icons[0].0, "icon_web.png");
        assert_eq!(icons[0].1.dimensions(), (128, 128));
        let mut no_xml = zip_with(&[("a.png", png_bytes(2, 2, |_, _| [0, 0, 0]))]);
        assert!(custom_icons_in_zip(&mut no_xml, &templates).unwrap().is_none());
    }

    #[test]
    fn wallpapers_from_theme_zip() {
        let dir = std::env::temp_dir().join(format!("vta_cart_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let mut archive = zip_with(&[
            ("x/br2.png", png_bytes(1920, 1080, |_, _| [10, 20, 30])),
            ("x/BR1.PNG", png_bytes(10, 10, |_, _| [1, 2, 3])),
            ("x/lockpaper.jpg", png_bytes(4, 4, |_, _| [9, 9, 9])),
            ("x/icon_web.png", png_bytes(4, 4, |_, _| [9, 9, 9])),
            ("x/br10.gif", png_bytes(4, 4, |_, _| [9, 9, 9])),
        ]);
        let theme = entry(json!({"id": 7, "title": "Café", "author": null}));
        save_theme_wallpapers(&mut archive, &dir, &theme).unwrap();
        let mut names: Vec<String> = std::fs::read_dir(&dir).unwrap().flatten().map(|e| e.file_name().to_string_lossy().into_owned()).collect();
        names.sort();
        assert_eq!(names, vec!["BR1.jpg", "br2.jpg", "lockpaper.jpg", "source.json"]);
        assert_eq!(image::open(dir.join("br2.jpg")).unwrap().width(), 1024);
        assert_eq!(std::fs::read_to_string(dir.join("source.json")).unwrap(), "{\"title\": \"Caf\\u00e9\", \"author\": null}");
        let _ = std::fs::remove_dir_all(&dir);
    }
}

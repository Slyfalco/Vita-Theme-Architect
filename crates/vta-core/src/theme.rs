//! The theme data model and the official `theme.xml` reader/writer.
//!
//! This is a behaviour-identical port of the v2 app's `parse_theme_xml` /
//! `write_theme_xml` (main_window.py). The output must stay byte-identical, because
//! themes are installed on real PS Vita hardware: the schema (including Sony's
//! `InfomationProperty` spelling), the fixed asset file names and the AARRGGBB colour
//! encoding are not ours to change. `tests/golden.rs` checks this against output
//! recorded from the Python (v2) app.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::Path;

/// A theme as the app works with it (the v2 `theme_data` dict).
///
/// Map semantics follow Python's `dict.get(key, default)`: a key that is present
/// with an empty value is *not* replaced by the default, it is written as an
/// empty element. That's why these are maps rather than fixed fields.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ThemeData {
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub author: Option<String>,
    #[serde(default)]
    pub version: Option<String>,
    /// Asset id (e.g. `br1.png`, `bgm.at9`, `icon_web.png`) -> file path relative to the theme folder.
    #[serde(default)]
    pub assets: BTreeMap<String, String>,
    /// Colour tag (`m_barColor`, ..., plus the global `m_fontColor`) -> AARRGGBB.
    #[serde(default)]
    pub colors: BTreeMap<String, String>,
    /// `br<N>_color` -> AARRGGBB, used when `use_global_font_color` is false.
    #[serde(default)]
    pub page_colors: BTreeMap<String, String>,
    /// `br<N>_wave` -> wave type number.
    #[serde(default)]
    pub page_waves: BTreeMap<String, String>,
    #[serde(default = "default_true")]
    pub use_global_font_color: bool,
}

fn default_true() -> bool {
    true
}

impl Default for ThemeData {
    fn default() -> Self {
        ThemeData {
            title: None,
            author: None,
            version: None,
            assets: BTreeMap::new(),
            colors: BTreeMap::new(),
            page_colors: BTreeMap::new(),
            page_waves: BTreeMap::new(),
            use_global_font_color: true,
        }
    }
}

/// What `parse_theme_xml` hands back for a file that is missing or isn't XML
/// (the v2 "Invalid Theme" placeholder).
pub fn invalid_theme() -> ThemeData {
    ThemeData {
        title: Some("Invalid Theme".into()),
        author: Some("N/A".into()),
        version: Some("00.00".into()),
        ..ThemeData::default()
    }
}

/// The 17 system app icons: (`HomeProperty` child tag, asset id), in schema order.
pub const ICONS: [(&str, &str); 17] = [
    ("m_browser", "icon_web.png"),
    ("m_calendar", "icon_calendar.png"),
    ("m_camera", "icon_photos.png"),
    ("m_email", "icon_email.png"),
    ("m_friend", "icon_friend.png"),
    ("m_hostCollabo", "icon_cma.png"),
    ("m_message", "icon_message.png"),
    ("m_music", "icon_music.png"),
    ("m_near", "icon_near.png"),
    ("m_parental", "icon_parental.png"),
    ("m_party", "icon_party.png"),
    ("m_power", "icon_power.png"),
    ("m_ps3Link", "icon_ps3link.png"),
    ("m_ps4Link", "icon_ps4link.png"),
    ("m_settings", "icon_settings.png"),
    ("m_trophy", "icon_trophy.png"),
    ("m_video", "icon_video.png"),
];

/// The colour tags read from a theme (colour id, element path).
const COLOR_TAGS: [(&str, &[&str]); 8] = [
    ("m_dateColor", &["StartScreenProperty", "m_dateColor"]),
    ("m_notifyBgColor", &["StartScreenProperty", "m_notifyBgColor"]),
    ("m_notifyBorderColor", &["StartScreenProperty", "m_notifyBorderColor"]),
    ("m_notifyFontColor", &["StartScreenProperty", "m_notifyFontColor"]),
    ("m_barColor", &["InfomationBarProperty", "m_barColor"]),
    ("m_indicatorColor", &["InfomationBarProperty", "m_indicatorColor"]),
    ("m_noticeFontColor", &["InfomationBarProperty", "m_noticeFontColor"]),
    ("m_noticeGlowColor", &["InfomationBarProperty", "m_noticeGlowColor"]),
];

const LANGS: [&str; 12] = [
    "m_da", "m_de", "m_es", "m_fi", "m_fr", "m_it", "m_nl", "m_no", "m_pl", "m_pt", "m_sv", "m_ja",
];

// ---------------------------------------------------------------------------
// Reading
// ---------------------------------------------------------------------------

/// One step of an ElementTree-style path: a child tag, optionally `[n]` (1-based
/// position among same-tag siblings).
#[derive(Clone, Copy)]
struct Step<'a> {
    tag: &'a str,
    index: Option<usize>,
}

fn steps<'a>(tags: &[&'a str]) -> Vec<Step<'a>> {
    tags.iter().map(|t| Step { tag: t, index: None }).collect()
}

/// ElementTree `findtext`: the text of the first element matching `path` (searched in
/// document order across every branch), `Some("")` for a match with no text, `None`
/// when nothing matches. Like `Element.text`, only the text before the first child counts.
fn findtext(root: roxmltree::Node, path: &[Step]) -> Option<String> {
    fn walk(node: roxmltree::Node, path: &[Step]) -> Option<String> {
        let Some((step, rest)) = path.split_first() else {
            return Some(leading_text(node));
        };
        let mut pos = 0;
        for child in node.children().filter(|c| c.is_element()) {
            if child.tag_name().name() != step.tag {
                continue;
            }
            pos += 1;
            if step.index.is_some_and(|i| i != pos) {
                continue;
            }
            if let Some(found) = walk(child, rest) {
                return Some(found);
            }
        }
        None
    }
    walk(root, path)
}

/// `Element.text`: the character data before the element's first child element.
fn leading_text(node: roxmltree::Node) -> String {
    let mut out = String::new();
    for child in node.children() {
        if child.is_element() {
            break;
        }
        if let Some(t) = child.text() {
            out.push_str(t);
        }
    }
    out
}

/// v2 colour normalisation: 6 hex digits (after removing leading `#`) gain an `ff` alpha.
fn normalise_color(value: String) -> String {
    let stripped = value.trim_start_matches('#');
    if stripped.chars().count() == 6 {
        format!("ff{stripped}")
    } else {
        value
    }
}

/// Decode a theme.xml file to text the way expat would: honour a BOM, else the XML
/// declaration's encoding, else UTF-8.
pub(crate) fn decode_xml(bytes: &[u8]) -> Option<String> {
    if let Some((enc, bom_len)) = encoding_rs::Encoding::for_bom(bytes) {
        let (text, had_errors) = enc.decode_without_bom_handling(&bytes[bom_len..]);
        return (!had_errors).then(|| text.into_owned());
    }
    // Look for <?xml ... encoding="..."?> in the ASCII-compatible prefix.
    let head = &bytes[..bytes.len().min(200)];
    let head = String::from_utf8_lossy(head);
    let declared = head.strip_prefix("<?xml").and_then(|decl| {
        let decl = &decl[..decl.find("?>")?];
        let at = decl.find("encoding")?;
        let rest = decl[at + "encoding".len()..].trim_start().strip_prefix('=')?.trim_start();
        let quote = rest.chars().next().filter(|c| *c == '"' || *c == '\'')?;
        let rest = &rest[1..];
        Some(rest[..rest.find(quote)?].to_string())
    });
    let enc = match declared {
        Some(label) => encoding_rs::Encoding::for_label(label.as_bytes())?,
        None => encoding_rs::UTF_8,
    };
    let (text, had_errors) = enc.decode_without_bom_handling(bytes);
    (!had_errors).then(|| text.into_owned())
}

/// Parse theme.xml text. `None` if it isn't well-formed XML.
pub fn parse_theme_xml_str(text: &str) -> Option<ThemeData> {
    let options = roxmltree::ParsingOptions { allow_dtd: true, ..Default::default() };
    let doc = roxmltree::Document::parse_with_options(text, options).ok()?;
    let root = doc.root_element();
    let text_at = |tags: &[&str]| findtext(root, &steps(tags));

    let mut data = ThemeData {
        title: text_at(&["InfomationProperty", "m_title", "m_default"]),
        author: text_at(&["InfomationProperty", "m_provider", "m_default"]),
        version: text_at(&["InfomationProperty", "m_contentVer"]),
        ..ThemeData::default()
    };

    let bg_param = |i: usize, leaf: &'static str| -> Option<String> {
        let path = [
            Step { tag: "HomeProperty", index: None },
            Step { tag: "m_bgParam", index: None },
            Step { tag: "BackgroundParam", index: Some(i) },
            Step { tag: leaf, index: None },
        ];
        findtext(root, &path)
    };

    let mut asset_paths: Vec<(String, Option<String>)> = vec![
        ("lockpaper.png".into(), text_at(&["StartScreenProperty", "m_filePath"])),
        ("bgm.at9".into(), text_at(&["HomeProperty", "m_bgmFilePath"])),
        ("preview_livearea.png".into(), text_at(&["InfomationProperty", "m_homePreviewFilePath"])),
        ("preview_lockscreen.png".into(), text_at(&["InfomationProperty", "m_startPreviewFilePath"])),
        ("preview_thumbnail.png".into(), text_at(&["InfomationProperty", "m_packageImageFilePath"])),
    ];
    for i in 1..=10 {
        asset_paths.push((format!("br{i}.png"), bg_param(i, "m_imageFilePath")));
    }
    for (tag, asset) in ICONS {
        asset_paths.push((asset.into(), text_at(&["HomeProperty", tag, "m_iconFilePath"])));
    }
    asset_paths.push(("screen_other.png".into(), text_at(&["HomeProperty", "m_basePageFilePath"])));
    asset_paths.push(("screen_current.png".into(), text_at(&["HomeProperty", "m_curPageFilePath"])));
    asset_paths.push(("noNotice.png".into(), text_at(&["InfomationBarProperty", "m_noNoticeFilePath"])));
    asset_paths.push(("newNotice.png".into(), text_at(&["InfomationBarProperty", "m_newNoticeFilePath"])));
    for (name, found) in asset_paths {
        if let Some(path) = found.filter(|p| !p.is_empty()) {
            data.assets.insert(name, path);
        }
    }

    for (name, tags) in COLOR_TAGS {
        if let Some(color) = text_at(tags).filter(|c| !c.is_empty()) {
            data.colors.insert(name.into(), normalise_color(color));
        }
    }

    for i in 1..=10 {
        if let Some(color) = bg_param(i, "m_fontColor").filter(|c| !c.is_empty()) {
            data.page_colors.insert(format!("br{i}_color"), normalise_color(color));
        }
        if let Some(wave) = bg_param(i, "m_waveType").filter(|w| !w.is_empty()) {
            data.page_waves.insert(format!("br{i}_wave"), wave);
        }
    }
    Some(data)
}

/// Read a theme folder's `theme.xml`. A missing or malformed file gives the v2
/// "Invalid Theme" placeholder, exactly like the Python app.
pub fn parse_theme_xml(xml_path: &Path) -> ThemeData {
    std::fs::read(xml_path)
        .ok()
        .and_then(|bytes| decode_xml(&bytes))
        .and_then(|text| parse_theme_xml_str(&text))
        .unwrap_or_else(invalid_theme)
}

// ---------------------------------------------------------------------------
// Writing
// ---------------------------------------------------------------------------

/// A minimal element tree, serialised the way Python's
/// `minidom.toprettyxml(indent="\t")` does for the shapes theme.xml uses
/// (leaf elements with text, or containers; never mixed content).
struct El {
    name: &'static str,
    attrs: &'static [(&'static str, &'static str)],
    text: String,
    children: Vec<El>,
}

impl El {
    fn new(name: &'static str) -> Self {
        El { name, attrs: &[], text: String::new(), children: Vec::new() }
    }
    /// `create_sub_element(parent, name, text)`: empty text leaves the element empty.
    fn leaf(&mut self, name: &'static str, text: &str) {
        let mut el = El::new(name);
        el.text = text.to_string();
        self.children.push(el);
    }
    fn child(&mut self, name: &'static str) -> &mut El {
        self.children.push(El::new(name));
        self.children.last_mut().unwrap()
    }
    fn localized(&mut self, name: &'static str, text: &str) {
        let el = self.child(name);
        el.leaf("m_default", text);
        let param = el.child("m_param");
        for lang in LANGS {
            param.leaf(lang, text);
        }
    }
    fn write(&self, out: &mut String, depth: usize) {
        let indent = "\t".repeat(depth);
        out.push_str(&indent);
        out.push('<');
        out.push_str(self.name);
        for (k, v) in self.attrs {
            out.push_str(&format!(" {k}=\"{}\"", escape(v)));
        }
        if !self.children.is_empty() {
            out.push_str(">\n");
            for c in &self.children {
                c.write(out, depth + 1);
            }
            out.push_str(&indent);
            out.push_str(&format!("</{}>\n", self.name));
        } else if !self.text.is_empty() {
            out.push_str(&format!(">{}</{}>\n", escape(&self.text), self.name));
        } else {
            out.push_str("/>\n");
        }
    }
}

/// minidom's text/attribute escaping: `& < " >` (apostrophes are left alone).
fn escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '"' => out.push_str("&quot;"),
            '>' => out.push_str("&gt;"),
            _ => out.push(c),
        }
    }
    out
}

/// Python `dict.get(key, default)`.
fn get<'a>(map: &'a BTreeMap<String, String>, key: &str, default: &'a str) -> &'a str {
    map.get(key).map(String::as_str).unwrap_or(default)
}

/// Build the official theme.xml text (LF newlines), identical to the v2 writer.
pub fn theme_xml_string(data: &ThemeData) -> String {
    let title = data.title.as_deref().unwrap_or("Untitled");
    let author = data.author.as_deref().unwrap_or("Unknown");
    let version = data.version.as_deref().unwrap_or("01.00");
    let (assets, colors) = (&data.assets, &data.colors);
    let asset = |k: &str| assets.get(k).filter(|v| !v.is_empty()).map(String::as_str);

    let mut root = El::new("theme");
    root.attrs = &[("format-ver", "01.00"), ("package", "0")];

    let home = root.child("HomeProperty");
    let bg_param = home.child("m_bgParam");
    let br1_path = get(assets, "br1.png", "br1.png");
    let tbr1_path = get(assets, "tbr1.png", "tbr1.png");
    let global_font_color = get(colors, "m_fontColor", "ffffffff");
    for i in 1..=10 {
        let br = asset(&format!("br{i}.png"));
        if let Some(br_path) = br {
            let tbr_path = asset(&format!("tbr{i}.png")).unwrap_or(tbr1_path);
            let font_color = if data.use_global_font_color {
                global_font_color
            } else {
                get(&data.page_colors, &format!("br{i}_color"), global_font_color)
            };
            let wave = get(&data.page_waves, &format!("br{i}_wave"), "12");
            let bg = bg_param.child("BackgroundParam");
            bg.leaf("m_imageFilePath", br_path);
            bg.leaf("m_thumbnailFilePath", tbr_path);
            bg.leaf("m_waveType", wave);
            bg.leaf("m_fontColor", font_color);
            bg.leaf("m_fontShadow", "1");
        } else if i == 1 {
            // Page 1 always exists.
            let bg = bg_param.child("BackgroundParam");
            bg.leaf("m_imageFilePath", br1_path);
            bg.leaf("m_thumbnailFilePath", tbr1_path);
            bg.leaf("m_waveType", get(&data.page_waves, "br1_wave", "12"));
            bg.leaf("m_fontColor", global_font_color);
            bg.leaf("m_fontShadow", "1");
        }
    }
    if let Some(bgm) = asset("bgm.at9") {
        home.leaf("m_bgmFilePath", bgm);
    }
    for (tag, icon) in ICONS {
        if let Some(path) = asset(icon) {
            home.child(tag).leaf("m_iconFilePath", path);
        }
    }
    if let Some(p) = asset("screen_other.png") {
        home.leaf("m_basePageFilePath", p);
    }
    if let Some(p) = asset("screen_current.png") {
        home.leaf("m_curPageFilePath", p);
    }

    let bar = root.child("InfomationBarProperty");
    bar.leaf("m_barColor", get(colors, "m_barColor", "ff000000"));
    bar.leaf("m_indicatorColor", get(colors, "m_indicatorColor", "ffffffff"));
    bar.leaf("m_noticeFontColor", get(colors, "m_noticeFontColor", "ffffffff"));
    bar.leaf("m_noticeGlowColor", get(colors, "m_noticeGlowColor", "ffca0000"));
    if let Some(p) = asset("noNotice.png") {
        bar.leaf("m_noNoticeFilePath", p);
    }
    if let Some(p) = asset("newNotice.png") {
        bar.leaf("m_newNoticeFilePath", p);
    }

    let info = root.child("InfomationProperty");
    info.localized("m_provider", author);
    info.leaf("m_contentVer", version);
    info.localized("m_title", title);
    info.leaf("m_homePreviewFilePath", get(assets, "preview_livearea.png", "preview_livearea.png"));
    info.leaf("m_startPreviewFilePath", get(assets, "preview_lockscreen.png", "preview_lockscreen.png"));
    info.leaf("m_packageImageFilePath", get(assets, "preview_thumbnail.png", "preview_thumbnail.png"));

    let start = root.child("StartScreenProperty");
    start.leaf("m_dateColor", get(colors, "m_dateColor", "ffffffff"));
    start.leaf("m_dateLayout", "0");
    start.leaf("m_filePath", get(assets, "lockpaper.png", "lockpaper.png"));
    start.leaf("m_notifyBgColor", get(colors, "m_notifyBgColor", "ffffffff"));
    start.leaf("m_notifyBorderColor", get(colors, "m_notifyBorderColor", "ffcccccc"));
    start.leaf("m_notifyFontColor", get(colors, "m_notifyFontColor", "ffffffff"));

    let mut out = String::from("<?xml version=\"1.0\" encoding=\"utf-8\"?>\n");
    root.write(&mut out, 0);
    out
}

/// Write `<theme_folder>/theme.xml`. Like the v2 app (which wrote in text mode), the
/// file uses the platform's newline convention, so output is byte-identical to v2 on
/// every OS.
pub fn write_theme_xml(theme_folder: &Path, data: &ThemeData) -> std::io::Result<()> {
    let mut text = theme_xml_string(data);
    if cfg!(windows) {
        text = text.replace('\n', "\r\n");
    }
    std::fs::write(theme_folder.join("theme.xml"), text)
}

//! Extra artwork search (v2 `ImageSearchDialog` and its helpers): Safebooru
//! search + tag autocomplete, Bing image results (SafeSearch strict), the
//! thumbnail loader, and saving the ticked pictures into `extra_artwork`.

use super::http;
use super::images;
use super::py::{self, PyError};
use serde_json::Value;
use sha1::{Digest, Sha1};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

/// v2 `SEARCH_API` (Safebooru JSON API).
pub const SEARCH_API: &str = "https://safebooru.org/index.php";
/// v2 `SUGGEST_API` (Safebooru tag autocomplete).
pub const SUGGEST_API: &str = "https://safebooru.org/autocomplete.php";
/// v2 `SEARCH_PAGE_SIZE`: results per page ("More Results" shows only when a page is full).
pub const SEARCH_PAGE_SIZE: usize = 40;
/// v2 `SEARCH_HEADERS`.
pub const SEARCH_HEADERS: [(&str, &str); 1] = [("User-Agent", "VitaThemeArchitect icon generator")];
/// v2 `WEB_SEARCH_URL` (Bing's image results page).
pub const WEB_SEARCH_URL: &str = "https://www.bing.com/images/async";
/// v2 `WEB_HEADERS`.
pub const WEB_HEADERS: [(&str, &str); 2] = [
    ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    ("Accept-Language", "en-US,en;q=0.9"),
];
/// v2 `IMAGE_SOURCES`: (label, source key).
pub const IMAGE_SOURCES: [(&str, &str); 2] = [("Anime art (Safebooru)", "booru"), ("Web (Bing images)", "web")];

/// One search result (v2's result dict).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct SearchResult {
    /// Shown as `#{id}`; the saved file is `<id>.png`.
    pub id: String,
    pub thumb: String,
    pub image: String,
    /// Tooltip (v2 shows the first 300 characters).
    pub tags: String,
    /// Safebooru only.
    pub width: Option<Value>,
    pub height: Option<Value>,
}

/// v2 `search_images(terms, page, solo, plain_background)`: Safebooru results
/// rated general/safe. Comma-separated terms become tags (spaces -> `_`).
pub fn search_images(terms: &str, page: usize, solo: bool, plain_background: bool) -> Result<Vec<SearchResult>, PyError> {
    let url = py::url_with_params(SEARCH_API, &booru_params(terms, page, solo, plain_background));
    let resp = http::get(&url, &SEARCH_HEADERS, 20)?;
    resp.raise_for_status()?;
    let text = resp.text();
    if text.trim().is_empty() {
        return Ok(Vec::new());
    }
    parse_booru(&resp.json()?)
}

/// The query of [`search_images`], in v2's parameter order.
pub fn booru_params(terms: &str, page: usize, solo: bool, plain_background: bool) -> Vec<(&'static str, String)> {
    let mut tags = terms
        .split(',')
        .filter(|t| !t.trim().is_empty())
        .map(|t| t.trim().replace(' ', "_"))
        .collect::<Vec<_>>()
        .join(" ");
    if solo {
        tags.push_str(" solo");
    }
    if plain_background {
        tags.push_str(" simple_background");
    }
    vec![
        ("page", "dapi".into()),
        ("s", "post".into()),
        ("q", "index".into()),
        ("json", "1".into()),
        ("limit", SEARCH_PAGE_SIZE.to_string()),
        ("pid", page.to_string()),
        ("tags", tags),
    ]
}

fn key_error(key: &str) -> PyError {
    PyError::new("KeyError", py::repr_str(key))
}

fn get<'a>(p: &'a Value, key: &str) -> Result<Option<&'a Value>, PyError> {
    match p {
        Value::Object(m) => Ok(m.get(key)),
        other => Err(PyError::new("AttributeError", format!("'{}' object has no attribute 'get'", py::type_name(other)))),
    }
}

/// The result list from Safebooru's JSON answer.
pub fn parse_booru(posts: &Value) -> Result<Vec<SearchResult>, PyError> {
    let items: Vec<Value> = match posts {
        Value::Array(a) => a.clone(),
        Value::Object(m) => m.keys().map(|k| Value::String(k.clone())).collect(),
        other => return Err(PyError::new("TypeError", format!("'{}' object is not iterable", py::type_name(other)))),
    };
    let mut out = Vec::new();
    for p in &items {
        let rating = get(p, "rating")?;
        if !matches!(rating, Some(Value::String(r)) if r == "general" || r == "safe") {
            continue;
        }
        let required = |k: &str| -> Result<String, PyError> { get(p, k)?.map(py::str_of).ok_or_else(|| key_error(k)) };
        let id = required("id")?;
        let thumb = required("preview_url")?;
        let sample = get(p, "sample_url")?;
        let image = if py::truthy(sample) { py::str_of(sample.unwrap()) } else { required("file_url")? };
        out.push(SearchResult {
            id,
            thumb,
            image,
            tags: py::str_or(get(p, "tags")?, ""),
            width: get(p, "width")?.cloned(),
            height: get(p, "height")?.cloned(),
        });
    }
    Ok(out)
}

/// v2 `search_web_images(terms, page, solo, plain_background)`: Bing image
/// results (SafeSearch strict, no key; unofficial page parsing).
pub fn search_web_images(terms: &str, page: usize, solo: bool, plain_background: bool) -> Result<Vec<SearchResult>, PyError> {
    let url = py::url_with_params(WEB_SEARCH_URL, &web_params(terms, page, solo, plain_background));
    let resp = http::get(&url, &WEB_HEADERS, 20)?;
    resp.raise_for_status()?;
    parse_bing(&resp.text())
}

/// The query of [`search_web_images`], in v2's parameter order.
pub fn web_params(terms: &str, page: usize, solo: bool, plain_background: bool) -> Vec<(&'static str, String)> {
    let query = format!(
        "{terms}{}{}",
        if solo { " character" } else { "" },
        if plain_background { " transparent png render" } else { "" }
    );
    vec![
        ("q", query),
        ("first", (page * SEARCH_PAGE_SIZE).to_string()),
        ("count", SEARCH_PAGE_SIZE.to_string()),
        ("adlt", "strict".into()),
        ("mmasync", "1".into()),
    ]
}

/// The results in a Bing image results page (each `m="{json}"` attribute with
/// `murl` and `turl`).
pub fn parse_bing(html: &str) -> Result<Vec<SearchResult>, PyError> {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let re = RE.get_or_init(|| regex::Regex::new(r#"\bm="([^"]+)""#).unwrap());
    let mut found = Vec::new();
    for c in re.captures_iter(html) {
        let text = htmlize::unescape(&c[1]);
        let Ok(d) = serde_json::from_str::<Value>(&text) else { continue };
        let murl = get(&d, "murl")?;
        if !(py::truthy(murl) && py::truthy(get(&d, "turl")?)) {
            continue;
        }
        let murl = match murl {
            Some(Value::String(s)) => s.clone(),
            Some(other) => {
                return Err(PyError::new("AttributeError", format!("'{}' object has no attribute 'encode'", py::type_name(other))))
            }
            None => unreachable!(),
        };
        let digest = Sha1::digest(murl.as_bytes());
        let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
        found.push(SearchResult {
            id: hex[..12].to_string(),
            thumb: py::str_of(get(&d, "turl")?.unwrap()),
            image: murl,
            tags: py::str_or(get(&d, "t")?, ""),
            width: None,
            height: None,
        });
    }
    Ok(found)
}

/// Run the search for a source key of [`IMAGE_SOURCES`] (`web` = Bing, else Safebooru).
pub fn search(source: &str, terms: &str, page: usize, solo: bool, plain_background: bool) -> Result<Vec<SearchResult>, PyError> {
    if source == "web" {
        search_web_images(terms, page, solo, plain_background)
    } else {
        search_images(terms, page, solo, plain_background)
    }
}

/// v2 `fetch_suggestions`: the part of the query to suggest tags for (after
/// the last comma, stripped), or `None` when it's shorter than 2 characters.
pub fn suggestion_fragment(query: &str) -> Option<String> {
    let text = query.rsplit(',').next().unwrap_or("").trim().to_string();
    (text.chars().count() >= 2).then_some(text)
}

/// v2 `suggest_tags(text)`: up to 15 Safebooru tag suggestions.
pub fn suggest_tags(text: &str) -> Result<Vec<String>, PyError> {
    let url = py::url_with_params(SUGGEST_API, &[("q", text.trim().replace(' ', "_"))]);
    let resp = http::get(&url, &SEARCH_HEADERS, 10)?;
    resp.raise_for_status()?;
    parse_suggestions(&resp.json()?)
}

/// The suggestion values from the autocomplete answer (first 15).
pub fn parse_suggestions(data: &Value) -> Result<Vec<String>, PyError> {
    let Value::Array(items) = data else {
        return Err(PyError::new("TypeError", format!("'{}' object is not iterable", py::type_name(data))));
    };
    let mut values = Vec::new();
    for item in items {
        let v = match item {
            Value::Object(m) => m.get("value").ok_or_else(|| key_error("value"))?,
            other => return Err(PyError::new("TypeError", format!("'{}' object is not subscriptable", py::type_name(other)))),
        };
        values.push(py::str_of(v));
    }
    values.truncate(15);
    Ok(values)
}

/// v2 `ThumbnailLoader.run`: fetch each thumbnail in turn and report the
/// successful ones as (index, bytes); failures are skipped silently.
pub fn load_thumbnails(urls: &[String], cancel: &AtomicBool, loaded: &mut dyn FnMut(usize, Vec<u8>)) {
    for (i, url) in urls.iter().enumerate() {
        if cancel.load(Ordering::Relaxed) {
            return;
        }
        if let Ok(resp) = http::get(url, &SEARCH_HEADERS, 15) {
            if resp.ok() {
                loaded(i, resp.body);
            }
        }
    }
}

/// v2 `ImageSearchDialog.add_ticked` download: each ticked picture is saved as
/// `<extra_art_dir>/<id>.png` (re-saved through the image decoder, so only real
/// images are kept; an existing file is reused). Pictures that fail are
/// skipped; an error when none could be saved.
pub fn download_ticked(ticked: &[SearchResult], extra_art_dir: &Path) -> Result<Vec<PathBuf>, PyError> {
    std::fs::create_dir_all(extra_art_dir).map_err(|e| PyError::new("OSError", e.to_string()))?;
    let mut paths = Vec::new();
    for r in ticked {
        let path = extra_art_dir.join(format!("{}.png", r.id));
        if !path.is_file() {
            let saved = (|| -> Result<(), PyError> {
                let resp = http::get(&r.image, &WEB_HEADERS, 60)?;
                resp.raise_for_status()?;
                resave_png(&resp.body, &path)
            })();
            if saved.is_err() {
                let _ = std::fs::remove_file(&path);
                continue;
            }
        }
        paths.push(path);
    }
    if paths.is_empty() {
        return Err(PyError::new("RuntimeError", "none of the ticked pictures could be downloaded"));
    }
    Ok(paths)
}

/// `Image.open(bytes).save(path, 'PNG')`: decode and write as PNG (transparency kept).
pub fn resave_png(bytes: &[u8], path: &Path) -> Result<(), PyError> {
    let img = images::open_bytes(bytes)?;
    img.save_with_format(path, image::ImageFormat::Png).map_err(|e| PyError::new("OSError", e.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn booru_query_matches_v2() {
        let url = py::url_with_params(SEARCH_API, &booru_params(" lucy (elfen lied), elfen lied ,", 2, true, true));
        assert_eq!(
            url,
            "https://safebooru.org/index.php?page=dapi&s=post&q=index&json=1&limit=40&pid=2&tags=lucy_%28elfen_lied%29+elfen_lied+solo+simple_background"
        );
        let url = py::url_with_params(SEARCH_API, &booru_params("", 0, false, false));
        assert!(url.ends_with("&pid=0&tags="));
    }

    #[test]
    fn web_query_matches_v2() {
        let url = py::url_with_params(WEB_SEARCH_URL, &web_params("Rem & Ram", 1, true, false));
        assert_eq!(url, "https://www.bing.com/images/async?q=Rem+%26+Ram+character&first=40&count=40&adlt=strict&mmasync=1");
    }

    #[test]
    fn booru_results() {
        let posts = serde_json::json!([
            {"id": 5, "rating": "general", "preview_url": "p5", "sample_url": "", "file_url": "f5", "tags": "a b", "width": 10, "height": 20},
            {"id": 6, "rating": "questionable", "preview_url": "p6", "file_url": "f6"},
            {"id": 7, "rating": "safe", "preview_url": "p7", "sample_url": "s7", "file_url": "f7"}
        ]);
        let r = parse_booru(&posts).unwrap();
        assert_eq!(r.len(), 2);
        assert_eq!((r[0].id.as_str(), r[0].image.as_str(), r[0].tags.as_str()), ("5", "f5", "a b"));
        assert_eq!(r[0].width, Some(serde_json::json!(10)));
        assert_eq!((r[1].image.as_str(), r[1].tags.as_str()), ("s7", ""));
        let missing = serde_json::json!([{"id": 1, "rating": "safe", "file_url": "x"}]);
        assert_eq!(parse_booru(&missing).unwrap_err().to_string(), "KeyError: 'preview_url'");
    }

    #[test]
    fn bing_results() {
        // Shape of Bing's async results page (attributes HTML-escaped).
        let html = r#"<div class="imgpt"><a class="iusc" style="" m="{&quot;cid&quot;:&quot;x&quot;,&quot;murl&quot;:&quot;https://example.com/a.png?x=1&amp;y=2&quot;,&quot;turl&quot;:&quot;https://tse1.mm.bing.net/th?id=OIP.a&amp;pid=15.1&quot;,&quot;t&quot;:&quot;Lucy &amp;#39;render&amp;#39;&quot;}" href="/images/search">x</a>
            <a m="{&quot;murl&quot;:&quot;&quot;,&quot;turl&quot;:&quot;t&quot;}"></a><a m="not json"></a><a data-m="{&quot;murl&quot;:&quot;u&quot;,&quot;turl&quot;:&quot;t&quot;}"></a></div>"#;
        let r = parse_bing(html).unwrap();
        assert_eq!(r.len(), 2, "{r:?}");
        assert_eq!(r[0].image, "https://example.com/a.png?x=1&y=2");
        assert_eq!(r[0].thumb, "https://tse1.mm.bing.net/th?id=OIP.a&pid=15.1");
        assert_eq!(r[0].tags, "Lucy &#39;render&#39;");
        // hashlib.sha1(murl.encode()).hexdigest()[:12]
        assert_eq!(r[0].id, "a47371af7c8c");
        assert_eq!(r[1].image, "u");
    }

    #[test]
    fn suggestions() {
        assert_eq!(suggestion_fragment("elfen lied, lu"), Some("lu".into()));
        assert_eq!(suggestion_fragment("elfen lied, l"), None);
        let data: Value = serde_json::from_str(r#"[{"label":"lucy (1)","value":"lucy"},{"label":"x","value":"lucy_(elfen_lied)"}]"#).unwrap();
        assert_eq!(parse_suggestions(&data).unwrap(), vec!["lucy", "lucy_(elfen_lied)"]);
    }

    #[test]
    fn resave_keeps_only_images() {
        let dir = std::env::temp_dir().join(format!("vta_resave_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let mut png = Vec::new();
        image::RgbaImage::from_pixel(3, 2, image::Rgba([1, 2, 3, 128]))
            .write_to(&mut std::io::Cursor::new(&mut png), image::ImageFormat::Png)
            .unwrap();
        let out = dir.join("1.png");
        resave_png(&png, &out).unwrap();
        let back = image::open(&out).unwrap().to_rgba8();
        assert_eq!(back.get_pixel(0, 0).0, [1, 2, 3, 128]);
        assert!(resave_png(b"<html>", &dir.join("2.png")).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }
}

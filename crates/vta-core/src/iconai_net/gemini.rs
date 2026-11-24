//! Google Gemini icon generation (v2 `icon_generator.py`, "Google Gemini
//! (cloud, the user's own API key)"): the theme is analysed once, then each
//! icon is one `generateContent` request carrying the template symbol, the
//! labelled theme images, community example icons and the icons already made.

use super::http;
use super::images;
use super::py::{self, PyError, PyRandom};
use super::rules;
use crate::settings::Settings;
use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

/// v2 `GENERATOR_CHOICES`: (label, key) of the "Generate with" choice.
pub const GENERATOR_CHOICES: [(&str, &str); 2] = [("This PC (local AI)", "local"), ("Google Gemini (your API key)", "google")];
/// v2 `GEMINI_API` (`{model}` is replaced).
pub const GEMINI_API: &str = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent";
/// v2 `GEMINI_MODELS`: (model id, label, USD per image).
pub const GEMINI_MODELS: [(&str, &str, f64); 3] = [
    ("gemini-3.1-flash-image-preview", "Gemini 3.1 Flash Image (best)", 0.067),
    ("gemini-3.1-flash-lite-image", "Gemini 3.1 Flash Lite Image (cheapest)", 0.0336),
    ("gemini-3-pro-image", "Gemini 3 Pro Image (slowest, most detailed)", 0.134),
];
/// v2 `DEFAULT_GEMINI_MODEL`.
pub const DEFAULT_GEMINI_MODEL: &str = GEMINI_MODELS[0].0;
/// v2 `GEMINI_FALLBACKS`: tried in order when a model isn't available to the key (404).
pub const GEMINI_FALLBACKS: [&str; 3] = ["gemini-3.1-flash-image-preview", "gemini-3.1-flash-image", "gemini-3.1-flash-lite-image"];
/// v2 `GEMINI_RETRIES`: retries for busy (429/500/503) answers.
pub const GEMINI_RETRIES: usize = 3;
/// v2 `GEMINI_REFERENCE_SIDE`: references are sent at most this size.
pub const GEMINI_REFERENCE_SIDE: u32 = 1024;
pub const GEMINI_MAX_WALLPAPERS: usize = 4;
pub const GEMINI_MAX_EXTRAS: usize = 3;
/// v2 `GEMINI_SYSTEM`.
pub const GEMINI_SYSTEM: &str = "You design custom app icons for PlayStation Vita themes. Every icon is a square, full-bleed piece of \
artwork in the theme's own art style and colours, built around the app's symbol so the app stays \
instantly recognisable. Reference images of the theme are provided. When a character appears, \
DO NOT reinterpret them: copy them exactly from the references, same face, eyes, hair, outfit and \
colours, and never draw a person who is not in the references. Characters are never cut off at the \
left, right or top edge. Only draw text that the request asks for, spelled exactly, in a clean font.";
/// v2 `GEMINI_TEXT_MODELS`: for the theme analysis.
pub const GEMINI_TEXT_MODELS: [&str; 2] = ["gemini-flash-latest", "gemini-2.5-flash"];
/// v2 `GEMINI_EXAMPLES_PER_ICON`.
pub const GEMINI_EXAMPLES_PER_ICON: usize = 4;
/// v2 `THEME_ANALYSIS_PROMPT`.
pub const THEME_ANALYSIS_PROMPT: &str = "These images are the wallpapers (and any extra character artwork) of a PlayStation Vita theme. \
Each image is labelled. Analyse them so an image model can make matching app icons. Return JSON with:\n\
- \"art_style\": the rendering style in a few words (e.g. anime cel shading, painterly, 3D render, photo).\n\
- \"setting\": the world, places and recurring motifs, in one or two sentences.\n\
- \"palette\": the main colours, as words.\n\
- \"characters\": every distinct character, each as {\"name\": their name if you recognise the series, \
otherwise a short label like \"girl with long pink hair\", \"appearance\": exact face, eyes, hair colour/style, \
outfit with colours, accessories and distinctive details, \"best_image\": the label of the image that shows \
them most completely}.\n\
Be precise and factual. Only list characters that are really in the images.";

/// v2 `google_api_key()`: the `google_api_key` setting, else `GEMINI_API_KEY`, stripped.
pub fn google_api_key(settings: &Settings) -> String {
    let from_settings = settings.str("google_api_key").filter(|s| !s.is_empty());
    match from_settings {
        Some(s) => s.trim().to_string(),
        None => std::env::var("GEMINI_API_KEY").unwrap_or_default().trim().to_string(),
    }
}

/// v2 `google_model()`: the `google_image_model` setting, else the default.
pub fn google_model(settings: &Settings) -> String {
    settings.str("google_image_model").filter(|s| !s.is_empty()).unwrap_or(DEFAULT_GEMINI_MODEL).to_string()
}

/// v2 `gemini_price(model)`: USD per image (first listed price when unknown).
pub fn gemini_price(model: &str) -> f64 {
    GEMINI_MODELS
        .iter()
        .find(|(key, _, _)| model.starts_with(key.strip_suffix("-preview").unwrap_or(key)))
        .map(|m| m.2)
        .unwrap_or(GEMINI_MODELS[0].2)
}

/// v2 `_confirm_gemini` question text (title "Generate with Google Gemini").
pub fn confirm_text(model: &str, count: usize) -> String {
    format!(
        "{count} icon{} will be made with {model}, one request each (about ${:.2} in total, billed to your Google API key).\n\n\
         The theme's wallpapers and any extra artwork are sent to Google as references.\n\nContinue?",
        if count != 1 { "s" } else { "" },
        gemini_price(model) * count as f64
    )
}

/// v2 `GeminiError`.
fn gemini_error(msg: impl Into<String>) -> PyError {
    PyError::new("GeminiError", msg)
}

fn url_for(model: &str) -> String {
    GEMINI_API.replace("{model}", model)
}

fn cancelled(c: &AtomicBool) -> bool {
    c.load(Ordering::Relaxed)
}

/// `d.get(key)` on a JSON value that must be a dict (else Python's AttributeError).
fn py_get<'a>(v: &'a Value, key: &str) -> Result<Option<&'a Value>, PyError> {
    match v {
        Value::Object(m) => Ok(m.get(key)),
        other => Err(PyError::new("AttributeError", format!("'{}' object has no attribute 'get'", py::type_name(other)))),
    }
}

/// `(v or [{}])[0]`.
fn first_or_empty(v: Option<&Value>) -> Result<Value, PyError> {
    if !py::truthy(v) {
        return Ok(Value::Object(Map::new()));
    }
    match v.unwrap() {
        Value::Array(a) => Ok(a[0].clone()),
        Value::String(s) => Ok(Value::String(s.chars().next().unwrap().to_string())),
        Value::Object(_) => Err(PyError::new("KeyError", "0")),
        other => Err(PyError::new("TypeError", format!("'{}' object is not subscriptable", py::type_name(other)))),
    }
}

/// `for x in (v or [])`: the items (dict keys / characters count as items).
fn iter_items(v: Option<&Value>) -> Result<Vec<Value>, PyError> {
    if !py::truthy(v) {
        return Ok(Vec::new());
    }
    match v.unwrap() {
        Value::Array(a) => Ok(a.clone()),
        Value::String(s) => Ok(s.chars().map(|c| Value::String(c.to_string())).collect()),
        Value::Object(m) => Ok(m.keys().map(|k| Value::String(k.clone())).collect()),
        other => Err(PyError::new("TypeError", format!("'{}' object is not iterable", py::type_name(other)))),
    }
}

/// Python's lenient `base64.b64decode` (characters outside the alphabet are dropped).
fn b64decode(data: &str) -> Result<Vec<u8>, PyError> {
    use base64::Engine;
    let clean: String = data.chars().filter(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '/' | '=')).collect();
    base64::engine::general_purpose::STANDARD
        .decode(clean.as_bytes())
        .map_err(|e| PyError::new("Error", e.to_string()))
}

/// v2 `gemini_generate(key, model, parts, seed, cancelled, log)`: one image
/// (as RGB) and the model that made it. Busy answers (429/500/503) are retried
/// up to [`GEMINI_RETRIES`] times (waiting `Retry-After` or 10 s, 20 s, 30 s);
/// a 404 moves on to the next of [`GEMINI_FALLBACKS`].
pub fn gemini_generate(
    key: &str,
    model: &str,
    parts: &[Value],
    seed: i64,
    cancel: &AtomicBool,
    log: &mut dyn FnMut(String),
) -> Result<(image::RgbImage, String), PyError> {
    let body = json!({
        "system_instruction": {"parts": [{"text": GEMINI_SYSTEM}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"], "seed": seed.rem_euclid(2_147_483_647),
                             "imageConfig": {"aspectRatio": "1:1"}},
    });
    let mut models = vec![model.to_string()];
    models.extend(GEMINI_FALLBACKS.iter().filter(|m| **m != model).map(|m| m.to_string()));
    for m in &models {
        for attempt in 0..=GEMINI_RETRIES {
            if cancelled(cancel) {
                return Err(gemini_error("cancelled"));
            }
            let resp = http::post_json(&url_for(m), &[("x-goog-api-key", key), ("Content-Type", "application/json")], &body, 180)?;
            if resp.status == 404 {
                log(format!("Model {m} isn't available to this key; trying the next one."));
                break;
            }
            if matches!(resp.status, 429 | 500 | 503) && attempt < GEMINI_RETRIES {
                let header = resp.header("Retry-After").unwrap_or("");
                let given = if header.is_empty() { 0 } else { py::int(header)? };
                let wait = if given != 0 { given } else { 10 * (attempt as i64 + 1) };
                log(format!("Gemini is busy ({}); retrying in {wait}s.", resp.status));
                for _ in 0..wait.max(0) {
                    if cancelled(cancel) {
                        return Err(gemini_error("cancelled"));
                    }
                    std::thread::sleep(std::time::Duration::from_secs(1));
                }
                continue;
            }
            if resp.status != 200 {
                let fallback = Value::String(py::head_chars(&resp.text(), 300));
                let message = match resp.json() {
                    Err(_) => fallback,
                    Ok(data) => {
                        let empty = Value::Object(Map::new());
                        let error = py_get(&data, "error")?.unwrap_or(&empty);
                        py_get(error, "message")?.cloned().unwrap_or(fallback)
                    }
                };
                if matches!(resp.status, 400 | 401 | 403) {
                    let Value::String(text) = &message else {
                        return Err(PyError::new(
                            "AttributeError",
                            format!("'{}' object has no attribute 'lower'", py::type_name(&message)),
                        ));
                    };
                    if text.to_lowercase().contains("key") {
                        return Err(gemini_error(format!("Google rejected the API key: {text}")));
                    }
                }
                let message = py::str_of(&message);
                if resp.status == 429 {
                    return Err(gemini_error(format!(
                        "Gemini quota reached ({message}). Image models need billing enabled on the key."
                    )));
                }
                return Err(gemini_error(format!("Gemini error {}: {message}", resp.status)));
            }
            let data = resp.json()?;
            let candidate = first_or_empty(py_get(&data, "candidates")?)?;
            let content = py_get(&candidate, "content")?.cloned();
            let content = if py::truthy(content.as_ref()) { content.unwrap() } else { Value::Object(Map::new()) };
            for part in iter_items(py_get(&content, "parts")?)? {
                let inline = match py_get(&part, "inline_data")? {
                    Some(v) if py::truthy(Some(v)) => Some(v),
                    _ => py_get(&part, "inlineData")?,
                };
                if let Some(inline) = inline.filter(|v| py::truthy(Some(v))) {
                    let data_field = py_get(inline, "data")?;
                    if py::truthy(data_field) {
                        let Some(Value::String(b64)) = data_field else {
                            return Err(PyError::new("TypeError", "argument should be a bytes-like object or ASCII string"));
                        };
                        let img = images::open_bytes(&b64decode(b64)?)?;
                        return Ok((img.to_rgb8(), m.clone()));
                    }
                }
            }
            let reason = match py_get(&candidate, "finishReason")? {
                Some(v) if py::truthy(Some(v)) => Some(v.clone()),
                _ => {
                    let feedback = py_get(&data, "promptFeedback")?.cloned();
                    let feedback = if py::truthy(feedback.as_ref()) { feedback.unwrap() } else { Value::Object(Map::new()) };
                    py_get(&feedback, "blockReason")?.cloned()
                }
            };
            let reason = match reason {
                Some(v) if py::truthy(Some(&v)) => py::str_of(&v),
                _ => "no reason given".to_string(),
            };
            return Err(gemini_error(format!("Gemini returned no image ({reason}).")));
        }
    }
    Err(gemini_error("None of the Gemini image models are available to this API key."))
}

/// v2 `_wallpaper_label(path, index)`: what a theme image is, from its Vita asset name.
pub fn wallpaper_label(path: &Path, index: usize) -> String {
    let file = path.to_string_lossy();
    let name = py::splitext_stem(py::basename(&file)).to_lowercase();
    if name == "lockpaper" {
        return "Lock screen wallpaper".into();
    }
    static BR: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let re = BR.get_or_init(|| regex::Regex::new(r"^br(\d+)$").unwrap());
    if let Some(c) = re.captures(&name) {
        return format!("Home page {} wallpaper", &c[1]);
    }
    format!("Theme wallpaper {}", index + 1)
}

/// v2 `_example_icons(icon_name, job, rng, count)`: the same app's icon from
/// the example sets (built-in and community), sampled with the job's rng.
pub fn example_icons(icon_name: &str, examples_dirs: &[PathBuf], rng: &mut PyRandom, count: usize) -> Vec<PathBuf> {
    let dirs: Vec<Option<PathBuf>> = examples_dirs.iter().cloned().map(Some).collect();
    let found: Vec<PathBuf> = rules::example_sets(&dirs)
        .into_iter()
        .map(|(_, d)| d.join(icon_name))
        .filter(|p| p.is_file())
        .collect();
    let k = count.min(found.len());
    rng.sample(&found, k)
}

/// v2 `_example_title(icon_path)`: the community theme an example icon comes
/// from (its set's `source.json`), else "another theme".
pub fn example_title(icon_path: &Path) -> Result<String, PyError> {
    let source = icon_path.parent().unwrap_or(Path::new("")).join("source.json");
    let Ok(bytes) = std::fs::read(&source) else { return Ok("another theme".into()) };
    let Ok(data) = serde_json::from_slice::<Value>(&bytes) else { return Ok("another theme".into()) };
    let title = py_get(&data, "title")?;
    Ok(if py::truthy(title) { py::str_of(title.unwrap()) } else { "another theme".into() })
}

/// v2 `gemini_describe_theme` with the image parts already made: the theme's
/// art style, setting, palette and characters, or `{}` when unavailable.
/// Network failures, non-200 answers and unreadable JSON are logged and the
/// next text model is tried.
pub fn gemini_describe_theme(
    key: &str,
    labelled_parts: &[(String, Value)],
    cancel: &AtomicBool,
    log: &mut dyn FnMut(String),
) -> Result<Map<String, Value>, PyError> {
    let mut parts = Vec::new();
    for (label, part) in labelled_parts {
        parts.push(json!({"text": format!("[{label}]")}));
        parts.push(part.clone());
    }
    parts.push(json!({"text": THEME_ANALYSIS_PROMPT}));
    let body = json!({"contents": [{"role": "user", "parts": parts}],
                      "generationConfig": {"responseMimeType": "application/json"}});
    for model in GEMINI_TEXT_MODELS {
        if cancelled(cancel) {
            return Ok(Map::new());
        }
        let resp = match http::post_json(&url_for(model), &[("x-goog-api-key", key), ("Content-Type", "application/json")], &body, 120) {
            Ok(r) => r,
            Err(e) => {
                log(format!("Theme analysis with {model} failed: {}", e.message));
                continue;
            }
        };
        if resp.status != 200 {
            log(format!("Theme analysis with {model} failed ({}).", resp.status));
            continue;
        }
        let data = match resp.json() {
            Ok(d) => d,
            Err(e) => {
                log(format!("Theme analysis with {model} failed: {}", e.message));
                continue;
            }
        };
        let candidate = first_or_empty(py_get(&data, "candidates")?)?;
        let content = py_get(&candidate, "content")?.cloned();
        let content = if py::truthy(content.as_ref()) { content.unwrap() } else { Value::Object(Map::new()) };
        let mut text = String::new();
        for (i, p) in iter_items(py_get(&content, "parts")?)?.iter().enumerate() {
            match py_get(p, "text")? {
                None => {}
                Some(Value::String(s)) => text.push_str(s),
                Some(other) => {
                    return Err(PyError::new(
                        "TypeError",
                        format!("sequence item {i}: expected str instance, {} found", py::type_name(other)),
                    ))
                }
            }
        }
        match serde_json::from_str::<Value>(&text) {
            Ok(Value::Object(m)) => return Ok(m),
            Ok(_) => return Ok(Map::new()),
            Err(e) => log(format!("Theme analysis with {model} failed: {e}")),
        }
    }
    Ok(Map::new())
}

/// The parts of a job [`gemini_icon_prompt`] needs.
#[derive(Debug, Clone, Default)]
pub struct PromptJob {
    pub title: String,
    pub theme_colors: Vec<String>,
    /// `auto`, `always` or `never` (v2 `THEME_COLOR_CHOICES`).
    pub theme_color_mode: String,
}

/// v2 `gemini_icon_prompt(icon_name, info, job, analysis, character)`.
pub fn gemini_icon_prompt(icon_name: &str, job: &PromptJob, analysis: &Map<String, Value>, character: Option<&Map<String, Value>>) -> String {
    let subject = rules::icon_subject(icon_name, "app");
    let text = rules::text_region(icon_name);
    let mode = job.theme_color_mode.as_str();
    let colours = if mode == "never" {
        String::new()
    } else {
        job.theme_colors
            .iter()
            .filter(|c| c.chars().count() >= 6)
            .map(|c| {
                let n = c.chars().count();
                format!("#{}", c.chars().skip(n - 6).collect::<String>())
            })
            .collect::<Vec<_>>()
            .join(", ")
    };
    let title = if job.title.is_empty() { "custom theme" } else { job.title.as_str() };
    let mut lines = vec![
        format!("Create the PS Vita '{subject}' app icon for the theme \"{title}\"."),
        format!(
            "[The app's system symbol] shows the symbol: keep it clearly recognisable as the {subject} symbol. \
             You may redesign its shape and give it detail, colour and depth, but it must still read as that symbol."
        ),
        "Use the art style, colours, lighting and world of the theme's wallpapers. \
         Don't copy a wallpaper; make icon artwork that belongs to the theme."
            .to_string(),
    ];
    if py::truthy(analysis.get("art_style")) {
        lines.push(format!(
            "Theme art style: {}. Setting and motifs: {} Palette: {}.",
            py::str_of(&analysis["art_style"]),
            py::str_or(analysis.get("setting"), ""),
            py::str_or(analysis.get("palette"), "")
        ));
    }
    if !colours.is_empty() {
        lines.push(if mode == "always" {
            format!("Use the theme's interface colours {colours} as the icon's main colours.")
        } else {
            format!("The theme's interface colours are {colours}; use them where they suit the art.")
        });
    }
    match character {
        Some(c) => {
            let where_ = if py::truthy(c.get("best_image")) {
                format!(" (see [{}])", py::str_of(&c["best_image"]))
            } else {
                String::new()
            };
            lines.push(format!(
                "Feature this character, copied exactly from the reference images{where_}: {}: {}. \
                 Place them next to or interacting with the symbol; the symbol must stay clearly visible.",
                py::str_or(c.get("name"), "the character"),
                py::str_or(c.get("appearance"), "")
            ));
        }
        None => lines.push("No characters or people in this icon: build it from the theme's scenery, shapes and motifs.".into()),
    }
    lines.push(match text {
        Some(t) => format!("Show the text \"{}\" clearly as part of the symbol, exactly as written.", t.text),
        None => "No text, letters or logos anywhere.".into(),
    });
    lines.push("Square, full-bleed art filling the whole image: no border, no rounded corners, no frame around the icon.".into());
    lines.join("\n")
}

/// The generation job (the keys of v2's `job` dict the Gemini path reads).
#[derive(Debug, Clone, Default)]
pub struct GeminiJob {
    pub title: String,
    pub theme_colors: Vec<String>,
    pub theme_color_mode: String,
    /// `Assets/IconTemplates` ([`rules::templates_dir`]).
    pub templates_dir: PathBuf,
    pub wallpapers: Vec<PathBuf>,
    pub icons: Vec<String>,
    pub output_dir: PathBuf,
    /// v2 `LAYOUT_STYLE_CHOICES` key; `emblem` means no characters.
    pub layout_style: String,
    pub extra_images: Vec<PathBuf>,
    /// [`rules::job_examples_dirs`].
    pub examples_dirs: Vec<PathBuf>,
    pub seed: i64,
}

/// What the generation reports, as v2's `GeminiIconThread` signals
/// (`event` dicts, `log` lines, `finished_ok`).
#[derive(Debug, Clone, PartialEq)]
pub enum GeminiEvent {
    /// `{'event': 'status', 'message'}`.
    Status { message: String },
    /// `{'event': 'progress', ...}`: icon `index` of `total` is being made.
    Progress { name: String, index: usize, total: usize },
    /// `{'event': 'skipped', ...}`: no icon for `name` (no template, or a Gemini error).
    Skipped { name: String, index: usize, total: usize, reason: String },
    /// `{'event': 'icon', ...}`: a finished 128x128 PNG at `path`.
    Icon { name: String, index: usize, total: usize, path: PathBuf },
    /// A line for the dialog's log.
    Log(String),
    /// `finished_ok(ok, message)`: always the last event.
    Finished { ok: bool, message: String },
}

/// v2 `GeminiIconThread.run`: generates the job's icons with Gemini, one
/// request per icon, reporting through `emit`. Blocking; `cancel` stops it
/// between requests (and during busy waits). Always ends with
/// [`GeminiEvent::Finished`].
pub fn run_gemini_job(job: &GeminiJob, key: &str, model: &str, cancel: &AtomicBool, emit: &mut dyn FnMut(GeminiEvent)) {
    let (ok, message) = match run_inner(job, key, model, cancel, emit) {
        Ok(r) => r,
        Err(e) => (false, e.to_string()),
    };
    emit(GeminiEvent::Finished { ok, message });
}

const CANCELLED: &str = "Generation cancelled.";

#[cfg(test)]
mod tests {
    use super::*;

    fn obj(v: Value) -> Map<String, Value> {
        match v {
            Value::Object(m) => m,
            _ => unreachable!(),
        }
    }

    /// Expected strings produced by v2's `gemini_icon_prompt` (Python 3.12).
    const V2_PROMPTS: &str = r#"["Create the PS Vita 'web browser globe' app icon for the theme \"Persona 5\".\n[The app's system symbol] shows the symbol: keep it clearly recognisable as the web browser globe symbol. You may redesign its shape and give it detail, colour and depth, but it must still read as that symbol.\nUse the art style, colours, lighting and world of the theme's wallpapers. Don't copy a wallpaper; make icon artwork that belongs to the theme.\nTheme art style: anime cel shading. Setting and motifs: Tokyo at night. Palette: red, black.\nUse the theme's interface colours #112233, #AABBCC as the icon's main colours.\nFeature this character, copied exactly from the reference images (see [Home page 1 wallpaper]): Joker: black hair, mask. Place them next to or interacting with the symbol; the symbol must stay clearly visible.\nShow the text \"www\" clearly as part of the symbol, exactly as written.\nSquare, full-bleed art filling the whole image: no border, no rounded corners, no frame around the icon.", "Create the PS Vita 'app' app icon for the theme \"custom theme\".\n[The app's system symbol] shows the symbol: keep it clearly recognisable as the app symbol. You may redesign its shape and give it detail, colour and depth, but it must still read as that symbol.\nUse the art style, colours, lighting and world of the theme's wallpapers. Don't copy a wallpaper; make icon artwork that belongs to the theme.\nNo characters or people in this icon: build it from the theme's scenery, shapes and motifs.\nNo text, letters or logos anywhere.\nSquare, full-bleed art filling the whole image: no border, no rounded corners, no frame around the icon.", "Create the PS Vita 'friends list faces' app icon for the theme \"X\".\n[The app's system symbol] shows the symbol: keep it clearly recognisable as the friends list faces symbol. You may redesign its shape and give it detail, colour and depth, but it must still read as that symbol.\nUse the art style, colours, lighting and world of the theme's wallpapers. Don't copy a wallpaper; make icon artwork that belongs to the theme.\nTheme art style: x. Setting and motifs:  Palette: ['a', 1].\nThe theme's interface colours are #112233; use them where they suit the art.\nFeature this character, copied exactly from the reference images: one character from the reference images: . Place them next to or interacting with the symbol; the symbol must stay clearly visible.\nNo text, letters or logos anywhere.\nSquare, full-bleed art filling the whole image: no border, no rounded corners, no frame around the icon."]"#;

    #[test]
    fn prompts_match_v2() {
        let expected: Vec<String> = serde_json::from_str(V2_PROMPTS).unwrap();
        let job = PromptJob {
            title: "Persona 5".into(),
            theme_colors: vec!["ff112233".into(), "abc".into(), "00AABBCC".into()],
            theme_color_mode: "always".into(),
        };
        let analysis = obj(json!({"art_style": "anime cel shading", "setting": "Tokyo at night.", "palette": "red, black"}));
        let ch = obj(json!({"name": "Joker", "appearance": "black hair, mask", "best_image": "Home page 1 wallpaper"}));
        assert_eq!(gemini_icon_prompt("icon_web.png", &job, &analysis, Some(&ch)), expected[0]);
        let job = PromptJob { title: String::new(), theme_colors: vec!["ff112233".into()], theme_color_mode: "never".into() };
        assert_eq!(gemini_icon_prompt("icon_unknown.png", &job, &Map::new(), None), expected[1]);
        let job = PromptJob { title: "X".into(), theme_colors: vec!["ff112233".into()], theme_color_mode: "auto".into() };
        let analysis = obj(json!({"art_style": "x", "palette": ["a", 1]}));
        let ch = obj(json!({"name": "one character from the reference images"}));
        assert_eq!(gemini_icon_prompt("icon_friend.png", &job, &analysis, Some(&ch)), expected[2]);
    }

    #[test]
    fn labels_prices_and_constants() {
        assert_eq!(wallpaper_label(Path::new("a/b/BR12.png"), 0), "Home page 12 wallpaper");
        assert_eq!(wallpaper_label(Path::new("lockpaper.jpg"), 1), "Lock screen wallpaper");
        assert_eq!(wallpaper_label(Path::new("wall.png"), 2), "Theme wallpaper 3");
        assert_eq!(gemini_price("gemini-3.1-flash-image"), 0.067);
        assert_eq!(gemini_price("gemini-3-pro-image-preview"), 0.134);
        assert_eq!(gemini_price("other"), 0.067);
        assert_eq!(confirm_text("gemini-3-pro-image", 17).lines().next().unwrap(),
                   "17 icons will be made with gemini-3-pro-image, one request each (about $2.28 in total, billed to your Google API key).");
        // json.dumps of v2's constants.
        assert_eq!(py::json_dumps(&Value::String(THEME_ANALYSIS_PROMPT.into())), "\"These images are the wallpapers (and any extra character artwork) of a PlayStation Vita theme. Each image is labelled. Analyse them so an image model can make matching app icons. Return JSON with:\\n- \\\"art_style\\\": the rendering style in a few words (e.g. anime cel shading, painterly, 3D render, photo).\\n- \\\"setting\\\": the world, places and recurring motifs, in one or two sentences.\\n- \\\"palette\\\": the main colours, as words.\\n- \\\"characters\\\": every distinct character, each as {\\\"name\\\": their name if you recognise the series, otherwise a short label like \\\"girl with long pink hair\\\", \\\"appearance\\\": exact face, eyes, hair colour/style, outfit with colours, accessories and distinctive details, \\\"best_image\\\": the label of the image that shows them most completely}.\\nBe precise and factual. Only list characters that are really in the images.\"");
        assert_eq!(GEMINI_SYSTEM, "You design custom app icons for PlayStation Vita themes. Every icon is a square, full-bleed piece of artwork in the theme's own art style and colours, built around the app's symbol so the app stays instantly recognisable. Reference images of the theme are provided. When a character appears, DO NOT reinterpret them: copy them exactly from the references, same face, eyes, hair, outfit and colours, and never draw a person who is not in the references. Characters are never cut off at the left, right or top edge. Only draw text that the request asks for, spelled exactly, in a clean font.");
    }

    #[test]
    fn example_icons_are_seeded_like_v2() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../Assets/IconExamples");
        let mut a = PyRandom::new(42);
        let mut b = PyRandom::new(42);
        let first = example_icons("icon_web.png", &[root.clone()], &mut a, 4);
        assert_eq!(first, example_icons("icon_web.png", &[root.clone()], &mut b, 4));
        assert_eq!(first.len(), 4);
        assert!(example_icons("icon_none.png", &[root], &mut a, 4).is_empty());
    }

    #[test]
    fn image_parts() {
        let dir = std::env::temp_dir().join(format!("vta_gem_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let jpg_src = dir.join("br1.png");
        image::RgbImage::from_pixel(2000, 1000, image::Rgb([5, 6, 7])).save(&jpg_src).unwrap();
        let part = images::image_part(&jpg_src, GEMINI_REFERENCE_SIDE).unwrap();
        assert_eq!(part["inline_data"]["mime_type"], "image/jpeg");
        let template = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../Assets/IconTemplates/icon_web.png");
        let part = images::image_part(&template, 512).unwrap();
        assert_eq!(part["inline_data"]["mime_type"], "image/png");
        let _ = std::fs::remove_dir_all(&dir);
    }
}

fn run_inner(job: &GeminiJob, key: &str, model: &str, cancel: &AtomicBool, emit: &mut dyn FnMut(GeminiEvent)) -> Result<(bool, String), PyError> {
    let icons = &job.icons;
    let total = icons.len();
    let mut rng = PyRandom::new(job.seed);
    let mut labelled: Vec<(String, PathBuf)> = job
        .wallpapers
        .iter()
        .take(GEMINI_MAX_WALLPAPERS)
        .enumerate()
        .map(|(i, p)| (wallpaper_label(p, i), p.clone()))
        .collect();
    let extras: Vec<&PathBuf> = job.extra_images.iter().filter(|p| p.is_file()).take(GEMINI_MAX_EXTRAS).collect();
    labelled.extend(extras.iter().enumerate().map(|(i, p)| (format!("Extra character artwork {}", i + 1), (*p).clone())));
    std::fs::create_dir_all(&job.output_dir).map_err(|e| PyError::new("OSError", e.to_string()))?;

    emit(GeminiEvent::Status { message: "Analysing the theme's wallpapers and characters...".into() });
    // v2 encodes the references twice (analysis, then icons) the same way; once is enough.
    let mut labelled_parts = Vec::new();
    for (label, path) in &labelled {
        labelled_parts.push((label.clone(), images::image_part(path, GEMINI_REFERENCE_SIDE)?));
    }
    let analysis = {
        let mut log = |s: String| emit(GeminiEvent::Log(s));
        gemini_describe_theme(key, &labelled_parts, cancel, &mut log)?
    };
    let mut characters: Vec<Map<String, Value>> = iter_items(analysis.get("characters"))?
        .into_iter()
        .filter_map(|c| match c {
            Value::Object(m) => Some(m),
            _ => None,
        })
        .collect();
    if !analysis.is_empty() {
        let mut names = Vec::new();
        for (i, c) in characters.iter().enumerate() {
            match c.get("name") {
                None => names.push("?".to_string()),
                Some(Value::String(s)) => names.push(s.clone()),
                Some(other) => {
                    return Err(PyError::new(
                        "TypeError",
                        format!("sequence item {i}: expected str instance, {} found", py::type_name(other)),
                    ))
                }
            }
        }
        let joined = names.join(", ");
        emit(GeminiEvent::Log(format!(
            "Theme: {}; characters: {}",
            py::str_or(analysis.get("art_style"), "?"),
            if joined.is_empty() { "none".to_string() } else { joined }
        )));
    }
    // Character icons take turns through the characters (seeded order).
    rng.shuffle(&mut characters);
    let wants: Vec<&String> = icons.iter().filter(|n| job.layout_style != "emblem" && rules::is_character_icon(n)).collect();
    let cast: std::collections::HashMap<&str, &Map<String, Value>> = if characters.is_empty() {
        Default::default()
    } else {
        wants.iter().enumerate().map(|(i, n)| (n.as_str(), &characters[i % characters.len()])).collect()
    };
    let mut generic = Map::new();
    generic.insert("name".into(), Value::String("one character from the reference images".into()));

    let mut refs = Vec::new();
    for (label, part) in &labelled_parts {
        refs.push(json!({"text": format!("[{label}]")}));
        refs.push(part.clone());
    }
    let prompt_job = PromptJob { title: job.title.clone(), theme_colors: job.theme_colors.clone(), theme_color_mode: job.theme_color_mode.clone() };
    let mut made: Vec<PathBuf> = Vec::new();
    let mut used = model.to_string();
    for (index, name) in icons.iter().enumerate() {
        if cancelled(cancel) {
            return Ok((false, CANCELLED.into()));
        }
        emit(GeminiEvent::Progress { name: name.clone(), index, total });
        let template = job.templates_dir.join(name);
        if !template.is_file() {
            emit(GeminiEvent::Skipped { name: name.clone(), index, total, reason: "template not found".into() });
            continue;
        }
        // Without an analysis, character icons still ask for "a character from the references".
        let character = cast.get(name.as_str()).copied().or_else(|| {
            (characters.is_empty() && wants.iter().any(|w| *w == name)).then_some(&generic)
        });
        let mut parts = vec![
            json!({"text": gemini_icon_prompt(name, &prompt_job, &analysis, character)}),
            json!({"text": "[The app's system symbol]"}),
            images::image_part(&template, 512)?,
        ];
        parts.extend(refs.iter().cloned());
        let examples = example_icons(name, &job.examples_dirs, &mut rng, GEMINI_EXAMPLES_PER_ICON);
        let subject = rules::icon_subject(name, "same");
        for (n, example) in examples.iter().enumerate() {
            let title = example_title(example)?;
            parts.push(json!({"text": format!(
                "[Example icon {} of {}: the {subject} icon from the community theme \"{title}\"] For ideas on how to design \
                 the symbol only: don't copy its characters, colours or style.", n + 1, examples.len())}));
            parts.push(images::image_part(example, 256)?);
        }
        if !made.is_empty() {
            // The first and the latest icon keep the set consistent.
            parts.push(json!({"text": "[Icons already made for this set] Match their style and finish:"}));
            let mut keep = vec![made[0].clone()];
            if made[made.len() - 1] != made[0] {
                keep.push(made[made.len() - 1].clone());
            }
            for p in keep {
                parts.push(images::image_part(&p, 256)?);
            }
        }
        parts.push(json!({"text": "Now create the icon."}));
        let result = {
            let mut log = |s: String| emit(GeminiEvent::Log(s));
            gemini_generate(key, &used, &parts, job.seed + index as i64 * 7919, cancel, &mut log)
        };
        let img = match result {
            Ok((img, m)) => {
                used = m;
                img
            }
            Err(e) if e.kind == "GeminiError" => {
                if cancelled(cancel) {
                    return Ok((false, CANCELLED.into()));
                }
                let msg = e.message.as_str();
                if msg.contains("API key") || msg.contains("quota") || msg.contains("available") {
                    return Err(e);
                }
                emit(GeminiEvent::Skipped { name: name.clone(), index, total, reason: e.message });
                continue;
            }
            Err(e) => return Err(e),
        };
        let side = img.width().min(img.height());
        let cropped = image::imageops::crop_imm(&img, (img.width() - side) / 2, (img.height() - side) / 2, side, side).to_image();
        let path = job.output_dir.join(name);
        images::resize_lanczos(&cropped, 128, 128)
            .save_with_format(&path, image::ImageFormat::Png)
            .map_err(|e| PyError::new("OSError", e.to_string()))?;
        made.push(path.clone());
        emit(GeminiEvent::Icon { name: name.clone(), index, total, path });
    }
    emit(GeminiEvent::Log(format!("Generated {} icon(s) with {used}.", made.len())));
    Ok(if made.is_empty() {
        (false, "No icons were generated. See the log for details.".into())
    } else {
        (true, "Done.".into())
    })
}

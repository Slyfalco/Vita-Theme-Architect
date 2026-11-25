//! Theme Creator controller (v2 `show_creator_ui`, `load_theme_data_into_creator`,
//! `build_theme`). Holds the sources the user picked and pushes them to the UI.

use crate::dialogs;
use crate::library_actions::SharedSettings;
use crate::library_view::{rescan, select, SharedLibrary};
use crate::{AppWindow, AssetSlot, ColorPicker, ColorSlot, Creator, DialogIcon, IconGroup, Library, PageSlot, Shell};
use slint::{ComponentHandle, Image, ModelRc, VecModel};
use std::cell::RefCell;
use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};
use vta_core::build::{self, BuildInput};
use vta_core::library::LibraryTheme;

/// v2's default colours (Colors tab), without the stray '#' v2 kept on untouched
/// defaults: the official format is bare AARRGGBB.
const DEFAULT_COLORS: [(&str, &str, &str, &str); 9] = [
    ("m_dateColor", "Date Color", "Start Screen", "ffffffff"),
    ("m_notifyBgColor", "Notify BG Color", "Start Screen", "ffffffff"),
    ("m_notifyBorderColor", "Notify Border Color", "Start Screen", "ffcccccc"),
    ("m_notifyFontColor", "Notify Font Color", "Start Screen", "ffffffff"),
    ("m_barColor", "Bar Color", "Info Bar", "ff000000"),
    ("m_indicatorColor", "Indicator Color", "Info Bar", "ffffffff"),
    ("m_noticeFontColor", "Notice Font Color", "Info Bar", "ffffffff"),
    ("m_noticeGlowColor", "Notice Glow Color", "Info Bar", "ffca0000"),
    ("m_fontColor", "Global Font Color", "Home", "ffffffff"),
];

const MAIN_SLOTS: [(&str, &str, &str); 5] = [
    ("lockpaper.png", "Lock Screen", "960x512"),
    ("br1.png", "Home Page 1", "960x544"),
    ("preview_livearea.png", "LiveArea Preview", "480x272"),
    ("preview_lockscreen.png", "Lockscreen Preview", "480x272"),
    ("preview_thumbnail.png", "Thumbnail Preview", "226x128"),
];

const UI_SLOTS: [(&str, &str, &str); 4] = [
    ("screen_other.png", "Page Indicator (other)", "22x22"),
    ("screen_current.png", "Current Page (current)", "22x22"),
    ("noNotice.png", "No Notice", "120x110"),
    ("newNotice.png", "New Notice", "120x110"),
];

/// (asset id, display name, group)
const ICON_INFO: [(&str, &str, &str); 17] = [
    ("icon_settings.png", "Settings", "Core System Apps"),
    ("icon_web.png", "Browser", "Core System Apps"),
    ("icon_music.png", "Music", "Core System Apps"),
    ("icon_photos.png", "Photos", "Core System Apps"),
    ("icon_video.png", "Videos", "Core System Apps"),
    ("icon_calendar.png", "Calendar", "Core System Apps"),
    ("icon_trophy.png", "Trophies", "Gaming & Social"),
    ("icon_friend.png", "Friends", "Gaming & Social"),
    ("icon_message.png", "Messages", "Gaming & Social"),
    ("icon_party.png", "Party", "Gaming & Social"),
    ("icon_near.png", "near", "Gaming & Social"),
    ("icon_cma.png", "Content Manager", "System Utilities & Services"),
    ("icon_email.png", "Email", "System Utilities & Services"),
    ("icon_parental.png", "Parental Controls", "System Utilities & Services"),
    ("icon_power.png", "Power", "System Utilities & Services"),
    ("icon_ps3link.png", "PS3 Link", "System Utilities & Services"),
    ("icon_ps4link.png", "PS4 Link", "System Utilities & Services"),
];

macro_rules! templates {
    ($($name:literal),*) => {
        [$(($name, include_bytes!(concat!("../../../Assets/IconTemplates/", $name)) as &[u8])),*]
    };
}
const TEMPLATE_ICONS: [(&str, &[u8]); 17] = templates!(
    "icon_settings.png", "icon_web.png", "icon_music.png", "icon_photos.png", "icon_video.png", "icon_calendar.png",
    "icon_trophy.png", "icon_friend.png", "icon_message.png", "icon_party.png", "icon_near.png", "icon_cma.png",
    "icon_email.png", "icon_parental.png", "icon_power.png", "icon_ps3link.png", "icon_ps4link.png"
);

#[derive(Clone)]
struct Page {
    src: String,
    wave: i32,
    color: String,
}

#[derive(Default)]
struct State {
    editing: Option<PathBuf>,
    target: String,
    sources: BTreeMap<String, String>,
    pages: Vec<Page>,
    bgm: String,
    colors: BTreeMap<String, String>,
    use_global: bool,
    dirty: bool,
    picking: Option<String>,
    thumbs: HashMap<String, (Image, String)>,
    templates: HashMap<String, Image>,
}

thread_local! {
    static STATE: RefCell<State> = RefCell::new(State::default());
}

fn image_from_png(bytes: &[u8]) -> Image {
    match image::load_from_memory(bytes) {
        Ok(img) => {
            let rgba = img.to_rgba8();
            let (w, h) = rgba.dimensions();
            let buf = slint::SharedPixelBuffer::<slint::Rgba8Pixel>::clone_from_slice(rgba.as_raw(), w, h);
            Image::from_rgba8(buf)
        }
        Err(_) => Image::default(),
    }
}

/// Thumbnail + "W×H · N KB" for a source file (cached by path).
fn thumb(st: &mut State, path: &str) -> (Image, String) {
    if let Some(t) = st.thumbs.get(path) {
        return t.clone();
    }
    let img = Image::load_from_path(Path::new(path)).unwrap_or_default();
    let dims = image::image_dimensions(path).map(|(w, h)| format!("{w}×{h}")).unwrap_or_default();
    let kb = std::fs::metadata(path).map(|m| m.len().div_ceil(1024)).unwrap_or(0);
    let info = if dims.is_empty() { format!("{kb} KB") } else { format!("{dims} · {kb} KB") };
    st.thumbs.insert(path.to_string(), (img.clone(), info.clone()));
    (img, info)
}

fn file_name(path: &str) -> String {
    Path::new(path).file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
}

fn parse_argb(hex: &str) -> slint::Color {
    let h = hex.trim().trim_start_matches('#');
    let h = if h.len() == 6 { format!("ff{h}") } else { h.to_string() };
    u32::from_str_radix(&h, 16)
        .map(|v| slint::Color::from_argb_u8((v >> 24) as u8, (v >> 16) as u8, (v >> 8) as u8, v as u8))
        .unwrap_or(slint::Color::from_rgb_u8(255, 255, 255))
}

fn normalise_hex(hex: &str) -> String {
    let h = hex.trim().trim_start_matches('#').to_lowercase();
    if h.len() == 6 {
        format!("ff{h}")
    } else {
        h
    }
}

fn slot(st: &mut State, id: &str, label: &str, spec: &str, required: bool, group: &str) -> AssetSlot {
    let path = st.sources.get(id).cloned().unwrap_or_default();
    let (img, info, has) = if path.is_empty() {
        match st.templates.get(id) {
            Some(t) => (t.clone(), String::new(), true),
            None => (Image::default(), String::new(), false),
        }
    } else {
        let (img, info) = thumb(st, &path);
        (img, info, true)
    };
    AssetSlot {
        id: id.into(),
        label: label.into(),
        spec: spec.into(),
        required,
        file: file_name(&path).into(),
        info: info.into(),
        thumb: img,
        has_thumb: has,
        group: group.into(),
    }
}

/// Push the whole state to the UI.
/// Budget for the VRAM footprint indicator (the Vita's theme asset ceiling is
/// modest; 16 MB of source imagery is a generous headroom guide).
const VRAM_BUDGET: u64 = 16 * 1024 * 1024;

/// Populate the creator's Validation Engine panel from the current theme state.
fn set_validation(c: &Creator, st: &State, required_ready: usize) {
    // Accent/text contrast: the global font colour over the info-bar background.
    let font = st.colors.get("m_fontColor").map(String::as_str).unwrap_or("ffffffff");
    let bar = st.colors.get("m_barColor").map(String::as_str).unwrap_or("ff000000");
    let ratio = vta_core::validation::contrast_ratio(font, bar);
    let level = vta_core::validation::contrast_level(ratio);
    let contrast_pass = ratio >= 4.5;
    c.set_contrast_ratio(format!("{ratio:.1}:1").into());
    c.set_contrast_level(level.into());
    c.set_contrast_pass(contrast_pass);

    // UI chrome: the swipeable home-page frames (page 1 + the additional pages).
    let home_frames = (1 + st.pages.iter().filter(|p| !p.src.is_empty()).count()).min(10);
    c.set_chrome_ready(home_frames as i32);
    c.set_chrome_total(10);

    // VRAM footprint: every image + BGM the theme carries on disk.
    let mut bytes = 0u64;
    let size_of = |p: &str| if p.is_empty() { 0 } else { std::fs::metadata(p).map(|m| m.len()).unwrap_or(0) };
    for p in st.sources.values() {
        bytes += size_of(p);
    }
    for pg in &st.pages {
        bytes += size_of(&pg.src);
    }
    bytes += size_of(&st.bgm);
    c.set_vram_text(crate::library_view::format_size(bytes).into());
    c.set_vram_budget(crate::library_view::format_size(VRAM_BUDGET).into());
    c.set_vram_fraction(bytes as f32 / VRAM_BUDGET as f32);

    c.set_validation_pass(required_ready == 5 && contrast_pass && bytes <= VRAM_BUDGET);
}

fn refresh(app: &AppWindow) {
    let c = app.global::<Creator>();
    let search = c.get_icon_search().to_lowercase();
    let filter = c.get_icon_filter();
    STATE.with(|s| {
        let st = &mut *s.borrow_mut();
        if st.templates.is_empty() {
            for (name, bytes) in TEMPLATE_ICONS {
                st.templates.insert(name.to_string(), image_from_png(bytes));
            }
        }
        let main: Vec<AssetSlot> = MAIN_SLOTS.iter().map(|(id, l, sp)| slot(st, id, l, sp, true, "")).collect();
        let ready = main.iter().filter(|s| !s.file.is_empty()).count();
        let ui: Vec<AssetSlot> = UI_SLOTS.iter().map(|(id, l, sp)| slot(st, id, l, sp, false, "")).collect();
        let icons: Vec<AssetSlot> = ICON_INFO.iter().map(|(id, l, g)| slot(st, id, l, "128x128", false, g)).collect();
        let custom = icons.iter().filter(|s| !s.file.is_empty()).count();
        let mut groups: Vec<IconGroup> = Vec::new();
        for g in ["Core System Apps", "Gaming & Social", "System Utilities & Services"] {
            let items: Vec<AssetSlot> = icons
                .iter()
                .filter(|s| s.group == g)
                .filter(|s| search.is_empty() || s.label.to_lowercase().contains(&search) || s.id.to_lowercase().contains(&search))
                .filter(|s| match filter {
                    1 => !s.file.is_empty(),
                    2 => s.file.is_empty(),
                    _ => true,
                })
                .cloned()
                .collect();
            if !items.is_empty() {
                groups.push(IconGroup { name: g.into(), items: ModelRc::new(VecModel::from(items)) });
            }
        }
        let pages: Vec<PageSlot> = st
            .pages
            .clone()
            .iter()
            .enumerate()
            .map(|(i, p)| {
                let (img, info) = thumb(st, &p.src);
                PageSlot {
                    page: i as i32 + 2,
                    file: file_name(&p.src).into(),
                    info: info.into(),
                    thumb: img,
                    has_thumb: true,
                    wave: p.wave,
                    color: p.color.clone().into(),
                    color_value: parse_argb(&p.color),
                }
            })
            .collect();
        let colors: Vec<ColorSlot> = DEFAULT_COLORS
            .iter()
            .filter(|(id, ..)| *id != "m_fontColor")
            .map(|(id, label, group, def)| {
                let hex = st.colors.get(*id).cloned().unwrap_or_else(|| def.to_string());
                ColorSlot { id: (*id).into(), label: (*label).into(), group: (*group).into(), value: parse_argb(&hex), hex: hex.into() }
            })
            .collect();
        let font = st.colors.get("m_fontColor").cloned().unwrap_or_else(|| "ffffffff".into());

        c.set_main_slots(ModelRc::new(VecModel::from(main)));
        c.set_required_ready(ready as i32);
        c.set_ui_slots(ModelRc::new(VecModel::from(ui)));
        c.set_icons_custom(custom as i32);
        c.set_prev_icons(ModelRc::new(VecModel::from(icons.iter().map(|s| s.thumb.clone()).collect::<Vec<_>>())));
        c.set_icons(ModelRc::new(VecModel::from(icons)));
        c.set_icon_groups(ModelRc::new(VecModel::from(groups)));
        c.set_pages(ModelRc::new(VecModel::from(pages)));
        let (start, bar): (Vec<ColorSlot>, Vec<ColorSlot>) = colors.into_iter().partition(|c| c.group == "Start Screen");
        c.set_colors(ModelRc::new(VecModel::from(start)));
        c.set_bar_colors(ModelRc::new(VecModel::from(bar)));
        c.set_font_color(font.clone().into());
        c.set_font_color_value(parse_argb(&font));
        c.set_use_global_font(st.use_global);
        c.set_dirty(st.dirty);
        c.set_editing(st.editing.is_some());
        c.set_target(st.target.clone().into());
        set_validation(&c, st, ready);

        let prev = |st: &mut State, id: &str| {
            st.sources.get(id).cloned().filter(|p| !p.is_empty()).map(|p| thumb(st, &p).0).unwrap_or_default()
        };
        c.set_prev_livearea(prev(st, "preview_livearea.png"));
        c.set_prev_lock(prev(st, "preview_lockscreen.png"));
        c.set_prev_thumb(prev(st, "preview_thumbnail.png"));
        let sel = c.get_selected_page();
        let sel_img = usize::try_from(sel).ok().and_then(|i| st.pages.get(i)).map(|p| p.src.clone());
        c.set_selected_page_image(sel_img.map(|p| thumb(st, &p).0).unwrap_or_default());
        if sel >= st.pages.len() as i32 {
            c.set_selected_page(st.pages.len() as i32 - 1);
        }

        c.set_has_bgm(!st.bgm.is_empty());
        c.set_bgm_name(file_name(&st.bgm).into());
        let encodes = build::bgm_needs_encoding(&st.bgm);
        c.set_bgm_encodes(encodes);
        c.set_bgm_info(
            if st.bgm.is_empty() {
                format!("Optional: {} or .at9. Encoded to the Vita's AT9 format on build.", build::ENCODABLE_AUDIO_EXTS.join(", "))
            } else if encodes {
                "Will be encoded to AT9 (Vita BGM size limit) when you build.".to_string()
            } else {
                "AT9 file: copied into the theme as-is.".to_string()
            }
            .into(),
        );
    });
}

fn mark_dirty(app: &AppWindow) {
    STATE.with(|s| s.borrow_mut().dirty = true);
    app.global::<Creator>().set_dirty(true);
}

fn reset(app: &AppWindow) {
    STATE.with(|s| {
        let mut st = s.borrow_mut();
        let templates = std::mem::take(&mut st.templates);
        *st = State { use_global: true, templates, ..Default::default() };
        st.colors = DEFAULT_COLORS.iter().map(|(id, _, _, v)| (id.to_string(), v.to_string())).collect();
    });
    let c = app.global::<Creator>();
    c.set_tab(0);
    c.set_preview_tab(0);
    c.set_selected_page(-1);
    c.set_icon_search("".into());
    c.set_icon_filter(0);
    c.set_ai_available(cfg!(feature = "icon-ai"));
}

/// Start a new theme (v2: title "My New Theme", author from settings).
pub fn new_theme(app: &AppWindow, settings: &SharedSettings) {
    reset(app);
    STATE.with(|s| s.borrow_mut().target = "a new folder in your theme library".into());
    let c = app.global::<Creator>();
    c.set_title("My New Theme".into());
    c.set_author(settings.borrow().str_or("default_author", "Your Name").into());
    c.set_version("01.00".into());
    c.set_loop_start("0".into());
    refresh(app);
    app.global::<Shell>().set_page(crate::Page::Creator);
}

/// Load a library theme into the creator (v2 `load_theme_data_into_creator`).
pub fn edit_theme(app: &AppWindow, theme: &LibraryTheme) {
    reset(app);
    let data = &theme.data;
    STATE.with(|s| {
        let mut st = s.borrow_mut();
        st.editing = Some(theme.path.clone());
        st.target = theme.path.display().to_string();
        for (id, rel) in &data.assets {
            let full = theme.path.join(rel);
            if !full.exists() {
                continue;
            }
            let full = full.to_string_lossy().into_owned();
            if id == "bgm.at9" {
                st.bgm = full;
            } else if !id.starts_with("tbr") && !(id.starts_with("br") && id != "br1.png") {
                st.sources.insert(id.clone(), full);
            }
        }
        // Colours: v2 took page 1's font colour as the global/page-1 colour.
        for (id, hex) in &data.colors {
            st.colors.insert(id.clone(), normalise_hex(hex));
        }
        let first = data
            .page_colors
            .get("br1_color")
            .map(|c| normalise_hex(c))
            .unwrap_or_else(|| st.colors.get("m_fontColor").cloned().unwrap_or_else(|| "ffffffff".into()));
        st.colors.insert("m_fontColor".into(), first.clone());
        let mut all_same = true;
        // Pages 2..10 must be contiguous (v2 cascade-cleared after a gap).
        for i in 2..=10 {
            let Some(rel) = data.assets.get(&format!("br{i}.png")) else { break };
            let full = theme.path.join(rel);
            if !full.exists() {
                break;
            }
            let wave = data.page_waves.get(&format!("br{i}_wave")).and_then(|w| w.trim().parse().ok()).unwrap_or(12);
            let color = data.page_colors.get(&format!("br{i}_color")).map(|c| normalise_hex(c)).unwrap_or_else(|| first.clone());
            if color != first {
                all_same = false;
            }
            st.pages.push(Page { src: full.to_string_lossy().into_owned(), wave, color });
        }
        st.use_global = all_same;
    });
    let c = app.global::<Creator>();
    c.set_title(data.title.clone().unwrap_or_default().into());
    c.set_author(data.author.clone().unwrap_or_default().into());
    c.set_version(data.version.clone().unwrap_or_else(|| "01.00".into()).into());
    c.set_loop_start("0".into());
    refresh(app);
    app.global::<Shell>().set_page(crate::Page::Creator);
}

fn pick_image(title: &str) -> Option<String> {
    rfd::FileDialog::new()
        .set_title(title)
        .add_filter("Images", &["png", "jpg", "jpeg"])
        .pick_file()
        .map(|p| p.to_string_lossy().into_owned())
}

fn build(app: &AppWindow, library: &SharedLibrary) {
    let c = app.global::<Creator>();
    let input = STATE.with(|s| {
        let st = s.borrow();
        let mut input = BuildInput {
            title: c.get_title().to_string(),
            author: c.get_author().to_string(),
            version: c.get_version().to_string(),
            sources: st.sources.clone(),
            bgm: st.bgm.clone(),
            bgm_loop_start: c.get_loop_start().trim().parse().ok(),
            colors: st.colors.clone(),
            use_global_font_color: st.use_global,
            ..Default::default()
        };
        for (i, p) in st.pages.iter().enumerate() {
            let n = i as u32 + 2;
            input.sources.insert(format!("br{n}.png"), p.src.clone());
            input.page_waves.insert(n, p.wave.to_string());
            input.page_colors.insert(n, p.color.clone());
        }
        input
    });
    let errors = build::validate(&input);
    if !errors.is_empty() {
        return dialogs::message(app, DialogIcon::Warning, "Validation Error", &errors.join("\n\n"));
    }
    let editing = STATE.with(|s| s.borrow().editing.clone());
    let dest = editing.clone().unwrap_or_else(|| vta_core::import::unique_theme_path(&library.borrow().root));
    let title = input.title.clone();
    let cancel = dialogs::progress_start(app, &format!("Building '{title}'…"), false);
    let _ = cancel;
    let weak = app.as_weak();
    let dest2 = dest.clone();
    std::thread::spawn(move || {
        let w2 = weak.clone();
        let progress = move |done: usize, total: usize, label: &str| {
            dialogs::progress_update(&w2, done as f32 / total.max(1) as f32, label.to_string());
        };
        let result = build::build_theme(&dest2, &input, &progress);
        let _ = weak.upgrade_in_event_loop(move |app| {
            dialogs::progress_close(&app);
            match result {
                Ok(warnings) => {
                    STATE.with(|s| {
                        let mut st = s.borrow_mut();
                        st.dirty = false;
                        st.editing = Some(dest.clone());
                        st.thumbs.clear();
                    });
                    crate::library_actions::with_state(|lib| {
                        rescan(&app, lib);
                        let idx = lib.borrow().visible.iter().position(|&i| lib.borrow().all[i].path == dest);
                        if let Some(i) = idx {
                            select(&app, lib, i as i32);
                        }
                    });
                    app.global::<Shell>().set_page(crate::Page::Library);
                    let mut text = format!("Theme '{title}' saved successfully!");
                    if !warnings.is_empty() {
                        text.push_str(&format!("\n\n{}", warnings.join("\n\n")));
                    }
                    crate::notify_done(&app);
                    let icon = if warnings.is_empty() { DialogIcon::Success } else { DialogIcon::Warning };
                    dialogs::message(&app, icon, "Success", &text);
                }
                Err(e) => dialogs::message(&app, DialogIcon::Error, "Build Error", &e),
            }
        });
    });
}

/// Leave the creator (asks first when there are unsaved changes).
pub fn leave(app: &AppWindow, then: impl FnOnce(&AppWindow) + 'static) {
    if STATE.with(|s| s.borrow().dirty) {
        dialogs::confirm(app, DialogIcon::Warning, "Discard changes?", "This theme has unsaved changes. Leave the Theme Creator and discard them?", "Discard", true, move |app| {
            STATE.with(|s| s.borrow_mut().dirty = false);
            then(app);
        });
    } else {
        then(app);
    }
}

/// What the AI icon generator needs (v2 `open_icon_generator`): the title, the
/// theme's wallpapers (lock screen + home pages, each picture once) and every
/// colour set in the creator (AARRGGBB).
pub fn icon_generator_inputs(app: &AppWindow) -> (String, Vec<PathBuf>, Vec<String>) {
    let title = app.global::<Creator>().get_title().to_string();
    STATE.with(|s| {
        let st = s.borrow();
        let mut paths: Vec<String> = Vec::new();
        for id in ["lockpaper.png", "br1.png"] {
            if let Some(p) = st.sources.get(id).filter(|p| !p.is_empty()) {
                paths.push(p.clone());
            }
        }
        paths.extend(st.pages.iter().map(|p| p.src.clone()));
        let mut seen = std::collections::HashSet::new();
        let wallpapers: Vec<PathBuf> = paths
            .into_iter()
            .filter(|p| Path::new(p).is_file())
            .filter(|p| seen.insert(vta_core::paths::normcase(&vta_core::paths::normpath(Path::new(p)))))
            .map(PathBuf::from)
            .collect();
        let mut colors: Vec<String> = st.colors.values().cloned().collect();
        colors.extend(st.pages.iter().map(|p| p.color.clone()));
        (title, wallpapers, colors)
    })
}

/// "Use Generated Icons": put the generated icons into the icon slots.
pub fn use_generated_icons(app: &AppWindow, icons: &BTreeMap<String, PathBuf>) {
    STATE.with(|s| {
        let mut st = s.borrow_mut();
        for (name, path) in icons {
            st.sources.insert(name.clone(), path.to_string_lossy().into_owned());
        }
        st.dirty = true;
    });
    refresh(app);
}

/// Source files currently used by the creator (to know which generated sets are still needed).
pub fn used_sources() -> Vec<PathBuf> {
    STATE.with(|s| s.borrow().sources.values().map(PathBuf::from).collect())
}

pub fn is_dirty() -> bool {
    STATE.with(|s| s.borrow().dirty)
}

fn open_color_picker(app: &AppWindow, id: &str) {
    let (hex, label) = STATE.with(|s| {
        let st = s.borrow();
        if let Some(n) = id.strip_prefix("br").and_then(|r| r.strip_suffix("_color")).and_then(|n| n.parse::<usize>().ok()) {
            (st.pages.get(n - 2).map(|p| p.color.clone()).unwrap_or_else(|| "ffffffff".into()), format!("Home Page {n} Font Color"))
        } else {
            let label = DEFAULT_COLORS.iter().find(|(c, ..)| *c == id).map(|(_, l, ..)| l.to_string()).unwrap_or_default();
            (st.colors.get(id).cloned().unwrap_or_else(|| "ffffffff".into()), label)
        }
    });
    STATE.with(|s| s.borrow_mut().picking = Some(id.to_string()));
    let p = app.global::<ColorPicker>();
    set_picker_hex(app, &hex);
    p.set_title(label.into());
    p.set_open(true);
}

fn set_picker_hex(app: &AppWindow, hex: &str) {
    let p = app.global::<ColorPicker>();
    let h = normalise_hex(hex);
    if let Ok(v) = u32::from_str_radix(&h, 16) {
        if h.len() == 8 {
            p.set_a((v >> 24) as f32);
            p.set_r(((v >> 16) & 0xff) as f32);
            p.set_g(((v >> 8) & 0xff) as f32);
            p.set_b((v & 0xff) as f32);
        }
    }
    p.set_hex(h.into());
}

fn picker_hex(app: &AppWindow) -> String {
    let p = app.global::<ColorPicker>();
    let c = |v: f32| v.round().clamp(0.0, 255.0) as u8;
    format!("{:02x}{:02x}{:02x}{:02x}", c(p.get_a()), c(p.get_r()), c(p.get_g()), c(p.get_b()))
}

pub fn wire(app: &AppWindow, library: &SharedLibrary, settings: &SharedSettings) {
    let c = app.global::<Creator>();
    let a = app.as_weak();
    c.on_edited(move || mark_dirty(&a.unwrap()));

    let a = app.as_weak();
    c.on_browse(move |id| {
        let app = a.unwrap();
        if let Some(p) = pick_image(&format!("Select {id}")) {
            STATE.with(|s| s.borrow_mut().sources.insert(id.to_string(), p));
            mark_dirty(&app);
            refresh(&app);
        }
    });
    let a = app.as_weak();
    c.on_clear(move |id| {
        let app = a.unwrap();
        STATE.with(|s| s.borrow_mut().sources.remove(id.as_str()));
        mark_dirty(&app);
        refresh(&app);
    });
    let a = app.as_weak();
    c.on_add_page(move || {
        let app = a.unwrap();
        let n = STATE.with(|s| s.borrow().pages.len()) + 2;
        if n > 10 {
            return;
        }
        if let Some(p) = pick_image(&format!("Select Home Page {n}")) {
            STATE.with(|s| {
                let mut st = s.borrow_mut();
                let color = st.colors.get("m_fontColor").cloned().unwrap_or_else(|| "ffffffff".into());
                st.pages.push(Page { src: p, wave: 12, color });
            });
            app.global::<Creator>().set_selected_page(n as i32 - 2);
            mark_dirty(&app);
            refresh(&app);
        }
    });
    let a = app.as_weak();
    c.on_browse_page(move |page| {
        let app = a.unwrap();
        if let Some(p) = pick_image(&format!("Select Home Page {page}")) {
            STATE.with(|s| {
                if let Some(pg) = s.borrow_mut().pages.get_mut(page as usize - 2) {
                    pg.src = p;
                }
            });
            mark_dirty(&app);
            refresh(&app);
        }
    });
    let a = app.as_weak();
    c.on_remove_page(move |page| {
        let app = a.unwrap();
        // As in v2: clearing a page also clears every page after it (pages must
        // stay contiguous; v2 cascade-cleared the later page inputs).
        STATE.with(|s| s.borrow_mut().pages.truncate(page as usize - 2));
        mark_dirty(&app);
        refresh(&app);
    });
    let a = app.as_weak();
    c.on_set_wave(move |page, wave| {
        let app = a.unwrap();
        STATE.with(|s| {
            if let Some(pg) = s.borrow_mut().pages.get_mut(page as usize - 2) {
                pg.wave = wave;
            }
        });
        mark_dirty(&app);
    });
    let a = app.as_weak();
    c.on_global_font_toggled(move |on| {
        let app = a.unwrap();
        STATE.with(|s| s.borrow_mut().use_global = on);
        mark_dirty(&app);
        refresh(&app);
    });
    let a = app.as_weak();
    c.on_icon_filter_changed(move || refresh(&a.unwrap()));
    let a = app.as_weak();
    c.on_pick_color(move |id| open_color_picker(&a.unwrap(), &id));
    let a = app.as_weak();
    c.on_browse_bgm(move || {
        let app = a.unwrap();
        let mut exts: Vec<&str> = build::ENCODABLE_AUDIO_EXTS.iter().map(|e| e.trim_start_matches('.')).collect();
        exts.push("at9");
        if let Some(p) = rfd::FileDialog::new().set_title("Select Audio File").add_filter("Audio", &exts).pick_file() {
            STATE.with(|s| s.borrow_mut().bgm = p.to_string_lossy().into_owned());
            mark_dirty(&app);
            refresh(&app);
        }
    });
    let a = app.as_weak();
    c.on_clear_bgm(move || {
        let app = a.unwrap();
        STATE.with(|s| s.borrow_mut().bgm.clear());
        mark_dirty(&app);
        refresh(&app);
    });
    let (a, l) = (app.as_weak(), library.clone());
    c.on_build(move || build(&a.unwrap(), &l));
    let a = app.as_weak();
    c.on_cancel(move || {
        leave(&a.unwrap(), |app| app.global::<Shell>().set_page(crate::Page::Library));
    });
    let a = app.as_weak();
    c.on_open_icon_generator(move || {
        let app = a.unwrap();
        crate::icongen_view::open(&app);
    });
    let _ = settings;

    // Colour picker
    let p = app.global::<ColorPicker>();
    let a = app.as_weak();
    p.on_channels_changed(move || {
        let app = a.unwrap();
        let hex = picker_hex(&app);
        app.global::<ColorPicker>().set_hex(hex.into());
    });
    let a = app.as_weak();
    p.on_hex_edited(move |text| {
        let app = a.unwrap();
        let h = normalise_hex(&text);
        if h.len() == 8 && u32::from_str_radix(&h, 16).is_ok() {
            set_picker_hex(&app, &h);
        }
    });
    let a = app.as_weak();
    p.on_done(move |ok| {
        let app = a.unwrap();
        app.global::<ColorPicker>().set_open(false);
        let Some(id) = STATE.with(|s| s.borrow_mut().picking.take()) else { return };
        if !ok {
            return;
        }
        let hex = picker_hex(&app);
        STATE.with(|s| {
            let mut st = s.borrow_mut();
            if let Some(n) = id.strip_prefix("br").and_then(|r| r.strip_suffix("_color")).and_then(|n| n.parse::<usize>().ok()) {
                if let Some(pg) = st.pages.get_mut(n - 2) {
                    pg.color = hex.clone();
                }
            } else {
                st.colors.insert(id.clone(), hex.clone());
            }
        });
        mark_dirty(&app);
        refresh(&app);
    });

    // Library "Edit" opens the creator on that theme.
    let (a, l) = (app.as_weak(), library.clone());
    app.global::<Library>().on_edit(move |i| {
        let app = a.unwrap();
        if let Some(t) = crate::library_view::theme_at(&l, i) {
            let t2 = t.clone();
            leave(&app, move |app| edit_theme(app, &t2));
        }
    });
}

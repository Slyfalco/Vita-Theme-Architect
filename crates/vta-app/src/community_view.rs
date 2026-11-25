//! Community Browser controller (v2 `theme_downloader.py`): pages of the online
//! theme list, previews, details, download & import, extras.

use crate::dialogs;
use crate::{AppWindow, Community, DialogIcon, PageButton, RemoteTheme};
use slint::{ComponentHandle, Image, ModelRc, SharedPixelBuffer, Rgba8Pixel, SharedString, VecModel};
use std::cell::RefCell;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use vta_online::{Entry, Field, Filter, Sort};

#[derive(Default)]
struct State {
    entries: Vec<Entry>,
    previews: HashMap<String, Image>,
    search_timer: Option<slint::Timer>,
    extras_root: PathBuf,
    settings: Option<crate::library_actions::SharedSettings>,
}

thread_local! {
    static STATE: RefCell<State> = RefCell::new(State::default());
}

/// Bumped whenever the shown page changes; late preview downloads are dropped.
static GENERATION: AtomicU64 = AtomicU64::new(0);

const FIELDS: [Field; 3] = [Field::Title, Field::Author, Field::Tags];
const SORTS: [Sort; 5] = [Sort::Newest, Sort::Oldest, Sort::Az, Sort::Za, Sort::Downloads];
// Matches the category chips in community.slint (All / Verified / Top Rated / Staff Picks).
const FILTERS: [Filter; 4] = [Filter::All, Filter::Verified, Filter::TopRated, Filter::StaffPicks];
/// Per-page options for the dropdown (v2 used a fixed 50); default is the last.
const PAGE_SIZES: [usize; 3] = [12, 24, 50];

fn compact_count(n: i64) -> String {
    if n >= 1_000_000 {
        format!("{:.1}M", n as f64 / 1e6)
    } else if n >= 1000 {
        format!("{:.1}k", n as f64 / 1e3)
    } else {
        n.to_string()
    }
}

fn with_commas(n: i64) -> String {
    let s = n.abs().to_string();
    let mut out = String::new();
    for (i, c) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    if n < 0 {
        format!("-{out}")
    } else {
        out
    }
}

/// v2 `convert_epoch_to_str` ('%d %B %Y'), in UTC.
fn epoch_to_date(text: &str) -> String {
    let Ok(secs) = text.trim().parse::<i64>() else { return text.to_string() };
    let days = secs.div_euclid(86_400);
    // Civil-from-days (Howard Hinnant).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    const MONTHS: [&str; 12] =
        ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
    format!("{d:02} {} {y}", MONTHS[(m - 1) as usize])
}

/// v2 `_page_numbers`: first, last, current ±1, with gaps (0 = "…").
fn page_numbers(current: i32, total: i32) -> Vec<i32> {
    let mut wanted: Vec<i32> = vec![1, total, current - 1, current, current + 1];
    wanted.sort();
    wanted.dedup();
    let mut out = Vec::new();
    let mut prev = 0;
    for p in wanted.into_iter().filter(|p| (1..=total).contains(p)) {
        if prev > 0 && p - prev > 1 {
            out.push(0);
        }
        out.push(p);
        prev = p;
    }
    out
}

fn to_image(buf: Option<SharedPixelBuffer<Rgba8Pixel>>) -> Option<Image> {
    buf.map(Image::from_rgba8)
}

/// Download an image and decode it (optionally downscaled) off the UI thread.
fn fetch_image(url: &str, max_w: u32) -> Option<SharedPixelBuffer<Rgba8Pixel>> {
    if url.is_empty() {
        return None;
    }
    let agent: ureq::Agent = ureq::Agent::config_builder().timeout_global(Some(std::time::Duration::from_secs(20))).build().into();
    let bytes = agent.get(&vta_online::secure_url(url)).call().ok()?.body_mut().with_config().limit(20 << 20).read_to_vec().ok()?;
    let mut img = image::load_from_memory(&bytes).ok()?;
    if img.width() > max_w {
        img = img.resize(max_w, max_w * img.height() / img.width().max(1), image::imageops::FilterType::Triangle);
    }
    let rgba = img.to_rgba8();
    let (w, h) = rgba.dimensions();
    Some(SharedPixelBuffer::clone_from_slice(rgba.as_raw(), w, h))
}

fn rows(entries: &[Entry], previews: &HashMap<String, Image>) -> Vec<RemoteTheme> {
    entries
        .iter()
        .enumerate()
        .map(|(i, e)| {
            let url = e.str("home_preview");
            let preview = previews.get(&url).cloned();
            RemoteTheme {
                index: i as i32,
                title: e.str("title").into(),
                author: e.str("author").into(),
                version: e.str("version").into(),
                downloads: compact_count(e.num("downloads")).into(),
                has_preview: preview.is_some(),
                preview: preview.unwrap_or_default(),
                verified: e.verified(),
                stars: e.stars(),
                rating: if e.has_validation() { format!("{:.1}", e.stars()) } else { String::new() }.into(),
            }
        })
        .collect()
}

/// Show one page for the current search/sort (v2 `show_page`).
pub fn show_page(app: &AppWindow, page: i32, fresh: bool) {
    let c = app.global::<Community>();
    let search = c.get_search().trim().to_string();
    let field = FIELDS[c.get_field().clamp(0, 2) as usize];
    let sort = SORTS[c.get_sort().clamp(0, 4) as usize];
    let filter = FILTERS[c.get_filter().clamp(0, 3) as usize];
    let per_page = PAGE_SIZES[c.get_page_size().clamp(0, PAGE_SIZES.len() as i32 - 1) as usize];
    c.set_loading(true);
    c.set_error("".into());
    let generation = GENERATION.fetch_add(1, Ordering::SeqCst) + 1;
    let weak = app.as_weak();
    std::thread::spawn(move || {
        let result = vta_online::count(&search, field, filter, fresh).and_then(|total| {
            let pages = total.div_ceil(per_page).max(1) as i32;
            let page = page.clamp(1, pages);
            vta_online::get(&search, field, filter, sort, page as usize, per_page, false).map(|list| (total, pages, page, list))
        });
        let _ = weak.upgrade_in_event_loop(move |app| {
            if GENERATION.load(Ordering::SeqCst) != generation {
                return;
            }
            let c = app.global::<Community>();
            c.set_loading(false);
            match result {
                Err(e) => c.set_error(format!("Could not fetch theme list:\n{e}").into()),
                Ok((total, pages, page, list)) => {
                    c.set_total_text(if total > 0 { format!("{} theme{}", with_commas(total as i64), if total == 1 { "" } else { "s" }) } else { "No themes found".into() }.into());
                    c.set_page(page);
                    c.set_pages(pages);
                    c.set_goto_page(page.to_string().into());
                    let pager: Vec<PageButton> = page_numbers(page, pages).into_iter().map(|number| PageButton { number }).collect();
                    c.set_pager(ModelRc::new(VecModel::from(pager)));
                    c.set_selected(-1);
                    c.set_list_version(c.get_list_version() + 1);
                    let urls: Vec<String> = list.iter().map(|e| e.str("home_preview")).collect();
                    STATE.with(|s| {
                        let mut st = s.borrow_mut();
                        st.entries = list;
                        c.set_themes(ModelRc::new(VecModel::from(rows(&st.entries, &st.previews))));
                    });
                    load_previews(&app, urls, generation);
                }
            }
        });
    });
}

/// Fetch card previews with a few worker threads.
fn load_previews(app: &AppWindow, urls: Vec<String>, generation: u64) {
    let missing: Vec<(usize, String)> = STATE.with(|s| {
        let st = s.borrow();
        urls.into_iter().enumerate().filter(|(_, u)| !u.is_empty() && !st.previews.contains_key(u)).collect()
    });
    let queue = Arc::new(Mutex::new(missing));
    for _ in 0..6 {
        let (queue, weak) = (queue.clone(), app.as_weak());
        std::thread::spawn(move || loop {
            if GENERATION.load(Ordering::SeqCst) != generation {
                return;
            }
            let Some((_, url)) = queue.lock().unwrap().pop() else { return };
            let buf = fetch_image(&url, 480);
            let _ = weak.upgrade_in_event_loop(move |app| {
                let Some(img) = to_image(buf) else { return };
                STATE.with(|s| {
                    let mut st = s.borrow_mut();
                    st.previews.insert(url, img);
                    if GENERATION.load(Ordering::SeqCst) == generation {
                        app.global::<Community>().set_themes(ModelRc::new(VecModel::from(rows(&st.entries, &st.previews))));
                    }
                });
            });
        });
    }
}

fn select(app: &AppWindow, index: i32) {
    if index < 0 {
        // Deselect: the detail panel slides away.
        app.global::<Community>().set_selected(-1);
        return;
    }
    let Some(e) = STATE.with(|s| s.borrow().entries.get(index as usize).cloned()) else { return };
    let c = app.global::<Community>();
    c.set_selected(index);
    c.set_d_title(e.str("title").into());
    c.set_d_author(e.str("author").into());
    c.set_d_version({
        let v = e.str("version");
        if v.is_empty() { "N/A".into() } else { v.into() }
    });
    c.set_d_downloads(with_commas(e.num("downloads")).into());
    c.set_d_added(epoch_to_date(&e.str("uid")).into());
    c.set_d_updated(epoch_to_date(&e.str("id")).into());
    let (w, er) = (e.str("warnings"), e.str("errors"));
    c.set_d_validation(format!("{} Warnings, {} Errors", if w.is_empty() { "0".into() } else { w }, if er.is_empty() { "0".into() } else { er }).into());
    c.set_d_vita_validated(e.vita_validated());
    c.set_d_verified(e.verified());
    c.set_d_stars(e.stars());
    c.set_d_rating(if e.has_validation() { format!("{:.1}", e.stars()) } else { String::new() }.into());
    c.set_d_votes(if e.has_validation() { format!("{} checks", e.validation_votes()) } else { String::new() }.into());
    // v2: tags from 'tags', or from a comma-separated description when tags is empty.
    let (tags_s, desc) = (e.str("tags"), e.str("description"));
    let (tags, description): (Vec<String>, String) = if desc.contains(',') && tags_s.is_empty() {
        (desc.split(',').map(|t| t.trim().to_string()).filter(|t| !t.is_empty()).collect(), String::new())
    } else {
        let tags = tags_s.split(',').map(|t| t.trim().to_string()).filter(|t| !t.is_empty()).collect();
        (tags, if desc.is_empty() { "No description provided.".into() } else { desc })
    };
    c.set_d_description(description.into());
    c.set_d_tags(ModelRc::new(VecModel::from(tags.into_iter().map(SharedString::from).collect::<Vec<_>>())));
    c.set_d_has_extra(!e.str("extra").is_empty());
    c.set_d_home_loaded(false);
    c.set_d_lock_loaded(false);
    for (key, is_home) in [("home_preview", true), ("lockscreen_preview", false)] {
        let url = e.str(key);
        let cached = STATE.with(|s| s.borrow().previews.get(&format!("full:{url}")).cloned());
        if let Some(img) = cached {
            if is_home {
                c.set_d_home(img);
                c.set_d_home_loaded(true);
            } else {
                c.set_d_lock(img);
                c.set_d_lock_loaded(true);
            }
            continue;
        }
        let weak = app.as_weak();
        std::thread::spawn(move || {
            let buf = fetch_image(&url, 960);
            let _ = weak.upgrade_in_event_loop(move |app| {
                let c = app.global::<Community>();
                if c.get_selected() != index {
                    return;
                }
                let img = to_image(buf);
                if let Some(img) = &img {
                    STATE.with(|s| s.borrow_mut().previews.insert(format!("full:{url}"), img.clone()));
                }
                if is_home {
                    c.set_d_home(img.unwrap_or_default());
                    c.set_d_home_loaded(true);
                } else {
                    c.set_d_lock(img.unwrap_or_default());
                    c.set_d_lock_loaded(true);
                }
            });
        });
    }
}

fn safe_name(title: &str) -> String {
    let s: String = title.chars().filter(|c| c.is_alphanumeric() || *c == ' ' || *c == '_').collect();
    s.trim_end().to_string()
}

/// Download `url` to a temp file behind the progress overlay, then `done`.
fn download(app: &AppWindow, url: String, label: String, name: String, done: impl FnOnce(&AppWindow, PathBuf) + Send + 'static) {
    let cancel = dialogs::progress_start(app, &label, true);
    let weak = app.as_weak();
    std::thread::spawn(move || {
        let dest = std::env::temp_dir().join(format!("{name}.download"));
        let result = (|| -> Result<Option<PathBuf>, String> {
            let agent: ureq::Agent = ureq::Agent::config_builder().timeout_global(Some(std::time::Duration::from_secs(600))).build().into();
            let mut resp = agent.get(&url).call().map_err(|e| e.to_string())?;
            let total: u64 = resp.headers().get("content-length").and_then(|v| v.to_str().ok()).and_then(|v| v.parse().ok()).unwrap_or(0);
            let mut reader = resp.body_mut().with_config().limit(u64::MAX).reader();
            let mut file = std::fs::File::create(&dest).map_err(|e| e.to_string())?;
            let mut buf = vec![0u8; 1 << 16];
            let mut got = 0u64;
            loop {
                if cancel.load(Ordering::SeqCst) {
                    drop(file);
                    let _ = std::fs::remove_file(&dest);
                    return Ok(None);
                }
                let n = std::io::Read::read(&mut reader, &mut buf).map_err(|e| e.to_string())?;
                if n == 0 {
                    break;
                }
                std::io::Write::write_all(&mut file, &buf[..n]).map_err(|e| e.to_string())?;
                got += n as u64;
                let v = if total > 0 { got as f32 / total as f32 } else { -1.0 };
                dialogs::progress_update(&weak, v, crate::library_view::format_size(got));
            }
            Ok(Some(dest.clone()))
        })();
        let _ = weak.upgrade_in_event_loop(move |app| {
            dialogs::progress_close(&app);
            match result {
                Ok(Some(path)) => done(&app, path),
                Ok(None) => {}
                Err(e) => dialogs::message(&app, DialogIcon::Error, "Download Error", &format!("Could not download the file:\n{e}")),
            }
        });
    });
}

fn download_theme(app: &AppWindow) {
    download_and_import(app, app.global::<Community>().get_selected());
}

/// Download + import the theme at `index`; if "Auto-deploy to Vita" is ticked and
/// a Vita is configured, send it on afterwards (Quick Install + auto-deploy).
fn download_and_import(app: &AppWindow, index: i32) {
    let Some(e) = STATE.with(|s| s.borrow().entries.get(index as usize).cloned()) else { return };
    let title = e.str("title");
    let url = vta_online::secure_url(&e.str("zip"));
    let auto_deploy = app.global::<Community>().get_auto_deploy();
    download(app, url, format!("Downloading '{title}'..."), safe_name(&title), move |app, path| {
        crate::library_actions::with_state(|lib| {
            let root = lib.borrow().root.clone();
            let result = vta_core::import::import_zip(&root, &path);
            let _ = std::fs::remove_file(&path);
            match result {
                Ok(t) => {
                    crate::library_view::rescan(app, lib);
                    if auto_deploy {
                        deploy_after_import(app, lib, &t);
                    } else {
                        dialogs::message(app, DialogIcon::Success, "Success", &format!("{t} was downloaded and imported"));
                    }
                }
                Err((t, m)) => dialogs::message(app, DialogIcon::Error, &t, &m),
            }
        });
    });
}

fn deploy_after_import(app: &AppWindow, lib: &crate::library_view::SharedLibrary, title: &str) {
    let settings = STATE.with(|s| s.borrow().settings.clone());
    match (settings, crate::library_actions::theme_by_title(lib, title)) {
        (Some(settings), Some(theme)) => crate::library_actions::deploy_theme(app, &settings, theme),
        _ => dialogs::message(
            app,
            DialogIcon::Success,
            "Imported",
            &format!("{title} was downloaded and imported (couldn't auto-deploy; set the Vita connection in Settings)."),
        ),
    }
}

fn download_extra(app: &AppWindow) {
    let index = app.global::<Community>().get_selected();
    let Some(e) = STATE.with(|s| s.borrow().entries.get(index as usize).cloned()) else { return };
    let extra = e.str("extra");
    if extra.is_empty() {
        return;
    }
    // v2: a bare value (not a URL) is a path on the repository site.
    let url = if extra.contains("://") { extra } else { format!("https://psvt.ovh/{extra}") };
    let title = e.str("title");
    let target = STATE.with(|s| s.borrow().extras_root.join(safe_name(&title)));
    download(app, url, format!("Downloading extra for '{title}'..."), format!("{}_extra", safe_name(&title)), move |app, path| {
        let _ = std::fs::create_dir_all(&target);
        let is_zip = std::fs::read(&path).map(|b| b.starts_with(b"PK")).unwrap_or(false);
        let result = if is_zip { vta_core::import::extract_zip(&path, &target) } else { Err("not a zip".into()) };
        let _ = std::fs::remove_file(&path);
        match result {
            Ok(()) => dialogs::message(app, DialogIcon::Success, "Extra Downloaded", &format!("Extra files for '{title}' extracted successfully to:\n{}", target.display())),
            Err(_) if !is_zip => dialogs::message(app, DialogIcon::Warning, "Download Error", "The downloaded 'extra' file was not a valid .zip file and could not be extracted."),
            Err(e) => dialogs::message(app, DialogIcon::Error, "Extraction Error", &format!("An error occurred during extraction: {e}")),
        }
    });
}

/// Called when the Community page is opened: always fetch the live list (v2).
pub fn opened(app: &AppWindow, extras_root: PathBuf) {
    STATE.with(|s| s.borrow_mut().extras_root = extras_root);
    if app.global::<Community>().get_available() {
        show_page(app, 1, true);
    }
}

pub fn wire(app: &AppWindow, settings: &crate::library_actions::SharedSettings) {
    STATE.with(|s| s.borrow_mut().settings = Some(settings.clone()));
    let c = app.global::<Community>();
    match vta_online::status() {
        Ok(()) => c.set_available(true),
        Err(reason) => {
            c.set_available(false);
            c.set_unavailable_reason(reason.into());
        }
    }
    let a = app.as_weak();
    c.on_search_changed(move |_| {
        // Wait for a pause in typing (v2: 300 ms).
        let weak = a.clone();
        let t = slint::Timer::default();
        t.start(slint::TimerMode::SingleShot, std::time::Duration::from_millis(300), move || {
            if let Some(app) = weak.upgrade() {
                show_page(&app, 1, false);
            }
        });
        STATE.with(|s| s.borrow_mut().search_timer = Some(t));
    });
    let a = app.as_weak();
    c.on_field_changed(move |_| show_page(&a.unwrap(), 1, false));
    let a = app.as_weak();
    c.on_sort_changed(move |_| show_page(&a.unwrap(), 1, false));
    let a = app.as_weak();
    c.on_filter_changed(move |_| show_page(&a.unwrap(), 1, false));
    let a = app.as_weak();
    c.on_page_size_changed(move |_| show_page(&a.unwrap(), 1, false));
    let a = app.as_weak();
    c.on_show_page(move |p| show_page(&a.unwrap(), p, false));
    let a = app.as_weak();
    c.on_reload(move || show_page(&a.unwrap(), 1, true));
    let a = app.as_weak();
    c.on_select(move |i| select(&a.unwrap(), i));
    let a = app.as_weak();
    c.on_tag_clicked(move |tag| {
        let app = a.unwrap();
        let c = app.global::<Community>();
        c.set_field(2);
        c.set_search(tag);
        show_page(&app, 1, false);
    });
    let a = app.as_weak();
    c.on_download(move || download_theme(&a.unwrap()));
    let a = app.as_weak();
    c.on_quick_install(move |i| {
        let app = a.unwrap();
        app.global::<Community>().invoke_select(i);
        download_and_import(&app, i);
    });
    let a = app.as_weak();
    c.on_download_extra(move || download_extra(&a.unwrap()));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pager_like_v2() {
        assert_eq!(page_numbers(5, 77), vec![1, 0, 4, 5, 6, 0, 77]);
        assert_eq!(page_numbers(1, 3), vec![1, 2, 3]);
        assert_eq!(page_numbers(1, 1), vec![1]);
    }

    #[test]
    fn dates() {
        assert_eq!(epoch_to_date("0"), "01 January 1970");
        assert_eq!(epoch_to_date("1723420800"), "12 August 2024");
        assert_eq!(epoch_to_date("N/A"), "N/A");
    }
}

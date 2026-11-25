//! "Submit to Repository" (v2 `submit_theme_to_repo`), kept identical: the theme
//! is zipped, the repository is checked for the same title (Theme Exists choice),
//! then the repository's submission page opens in an app window using the
//! system web engine (WebView2 / WKWebView / WebKitGTK — as pywebview did) and the
//! same scripts as v2 fill the theme package into the form.

use crate::library_view::{theme_at, SharedLibrary};
use crate::{dialogs, AppWindow, DialogIcon, ExistingTheme, SubmitWindow, ThemeExists};
use base64::Engine;
use slint::{ComponentHandle, Image, ModelRc, VecModel};
use std::cell::RefCell;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::Duration;
use vta_online::{Entry, Field, Sort, PAGE};
use wry::{PageLoadEvent, Rect, WebView, WebViewBuilder};

const NEW_URL: &str = "https://psvt.ovh/index.php#submit";
const SUBMIT_PREFIX: &str = "https://psvt.ovh/submit.php";

// --- Page scripts: verbatim from v2 ------------------------------------------

const NAV_REMOVAL: &str = r#"
        document.querySelectorAll(".ui-bar-a, .ui-page-theme-a .ui-bar-inherit, html .ui-bar-a .ui-bar-inherit, html .ui-body-a .ui-bar-inherit, html body .ui-group-theme-a .ui-bar-inherit").forEach(function(i) {if($(i.parentNode).attr('data-url') !== "/submit.php") {i.remove()}});
        "#;
const PADDING_CHANGE: &str = r#"
        const submitPage = document.getElementById('submit');
        submitPage.style.paddingTop = '0px'
        "#;
const NAV_THEME: &str = r#"
        (function tryClick(attempts) {
            var btn = document.querySelector('.ui-input-btn input');
            if (btn) { btn.click(); return; }
            if (attempts > 0) setTimeout(function() { tryClick(attempts - 1); }, 300);
        })(15);
        "#;
const BASE64_TO_FILE: &str = r#"
        function base64ToFile(base64, filename, mimeType) {
            // 1. Decode the (Base64 string to a binary string
            const byteCharacters = atob(base64);

            // 2. Create an ArrayBuffer to hold the binary data
            const byteNumbers = new Array(byteCharacters.length);
            for (let i = 0; i < byteCharacters.length; i++) {
                byteNumbers[i] = byteCharacters.charCodeAt(i);
            }
            const byteArray = new Uint8Array(byteNumbers);

            // 3. Create a Blob (a file-like object) from the binary data
            const blob = new Blob([byteArray], { type: mimeType });

            // 4. Create the final File object from the Blob
            // The File object is derived from Blob, but also includes name and lastModifiedDate.
            return new File([blob], filename, { type: mimeType });
        }
        "#;
const GUESS_MIME_TYPE: &str = r#"
        function guessMimeType(filename) {
            const ext = filename.split('.').pop().toLowerCase();
            switch (ext) {
                case 'zip': return 'application/zip'
                default: return 'error';
            }
        }
        "#;
const CANCEL_BUTTON: &str = r#"
        document.querySelectorAll(".ui-page-theme-a .ui-btn, html .ui-bar-a .ui-btn, html .ui-body-a .ui-btn, html body .ui-group-theme-a .ui-btn, html head + body .ui-btn.ui-btn-a, .ui-page-theme-a .ui-btn:visited, html .ui-bar-a .ui-btn:visited, html .ui-body-a .ui-btn:visited, html body .ui-group-theme-a .ui-btn:visited, html head + body .ui-btn.ui-btn-a:visited").forEach(function(i) {if($(i).attr('href') === "./index.php") {
            i.onclick = function (e) { e.preventDefault(); };

            i.addEventListener('click', async() => {
                await pywebview.api.cancel()
            })
        }});
        "#;
const FILE_TEXT: &str = r#"
        $('label[for="file"]').get()[0].innerText = "Your Custom Theme's package:"
        "#;
const OPEN_FILE_BUTTON: &str = r#"
        const openFileButton = document.getElementById('upl');
        openFileButton.onclick = function (e) { e.preventDefault(); };
        openFileButton.ondrop = function (e) { e.preventDefault(); };
        "#;
const GET_CONTENT: &str = r#"
        async function fetchData() {
            try {
                const result = await pywebview.api.get_content();
                if (result.status === 'success') {
                    const filename = result.filename;
                    const base64Content = result.content;
                    const mimeType = guessMimeType(filename);
                    if (mimeType !== 'error') {
                        const fileObject = base64ToFile(base64Content, filename, mimeType);
                        let s = document.getElementById('upl');
                        let list = new DataTransfer();
                        list.items.add(fileObject);
                        s.files = list.files;
                    }
                }
            } catch (_) {}
            await pywebview.api.ready();
        }
        "#;

/// The `pywebview.api` the scripts call, bridged to Rust over the IPC channel.
const API_SHIM: &str = r#"
window.pywebview = { api: {
    ready: function () { window.ipc.postMessage('ready'); return Promise.resolve(); },
    cancel: function () { window.ipc.postMessage('cancel'); return Promise.resolve(); },
    get_content: function () {
        return new Promise(function (resolve) { window.__vtaContent = resolve; window.ipc.postMessage('get_content'); });
    }
} };
"#;

// -----------------------------------------------------------------------------

struct Session {
    window: SubmitWindow,
    webview: Option<WebView>,
    is_update: bool,
    first_load_pending: bool,
    theme_page_handled: bool,
    run_once: u32,
    zip: PathBuf,
    shown: bool,
}

thread_local! {
    static SESSION: RefCell<Option<Session>> = const { RefCell::new(None) };
    static PENDING: RefCell<Option<(PathBuf, Vec<Entry>)>> = const { RefCell::new(None) };
}

/// Debug builds: trace the submission flow to %TEMP%/vta-submit.log.
fn trace(msg: &str) {
    #[cfg(debug_assertions)]
    {
        use std::io::Write as _;
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(std::env::temp_dir().join("vta-submit.log")) {
            let t = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0);
            let _ = writeln!(f, "{t} {msg}");
        }
    }
    #[cfg(not(debug_assertions))]
    let _ = msg;
}

fn cleanup_zip(path: &Path) {
    let _ = std::fs::remove_file(path);
}

fn safe_title(title: &str) -> String {
    let s: String = title.chars().filter(|c| c.is_alphanumeric() || *c == ' ' || *c == '_').collect();
    s.trim_end().to_string()
}

/// Zip a theme folder (paths relative to the folder), like v2.
fn zip_theme(dir: &Path, dest: &Path) -> Result<(), String> {
    let file = std::fs::File::create(dest).map_err(|e| e.to_string())?;
    let mut zip = zip::ZipWriter::new(file);
    let opts = zip::write::SimpleFileOptions::default().compression_method(zip::CompressionMethod::Deflated);
    fn walk(base: &Path, dir: &Path, zip: &mut zip::ZipWriter<std::fs::File>, opts: zip::write::SimpleFileOptions) -> Result<(), String> {
        let mut entries: Vec<PathBuf> = std::fs::read_dir(dir).map_err(|e| e.to_string())?.flatten().map(|e| e.path()).collect();
        entries.sort();
        for p in entries {
            if p.is_dir() {
                walk(base, &p, zip, opts)?;
            } else {
                let rel = p.strip_prefix(base).unwrap_or(&p).to_string_lossy().replace('\\', "/");
                zip.start_file(rel, opts).map_err(|e| e.to_string())?;
                zip.write_all(&std::fs::read(&p).map_err(|e| e.to_string())?).map_err(|e| e.to_string())?;
            }
        }
        Ok(())
    }
    walk(dir, dir, &mut zip, opts)?;
    zip.finish().map_err(|e| e.to_string())?;
    Ok(())
}

/// Every theme whose title contains `title` (v2 `theme_pages(title, 'title')`).
fn all_title_matches(title: &str) -> Result<Vec<Entry>, String> {
    let mut out = Vec::new();
    for page in 1.. {
        let list = vta_online::get(title, Field::Title, vta_online::Filter::All, Sort::Newest, page, PAGE, false)?;
        let n = list.len();
        out.extend(list);
        if n < PAGE {
            break;
        }
    }
    Ok(out)
}

pub fn submit(app: &AppWindow, library: &SharedLibrary, index: i32) {
    if let Err(reason) = vta_online::status() {
        return dialogs::message(app, DialogIcon::Info, "Unavailable", &reason);
    }
    if SESSION.with(|s| s.borrow().is_some()) {
        return; // one submission window at a time
    }
    let Some(theme) = theme_at(library, index) else { return };
    let Some(title) = theme.data.title.clone().filter(|t| !t.is_empty()) else {
        return dialogs::message(app, DialogIcon::Warning, "Error", "Cannot submit theme: Title is missing from theme.xml.");
    };
    let zip_path = std::env::temp_dir().join(format!("{}_upload.zip", safe_title(&title)));
    if let Err(e) = zip_theme(&theme.path, &zip_path) {
        cleanup_zip(&zip_path);
        return dialogs::message(app, DialogIcon::Error, "Zip Error", &format!("Failed to create theme zip: {e}"));
    }
    let cancel = dialogs::progress_start(app, "Checking the repository…", false);
    let _ = cancel;
    let weak = app.as_weak();
    let t2 = title.clone();
    std::thread::spawn(move || {
        let result = all_title_matches(&t2);
        let _ = weak.upgrade_in_event_loop(move |app| {
            dialogs::progress_close(&app);
            let all = match result {
                Ok(a) => a,
                Err(e) => {
                    cleanup_zip(&zip_path);
                    return dialogs::message(&app, DialogIcon::Error, "API Error", &format!("Could not fetch theme list from repository: {e}"));
                }
            };
            let matches: Vec<Entry> = all.into_iter().filter(|e| e.str("title").to_lowercase() == title.to_lowercase()).collect();
            if matches.is_empty() {
                open_window(&app, NEW_URL.to_string(), zip_path);
            } else {
                ask_theme_exists(&app, &title, matches, zip_path);
            }
        });
    });
}

fn ask_theme_exists(app: &AppWindow, title: &str, matches: Vec<Entry>, zip: PathBuf) {
    let te = app.global::<ThemeExists>();
    te.set_theme_title(title.into());
    te.set_chosen(0);
    let rows: Vec<ExistingTheme> = matches
        .iter()
        .map(|m| ExistingTheme {
            title: m.str("title").into(),
            author: { let a = m.str("author"); if a.is_empty() { "?".into() } else { a.into() } },
            version: { let v = m.str("version"); if v.is_empty() { "?".into() } else { v.into() } },
            downloads: { let d = m.str("downloads"); if d.is_empty() { "0".into() } else { d.into() } },
            preview: Image::default(),
            has_preview: false,
        })
        .collect();
    te.set_matches(ModelRc::new(VecModel::from(rows)));
    te.set_open(true);
    // Previews (home, else lockscreen), loaded in the background.
    for (i, m) in matches.iter().enumerate() {
        let url = { let h = m.str("home_preview"); if h.is_empty() { m.str("lockscreen_preview") } else { h } };
        let weak = app.as_weak();
        std::thread::spawn(move || {
            let buf = (|| {
                let agent: ureq::Agent = ureq::Agent::config_builder().timeout_global(Some(Duration::from_secs(20))).build().into();
                let bytes = agent.get(&vta_online::secure_url(&url)).call().ok()?.body_mut().with_config().limit(20 << 20).read_to_vec().ok()?;
                let img = image::load_from_memory(&bytes).ok()?.thumbnail(480, 270).to_rgba8();
                let (w, h) = img.dimensions();
                Some(slint::SharedPixelBuffer::<slint::Rgba8Pixel>::clone_from_slice(img.as_raw(), w, h))
            })();
            let _ = weak.upgrade_in_event_loop(move |app| {
                let Some(buf) = buf else { return };
                let te = app.global::<ThemeExists>();
                let model = te.get_matches();
                if let Some(mut row) = slint::Model::row_data(&model, i) {
                    row.preview = Image::from_rgba8(buf);
                    row.has_preview = true;
                    slint::Model::set_row_data(&model, i, row);
                }
            });
        });
    }
    PENDING.with(|p| *p.borrow_mut() = Some((zip, matches)));
    // Debug builds only: answer the choice automatically for screenshots/tests.
    #[cfg(debug_assertions)]
    if let Ok(action) = std::env::var("VTA_DEV_SUBMIT_ACTION") {
        let weak = app.as_weak();
        slint::Timer::single_shot(Duration::from_secs(3), move || {
            if let Some(app) = weak.upgrade() {
                app.global::<ThemeExists>().invoke_finish(action.into());
            }
        });
    }
}

/// Show the submission window centred on the screen.
fn reveal(s: &mut Session) {
    if s.shown {
        return;
    }
    s.shown = true;
    trace("reveal");
    let w = s.window.window();
    crate::center_on_screen(w);
}

fn hide(s: &mut Session) {
    s.shown = false;
    trace("hide");
    s.window.window().set_position(slint::PhysicalPosition::new(-20_000, -20_000));
}

fn run_js(js: &str) {
    SESSION.with(|s| {
        if let Some(wv) = s.borrow().as_ref().and_then(|s| s.webview.as_ref()) {
            let _ = wv.evaluate_script(js);
        }
    });
}

/// Run a script and call `then` on the UI thread once it has finished running
/// (scripts are evaluated in order, so earlier `run_js` calls are done too).
fn run_js_then(js: &str, then: impl FnOnce() + 'static) {
    let then = std::rc::Rc::new(std::cell::RefCell::new(Some(then)));
    let (tx, rx) = std::sync::mpsc::channel::<()>();
    let ok = SESSION.with(|s| {
        s.borrow().as_ref().and_then(|s| s.webview.as_ref()).is_some_and(|wv| {
            wv.evaluate_script_with_callback(js, move |_| {
                let _ = tx.send(());
            })
            .is_ok()
        })
    });
    if !ok {
        if let Some(f) = then.borrow_mut().take() {
            f();
        }
        return;
    }
    // The callback arrives on the UI thread's message loop; poll for it.
    let timer = std::rc::Rc::new(slint::Timer::default());
    let t2 = timer.clone();
    let started = std::time::Instant::now();
    timer.start(slint::TimerMode::Repeated, Duration::from_millis(10), move || {
        if rx.try_recv().is_ok() || started.elapsed() > Duration::from_secs(5) {
            t2.stop();
            if let Some(f) = then.borrow_mut().take() {
                f();
            }
        }
    });
    std::mem::forget(timer);
}

fn close_session() {
    if let Some(s) = SESSION.with(|s| s.borrow_mut().take()) {
        drop(s.webview);
        let _ = s.window.hide();
        cleanup_zip(&s.zip);
    }
}

fn on_ipc(message: &str) {
    trace(&format!("ipc {message}"));
    match message {
        "ready" => SESSION.with(|s| {
            if let Some(s) = s.borrow_mut().as_mut() {
                reveal(s);
            }
        }),
        "cancel" => close_session(),
        "get_content" => {
            let zip = SESSION.with(|s| s.borrow().as_ref().map(|s| s.zip.clone()));
            let reply = match zip.filter(|z| z.is_file()) {
                Some(z) => {
                    let content = std::fs::read(&z).map(|b| base64::engine::general_purpose::STANDARD.encode(b)).unwrap_or_default();
                    let name = z.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
                    serde_json::json!({ "filename": name, "content": content, "status": "success" })
                }
                None => serde_json::json!({ "status": "cancelled" }),
            };
            run_js(&format!("window.__vtaContent && window.__vtaContent({reply});"));
        }
        _ => {}
    }
}

fn on_page_load(event: PageLoadEvent, url: String) {
    trace(&format!("{} {url}", if matches!(event, PageLoadEvent::Started) { "started" } else { "finished" }));
    match event {
        // v2 `response_received` (update path): on theme.php, click through to the
        // update form after 1 s.
        PageLoadEvent::Started => {
            let go = SESSION.with(|s| {
                let mut b = s.borrow_mut();
                let Some(s) = b.as_mut() else { return false };
                if s.is_update && url.contains("psvt.ovh/theme.php") && !s.theme_page_handled {
                    s.theme_page_handled = true;
                    return true;
                }
                false
            });
            if go {
                slint::Timer::single_shot(Duration::from_secs(1), || run_js(NAV_THEME));
            }
        }
        PageLoadEvent::Finished => {
            // v2 `loaded`: first load -> removeNav (new) / navToUpdate (update).
            let first = SESSION.with(|s| {
                let mut b = s.borrow_mut();
                let Some(s) = b.as_mut() else { return None };
                if !s.first_load_pending {
                    return None;
                }
                s.first_load_pending = false;
                Some(s.is_update)
            });
            match first {
                Some(false) => {
                    // v2's `run_js` blocks until each script has run, so the
                    // window only appears once the page is already cleaned up.
                    // Do the same: show it from the scripts' completion callback.
                    run_js(NAV_REMOVAL);
                    run_js_then(PADDING_CHANGE, || {
                        SESSION.with(|s| {
                            if let Some(s) = s.borrow_mut().as_mut() {
                                reveal(s);
                            }
                        })
                    });
                }
                Some(true) => run_js(NAV_THEME),
                None => {}
            }
            // v2 `onSubmitLoaded`: on submit.php (once), hide (new path) and fill the form.
            let act = SESSION.with(|s| {
                let mut b = s.borrow_mut();
                let Some(s) = b.as_mut() else { return false };
                if !url.starts_with(SUBMIT_PREFIX) || s.run_once > 0 {
                    return false;
                }
                s.run_once += 1;
                if !s.is_update {
                    hide(s);
                }
                true
            });
            if act {
                let combined = [CANCEL_BUTTON, FILE_TEXT, BASE64_TO_FILE, GUESS_MIME_TYPE, OPEN_FILE_BUTTON, GET_CONTENT, "fetchData();"].concat();
                run_js(&combined);
            }
        }
    }
}

fn open_window(app: &AppWindow, url: String, zip: PathBuf) {
    let is_update = url != NEW_URL;
    let window = match SubmitWindow::new() {
        Ok(w) => w,
        Err(e) => {
            cleanup_zip(&zip);
            return dialogs::message(app, DialogIcon::Error, "Webview Error", &format!("Could not open submission window: {e}"));
        }
    };
    // Created hidden (off-screen) like v2's `hidden=True`; shown when the page is ready.
    window.window().set_position(slint::PhysicalPosition::new(-20_000, -20_000));
    if let Err(e) = window.show() {
        cleanup_zip(&zip);
        return dialogs::message(app, DialogIcon::Error, "Webview Error", &format!("Could not open submission window: {e}"));
    }
    // v2 `confirm_close=True`.
    window.window().on_close_requested(|| {
        let ok = rfd::MessageDialog::new()
            .set_title("Submit Theme to PSVT")
            .set_description("Do you really want to quit?")
            .set_buttons(rfd::MessageButtons::YesNo)
            .show();
        if ok == rfd::MessageDialogResult::Yes {
            slint::Timer::single_shot(Duration::ZERO, close_session);
        }
        slint::CloseRequestResponse::KeepWindowShown
    });
    SESSION.with(|s| {
        *s.borrow_mut() = Some(Session {
            window,
            webview: None,
            is_update,
            first_load_pending: true,
            theme_page_handled: false,
            run_once: 0,
            zip,
            shown: false,
        })
    });
    // The native window exists once the event loop has run; attach the web view then.
    let tries = std::rc::Rc::new(std::cell::Cell::new(0));
    let timer = std::rc::Rc::new(slint::Timer::default());
    let t2 = timer.clone();
    let weak = app.as_weak();
    timer.start(slint::TimerMode::Repeated, Duration::from_millis(30), move || {
        tries.set(tries.get() + 1);
        let built = SESSION.with(|s| {
            let b = s.borrow();
            let Some(sess) = b.as_ref() else { return Some(Err("closed".to_string())) };
            let handle = sess.window.window().window_handle();
            let builder = WebViewBuilder::new()
                .with_url(url.clone())
                .with_bounds(Rect {
                    position: wry::dpi::LogicalPosition::new(0, 0).into(),
                    size: wry::dpi::LogicalSize::new(800, 600).into(),
                })
                .with_initialization_script(API_SHIM)
                .with_ipc_handler(|req| {
                    let body = req.body().clone();
                    slint::Timer::single_shot(Duration::ZERO, move || on_ipc(&body));
                })
                .with_on_page_load_handler(|event, url| {
                    slint::Timer::single_shot(Duration::ZERO, move || on_page_load(event, url));
                });
            match builder.build_as_child(&handle) {
                Ok(wv) => Some(Ok(wv)),
                Err(e) if tries.get() > 60 => Some(Err(e.to_string())),
                Err(_) => None,
            }
        });
        match built {
            None => {}
            Some(Ok(wv)) => {
                SESSION.with(|s| {
                    if let Some(s) = s.borrow_mut().as_mut() {
                        s.webview = Some(wv);
                    }
                });
                t2.stop();
            }
            Some(Err(e)) => {
                t2.stop();
                close_session();
                if e != "closed" {
                    if let Some(app) = weak.upgrade() {
                        dialogs::message(&app, DialogIcon::Error, "Webview Error", &format!("Could not open submission window: {e}"));
                    }
                }
            }
        }
    });
    std::mem::forget(timer);
}

pub fn wire(app: &AppWindow, library: &SharedLibrary) {
    let (a, l) = (app.as_weak(), library.clone());
    app.global::<crate::Library>().on_submit(move |i| submit(&a.unwrap(), &l, i));
    let a = app.as_weak();
    app.global::<ThemeExists>().on_finish(move |action| {
        let app = a.unwrap();
        let te = app.global::<ThemeExists>();
        te.set_open(false);
        let Some((zip, matches)) = PENDING.with(|p| p.borrow_mut().take()) else { return };
        match action.as_str() {
            "new" => open_window(&app, NEW_URL.to_string(), zip),
            "update" => {
                let chosen = matches.get(te.get_chosen().max(0) as usize).or(matches.first());
                let id = chosen.map(|m| m.str("id")).unwrap_or_default();
                open_window(&app, format!("https://psvt.ovh/theme.php?id={id}"), zip);
            }
            _ => cleanup_zip(&zip),
        }
    });
}

//! "Submit to Repository" (v2 `submit_theme_to_repo`), kept identical: the theme
//! is zipped, the repository is checked for the same title (Theme Exists choice),
//! then the repository's submission page opens in an app window using the
//! system web engine (WebView2 / WKWebView / WebKitGTK — as pywebview did) and the
//! same scripts as v2 fill the theme package into the form.

use crate::library_actions::SharedSettings;
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

// --- submit.php scripts: verbatim from v2 (that page is unchanged) ----------

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

// --- The site's current home and theme pages ----------------------------------
//
// psvt.ovh rebuilt index.php and theme.php (Bootstrap): "#submit" is no longer
// its own page but a warning pop-up (#submitModal) opened from the menu, and a
// theme's page has a plain "Submit a New Version" form. submit.php itself is
// unchanged, so v2's scripts above still fill it in.

/// New theme: open the submit warning straight away, filling the whole window,
/// with no way to dismiss it (no close button, Esc or backdrop click), so the
/// only way on is its "go on" button. The window is shown once it is open.
const SUBMIT_MODAL: &str = r#"
(function openSubmit(attempts) {
    var el = document.getElementById('submitModal');
    if (!el || !window.bootstrap) {
        if (attempts > 0) setTimeout(function () { openSubmit(attempts - 1); }, 100);
        else window.pywebview.api.ready();
        return;
    }
    el.classList.remove('fade');
    el.setAttribute('data-bs-backdrop', 'static');
    el.setAttribute('data-bs-keyboard', 'false');
    var dialog = el.querySelector('.modal-dialog');
    dialog.classList.remove('modal-lg', 'modal-dialog-scrollable');
    dialog.classList.add('modal-fullscreen');
    el.querySelectorAll('[data-bs-dismiss="modal"]').forEach(function (b) { b.remove(); });
    var style = document.createElement('style');
    style.textContent = 'body > :not(#submitModal):not(.modal-backdrop):not(script){visibility:hidden!important}'
        + '.modal-backdrop{opacity:1!important}';
    document.head.appendChild(style);
    el.addEventListener('shown.bs.modal', function () { window.pywebview.api.ready(); }, { once: true });
    el.addEventListener('hide.bs.modal', function (e) { e.preventDefault(); });
    new bootstrap.Modal(el, { backdrop: 'static', keyboard: false, focus: true }).show();
})(50);
"#;

/// Update: send the theme page's "Submit a New Version" form (while hidden),
/// which opens submit.php for that theme.
const SUBMIT_NEW_VERSION: &str = r#"
(function trySubmit(attempts) {
    if (window.__vtaSent) return;
    var form = document.querySelector('form[action="./submit.php"]');
    if (form) { window.__vtaSent = true; form.submit(); return; }
    if (attempts > 0) setTimeout(function () { trySubmit(attempts - 1); }, 300);
})(15);
"#;

/// submit.php: attach the user key from Settings through the page's own "Add"
/// pop-up (fill #uki, press Attach), waiting until the page reports it attached
/// (#ukey set) or shows an error (#error-attach), which is passed back to the app.
/// `__VTA_KEY__` is replaced with the JSON-quoted key.
const ATTACH_KEY: &str = r#"
function vtaAttachKey() {
    return new Promise(function (done) {
        var key = __VTA_KEY__;
        if (!key || !window.jQuery || !document.getElementById('uki')) { done(); return; }
        $('#uki').val(key);
        $('#btn-attach').trigger('click');
        var tries = 40;
        (function wait() {
            var err = ($('#error-attach').text() || '').trim();
            if (($('#ukey').val() || '') !== '') { window.ipc.postMessage('keyok'); done(); return; }
            if (err !== '') {
                try { $('#popupAddukey').popup('close'); } catch (_) {}
                window.ipc.postMessage('keyerror:' + err);
                done();
                return;
            }
            if (--tries <= 0) { done(); return; }
            setTimeout(wait, 100);
        })();
    });
}
"#;

/// submit.php, after the user presses Submit: the repository shows a brand-new
/// user key on screen when none was attached. Keep all page text selectable,
/// and watch for a new key-like code (a run of 16+ letters/digits that was not
/// on the page before), then hand it to the app so the user can copy / save it.
const WATCH_NEW_KEY: &str = r#"
(function () {
    var css = document.createElement('style');
    css.textContent = '*{-webkit-user-select:text!important;user-select:text!important}';
    document.head.appendChild(css);
    // The result page (as seen on the site) shows the new key in a text box,
    // next to a link to the new theme (theme.php?id=<40 hex>) that must not be
    // mistaken for it. So: new codes in text boxes first; codes in plain page
    // text only as a fallback, never ones that are part of a link or URL.
    var re = /[A-Za-z0-9]{16,}/g;
    function grab(s, out) { (String(s || '').match(re) || []).forEach(function (t) { out[t] = true; }); }
    function linkTokens() {
        var out = {};
        document.querySelectorAll('a').forEach(function (a) { grab(a.href, out); grab(a.textContent, out); });
        grab((document.body.innerText || '').match(/https?:\/\/\S+/g), out);
        return out;
    }
    function inputTokens() {
        var out = {};
        document.querySelectorAll('input:not([type=hidden]):not([type=file]):not([type=password]), textarea').forEach(function (i) { grab(i.value, out); });
        return out;
    }
    function textTokens() { var out = {}; grab(document.body.innerText, out); return out; }
    var before = Object.assign({}, inputTokens(), textTokens()), sent = {};
    new MutationObserver(function () {
        if (!/user\s*key/i.test(document.body.innerText || '')) return;
        var links = linkTokens(), fresh = function (t) { return !before[t] && !links[t]; };
        var now = Object.keys(inputTokens()).filter(fresh);
        if (now.length === 0) now = Object.keys(textTokens()).filter(fresh);
        now.forEach(function (t) {
            if (before[t] || sent[t]) return;
            if (Object.keys(sent).some(function (s) { return t.indexOf(s) === 0; })) return;
            sent[t] = true;
            window.ipc.postMessage('newkey:' + t);
            // A "Copy key" button right beside the key on the page.
            var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT), n, host = null;
            while ((n = walker.nextNode())) { if (n.nodeValue.indexOf(t) >= 0) { host = n.parentElement; break; } }
            if (!host) document.querySelectorAll('input, textarea').forEach(function (i) { if ((i.value || '').indexOf(t) >= 0) host = i; });
            if (!host) return;
            var b = document.createElement('button');
            b.type = 'button';
            b.textContent = 'Copy key';
            b.style.cssText = 'margin:0.5em;padding:0.4em 1.2em;font-weight:bold;border-radius:6px;border:1px solid #2a7;background:#2a7;color:#fff;cursor:pointer';
            b.addEventListener('click', function (e) { e.preventDefault(); window.ipc.postMessage('copykey:' + t); b.textContent = 'Copied'; });
            host.insertAdjacentElement('afterend', b);
            host.insertAdjacentText('afterend', ' ');
        });
    }).observe(document.body, { childList: true, subtree: true, characterData: true });
})();
"#;

/// Debug builds only (`VTA_DEV_SUBMIT_SPY`): log every request the page makes,
/// with its body and the server's reply, to the submit trace.
/// Debug builds only (`VTA_DEV_SUBMIT_SPY`): snapshot the page's full HTML
/// (with current form values) when it loads and again whenever it changes
/// (debounced), so the real pages can be inspected afterwards.
#[cfg(debug_assertions)]
const PAGE_DUMP: &str = r#"
(function () {
    var timer = null, last = '';
    function dump(why) {
        if (!document.documentElement) return;
        document.querySelectorAll('input, textarea').forEach(function (i) {
            if (i.type !== 'file' && i.type !== 'password') i.setAttribute('data-vta-value', i.value || '');
        });
        var html = document.documentElement.outerHTML;
        if (html === last) return;
        last = html;
        window.ipc.postMessage('dump:' + why + '|' + location.pathname + '|' + html);
    }
    window.addEventListener('load', function () {
        dump('load');
        new MutationObserver(function () {
            clearTimeout(timer);
            timer = setTimeout(function () { dump('change'); }, 1200);
        }).observe(document.body, { childList: true, subtree: true, attributes: true, characterData: true });
    });
})();
"#;

#[cfg(debug_assertions)]
const XHR_SPY: &str = r#"
(function () {
    var open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (m, u) { this.__vta = m + ' ' + u; return open.apply(this, arguments); };
    XMLHttpRequest.prototype.send = function (body) {
        var x = this, b = (typeof body === 'string') ? body : (body instanceof FormData ? '[FormData ' + Array.from(body.keys()).join(',') + ']' : String(body));
        x.addEventListener('loadend', function () {
            window.ipc.postMessage('log:' + x.__vta + ' body=' + b.slice(0, 300) + ' status=' + x.status + ' reply=' + String(x.responseText).slice(0, 2000));
        });
        return send.apply(this, arguments);
    };
})();
"#;

/// Debug builds only (`VTA_DEV_FAKE_UKEY=<reply>`): answer the site's user-key
/// check (POST ukey.php) locally with `<reply>`, to test an accepted key
/// without a real one. Nothing is sent to the server for that request.
#[cfg(debug_assertions)]
const FAKE_UKEY: &str = r#"
(function () {
    var open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (m, u) { this.__vtaUrl = String(u); return open.apply(this, arguments); };
    XMLHttpRequest.prototype.send = function () {
        if (!/ukey\.php$/.test(this.__vtaUrl || '')) return send.apply(this, arguments);
        var x = this;
        setTimeout(function () {
            Object.defineProperty(x, 'readyState', { value: 4 });
            Object.defineProperty(x, 'status', { value: 200 });
            Object.defineProperty(x, 'statusText', { value: 'OK' });
            Object.defineProperty(x, 'responseText', { value: __VTA_REPLY__ });
            Object.defineProperty(x, 'response', { value: __VTA_REPLY__ });
            x.getAllResponseHeaders = function () { return 'content-type: text/html\r\n'; };
            if (x.onreadystatechange) x.onreadystatechange();
            if (x.onload) x.onload();
            x.dispatchEvent(new Event('load'));
            x.dispatchEvent(new Event('loadend'));
        }, 50);
    };
})();
"#;

fn init_script() -> String {
    #[allow(unused_mut)]
    let mut s = API_SHIM.to_string();
    #[cfg(debug_assertions)]
    {
        if std::env::var("VTA_DEV_SUBMIT_SPY").is_ok() {
            s.push_str(XHR_SPY);
            s.push_str(PAGE_DUMP);
        }
        if let Ok(reply) = std::env::var("VTA_DEV_FAKE_UKEY") {
            s.push_str(&FAKE_UKEY.replace("__VTA_REPLY__", &serde_json::Value::String(reply).to_string()));
        }
    }
    s
}

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
    /// The repository user key from Settings (empty = none), attached on submit.php.
    user_key: String,
}

thread_local! {
    static SESSION: RefCell<Option<Session>> = const { RefCell::new(None) };
    /// The main window (for messages raised from the web page).
    static APP: RefCell<Option<slint::Weak<AppWindow>>> = const { RefCell::new(None) };
    /// Settings, for the user key.
    static SETTINGS: RefCell<Option<SharedSettings>> = const { RefCell::new(None) };
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

fn close_session() {
    if let Some(s) = SESSION.with(|s| s.borrow_mut().take()) {
        drop(s.webview);
        let _ = s.window.hide();
        cleanup_zip(&s.zip);
    }
}

fn on_ipc(message: &str) {
    // Debug builds: page snapshots go to %TEMP%/vta-submit-pages/, not the trace.
    #[cfg(debug_assertions)]
    if let Some(rest) = message.strip_prefix("dump:") {
        let mut parts = rest.splitn(3, '|');
        let (why, path, html) = (parts.next().unwrap_or(""), parts.next().unwrap_or(""), parts.next().unwrap_or(""));
        let dir = std::env::temp_dir().join("vta-submit-pages");
        let _ = std::fs::create_dir_all(&dir);
        let n = std::fs::read_dir(&dir).map(|d| d.count()).unwrap_or(0);
        let page: String = path.trim_start_matches('/').chars().map(|c| if c.is_alphanumeric() || c == '.' { c } else { '_' }).collect();
        let file = dir.join(format!("{n:03}-{page}-{why}.html"));
        let _ = std::fs::write(&file, html);
        trace(&format!("dump {}", file.display()));
        return;
    }
    trace(&format!("ipc {message}"));
    match message {
        "ready" => {
            SESSION.with(|s| {
                if let Some(s) = s.borrow_mut().as_mut() {
                    reveal(s);
                }
            });
            // Debug builds (`VTA_DEV_FAKE_DONE`): then write a made-up result
            // into the page (as if a first submission finished) to test the
            // new-key capture without submitting anything.
            #[cfg(debug_assertions)]
            if std::env::var("VTA_DEV_FAKE_DONE").is_ok() {
                slint::Timer::single_shot(Duration::from_secs(2), || {
                    // Modelled on the site's result page: a link to the new theme
                    // (40-hex id) and the new key in a text box below the note.
                    run_js("document.getElementById('mainpage').style.display='none'; document.querySelector('.upload').innerHTML = '<h1>Congratulations!</h1><p><i>Your custom theme has been uploaded to the repository!</i></p><p>You can find it at this address:<br><a href=\"http://psvt.ovh/theme.php?id=58a5c0919e883513de73d59a65d54eef7211aef8\">http://psvt.ovh/theme.php?id=58a5c0919e883513de73d59a65d54eef7211aef8</a></p><p><b>Note:</b> the Custom Theme has been successfully added but it will appear in repository main page only after a check by the admin. Since you have not attached a user key to the release, a new one has been generated to attach this release to. You can find it here below, please save it and remember to attach it to your next Custom Themes submissions.</p><input type=\"text\" readonly value=\"vtaFAKEkey0123456789abcdefTEST\">'; document.querySelector('.upload').scrollIntoView();");
                });
            }
        }
        "cancel" => close_session(),
        m if m.starts_with("copykey:") => {
            let key = m["copykey:".len()..].trim().to_string();
            let _ = arboard::Clipboard::new().and_then(|mut c| c.set_text(key));
        }
        m if m.starts_with("newkey:") => {
            let key = m["newkey:".len()..].trim().to_string();
            // Only offered when no key was attached (the repository makes one then).
            let had_key = SESSION.with(|s| s.borrow().as_ref().is_some_and(|s| !s.user_key.is_empty()));
            if key.is_empty() || had_key {
                return;
            }
            if let Some(app) = APP.with(|a| a.borrow().as_ref().and_then(|w| w.upgrade())) {
                offer_new_key(&app, key);
            }
        }
        "keyok" => {
            // Debug builds: when testing with a faked key check, scroll the
            // user-key controls into view for a screenshot.
            #[cfg(debug_assertions)]
            if std::env::var("VTA_DEV_FAKE_UKEY").is_ok() {
                run_js("document.getElementById('ukeylabel').scrollIntoView({block:'center'});");
            }
        }
        m if m.starts_with("keyerror:") => {
            let err = m["keyerror:".len()..].to_string();
            if let Some(app) = APP.with(|a| a.borrow().as_ref().and_then(|w| w.upgrade())) {
                dialogs::message(
                    &app,
                    DialogIcon::Warning,
                    "User key not attached",
                    &format!("The repository did not accept the user key saved in Settings: {err}\n\nYou can still submit without it, or fix the key in Settings > Connection."),
                );
            }
        }
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
                slint::Timer::single_shot(Duration::from_secs(1), || run_js(SUBMIT_NEW_VERSION));
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
                // New theme: open the full-window submit warning; the page calls
                // `ready()` once it is showing, and only then is the window shown.
                Some(false) => run_js(SUBMIT_MODAL),
                // Update: go straight on to submit.php via the theme page's form.
                Some(true) => run_js(SUBMIT_NEW_VERSION),
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
                // The user key (if any) is attached first, so the window shows
                // with both the theme package and the key already in place.
                let key = SESSION.with(|s| s.borrow().as_ref().map(|s| s.user_key.clone())).unwrap_or_default();
                let attach = ATTACH_KEY.replace("__VTA_KEY__", &serde_json::Value::String(key).to_string());
                let combined = [
                    CANCEL_BUTTON,
                    FILE_TEXT,
                    BASE64_TO_FILE,
                    GUESS_MIME_TYPE,
                    OPEN_FILE_BUTTON,
                    GET_CONTENT,
                    &attach,
                    WATCH_NEW_KEY,
                    "vtaAttachKey().then(fetchData);",
                ]
                .concat();
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
            user_key: user_key(),
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
                    size: wry::dpi::LogicalSize::new(1100, 620).into(),
                })
                .with_initialization_script(&init_script())
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

/// The repository showed a brand-new user key after a submission: show it in the
/// app, with Copy and Save to Settings (so it is attached next time).
fn offer_new_key(app: &AppWindow, key: String) {
    let copy = |key: &str| arboard::Clipboard::new().and_then(|mut c| c.set_text(key.to_string())).is_ok();
    dialogs::ask(
        app,
        DialogIcon::Success,
        "Your new repository user key",
        &format!(
            "The repository created a user key for you:\n\n{key}\n\nKeep it safe like a password: it cannot be recovered if lost. Save it to Settings and it will be attached to your future submissions automatically."
        ),
        "",
        &[("Copy", dialogs::Kind::Secondary), ("Save to Settings & Copy", dialogs::Kind::Primary)],
        move |app, choice| {
            let copied = copy(&key);
            if choice == 1 {
                SETTINGS.with(|s| {
                    if let Some(s) = s.borrow().as_ref() {
                        let mut s = s.borrow_mut();
                        s.set("psvt_user_key", key.clone());
                        let _ = s.save();
                    }
                });
            }
            let msg = match (choice, copied) {
                (1, true) => "User key saved to Settings and copied to the clipboard.",
                (1, false) => "User key saved to Settings (could not access the clipboard).",
                (_, true) => "User key copied to the clipboard.",
                _ => "Could not access the clipboard.",
            };
            crate::show_toast(app, msg, !copied);
        },
    );
}

/// The repository user key saved in Settings (empty = none).
fn user_key() -> String {
    // Debug builds only: test a key without touching settings.json.
    #[cfg(debug_assertions)]
    if let Ok(k) = std::env::var("VTA_DEV_PSVT_KEY") {
        return k;
    }
    SETTINGS.with(|s| s.borrow().as_ref().map(|s| s.borrow().str_or("psvt_user_key", "").trim().to_string())).unwrap_or_default()
}

/// Before submitting without a saved user key, ask for one (every time, until
/// a key is saved or "Don't ask again" is chosen; "Not now" skips just this time).
fn submit_with_key_prompt(app: &AppWindow, library: &SharedLibrary, index: i32) {
    #[allow(unused_mut)]
    let mut ask = SETTINGS.with(|s| s.borrow().as_ref().is_some_and(|s| s.borrow().flag("psvt_key_prompt", true)));
    // Debug builds: tests that submit without a key skip the question.
    #[cfg(debug_assertions)]
    if std::env::var("VTA_DEV_FAKE_DONE").is_ok() {
        ask = false;
    }
    if !user_key().is_empty() || !ask || vta_online::status().is_err() || SESSION.with(|s| s.borrow().is_some()) {
        return submit(app, library, index);
    }
    let library = library.clone();
    dialogs::ask_text(
        app,
        DialogIcon::Question,
        "Do you have a repository user key?",
        "The repository gives you a user key on your first submission. Attach it every time so your submissions count toward your uploader record (after 5 accepted themes they skip moderation). Paste it below to save it in Settings, and it will be attached for you from now on. If this is your first submission, choose Not now: the repository will create your key.",
        "Paste your user key",
        true,
        &[("Don't ask again", dialogs::Kind::Ghost), ("Not now", dialogs::Kind::Secondary), ("Save key", dialogs::Kind::Primary)],
        move |app, choice, text| {
            let key = text.trim().to_string();
            SETTINGS.with(|s| {
                if let Some(s) = s.borrow().as_ref() {
                    let mut s = s.borrow_mut();
                    match choice {
                        0 => s.set("psvt_key_prompt", false),
                        2 if !key.is_empty() => s.set("psvt_user_key", key.clone()),
                        _ => return,
                    }
                    let _ = s.save();
                }
            });
            submit(app, &library, index);
        },
    );
}

pub fn wire(app: &AppWindow, library: &SharedLibrary, settings: &SharedSettings) {
    APP.with(|a| *a.borrow_mut() = Some(app.as_weak()));
    SETTINGS.with(|s| *s.borrow_mut() = Some(settings.clone()));
    let (a, l) = (app.as_weak(), library.clone());
    app.global::<crate::Library>().on_submit(move |i| submit_with_key_prompt(&a.unwrap(), &l, i));
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

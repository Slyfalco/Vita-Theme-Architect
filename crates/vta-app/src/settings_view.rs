//! Settings page controller: loads settings.json into the form, saves it back
//! (keeping keys it doesn't manage), and handles browse/test/app-theme actions.
//! Keys and value formats are v2's (e.g. `vita_port` is stored as a string).

use crate::library_actions::SharedSettings;
use crate::library_view::SharedLibrary;
use crate::{app_theme, AppWindow, SettingsState, Shell};
use slint::{ComponentHandle, ModelRc, SharedString, VecModel};
use std::path::PathBuf;
use std::rc::Rc;
use vta_core::paths::DataDirs;
use vta_core::CURRENT_VERSION;

/// Gemini image models: (id, label, price per icon in USD). Same list as v2.
pub const GEMINI_MODELS: [(&str, &str, f64); 3] = [
    ("gemini-3.1-flash-image-preview", "Gemini 3.1 Flash Image (best)", 0.067),
    ("gemini-3.1-flash-lite-image", "Gemini 3.1 Flash Lite Image (cheapest)", 0.0336),
    ("gemini-3-pro-image", "Gemini 3 Pro Image (slowest, most detailed)", 0.134),
];
const ARCHS: [&str; 3] = ["auto", "sd15", "sdxl"];

/// Whether this is an official release build (the in-app updater runs only then).
pub fn official_build() -> bool {
    !cfg!(debug_assertions) && vta_online::available()
}

pub struct SettingsCtx {
    pub dirs: DataDirs,
    pub settings: SharedSettings,
    pub library: SharedLibrary,
    pub themes: Vec<app_theme::AppTheme>,
}

/// Fill the form from settings.json.
pub fn load(app: &AppWindow, ctx: &SettingsCtx) {
    let s = ctx.settings.borrow();
    let st = app.global::<SettingsState>();
    st.set_ai_edition(cfg!(feature = "icon-ai"));
    crate::ai_storage_view::refresh(app);
    st.set_official_build(official_build());
    st.set_version_line(
        format!(
            "Version {CURRENT_VERSION} · {} · {}",
            crate::edition_name(),
            if official_build() { "Official build" } else { "Unofficial build" }
        )
        .into(),
    );
    st.set_update_status(if official_build() { "".into() } else { "Updates available in official builds only".into() });
    st.set_default_author(s.str_or("default_author", "").into());
    st.set_theme_library_path(
        ctx.dirs.load_path(&s.str_or("theme_library_path", ""), &ctx.dirs.default_themes_path()).to_string_lossy().as_ref().into(),
    );
    st.set_extras_library_path(
        ctx.dirs.load_path(&s.str_or("extras_library_path", ""), &ctx.dirs.default_extras_path()).to_string_lossy().as_ref().into(),
    );
    st.set_auto_update_check(s.flag("auto_update_check", true));
    st.set_vita_ip(s.str_or("vita_ip", "").into());
    let port = match s.values.get("vita_port") {
        Some(serde_json::Value::Number(n)) => n.to_string(),
        Some(serde_json::Value::String(p)) => p.clone(),
        _ => "1337".into(),
    };
    st.set_vita_port(port.into());
    st.set_conn_status("".into());
    st.set_conn_state(0);

    let names: Vec<SharedString> = ctx.themes.iter().map(|t| t.name.clone().into()).collect();
    st.set_app_themes(ModelRc::new(VecModel::from(names)));
    let current = s.str_or("app_theme", app_theme::DEFAULT_ID);
    st.set_app_theme_index(ctx.themes.iter().position(|t| t.id == current).unwrap_or(0) as i32);

    let models: Vec<SharedString> =
        GEMINI_MODELS.iter().map(|(_, label, price)| format!("{label}, ~${price:.3}/icon").into()).collect();
    st.set_gemini_models(ModelRc::new(VecModel::from(models)));
    st.set_google_api_key(s.str_or("google_api_key", "").into());
    let model = s.str("google_image_model").filter(|m| !m.is_empty()).unwrap_or(GEMINI_MODELS[0].0).to_string();
    st.set_gemini_model_index(GEMINI_MODELS.iter().position(|(id, _, _)| *id == model).unwrap_or(0) as i32);
    st.set_custom_base_model(s.str_or("custom_base_model", "").into());
    let arch = s.str_or("custom_base_arch", "auto");
    st.set_custom_arch_index(ARCHS.iter().position(|a| *a == arch).unwrap_or(0) as i32);
    st.set_shared_model_cache(s.str_or("shared_model_cache", "").into());
    st.set_hf_token(s.str_or("hf_token", "").into());
    st.set_ai_min_memory(s.flag("ai_min_memory", false));
    st.set_show_key(false);
    st.set_show_token(false);
    st.set_footer_status(format!("Settings are saved to {}", ctx.dirs.settings_file().display()).into());
}

/// Write the form back to settings.json (v2 `save_settings`), then apply it.
fn save(app: &AppWindow, ctx: &SettingsCtx) -> Result<(), String> {
    let st = app.global::<SettingsState>();
    let theme_path = st.get_theme_library_path().to_string();
    let extras_path = st.get_extras_library_path().to_string();
    {
        let mut s = ctx.settings.borrow_mut();
        // Reload first so keys changed elsewhere (or not managed here) are kept.
        let fresh = vta_core::settings::Settings::load(s.path());
        *s = fresh;
        s.set("default_author", st.get_default_author().to_string());
        s.set("theme_library_path", ctx.dirs.store_path(&theme_path));
        s.set("extras_library_path", ctx.dirs.store_path(&extras_path));
        s.set("vita_ip", st.get_vita_ip().to_string());
        s.set("vita_port", st.get_vita_port().to_string());
        s.set("auto_update_check", st.get_auto_update_check());
        if let Some(t) = ctx.themes.get(st.get_app_theme_index() as usize) {
            s.set("app_theme", t.id.clone());
        }
        if cfg!(feature = "icon-ai") {
            s.set("google_api_key", st.get_google_api_key().trim().to_string());
            let model = GEMINI_MODELS.get(st.get_gemini_model_index() as usize).unwrap_or(&GEMINI_MODELS[0]).0;
            s.set("google_image_model", model);
            s.set("custom_base_model", st.get_custom_base_model().trim().to_string());
            s.set("custom_base_arch", *ARCHS.get(st.get_custom_arch_index() as usize).unwrap_or(&"auto"));
            s.set("shared_model_cache", st.get_shared_model_cache().trim().to_string());
            s.set("hf_token", st.get_hf_token().trim().to_string());
            s.set("ai_min_memory", st.get_ai_min_memory());
        }
        s.save().map_err(|e| format!("Could not save settings: {e}"))?;
    }
    // Apply: library location (rescan), Vita address in the shell.
    let new_root = PathBuf::from(&theme_path);
    let _ = std::fs::create_dir_all(&new_root);
    let _ = std::fs::create_dir_all(&extras_path);
    let changed = ctx.library.borrow().root != new_root;
    if changed {
        ctx.library.borrow_mut().root = new_root;
        crate::library_view::rescan(app, &ctx.library);
    }
    crate::apply_settings(app, &ctx.settings, &ctx.library);
    Ok(())
}

pub(crate) fn pick_folder(title: &str, start: &str) -> Option<String> {
    let mut d = rfd::FileDialog::new().set_title(title);
    if !start.is_empty() && std::path::Path::new(start).is_dir() {
        d = d.set_directory(start);
    }
    d.pick_folder().map(|p| p.to_string_lossy().into_owned())
}

pub fn wire(app: &AppWindow, ctx: Rc<SettingsCtx>) {
    let st = app.global::<SettingsState>();

    let a = app.as_weak();
    st.on_browse_theme_library(move || {
        let app = a.unwrap();
        let st = app.global::<SettingsState>();
        if let Some(p) = pick_folder("Select Theme Library Folder", &st.get_theme_library_path()) {
            st.set_theme_library_path(p.into());
        }
    });
    let a = app.as_weak();
    st.on_browse_extras_library(move || {
        let app = a.unwrap();
        let st = app.global::<SettingsState>();
        if let Some(p) = pick_folder("Select Extras Library Folder", &st.get_extras_library_path()) {
            st.set_extras_library_path(p.into());
        }
    });
    let a = app.as_weak();
    st.on_browse_model_file(move || {
        let app = a.unwrap();
        if let Some(p) = rfd::FileDialog::new()
            .set_title("Select a model checkpoint")
            .add_filter("Model checkpoints", &["safetensors", "ckpt"])
            .pick_file()
        {
            app.global::<SettingsState>().set_custom_base_model(p.to_string_lossy().as_ref().into());
        }
    });
    let a = app.as_weak();
    st.on_browse_model_folder(move || {
        let app = a.unwrap();
        if let Some(p) = pick_folder("Select a diffusers model folder", "") {
            app.global::<SettingsState>().set_custom_base_model(p.into());
        }
    });
    let a = app.as_weak();
    st.on_browse_shared_cache(move || {
        let app = a.unwrap();
        if let Some(p) = pick_folder("Select a Hugging Face cache folder", &app.global::<SettingsState>().get_shared_model_cache()) {
            app.global::<SettingsState>().set_shared_model_cache(p.into());
        }
    });
    st.on_open_repo(|| crate::platform::open_url("https://github.com/Slyfalco/Vita-Theme-Architect"));
    st.on_open_gemini_keys(|| crate::platform::open_url("https://aistudio.google.com/apikey"));

    // Live preview of the chosen app theme (saved with Save; Cancel reverts).
    let (a, c) = (app.as_weak(), ctx.clone());
    st.on_app_theme_changed(move |i| {
        if let Some(t) = c.themes.get(i as usize) {
            app_theme::apply(&a.unwrap(), t);
        }
    });

    let a = app.as_weak();
    st.on_test_connection(move || {
        let app = a.unwrap();
        let st = app.global::<SettingsState>();
        let ip = st.get_vita_ip().trim().to_string();
        let port_text = st.get_vita_port().trim().to_string();
        let port_text = if port_text.is_empty() { "1337".to_string() } else { port_text };
        if ip.is_empty() {
            st.set_conn_state(3);
            st.set_conn_status("Enter the Vita's IP address first.".into());
            return;
        }
        let Ok(port) = port_text.parse::<u16>() else {
            st.set_conn_state(3);
            st.set_conn_status("Port must be a number.".into());
            return;
        };
        st.set_conn_state(1);
        st.set_conn_status("Testing…".into());
        let weak = app.as_weak();
        std::thread::spawn(move || {
            let result = vta_core::vita::test_connection(&ip, port);
            let _ = weak.upgrade_in_event_loop(move |app| {
                let st = app.global::<SettingsState>();
                match result {
                    Ok((count, latency)) => {
                        st.set_conn_state(2);
                        st.set_conn_status(
                            format!("Connected • {} ms • {count} theme(s) on ux0:customtheme", latency.as_millis()).into(),
                        );
                    }
                    Err(e) => {
                        st.set_conn_state(3);
                        st.set_conn_status(e.into());
                    }
                }
            });
        });
    });

    let (a, c) = (app.as_weak(), ctx.clone());
    st.on_save(move || {
        let app = a.unwrap();
        match save(&app, &c) {
            Ok(()) => {
                crate::show_toast(&app, "Settings saved.", false);
                app.global::<Shell>().set_page(crate::Page::Library);
            }
            Err(e) => crate::dialogs::message(&app, crate::DialogIcon::Error, "Error", &e),
        }
    });
    let (a, c) = (app.as_weak(), ctx.clone());
    st.on_cancel(move || {
        let app = a.unwrap();
        // Revert any live app-theme preview, then reload the saved values.
        let id = c.settings.borrow().str_or("app_theme", app_theme::DEFAULT_ID);
        if let Some(t) = c.themes.iter().find(|t| t.id == id).or(c.themes.first()) {
            app_theme::apply(&app, t);
        }
        load(&app, &c);
        app.global::<Shell>().set_page(crate::Page::Library);
    });

    let a = app.as_weak();
    st.on_check_updates(move || crate::updater_view::check(&a.unwrap(), true));
}

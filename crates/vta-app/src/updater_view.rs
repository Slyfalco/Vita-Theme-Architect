//! In-app update flow (v2 `check_for_updates` / `offer_update` /
//! `download_and_apply_update`): only official builds update themselves.

use crate::dialogs::{self, Kind};
use crate::{AppWindow, DialogIcon, SettingsState};
use slint::ComponentHandle;
use vta_core::updater::{self, Release};
use vta_core::CURRENT_VERSION;

thread_local! {
    /// The in-folder data dir of a portable build (kept by an edition-switch mirror).
    pub static PORTABLE_DATA: std::cell::RefCell<Option<std::path::PathBuf>> = const { std::cell::RefCell::new(None) };
}

fn edition() -> &'static str {
    if cfg!(feature = "icon-ai") {
        "ai"
    } else {
        "standard"
    }
}

/// Check GitHub for a newer release. `manual` (from Settings) reports
/// "up to date" and errors; the silent startup check only speaks up for an update.
pub fn check(app: &AppWindow, manual: bool) {
    if !crate::settings_view::official_build() {
        if manual {
            dialogs::message(
                app,
                DialogIcon::Info,
                "Updates Unavailable",
                "In-app updates are only available in official builds.\n\nThis looks like an unofficial or source build, so automatic updating is turned off — it won't replace this build with a release.",
            );
        }
        return;
    }
    let st = app.global::<SettingsState>();
    st.set_update_checking(true);
    st.set_update_status("Checking…".into());
    let weak = app.as_weak();
    std::thread::spawn(move || {
        let result = updater::fetch_latest();
        let _ = weak.upgrade_in_event_loop(move |app| {
            let st = app.global::<SettingsState>();
            st.set_update_checking(false);
            match result {
                Err(e) => {
                    st.set_update_status("Couldn't check for updates.".into());
                    if manual {
                        dialogs::message(&app, DialogIcon::Warning, "Update Check Failed", &format!("Couldn't check for updates:\n{e}"));
                    }
                }
                Ok(rel) if rel.tag.is_empty() => st.set_update_status("Couldn't check for updates.".into()),
                Ok(rel) if updater::is_newer(&rel.tag, CURRENT_VERSION) => {
                    st.set_update_status(format!("Update available: {}", rel.tag).into());
                    offer(&app, rel);
                }
                Ok(_) => {
                    st.set_update_status("You're up to date.".into());
                    if manual {
                        dialogs::message(&app, DialogIcon::Success, "No Updates", &format!("You're up to date (v{CURRENT_VERSION})."));
                    }
                }
            }
        });
    });
}

/// Show the new version with a short summary (full notes under "Show details").
pub fn offer(app: &AppWindow, rel: Release) {
    let short = updater::summarize_notes(&rel.body, 6, 1500);
    let full = updater::summarize_notes(&rel.body, 1000, 20000);
    let mut text = format!("Version {} is available — you have v{CURRENT_VERSION}.", rel.tag);
    if !short.is_empty() {
        text.push_str(&format!("\n\nWhat's new:\n{short}"));
    }
    text.push_str("\n\nDownload and install it now?");
    let details = if !full.is_empty() && full != short { full } else { String::new() };
    dialogs::ask(app, DialogIcon::Info, "Update Available", &text, &details, &[("No", Kind::Secondary), ("Yes, update", Kind::Primary)], move |app, i| {
        if i == 1 {
            install(app, rel, false);
        }
    });
}

/// Download, extract, hand off to the helper and quit (v2 `_install_release`).
pub fn install(app: &AppWindow, rel: Release, clean: bool) {
    let Some(asset) = updater::pick_asset(&rel.assets, Some(edition()), clean).cloned() else {
        let (os, arch) = updater::platform_tag();
        dialogs::confirm(
            app,
            DialogIcon::Warning,
            "No Matching Download",
            &format!("Release {} has no matching build for your system ({os}-{arch}).\n\nOpen the releases page to check manually?", rel.tag),
            "Open",
            false,
            |_| crate::platform::open_url(updater::RELEASES_URL),
        );
        return;
    };
    let cancel = dialogs::progress_start(app, &format!("Downloading {}…", rel.tag), true);
    let weak = app.as_weak();
    std::thread::spawn(move || {
        let w2 = weak.clone();
        let result = updater::prepare_update(&asset, &cancel, |done, total| {
            let v = if total > 0 { done as f32 / total as f32 } else { -1.0 };
            dialogs::progress_update(&w2, v, format!("{} / {}", crate::library_view::format_size(done), if total > 0 { crate::library_view::format_size(total) } else { "?".into() }));
        });
        let _ = weak.upgrade_in_event_loop(move |app| {
            dialogs::progress_close(&app);
            let staged = match result {
                Ok(Some(s)) => s,
                Ok(None) => return,
                Err(e) => return dialogs::message(&app, DialogIcon::Error, "Update Error", &format!("Could not download or extract the build:\n{e}")),
            };
            let preserve = PORTABLE_DATA.with(|p| p.borrow().clone());
            match updater::apply_and_restart(&staged, clean, preserve.as_deref()) {
                Ok(true) => {
                    crate::bgm_view::stop(&app);
                    let _ = slint::quit_event_loop();
                }
                Ok(false) => {
                    let _ = std::fs::remove_dir_all(&staged.stage_root);
                    dialogs::message(&app, DialogIcon::Info, "Update", "This update needs administrator permission, which was declined.\n\nNo changes were made.");
                }
                Err(e) => {
                    let _ = std::fs::remove_dir_all(&staged.stage_root);
                    dialogs::message(&app, DialogIcon::Error, "Update Error", &format!("Could not apply the build:\n{e}"));
                }
            }
        });
    });
}

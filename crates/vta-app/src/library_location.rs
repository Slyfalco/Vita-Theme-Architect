//! First-run "Choose a Theme Library Folder" recommendation and the relocate
//! flow behind it (v2 `recommend_library_location` + `change_theme_library_location`).
//!
//! Shown once, on the first launch where the library is still the default hidden
//! app-data folder. The Settings "Browse…" button deliberately does NOT move
//! themes (v2 only sets the path there); only this first-run "Choose Folder…"
//! path relocates and offers to move existing themes.

use crate::dialogs::{self, Kind};
use crate::settings_view::{pick_folder, SettingsCtx};
use crate::{AppWindow, DialogIcon, SettingsState};
use slint::ComponentHandle;
use std::path::{Path, PathBuf};
use std::rc::Rc;
use vta_core::library;
use vta_core::paths::{normcase, normpath};

fn same_path(a: &Path, b: &Path) -> bool {
    normcase(&normpath(a)) == normcase(&normpath(b))
}

fn mark_prompted(ctx: &SettingsCtx) {
    let mut s = ctx.settings.borrow_mut();
    s.set("library_location_prompted", true);
    let _ = s.save();
}

/// v2 `recommend_library_location`: offer to move the library out of the hidden
/// data folder, once, on first run.
pub fn recommend_on_first_run(app: &AppWindow, ctx: Rc<SettingsCtx>) {
    let forced = cfg!(debug_assertions) && std::env::var("VTA_DEV_FIRSTRUN").is_ok();
    if !forced && ctx.settings.borrow().flag("library_location_prompted", false) {
        return;
    }
    let current = ctx.library.borrow().root.clone();
    // Already pointing somewhere the user chose → nothing to recommend; just stop asking.
    if !forced && !same_path(&current, &ctx.dirs.default_themes_path()) {
        mark_prompted(&ctx);
        return;
    }
    let msg = format!(
        "Your themes are currently stored in the app's hidden data folder:\n\n{}\n\n\
         It is recommended to pick your own folder (for example a \"Vita Themes\" folder in Documents) \
         so your themes are easy to find, back up and keep if the app is removed.\n\n\
         You can change this later in Settings.",
        current.display()
    );
    let ctx2 = ctx.clone();
    dialogs::ask(
        app,
        DialogIcon::Info,
        "Choose a Theme Library Folder",
        &msg,
        "",
        &[("Ask Me Later", Kind::Ghost), ("Keep Default", Kind::Secondary), ("Choose Folder…", Kind::Primary)],
        move |app, i| match i {
            1 => mark_prompted(&ctx2),                 // Keep Default: don't ask again
            2 => choose_folder(app, &ctx2),            // Choose Folder…
            _ => {}                                    // Ask Me Later: ask again next launch
        },
    );
}

/// v2 `change_theme_library_location`, driven from the first-run recommendation.
fn choose_folder(app: &AppWindow, ctx: &Rc<SettingsCtx>) {
    let current = ctx.library.borrow().root.clone();
    let Some(picked) = pick_folder("Select Theme Library Folder", &current.to_string_lossy()) else {
        return; // cancelled: leave the prompt unanswered, ask again next launch
    };
    let new = PathBuf::from(picked);
    if same_path(&new, &current) {
        mark_prompted(ctx); // v2: same folder still counts as answered
        return;
    }
    if let Err(e) = library::ensure_writable_dir(&new) {
        dialogs::message(app, DialogIcon::Warning, "Error", &format!("Cannot use this folder: {e}"));
        return; // not writable: unanswered
    }
    let existing = library::theme_folder_names(&current);
    if existing.is_empty() {
        apply_relocation(app, ctx, &current, &new, false);
        mark_prompted(ctx);
        return;
    }
    // Existing themes: offer to move them. Either answer relocates (v2 stores the
    // new path regardless); "Yes" also moves the folders.
    let (c, old, nw) = (ctx.clone(), current.clone(), new.clone());
    dialogs::ask(
        app,
        DialogIcon::Question,
        "Move Existing Themes",
        &format!("Move your {} existing theme folder(s) to the new location?\n\n{}", existing.len(), new.display()),
        "",
        &[("No", Kind::Secondary), ("Yes", Kind::Primary)],
        move |app, i| {
            apply_relocation(app, &c, &old, &nw, i == 1);
            mark_prompted(&c);
        },
    );
}

fn apply_relocation(app: &AppWindow, ctx: &SettingsCtx, old: &Path, new: &Path, do_move: bool) {
    let mut failed = Vec::new();
    if do_move {
        failed = library::move_themes(old, new);
    }
    {
        let mut s = ctx.settings.borrow_mut();
        s.set("theme_library_path", ctx.dirs.store_path(&new.to_string_lossy()));
        let _ = s.save();
    }
    ctx.library.borrow_mut().root = new.to_path_buf();
    crate::library_view::rescan(app, &ctx.library);
    // Keep the Settings form showing the new path if it's open.
    app.global::<SettingsState>().set_theme_library_path(new.to_string_lossy().as_ref().into());
    if !failed.is_empty() {
        dialogs::message(
            app,
            DialogIcon::Warning,
            "Move Themes",
            &format!("Some themes could not be moved:\n {}", failed.join("\n ")),
        );
    }
}

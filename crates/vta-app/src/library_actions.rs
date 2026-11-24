//! Theme Library actions: import (zip / folder / bulk / from PS Vita), delete,
//! send to PS Vita, copy path. Behaviour follows v2 (main_window.py,
//! vita_importer.py); long work runs on a thread behind the progress overlay.

use crate::dialogs::{self, Kind};
use crate::library_view::{rescan, theme_at, SharedLibrary};
use crate::{AppWindow, DialogIcon, Library};
use slint::ComponentHandle;
use std::cell::RefCell;
use std::path::PathBuf;
use std::rc::Rc;
use vta_core::import;
use vta_core::settings::Settings;
use vta_core::vita::{self, Destination};

pub type SharedSettings = Rc<RefCell<Settings>>;

fn library_root(state: &SharedLibrary) -> PathBuf {
    state.borrow().root.clone()
}

fn after_import(app: &AppWindow, state: &SharedLibrary, result: import::ImportResult) {
    match result {
        Ok(title) => {
            rescan(app, state);
            dialogs::message(app, DialogIcon::Success, "Success", &format!("{title} was imported"));
        }
        Err((title, msg)) => dialogs::message(app, DialogIcon::Error, &title, &msg),
    }
}

/// Run `work` on a thread behind a progress overlay, then `done` on the UI thread.
fn run_with_progress<T: Send + 'static>(
    app: &AppWindow,
    title: &str,
    cancelable: bool,
    work: impl FnOnce(slint::Weak<AppWindow>, std::sync::Arc<std::sync::atomic::AtomicBool>) -> T + Send + 'static,
    done: impl FnOnce(&AppWindow, T) + Send + 'static,
) {
    let cancel = dialogs::progress_start(app, title, cancelable);
    let weak = app.as_weak();
    std::thread::spawn(move || {
        let result = work(weak.clone(), cancel);
        let _ = weak.upgrade_in_event_loop(move |app| {
            dialogs::progress_close(&app);
            done(&app, result);
        });
    });
}

pub fn import_zip(app: &AppWindow, state: &SharedLibrary) {
    let Some(path) = rfd::FileDialog::new().set_title("Import Theme Zip").add_filter("Zip Files", &["zip"]).pick_file() else {
        return;
    };
    let lib = library_root(state);
    run_with_progress(app, "Importing theme…", false, move |_, _| import::import_zip(&lib, &path), |app, r| {
        with_state(|st| after_import(app, st, r));
    });
}

thread_local! {
    /// The library state, reachable from closures that must be `Send` (they only
    /// ever run on the UI thread, where this lives).
    static LAST_STATE: RefCell<Option<SharedLibrary>> = RefCell::new(None);
}

fn remember_state(state: SharedLibrary) {
    LAST_STATE.with(|ls| *ls.borrow_mut() = Some(state));
}

pub fn with_state(f: impl FnOnce(&SharedLibrary)) {
    LAST_STATE.with(|ls| {
        if let Some(st) = ls.borrow().as_ref() {
            f(st);
        }
    });
}

pub fn import_folder(app: &AppWindow, state: &SharedLibrary) {
    let Some(path) = rfd::FileDialog::new().set_title("Import Theme Folder").pick_folder() else { return };
    let lib = library_root(state);
    run_with_progress(app, "Importing theme…", false, move |_, _| import::import_folder(&lib, &path), |app, r| {
        with_state(|st| after_import(app, st, r));
    });
}

/// v2 bulk import: a folder or a zip holding many themes.
pub fn import_multiple(app: &AppWindow, state: &SharedLibrary) {
    let lib = library_root(state);
    dialogs::ask(
        app,
        DialogIcon::Question,
        "Import Multiple Themes",
        "Import every theme found in a folder or a zip (themes may be folders or zips, even nested).",
        "",
        &[("Cancel", Kind::Secondary), ("From a Zip…", Kind::Secondary), ("From a Folder…", Kind::Primary)],
        move |app, i| {
            let source = match i {
                1 => rfd::FileDialog::new().set_title("Select Zip Containing Themes").add_filter("Zip Files", &["zip", "ZIP"]).pick_file(),
                2 => rfd::FileDialog::new().set_title("Select Folder Containing Themes").pick_folder(),
                _ => None,
            };
            let Some(source) = source else { return };
            run_with_progress(
                app,
                "Importing themes…",
                true,
                move |weak, cancel| {
                    import::bulk_import(&lib, &source, &cancel, |done, total| {
                        let v = if total == 0 { -1.0 } else { done as f32 / total as f32 };
                        dialogs::progress_update(&weak, v, format!("{done} of {total}"));
                    })
                },
                |app, report| {
                    with_state(|st| rescan(app, st));
                    if !report.found_any && report.errors.is_empty() {
                        dialogs::message(app, DialogIcon::Warning, "Bulk Import", "No themes (theme.xml) were found.");
                        return;
                    }
                    let mut text = format!("Imported {} theme(s).", report.imported.len());
                    if !report.errors.is_empty() {
                        text.push_str(&format!("\n{} item(s) were skipped. See details.", report.errors.len()));
                    }
                    let mut details = Vec::new();
                    if !report.imported.is_empty() {
                        details.push(format!("Imported:\n {}", report.imported.join("\n ")));
                    }
                    if !report.errors.is_empty() {
                        details.push(format!("Skipped:\n {}", report.errors.join("\n ")));
                    }
                    let icon = if report.errors.is_empty() { DialogIcon::Success } else { DialogIcon::Warning };
                    dialogs::message_details(app, icon, "Bulk Import", &text, &details.join("\n\n"));
                },
            );
        },
    );
}

pub fn delete(app: &AppWindow, state: &SharedLibrary, index: i32) {
    let Some(theme) = theme_at(state, index) else { return };
    let name = theme.title().to_string();
    let path = theme.path.clone();
    dialogs::confirm(
        app,
        DialogIcon::Warning,
        "Confirm Delete",
        &format!("Are you sure you want to permanently delete '{name}'?"),
        "Delete",
        true,
        move |app| {
            crate::bgm_view::stop(app);
            match std::fs::remove_dir_all(&path) {
                Ok(()) => {
                    with_state(|st| rescan(app, st));
                    crate::show_toast(app, &format!("Theme '{name}' has been deleted."), false);
                }
                Err(e) => dialogs::message(app, DialogIcon::Error, "Error", &format!("Could not delete theme: {e}")),
            }
        },
    );
}

/// The configured Vita address, or a message telling the user to set it.
pub fn vita_address(settings: &SharedSettings) -> Result<(String, u16), String> {
    let s = settings.borrow();
    let ip = s.str_or("vita_ip", "").trim().to_string();
    if ip.is_empty() {
        return Err("PS Vita IP address is not set. Please set it in Settings → Connection.".into());
    }
    let port = match s.values.get("vita_port") {
        Some(serde_json::Value::Number(n)) => n.as_u64().map(|p| p as u16),
        Some(serde_json::Value::String(p)) => p.trim().parse().ok(),
        _ => None,
    }
    .unwrap_or(vita::DEFAULT_PORT);
    Ok((ip, port))
}

/// v2 `send_to_vita`, with its two bugs fixed: overwriting now really clears the
/// old files, and "send to a separate folder" moves on instead of looping forever.
pub fn send_to_vita(app: &AppWindow, state: &SharedLibrary, settings: &SharedSettings, index: i32) {
    let Some(theme) = theme_at(state, index) else { return };
    let (ip, port) = match vita_address(settings) {
        Ok(a) => a,
        Err(e) => return dialogs::message(app, DialogIcon::Warning, "Error", &e),
    };
    probe(app, ip, port, theme, 0);
}

fn probe(app: &AppWindow, ip: String, port: u16, theme: vta_core::library::LibraryTheme, start: u32) {
    let data = theme.data.clone();
    let (ip2, theme2) = (ip.clone(), theme.clone());
    run_with_progress(
        app,
        &format!("Connecting to {ip}:{port}…"),
        false,
        move |_, _| vita::probe_destination(&ip, port, &data, start),
        move |app, result| match result {
            Err(e) => dialogs::message(app, DialogIcon::Error, "FTP Transfer Error", &format!("Could not send theme:\n{e}")),
            Ok((dest, slot)) => decide(app, ip2, port, theme2, dest, slot),
        },
    );
}

fn decide(app: &AppWindow, ip: String, port: u16, theme: vta_core::library::LibraryTheme, dest: Destination, slot: u32) {
    let title = theme.title().to_string();
    let local_v = theme.version().to_string();
    let folder = dest.folder().to_string();
    let other_folder = move |app: &AppWindow, ip: String, theme| {
        let f2 = folder.clone();
        dialogs::confirm(app, DialogIcon::Question, "Send to Vita", &format!("Send to a separate folder than {f2}?"), "Yes", false, move |app| {
            probe(app, ip, port, theme, slot + 1);
        });
    };
    match dest {
        Destination::New { folder } => upload(app, ip, port, theme, folder, false),
        Destination::SameTheme { folder, remote_version, order } => match order {
            std::cmp::Ordering::Less => dialogs::message(
                app,
                DialogIcon::Warning,
                "Upload Error",
                &format!("{title} already has a newer version '{remote_version}.'"),
            ),
            ord => {
                let text = if ord == std::cmp::Ordering::Greater {
                    format!("Local theme '{title}' (v{local_v}) is newer than the one on Vita (v{remote_version}) at '{folder}'.\n\nOverwrite it?")
                } else {
                    format!("Local theme '{title}' (v{local_v}) already exists at '{folder}'.\n\nOverwrite it?")
                };
                let (ip_a, theme_a, folder_a) = (ip.clone(), theme.clone(), folder.clone());
                dialogs::ask(app, DialogIcon::Question, "Confirm Overwrite", &text, "", &[("No", Kind::Secondary), ("Overwrite", Kind::Primary)], move |app, i| {
                    if i == 1 {
                        upload(app, ip_a, port, theme_a, folder_a, true);
                    } else {
                        other_folder(app, ip, theme);
                    }
                });
            }
        },
        Destination::Unreadable { folder, error } => {
            let (ip_a, theme_a, folder_a) = (ip.clone(), theme.clone(), folder.clone());
            dialogs::ask(
                app,
                DialogIcon::Warning,
                "Remote XML Error",
                &format!("Could not read theme.xml for '{folder}' on Vita: {error}.\n\nTry to overwrite this folder?"),
                "",
                &[("No", Kind::Secondary), ("Overwrite", Kind::Primary)],
                move |app, i| {
                    if i == 1 {
                        upload(app, ip_a, port, theme_a, folder_a, true);
                    } else {
                        probe(app, ip, port, theme, slot + 1);
                    }
                },
            );
        }
    }
}

fn upload(app: &AppWindow, ip: String, port: u16, theme: vta_core::library::LibraryTheme, folder: String, overwrite: bool) {
    let title = theme.title().to_string();
    let dir = theme.path.clone();
    let folder2 = folder.clone();
    run_with_progress(
        app,
        &format!("Transferring '{title}' to {folder}"),
        true,
        move |weak, cancel| {
            vita::upload_theme(&ip, port, &dir, &folder, overwrite, &cancel, |done, total, file| {
                let v = if total == 0 { 1.0 } else { done as f32 / total as f32 };
                dialogs::progress_update(&weak, v, if file.is_empty() { String::new() } else { format!("Uploading {file}...") });
            })
        },
        move |app, result| match result {
            Ok(true) => dialogs::message(
                app,
                DialogIcon::Success,
                "Transfer Complete",
                &format!("Theme '{title}' sent to Vita successfully at:\n{}/{folder2}", vita::REMOTE_BASE),
            ),
            Ok(false) => dialogs::message(app, DialogIcon::Warning, "Cancelled", "Theme transfer was cancelled."),
            Err(e) => dialogs::message(app, DialogIcon::Error, "FTP Transfer Error", &format!("Could not send theme:\n{e}")),
        },
    );
}

/// v2 `VitaImporterDialog`: list ux0:customtheme, pick themes, download them.
pub fn import_from_vita(app: &AppWindow, state: &SharedLibrary, settings: &SharedSettings) {
    let (ip, port) = match vita_address(settings) {
        Ok(a) => a,
        Err(e) => return dialogs::message(app, DialogIcon::Warning, "Error", &e),
    };
    let lib = library_root(state);
    let ip2 = ip.clone();
    run_with_progress(
        app,
        &format!("Connecting to {ip}:{port}…"),
        false,
        move |_, _| vita::list_remote_themes(&ip, port),
        move |app, result| match result {
            Err(e) => dialogs::message(app, DialogIcon::Error, "FTP Error", &format!("Could not connect or list themes:\n{e}")),
            Ok(names) if names.is_empty() => {
                dialogs::message(app, DialogIcon::Info, "Import from PS Vita", "Connected — no themes were found in ux0:customtheme.")
            }
            Ok(names) => {
                let n = names.len();
                let shown = names.clone();
                dialogs::pick(
                    app,
                    "Import from PS Vita",
                    &format!("Connected — {n} theme(s) found. Select to import."),
                    &shown,
                    "Download & Import Selected",
                    move |app, chosen| {
                        let Some(chosen) = chosen else { return };
                        if chosen.is_empty() {
                            return dialogs::message(app, DialogIcon::Warning, "No Selection", "Please select one or more themes to import.");
                        }
                        let picked: Vec<String> = chosen.iter().map(|&i| names[i].clone()).collect();
                        run_with_progress(
                            app,
                            "Importing themes from Vita...",
                            true,
                            move |weak, cancel| {
                                vita::download_themes(&ip2, port, &picked, |_| {
                                    let p = import::unique_theme_path(&lib);
                                    let _ = std::fs::create_dir_all(&p);
                                    p
                                }, &cancel, |done, total, label| {
                                    let v = if total == 0 { 1.0 } else { done as f32 / total as f32 };
                                    dialogs::progress_update(&weak, v, label.to_string());
                                })
                            },
                            |app, result| {
                                with_state(|st| rescan(app, st));
                                match result {
                                    Ok((imported, errors)) => {
                                        let text = format!("Imported {} themes.", imported.len());
                                        if errors.is_empty() {
                                            dialogs::message(app, DialogIcon::Success, "Success", &text);
                                        } else {
                                            dialogs::message_details(app, DialogIcon::Warning, "Import from PS Vita", &text, &errors.join("\n"));
                                        }
                                    }
                                    Err(e) => dialogs::message(app, DialogIcon::Error, "FTP Error", &format!("An error occurred during import:\n{e}")),
                                }
                            },
                        );
                    },
                );
            }
        },
    );
}

pub fn wire(app: &AppWindow, state: &SharedLibrary, settings: &SharedSettings) {
    remember_state(state.clone());
    let lib = app.global::<Library>();
    let (a, s) = (app.as_weak(), state.clone());
    lib.on_import_zip(move || import_zip(&a.unwrap(), &s));
    let (a, s) = (app.as_weak(), state.clone());
    lib.on_import_folder(move || import_folder(&a.unwrap(), &s));
    let (a, s) = (app.as_weak(), state.clone());
    lib.on_import_multiple(move || import_multiple(&a.unwrap(), &s));
    let (a, s, st) = (app.as_weak(), state.clone(), settings.clone());
    lib.on_import_from_vita(move || import_from_vita(&a.unwrap(), &s, &st));
    let (a, s) = (app.as_weak(), state.clone());
    lib.on_delete(move |i| delete(&a.unwrap(), &s, i));
    let (a, s, st) = (app.as_weak(), state.clone(), settings.clone());
    lib.on_send_to_vita(move |i| send_to_vita(&a.unwrap(), &s, &st, i));
    let (a, s) = (app.as_weak(), state.clone());
    lib.on_copy_path(move || {
        let path = s.borrow().root.to_string_lossy().into_owned();
        let ok = arboard::Clipboard::new().and_then(|mut c| c.set_text(path)).is_ok();
        crate::show_toast(&a.unwrap(), if ok { "Library path copied to the clipboard." } else { "Could not access the clipboard." }, !ok);
    });
}

//! Settings > AI Icons "AI storage" box (v2 `SettingsDialog.refresh_storage` /
//! `_show_storage` / `remove_setup` / `remove_flux2_tier` / `remove_all_ai` /
//! `_share_calibration`).

use crate::{dialogs, AppWindow, DialogIcon, SettingsState, StorageRow};
use slint::{ComponentHandle, ModelRc, VecModel};
use std::cell::RefCell;
use std::path::{Path, PathBuf};
use vta_core::iconai::estimates::{format_size, memory_note};
use vta_core::iconai::IconAi;
use vta_core::paths::DataDirs;

#[derive(Clone)]
enum Remove {
    Setup { model: String, poses: bool, label: String },
    Flux2 { variant: String, label: String },
}

#[derive(Default)]
struct State {
    ai: Option<IconAi>,
    actions: Vec<Option<Remove>>,
    total: Option<u64>,
    generation: u64,
}

thread_local! {
    static S: RefCell<State> = RefCell::new(State::default());
}

fn with<R>(f: impl FnOnce(&mut State) -> R) -> R {
    S.with(|s| f(&mut s.borrow_mut()))
}

fn ai() -> Option<IconAi> {
    with(|s| s.ai.clone())
}

/// Measure in the background; the table fills in when done.
pub fn refresh(app: &AppWindow) {
    let Some(ai) = ai() else { return };
    let st = app.global::<SettingsState>();
    st.set_storage_state(0);
    let generation = with(|s| {
        s.generation += 1;
        s.generation
    });
    let weak = app.as_weak();
    std::thread::spawn(move || {
        let report = ai.storage_report();
        let count = ai.calibration_count();
        let min_mem = ai.settings().flag("ai_min_memory", false);
        let dir = ai.icon_ai_dir();
        let _ = weak.upgrade_in_event_loop(move |app| {
            if with(|s| s.generation) != generation {
                return; // an older measurement
            }
            show(&app, report, count, min_mem, &dir);
        });
    });
}

fn show(app: &AppWindow, report: vta_core::iconai::storage::StorageReport, count: usize, min_mem: bool, dir: &Path) {
    let st = app.global::<SettingsState>();
    with(|s| s.total = Some(report.total));
    if report.total == 0 {
        st.set_storage_state(2);
        return;
    }
    st.set_storage_total(format!("Total: {} in {}", format_size(report.total as f64), dir.display()).into());
    st.set_storage_shared_note(
        if report.shared {
            format!(
                "Models are in a shared cache: {} — manage them there (they may be used by other apps, so this app won't delete them).",
                report.models_dir.display()
            )
        } else {
            String::new()
        }
        .into(),
    );
    let mut rows: Vec<(String, u64, Option<Remove>, Option<String>)> =
        vec![("PyTorch and AI libraries (plus a small ~140 MB Python to run them)".into(), report.runtime, None, None)];
    for u in &report.setups {
        let mut label = u.label.clone();
        if u.poses {
            label.push_str(" (only for the experimental AI poses option)");
        }
        let size = if u.installed || u.bytes > 0 { u.bytes } else { 0 };
        let action = Remove::Setup { model: u.model.clone(), poses: u.poses, label: label.clone() };
        rows.push((label, size, Some(action), (!u.poses).then(|| u.model.clone())));
    }
    for f in &report.flux2 {
        let action = f.present.then(|| Remove::Flux2 { variant: f.variant.clone(), label: f.label.clone() });
        rows.push((f.label.clone(), if f.present { f.bytes } else { 0 }, action, Some(f.variant.clone())));
    }
    rows.push(("Shared helper models (needed by both models)".into(), report.helpers, None, None));
    rows.push(("Other (example icon sets, extra artwork, caches)".into(), report.other, None, None));
    let mut actions = Vec::new();
    let model: Vec<StorageRow> = rows
        .into_iter()
        .map(|(name, size, remove, key)| {
            let removable = remove.is_some();
            actions.push(remove);
            StorageRow {
                name: name.into(),
                note: key.map(|k| memory_note(&k, min_mem)).unwrap_or_default().into(),
                size: if size > 0 { format_size(size as f64) } else { "not installed".into() }.into(),
                removable,
                remove_enabled: size > 0 && !report.shared,
            }
        })
        .collect();
    with(|s| s.actions = actions);
    st.set_storage_rows(ModelRc::new(VecModel::from(model)));
    st.set_calibration_note(
        if count > 0 {
            format!(
                "Performance data: {count} de-duplicated record{} (hardware, settings, timing and memory — no personal or network data).",
                if count != 1 { "s" } else { "" }
            )
        } else {
            String::new()
        }
        .into(),
    );
    // Sharing is an online feature: official builds only (v2 ONLINE_FEATURES_AVAILABLE).
    st.set_can_share_calibration(count > 0 && vta_online::available());
    st.set_storage_state(3);
}

fn remove_row(app: &AppWindow, i: usize) {
    let Some(Some(action)) = with(|s| s.actions.get(i).cloned()) else { return };
    let (title, text) = match &action {
        Remove::Setup { poses, label, .. } => (
            "Remove AI Model",
            format!(
                "Delete the {label}?{}\n\nEverything the other model still needs is kept, so it keeps working. You can download it again from the icon generator.",
                if *poses { "" } else { " Its AI poses add-on is removed too." }
            ),
        ),
        Remove::Flux2 { label, .. } => ("Remove AI Model", format!("Delete the {label}?\n\nYou can download it again from the icon generator.")),
    };
    dialogs::confirm(app, DialogIcon::Question, title, &text, "Yes", false, move |app| {
        if let Some(ai) = ai() {
            match &action {
                Remove::Setup { model, poses, .. } => ai.remove_model_setup(model, *poses),
                Remove::Flux2 { variant, .. } => ai.remove_flux2(variant),
            }
        }
        refresh(app);
    });
}

fn remove_all(app: &AppWindow) {
    let Some(ai) = ai() else { return };
    let total = with(|s| s.total).unwrap_or_else(|| ai.runtime_disk_usage());
    dialogs::confirm(
        app,
        DialogIcon::Question,
        "Remove All AI Components",
        &format!("Delete the AI runtime and every model ({})?\n\n{}", format_size(total as f64), ai.icon_ai_dir().display()),
        "Yes",
        false,
        move |app| {
            ai.remove_runtime();
            refresh(app);
        },
    );
}

fn reveal_file(path: &Path) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let _ = std::process::Command::new("explorer").raw_arg(format!("/select,\"{}\"", path.display())).spawn();
    }
    #[cfg(target_os = "macos")]
    let _ = std::process::Command::new("open").arg("-R").arg(path).spawn();
    #[cfg(all(unix, not(target_os = "macos")))]
    if let Some(dir) = path.parent() {
        let _ = std::process::Command::new("xdg-open").arg(dir).spawn();
    }
}

fn share_calibration(app: &AppWindow) {
    let Some(ai) = ai() else { return };
    let Some(mut dest) = rfd::FileDialog::new()
        .set_title("Share Performance Data With Creator")
        .set_file_name("vta_performance_data.jsonl")
        .add_filter("Data files", &["jsonl"])
        .save_file()
    else {
        return;
    };
    if !dest.to_string_lossy().to_lowercase().ends_with(".jsonl") {
        dest = PathBuf::from(format!("{}.jsonl", dest.display())); // keep it to the one accepted type
    }
    let written = match ai.export_calibration(&dest) {
        Ok(n) => n,
        Err(e) => return dialogs::message(app, DialogIcon::Warning, "Could not save", &format!("The data could not be saved:\n{e}")),
    };
    dialogs::ask(
        app,
        DialogIcon::Info,
        "Ready to share",
        &format!(
            "Saved {written} validated, de-duplicated record{} to:\n{}\n\nSend this file to the creator (for example by email or by attaching it to a GitHub issue) to help improve the estimates. It contains only hardware details, the settings used and the measured time and memory — no personal or network data.",
            if written != 1 { "s" } else { "" },
            dest.display()
        ),
        "",
        &[("Show file", dialogs::Kind::Secondary), ("OK", dialogs::Kind::Primary)],
        move |_, i| {
            if i == 0 {
                reveal_file(&dest);
            }
        },
    );
}

pub fn wire(app: &AppWindow, dirs: &DataDirs) {
    if !cfg!(feature = "icon-ai") {
        return;
    }
    with(|s| s.ai = Some(IconAi::new(dirs, &crate::icongen_view::app_dir())));
    let st = app.global::<SettingsState>();
    let a = app.as_weak();
    st.on_refresh_storage(move || refresh(&a.unwrap()));
    let a = app.as_weak();
    st.on_remove_storage_row(move |i| remove_row(&a.unwrap(), i as usize));
    let a = app.as_weak();
    st.on_remove_all_ai(move || remove_all(&a.unwrap()));
    let a = app.as_weak();
    st.on_share_calibration(move || share_calibration(&a.unwrap()));
}

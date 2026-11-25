//! Rust side of the in-window dialogs (ui/dialogs.slint): message/confirm boxes,
//! a progress overlay for background work, and a multi-select picker.

use crate::{AppWindow, Dialog, DialogButton, DialogIcon, PickItem};
use slint::{ComponentHandle, Model, ModelRc, SharedString, VecModel, Weak};
use std::cell::RefCell;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

type Answer = Box<dyn FnOnce(&AppWindow, usize)>;
type PickAnswer = Box<dyn FnOnce(&AppWindow, Option<Vec<usize>>)>;

thread_local! {
    static PENDING: RefCell<Option<Answer>> = RefCell::new(None);
    static PENDING_PICK: RefCell<Option<PickAnswer>> = RefCell::new(None);
    static CANCEL: RefCell<Option<Arc<AtomicBool>>> = RefCell::new(None);
}

pub use crate::ButtonKind as Kind;

/// Show a dialog; `on_answer` gets the index of the clicked button.
pub fn ask(
    app: &AppWindow,
    icon: DialogIcon,
    title: &str,
    message: &str,
    details: &str,
    buttons: &[(&str, Kind)],
    on_answer: impl FnOnce(&AppWindow, usize) + 'static,
) {
    let d = app.global::<Dialog>();
    d.set_icon(icon);
    d.set_title(title.into());
    d.set_message(message.into());
    d.set_details(details.into());
    let buttons: Vec<DialogButton> = buttons
        .iter()
        .map(|(t, k)| DialogButton { text: SharedString::from(*t), kind: *k })
        .collect();
    d.set_buttons(ModelRc::new(VecModel::from(buttons)));
    d.set_has_input(false);
    PENDING.with(|p| *p.borrow_mut() = Some(Box::new(on_answer)));
    d.set_open(true);
}

/// A dialog with a one-line text entry; `on_answer` gets the clicked button and
/// the text entered.
#[allow(clippy::too_many_arguments)]
pub fn ask_text(
    app: &AppWindow,
    icon: DialogIcon,
    title: &str,
    message: &str,
    placeholder: &str,
    password: bool,
    buttons: &[(&str, Kind)],
    on_answer: impl FnOnce(&AppWindow, usize, String) + 'static,
) {
    ask(app, icon, title, message, "", buttons, move |app, i| {
        let d = app.global::<Dialog>();
        let text = d.get_input().to_string();
        d.set_input("".into());
        d.set_has_input(false);
        on_answer(app, i, text)
    });
    let d = app.global::<Dialog>();
    d.set_input("".into());
    d.set_input_placeholder(placeholder.into());
    d.set_input_password(password);
    d.set_has_input(true);
}

/// A message with a single OK button.
pub fn message(app: &AppWindow, icon: DialogIcon, title: &str, message: &str) {
    ask(app, icon, title, message, "", &[("OK", Kind::Primary)], |_, _| {});
}

pub fn message_details(app: &AppWindow, icon: DialogIcon, title: &str, text: &str, details: &str) {
    ask(app, icon, title, text, details, &[("OK", Kind::Primary)], |_, _| {});
}

/// Yes/No question; `on_yes` runs only for Yes.
pub fn confirm(app: &AppWindow, icon: DialogIcon, title: &str, text: &str, yes: &str, danger: bool, on_yes: impl FnOnce(&AppWindow) + 'static) {
    let kind = if danger { Kind::Danger } else { Kind::Primary };
    ask(app, icon, title, text, "", &[("No", Kind::Secondary), (yes, kind)], move |app, i| {
        if i == 1 {
            on_yes(app);
        }
    });
}

/// Open the progress overlay. Returns the cancel flag for the worker to poll.
pub fn progress_start(app: &AppWindow, title: &str, cancelable: bool) -> Arc<AtomicBool> {
    let d = app.global::<Dialog>();
    d.set_progress_title(title.into());
    d.set_progress_label("".into());
    d.set_progress_value(-1.0);
    d.set_progress_cancelable(cancelable);
    d.set_progress_cancelling(false);
    d.set_progress_open(true);
    let flag = Arc::new(AtomicBool::new(false));
    CANCEL.with(|c| *c.borrow_mut() = Some(flag.clone()));
    flag
}

/// Update the progress overlay from any thread.
pub fn progress_update(weak: &Weak<AppWindow>, value: f32, label: impl Into<String>) {
    let label = label.into();
    let _ = weak.upgrade_in_event_loop(move |app| {
        let d = app.global::<Dialog>();
        d.set_progress_value(value);
        d.set_progress_label(label.into());
    });
}

pub fn progress_close(app: &AppWindow) {
    app.global::<Dialog>().set_progress_open(false);
    CANCEL.with(|c| *c.borrow_mut() = None);
}

/// Multi-select list; `on_answer` gets the chosen indices, or None if cancelled.
pub fn pick(
    app: &AppWindow,
    title: &str,
    message: &str,
    items: &[String],
    confirm: &str,
    on_answer: impl FnOnce(&AppWindow, Option<Vec<usize>>) + 'static,
) {
    let d = app.global::<Dialog>();
    d.set_picker_title(title.into());
    d.set_picker_message(message.into());
    d.set_picker_confirm(confirm.into());
    let items: Vec<PickItem> = items.iter().map(|t| PickItem { text: t.into(), checked: false }).collect();
    d.set_picker_items(ModelRc::new(VecModel::from(items)));
    PENDING_PICK.with(|p| *p.borrow_mut() = Some(Box::new(on_answer)));
    d.set_picker_open(true);
}

pub fn wire(app: &AppWindow) {
    let d = app.global::<Dialog>();
    let w = app.as_weak();
    d.on_answer(move |i| {
        let app = w.unwrap();
        app.global::<Dialog>().set_open(false);
        if let Some(cb) = PENDING.with(|p| p.borrow_mut().take()) {
            cb(&app, i as usize);
        }
    });
    d.on_progress_cancel(|| {
        CANCEL.with(|c| {
            if let Some(flag) = c.borrow().as_ref() {
                flag.store(true, Ordering::SeqCst);
            }
        });
    });
    let w = app.as_weak();
    d.on_picker_toggle(move |i| {
        let app = w.unwrap();
        let items = app.global::<Dialog>().get_picker_items();
        if let Some(mut item) = items.row_data(i as usize) {
            item.checked = !item.checked;
            items.set_row_data(i as usize, item);
        }
    });
    let w = app.as_weak();
    d.on_picker_answer(move |ok| {
        let app = w.unwrap();
        let d = app.global::<Dialog>();
        d.set_picker_open(false);
        let chosen: Vec<usize> = d
            .get_picker_items()
            .iter()
            .enumerate()
            .filter(|(_, it)| it.checked)
            .map(|(i, _)| i)
            .collect();
        if let Some(cb) = PENDING_PICK.with(|p| p.borrow_mut().take()) {
            cb(&app, ok.then_some(chosen));
        }
    });
}

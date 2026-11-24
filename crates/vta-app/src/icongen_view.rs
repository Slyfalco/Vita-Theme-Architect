//! AI icon generator (AI edition), opened from the Theme Creator.
//! (Controller being ported from v2 `IconGeneratorDialog`.)

use crate::{AppWindow, IconGen, Page, Shell};
use slint::ComponentHandle;

pub fn open(app: &AppWindow) {
    app.global::<IconGen>().set_view(0);
    app.global::<Shell>().set_page(Page::Icongen);
}

pub fn wire(app: &AppWindow) {
    let a = app.as_weak();
    app.global::<IconGen>().on_close(move || a.unwrap().global::<Shell>().set_page(Page::Creator));
}

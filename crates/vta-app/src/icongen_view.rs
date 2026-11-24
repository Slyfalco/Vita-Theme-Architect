//! AI icon generator (AI edition). Opened from the Theme Creator.

use crate::AppWindow;

pub fn open(app: &AppWindow) {
    crate::dialogs::message(app, crate::DialogIcon::Info, "AI Icon Generator", "The AI icon generator is being ported to v3.");
}

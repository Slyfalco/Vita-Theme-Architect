//! The Library screen's BGM player and "Extract BGM".
//!
//! Phase 1 decodes the whole AT9 into memory and plays the PCM (see audio.rs).
//! Phase 2 swaps that for at9codec's streaming playback with looping; only
//! audio.rs internals change, this controller and the UI stay the same.

use crate::audio::{self, Player, Track};
use crate::library_view::{theme_at, SharedLibrary};
use crate::{AppWindow, Library};
use slint::{ComponentHandle, ModelRc, VecModel};
use std::cell::RefCell;
use std::path::PathBuf;
use std::sync::Arc;
use vta_core::library::LibraryTheme;

#[derive(Default)]
struct Bgm {
    /// Bumped on every selection change so stale background decodes are dropped.
    generation: u64,
    path: Option<PathBuf>,
    track: Option<Arc<Track>>,
    player: Option<Player>,
    play_when_ready: bool,
    looping: bool,
    timer: Option<slint::Timer>,
}

thread_local! {
    static BGM: RefCell<Bgm> = RefCell::new(Bgm { looping: true, ..Default::default() });
}

const WAVE_BINS: usize = 40;

fn reset_ui(app: &AppWindow) {
    let lib = app.global::<Library>();
    lib.set_bgm_playing(false);
    lib.set_bgm_progress(0.0);
    lib.set_bgm_time("00:00".into());
    lib.set_bgm_length("--:--".into());
    lib.set_bgm_wave(ModelRc::new(VecModel::from(Vec::<f32>::new())));
}

/// Stop playback and forget the loaded track (e.g. before deleting a theme).
pub fn stop(app: &AppWindow) {
    BGM.with(|b| {
        let mut b = b.borrow_mut();
        b.generation += 1;
        b.path = None;
        b.track = None;
        b.player = None;
        b.timer = None;
    });
    reset_ui(app);
}

/// Called when the selection changes: stop playback and decode the new theme's BGM.
pub fn load(app: &AppWindow, theme: Option<&LibraryTheme>) {
    let path = theme.and_then(|t| t.asset_path("bgm.at9")).filter(|p| p.is_file());
    if path.is_some() && BGM.with(|b| b.borrow().path == path) {
        // Same track: keep playing, but re-apply the decoded info to the new detail.
        let info = BGM.with(|b| b.borrow().track.as_ref().map(|t| t.describe()));
        if let Some(info) = info {
            let lib = app.global::<Library>();
            let mut d = lib.get_detail();
            d.bgm_info = info.into();
            lib.set_detail(d);
        }
        return;
    }
    let generation = BGM.with(|b| {
        let mut b = b.borrow_mut();
        b.generation += 1;
        b.path = path.clone();
        b.track = None;
        b.player = None;
        b.play_when_ready = false;
        b.timer = None;
        b.generation
    });
    reset_ui(app);
    let Some(path) = path else { return };
    let lib = app.global::<Library>();
    let mut detail = lib.get_detail();
    detail.bgm_info = "Decoding…".into();
    lib.set_detail(detail);
    let weak = app.as_weak();
    std::thread::spawn(move || {
        let result = audio::decode_file(&path);
        let _ = slint::invoke_from_event_loop(move || {
            let Some(app) = weak.upgrade() else { return };
            if !BGM.with(|b| b.borrow().generation == generation) {
                return;
            }
            let lib = app.global::<Library>();
            let mut detail = lib.get_detail();
            match result {
                Ok(track) => {
                    detail.bgm_info = track.describe().into();
                    lib.set_bgm_length(audio::format_time(track.duration_secs()).into());
                    lib.set_bgm_wave(ModelRc::new(VecModel::from(track.waveform(WAVE_BINS))));
                    let play = BGM.with(|b| {
                        let mut b = b.borrow_mut();
                        b.track = Some(Arc::new(track));
                        b.play_when_ready
                    });
                    lib.set_detail(detail);
                    if play {
                        toggle_play(&app);
                    }
                }
                Err(e) => {
                    detail.bgm_info = "Could not decode this BGM".into();
                    lib.set_detail(detail);
                    crate::show_toast(&app, &e, true);
                }
            }
        });
    });
}

fn sync_ui(app: &AppWindow) {
    BGM.with(|b| {
        let b = b.borrow();
        let lib = app.global::<Library>();
        if let Some(p) = &b.player {
            lib.set_bgm_playing(p.is_playing());
            lib.set_bgm_progress(p.progress());
            lib.set_bgm_time(audio::format_time(p.position_secs()).into());
        }
    });
}

fn ensure_timer(app: &AppWindow) {
    BGM.with(|b| {
        let mut b = b.borrow_mut();
        if b.timer.is_none() {
            let t = slint::Timer::default();
            let weak = app.as_weak();
            t.start(slint::TimerMode::Repeated, std::time::Duration::from_millis(100), move || {
                if let Some(app) = weak.upgrade() {
                    sync_ui(&app);
                }
            });
            b.timer = Some(t);
        }
    });
}

pub fn toggle_play(app: &AppWindow) {
    let result: Result<(), String> = BGM.with(|b| {
        let mut b = b.borrow_mut();
        let Some(track) = b.track.clone() else {
            // Still decoding: start as soon as it's ready.
            b.play_when_ready = b.path.is_some();
            return Ok(());
        };
        if b.player.is_none() {
            let looping = b.looping;
            b.player = Some(Player::new(track, looping)?);
        }
        let p = b.player.as_ref().expect("player was just created");
        if p.is_playing() {
            p.pause();
        } else {
            p.play();
        }
        Ok(())
    });
    match result {
        Ok(()) => {
            ensure_timer(app);
            sync_ui(app);
        }
        Err(e) => crate::show_toast(app, &e, true),
    }
}

fn safe_title(title: &str) -> String {
    let s: String = title.chars().filter(|c| c.is_alphanumeric() || *c == ' ' || *c == '_').collect();
    s.trim_end().to_string()
}

/// v2 "Extract BGM": decode the theme's bgm.at9 and save it as a WAV.
pub fn extract(app: &AppWindow, state: &SharedLibrary, index: i32) {
    let Some(theme) = theme_at(state, index) else { return };
    let Some(rel) = theme.data.assets.get("bgm.at9").filter(|p| !p.is_empty()).cloned() else {
        crate::show_toast(app, "This theme has no BGM listed in its theme.xml.", true);
        return;
    };
    let src = theme.path.join(&rel);
    if !src.is_file() {
        crate::show_toast(app, &format!("{rel} was not found in the theme folder."), true);
        return;
    }
    let Some(dest) = rfd::FileDialog::new()
        .set_title("Save BGM As WAV")
        .set_file_name(format!("{}_bgm.wav", safe_title(theme.title())))
        .add_filter("WAV Files", &["wav"])
        .save_file()
    else {
        return;
    };
    let cached = BGM.with(|b| {
        let b = b.borrow();
        if b.path.as_deref() == Some(src.as_path()) {
            b.track.clone()
        } else {
            None
        }
    });
    crate::show_toast(app, "Converting audio…", false);
    let weak = app.as_weak();
    std::thread::spawn(move || {
        let result = match cached {
            Some(t) => audio::write_wav(&t, &dest).map_err(|e| e.to_string()),
            None => audio::decode_file(&src).and_then(|t| audio::write_wav(&t, &dest).map_err(|e| e.to_string())),
        };
        let _ = slint::invoke_from_event_loop(move || {
            if let Some(app) = weak.upgrade() {
                match result {
                    Ok(()) => crate::show_toast(&app, &format!("BGM extracted successfully to:\n{}", dest.display()), false),
                    Err(e) => crate::show_toast(&app, &format!("Failed to convert the BGM to WAV: {e}"), true),
                }
            }
        });
    });
}

pub fn wire(app: &AppWindow, state: &SharedLibrary) {
    let lib = app.global::<Library>();
    lib.set_bgm_loop(true);
    let a = app.as_weak();
    lib.on_bgm_toggle_play(move || toggle_play(&a.unwrap()));
    let a = app.as_weak();
    lib.on_bgm_toggle_loop(move || {
        let app = a.unwrap();
        let on = !app.global::<Library>().get_bgm_loop();
        app.global::<Library>().set_bgm_loop(on);
        BGM.with(|b| {
            let mut b = b.borrow_mut();
            b.looping = on;
            if let Some(p) = &b.player {
                p.set_looping(on);
            }
        });
    });
    let a = app.as_weak();
    lib.on_bgm_seek(move |f| {
        BGM.with(|b| {
            if let Some(p) = &b.borrow().player {
                p.seek(f);
            }
        });
        sync_ui(&a.unwrap());
    });
    let (a, s) = (app.as_weak(), state.clone());
    lib.on_extract_bgm(move |i| extract(&a.unwrap(), &s, i));
}

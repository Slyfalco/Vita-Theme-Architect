// A GUI app on Windows in every build: no console window. Debug builds attach to
// the terminal they were started from (if any) so log output still shows.
#![windows_subsystem = "windows"]

slint::include_modules!();

mod app_theme;
mod audio;
mod bgm_view;
mod community_view;
mod crashlog;
mod creator_view;
mod icongen_view;
mod dialogs;
mod library_actions;
mod library_view;
mod platform;
mod settings_view;
mod submit_view;
mod updater_view;

use library_view::{LibraryState, SharedLibrary};
use slint::winit_030::winit::window::ResizeDirection;
use slint::winit_030::WinitWindowAccessor;
use slint::{ComponentHandle, Timer, TimerMode};
use std::cell::RefCell;
use std::rc::Rc;
use std::time::{Duration, Instant};
use vta_core::paths::{self, DataDirs};
use vta_core::settings::Settings;
use vta_core::CURRENT_VERSION;

/// The splash stays up at least this long (as in v2).
const SPLASH_MIN: Duration = Duration::from_secs(2);

pub fn edition_name() -> &'static str {
    if cfg!(feature = "icon-ai") {
        "AI Edition"
    } else {
        "Standard"
    }
}

struct App {
    window: AppWindow,
    dirs: DataDirs,
    settings: Rc<RefCell<Settings>>,
    library: SharedLibrary,
    themes: Vec<app_theme::AppTheme>,
}

pub(crate) fn show_toast(app: &AppWindow, text: &str, error: bool) {
    let shell = app.global::<Shell>();
    shell.set_toast(text.into());
    shell.set_toast_error(error);
    let weak = app.as_weak();
    let text = text.to_string();
    Timer::single_shot(Duration::from_secs(5), move || {
        if let Some(app) = weak.upgrade() {
            if app.global::<Shell>().get_toast() == text.as_str() {
                app.global::<Shell>().set_toast("".into());
            }
        }
    });
}

fn wire_shell(app: &App, ctx: Rc<settings_view::SettingsCtx>) {
    let shell = app.window.global::<Shell>();
    shell.set_version(format!("v{CURRENT_VERSION}").into());
    shell.set_native_frame(cfg!(target_os = "macos"));

    let w = app.window.as_weak();
    shell.on_start_drag(move || {
        if let Some(app) = w.upgrade() {
            app.window().with_winit_window(|win| {
                let _ = win.drag_window();
            });
        }
    });
    let w = app.window.as_weak();
    shell.on_start_resize(move |edge| {
        let dir = match edge.as_str() {
            "n" => ResizeDirection::North,
            "s" => ResizeDirection::South,
            "e" => ResizeDirection::East,
            "w" => ResizeDirection::West,
            "ne" => ResizeDirection::NorthEast,
            "nw" => ResizeDirection::NorthWest,
            "se" => ResizeDirection::SouthEast,
            _ => ResizeDirection::SouthWest,
        };
        if let Some(app) = w.upgrade() {
            app.window().with_winit_window(|win| {
                let _ = win.drag_resize_window(dir);
            });
        }
    });
    let w = app.window.as_weak();
    shell.on_minimize(move || {
        if let Some(app) = w.upgrade() {
            app.window().set_minimized(true);
        }
    });
    let w = app.window.as_weak();
    shell.on_toggle_maximize(move || {
        if let Some(app) = w.upgrade() {
            let max = !app.window().is_maximized();
            app.window().set_maximized(max);
            app.global::<Shell>().set_maximized(max);
        }
    });
    shell.on_close(|| {
        let _ = slint::quit_event_loop();
    });
    let w = app.window.as_weak();
    let ctx2 = ctx.clone();
    shell.on_navigate(move |page| {
        let Some(app) = w.upgrade() else { return };
        let current = app.global::<Shell>().get_page();
        if page == current {
            return;
        }
        let ctx = ctx2.clone();
        let go = move |app: &AppWindow| {
            if page == Page::Settings {
                settings_view::load(app, &ctx);
            }
            if page == Page::Community {
                let s = ctx.settings.borrow();
                let extras = ctx.dirs.load_path(&s.str_or("extras_library_path", ""), &ctx.dirs.default_extras_path());
                drop(s);
                community_view::opened(app, extras);
            }
            if page == Page::Creator && !creator_view::is_dirty() && app.global::<Creator>().get_title().is_empty() {
                // Nothing loaded yet: start a new theme.
                creator_view::new_theme(app, &ctx.settings);
                return;
            }
            app.global::<Shell>().set_page(page);
        };
        if current == Page::Creator {
            creator_view::leave(&app, go);
        } else {
            go(&app);
        }
    });
    let w = app.window.as_weak();
    shell.on_new_theme(move || {
        if let Some(app) = w.upgrade() {
            let settings = ctx.settings.clone();
            creator_view::leave(&app, move |app| creator_view::new_theme(app, &settings));
        }
    });
    let w = app.window.as_weak();
    shell.on_about(move || {
        if let Some(app) = w.upgrade() {
            // v2 AboutDialog: name + version, description, author, GitHub link, shortcuts.
            let text = format!(
                "A tool to create, manage, and download themes for the PS Vita.\n\nCreated by: SlyFalco\n\n\
                 Keyboard Shortcuts (Manager)\n\
                 Ctrl+N    New Theme\n\
                 Ctrl+I    Import Theme (.zip)\n\
                 Ctrl+O    Import Theme (Folder)\n\
                 Ctrl+V    Import from PS Vita\n\
                 Ctrl+D    Download Themes\n\
                 Ctrl+E    Edit Selected Theme\n\
                 Ctrl+P    Play Selected BGM\n\
                 Ctrl+X    Extract Selected BGM\n\
                 Ctrl+S    Send Selected to Vita\n\
                 Delete    Delete Selected Theme"
            );
            dialogs::ask(
                &app,
                DialogIcon::Info,
                &format!("VitaTheme Architect v{CURRENT_VERSION} ({})", edition_name()),
                &text,
                "",
                &[("GitHub Project", dialogs::Kind::Secondary), ("OK", dialogs::Kind::Primary)],
                |_, i| {
                    if i == 0 {
                        platform::open_url("https://github.com/Slyfalco/Vita-Theme-Architect");
                    }
                },
            );
        }
    });
}

/// Values from settings.json shown in the shell.
pub(crate) fn apply_settings(window: &AppWindow, settings: &Rc<RefCell<Settings>>, library: &SharedLibrary) {
    let s = settings.borrow();
    let shell = window.global::<Shell>();
    let ip = s.str_or("vita_ip", "");
    let port = match s.values.get("vita_port") {
        Some(serde_json::Value::Number(n)) => n.to_string(),
        Some(serde_json::Value::String(p)) if !p.is_empty() => p.clone(),
        _ => "1337".into(),
    };
    shell.set_vita_address(if ip.is_empty() { "".into() } else { format!("{ip}:{port}").into() });
    let library = library.borrow().root.clone();
    shell.set_library_path(library.to_string_lossy().as_ref().into());
}

/// The theme library folder from settings (v2 `load_paths_from_settings`), creating
/// it and recording defaults in settings.json the same way.
fn library_paths(dirs: &DataDirs, settings: &mut Settings) -> std::path::PathBuf {
    let themes = dirs.load_path(&settings.str_or("theme_library_path", ""), &dirs.default_themes_path());
    let extras = dirs.load_path(&settings.str_or("extras_library_path", ""), &dirs.default_extras_path());
    if !settings.contains("theme_library_path") || !settings.contains("extras_library_path") {
        settings.set("theme_library_path", dirs.store_path(&themes.to_string_lossy()));
        settings.set("extras_library_path", dirs.store_path(&extras.to_string_lossy()));
        let _ = settings.save();
    }
    let _ = std::fs::create_dir_all(&themes);
    let _ = std::fs::create_dir_all(&extras);
    themes
}

/// True for builds made by the packaging tool (it sets VTA_PACKAGED=1 at compile
/// time and embeds the integrity manifest). Plain `cargo` builds are development
/// builds, like v2's runs from source.
const PACKAGED: bool = option_env!("VTA_PACKAGED").is_some();

/// v2 `main`: the install-integrity check (packaged Windows/Linux builds; fails
/// closed) and, on macOS, the "running from Downloads" note.
fn startup_checks() {
    if !PACKAGED {
        return;
    }
    if !cfg!(target_os = "macos") {
        let problems = std::env::current_exe().map(|exe| vta_core::integrity::check(&exe)).unwrap_or_default();
        if !problems.is_empty() {
            let mut list = problems.iter().take(8).cloned().collect::<Vec<_>>().join("\n");
            if problems.len() > 8 {
                list.push_str("\n…");
            }
            rfd::MessageDialog::new()
                .set_level(rfd::MessageLevel::Error)
                .set_title("Installation Problem")
                .set_description(format!("VitaTheme Architect's files look corrupted or modified:\n\n{list}\n\nPlease reinstall or re-download the app."))
                .set_buttons(rfd::MessageButtons::Ok)
                .show();
            std::process::exit(1);
        }
    } else if let (Ok(exe), Some(home)) = (std::env::current_exe(), std::env::var_os("HOME")) {
        if exe.starts_with(std::path::Path::new(&home).join("Downloads")) {
            rfd::MessageDialog::new()
                .set_level(rfd::MessageLevel::Warning)
                .set_title("Move to Applications")
                .set_description(
                    "VitaTheme Architect is running from your Downloads folder.\n\n\
                     macOS will ask for Downloads access on every launch because the app's resources are stored there.\n\n\
                     Move VitaThemeArchitect.app to your Applications folder to avoid this prompt.",
                )
                .show();
        }
    }
}

/// Centre a window on the monitor it opened on (falls back to the primary one).
/// Returns false while the native window doesn't exist yet.
pub(crate) fn center_on_screen(window: &slint::Window) -> bool {
    let target = window.with_winit_window(|win| {
        let monitor = win.current_monitor().or_else(|| win.primary_monitor())?;
        let (pos, size) = (monitor.position(), monitor.size());
        let outer = win.outer_size();
        let x = pos.x + (size.width as i32 - outer.width as i32) / 2;
        let y = pos.y + (size.height as i32 - outer.height as i32) / 2;
        Some((x, y))
    });
    match target {
        Some(Some((x, y))) => {
            window.set_position(slint::PhysicalPosition::new(x, y));
            true
        }
        // Window exists but no monitor info: nothing to centre on.
        Some(None) => true,
        None => false,
    }
}

/// Debug builds only: put the UI in a given state for screenshots
/// (`VTA_DEV_PAGE=library|creator|community|settings`, `VTA_DEV_SELECT=<index>`,
/// `VTA_DEV_STAGE=0|1|2`).
#[cfg(debug_assertions)]
fn dev_preselect(app: &App) {
    if let Ok(page) = std::env::var("VTA_DEV_PAGE") {
        let page = match page.as_str() {
            "creator" => Page::Creator,
            "community" => Page::Community,
            "settings" => Page::Settings,
            _ => Page::Library,
        };
        app.window.global::<Shell>().invoke_navigate(page);
    }
    if let Some(t) = std::env::var("VTA_DEV_TAB").ok().and_then(|v| v.parse().ok()) {
        app.window.global::<SettingsState>().set_tab(t);
    }
    if let Some(i) = std::env::var("VTA_DEV_SELECT").ok().and_then(|v| v.parse().ok()) {
        library_view::select(&app.window, &app.library, i);
    }
    if let Some(i) = std::env::var("VTA_DEV_SUBMIT").ok().and_then(|v| v.parse().ok()) {
        app.window.global::<Library>().invoke_submit(i);
    }
    if let Some(i) = std::env::var("VTA_DEV_EDIT").ok().and_then(|v| v.parse().ok()) {
        app.window.global::<Library>().invoke_edit(i);
    }
    if let Some(t) = std::env::var("VTA_DEV_CTAB").ok().and_then(|v| v.parse().ok()) {
        app.window.global::<Creator>().set_tab(t);
    }
    if let Some(t) = std::env::var("VTA_DEV_STAGE").ok().and_then(|v| v.parse().ok()) {
        app.window.global::<Library>().set_stage_tab(t);
    }
}

#[cfg(not(debug_assertions))]
fn dev_preselect(_: &App) {}

fn main() -> Result<(), slint::PlatformError> {
    crashlog::install();
    startup_checks();
    #[cfg(all(windows, debug_assertions))]
    // SAFETY: plain Win32 call with a constant argument; failure just means no console.
    unsafe {
        windows_sys::Win32::System::Console::AttachConsole(windows_sys::Win32::System::Console::ATTACH_PARENT_PROCESS);
    }
    let splash = Splash::new()?;
    splash.set_version(format!("v{CURRENT_VERSION}").into());
    splash.set_edition(edition_name().into());
    splash.show()?;
    // The native window only exists once the event loop runs, so centre it then.
    // Poll until the native window exists, centre it, then fade the (still
    // transparent) splash in, so it never visibly jumps.
    let sw = splash.as_weak();
    let reveal = Rc::new(Timer::default());
    let reveal2 = reveal.clone();
    let tries = std::cell::Cell::new(0);
    reveal.start(TimerMode::Repeated, Duration::from_millis(10), move || {
        let Some(s) = sw.upgrade() else { return };
        tries.set(tries.get() + 1);
        if center_on_screen(s.window()) || tries.get() > 50 {
            s.set_revealed(true);
            reveal2.stop();
        }
    });
    let started = Instant::now();

    // Startup work runs step by step on timers so the splash keeps animating.
    type Step = Box<dyn FnOnce(&mut Option<App>) -> Option<String>>;
    let steps: Vec<(&str, Step)> = vec![
        ("Locating data folders…", Box::new(|_| None)),
        (
            "Loading settings…",
            Box::new(|slot| {
                let dirs = paths::resolve(CURRENT_VERSION);
                vta_core::migrations::run(&dirs.app_support, CURRENT_VERSION);
                let mut settings = Settings::load(&dirs.settings_file());
                let root = library_paths(&dirs, &mut settings);
                let window = AppWindow::new().ok()?;
                let theme_id = settings.str_or("app_theme", app_theme::DEFAULT_ID);
                let themes = app_theme::available(&dirs.app_support.join("app_themes"));
                let theme = themes.iter().find(|t| t.id == theme_id).or(themes.first()).cloned();
                if let Some(t) = theme {
                    app_theme::apply(&window, &t);
                }
                let library = Rc::new(RefCell::new(LibraryState { root, ..Default::default() }));
                *slot = Some(App { window, dirs, settings: Rc::new(RefCell::new(settings)), library, themes });
                None
            }),
        ),
        (
            "Scanning theme library…",
            Box::new(|slot| {
                let app = slot.as_ref()?;
                let ctx = Rc::new(settings_view::SettingsCtx {
                    dirs: app.dirs.clone(),
                    settings: app.settings.clone(),
                    library: app.library.clone(),
                    themes: app.themes.clone(),
                });
                wire_shell(app, ctx.clone());
                settings_view::wire(&app.window, ctx);
                apply_settings(&app.window, &app.settings, &app.library);
                if app.dirs.is_portable() {
                    updater_view::PORTABLE_DATA.with(|p| *p.borrow_mut() = Some(app.dirs.app_support.clone()));
                }
                library_view::wire(&app.window, &app.library);
                bgm_view::wire(&app.window, &app.library);
                dialogs::wire(&app.window);
                library_actions::wire(&app.window, &app.library, &app.settings);
                creator_view::wire(&app.window, &app.library, &app.settings);
                community_view::wire(&app.window);
                submit_view::wire(&app.window, &app.library);
                library_view::rescan(&app.window, &app.library);
                let problems = app.library.borrow().problems.len();
                (problems > 0).then(|| format!("{problems} theme folder(s) skipped (invalid theme.xml or missing files)"))
            }),
        ),
        ("Ready", Box::new(|_| None)),
    ];

    let app_slot: Rc<RefCell<Option<App>>> = Rc::new(RefCell::new(None));
    let steps = Rc::new(RefCell::new(steps.into_iter()));
    let notices: Rc<RefCell<Vec<String>>> = Rc::default();
    let timer = Rc::new(Timer::default());
    let total = 4.0;
    let done = Rc::new(RefCell::new(0.0f32));
    let splash_weak = splash.as_weak();
    let (slot, t2) = (app_slot.clone(), timer.clone());
    timer.start(TimerMode::Repeated, Duration::from_millis(180), move || {
        let splash = splash_weak.unwrap();
        let next = steps.borrow_mut().next();
        match next {
            Some((label, step)) => {
                splash.set_status(label.into());
                if let Some(note) = step(&mut slot.borrow_mut()) {
                    notices.borrow_mut().push(note);
                }
                *done.borrow_mut() += 1.0;
                splash.set_progress(*done.borrow() / total);
            }
            None => {
                if started.elapsed() < SPLASH_MIN {
                    return;
                }
                t2.stop();
                let slot = slot.borrow();
                let Some(app) = slot.as_ref() else {
                    splash.set_status("Could not start the main window.".into());
                    return;
                };
                let _ = app.window.show();
                // On screens smaller than the preferred size, open maximised.
                let small = app
                    .window
                    .window()
                    .with_winit_window(|win| {
                        win.current_monitor().map(|m| {
                            let size = m.size().to_logical::<f64>(m.scale_factor());
                            size.width < 1700.0 || size.height < 1050.0
                        })
                    })
                    .flatten()
                    .unwrap_or(false);
                let dev_size = cfg!(debug_assertions) && std::env::var("VTA_DEV_SIZE").is_ok();
                if dev_size {
                    if let Some((w, h)) = std::env::var("VTA_DEV_SIZE").ok().and_then(|v| {
                        let (w, h) = v.split_once('x')?;
                        Some((w.parse::<f32>().ok()?, h.parse::<f32>().ok()?))
                    }) {
                        app.window.window().set_size(slint::LogicalSize::new(w, h));
                        app.window.window().set_position(slint::PhysicalPosition::new(0, 0));
                    }
                } else if small {
                    app.window.window().set_maximized(true);
                    app.window.global::<Shell>().set_maximized(true);
                } else {
                    center_on_screen(app.window.window());
                }
                let _ = splash.hide();
                for n in notices.borrow().iter() {
                    show_toast(&app.window, n, true);
                }
                dev_preselect(app);
                // Silent startup update check (official builds, if enabled).
                if settings_view::official_build() && app.settings.borrow().flag("auto_update_check", true) {
                    let w = app.window.as_weak();
                    Timer::single_shot(Duration::from_secs(2), move || {
                        if let Some(app) = w.upgrade() {
                            updater_view::check(&app, false);
                        }
                    });
                }
                // Keep the maximise button icon in sync with OS snaps / double-clicks.
                let w = app.window.as_weak();
                let sync = Timer::default();
                sync.start(TimerMode::Repeated, Duration::from_millis(300), move || {
                    if let Some(app) = w.upgrade() {
                        let max = app.window().is_maximized();
                        if app.global::<Shell>().get_maximized() != max {
                            app.global::<Shell>().set_maximized(max);
                        }
                    }
                });
                std::mem::forget(sync);
            }
        }
    });

    slint::run_event_loop_until_quit()?;
    drop(app_slot);
    Ok(())
}

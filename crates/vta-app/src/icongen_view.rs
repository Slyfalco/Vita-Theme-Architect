//! AI icon generator controller: port of v2 `IconGeneratorDialog` (+
//! `RequirementsDialog`, `ImageSearchDialog`), driving the core ports in
//! `vta_core::iconai` (runtime, estimates, calibration, storage) and
//! `vta_core::iconai_net` (Gemini, artwork search, community sets/art).

use crate::{dialogs, AppWindow, DialogIcon, GenIcon, IconGen, ImageSearch, Page, ReqRow, SearchItem, Shell};
use serde_json::{json, Map, Value};
use slint::{ComponentHandle, Image, Model, ModelRc, SharedString, VecModel};
use std::cell::RefCell;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Instant;
use vta_core::iconai::estimates::{self as est, Estimate};
use vta_core::iconai::IconAi;
use vta_core::iconai_net::{community, gemini, rules, search};

const MIN_WALLPAPERS: usize = 3;
const DEFAULT_THEME_TITLE: &str = "My New Theme";
const COLOR_MODES: [&str; 3] = ["auto", "always", "never"];
const LAYOUTS: [&str; 5] = ["mixed", "examples", "beside", "portrait", "emblem"];

struct Run {
    key: String,
    total: usize,
    done: usize,
    start: Instant,
    first: Option<Instant>,
    last: Option<Instant>,
    startup: f64,
    per: f64,
    cloud: bool,
    job: Map<String, Value>,
    peak_vram: Option<f64>,
    peak_ram: Option<f64>,
    downloaded: bool,
}

#[derive(Default)]
struct State {
    ai: Option<IconAi>,
    title: String,
    has_title: bool,
    wallpapers: Vec<PathBuf>,
    theme_colors: Vec<String>,
    extra_images: Vec<PathBuf>,
    generated: BTreeMap<String, PathBuf>,
    output_dir: Option<PathBuf>,
    used_dirs: Vec<PathBuf>,
    cancel: Option<Arc<AtomicBool>>,
    run: Option<Run>,
    remaining: Option<f64>,
    eta_timer: Option<slint::Timer>,
    community_tmp: Option<PathBuf>,
    community_art: Option<Vec<PathBuf>>,
    models_ready: BTreeMap<(String, bool), bool>,
    ai_size: Option<u64>,
    vram: f64,
    /// (model key, selectable, why not)
    models: Vec<(String, bool, String)>,
    log: String,
    pending_req: Option<Box<dyn FnOnce(&AppWindow)>>,
    examples_busy: bool,
    // Image search
    results: Vec<search::SearchResult>,
    checked: Vec<bool>,
    thumbs: Vec<Option<Image>>,
    page: usize,
    search_cancel: Option<Arc<AtomicBool>>,
    suggest_timer: Option<slint::Timer>,
    search_gen: u64,
}

thread_local! {
    static S: RefCell<State> = RefCell::new(State::default());
}

fn with<R>(f: impl FnOnce(&mut State) -> R) -> R {
    S.with(|s| f(&mut s.borrow_mut()))
}

/// The folder holding `Assets/` and `icon_ai_worker.py`: next to the exe in a
/// packaged build, the repository in development.
fn app_dir() -> PathBuf {
    let exe = std::env::current_exe().unwrap_or_default();
    if vta_core::paths::is_dev_build(&exe) {
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
    } else {
        exe.parent().map(Path::to_path_buf).unwrap_or_default()
    }
}

fn core_ai() -> IconAi {
    with(|s| s.ai.clone()).expect("icon generator not initialised")
}

fn strip_tags(s: &str) -> String {
    let mut out = String::new();
    let mut in_tag = false;
    for c in s.chars() {
        match c {
            '<' => in_tag = true,
            '>' => in_tag = false,
            _ if !in_tag => out.push(c),
            _ => {}
        }
    }
    out.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&")
}

fn log(app: &AppWindow, line: &str) {
    let text = with(|s| {
        s.log.push_str(line);
        s.log.push('\n');
        s.log.clone()
    });
    app.global::<IconGen>().set_log(text.into());
}

fn set_status(app: &AppWindow, text: &str) {
    app.global::<IconGen>().set_status(strip_tags(text).into());
}

fn is_cloud(app: &AppWindow) -> bool {
    app.global::<IconGen>().get_generator() == 1
}

fn current_model(app: &AppWindow) -> String {
    let i = app.global::<IconGen>().get_model() as usize;
    with(|s| s.models.get(i).map(|m| m.0.clone())).unwrap_or_else(|| "sd15".into())
}

fn min_mem() -> bool {
    core_ai().settings().flag("ai_min_memory", false)
}

fn steps(app: &AppWindow) -> u32 {
    app.global::<IconGen>().get_steps().trim().parse::<u32>().unwrap_or(20).clamp(10, 60)
}

fn wallpapers_needed_text(st: &State) -> String {
    if !st.has_title {
        return "Give the theme its name first (Title in the creator, not \"My New Theme\"). \
                The name tells the generator and the artwork search which series and characters the theme is about."
            .into();
    }
    let more = MIN_WALLPAPERS - st.wallpapers.len();
    format!(
        "Add {more} more wallpaper{} first (home pages; a lock screen counts too if the theme has one). \
         Icons are made from the theme's own wallpapers (style, colours and characters), so at least \
         {MIN_WALLPAPERS} are needed; this theme has {}.",
        if more != 1 { "s" } else { "" },
        st.wallpapers.len()
    )
}

/// v2 `refresh_state`.
fn refresh_state(app: &AppWindow) {
    let g = app.global::<IconGen>();
    let busy = with(|s| s.cancel.is_some());
    let ai = core_ai();
    let cloud = is_cloud(app);
    let installed = ai.is_runtime_installed();
    let needs_update = ai.runtime_needs_update();
    g.set_installed(installed);
    g.set_needs_update(needs_update);
    g.set_busy(busy);
    g.set_install_text(if needs_update { "Update AI Components (small download)..." } else { "Install AI Components..." }.into());
    let (enough, has_title) = with(|s| (s.wallpapers.len() >= MIN_WALLPAPERS && s.has_title, s.has_title));
    let has_inputs = enough && ai.templates_dir().is_some() && ai.worker_script().is_file();
    g.set_show_install(!installed && !cloud);
    g.set_show_remove(ai.icon_ai_dir().is_dir() && !cloud);
    let size = with(|s| s.ai_size);
    g.set_remove_text(match size {
        Some(n) if n > 0 => format!("Remove AI Components ({})", est::format_size(n as f64)),
        _ => "Remove AI Components".into(),
    }.into());
    g.set_can_search_web(has_title);
    let settings = ai.settings();
    let key = gemini::google_api_key(&settings);
    let ready = if cloud { !key.is_empty() && enough && ai.templates_dir().is_some() } else { installed && has_inputs };
    g.set_can_generate(ready && !busy);
    let n_icons = rules::ICON_NAMES.len();
    if busy {
        return;
    }
    if cloud {
        let model = gemini::google_model(&settings);
        if key.is_empty() {
            set_status(app, "Add your Google API key in Settings > AI Icons to generate with Gemini (get one at aistudio.google.com/apikey).");
        } else if !enough {
            set_status(app, &with(|s| wallpapers_needed_text(s)));
        } else {
            let (startup, per_icon) = ai.load_timing("google");
            let total = startup + per_icon * n_icons as f64;
            set_status(
                app,
                &format!(
                    "Ready. Gemini ({model}) makes all {n_icons} icons in the cloud in {}: nothing to download, about ${:.2} in total (${:.3} per icon) billed to your Google API key.",
                    est::format_eta(Some(total)),
                    gemini::gemini_price(&model) * n_icons as f64,
                    gemini::gemini_price(&model)
                ),
            );
        }
        return;
    }
    if needs_update {
        set_status(app, "AI components need a small update for this version (PyTorch is kept).");
    } else if !installed {
        set_status(app, "AI components are not installed. This feature is optional. Nothing is downloaded unless you click Install.");
    } else if !enough {
        set_status(app, &with(|s| wallpapers_needed_text(s)));
    } else if !has_inputs {
        set_status(app, "Icon templates or the generator script are missing from this installation.");
    } else {
        let model = current_model(app);
        let poses = g.get_poses();
        let mm = min_mem();
        let mut job = Map::new();
        job.insert("model".into(), json!(model));
        job.insert("pose_variation".into(), json!(poses));
        job.insert("min_memory".into(), json!(mm));
        let (startup, per_icon) = ai.load_timing(&est::timing_key(&job, false));
        let ready_models = with(|s| s.models_ready.get(&(model.clone(), poses)).copied());
        let ready_models = ready_models.unwrap_or_else(|| {
            let r = if est::is_flux2(&model) {
                ai.flux2_present(&model)
            } else {
                vta_core::iconai::worker_files::model_files(ai.runtime_uses_half_precision(), true, poses, &model, false)
                    .iter()
                    .all(|(r, f)| ai.model_cached(r, f))
            };
            with(|s| s.models_ready.insert((model.clone(), poses), r));
            r
        });
        let download = if ready_models { "" } else { " The first run with this model also downloads it (the size is shown before anything downloads)." };
        let step_factor = (steps(app) as f64 / 20.0).max(0.4);
        let total = startup + per_icon * step_factor * n_icons as f64;
        let mut extras = Vec::new();
        if poses {
            extras.push("AI poses");
        }
        if mm {
            extras.push("Minimal VRAM mode");
        }
        let with_note = if extras.is_empty() { String::new() } else { format!(" (with {})", extras.join(", ")) };
        set_status(app, &format!("Ready. Making all {n_icons} icons takes {} on this PC{with_note}.{download}", est::format_eta(Some(total))));
    }
}

fn measure_size(app: &AppWindow) {
    with(|s| s.ai_size = None);
    let ai = core_ai();
    let weak = app.as_weak();
    std::thread::spawn(move || {
        let size = ai.runtime_disk_usage();
        let _ = weak.upgrade_in_event_loop(move |app| {
            with(|s| s.ai_size = Some(size));
            refresh_state(&app);
        });
    });
}

fn update_extra_label(app: &AppWindow) {
    let n = with(|s| s.extra_images.len());
    app.global::<IconGen>().set_extra_label(if n > 0 { format!("{n} image{} added", if n != 1 { "s" } else { "" }) } else { "None (optional)".into() }.into());
}

fn update_examples_label(app: &AppWindow) {
    let ai = core_ai();
    let count = rules::example_set_count(ai.builtin_examples_dir().as_deref(), &ai.examples_dir());
    let busy = with(|s| s.examples_busy);
    let mut text = format!("{count} icon set{} from community themes", if count != 1 { "s" } else { "" });
    if busy {
        text.push_str(" (adding more in the background...)");
    }
    app.global::<IconGen>().set_examples_text(text.into());
}

/// v2 `fetch_examples`: top up community icon sets in the background (release builds).
fn fetch_examples(app: &AppWindow) {
    let ai = core_ai();
    let wanted = rules::examples_to_fetch(ai.builtin_examples_dir().as_deref(), &ai.examples_dir());
    if wanted == 0 || !vta_online::available() || with(|s| s.examples_busy) {
        return;
    }
    with(|s| s.examples_busy = true);
    update_examples_label(app);
    let weak = app.as_weak();
    std::thread::spawn(move || {
        let cancel = AtomicBool::new(false);
        let result = community::download_examples(
            wanted,
            ai.builtin_examples_dir().as_deref(),
            &ai.examples_dir(),
            &ai.templates_dir().unwrap_or_default(),
            &cancel,
            &mut |_, _| {},
        );
        let msg = match result {
            Ok(m) | Err(m) => m,
        };
        let _ = weak.upgrade_in_event_loop(move |app| {
            with(|s| s.examples_busy = false);
            log(&app, &format!("Community icon sets: {msg}"));
            update_examples_label(&app);
        });
    });
}

/// Model choices (v2: MODEL_CHOICES, then FLUX.2 tiers greyed out on a GPU that can't run them).
fn build_model_options(app: &AppWindow) {
    let ai = core_ai();
    let vram = if ai.runtime_uses_half_precision() { vta_core::iconai::hardware::gpu_vram_gb() } else { 0.0 };
    let mm = min_mem();
    let mut labels: Vec<SharedString> = Vec::new();
    let mut models = Vec::new();
    for (label, key, _need) in est::MODEL_CHOICES {
        labels.push((*label).into());
        models.push((key.to_string(), true, String::new()));
    }
    for (label, key, need) in est::FLUX2_CHOICES {
        let req = if mm { est::FLUX2_MIN_MEM_VRAM.iter().find(|(k, _)| k == key).map(|(_, v)| *v).unwrap_or(*need) } else { *need };
        let ok = vram >= req;
        let mut text = format!("{label}{}", if mm { "  — minimal VRAM mode" } else { "" });
        let mut why = String::new();
        if !ok {
            text.push_str("  (needs more GPU memory)");
            why = format!(
                "Needs about {req} GB of GPU memory{}",
                if vram > 0.0 { format!("; this PC has {vram:.0} GB.") } else { " (no CUDA GPU detected).".into() }
            );
            if !mm {
                why.push_str(" Turn on 'Minimal VRAM mode' in Settings to run it on less GPU (offloads to RAM; slower).");
            }
        }
        labels.push(text.into());
        models.push((key.to_string(), ok, why));
    }
    let suggested = est::MODEL_CHOICES.iter().position(|(_, _, need)| vram >= *need).unwrap_or(est::MODEL_CHOICES.len() - 1);
    with(|s| {
        s.vram = vram;
        s.models = models;
    });
    let g = app.global::<IconGen>();
    g.set_model_options(ModelRc::new(VecModel::from(labels)));
    g.set_model(suggested as i32);
    let gpu = vta_core::iconai::hardware::gpu_name();
    g.set_gpu_line(if vram > 0.0 { format!("{gpu} • VRAM: {vram:.0} GB").into() } else if !gpu.is_empty() { gpu.into() } else { "".into() });
}

/// Open the generator for the theme being edited.
pub fn open(app: &AppWindow) {
    let dirs = crate::DATA_DIRS.with(|d| d.borrow().clone());
    let Some(dirs) = dirs else { return };
    let (title, wallpapers, colors) = crate::creator_view::icon_generator_inputs(app);
    let title = title.trim().to_string();
    let has_title = !title.is_empty() && title != DEFAULT_THEME_TITLE;
    let first = with(|s| s.ai.is_none());
    with(|s| {
        if s.ai.is_none() {
            s.ai = Some(IconAi::new(&dirs, &app_dir()));
        }
        s.has_title = has_title;
        s.title = title;
        s.wallpapers = wallpapers;
        s.theme_colors = colors;
    });
    let g = app.global::<IconGen>();
    let ai = core_ai();
    let settings = ai.settings();
    // v2: the saved generator, else Gemini when it has a key and no local runtime.
    let key = gemini::google_api_key(&settings);
    let mut saved = settings.str_or("icon_ai_generator", "");
    if !(saved == "local" || saved == "google") || (saved == "google" && key.is_empty()) {
        saved = if !key.is_empty() && !ai.is_runtime_installed() { "google".into() } else { "local".into() };
    }
    g.set_generator(if saved == "google" { 1 } else { 0 });
    g.set_generator_options(ModelRc::new(VecModel::from(vec![
        SharedString::from(gemini::GENERATOR_CHOICES[0].0),
        if key.is_empty() { format!("{}: needs an API key in Settings", gemini::GENERATOR_CHOICES[1].0).into() } else { gemini::GENERATOR_CHOICES[1].0.into() },
    ])));
    let colors_set = with(|s| !s.theme_colors.is_empty());
    g.set_color_mode_enabled(colors_set);
    if first {
        g.set_seed(rand_seed().to_string().into());
        build_model_options(app);
    }
    let n = with(|s| s.wallpapers.len());
    g.set_wallpapers_text(format!("{n} wallpaper(s) from this theme").into());
    update_extra_label(app);
    update_examples_label(app);
    if with(|s| s.cancel.is_none()) {
        g.set_view(0);
    }
    reset_results(app);
    measure_size(app);
    refresh_state(app);
    fetch_examples(app);
    app.global::<Shell>().set_page(Page::Icongen);
}

fn rand_seed() -> u32 {
    let n = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.subsec_nanos() ^ (d.as_secs() as u32).rotate_left(7)).unwrap_or(4242);
    n % 1_000_000
}

fn reset_results(app: &AppWindow) {
    let rows: Vec<GenIcon> = rules::ICON_NAMES
        .iter()
        .map(|name| {
            let short = name.trim_start_matches("icon_").trim_end_matches(".png").to_string();
            let subject = rules::icon_subject(name, "app");
            let (image, has) = with(|s| s.generated.get(*name).cloned())
                .and_then(|p| Image::load_from_path(&p).ok())
                .map(|i| (i, true))
                .unwrap_or_default();
            GenIcon {
                name: (*name).into(),
                label: short.into(),
                tip: format!(
                    "The {subject} icon. {}",
                    if rules::is_character_icon(name) { "It features one of the theme's characters." } else { "Symbol and theme art only, no character." }
                )
                .into(),
                image,
                has,
            }
        })
        .collect();
    app.global::<IconGen>().set_icons(ModelRc::new(VecModel::from(rows)));
}

// ---------------------------------------------------------------- tasks

fn start_task(app: &AppWindow) -> Arc<AtomicBool> {
    let cancel = Arc::new(AtomicBool::new(false));
    with(|s| s.cancel = Some(cancel.clone()));
    app.global::<IconGen>().set_busy(true);
    refresh_state(app);
    cancel
}

/// v2 `on_task_finished`.
fn task_finished(app: &AppWindow, ok: bool, message: &str) {
    let g = app.global::<IconGen>();
    let run = with(|s| {
        s.cancel = None;
        s.models_ready.clear();
        s.eta_timer = None;
        s.run.take()
    });
    measure_size(app);
    let ai = core_ai();
    if let Some(run) = &run {
        if ok && run.done > 0 {
            if let (Some(first), Some(last)) = (run.first, run.last) {
                let startup = first.duration_since(run.start).as_secs_f64();
                let per_icon = last.duration_since(first).as_secs_f64() / run.done as f64;
                if !run.downloaded {
                    ai.save_timing(&run.key, startup, per_icon);
                }
                ai.record_calibration(&run.key, &run.job, if run.downloaded { None } else { Some(startup) }, per_icon, run.done, run.peak_vram, run.peak_ram, run.cloud);
            }
        }
    }
    let has_generated = with(|s| !s.generated.is_empty());
    g.set_view(if run.is_some() && has_generated { 1 } else { 0 });
    g.set_busy(false);
    refresh_state(app);
    set_status(app, &if ok { message.to_string() } else { format!("Failed: {message}") });
    g.set_can_apply(has_generated);
    if ok {
        crate::notify_done(app);
    } else {
        g.set_show_log(true);
    }
}

fn show_progress(app: &AppWindow, title: &str, job: Option<&Map<String, Value>>, cloud: bool) {
    let g = app.global::<IconGen>();
    g.set_view(2);
    g.set_prog_title(title.into());
    g.set_prog_phase("Loading up...".into());
    g.set_prog_value(-1.0);
    g.set_prog_count("".into());
    g.set_prog_has_last(false);
    g.set_prog_last_name("".into());
    let remaining = job.map(|job| {
        let key = est::timing_key(job, cloud);
        let (startup, per) = core_ai().load_timing(&key);
        let total = job.get("icons").and_then(|i| i.as_array()).map(|a| a.len()).unwrap_or(0);
        with(|s| {
            s.run = Some(Run {
                key,
                total,
                done: 0,
                start: Instant::now(),
                first: None,
                last: None,
                startup,
                per,
                cloud,
                job: job.clone(),
                peak_vram: None,
                peak_ram: None,
                downloaded: false,
            })
        });
        g.set_prog_count(format!("Icon 0 of {total} finished").into());
        startup + per * total as f64
    });
    with(|s| s.remaining = remaining);
    show_eta(app);
    let t = slint::Timer::default();
    let weak = app.as_weak();
    t.start(slint::TimerMode::Repeated, std::time::Duration::from_secs(1), move || {
        if let Some(app) = weak.upgrade() {
            with(|s| {
                if let Some(r) = s.remaining.as_mut() {
                    *r = (*r - 1.0).max(0.0);
                }
            });
            show_eta(&app);
        }
    });
    with(|s| s.eta_timer = Some(t));
}

fn show_eta(app: &AppWindow) {
    let r = with(|s| s.remaining);
    app.global::<IconGen>().set_prog_eta(
        match r {
            None => String::new(),
            Some(r) if r <= 1.0 => "Almost done...".into(),
            Some(r) => est::format_countdown(r),
        }
        .into(),
    );
}

/// v2 `_reestimate`: time left from this run's own pace.
fn reestimate() {
    with(|s| {
        let Some(run) = &s.run else { return };
        let now = Instant::now();
        let left = run.total.saturating_sub(run.done);
        let per_icon = match run.first {
            Some(first) if run.done > 0 => {
                let measured = now.duration_since(first).as_secs_f64() / run.done as f64;
                (run.per * 2.0 + measured * run.done as f64) / (2.0 + run.done as f64)
            }
            _ => run.per,
        };
        let since_last = run.last.map(|l| now.duration_since(l).as_secs_f64()).unwrap_or(0.0);
        s.remaining = Some(if left > 0 { (per_icon * left as f64 - per_icon.min(since_last)).max(3.0) } else { 0.0 });
    });
}

/// v2 `on_generation_event` (worker `@@VTA` events and Gemini events alike).
fn on_event(app: &AppWindow, data: &Value) {
    let g = app.global::<IconGen>();
    let event = data.get("event").and_then(|e| e.as_str()).unwrap_or("");
    let s_ = |k: &str| data.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    let n_ = |k: &str| data.get(k).and_then(|v| v.as_f64()).unwrap_or(0.0);
    match event {
        "status" => {
            set_status(app, &s_("message"));
            if with(|s| s.run.as_ref().is_none_or(|r| r.first.is_none())) {
                g.set_prog_phase(s_("message").into());
            }
        }
        "progress" => {
            let first_icon = with(|s| {
                let Some(run) = s.run.as_mut() else { return false };
                if run.first.is_none() {
                    run.first = Some(Instant::now());
                    run.last = run.first;
                    true
                } else {
                    false
                }
            });
            if first_icon {
                reestimate();
            }
            let (done, total) = with(|s| s.run.as_ref().map(|r| (r.done, r.total))).unwrap_or((n_("index") as usize, n_("total") as usize));
            g.set_prog_value(done as f32 / total.max(1) as f32);
            let name = s_("name");
            let short = name.trim_start_matches("icon_").trim_end_matches(".png");
            g.set_prog_phase(format!("Making the {short} icon...").into());
            set_status(app, &format!("Generating {name} ({}/{})...", n_("index") as usize + 1, n_("total") as usize));
        }
        "icon" => {
            let name = s_("name");
            let path = PathBuf::from(s_("path"));
            with(|s| s.generated.insert(name.clone(), path.clone()));
            if let Ok(img) = Image::load_from_path(&path) {
                let icons = g.get_icons();
                for i in 0..icons.row_count() {
                    if let Some(mut row) = icons.row_data(i) {
                        if row.name == name.as_str() {
                            row.image = img.clone();
                            row.has = true;
                            icons.set_row_data(i, row);
                        }
                    }
                }
                g.set_prog_last(img);
                g.set_prog_has_last(true);
            }
            g.set_prog_last_name(format!("Just finished: {}", name.trim_start_matches("icon_").trim_end_matches(".png")).into());
            let counts = with(|s| {
                let run = s.run.as_mut()?;
                run.done += 1;
                run.last = Some(Instant::now());
                Some((run.done, run.total))
            });
            if let Some((done, total)) = counts {
                g.set_prog_value(done as f32 / total.max(1) as f32);
                g.set_prog_count(format!("Icon {done} of {total} finished").into());
                reestimate();
                show_eta(app);
            }
            g.set_can_apply(true);
        }
        "peak" => with(|s| {
            if let Some(run) = s.run.as_mut() {
                run.peak_vram = data.get("vram_gb").and_then(|v| v.as_f64());
                run.peak_ram = data.get("ram_gb").and_then(|v| v.as_f64());
            }
        }),
        "skipped" => {
            log(app, &format!("Skipped {}: {}", s_("name"), data.get("reason").map(|r| r.to_string().trim_matches('"').to_string()).unwrap_or_default()));
            with(|s| {
                if let Some(run) = s.run.as_mut() {
                    run.total = run.done.max(run.total.saturating_sub(1));
                }
            });
        }
        "download" => {
            let (done, total) = (n_("done"), n_("total"));
            with(|s| {
                if let Some(run) = s.run.as_mut() {
                    run.downloaded = true;
                }
            });
            g.set_prog_value((done / total.max(1.0)) as f32);
            let speed = data.get("speed").and_then(|v| v.as_f64()).filter(|v| *v > 0.0);
            g.set_prog_phase(est::download_text("Downloading AI models (one time only)", done, total, speed).into());
            if let Some(eta) = data.get("eta").and_then(|v| v.as_f64()) {
                with(|s| {
                    if let Some(run) = &s.run {
                        s.remaining = Some(eta + run.startup + run.per * run.total as f64);
                    }
                });
                show_eta(app);
            }
        }
        _ => {}
    }
}

// ---------------------------------------------------------------- install / remove

fn estimate_text(e: Option<&Estimate>) -> String {
    let Some(e) = e else { return "The download size and time could not be estimated right now.".into() };
    let mut lines = vec![format!("Download: about {}", est::format_size(e.total))];
    if e.parts.len() > 1 {
        lines.extend(e.parts.iter().map(|(n, s)| format!("  • {n}: {}", est::format_size(*s))));
    }
    if e.disk_use > 0.0 {
        lines.push(format!("Space used on disk once installed: about {}", est::format_size(e.disk_use)));
    }
    if let Some(speed) = e.speed {
        let dl = e.download_eta.or(e.eta);
        lines.push(format!(
            "  • Downloading: {} (download server speed ~{}/s{}",
            est::format_eta(dl),
            est::format_size(speed),
            if e.disk > 0.0 { format!(", disk ~{}/s)", est::format_size(e.disk)) } else { ")".into() }
        ));
        if e.install_eta.is_some() {
            lines.push(format!("  • Unpacking and installing: {}", est::format_eta(e.install_eta)));
        }
        if e.load_eta.is_some() {
            lines.push(format!("  • First model load from disk: {}", est::format_eta(e.load_eta)));
        }
        lines.push(format!("Total: {}.", est::format_eta(e.eta)));
    }
    lines.join("\n")
}

/// v2 `RequirementsDialog`.
fn ask_requirements(app: &AppWindow, title: &str, intro: &str, e: Option<&Estimate>, rows: Vec<est::Requirement>, on_ok: impl FnOnce(&AppWindow) + 'static) {
    let g = app.global::<IconGen>();
    let blocked = rows.iter().any(|r| r.status == "fail");
    let warned = rows.iter().any(|r| r.status == "warn");
    g.set_req_title(title.into());
    g.set_req_intro(intro.into());
    g.set_req_estimate(estimate_text(e).into());
    let rows: Vec<ReqRow> = rows
        .into_iter()
        .map(|r| ReqRow { what: r.what.into(), needed: r.needed.into(), have: r.this_pc.into(), state: match r.status { "ok" => 0, "warn" => 1, _ => 2 } })
        .collect();
    g.set_req_rows(ModelRc::new(VecModel::from(rows)));
    g.set_req_blocked(blocked);
    g.set_req_note(
        if blocked {
            "This PC doesn't meet the requirements above, so nothing will be downloaded."
        } else if warned {
            "It will work, but more slowly than on a recommended PC."
        } else {
            "This PC meets all the requirements."
        }
        .into(),
    );
    g.set_req_saved_in(core_ai().icon_ai_dir().display().to_string().into());
    with(|s| s.pending_req = Some(Box::new(on_ok)));
    g.set_req_open(true);
}

/// Run an estimate in the background, then `then(estimate)` (v2 `_with_estimate`).
fn with_estimate<T: Send + 'static>(app: &AppWindow, status: &str, work: impl FnOnce(IconAi) -> T + Send + 'static, then: impl FnOnce(&AppWindow, T) + Send + 'static) {
    set_status(app, status);
    let g = app.global::<IconGen>();
    g.set_can_generate(false);
    g.set_show_install(false);
    let ai = core_ai();
    let weak = app.as_weak();
    std::thread::spawn(move || {
        let result = work(ai);
        let _ = weak.upgrade_in_event_loop(move |app| {
            refresh_state(&app);
            then(&app, result);
        });
    });
}

fn install_runtime(app: &AppWindow) {
    with_estimate(app, "Checking download size and your connection speed...", |ai| ai.estimate_install().ok(), |app, e| {
        let ai = core_ai();
        let rows = ai.check_requirements("install", e.as_ref(), &current_model(app), app.global::<IconGen>().get_poses(), min_mem());
        ask_requirements(app, "Install AI Components", "Install the AI components to make icons on this PC?", e.as_ref(), rows, |app| {
            let cancel = start_task(app);
            show_progress(app, "Installing the AI components", None, false);
            let ai = core_ai();
            let weak = app.as_weak();
            std::thread::spawn(move || {
                let w2 = weak.clone();
                let (ok, msg) = ai.install_runtime(&cancel, move |ev| {
                    let _ = w2.upgrade_in_event_loop(move |app| match ev {
                        vta_core::iconai::runtime::InstallEvent::Log(l) => log(&app, &l),
                        vta_core::iconai::runtime::InstallEvent::Progress(pct, text) => {
                            set_status(&app, &text);
                            let g = app.global::<IconGen>();
                            g.set_prog_phase(text.into());
                            g.set_prog_value(if pct < 0 { -1.0 } else { pct as f32 / 100.0 });
                        }
                    });
                });
                let _ = weak.upgrade_in_event_loop(move |app| task_finished(&app, ok, &msg));
            });
        });
    });
}

fn remove_runtime(app: &AppWindow) {
    let ai = core_ai();
    let size = with(|s| s.ai_size).unwrap_or_else(|| ai.runtime_disk_usage());
    dialogs::confirm(
        app,
        DialogIcon::Warning,
        "Remove AI Components",
        &format!("Delete the AI runtime and downloaded models ({})?\n\n{}", est::format_size(size as f64), ai.icon_ai_dir().display()),
        "Delete",
        true,
        |app| {
            core_ai().remove_runtime();
            with(|s| s.models_ready.clear());
            measure_size(app);
            refresh_state(app);
        },
    );
}

// ---------------------------------------------------------------- generate

fn remember_generator(app: &AppWindow) {
    let ai = core_ai();
    let mut settings = ai.settings();
    settings.set("icon_ai_generator", if is_cloud(app) { "google" } else { "local" });
    let _ = settings.save();
}

fn discard_output() {
    with(|s| {
        if let Some(d) = s.output_dir.take() {
            if !s.used_dirs.contains(&d) {
                let _ = std::fs::remove_dir_all(&d);
            }
        }
    });
}

fn generate(app: &AppWindow) {
    let g = app.global::<IconGen>();
    if !g.get_lock_seed() {
        g.set_seed(rand_seed().to_string().into());
    }
    discard_output();
    let output = std::env::temp_dir().join(format!("vta_icons_{}_{}", std::process::id(), rand_seed()));
    let _ = std::fs::create_dir_all(&output);
    with(|s| {
        s.generated.clear();
        s.output_dir = Some(output.clone());
    });
    g.set_can_apply(false);
    let ai = core_ai();
    let (title, colors, wallpapers, extras) = with(|s| (s.title.clone(), s.theme_colors.clone(), s.wallpapers.clone(), s.extra_images.clone()));
    let examples = rules::job_examples_dirs(ai.builtin_examples_dir().as_deref(), &ai.examples_dir());
    let seed: i64 = g.get_seed().trim().parse().unwrap_or(0);
    let mut job = Map::new();
    job.insert("title".into(), json!(title));
    job.insert("theme_colors".into(), json!(colors));
    job.insert("theme_color_mode".into(), json!(COLOR_MODES[g.get_color_mode().clamp(0, 2) as usize]));
    job.insert("templates_dir".into(), json!(ai.templates_dir().map(|p| p.display().to_string())));
    job.insert("wallpapers".into(), json!(wallpapers.iter().map(|p| p.display().to_string()).collect::<Vec<_>>()));
    job.insert("icons".into(), json!(rules::ICON_NAMES));
    job.insert("output_dir".into(), json!(output.display().to_string()));
    job.insert("creativity".into(), json!(g.get_creativity().round() / 100.0));
    job.insert("style_scale".into(), json!(g.get_style().round() / 100.0));
    job.insert("consistency".into(), json!(g.get_consistency().round() / 100.0));
    job.insert("clarity".into(), json!(g.get_clarity().round() / 100.0));
    job.insert("layout_style".into(), json!(LAYOUTS[g.get_layout_style().clamp(0, 4) as usize]));
    job.insert("extra_images".into(), json!(extras.iter().map(|p| p.display().to_string()).collect::<Vec<_>>()));
    job.insert("pose_variation".into(), json!(g.get_poses()));
    job.insert("model".into(), json!(current_model(app)));
    job.insert("redraw".into(), json!(g.get_redraw()));
    job.insert("examples_dirs".into(), json!(examples.iter().map(|p| p.display().to_string()).collect::<Vec<_>>()));
    job.insert("cache_dir".into(), json!(ai.icon_ai_dir().display().to_string()));
    job.insert("steps".into(), json!(steps(app)));
    job.insert("seed".into(), json!(seed));
    for line in ai.apply_model_settings(&mut job) {
        log(app, &line);
    }
    remember_generator(app);
    let cached_art = with(|s| s.community_art.clone());
    if let Some(art) = cached_art {
        job.insert("auto_extra_images".into(), json!(art.iter().map(|p| p.display().to_string()).collect::<Vec<_>>()));
        return continue_generate(app, job);
    }
    if vta_online::available() && job["layout_style"] != "emblem" && !is_cloud(app) {
        set_status(app, "Looking for more art of this theme's characters in community themes...");
        g.set_can_generate(false);
        let tmp = std::env::temp_dir().join(format!("vta_community_art_{}", rand_seed()));
        let _ = std::fs::create_dir_all(&tmp);
        with(|s| s.community_tmp = Some(tmp.clone()));
        let weak = app.as_weak();
        let title = job["title"].as_str().unwrap_or("").to_string();
        std::thread::spawn(move || {
            let mut lines = Vec::new();
            let paths = community::community_theme_art(&title, &tmp, &|| false, &mut |l| lines.push(l));
            let _ = weak.upgrade_in_event_loop(move |app| {
                for l in lines {
                    log(&app, &l);
                }
                with(|s| s.community_art = Some(paths.clone()));
                job.insert("auto_extra_images".into(), json!(paths.iter().map(|p| p.display().to_string()).collect::<Vec<_>>()));
                continue_generate(&app, job);
            });
        });
        return;
    }
    continue_generate(app, job);
}

fn continue_generate(app: &AppWindow, job: Map<String, Value>) {
    refresh_state(app);
    if is_cloud(app) {
        return confirm_gemini(app, job);
    }
    let segmenter = job["layout_style"] != "emblem";
    let poses = job["pose_variation"].as_bool().unwrap_or(false) && segmenter;
    let model = job["model"].as_str().unwrap_or("sd15").to_string();
    let skip_base = job.get("custom_base_model").is_some();
    with_estimate(
        app,
        "Checking whether AI models need downloading...",
        move |ai| ai.estimate_models(segmenter, poses, &model, skip_base),
        move |app, e| match e {
            Ok(e) => confirm_models(app, e, job),
            Err(err) => set_status(app, &format!("Failed: {err}")),
        },
    );
}

fn confirm_gemini(app: &AppWindow, job: Map<String, Value>) {
    let ai = core_ai();
    let settings = ai.settings();
    let model = gemini::google_model(&settings);
    let count = rules::ICON_NAMES.len();
    dialogs::confirm(app, DialogIcon::Question, "Generate with Google Gemini", &gemini::confirm_text(&model, count), "Yes", false, move |app| {
        reset_results(app);
        show_progress(app, "Making your icons with Gemini", Some(&job), true);
        let cancel = start_task(app);
        let key = gemini::google_api_key(&ai.settings());
        let gjob = gemini::GeminiJob {
            title: job["title"].as_str().unwrap_or("").into(),
            theme_colors: job["theme_colors"].as_array().map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect()).unwrap_or_default(),
            theme_color_mode: job["theme_color_mode"].as_str().unwrap_or("auto").into(),
            templates_dir: ai.templates_dir().unwrap_or_default(),
            wallpapers: with(|s| s.wallpapers.clone()),
            icons: rules::ICON_NAMES.iter().map(|s| s.to_string()).collect(),
            output_dir: with(|s| s.output_dir.clone()).unwrap_or_default(),
            layout_style: job["layout_style"].as_str().unwrap_or("mixed").into(),
            extra_images: with(|s| s.extra_images.clone()),
            examples_dirs: rules::job_examples_dirs(ai.builtin_examples_dir().as_deref(), &ai.examples_dir()),
            seed: job["seed"].as_i64().unwrap_or(0),
        };
        let weak = app.as_weak();
        std::thread::spawn(move || {
            gemini::run_gemini_job(&gjob, &key, &model, &cancel, &mut |ev| {
                let _ = weak.upgrade_in_event_loop(move |app| match ev {
                    gemini::GeminiEvent::Status { message } => on_event(&app, &json!({"event": "status", "message": message})),
                    gemini::GeminiEvent::Progress { name, index, total } => on_event(&app, &json!({"event": "progress", "name": name, "index": index, "total": total})),
                    gemini::GeminiEvent::Skipped { name, index, total, reason } => on_event(&app, &json!({"event": "skipped", "name": name, "index": index, "total": total, "reason": reason})),
                    gemini::GeminiEvent::Icon { name, index, total, path } => on_event(&app, &json!({"event": "icon", "name": name, "index": index, "total": total, "path": path.display().to_string()})),
                    gemini::GeminiEvent::Log(l) => log(&app, &l),
                    gemini::GeminiEvent::Finished { ok, message } => task_finished(&app, ok, &message),
                });
            });
        });
    });
}

fn flux2_license_ok(app: &AppWindow, then: impl FnOnce(&AppWindow) + 'static) {
    let ai = core_ai();
    if ai.settings().flag("flux2_license_accepted", false) {
        return then(app);
    }
    dialogs::confirm(
        app,
        DialogIcon::Question,
        "FLUX.2 Licence",
        &format!(
            "FLUX.2 [dev] is downloaded from Black Forest Labs under the FLUX.2 Non-Commercial Licence. The model weights are for personal, non-commercial use; the icons you make with it are yours to use.\n\nFull licence:\n{}\n\nDownload and use FLUX.2?",
            est::FLUX2_LICENSE_URL
        ),
        "Yes",
        false,
        move |app| {
            let mut s = ai.settings();
            s.set("flux2_license_accepted", true);
            let _ = s.save();
            then(app);
        },
    );
}

fn confirm_models(app: &AppWindow, e: Option<Estimate>, job: Map<String, Value>) {
    let model = job["model"].as_str().unwrap_or("sd15").to_string();
    if est::is_flux2(&model) {
        let (e2, j2) = (e.clone(), job.clone());
        return flux2_license_ok(app, move |app| {
            let ai = core_ai();
            if model == "flux2_full" && !ai.flux2_present("flux2_full") && ai.settings().str_or("hf_token", "").trim().is_empty() {
                return dialogs::message(
                    app,
                    DialogIcon::Info,
                    "Hugging Face Token Needed",
                    "The full FLUX.2 (Ultra+) model is a gated download. To use it:\n\n1. Create a free Hugging Face access token (huggingface.co/settings/tokens).\n2. Accept the FLUX.2 licence on its model page (huggingface.co/black-forest-labs/FLUX.2-dev).\n3. Paste the token in Settings > AI Icons (Hugging Face token).\n\nThe 4-bit FLUX.2 tier needs none of this.",
                );
            }
            if model == "flux2" && !ai.package_installed("bitsandbytes") {
                let cancel = dialogs::progress_start(app, "Installing a FLUX.2 component (bitsandbytes)…", false);
                let _ = cancel;
                let weak = app.as_weak();
                std::thread::spawn(move || {
                    let ok = ai.install_bitsandbytes();
                    let _ = weak.upgrade_in_event_loop(move |app| {
                        dialogs::progress_close(&app);
                        if ok {
                            start_generation(&app, e2, j2);
                        } else {
                            dialogs::message(&app, DialogIcon::Warning, "FLUX.2", "Could not install the FLUX.2 component (bitsandbytes).");
                        }
                    });
                });
                return;
            }
            start_generation(app, e2, j2);
        });
    }
    start_generation(app, e, job);
}

fn start_generation(app: &AppWindow, e: Option<Estimate>, job: Map<String, Value>) {
    let go = move |app: &AppWindow| {
        reset_results(app);
        show_progress(app, "Making your icons", Some(&job), false);
        let cancel = start_task(app);
        let ai = core_ai();
        let job_path = std::env::temp_dir().join(format!("vta_icon_job_{}.json", rand_seed()));
        let _ = std::fs::write(&job_path, serde_json::to_vec(&job).unwrap_or_default());
        let weak = app.as_weak();
        std::thread::spawn(move || {
            let mut error: Option<String> = None;
            let args: Vec<std::ffi::OsString> = vec!["-I".into(), "-u".into(), "-X".into(), "utf8".into(), ai.worker_script().into(), job_path.clone().into()];
            let w_line = weak.clone();
            let w_log = weak.clone();
            let code = vta_core::iconai::proc::run_process(
                &ai.runtime_python(),
                &args,
                &ai.child_env(),
                &cancel,
                |line| {
                    let Some(rest) = line.strip_prefix(vta_core::iconai::worker_files::EVENT_PREFIX) else { return false };
                    let Ok(data) = serde_json::from_str::<Value>(rest) else { return false };
                    if data.get("event").and_then(|e| e.as_str()) == Some("error") {
                        error = data.get("message").and_then(|m| m.as_str()).map(String::from);
                    }
                    let _ = w_line.upgrade_in_event_loop(move |app| on_event(&app, &data));
                    true
                },
                |line| {
                    let _ = w_log.upgrade_in_event_loop(move |app| log(&app, &line));
                },
            );
            let _ = std::fs::remove_file(&job_path);
            let (ok, msg) = if cancel.load(Ordering::SeqCst) {
                (false, "Generation cancelled.".to_string())
            } else {
                match code {
                    Ok(0) => (true, "Done.".to_string()),
                    Ok(c) => (false, error.unwrap_or_else(|| format!("Generator exited with code {c}. See the log for details."))),
                    Err(e) => (false, format!("OSError: {e}")),
                }
            };
            let _ = weak.upgrade_in_event_loop(move |app| task_finished(&app, ok, &msg));
        });
    };
    match e {
        Some(e) => {
            let job_model = current_model(app);
            let rows = core_ai().check_requirements("models", Some(&e), &job_model, app.global::<IconGen>().get_poses(), min_mem());
            ask_requirements(app, "Download AI Models", "The AI models need downloading first (one time only).", Some(&e), rows, go);
        }
        None => go(app),
    }
}

// ---------------------------------------------------------------- image search

fn search_rows(app: &AppWindow) {
    let rows: Vec<SearchItem> = with(|s| {
        s.results
            .iter()
            .enumerate()
            .map(|(i, r)| {
                let thumb = s.thumbs.get(i).cloned().flatten();
                SearchItem {
                    has_thumb: thumb.is_some(),
                    thumb: thumb.unwrap_or_default(),
                    checked: s.checked.get(i).copied().unwrap_or(false),
                    tip: r.tags.chars().take(300).collect::<String>().into(),
                }
            })
            .collect()
    });
    app.global::<ImageSearch>().set_results(ModelRc::new(VecModel::from(rows)));
}

fn run_search(app: &AppWindow) {
    let isg = app.global::<ImageSearch>();
    let terms = isg.get_query().trim().to_string();
    if terms.is_empty() {
        return;
    }
    isg.set_status("Searching...".into());
    let (solo, plain, page) = (isg.get_solo(), isg.get_plain(), with(|s| s.page));
    let source = search::IMAGE_SOURCES[isg.get_source().clamp(0, 1) as usize].1.to_string();
    let generation = with(|s| {
        s.search_gen += 1;
        s.search_gen
    });
    let weak = app.as_weak();
    std::thread::spawn(move || {
        let found = search::search(&source, &terms, page, solo, plain);
        let _ = weak.upgrade_in_event_loop(move |app| {
            if with(|s| s.search_gen) != generation {
                return;
            }
            let isg = app.global::<ImageSearch>();
            let found = match found {
                Ok(f) => f,
                Err(e) => return isg.set_status(format!("Search failed: {e}").into()),
            };
            let start = with(|s| {
                let start = s.results.len();
                s.results.extend(found.iter().cloned());
                s.checked.resize(s.results.len(), false);
                s.thumbs.resize(s.results.len(), None);
                start
            });
            isg.set_more_enabled(found.len() == search::SEARCH_PAGE_SIZE);
            let total = with(|s| s.results.len());
            isg.set_status(if total > 0 { format!("{total} pictures. Click to tick the ones to use.") } else { "No pictures found. Try different words, or untick the filters.".into() }.into());
            search_rows(&app);
            let urls: Vec<String> = found.iter().map(|r| r.thumb.clone()).collect();
            let cancel = Arc::new(AtomicBool::new(false));
            with(|s| s.search_cancel = Some(cancel.clone()));
            let weak = app.as_weak();
            std::thread::spawn(move || {
                search::load_thumbnails(&urls, &cancel, &mut |i, bytes| {
                    let Some(buf) = image::load_from_memory(&bytes).ok().map(|img| {
                        let rgba = img.thumbnail(140, 140).to_rgba8();
                        let (w, h) = rgba.dimensions();
                        slint::SharedPixelBuffer::<slint::Rgba8Pixel>::clone_from_slice(rgba.as_raw(), w, h)
                    }) else {
                        return;
                    };
                    let _ = weak.upgrade_in_event_loop(move |app| {
                        if with(|s| s.search_gen) != generation {
                            return;
                        }
                        with(|s| {
                            if let Some(slot) = s.thumbs.get_mut(start + i) {
                                *slot = Some(Image::from_rgba8(buf));
                            }
                        });
                        search_rows(&app);
                    });
                });
            });
        });
    });
}

fn new_search(app: &AppWindow) {
    with(|s| {
        if let Some(c) = s.search_cancel.take() {
            c.store(true, Ordering::SeqCst);
        }
        s.page = 0;
        s.results.clear();
        s.checked.clear();
        s.thumbs.clear();
    });
    search_rows(app);
    run_search(app);
}

fn close_search(app: &AppWindow) {
    with(|s| {
        if let Some(c) = s.search_cancel.take() {
            c.store(true, Ordering::SeqCst);
        }
        s.search_gen += 1;
    });
    app.global::<ImageSearch>().set_open(false);
}

// ---------------------------------------------------------------- wiring

pub fn wire(app: &AppWindow) {
    let g = app.global::<IconGen>();
    let a = app.as_weak();
    g.on_options_changed(move || refresh_state(&a.unwrap()));
    let a = app.as_weak();
    g.on_generator_changed(move |_| {
        let app = a.unwrap();
        if is_cloud(&app) && gemini::google_api_key(&core_ai().settings()).is_empty() {
            app.global::<IconGen>().set_generator(0);
            dialogs::ask(
                &app,
                DialogIcon::Warning,
                "Google API Key Needed",
                "Generating with Google Gemini needs your own Google API key.\n\nAdd it in Settings > AI Icons (get one at aistudio.google.com/apikey). Gemini image models are paid: each icon is billed to your key.",
                "",
                &[("Cancel", dialogs::Kind::Secondary), ("Open Settings", dialogs::Kind::Primary)],
                |app, i| {
                    if i == 1 {
                        app.global::<Shell>().invoke_navigate(Page::Settings);
                        app.global::<crate::SettingsState>().set_tab(2);
                    }
                },
            );
        }
        refresh_state(&app);
    });
    let a = app.as_weak();
    g.on_install(move || install_runtime(&a.unwrap()));
    let a = app.as_weak();
    g.on_remove(move || remove_runtime(&a.unwrap()));
    let a = app.as_weak();
    g.on_reseed(move || a.unwrap().global::<IconGen>().set_seed(rand_seed().to_string().into()));
    let a = app.as_weak();
    g.on_add_images(move || {
        let app = a.unwrap();
        if let Some(paths) = rfd::FileDialog::new().set_title("Add Character Artwork").add_filter("Images", &["png", "jpg", "jpeg", "webp"]).pick_files() {
            with(|s| {
                for p in paths {
                    if !s.extra_images.contains(&p) {
                        s.extra_images.push(p);
                    }
                }
            });
            update_extra_label(&app);
        }
    });
    let a = app.as_weak();
    g.on_clear_images(move || {
        let app = a.unwrap();
        with(|s| s.extra_images.clear());
        update_extra_label(&app);
    });
    let a = app.as_weak();
    g.on_search_web(move || {
        let app = a.unwrap();
        let isg = app.global::<ImageSearch>();
        let title = with(|s| s.title.clone());
        isg.set_query(title.clone().into());
        isg.set_adding(false);
        isg.set_status("".into());
        isg.set_suggestions(ModelRc::new(VecModel::from(Vec::<SharedString>::new())));
        isg.set_open(true);
        new_search(&app);
    });
    let a = app.as_weak();
    g.on_generate(move || generate(&a.unwrap()));
    let a = app.as_weak();
    g.on_cancel(move || {
        let app = a.unwrap();
        if let Some(c) = with(|s| s.cancel.clone()) {
            set_status(&app, "Cancelling...");
            c.store(true, Ordering::SeqCst);
        }
    });
    let a = app.as_weak();
    g.on_close(move || {
        let app = a.unwrap();
        if let Some(c) = with(|s| s.cancel.clone()) {
            c.store(true, Ordering::SeqCst);
        }
        discard_output();
        if let Some(tmp) = with(|s| {
            s.community_art = None;
            s.community_tmp.take()
        }) {
            let _ = std::fs::remove_dir_all(tmp);
        }
        app.global::<Shell>().set_page(Page::Creator);
    });
    let a = app.as_weak();
    g.on_apply(move || {
        let app = a.unwrap();
        let (icons, out) = with(|s| (s.generated.clone(), s.output_dir.clone()));
        crate::creator_view::use_generated_icons(&app, &icons);
        // The creator now uses this set; earlier sets no longer referenced are deleted.
        with(|s| {
            if let Some(o) = out {
                s.used_dirs.push(o);
            }
            s.output_dir = None;
            let used = crate::creator_view::used_sources();
            s.used_dirs.retain(|d| {
                let keep = used.iter().any(|p| p.starts_with(d));
                if !keep {
                    let _ = std::fs::remove_dir_all(d);
                }
                keep
            });
            if let Some(tmp) = s.community_tmp.take() {
                let _ = std::fs::remove_dir_all(tmp);
            }
            s.community_art = None;
        });
        app.global::<Shell>().set_page(Page::Creator);
    });
    let a = app.as_weak();
    g.on_req_answer(move |ok| {
        let app = a.unwrap();
        app.global::<IconGen>().set_req_open(false);
        let cb = with(|s| s.pending_req.take());
        if ok {
            if let Some(cb) = cb {
                cb(&app);
            }
        }
    });

    // Image search
    let isg = app.global::<ImageSearch>();
    let a = app.as_weak();
    isg.on_new_search(move || new_search(&a.unwrap()));
    let a = app.as_weak();
    isg.on_more(move || {
        let app = a.unwrap();
        with(|s| s.page += 1);
        run_search(&app);
    });
    let a = app.as_weak();
    isg.on_toggle(move |i| {
        let app = a.unwrap();
        with(|s| {
            if let Some(c) = s.checked.get_mut(i as usize) {
                *c = !*c;
            }
        });
        search_rows(&app);
    });
    let a = app.as_weak();
    isg.on_cancel(move || close_search(&a.unwrap()));
    let a = app.as_weak();
    isg.on_query_edited(move |text: SharedString| {
        let text = text.to_string();
        let weak = a.clone();
        let t = slint::Timer::default();
        t.start(slint::TimerMode::SingleShot, std::time::Duration::from_millis(400), move || {
            let Some(frag) = search::suggestion_fragment(&text) else { return };
            if frag.chars().count() < 2 {
                return;
            }
            let weak = weak.clone();
            std::thread::spawn(move || {
                if let Ok(tags) = search::suggest_tags(&frag) {
                    let _ = weak.upgrade_in_event_loop(move |app| {
                        let tags: Vec<SharedString> = tags.into_iter().take(8).map(SharedString::from).collect();
                        app.global::<ImageSearch>().set_suggestions(ModelRc::new(VecModel::from(tags)));
                    });
                }
            });
        });
        with(|s| s.suggest_timer = Some(t));
    });
    let a = app.as_weak();
    isg.on_pick_suggestion(move |tag| {
        let app = a.unwrap();
        let isg = app.global::<ImageSearch>();
        let q = isg.get_query().to_string();
        let head: Vec<&str> = q.split(',').collect();
        let mut parts: Vec<String> = head[..head.len().saturating_sub(1)].iter().map(|s| s.trim().to_string()).collect();
        parts.push(tag.to_string());
        isg.set_query(parts.join(", ").into());
        isg.set_suggestions(ModelRc::new(VecModel::from(Vec::<SharedString>::new())));
    });
    let a = app.as_weak();
    isg.on_add_ticked(move || {
        let app = a.unwrap();
        let ticked: Vec<search::SearchResult> = with(|s| s.results.iter().zip(&s.checked).filter(|(_, c)| **c).map(|(r, _)| r.clone()).collect());
        if ticked.is_empty() {
            return dialogs::message(&app, DialogIcon::Info, "Nothing Ticked", "Tick the pictures you want to use first.");
        }
        let isg = app.global::<ImageSearch>();
        isg.set_status(format!("Downloading {} picture(s)...", ticked.len()).into());
        isg.set_adding(true);
        let dir = core_ai().extra_art_dir();
        let weak = app.as_weak();
        std::thread::spawn(move || {
            let result = search::download_ticked(&ticked, &dir);
            let _ = weak.upgrade_in_event_loop(move |app| match result {
                Ok(paths) => {
                    with(|s| {
                        for p in paths {
                            if !s.extra_images.contains(&p) {
                                s.extra_images.push(p);
                            }
                        }
                    });
                    update_extra_label(&app);
                    close_search(&app);
                }
                Err(e) => {
                    let isg = app.global::<ImageSearch>();
                    isg.set_status(format!("Download failed: {e}").into());
                    isg.set_adding(false);
                }
            });
        });
    });
}

/// Exit cleanup (v2 `clear_generated_icons`): temporary icon sets go.
pub fn cleanup() {
    with(|s| {
        for d in s.used_dirs.drain(..) {
            let _ = std::fs::remove_dir_all(d);
        }
        if let Some(d) = s.output_dir.take() {
            let _ = std::fs::remove_dir_all(d);
        }
        if let Some(d) = s.community_tmp.take() {
            let _ = std::fs::remove_dir_all(d);
        }
    });
}

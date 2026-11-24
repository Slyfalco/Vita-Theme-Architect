//! Theme Library screen controller: scanning, search/filter/sort, selection and the
//! inspector details.

use crate::{AppWindow, Library, Shell, ThemeDetail, ThemeRow};
use slint::{ComponentHandle, Image, ModelRc, SharedString, VecModel};
use std::cell::RefCell;
use std::path::PathBuf;
use std::rc::Rc;
use vta_core::library::{self, LibraryTheme, SortMode, ThemeProblem};

#[derive(Default)]
pub struct LibraryState {
    pub root: PathBuf,
    pub all: Vec<LibraryTheme>,
    /// Indices into `all` of the themes currently shown, in display order.
    pub visible: Vec<usize>,
    pub problems: Vec<ThemeProblem>,
    pub selected_path: Option<PathBuf>,
}

pub type SharedLibrary = Rc<RefCell<LibraryState>>;

pub fn format_size(bytes: u64) -> String {
    let mb = bytes as f64 / (1024.0 * 1024.0);
    if mb >= 1024.0 {
        format!("{:.2} GB", mb / 1024.0)
    } else if mb >= 0.1 {
        format!("{mb:.1} MB")
    } else {
        format!("{} KB", (bytes as f64 / 1024.0).ceil() as u64)
    }
}

fn plural(n: usize, one: &str, many: &str) -> String {
    format!("{n} {}", if n == 1 { one } else { many })
}

fn summary(t: &LibraryTheme) -> String {
    let mut parts = vec![plural(t.page_count(), "home page", "home pages")];
    parts.push(match t.icon_count() {
        0 => "default system icons".into(),
        n => plural(n, "custom icon", "custom icons"),
    });
    if t.has_bgm() {
        parts.push("background music".into());
    }
    let mut s = parts.join(" · ");
    if let Some(first) = s.get_mut(0..1) {
        first.make_ascii_uppercase();
    }
    s
}

fn load_image(path: Option<PathBuf>) -> Option<Image> {
    path.filter(|p| p.is_file()).and_then(|p| Image::load_from_path(&p).ok())
}

fn row_for(t: &LibraryTheme) -> ThemeRow {
    let thumb = load_image(t.asset_path("preview_thumbnail.png"));
    ThemeRow {
        title: t.title().into(),
        author: t.author().into(),
        version: t.version().into(),
        folder: t.folder_name.clone().into(),
        summary: summary(t).into(),
        has_thumb: thumb.is_some(),
        thumb: thumb.unwrap_or_default(),
        has_bgm: t.has_bgm(),
        size: format_size(t.size_bytes).into(),
        pages: t.page_count() as i32,
        icons: t.icon_count() as i32,
    }
}

fn detail_for(t: &LibraryTheme) -> ThemeDetail {
    let icons: Vec<Image> = vta_core::theme::ICONS
        .iter()
        .filter_map(|(_, id)| load_image(t.asset_path(id)))
        .collect();
    let bgm = t.asset_path("bgm.at9").filter(|p| p.is_file());
    let icon_count = t.icon_count();
    ThemeDetail {
        valid: true,
        title: t.title().into(),
        author: t.author().into(),
        version: t.version().into(),
        folder: t.folder_name.clone().into(),
        target: format!("ux0:customtheme/{}", t.folder_name).into(),
        icons_text: if icon_count == 0 {
            "Default system icons".into()
        } else {
            format!("{icon_count} of 17 custom").into()
        },
        home: load_image(t.asset_path("preview_livearea.png")).unwrap_or_default(),
        lock: load_image(t.asset_path("preview_lockscreen.png")).unwrap_or_default(),
        icons: ModelRc::new(VecModel::from(icons)),
        has_bgm: bgm.is_some(),
        bgm_name: bgm
            .as_ref()
            .and_then(|p| p.file_name())
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default()
            .into(),
        bgm_info: "ATRAC9 audio".into(),
    }
}

/// Rescan the library folder and refresh the screen.
pub fn rescan(app: &AppWindow, state: &SharedLibrary) {
    {
        let mut s = state.borrow_mut();
        let (all, problems) = library::scan(&s.root);
        s.all = all;
        s.problems = problems;
    }
    let lib = app.global::<Library>();
    let s = state.borrow();
    lib.set_total(s.all.len() as i32);
    lib.set_with_bgm(s.all.iter().filter(|t| t.has_bgm()).count() as i32);
    lib.set_with_icons(s.all.iter().filter(|t| t.icon_count() > 0).count() as i32);
    lib.set_total_size(format_size(s.all.iter().map(|t| t.size_bytes).sum()).into());
    drop(s);
    refresh_view(app, state);
}

/// Re-apply sort, search and filter, rebuild the list and keep the selection.
pub fn refresh_view(app: &AppWindow, state: &SharedLibrary) {
    let lib = app.global::<Library>();
    let mut s = state.borrow_mut();
    let mode = SortMode::ALL.get(lib.get_sort() as usize).copied().unwrap_or(SortMode::TitleAsc);
    library::sort(&mut s.all, mode);
    let search = lib.get_search().to_string();
    let filter = lib.get_filter();
    s.visible = s
        .all
        .iter()
        .enumerate()
        .filter(|(_, t)| library::matches(t, &search))
        .filter(|(_, t)| match filter {
            1 => t.has_bgm(),
            2 => t.icon_count() > 0,
            _ => true,
        })
        .map(|(i, _)| i)
        .collect();
    let rows: Vec<ThemeRow> = s.visible.iter().map(|&i| row_for(&s.all[i])).collect();
    lib.set_themes(ModelRc::new(VecModel::from(rows)));
    let sel = s
        .selected_path
        .as_ref()
        .and_then(|p| s.visible.iter().position(|&i| &s.all[i].path == p));
    drop(s);
    match sel {
        Some(i) => select(app, state, i as i32),
        None => select(app, state, -1),
    }
}

/// The theme at a list position.
pub fn theme_at(state: &SharedLibrary, index: i32) -> Option<LibraryTheme> {
    let s = state.borrow();
    usize::try_from(index).ok().and_then(|i| s.visible.get(i)).map(|&i| s.all[i].clone())
}

pub fn select(app: &AppWindow, state: &SharedLibrary, index: i32) {
    let lib = app.global::<Library>();
    let theme = theme_at(state, index);
    state.borrow_mut().selected_path = theme.as_ref().map(|t| t.path.clone());
    match &theme {
        Some(t) => {
            lib.set_selected(index);
            lib.set_detail(detail_for(t));
        }
        None => {
            lib.set_selected(-1);
            lib.set_detail(ThemeDetail::default());
        }
    }
    crate::bgm_view::load(app, theme.as_ref());
}

pub fn wire(app: &AppWindow, state: &SharedLibrary) {
    let lib = app.global::<Library>();
    let sort_labels: Vec<SharedString> = SortMode::ALL.iter().map(|m| m.label().into()).collect();
    lib.set_sort_options(ModelRc::new(VecModel::from(sort_labels)));

    let (a, s) = (app.as_weak(), state.clone());
    lib.on_select(move |i| select(&a.unwrap(), &s, i));
    let (a, s) = (app.as_weak(), state.clone());
    lib.on_search_changed(move |_| refresh_view(&a.unwrap(), &s));
    let (a, s) = (app.as_weak(), state.clone());
    lib.on_filter_changed(move |f| {
        let app = a.unwrap();
        app.global::<Library>().set_filter(f);
        refresh_view(&app, &s);
    });
    let (a, s) = (app.as_weak(), state.clone());
    lib.on_sort_changed(move |_| refresh_view(&a.unwrap(), &s));

    let s = state.clone();
    lib.on_open_library_folder(move || crate::platform::open_path(&s.borrow().root));
    let a = app.as_weak();
    lib.on_configure_ftp(move || {
        a.unwrap().global::<Shell>().invoke_navigate(crate::Page::Settings);
    });
}

//! App themes: the desktop app's own look (not the Vita theme being edited).
//!
//! An app theme is a JSON definition of design tokens (colours, radii, fonts and an
//! optional backdrop image) applied to the `Palette` global at runtime, so new looks
//! can be added without code changes. "Obsidian Vita Deck" is built in; extra
//! themes are `*.json` files in `<app data>/app_themes/`.

use crate::{AppWindow, Palette};
use serde::Deserialize;
use slint::{Color, ComponentHandle};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

pub const DEFAULT_ID: &str = "obsidian-vita-deck";
const BUILT_IN: &[&str] = &[include_str!("../themes/obsidian-vita-deck.json")];

#[derive(Debug, Clone, Deserialize)]
pub struct AppTheme {
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub colors: BTreeMap<String, String>,
    #[serde(rename = "radius-control")]
    pub radius_control: Option<f32>,
    #[serde(rename = "radius-card")]
    pub radius_card: Option<f32>,
    #[serde(rename = "ui-font")]
    pub ui_font: Option<String>,
    #[serde(rename = "mono-font")]
    pub mono_font: Option<String>,
    /// Image path; relative paths resolve against the theme file's folder.
    pub backdrop: Option<String>,
    #[serde(rename = "backdrop-opacity")]
    pub backdrop_opacity: Option<f32>,
    #[serde(skip)]
    pub folder: Option<PathBuf>,
}

/// All available app themes: built-ins first, then user files (a user file with a
/// built-in id overrides it).
pub fn available(user_dir: &Path) -> Vec<AppTheme> {
    let mut themes: Vec<AppTheme> = BUILT_IN
        .iter()
        .map(|s| serde_json::from_str(s).expect("built-in app theme is valid"))
        .collect();
    if let Ok(entries) = std::fs::read_dir(user_dir) {
        for path in entries.flatten().map(|e| e.path()) {
            if path.extension().is_none_or(|e| e != "json") {
                continue;
            }
            let Some(mut t) = std::fs::read_to_string(&path)
                .ok()
                .and_then(|s| serde_json::from_str::<AppTheme>(&s).ok())
            else {
                continue;
            };
            t.folder = path.parent().map(Path::to_path_buf);
            themes.retain(|e| e.id != t.id);
            themes.push(t);
        }
    }
    themes
}

fn parse_color(s: &str) -> Option<Color> {
    let hex = s.trim().trim_start_matches('#');
    let v = u32::from_str_radix(hex, 16).ok()?;
    match hex.len() {
        6 => Some(Color::from_rgb_u8((v >> 16) as u8, (v >> 8) as u8, v as u8)),
        8 => Some(Color::from_argb_u8(v as u8, (v >> 24) as u8, (v >> 16) as u8, (v >> 8) as u8)),
        _ => None,
    }
}

/// Apply a theme to the UI. Tokens the theme doesn't define keep the built-in value.
pub fn apply(app: &AppWindow, theme: &AppTheme) {
    let p = app.global::<Palette>();
    let base: AppTheme = serde_json::from_str(BUILT_IN[0]).expect("built-in app theme is valid");
    let color = |k: &str| {
        theme
            .colors
            .get(k)
            .and_then(|c| parse_color(c))
            .or_else(|| base.colors.get(k).and_then(|c| parse_color(c)))
            .unwrap_or(Color::from_rgb_u8(255, 0, 255))
    };
    p.set_canvas(color("canvas"));
    p.set_chrome(color("chrome"));
    p.set_surface1(color("surface1"));
    p.set_surface2(color("surface2"));
    p.set_surface3(color("surface3"));
    p.set_inset(color("inset"));
    p.set_outline(color("outline"));
    p.set_primary(color("primary"));
    p.set_primary_dark(color("primary-dark"));
    p.set_accent(color("accent"));
    p.set_success(color("success"));
    p.set_success_text(color("success-text"));
    p.set_warning(color("warning"));
    p.set_danger(color("danger"));
    p.set_text_hi(color("text-hi"));
    p.set_text_mid(color("text-mid"));
    p.set_text_lo(color("text-lo"));
    p.set_on_primary(color("on-primary"));
    p.set_radius_control(theme.radius_control.or(base.radius_control).unwrap_or(8.0));
    p.set_radius_card(theme.radius_card.or(base.radius_card).unwrap_or(16.0));
    p.set_ui_font(theme.ui_font.clone().or(base.ui_font).unwrap_or_default().into());
    p.set_mono_font(theme.mono_font.clone().or(base.mono_font).unwrap_or_default().into());

    let backdrop = theme.backdrop.as_ref().and_then(|b| {
        let path = Path::new(b);
        let path = match (&theme.folder, path.is_relative()) {
            (Some(dir), true) => dir.join(path),
            _ => path.to_path_buf(),
        };
        slint::Image::load_from_path(&path).ok()
    });
    match backdrop {
        Some(img) => {
            p.set_backdrop(img);
            p.set_backdrop_opacity(theme.backdrop_opacity.unwrap_or(0.25));
        }
        None => p.set_backdrop_opacity(0.0),
    }
}

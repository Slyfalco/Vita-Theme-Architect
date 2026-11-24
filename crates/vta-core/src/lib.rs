//! Core (UI-independent) logic for VitaTheme Architect v3.

pub mod build;
pub mod iconai;
pub mod import;
pub mod integrity;
pub mod library;
pub mod migrations;
pub mod paths;
pub mod settings;
pub mod theme;
pub mod updater;
pub mod vita;

/// The app version (the v2 `CURRENT_VERSION`): must match the release tag and the
/// exe's version resource.
pub const CURRENT_VERSION: &str = "3.0";

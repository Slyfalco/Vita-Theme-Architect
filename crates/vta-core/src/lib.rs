//! Core (UI-independent) logic for VitaTheme Architect v3.

pub mod library;
pub mod paths;
pub mod settings;
pub mod theme;

/// The app version (the v2 `CURRENT_VERSION`): must match the release tag and the
/// exe's version resource.
pub const CURRENT_VERSION: &str = "3.0";

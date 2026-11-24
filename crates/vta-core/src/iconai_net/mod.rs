//! The network side of the AI icon generator (v2 `icon_generator.py`), with
//! no UI:
//!
//! - [`gemini`]: the Google Gemini path (`GeminiIconThread` and its helpers).
//! - [`search`]: the extra artwork search (`ImageSearchDialog` helpers:
//!   Safebooru, Bing, thumbnails, saving ticked pictures).
//! - [`community`]: community icon sets as examples (`ExampleDownloadThread`)
//!   and community theme art (`community_theme_art`).
//! - [`rules`]: the generator rules and locations these share.
//!
//! All calls are blocking; run them off the UI thread. Errors are
//! [`py::PyError`]s, which display as v2 shows exceptions (`Type: message`).

pub mod community;
pub mod gemini;
mod http;
pub mod images;
pub mod py;
pub mod rules;
pub mod search;

pub use community::{community_theme_art, download_examples, matching_community_themes, theme_pages};
pub use gemini::{run_gemini_job, GeminiEvent, GeminiJob};
pub use py::PyError;
pub use search::{download_ticked, load_thumbnails, search, suggest_tags, SearchResult};

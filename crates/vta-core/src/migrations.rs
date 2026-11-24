//! Post-update migrations for local data (port of v2 `data_migrations.py`, same
//! `.data_version` marker so v2 → v3 → later versions chain).
//!
//! `run` compares the version that last wrote the data (`<app data>/.data_version`)
//! with the current version and runs every registered migration in between, in
//! order. Best-effort: a failing migration is logged and stops the run (marker
//! left at the last success, so it retries next launch); it never blocks startup.
//! No marker + existing data = pre-versioning (all migrations run); no marker and
//! no data = clean install (just stamp the current version).

use std::path::Path;

/// A migration: (version that introduced the format change, function).
pub type Migration = (&'static str, fn(&Path) -> Result<(), String>);

/// Registered migrations. v2 had none; v3 keeps v2's data formats unchanged
/// (settings.json, calibration, AI runtime layout), so none are needed yet.
pub const MIGRATIONS: &[Migration] = &[];

/// Version string -> comparable tuple ("1.2.10" -> [1, 2, 10]).
fn vt(text: &str) -> Vec<u64> {
    let nums: Vec<u64> = text.split(|c: char| !c.is_ascii_digit()).filter(|s| !s.is_empty()).filter_map(|s| s.parse().ok()).collect();
    if nums.is_empty() {
        vec![0]
    } else {
        nums
    }
}

fn has_existing_data(app_support: &Path) -> bool {
    app_support.join("settings.json").exists() || app_support.join("icon_ai").is_dir()
}

/// Bring local data up to `current`; returns the migration versions applied.
pub fn run(app_support: &Path, current: &str) -> Vec<&'static str> {
    run_with(app_support, current, MIGRATIONS)
}

pub fn run_with(app_support: &Path, current: &str, migrations: &[Migration]) -> Vec<&'static str> {
    let marker = app_support.join(".data_version");
    let write = |v: &str| {
        let _ = std::fs::write(&marker, v);
    };
    let stored = match std::fs::read_to_string(&marker) {
        Ok(s) => s.trim().to_string(),
        Err(_) => {
            if !has_existing_data(app_support) {
                write(current);
                return Vec::new();
            }
            "0".to_string()
        }
    };
    if vt(&stored) >= vt(current) {
        return Vec::new();
    }
    let mut pending: Vec<&Migration> = migrations.iter().filter(|(v, _)| vt(&stored) < vt(v) && vt(v) <= vt(current)).collect();
    pending.sort_by_key(|(v, _)| vt(v));
    let mut applied = Vec::new();
    let mut last_good = stored.clone();
    for (version, f) in pending {
        if let Err(e) = f(app_support) {
            eprintln!("[migrations] migration for {version} failed:\n{e}");
            write(&last_good);
            return applied;
        }
        applied.push(*version);
        last_good = version.to_string();
        write(version);
    }
    write(current);
    applied
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ok(_: &Path) -> Result<(), String> {
        Ok(())
    }
    fn fail(_: &Path) -> Result<(), String> {
        Err("boom".into())
    }

    #[test]
    fn runner_like_v2() {
        let d = std::env::temp_dir().join(format!("vta-mig-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        // Clean install: stamp only.
        assert!(run_with(&d, "3.0", &[("2.5", ok)]).is_empty());
        assert_eq!(std::fs::read_to_string(d.join(".data_version")).unwrap(), "3.0");
        // Upgrade from 2.0 runs the in-between migrations in order.
        std::fs::write(d.join(".data_version"), "2.0").unwrap();
        assert_eq!(run_with(&d, "3.0", &[("3.0", ok), ("2.5", ok), ("1.0", ok)]), vec!["2.5", "3.0"]);
        // A failure stops and leaves the marker at the last success.
        std::fs::write(d.join(".data_version"), "2.0").unwrap();
        assert_eq!(run_with(&d, "3.0", &[("2.5", ok), ("2.9", fail)]), vec!["2.5"]);
        assert_eq!(std::fs::read_to_string(d.join(".data_version")).unwrap(), "2.5");
        std::fs::remove_dir_all(&d).ok();
    }
}

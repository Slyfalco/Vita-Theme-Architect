"""Post-update migrations for the app's local data.

All of the user's local data — settings.json, the AI icon generator's
hardware/calibration data (icon_ai/calibration.dat and the per-install
.perf_key), the downloaded icon_ai runtime, and the Themes/Extras folders —
lives in the app-support directory and is never deleted or overwritten by an
update (the updater only touches the install folder).  But when a new release
changes a stored *format*, the new version has to migrate the existing data on
first launch.  Register such a change as a migration below.

run_migrations() compares the version that last wrote the data (recorded in
.data_version) against CURRENT_VERSION and runs every registered migration in
between, in order.  It is best-effort and defensive: a migration that raises is
logged and stops the run (so it is retried on the next launch) but never
prevents the app from starting.  The marker is only advanced past a migration
that succeeded.
"""
import os
import re
import traceback

from constants import APP_SUPPORT_DIR, SETTINGS_FILE, CURRENT_VERSION

_MARKER = os.path.join(APP_SUPPORT_DIR, '.data_version')
_ICON_AI_DIR = os.path.join(APP_SUPPORT_DIR, 'icon_ai')


def _vt(text):
    """Version string -> comparable tuple, e.g. '1.2.10' -> (1, 2, 10)."""
    nums = re.findall(r'\d+', str(text or ''))
    return tuple(int(n) for n in nums) if nums else (0,)


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------
# Each entry is (version_introduced, callable).  The callable takes no args,
# operates on files under APP_SUPPORT_DIR, and runs only when upgrading PAST
# that version.  Make them idempotent where you can.  Example:
#
#   def _m_1_1():
#       # rewrite icon_ai/calibration.dat into the v1.1 record schema
#       ...
#
#   MIGRATIONS = [("1.1", _m_1_1)]
MIGRATIONS = []


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def _has_existing_data():
    return os.path.exists(SETTINGS_FILE) or os.path.isdir(_ICON_AI_DIR)


def _read_marker():
    try:
        with open(_MARKER, encoding='utf-8') as fh:
            return fh.read().strip()
    except OSError:
        return None


def _write_marker(version):
    try:
        with open(_MARKER, 'w', encoding='utf-8') as fh:
            fh.write(str(version))
    except OSError:
        pass


def run_migrations():
    """Bring local data up to CURRENT_VERSION.  Safe to call on every launch;
    returns the list of migration versions applied this run."""
    stored = _read_marker()
    if stored is None:
        # No marker yet.  Existing data from before versioning: treat it as the
        # earliest version so every migration runs.  A clean install has nothing
        # to migrate, so just stamp the current version.
        if not _has_existing_data():
            _write_marker(CURRENT_VERSION)
            return []
        stored = "0"

    if _vt(stored) >= _vt(CURRENT_VERSION):
        return []

    pending = [(v, fn) for v, fn in MIGRATIONS
               if _vt(stored) < _vt(v) <= _vt(CURRENT_VERSION)]
    pending.sort(key=lambda pair: _vt(pair[0]))

    applied, last_good = [], stored
    for version, fn in pending:
        try:
            fn()
        except Exception:
            print(f"[migrations] migration for {version} failed:\n{traceback.format_exc()}")
            _write_marker(last_good)
            return applied
        applied.append(version)
        last_good = version
        _write_marker(version)

    _write_marker(CURRENT_VERSION)
    return applied

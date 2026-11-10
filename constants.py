import os

# secure_function is generated at build time and only ships (encrypted) in
# official release builds. Source/self-built copies run without it, with
# online features (theme downloader, repo submission) disabled.
try:
    import secure_function as crypted
except ImportError:
    crypted = None
ONLINE_FEATURES_AVAILABLE = crypted is not None
ONLINE_FEATURES_UNAVAILABLE_MSG = "Online features are only available in official release builds."
# Online features turn themselves off in a virtual machine (this protects the
# online theme database); the app explains why rather than failing silently.
if ONLINE_FEATURES_AVAILABLE:
    try:
        if crypted.reason() == 'vm':
            ONLINE_FEATURES_AVAILABLE = False
            ONLINE_FEATURES_UNAVAILABLE_MSG = ("Online features (theme downloader and submission) are disabled "
                                               "because the app appears to be running in a virtual machine, which "
                                               "protects the online theme database. Run it on a normal installation "
                                               "to use them.")
    except Exception:
        pass

# The online theme list is handed out a page at a time: crypted.get(search,
# field, sort, page) returns at most THEME_PAGE themes. field: 'title',
# 'author', 'tags' or 'title_tags'; sort: 'newest', 'oldest', 'az', 'za' or
# 'downloads'. crypted.count(search, field) gives the number of matches.
THEME_PAGE = 50


def theme_pages(search='', field='title', sort='newest', max_pages=None):
    """Themes from the online list, fetched a page at a time. Raises
    AttributeError when online features aren't available."""
    page = 1
    while max_pages is None or page <= max_pages:
        found = crypted.get(search, field, sort, page)
        if found is None:
            raise AttributeError("Conditions not met.")
        yield from found
        if len(found) < THEME_PAGE:
            return
        page += 1

# --- Program Configuration ---
CURRENT_VERSION = "1.0"
GITHUB_REPO = "Slyfalco/Vita-Theme-Architect"
UPDATE_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
UPDATE_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
UPDATE_API_TAG = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{{tag}}"



def secure_url(url):
    """psvt.ovh lists its theme files as http://; the same files are served
    over https, so fetch them that way (encrypted, certificate checked)."""
    if url and url.lower().startswith('http://'):
        return 'https://' + url[len('http://'):]
    return url

# --- Configuration File ---
# Where data (settings, themes, AI runtime/models, migration marker) is kept is
# decided automatically from HOW the build is being run — no flag or marker:
#
#  * Portable (run in place — a USB stick, Downloads, any folder that is NOT a
#    standard program-install path and was NOT put there by our installer): ALL
#    data lives beside the app (in "portable_data" next to the exe; next to the
#    .app on macOS), so it travels with the app on the same medium. If that
#    medium is read-only, it falls back to the per-user location below.
#  * Installed (placed by our installer, or sitting in a program-install path):
#    per-user data (%APPDATA% / ~/Library / ~/.local/share) with a PER-INSTALL
#    subfolder keyed off the install path, so multiple installs on one machine
#    (AI + Standard, unofficial, etc.) never share settings or the migration
#    marker. The default theme/model library is shared at the per-user base so a
#    user's installs don't each keep a copy; a CUSTOM library path is per-install.
#  * From source (not frozen): the shared per-user base, unchanged (dev data).
import sys as _sys
import platform as _platform


def _base_support_dir():
    if _platform.system() == 'Darwin':
        return os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'VitaThemeArchitect')
    if _platform.system() == 'Linux':
        _xdg = os.environ.get('XDG_DATA_HOME', os.path.join(os.path.expanduser('~'), '.local', 'share'))
        return os.path.join(_xdg, 'VitaThemeArchitect')
    # Windows — %APPDATA%\VitaThemeArchitect, so data persists regardless of
    # where the exe is placed and works even from a read-only Program Files.
    _appdata = os.environ.get('APPDATA') or os.path.expanduser('~')
    return os.path.join(_appdata, 'VitaThemeArchitect')


def _running_frozen():
    try:
        import __main__
        if getattr(__main__, '__compiled__', False):
            return True
    except Exception:
        pass
    return bool(getattr(_sys, 'frozen', False) or '__compiled__' in globals())


def _install_dir():
    # The folder the running build is installed in. dirname(sys.executable) is
    # the install folder for a Nuitka standalone build (the basename can be an
    # unreliable bundled 'python', but the directory is still the install dir).
    try:
        return os.path.normcase(os.path.normpath(os.path.dirname(os.path.abspath(_sys.executable))))
    except Exception:
        return None


def _install_key(install_dir):
    import hashlib
    return hashlib.sha256(install_dir.encode('utf-8', 'replace')).hexdigest()[:16]


def _is_writable_dir(d):
    # True if d can be created and written in — so a read-only medium (a
    # write-protected USB stick, an optical disc) is detected and we fall back.
    try:
        os.makedirs(d, exist_ok=True)
        _probe = os.path.join(d, '.vta_write_test')
        with open(_probe, 'w') as _f:
            _f.write('')
        os.remove(_probe)
        return True
    except Exception:
        return False


def _in_program_location(folder):
    # True when `folder` sits under a standard program-install path for the OS.
    _h = os.path.normcase(os.path.normpath(folder))
    _roots = []
    _sysname = _platform.system()
    if _sysname == 'Windows':
        for _var in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432'):
            _v = os.environ.get(_var)
            if _v:
                _roots.append(_v)
        _la = os.environ.get('LOCALAPPDATA')
        if _la:
            _roots.append(os.path.join(_la, 'Programs'))
    elif _sysname == 'Darwin':
        _roots += ['/Applications', os.path.expanduser('~/Applications')]
    else:
        _roots += ['/usr', '/opt', '/bin', '/sbin',
                   os.path.expanduser('~/.local/bin'),
                   os.path.expanduser('~/.local/share'),
                   os.path.expanduser('~/.local/lib')]
    for _r in _roots:
        _rn = os.path.normcase(os.path.normpath(_r))
        if _h == _rn or _h.startswith(_rn + os.sep):
            return True
    return False


def _portable_base():
    # Decide portable vs installed AUTOMATICALLY, from where the build runs — no
    # flag or marker. Returns the folder to keep data beside the app (portable),
    # or None for an installed build (use the per-user location) or a read-only
    # medium (fall back to per-user). Because the folder is resolved from the exe
    # location every launch, a portable copy keeps working when the USB drive
    # letter or volume label changes (data is found relative to the exe, never by
    # a remembered absolute path).
    if not _running_frozen():
        return None
    try:
        _exe = os.path.abspath(_sys.executable)
    except Exception:
        return None
    if _platform.system() == 'Darwin':
        _macos = os.path.dirname(_exe)                       # .../Contents/MacOS
        _bundle = os.path.dirname(os.path.dirname(_macos))   # .../Something.app
        _app_folder = _macos
        # Data goes BESIDE the .app, never inside it (that breaks the signature).
        _host = os.path.dirname(_bundle) if _bundle.endswith('.app') else _macos
        _data_name = 'VitaThemeArchitect-data'
    else:
        _app_folder = os.path.dirname(_exe)                  # the app folder
        _host = _app_folder                                  # data sits inside it
        _data_name = 'portable_data'
    # Installed (NOT portable) when our installer's uninstaller sits one level
    # above the app folder (Windows managed install), or the app is in a standard
    # program-install path.
    if (_platform.system() == 'Windows'
            and os.path.isfile(os.path.join(os.path.dirname(_app_folder), 'unins000.exe'))):
        return None
    if _in_program_location(_app_folder):
        return None
    _data = os.path.join(_host, _data_name)
    return _data if _is_writable_dir(_data) else None


_BASE_SUPPORT = _base_support_dir()
_PORTABLE_BASE = _portable_base()
_INSTALL_DIR = _install_dir() if (_running_frozen() and not _PORTABLE_BASE) else None

if _PORTABLE_BASE:
    # Portable: everything (settings, themes, AI runtime + models) lives beside
    # the app and travels with it on the same medium. One copy = one data dir, so
    # there is no per-install keying and no legacy adoption.
    _APP_SUPPORT = _PORTABLE_BASE
    _BASE_SUPPORT = _PORTABLE_BASE
    os.makedirs(_APP_SUPPORT, exist_ok=True)
elif _INSTALL_DIR:
    _key = _install_key(_INSTALL_DIR)
    _INSTALLS_ROOT = os.path.join(_BASE_SUPPORT, 'installs')
    _APP_SUPPORT = os.path.join(_INSTALLS_ROOT, _key)
    _first_install_here = not os.path.isdir(_APP_SUPPORT)
    # Whether any per-install dir already exists. If none do and there is
    # shared-root settings, this is the one-time transition from the old single
    # shared layout: that config belongs to this install, so adopt it.
    try:
        _installs_had_entries = os.path.isdir(_INSTALLS_ROOT) and any(
            os.path.isdir(os.path.join(_INSTALLS_ROOT, _e)) for _e in os.listdir(_INSTALLS_ROOT))
    except Exception:
        _installs_had_entries = False
    os.makedirs(_APP_SUPPORT, exist_ok=True)
    _legacy_settings = os.path.join(_BASE_SUPPORT, 'settings.json')
    if _first_install_here and not _installs_had_entries and os.path.isfile(_legacy_settings):
        # One-time adoption of the pre-per-install layout: copy the small state
        # files in (leaving the originals, which are harmless and only a source
        # run would still read). The shared Themes/Extras libraries at the base
        # are deliberately NOT moved — they stay shared.
        import shutil
        for _name in ('settings.json', '.data_version'):
            _src = os.path.join(_BASE_SUPPORT, _name)
            _dst = os.path.join(_APP_SUPPORT, _name)
            try:
                if os.path.isfile(_src) and not os.path.exists(_dst):
                    shutil.copy2(_src, _dst)
            except Exception:
                pass
    # Record which install folder owns this data dir, so the uninstaller can
    # find and remove exactly this build's data folder: it enumerates the
    # per-install dirs, reads this marker, and matches it against the install
    # path being removed. Plain text (one line) keeps the installer's read
    # trivial. The second line is the version, for reference.
    try:
        with open(os.path.join(_APP_SUPPORT, 'install_path.txt'), 'w', encoding='utf-8') as _f:
            _f.write(_INSTALL_DIR + '\n' + CURRENT_VERSION + '\n')
    except Exception:
        pass
else:
    _APP_SUPPORT = _BASE_SUPPORT
    os.makedirs(_APP_SUPPORT, exist_ok=True)

os.makedirs(_BASE_SUPPORT, exist_ok=True)
APP_SUPPORT_DIR = _APP_SUPPORT
# The shared, per-user base (one per OS user, regardless of how many builds are
# installed). Per-user even for an all-users/admin install, because it lives in
# the user profile. Large, reusable downloads (AI models, example sets) and the
# default theme/extras libraries live here so a user's multiple installs don't
# each keep their own copy. Per-install, version-specific state (settings, the
# AI runtime, the data-migration marker) lives under APP_SUPPORT_DIR instead.
BASE_SUPPORT_DIR = _BASE_SUPPORT
INSTALL_KEY = _key if _INSTALL_DIR else None
IS_PORTABLE = _PORTABLE_BASE is not None
SETTINGS_FILE = os.path.join(_APP_SUPPORT, 'settings.json')


def is_portable():
    """True when running as a portable copy (data kept beside the app)."""
    return IS_PORTABLE


def store_path(p):
    """Serialize a user-chosen path for settings.json. A path INSIDE the data base
    is stored RELATIVE to the base, so a portable copy keeps resolving it after the
    USB drive letter or volume label changes; a path outside the base (a fixed
    location on this machine) is stored absolute. Empty stays empty."""
    if not p:
        return p
    try:
        _ap = os.path.abspath(p)
        _base = os.path.abspath(BASE_SUPPORT_DIR)
        if os.path.normcase(_ap) == os.path.normcase(_base) or \
           os.path.normcase(_ap).startswith(os.path.normcase(_base) + os.sep):
            return os.path.relpath(_ap, _base).replace(os.sep, '/')
    except Exception:
        pass
    return p


def load_path(p, default=''):
    """Resolve a settings path saved by store_path(). A relative value is resolved
    against the CURRENT data base (so it follows the app across drives/machines);
    an absolute value is used as-is; empty -> default."""
    if not p:
        return default
    if os.path.isabs(p):
        return p
    try:
        return os.path.normpath(os.path.join(BASE_SUPPORT_DIR, p))
    except Exception:
        return default
# Default theme/extras libraries live at the SHARED base so every install sees
# one collection. A user-set custom location (per-install, in settings.json) is
# never shared across installs.
DEFAULT_THEMES_PATH = os.path.join(_BASE_SUPPORT, 'Themes')
DEFAULT_EXTRAS_PATH = os.path.join(_BASE_SUPPORT, 'Extras')


def other_per_install_dirs_exist():
    """True when another installed build has its own data dir besides this one.
    Used before deleting shared downloads (AI models, examples) so a 'free space'
    or edition-switch action never pulls data out from under another install."""
    try:
        _root = os.path.join(_BASE_SUPPORT, 'installs')
        if not os.path.isdir(_root):
            return False
        for _e in os.listdir(_root):
            if os.path.isdir(os.path.join(_root, _e)) and _e != INSTALL_KEY:
                return True
    except Exception:
        pass
    return False
os.environ['QT_LOGGING_RULES'] = 'qt.multimedia=true'

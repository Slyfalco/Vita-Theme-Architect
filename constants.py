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
CURRENT_VERSION = "1.0"  # Update this when releasing new versions
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
import platform as _platform
if _platform.system() == 'Darwin':
    _APP_SUPPORT = os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'VitaThemeArchitect')
elif _platform.system() == 'Linux':
    _xdg = os.environ.get('XDG_DATA_HOME', os.path.join(os.path.expanduser('~'), '.local', 'share'))
    _APP_SUPPORT = os.path.join(_xdg, 'VitaThemeArchitect')
else:
    # Windows — store in %APPDATA%\VitaThemeArchitect so settings persist
    # regardless of where the exe is placed, survive updates, and work even
    # when the exe is run from Program Files (read-only for the exe directory).
    _appdata = os.environ.get('APPDATA') or os.path.expanduser('~')
    _APP_SUPPORT = os.path.join(_appdata, 'VitaThemeArchitect')
os.makedirs(_APP_SUPPORT, exist_ok=True)
APP_SUPPORT_DIR = _APP_SUPPORT
SETTINGS_FILE = os.path.join(_APP_SUPPORT, 'settings.json')
DEFAULT_THEMES_PATH = os.path.join(_APP_SUPPORT, 'Themes')
DEFAULT_EXTRAS_PATH = os.path.join(_APP_SUPPORT, 'Extras')
os.environ['QT_LOGGING_RULES'] = 'qt.multimedia=true'

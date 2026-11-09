import sys
import os
import time
import platform
import traceback
import hashlib
import json

# Release builds compile in a start-up module of their own; it has to come
# first, before anything else is imported. Other builds don't have it.
if globals().get('__compiled__', False):
    try:
        import release_runtime  # noqa: F401
    except ImportError:
        pass

_CRASH_LOG_MAX_BYTES = 512 * 1024  # 500 KB before rotation
_CRASH_LOG_KEEP_LINES = 500


def _rotate_crash_log_if_needed(log_path):
    """Truncate crash.log to the last _CRASH_LOG_KEEP_LINES lines when it grows
    past _CRASH_LOG_MAX_BYTES, preventing unbounded disk usage."""
    try:
        if os.path.exists(log_path) and os.path.getsize(log_path) > _CRASH_LOG_MAX_BYTES:
            import datetime
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
            kept = lines[-_CRASH_LOG_KEEP_LINES:] if len(lines) > _CRASH_LOG_KEEP_LINES else lines
            with open(log_path, 'w', encoding='utf-8') as f:
                f.write(f"--- Log rotated at {datetime.datetime.now()} "
                        f"(kept last {len(kept)} lines) ---\n")
                f.writelines(kept)
    except Exception:
        pass


def _crash_log(exc_type, exc_value, exc_tb):
    """Write unhandled exceptions to a crash log next to the executable."""
    text = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
    try:
        if globals().get('__compiled__', False):
            if sys.platform == 'win32':
                # Write to APPDATA: the app folder may be read-only (e.g.
                # Program Files), and older onefile builds ran from a temp
                # dir that was deleted on exit.
                _appdata = os.environ.get('APPDATA') or os.path.expanduser('~')
                log_path = os.path.join(_appdata, 'VitaThemeArchitect', 'crash.log')
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
            else:
                log_path = os.path.join(os.path.dirname(sys.executable), 'crash.log')
        else:
            log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'crash.log')
        _rotate_crash_log_if_needed(log_path)
        with open(log_path, 'a', encoding='utf-8') as f:
            import datetime
            f.write(f"\n--- {datetime.datetime.now()} ---\n{text}\n")
    except Exception:
        pass
    sys.__excepthook__(exc_type, exc_value, exc_tb)


sys.excepthook = _crash_log

# On macOS, a C++ static initialiser inside QtCore.so calls
# CFBundleGetMainBundle() during dlopen() — before the Cocoa run loop starts.
# On macOS 26, this returns NULL (or an uninitialised bundle object), causing
# an immediate SIGSEGV in __CFCheckCFInfoPACSignature.
#
# NSApplicationLoad() is a plain-C AppKit function that registers the app
# bundle with the OS without starting the event loop. Calling it via ctypes
# BEFORE the first PyQt6 import ensures CFBundleGetMainBundle() returns a
# valid bundle when the Qt static initialiser fires.
if sys.platform == 'darwin' and globals().get('__compiled__', False):
    try:
        import ctypes
        _appkit = ctypes.CDLL('/System/Library/Frameworks/AppKit.framework/AppKit')
        _appkit.NSApplicationLoad()
        del _appkit
    except Exception:
        pass

    # Also tell Qt where its plugins are so it never needs CFBundle for
    # plugin-path discovery at runtime.
    _exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    for _rel in ('PyQt6/Qt6/plugins', 'lib/PyQt6/Qt6/plugins', 'plugins'):
        _candidate = os.path.join(_exe_dir, _rel)
        if os.path.isdir(_candidate):
            os.environ.setdefault('QT_PLUGIN_PATH', _candidate)
            os.environ.setdefault('QT_QPA_PLATFORM_PLUGIN_PATH',
                                  os.path.join(_candidate, 'platforms'))
            break

# On Windows, Nuitka's QtCore-postLoad.py is supposed to set QT_PLUGIN_PATH
# after importing QtCore, but this can fire too late — Qt's QPA initialisation
# (triggered by QApplication()) may already have failed with qFatal() before
# the postLoad script runs.  Set the path explicitly here, before any PyQt6
# import, so Qt finds qwindows.dll reliably in both onefile and standalone
# layouts.
#
# In Nuitka onefile the extraction temp dir is on sys.path (needed so .pyd
# extension modules can be imported); we search it for PyQt6/Qt6/plugins/.
# For standalone builds sys.executable sits next to the PyQt6/ folder, so
# the fallback covers that case.
if sys.platform == 'win32' and globals().get('__compiled__', False):
    _qt_plugins = None
    for _p in sys.path:
        if not _p:
            continue
        _candidate = os.path.join(_p, 'PyQt6', 'Qt6', 'plugins')
        if os.path.isdir(_candidate):
            _qt_plugins = _candidate
            break
    if not _qt_plugins:
        # Fallback: standalone layout — PyQt6/ sits next to the executable.
        _exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        _candidate = os.path.join(_exe_dir, 'PyQt6', 'Qt6', 'plugins')
        if os.path.isdir(_candidate):
            _qt_plugins = _candidate
    if _qt_plugins:
        os.environ.setdefault('QT_PLUGIN_PATH', _qt_plugins)
        os.environ.setdefault('QT_QPA_PLATFORM_PLUGIN_PATH',
                              os.path.join(_qt_plugins, 'platforms'))
    del _qt_plugins, _candidate

from PyQt6.QtWidgets import QApplication, QDialog, QVBoxLayout, QLabel
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QIcon


def app_icon():
    """The application icon, loaded from the bundled Assets folder (next to this
    file in source, next to the exe in a build). Returns an empty QIcon if the
    file is missing so startup never fails over a cosmetic asset."""
    base = os.path.dirname(os.path.abspath(__file__))
    for name in ('app_icon.ico', 'app_icon.png'):
        path = os.path.join(base, 'Assets', name)
        if os.path.exists(path):
            return QIcon(path)
    return QIcon()


def _verify_install_integrity(base_dir=None, max_problems=20):
    """Public corruption check. A build ships an integrity.json next to the app
    files (SHA-256 per file); confirm the shipped files still match. Returns
    (ok, problems). No manifest (running from source, or macOS) -> (True, []).
    Advisory only — catches a bad/incomplete install; the real official-only
    enforcement lives in the release modules, not here. Kept in main (compiled
    into the exe) rather than its own file."""
    base = base_dir or os.path.dirname(os.path.abspath(__file__))
    manifest = os.path.join(base, 'integrity.json')
    if not os.path.isfile(manifest):
        return True, []
    try:
        with open(manifest, 'r', encoding='utf-8') as fh:
            files = (json.load(fh) or {}).get('files', {})
    except (OSError, ValueError):
        return False, ['integrity.json is unreadable']
    problems = []
    for rel, expected in files.items():
        path = os.path.join(base, rel.replace('/', os.sep))
        if not os.path.isfile(path):
            problems.append('missing: ' + rel)
        else:
            try:
                h = hashlib.sha256()
                with open(path, 'rb') as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b''):
                        h.update(chunk)
                if h.hexdigest().lower() != str(expected).lower():
                    problems.append('changed: ' + rel)
            except OSError:
                problems.append('unreadable: ' + rel)
        if len(problems) >= max_problems:
            break
    return (not problems), problems


class SplashScreen(QDialog):
    """Frameless splash screen shown during app startup."""

    MINIMUM_DISPLAY_SECONDS = 2

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Dialog
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setFixedSize(420, 190)
        self.setStyleSheet("""
            QDialog {
                background-color: #1a1a2e;
                border: 2px solid #0f3460;
                border-radius: 10px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(8)

        title = QLabel("VitaTheme Architect")
        title.setFont(QFont("Arial", 20, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("color: #e0e0ff; letter-spacing: 1px;")
        layout.addWidget(title)

        subtitle = QLabel("PS Vita Theme Editor")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet("color: #6a8fc8; font-size: 11px;")
        layout.addWidget(subtitle)

        separator = QLabel()
        separator.setFixedHeight(1)
        separator.setStyleSheet("background-color: #0f3460; margin: 4px 20px;")
        layout.addWidget(separator)

        layout.addSpacing(6)

        self.status_label = QLabel("Initializing...")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet(
            "color: #8ab4f8; font-size: 12px; font-style: italic;"
        )
        layout.addWidget(self.status_label)

        self._start_time = None
        self._tasks_done = False

    def showEvent(self, event):
        super().showEvent(event)
        # Center on screen
        screen = self.screen().availableGeometry()
        self.move(
            screen.x() + (screen.width() - self.width()) // 2,
            screen.y() + (screen.height() - self.height()) // 2,
        )
        # Bring the splash to the front so it isn't hidden behind other windows.
        self.raise_()
        self.activateWindow()
        # Start tasks after the event loop processes the show
        self._start_time = time.monotonic()
        QTimer.singleShot(0, self._run_startup_tasks)

    def _update_status(self, message):
        self.status_label.setText(message)
        QApplication.processEvents()

    def _run_startup_tasks(self):
        if platform.system() != 'Windows':
            try:
                self._update_status("Warming up OS compatibility...")
                from wine_setup import startup_wine_warmup
                startup_wine_warmup(status_callback=self._update_status)
            except Exception as e:
                print(f"Wine warmup error (non-fatal): {e}")

        self._update_status("Ready — launching app...")
        self._tasks_done = True
        self._check_minimum_time()

    def _check_minimum_time(self):
        elapsed = time.monotonic() - self._start_time
        remaining_ms = int((self.MINIMUM_DISPLAY_SECONDS - elapsed) * 1000)
        if remaining_ms > 0:
            QTimer.singleShot(remaining_ms, self.accept)
        else:
            self.accept()


if __name__ == "__main__":
    try:
        import atexit
        if platform.system() != 'Windows':
            from wine_setup import shutdown_wine
            atexit.register(shutdown_wine)

        # Windows compiled: PyQt6.QtCore is already imported at module level.
        # Use its __file__ to locate the plugins directory and register it
        # with Qt before QApplication() triggers QPA initialisation.
        # This is a belt-and-suspenders complement to the qt.conf bundled by
        # setup_build.py and the env-var block above the PyQt6 imports.
        if sys.platform == 'win32' and globals().get('__compiled__', False):
            try:
                _qtcore = sys.modules.get('PyQt6.QtCore')
                if _qtcore and getattr(_qtcore, '__file__', None):
                    from PyQt6.QtCore import QCoreApplication as _QCA
                    _QCA.addLibraryPath(os.path.join(
                        os.path.dirname(os.path.abspath(_qtcore.__file__)),
                        'Qt6', 'plugins'))
                    del _QCA
                del _qtcore
            except Exception:
                pass

        app = QApplication(sys.argv)
        app.setWindowIcon(app_icon())  # taskbar + every window/dialog
        from style import apply_modern_style
        apply_modern_style(app)

        # Public install-integrity check: on a build that ships integrity.json,
        # confirm the files aren't corrupted/incomplete and advise reinstall if
        # so. No-op from source / on macOS (no manifest). Advisory only.
        try:
            _ok, _problems = _verify_install_integrity()
        except Exception:
            _ok, _problems = True, []
        if not _ok:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.critical(
                None, "Installation Problem",
                "VitaTheme Architect's files look corrupted or incomplete:\n\n"
                + "\n".join(_problems[:8])
                + ("\n…" if len(_problems) > 8 else "")
                + "\n\nPlease reinstall or re-download the app.")
            sys.exit(1)

        # Warn if running from ~/Downloads — macOS will prompt for folder
        # access every launch because the app's own bundled files live there.
        if sys.platform == 'darwin' and globals().get('__compiled__', False):
            _exe = os.path.abspath(sys.executable)
            _downloads = os.path.expanduser('~/Downloads')
            if _exe.startswith(_downloads + os.sep):
                from PyQt6.QtWidgets import QMessageBox
                _msg = QMessageBox()
                _msg.setWindowTitle("Move to Applications")
                _msg.setText(
                    "VitaTheme Architect is running from your Downloads folder.\n\n"
                    "macOS will ask for Downloads access on every launch because "
                    "the app's resources are stored there.\n\n"
                    "Move VitaThemeArchitect.app to your Applications folder "
                    "to avoid this prompt."
                )
                _msg.setIcon(QMessageBox.Icon.Warning)
                _msg.exec()

        splash = SplashScreen()
        splash.exec()

        from main_window import VitaThemeArchitect
        window = VitaThemeArchitect()
        window.show()
        window.raise_()
        window.activateWindow()
        sys.exit(app.exec())
    except Exception:
        traceback.print_exc()
        _crash_log(*sys.exc_info())
        sys.exit(1)

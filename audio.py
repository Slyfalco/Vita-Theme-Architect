import sys
import os
import platform
import shutil
import subprocess
import tempfile
import datetime
import traceback

from wine_setup import (
    startup_wine_warmup,
    _wine_version_stamp,
    _setup_persistent_wine,
    _wine_tools_dir,
    _wine_env,
    _wine_invocation,
    _find_bundled_wine,
)

# On Windows, running a console .exe from a windowless GUI app causes a black
# terminal window to flash open and close for each subprocess call.
# CREATE_NO_WINDOW suppresses that. The flag only exists on Windows.
_SUBPROCESS_FLAGS = (
    {'creationflags': subprocess.CREATE_NO_WINDOW}
    if platform.system() == 'Windows'
    else {}
)

# --- VLC environment setup (MUST happen before 'import vlc') ---
# python-vlc checks PYTHON_VLC_LIB_PATH (full path to libvlc shared lib) and
# PYTHON_VLC_MODULE_PATH (plugin directory) at import time via find_lib().
# In a frozen bundle (PyInstaller or Nuitka) the VLC files are bundled
# relative to the executable / extraction directory.
#
# NOTE: Nuitka standalone mode does not always set sys.frozen=True, so we
# always probe for bundled dylibs relative to sys.executable and only fall
# back to the sys.frozen/_MEIPASS path for PyInstaller onefile bundles.
if 'PYTHON_VLC_LIB_PATH' not in os.environ:
    # Determine base directory candidates to search for bundled VLC libs.
    _base_dirs = []
    if hasattr(sys, '_MEIPASS'):
        # PyInstaller onefile extracts to _MEIPASS
        _base_dirs.append(sys._MEIPASS)
    # Always check relative to the running executable (covers Nuitka standalone
    # regardless of whether sys.frozen is set).
    _base_dirs.append(os.path.dirname(os.path.abspath(sys.executable)))

    _libvlc = None
    _base_dir = None
    for _bd in _base_dirs:
        _vlc_bundle = os.path.join(_bd, 'VLC')
        if sys.platform == 'win32':
            _candidates = [os.path.join(_vlc_bundle, 'libvlc.dll')]
        elif sys.platform == 'darwin':
            _candidates = [
                os.path.join(_bd, 'lib', 'libvlc.dylib'),
                os.path.join(_vlc_bundle, 'lib', 'libvlc.dylib'),
                os.path.join(_bd, 'libvlc.dylib'),
            ]
        else:
            _candidates = [
                os.path.join(_bd, 'libvlc.so'),
                os.path.join(_vlc_bundle, 'libvlc.so'),
            ]
        for _c in _candidates:
            if os.path.exists(_c):
                _libvlc = _c
                _base_dir = _bd
                break
        if _libvlc:
            break

    if _libvlc:
        # On macOS, libvlc.dylib uses @rpath/libvlccore.dylib but carries no
        # LC_RPATH entries of its own.  Pre-loading libvlccore.dylib by its
        # absolute path first puts it in the process image list so dyld can
        # satisfy the @rpath reference when libvlc.dylib is subsequently loaded.
        # This mirrors what python-vlc's find_lib() does for system VLC at
        # /Applications/VLC.app/Contents/MacOS/lib/.
        if sys.platform == 'darwin':
            import ctypes as _ctypes
            _libvlccore = os.path.join(os.path.dirname(_libvlc), 'libvlccore.dylib')
            if os.path.exists(_libvlccore):
                try:
                    # Keep a module-level reference so the handle is not
                    # closed (dlclose) before libvlc.dylib links against it.
                    _vlccore_handle = _ctypes.CDLL(_libvlccore)
                except OSError:
                    pass
            del _ctypes
        os.environ['PYTHON_VLC_LIB_PATH'] = _libvlc

        # Find plugins directory.  The stripped VLC build bundles only external
        # plugins (e.g. libbluray JARs) — basic codecs are compiled into
        # libvlccore.  Only set PYTHON_VLC_MODULE_PATH when the directory
        # actually contains VLC plugin files (.dylib/.so); skip stale
        # plugins.dat-only dirs so VLC doesn't waste time parsing an invalid
        # cache from the build machine.
        _vlc_bundle = os.path.join(_base_dir, 'VLC')
        for _pdir in [os.path.join(_base_dir, 'plugins'),
                      os.path.join(_vlc_bundle, 'plugins')]:
            if os.path.isdir(_pdir):
                _has_plugins = any(
                    f.endswith(('.dylib', '.so', '.dll'))
                    for f in os.listdir(_pdir)
                )
                if _has_plugins:
                    os.environ['PYTHON_VLC_MODULE_PATH'] = _pdir
                break
# ---------------------------------------------------------------

def _sndfile_platform_tag():
    """`<os>-<arch>` for the running machine (e.g. 'windows-arm64'), matching
    setup_build.py's _build_tag() so the right native lib folder is chosen."""
    os_name = {'Windows': 'windows', 'Darwin': 'macos', 'Linux': 'linux'}.get(
        platform.system(), platform.system().lower())
    machine = (platform.machine() or '').lower()
    arch = {'amd64': 'x64', 'x86_64': 'x64', 'x64': 'x64',
            'arm64': 'arm64', 'aarch64': 'arm64'}.get(machine, machine)
    return f"{os_name}-{arch}"


def _prepare_bundled_sndfile():
    """Make a bundled libsndfile loadable before `import soundfile`.

    soundfile's pip wheel bundles libsndfile for every platform EXCEPT
    Windows-on-ARM (no win-arm64 build exists upstream), where its import fails
    with "libsndfile.dll error 0x7e". If we ship a native libsndfile for such a
    platform — flattened into `<exe dir>/libsndfile/` (builds; setup_build only
    bundles the matching arch) or under `<repo>/libsndfile/<os>-<arch>/` (source;
    arch-tagged so an arm64 DLL is never loaded on an x64 run) — put that folder
    on the loader search path and, on Windows, preload the DLL so soundfile's own
    bare `dlopen('libsndfile.dll')` resolves to it (Windows matches an
    already-loaded module by base name). No-op when nothing is bundled, so
    platforms where the wheel already works are untouched.

    This is the same trust model as the other bundled native libs (Qt, VLC, the
    Python runtime): whatever ships in the app folder is trusted. The real
    boundary is write access to that folder, not this one file.
    """
    libname = ('libsndfile.dll' if sys.platform == 'win32'
               else 'libsndfile.dylib' if sys.platform == 'darwin'
               else 'libsndfile.so')
    exedir = os.path.dirname(os.path.abspath(sys.executable))
    here = os.path.dirname(os.path.abspath(__file__))
    tag = _sndfile_platform_tag()
    for d in (os.path.join(exedir, 'libsndfile'),              # builds (flat)
              os.path.join(here, 'libsndfile', tag)):          # source (per-arch)
        full = os.path.join(d, libname)
        if os.path.isfile(full):
            try:
                if sys.platform == 'win32':
                    os.add_dll_directory(d)   # so libsndfile's own deps resolve
                    import ctypes
                    ctypes.WinDLL(full)       # base-name match for soundfile
                else:
                    var = 'DYLD_LIBRARY_PATH' if sys.platform == 'darwin' else 'LD_LIBRARY_PATH'
                    os.environ[var] = d + os.pathsep + os.environ.get(var, '')
            except OSError:
                pass
            return


_prepare_bundled_sndfile()
try:
    import soundfile
    SOUNDFILE_AVAILABLE = True
except (ImportError, OSError) as _sf_err:
    # soundfile bundles libsndfile, which can't load on Windows-on-ARM (the wheel
    # ships an x86-64 libsndfile.dll → "error 0x7e") unless a native libsndfile
    # is bundled (see _prepare_bundled_sndfile). It's only used to normalise a
    # source file to 16-bit WAV in convert_to_at9, which also falls back to
    # ffmpeg / a stdlib WAV path, so never let this block startup.
    soundfile = None
    SOUNDFILE_AVAILABLE = False
    print(f"soundfile unavailable ({_sf_err}); audio encoding falls back to "
          f"ffmpeg or a stdlib WAV path.")

from PySide6.QtWidgets import (
    QDialog, QPushButton, QVBoxLayout, QHBoxLayout,
    QSlider, QLabel, QStyle, QMessageBox
)
from PySide6.QtCore import Qt, QTimer, QDir

# VLC is imported lazily — only needed for AudioPlayerDialog playback,
# not for audio conversion.  This prevents the entire module from failing
# when VLC is not installed.
vlc = None

def _ensure_vlc():
    """Import vlc on first use. Returns True if available."""
    global vlc
    if vlc is not None:
        return True
    try:
        import vlc as _vlc
        vlc = _vlc
        return True
    except (ImportError, OSError) as e:
        print(f"  VLC not available: {e}")
        return False


# =============================================================================
# == Crash Log Helper
# =============================================================================
def _crash_log_path():
    """Return the path to crash.log (next to executable or script)."""
    if globals().get('__compiled__', False):
        return os.path.join(os.path.dirname(sys.executable), 'crash.log')
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'crash.log')


_CRASH_LOG_MAX_BYTES = 512 * 1024  # 500 KB before rotation
_CRASH_LOG_KEEP_LINES = 500


def _rotate_crash_log_if_needed(log_path):
    """Truncate crash.log to the last _CRASH_LOG_KEEP_LINES lines when it grows
    past _CRASH_LOG_MAX_BYTES, preventing unbounded disk usage."""
    try:
        if os.path.exists(log_path) and os.path.getsize(log_path) > _CRASH_LOG_MAX_BYTES:
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
            kept = lines[-_CRASH_LOG_KEEP_LINES:] if len(lines) > _CRASH_LOG_KEEP_LINES else lines
            with open(log_path, 'w', encoding='utf-8') as f:
                f.write(f"--- Log rotated at {datetime.datetime.now()} "
                        f"(kept last {len(kept)} lines) ---\n")
                f.writelines(kept)
    except Exception:
        pass


def _log_error(context, message, exc=None):
    """Log an error to both stdout and crash.log with diagnostic details."""
    timestamp = datetime.datetime.now().isoformat()
    lines = [
        f"\n--- {timestamp} [audio/{context}] ---",
        f"  Platform: {platform.system()} {platform.machine()}",
        f"  Compiled: {globals().get('__compiled__', False)}",
        f"  Error: {message}",
    ]
    if exc:
        lines.append(f"  Exception: {type(exc).__name__}: {exc}")
        lines.append(f"  Traceback:\n{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))}")

    log_text = '\n'.join(lines) + '\n'
    print(log_text)
    try:
        log_path = _crash_log_path()
        _rotate_crash_log_if_needed(log_path)
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(log_text)
    except Exception:
        pass


def _log_wine_error(context, cmd, returncode, stderr_text, wine_path=None, env=None):
    """Log a Wine-specific error with full diagnostic info to crash.log."""
    timestamp = datetime.datetime.now().isoformat()
    tools = _tools_dir()
    wine_bin_dir = os.path.join(tools, 'wine_minimal', 'bin')

    lines = [
        f"\n--- {timestamp} [audio/{context}] ---",
        f"  Platform: {platform.system()} {platform.machine()}",
        f"  Compiled: {globals().get('__compiled__', False)}",
        f"  Tools dir: {tools}",
        f"  Tools dir exists: {os.path.isdir(tools)}",
        f"  Wine bin dir exists: {os.path.isdir(wine_bin_dir)}",
    ]

    if os.path.isdir(wine_bin_dir):
        for name in os.listdir(wine_bin_dir):
            fpath = os.path.join(wine_bin_dir, name)
            try:
                st = os.stat(fpath)
                lines.append(f"    {name}: mode={oct(st.st_mode)}, size={st.st_size}")
            except OSError as e:
                lines.append(f"    {name}: stat failed: {e}")

    if wine_path:
        lines.append(f"  Wine binary: {wine_path}")
        lines.append(f"  Wine binary exists: {os.path.exists(wine_path)}")
    lines.append(f"  Command: {cmd}")
    lines.append(f"  Exit code: {returncode}")
    lines.append(f"  stderr: {stderr_text}")
    if env:
        for key in ('WINEPREFIX', 'WINEDEBUG', 'PATH', 'DYLD_FALLBACK_LIBRARY_PATH', 'LD_LIBRARY_PATH'):
            if key in env:
                lines.append(f"  env[{key}]: {env[key][:200]}")

    log_text = '\n'.join(lines) + '\n'
    print(log_text)
    try:
        log_path = _crash_log_path()
        _rotate_crash_log_if_needed(log_path)
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(log_text)
    except Exception:
        pass


# =============================================================================
# == Tool Path Resolution
# =============================================================================
def _tools_dir():
    """Return the path to the bundled Tools directory (VGAudioCli, etc.)."""
    if globals().get('__compiled__', False):
        if hasattr(sys, '_MEIPASS'):
            return os.path.join(sys._MEIPASS, 'Tools')
        return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), 'Tools')
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Tools')


def _at9tool_dir():
    """Return the path to at9tool's directory.
    at9tool is user-provided. Checks settings.json for a custom path first,
    then falls back to the default Tools/ location."""
    # 1) Check user-configured path in settings.json
    try:
        import json
        _system = platform.system()
        if _system == 'Darwin':
            _settings_file = os.path.join(
                os.path.expanduser('~'), 'Library', 'Application Support',
                'VitaThemeArchitect', 'settings.json'
            )
        elif _system == 'Linux':
            _xdg = os.environ.get('XDG_DATA_HOME',
                                   os.path.join(os.path.expanduser('~'), '.local', 'share'))
            _settings_file = os.path.join(_xdg, 'VitaThemeArchitect', 'settings.json')
        else:
            _appdata = os.environ.get('APPDATA') or os.path.expanduser('~')
            _settings_file = os.path.join(_appdata, 'VitaThemeArchitect', 'settings.json')
        if os.path.isfile(_settings_file):
            with open(_settings_file, 'r') as _f:
                _settings = json.load(_f)
            _custom = _settings.get('at9tool_path', '')
            if _custom and os.path.isfile(_custom):
                return os.path.dirname(_custom)
    except Exception:
        pass

    # 2) Default location
    if globals().get('__compiled__', False):
        if platform.system() == 'Darwin':
            # On macOS the .app bundle is read-only; use the writable
            # Application Support directory so users can place at9tool.exe there.
            app_support = os.path.join(
                os.path.expanduser('~'), 'Library', 'Application Support',
                'VitaThemeArchitect', 'Tools'
            )
            os.makedirs(app_support, exist_ok=True)
            return app_support
        return os.path.join(os.path.dirname(sys.executable), 'Tools')
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Tools')


AT9TOOL_REQUIRED_MSG = (
    "Audio to AT9 encoding is disabled (only .at9 BGM files can be used).\n\n"
    "To use this feature you must provide at9tool.exe (made by Sony). "
    "Set its path in Settings. It works on every OS — on macOS and Linux "
    "it runs through the bundled Wine."
)

# BGM source formats (besides .at9) that can be encoded to AT9. Each is first
# converted to a temporary WAV in convert_to_at9, then encoded by at9tool.
# All are decoded by soundfile's bundled libsndfile at their original sample
# rate, so sample positions (BGM loop start), pitch and speed are unchanged.
ENCODABLE_AUDIO_EXTS = ['.wav', '.flac', '.ogg', '.opus', '.mp3', '.aiff', '.aif']


def find_at9tool():
    """Return the full path to the user-provided at9tool.exe, or None if missing."""
    at9tool_path = os.path.join(_at9tool_dir(), 'at9tool.exe')
    return at9tool_path if os.path.isfile(at9tool_path) else None


def _at9tool_cmd(at9tool_path):
    """Return the command list prefix needed to run at9tool.
    On Windows it runs natively; on other platforms it needs Wine.
    Prefers the bundled Wine, falls back to system wine."""
    if platform.system() == 'Windows':
        return [at9tool_path]
    bundled = _find_bundled_wine()
    if bundled:
        return _wine_invocation(bundled) + [at9tool_path]
    return _wine_invocation('wine') + [at9tool_path]


def _clean_wine_runtime_cache():
    """Clean Wine temp/cache files after running at9tool/VGAudioCli.

    Removes temporary files Wine creates during execution to prevent
    disk bloat across repeated conversions. Only runs on non-Windows.
    """
    if platform.system() == 'Windows':
        return

    # Clean both bundled and persistent wine prefix locations
    prefix_dirs = []
    tools = _tools_dir()
    prefix_dirs.append(os.path.join(tools, 'wine_prefix'))
    wine_tools = _wine_tools_dir()
    prefix_dirs.append(os.path.join(wine_tools, 'wine_prefix'))

    for wine_prefix in prefix_dirs:
        if not os.path.isdir(wine_prefix):
            continue

        temp_dirs = [
            os.path.join(wine_prefix, 'drive_c', 'windows', 'temp'),
        ]
        users_dir = os.path.join(wine_prefix, 'drive_c', 'users')
        if os.path.isdir(users_dir):
            for user_folder in os.listdir(users_dir):
                user_temp = os.path.join(users_dir, user_folder, 'Temp')
                if os.path.isdir(user_temp):
                    temp_dirs.append(user_temp)

        for temp_dir in temp_dirs:
            if os.path.isdir(temp_dir):
                try:
                    for entry in os.listdir(temp_dir):
                        entry_path = os.path.join(temp_dir, entry)
                        if os.path.isfile(entry_path):
                            os.remove(entry_path)
                        elif os.path.isdir(entry_path):
                            shutil.rmtree(entry_path, ignore_errors=True)
                except OSError:
                    pass


# =============================================================================
# == Helper Functions (Audio Conversion)
# =============================================================================
def _try_ffmpeg_decode(at9_path, wav_path, status_callback=None):
    """Try decoding AT9 to WAV using system ffmpeg. Returns True on success.

    Used only as a last resort after at9tool and VGAudioCli have both failed.
    FFmpeg has native ATRAC9 (AT9) support and runs without Wine, but may be
    slower than the preferred decoders.
    """
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        return False
    if status_callback:
        status_callback("Falling back to ffmpeg decoder.\nThis may take a little longer...")
    try:
        cmd = [ffmpeg, '-y', '-i', at9_path, wav_path]
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, **_SUBPROCESS_FLAGS)
        if r.returncode == 0 and os.path.exists(wav_path):
            return True
        stderr_text = r.stderr.decode(errors='replace').strip()
        _log_error('ffmpeg_decode', f"ffmpeg failed (exit {r.returncode}): {stderr_text[-500:]}")
        return False
    except subprocess.TimeoutExpired:
        _log_error('ffmpeg_decode', "ffmpeg timed out (>30s)")
        return False
    except Exception as e:
        _log_error('ffmpeg_decode', f"ffmpeg error: {e}", exc=e)
        return False


def _ffmpeg_to_pcm16_wav(input_path, wav_path):
    """Normalise any source audio to a 16-bit PCM WAV with system ffmpeg.

    Fallback for convert_to_at9 when soundfile/libsndfile is unavailable (e.g.
    ARM64 Windows). Returns the output sample rate on success, else None.
    """
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        return None
    try:
        cmd = [ffmpeg, '-y', '-i', input_path, '-c:a', 'pcm_s16le', wav_path]
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=120, **_SUBPROCESS_FLAGS)
        if r.returncode == 0 and os.path.exists(wav_path):
            import wave
            with wave.open(wav_path, 'rb') as w:
                return w.getframerate()
        stderr_text = r.stderr.decode(errors='replace').strip() if r.stderr else ''
        _log_error('ffmpeg_normalise',
                   f"ffmpeg failed (exit {r.returncode}): {stderr_text[-500:]}")
        return None
    except Exception as e:
        _log_error('ffmpeg_normalise', f"ffmpeg error: {e}", exc=e)
        return None


def _normalise_to_pcm16_wav(input_path, tmp_wav):
    """Produce a 16-bit PCM WAV at tmp_wav from any supported source, trying,
    in order: soundfile (works on almost every platform), ffmpeg (any format,
    if installed), then a plain copy when the source is already a 16-bit PCM WAV
    (stdlib only — no deps). Returns the sample rate, or None if none worked."""
    # 1) soundfile — preferred; handles mp3/flac/ogg/wav/etc. everywhere its
    #    bundled libsndfile loads (all platforms except Windows-on-ARM).
    if SOUNDFILE_AVAILABLE:
        data, sr = soundfile.read(input_path)
        soundfile.write(tmp_wav, data, sr, subtype='PCM_16')
        return sr
    # 2) ffmpeg — any format, when soundfile can't load (e.g. Windows-ARM).
    sr = _ffmpeg_to_pcm16_wav(input_path, tmp_wav)
    if sr is not None:
        return sr
    # 3) No deps (stdlib only): convert any uncompressed PCM WAV to 16-bit.
    #    Covers 8/16/24/32-bit PCM WAV in-program, so a WAV source always works
    #    even with no soundfile and no ffmpeg (e.g. Windows-ARM).
    if input_path.lower().endswith('.wav'):
        try:
            import wave
            with wave.open(input_path, 'rb') as w:
                if w.getcomptype() != 'NONE':
                    raise ValueError('compressed WAV needs soundfile or ffmpeg')
                sr = w.getframerate()
                ch = w.getnchannels()
                sw = w.getsampwidth()
                frames = w.readframes(w.getnframes())
            if sw != 2:
                # Convert sample WIDTH only (e.g. 24/32-bit → 16-bit). This does
                # NOT resample or change rate/frame-count/pitch/tempo, so loop
                # sample indices stay exact. audioop is imported only here (it's
                # removed in Python 3.13; 16-bit WAV never needs it).
                import audioop
                frames = audioop.lin2lin(frames, sw, 2)
            with wave.open(tmp_wav, 'wb') as o:
                o.setnchannels(ch)
                o.setsampwidth(2)
                o.setframerate(sr)
                o.writeframes(frames)
            return sr
        except Exception as e:
            _log_error('normalise_wav', f"stdlib WAV convert failed: {e}", exc=e)
    return None


def convert_at9_to_wav(at9_path, wav_path, status_callback=None):
    """Convert AT9 to WAV using at9tool (preferred), VGAudioCli, or ffmpeg (last resort).

    status_callback: optional callable(str) — receives status/warning messages
    for display in a progress window. Called with the ffmpeg fallback warning
    if neither at9tool nor VGAudioCli succeeds.
    """
    print(f"Converting {at9_path} to {wav_path}")
    if at9_path.lower().endswith(".wav"):
        if at9_path != wav_path:
            shutil.copy(at9_path, wav_path)
        return True

    if not os.path.exists(at9_path):
        _log_error('convert_at9_to_wav', f"Source file not found: {at9_path}")
        return False

    # 1) Try at9tool — user-provided, lives outside _MEIPASS
    at9_tools = _at9tool_dir()
    at9tool_path = os.path.join(at9_tools, 'at9tool.exe')

    if os.path.exists(at9tool_path):
        try:
            cmd = _at9tool_cmd(at9tool_path) + ['-d', at9_path, wav_path]
            env = _wine_env() if platform.system() != 'Windows' else None
            use_new_session = platform.system() != 'Windows'
            r = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=at9_tools,
                env=env, start_new_session=use_new_session, **_SUBPROCESS_FLAGS
            )
            _clean_wine_runtime_cache()
            if r.returncode == 0:
                return True
            stderr_text = r.stderr.decode(errors='replace').strip() if r.stderr else ''
            print(f"  at9tool decode failed (exit code {r.returncode})")
            _log_wine_error('at9tool_decode', cmd, r.returncode, stderr_text,
                            wine_path=cmd[0] if len(cmd) > 1 else None, env=env)
        except Exception as e:
            _clean_wine_runtime_cache()
            _log_error('at9tool_decode', f"at9tool error: {e}", exc=e)

    # 2) Fallback to VGAudioCli.exe (bundled in _MEIPASS or project Tools/).
    #    Runs natively on Windows, via Wine on macOS/Linux.
    tools = _tools_dir()
    vgaudio_exe = os.path.join(tools, 'VGAudioCli.exe')
    if os.path.exists(vgaudio_exe):
        is_win = platform.system() == 'Windows'
        wine_path = None
        if is_win:
            cmd = [vgaudio_exe, '-i', at9_path, '-o', wav_path]
            env = None
            can_run = True
        else:
            wine_path = _find_bundled_wine() or shutil.which('wine')
            if wine_path:
                cmd = _wine_invocation(wine_path) + [vgaudio_exe, '-i', at9_path, '-o', wav_path]
                env = _wine_env()
                can_run = True
            else:
                _log_error('VGAudioCli_decode',
                           "Wine not found — cannot run VGAudioCli.exe. "
                           f"Bundled wine dir: {os.path.join(tools, 'wine_minimal', 'bin')} "
                           f"exists={os.path.isdir(os.path.join(tools, 'wine_minimal', 'bin'))}")
                can_run = False

        if can_run:
            try:
                print("  Falling back to VGAudioCli for decoding...")
                # Use start_new_session=True on non-Windows to force
                # fork+exec instead of posix_spawn.  On macOS ARM64,
                # posix_spawn can create a process context in which Wine's
                # subsequent exec of wine-preloader fails ("could not exec
                # the wine loader").  fork+exec avoids this.
                use_new_session = not is_win
                r = subprocess.run(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=tools, env=env, timeout=30,
                    start_new_session=use_new_session, **_SUBPROCESS_FLAGS
                )
                if not is_win:
                    _clean_wine_runtime_cache()
                if r.returncode == 0:
                    return True
                stderr_text = r.stderr.decode(errors='replace').strip() if r.stderr else ''
                print(f"  VGAudioCli decode failed (exit code {r.returncode})")
                if stderr_text:
                    print(f"  stderr: {stderr_text}")
                _log_wine_error('VGAudioCli_decode', cmd, r.returncode, stderr_text,
                                wine_path=wine_path, env=env)
            except subprocess.TimeoutExpired:
                if not is_win:
                    _clean_wine_runtime_cache()
                _log_error('VGAudioCli_decode', "VGAudioCli timed out (>30s)")
            except Exception as e:
                if not is_win:
                    _clean_wine_runtime_cache()
                _log_error('VGAudioCli_decode', f"VGAudioCli error: {e}", exc=e)
    else:
        _log_error('convert_at9_to_wav',
                    f"VGAudioCli.exe not found at {vgaudio_exe}, "
                    f"tools_dir={tools}, exists={os.path.isdir(tools)}, "
                    f"contents={os.listdir(tools) if os.path.isdir(tools) else 'N/A'}")

    # 3) Last resort: ffmpeg — slower but has native ATRAC9 support.
    print("  WARNING: Falling back to ffmpeg for decoding. This may take a little longer.")
    if _try_ffmpeg_decode(at9_path, wav_path, status_callback=status_callback):
        return True

    _log_error('convert_at9_to_wav',
               "No working AT9 decoder found (tried at9tool, VGAudioCli, and ffmpeg)")
    return False


def convert_to_at9(input_path, at9_path, loop_start_sample=None):
    """Convert WAV/AT9 to AT9 using at9tool (required for encoding)."""
    print(f"Converting {input_path} to {at9_path} (Loop: {loop_start_sample})")
    if input_path.lower().endswith(".at9"):
        if at9_path.lower() != input_path.lower():
            shutil.copy(input_path, at9_path)
        return True

    if not os.path.exists(input_path):
        _log_error('convert_to_at9', f"Source file not found: {input_path}")
        return False

    # at9tool is user-provided, lives outside _MEIPASS
    at9_tools = _at9tool_dir()
    at9tool_path = os.path.join(at9_tools, 'at9tool.exe')

    if not os.path.exists(at9tool_path):
        _log_error('convert_to_at9',
                    f"at9tool.exe not found in {at9_tools}. {AT9TOOL_REQUIRED_MSG}")
        return False

    # Normalise any source format to a 16-bit WAV for at9tool
    # (soundfile → ffmpeg → plain-WAV passthrough).
    tmp_wav = os.path.join(at9_tools, 'BGM.wav')
    samplerate = _normalise_to_pcm16_wav(input_path, tmp_wav)
    if samplerate is None:
        _log_error('convert_to_at9',
                   "Cannot encode audio: soundfile/libsndfile couldn't load on this "
                   "platform (e.g. ARM64 Windows), ffmpeg was not found, and the source "
                   "isn't a 16-bit PCM WAV. Install ffmpeg (on PATH), or supply a 16-bit "
                   "PCM WAV, to convert audio to AT9 here.")
        return False

    if samplerate > 24000:
        samplerate = 48000
    elif samplerate > 12000:
        samplerate = 24000
    else:
        samplerate = 12000

    try:
        cmd_prefix = _at9tool_cmd(at9tool_path)
        env = _wine_env() if platform.system() != 'Windows' else None
        if (loop_start_sample is not None) and (loop_start_sample > 0) and (loop_start_sample != ''):
            import wave
            with wave.open(tmp_wav, 'rb') as _w:
                samples = _w.getnframes() - 1
            cmd = cmd_prefix + ['-e', '-br', '144', '-fs', str(samplerate),
                                '-loop', str(loop_start_sample), str(samples), 'BGM.wav', at9_path]
        else:
            cmd = cmd_prefix + ['-e', '-br', '144', '-fs', str(samplerate),
                                '-wholeloop', 'BGM.wav', at9_path]

        use_new_session = platform.system() != 'Windows'
        r = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=at9_tools,
            env=env, start_new_session=use_new_session, **_SUBPROCESS_FLAGS
        )
        _clean_wine_runtime_cache()

        if os.path.exists(tmp_wav):
            os.remove(tmp_wav)

        if r.returncode != 0:
            stderr_text = r.stderr.decode(errors='replace').strip() if r.stderr else ''
            stdout_text = r.stdout.decode(errors='replace').strip() if r.stdout else ''
            _log_wine_error('at9tool_encode', cmd, r.returncode,
                            f"stderr: {stderr_text}\nstdout: {stdout_text}",
                            wine_path=cmd[0] if len(cmd) > 1 else None, env=env)
            return False
        return True
    except FileNotFoundError as e:
        _clean_wine_runtime_cache()
        if platform.system() != 'Windows':
            _log_error('at9tool_encode',
                        "Wine not found. at9tool.exe requires Wine on non-Windows platforms.", exc=e)
        else:
            _log_error('at9tool_encode', "at9tool.exe not found.", exc=e)
        return False
    except Exception as e:
        _clean_wine_runtime_cache()
        _log_error('at9tool_encode', f"Conversion error: {e}", exc=e)
        return False

def removeTemp(file):
    if os.path.exists(file):
        # Using QDir.toNativeSeparators to ensure path is correct for OS
        os.remove(QDir.toNativeSeparators(file))
        print(f"Temporary file deleted: {file}")

class AudioPlayerDialog(QDialog):
    def __init__(self, wav_path, theme_name, parent=None):
        super().__init__(parent)
        self.wav_path = wav_path
        self.setWindowTitle(f"🔊 Playing BGM - {theme_name}")
        self.setMinimumWidth(400)

        # --- VLC Initialization with Options ---
        if not _ensure_vlc():
            QMessageBox.critical(self, "VLC Error",
                "VLC is not installed or could not be loaded.\n\n"
                "Please install VLC media player to enable audio playback.")
            self.close()
            return

        # Build VLC instance arguments.
        # --plugin-path: tell libvlc where our bundled plugins are (mirrors
        #   what VLC_PLUGIN_PATH does, but passed directly to libvlc_new so
        #   it takes effect even if the env var is set after library load).
        # --aout=auhal: force macOS CoreAudio (AUHAL) output; without this
        #   libvlc may fall back to the dummy audio output when plugin
        #   discovery finds both adummy and auhal.
        _vlc_args = ['--no-video', '--file-caching=300']
        _plugin_dir = os.environ.get('PYTHON_VLC_MODULE_PATH') or os.environ.get('VLC_PLUGIN_PATH')
        if _plugin_dir and os.path.isdir(_plugin_dir):
            _vlc_args.append(f'--plugin-path={_plugin_dir}')
        if sys.platform == 'darwin':
            _vlc_args.append('--aout=auhal')
        self.vlc_instance = vlc.Instance(*_vlc_args)
        if self.vlc_instance is None:
            QMessageBox.critical(self, "VLC Error",
                "Could not initialize VLC.\n\n"
                "Please ensure VLC media player is installed and its architecture "
                "(32/64-bit) matches your Python installation.")
            self.close()
            return
        self.player = self.vlc_instance.media_player_new()
        self.is_ready_to_play = False # Flag to manage initial play state
        # -------------------------------------

        layout = QVBoxLayout(self)

        # Controls and UI setup (no change to layout)
        controls_layout = QHBoxLayout()
        # ... (rest of controls layout setup is omitted for brevity)
        self.rewind_button = QPushButton()
        self.rewind_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaSeekBackward))
        self.rewind_button.clicked.connect(self.rewind)

        self.play_pause_button = QPushButton()
        self.play_pause_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.play_pause_button.clicked.connect(self.toggle_play_pause)

        self.fast_forward_button = QPushButton()
        self.fast_forward_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaSeekForward))
        self.fast_forward_button.clicked.connect(self.fast_forward)

        controls_layout.addWidget(self.rewind_button)
        controls_layout.addWidget(self.play_pause_button)
        controls_layout.addWidget(self.fast_forward_button)
        layout.addLayout(controls_layout)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.sliderMoved.connect(self.set_position_from_slider)
        self.slider.setRange(0, 1000)
        self.slider.setEnabled(False)
        layout.addWidget(self.slider)

        self.status_label = QLabel("00:00 / 00:00")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; "
            "font-size: 13px; color: #aaaacc; padding: 2px 0px;"
        )
        layout.addWidget(self.status_label)

        # Timer for updates
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.update_ui)

        # Connect VLC events
        self.events = self.player.event_manager()
        self.events.event_attach(vlc.EventType.MediaPlayerEndReached, self.handle_end_of_media)
        self.events.event_attach(vlc.EventType.MediaPlayerEncounteredError, self.handle_error)
        self.events.event_attach(vlc.EventType.MediaStateChanged, self._initial_play_check)

        # --- Initial Media Setup (NEW) ---
        self._load_media()
        # ---------------------------------

        self.timer.start()

    def _load_media(self):
        """Loads media and checks file existence."""
        media_source = self.wav_path

        if not os.path.exists(media_source):
             QMessageBox.critical(self, "Error", f"Media file not found: {media_source}")
             media = self.vlc_instance.media_new("")
             self.is_ready_to_play = False
        else:
             # Use vlc.Media object and set it to the player
             # QDir.toNativeSeparators handles potential issues with file paths
             # for vlc on Windows (using backslashes)
             media = self.vlc_instance.media_new(QDir.toNativeSeparators(media_source))
             self.player.set_media(media)
             self.is_ready_to_play = True

    def _initial_play_check(self, event):
        """
        Called on MediaStateChanged event.
        Used to automatically play once the media has parsed metadata and is ready.
        """
        if self.is_ready_to_play and self.player.get_state() == vlc.State.Playing:
            # Media is playing, set initial icon
            self.update_ui()
            # Detach this specific event handler after first play to prevent overhead
            self.events.event_detach(vlc.EventType.MediaStateChanged, self._initial_play_check)

    def showEvent(self, event):
        """Overrides showEvent to start playback once the dialog is fully displayed."""
        super().showEvent(event)
        # Center on parent window (clamped to the screen)
        from widgets import center_on_parent
        center_on_parent(self)
        if getattr(self, 'is_ready_to_play', False):
            # Start playback only after the window is fully initialized/visible
            self.slider.setEnabled(True)
            self.player.play()

    def toggle_play_pause(self):
        if not self.slider.isEnabled():
            self.slider.setEnabled(True)
        if self.player.is_playing():
            self.player.pause()
        elif self.is_ready_to_play:
            self.player.play()
        self.update_ui()

    def rewind(self):
        current_time_ms = self.player.get_time()
        duration_ms = self.player.get_length()
        if duration_ms > 0:
            new_time_ms = min(current_time_ms - 5000, duration_ms)
            new_position_frac = new_time_ms / duration_ms
            self.player.set_position(new_position_frac)
            self.update_ui()

    def fast_forward(self):
        current_time_ms = self.player.get_time()
        duration_ms = self.player.get_length()
        if duration_ms > 0:
            new_time_ms = min(current_time_ms + 5000, duration_ms)
            new_position_frac = new_time_ms / duration_ms
            self.player.set_position(new_position_frac)
            self.update_ui()

    def set_position_from_slider(self, value):
        position_frac = value / 1000.0
        self.player.set_position(position_frac)

    def update_ui(self):
        if self.player.is_playing():
            self.play_pause_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPause))
        else:
            self.play_pause_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))

        position_frac = self.player.get_position()
        if position_frac >= 0.0 and not self.slider.isSliderDown():
            self.slider.setValue(int(position_frac * 1000))

        current_ms = self.player.get_time()
        duration_ms = self.player.get_length()

        if duration_ms > 0:
            pos_s = current_ms // 1000
            dur_s = duration_ms // 1000
            self.status_label.setText(f"{pos_s // 60:02d}:{pos_s % 60:02d} / {dur_s // 60:02d}:{dur_s % 60:02d}")
        else:
            self.status_label.setText("00:00 / 00:00")

    def handle_end_of_media(self, event):
        """Called by the VLC event manager when media ends."""
        QTimer.singleShot(0, self.endOfMedia)
    def endOfMedia(self):
        self.player.stop()
        self.slider.setEnabled(False)
        self.player.set_position(0.0)
        self.slider.setValue(0)
        self.update_ui()
    def handle_error(self, event):
        """Called by the VLC event manager on a critical error."""
        error_message = f"VLC Error: Playback failed or media is invalid."
        QMessageBox.warning(self, "Playback Error", error_message)
        self.close()

    def closeEvent(self, event):
        if not hasattr(self, 'player'):
            super().closeEvent(event)
            return
        self.player.stop()
        self.timer.stop()

        # Explicitly release media and player resources
        self.player.set_media(None)
        try:
            if os.path.exists(self.wav_path):
                removeTemp(self.wav_path)
        except Exception as e:
            print(f"Error deleting temporary file {self.wav_path}: {e}")
        super().closeEvent(event)

import sys
import os
import platform
import shutil
import tempfile
import datetime
import traceback
import struct
from collections import namedtuple

import at9_native

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
    # source file to 16-bit WAV in convert_to_at9, which also has a stdlib WAV
    # fallback, so never let this block startup.
    soundfile = None
    SOUNDFILE_AVAILABLE = False
    print(f"soundfile unavailable ({_sf_err}); audio encoding falls back to "
          f"a stdlib WAV path.")

from PySide6.QtWidgets import (
    QDialog, QPushButton, QVBoxLayout, QHBoxLayout,
    QSlider, QLabel, QStyle, QMessageBox, QCheckBox
)
from PySide6.QtCore import Qt, QTimer, QDir

# Playback uses a small first-party libVLC binding (vlc_player), loaded lazily —
# only needed for AudioPlayerDialog playback, not for audio conversion — so this
# module never fails to import when libVLC is unavailable.
vlc_player = None

def _ensure_vlc():
    """Load the libVLC binding on first use. Returns True if playback is available."""
    global vlc_player
    if vlc_player is None:
        try:
            import vlc_player as _vp
            vlc_player = _vp
        except Exception as e:
            print(f"  VLC binding not available: {e}")
            return False
    return vlc_player.available()


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


# =============================================================================
# == Native codec availability
# =============================================================================
# AT9 encode/decode is handled in-process by the bundled first-party at9codec
# shared library (see at9_native.py). There is no external tool, and no Wine.

# Message shown when the native codec can't be loaded (should not happen in a
# normal build; only if the bundled library is missing/unloadable).
AT9_CODEC_UNAVAILABLE_MSG = (
    "The audio codec could not be loaded, so AT9 BGM can't be created or played "
    "on this system. This usually means the app's files are incomplete — "
    "reinstalling should fix it."
)

# BGM source formats (besides .at9) that can be encoded to AT9. Each is first
# converted to a temporary WAV in convert_to_at9, then encoded by the native
# codec. All are decoded by soundfile's bundled libsndfile; the codec resamples
# any non-Vita rate to a supported one itself (scaling loop points), so the
# sample positions the user sees (BGM loop start) are given in the source rate.
ENCODABLE_AUDIO_EXTS = ['.wav', '.flac', '.ogg', '.opus', '.mp3', '.aiff', '.aif']

def encoding_available():
    """True if AT9 BGM can be created on this system (the native codec loaded).

    With the bundled native codec this is normally always True; it mirrors the
    former find_at9tool() gate, which asked whether an encoder was present."""
    return at9_native.available()


# =============================================================================
# == Helper Functions (Audio Conversion)
# =============================================================================
def _normalise_to_pcm16_wav(input_path, tmp_wav):
    """Produce a 16-bit PCM WAV at tmp_wav from any supported source, trying,
    in order: soundfile (works on almost every platform), then a plain copy when
    the source is already an uncompressed PCM WAV (stdlib only — no deps).
    Returns the sample rate, or None if none worked."""
    # 1) soundfile — preferred; handles mp3/flac/ogg/wav/etc. everywhere its
    #    bundled libsndfile loads (all platforms except Windows-on-ARM).
    if SOUNDFILE_AVAILABLE:
        data, sr = soundfile.read(input_path)
        soundfile.write(tmp_wav, data, sr, subtype='PCM_16')
        return sr
    # 2) No deps (stdlib only): convert any uncompressed PCM WAV to 16-bit.
    #    Covers 8/16/24/32-bit PCM WAV in-program, so a WAV source always works
    #    even with no soundfile (e.g. Windows-ARM without a bundled libsndfile).
    if input_path.lower().endswith('.wav'):
        try:
            import wave
            with wave.open(input_path, 'rb') as w:
                if w.getcomptype() != 'NONE':
                    raise ValueError('compressed WAV needs soundfile')
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


# An AT9 file is a RIFF/WAVE container. Its true (un-looped) length and loop
# points are stored in standard chunks and can be read WITHOUT decoding:
#   fact  -> field[0] = total decoded samples (the true length);
#            field[1] = encoder delay in samples (priming, typically 256).
#   smpl  -> sample loops; each loop start/end is in AT9 sample coordinates,
#            which INCLUDE the encoder delay.
# Decoders that strip the delay (at9tool, VGAudioCli) emit audio aligned to
# sample 0, so loop points in the DECODED WAV are the AT9 values minus the delay.
# (at9tool prints exactly this: "Loop Position = [smpl.start-delay, total-1]".)
At9LoopInfo = namedtuple('At9LoopInfo', [
    'sample_rate',    # Hz
    'channels',
    'total_samples',  # true decoded length (fact field[0]); None if unknown
    'delay',          # encoder priming samples (fact field[1]); 0 if absent
    'has_loop',       # True when a forward loop is defined
    'loop_start',     # loop start in DECODED-WAV sample coords (>= 0)
    'loop_end',       # loop end in DECODED-WAV sample coords (== total_samples)
    'is_whole_loop',  # True when the loop covers the whole track (start == 0)
])


def read_at9_loop_info(at9_path):
    """Parse an AT9 (RIFF/WAVE) file's fmt/fact/smpl chunks and return an
    At9LoopInfo in decoded-WAV sample coordinates, or None if the file isn't a
    readable AT9/RIFF. Reads only the small header chunks (seeks over `data`),
    so it's cheap to call before decoding."""
    try:
        with open(at9_path, 'rb') as f:
            if f.read(4) != b'RIFF':
                return None
            f.read(4)  # RIFF size
            if f.read(4) != b'WAVE':
                return None
            sample_rate = channels = None
            total_samples = None
            delay = 0
            has_loop = False
            smpl_start = smpl_end = None
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                cid = hdr[:4]
                csize = struct.unpack('<I', hdr[4:8])[0]
                if cid == b'fmt ':
                    body = f.read(csize)
                    if len(body) >= 16:
                        (_tag, channels, sample_rate,
                         _avg, _align, _bits) = struct.unpack('<HHIIHH', body[:16])
                elif cid == b'fact':
                    body = f.read(csize)
                    if len(body) >= 4:
                        total_samples = struct.unpack('<I', body[:4])[0]
                    if len(body) >= 8:
                        delay = struct.unpack('<I', body[4:8])[0]
                elif cid == b'smpl':
                    body = f.read(csize)
                    if len(body) >= 36:
                        num_loops = struct.unpack('<I', body[28:32])[0]
                        if num_loops >= 1 and len(body) >= 36 + 24:
                            # first loop: cueId, type, start, end, fraction, playCount
                            _cue, _ltype, smpl_start, smpl_end = struct.unpack(
                                '<IIII', body[36:52])
                            has_loop = True
                else:
                    # Skip this chunk's body (plus RIFF word padding).
                    f.seek(csize + (csize & 1), os.SEEK_CUR)
                    continue
                if csize & 1:
                    f.seek(1, os.SEEK_CUR)  # pad byte after an odd-size body

            if sample_rate is None:
                return None

            loop_start = loop_end = None
            is_whole_loop = False
            if has_loop and smpl_start is not None:
                loop_start = max(0, smpl_start - delay)
                # The loop always ends at the true end of the audio.
                loop_end = total_samples if total_samples else (smpl_end - delay + 1)
                is_whole_loop = (loop_start == 0)
            return At9LoopInfo(
                sample_rate=sample_rate, channels=channels,
                total_samples=total_samples, delay=delay,
                has_loop=has_loop, loop_start=loop_start, loop_end=loop_end,
                is_whole_loop=is_whole_loop,
            )
    except Exception as e:
        _log_error('read_at9_loop_info', f"failed to parse {at9_path}: {e}", exc=e)
        return None


def _make_progress(status_callback, verb):
    """Build a (done, total) progress reporter that posts a throttled percentage
    to status_callback, or None when there's nothing to report to. Exceptions in
    the UI callback are swallowed so progress never breaks a conversion."""
    if not status_callback:
        return None
    last = [-1]

    def report(done, total):
        if total <= 0:
            return
        pct = int(done * 100 / total)
        if pct != last[0]:
            last[0] = pct
            try:
                status_callback(verb, pct)
            except Exception:
                pass

    return report


def _warn_if_bgm_too_large(at9_path, status_callback=None):
    """Warn (log + status) if an AT9 BGM exceeds the PS Vita's theme-BGM size
    limit, which the console won't use. Returns True if it's within the limit.
    Called after encoding and for a user-supplied .at9 passed through as-is."""
    try:
        size = os.path.getsize(at9_path)
    except OSError:
        return True
    if size <= at9_native.VITA_BGM_MAX_BYTES:
        return True
    msg = ("This background music is larger than the PS Vita's AT9 size limit "
           f"(about 5 MB) at {size / (1024 * 1024):.1f} MB, so the Vita may not "
           "use it. Use a shorter track.")
    _log_error('convert_to_at9', msg)
    if status_callback:
        status_callback(msg)
    return False


def convert_at9_to_wav(at9_path, wav_path, status_callback=None):
    """Decode AT9 to a 16-bit PCM WAV with the bundled native codec.

    The native decoder returns the true, un-looped length directly (no trailing
    loop to trim). status_callback: optional callable(str) for progress text.
    """
    print(f"Converting {at9_path} to {wav_path}")
    if at9_path.lower().endswith(".wav"):
        if at9_path != wav_path:
            shutil.copy(at9_path, wav_path)
        return True

    if not os.path.exists(at9_path):
        _log_error('convert_at9_to_wav', f"Source file not found: {at9_path}")
        return False

    if not at9_native.available():
        _log_error('convert_at9_to_wav',
                   "Native at9codec library not available — cannot decode AT9.")
        if status_callback:
            status_callback(AT9_CODEC_UNAVAILABLE_MSG)
        return False

    try:
        if at9_native.decode_to_wav(at9_path, wav_path,
                                    progress=_make_progress(status_callback, "Decoding audio…")):
            return True
        _log_error('convert_at9_to_wav', f"Native AT9 decode failed for {at9_path}")
    except Exception as e:
        _log_error('convert_at9_to_wav', f"Native AT9 decode error: {e}", exc=e)
    return False


def convert_to_at9(input_path, at9_path, loop_start_sample=None, status_callback=None):
    """Encode WAV/other audio (or pass through .at9) to AT9 with the native codec.

    loop_start_sample: None/0/'' -> whole-track loop; a positive sample index ->
    loop from there to the end. The source is normalised to a 16-bit WAV and, if
    its rate isn't a Vita AT9 rate (48/24/12 kHz), resampled (loop index scaled).
    """
    print(f"Converting {input_path} to {at9_path} (Loop: {loop_start_sample})")
    if input_path.lower().endswith(".at9"):
        if at9_path.lower() != input_path.lower():
            shutil.copy(input_path, at9_path)
        # A user-supplied .at9 is used as-is; still warn if it's over the limit.
        _warn_if_bgm_too_large(at9_path, status_callback)
        return True

    if not os.path.exists(input_path):
        _log_error('convert_to_at9', f"Source file not found: {input_path}")
        return False

    if not at9_native.available():
        _log_error('convert_to_at9',
                   "Native at9codec library not available — cannot encode AT9.")
        if status_callback:
            status_callback(AT9_CODEC_UNAVAILABLE_MSG)
        return False

    tmp_wav = os.path.join(tempfile.gettempdir(), 'vta_bgm_encode.wav')
    try:
        samplerate = _normalise_to_pcm16_wav(input_path, tmp_wav)
        if samplerate is None:
            _log_error('convert_to_at9',
                       "Cannot read source audio: soundfile/libsndfile couldn't load on "
                       "this platform (e.g. ARM64 Windows) and the source isn't an "
                       "uncompressed PCM WAV. Supply a WAV, or a build with libsndfile.")
            return False

        # Whole-track loop unless an explicit (positive) loop start is given.
        if (loop_start_sample is not None) and (loop_start_sample != '') \
                and (int(loop_start_sample) > 0):
            loop_start = int(loop_start_sample)
        else:
            loop_start = 0  # 0 = whole-track loop (matches the old -wholeloop)

        # The codec resamples a non-Vita rate (48/24/12 kHz) to a supported one
        # itself and scales the loop point, so we pass the WAV at its native rate
        # and the loop start in that rate's samples.

        # Encode theme BGM at the best bitrate that fits the Vita's size limit:
        # with no bitrate given the codec defaults to auto — it starts at the top
        # standard rate (192 kbps stereo / 96 kbps mono) and steps down to the
        # highest standard rate whose file stays under max_bytes. So a short/medium
        # track gets 192 kbps and only long tracks drop (144, then lower) to stay
        # under the 5 MB ceiling. The rate is chosen by calculation in a single
        # pass, so starting high costs no extra encode time. max_bytes is the
        # console size limit's safety net.
        if at9_native.encode_wav_to_at9(
                tmp_wav, at9_path, loop_start=loop_start,
                max_bytes=at9_native.VITA_BGM_MAX_BYTES,
                progress=_make_progress(status_callback, "Encoding audio…")):
            # If the track is so long it overflows even at the lowest bitrate, warn.
            _warn_if_bgm_too_large(at9_path, status_callback)
            return True
        _log_error('convert_to_at9', "Native AT9 encode failed.")
        return False
    except Exception as e:
        _log_error('convert_to_at9', f"Conversion error: {e}", exc=e)
        return False
    finally:
        if os.path.exists(tmp_wav):
            try:
                os.remove(tmp_wav)
            except OSError:
                pass

def removeTemp(file):
    if os.path.exists(file):
        # Using QDir.toNativeSeparators to ensure path is correct for OS
        os.remove(QDir.toNativeSeparators(file))
        print(f"Temporary file deleted: {file}")

# When looping is enabled, seek back to the loop point this many ms before the
# true end. The UI timer polls every 200 ms, so this guard must exceed one tick
# to catch the end reliably; the small tail skipped is why this best-effort VLC
# loop isn't seamless (sample-accurate looping needs raw PCM — v2 Phase 2).
_LOOP_GUARD_MS = 300


class AudioPlayerDialog(QDialog):
    def __init__(self, wav_path, theme_name, parent=None, loop_info=None):
        super().__init__(parent)
        self.wav_path = wav_path
        # loop_info: an audio.At9LoopInfo (or None). Drives the loop checkbox —
        # the fraction of the track to jump back to at the end.
        self._loop_info = loop_info
        self._loop_enabled = False
        self._loop_frac = self._compute_loop_fraction(loop_info)
        self.setWindowTitle(f"🔊 Playing BGM - {theme_name}")
        self.setMinimumWidth(400)
        # The caller checks init_ok and only exec()s the dialog when it is True;
        # on a failure path it stays False so the player window never shows.
        self.init_ok = True

        # --- VLC Initialization with Options ---
        if not _ensure_vlc():
            QMessageBox.critical(self, "VLC Error",
                "VLC is not installed or could not be loaded.\n\n"
                "Please install VLC media player to enable audio playback.")
            self.init_ok = False
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
        try:
            self.player = vlc_player.VlcPlayer(_vlc_args)
        except Exception as _e:
            QMessageBox.critical(self, "VLC Error",
                "Could not initialize VLC.\n\n"
                f"Audio playback is unavailable: {_e}")
            self.player = None
            self.init_ok = False
            return
        self.is_ready_to_play = False  # Flag to manage initial play state
        self._ended = False            # set when playback reaches the end
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

        # Loop checkbox — mimics the Vita's AT9 looping. When enabled, playback
        # jumps back to the BGM loop point (read from the AT9) at the end instead
        # of stopping. The total-length display is unchanged.
        self.loop_checkbox = QCheckBox("🔁 Loop (mimic Vita BGM looping)")
        if loop_info is not None and loop_info.has_loop and loop_info.loop_start:
            # Partial loop: tell the user where it jumps back to.
            loop_s = loop_info.loop_start // loop_info.sample_rate
            self.loop_checkbox.setToolTip(
                f"Loops back to {loop_s // 60:02d}:{loop_s % 60:02d} (the BGM loop "
                f"point) at the end.\nBest-effort seek — a seamless, gap-free loop "
                f"is planned for a later version.")
        else:
            self.loop_checkbox.setToolTip(
                "Loops the whole track at the end.\nBest-effort seek — a seamless, "
                "gap-free loop is planned for a later version.")
        self.loop_checkbox.toggled.connect(self._on_loop_toggled)
        layout.addWidget(self.loop_checkbox)

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

        # End-of-media and errors are detected by polling the player state in
        # update_ui (the 200 ms timer), not via libVLC event callbacks (which
        # would fire on VLC's own thread).

        # --- Initial Media Setup (NEW) ---
        self._load_media()
        # ---------------------------------

        self.timer.start()

    @staticmethod
    def _compute_loop_fraction(loop_info):
        """Fraction (0.0–1.0) of the track to seek back to when looping.
        Uses the AT9 loop point when known, else 0.0 (loop the whole track)."""
        if (loop_info is not None and loop_info.total_samples
                and loop_info.loop_start is not None and loop_info.loop_start > 0):
            return max(0.0, min(1.0, loop_info.loop_start / loop_info.total_samples))
        return 0.0

    def _on_loop_toggled(self, checked):
        self._loop_enabled = bool(checked)

    def _loop_now(self):
        """Seek back to the loop point and keep playing (best-effort VLC loop)."""
        self.player.set_position(self._loop_frac)
        if not self.player.is_playing():
            self.player.play()
        self._ended = False

    def _load_media(self):
        """Loads media and checks file existence."""
        media_source = self.wav_path
        if not os.path.exists(media_source):
            QMessageBox.critical(self, "Error", f"Media file not found: {media_source}")
            self.is_ready_to_play = False
            return
        # QDir.toNativeSeparators handles file paths for libvlc on Windows.
        self.is_ready_to_play = self.player.load(QDir.toNativeSeparators(media_source))

    def showEvent(self, event):
        """Overrides showEvent to start playback once the dialog is fully displayed."""
        super().showEvent(event)
        # Center on parent window (clamped to the screen)
        from widgets import center_on_parent
        center_on_parent(self)
        if getattr(self, 'is_ready_to_play', False):
            # Start playback only after the window is fully initialized/visible
            self.slider.setEnabled(True)
            self._ended = False
            self.player.play()

    def toggle_play_pause(self):
        if not self.slider.isEnabled():
            self.slider.setEnabled(True)
        if self.player.is_playing():
            self.player.pause()
        elif self.is_ready_to_play:
            self._ended = False
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
        if getattr(self, 'player', None) is None:
            return
        # Poll for end-of-media / error (replaces libVLC event callbacks).
        state = self.player.state()
        if state == vlc_player.STATE_ENDED and not self._ended:
            # If we reached the true end before the proactive loop-seek caught it,
            # restart at the loop point (best-effort — there will be a brief gap).
            if self._loop_enabled:
                self._loop_now()
                return
            self._ended = True
            self.endOfMedia()
            return
        if state == vlc_player.STATE_ERROR:
            self.timer.stop()
            self.handle_error()
            return
        if self.player.is_playing():
            self.play_pause_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPause))
        else:
            self.play_pause_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))

        position_frac = self.player.get_position()
        if position_frac >= 0.0 and not self.slider.isSliderDown():
            self.slider.setValue(int(position_frac * 1000))

        current_ms = self.player.get_time()
        duration_ms = self.player.get_length()

        # Proactively loop back a little before the true end, so playback never
        # stops (smoother than waiting for STATE_ENDED). Not sample-accurate — a
        # gap-free loop is a later (PCM-based) feature.
        if (self._loop_enabled and duration_ms > 0 and current_ms >= 0
                and (duration_ms - current_ms) <= _LOOP_GUARD_MS):
            self._loop_now()
            current_ms = self.player.get_time()

        if duration_ms > 0:
            pos_s = current_ms // 1000
            dur_s = duration_ms // 1000
            self.status_label.setText(f"{pos_s // 60:02d}:{pos_s % 60:02d} / {dur_s // 60:02d}:{dur_s % 60:02d}")
        else:
            self.status_label.setText("00:00 / 00:00")

    def endOfMedia(self):
        self.player.stop()
        self.slider.setEnabled(False)
        self.player.set_position(0.0)
        self.slider.setValue(0)
        self.update_ui()
    def handle_error(self, event=None):
        """Called when playback reaches an error state (polled in update_ui)."""
        QMessageBox.warning(self, "Playback Error",
                            "VLC Error: Playback failed or media is invalid.")
        self.close()

    def closeEvent(self, event):
        if getattr(self, 'player', None) is None:
            super().closeEvent(event)
            return
        self.timer.stop()
        # Stop and release the player (frees libVLC resources).
        self.player.release()
        try:
            if os.path.exists(self.wav_path):
                removeTemp(self.wav_path)
        except Exception as e:
            print(f"Error deleting temporary file {self.wav_path}: {e}")
        super().closeEvent(event)

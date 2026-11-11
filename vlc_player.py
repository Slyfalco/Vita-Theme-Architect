"""Minimal first-party libVLC binding for BGM preview playback.

This replaces the python-vlc package with a tiny ctypes wrapper around just the
handful of libVLC calls the app needs, so this glue is first-party code (compiled
into release builds and covered by the integrity/fingerprint protection) rather
than a shipped third-party module. The native libVLC library itself (LGPL) is
still loaded dynamically from the bundled VLC and remains replaceable.

Only what AudioPlayerDialog uses is implemented: create/load/play/pause/stop,
is_playing, state, and time/length/position getters + seek. End-of-media and
errors are detected by polling state() (STATE_ENDED / STATE_ERROR) rather than
libVLC event callbacks, which keeps this binding simple and thread-safe.
"""

import ctypes
import os
import sys

# libvlc_state_t
STATE_NOTHING = 0
STATE_OPENING = 1
STATE_BUFFERING = 2
STATE_PLAYING = 3
STATE_PAUSED = 4
STATE_STOPPED = 5
STATE_ENDED = 6
STATE_ERROR = 7

_lib = None
_lib_error = None


def _lib_names():
    if sys.platform == 'win32':
        return 'libvlc.dll', 'libvlccore.dll'
    if sys.platform == 'darwin':
        return 'libvlc.dylib', 'libvlccore.dylib'
    return 'libvlc.so', 'libvlc.so.5', 'libvlccore.so'


def _candidates():
    """Yield possible libvlc locations, best first: the env override that audio.py
    sets for a bundled VLC, then the system install (registry / known folders), so
    an installed VLC is found even when nothing is bundled (e.g. source runs)."""
    p = (os.environ.get('PYTHON_VLC_LIB_PATH') or '').strip()
    if p:
        yield p
    if sys.platform == 'win32':
        try:
            import winreg
            for root, extra in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                                (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                                (winreg.HKEY_CURRENT_USER, 0)):
                try:
                    with winreg.OpenKey(root, r'SOFTWARE\VideoLAN\VLC', 0,
                                        winreg.KEY_READ | extra) as k:
                        d = winreg.QueryValueEx(k, 'InstallDir')[0]
                        if d:
                            yield os.path.join(d, 'libvlc.dll')
                except OSError:
                    pass
        except Exception:
            pass
        for env in ('ProgramFiles', 'ProgramW6432', 'ProgramFiles(x86)'):
            base = os.environ.get(env)
            if base:
                yield os.path.join(base, 'VideoLAN', 'VLC', 'libvlc.dll')
    elif sys.platform == 'darwin':
        for base in ('/Applications/VLC.app/Contents/MacOS/lib',
                     os.path.expanduser('~/Applications/VLC.app/Contents/MacOS/lib')):
            yield os.path.join(base, 'libvlc.dylib')
    else:
        for base in ('/usr/lib', '/usr/local/lib', '/usr/lib/x86_64-linux-gnu',
                     '/usr/lib64'):
            yield os.path.join(base, 'libvlc.so.5')
            yield os.path.join(base, 'libvlc.so')


def _locate():
    """First existing libvlc path from the candidates, else the bare library name
    (which the OS loader may still resolve via PATH / ldconfig)."""
    for c in _candidates():
        if c and os.path.isfile(c):
            return c
    return _lib_names()[0]


def _configure(lib):
    c = ctypes
    VP = c.c_void_p
    sigs = {
        'libvlc_new': (VP, [c.c_int, c.c_void_p]),
        'libvlc_release': (None, [VP]),
        'libvlc_media_new_path': (VP, [VP, c.c_char_p]),
        'libvlc_media_release': (None, [VP]),
        'libvlc_media_player_new': (VP, [VP]),
        'libvlc_media_player_release': (None, [VP]),
        'libvlc_media_player_set_media': (None, [VP, VP]),
        'libvlc_media_player_play': (c.c_int, [VP]),
        'libvlc_media_player_set_pause': (None, [VP, c.c_int]),
        'libvlc_media_player_stop': (None, [VP]),
        'libvlc_media_player_is_playing': (c.c_int, [VP]),
        'libvlc_media_player_get_state': (c.c_int, [VP]),
        'libvlc_media_player_get_time': (c.c_longlong, [VP]),
        'libvlc_media_player_get_length': (c.c_longlong, [VP]),
        'libvlc_media_player_get_position': (c.c_float, [VP]),
        'libvlc_media_player_set_position': (None, [VP, c.c_float]),
    }
    for name, (restype, argtypes) in sigs.items():
        fn = getattr(lib, name)
        fn.restype = restype
        fn.argtypes = argtypes


def _get_lib():
    global _lib, _lib_error
    if _lib is not None:
        return _lib
    if _lib_error is not None:
        raise _lib_error
    try:
        path = _locate()
        folder = os.path.dirname(path) if os.path.isabs(path) else None
        if folder:
            # Preload libvlccore from the same folder so libvlc resolves it, and
            # add the folder to the DLL search path on Windows.
            if sys.platform == 'win32' and hasattr(os, 'add_dll_directory'):
                try:
                    os.add_dll_directory(folder)
                except OSError:
                    pass
            core = _lib_names()[1]
            core_path = os.path.join(folder, core)
            if os.path.isfile(core_path):
                try:
                    ctypes.CDLL(core_path)
                except OSError:
                    pass
        lib = ctypes.CDLL(path)
        _configure(lib)
        _lib = lib
        return lib
    except OSError as e:
        _lib_error = e
        raise


def available():
    """True if libVLC can be loaded (so playback is possible)."""
    try:
        return _get_lib() is not None
    except Exception:
        return False


class VlcPlayer:
    """A single-media audio player over libVLC."""

    def __init__(self, args=None):
        lib = _get_lib()
        self._lib = lib
        args = list(args or [])
        if args:
            arr = (ctypes.c_char_p * len(args))(*[a.encode('utf-8') for a in args])
            argv = ctypes.cast(arr, ctypes.c_void_p)
        else:
            argv = None
        self._inst = lib.libvlc_new(len(args), argv)
        if not self._inst:
            raise RuntimeError('libvlc_new failed')
        self._mp = lib.libvlc_media_player_new(self._inst)
        if not self._mp:
            raise RuntimeError('libvlc_media_player_new failed')

    def load(self, path):
        """Set the media to the given file path. Returns True on success."""
        media = self._lib.libvlc_media_new_path(self._inst, os.fspath(path).encode('utf-8'))
        if not media:
            return False
        self._lib.libvlc_media_player_set_media(self._mp, media)
        self._lib.libvlc_media_release(media)
        return True

    def play(self):
        """Start or resume playback. Returns True on success."""
        return self._lib.libvlc_media_player_play(self._mp) == 0

    def pause(self):
        """Pause playback (does not toggle)."""
        self._lib.libvlc_media_player_set_pause(self._mp, 1)

    def stop(self):
        if self._mp:
            self._lib.libvlc_media_player_stop(self._mp)

    def is_playing(self):
        return bool(self._lib.libvlc_media_player_is_playing(self._mp))

    def state(self):
        return int(self._lib.libvlc_media_player_get_state(self._mp))

    def get_time(self):
        """Current time in milliseconds (-1 if unavailable)."""
        return int(self._lib.libvlc_media_player_get_time(self._mp))

    def get_length(self):
        """Media length in milliseconds (0/-1 if unknown)."""
        return int(self._lib.libvlc_media_player_get_length(self._mp))

    def get_position(self):
        """Playback position as a fraction 0.0–1.0 (-1.0 if unavailable)."""
        return float(self._lib.libvlc_media_player_get_position(self._mp))

    def set_position(self, fraction):
        self._lib.libvlc_media_player_set_position(self._mp, ctypes.c_float(fraction))

    def release(self):
        try:
            if self._mp:
                self._lib.libvlc_media_player_stop(self._mp)
                self._lib.libvlc_media_player_release(self._mp)
                self._mp = None
            if self._inst:
                self._lib.libvlc_release(self._inst)
                self._inst = None
        except Exception:
            pass

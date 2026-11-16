"""Thin ctypes binding to the bundled at9codec shared library.

at9codec is a first-party, clean-room ATRAC9 (AT9) decoder/encoder built as a
small C-ABI shared library (`at9codec.dll` / `libat9codec.so` / `.dylib`). It
replaces the former external audio toolchain (the Windows-only command-line
tools that had to run under a bundled Wine on macOS/Linux) with in-process,
native decode and encode on every platform.

This module loads the library, exposes decode/encode that read and write ordinary
files, and surfaces an optional progress callback. It is import-safe: if the
library can't be found or loaded, `available()` returns False and callers fall
back to reporting an error (there is no longer any other codec to fall back to).
"""

import ctypes
import os
import platform
import sys
import wave

# --- status codes (mirror native/at9codec.h) ---
AT9_OK = 0

# Theme-BGM size caps (mirror native/at9codec.h). The Vita won't use a theme AT9
# larger than ~5 MB; a theme that must also run on PS4/PS5 is capped at 2 MiB.
VITA_BGM_MAX_BYTES = 5 * 1024 * 1024
PS_CROSS_BGM_MAX_BYTES = 2 * 1024 * 1024

# Progress callback type: (user, done, total) -> None.
_ProgressCb = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32)

_lib = None
_load_tried = False


def _platform_tag():
    """`<os>-<arch>` for the running machine (matches setup_build._build_tag and
    audio._sndfile_platform_tag), so the right native subfolder is chosen."""
    os_name = {'Windows': 'windows', 'Darwin': 'macos', 'Linux': 'linux'}.get(
        platform.system(), platform.system().lower())
    machine = (platform.machine() or '').lower()
    arch = {'amd64': 'x64', 'x86_64': 'x64', 'x64': 'x64',
            'arm64': 'arm64', 'aarch64': 'arm64'}.get(machine, machine)
    return f"{os_name}-{arch}"


def _lib_filename():
    if sys.platform == 'win32':
        return 'at9codec.dll'
    if sys.platform == 'darwin':
        return 'libat9codec.dylib'
    return 'libat9codec.so'


def _candidate_paths():
    """Where the shared library may live: next to the exe (builds; setup_build
    bundles the matching arch, flat) or under `<repo>/native/<os>-<arch>/`
    (running from source, arch-tagged)."""
    name = _lib_filename()
    exedir = os.path.dirname(os.path.abspath(sys.executable))
    here = os.path.dirname(os.path.abspath(__file__))
    tag = _platform_tag()
    return [
        os.path.join(exedir, 'native', name),   # builds (flat, preferred)
        os.path.join(exedir, name),              # builds (beside exe)
        os.path.join(here, 'native', tag, name),  # source (per-arch)
    ]


def _bind(lib):
    """Declare the C signatures we use."""
    lib.at9_version.restype = ctypes.c_char_p
    lib.at9_version.argtypes = []

    lib.at9_decode_ex.restype = ctypes.c_int32
    lib.at9_decode_ex.argtypes = [
        ctypes.c_char_p, ctypes.c_size_t,                     # at9, len
        _ProgressCb, ctypes.c_void_p,                         # cb, user
        ctypes.POINTER(ctypes.POINTER(ctypes.c_int16)),       # out_pcm
        ctypes.POINTER(ctypes.c_size_t),                      # out_count
        ctypes.POINTER(ctypes.c_uint16),                      # out_channels
        ctypes.POINTER(ctypes.c_uint32),                      # out_sample_rate
    ]
    lib.at9_free_pcm.restype = None
    lib.at9_free_pcm.argtypes = [ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t]

    lib.at9_encode_wav_ex.restype = ctypes.c_int32
    lib.at9_encode_wav_ex.argtypes = [
        ctypes.c_char_p, ctypes.c_size_t,                     # wav, len
        ctypes.c_uint32,                                      # bitrate
        ctypes.c_int32,                                       # loop_start
        ctypes.c_size_t,                                      # max_bytes
        _ProgressCb, ctypes.c_void_p,                         # cb, user
        ctypes.POINTER(ctypes.POINTER(ctypes.c_uint8)),       # out_at9
        ctypes.POINTER(ctypes.c_size_t),                      # out_len
    ]
    lib.at9_free_at9.restype = None
    lib.at9_free_at9.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]


def _load():
    global _lib, _load_tried
    if _load_tried:
        return _lib
    _load_tried = True
    for path in _candidate_paths():
        if not os.path.isfile(path):
            continue
        try:
            if sys.platform == 'win32':
                # Let the loader resolve any sibling deps from the lib's dir.
                try:
                    os.add_dll_directory(os.path.dirname(path))
                except (OSError, AttributeError):
                    pass
            lib = ctypes.CDLL(path)
            _bind(lib)
            _lib = lib
            break
        except (OSError, AttributeError):
            continue
    return _lib


def available():
    """True if the native codec is loaded and usable."""
    return _load() is not None


def version():
    lib = _load()
    if lib is None:
        return None
    v = lib.at9_version()
    return v.decode('ascii', 'replace') if v else None


def _wrap_progress(progress):
    """Turn a Python callable(done, total) into a (keep-alive) C callback, or
    (None-callback, None) when no progress is wanted."""
    if progress is None:
        return ctypes.cast(None, _ProgressCb), None

    def _cb(_user, done, total):
        try:
            progress(int(done), int(total))
        except Exception:
            pass

    cb = _ProgressCb(_cb)
    return cb, cb  # second value kept by caller so the thunk isn't GC'd


class At9NativeError(RuntimeError):
    pass


def decode_to_wav(at9_path, wav_path, progress=None):
    """Decode an AT9 file to a 16-bit PCM WAV at the true, un-looped length.

    Returns True on success. Raises At9NativeError if the library is missing."""
    lib = _load()
    if lib is None:
        raise At9NativeError("at9codec native library not available")
    with open(at9_path, 'rb') as f:
        data = f.read()

    cb, _keep = _wrap_progress(progress)
    out_pcm = ctypes.POINTER(ctypes.c_int16)()
    out_count = ctypes.c_size_t()
    out_ch = ctypes.c_uint16()
    out_sr = ctypes.c_uint32()
    rc = lib.at9_decode_ex(
        data, len(data), cb, None,
        ctypes.byref(out_pcm), ctypes.byref(out_count),
        ctypes.byref(out_ch), ctypes.byref(out_sr),
    )
    if rc != AT9_OK:
        return False
    try:
        n = out_count.value
        pcm_bytes = ctypes.string_at(out_pcm, n * 2)  # int16 = 2 bytes
    finally:
        lib.at9_free_pcm(out_pcm, out_count)
    with wave.open(wav_path, 'wb') as w:
        w.setnchannels(int(out_ch.value))
        w.setsampwidth(2)
        w.setframerate(int(out_sr.value))
        w.writeframes(pcm_bytes)
    return True


def encode_wav_to_at9(wav_path, at9_path, loop_start=0, bitrate=0, max_bytes=0,
                      progress=None):
    """Encode a 16-bit PCM WAV (12/24/48 kHz) to an AT9 file.

    loop_start: 0 = whole-track loop, >0 = loop from that sample to the end,
    <0 = no loop. max_bytes: 0 = no size cap, else keep the output at or under
    that size (e.g. VITA_BGM_MAX_BYTES for theme BGM). Returns True on success.
    Raises At9NativeError if the library is missing."""
    lib = _load()
    if lib is None:
        raise At9NativeError("at9codec native library not available")
    with open(wav_path, 'rb') as f:
        data = f.read()

    cb, _keep = _wrap_progress(progress)
    out_at9 = ctypes.POINTER(ctypes.c_uint8)()
    out_len = ctypes.c_size_t()
    rc = lib.at9_encode_wav_ex(
        data, len(data), ctypes.c_uint32(bitrate), ctypes.c_int32(loop_start),
        ctypes.c_size_t(max_bytes), cb, None,
        ctypes.byref(out_at9), ctypes.byref(out_len),
    )
    if rc != AT9_OK:
        return False
    try:
        blob = ctypes.string_at(out_at9, out_len.value)
    finally:
        lib.at9_free_at9(out_at9, out_len)
    with open(at9_path, 'wb') as f:
        f.write(blob)
    return True

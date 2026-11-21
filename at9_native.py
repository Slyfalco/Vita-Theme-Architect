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
AT9_ERR_BITRATE = -5    # bitrate is not a standard rate (use 0 = auto)
AT9_ERR_TOO_LARGE = -6  # too long to fit the size limit at any standard bitrate

# --- size profiles (mirror native/at9codec.h) ---
# Name a console target and the codec applies its own byte cap for it, so the limit
# values live only in the codec (query profile_max_bytes() if you need the number).
AT9_SIZE_NONE = 0
AT9_SIZE_VITA_BGM = 1   # PS Vita system/theme BGM (under 5 MB)
AT9_SIZE_PS_CROSS = 2   # tighter 2 MiB cap for PS4/PS5 cross-support

# Progress callback type: (user, done, total) -> None.
_ProgressCb = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32)


class _DecodeOptions(ctypes.Structure):
    """Mirrors At9DecodeOptions in native/at9codec.h."""
    _fields_ = [
        ('struct_size', ctypes.c_uint32),
        ('progress', _ProgressCb),
        ('user', ctypes.c_void_p),
    ]


class _EncodeOptions(ctypes.Structure):
    """Mirrors At9EncodeOptions in native/at9codec.h."""
    _fields_ = [
        ('struct_size', ctypes.c_uint32),
        ('bitrate', ctypes.c_uint32),
        ('max_bytes', ctypes.c_size_t),
        ('loop_start', ctypes.c_int32),
        ('loop_end', ctypes.c_int32),
        ('progress', _ProgressCb),
        ('user', ctypes.c_void_p),
        ('size_profile', ctypes.c_uint32),
    ]


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

    lib.at9_decode.restype = ctypes.c_int32
    lib.at9_decode.argtypes = [
        ctypes.c_char_p, ctypes.c_size_t,                     # at9, len
        ctypes.POINTER(_DecodeOptions),                       # opts (NULL = defaults)
        ctypes.POINTER(ctypes.POINTER(ctypes.c_int16)),       # out_pcm
        ctypes.POINTER(ctypes.c_size_t),                      # out_count
        ctypes.POINTER(ctypes.c_uint16),                      # out_channels
        ctypes.POINTER(ctypes.c_uint32),                      # out_sample_rate
    ]
    lib.at9_free_pcm.restype = None
    lib.at9_free_pcm.argtypes = [ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t]

    lib.at9_encode_wav.restype = ctypes.c_int32
    lib.at9_encode_wav.argtypes = [
        ctypes.c_char_p, ctypes.c_size_t,                     # wav, len
        ctypes.POINTER(_EncodeOptions),                       # opts (NULL = defaults)
        ctypes.POINTER(ctypes.POINTER(ctypes.c_uint8)),       # out_at9
        ctypes.POINTER(ctypes.c_size_t),                      # out_len
    ]
    lib.at9_free_at9.restype = None
    lib.at9_free_at9.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]

    lib.at9_profile_max_bytes.restype = ctypes.c_size_t
    lib.at9_profile_max_bytes.argtypes = [ctypes.c_uint32]


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
    opts = _DecodeOptions(struct_size=ctypes.sizeof(_DecodeOptions), progress=cb, user=None)
    out_pcm = ctypes.POINTER(ctypes.c_int16)()
    out_count = ctypes.c_size_t()
    out_ch = ctypes.c_uint16()
    out_sr = ctypes.c_uint32()
    rc = lib.at9_decode(
        data, len(data), ctypes.byref(opts),
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


def profile_max_bytes(size_profile):
    """The codec's byte cap for a size profile (AT9_SIZE_*); 0 = no cap/unknown.
    Reads the limit from the library so callers never hard-code it. Raises
    At9NativeError if the library is missing."""
    lib = _load()
    if lib is None:
        raise At9NativeError("at9codec native library not available")
    return int(lib.at9_profile_max_bytes(size_profile))


class At9TooLargeError(At9NativeError):
    """Encoding failed because the track is too long to fit the size limit even at
    the lowest standard bitrate."""


def encode_wav_to_at9(wav_path, at9_path, loop_start=0, loop_end=0, bitrate=0,
                      size_profile=AT9_SIZE_NONE, max_bytes=0, progress=None):
    """Encode a WAV (12/24/48 kHz) to an AT9 file.

    loop_start: 0 = whole-track loop, >0 = loop from that sample, <0 = no loop.
    loop_end: 0 = to the track end, >0 = inclusive last sample of the loop.
    bitrate: 0 = auto (highest standard rate that fits the size limit); a non-zero
    value must be a standard rate or encoding fails.
    size_profile: AT9_SIZE_VITA_BGM / AT9_SIZE_PS_CROSS to let the codec apply its
    own console size cap; AT9_SIZE_NONE (default) uses max_bytes instead.
    max_bytes: custom size cap in bytes (ignored when size_profile is set).
    Returns True on success. Raises At9TooLargeError if the track can't fit the
    size limit, or At9NativeError if the library is missing."""
    lib = _load()
    if lib is None:
        raise At9NativeError("at9codec native library not available")
    with open(wav_path, 'rb') as f:
        data = f.read()

    cb, _keep = _wrap_progress(progress)
    opts = _EncodeOptions(
        struct_size=ctypes.sizeof(_EncodeOptions),
        bitrate=bitrate, max_bytes=max_bytes,
        loop_start=loop_start, loop_end=loop_end, progress=cb, user=None,
        size_profile=size_profile,
    )
    out_at9 = ctypes.POINTER(ctypes.c_uint8)()
    out_len = ctypes.c_size_t()
    rc = lib.at9_encode_wav(
        data, len(data), ctypes.byref(opts),
        ctypes.byref(out_at9), ctypes.byref(out_len),
    )
    if rc == AT9_ERR_TOO_LARGE:
        raise At9TooLargeError(
            "track is too long to fit the size limit at any standard bitrate")
    if rc != AT9_OK:
        return False
    try:
        blob = ctypes.string_at(out_at9, out_len.value)
    finally:
        lib.at9_free_at9(out_at9, out_len)
    with open(at9_path, 'wb') as f:
        f.write(blob)
    return True

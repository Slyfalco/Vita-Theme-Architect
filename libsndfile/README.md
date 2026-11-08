# Bundled native `libsndfile`

`soundfile` (used to normalise source audio to 16-bit WAV for AT9 encoding)
bundles its own `libsndfile` on every platform **except Windows-on-ARM** — PyPI
ships no `win-arm64` build, so on an ARM64 Windows box `import soundfile` fails
with `libsndfile.dll error 0x7e`.

To get full-format audio support (mp3/flac/ogg) there **in-app** (no ffmpeg),
drop a native `libsndfile` here and it is used automatically:

- **Source runs:** `audio._prepare_bundled_sndfile()` looks in
  `libsndfile/<os>-<arch>/` for this machine and preloads it before
  `import soundfile`.
- **Builds:** `setup_build.py` bundles only the building machine's arch folder,
  flattened to `libsndfile/` next to the exe.

## Layout

```
libsndfile/
  windows-arm64/
    libsndfile.dll        <- the main library (rename from sndfile.dll)
    FLAC.dll, ogg.dll, vorbis.dll, opus.dll, mpg123.dll, ...   <- its deps
    LICENSE-libsndfile.txt, LICENSE-FLAC.txt, ...              <- see Licensing
  <other-os-arch>/ ...    (only if that platform also lacks a wheel lib)
```

The folder name must match `setup_build._build_tag()` / `audio._sndfile_platform_tag()`
(e.g. `windows-arm64`, `windows-x64`, `linux-arm64`, `macos-arm64`). On Windows the
main file **must be named `libsndfile.dll`** (soundfile `dlopen`s that exact name).

## How to build it (once — it's independent of the app version)

No official prebuilt `win-arm64` libsndfile exists, so build it once with vcpkg
(can cross-compile from x64 — no ARM machine needed):

```
vcpkg install libsndfile:arm64-windows
```

Then copy from `vcpkg/installed/arm64-windows/bin/`:
- `sndfile.dll` → `libsndfile/windows-arm64/libsndfile.dll`
- every dependency DLL it pulled in (FLAC, ogg, vorbis, opus, mpg123, etc.)

Rebuild/run — that's it. You only redo this if you want a newer libsndfile.

## Licensing (safe to commit / ship)

`libsndfile` is **LGPL-2.1-or-later**; its format deps are permissive/LGPL
(FLAC, Ogg, Vorbis, Opus = BSD-style; mpg123 = LGPL). Because we ship them as
**separate DLLs loaded at runtime** (dynamic linking — the user can replace the
DLL), redistribution is allowed, including in a public/closed app. Just include
each library's license text in the arch folder next to the DLLs (that's what the
`LICENSE-*.txt` files above are for), and this notice documents the use.

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
    libsndfile.dll        <- the library (rename from sndfile.dll)
    LICENSE-*.txt         <- libsndfile + each dependency's license (see Licensing)
  <other-os-arch>/ ...    (only if that platform also lacks a wheel lib)
```

The folder name must match `setup_build._build_tag()` / `audio._sndfile_platform_tag()`
(e.g. `windows-arm64`, `windows-x64`, `linux-arm64`, `macos-arm64`). On Windows the
main file **must be named `libsndfile.dll`** (soundfile `dlopen`s that exact name).
The loader/bundler just take whatever files are in the folder, so a single
self-contained DLL (preferred) or a DLL + sidecar dep DLLs both work.

## How to build it (once — it's independent of the app version)

No official prebuilt `win-arm64` libsndfile exists, so build it once with vcpkg
(can cross-compile from x64 — no ARM machine needed).

### Preferred: one self-contained `libsndfile.dll` (deps linked in statically)

Build the format deps (FLAC/ogg/vorbis/opus/mpg123) as static libs linked **into**
libsndfile, while libsndfile itself stays a DLL — so you get a single file with no
sidecar DLLs. Make an overlay triplet `arm64-windows-mixed.cmake`:

```cmake
set(VCPKG_TARGET_ARCHITECTURE arm64)
set(VCPKG_CRT_LINKAGE dynamic)      # use the OS's UCRT (normal on Windows)
set(VCPKG_LIBRARY_LINKAGE static)   # deps static...
if(PORT STREQUAL "libsndfile")
    set(VCPKG_LIBRARY_LINKAGE dynamic)   # ...but libsndfile itself is a DLL
endif()
```

```
vcpkg install libsndfile:arm64-windows-mixed --overlay-triplets=<folder-with-that-triplet>
```

Copy `installed/arm64-windows-mixed/bin/sndfile.dll` →
`libsndfile/windows-arm64/libsndfile.dll`. That one file is everything.

### Alternative: plain dynamic build (a handful of small DLLs)

```
vcpkg install libsndfile:arm64-windows
```

Copy from `vcpkg/installed/arm64-windows/bin/`: `sndfile.dll` → `libsndfile.dll`,
plus every dependency DLL it pulled in (FLAC, ogg, vorbis, opus, mpg123, …).

Either way, rebuild/run — that's it. You only redo this for a newer libsndfile.

## Trust

This is bundled like the app's other native libs (Qt, VLC, the Python runtime):
whatever ships in the app/repo folder is trusted, and the real boundary is write
access to that folder — not this one file. So just commit a `libsndfile.dll` you
built yourself from the official sources. (Prefer the single self-contained DLL so
this folder exposes only the one library, with no extra sidecar DLLs.)

## Licensing (safe to commit / ship)

`libsndfile` is **LGPL-2.1-or-later**; its format deps are permissive/LGPL
(FLAC, Ogg, Vorbis, Opus = BSD-style; mpg123 = LGPL). Our app loads
`libsndfile.dll` as a **replaceable runtime DLL** (soundfile `dlopen`s it; a user
can swap it), which satisfies LGPL — so redistribution is allowed in a public or
closed app. This holds for **both** builds above: shipping the deps as separate
DLLs, or linking them statically **into** `libsndfile.dll` (the combined DLL is
still LGPL/BSD and still replaceable). Just include each library's license text
alongside the DLL (`LICENSE-*.txt`), and keep this notice. These are unmodified
upstream libraries, so their public sources + the vcpkg recipe are the source
reference.

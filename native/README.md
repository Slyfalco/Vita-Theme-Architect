# Native AT9 codec libraries

This folder holds the first-party **at9codec** shared library — the in-process,
clean-room ATRAC9 (AT9) decoder/encoder that replaced the old external audio
toolchain (and the bundled Wine on macOS/Linux). The app loads it at runtime via
`at9_native.py`, and `setup_build.py` bundles the building machine's arch into
`<app>/native/` beside the exe.

## Layout

One subfolder per platform tag (`<os>-<arch>`, matching `setup_build._build_tag()`):

```
native/
  at9codec.h            C header (reference)
  LICENSE-at9codec      at9codec's license (MIT)
  windows-x64/at9codec.dll
  windows-arm64/at9codec.dll
  linux-x64/libat9codec.so
  linux-arm64/libat9codec.so
  macos-x64/libat9codec.dylib
  macos-arm64/libat9codec.dylib
```

`at9_native.py` loads `at9codec.dll` / `libat9codec.so` / `libat9codec.dylib`
from the tag folder that matches the running machine.

## Where the libraries come from

The library is built from the separate **at9codec** repository (pure Rust, no C
dependencies). Its CI workflow (`.github/workflows/build-libs.yml`) builds every
platform/arch on its own native runner and publishes them as a GitHub release,
including a combined `at9codec-native-libs.zip` laid out exactly like the tree
above.

**To update the bundled libraries:** run that workflow, download
`at9codec-native-libs.zip` from the release, and extract its `native/` contents
over this folder (keep this `README.md`). Only the building machine's own arch is
needed to build/run locally; the full set is only needed to ship every platform.

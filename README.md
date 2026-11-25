# VitaTheme Architect

A desktop app for creating, editing, importing, downloading and installing custom themes for the PlayStation Vita. It runs on Windows, macOS and Linux.

> **v3** is a rebuild of the app in Rust with a new native interface (no Python, Qt or bundled browser). The theme format, your settings and your theme library carry over unchanged from v2.

## Features

- **Theme editor** — build a theme from wallpapers, icons, colours and background music, following the Vita's official theme format, with a live **PS Vita simulator** (lock screen, home pages, background waves, LiveArea, the theme's music and UI sounds).
- **Import & export** — bring in existing themes from a folder or `.zip`, and manage your local theme library.
- **Community browser** — browse, search and import themes shared by the community.
- **Submit to the repository** — upload your theme (or a new version of it) to the community repository, with your uploader key attached automatically.
- **Send to your Vita** — transfer themes straight to the console over Wi-Fi (FTP via VitaShell).
- **Import from your Vita** — pull themes back off the console.
- **Background music** — play AT9 natively (with its loop points) and convert audio (WAV, MP3, FLAC, OGG and more) to the Vita's AT9 format.
- **AI icon generator (optional)** — generate a matching set of app icons for a theme from its wallpapers. This is an optional component that downloads its own tools on first use; nothing heavy is bundled with the app.

## Download

Get the latest build from the [Releases page](https://github.com/Slyfalco/Vita-Theme-Architect/releases/latest), picking by **OS + architecture + edition**:

- **OS / architecture** — Windows, macOS or Linux, each in **x64** and **arm64**. Most PCs are x64; Apple Silicon Macs (M1/M2/M3…) and newer ARM devices are arm64.
- **Edition** — **AI** includes the AI icon generator; **Standard** is a smaller download without it.

Then:

- **Windows** — run the installer (`…-setup.exe`, or the small `…-online-setup.exe`) and choose your edition, or use the portable `.zip`.
- **macOS** — open the `.dmg` and drag the app to Applications, or use the portable `.zip`.
- **Linux** — extract the `.tar.gz` and run the app.

The in-app updater keeps you on the same edition and preserves your settings and library.

## Running from source

Requires a current stable [Rust](https://rustup.rs/) toolchain.

```bash
cargo run -p vta-app --release
```

- **Windows** — the MSVC build tools (Visual Studio Build Tools, "Desktop development with C++"). The repository submission window uses the system's WebView2 runtime, which Windows 10/11 already include.
- **macOS** — the Xcode command-line tools.
- **Linux** — GTK 3, WebKitGTK and ALSA development packages (for example on Debian/Ubuntu: `libgtk-3-dev libwebkit2gtk-4.1-dev libasound2-dev`).

> The AT9 codec comes from the separate [at9codec](https://github.com/Slyfalco/at9codec) repository, which Cargo fetches automatically. During v3 development the workspace may point at a local checkout instead (see the `[patch]` section in `Cargo.toml`); in that case, clone at9codec next to this repository.

Online features (community browser, repository submission) are available in official builds only.

## Building a standalone app

The `xtask` packager builds and packages the app for the current OS and architecture:

```bash
cargo run -p xtask --release -- package --edition ai        # with the AI icon generator
cargo run -p xtask --release -- package --edition standard  # without it
cargo run -p xtask --release -- package --edition both
```

The app folder and its `.zip` / `.tar.gz` are written to `dist/v3/`.

## Platform notes

- **Windows, macOS and Linux** are supported, each on **x64** and **arm64**.
- AT9 theme audio is handled by a first-party native codec built into the app — there are no external audio tools on any platform.

## Using it with your Vita

You'll need a Vita running custom firmware with **VitaShell**. In the app's **Settings → Connection** there's a step-by-step guide for starting VitaShell's FTP server and entering your Vita's IP address and port.

## License

VitaTheme Architect's own source code is released under the **VitaTheme Architect
License** (see [`LICENSE`](LICENSE)): you may use, study, modify, and build the
source, and redistribute the source and your modifications. The copyright
holder's **official binary releases**, the **name/branding**, and the **online
theme service** are reserved — official builds include integrity protection and
should not be modified or passed off as official, and unofficial builds are not
authorized to use the online service. Builds you compile yourself from source for
your own use are not restricted by those reservations.

> This is a summary, not legal advice.

## Third-party licenses

This app is built on open-source components that keep their own licenses, which
govern those components:

- **Slint** (user interface) — see the Slint project's licensing terms (https://slint.dev).
- **at9codec** (AT9 audio) — the project's own codec library; see its repository.
- **wry** (repository submission window) and other Rust crates such as **cpal**,
  **image**, **zip** and **ureq** — under their respective permissive open-source
  licenses (MIT / Apache-2.0 and similar).
- The optional AI icon generator downloads its own Python runtime and libraries
  (such as PyTorch and Diffusers) on first use; they are not bundled and keep their
  own licenses.

Each component's license applies to that component; see the respective projects
for full terms.

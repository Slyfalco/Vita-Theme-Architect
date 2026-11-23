# VitaTheme Architect

A desktop app for creating, editing, importing, downloading and installing custom themes for the PlayStation Vita. It runs on Windows, macOS and Linux.

## Features

- **Theme editor** — build a theme from wallpapers, icons, colours and background music, following the Vita's official theme format.
- **Import & export** — bring in existing themes from a folder or `.zip`, and manage your local theme library.
- **Download community themes** — browse and import themes shared by the community.
- **Send to your Vita** — transfer themes straight to the console over Wi-Fi (FTP via VitaShell).
- **Import from your Vita** — pull themes back off the console.
- **Background music** — preview and convert audio (WAV, MP3, FLAC, OGG and more) to the Vita's AT9 format.
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

Requires Python 3.12.

```bash
python -m pip install -r requirements.txt
python main.py
```

> Note: `requirements.txt` is UTF-16 encoded.

## Building a standalone app

The project builds a standalone, portable application with [Nuitka](https://nuitka.net/):

```bash
python setup_build.py                # standalone build for the current OS/architecture (includes the AI icon generator)
python setup_build.py --no-icon-ai  # build without the AI icon generator and its features
```

The build downloads the extra tools it needs (such as a portable media player) automatically, and outputs to `dist/`.

## Platform notes

- **Windows, macOS and Linux** are supported, each on **x64** and **arm64**.
- AT9 theme audio is handled by a first-party native codec built into the app — there are no external audio tools and no Wine dependency on any platform.

## Using it with your Vita

You'll need a Vita running custom firmware with **VitaShell**. In the app's **Settings → Connection** there's a step-by-step guide for starting VitaShell's FTP server and entering your Vita's IP address and port.

## Roadmap

A full UI overhaul is in the works, potentially including a change of the app's implementation language away from Python. The current feature set and theme format will carry over.

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

- **Qt** (used via **PySide6**) — **GNU LGPL v3**. Official builds ship the Qt
  libraries as separate, replaceable files; under the LGPL you may replace them
  with your own compatible build of Qt. The full LGPL-3.0 text and Qt's notices
  are available from the Qt Project (https://www.qt.io/licensing) and the Free
  Software Foundation (https://www.gnu.org/licenses/lgpl-3.0.html).
- **VLC / libVLC** (media playback) — LGPL.
- **libsndfile** and its bundled audio codec libraries (FLAC, Ogg, Vorbis, Opus,
  mpg123, LAME) — LGPL / BSD, under their respective licenses.
- Other libraries (NumPy, Pillow, requests, cryptography, and so on) are under
  their respective permissive open-source licenses.

Each component's license applies to that component; see the respective projects
for full terms.

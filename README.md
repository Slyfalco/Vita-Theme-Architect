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

The build downloads the extra tools it needs (such as the audio converter and a portable media player) automatically, and outputs to `dist/`.

## Platform notes

- **Windows / macOS / Linux** are all supported.
- On macOS and Linux, the Windows-only audio tools run through a bundled, stripped-down minimal build of Wine, set up automatically on first use.

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

- **Qt** (used via **PySide6**) — **GNU LGPL v3**. Official builds ship the Qt
  libraries as separate, replaceable files; under the LGPL you may replace them
  with your own compatible build of Qt. The full LGPL-3.0 text and Qt's notices
  are available from the Qt Project (https://www.qt.io/licensing) and the Free
  Software Foundation (https://www.gnu.org/licenses/lgpl-3.0.html).
- **VLC / libVLC** (media playback) — LGPL.
- **FFmpeg / libsndfile** (audio) — LGPL / BSD, where used.
- Other libraries (NumPy, Pillow, requests, cryptography, and so on) are under
  their respective permissive open-source licenses.

Each component's license applies to that component; see the respective projects
for full terms.

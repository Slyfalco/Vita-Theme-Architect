#!/usr/bin/env python3
"""
VitaTheme Architect - Build Setup Script

Downloads required dependencies, generates the release secure modules (release
builds only), and builds the project with Nuitka. Non-release builds have no
online features.

Usage:
    python setup_build.py [--no-icon-ai] [--no-package]

    --no-icon-ai   exclude the AI icon generator and all AI features from the
                   build (or set VTA_ICON_AI=0). It is included by default.
    --no-package   skip zipping the finished build into a release asset. By
                   default a successful build is packaged as
                   dist/vitaThemeArchitect-<os>-<arch>-<edition>.<ext>, the name
                   the in-app updater matches.
"""
import os
import sys
import platform
import shutil
import subprocess
import zipfile
import ssl
import urllib.request
import json
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.join(PROJECT_ROOT, 'Tools')
# Release builds: set VTA_RELEASE_SCRIPT on the build machine (path relative
# to the project root, or absolute). Unset means a non-release build.
_RELEASE_SCRIPT = os.environ.get('VTA_RELEASE_SCRIPT', '').strip()
# AI icon generator (on by default). Disable with --no-icon-ai or VTA_ICON_AI=0
# to strip the generator and all AI features from the build. Only the small
# app-side code and example/template icons are bundled; PyTorch/diffusers are
# downloaded at runtime if the user opts in.
ICON_AI_ENABLED = not ('--no-icon-ai' in sys.argv[1:]
                       or os.environ.get('VTA_ICON_AI', '').strip().lower() in ('0', 'false', 'no'))
# Package the finished build into a release asset by default (skip with
# --no-package or VTA_PACKAGE=0). The asset is named for OS, arch and edition so
# the in-app updater (app_updater.pick_asset) can match it.
PACKAGE_BUILD = not ('--no-package' in sys.argv[1:]
                     or os.environ.get('VTA_PACKAGE', '').strip().lower() in ('0', 'false', 'no'))
BUILD_EDITION = 'ai' if ICON_AI_ENABLED else 'standard'
# Standalone output folder (Windows/Linux): dist/vitaThemeArchitect/
def _build_tag():
    """<os>-<arch> for this build machine (e.g. windows-x64, linux-arm64,
    macos-arm64), so builds from different machines don't share a folder."""
    os_name = {'Windows': 'windows', 'Darwin': 'macos', 'Linux': 'linux'}.get(
        platform.system(), platform.system().lower())
    machine = (platform.machine() or 'unknown').lower()
    arch = {'amd64': 'x64', 'x86_64': 'x64', 'x64': 'x64',
            'arm64': 'arm64', 'aarch64': 'arm64'}.get(machine, machine)
    return f"{os_name}-{arch}"


# The distribution folder is named per OS + architecture. On Windows/Linux it is
# the portable folder itself; on macOS it is the parent of VitaThemeArchitect.app
# (whose name stays the same, so users still see "VitaThemeArchitect").
APP_FOLDER_NAME = f"vitaThemeArchitect-{_build_tag()}"

# Download URLs
VGAUDIO_API_URL = 'https://api.github.com/repos/Thealexbarney/VGAudio/releases/latest'
VGAUDIO_FALLBACK_URL = 'https://github.com/Thealexbarney/VGAudio/releases/download/v2.2.1/VGAudioCli.zip'

# Packages that Nuitka must explicitly include (not always auto-detected).
NUITKA_INCLUDE_PACKAGES = [
    'cffi', '_cffi_backend',
    'rich', 'cryptography',
]

# Single-file modules that Nuitka must explicitly include.
NUITKA_INCLUDE_MODULES = []


def print_header(msg):
    print(f"\n{'=' * 60}")
    print(f"  {msg}")
    print(f"{'=' * 60}")


def print_step(step, msg):
    print(f"\n[Step {step}] {msg}")


def _make_ssl_context():
    """Create an SSL context, trying certifi first, then system certs, then unverified."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    ctx = ssl.create_default_context()
    try:
        urllib.request.urlopen('https://github.com', timeout=5, context=ctx)
        return ctx
    except Exception:
        pass
    print("  WARNING: SSL certificate verification disabled (no valid CA bundle found).")
    print("  To fix, run: /Applications/Python*/Install\\ Certificates.command")
    print("  Or install certifi: pip3 install certifi")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def download_file(url, dest, description=""):
    """Download a file from a URL."""
    print(f"  Downloading {description or url}...")
    try:
        ssl_ctx = _make_ssl_context()
        req = urllib.request.Request(url, headers={'User-Agent': 'VitaThemeArchitect-Setup'})
        with urllib.request.urlopen(req, context=ssl_ctx) as resp, open(dest, 'wb') as out:
            total = resp.headers.get('Content-Length')
            if total:
                total = int(total)
                downloaded = 0
                block_size = 8192
                while True:
                    chunk = resp.read(block_size)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    pct = int(downloaded * 100 / total)
                    print(f"\r  Progress: {pct}% ({downloaded // 1024}KB / {total // 1024}KB)", end='', flush=True)
                print()
            else:
                out.write(resp.read())
        print(f"  Saved to: {dest}")
        return True
    except Exception as e:
        print(f"  Download failed: {e}")
        return False


def setup_tools_dir():
    """Create the Tools directory if it doesn't exist."""
    os.makedirs(TOOLS_DIR, exist_ok=True)
    print(f"  Tools directory: {TOOLS_DIR}")


def _get_release_download_url():
    """Query the GitHub API for the latest VGAudio release download URL."""
    try:
        print("  Querying GitHub API for latest VGAudio release...")
        ssl_ctx = _make_ssl_context()
        req = urllib.request.Request(VGAUDIO_API_URL, headers={'User-Agent': 'VitaThemeArchitect-Setup'})
        with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as resp:
            release = json.loads(resp.read().decode())
        tag = release.get('tag_name', 'v2.2.1')
        for asset in release.get('assets', []):
            if 'VGAudioCli' in asset['name'] and asset['name'].endswith('.zip'):
                print(f"  Found release: {tag} - {asset['name']}")
                return asset['browser_download_url']
    except Exception as e:
        print(f"  GitHub API query failed: {e}")

    print("  Using fallback download URL (v2.2.1)...")
    return VGAUDIO_FALLBACK_URL


def download_vgaudiocli_exe():
    """Download pre-built VGAudioCli.exe from GitHub releases.

    On Windows this is the primary decoder.  On macOS/Linux it serves as a
    Wine-based fallback when the native self-contained binary hangs or crashes.
    """
    vgaudio_path = os.path.join(TOOLS_DIR, 'VGAudioCli.exe')

    if os.path.exists(vgaudio_path):
        print(f"  VGAudioCli already exists: {vgaudio_path}")
        return True

    download_url = _get_release_download_url()

    tmp = tempfile.mkdtemp()
    try:
        archive = os.path.join(tmp, 'VGAudioCli.zip')
        if not download_file(download_url, archive, 'VGAudioCli'):
            return False

        print("  Extracting VGAudioCli...")
        with zipfile.ZipFile(archive, 'r') as zf:
            zf.extractall(tmp)

        # The zip contains subdirectories (net451/, net451_standalone/, netcoreapp2.0/).
        # On Windows, prefer net451 (small, uses system .NET Framework).
        # On macOS/Linux, prefer net451_standalone (self-contained, no .NET runtime needed in Wine).
        if platform.system() == 'Windows':
            candidates = ('net451', 'net451_standalone', 'netcoreapp2.0')
        else:
            candidates = ('net451_standalone', 'netcoreapp2.0', 'net451')
        extract_dir = None
        for candidate in candidates:
            candidate_path = os.path.join(tmp, candidate)
            if os.path.isdir(candidate_path):
                cli_in_candidate = os.path.join(candidate_path, 'VGAudioCli.exe')
                if os.path.exists(cli_in_candidate):
                    extract_dir = candidate_path
                    print(f"  Using variant: {candidate}")
                    break

        if not extract_dir:
            print("  ERROR: VGAudioCli.exe not found in any subdirectory")
            print(f"  Archive contents: {os.listdir(tmp)}")
            return False

        # Copy all files from the chosen variant into Tools/
        for item in os.listdir(extract_dir):
            src = os.path.join(extract_dir, item)
            dst = os.path.join(TOOLS_DIR, item)
            if os.path.isdir(src):
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)

        if os.path.exists(vgaudio_path):
            return True

        print("  ERROR: VGAudioCli.exe not found after extraction")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def get_credentials():
    """Prompt once for (username, password) on non-Windows platforms.

    Returns (username, password) on Linux/macOS, or (None, None) on Windows.
    The credentials are used by steps that need elevated privileges
    (e.g. Wine build).
    """
    if platform.system() == 'Windows':
        return None, None

    import getpass
    print("\n  Some build steps require admin privileges (Wine build, etc.).")
    print("  Credentials are requested once and reused for all steps.")
    current_user = os.environ.get('USER', os.environ.get('LOGNAME', ''))
    if current_user:
        print(f"  Current user detected: {current_user}")
        use_current = input(f"  Use '{current_user}'? [Y/n]: ").strip().lower()
        if use_current in ('', 'y', 'yes'):
            username = current_user
        else:
            username = input("  Enter username with admin privileges: ").strip()
    else:
        username = input("  Enter username with admin privileges: ").strip()

    password = getpass.getpass(f"  Enter password for '{username}': ")
    return username, password


def _scrape_vlc_filename(url, pattern):
    """Scrape a VideoLAN directory listing for a filename matching pattern."""
    import re
    try:
        ssl_ctx = _make_ssl_context()
        req = urllib.request.Request(url, headers={'User-Agent': 'VitaThemeArchitect-Setup'})
        with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as resp:
            listing = resp.read().decode('utf-8', errors='replace')
        matches = re.findall(r'href="(' + pattern + r')"', listing)
        if matches:
            return matches[0]
    except Exception as e:
        print(f"  Failed to scrape directory listing: {e}")
    return None


def _download_vlc_windows(vlc_dir):
    """Download portable VLC zip for Windows."""
    vlc_exe = os.path.join(vlc_dir, 'vlc.exe')
    if os.path.exists(vlc_exe):
        print(f"  Portable VLC already exists: {vlc_dir}")
        return True

    machine = platform.machine().lower()
    arch = 'win64' if machine in ('amd64', 'x86_64') else 'win32'
    url = f'https://download.videolan.org/vlc/last/{arch}/'

    filename = _scrape_vlc_filename(url, r'vlc-[\d.]+-' + arch + r'\.zip')
    if filename:
        zip_url = url + filename
    else:
        zip_url = f'https://download.videolan.org/vlc/3.0.21/{arch}/vlc-3.0.21-{arch}.zip'

    tmp = tempfile.mkdtemp()
    try:
        archive = os.path.join(tmp, 'vlc.zip')
        if not download_file(zip_url, archive, f'VLC portable ({arch})'):
            return False

        print("  Extracting VLC...")
        with zipfile.ZipFile(archive, 'r') as zf:
            zf.extractall(tmp)

        extracted_dirs = [d for d in os.listdir(tmp)
                          if os.path.isdir(os.path.join(tmp, d)) and d.startswith('vlc-')]
        if not extracted_dirs:
            print("  ERROR: Could not find VLC directory in archive.")
            return False

        extracted_vlc = os.path.join(tmp, extracted_dirs[0])
        if os.path.exists(vlc_dir):
            shutil.rmtree(vlc_dir)
        shutil.move(extracted_vlc, vlc_dir)

        if os.path.exists(vlc_exe):
            return True

        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _download_vlc_macos(vlc_dir):
    """Download VLC .dmg for macOS, mount, and copy VLC.app."""
    vlc_app = os.path.join(vlc_dir, 'VLC.app')
    if os.path.exists(vlc_app):
        return True

    machine = platform.machine().lower()
    arch = 'arm64' if machine in ('arm64', 'aarch64') else 'intel64'

    url = 'https://download.videolan.org/vlc/last/macosx/'
    filename = _scrape_vlc_filename(url, r'vlc-[\d.]+-' + arch + r'\.dmg')
    if filename:
        dmg_url = url + filename
    else:
        dmg_url = f'https://download.videolan.org/vlc/3.0.21/macosx/vlc-3.0.21-{arch}.dmg'

    tmp = tempfile.mkdtemp()
    mount_point = os.path.join(tmp, 'vlc_mount')
    try:
        dmg_path = os.path.join(tmp, 'vlc.dmg')
        if not download_file(dmg_url, dmg_path, f'VLC ({arch} dmg)'):
            return False

        os.makedirs(mount_point, exist_ok=True)
        result = subprocess.run(
            ['hdiutil', 'attach', dmg_path, '-nobrowse', '-mountpoint', mount_point],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            return False

        src_app = os.path.join(mount_point, 'VLC.app')
        if not os.path.exists(src_app):
            return False

        os.makedirs(vlc_dir, exist_ok=True)
        shutil.copytree(src_app, vlc_app)
        return True
    except subprocess.TimeoutExpired:
        return False
    except Exception as e:
        return False
    finally:
        subprocess.run(['hdiutil', 'detach', mount_point, '-force'], capture_output=True, timeout=30)
        shutil.rmtree(tmp, ignore_errors=True)


# Plugin filename substrings that must be kept for audio-only libvlc use.
# Matched case-insensitively against the full plugin filename.
# Any plugin whose name does NOT contain at least one of these strings is
# removed.  This covers: file access, common audio containers/decoders, and
# macOS audio output.  Video decoders, subtitle renderers, video filters,
# broadcast inputs, etc. are all dropped.
_VLC_AUDIO_PLUGINS = frozenset([
    # ── File / stream access ──────────────────────────────────────────────
    'access_file', 'filesystem', 'access_imem', 'access_smb',
    # ── Audio containers / demuxers ───────────────────────────────────────
    'wav', 'aiff', '_au_', 'ogg', 'mkv', 'mp4', 'flac',
    'rawaud', 'rawdv',
    # ── Audio decoders ────────────────────────────────────────────────────
    'araw', '_pcm', 'rawaud',
    'a52', '_dca',               # Dolby Digital / DTS
    'mpga', '_mad',              # MPEG audio / MP3
    'faad', 'mp4_audio',         # AAC
    'vorbis', 'opus', 'speex',   # Ogg family
    'flac',                      # FLAC (also demuxer above)
    'adpcm', 'g711', 'g72',      # telephony codecs
    'tta_', '_wma',              # TrueAudio, WMA
    # ── Audio output (macOS) ──────────────────────────────────────────────
    'coreaudio', 'audiounit', 'auhal', 'aout_file',
    'adummy', '_dummy',
    # ── Audio filter chain (needed for libvlc internal pipeline) ─────────
    # Without these, libvlc silently fails to build the audio output chain
    # and no sound is produced even though auhal initialises successfully.
    'audio_format',          # PCM format conversion (float↔int, rate, etc.)
    'scaletempo',            # time-stretch / tempo without pitch shift
    'channel_mixer',         # channel up/downmix
    'trivial_channel',       # trivial (pass-through) channel mixer
    'simple_channel',        # simple stereo ↔ mono mixer
    'normvol', 'normalizer', # volume normalisation
    'gain',                  # software gain
    'equalizer', 'param_eq', # EQ
    'compress',              # dynamic-range compressor
    'stereo_widen',          # stereo widener
    'samplerate', 'bandlimited',  # alternative resamplers
    # ── Infrastructure ────────────────────────────────────────────────────
    'logger', 'console',
])


def _strip_vlc_macos(vlc_dir):
    """Strip a freshly downloaded VLC.app to the minimum needed for libvlc audio.

    A full VLC.app can be 200 MB+.  For libvlc-based audio playback we only
    need:
      • Contents/MacOS/lib/libvlc*.dylib + libvlccore*.dylib
      • Contents/MacOS/plugins/*.dylib  — audio-related plugins only
        (in a stripped/monolithic VLC build these may not exist at all)

    Everything else — the VLC binary, Frameworks, Resources, code-signature
    directory, libbluray JARs, stale plugins.dat — is deleted here so that
    neither Tools/ nor the final Nuitka bundle carry unnecessary weight.

    Skips silently if stripping was already done (VLC binary already gone).
    """
    vlc_app = os.path.join(vlc_dir, 'VLC.app')
    if not os.path.isdir(vlc_app):
        return

    macos_dir = os.path.join(vlc_app, 'Contents', 'MacOS')
    removed_bytes = 0

    def _rm(path):
        nonlocal removed_bytes
        try:
            if os.path.islink(path):
                removed_bytes += 0
                os.remove(path)
            elif os.path.isfile(path):
                removed_bytes += os.path.getsize(path)
                os.remove(path)
            elif os.path.isdir(path):
                for root, _dirs, files in os.walk(path):
                    for f in files:
                        try:
                            removed_bytes += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass

    # ── 1. Remove the VLC binary (25-40 MB) ──────────────────────────────
    vlc_bin = os.path.join(macos_dir, 'VLC')
    if not os.path.exists(vlc_bin):
        return  # already stripped
    _rm(vlc_bin)

    # ── 2. Remove Frameworks/ (Qt, Sparkle, etc. — 80-150 MB) ────────────
    _rm(os.path.join(vlc_app, 'Contents', 'Frameworks'))

    # ── 3. Remove Resources/ (icons, nibs, locales — 5-20 MB) ────────────
    _rm(os.path.join(vlc_app, 'Contents', 'Resources'))

    # ── 4. Remove code-signature directory (no longer valid after stripping)
    _rm(os.path.join(vlc_app, 'Contents', '_CodeSignature'))

    # ── 5. Trim lib/ — keep only libvlc*.dylib and libvlccore*.dylib ─────
    lib_dir = os.path.join(macos_dir, 'lib')
    if os.path.isdir(lib_dir):
        for name in list(os.listdir(lib_dir)):
            if not (name.startswith('libvlc.') or name.startswith('libvlccore.')):
                _rm(os.path.join(lib_dir, name))
                print(f"    Removed lib/{name}")

    # ── 6. Trim plugins/ ─────────────────────────────────────────────────
    plugins_dir = os.path.join(macos_dir, 'plugins')
    if os.path.isdir(plugins_dir):
        for name in list(os.listdir(plugins_dir)):
            fpath = os.path.join(plugins_dir, name)
            name_lc = name.lower()
            if not name_lc.endswith('.dylib'):
                # Remove JARs, plugins.dat, and anything else non-dylib
                _rm(fpath)
                print(f"    Removed plugins/{name}")
                continue
            # Keep only audio-related plugin dylibs
            if not any(pat in name_lc for pat in _VLC_AUDIO_PLUGINS):
                _rm(fpath)
                print(f"    Removed plugins/{name} (non-audio)")

    mb = removed_bytes / (1024 * 1024)
    print(f"  VLC stripped: freed {mb:.1f} MB from Tools/VLC/")


def _download_vlc_linux(vlc_dir):
    """Copy system-installed VLC libraries into Tools/VLC/ for bundling."""
    import glob as glob_module

    libvlc_so = os.path.join(vlc_dir, 'libvlc.so')
    if os.path.exists(libvlc_so):
        return True

    search_paths = ['/usr/lib/x86_64-linux-gnu', '/usr/lib/aarch64-linux-gnu', '/usr/lib64', '/usr/lib']
    libvlc_src = None
    for search in search_paths:
        candidate = os.path.join(search, 'libvlc.so')
        if os.path.exists(candidate) or glob_module.glob(os.path.join(search, 'libvlc.so*')):
            libvlc_src = search
            break

    if not libvlc_src:
        return False

    os.makedirs(vlc_dir, exist_ok=True)
    for lib in glob_module.glob(os.path.join(libvlc_src, 'libvlc*.so*')):
        dest = os.path.join(vlc_dir, os.path.basename(lib))
        if os.path.islink(lib):
            link_target = os.readlink(lib)
            if os.path.exists(dest):
                os.remove(dest)
            os.symlink(link_target, dest)
        else:
            shutil.copy2(lib, dest)

    plugin_search = [os.path.join(libvlc_src, 'vlc', 'plugins'), '/usr/lib/vlc/plugins', '/usr/lib64/vlc/plugins']
    for plugin_dir in plugin_search:
        if os.path.isdir(plugin_dir):
            dest_plugins = os.path.join(vlc_dir, 'plugins')
            if os.path.exists(dest_plugins):
                shutil.rmtree(dest_plugins)
            shutil.copytree(plugin_dir, dest_plugins)
            break
    return True


def download_vlc_portable():
    """Download or copy VLC for the current platform into Tools/VLC/."""
    system = platform.system()
    vlc_dir = os.path.join(TOOLS_DIR, 'VLC')

    if system == 'Windows':
        return _download_vlc_windows(vlc_dir)
    elif system == 'Darwin':
        ok = _download_vlc_macos(vlc_dir)
        if ok:
            _strip_vlc_macos(vlc_dir)
        return ok
    elif system == 'Linux':
        return _download_vlc_linux(vlc_dir)
    else:
        print(f"  Unsupported platform: {system}")
        return False


def install_pip_deps():
    """Install Python dependencies from requirements.txt."""
    req_file = os.path.join(PROJECT_ROOT, 'requirements.txt')
    if not os.path.exists(req_file):
        return True

    result = subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', req_file], cwd=PROJECT_ROOT)
    return result.returncode == 0


def app_layout():
    """Return (app_dir, exe_path) of the built app.

    app_dir is the folder that holds the executable and is on sys.path at
    runtime (where loose extension modules must go).
    """
    dist_dir = os.path.join(PROJECT_ROOT, 'dist')
    system = platform.system()
    if system == 'Darwin':
        import plistlib
        app_dir = os.path.join(dist_dir, 'VitaThemeArchitect.app', 'Contents', 'MacOS')
        exe_name = 'VitaThemeArchitect'
        try:
            with open(os.path.join(app_dir, '..', 'Info.plist'), 'rb') as f:
                exe_name = plistlib.load(f).get('CFBundleExecutable', exe_name)
        except (OSError, ValueError):
            pass
        return app_dir, os.path.join(app_dir, exe_name)
    app_dir = os.path.join(dist_dir, APP_FOLDER_NAME)
    exe_name = 'vitaThemeArchitect.exe' if system == 'Windows' else 'vitaThemeArchitect'
    return app_dir, os.path.join(app_dir, exe_name)


def bind_release_modules():
    """Generate the release modules for this exact build and add them.

    Runs after Nuitka: the release script receives the built executable via
    VTA_BUILD_EXE; the modules are then copied next to the executable (and
    the bundle re-signed on macOS).

    Returns True on success, False on failure (the build stops), or None for
    a non-release build — the build continues without online features.
    """
    encrypt_path = os.path.join(PROJECT_ROOT, _RELEASE_SCRIPT) if _RELEASE_SCRIPT else ''
    if not encrypt_path or not os.path.isfile(encrypt_path):
        print("  Non-release (source) build.")
        print("  Online features (theme downloader, theme submission) are release-only and will be disabled.")
        return None

    app_dir, exe_path = app_layout()
    if not os.path.isfile(exe_path):
        print(f"  Built executable not found: {exe_path}")
        return False

    env = dict(os.environ, VTA_BUILD_EXE=exe_path)
    # The release script confirms the final (copied / re-signed) build; if
    # signing changed it, the modules are generated again for the final build.
    for attempt in range(3):
        if not _generate_and_install_modules(encrypt_path, env, app_dir):
            return False
        verify = subprocess.run([sys.executable, encrypt_path, '--verify'], cwd=PROJECT_ROOT, env=env)
        if verify.returncode == 0:
            return True
        print("  The final build changed (signing): generating the modules again...")
    print("  ERROR: the build kept changing, so its online features would not work.")
    return False


def _generate_and_install_modules(encrypt_path, env, app_dir):
    """Run the release script, copy its modules next to the exe, re-sign on macOS."""
    import glob as glob_module
    result = subprocess.run([sys.executable, encrypt_path], cwd=PROJECT_ROOT, env=env)
    if result.returncode != 0:
        return False

    ext_suffix = '.pyd' if platform.system() == 'Windows' else '.so'
    copied = 0
    for pattern in (f'secure_*{ext_suffix}', f'codeenigma_runtime*{ext_suffix}'):
        for ext_file in glob_module.glob(os.path.join(PROJECT_ROOT, pattern)):
            shutil.copy2(ext_file, app_dir)
            print(f"  Added: {os.path.basename(ext_file)}")
            copied += 1
    if not copied:
        print("  No secure modules were generated.")
        return False

    if platform.system() == 'Darwin':
        _resign_app(os.path.join(PROJECT_ROOT, 'dist', 'VitaThemeArchitect.app'))
    return True


def _find_windows_msvc():
    """A path to MSVC (cl.exe / its VS install) if present, else None. Looks via
    vswhere, then common VS/BuildTools dirs, then PATH — so it's found even when
    not run from a Developer Command Prompt."""
    import glob as glob_module
    if shutil.which('cl'):
        return shutil.which('cl')
    vswhere = os.path.join(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
                           'Microsoft Visual Studio', 'Installer', 'vswhere.exe')
    if os.path.isfile(vswhere):
        try:
            out = subprocess.run([vswhere, '-latest', '-products', '*', '-requires',
                                  'Microsoft.VisualCpp.Tools.Host.x64.TargetX64',
                                  '-property', 'installationPath'],
                                 capture_output=True, text=True, timeout=30).stdout.strip()
            if out:
                return out
        except (OSError, subprocess.SubprocessError):
            pass
    for base in (os.environ.get('ProgramFiles(x86)', ''), os.environ.get('ProgramFiles', '')):
        if base:
            hits = glob_module.glob(os.path.join(base, 'Microsoft Visual Studio', '*', '*',
                                                 'VC', 'Tools', 'MSVC', '*', 'bin', 'Host*', '*', 'cl.exe'))
            if hits:
                return hits[0]
    return None


def check_toolchain(is_release):
    """Warn (with per-OS guidance) if the C toolchain this build needs isn't
    available. Release builds compile the secure modules (Cython + codeenigma),
    which need a working system C compiler; the main Nuitka build can fetch MinGW
    on Windows if MSVC is absent. Returns True if the build can proceed."""
    system = platform.system()
    print("\n  Build toolchain:")
    compiler_ok = True
    if system == 'Windows':
        msvc = _find_windows_msvc()
        if msvc:
            print(f"    C compiler (MSVC): found")
            if shutil.which('cl') is None:
                print("    NOTE: cl.exe is not on PATH. Run this build from the 'x64 Native Tools")
                print("          Command Prompt for VS' (or call vcvarsall.bat x64 first), otherwise the")
                print("          secure modules (release) may not find the compiler.")
        else:
            compiler_ok = False
            print("    C compiler (MSVC): NOT FOUND")
            print("      Install 'Visual Studio Build Tools' with the 'Desktop development with C++'")
            print("      workload, then build from the 'x64 Native Tools Command Prompt for VS'.")
    elif system == 'Darwin':
        clang = shutil.which('clang')
        if clang:
            print(f"    C compiler (clang): found")
        else:
            compiler_ok = False
            print("    C compiler (clang): NOT FOUND")
            print("      Install the Xcode Command Line Tools:  xcode-select --install")
    else:
        cc = shutil.which('gcc') or shutil.which('cc')
        if cc:
            print(f"    C compiler: found ({os.path.basename(cc)})")
        else:
            compiler_ok = False
            print("    C compiler (gcc): NOT FOUND")
            print("      Install build tools, e.g.:  sudo apt install build-essential   (Debian/Ubuntu)")
            print("                                  sudo dnf groupinstall 'Development Tools'   (Fedora)")

    release_ok = True
    if is_release:
        import importlib.util
        if importlib.util.find_spec('Cython') is None:
            release_ok = False
            print("    Cython: NOT FOUND  (pip install Cython)")
        else:
            print("    Cython: found")
        if shutil.which('codeenigma') is None:
            print("    codeenigma: not on PATH — run the build with the project's venv active,")
            print("                or obfuscation of the secure modules will fail.")
        else:
            print("    codeenigma: found")

    if system == 'Windows' and not compiler_ok:
        # Nuitka could fetch MinGW for the main exe, but the release secure
        # modules need MSVC, so a release can't proceed without it.
        if is_release:
            print("\n  A release build needs MSVC for the secure modules. Install it and retry.")
            return False
        print("\n  No MSVC found; Nuitka will offer to download MinGW for the (non-release) build.")
        return True
    if not compiler_ok or not release_ok:
        print("\n  Install the missing build tools above, then retry.")
        return False
    return True


def verify_tools():
    """Print a summary of all tools and their status."""
    system = platform.system()
    tools = {}

    tools['VGAudioCli'] = os.path.join(TOOLS_DIR, 'VGAudioCli.exe')

    vlc_dir = os.path.join(TOOLS_DIR, 'VLC')
    if system == 'Windows':
        tools['VLC'] = os.path.join(vlc_dir, 'vlc.exe')
    elif system == 'Darwin':
        tools['VLC'] = os.path.join(vlc_dir, 'VLC.app')
    else:
        tools['VLC'] = os.path.join(vlc_dir, 'libvlc.so')

    print("\n  Tool verification:")
    all_ok = True
    for name, path in tools.items():
        exists = os.path.exists(path)
        status = 'FOUND' if exists else 'MISSING'
        print(f"    {name}: {status} ({path})")
        if not exists:
            all_ok = False
    return all_ok


def _include_dir_files(src_dir, dest_prefix):
    """Return --include-data-files args for every file under src_dir.

    Nuitka's --include-data-dir warns (and may skip) when a directory contains
    only symlinks or subdirectories at its top level.  Walking the tree and
    emitting one --include-data-files entry per file avoids those warnings and
    correctly handles symlink farms (e.g. Wine lib/, VLC lib/).
    """
    args = []
    if not os.path.isdir(src_dir):
        return args
    for root, _dirs, files in os.walk(src_dir, followlinks=True):
        for fname in files:
            abs_src = os.path.join(root, fname)
            rel = os.path.relpath(abs_src, src_dir)
            dest = f'{dest_prefix}/{rel}'.replace(os.sep, '/')
            args.append(f'--include-data-files={abs_src}={dest}')
    return args


def _include_tree(src_dir, dest_prefix):
    """Compact data-include for a normal (non-symlink) asset tree: one
    --include-data-dir per directory that holds files directly.

    Unlike _include_dir_files (one arg per file — needed for the Wine/VLC
    symlink farms), this keeps the Nuitka command line short.  With the AI
    build's many example icons, per-file includes overflow the Windows
    command-line limit (WinError 206).  Emitting a dir entry only for folders
    that actually contain files sidesteps Nuitka skipping a top level that holds
    only sub-directories (e.g. IconExamples/<set>/), with no double-includes for
    our shallow asset trees.
    """
    args = []
    if not os.path.isdir(src_dir):
        return args
    for root, _dirs, files in os.walk(src_dir):
        if not files:
            continue
        rel = os.path.relpath(root, src_dir)
        dest = dest_prefix if rel == '.' else f'{dest_prefix}/{rel}'.replace(os.sep, '/')
        args.append(f'--include-data-dir={root}={dest}')
    return args


def release_prebuild(shipped_py):
    """Release builds: the release script's pre-build step. Returns (extra
    Nuitka options, generated files to delete after the build)."""
    script = os.path.join(PROJECT_ROOT, _RELEASE_SCRIPT) if _RELEASE_SCRIPT else ''
    if not script or not os.path.isfile(script):
        return [], []
    env = dict(os.environ, VTA_SHIPPED_PY=json.dumps(shipped_py))
    if subprocess.run([sys.executable, script, '--prebuild'], cwd=PROJECT_ROOT, env=env).returncode != 0:
        print("  Release pre-build step FAILED.")
        sys.exit(1)
    with open(os.path.join(PROJECT_ROOT, 'result', 'prebuild.json'), 'r', encoding='utf-8') as f:
        info = json.load(f)
    return info.get('nuitka_args', []), info.get('generated', [])


def run_nuitka():
    """Build the project with Nuitka.

    Packaging strategy per platform:
    - Windows : standalone folder (dist/vitaThemeArchitect/) — portable, no
                per-launch extraction; distribute as a zip.
    - macOS   : standalone + .app bundle — no per-launch extraction lag on
                Apple Silicon.
    - Linux   : standalone folder (dist/vitaThemeArchitect/) — distribute as
                a tarball.
    """
    import glob as glob_module
    system = platform.system()

    # Remove Wine temp files from wine_prefix before bundling.
    wine_prefix = os.path.join(TOOLS_DIR, 'wine_prefix')
    if os.path.isdir(wine_prefix):
        for root, _dirs, files in os.walk(wine_prefix, followlinks=False):
            for fname in files:
                if fname.endswith('.tmp'):
                    try:
                        os.remove(os.path.join(root, fname))
                    except OSError:
                        pass

    # Remove only THIS build's own stale output, so builds for other OSes /
    # architectures already in dist/ (e.g. a Linux folder on a Windows machine)
    # are kept — that's the whole point of the per-OS-arch folder name.
    dist_dir = os.path.join(PROJECT_ROOT, 'dist')
    stale = [os.path.join(dist_dir, APP_FOLDER_NAME),            # final tagged folder
             os.path.join(dist_dir, APP_FOLDER_NAME + '.dist'),  # Nuitka standalone output
             os.path.join(dist_dir, APP_FOLDER_NAME + '.build')] # Nuitka build dir
    if system == 'Darwin':
        stale.append(os.path.join(dist_dir, 'VitaThemeArchitect.app'))   # transient bundle location
    # Also old untagged/default leftovers from before the per-OS naming.
    stale += [os.path.join(dist_dir, 'main.dist'), os.path.join(dist_dir, 'main.build'),
              os.path.join(dist_dir, 'main.app')]
    for path in stale:
        if os.path.isdir(path):
            print(f"  Removing stale: {path}")
            shutil.rmtree(path, ignore_errors=True)

    # ── Build the Nuitka command ────────────────────────────────────────
    cmd = [sys.executable, '-m', 'nuitka']

    # Mode — standalone on every platform (required for the .app bundle; no
    # extraction lag; and the release secure modules are added to the output
    # folder after the build — see bind_release_modules()).
    cmd += ['--standalone']

    # Output
    cmd += [
        '--output-filename=vitaThemeArchitect',
        f'--output-dir={dist_dir}',
    ]
    if system != 'Darwin':
        # dist/vitaThemeArchitect/ instead of dist/main.dist/
        cmd += [f'--output-folder-name={APP_FOLDER_NAME}']

    # Disable console window on all platforms
    if system == 'Windows':
        cmd += ['--windows-console-mode=disable']
        _app_ico = os.path.join(PROJECT_ROOT, 'Assets', 'app_icon.ico')
        if os.path.exists(_app_ico):
            cmd += [f'--windows-icon-from-ico={_app_ico}']
    elif system == 'Darwin':
        cmd += ['--macos-create-app-bundle',
                '--macos-app-name=VitaThemeArchitect',
                '--macos-app-version=1.0',
                # options-nanny fatally rejects PyQt6 on macOS; the app works
                # fine with PyQt6 so we suppress that overly strict check.
                '--disable-plugin=options-nanny',
                ]
        _app_icns = os.path.join(PROJECT_ROOT, 'Assets', 'app_icon.icns')
        if os.path.exists(_app_icns):
            cmd += [f'--macos-app-icon={_app_icns}']
    # Linux: no special console flag needed for a GUI app without a terminal

    # PyQt6 plugin — essential for Qt to be picked up correctly
    cmd += ['--enable-plugin=pyqt6']

    # cffi imports setuptools only at build/install time (cffi.setuptools_ext).
    # At runtime that module is never used, so tell Nuitka's anti-bloat plugin
    # not to follow the setuptools import instead of warning and bundling it.
    cmd += ['--noinclude-setuptools-mode=nofollow']

    # Explicitly include packages Nuitka may not auto-detect
    for pkg in NUITKA_INCLUDE_PACKAGES:
        cmd += [f'--include-package={pkg}']

    # Single-file modules Nuitka may not auto-detect (lazy imports, etc.)
    for mod in NUITKA_INCLUDE_MODULES:
        cmd += [f'--include-module={mod}']

    # ── Data / binary includes ──────────────────────────────────────────

    # App icon (window/taskbar icon, loaded at runtime via QIcon). Shipped in
    # every build, AI or standard.
    for _icon_name in ('app_icon.ico', 'app_icon.png'):
        _icon_src = os.path.join(PROJECT_ROOT, 'Assets', _icon_name)
        if os.path.exists(_icon_src):
            cmd += [f'--include-data-files={_icon_src}=Assets/{_icon_name}']

    # Native libsndfile for platforms where soundfile's wheel has none (e.g.
    # Windows-ARM). The built DLL/.dylib/.so (+ dependency libs + license) live
    # per-arch in `libsndfile/<os>-<arch>/`; only THIS build's arch is bundled,
    # flattened to `libsndfile/` next to the exe, and loaded by
    # audio._prepare_bundled_sndfile() before `import soundfile`. Absent for
    # arches where the wheel already works — nothing is bundled there.
    _sndfile_dir = os.path.join(PROJECT_ROOT, 'libsndfile', _build_tag())
    if os.path.isdir(_sndfile_dir):
        for _f in sorted(os.listdir(_sndfile_dir)):
            _fp = os.path.join(_sndfile_dir, _f)
            if not os.path.isfile(_fp):
                continue
            _low = _f.lower()
            _is_lib = _low.endswith(('.dll', '.dylib', '.so')) or '.so.' in _low
            _is_license = (_low.startswith(('license', 'copying', 'notice'))
                           or _low.endswith('.license'))
            if _is_lib or _is_license:  # ship only the native libs + licenses,
                cmd += [f'--include-data-files={_fp}=libsndfile/{_f}']
            # (skip placeholders/scripts/docs like PLACE_DLL_HERE.txt)

    # VGAudioCli.exe + any DLLs alongside it
    vgaudio_exe = os.path.join(TOOLS_DIR, 'VGAudioCli.exe')
    if os.path.exists(vgaudio_exe):
        cmd += [f'--include-data-files={vgaudio_exe}=Tools/VGAudioCli.exe']
        for dll in glob_module.glob(os.path.join(TOOLS_DIR, '*.dll')):
            dll_name = os.path.basename(dll)
            cmd += [f'--include-data-files={dll}=Tools/{dll_name}']

    # VLC
    vlc_dir = os.path.join(TOOLS_DIR, 'VLC')
    if system == 'Windows':
        if os.path.isdir(vlc_dir):
            cmd += [f'--include-data-dir={vlc_dir}=VLC']
    elif system == 'Darwin':
        vlc_app = os.path.join(vlc_dir, 'VLC.app')
        vlc_lib = os.path.join(vlc_app, 'Contents', 'MacOS', 'lib')
        vlc_plugins = os.path.join(vlc_app, 'Contents', 'MacOS', 'plugins')
        # Use per-file includes to avoid Nuitka's "no data files" warning that
        # fires when a directory contains only symlinks at its top level.
        cmd += _include_dir_files(vlc_lib, 'lib')
        if os.path.isdir(vlc_plugins):
            # Drop plugins.dat — it is a VLC plugin-path cache generated on the
            # build machine with absolute paths that are invalid in the bundle.
            # VLC regenerates it (or skips it gracefully) at runtime.
            plugins_dat = os.path.join(vlc_plugins, 'plugins.dat')
            if os.path.exists(plugins_dat):
                os.remove(plugins_dat)
                print("  Removed stale plugins.dat from VLC plugins dir")
            # Use per-file includes for the same symlink-safety reason as lib/.
            cmd += _include_dir_files(vlc_plugins, 'plugins')
    elif system == 'Linux':
        for so in glob_module.glob(os.path.join(vlc_dir, 'libvlc*.so*')):
            so_name = os.path.basename(so)
            cmd += [f'--include-data-files={so}={so_name}']
        vlc_plugins = os.path.join(vlc_dir, 'plugins')
        if os.path.isdir(vlc_plugins):
            cmd += [f'--include-data-dir={vlc_plugins}=plugins']

    # Wine (non-Windows) — included as data so Nuitka never touches the Mach-O
    # headers of wine-preloader/wine64-preloader (same reason as with PyInstaller).
    if system != 'Windows':
        wine_dir = os.path.join(TOOLS_DIR, 'wine_minimal')
        for subdir in ('bin', 'lib', 'lib64', 'share'):
            src = os.path.join(wine_dir, subdir)
            if os.path.isdir(src):
                # Use per-file includes for lib/ which may contain only symlinks
                # at its top level — --include-data-dir warns on those.
                cmd += _include_dir_files(src, f'Tools/wine_minimal/{subdir}')
        # wine_prefix is NOT bundled — it is built at first launch by
        # startup_wine_warmup() inside _init_wine_prefix_if_needed().
        # This avoids bundling ~10 MB of prefix data and, more importantly,
        # avoids absolute symlinks in system32/ that are only valid on the
        # build machine.

    # The release modules are NOT included here: they are generated after the
    # build (bind_release_modules).

    # vlc.py (python-vlc) — include as a data file rather than via
    # --include-module=vlc.  The module include fails silently when VLC is not
    # installed system-wide because vlc.py's module-level find_lib() raises
    # OSError (libvlccore not found) during Nuitka's analysis phase, causing
    # vlc.py to be silently dropped from the bundle.  Including it as a data
    # file bypasses the build-time import entirely.  At runtime, audio.py
    # pre-loads libvlccore and sets PYTHON_VLC_LIB_PATH before _ensure_vlc()
    # ever calls "import vlc", so the import succeeds with the bundled dylib.
    vlc_source = None
    try:
        import importlib.util as _ilu
        _vlc_spec = _ilu.find_spec('vlc')
        if _vlc_spec and _vlc_spec.origin and _vlc_spec.origin.endswith('.py'):
            vlc_source = _vlc_spec.origin
            cmd += [f'--include-data-files={_vlc_spec.origin}=vlc.py']
            print(f"  Including vlc.py from: {_vlc_spec.origin}")
        else:
            print("  WARNING: vlc.py not found in site-packages — BGM audio playback will be unavailable in the bundle")
    except Exception as _e:
        print(f"  WARNING: Could not locate vlc.py ({_e}) — BGM audio playback may be unavailable")

    # Optional AI icon generator. The worker script runs in the downloaded
    # runtime's Python, so it ships as a data file, not a compiled module.
    # Built-in template icons (compressed, ~33 KB) ship in every build.
    cmd += _include_tree(os.path.join(PROJECT_ROOT, 'Assets', 'IconTemplates'), 'Assets/IconTemplates')
    # The "done" chime played when generation or saving a theme finishes (~40 KB).
    cmd += _include_tree(os.path.join(PROJECT_ROOT, 'Assets', 'Sounds'), 'Assets/Sounds')
    if ICON_AI_ENABLED:
        cmd += ['--include-module=icon_generator',
                f"--include-data-files={os.path.join(PROJECT_ROOT, 'icon_ai_worker.py')}=icon_ai_worker.py"]
        # Built-in example icon sets the generator learns layouts from (~70 KB each).
        cmd += _include_tree(os.path.join(PROJECT_ROOT, 'Assets', 'IconExamples'), 'Assets/IconExamples')
        # Shipped performance reference data (binary data file; encrypted +
        # authenticated inside), if present: lets the estimates predict hardware
        # the user hasn't measured.
        perf_ref = os.path.join(PROJECT_ROOT, 'Assets', 'icon_estimates.dat')
        if os.path.isfile(perf_ref):
            cmd += [f'--include-data-files={perf_ref}=Assets/icon_estimates.dat']
            print("  Including AI performance reference data")
        print("  Including AI icon generator")
    else:
        cmd += ['--nofollow-import-to=icon_generator']

    # Release builds: the release script's pre-build step.
    shipped_py = {'vlc': vlc_source}
    if ICON_AI_ENABLED:
        shipped_py['icon_ai_worker'] = os.path.join(PROJECT_ROOT, 'icon_ai_worker.py')
    prebuild_args, prebuild_files = release_prebuild(shipped_py)
    cmd += prebuild_args

    # Main script
    cmd += [os.path.join(PROJECT_ROOT, 'main.py')]

    if system == 'Darwin':
        mode = 'standalone + .app bundle'
    else:
        mode = f'standalone (dist/{APP_FOLDER_NAME}/)'
    print(f"  Mode: {mode}")
    print(f"  Running Nuitka...")
    try:
        result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    finally:
        # Generated per build; they live on only inside the executable.
        for path in prebuild_files:
            if os.path.exists(path):
                os.remove(path)
    if result.returncode != 0:
        return False

    # Nuitka names the standalone output folder "<name>.dist"; rename it to the
    # clean tagged name so app_layout() and the distribution folder match
    # (non-macOS; macOS produces a .app handled below).
    if system != 'Darwin':
        import glob as glob_module
        final = os.path.join(dist_dir, APP_FOLDER_NAME)
        produced = os.path.join(dist_dir, APP_FOLDER_NAME + '.dist')
        if not os.path.isdir(produced):
            candidates = [d for d in glob_module.glob(os.path.join(dist_dir, '*.dist')) if os.path.isdir(d)]
            produced = candidates[0] if candidates else produced
        if os.path.isdir(produced) and os.path.abspath(produced) != os.path.abspath(final):
            if os.path.isdir(final):
                shutil.rmtree(final)
            os.rename(produced, final)
            print(f"  Output folder: {final}")

    # Windows post-build: qt.conf beside the exe so Qt finds its plugins even
    # before main.py's QT_PLUGIN_PATH fallback runs.
    if system == 'Windows':
        app_dir, win_exe = app_layout()
        if os.path.exists(win_exe):
            win_qt_conf = os.path.join(app_dir, 'qt.conf')
            with open(win_qt_conf, 'w') as _f:
                _f.write('[Paths]\nPrefix = .\nPlugins = PyQt6/Qt6/plugins\n')
            print(f"  Wrote qt.conf: {win_qt_conf}")

    # Nuitka names the .app bundle after the input script (main.py → main.app)
    # regardless of --macos-app-name.  Rename to VitaThemeArchitect.app.
    if system == 'Darwin':
        import glob as glob_module
        candidates = glob_module.glob(os.path.join(dist_dir, '*.app'))
        for bundle in candidates:
            if os.path.basename(bundle) != 'VitaThemeArchitect.app':
                target = os.path.join(dist_dir, 'VitaThemeArchitect.app')
                if os.path.exists(target):
                    shutil.rmtree(target)
                shutil.move(bundle, target)
                print(f"  Renamed {os.path.basename(bundle)} → VitaThemeArchitect.app")
                bundle = target

        final_bundle = os.path.join(dist_dir, 'VitaThemeArchitect.app')
        # Patch Info.plist — Nuitka omits CFBundleIdentifier; without it
        # CFBundleGetMainBundle() can return NULL inside Qt's C++ static
        # initialisers, causing a SIGSEGV before any Python code runs.
        _patch_info_plist(final_bundle)
        # Write qt.conf beside the executable.  Qt reads this via
        # _NSGetExecutablePath() (NOT via CFBundle) so it is found before the
        # static initialisers in QtCore.so call CFBundleGetMainBundle().
        # Without qt.conf, QLibraryInfoPrivate::paths() falls through to the
        # CFBundle code path and crashes on a partially-initialised bundle.
        _write_qt_conf(final_bundle)
        # Symlink Contents/MacOS/platforms → PyQt6/Qt6/plugins/platforms.
        # Qt always appends '/platforms' to each entry in QCoreApplication::
        # libraryPaths() when searching for the QPA plugin.  The executable
        # directory (Contents/MacOS/) is always in that list.  If MacOS/platforms/
        # does not exist Qt finds nothing and calls qFatal() → SIGABRT.
        # QLibraryInfo / qt.conf is only consulted for QLibraryInfo::PluginsPath,
        # but Qt 6.10 on macOS 26 returns "" for that path (qt.conf lookup fails
        # before the Cocoa run loop starts), so the library-path fallback is the
        # only reliable mechanism.
        _create_platforms_symlink(final_bundle)
        # Re-sign after plist/qt.conf/symlink modifications.  Nuitka signs the
        # bundle during the build, but _patch_info_plist then modifies Info.plist
        # which invalidates the signature.  An invalid signature causes macOS to
        # silently refuse dlopen() on the platform plugin (libqcocoa.dylib), so Qt
        # calls qFatal("no Qt platform plugin") → SIGABRT before any window is shown.
        _resign_app(final_bundle)

    return True


def _patch_info_plist(app_bundle):
    """Add/fix required keys in the app bundle's Info.plist."""
    import plistlib
    plist_path = os.path.join(app_bundle, 'Contents', 'Info.plist')
    if not os.path.exists(plist_path):
        print(f"  WARNING: Info.plist not found at {plist_path}")
        return

    with open(plist_path, 'rb') as f:
        plist = plistlib.load(f)

    changed = False
    defaults = {
        'CFBundleIdentifier':       'com.vitathemearchitect.app',
        'CFBundleName':             'VitaThemeArchitect',
        'CFBundleDisplayName':      'VitaThemeArchitect',
        'NSPrincipalClass':         'NSApplication',
        'NSHighResolutionCapable':  True,
        'LSMinimumSystemVersion':   '11.0',
        'LSBackgroundOnly':         False,
    }
    for key, value in defaults.items():
        if plist.get(key) != value:
            plist[key] = value
            changed = True

    if changed:
        with open(plist_path, 'wb') as f:
            plistlib.dump(plist, f)
        print(f"  Patched Info.plist: {plist_path}")


def _write_qt_conf(app_bundle):
    """Write qt.conf beside the executable to bypass Qt's CFBundle path discovery.

    Qt locates qt.conf via _NSGetExecutablePath() — not via CFBundleGetMainBundle()
    — so it is found and parsed before any C++ static initialisers run.
    Without qt.conf, QLibraryInfoPrivate::paths() falls through to CFBundle,
    which crashes when the bundle is only partially initialised at dlopen time.

    qt.conf is placed at Contents/MacOS/qt.conf (beside the executable).
    Prefix = . means all Qt paths are relative to Contents/MacOS/.
    The Plugins entry is discovered by locating the platforms/libqcocoa.dylib
    shipped by Nuitka's pyqt6 plugin.
    """
    import glob as glob_module

    macos_dir = os.path.join(app_bundle, 'Contents', 'MacOS')

    # Find Qt plugins root by locating the cocoa platform plugin.
    plugins_rel = None
    for cocoa in glob_module.glob(
            os.path.join(macos_dir, '**', 'platforms', 'libqcocoa.dylib'),
            recursive=True):
        # platforms/ parent is the plugins/ dir; make it relative to macos_dir.
        plugins_abs = os.path.dirname(os.path.dirname(cocoa))
        plugins_rel = os.path.relpath(plugins_abs, macos_dir)
        break

    lines_macos = ['[Paths]', 'Prefix = .']
    if plugins_rel:
        lines_macos.append(f'Plugins = {plugins_rel}')

    # Write beside the executable — Qt finds this via _NSGetExecutablePath()
    # without touching CFBundle, so it is read before static initialisers run.
    qt_conf_macos = os.path.join(macos_dir, 'qt.conf')
    with open(qt_conf_macos, 'w') as f:
        f.write('\n'.join(lines_macos) + '\n')

    # Also write to Contents/Resources/qt.conf (the traditional bundle location).
    # Prefix = .. because Resources/ is one level below Contents/.
    resources_dir = os.path.join(app_bundle, 'Contents', 'Resources')
    os.makedirs(resources_dir, exist_ok=True)
    lines_res = ['[Paths]', 'Prefix = ..']
    if plugins_rel:
        lines_res.append(f'Plugins = MacOS/{plugins_rel}')
    qt_conf_res = os.path.join(resources_dir, 'qt.conf')
    with open(qt_conf_res, 'w') as f:
        f.write('\n'.join(lines_res) + '\n')

    print(f"  Wrote qt.conf (MacOS/ and Resources/): Plugins={plugins_rel or 'not found'}")


def _create_platforms_symlink(app_bundle):
    """Create Contents/MacOS/platforms → PyQt6/Qt6/plugins/platforms symlink.

    Qt's QPlatformIntegrationFactory always checks <executable_dir>/platforms/
    as a fallback when QLibraryInfo::path(PluginsPath) is empty.  On macOS 26
    with Qt 6.10, QLibraryInfo returns "" (qt.conf lookup fails before the
    Cocoa run loop starts), so this fallback is the only reliable discovery
    path.  Creating a relative symlink here costs nothing and works regardless
    of where the .app bundle is installed.
    """
    if platform.system() != 'Darwin':
        return

    macos_dir = os.path.join(app_bundle, 'Contents', 'MacOS')
    link_path = os.path.join(macos_dir, 'platforms')

    if os.path.islink(link_path) or os.path.exists(link_path):
        return  # already present from a previous run or Nuitka itself

    # Find the actual platforms directory — try canonical paths first.
    for rel in ('PyQt6/Qt6/plugins/platforms', 'lib/PyQt6/Qt6/plugins/platforms'):
        candidate = os.path.join(macos_dir, rel)
        if os.path.isdir(candidate):
            # Use a relative symlink so the bundle stays relocatable.
            target = os.path.relpath(candidate, macos_dir)
            os.symlink(target, link_path)
            print(f"  Created platforms symlink: MacOS/platforms → {target}")
            return

    print("  WARNING: Could not find PyQt6 platforms directory — skipping symlink")


def _resign_app(app_bundle):
    """Ad-hoc re-sign the entire app bundle after post-build plist/file edits.

    Nuitka signs the bundle at build time.  Any subsequent file modification
    (Info.plist patching, qt.conf creation) invalidates that signature.
    On macOS 12+ / Apple Silicon, an invalid signature causes dlopen() to
    silently return NULL for the Qt platform plugin, which makes Qt call
    qFatal() → SIGABRT before any window appears.

    We use '-' (ad-hoc identity) so no developer certificate is required.
    '--force' replaces any existing (now-broken) signature.
    '--deep' also signs nested frameworks and dylibs.
    """
    if platform.system() != 'Darwin':
        return
    if not os.path.isdir(app_bundle):
        print(f"  WARNING: Cannot re-sign — bundle not found: {app_bundle}")
        return

    print(f"  Re-signing app bundle (ad-hoc)...")
    result = subprocess.run(
        ['codesign', '--force', '--deep', '--sign', '-', app_bundle],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print(f"  Re-signed: {app_bundle}")
    else:
        print(f"  WARNING: codesign failed (exit {result.returncode}):")
        if result.stderr:
            print(f"    {result.stderr.strip()}")


def cleanup():
    """Remove downloaded Tools/ and Nuitka build artifacts."""
    import glob as glob_module

    # Nuitka build directories: <name>.build/ and <name>.onefile-build/
    # These end up inside dist/ when --output-dir=dist is used.
    dist_dir = os.path.join(PROJECT_ROOT, 'dist')
    for build_dir in glob_module.glob(os.path.join(dist_dir, '*.build')) + \
                     glob_module.glob(os.path.join(dist_dir, '*.onefile-build')):
        if os.path.isdir(build_dir):
            print(f"  Removing: {build_dir}")
            shutil.rmtree(build_dir, ignore_errors=True)

    # Tools/ (downloaded dependencies)
    if os.path.isdir(TOOLS_DIR):
        print(f"  Removing: {TOOLS_DIR}")
        shutil.rmtree(TOOLS_DIR, ignore_errors=True)

    ext_suffix = '.pyd' if platform.system() == 'Windows' else '.so'
    for pattern in (f'secure_*{ext_suffix}', f'codeenigma_runtime*{ext_suffix}'):
        for f in glob_module.glob(os.path.join(PROJECT_ROOT, pattern)):
            os.remove(f)
            print(f"  Removed: {f}")

    print("  Cleanup complete.")


_DEV_STUB = os.path.join(PROJECT_ROOT, 'secure_function.py')
_DEV_STASH = os.path.join(PROJECT_ROOT, 'secure_function.py.devstash')


def _stash_dev_stub():
    """Set a local, uncommitted secure_function.py aside for the duration of the
    build and restore it afterwards, so a developer's local copy is never bundled
    into the build. No-op when there isn't one. Self-heals a stash a previous
    interrupted build may have left behind. Returns True if one was stashed."""
    if os.path.isfile(_DEV_STASH) and not os.path.isfile(_DEV_STUB):
        os.replace(_DEV_STASH, _DEV_STUB)   # recover an interrupted earlier build
    if os.path.isfile(_DEV_STUB):
        os.replace(_DEV_STUB, _DEV_STASH)
        print("  Set a local secure_function.py aside for the build (restored afterwards).")
        return True
    return False


def _restore_dev_stub(stashed):
    if stashed and os.path.isfile(_DEV_STASH):
        os.replace(_DEV_STASH, _DEV_STUB)
        print("  Restored the local secure_function.py.")


def generate_integrity_manifest(app_dir):
    """Write app_dir/integrity.json — SHA-256 of every shipped file — for the
    public install-integrity check (main._verify_install_integrity). Excludes the
    manifest itself, the main executable (it may be signed afterwards), and
    __pycache__. Windows/Linux only — macOS uses its code signature, and adding a
    file to a signed .app would invalidate it. Called AFTER Nuitka but BEFORE the
    secure modules, so the release fingerprint includes integrity.json (no
    skip-list needed); the secure modules, generated afterwards, aren't listed
    here but are covered by that fingerprint instead."""
    import hashlib
    exe_names = {'vitaThemeArchitect.exe', 'vitaThemeArchitect'}
    files = {}
    for root, dirs, names in os.walk(app_dir):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        for name in names:
            if name == 'integrity.json' or name in exe_names:
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, app_dir).replace(os.sep, '/')
            h = hashlib.sha256()
            try:
                with open(full, 'rb') as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b''):
                        h.update(chunk)
            except OSError:
                continue
            files[rel] = h.hexdigest()
    with open(os.path.join(app_dir, 'integrity.json'), 'w', encoding='utf-8') as fh:
        json.dump({'files': files}, fh)
    print(f"  Wrote integrity.json ({len(files)} files) for the install-integrity check.")


def ensure_arm_libsndfile():
    """On a Windows-ARM build only: soundfile's wheel ships no working libsndfile,
    so mp3/flac/ogg audio needs a native libsndfile bundled. If one isn't already
    in libsndfile/windows-arm64/, OFFER to build it now (build-libsndfile.ps1 via
    vcpkg) so the build can bundle it. No-op on every other platform. Set
    VTA_BUILD_LIBSNDFILE=1 to build without prompting, =0 to skip silently."""
    if _build_tag() != 'windows-arm64':
        return
    dest = os.path.join(PROJECT_ROOT, 'libsndfile', 'windows-arm64')
    dll = os.path.join(dest, 'libsndfile.dll')
    if os.path.isfile(dll):
        return  # already have it — the include step will bundle it
    script = os.path.join(PROJECT_ROOT, 'libsndfile', 'build-libsndfile.ps1')
    if not os.path.isfile(script):
        return

    print("\n[libsndfile] No ARM64 libsndfile.dll found for this Windows-ARM build.")
    print("  Without it, mp3/flac/ogg audio won't work here (WAV still will).")
    choice = os.environ.get('VTA_BUILD_LIBSNDFILE', '').strip().lower()
    if choice in ('0', 'false', 'no'):
        return
    build_it = choice in ('1', 'true', 'yes')
    if not build_it:
        try:
            build_it = (sys.stdin and sys.stdin.isatty()
                        and input("  Build it now with vcpkg? [y/N] ").strip().lower()
                        in ('y', 'yes'))
        except (EOFError, OSError):
            build_it = False
    if not build_it:
        print("  Skipping. Build later with:")
        print("    powershell -ExecutionPolicy Bypass -File libsndfile\\build-libsndfile.ps1")
        return

    print("  Building libsndfile with vcpkg (first time takes a while)...")
    try:
        subprocess.run(['powershell', '-ExecutionPolicy', 'Bypass', '-File', script],
                       check=False)
    except FileNotFoundError:
        print("  Couldn't launch PowerShell to build libsndfile; continuing without it.")
        return
    if os.path.isfile(dll):
        print("  libsndfile ready — it will be bundled into this build.")
    else:
        print("  libsndfile not produced (vcpkg missing?); continuing WAV-only on ARM.")


def main():
    print_header("VitaTheme Architect - Build Setup")
    print(f"  Platform: {platform.system()} {platform.machine()}")
    print(f"  Python:   {sys.version.split()[0]}")
    is_release = bool(_RELEASE_SCRIPT) and os.path.isfile(os.path.join(PROJECT_ROOT, _RELEASE_SCRIPT))
    if _RELEASE_SCRIPT and not is_release:
        print("  WARNING: VTA_RELEASE_SCRIPT is set but the file was not found — building non-release.")
    build_type = ("Release (online features enabled)" if is_release
                  else "Source / non-release (online features disabled)")
    print(f"  Build:    {build_type}")
    print(f"  Icon AI:  {'included' if ICON_AI_ENABLED else 'not included (removed with --no-icon-ai)'}")

    # Prompt once for credentials on non-Windows platforms
    cred_username, cred_password = get_credentials()

    print_step(1, "Creating Tools directory...")
    setup_tools_dir()

    print_step(2, "Installing Python dependencies...")
    pip_ok = install_pip_deps()

    print_step(3, "Downloading VGAudioCli...")
    vgaudio_ok = download_vgaudiocli_exe()

    print_step(4, "Setting up VLC (portable)...")
    vlc_ok = download_vlc_portable()

    print_step(5, "Verifying tools...")
    tools_ok = verify_tools()

    # Build/download Wine for non-Windows (at9tool + VGAudioCli.exe need it)
    wine_ok = True
    if platform.system() != 'Windows':
        print_step(6, "Building/downloading Wine...")
        from wine_builder import build_wine
        wine_ok = build_wine(username=cred_username, password=cred_password)

    print_header("Setup Summary")
    print(f"  {'Build type:':<20} {build_type}")
    print(f"  {'AI icon generator:':<20} {'included' if ICON_AI_ENABLED else 'not included'}")
    results = [
        ("pip dependencies", pip_ok),
        ("VGAudioCli", vgaudio_ok),
        ("VLC (portable)", vlc_ok),
    ]
    if platform.system() != 'Windows':
        results.append(("Wine (bundled)", wine_ok))

    for name, ok in results:
        print(f"  {name + ':':<20} {'OK' if ok else 'FAILED'}")

    if not (all(ok for _, ok in results) and tools_ok):
        print(f"\n  Some steps failed. Please resolve issues above before building.")
        sys.exit(1)

    # Check the C toolchain (per OS) before the long Nuitka run, so a user on a
    # fresh machine is told exactly what to install instead of failing deep in.
    if not check_toolchain(is_release):
        sys.exit(1)

    # On Windows-ARM, offer to build the native libsndfile the soundfile wheel
    # lacks, so this build can bundle it (enables mp3/flac/ogg there). No-op
    # elsewhere and when the DLL is already present.
    ensure_arm_libsndfile()

    # A local, uncommitted secure_function.py is set aside for the whole build
    # and put back at the end (even on failure), so a developer's local copy is
    # never bundled and never needs removing by hand.
    dev_stub_stashed = _stash_dev_stub()
    try:
        print_step(7, "Building with Nuitka...")
        build_ok = run_nuitka()

        if not build_ok:
            print("\n  Nuitka build FAILED. Check output above.")
            sys.exit(1)

        # Public install-integrity manifest — written NOW (after Nuitka, BEFORE
        # the secure modules) so the release fingerprint INCLUDES integrity.json
        # and never sees it as an "added" file (no Encrypt.py skip-list needed).
        # Windows/Linux only; macOS uses its code signature (a manifest file
        # would invalidate the .app signature).
        if platform.system() != 'Darwin':
            try:
                _mf_app_dir, _ = app_layout()
                generate_integrity_manifest(_mf_app_dir)
            except Exception as e:
                print(f"  Integrity manifest step skipped: {e}")

        # After the build: the release modules for this build.
        if is_release:
            print_step(8, "Generating secure modules for this build (release build)...")
        else:
            print_step(8, "Skipping secure module generation (non-release build)...")
        encrypt_ok = bind_release_modules()
        if encrypt_ok is False:
            print("\n  Secure module generation FAILED. Check output above.")
            sys.exit(1)

        print_step(9, "Cleaning up build artifacts...")
        cleanup()
    finally:
        _restore_dev_stub(dev_stub_stashed)

    dist_dir = os.path.join(PROJECT_ROOT, 'dist')
    print_header("Build Complete")
    print(f"  Build type: {build_type}")
    print(f"  Output: {dist_dir}")
    if platform.system() == 'Darwin':
        # Keep the bundle named VitaThemeArchitect.app (the name users see) but
        # place it in the OS+arch folder so builds from different machines don't
        # collide. Moving a signed bundle keeps its signature valid and the
        # content-based module fingerprint unchanged (paths aren't fingerprinted).
        bundle = os.path.join(dist_dir, 'VitaThemeArchitect.app')
        tagged = os.path.join(dist_dir, APP_FOLDER_NAME)
        exe = os.path.join(tagged, 'VitaThemeArchitect.app')
        if os.path.isdir(bundle):
            shutil.rmtree(tagged, ignore_errors=True)
            os.makedirs(tagged, exist_ok=True)
            shutil.move(bundle, exe)
        print(f"  Executable: {exe}")
        print(f"  Distribution — ship the whole {tagged} folder (zip/tarball).")
    else:
        app_dir, exe = app_layout()
        print(f"  Executable: {exe}")
        print(f"  Portable distribution — ship the whole {app_dir} folder (zip/tarball).")
    if not is_release:
        print("\n  NOTE: Non-release build — online features are only available in official releases.")

    if PACKAGE_BUILD:
        package_build(dist_dir)


def package_build(dist_dir):
    """Zip the finished build into a single release asset named for its OS,
    architecture and edition, e.g. dist/vitaThemeArchitect-windows-x64-ai.zip.
    These exact os-arch-edition tokens are what the in-app updater
    (app_updater.pick_asset) matches to download the right build. Per OS:
    Windows → .zip, Linux → .tar.gz (keeps exec bits + symlinks), macOS → .zip
    via `ditto` (keeps the .app signature and symlinks)."""
    system = platform.system()
    if not os.path.isdir(os.path.join(dist_dir, APP_FOLDER_NAME)):
        print("  Skipping packaging — build folder not found.")
        return None
    out_base = os.path.join(dist_dir, f"{APP_FOLDER_NAME}-{BUILD_EDITION}")
    print_header("Packaging release asset")
    try:
        if system == 'Darwin':
            tagged = os.path.join(dist_dir, APP_FOLDER_NAME)
            archive = out_base + '.zip'
            if os.path.exists(archive):
                os.remove(archive)
            if shutil.which('ditto'):
                subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent',
                                tagged, archive], check=True)
            else:
                print("  WARNING: 'ditto' not found; using a plain zip "
                      "(may not preserve the .app signature).")
                archive = shutil.make_archive(out_base, 'zip',
                                              root_dir=dist_dir, base_dir=APP_FOLDER_NAME)
        elif system == 'Linux':
            archive = shutil.make_archive(out_base, 'gztar',
                                          root_dir=dist_dir, base_dir=APP_FOLDER_NAME)
        else:  # Windows (and any other)
            archive = shutil.make_archive(out_base, 'zip',
                                          root_dir=dist_dir, base_dir=APP_FOLDER_NAME)
        size_mb = os.path.getsize(archive) / 1024 ** 2
        print(f"  Created: {archive} ({size_mb:.1f} MB)")
        print(f"  Upload this as the GitHub release asset for the {BUILD_EDITION} build.")
        return archive
    except Exception as e:
        print(f"  WARNING: packaging failed ({e}).")
        print(f"  Zip the {APP_FOLDER_NAME} folder manually, named "
              f"{os.path.basename(out_base)}.<zip|tar.gz>.")
        return None


if __name__ == '__main__':
    main()
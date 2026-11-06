"""Cross-platform in-app updater.

Checks the project's GitHub releases, downloads the build that matches the
operating system and CPU architecture the app is running on, applies it in
place and restarts the app.

Design notes:
  * All of the app's *local data* (settings, the icon_ai runtime, calibration
    data, the per-install key, etc.) lives in the per-OS app-support directory,
    NOT in the app's install folder.  The updater only ever touches the install
    folder, and it copies the new files *over* the old ones (it never wipes the
    folder first), so nothing the user owns is deleted by an update.
  * A running program can't overwrite its own files on Windows (they're locked),
    so the actual swap is done by a tiny detached helper script that waits for
    this process to exit, copies the new build in, relaunches the app and then
    deletes itself.
  * In-place updating only makes sense for a compiled/standalone build.  When
    running from source `running_frozen()` is False and the UI falls back to
    pointing the user at the releases page.
"""
import os
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile

import requests

from constants import UPDATE_API_URL, UPDATE_API_TAG

EDITIONS = ('ai', 'standard')
_ARCHIVE_EXTS = ('.tar.gz', '.tgz', '.zip', '.dmg', '.tar')


# ---------------------------------------------------------------------------
# Platform / version helpers
# ---------------------------------------------------------------------------
def platform_tag():
    """(os, arch) for the running machine, e.g. ('windows', 'x64').

    Mirrors setup_build.py's _build_tag() so release assets built there can be
    matched here."""
    os_name = {'Windows': 'windows', 'Darwin': 'macos', 'Linux': 'linux'}.get(
        platform.system(), platform.system().lower())
    machine = (platform.machine() or 'unknown').lower()
    arch = {'amd64': 'x64', 'x86_64': 'x64', 'x64': 'x64',
            'arm64': 'arm64', 'aarch64': 'arm64'}.get(machine, machine)
    return os_name, arch


def _parse_version(text):
    """Turn a tag like 'v1.2.10' into a comparable tuple (1, 2, 10)."""
    nums = re.findall(r'\d+', text or '')
    return tuple(int(n) for n in nums) if nums else (0,)


def is_newer(latest, current):
    """True when release tag `latest` is a newer version than `current`."""
    return _parse_version(latest) > _parse_version(current)


def running_frozen():
    """True when running as a compiled/standalone build (not from source)."""
    try:
        import __main__
        if getattr(__main__, '__compiled__', False):
            return True
    except Exception:
        pass
    return bool(getattr(sys, 'frozen', False) or '__compiled__' in globals())


def app_root():
    """The install folder (Windows/Linux) or the .app bundle (macOS) that an
    update replaces.  Only meaningful when running_frozen()."""
    exe = os.path.abspath(sys.executable)
    if platform.system() == 'Darwin':
        macos_dir = os.path.dirname(exe)            # .../Contents/MacOS
        contents = os.path.dirname(macos_dir)       # .../Contents
        bundle = os.path.dirname(contents)          # .../Something.app
        if bundle.endswith('.app'):
            return bundle
    return os.path.dirname(exe)


# ---------------------------------------------------------------------------
# Release info
# ---------------------------------------------------------------------------
def _release_dict(data):
    return {
        'tag': data.get('tag_name') or '',
        'name': data.get('name') or '',
        'body': data.get('body') or '',
        'prerelease': bool(data.get('prerelease')),
        'assets': [{'name': a.get('name', ''),
                    'url': a.get('browser_download_url', ''),
                    'size': a.get('size', 0)}
                   for a in (data.get('assets') or [])],
    }


def _get_json(url, timeout):
    resp = requests.get(
        url,
        headers={'User-Agent': 'VitaThemeArchitect-Updater',
                 'Accept': 'application/vnd.github+json'},
        timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def fetch_latest(timeout=10):
    """Fetch the latest release from GitHub. Returns a dict with tag, name,
    body (release notes) and assets [{name, url, size}]."""
    return _release_dict(_get_json(UPDATE_API_URL, timeout))


def fetch_release_by_tag(tag, timeout=10):
    """Fetch a specific release by its tag (trying both 'x' and 'vx'), or None.
    Used to switch build edition while staying on the same version."""
    tag = (tag or '').strip()
    tries = [tag, 'v' + tag] if not tag.startswith('v') else [tag, tag[1:]]
    for candidate in tries:
        if not candidate:
            continue
        try:
            return _release_dict(_get_json(UPDATE_API_TAG.format(tag=candidate), timeout))
        except Exception:
            continue
    return None


def summarize_notes(body, max_lines=14, max_chars=800):
    """Turn a release's markdown body into a short, plain summary for a dialog.

    Strips HTML comments, heading markers and list bullets, drops blank and
    boilerplate lines, and caps the length so the dialog stays small."""
    if not body:
        return ""
    body = re.sub(r'<!--.*?-->', '', body, flags=re.S)
    lines = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith(('**full changelog', 'full changelog')) or 'github.com' in low and 'compare' in low:
            continue
        line = re.sub(r'^#+\s*', '', line)          # headings
        line = re.sub(r'^[-*+]\s+', '• ', line)     # bullets
        line = re.sub(r'`([^`]*)`', r'\1', line)    # inline code
        line = re.sub(r'\*\*([^*]*)\*\*', r'\1', line)  # bold
        line = re.sub(r'(?<!\w)\*([^*]+)\*(?!\w)', r'\1', line)  # italic
        lines.append(line)
        if len(lines) >= max_lines:
            lines.append('…')
            break
    summary = '\n'.join(lines).strip()
    if len(summary) > max_chars:
        summary = summary[:max_chars].rstrip() + '…'
    return summary


def _stem_tokens(name):
    """Lowercased asset name, archive extension removed, split into tokens."""
    low = name.lower()
    for ext in _ARCHIVE_EXTS:
        if low.endswith(ext):
            low = low[:-len(ext)]
            break
    return set(t for t in re.split(r'[-_.\s]+', low) if t)


def asset_edition(name):
    """The build edition an asset is for: 'ai' if its name carries the 'ai'
    token, otherwise 'standard' (standard builds may be left unlabelled)."""
    return 'ai' if 'ai' in _stem_tokens(name) else 'standard'


def current_edition():
    """The edition of the running build: 'ai' when the icon generator is
    compiled in, else 'standard'."""
    try:
        import icon_generator  # noqa: F401  (present only in AI builds)
        return 'ai'
    except Exception:
        return 'standard'


def pick_asset(assets, edition=None, require_edition=False):
    """Choose the release asset matching this OS + architecture (and edition,
    when given), or None.

    `edition` ('ai'/'standard') is preferred; with `require_edition=True` an
    asset of the other edition is never returned (used when switching editions
    so an AI build is never mistaken for a standard one or vice-versa)."""
    os_name, arch = platform_tag()
    pref_ext = {'windows': ['.zip'],
                'macos': ['.dmg', '.zip'],
                'linux': ['.tar.gz', '.tgz', '.zip']}.get(os_name, ['.zip'])
    best, best_score = None, None
    for a in assets:
        if not a.get('url'):
            continue
        toks = _stem_tokens(a['name'])
        if os_name not in toks:
            continue
        ed = asset_edition(a['name'])
        if edition is not None and ed != edition:
            if require_edition:
                continue
            score = -3                       # wrong edition: last resort
        else:
            score = 2 if edition is not None else 0
        if arch in toks:
            score += 4
        low = a['name'].lower()
        for i, ext in enumerate(pref_ext):
            if low.endswith(ext):
                score += len(pref_ext) - i
                break
        if best_score is None or score > best_score:
            best, best_score = a, score
    return best


# ---------------------------------------------------------------------------
# Download / extract
# ---------------------------------------------------------------------------
def download_asset(asset, dest_dir, progress=None, cancel=None, chunk=1 << 16):
    """Stream an asset to dest_dir. `progress(done, total)` is called as it
    downloads; `cancel()` returning True aborts and returns None."""
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, asset['name'] or 'update.bin')
    with requests.get(asset['url'], stream=True, timeout=60,
                      headers={'User-Agent': 'VitaThemeArchitect-Updater'}) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get('content-length') or asset.get('size') or 0)
        done = 0
        with open(path, 'wb') as fh:
            for block in resp.iter_content(chunk):
                if cancel and cancel():
                    fh.close()
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    return None
                fh.write(block)
                done += len(block)
                if progress:
                    progress(done, total)
    return path


def _extract(archive, dest):
    os.makedirs(dest, exist_ok=True)
    low = archive.lower()
    if low.endswith('.zip'):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    elif low.endswith(('.tar.gz', '.tgz', '.tar')):
        with tarfile.open(archive) as tf:
            tf.extractall(dest)
    elif low.endswith('.dmg'):
        _extract_dmg(archive, dest)
    else:
        raise ValueError(f'unsupported archive type: {os.path.basename(archive)}')
    return dest


def _extract_dmg(dmg, dest):
    """macOS: mount a .dmg, copy the .app out, unmount."""
    out = subprocess.check_output(
        ['hdiutil', 'attach', '-nobrowse', '-readonly', dmg], text=True)
    mount = None
    for line in out.splitlines():
        if '/Volumes/' in line:
            mount = line.split('\t')[-1].strip() or line.split('/Volumes/')[-1]
            mount = '/Volumes/' + mount.split('/Volumes/')[-1] if '/Volumes/' in line else mount
            break
    try:
        src_app = None
        for entry in os.listdir(mount):
            if entry.endswith('.app'):
                src_app = os.path.join(mount, entry)
                break
        if not src_app:
            raise ValueError('no .app inside the .dmg')
        target = os.path.join(dest, os.path.basename(src_app))
        subprocess.check_call(['ditto', src_app, target])
    finally:
        subprocess.call(['hdiutil', 'detach', mount, '-quiet'])


def _find_payload(extracted):
    """Locate the new app's root inside the extracted archive: the .app on
    macOS, otherwise the folder holding the executable (or the single top-level
    folder)."""
    os_name, _ = platform_tag()
    if os_name == 'macos':
        for root, dirs, _files in os.walk(extracted):
            for d in dirs:
                if d.endswith('.app'):
                    return os.path.join(root, d)
    # Prefer a folder that actually contains an executable.
    exe_base = os.path.basename(sys.executable)
    for root, _dirs, files in os.walk(extracted):
        if exe_base in files or any(f.lower().endswith('.exe') for f in files):
            return root
    entries = [os.path.join(extracted, e) for e in os.listdir(extracted)]
    dirs = [e for e in entries if os.path.isdir(e)]
    if len(dirs) == 1:
        return dirs[0]
    return extracted


def prepare_update(asset, progress=None, cancel=None):
    """Download + extract an update into a fresh temp staging area.

    Returns (new_app_path, stage_root) or None if cancelled. `stage_root` is a
    temp folder the caller passes to apply_and_restart() for cleanup."""
    stage_root = tempfile.mkdtemp(prefix='vta_update_')
    archive = download_asset(asset, stage_root, progress=progress, cancel=cancel)
    if not archive:
        return None
    extracted = _extract(archive, os.path.join(stage_root, 'payload'))
    new_app = _find_payload(extracted)
    return new_app, stage_root


# ---------------------------------------------------------------------------
# Apply + restart (detached helper)
# ---------------------------------------------------------------------------
def apply_and_restart(new_app, stage_root, clean=False):
    """Launch a detached helper that waits for this process to exit, installs the
    new build, relaunches the app and cleans up. The caller should quit the app
    right after this returns.

    `clean=False` (normal update) copies the new files over the install folder
    without deleting anything already there. `clean=True` (edition switch)
    mirrors the install folder so files that only belong to the old edition
    (e.g. the AI modules when moving to a standard build) are removed. Either
    way only the *install folder* is touched — never the app-support dir, where
    all user/AI/calibration data lives."""
    install = app_root()
    # Safety: the install folder must be the one that holds the running exe
    # (app_root() guarantees this). Refuse an obviously wrong / shallow target
    # so a clean mirror can never wipe something unexpected.
    install_abs = os.path.abspath(install)
    if (os.path.commonpath([os.path.abspath(sys.executable), install_abs]) != install_abs
            or len(install_abs.rstrip('\\/').split(os.sep)) < 2):
        raise RuntimeError(f'refusing to update an unexpected install path: {install_abs}')
    pid = os.getpid()
    system = platform.system()
    if system == 'Windows':
        return _apply_windows(new_app, install_abs, stage_root, pid, clean)
    return _apply_unix(new_app, install_abs, stage_root, pid, system, clean)


def _apply_windows(new_app, install, stage_root, pid, clean):
    exe = os.path.join(install, os.path.basename(sys.executable))
    script = os.path.join(tempfile.gettempdir(), f'vta_update_{pid}.bat')
    # /MIR mirrors (removes old-edition files); /E only overwrites/adds. Either
    # way this is the install folder only, which holds no user data.
    mode = '/MIR' if clean else '/E'
    lines = [
        '@echo off',
        'setlocal',
        ':waitloop',
        f'tasklist /FI "PID eq {pid}" | find "{pid}" >nul 2>&1',
        'if %errorlevel%==0 (',
        '  timeout /t 1 /nobreak >nul',
        '  goto waitloop',
        ')',
        f'robocopy "{new_app}" "{install}" {mode} /R:3 /W:1 /NFL /NDL /NJH /NJS /NC /NS /NP >nul',
        f'start "" "{exe}"',
        f'rmdir /s /q "{stage_root}" >nul 2>&1',
        'del "%~f0"',
    ]
    with open(script, 'w', encoding='ascii') as fh:
        fh.write('\r\n'.join(lines))
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    subprocess.Popen(['cmd', '/c', script], close_fds=True,
                     creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)


def _apply_unix(new_app, install, stage_root, pid, system, clean):
    script = os.path.join(tempfile.gettempdir(), f'vta_update_{pid}.sh')
    if system == 'Darwin':
        relaunch = f'open "{install}"'
    else:
        exe = os.path.join(install, os.path.basename(sys.executable))
        relaunch = f'"{exe}" &'
    if clean:
        # Mirror: replace the install folder outright (old-edition files go).
        install_cmds = [f'rm -rf "{install}"',
                        f'mkdir -p "{install}"',
                        f'cp -R "{new_app}/." "{install}"']
    else:
        # Overwrite/add without removing anything already present.
        install_cmds = [f'mkdir -p "{install}"',
                        f'cp -R "{new_app}/." "{install}"']
    lines = ['#!/bin/sh',
             f'while kill -0 {pid} 2>/dev/null; do sleep 1; done'] + install_cmds + [
        relaunch,
        f'rm -rf "{stage_root}"',
        'rm -f "$0"',
    ]
    with open(script, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines))
    os.chmod(script, 0o755)
    subprocess.Popen(['sh', script], start_new_session=True, close_fds=True)

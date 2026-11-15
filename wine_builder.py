#!/usr/bin/env python3
"""
Wine Builder for VitaTheme Architect

Downloads a minimal Wine installation for running at9tool.exe
on non-Windows platforms. Skips entirely on Windows.

- Linux: Downloads a pre-built portable Wine binary, then aggressively strips
         it down to the bare minimum needed for CLI-only at9tool execution.
- macOS: Downloads a pre-built Wine binary package, then strips it.
- Both: Downloads winetricks, initializes a Wine prefix, installs vcrun2008.

All privileged commands run via sudo; all user-level commands (wineboot,
winetricks) run via ``su - <username>`` so they execute under the correct
non-root account even when the script itself is launched as root.
"""
import os
import sys
import platform
import shutil
import subprocess
import tempfile
import tarfile
import ssl
import urllib.request

# Paths relative to project root
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.join(PROJECT_ROOT, 'Tools')
WINE_DIR = os.path.join(TOOLS_DIR, 'wine_minimal')
WINE_PREFIX = os.path.join(TOOLS_DIR, 'wine_prefix')

# Wine version (Linux — standard Wine)
WINE_VERSION = '9.0'

# Pre-built portable Wine for Linux (Kron4ek/Wine-Builds — standalone, no install needed)
LINUX_WINE_URL = f'https://github.com/Kron4ek/Wine-Builds/releases/download/{WINE_VERSION}/wine-{WINE_VERSION}-amd64.tar.xz'
LINUX_WINE_ARM64_URL = f'https://github.com/Kron4ek/Wine-Builds/releases/download/{WINE_VERSION}/wine-{WINE_VERSION}-aarch64.tar.xz'

# macOS: the official WineHQ macOS packages (Gcenx/macOS_Wine_builds). These are
# osx64 (x86-64) builds that run on Apple Silicon via Rosetta 2 (Rosetta must be
# installed). The former CrossOver source (Gcenx/winecx) has been discontinued;
# modern Wine handles the Apple-Silicon/Rosetta exec restrictions that once
# required CrossOver.
MACOS_WINECX_API_URL = 'https://api.github.com/repos/Gcenx/macOS_Wine_builds/releases?per_page=12'
MACOS_WINECX_FALLBACK_URL = 'https://github.com/Gcenx/macOS_Wine_builds/releases/download/11.21/wine-staging-11.21-osx64.tar.xz'

WINETRICKS_URL = 'https://raw.githubusercontent.com/Winetricks/winetricks/master/src/winetricks'

# ── Per-distro runtime dependency packages ───────────────────────────────
# Only runtime deps needed now (cabextract + wget for winetricks)
_LINUX_DEPS = {
    'apt-get': {
        'update': ['apt-get', 'update', '-qq'],
        'install': ['apt-get', 'install', '-y'],
        'runtime': ['cabextract', 'wget'],
    },
    'dnf': {
        'update': None,
        'install': ['dnf', 'install', '-y'],
        'runtime': ['cabextract', 'wget'],
    },
    'pacman': {
        'update': ['pacman', '-Sy', '--noconfirm'],
        'install': ['pacman', '-S', '--noconfirm', '--needed'],
        'runtime': ['cabextract', 'wget'],
    },
    'zypper': {
        'update': None,
        'install': ['zypper', 'install', '-y'],
        'runtime': ['cabextract', 'wget'],
    },
}


# ── Helpers ─────────────────────────────────────────────────────────────
def _make_ssl_context():
    """Create an SSL context, trying certifi first, then system certs, then unverified."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    ctx = ssl.create_default_context()
    try:
        ctx.load_default_certs()
    except Exception:
        pass
    # Test if the default context can reach github.com
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

def _download(url, dest, description=""):
    """Download a file from a URL with progress."""
    print(f"  Downloading {description or url}...")
    try:
        ssl_ctx = _make_ssl_context()
        req = urllib.request.Request(url, headers={'User-Agent': 'VitaThemeArchitect-WineBuilder'})
        with urllib.request.urlopen(req, timeout=120, context=ssl_ctx) as resp, open(dest, 'wb') as out:
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
                    print(f"\r  Progress: {pct}% ({downloaded // 1024}KB / {total // 1024}KB)",
                          end='', flush=True)
                print()
            else:
                out.write(resp.read())
        return True
    except Exception as e:
        print(f"  Download failed: {e}")
        return False


def _run_sudo(cmd, password):
    """Run a command with sudo (root privileges), piping the password via stdin."""
    proc = subprocess.Popen(
        ['sudo', '-S'] + cmd,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    stdout, stderr = proc.communicate(input=(password + '\n').encode())
    if stdout:
        for line in stdout.decode(errors='replace').strip().splitlines():
            print(f"    {line}")
    if proc.returncode != 0 and stderr:
        for line in stderr.decode(errors='replace').strip().splitlines():
            if 'password' not in line.lower():
                print(f"    {line}")
    return proc.returncode == 0


def _run_as_user(command_str, username, password, timeout=600, cwd=None):
    """Run a shell command as a specific (non-root) user via ``su``.

    This ensures Wine builds and prefix operations always run under a real
    user account, even when the script is launched as root or via sudo.
    """
    su_cmd = ['su', '-', username, '-c', command_str]
    if platform.system() == 'Darwin':
        # macOS su expects the password on stdin
        proc = subprocess.Popen(
            su_cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd
        )
        stdout, stderr = proc.communicate(input=(password + '\n').encode(), timeout=timeout)
    else:
        # Linux: use sudo -u which is more reliable than su for non-interactive use
        sudo_cmd = ['sudo', '-S', '-u', username]
        if cwd:
            sudo_cmd += ['--']
        sudo_cmd += ['bash', '-c', command_str]
        proc = subprocess.Popen(
            sudo_cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd
        )
        stdout, stderr = proc.communicate(input=(password + '\n').encode(), timeout=timeout)

    if stdout:
        for line in stdout.decode(errors='replace').strip().splitlines():
            print(f"    {line}")
    if proc.returncode != 0 and stderr:
        for line in stderr.decode(errors='replace').strip().splitlines():
            if 'password' not in line.lower() and 'sorry' not in line.lower():
                print(f"    {line}")
    return proc.returncode == 0


def _detect_pkg_manager():
    """Detect which package manager is available on this Linux system."""
    for name in ('apt-get', 'dnf', 'pacman', 'zypper'):
        if shutil.which(name):
            return name
    return None


# Packages this build auto-installed (absent beforehand). They are removed again
# once the build has copied what it needs, so the build leaves the machine as it
# found it — the libs it needs are already baked into the bundle.
_BUILD_INSTALLED_PKGS = []


def _pkg_installed(pkg, pkg_mgr=None):
    """True if a system package is currently installed (best-effort per distro)."""
    pkg_mgr = pkg_mgr or _detect_pkg_manager()
    try:
        if pkg_mgr == 'apt-get':
            return subprocess.run(['dpkg', '-s', pkg], capture_output=True).returncode == 0
        if pkg_mgr in ('dnf', 'zypper'):
            return subprocess.run(['rpm', '-q', pkg], capture_output=True).returncode == 0
        if pkg_mgr == 'pacman':
            return subprocess.run(['pacman', '-Q', pkg], capture_output=True).returncode == 0
    except Exception:
        pass
    return True  # unsure -> assume present, so we never remove something we didn't add


def _remove_linux_pkgs(pkgs, password):
    """Remove packages the build installed, then autoremove their orphaned deps."""
    pkgs = list(pkgs or [])
    if not pkgs:
        return
    pkg_mgr = _detect_pkg_manager()
    if not pkg_mgr:
        return
    remove = {
        'apt-get': ['apt-get', 'remove', '-y'],
        'dnf': ['dnf', 'remove', '-y'],
        'pacman': ['pacman', '-Rns', '--noconfirm'],
        'zypper': ['zypper', 'remove', '-y'],
    }[pkg_mgr]
    print(f"  Removing build-only packages (restoring the machine): {' '.join(pkgs)}")
    _run_sudo(remove + pkgs, password)
    if pkg_mgr == 'apt-get':
        _run_sudo(['apt-get', 'autoremove', '-y'], password)
    elif pkg_mgr == 'dnf':
        _run_sudo(['dnf', 'autoremove', '-y'], password)


# ── Dependency installation ─────────────────────────────────────────────
def _install_linux_runtime_deps(password):
    """Install runtime dependencies (cabextract, wget) needed by winetricks.

    Returns True if all packages were installed successfully.
    """
    pkg_mgr = _detect_pkg_manager()
    if not pkg_mgr:
        print("  WARNING: Could not detect a supported package manager. "
              "Ensure cabextract and wget are installed manually.")
        return True  # Non-fatal, winetricks might still work

    info = _LINUX_DEPS[pkg_mgr]
    packages = list(info['runtime'])
    # Track which were absent so build_wine can remove them afterwards.
    newly = [p for p in packages if not _pkg_installed(p, pkg_mgr)]

    print(f"  Package manager: {pkg_mgr}")
    print(f"  Runtime packages to install: {' '.join(packages)}")

    # Refresh index if needed
    if info['update']:
        print("  Refreshing package index...")
        if not _run_sudo(info['update'], password):
            print("  WARNING: Package index refresh failed; continuing anyway...")

    # Install packages
    print("  Installing Wine runtime dependencies...")
    if not _run_sudo(info['install'] + packages, password):
        print("  WARNING: Failed to install runtime dependencies. "
              "winetricks may not work correctly.")
        return True  # Non-fatal

    _BUILD_INSTALLED_PKGS.extend(newly)
    print("  Runtime dependencies installed successfully.")
    return True


def _find_wine_in_tree(root):
    """Recursively search for a wine/wine64 binary under root.
    Returns the directory containing 'bin/wine*' (i.e. the parent of bin/)."""
    for dirpath, _dirnames, filenames in os.walk(root):
        if os.path.basename(dirpath) == 'bin':
            for name in ('wine64', 'wine'):
                if name in filenames:
                    return os.path.dirname(dirpath)  # parent of bin/
    return None


# ── Linux: download pre-built ───────────────────────────────────────────
def _download_wine_linux(username, password):
    """Download a pre-built portable Wine binary for Linux.
    No compilation needed — just download, extract, and strip."""
    wine_bin = os.path.join(WINE_DIR, 'bin', 'wine64')
    wine_bin_alt = os.path.join(WINE_DIR, 'bin', 'wine')
    if os.path.exists(wine_bin) or os.path.exists(wine_bin_alt):
        print(f"  Bundled Wine already exists: {WINE_DIR}")
        return True

    # Install runtime deps (cabextract, wget) for winetricks
    _install_linux_runtime_deps(password)

    machine = platform.machine().lower()
    url = LINUX_WINE_ARM64_URL if machine in ('arm64', 'aarch64') else LINUX_WINE_URL
    print(f"  Architecture: {machine}")

    tmp = tempfile.mkdtemp()
    os.chmod(tmp, 0o755)

    try:
        archive = os.path.join(tmp, f'wine-{WINE_VERSION}.tar.xz')
        if not _download(url, archive, f'Wine {WINE_VERSION} (Linux pre-built)'):
            return False

        print("  Extracting Wine...")
        # Extract as user so files are owned correctly
        extract_cmd = f'tar xf {archive} -C {tmp}'
        if not _run_as_user(extract_cmd, username, password, timeout=120):
            # Fallback: extract in-process
            with tarfile.open(archive, 'r:xz') as tf:
                tf.extractall(tmp)

        # Find the extracted wine directory
        extracted = None
        for item in os.listdir(tmp):
            candidate = os.path.join(tmp, item)
            if os.path.isdir(candidate) and 'wine' in item.lower():
                extracted = candidate
                break

        if not extracted:
            if os.path.isdir(os.path.join(tmp, 'bin')):
                extracted = tmp
            else:
                print("  ERROR: Could not find Wine directory in extracted archive.")
                return False

        # Move to WINE_DIR
        if os.path.exists(WINE_DIR):
            shutil.rmtree(WINE_DIR)
        shutil.copytree(extracted, WINE_DIR)

        # Check standard location
        if os.path.exists(wine_bin) or os.path.exists(wine_bin_alt):
            print("  Wine downloaded and extracted successfully.")
            return True

        # Search for wine binary anywhere in the extracted tree
        wine_root = _find_wine_in_tree(WINE_DIR)
        if wine_root and wine_root != WINE_DIR:
            tmp_move = WINE_DIR + '_tmp'
            shutil.move(wine_root, tmp_move)
            shutil.rmtree(WINE_DIR)
            shutil.move(tmp_move, WINE_DIR)
            print("  Wine downloaded and extracted successfully.")
            return True

        print("  WARNING: Wine archive extracted but wine binary not found in expected location.")
        print(f"  Contents: {os.listdir(WINE_DIR)}")
        return False

    except Exception as e:
        print(f"  ERROR: Wine download/extraction failed: {e}")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── macOS: download CrossOver Wine ─────────────────────────────────────
def _get_crossover_wine_url():
    """Query the Gcenx/macOS_Wine_builds GitHub API for the latest Wine macOS
    package (osx64). Prefers a stable build, then staging, then any osx64 build.
    Falls back to a known-good version if the API is unreachable."""
    try:
        print("  Querying GitHub API for latest Wine macOS package...")
        ssl_ctx = _make_ssl_context()
        req = urllib.request.Request(MACOS_WINECX_API_URL,
                                    headers={'User-Agent': 'VitaThemeArchitect-WineBuilder'})
        with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as resp:
            import json
            releases = json.loads(resp.read().decode())
        # Collect osx64 assets from non-broken releases (API is newest-first).
        candidates = []
        for release in releases:
            if release.get('prerelease') or release.get('draft'):
                continue
            if 'broken' in release.get('name', '').lower():
                continue
            for asset in release.get('assets', []):
                if asset['name'].endswith('-osx64.tar.xz'):
                    candidates.append((release.get('tag_name', '?'),
                                       asset['name'], asset['browser_download_url']))
        # Prefer stable > staging > devel/other; stable sort keeps newest-first.
        def _rank(c):
            n = c[1].lower()
            return 0 if 'stable' in n else (1 if 'staging' in n else 2)
        candidates.sort(key=_rank)
        if candidates:
            tag, name, url = candidates[0]
            print(f"  Found Wine macOS release: {tag} - {name}")
            return url
    except Exception as e:
        print(f"  GitHub API query failed: {e}")

    print("  Using fallback Wine macOS URL (wine-staging 11.21)...")
    return MACOS_WINECX_FALLBACK_URL


def _download_wine_macos(username, password):
    """Download CrossOver Wine for macOS.

    CrossOver Wine includes CodeWeavers patches that fix the preloader for
    Apple Silicon — standard Wine's preloader cannot exec() under Rosetta 2
    due to ARM64 low-memory mapping restrictions.
    """
    wine_bin = os.path.join(WINE_DIR, 'bin', 'wine64')
    wine_bin_alt = os.path.join(WINE_DIR, 'bin', 'wine')
    if os.path.exists(wine_bin) or os.path.exists(wine_bin_alt):
        print(f"  Bundled Wine already exists: {WINE_DIR}")
        return True

    url = _get_crossover_wine_url()

    tmp = tempfile.mkdtemp()
    try:
        archive = os.path.join(tmp, 'wine-macos.tar.xz')
        if not _download(url, archive, 'CrossOver Wine (macOS)'):
            return False

        print("  Extracting CrossOver Wine...")
        os.makedirs(WINE_DIR, exist_ok=True)

        # Extract as user
        extract_cmd = f'tar xf {archive} -C {tmp}'
        if not _run_as_user(extract_cmd, username, password, timeout=120):
            # Fallback: extract in-process
            with tarfile.open(archive, 'r:xz') as tf:
                tf.extractall(tmp)

        # Find the extracted wine directory
        extracted = None
        for item in os.listdir(tmp):
            candidate = os.path.join(tmp, item)
            if os.path.isdir(candidate) and 'wine' in item.lower():
                extracted = candidate
                break

        if not extracted:
            if os.path.isdir(os.path.join(tmp, 'bin')):
                extracted = tmp
            else:
                print("  ERROR: Could not find Wine directory in extracted archive.")
                return False

        # Move to WINE_DIR
        if os.path.exists(WINE_DIR):
            shutil.rmtree(WINE_DIR)
        shutil.copytree(extracted, WINE_DIR)

        if os.path.exists(wine_bin) or os.path.exists(wine_bin_alt):
            print("  CrossOver Wine downloaded and extracted successfully.")
            return True

        # Search for wine binary anywhere in the extracted tree
        # Handles macOS .app bundles (Contents/Resources/wine/bin/),
        # usr/local/bin/, opt/wine/bin/, etc.
        wine_root = _find_wine_in_tree(WINE_DIR)
        if wine_root and wine_root != WINE_DIR:
            tmp_move = WINE_DIR + '_tmp'
            shutil.move(wine_root, tmp_move)
            shutil.rmtree(WINE_DIR)
            shutil.move(tmp_move, WINE_DIR)
            print("  CrossOver Wine downloaded and extracted successfully.")
            return True

        print("  WARNING: Wine archive extracted but wine binary not found in expected location.")
        print(f"  Contents: {os.listdir(WINE_DIR)}")
        return False

    except Exception as e:
        print(f"  ERROR: Wine download/extraction failed: {e}")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── Wine prefix setup ──────────────────────────────────────────────────
def _find_wine_binary():
    """Find the wine binary in the bundled installation."""
    for name in ('wine64', 'wine'):
        path = os.path.join(WINE_DIR, 'bin', name)
        if os.path.exists(path):
            return path
    return None


def _setup_wine_prefix(username, password):
    """Initialize Wine prefix and install vcrun2008 via winetricks.
    All commands run as *username* so the prefix is owned by that user."""
    if os.path.isdir(WINE_PREFIX) and os.path.exists(os.path.join(WINE_PREFIX, 'system.reg')):
        print(f"  Wine prefix already initialized: {WINE_PREFIX}")
        return True

    wine_bin = _find_wine_binary()
    if not wine_bin:
        print("  ERROR: Wine binary not found, cannot initialize prefix.")
        return False

    wine_bin_dir = os.path.join(WINE_DIR, 'bin')
    wine_env = os.environ.copy()
    wine_env['WINEPREFIX'] = WINE_PREFIX
    wine_env['WINEDEBUG'] = '-all'
    wine_env['PATH'] = wine_bin_dir + os.pathsep + wine_env.get('PATH', '')

    # Enable fast sync during prefix init so wineboot itself runs faster.
    # WINEMSYNC: CrossOver/Gcenx Wine on macOS (Mach port-based sync).
    # WINEESYNC: Wine-Staging/Kron4ek on Linux (eventfd-based sync).
    _system = platform.system()
    if _system == 'Darwin':
        wine_env['WINEMSYNC'] = '1'
        _sync_export = 'export WINEMSYNC=1 && '
    elif _system == 'Linux':
        wine_env['WINEESYNC'] = '1'
        _sync_export = 'export WINEESYNC=1 && '
    else:
        _sync_export = ''

    env_exports = (
        f'export WINEPREFIX="{WINE_PREFIX}" && '
        f'export WINEDEBUG=-all && '
        f'{_sync_export}'
        f'export PATH="{wine_bin_dir}:$PATH"'
    )

    # Initialize the prefix with wineboot
    print("  Initializing Wine prefix...")
    os.makedirs(WINE_PREFIX, exist_ok=True)

    prefix_created = False
    # Try direct execution first (works when script is run as the target user)
    try:
        result = subprocess.run(
            [wine_bin, 'wineboot', '--init'],
            env=wine_env, capture_output=True, timeout=120
        )
        if result.returncode == 0 or os.path.exists(os.path.join(WINE_PREFIX, 'system.reg')):
            prefix_created = True
            if result.returncode != 0:
                print("  wineboot returned non-zero but prefix was created.")
            else:
                print("  Wine prefix initialized.")
        else:
            stderr = result.stderr.decode(errors='replace').strip()
            if stderr:
                for line in stderr.splitlines()[-5:]:
                    print(f"    {line}")
    except subprocess.TimeoutExpired:
        print("  WARNING: wineboot timed out (direct).")
    except Exception as e:
        print(f"  WARNING: Direct wineboot failed: {e}")

    # Fallback: run via su (for cases where ownership matters)
    if not prefix_created:
        print("  Trying wineboot via su...")
        wineboot_cmd = f'{env_exports} && "{wine_bin}" wineboot --init'
        try:
            ok = _run_as_user(wineboot_cmd, username, password, timeout=120)
            if ok or os.path.exists(os.path.join(WINE_PREFIX, 'system.reg')):
                prefix_created = True
        except subprocess.TimeoutExpired:
            print("  WARNING: wineboot timed out (su).")
        except Exception as e:
            print(f"  WARNING: wineboot via su failed: {e}")

    if not prefix_created:
        print("  ERROR: Wine prefix was not created.")
        return False

    # Permanently disable Gecko and corefonts download prompts in the prefix
    # registry.  This is belt-and-suspenders with the WINEDLLOVERRIDES env var
    # set at runtime: even if the env var is missing, the prefix itself will
    # never prompt for a download.
    #
    # How it works:
    #   - Wine's DLL override system checks HKCU\Software\Wine\DllOverrides
    #     before loading any DLL.  Setting "mshtml"="" (empty = disabled) tells
    #     Wine to never load mshtml.dll, which is the only path that triggers
    #     the "install wine-gecko?" prompt.
    #   - Corefonts are only downloaded when a GUI app explicitly requests
    #     Windows core fonts via winetricks or a font-install trigger; disabling
    #     mshtml covers the most common case.  We also add a stub entry for the
    #     IE version key that winecfg/mshtml reads to decide whether to prompt.
    print("  Suppressing Gecko/corefonts download prompts in prefix registry...")
    _reg_suppressions = [
        # Disable mshtml (Gecko) — empty string = "disabled" in Wine DLL overrides
        ('HKCU\\Software\\Wine\\DllOverrides', 'mshtml', 'REG_SZ', ''),
        # Stub IE version so nothing thinks IE/Gecko is missing
        ('HKLM\\Software\\Microsoft\\Internet Explorer', 'Version', 'REG_SZ', '9.11.10.0'),
        ('HKLM\\Software\\Microsoft\\Internet Explorer', 'svcVersion', 'REG_SZ', '9.11.10.0'),
    ]
    for reg_key, reg_val, reg_type, reg_data in _reg_suppressions:
        try:
            reg_cmd = [wine_bin, 'reg', 'add', reg_key,
                       '/v', reg_val, '/t', reg_type, '/d', reg_data, '/f']
            subprocess.run(reg_cmd, env=wine_env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15)
        except Exception:
            pass

    # Download winetricks
    winetricks_path = os.path.join(TOOLS_DIR, 'winetricks')
    if not os.path.exists(winetricks_path):
        if not _download(WINETRICKS_URL, winetricks_path, 'winetricks'):
            print("  WARNING: Could not download winetricks. vcrun2008 will not be installed.")
            return True
        os.chmod(winetricks_path, 0o755)

    # Install vcrun2008 via winetricks
    print("  Installing vcrun2008 via winetricks...")
    # Try direct first
    try:
        result = subprocess.run(
            [winetricks_path, '-q', 'vcrun2008'],
            env=wine_env, capture_output=True, timeout=300
        )
        if result.returncode == 0:
            print("  vcrun2008 installed successfully.")
            return True
    except Exception:
        pass

    # Fallback to su
    winetricks_cmd = f'{env_exports} && "{winetricks_path}" -q vcrun2008'
    try:
        ok = _run_as_user(winetricks_cmd, username, password, timeout=300)
        if not ok:
            print("  WARNING: winetricks vcrun2008 returned non-zero (at9tool may still work)")
        else:
            print("  vcrun2008 installed successfully.")
    except subprocess.TimeoutExpired:
        print("  WARNING: winetricks timed out installing vcrun2008.")
    except Exception as e:
        print(f"  WARNING: winetricks failed: {e}")

    return True


# ── Post-install cleanup ────────────────────────────────────────────────

# DLLs that at9tool.exe needs to run under Wine (core Windows runtime).
# Everything else gets deleted.  Keep this whitelist minimal.
_KEEP_DLLS = {
    # Wine core / PE loader
    'ntdll.dll', 'kernel32.dll', 'kernelbase.dll',
    # C runtime — at9tool links msvcrt; keep common VC redists too
    'msvcrt.dll', 'ucrtbase.dll',
    'msvcp90.dll', 'msvcr90.dll', 'msvcp100.dll', 'msvcr100.dll',
    'msvcp110.dll', 'msvcr110.dll', 'msvcp120.dll', 'msvcr120.dll',
    'msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll',
    'concrt140.dll', 'api-ms-win-crt-runtime-l1-1-0.dll',
    'api-ms-win-crt-heap-l1-1-0.dll', 'api-ms-win-crt-math-l1-1-0.dll',
    'api-ms-win-crt-stdio-l1-1-0.dll', 'api-ms-win-crt-locale-l1-1-0.dll',
    'api-ms-win-crt-string-l1-1-0.dll', 'api-ms-win-crt-convert-l1-1-0.dll',
    'api-ms-win-crt-time-l1-1-0.dll', 'api-ms-win-crt-filesystem-l1-1-0.dll',
    'api-ms-win-crt-environment-l1-1-0.dll',
    'api-ms-win-crt-process-l1-1-0.dll',
    'api-ms-win-crt-utility-l1-1-0.dll',
    'api-ms-win-crt-multibyte-l1-1-0.dll',
    'api-ms-win-crt-conio-l1-1-0.dll',
    'api-ms-win-crt-private-l1-1-0.dll',
    # API set schema — Wine uses this to resolve api-ms-win-crt-* and
    # api-ms-win-core-* virtual DLL names to their real implementations
    # (ucrtbase.dll, kernelbase.dll, etc.).  Without it, every api-ms-win-*
    # lookup fails with c000000f, which prevents Wine-Mono from loading and
    # causes VGAudioCli.exe (net451) to exit with code 255.
    'apisetschema.dll',
    # User environment / profile — imported by services.exe.  Without it
    # services.exe crashes on startup which prevents kernel32.dll from loading
    # in any subsequent process (Wine's PE subsystem never initialises).
    'userenv.dll',
    # Windows base subsystems needed by most .exe
    'advapi32.dll', 'sechost.dll', 'bcrypt.dll', 'crypt32.dll',
    'rpcrt4.dll', 'combase.dll', 'ole32.dll', 'oleaut32.dll',
    'user32.dll', 'gdi32.dll', 'win32u.dll', 'imm32.dll',
    'version.dll', 'shlwapi.dll', 'shell32.dll',
    'setupapi.dll', 'cfgmgr32.dll',
    # Winsock — required by Wine-Mono's base class library (socket APIs).
    # Without ws2_32.dll, wineboot DLL init fails and Mono's BCL cannot load.
    'ws2_32.dll',
    # Windows Multimedia and Audio Compression Manager — referenced by
    # Wine-Mono's BCL and audio-related imports in VGAudioCli.
    'winmm.dll', 'msacm32.dll',
    # Needed for wineboot / prefix init and every Wine process launch
    'wineboot.exe', 'wineserver',
    # services.exe is Wine's service manager — launched automatically by
    # wineserver on every wine invocation.  Without it Wine cannot start
    # the Windows PE loader at all (exits with c0000135 / kernel32 not found).
    'services.exe',
    # plugplay.exe is the Plug-and-Play manager — also auto-started and
    # required for Wine's device initialisation before any DLL loads.
    'plugplay.exe',
    # winedevice.exe handles kernel device drivers at startup.
    'winedevice.exe',
    'cabinet.dll', 'msi.dll', 'fusion.dll',
    'nsi.dll', 'iphlpapi.dll', 'netapi32.dll',
    'shcore.dll', 'propsys.dll',
    'comctl32.dll', 'comdlg32.dll',
    # regsvr32 is used during prefix setup
    'regsvr32.exe',
    # .NET / Mono bootstrapper — required by VGAudioCli.exe (net451).
    # mscoree.dll is Wine's bridge to Wine-Mono; without it Wine returns
    # exit code 255 immediately when launching any .NET Framework exe.
    'mscoree.dll',
    # Debug helper — imported by mscoree.dll; without it mscoree fails to load
    # and VGAudioCli.exe (IL-only .NET binary) cannot start.
    'dbghelp.dll',
    # NT kernel — imported by winedevice.exe (device driver manager).
    # Missing ntoskrnl causes winedevice to crash on startup, which generates
    # service-protocol errors that can interfere with process startup.
    'ntoskrnl.exe',
}

# Unix .so loaders that Wine needs on the host side
_KEEP_UNIX_SOS = {
    'ntdll.so', 'win32u.so', 'nsiproxy.so',
    'mountmgr.so', 'bcrypt.so', 'crypt32.so', 'secur32.so',
    # macOS display driver (needed even headless for some init paths)
    'winemac.so',
    # Winsock native implementation — ws2_32.dll delegates to this .so.
    # Without it, ws2_32.dll fails to initialise and Wine-Mono's BCL
    # (which opens sockets internally) cannot start.
    'ws2_32.so',
}

# Host dylibs Wine actually links against for a CLI-only run
_KEEP_DYLIB_BASES = {
    'libfreetype', 'libz', 'libbz2', 'liblzma', 'libxml2',
    'libiconv', 'libpng', 'libpng16', 'libgnutls', 'libnettle',
    'libhogweed', 'libgmp', 'libintl', 'libtasn1', 'libidn2',
    'libunistring', 'libffi', 'libbrotlicommon', 'libbrotlidec',
    'libinotify',
}


def _cleanup_wine_installation(username, password):
    """Aggressively strip Wine to the bare minimum for CLI-only at9tool.exe.

    Uses a whitelist approach: only keep DLLs/SOs that at9tool and the Wine
    runtime actually need.  Everything else is deleted.
    """
    print("  Stripping Wine to minimal at9tool footprint...")
    total_saved = 0

    # ── 1. Nuke entire directories that are never needed ─────────────────
    dirs_to_wipe = {
        os.path.join(WINE_DIR, 'share', 'wine', 'gecko'): 'gecko (~100MB)',
        # Keep mono — needed for .NET Framework apps (VGAudioCli.exe net451)
        os.path.join(WINE_DIR, 'share', 'wine', 'fonts'): 'fonts',
        # Keep nls — Wine requires l_intl.nls to start
        os.path.join(WINE_DIR, 'share', 'man'): 'man pages',
        os.path.join(WINE_DIR, 'share', 'doc'): 'documentation',
        os.path.join(WINE_DIR, 'share', 'applications'): 'desktop entries',
        os.path.join(WINE_DIR, 'include'): 'C headers',
        # NOTE: i386-windows must NOT be deleted — VGAudioCli.exe is a 32-bit
        # PE (net451_standalone x86).  Wine's WoW64 layer loads 32-bit DLLs
        # from i386-windows/ when running a 32-bit exe under wine64.  Removing
        # this directory causes "could not load kernel32.dll" at startup.
        # os.path.join(WINE_DIR, 'lib', 'wine', 'i386-windows'): 'i386-windows (32-bit DLLs)',
    }
    for path, desc in dirs_to_wipe.items():
        if os.path.isdir(path):
            size = _dir_size(path)
            shutil.rmtree(path, ignore_errors=True)
            total_saved += size
            print(f"    Removed {desc} [{_fmt_size(size)}]")

    # ── 2. Whitelist-prune x86_64-windows/ — remove all DLLs not needed ──
    win64_dir = os.path.join(WINE_DIR, 'lib', 'wine', 'x86_64-windows')
    if os.path.isdir(win64_dir):
        removed_dll = 0
        removed_count = 0
        for fname in os.listdir(win64_dir):
            low = fname.lower()
            # Keep anything in the whitelist
            if low in _KEEP_DLLS:
                continue
            # Keep all api-ms-win-crt-* shims
            if low.startswith('api-ms-win-crt-'):
                continue
            # Keep all api-ms-win-core-* shims
            if low.startswith('api-ms-win-core-'):
                continue
            fpath = os.path.join(win64_dir, fname)
            if os.path.isfile(fpath):
                try:
                    size = os.path.getsize(fpath)
                    os.remove(fpath)
                    removed_dll += size
                    removed_count += 1
                except OSError:
                    pass
        if removed_dll:
            total_saved += removed_dll
            print(f"    Removed {removed_count} unnecessary x86_64 DLLs [{_fmt_size(removed_dll)}]")

    # ── 3. Whitelist-prune x86_64-unix/ — remove unused .so loaders ──────
    unix_dir = os.path.join(WINE_DIR, 'lib', 'wine', 'x86_64-unix')
    if os.path.isdir(unix_dir):
        removed_so = 0
        for fname in os.listdir(unix_dir):
            if fname in _KEEP_UNIX_SOS:
                continue
            fpath = os.path.join(unix_dir, fname)
            if os.path.isfile(fpath):
                try:
                    size = os.path.getsize(fpath)
                    os.remove(fpath)
                    removed_so += size
                except OSError:
                    pass
        if removed_so:
            total_saved += removed_so
            print(f"    Removed unnecessary unix SOs [{_fmt_size(removed_so)}]")

    # ── 4. Deduplicate dylibs (replace copies with symlinks) ─────────────
    #    and remove dylibs not needed for CLI Wine
    lib_dir = os.path.join(WINE_DIR, 'lib')
    if os.path.isdir(lib_dir):
        removed_dylib = 0
        deduped = 0
        dylibs = [f for f in os.listdir(lib_dir)
                   if f.endswith('.dylib') and os.path.isfile(os.path.join(lib_dir, f))]

        # Group dylibs by base name
        groups = {}
        for f in dylibs:
            # Extract base: libfoo from libfoo.6.2.dylib / libfoo.6.dylib / libfoo.dylib
            base = f.split('.')[0]
            groups.setdefault(base, []).append(f)

        for base, members in groups.items():
            if base not in _KEEP_DYLIB_BASES:
                # Remove entirely — not needed for CLI at9tool
                for f in members:
                    fpath = os.path.join(lib_dir, f)
                    try:
                        size = os.path.getsize(fpath)
                        os.remove(fpath)
                        removed_dylib += size
                    except OSError:
                        pass
            elif len(members) > 1:
                # Keep one real file, symlink the rest
                members.sort(key=lambda f: len(f))  # shortest name = canonical
                canonical = members[0]
                canonical_path = os.path.join(lib_dir, canonical)
                for f in members[1:]:
                    fpath = os.path.join(lib_dir, f)
                    if os.path.isfile(fpath) and not os.path.islink(fpath):
                        try:
                            size = os.path.getsize(fpath)
                            os.remove(fpath)
                            os.symlink(canonical, fpath)
                            deduped += size
                        except OSError:
                            pass
        if removed_dylib:
            total_saved += removed_dylib
            print(f"    Removed unnecessary dylibs [{_fmt_size(removed_dylib)}]")
        if deduped:
            total_saved += deduped
            print(f"    Deduplicated dylibs (symlinked copies) [{_fmt_size(deduped)}]")

    # ── 5. Remove static libraries (*.a) ─────────────────────────────────
    removed_static = 0
    for root, dirs, files in os.walk(WINE_DIR):
        for fname in files:
            if fname.endswith('.a'):
                fpath = os.path.join(root, fname)
                try:
                    size = os.path.getsize(fpath)
                    os.remove(fpath)
                    removed_static += size
                except OSError:
                    pass
    if removed_static:
        total_saved += removed_static
        print(f"    Removed static libraries [{_fmt_size(removed_static)}]")

    # ── 6. Remove unnecessary bin/ entries ────────────────────────────────
    bin_dir = os.path.join(WINE_DIR, 'bin')
    keep_bins = {'wine', 'wine64', 'wine-preloader', 'wine64-preloader',
                 'wineserver', 'wineboot', 'regsvr32'}
    if os.path.isdir(bin_dir):
        removed_bin = 0
        for fname in os.listdir(bin_dir):
            if fname not in keep_bins:
                fpath = os.path.join(bin_dir, fname)
                if os.path.isfile(fpath):
                    try:
                        size = os.path.getsize(fpath)
                        os.remove(fpath)
                        removed_bin += size
                    except OSError:
                        pass
        if removed_bin:
            total_saved += removed_bin
            print(f"    Removed unnecessary bin/ entries [{_fmt_size(removed_bin)}]")

    # ── 7. Strip debugging symbols from remaining shared libraries ───────
    #    IMPORTANT: Do NOT strip bin/ executables.  On Apple Silicon macOS,
    #    strip invalidates the ad-hoc code signature, making the binary
    #    unexecutable ("could not exec the wine loader").  Wine's preloader
    #    also has custom Mach-O segment layouts that strip can corrupt.
    strip_cmd = shutil.which('strip')
    if strip_cmd:
        print("    Stripping debug symbols (libs only, skipping bin/)...")
        for folder in ('lib', 'lib64'):
            target = os.path.join(WINE_DIR, folder)
            if os.path.isdir(target):
                for root, dirs, files in os.walk(target):
                    for fname in files:
                        fpath = os.path.join(root, fname)
                        if os.path.isfile(fpath) and not os.path.islink(fpath):
                            try:
                                subprocess.run(
                                    [strip_cmd, '-x', fpath],
                                    capture_output=True, timeout=10
                                )
                            except Exception:
                                pass
        # Re-sign stripped libraries on macOS (strip invalidates signatures)
        if platform.system() == 'Darwin':
            codesign = shutil.which('codesign')
            if codesign:
                print("    Re-signing stripped libraries...")
                for folder in ('lib', 'lib64'):
                    target = os.path.join(WINE_DIR, folder)
                    if os.path.isdir(target):
                        for root, dirs, files in os.walk(target):
                            for fname in files:
                                fpath = os.path.join(root, fname)
                                if os.path.isfile(fpath) and not os.path.islink(fpath):
                                    try:
                                        subprocess.run(
                                            [codesign, '--force', '--sign', '-', fpath],
                                            capture_output=True, timeout=10
                                        )
                                    except Exception:
                                        pass
        print("    Libraries stripped.")

    print(f"  Wine cleanup complete. Space saved: ~{_fmt_size(total_saved)}")


def _dir_size(path):
    """Calculate total size of a directory in bytes."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def _fmt_size(size_bytes):
    """Format byte count as human-readable string."""
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f}MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f}KB"
    return f"{size_bytes}B"


def _clean_wine_cache(username, password):
    """Clean Wine cache directories to free disk space.

    Removes temporary files Wine creates during prefix init and runtime.
    """
    print("  Cleaning Wine caches...")

    # Clean cache inside the Wine prefix
    cache_dirs = [
        os.path.join(WINE_PREFIX, 'drive_c', 'users', 'steamuser', 'Temp'),
        os.path.join(WINE_PREFIX, 'drive_c', 'users', 'steamuser', 'Local Settings',
                     'Temporary Internet Files'),
        os.path.join(WINE_PREFIX, 'drive_c', 'windows', 'temp'),
    ]

    # Also check for the actual username's temp dirs
    if username:
        cache_dirs.extend([
            os.path.join(WINE_PREFIX, 'drive_c', 'users', username, 'Temp'),
            os.path.join(WINE_PREFIX, 'drive_c', 'users', username, 'Local Settings',
                         'Temporary Internet Files'),
        ])

    # Clean user-level Wine cache (~/.cache/wine)
    home_dir = os.path.expanduser('~')
    wine_user_cache = os.path.join(home_dir, '.cache', 'wine')
    if os.path.isdir(wine_user_cache):
        cache_dirs.append(wine_user_cache)

    for cache_dir in cache_dirs:
        if os.path.isdir(cache_dir):
            try:
                shutil.rmtree(cache_dir, ignore_errors=True)
                os.makedirs(cache_dir, exist_ok=True)
                print(f"    Cleaned: {cache_dir}")
            except OSError:
                pass

    # Clean winetricks cache
    winetricks_cache = os.path.join(home_dir, '.cache', 'winetricks')
    if os.path.isdir(winetricks_cache):
        shutil.rmtree(winetricks_cache, ignore_errors=True)
        print("    Cleaned winetricks cache")

    print("  Wine cache cleanup complete.")


# ── Public entry point ──────────────────────────────────────────────────
def build_wine(username=None, password=None):
    """Main entry point: build/download Wine and set up the prefix.

    Args:
        username: OS username to run build/prefix commands as.
        password: Password for sudo (package installs) and su (user commands).
                  When running standalone and credentials are not supplied,
                  the user will be prompted.

    Returns True on success, False on failure. Skips entirely on Windows.
    """
    system = platform.system()

    if system == 'Windows':
        print("  Windows detected — Wine not needed (at9tool runs natively).")
        return True

    print(f"  Platform: {system} {platform.machine()}")

    # Prompt for credentials if not provided
    if username is None or password is None:
        import getpass
        print("  Wine setup requires credentials for installing packages and "
              "running build commands.")
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

    # Step 1: Get Wine binary (download pre-built for both platforms)
    if system == 'Linux':
        wine_ok = _download_wine_linux(username, password)
    elif system == 'Darwin':
        wine_ok = _download_wine_macos(username, password)
    else:
        print(f"  Unsupported platform: {system}")
        return False

    if not wine_ok:
        print("  ERROR: Failed to obtain Wine binary.")
        return False

    # Step 2: macOS-specific preparation
    if system == 'Darwin':
        # 2a: Install Rosetta 2 if on Apple Silicon (x86_64 Wine needs it)
        machine = platform.machine().lower()
        if machine in ('arm64', 'aarch64'):
            print("  Checking Rosetta 2 (required for x86_64 Wine on Apple Silicon)...")
            try:
                # Check if Rosetta is already installed
                check = subprocess.run(
                    ['arch', '-x86_64', '/usr/bin/true'],
                    capture_output=True, timeout=10
                )
                if check.returncode != 0:
                    raise FileNotFoundError
                print("  Rosetta 2 is already installed.")
            except Exception:
                print("  Installing Rosetta 2...")
                try:
                    subprocess.run(
                        ['softwareupdate', '--install-rosetta', '--agree-to-license'],
                        capture_output=True, timeout=120
                    )
                    print("  Rosetta 2 installed.")
                except Exception as e:
                    print(f"  WARNING: Could not install Rosetta 2: {e}")
                    print("  Wine may not work. Install manually: softwareupdate --install-rosetta")

        # 2b: Remove quarantine attributes so Gatekeeper doesn't block wine
        print("  Removing macOS quarantine attributes from Wine binaries...")
        try:
            subprocess.run(
                ['xattr', '-r', '-d', 'com.apple.quarantine', WINE_DIR],
                capture_output=True, timeout=60
            )
        except Exception:
            pass  # non-fatal — xattr may not be present or quarantine not set

        # 2c: Ensure wine binaries are executable
        wine_bin = _find_wine_binary()
        if wine_bin:
            for name in ('wine', 'wine64', 'wineboot', 'winecfg', 'wineserver'):
                p = os.path.join(os.path.dirname(wine_bin), name)
                if os.path.exists(p):
                    os.chmod(p, 0o755)

    # Step 3: Wine prefix — skipped at build time.
    # The prefix is now created at first app launch by startup_wine_warmup()
    # via _init_wine_prefix_if_needed().  Building it here would embed
    # absolute symlinks (pointing to build-machine paths) that break on any
    # other machine, and it would add ~10 MB of unnecessary bundle weight.

    # Remove winetricks if present from a previous build run.
    winetricks_path = os.path.join(TOOLS_DIR, 'winetricks')
    if os.path.exists(winetricks_path):
        print("  Removing winetricks (no longer needed)...")
        try:
            os.remove(winetricks_path)
        except OSError as e:
            print(f"  WARNING: Could not remove winetricks: {e}")

    # Step 4: Clean up Wine installation (remove unneeded files, strip binaries)
    #         Safe to strip now that the prefix is initialized
    _cleanup_wine_installation(username, password)

    # Step 5: Clean Wine caches (prefix init temp files, winetricks downloads)
    _clean_wine_cache(username, password)

    # Step 6: leave the machine as we found it — remove any runtime packages this
    # build auto-installed (the bundled Wine needs nothing further from them).
    try:
        if system == 'Linux' and _BUILD_INSTALLED_PKGS:
            _remove_linux_pkgs(list(_BUILD_INSTALLED_PKGS), password)
            _BUILD_INSTALLED_PKGS.clear()
    except Exception as e:
        print(f"  (package cleanup skipped: {e})")

    print("  Wine setup complete.")
    return True


if __name__ == '__main__':
    print("Wine Builder for VitaTheme Architect")
    print("=" * 40)
    success = build_wine()
    sys.exit(0 if success else 1)

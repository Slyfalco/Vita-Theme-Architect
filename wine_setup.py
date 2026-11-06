"""Wine environment setup for VitaThemeArchitect.

Handles persistent Wine installation management, including:
- Copying Wine from PyInstaller bundle to a persistent user directory
- Wine version stamping for update detection
- Wine warmup at application startup
- Wine environment and invocation helpers
"""
import sys
import os
import platform
import shutil
import subprocess
import datetime
import traceback


# =============================================================================
# == Path / Log Helpers (standalone — no imports from other project modules)
# =============================================================================

def _crash_log_path():
    """Return the path to crash.log (next to executable or script)."""
    if globals().get('__compiled__', False):
        return os.path.join(os.path.dirname(sys.executable), 'crash.log')
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'crash.log')


_CRASH_LOG_MAX_BYTES = 512 * 1024  # 500 KB before rotation
_CRASH_LOG_KEEP_LINES = 500


def _rotate_crash_log_if_needed(log_path):
    """Truncate crash.log to the last _CRASH_LOG_KEEP_LINES lines when it grows
    past _CRASH_LOG_MAX_BYTES, preventing unbounded disk usage."""
    try:
        if os.path.exists(log_path) and os.path.getsize(log_path) > _CRASH_LOG_MAX_BYTES:
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
            kept = lines[-_CRASH_LOG_KEEP_LINES:] if len(lines) > _CRASH_LOG_KEEP_LINES else lines
            with open(log_path, 'w', encoding='utf-8') as f:
                f.write(f"--- Log rotated at {datetime.datetime.now()} "
                        f"(kept last {len(kept)} lines) ---\n")
                f.writelines(kept)
    except Exception:
        pass


def _log_error(context, message, exc=None):
    """Log an error to both stdout and crash.log with diagnostic details."""
    timestamp = datetime.datetime.now().isoformat()
    lines = [
        f"\n--- {timestamp} [wine_setup/{context}] ---",
        f"  Platform: {platform.system()} {platform.machine()}",
        f"  Compiled: {globals().get('__compiled__', False)}",
        f"  Error: {message}",
    ]
    if exc:
        lines.append(f"  Exception: {type(exc).__name__}: {exc}")
        lines.append(f"  Traceback:\n{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))}")

    log_text = '\n'.join(lines) + '\n'
    print(log_text)
    try:
        log_path = _crash_log_path()
        _rotate_crash_log_if_needed(log_path)
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(log_text)
    except Exception:
        pass


def _tools_dir():
    """Return the path to the bundled Tools directory (VGAudioCli, etc.)."""
    if globals().get('__compiled__', False):
        if hasattr(sys, '_MEIPASS'):
            return os.path.join(sys._MEIPASS, 'Tools')
        return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), 'Tools')
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Tools')


# =============================================================================
# == Persistent Wine Directory
# =============================================================================
# On macOS Apple Silicon, x86_64 Wine binaries cannot be exec'd from
# PyInstaller's _MEIPASS temp directory (/var/folders/...) due to macOS
# security restrictions on Rosetta 2 execution from transient paths.
# We copy Wine to a persistent user directory on first use.
# =============================================================================
_PERSISTENT_WINE_DIR = None
_wineserver_started = False


def _persistent_app_dir():
    """Return a persistent application support directory for Wine."""
    system = platform.system()
    if system == 'Darwin':
        base = os.path.join(os.path.expanduser('~'), 'Library',
                            'Application Support', 'VitaThemeArchitect')
    elif system == 'Linux':
        xdg = os.environ.get('XDG_DATA_HOME', os.path.join(os.path.expanduser('~'), '.local', 'share'))
        base = os.path.join(xdg, 'VitaThemeArchitect')
    else:
        return None
    return base


def _wine_version_stamp(wine_dir):
    """Read or compute a version stamp for a Wine installation.

    Covers both bin/ and lib/wine/x86_64-windows/ so that adding or
    removing DLLs (e.g. mscoree.dll) triggers a persistent-copy refresh.
    """
    total = 0
    file_count = 0
    for subdir in ('bin', os.path.join('lib', 'wine', 'x86_64-windows')):
        d = os.path.join(wine_dir, subdir)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            fpath = os.path.join(d, name)
            if os.path.isfile(fpath):
                try:
                    total += os.path.getsize(fpath)
                    file_count += 1
                except OSError:
                    pass
    if file_count == 0:
        return '0'
    return f"{file_count}:{total}"


def _setup_persistent_wine():
    """Copy Wine from _MEIPASS to a persistent directory if needed.

    Returns the persistent wine_minimal directory, or None if not applicable.
    On non-frozen (development) runs, returns None (use Tools/ directly).
    """
    global _PERSISTENT_WINE_DIR
    if _PERSISTENT_WINE_DIR is not None:
        return _PERSISTENT_WINE_DIR if _PERSISTENT_WINE_DIR != '' else None

    # Only needed for frozen (PyInstaller) builds on non-Windows
    if not globals().get('__compiled__', False) or platform.system() == 'Windows':
        _PERSISTENT_WINE_DIR = ''
        return None

    bundled_tools = _tools_dir()
    bundled_wine = os.path.join(bundled_tools, 'wine_minimal')
    if not os.path.isdir(bundled_wine):
        _PERSISTENT_WINE_DIR = ''
        return None

    app_dir = _persistent_app_dir()
    if not app_dir:
        _PERSISTENT_WINE_DIR = ''
        return None

    persistent_wine = os.path.join(app_dir, 'wine_minimal')
    persistent_prefix = os.path.join(app_dir, 'wine_prefix')
    stamp_file = os.path.join(app_dir, '.wine_stamp')

    # Check if persistent copy is up-to-date
    bundled_stamp = _wine_version_stamp(bundled_wine)
    existing_stamp = ''
    if os.path.exists(stamp_file):
        try:
            with open(stamp_file, 'r') as f:
                existing_stamp = f.read().strip()
        except OSError:
            pass

    if existing_stamp == bundled_stamp and os.path.isdir(persistent_wine):
        _PERSISTENT_WINE_DIR = persistent_wine
        _log_error('setup_persistent_wine',
                    f"Using existing persistent Wine at {persistent_wine}")
        return persistent_wine

    # Copy Wine to persistent location
    try:
        print("  Setting up Wine in persistent location (first run)...")
        os.makedirs(app_dir, exist_ok=True)

        # Copy wine_minimal
        if os.path.exists(persistent_wine):
            shutil.rmtree(persistent_wine)
        shutil.copytree(bundled_wine, persistent_wine)

        # wine_prefix is NOT copied from the bundle — it is created at first
        # launch by _init_wine_prefix_if_needed() in startup_wine_warmup().

        # Ensure executable permissions, clear quarantine, and ad-hoc
        # sign on macOS.  Use absolute path for codesign because frozen
        # PyInstaller builds often have a PATH that omits /usr/bin/.
        wine_bin_dir = os.path.join(persistent_wine, 'bin')
        is_mac = platform.system() == 'Darwin'
        codesign = None
        if is_mac:
            for cs_path in ('/usr/bin/codesign',):
                if os.path.isfile(cs_path):
                    codesign = cs_path
                    break
            if not codesign:
                codesign = shutil.which('codesign')

        if os.path.isdir(wine_bin_dir):
            # Clear quarantine on entire directory (macOS)
            if is_mac:
                try:
                    subprocess.run(['/usr/bin/xattr', '-cr', persistent_wine],
                                   capture_output=True, timeout=30)
                except Exception:
                    pass

            for name in os.listdir(wine_bin_dir):
                fpath = os.path.join(wine_bin_dir, name)
                if os.path.isfile(fpath):
                    try:
                        os.chmod(fpath, 0o755)
                    except OSError:
                        pass
                    if codesign:
                        try:
                            subprocess.run(
                                [codesign, '--force', '--sign', '-', fpath],
                                capture_output=True, timeout=10
                            )
                        except Exception:
                            pass

        # Also sign shared libraries (may lose signatures during copy)
        if codesign:
            for lib_sub in ('lib', 'lib64'):
                lib_dir = os.path.join(persistent_wine, lib_sub)
                if os.path.isdir(lib_dir):
                    for root, _dirs, files in os.walk(lib_dir):
                        for fname in files:
                            fpath = os.path.join(root, fname)
                            if os.path.isfile(fpath) and not os.path.islink(fpath):
                                try:
                                    subprocess.run(
                                        [codesign, '--force', '--sign', '-',
                                         fpath],
                                        capture_output=True, timeout=10
                                    )
                                except Exception:
                                    pass

        # Verify wine64 actually runs after setup
        wine64_bin = os.path.join(wine_bin_dir, 'wine64')
        wine_ok = False
        diag_parts = []
        if os.path.isfile(wine64_bin):
            try:
                test_env = os.environ.copy()
                test_env['WINEDEBUG'] = '-all'
                test_env['WINEPREFIX'] = persistent_prefix
                wine_lib = os.path.join(persistent_wine, 'lib')
                if os.path.isdir(wine_lib):
                    test_env['DYLD_FALLBACK_LIBRARY_PATH'] = wine_lib
                vr = subprocess.run(
                    [wine64_bin, '--version'],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=test_env, timeout=15,
                    start_new_session=True
                )
                diag_parts.append(f"wine64 --version: exit={vr.returncode}, "
                                  f"out={vr.stdout.decode(errors='replace').strip()!r}, "
                                  f"err={vr.stderr.decode(errors='replace').strip()!r}")
                wine_ok = vr.returncode == 0
            except Exception as ve:
                diag_parts.append(f"wine64 --version failed: {ve}")

        # Write stamp
        with open(stamp_file, 'w') as f:
            f.write(bundled_stamp)

        _PERSISTENT_WINE_DIR = persistent_wine
        _log_error('setup_persistent_wine',
                    f"Wine copied to persistent location: {persistent_wine}"
                    f"\n  codesign binary: {codesign}"
                    f"\n  wine64 verified: {wine_ok}"
                    f"\n  {'  '.join(diag_parts)}")
        return persistent_wine

    except Exception as e:
        _log_error('setup_persistent_wine',
                    f"Failed to set up persistent Wine directory: {e}", exc=e)
        _PERSISTENT_WINE_DIR = ''
        return None


def _wine_tools_dir():
    """Return the directory containing Wine (persistent on frozen macOS/Linux,
    otherwise the bundled Tools/ directory)."""
    persistent = _setup_persistent_wine()
    if persistent:
        # Return parent dir of wine_minimal (the app_dir acts as "tools")
        return os.path.dirname(persistent)
    return _tools_dir()


def _find_bundled_wine():
    """Find the Wine binary, preferring the persistent copy."""
    wine_tools = _wine_tools_dir()
    wine_bin_dir = os.path.join(wine_tools, 'wine_minimal', 'bin')
    for name in ('wine64', 'wine'):
        path = os.path.join(wine_bin_dir, name)
        if os.path.exists(path):
            return path
    return None


def _populate_system32_symlinks(wine_tools):
    """Symlink Wine built-in DLLs into the Wine prefix's system32 directory.

    On macOS under Rosetta 2, ntdll.so's dladdr()-based self-discovery of Wine's
    own DLL directory fails in child processes (those spawned by wineserver rather
    than by the initial wine64 invocation).  The result is that Wine cannot locate
    kernel32.dll and other built-ins, causing every Windows subprocess to exit
    with 'could not load kernel32.dll, status c0000135'.

    The workaround is to place the built-in DLLs where Wine's filesystem search
    *always* succeeds: drive_c/windows/system32/.  We use symlinks rather than
    copies so no extra disk space is used and the files stay in sync with the
    wine_minimal directory.
    """
    wine_prefix = os.path.join(wine_tools, 'wine_prefix')
    sys32 = os.path.join(wine_prefix, 'drive_c', 'windows', 'system32')
    if not os.path.isdir(sys32):
        return  # prefix not yet initialised — nothing to do

    dll_dirs = [
        os.path.join(wine_tools, 'wine_minimal', 'lib', 'wine', 'x86_64-windows'),
    ]
    for dll_dir in dll_dirs:
        if not os.path.isdir(dll_dir):
            continue
        for name in os.listdir(dll_dir):
            if not (name.endswith('.dll') or name.endswith('.exe')):
                continue
            dest = os.path.join(sys32, name)
            if os.path.lexists(dest):
                continue  # already present (file or symlink)
            src = os.path.join(dll_dir, name)
            try:
                os.symlink(src, dest)
            except OSError:
                pass


def _wine_env():
    """Return an env dict configured for Wine."""
    wine_tools = _wine_tools_dir()
    env = os.environ.copy()

    wine_prefix = os.path.join(wine_tools, 'wine_prefix')
    # Always set WINEPREFIX so Wine creates the prefix here rather than
    # falling back to ~/.wine (user root).
    env['WINEPREFIX'] = wine_prefix
    env['WINEDEBUG'] = '-all'

    # Suppress Wine's auto-download dialogs for Gecko (mshtml) and corefonts.
    # VGAudioCli/at9tool are CLI tools — they never use HTML rendering or Windows
    # system fonts. Setting mshtml= (empty override) prevents mshtml.dll from
    # loading at all, which blocks Wine's gecko-installation check entirely.
    # Also disable gecko and mono install prompts via Wine-specific env vars.
    existing_overrides = env.get('WINEDLLOVERRIDES', '')
    gecko_override = 'mshtml='
    env['WINEDLLOVERRIDES'] = (gecko_override + ';' + existing_overrides).rstrip(';') if existing_overrides else gecko_override
    # Tell Wine's Gecko/Mono installer to stay silent — these are no-ops if the
    # env vars are unrecognised, but recognised builds will skip the download UI.
    env['WINE_SKIP_GECKO_INSTALLATION'] = '1'
    env['WINE_SKIP_MONO_INSTALLATION'] = '1'

    # Enable fast synchronization to avoid slow wineserver round-trips.
    # WINEMSYNC: CrossOver/Gcenx Wine on macOS — Mach port-based sync.
    # WINEESYNC: Wine-Staging/Kron4ek on Linux — eventfd-based sync.
    # Without these, Wine uses "server-side synchronization" which serializes
    # every lock operation through wineserver, causing significant latency.
    if platform.system() == 'Darwin':
        env['WINEMSYNC'] = '1'
    elif platform.system() == 'Linux':
        env['WINEESYNC'] = '1'

    # Add wine to PATH
    wine_bin_dir = os.path.join(wine_tools, 'wine_minimal', 'bin')
    if os.path.isdir(wine_bin_dir):
        env['PATH'] = wine_bin_dir + os.pathsep + env.get('PATH', '')

    # Wine needs its bundled shared libs. On macOS, SIP strips DYLD_LIBRARY_PATH
    # from child processes, so use DYLD_FALLBACK_LIBRARY_PATH instead.
    wine_lib = os.path.join(wine_tools, 'wine_minimal', 'lib')
    if os.path.isdir(wine_lib):
        env['DYLD_FALLBACK_LIBRARY_PATH'] = wine_lib + os.pathsep + env.get('DYLD_FALLBACK_LIBRARY_PATH', '')
        env['LD_LIBRARY_PATH'] = wine_lib + os.pathsep + env.get('LD_LIBRARY_PATH', '')

    # On macOS under Rosetta 2, ntdll.so's dladdr()-based self-discovery of
    # Wine's DLL directory fails, so Wine cannot find its own built-in DLLs
    # (kernel32.dll etc.) without an explicit hint.  WINEDLLPATH tells Wine
    # exactly where its x86_64-windows PE DLLs live, which fixes the
    # "could not load kernel32.dll" crash when running VGAudioCli.exe.
    wine_dlldir = os.path.join(wine_tools, 'wine_minimal', 'lib', 'wine', 'x86_64-windows')
    if os.path.isdir(wine_dlldir):
        env['WINEDLLPATH'] = wine_dlldir + os.pathsep + env.get('WINEDLLPATH', '')

    return env


def _wine_invocation(wine_path):
    """Return the command prefix to invoke wine correctly on this platform.

    On macOS Apple Silicon (arm64), native Python spawns wine64 (x86_64)
    via Rosetta 2.  When wine64 then tries to exec wine64-preloader, the
    Rosetta exec chain can fail ("could not exec the wine loader") because
    the child process context was not set up for x86_64 from the start.

    Wrapping the call with ``/usr/bin/arch -x86_64`` forces macOS to
    establish a proper x86_64 process context before handing control to
    wine64, so the subsequent wine64→wine64-preloader exec succeeds.
    """
    if platform.system() == 'Darwin' and platform.machine() in ('arm64', 'aarch64'):
        arch_bin = '/usr/bin/arch'
        if os.path.isfile(arch_bin):
            return [arch_bin, '-x86_64', wine_path]
    return [wine_path]


def _init_wine_prefix_if_needed(wine_tools, env, wine64_bin, status_callback=None):
    """Initialize the Wine prefix at first launch if it does not exist yet.

    The prefix is no longer bundled with the app (it was removed from the
    Nuitka includes in setup_build.py).  Instead, we run ``wineboot --init``
    on first launch to create a minimal prefix inside the persistent app
    support directory.

    A stamp file (<prefix>/.init_done) is written after successful init so
    that subsequent launches skip this step entirely.

    All Wine dialog/download prompts are suppressed via the env vars already
    set in ``env`` (WINE_SKIP_GECKO_INSTALLATION, WINE_SKIP_MONO_INSTALLATION,
    WINEDLLOVERRIDES=mshtml=).
    """
    wine_prefix = os.path.join(wine_tools, 'wine_prefix')
    stamp = os.path.join(wine_prefix, '.init_done')

    if os.path.isfile(stamp):
        return  # already initialized on a previous launch

    if status_callback:
        status_callback("Initializing Wine prefix (first run)...")

    # Create the directory structure that wineboot expects to find.
    sys32 = os.path.join(wine_prefix, 'drive_c', 'windows', 'system32')
    try:
        os.makedirs(sys32, exist_ok=True)
    except OSError as e:
        _log_error('init_wine_prefix', f"Could not create system32 dir: {e}", exc=e)
        return

    # Populate system32/ with DLL symlinks BEFORE wineboot runs so that Wine
    # can find its built-ins via filesystem lookup (the dladdr() self-discovery
    # path fails under Rosetta 2 for child processes).
    _populate_system32_symlinks(wine_tools)

    # Run wineboot --init to create registry hives, user directories, etc.
    # start_new_session=True detaches from our process group so any GUI
    # dialogs Wine might attempt to open go to a separate session and cannot
    # block our main process.
    try:
        subprocess.run(
            _wine_invocation(wine64_bin) + ['wineboot', '--init'],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            start_new_session=True,
        )
    except Exception as e:
        _log_error('init_wine_prefix', f"wineboot --init failed: {e}", exc=e)
        return

    # Re-run symlink population in case wineboot created or replaced files in
    # system32/ that we should also have symlinked.
    _populate_system32_symlinks(wine_tools)

    # Write stamp so we skip wineboot on all future launches.
    try:
        with open(stamp, 'w') as f:
            f.write('1')
    except OSError:
        pass


def startup_wine_warmup(status_callback=None):
    """Run Wine setup and warmup synchronously at app startup.

    Called from the splash screen so Wine is ready before the main window
    appears.  On Windows this is a no-op.

    status_callback: optional callable(str) — receives progress messages.
    """
    global _wineserver_started
    if platform.system() == 'Windows':
        return

    if status_callback:
        status_callback("Setting up Wine environment...")

    # Ensure persistent Wine dir is ready (copies Wine on first run)
    _setup_persistent_wine()

    if status_callback:
        status_callback("Warming up Wine...")

    wine_tools = _wine_tools_dir()
    wine_bin_dir = os.path.join(wine_tools, 'wine_minimal', 'bin')
    wine64_bin = os.path.join(wine_bin_dir, 'wine64')
    if not os.path.isfile(wine64_bin):
        _wineserver_started = True
        return

    env = _wine_env()

    # Step 0 — initialize wine prefix if this is the first launch, otherwise
    # just ensure system32/ symlinks are up-to-date (no-op if all present).
    # _init_wine_prefix_if_needed() calls _populate_system32_symlinks() itself;
    # we call _populate_system32_symlinks() directly here as well so that any
    # new DLLs added by a wine_minimal update are linked even on subsequent
    # launches (when the prefix already exists and wineboot is skipped).
    _init_wine_prefix_if_needed(wine_tools, env, wine64_bin, status_callback)
    _populate_system32_symlinks(wine_tools)

    # Step 1 — run wine64 --version to load the Wine loader into the OS page cache
    # and verify that the binary executes correctly.
    try:
        subprocess.run(
            _wine_invocation(wine64_bin) + ['--version'],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            start_new_session=True,
        )
    except Exception:
        pass

    # Step 2 — keep wineserver alive with the persistent flag (-p).
    # Without this, wineserver auto-exits ~3 seconds after the last client
    # disconnects. By the time the user triggers a conversion, wineserver has
    # already exited and has to cold-start again, causing the same startup lag
    # we were trying to avoid. -p keeps it running until explicitly shut down
    # or until Wine itself replaces it.
    wineserver_bin = os.path.join(wine_bin_dir, 'wineserver')
    if os.path.isfile(wineserver_bin):
        try:
            subprocess.Popen(
                [wineserver_bin, '-p'],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            pass

    # Step 3 — pre-warm Wine-Mono's JIT by running VGAudioCli.exe at startup.
    # VGAudioCli is a .NET Framework 4.5.1 app. On first invocation Wine-Mono
    # has to JIT-compile all referenced assemblies from scratch, which on macOS
    # Apple Silicon (Rosetta + Wine) can take 5–15 seconds. Running it here
    # (during the splash screen) hides that cost from the user.
    if status_callback:
        status_callback("Pre-warming audio engine...")
    vgaudio_exe = os.path.join(_tools_dir(), 'VGAudioCli.exe')
    if os.path.isfile(vgaudio_exe):
        try:
            subprocess.run(
                _wine_invocation(wine64_bin) + [vgaudio_exe],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=45,
                start_new_session=True,
            )
        except Exception:
            pass

    _wineserver_started = True


def shutdown_wine():
    """Stop all Wine processes and delete the persistent Wine directory.

    Called on app exit (non-Windows only).  Order:
    1. Ask wineserver to exit cleanly (-k / --kill).
    2. SIGKILL any remaining wine64/wineserver processes that belong to our
       WINEPREFIX so we don't leave orphaned server sockets.
    3. Delete the persistent Wine directory so the next run starts fresh
       (avoids stale prefix / stamp mismatches after app updates).
    """
    if platform.system() == 'Windows':
        return

    wine_tools = _wine_tools_dir()
    wine_bin_dir = os.path.join(wine_tools, 'wine_minimal', 'bin')
    wine_prefix = os.path.join(wine_tools, 'wine_prefix')
    env = _wine_env()

    # Step 1 — ask wineserver to shut down gracefully
    wineserver_bin = os.path.join(wine_bin_dir, 'wineserver')
    if os.path.isfile(wineserver_bin):
        try:
            subprocess.run(
                [wineserver_bin, '-k'],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except Exception:
            pass

    # Step 2 — force-kill any stray wine64/wineserver processes that still
    # have our WINEPREFIX open.  We match by prefix path to avoid killing
    # unrelated Wine instances the user may have running.
    try:
        import psutil
        for proc in psutil.process_iter(['pid', 'name', 'environ']):
            try:
                pname = (proc.info.get('name') or '').lower()
                if pname not in ('wine64', 'wine', 'wineserver', 'wine-preloader',
                                 'wine64-preloader'):
                    continue
                penv = proc.info.get('environ') or {}
                if penv.get('WINEPREFIX') == wine_prefix:
                    proc.kill()
            except Exception:
                pass
    except ImportError:
        # psutil not available — fall back to pkill by name only
        for name in ('wineserver', 'wine64', 'wine'):
            try:
                subprocess.run(
                    ['pkill', '-9', '-x', name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
                )
            except Exception:
                pass

    # Step 3 — delete only the Wine-specific entries inside the app support dir.
    # Do NOT delete the entire app_dir — it also contains settings.json, Themes/,
    # Extras/, and Tools/ (at9tool.exe) which must survive across sessions.
    app_dir = _persistent_app_dir()
    if app_dir:
        for wine_entry in ('wine_minimal', 'wine_prefix', '.wine_stamp'):
            entry_path = os.path.join(app_dir, wine_entry)
            try:
                if os.path.isdir(entry_path):
                    shutil.rmtree(entry_path, ignore_errors=True)
                elif os.path.isfile(entry_path):
                    os.remove(entry_path)
            except Exception:
                pass

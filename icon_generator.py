"""
VitaTheme Architect - AI theme icon generator (optional build feature).

This module is only compiled in when setup_build.py runs with the icon AI
option enabled. main_window imports it inside try/except and hides the
feature when it is missing.

The heavy dependencies (PyTorch, diffusers, model weights) are never bundled.
Nothing is downloaded until the user explicitly installs the AI components
from the generator dialog. They go into a private Python runtime under
<app-support>/icon_ai/, and generation runs icon_ai_worker.py there as a
subprocess, so the main app (and its Nuitka build) never imports torch.
The user can remove the components again from the same dialog.
"""
import os
import sys
import json
import shutil
import platform
import subprocess
import tarfile
import tempfile
import random
import re
import time

import requests
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QCheckBox, QSlider, QSpinBox, QProgressBar, QPlainTextEdit, QMessageBox,
    QWidget, QScrollArea, QFormLayout, QComboBox, QFileDialog, QLineEdit, QListWidget, QListWidgetItem,
    QCompleter, QAbstractItemView, QStackedWidget, QProgressDialog
)
from PyQt6.QtGui import QPixmap, QIcon
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize, QTimer, QStringListModel

from constants import (APP_SUPPORT_DIR, SETTINGS_FILE, crypted, ONLINE_FEATURES_AVAILABLE, secure_url, theme_pages,
                       CURRENT_VERSION)
from widgets import play_done_sound, download_progress_text

# --- Locations ---
ICON_AI_DIR = os.path.join(APP_SUPPORT_DIR, 'icon_ai')
RUNTIME_DIR = os.path.join(ICON_AI_DIR, 'python')
MODELS_DIR = os.path.join(ICON_AI_DIR, 'models')
# Community icon sets downloaded as layout examples (release builds only).
EXAMPLES_DIR = os.path.join(ICON_AI_DIR, 'examples')
# Community icon sets (how other authors designed each app's icon, and their
# layouts) are fetched automatically in the background, a few per dialog
# opening, until this many are kept (~70 KB each).
COMMUNITY_EXAMPLE_TARGET = 24
COMMUNITY_EXAMPLES_PER_OPEN = 4
# theme.xml HomeProperty tag -> icon asset name (same mapping as main_window).
THEME_XML_ICON_TAGS = {
    'm_browser': 'icon_web.png', 'm_calendar': 'icon_calendar.png', 'm_camera': 'icon_photos.png',
    'm_email': 'icon_email.png', 'm_friend': 'icon_friend.png', 'm_hostCollabo': 'icon_cma.png',
    'm_message': 'icon_message.png', 'm_music': 'icon_music.png', 'm_near': 'icon_near.png',
    'm_parental': 'icon_parental.png', 'm_party': 'icon_party.png', 'm_power': 'icon_power.png',
    'm_ps3Link': 'icon_ps3link.png', 'm_ps4Link': 'icon_ps4link.png', 'm_settings': 'icon_settings.png',
    'm_trophy': 'icon_trophy.png', 'm_video': 'icon_video.png',
}
RUNTIME_MARKER = os.path.join(ICON_AI_DIR, 'runtime.json')
# Calibration log: one JSON line per finished run (hardware + settings +
# measured time and peak memory). Hardware-normalised, so a run measured on any
# machine can predict another, and the file can be exported/uploaded to improve
# the shipped estimates in a later update.
CALIBRATION_FILE = os.path.join(ICON_AI_DIR, 'calibration.dat')
# The local performance data is encrypted and bound to THIS installed copy with a
# random key generated once and kept beside it. So the stored file can't be read
# by eye or by another copy/machine, and stays tamper-proof. The only readable
# form is the separate file the user makes with the "Share with creator" button.
_LOCAL_KEY_FILE = os.path.join(ICON_AI_DIR, '.perf_key')
# Aggregated performance data shipped inside the release (read-only). Used for
# prediction, kept separate from the user's own local recordings so the shared
# file never re-exports what already ships in the release. Stored as a plain
# binary data file (encrypted + authenticated inside, but with no tell-tale
# "encrypted token" look), so it reads as an ordinary bundled resource rather
# than something to be suspicious of. See _ref_cipher / reference_records.
REFERENCE_DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Assets', 'icon_estimates.dat')
# Bump when the pip package set changes so existing installs get redone.
RUNTIME_VERSION = 2

# Private Python runtime (python-build-standalone, relocatable, ships pip).
PYTHON_BUILD_TAG = '20261013'
PYTHON_VERSION = '3.12.15'
PYTHON_URL = ('https://github.com/astral-sh/python-build-standalone/releases/download/'
              '{tag}/cpython-{ver}+{tag}-{triple}-install_only.tar.gz')
TORCH_CUDA_INDEX = 'https://download.pytorch.org/whl/cu126'
# CUDA 13 wheels are ~25% smaller (~1.9 GB vs ~2.5 GB) but need an R580+
# driver and a Turing-or-newer GPU (CUDA 13 dropped Maxwell/Pascal/Volta).
TORCH_CUDA13_INDEX = 'https://download.pytorch.org/whl/cu130'
CUDA13_MIN_DRIVER = {'Windows': (581, 0), 'Linux': (580, 65)}
CUDA13_MIN_COMPUTE = (7, 5)
TORCH_CPU_INDEX = 'https://download.pytorch.org/whl/cpu'
AI_PACKAGES = ['diffusers', 'transformers', 'accelerate', 'safetensors', 'pillow', 'onnxruntime']
# Intel Macs: the last PyTorch with x86_64 macOS wheels is 2.2.2, which needs
# an older numpy/diffusers stack.
INTEL_MAC_PACKAGES = ['torch==2.2.2', 'numpy<2', 'diffusers==0.30.3', 'transformers==4.44.2',
                      'accelerate==0.34.2', 'safetensors', 'pillow', 'onnxruntime==1.19.2']

ICON_NAMES = [
    'icon_web.png', 'icon_calendar.png', 'icon_photos.png', 'icon_email.png', 'icon_friend.png',
    'icon_cma.png', 'icon_message.png', 'icon_music.png', 'icon_near.png', 'icon_parental.png',
    'icon_party.png', 'icon_power.png', 'icon_ps3link.png', 'icon_ps4link.png', 'icon_settings.png',
    'icon_trophy.png', 'icon_video.png',
]
# The theme's wallpapers are the source of its style, colours and characters:
# fewer than this isn't enough to make icons that belong to the theme.
MIN_WALLPAPERS = 3
# The creator's placeholder title. The theme's real name is needed: it guides
# Gemini and the artwork search (which finds the theme's characters by name).
DEFAULT_THEME_TITLE = 'My New Theme'
EVENT_PREFIX = '@@VTA '  # must match icon_ai_worker.EVENT_PREFIX
ICON_PREVIEW = 128  # results are shown at the icons' real Vita size
# (label, icon_ai_worker.LAYOUT_STYLES key)
# (label, job['theme_color_mode']): how the theme's own colours (Colors tab) are used.
THEME_COLOR_CHOICES = [
    ("Auto: use when they match the wallpapers", 'auto'),
    ("Always use them", 'always'),
    ("Don't use them (wallpaper colours only)", 'never'),
]
LAYOUT_STYLE_CHOICES = [
    ("Mixed variety", 'mixed'),
    ("Like example icon sets", 'examples'),
    ("Character beside symbol", 'beside'),
    ("Character portrait behind symbol", 'portrait'),
    ("Symbol only", 'emblem'),
]
INSTALL_SIZE_MSG = ("This downloads a private Python runtime and PyTorch "
                    "(about 0.5 GB, or 2-2.5 GB with NVIDIA GPU support). On the first generation, "
                    "about 5.6 GB of AI models are downloaded (about 8.5 GB without a GPU).\n\n"
                    f"Everything is stored in:\n{ICON_AI_DIR}\n\n"
                    "You can remove it again from this dialog at any time.")

# Same reason as audio.py: no console window flashing for child processes.
_SUBPROCESS_FLAGS = (
    {'creationflags': subprocess.CREATE_NO_WINDOW}
    if platform.system() == 'Windows'
    else {}
)

# Source runs: next to this file. Nuitka (onefile/standalone/.app): data files
# sit next to the compiled module, so __file__'s directory works there too.
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _worker_script():
    return os.path.join(_BASE_DIR, 'icon_ai_worker.py')


def _templates_dir():
    """Built-in template icons (Assets/IconTemplates, bundled in every build)."""
    candidate = os.path.join(_BASE_DIR, 'Assets', 'IconTemplates')
    return candidate if os.path.isdir(candidate) else None


def _examples_dir():
    """Built-in example icon sets used as layout references (Assets/IconExamples/<set>/)."""
    candidate = os.path.join(_BASE_DIR, 'Assets', 'IconExamples')
    return candidate if os.path.isdir(candidate) else None


def _strip_runtime():
    """Remove parts of the private Python runtime the AI worker never uses (test
    suites, Tk/IDLE, 2to3, C headers, static libs), to save disk. Verified safe:
    torch, diffusers, transformers and onnxruntime import none of these. Only
    ever runs on the downloaded runtime, never on a reused app Python."""
    import glob as _glob
    freed = 0
    lib = os.path.join(RUNTIME_DIR, 'Lib')          # Windows layout
    if not os.path.isdir(lib):
        found = _glob.glob(os.path.join(RUNTIME_DIR, 'lib', 'python3.*'))
        lib = found[0] if found else None
    targets = []
    if lib:
        targets += [os.path.join(lib, d) for d in
                    ('test', 'tkinter', 'idlelib', 'turtledemo', 'lib2to3', 'pydoc_data', 'ensurepip')]
    targets.append(os.path.join(RUNTIME_DIR, 'tcl'))          # Tk GUI runtime (the worker is headless)
    targets += [os.path.join(RUNTIME_DIR, 'DLLs', f) for f in ('_tkinter.pyd', 'tcl86t.dll', 'tk86t.dll')]
    # Static embedding library (macOS/Linux): not needed to run the interpreter.
    targets += _glob.glob(os.path.join(RUNTIME_DIR, '**', 'libpython*.a'), recursive=True)
    # (C headers in include/ are kept, in case a dependency ever builds from source.)
    for t in targets:
        try:
            if os.path.isdir(t):
                freed += _folder_size(t)
                shutil.rmtree(t, ignore_errors=True)
            elif os.path.isfile(t):
                freed += os.path.getsize(t)
                os.remove(t)
        except OSError:
            pass
    return freed


def _use_app_python():
    """From source (not a Nuitka build) the app runs on a normal CPython with
    pip, so reuse it for the AI worker instead of downloading a second
    interpreter. A built app can't (its compiled interpreter can't host PyTorch)."""
    return '__compiled__' not in globals()


def _runtime_python():
    downloaded = (os.path.join(RUNTIME_DIR, 'python.exe') if platform.system() == 'Windows'
                  else os.path.join(RUNTIME_DIR, 'bin', 'python3'))
    # From source with no private runtime present, reuse the app's own Python so
    # development needs no second interpreter. An existing private runtime (or a
    # built app) is always used as-is.
    if _use_app_python() and not os.path.isfile(downloaded):
        return sys.executable
    return downloaded


def _install_bitsandbytes():
    """pip-install bitsandbytes into the AI runtime (needed by the FLUX.2 4-bit
    build). Returns True on success."""
    py = _runtime_python()
    if not os.path.isfile(py):
        return False
    try:
        r = subprocess.run([py, '-I', '-m', 'pip', 'install', '--progress-bar', 'off', 'bitsandbytes'],
                           capture_output=True, text=True, timeout=900, **_SUBPROCESS_FLAGS)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _package_installed(name):
    """True if `name` can be imported in the AI runtime (not the app's venv)."""
    py = _runtime_python()
    if not os.path.isfile(py):
        return False
    try:
        r = subprocess.run(
            [py, '-I', '-c', f"import importlib.util,sys; sys.exit(0 if importlib.util.find_spec({name!r}) else 1)"],
            capture_output=True, timeout=30, **_SUBPROCESS_FLAGS)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def is_runtime_installed():
    try:
        with open(RUNTIME_MARKER, 'r', encoding='utf-8') as f:
            marker = json.load(f)
    except (OSError, ValueError):
        return False
    return marker.get('version') == RUNTIME_VERSION and os.path.isfile(_runtime_python())


def runtime_needs_update():
    """An older runtime exists: updating keeps PyTorch and only installs new packages."""
    return os.path.isfile(_runtime_python()) and not is_runtime_installed()


def _python_triple():
    system, machine = platform.system(), platform.machine().lower()
    arch = {'amd64': 'x86_64', 'x86_64': 'x86_64', 'arm64': 'aarch64', 'aarch64': 'aarch64'}.get(machine)
    if not arch:
        return None
    if system == 'Windows':
        return f'{arch}-pc-windows-msvc'
    if system == 'Darwin':
        return f'{arch}-apple-darwin'
    if system == 'Linux':
        return f'{arch}-unknown-linux-gnu'
    return None


def _nvidia_smi():
    found = shutil.which('nvidia-smi')
    if found:
        return found
    if platform.system() == 'Windows':
        sys_root = os.environ.get('SystemRoot', r'C:\Windows')
        candidate = os.path.join(sys_root, 'System32', 'nvidia-smi.exe')
        if os.path.isfile(candidate):
            return candidate
    return None


def _parse_version(text):
    try:
        return tuple(int(part) for part in text.strip().split('.')[:2])
    except ValueError:
        return None


_GPU_VRAM = []


def gpu_vram_gb():
    """Total VRAM of the largest NVIDIA GPU in GB, or 0 if unknown; looked up
    once per app session (nvidia-smi takes ~40 ms a call)."""
    if not _GPU_VRAM:
        vram, smi = 0.0, _nvidia_smi()
        if smi:
            try:
                out = subprocess.run([smi, '--query-gpu=memory.total', '--format=csv,noheader,nounits'],
                                     capture_output=True, text=True, timeout=15, **_SUBPROCESS_FLAGS).stdout
                vram = max(float(v) for v in out.split()) / 1024
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
        _GPU_VRAM.append(vram)
    return _GPU_VRAM[0]


# (label, worker model key, minimum VRAM in GB to suggest it by default)
MODEL_CHOICES = [
    ("High quality (SDXL anime, 8 GB GPU)", 'sdxl', 7.5),
    ("Standard (SD 1.5, 4 GB GPU)", 'sd15', 0),
]
# FLUX.2: the large 'reference' backend (Phase 2), in two optional tiers. The
# 4-bit build runs on ~8-12 GB cards with group offloading (slower, large
# download); the full bf16 build is faster and higher quality on high-end GPUs
# (very large download). Non-commercial weights (the user accepts the licence the
# first time); outputs are theirs to use. (label, key, minimum VRAM to offer it.)
FLUX2_CHOICES = [
    ("Ultra (FLUX.2 4-bit — slower, for ~8–12 GB GPUs, large download)", 'flux2', 7.5),
    ("Ultra+ (FLUX.2 full — faster, for high-end GPUs, very large download)", 'flux2_full', 20.0),
]
FLUX2_KEYS = {k for _l, k, _v in FLUX2_CHOICES}
# With 'Use minimum memory' on, group offloading streams the model from the CPU,
# so the tiers run on much less GPU memory (the full tier fits ~8 GB) — at the
# cost of speed. These are the reduced VRAM requirements in that mode.
FLUX2_MIN_MEM_VRAM = {'flux2': 6.0, 'flux2_full': 8.0}
# Rough memory use per model, GB: (normal VRAM, Minimal-VRAM VRAM, Minimal-VRAM
# system RAM, normal system RAM). Minimal-VRAM offloads the model to system RAM,
# so VRAM drops and RAM rises. Measured where possible (SDXL minimal-VRAM = 1.6 GB
# VRAM / 10.8 GB RAM on a 12 GB 4070); the rest are approximate and informational.
MODEL_MEMORY = {
    'sd15':       (4,  1,  8,  6),
    'sdxl':       (8,  2, 16, 10),
    'flux2':      (16, 6, 24, 16),
    'flux2_full': (40, 8, 32, 24),
}


def memory_estimate(model, min_mem=False):
    """(VRAM GB, system RAM GB) the model is expected to use, in the current mode."""
    normal_vram, minvram_vram, minvram_ram, normal_ram = MODEL_MEMORY.get(model, (6, 2, 12, 8))
    return (minvram_vram, minvram_ram) if min_mem else (normal_vram, normal_ram)


def memory_note(model, min_mem=False):
    """A short memory label for a model in the current mode.

    In normal mode the model lives in VRAM, so only VRAM is the real
    requirement. The large system-RAM figure applies only to Minimal VRAM
    mode, where weights are offloaded to RAM, so it is shown only then.
    """
    vram, ram = memory_estimate(model, min_mem)
    if min_mem:
        return f"~{vram:g} GB VRAM + ~{ram:g} GB system RAM (offloaded)"
    return f"~{vram:g} GB VRAM"
FLUX2_LICENSE_URL = 'https://huggingface.co/black-forest-labs/FLUX.2-dev/blob/main/LICENSE.md'


def _supports_cuda13(smi):
    """True only if every GPU and the driver are confirmed CUDA 13 capable."""
    try:
        result = subprocess.run([smi, '--query-gpu=driver_version,compute_cap', '--format=csv,noheader'],
                                capture_output=True, text=True, timeout=15, **_SUBPROCESS_FLAGS)
    except (OSError, subprocess.SubprocessError):
        return False
    rows = [line.split(',') for line in result.stdout.strip().splitlines() if line.strip()]
    if result.returncode != 0 or not rows:
        return False
    min_driver = CUDA13_MIN_DRIVER.get(platform.system())
    for row in rows:
        if len(row) != 2:
            return False
        driver, compute = _parse_version(row[0]), _parse_version(row[1])
        if not (min_driver and driver and compute and driver >= min_driver and compute >= CUDA13_MIN_COMPUTE):
            return False
    return True


def _pip_install_commands():
    """pip install argument lists for the current platform."""
    system, machine = platform.system(), platform.machine().lower()
    if system == 'Darwin':
        if machine in ('x86_64', 'amd64'):
            return [INTEL_MAC_PACKAGES]
        return [['torch'] + AI_PACKAGES]  # PyPI wheels include MPS support
    smi = _nvidia_smi() if machine in ('x86_64', 'amd64') else None
    if smi:
        index = TORCH_CUDA13_INDEX if _supports_cuda13(smi) else TORCH_CUDA_INDEX
        torch_cmd = ['torch', '--index-url', index]
    else:
        torch_cmd = ['torch', '--index-url', TORCH_CPU_INDEX]
    return [torch_cmd, AI_PACKAGES]


def _models_dir():
    """Where Hugging Face models are kept: a user-chosen shared cache (to reuse
    models already downloaded by other apps and save disk), or the app's own
    icon_ai/models. The runtime (Python + torch) always stays in ICON_AI_DIR."""
    try:
        shared = (_settings().get('shared_model_cache') or '').strip()
    except Exception:
        shared = ''
    return shared if shared else MODELS_DIR


def _using_shared_cache():
    return _models_dir() != MODELS_DIR


def _child_env():
    """Environment for runtime subprocesses, free of the app's Python/Qt/lib vars."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('PYTHON', 'QT_', 'DYLD_')) and k != 'LD_LIBRARY_PATH'}
    env['HF_HOME'] = _models_dir()
    env['HF_HUB_DISABLE_TELEMETRY'] = '1'
    env['PIP_DISABLE_PIP_VERSION_CHECK'] = '1'
    # Plain HTTP downloads into the HF cache, so the worker can measure progress.
    env['HF_HUB_DISABLE_XET'] = '1'
    return env


# --- Download size / time estimates ----------------------------------------
SPEED_TEST_BYTES = 64 * 1024 * 1024
SPEED_TEST_SECONDS = 5
SPEED_WARMUP = 1.0
# Python runtime plus diffusers/transformers/numpy/etc. besides PyTorch.
LIBRARIES_ESTIMATE = 180 * 1024 * 1024
UPDATE_ESTIMATE = 20 * 1024 * 1024


def format_size(n):
    return f"{n / 1024 ** 3:.1f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB"


# Seconds before the first icon starts (model loading, wallpaper analysis) and
# per icon, until this PC has its own measurements (saved after every run).
MODEL_BYTES = {'sdxl': 7.4e9, 'sd15': 2.6e9, 'flux2': 16e9, 'flux2_full': 32e9}   # roughly what each model loads from disk
# (Measured on a 12 GB RTX card: SDXL ~4.5-5 min per set, SDXL + poses ~8.5-10 min
# with the memory-saving mode, Standard + poses ~2-3 min. FLUX.2 is a 32B model,
# much slower — rough starting guesses, refined after the first run on this PC.)
TIMING_DEFAULTS = {'local:sdxl': (25, 15), 'local:sdxl+poses': (45, 29), 'local:sd15': (12, 6),
                   'local:sd15+poses': (15, 8), 'local:flux2': (60, 75), 'local:flux2_full': (90, 110),
                   'google': (5, 20)}
# How much slower the minimal-VRAM path (sequential CPU offload) is per icon.
MINMEM_SLOWDOWN = 4.0


def timing_key(job, cloud):
    if cloud:
        return 'google'
    model = job.get('model', 'sd15')
    key = f"local:{model}"
    if job.get('pose_variation') and model in ('sd15', 'sdxl'):   # poses apply only to the scaffold models
        key += '+poses'
    if job.get('min_memory'):   # the minimal-VRAM path is timed (and estimated) separately: it's much slower
        key += '+minmem'
    return key


# Relative Stable Diffusion speed of common GPUs, with the RTX 4070 the default
# timings were measured on as 1.0 (approximate, from public SD benchmarks).
# Longer names first, so "4070 ti super" wins over "4070".
GPU_SPEED = [
    ('5090', 3.0), ('5080', 1.9), ('5070 ti', 1.6), ('5070', 1.2), ('5060 ti', 0.85), ('5060', 0.7),
    ('4090', 2.2), ('4080', 1.6), ('4070 ti super', 1.35), ('4070 ti', 1.25), ('4070 super', 1.15), ('4070', 1.0),
    ('4060 ti', 0.75), ('4060', 0.6),
    ('3090 ti', 1.25), ('3090', 1.15), ('3080 ti', 1.1), ('3080', 1.0), ('3070 ti', 0.75), ('3070', 0.7),
    ('3060 ti', 0.62), ('3060', 0.5), ('3050', 0.33),
    ('2080 ti', 0.6), ('2080', 0.48), ('2070', 0.4), ('2060', 0.33),
    ('1080 ti', 0.3), ('1660', 0.2), ('1070', 0.22), ('1060', 0.16),
]
APPLE_SPEED = {'m1': 0.15, 'm2': 0.2, 'm3': 0.25, 'm4': 0.3}
APPLE_TIER = {'ultra': 4.0, 'max': 2.5, 'pro': 1.6}


_GPU_NAME = []


def gpu_name():
    """The GPU (NVIDIA card or Apple chip) as named by the system, '' if none;
    looked up once per app session."""
    if not _GPU_NAME:
        name = ''
        try:
            if platform.system() == 'Darwin':
                name = subprocess.run(['sysctl', '-n', 'machdep.cpu.brand_string'], capture_output=True, text=True,
                                      timeout=10).stdout.strip()
            elif _nvidia_smi():
                name = subprocess.run([_nvidia_smi(), '--query-gpu=name', '--format=csv,noheader'], capture_output=True,
                                      text=True, timeout=15, **_SUBPROCESS_FLAGS).stdout.strip().splitlines()[0]
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
        _GPU_NAME.append(name)
    return _GPU_NAME[0]


def hardware_print():
    """What timings depend on: GPU, its memory, system and processor type.
    Measured timings only count while this stays the same."""
    return f"{gpu_name()}|{round(gpu_vram_gb())}|{platform.system()}|{platform.machine()}"


def gpu_speed():
    """This PC's GPU speed relative to the RTX 4070 the default timings were
    measured on (1.0), or None for an unlisted GPU."""
    name = gpu_name().lower()
    if platform.system() == 'Darwin':
        base = next((v for k, v in APPLE_SPEED.items() if k in name), None)
        if base is None:
            return None
        return base * next((v for k, v in APPLE_TIER.items() if k in name), 1.0)
    return next((v for k, v in GPU_SPEED if k in name), None) if name else None


def _saved_timing(key):
    """The measured (start-up, per icon) for this setup, only if it was
    measured on the hardware this PC has now (a new GPU makes it stale)."""
    saved = (_settings().get('icon_ai_timing') or {}).get(key)
    if saved and len(saved) >= 3 and saved[2] == hardware_print():
        return float(saved[0]), float(saved[1])
    return None


def timing_measured(key):
    """True once this PC (as it is now) has finished a run with this setup."""
    return _saved_timing(key) is not None


def load_timing(key):
    """(start-up seconds, seconds per icon) for this PC. In order of trust:
    a run measured on this exact hardware; else a run measured on *other*
    hardware (from the calibration log) scaled to this PC's GPU; else the
    shipped defaults (measured on a 12 GB NVIDIA card) adjusted for this PC's
    GPU and disk."""
    saved = _saved_timing(key)
    if saved:
        return saved
    # The minimal-VRAM variant estimates from its base setup, then slowed down.
    minmem = key.endswith('+minmem')
    base_key = key[:-len('+minmem')] if minmem else key
    startup, per_icon = TIMING_DEFAULTS.get(base_key, (20, 18))
    if key.startswith('local'):
        vram = gpu_vram_gb()
        apple = platform.system() == 'Darwin' and platform.machine().lower() in ('arm64', 'aarch64')
        speed = gpu_speed()
        # How much this PC's GPU scales a reference-GPU time (unlisted GPUs are
        # judged by their memory; no GPU is far slower).
        factor = (speed or 0.4) if apple else (0.1 if not vram else
                                               speed or (1.0 if vram >= 10 else 0.6 if vram >= 6 else 0.35))
        ref = _dataset_reference(base_key, minmem)   # measured on any hardware, reference-GPU-equivalent
        if ref:
            # Measured elsewhere: the per-icon figure already reflects this exact
            # mode (min-VRAM, poses), so just scale it to this PC's GPU.
            if ref[0]:
                startup = ref[0]
            per_icon = ref[1] / factor
            if apple:
                startup *= 1.5
            elif not vram:
                startup *= 3
        else:
            # No measurement anywhere: the shipped reference-GPU default, scaled,
            # with the mode penalties the defaults don't already include.
            per_icon /= factor
            if apple:
                startup *= 1.5
            elif not vram:
                startup *= 3
            elif minmem:
                per_icon *= MINMEM_SLOWDOWN   # offload to RAM streams layers — much slower
                startup *= 1.2
            elif 'sdxl' in base_key and '+poses' not in base_key and vram < 10:
                per_icon *= 1.8   # SDXL on under 10 GB runs in the slower memory-saving mode
        # Loading the models from disk dominates start-up; a slow drive (HDD)
        # adds to it. Uses the disk speed if it has been measured already.
        disk = _settings().get('icon_ai_disk_speed')
        if disk and disk[0]:
            model_bytes = MODEL_BYTES.get(base_key.split(':')[1].split('+')[0], 3e9) + (3.4e9 if '+poses' in base_key else 0)
            startup = max(startup, 10 + model_bytes / float(disk[0]))
    return float(startup), float(per_icon)


def save_timing(key, startup, per_icon):
    """Remember how long this run took, blended with earlier runs on the same
    hardware (after a hardware change it starts afresh)."""
    settings = _settings()
    timing = settings.setdefault('icon_ai_timing', {})
    old = _saved_timing(key)
    if old:
        startup, per_icon = (old[0] + startup) / 2, (old[1] + per_icon) / 2
    timing[key] = [round(startup, 1), round(per_icon, 1), hardware_print()]
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=4)
    except OSError:
        pass


# --- Hardware profile + calibration log -------------------------------------
# Timing and memory depend on the whole machine, not just the GPU. These
# gather the details once per session (each probe is best-effort and never
# raises), so every finished run can be recorded with enough context to predict
# other hardware by comparison.
_CPU_INFO = {}
_RAM_MHZ = []
_GPU_DETAIL = {}
_HW_PROFILE = {}


def _cpu_info():
    """{'name', 'cores' (logical), 'mhz' (base clock)} — best-effort, cached."""
    if _CPU_INFO:
        return _CPU_INFO
    info = {'name': '', 'cores': os.cpu_count() or 0, 'mhz': 0}
    try:
        system = platform.system()
        if system == 'Windows':
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key:
                info['name'] = str(winreg.QueryValueEx(key, 'ProcessorNameString')[0]).strip()
                info['mhz'] = int(winreg.QueryValueEx(key, '~MHz')[0])
        elif system == 'Darwin':
            info['name'] = subprocess.run(['sysctl', '-n', 'machdep.cpu.brand_string'],
                                          capture_output=True, text=True, timeout=10).stdout.strip()
            hz = subprocess.run(['sysctl', '-n', 'hw.cpufrequency_max'],
                                capture_output=True, text=True, timeout=10).stdout.strip()
            if hz.isdigit():
                info['mhz'] = int(hz) // 1_000_000
        else:
            with open('/proc/cpuinfo', encoding='utf-8') as f:
                for line in f:
                    if line.startswith('model name') and not info['name']:
                        info['name'] = line.split(':', 1)[1].strip()
                    elif line.startswith('cpu MHz') and not info['mhz']:
                        info['mhz'] = int(float(line.split(':', 1)[1].strip()))
    except (OSError, ValueError, subprocess.SubprocessError, ImportError, IndexError):
        pass
    _CPU_INFO.update(info)
    return _CPU_INFO


def ram_speed_mhz():
    """Fastest installed memory module's speed in MHz (0 if unknown). RAM speed
    matters most in minimal-VRAM mode, where weights stream from RAM."""
    if _RAM_MHZ:
        return _RAM_MHZ[0]
    mhz = 0
    try:
        system = platform.system()
        if system == 'Windows':
            out = subprocess.run(
                ['powershell', '-NoProfile', '-Command',
                 '(Get-CimInstance Win32_PhysicalMemory | Measure-Object -Property Speed -Maximum).Maximum'],
                capture_output=True, text=True, timeout=12, **_SUBPROCESS_FLAGS).stdout.strip()
            if out.isdigit():
                mhz = int(out)
        elif system == 'Darwin':
            out = subprocess.run(['system_profiler', 'SPMemoryDataType'],
                                 capture_output=True, text=True, timeout=15).stdout
            found = re.search(r'Speed:\s*([0-9]+)\s*MHz', out)
            if found:
                mhz = int(found.group(1))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    _RAM_MHZ.append(mhz)
    return _RAM_MHZ[0]


def gpu_detail():
    """{'compute_cap', 'driver'} for an NVIDIA card ('' if unknown)."""
    if _GPU_DETAIL:
        return _GPU_DETAIL
    detail = {'compute_cap': '', 'driver': ''}
    try:
        smi = _nvidia_smi()
        if smi:
            lines = subprocess.run([smi, '--query-gpu=driver_version,compute_cap', '--format=csv,noheader'],
                                   capture_output=True, text=True, timeout=15,
                                   **_SUBPROCESS_FLAGS).stdout.strip().splitlines()
            if lines:
                parts = lines[0].split(',')
                detail['driver'] = parts[0].strip()
                if len(parts) > 1:
                    detail['compute_cap'] = parts[1].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    _GPU_DETAIL.update(detail)
    return _GPU_DETAIL


def hardware_profile():
    """Full machine context for a calibration record, gathered once per session.
    Every field is best-effort; unknown ones are blank/0/None and never block."""
    if _HW_PROFILE:
        return _HW_PROFILE
    cpu = _cpu_info()
    detail = gpu_detail()
    _HW_PROFILE.update({
        'gpu': gpu_name(), 'vram_gb': round(gpu_vram_gb(), 1), 'gpu_speed': gpu_speed(),
        'compute_cap': detail['compute_cap'], 'driver': detail['driver'],
        'cpu': cpu['name'], 'cpu_cores': cpu['cores'], 'cpu_mhz': cpu['mhz'],
        'ram_gb': round(total_ram_gb(), 1), 'ram_mhz': ram_speed_mhz(),
        'os': platform.system(), 'arch': platform.machine(), 'python': platform.python_version(),
    })
    return _HW_PROFILE


_CALIB_CACHE = {'mtime': None, 'rows': []}
_REF_CACHE = {'mtime': None, 'rows': []}
_LOCAL_KEY = []


def _local_cipher():
    """Fernet bound to THIS installed copy: a random key generated once and kept
    in the app-support folder next to the data. The local performance data is
    encrypted with it, so the stored file can't be read by eye, can't be read by
    another copy/machine that lacks this key, and is tamper-proof (Fernet HMAC).
    Separate from the shipped reference cipher (_ref_cipher)."""
    from cryptography.fernet import Fernet
    if not _LOCAL_KEY:
        key = None
        try:
            with open(_LOCAL_KEY_FILE, 'rb') as f:
                key = f.read().strip()
        except OSError:
            pass
        if not key:
            key = Fernet.generate_key()
            os.makedirs(os.path.dirname(_LOCAL_KEY_FILE), exist_ok=True)
            with open(_LOCAL_KEY_FILE, 'wb') as f:
                f.write(key)
        _LOCAL_KEY.append(key)
    return Fernet(_LOCAL_KEY[0])


def calibration_records():
    """Every recorded run, decrypted with this copy's key and parsed, cached
    until the file changes. Returns [] if the file is missing, the key is gone,
    or it fails its tamper check (it was written by another copy, or edited)."""
    try:
        mtime = os.path.getmtime(CALIBRATION_FILE)
    except OSError:
        return []
    if _CALIB_CACHE['mtime'] != mtime:
        rows = []
        try:
            import base64
            with open(CALIBRATION_FILE, 'rb') as f:
                token = base64.urlsafe_b64encode(f.read())   # back to the Fernet token form
            text = _local_cipher().decrypt(token).decode('utf-8')
            for line in text.splitlines():
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
            _CALIB_CACHE.update({'rows': rows, 'mtime': mtime})
        except Exception:
            # Missing key, corrupt, or tampered/another copy: ignore it.
            _CALIB_CACHE.update({'rows': [], 'mtime': mtime})
            return []
    return _CALIB_CACHE['rows']


def calibration_count():
    return len(calibration_records())


def _valid_calibration_record(row):
    """True if a record has the fields and plausible values worth sharing.
    A first guard against malformed or junk data before it is shared (a server
    that collects it must re-check, since a client can be modified)."""
    try:
        if not isinstance(row, dict):
            return False
        if not row.get('model') or not isinstance(row.get('hw'), dict):
            return False
        per = row.get('per_icon_s')
        if not isinstance(per, (int, float)) or not (0.1 <= per <= 3600):
            return False
        for field in ('peak_vram_gb', 'peak_ram_gb'):
            val = row.get(field)
            if val is not None and (not isinstance(val, (int, float)) or not (0 <= val <= 2048)):
                return False
        return True
    except (TypeError, ValueError):
        return False


def _hw_signature(hw):
    """A stable key for 'the same machine', from the hardware that affects
    timing and memory (ignores driver/app versions, which change often)."""
    return '|'.join(str(hw.get(k, '')) for k in
                    ('gpu', 'vram_gb', 'cpu', 'cpu_cores', 'cpu_mhz', 'ram_gb', 'ram_mhz', 'os', 'arch'))


def _group_key(hw, model, min_memory, poses, steps, cloud):
    """Identifies 'the same measurement': one machine, one model, one mode."""
    return (_hw_signature(hw or {}), model, bool(min_memory), bool(poses), steps, bool(cloud))


def _ref_cipher():
    """Fernet used for the shipped reference data. The key is derived from a
    secret assembled at runtime by XOR-ing two byte arrays, so there is no
    readable key string in the binary. Fernet is authenticated encryption
    (AES-CBC + HMAC): any edit to the file fails to decrypt, which is the tamper
    detection for this read-only data. This keeps the data from being read or
    quietly changed by eye; it is a strong deterrent, not proof against a
    determined reverse-engineer (the authoritative check on crowd data is
    server-side validation when it is uploaded)."""
    import base64
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    part_a = bytes([80, 35, 178, 252, 165, 62, 0, 209, 176, 126, 23, 77, 234, 214, 12, 129,
                    104, 199, 129, 240, 145, 48, 247, 47, 128, 163, 129, 190])
    part_b = bytes([6, 87, 211, 172, 192, 76, 102, 131, 213, 24, 58, 127, 218, 228, 58, 172,
                    33, 164, 238, 158, 212, 67, 131, 70, 237, 194, 245, 219])
    secret = bytes(x ^ y for x, y in zip(part_a, part_b))
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=b'vta-icon-perf-ref-v1', iterations=100_000)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(secret)))


def reference_records():
    """Aggregated performance data shipped INSIDE the release (read-only,
    encrypted + authenticated). Used for prediction alongside the user's own
    data, but never written to and never part of the user's shared file — so the
    shared file is the user's own measurements, not the release's. Returns [] if
    the app ships no reference data (first release / source build), if the crypto
    library is missing, or if the file FAILS ITS TAMPER CHECK (any edit makes
    decryption fail, and the tampered data is ignored rather than trusted)."""
    try:
        mtime = os.path.getmtime(REFERENCE_DATA_FILE)
    except OSError:
        return []
    if _REF_CACHE['mtime'] != mtime:
        rows = []
        try:
            import base64
            import gzip
            with open(REFERENCE_DATA_FILE, 'rb') as f:
                token = base64.urlsafe_b64encode(f.read())   # back to the Fernet token form
            payload = gzip.decompress(_ref_cipher().decrypt(token)).decode('utf-8')
            for line in payload.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if _valid_calibration_record(row):
                        row['source'] = 'reference'
                        rows.append(row)
                except ValueError:
                    pass
            _REF_CACHE.update({'mtime': mtime, 'rows': rows})
        except Exception:
            # Missing crypto lib, corrupt, or TAMPERED (Fernet InvalidToken):
            # ignore the reference data entirely rather than trust it.
            _REF_CACHE.update({'mtime': mtime, 'rows': []})
            return []
    return _REF_CACHE['rows']


def _prediction_records():
    """Everything used to predict: the user's own local data plus the shipped
    reference data. The local records win where both describe the same setup
    (the user's own machine is the most accurate source for itself)."""
    local = calibration_records()
    seen = {_group_key(r.get('hw'), r.get('model'), r.get('min_memory'), r.get('poses'),
                       r.get('steps'), r.get('cloud')) for r in local}
    extra = [r for r in reference_records()
             if _group_key(r.get('hw'), r.get('model'), r.get('min_memory'), r.get('poses'),
                           r.get('steps'), r.get('cloud')) not in seen]
    return local + extra


def export_calibration(dest):
    """Write the user's OWN performance data to share — validated, and ONLY the
    locally recorded records (never the reference data shipped in the release).
    The log is already de-duplicated as it is recorded. JSON Lines, one per
    line; internal bookkeeping fields (leading '_') are stripped."""
    rows = []
    for row in calibration_records():
        if row.get('source', 'local') != 'local' or not _valid_calibration_record(row):
            continue
        rows.append({k: v for k, v in row.items() if not k.startswith('_')})
    with open(dest, 'w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row) + '\n')
    return len(rows)


def record_calibration(key, job, startup, per_icon, icons, peak_vram=None, peak_ram=None, cloud=False):
    """Fold one finished run into the local performance data: the hardware, the
    settings used, and the measured start-up/per-icon time and peak VRAM/RAM.

    The log is kept DE-DUPLICATED as it is written — one record per
    (machine, model, mode, steps) — so it favours accuracy as more runs are
    folded in (a running mean of the times, worst-case peak memory, and a sample
    count) and never grows without bound. per_icon is also stored
    reference-GPU-equivalent (x this GPU's relative speed) so a measurement on
    one card predicts another.

    Network/download (Wi-Fi) speed is never recorded (connection-specific, not
    shareable). `startup` is None for runs that downloaded models (it would
    include download time). Best-effort; never raises into the caller."""
    try:
        hw = hardware_profile()
        speed = hw.get('gpu_speed') or 0
        model = 'google' if cloud else job.get('model')
        min_memory = bool(job.get('min_memory'))
        poses = bool(job.get('pose_variation'))
        steps = job.get('steps')
        gkey = _group_key(hw, model, min_memory, poses, steps, cloud)
        ref = round(per_icon * speed, 2) if speed else None
        ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

        rows = [dict(r) for r in calibration_records() if r.get('source', 'local') == 'local']
        merged = False
        for agg in rows:
            if _group_key(agg.get('hw'), agg.get('model'), agg.get('min_memory'),
                          agg.get('poses'), agg.get('steps'), agg.get('cloud')) != gkey:
                continue
            n = agg.get('samples', 1)
            agg['per_icon_s'] = round((agg.get('per_icon_s', per_icon) * n + per_icon) / (n + 1), 2)
            if ref is not None:
                rn = agg.get('_ref_n', 0)
                agg['per_icon_ref'] = round(((agg.get('per_icon_ref') or 0) * rn + ref) / (rn + 1), 2)
                agg['_ref_n'] = rn + 1
            if startup is not None:
                sn = agg.get('_startup_n', 0)
                agg['startup_s'] = round(((agg.get('startup_s') or 0) * sn + startup) / (sn + 1), 1)
                agg['_startup_n'] = sn + 1
            if peak_vram:
                agg['peak_vram_gb'] = max(agg.get('peak_vram_gb') or 0, peak_vram)
            if peak_ram:
                agg['peak_ram_gb'] = max(agg.get('peak_ram_gb') or 0, peak_ram)
            agg['samples'] = n + 1
            agg['last_ts'], agg['app'], agg['hw'] = ts, CURRENT_VERSION, hw
            merged = True
            break
        if not merged:
            rows.append({
                'source': 'local', 'key': key, 'cloud': bool(cloud), 'model': model,
                'min_memory': min_memory, 'poses': poses, 'steps': steps,
                'resolution': job.get('resolution'),
                'per_icon_s': round(per_icon, 2),
                'per_icon_ref': ref, '_ref_n': 1 if ref is not None else 0,
                'startup_s': round(startup, 1) if startup is not None else None,
                '_startup_n': 1 if startup is not None else 0,
                'peak_vram_gb': peak_vram, 'peak_ram_gb': peak_ram,
                'samples': 1, 'first_ts': ts, 'last_ts': ts, 'app': CURRENT_VERSION, 'hw': hw,
            })
        os.makedirs(os.path.dirname(CALIBRATION_FILE), exist_ok=True)
        import base64
        payload = '\n'.join(json.dumps(row) for row in rows).encode('utf-8')
        # Encrypted + bound to this copy; stored as raw bytes so it looks like an
        # ordinary binary data file, not an encrypted token.
        blob = base64.urlsafe_b64decode(_local_cipher().encrypt(payload))
        tmp = CALIBRATION_FILE + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(blob)
        os.replace(tmp, CALIBRATION_FILE)
        _CALIB_CACHE['mtime'] = None   # force a reload next read
    except (OSError, ValueError, TypeError):
        pass


def _hw_time_ratio(src_hw, minmem):
    """How much slower (>1) or faster (<1) this PC runs than the machine a
    measurement came from, from RAM and CPU speed — the part the GPU-model
    factor doesn't cover. (VRAM bandwidth is already folded into that GPU
    factor, which tracks real render throughput.) Unknown fields contribute
    nothing. RAM speed dominates in minimal-VRAM mode, where weights stream
    from system RAM; CPU speed has a small, steady effect. The weights are a
    starting point, tuned as real multi-hardware data is uploaded."""
    ratio = 1.0
    here = hardware_profile()
    src_ram, my_ram = src_hw.get('ram_mhz') or 0, here.get('ram_mhz') or 0
    if src_ram and my_ram:
        weight = 0.6 if minmem else 0.05
        ratio *= (1 - weight) + weight * (src_ram / my_ram)
    src_cpu, my_cpu = src_hw.get('cpu_mhz') or 0, here.get('cpu_mhz') or 0
    if src_cpu and my_cpu:
        weight = 0.15 if minmem else 0.1
        ratio *= (1 - weight) + weight * (src_cpu / my_cpu)
    return ratio


def _dataset_reference(base_key, minmem):
    """Mean (start-up, reference-GPU per-icon) measured for this exact setup on
    ANY hardware, from the calibration log — or None. This is what lets a run
    measured on another machine predict this one before it has run the setup
    itself. Each record's per-icon is reference-GPU-equivalent (GPU model
    removed) and is first adjusted for this PC's RAM/CPU speed vs the machine it
    was measured on (_hw_time_ratio), so load_timing only has to re-apply this
    PC's own GPU speed."""
    if ':' not in base_key:
        return None
    model = base_key.split(':', 1)[1].split('+')[0]
    poses = '+poses' in base_key
    per, start = [], []
    for row in _prediction_records():   # the user's own data plus the shipped reference data
        if row.get('cloud') or row.get('model') != model:
            continue
        if bool(row.get('poses')) != poses or bool(row.get('min_memory')) != minmem:
            continue
        ref = row.get('per_icon_ref')
        if ref:
            per.append(ref * _hw_time_ratio(row.get('hw') or {}, minmem))
            if row.get('startup_s'):
                start.append(row['startup_s'])
    if not per:
        return None
    return (sum(start) / len(start) if start else None, sum(per) / len(per))


def format_countdown(seconds):
    """'3 min 12 s left' style, for the live countdown."""
    seconds = max(0, int(round(seconds)))
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min left"
    if seconds >= 60:
        return f"{seconds // 60} min {seconds % 60:02d} s left"
    return f"{seconds} s left"


def download_text(label, done, total, speed):
    """'label: 1.2 GB of 2.5 GB, 18 MB/s, 1 min 10 s left' (speed from the
    bytes already received, so measuring it costs the download nothing)."""
    return download_progress_text(label, done, total, speed)


def total_ram_gb():
    """Installed system memory in GB (0 if unknown)."""
    try:
        system = platform.system()
        if system == 'Windows':
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                            ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                            ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                            ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                            ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
            status = MemoryStatus(); status.dwLength = ctypes.sizeof(MemoryStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return status.ullTotalPhys / 1024 ** 3
        if system == 'Darwin':
            out = subprocess.run(['sysctl', '-n', 'hw.memsize'], capture_output=True, text=True, timeout=10).stdout
            return int(out.strip()) / 1024 ** 3
        with open('/proc/meminfo', encoding='utf-8') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    return int(line.split()[1]) / 1024 ** 2
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        pass
    return 0.0


def free_disk_bytes(path=ICON_AI_DIR):
    """Free space on the drive the AI components go to."""
    probe = path
    while probe and not os.path.exists(probe):
        probe = os.path.dirname(probe)
    try:
        return shutil.disk_usage(probe or os.path.expanduser('~')).free
    except OSError:
        return 0


# Installed size vs download: wheels and archives unpack larger than they download.
UNPACK_FACTOR = {'install': 2.2, 'models': 1.05}
DISK_MARGIN = 1024 ** 3      # keep 1 GB free on top
MIN_RAM_GB = {'sd15': 8, 'sdxl': 16, 'flux2': 32, 'flux2_full': 32}


def check_requirements(kind, est, model='sd15', poses=False, min_mem=False):
    """[(what, needed, this PC, 'ok' | 'warn' | 'fail')] for installing the
    runtime (kind 'install') or downloading the models (kind 'models'). A
    'fail' blocks the download (not enough disk space, unsupported system);
    'warn' still allows it (it works, slower)."""
    rows = []
    if _python_triple() is None:
        rows.append(("Supported system", "Windows, macOS or Linux (64-bit)", f"{platform.system()} {platform.machine()}", 'fail'))
    else:
        rows.append(("Supported system", "Windows, macOS or Linux (64-bit)", f"{platform.system()} {platform.machine()}", 'ok'))
    size = (est or {}).get('total') or 0
    if size:
        need = size * UNPACK_FACTOR[kind] + DISK_MARGIN
        free = free_disk_bytes()
        rows.append(("Free disk space", format_size(need), format_size(free), 'ok' if free >= need else 'fail'))
    vram = gpu_vram_gb()
    apple = platform.system() == 'Darwin' and platform.machine().lower() in ('arm64', 'aarch64')
    # Minimal-VRAM mode lowers the GPU need (offloading) but raises the system-RAM need.
    if model in MODEL_MEMORY:
        want_vram, want_ram_model = memory_estimate(model, min_mem)
    else:
        want_vram = 7.5 if model == 'sdxl' else 4
        want_ram_model = None
    if apple:
        rows.append(("Graphics (GPU)", "NVIDIA or Apple Silicon", "Apple Silicon", 'ok'))
    elif vram:
        note = 'ok' if vram >= want_vram else 'warn'
        gpu_note = " (Minimal VRAM mode, offloaded to RAM)" if min_mem else (" (poses use a memory-saving mode)" if poses and model == 'sdxl' else '')
        rows.append(("GPU memory", f"{want_vram:g} GB recommended" + gpu_note, f"{vram:.0f} GB (NVIDIA)", note))
    else:
        rows.append(("Graphics (GPU)", "NVIDIA or Apple Silicon recommended", "none found: several minutes per icon", 'warn'))
    ram = total_ram_gb()
    if ram:
        # Minimal-VRAM mode holds the model in system RAM, so it needs more.
        want_ram = want_ram_model if want_ram_model is not None else MIN_RAM_GB.get(model, 8)
        rows.append(("System memory", f"{want_ram} GB recommended" + (" (Minimal VRAM mode)" if min_mem else ''),
                     f"{ram:.0f} GB", 'ok' if ram >= want_ram * 0.9 else 'warn'))
    return rows


def format_eta(seconds):
    if seconds is None:
        return "an unknown time"
    if seconds < 60:
        return "under a minute"
    if seconds < 3600:
        return f"about {round(seconds / 60)} min"
    return f"about {seconds / 3600:.1f} hours"


def measure_speed(url):
    """Bytes/second from a short ranged download from the same server. The
    first second (connection ramp-up) is ignored so the figure reflects the
    sustained speed a long download will get."""
    start, got, warm = time.monotonic(), 0, None
    with requests.get(url, headers={'Range': f'bytes=0-{SPEED_TEST_BYTES - 1}'}, stream=True, timeout=15) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_content(chunk_size=1 << 16):
            got += len(chunk)
            now = time.monotonic() - start
            if warm is None and now >= SPEED_WARMUP:
                warm = (now, got)
            if got >= SPEED_TEST_BYTES or now > SPEED_TEST_SECONDS:
                break
    elapsed = time.monotonic() - start
    if warm and elapsed > warm[0]:
        return (got - warm[1]) / (elapsed - warm[0])
    return got / elapsed if got and elapsed > 0 else None


def _version_key(name):
    match = re.search(r'torch-([0-9.]+)', name)
    return tuple(int(x) for x in match.group(1).strip('.').split('.')) if match else ()


def _torch_wheel_url():
    """URL of the PyTorch wheel pip will download for this machine, or None."""
    torch_cmd = next((c for c in _pip_install_commands() if any(a.startswith('torch') for a in c)), None)
    if not torch_cmd:
        return None
    spec = next(a for a in torch_cmd if a.startswith('torch'))
    system, machine = platform.system(), platform.machine().lower()
    if '--index-url' in torch_cmd:
        index = torch_cmd[torch_cmd.index('--index-url') + 1]
        page = requests.get(f"{index}/torch/", timeout=15).text
        plat = 'win_amd64' if system == 'Windows' else ('x86_64' if machine in ('x86_64', 'amd64') else 'aarch64')
        names = [h for h in re.findall(r'href="([^"]+?\.whl)', page) if 'cp312-cp312' in h and plat in h
                 and (system != 'Linux' or 'linux' in h)]
        if not names:
            return None
        best = max(names, key=_version_key)
        return requests.compat.urljoin(f"{index}/torch/", best.split('#')[0])
    version = spec.split('==')[1] if '==' in spec else None
    info = requests.get(f"https://pypi.org/pypi/torch/{version + '/' if version else ''}json", timeout=15).json()
    arch = 'arm64' if machine in ('arm64', 'aarch64') else 'x86_64'
    for f in info.get('urls', []):
        if 'cp312' in f['filename'] and 'macosx' in f['filename'] and arch in f['filename']:
            return f['url']
    return None


DISK_TEST_BYTES = 128 * 1024 * 1024
DISK_SPEED_MAX_AGE = 7 * 24 * 3600   # re-measure the disk weekly


def disk_speed():
    """Bytes/second the AI folder's drive writes (a one-off ~128 MB test,
    flushed to disk, then deleted; remembered for a week). Reads are taken as
    at least as fast: a fresh read-back would only measure the OS cache."""
    saved = _settings().get('icon_ai_disk_speed')
    if saved and time.time() - saved[1] < DISK_SPEED_MAX_AGE:
        return float(saved[0])
    os.makedirs(ICON_AI_DIR, exist_ok=True)
    path = os.path.join(ICON_AI_DIR, '.disk_speed_test')
    block = os.urandom(4 * 1024 * 1024)
    try:
        start = time.monotonic()
        with open(path, 'wb') as f:
            for _ in range(DISK_TEST_BYTES // len(block)):
                f.write(block)
            f.flush()
            os.fsync(f.fileno())
        speed = DISK_TEST_BYTES / max(0.01, time.monotonic() - start)
    except OSError:
        return 0.0
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    settings = _settings()
    settings['icon_ai_disk_speed'] = [speed, time.time()]
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=4)
    except OSError:
        pass
    return speed


def _timed(total, net, disk, unpack=1.0):
    """Download time (bounded by the slower of the server-measured connection
    and the disk, which saves it as it arrives) plus unpacking time."""
    if not net:
        return None, None
    download = max(total / net, total / disk if disk else 0)
    install = total * unpack / disk if disk and unpack > 1.0 else 0.0
    return download, install


def estimate_install():
    """{'total', 'speed', 'eta', 'parts', 'download_eta', 'install_eta', 'disk'} for
    installing (or updating) the runtime. The speed is measured from the
    PyTorch download server itself, so any per-download cap it has counts."""
    python_url = PYTHON_URL.format(tag=PYTHON_BUILD_TAG, ver=PYTHON_VERSION, triple=_python_triple())
    if runtime_needs_update():
        parts, speed_url = [("New libraries", UPDATE_ESTIMATE)], python_url
    else:
        url = _torch_wheel_url()
        size = int(requests.head(url, allow_redirects=True, timeout=15).headers.get('Content-Length', 0)) if url else 0
        lib_label = "AI libraries (reusing this app's Python)" if _use_app_python() else "Python and libraries"
        parts = [("PyTorch", size or 2 * 1024 ** 3), (lib_label, LIBRARIES_ESTIMATE)]
        speed_url = url or python_url
    total = sum(size for _n, size in parts)
    speed = measure_speed(speed_url)
    disk = disk_speed()
    # Wheels unpack to about 2.2x their size, written and indexed by pip.
    download, install = _timed(total, speed, disk, unpack=UNPACK_FACTOR['install'])
    return {'total': total, 'speed': speed, 'disk': disk, 'parts': parts, 'download_eta': download,
            'install_eta': install, 'eta': (download + install) if download is not None else None,
            'disk_use': total * UNPACK_FACTOR['install']}


def _worker_module():
    """icon_ai_worker loaded from its file (it imports nothing heavy at module level).
    Builds import it by name from the app folder, through the normal import
    system (the name is computed so Nuitka doesn't compile the worker in)."""
    if '__compiled__' in globals():
        import importlib
        return importlib.import_module(os.path.splitext(os.path.basename(_worker_script()))[0])
    import importlib.util
    spec = importlib.util.spec_from_file_location('icon_ai_worker_info', _worker_script())
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runtime_uses_half_precision():
    try:
        with open(RUNTIME_MARKER, 'r', encoding='utf-8') as f:
            return 'download.pytorch.org/whl/cu' in json.dumps(json.load(f).get('packages'))
    except (OSError, ValueError):
        return False


def _model_cached(repo, path):
    snapshots = os.path.join(_models_dir(), 'hub', 'models--' + repo.replace('/', '--'), 'snapshots')
    return os.path.isdir(snapshots) and any(os.path.isfile(os.path.join(snapshots, s, path)) for s in os.listdir(snapshots))


def _flux2_present(variant='flux2'):
    """True when the FLUX.2 variant's key files are cached. Filesystem check (the
    app process has no huggingface_hub; the models live in the HF cache layout)."""
    worker = _worker_module()
    return all(_model_cached(r, p) for r, p in worker.flux2_key_files(variant))


def _flux2_missing_size(variant='flux2'):
    """Bytes still to download for a FLUX.2 variant, or 0 when it's already
    cached. Uses the HF web API (requests), like the other estimates — no
    huggingface_hub in the app process."""
    if _flux2_present(variant):
        return 0
    import fnmatch
    worker = _worker_module()
    token = (_settings().get('hf_token') or '').strip()
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    total = 0
    for repo, patterns in worker.flux2_files(variant):
        try:
            info = requests.get(f"https://huggingface.co/api/models/{repo}", headers=headers, timeout=20)
            info.raise_for_status()
            files = [s['rfilename'] for s in info.json().get('siblings', [])
                     if any(fnmatch.fnmatch(s['rfilename'], pat) for pat in patterns)]
            resp = requests.post(f"https://huggingface.co/api/models/{repo}/paths-info/main",
                                 headers=headers, data={'paths': files}, timeout=30)
            resp.raise_for_status()
            for item in resp.json():
                total += (item.get('lfs') or {}).get('size') or item.get('size') or 0
        except Exception:
            pass
    return total


def estimate_flux2(variant='flux2'):
    """Download size and time for a FLUX.2 variant (plus bitsandbytes if the
    4-bit build needs it), or None when nothing is needed."""
    size = _flux2_missing_size(variant)
    need_bnb = (variant == 'flux2') and not _package_installed('bitsandbytes')
    if not size and not need_bnb:
        return None
    size += (300 * 1024 ** 2 if need_bnb else 0)   # bitsandbytes wheel, rough
    speed = measure_speed('https://huggingface.co/black-forest-labs/FLUX.2-dev/resolve/main/model_index.json')
    disk = disk_speed()
    download, _install = _timed(size, speed, disk)
    load = size / disk if disk else None
    return {'total': size, 'speed': speed, 'disk': disk, 'parts': [("FLUX.2 model", size)],
            'download_eta': download, 'load_eta': load,
            'eta': (download + (load or 0)) if download is not None else None,
            'disk_use': size * UNPACK_FACTOR['models']}


def estimate_models(segmenter=True, poses=False, model='sd15', skip_base=False):
    """Missing model download size and time, or None when everything is cached.
    With skip_base, the base checkpoint is excluded (a custom model supplies it)."""
    if model in FLUX2_KEYS:
        return estimate_flux2(model)
    files = _worker_module().model_files(_runtime_uses_half_precision(), segmenter, poses, model, skip_base=skip_base)
    missing = [(r, p) for r, p in files if not _model_cached(r, p)]
    if not missing:
        return None
    sizes = {}
    for repo in {r for r, _p in missing}:
        resp = requests.post(f"https://huggingface.co/api/models/{repo}/paths-info/main",
                             data={'paths': [p for r, p in missing if r == repo]}, timeout=20)
        resp.raise_for_status()
        for item in resp.json():
            sizes[(repo, item['path'])] = (item.get('lfs') or {}).get('size') or item.get('size') or 0
    total = sum(sizes.values())
    biggest = max(sizes, key=sizes.get)
    # Measured from the model server (Hugging Face) itself: its per-download cap counts.
    speed = measure_speed(f"https://huggingface.co/{biggest[0]}/resolve/main/{biggest[1]}")
    disk = disk_speed()
    download, _install = _timed(total, speed, disk)
    # The first generation then loads every model from disk once.
    load = total / disk if disk else None
    return {'total': total, 'speed': speed, 'disk': disk, 'parts': [("AI models", total)], 'download_eta': download,
            'load_eta': load, 'eta': (download + (load or 0)) if download is not None else None,
            'disk_use': total * UNPACK_FACTOR['models']}


def remove_runtime():
    shutil.rmtree(ICON_AI_DIR, ignore_errors=True)


def app_downloaded_ai_bytes():
    """Total size of AI data the app downloaded itself (the private Python
    runtime, the app's own model cache and the fetched example sets). Excludes
    anything the user provided — a custom model, a custom/shared model cache,
    curated extra artwork — and the small calibration/hardware data."""
    total = 0
    targets = [RUNTIME_DIR, EXAMPLES_DIR, RUNTIME_MARKER]
    if not using_shared_models():
        targets.append(MODELS_DIR)      # the app's own cache, not a custom path
    for path in targets:
        if os.path.isfile(path):
            try:
                total += os.path.getsize(path)
            except OSError:
                pass
        elif os.path.isdir(path):
            for root, _dirs, files in os.walk(path):
                for name in files:
                    try:
                        total += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        pass
    return total


def remove_app_downloaded_ai_data():
    """Delete only the AI data the app downloaded (runtime, the app's own model
    cache, fetched examples). Deliberately KEEPS user-provided data: a custom
    base model, a custom/shared model cache (lives outside icon_ai), curated
    extra artwork, and the calibration/hardware data + per-install key — so
    switching AI->standard frees space without discarding the user's own work.
    Returns the list of paths removed."""
    removed = []
    targets = [RUNTIME_DIR, EXAMPLES_DIR, RUNTIME_MARKER]
    if not using_shared_models():
        targets.append(MODELS_DIR)
    for path in targets:
        try:
            if os.path.isfile(path):
                os.remove(path); removed.append(path)
            elif os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True); removed.append(path)
        except OSError:
            pass
    return removed


# --- storage per part, and removing one model setup -----------------------------------
# (model key, with poses, label). A model's files are what it needs beyond the
# shared helper models; a pose add-on is what poses need beyond its model.
MODEL_SETUPS = [
    ('sdxl', False, "High quality model (SDXL)"),
    ('sd15', False, "Standard model (SD 1.5)"),
    ('sdxl', True, "AI poses add-on for High quality"),
    ('sd15', True, "AI poses add-on for Standard"),
]


def _setup_files(model, poses):
    worker = _worker_module()
    half = _runtime_uses_half_precision()
    helpers = set(worker._helper_model_files(True))
    base = set(worker.model_files(half, True, False, model)) - helpers
    return set(worker.model_files(half, True, True, model)) - helpers - base if poses else base


def _snapshot_paths(repo, path):
    snapshots = os.path.join(_models_dir(), 'hub', 'models--' + repo.replace('/', '--'), 'snapshots')
    if not os.path.isdir(snapshots):
        return []
    return [os.path.join(snapshots, rev, path) for rev in os.listdir(snapshots)
            if os.path.isfile(os.path.join(snapshots, rev, path))]


def _files_size(files):
    total = 0
    for repo, path in files:
        for f in _snapshot_paths(repo, path):
            try:
                total += os.path.getsize(os.path.realpath(f))
            except OSError:
                pass
    return total


def _folder_size(folder):
    """Bytes used by a folder's files. Uses the sizes the directory listing
    already carries (no per-file lookup on Windows): ~10x faster than
    os.walk + getsize on the ~36,000 files of an AI install, same total."""
    total, folders = 0, [folder]
    while folders:
        try:
            with os.scandir(folders.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            folders.append(entry.path)
                        else:
                            total += entry.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def storage_report():
    """Disk use of the AI parts: {'total', 'runtime', 'helpers', 'other',
    'setups': [(model, poses, label, bytes, installed)], 'shared', 'models_dir'}.
    Files shared by both models (the style adapter's image encoder) count once,
    under the first. When a shared model cache is used, the models live outside
    the app folder: they're measured but not counted as removable (they may
    belong to another app), and 'shared' is True."""
    shared = _using_shared_cache()
    report = {'total': _folder_size(ICON_AI_DIR) if os.path.isdir(ICON_AI_DIR) else 0,
              'runtime': _folder_size(RUNTIME_DIR) if os.path.isdir(RUNTIME_DIR) else 0,
              'shared': shared, 'models_dir': _models_dir()}
    try:
        worker = _worker_module()
        report['helpers'] = _files_size(set(worker._helper_model_files(True)))
        counted, setups = set(), []
        for model, poses, label in MODEL_SETUPS:
            files = _setup_files(model, poses)
            installed = all(_model_cached(r, p) for r, p in files)
            setups.append((model, poses, label, _files_size(files - counted), installed))
            counted |= files
        report['setups'] = setups
    except Exception:
        report['helpers'], report['setups'] = 0, []
    # FLUX.2 tiers, listed so the user can see their size and download/remove them.
    flux2 = []
    try:
        for variant in ('flux2', 'flux2_full'):
            folder = _flux2_repo_folder(variant)
            size = _folder_size(folder) if folder and os.path.isdir(folder) else 0
            flux2.append((variant, _flux2_label(variant), size, _flux2_present(variant)))
    except Exception:
        flux2 = []
    report['flux2'] = flux2
    flux2_total = sum(x[2] for x in flux2)
    if shared:
        # Models aren't inside ICON_AI_DIR; count what we measured in the cache.
        report['other'] = 0
        report['total'] = report['runtime'] + report['helpers'] + sum(x[3] for x in report['setups']) + flux2_total
    else:
        report['other'] = max(0, report['total'] - report['runtime'] - report['helpers']
                              - sum(x[3] for x in report['setups']) - flux2_total)
    return report


def _flux2_repo_folder(variant):
    """The HF cache folder for a FLUX.2 variant's repo, or None."""
    worker = _worker_module()
    repo = worker.FLUX2_REPO if worker._flux2_full(variant) else worker.FLUX2_BNB_REPO
    return os.path.join(_models_dir(), 'hub', 'models--' + repo.replace('/', '--'))


def _flux2_label(variant):
    try:
        return _worker_module().BACKENDS[variant]['label']
    except Exception:
        return variant


def remove_flux2(variant):
    """Delete a downloaded FLUX.2 tier. Refuses when a shared cache is in use."""
    if _using_shared_cache():
        return
    folder = _flux2_repo_folder(variant)
    if folder and os.path.isdir(folder):
        shutil.rmtree(folder, ignore_errors=True)


def remove_model_setup(model, poses):
    """Delete one model setup's files, keeping everything another installed
    setup or the shared helpers still need. Removing a model also removes its
    pose add-on (useless without it). Refuses when a shared cache is in use, so
    another app's models are never deleted."""
    if _using_shared_cache():
        return
    worker = _worker_module()
    remove = _setup_files(model, poses) | (set() if poses else _setup_files(model, True))
    keep = set(worker._helper_model_files(True))
    for other_model, other_poses, _label in MODEL_SETUPS:
        if other_model == model and (other_poses == poses or not poses):
            continue
        files = _setup_files(other_model, other_poses)
        if all(_model_cached(r, p) for r, p in files):
            keep |= files
    repos = set()
    for repo, path in remove - keep:
        for f in _snapshot_paths(repo, path):
            target = os.path.realpath(f)
            for doomed in {f, target}:
                try:
                    os.remove(doomed)
                except OSError:
                    pass
        repos.add(repo)
    # A model folder with nothing left in it goes entirely.
    for repo in repos:
        folder = os.path.join(_models_dir(), 'hub', 'models--' + repo.replace('/', '--'))
        snapshots = os.path.join(folder, 'snapshots')
        left = any(files for _root, _dirs, files in os.walk(snapshots)) if os.path.isdir(snapshots) else False
        if not left:
            shutil.rmtree(folder, ignore_errors=True)


def runtime_disk_usage():
    return _folder_size(ICON_AI_DIR)


# =============================================================================
# == Worker threads
# =============================================================================
class _SubprocessThread(QThread):
    """Base thread that runs subprocesses and streams their output."""
    log = pyqtSignal(str)
    finished_ok = pyqtSignal(bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc = None
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def _run_process(self, cmd, on_line=None):
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env=_child_env(), text=True, encoding='utf-8', errors='replace', bufsize=1,
            **_SUBPROCESS_FLAGS)
        for raw in self._proc.stdout:
            line = raw.rstrip()
            if on_line and on_line(line):
                continue
            if line:
                self.log.emit(line)
        return self._proc.wait()


class RuntimeInstallThread(_SubprocessThread):
    progress = pyqtSignal(int, str)  # percent (-1 = busy), status text

    def run(self):
        self._fresh = False
        try:
            self._install()
        except Exception as e:
            if self._cancelled:
                # Drop a half-installed fresh runtime (an update keeps the old
                # one); downloaded models are always kept.
                if self._fresh:
                    shutil.rmtree(RUNTIME_DIR, ignore_errors=True)
                self.finished_ok.emit(False, "Installation cancelled.")
            else:
                self.finished_ok.emit(False, f"{type(e).__name__}: {e}")

    def _install(self):
        triple = _python_triple()
        if not triple:
            raise RuntimeError(f"Unsupported platform: {platform.system()} {platform.machine()}")
        try:
            os.remove(RUNTIME_MARKER)
        except OSError:
            pass
        os.makedirs(ICON_AI_DIR, exist_ok=True)
        # Updating an older runtime keeps Python and its packages; pip below
        # then only fetches what is new.
        self._fresh = not os.path.isfile(_runtime_python())
        if self._fresh:
            self._install_python(triple)

        python = _runtime_python()
        for pkgs in _pip_install_commands():
            if self._cancelled:
                raise RuntimeError("cancelled")
            name = pkgs[0].split('=')[0].split('<')[0]
            self.progress.emit(-1, f"Installing {name}{' and dependencies' if len(pkgs) > 1 else ''}...")
            label = f"Installing {name}{' and dependencies' if len(pkgs) > 1 else ''}"
            code = self._run_process([python, '-I', '-m', 'pip', 'install', '--no-warn-script-location',
                                      '--progress-bar', 'raw', *pkgs], self._pip_progress(label))
            if code != 0:
                raise RuntimeError(f"pip install failed (exit code {code}). See the log for details.")

        with open(RUNTIME_MARKER, 'w', encoding='utf-8') as f:
            json.dump({'version': RUNTIME_VERSION, 'python': PYTHON_VERSION,
                       'packages': _pip_install_commands()}, f, indent=2)
        self.finished_ok.emit(True, "AI components installed.")

    def _pip_progress(self, label):
        """Turns pip's 'Progress X of Y' lines into progress with time left."""
        state = {'total': None, 'start': 0.0}

        def on_line(line):
            match = re.match(r'Progress (\d+) of (\d+)', line)
            if not match:
                return False
            done, total = int(match.group(1)), int(match.group(2))
            if total != state['total']:
                state['total'], state['start'] = total, time.monotonic()
            elapsed = time.monotonic() - state['start']
            speed = done / elapsed if done and elapsed > 1 else None
            self.progress.emit(int(done * 100 / max(1, total)), download_text(label, done, total, speed))
            return True
        return on_line

    def _install_python(self, triple):
        shutil.rmtree(RUNTIME_DIR, ignore_errors=True)
        url = PYTHON_URL.format(tag=PYTHON_BUILD_TAG, ver=PYTHON_VERSION, triple=triple)
        self.log.emit(f"Downloading {url}")
        fd, archive = tempfile.mkstemp(prefix='vta_python_', suffix='.tar.gz')
        os.close(fd)
        try:
            with requests.get(url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get('Content-Length') or 0)
                done = 0
                start = time.monotonic()
                with open(archive, 'wb') as out:
                    for chunk in resp.iter_content(chunk_size=1 << 16):
                        if self._cancelled:
                            raise RuntimeError("cancelled")
                        out.write(chunk)
                        done += len(chunk)
                        if total:
                            elapsed = time.monotonic() - start
                            speed = done / elapsed if elapsed > 1 else None
                            self.progress.emit(int(done * 100 / total), download_text("Downloading Python runtime", done, total, speed))
            self.progress.emit(-1, "Extracting Python runtime...")
            # The archive's top-level folder is "python/".
            with tarfile.open(archive, 'r:gz') as tar:
                tar.extractall(ICON_AI_DIR, filter='data')
            try:
                freed = _strip_runtime()
                if freed:
                    self.log.emit(f"Trimmed {format_size(freed)} of unused files from the Python runtime.")
            except Exception as e:
                self.log.emit(f"(runtime trim skipped: {e})")
        finally:
            try:
                os.remove(archive)
            except OSError:
                pass


class IconGenerationThread(_SubprocessThread):
    event = pyqtSignal(dict)

    def __init__(self, job, parent=None):
        super().__init__(parent)
        self.job = job

    def run(self):
        fd, job_path = tempfile.mkstemp(prefix='vta_icon_job_', suffix='.json')
        error = None
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self.job, f)

            def on_line(line):
                nonlocal error
                if not line.startswith(EVENT_PREFIX):
                    return False
                try:
                    data = json.loads(line[len(EVENT_PREFIX):])
                except ValueError:
                    return False
                if data.get('event') == 'error':
                    error = data.get('message')
                self.event.emit(data)
                return True

            # -I: ignore user site-packages / PYTHON* vars; -u: unbuffered so
            # progress lines arrive immediately.
            code = self._run_process([_runtime_python(), '-I', '-u', '-X', 'utf8', _worker_script(), job_path], on_line)
            if self._cancelled:
                self.finished_ok.emit(False, "Generation cancelled.")
            elif code != 0:
                self.finished_ok.emit(False, error or f"Generator exited with code {code}. See the log for details.")
            else:
                self.finished_ok.emit(True, "Done.")
        except Exception as e:
            self.finished_ok.emit(False, f"{type(e).__name__}: {e}")
        finally:
            try:
                os.remove(job_path)
            except OSError:
                pass


# =============================================================================
# == Google Gemini (cloud, the user's own API key)
# =============================================================================
# The light path: like Pilot Framework's social stories, Gemini gets the
# template symbol, the theme's wallpapers and character art as references and
# paints each icon itself. No local runtime, models or checks are needed:
# Gemini copies characters from reference images faithfully on its own.
GENERATOR_CHOICES = [
    ("This PC (local AI)", 'local'),
    ("Google Gemini (your API key)", 'google'),
]
GEMINI_API = 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
# (model id, label, USD per 1K image from ai.google.dev/gemini-api/docs/pricing)
GEMINI_MODELS = [
    # The id Pilot Framework's social stories uses (proven reliable there).
    ('gemini-3.1-flash-image-preview', "Gemini 3.1 Flash Image (best)", 0.067),
    ('gemini-3.1-flash-lite-image', "Gemini 3.1 Flash Lite Image (cheapest)", 0.0336),
    ('gemini-3-pro-image', "Gemini 3 Pro Image (slowest, most detailed)", 0.134),
]
DEFAULT_GEMINI_MODEL = GEMINI_MODELS[0][0]
# Tried in order when the chosen model isn't available to the key (404).
GEMINI_FALLBACKS = ['gemini-3.1-flash-image-preview', 'gemini-3.1-flash-image', 'gemini-3.1-flash-lite-image']
GEMINI_RETRIES = 3          # for busy (429/503) answers
GEMINI_REFERENCE_SIDE = 1024  # wallpapers are sent at most this size (smaller upload, same detail for the model)
GEMINI_MAX_WALLPAPERS = 4
GEMINI_MAX_EXTRAS = 3
GEMINI_SYSTEM = (
    "You design custom app icons for PlayStation Vita themes. Every icon is a square, full-bleed piece of "
    "artwork in the theme's own art style and colours, built around the app's symbol so the app stays "
    "instantly recognisable. Reference images of the theme are provided. When a character appears, "
    "DO NOT reinterpret them: copy them exactly from the references, same face, eyes, hair, outfit and "
    "colours, and never draw a person who is not in the references. Characters are never cut off at the "
    "left, right or top edge. Only draw text that the request asks for, spelled exactly, in a clean font."
)


def _settings():
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def google_api_key():
    return (_settings().get('google_api_key') or os.environ.get('GEMINI_API_KEY') or '').strip()


def google_model():
    return _settings().get('google_image_model') or DEFAULT_GEMINI_MODEL


def gemini_price(model):
    return next((price for key, _l, price in GEMINI_MODELS if model.startswith(key.removesuffix('-preview'))),
                GEMINI_MODELS[0][2])


def _image_part(path_or_image, max_side=GEMINI_REFERENCE_SIDE):
    """An inline image part (PNG keeps transparency for character renders, JPEG keeps wallpapers small)."""
    from PIL import Image
    import base64
    import io
    img = path_or_image if isinstance(path_or_image, Image.Image) else Image.open(path_or_image)
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    if img.mode in ('RGBA', 'LA', 'P'):
        img.convert('RGBA').save(buf, 'PNG')
        mime = 'image/png'
    else:
        img.convert('RGB').save(buf, 'JPEG', quality=90)
        mime = 'image/jpeg'
    return {'inline_data': {'mime_type': mime, 'data': base64.b64encode(buf.getvalue()).decode('ascii')}}


class GeminiError(Exception):
    pass


def gemini_generate(key, model, parts, seed, cancelled=lambda: False, log=print):
    """One image from Gemini, as PIL. Retries busy answers and falls back to
    other image models the key can use."""
    from PIL import Image
    import base64
    import io
    body = {
        'system_instruction': {'parts': [{'text': GEMINI_SYSTEM}]},
        'contents': [{'role': 'user', 'parts': parts}],
        'generationConfig': {'responseModalities': ['TEXT', 'IMAGE'], 'seed': seed % 2_147_483_647,
                             'imageConfig': {'aspectRatio': '1:1'}},
    }
    models = [model] + [m for m in GEMINI_FALLBACKS if m != model]
    for m in models:
        for attempt in range(GEMINI_RETRIES + 1):
            if cancelled():
                raise GeminiError("cancelled")
            resp = requests.post(GEMINI_API.format(model=m), json=body, timeout=180,
                                 headers={'x-goog-api-key': key, 'Content-Type': 'application/json'})
            if resp.status_code == 404:
                log(f"Model {m} isn't available to this key; trying the next one.")
                break
            if resp.status_code in (429, 500, 503) and attempt < GEMINI_RETRIES:
                wait = int(resp.headers.get('Retry-After') or 0) or 10 * (attempt + 1)
                log(f"Gemini is busy ({resp.status_code}); retrying in {wait}s.")
                for _ in range(wait):
                    if cancelled():
                        raise GeminiError("cancelled")
                    time.sleep(1)
                continue
            if resp.status_code != 200:
                try:
                    message = resp.json().get('error', {}).get('message', resp.text[:300])
                except ValueError:
                    message = resp.text[:300]
                if resp.status_code in (400, 401, 403) and 'key' in message.lower():
                    raise GeminiError(f"Google rejected the API key: {message}")
                if resp.status_code == 429:
                    raise GeminiError(f"Gemini quota reached ({message}). Image models need billing enabled on the key.")
                raise GeminiError(f"Gemini error {resp.status_code}: {message}")
            data = resp.json()
            candidate = (data.get('candidates') or [{}])[0]
            for part in (candidate.get('content') or {}).get('parts') or []:
                inline = part.get('inline_data') or part.get('inlineData')
                if inline and inline.get('data'):
                    return Image.open(io.BytesIO(base64.b64decode(inline['data']))).convert('RGB'), m
            reason = candidate.get('finishReason') or (data.get('promptFeedback') or {}).get('blockReason')
            raise GeminiError(f"Gemini returned no image ({reason or 'no reason given'}).")
    raise GeminiError("None of the Gemini image models are available to this API key.")


GEMINI_TEXT_MODELS = ['gemini-flash-latest', 'gemini-2.5-flash']  # for the theme analysis (cheap text + vision)
GEMINI_EXAMPLES_PER_ICON = 4  # each from a different community set: ideas, not one design to copy
THEME_ANALYSIS_PROMPT = (
    "These images are the wallpapers (and any extra character artwork) of a PlayStation Vita theme. "
    "Each image is labelled. Analyse them so an image model can make matching app icons. Return JSON with:\n"
    "- \"art_style\": the rendering style in a few words (e.g. anime cel shading, painterly, 3D render, photo).\n"
    "- \"setting\": the world, places and recurring motifs, in one or two sentences.\n"
    "- \"palette\": the main colours, as words.\n"
    "- \"characters\": every distinct character, each as {\"name\": their name if you recognise the series, "
    "otherwise a short label like \"girl with long pink hair\", \"appearance\": exact face, eyes, hair colour/style, "
    "outfit with colours, accessories and distinctive details, \"best_image\": the label of the image that shows "
    "them most completely}.\n"
    "Be precise and factual. Only list characters that are really in the images."
)


def _wallpaper_label(path, index):
    """What a theme image is, from its Vita asset name."""
    name = os.path.splitext(os.path.basename(path))[0].lower()
    if name == 'lockpaper':
        return "Lock screen wallpaper"
    match = re.fullmatch(r'br(\d+)', name)
    if match:
        return f"Home page {match.group(1)} wallpaper"
    return f"Theme wallpaper {index + 1}"


def _example_icons(icon_name, job, rng, count=GEMINI_EXAMPLES_PER_ICON):
    """The same app's icon from example sets (built-in and community), as design ideas."""
    sets = _worker_module().example_sets(job.get('examples_dirs'))
    found = [os.path.join(d, icon_name) for _n, d in sets if os.path.isfile(os.path.join(d, icon_name))]
    return rng.sample(found, min(count, len(found)))


def _example_title(icon_path):
    """The community theme an example icon comes from (its set's source.json)."""
    try:
        with open(os.path.join(os.path.dirname(icon_path), 'source.json'), encoding='utf-8') as f:
            return json.load(f).get('title') or 'another theme'
    except (OSError, ValueError):
        return 'another theme'


def gemini_describe_theme(key, labelled_images, cancelled=lambda: False, log=print):
    """The theme's art style, setting, palette and characters, from Gemini
    looking at the labelled images (like Pilot Framework's reference image
    descriptions). Returns a dict, or {} if the analysis isn't available."""
    parts = []
    for label, path in labelled_images:
        parts += [{'text': f"[{label}]"}, _image_part(path)]
    parts.append({'text': THEME_ANALYSIS_PROMPT})
    body = {'contents': [{'role': 'user', 'parts': parts}],
            'generationConfig': {'responseMimeType': 'application/json'}}
    for model in GEMINI_TEXT_MODELS:
        if cancelled():
            return {}
        try:
            resp = requests.post(GEMINI_API.format(model=model), json=body, timeout=120,
                                 headers={'x-goog-api-key': key, 'Content-Type': 'application/json'})
            if resp.status_code != 200:
                log(f"Theme analysis with {model} failed ({resp.status_code}).")
                continue
            text = ''.join(p.get('text', '') for p in
                           ((resp.json().get('candidates') or [{}])[0].get('content') or {}).get('parts') or [])
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except (requests.RequestException, ValueError) as e:
            log(f"Theme analysis with {model} failed: {e}")
    return {}


def gemini_icon_prompt(icon_name, info, job, analysis, character):
    """The request for one icon, from the generator's own rules (what each
    symbol means, which icons feature a character, which carry text) and the
    theme analysis (style, world, the character to feature)."""
    subject = info.ICON_SUBJECTS.get(icon_name, 'app')
    text = info.TEXT_REGIONS.get(icon_name)
    mode = job.get('theme_color_mode', 'auto')
    colours = '' if mode == 'never' else ', '.join('#' + c[-6:] for c in job.get('theme_colors') or [] if len(c) >= 6)
    lines = [
        f"Create the PS Vita '{subject}' app icon for the theme \"{job.get('title') or 'custom theme'}\".",
        f"[The app's system symbol] shows the symbol: keep it clearly recognisable as the {subject} symbol. "
        "You may redesign its shape and give it detail, colour and depth, but it must still read as that symbol.",
        "Use the art style, colours, lighting and world of the theme's wallpapers. "
        "Don't copy a wallpaper; make icon artwork that belongs to the theme.",
    ]
    if analysis.get('art_style'):
        lines.append(f"Theme art style: {analysis['art_style']}. Setting and motifs: {analysis.get('setting', '')} "
                     f"Palette: {analysis.get('palette', '')}.")
    if colours:
        lines.append(f"Use the theme's interface colours {colours} as the icon's main colours." if mode == 'always' else
                     f"The theme's interface colours are {colours}; use them where they suit the art.")
    if character:
        where = f" (see [{character['best_image']}])" if character.get('best_image') else ''
        lines.append(f"Feature this character, copied exactly from the reference images{where}: "
                     f"{character.get('name', 'the character')}: {character.get('appearance', '')}. "
                     "Place them next to or interacting with the symbol; the symbol must stay clearly visible.")
    else:
        lines.append("No characters or people in this icon: build it from the theme's scenery, shapes and motifs.")
    lines.append(f'Show the text "{text[2]}" clearly as part of the symbol, exactly as written.' if text
                 else "No text, letters or logos anywhere.")
    lines.append("Square, full-bleed art filling the whole image: no border, no rounded corners, no frame around the icon.")
    return '\n'.join(lines)


class GeminiIconThread(QThread):
    """Generates the icons with Gemini, one request per icon, emitting the same
    events as the local worker so the dialog treats both alike. Like Pilot
    Framework's social stories, every reference image is labelled and the
    theme is analysed first, so nothing has to be described by the user."""
    event = pyqtSignal(dict)
    log = pyqtSignal(str)
    finished_ok = pyqtSignal(bool, str)

    def __init__(self, job, key, model, parent=None):
        super().__init__(parent)
        self.job, self.key, self.model = job, key, model
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        from PIL import Image
        try:
            info = _worker_module()
            job = self.job
            icons = job['icons']
            rng = random.Random(job.get('seed', 0))
            labelled = [(_wallpaper_label(p, i), p) for i, p in enumerate(job['wallpapers'][:GEMINI_MAX_WALLPAPERS])]
            extras = [p for p in job.get('extra_images') or [] if os.path.isfile(p)][:GEMINI_MAX_EXTRAS]
            labelled += [(f"Extra character artwork {i + 1}", p) for i, p in enumerate(extras)]
            os.makedirs(job['output_dir'], exist_ok=True)

            self.event.emit({'event': 'status', 'message': "Analysing the theme's wallpapers and characters..."})
            analysis = gemini_describe_theme(self.key, labelled, lambda: self._cancelled, self.log.emit)
            characters = [c for c in analysis.get('characters') or [] if isinstance(c, dict)]
            if analysis:
                self.log.emit(f"Theme: {analysis.get('art_style', '?')}; characters: "
                              + (', '.join(c.get('name', '?') for c in characters) or 'none'))
            # Character icons take turns through the characters (seeded order).
            rng.shuffle(characters)
            wants = [n for n in icons if job.get('layout_style') != 'emblem' and n in info.CHARACTER_ICONS]
            cast = {name: characters[i % len(characters)] for i, name in enumerate(wants)} if characters else {}

            refs = []
            for label, path in labelled:
                refs += [{'text': f"[{label}]"}, _image_part(path)]
            made, used = [], self.model
            for index, name in enumerate(icons):
                if self._cancelled:
                    self.finished_ok.emit(False, "Generation cancelled.")
                    return
                self.event.emit({'event': 'progress', 'name': name, 'index': index, 'total': len(icons)})
                template = os.path.join(job['templates_dir'], name)
                if not os.path.isfile(template):
                    self.event.emit({'event': 'skipped', 'name': name, 'index': index, 'total': len(icons),
                                     'reason': 'template not found'})
                    continue
                # Without an analysis, character icons still ask for "a character from the references".
                character = cast.get(name) or ({'name': 'one character from the reference images'}
                                                if not characters and name in wants else None)
                parts = [{'text': gemini_icon_prompt(name, info, job, analysis, character)},
                         {'text': "[The app's system symbol]"}, _image_part(template, 512)] + refs
                examples = _example_icons(name, job, rng)
                subject = info.ICON_SUBJECTS.get(name, 'same')
                for n, example in enumerate(examples, 1):
                    parts += [{'text': f"[Example icon {n} of {len(examples)}: the {subject} icon from the community "
                                       f"theme \"{_example_title(example)}\"] For ideas on how to design the symbol only: "
                                       "don't copy its characters, colours or style."},
                              _image_part(example, 256)]
                if made:
                    # The first and the latest icon keep the set consistent.
                    parts.append({'text': "[Icons already made for this set] Match their style and finish:"})
                    parts += [_image_part(p, 256) for p in dict.fromkeys([made[0], made[-1]])]
                parts.append({'text': "Now create the icon."})
                try:
                    img, used = gemini_generate(self.key, used, parts, job.get('seed', 0) + index * 7919,
                                                lambda: self._cancelled, self.log.emit)
                except GeminiError as e:
                    if self._cancelled:
                        self.finished_ok.emit(False, "Generation cancelled.")
                        return
                    if 'API key' in str(e) or 'quota' in str(e) or 'available' in str(e):
                        raise
                    self.event.emit({'event': 'skipped', 'name': name, 'index': index, 'total': len(icons), 'reason': str(e)})
                    continue
                side = min(img.size)
                img = img.crop(((img.width - side) // 2, (img.height - side) // 2,
                                (img.width + side) // 2, (img.height + side) // 2))
                path = os.path.join(job['output_dir'], name)
                img.resize((128, 128), Image.Resampling.LANCZOS).save(path)
                made.append(path)
                self.event.emit({'event': 'icon', 'name': name, 'index': index, 'total': len(icons), 'path': path})
            self.log.emit(f"Generated {len(made)} icon(s) with {used}.")
            self.finished_ok.emit(bool(made), "Done." if made else "No icons were generated. See the log for details.")
        except Exception as e:
            self.finished_ok.emit(False, f"{type(e).__name__}: {e}")


class EstimateThread(QThread):
    """Runs a slow function (estimate_*, disk measuring) off the UI thread."""
    result = pyqtSignal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.result.emit(self.fn())
        except Exception as e:
            self.result.emit({'error': f"{type(e).__name__}: {e}"})


class ExampleDownloadThread(QThread):
    """Downloads community themes and keeps only their custom icon sets as
    icon design and layout examples (~70 KB each). Release builds only (uses
    crypted). Runs quietly in the background when the generator opens."""
    progress = pyqtSignal(int, str)
    finished_ok = pyqtSignal(bool, str)

    def __init__(self, wanted, parent=None):
        super().__init__(parent)
        self.wanted = wanted
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            self.finished_ok.emit(True, self._download())
        except Exception as e:
            self.finished_ok.emit(False, f"{type(e).__name__}: {e}")

    def _download(self):
        import io
        import zipfile
        import xml.etree.ElementTree as ET
        from PIL import Image
        self.progress.emit(0, "Getting the community theme list...")
        # The most downloaded themes (two pages of the online list).
        themes = list(theme_pages(sort='downloads', max_pages=2))
        # Sets already shipped with the app or downloaded before are skipped.
        have = {name for name, _d in _worker_module().example_sets([_examples_dir(), EXAMPLES_DIR])}
        # Popular themes first, shuffled within that pool for variety.
        candidates = sorted(themes, key=lambda t: int(t.get('downloads') or 0), reverse=True)[:80]
        random.shuffle(candidates)
        templates = _templates_dir()
        added = 0
        for theme in candidates:
            if self._cancelled or added >= self.wanted:
                break
            theme_id = str(theme.get('id', ''))
            if not theme_id or theme_id in have or not theme.get('zip'):
                continue
            self.progress.emit(int(added * 100 / self.wanted), f"Checking \"{theme.get('title', '?')}\"...")
            try:
                resp = requests.get(secure_url(theme['zip']), timeout=60)
                resp.raise_for_status()
                archive = zipfile.ZipFile(io.BytesIO(resp.content))
            except (requests.RequestException, zipfile.BadZipFile):
                continue
            names = {os.path.basename(n).lower(): n for n in archive.namelist() if not n.endswith('/')}
            if 'theme.xml' not in names:
                continue
            try:
                root = ET.fromstring(archive.read(names['theme.xml']))
            except ET.ParseError:
                continue
            icons = {}
            for tag, icon_name in THEME_XML_ICON_TAGS.items():
                el = root.find(f'./HomeProperty/{tag}/m_iconFilePath')
                member = names.get(os.path.basename(el.text or '').lower()) if el is not None and el.text else None
                if member:
                    try:
                        icons[icon_name] = Image.open(io.BytesIO(archive.read(member))).convert('RGB').resize((128, 128), Image.Resampling.LANCZOS)
                    except Exception:
                        pass
            # Only real custom sets: most icons present and not the stock symbols.
            custom = [n for n, img in icons.items() if not _looks_like_template(img, os.path.join(templates, n))]
            if len(custom) < 12:
                continue
            out_dir = os.path.join(EXAMPLES_DIR, theme_id)
            try:
                os.makedirs(out_dir, exist_ok=True)
                for icon_name in custom:
                    small = icons[icon_name].quantize(colors=32, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
                    # Some sources carry an RGB transparency key that a palette PNG can't store.
                    small.info.pop('transparency', None)
                    small.save(os.path.join(out_dir, icon_name), optimize=True)
                with open(os.path.join(out_dir, 'source.json'), 'w', encoding='utf-8') as f:
                    json.dump({'title': theme.get('title'), 'author': theme.get('author')}, f)
            except Exception:
                # One bad theme must not stop the rest; drop its partial folder.
                shutil.rmtree(out_dir, ignore_errors=True)
                continue
            added += 1
        return f"Added {added} community example set(s)." if added else "No new community icon sets found."


# --- Art from community themes of the same series (theme API, automatic) ---
# When icons are generated, community themes whose title or tags match this
# theme's name are downloaded in the background (release builds) and only
# their wallpapers are taken, into a temporary folder that is deleted after
# the generation: nothing is stored. The worker judges every picture and
# only uses what clearly belongs to this theme (see icon_ai_worker).
COMMUNITY_ART_THEMES = 3       # matching community themes used
COMMUNITY_ART_PER_THEME = 6    # wallpapers taken from each
_TITLE_FILLER = {'theme', 'themes', 'ps', 'vita', 'psvita', 'download', 'themedownload', 'dynamic', 'custom',
                 'the', 'a', 'an', 'of', 'and', 'by', 'for', 'hd', 'edition', 'version', 'new', 'my', 'fixed', 'test'}


def _title_words(text):
    words = re.findall(r"[a-z0-9]+", (text or '').lower())
    return {w for w in words if w not in _TITLE_FILLER and len(w) > 1 and not w.isdigit()
            and not re.fullmatch(r'v\d+|theme\d*', w)}


def _title_numbers(text):
    """Numbers in a name ("5" in "Persona 5"): they rank the closest matches
    first, they don't decide what matches."""
    return set(re.findall(r"(?<![a-z0-9])\d+(?![a-z0-9])", (text or '').lower()))


def matching_community_themes(title, themes, limit=COMMUNITY_ART_THEMES):
    """Community themes about the same thing as this theme, best match first.
    Longer names must share at least two words (so "Corpse Party Blood Drive"
    doesn't pull in any theme with "party"). A one-word name is often a
    common word ("Snow"), so the community theme's title must be that same
    name, not merely contain it. The closest come first: the same number as
    this theme ("Persona 5" before "Persona 3"), then the most shared words,
    then the most downloaded."""
    words = _title_words(title)
    if not words:
        return []
    numbers = _title_numbers(title)
    scored = []
    for theme in themes:
        if len(words) == 1:
            shared = words if _title_words(theme.get('title')) == words else set()
        else:
            shared = words & (_title_words(theme.get('title')) | _title_words(theme.get('tags')))
            if len(shared) < 2:
                shared = set()
        if shared and theme.get('zip') and theme.get('id') is not None:
            same_number = len(numbers & (_title_numbers(theme.get('title')) | _title_numbers(theme.get('tags'))))
            scored.append((same_number, len(shared), int(theme.get('downloads') or 0), theme))
    scored.sort(key=lambda item: item[:3], reverse=True)
    return [item[3] for item in scored[:limit]]


def community_theme_art(title, dest, cancelled=lambda: False, log=print):
    """Wallpapers of community themes matching this theme's name, saved into
    `dest` (a temporary folder the caller deletes after the generation).
    Release builds only; [] when nothing matches."""
    import io
    import zipfile
    from PIL import Image
    if not ONLINE_FEATURES_AVAILABLE or not _title_words(title):
        return []
    # Search the online list for each word of the name (the most downloaded
    # matches first); matching_community_themes then applies the strict rules.
    words = _title_words(title)
    field = 'title' if len(words) == 1 else 'title_tags'
    themes, seen = [], set()
    try:
        for word in sorted(words):
            for theme in theme_pages(word, field, 'downloads', max_pages=2):
                if theme.get('id') not in seen:
                    seen.add(theme.get('id'))
                    themes.append(theme)
    except Exception as e:
        log(f"Community theme list unavailable: {e}")
        return []
    paths = []
    for theme in matching_community_themes(title, themes):
        if cancelled():
            break
        folder = os.path.join(dest, str(theme['id']))
        if not os.path.isdir(folder):
            try:
                resp = requests.get(secure_url(theme['zip']), timeout=60)
                resp.raise_for_status()
                archive = zipfile.ZipFile(io.BytesIO(resp.content))
                names = [n for n in archive.namelist()
                         if re.fullmatch(r'(lockpaper|br\d+)\.(png|jpe?g)', os.path.basename(n).lower())]
                os.makedirs(folder, exist_ok=True)
                for n in sorted(names)[:COMMUNITY_ART_PER_THEME]:
                    img = Image.open(io.BytesIO(archive.read(n))).convert('RGB')
                    img.thumbnail((1024, 1024))
                    img.save(os.path.join(folder, os.path.splitext(os.path.basename(n))[0] + '.jpg'), quality=90)
                with open(os.path.join(folder, 'source.json'), 'w', encoding='utf-8') as f:
                    json.dump({'title': theme.get('title'), 'author': theme.get('author')}, f)
            except Exception as e:
                shutil.rmtree(folder, ignore_errors=True)
                log(f"Couldn't use community theme \"{theme.get('title')}\": {e}")
                continue
        found = sorted(os.path.join(folder, n) for n in os.listdir(folder) if n.endswith('.jpg'))
        log(f"Using art from the community theme \"{theme.get('title')}\" ({len(found)} pictures).")
        paths += found
    return paths


class CommunityArtThread(QThread):
    """Fetches the matching community themes' art off the UI thread."""
    done = pyqtSignal(list)
    log = pyqtSignal(str)

    def __init__(self, title, dest, parent=None):
        super().__init__(parent)
        self.title, self.dest = title, dest

    def run(self):
        try:
            self.done.emit(community_theme_art(self.title, self.dest, log=self.log.emit))
        except Exception as e:
            self.log.emit(f"Community theme art skipped: {e}")
            self.done.emit([])


def _looks_like_template(img, template_path):
    """True if an icon is (nearly) the stock template symbol."""
    import numpy as np
    from PIL import Image
    if not os.path.isfile(template_path):
        return False
    a = np.asarray(img.convert('L'), dtype=np.float32)
    b = np.asarray(Image.open(template_path).convert('L').resize((128, 128)), dtype=np.float32)
    return float(np.abs(a - b).mean()) < 12


_BACKGROUND = []  # background downloads that may outlive the dialog that started them


def example_set_count():
    """Icon example sets available: built into the app plus downloaded, each once."""
    return len(_worker_module().example_sets([_examples_dir(), EXAMPLES_DIR]))


# =============================================================================
# == Dialog
# =============================================================================
# =============================================================================
# == Web image search for extra character artwork
# =============================================================================
# Safebooru: a public, safe-for-work anime/cartoon art archive with a free
# JSON API (no account). Its tags suit cut-outs: "solo" + "simple_background"
# finds clean single-character renders.
SEARCH_API = 'https://safebooru.org/index.php'
SUGGEST_API = 'https://safebooru.org/autocomplete.php'
SEARCH_PAGE_SIZE = 40
SEARCH_HEADERS = {'User-Agent': 'VitaThemeArchitect icon generator'}
EXTRA_ART_DIR = os.path.join(ICON_AI_DIR, 'extra_artwork')


def search_images(terms, page=0, solo=True, plain_background=True):
    """Image search results: [{'id', 'thumb', 'image', 'tags', 'width', 'height'}]."""
    tags = ' '.join(t.strip().replace(' ', '_') for t in terms.split(',') if t.strip())
    if solo:
        tags += ' solo'
    if plain_background:
        tags += ' simple_background'
    resp = requests.get(SEARCH_API, params={'page': 'dapi', 's': 'post', 'q': 'index', 'json': 1,
                                            'limit': SEARCH_PAGE_SIZE, 'pid': page, 'tags': tags},
                        headers=SEARCH_HEADERS, timeout=20)
    resp.raise_for_status()
    posts = resp.json() if resp.text.strip() else []
    return [{'id': p['id'], 'thumb': p['preview_url'], 'image': p.get('sample_url') or p['file_url'],
             'tags': p.get('tags', ''), 'width': p.get('width'), 'height': p.get('height')}
            for p in posts if p.get('rating') in ('general', 'safe')]


WEB_SEARCH_URL = 'https://www.bing.com/images/async'
WEB_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                             'Chrome/130.0 Safari/537.36', 'Accept-Language': 'en-US,en;q=0.9'}
# (label, search function key)
IMAGE_SOURCES = [("Anime art (Safebooru)", 'booru'), ("Web (Bing images)", 'web')]


def search_web_images(terms, page=0, solo=True, plain_background=True):
    """General web image search (Bing's image results page, SafeSearch strict;
    no API key). Unofficial: if Bing changes the page, the search reports an
    error and the Safebooru source still works."""
    import hashlib
    import html as htmllib
    query = terms + (' character' if solo else '') + (' transparent png render' if plain_background else '')
    resp = requests.get(WEB_SEARCH_URL, params={'q': query, 'first': page * SEARCH_PAGE_SIZE, 'count': SEARCH_PAGE_SIZE,
                                                'adlt': 'strict', 'mmasync': 1}, headers=WEB_HEADERS, timeout=20)
    resp.raise_for_status()
    found = []
    for m in re.finditer(r'\bm="([^"]+)"', resp.text):
        try:
            d = json.loads(htmllib.unescape(m.group(1)))
        except ValueError:
            continue
        if d.get('murl') and d.get('turl'):
            found.append({'id': hashlib.sha1(d['murl'].encode()).hexdigest()[:12], 'thumb': d['turl'],
                          'image': d['murl'], 'tags': d.get('t', '')})
    return found


def suggest_tags(text):
    """Tag suggestions (with result counts) for what the user is typing."""
    resp = requests.get(SUGGEST_API, params={'q': text.strip().replace(' ', '_')}, headers=SEARCH_HEADERS, timeout=10)
    resp.raise_for_status()
    return [item['value'] for item in resp.json()][:15]


class _Worker(QThread):
    """Runs one network function off the UI thread."""
    done = pyqtSignal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn())
        except Exception as e:
            self.done.emit({'error': f"{type(e).__name__}: {e}"})


class ThumbnailLoader(QThread):
    """Loads result thumbnails one by one."""
    loaded = pyqtSignal(int, bytes)

    def __init__(self, urls, parent=None):
        super().__init__(parent)
        self.urls = urls
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        for i, url in enumerate(self.urls):
            if self._cancelled:
                return
            try:
                resp = requests.get(url, headers=SEARCH_HEADERS, timeout=15)
                if resp.ok:
                    self.loaded.emit(i, resp.content)
            except requests.RequestException:
                pass


class ImageSearchDialog(QDialog):
    """Search the web for pictures of the theme's characters, tick the ones to
    keep, search again with other terms; the ticked images are downloaded and
    returned in `chosen` (local file paths)."""

    def __init__(self, parent, initial_terms=''):
        super().__init__(parent)
        self.setWindowTitle("Find Character Artwork")
        self.setMinimumSize(760, 600)
        self.results, self.chosen, self.page = [], [], 0
        self._threads = []
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Search for the theme's characters (e.g. a character or series name). "
                                "Tick the pictures to use, or search again with different words."))
        row = QHBoxLayout()
        self.query = QLineEdit(initial_terms); self.query.setPlaceholderText("e.g. lucy (elfen lied), or elfen lied")
        self.query.returnPressed.connect(self.new_search)
        self._suggestions = QStringListModel(self)
        completer = QCompleter(self._suggestions, self); completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.query.setCompleter(completer)
        self._suggest_timer = QTimer(self); self._suggest_timer.setSingleShot(True); self._suggest_timer.setInterval(400)
        self._suggest_timer.timeout.connect(self.fetch_suggestions)
        self.query.textEdited.connect(lambda _t: self._suggest_timer.start())
        row.addWidget(self.query, 1)
        btn = QPushButton("Search"); btn.clicked.connect(self.new_search); row.addWidget(btn)
        self.query.setToolTip("Search terms: a character's name, the series, or both.\n"
                              "Anime art suggests matching tags as you type.")
        btn.setToolTip("Search with these terms (replaces the current results).")
        layout.addLayout(row)
        opts = QHBoxLayout()
        self.source = QComboBox()
        for label, key in IMAGE_SOURCES:
            self.source.addItem(label, key)
        self.source.setToolTip("Anime art: precise, clean results for anime/cartoon characters.\n"
                               "Web: broader (official art, game renders, any style) but noisier - tick only the right ones.")
        opts.addWidget(self.source)
        self.solo = QCheckBox("Single character"); self.solo.setChecked(True)
        self.plain = QCheckBox("Plain background (best for cut-outs)"); self.plain.setChecked(True)
        self.solo.setToolTip("Only pictures showing one character: they cut out cleanly and match a single person.")
        self.plain.setToolTip("Prefer pictures on a plain or transparent background, so the character cuts out cleanly.")
        opts.addWidget(self.solo); opts.addWidget(self.plain); opts.addStretch()
        layout.addLayout(opts)
        self.grid = QListWidget(); self.grid.setViewMode(QListWidget.ViewMode.IconMode); self.grid.setIconSize(QSize(140, 140))
        self.grid.setToolTip("Tick the pictures to keep. Search again with other terms to find more.")
        self.grid.setResizeMode(QListWidget.ResizeMode.Adjust); self.grid.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.grid.setGridSize(QSize(160, 175)); self.grid.setMovement(QListWidget.Movement.Static)
        self.grid.itemClicked.connect(lambda item: item.setCheckState(
            Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked else Qt.CheckState.Checked))
        layout.addWidget(self.grid, 1)
        self.status = QLabel(); layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.btn_more = QPushButton("More Results"); self.btn_more.clicked.connect(self.more); self.btn_more.setEnabled(False)
        self.btn_more.setToolTip("Load the next page of results for the same search.")
        buttons.addWidget(self.btn_more); buttons.addStretch()
        self.source.currentIndexChanged.connect(lambda _i: self.new_search() if self.query.text().strip() else None)
        self.btn_add = QPushButton("Add Ticked Images"); self.btn_add.setProperty("role", "success"); self.btn_add.clicked.connect(self.add_ticked)
        self.btn_add.setToolTip("Download the ticked pictures and add them to this generation's extra artwork.")
        buttons.addWidget(self.btn_add)
        close = QPushButton("Cancel"); close.clicked.connect(self.reject); buttons.addWidget(close)
        layout.addLayout(buttons)
        layout.addWidget(QLabel("<small>Images come from safebooru.org (fan and official art). Use them for your own theme "
                                "and respect the artists.</small>"))
        if initial_terms:
            QTimer.singleShot(0, self.new_search)

    def showEvent(self, event):
        super().showEvent(event)
        from widgets import center_on_parent
        center_on_parent(self)

    def _run(self, fn, then):
        worker = _Worker(fn, self)
        worker.done.connect(then)
        self._threads.append(worker)
        worker.start()

    def fetch_suggestions(self):
        text = self.query.text().split(',')[-1].strip()
        if len(text) >= 2:
            self._run(lambda: suggest_tags(text), lambda tags: self._suggestions.setStringList(tags) if isinstance(tags, list) else None)

    def new_search(self):
        self.page = 0
        self.results = []
        self.grid.clear()
        self.search()

    def more(self):
        self.page += 1
        self.search()

    def search(self):
        terms = self.query.text().strip()
        if not terms:
            return
        self.status.setText("Searching...")
        solo, plain, page = self.solo.isChecked(), self.plain.isChecked(), self.page
        search = search_web_images if self.source.currentData() == 'web' else search_images
        self._run(lambda: search(terms, page, solo, plain), self.show_results)

    def show_results(self, found):
        if isinstance(found, dict):
            self.status.setText(f"Search failed: {found['error']}")
            return
        start = len(self.results)
        self.results += found
        for r in found:
            item = QListWidgetItem(f"#{r['id']}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setToolTip(r['tags'][:300])
            self.grid.addItem(item)
        self.btn_more.setEnabled(len(found) == SEARCH_PAGE_SIZE)
        self.status.setText(f"{len(self.results)} pictures. Click to tick the ones to use." if self.results
                            else "No pictures found. Try different words, or untick the filters.")
        loader = ThumbnailLoader([r['thumb'] for r in found], self)
        loader.loaded.connect(lambda i, data, base=start: self._set_thumb(base + i, data))
        self._threads.append(loader)
        loader.start()

    def _set_thumb(self, row, data):
        pix = QPixmap()
        if pix.loadFromData(data) and row < self.grid.count():
            self.grid.item(row).setIcon(QIcon(pix))

    def add_ticked(self):
        ticked = [self.results[i] for i in range(self.grid.count()) if self.grid.item(i).checkState() == Qt.CheckState.Checked]
        if not ticked:
            QMessageBox.information(self, "Nothing Ticked", "Tick the pictures you want to use first.")
            return
        self.status.setText(f"Downloading {len(ticked)} picture(s)...")
        self.btn_add.setEnabled(False)

        def download():
            os.makedirs(EXTRA_ART_DIR, exist_ok=True)
            paths = []
            import io
            from PIL import Image
            skipped = 0
            for r in ticked:
                path = os.path.join(EXTRA_ART_DIR, f"{r['id']}.png")
                if not os.path.isfile(path):
                    try:
                        resp = requests.get(r['image'], headers=WEB_HEADERS, timeout=60)
                        resp.raise_for_status()
                        # Re-save through Pillow: only real images are kept (transparency included).
                        Image.open(io.BytesIO(resp.content)).save(path, 'PNG')
                    except Exception:
                        skipped += 1
                        continue
                paths.append(path)
            if not paths:
                raise RuntimeError("none of the ticked pictures could be downloaded")
            return paths

        def finished(paths):
            if isinstance(paths, dict):
                self.status.setText(f"Download failed: {paths['error']}")
                self.btn_add.setEnabled(True)
                return
            self.chosen = paths
            self.accept()
        self._run(download, finished)

    def done(self, result):
        for t in self._threads:
            if isinstance(t, ThumbnailLoader):
                t.cancel()
            t.wait()
        super().done(result)


class RequirementsDialog(QDialog):
    """Before any download: what it needs, what this PC has, and the choice to
    start or not. Not enough disk space (or an unsupported system) refuses the
    download; a weak GPU or little memory only warns (it works, slower)."""

    MARKS = {'ok': ('\u2714', '#2e9e44'), 'warn': ('\u26a0', '#d08a00'), 'fail': ('\u2716', '#d23c3c')}

    def __init__(self, parent, title, intro, est, rows):
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        heading = QLabel(f"<b>{intro}</b>"); heading.setWordWrap(True); layout.addWidget(heading)
        layout.addWidget(QLabel(IconGeneratorDialog._estimate_text(est)))
        table = QGridLayout()
        for col, head in enumerate(("", "Requirement", "Needed", "This PC")):
            table.addWidget(QLabel(f"<b>{head}</b>"), 0, col)
        for row, (what, needed, have, state) in enumerate(rows, 1):
            mark, colour = self.MARKS[state]
            table.addWidget(QLabel(f"<span style='color:{colour}; font-size:15px'>{mark}</span>"), row, 0)
            table.addWidget(QLabel(what), row, 1)
            table.addWidget(QLabel(needed), row, 2)
            table.addWidget(QLabel(have), row, 3)
        layout.addLayout(table)
        blocked = any(state == 'fail' for *_x, state in rows)
        warned = any(state == 'warn' for *_x, state in rows)
        note = QLabel("<b>This PC doesn't meet the requirements above, so nothing will be downloaded.</b>" if blocked else
                      "It will work, but more slowly than on a recommended PC." if warned else
                      "This PC meets all the requirements.")
        note.setWordWrap(True); layout.addWidget(note)
        layout.addWidget(QLabel(f"Saved in: {ICON_AI_DIR}"))
        buttons = QHBoxLayout(); buttons.addStretch()
        start = QPushButton("Start Download"); start.setProperty("role", "success"); start.setEnabled(not blocked)
        start.clicked.connect(self.accept); buttons.addWidget(start)
        refuse = QPushButton("Don't Download"); refuse.clicked.connect(self.reject); buttons.addWidget(refuse)
        layout.addLayout(buttons)

    def showEvent(self, event):
        super().showEvent(event)
        from widgets import center_on_parent
        center_on_parent(self)


class IconGeneratorDialog(QDialog):
    """Generates the 17 system icons from the Template icons, styled by the theme's wallpapers.

    After exec() returns Accepted, `generated` maps icon asset names to PNG paths.
    """

    def __init__(self, parent, theme_title, wallpapers, theme_colors=None):
        super().__init__(parent)
        self.setWindowTitle("Generate Theme Icons (AI)")
        self.setMinimumSize(720, 640)
        self.theme_title = (theme_title or '').strip()
        self.has_title = bool(self.theme_title) and self.theme_title != DEFAULT_THEME_TITLE
        self.wallpapers = wallpapers
        self.theme_colors = list(theme_colors or [])  # 'AARRGGBB' from the creator's Colors tab
        self.generated = {}
        self.output_dir = None
        self.thread = None
        self.preview_labels = {}

        layout = QVBoxLayout(self)
        self.status_label = QLabel(); self.status_label.setWordWrap(True); layout.addWidget(self.status_label)

        # Install / remove row
        self.btn_install = QPushButton("Install AI Components..."); self.btn_install.clicked.connect(self.install_runtime)
        self.btn_remove = QPushButton("Remove AI Components"); self.btn_remove.clicked.connect(self.remove_runtime)
        self.btn_install.setToolTip("Download the private AI runtime used to make icons on this PC (shows the size and time first).\n"
                                    "Optional: nothing is downloaded until you click this.")
        self.btn_remove.setToolTip("Delete the AI runtime and all downloaded AI models to free the disk space.\n"
                                   "You can install them again at any time.")
        # (These two sit in the bottom button row: a row of their own at the top looked out of place.)

        # Options
        options = QFormLayout()
        # Where the icons are made: this PC (private runtime, all quality
        # checks) or Google Gemini with the user's own API key (no downloads).
        self.generator_combo = QComboBox()
        for label, key in GENERATOR_CHOICES:
            self.generator_combo.addItem(label, key)
        saved = _settings().get('icon_ai_generator')
        if saved not in ('local', 'google') or (saved == 'google' and not google_api_key()):
            saved = 'google' if google_api_key() and not is_runtime_installed() else 'local'
        self.generator_combo.setCurrentIndex(self.generator_combo.findData(saved))
        self.update_generator_options()
        self.generator_combo.setToolTip("This PC: free and unlimited, needs a one-time download and works best with a GPU.\n"
                                        "Google Gemini: nothing to download and fast on any PC, but each icon is billed "
                                        "to your Google API key (set it in Settings > AI Icons).")
        self.generator_combo.currentIndexChanged.connect(self.on_generator_changed)
        options.addRow("Generate with:", self.generator_combo)
        def add_slider(label, tooltip, low, high, value):
            slider = QSlider(Qt.Orientation.Horizontal); slider.setRange(low, high); slider.setValue(value); slider.setToolTip(tooltip)
            options.addRow(label, slider); return slider
        self.creativity_slider = add_slider("Creativity:", "How freely the AI redesigns each icon. The symbol's outline is always followed.", 50, 100, 75)
        self.style_slider = add_slider("Theme style influence:", "How strongly the wallpapers' style and mood guide the icons (their content is not copied).", 0, 100, 55)
        self.consistency_slider = add_slider("Colour consistency:", "How closely every icon is matched to the theme's colour palette.", 0, 100, 70)
        self.clarity_slider = add_slider("Symbol clarity:", "How strongly the symbol is reinforced for readability (higher = closer to the template shape).", 0, 100, 50)
        self.color_mode_combo = QComboBox()
        for label, key in THEME_COLOR_CHOICES:
            self.color_mode_combo.addItem(label, key)
        self.color_mode_combo.setToolTip("The colours set in the theme's Colors tab (menus, bars, notifications).\n"
                                         "Auto uses them when the wallpapers share them (e.g. a red theme with red menus) "
                                         "and skips them when they don't (e.g. an orange bar on blue artwork).")
        if not self.theme_colors:
            self.color_mode_combo.setEnabled(False)
            self.color_mode_combo.setToolTip("This theme has no colours set in its Colors tab yet.")
        options.addRow("Theme colours:", self.color_mode_combo)
        self.layout_combo = QComboBox()
        for label, key in LAYOUT_STYLE_CHOICES:
            self.layout_combo.addItem(label, key)
        self.layout_combo.setToolTip("Characters are cut out of the theme's own wallpapers, so they look exactly right.")
        options.addRow("Layout style:", self.layout_combo)
        # Image model: SDXL is much better but needs ~8 GB VRAM and ~8 GB more download.
        self.model_combo = QComboBox()
        vram = gpu_vram_gb() if _runtime_uses_half_precision() else 0.0
        min_mem = bool(_settings().get('ai_min_memory'))
        for label, key, _need in MODEL_CHOICES:
            self.model_combo.addItem(label, key)
            item = self.model_combo.model().item(self.model_combo.count() - 1)
            if item is not None:
                item.setToolTip(f"Expected memory: {memory_note(key, min_mem)}.")
        # FLUX.2 tiers are always listed (never auto-suggested), but greyed out and
        # unselectable on a GPU that can't meet their VRAM requirement. 'Minimal VRAM
        # mode' lowers that requirement (group offloading), so the labels and which
        # tiers are selectable change with it.
        for lbl, key, need in FLUX2_CHOICES:
            req = FLUX2_MIN_MEM_VRAM.get(key, need) if min_mem else need
            self.model_combo.addItem(lbl + ("  — minimal VRAM mode" if min_mem else ""), key)
            item = self.model_combo.model().item(self.model_combo.count() - 1)
            if item is not None:
                tip = f"Expected memory: {memory_note(key, min_mem)}."
                if vram < req:
                    item.setEnabled(False)
                    tip += (f"\nNeeds about {req:g} GB of GPU memory"
                            + (f"; this PC has {vram:.0f} GB." if vram else " (no CUDA GPU detected)."))
                    if not min_mem:
                        tip += " Turn on 'Minimal VRAM mode' in Settings to run it on less GPU (offloads to RAM; slower)."
                item.setToolTip(tip)
        suggested = next(i for i, (_l, _k, need) in enumerate(MODEL_CHOICES) if vram >= need)
        self.model_combo.setCurrentIndex(suggested)
        self._vram = vram
        self.model_combo.currentIndexChanged.connect(self.update_model_options)
        options.addRow("Model:", self.model_combo)
        # Optional extra pictures of the theme's characters (renders, official
        # art): more complete figures and poses. Used for characters only.
        self.extra_images = []
        extra_row = QHBoxLayout(); self.extra_label = QLabel(); extra_row.addWidget(self.extra_label, 1)
        btn_add_extra = QPushButton("Add Images..."); btn_add_extra.clicked.connect(self.add_extra_images); extra_row.addWidget(btn_add_extra)
        btn_add_extra.setToolTip("Add pictures of this theme's characters from your PC (official art, renders, screenshots).\n"
                                 "Transparent PNG renders work best.")
        self.btn_web_extra = QPushButton("Search Web..."); self.btn_web_extra.clicked.connect(self.search_extra_images); extra_row.addWidget(self.btn_web_extra)
        self.btn_web_extra.setToolTip("Find pictures of the theme's characters online, tick the ones to keep, search again as needed."
                                      if self.has_title else
                                      "Give the theme its name first (Title in the creator): the search starts from it.")
        self.btn_web_extra.setEnabled(self.has_title)
        btn_clear_extra = QPushButton("Clear"); btn_clear_extra.clicked.connect(self.clear_extra_images); extra_row.addWidget(btn_clear_extra)
        btn_clear_extra.setToolTip("Remove all added extra artwork from this generation (the files themselves are kept).")
        self.extra_label.setToolTip("Optional: more pictures of this theme's characters (official art, renders, screenshots). "
                                    "They give more complete figures and poses for the icons. Transparent PNG renders work best. "
                                    "The theme's colours and style still come from its wallpapers.")
        options.addRow("Extra artwork:", extra_row)
        self.pose_check = QCheckBox("AI pose && expression variation (experimental)")
        self.pose_check.toggled.connect(lambda _on: (self.update_model_tooltip(), self.refresh_state()))
        self.pose_check.setToolTip("When a character is reused, redraw it in a new pose and expression instead of repeating it. "
                                   "Downloads about 0.8 GB more with the Standard model, or about 3.4 GB more with "
                                   "High quality, and adds a few seconds per redrawn icon. "
                                   "Redraws that no longer look like the character are discarded automatically.")
        options.addRow("", self.pose_check)
        self.redraw_check = QCheckBox("Redraw characters into each icon (accuracy-checked)"); self.redraw_check.setChecked(True)
        self.redraw_check.setToolTip("Repaint each character into the icon's art style, light and edges instead of pasting it, "
                                     "and continue characters that are cut off in the wallpapers. Every redraw is compared with "
                                     "the real character and only kept if it still matches. Adds a few seconds per character icon.")
        options.addRow("", self.redraw_check)
        self.update_model_options()
        self.update_extra_label()
        self.steps_spin = QSpinBox(); self.steps_spin.setRange(10, 60); self.steps_spin.setValue(20)
        self.steps_spin.setToolTip("How many refinement passes the AI makes on each image.\n"
                                   "20 is the sweet spot. Lower is faster but rougher; higher adds a little detail "
                                   "but takes proportionally longer (40 steps is about twice the time of 20), "
                                   "with little visible gain past 30.")
        self.steps_spin.valueChanged.connect(lambda _v: self.refresh_state())   # keep the time estimate live
        options.addRow("Quality steps:", self.steps_spin)
        seed_row = QHBoxLayout(); self.seed_spin = QSpinBox(); self.seed_spin.setRange(0, 2_147_483_647); self.seed_spin.setValue(random.randint(0, 999999))
        btn_reseed = QPushButton("🎲"); btn_reseed.setFixedWidth(36); btn_reseed.setToolTip("New random seed"); btn_reseed.clicked.connect(lambda: self.seed_spin.setValue(random.randint(0, 999999)))
        self.lock_seed = QCheckBox("Lock seed"); self.lock_seed.setToolTip("Keep this seed to reproduce a result. Unlocked, every Generate uses a new random seed.")
        seed_row.addWidget(self.seed_spin); seed_row.addWidget(btn_reseed); seed_row.addWidget(self.lock_seed); options.addRow("Seed:", seed_row)
        self.seed_spin.setToolTip("The number every random choice comes from (layouts, characters, art).\n"
                                  "The same seed and settings give the same icons; a new seed gives a new variation.")
        options.addRow("Style references:", QLabel(f"{len(wallpapers)} wallpaper(s) from this theme"))
        self.examples_label = QLabel()
        self.examples_label.setToolTip("Custom icon sets from community themes: how other authors designed each app's icon, "
                                       "and their layouts. New sets are added automatically in the background.")
        options.addRow("Icon examples:", self.examples_label)
        self.examples_thread = None
        # Two pages: the settings, then (once Generate is pressed) the results
        # at the icons' real size. No preview box before anything is generated.
        self.pages = QStackedWidget()
        settings_page = QWidget(); settings_layout = QVBoxLayout(settings_page)
        settings_layout.setContentsMargins(0, 0, 0, 0); settings_layout.addLayout(options); settings_layout.addStretch()
        self.pages.addWidget(settings_page)

        # Results: every icon at its real Vita size (128 px), filled in as each
        # one finishes (the whole set is always generated: a theme needs every icon).
        results_page = QWidget(); grid = QGridLayout(results_page)
        grid.setHorizontalSpacing(14); grid.setVerticalSpacing(6)
        info = _worker_module()
        for i, name in enumerate(ICON_NAMES):
            cell = QVBoxLayout()
            preview = QLabel(); preview.setFixedSize(ICON_PREVIEW, ICON_PREVIEW); preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            preview.setProperty("role", "preview")
            preview.setToolTip(f"The {info.ICON_SUBJECTS.get(name, 'app')} icon. "
                               + ("It features one of the theme's characters." if name in info.CHARACTER_ICONS
                                  else "Symbol and theme art only, no character."))
            caption = QLabel(name.removeprefix('icon_').removesuffix('.png'))
            cell.addWidget(preview, alignment=Qt.AlignmentFlag.AlignCenter); cell.addWidget(caption, alignment=Qt.AlignmentFlag.AlignCenter)
            grid.addLayout(cell, i // 6, i % 6)
            self.preview_labels[name] = preview
        self.pages.addWidget(results_page)

        # Progress: its own screen while icons are made or AI parts download.
        progress_page = QWidget(); pl = QVBoxLayout(progress_page); pl.addStretch()
        self.prog_title = QLabel(); self.prog_title.setStyleSheet("font-size: 18px; font-weight: bold;")
        self.prog_phase = QLabel(); self.prog_phase.setWordWrap(True)
        self.prog_bar = QProgressBar(); self.prog_bar.setMinimumHeight(26)
        self.prog_count = QLabel(); self.prog_count.setStyleSheet("font-size: 15px;")
        self.prog_eta = QLabel(); self.prog_eta.setStyleSheet("font-size: 15px; font-weight: bold;")
        self.prog_last = QLabel(); self.prog_last.setFixedSize(ICON_PREVIEW, ICON_PREVIEW); self.prog_last.setProperty("role", "preview")
        self.prog_last_name = QLabel()
        for w in (self.prog_title, self.prog_phase, self.prog_bar, self.prog_count, self.prog_eta):
            pl.addWidget(w, alignment=Qt.AlignmentFlag.AlignHCenter if w is not self.prog_bar else Qt.AlignmentFlag.AlignVCenter)
        pl.addSpacing(12)
        pl.addWidget(self.prog_last, alignment=Qt.AlignmentFlag.AlignHCenter)
        pl.addWidget(self.prog_last_name, alignment=Qt.AlignmentFlag.AlignHCenter)
        pl.addStretch()
        self.pages.addWidget(progress_page)
        self._run = None
        self._remaining = None
        self._eta_timer = QTimer(self); self._eta_timer.timeout.connect(self._tick_eta)
        # Community theme art: fetched once per session (a new seed reuses it),
        # deleted when this window closes.
        self._community_tmp = None
        self._community_art = None
        layout.addWidget(self.pages, 1)

        self.progress = QProgressBar(); self.progress.setVisible(False); layout.addWidget(self.progress)
        self.log_view = QPlainTextEdit(); self.log_view.setReadOnly(True); self.log_view.setMaximumHeight(110); self.log_view.setVisible(False); layout.addWidget(self.log_view)

        buttons = QHBoxLayout()
        self.btn_log = QPushButton("Show Log"); self.btn_log.setCheckable(True); self.btn_log.toggled.connect(self.toggle_log); buttons.addWidget(self.btn_log)
        self.btn_log.setToolTip("Show the generator's detailed messages (character redraw scores, symbol checks, errors).")
        self.btn_settings = QPushButton("Change Settings"); self.btn_settings.clicked.connect(lambda: self.pages.setCurrentIndex(0))
        self.btn_settings.setToolTip("Go back to the options, then Generate again for a new set.")
        buttons.addWidget(self.btn_settings)
        buttons.addWidget(self.btn_install)
        buttons.addWidget(self.btn_remove)
        buttons.addStretch()
        self.btn_generate = QPushButton("Generate"); self.btn_generate.setProperty("role", "success"); self.btn_generate.clicked.connect(self.generate); buttons.addWidget(self.btn_generate)
        self.btn_generate.setToolTip("Make all 17 icons. They appear at their real size as each one finishes.\n"
                                     "Unless the seed is locked, every click gives a new variation.")
        self.btn_cancel = QPushButton("Cancel"); self.btn_cancel.clicked.connect(self.cancel_task); self.btn_cancel.setVisible(False); buttons.addWidget(self.btn_cancel)
        self.btn_apply = QPushButton("Use Generated Icons"); self.btn_apply.setEnabled(False); self.btn_apply.clicked.connect(self.accept); buttons.addWidget(self.btn_apply)
        self.btn_apply.setToolTip("Put the generated icons into the theme's icon slots in the creator.\n"
                                  "Nothing is saved until you save the theme.")
        btn_close = QPushButton("Close"); btn_close.clicked.connect(self.reject); buttons.addWidget(btn_close)
        layout.addLayout(buttons)

        self.pages.currentChanged.connect(lambda: self.refresh_state(busy=self.thread is not None))
        self.refresh_state()
        self.update_examples_label()
        self.fetch_examples()

    def showEvent(self, event):
        super().showEvent(event)
        from widgets import center_on_parent
        center_on_parent(self)

    # --- State ---
    def update_generator_options(self):
        """Google shows as unavailable (greyed, with a hint) until a key is set."""
        from PyQt6.QtGui import QColor
        index = self.generator_combo.findData('google')
        label = next(l for l, k in GENERATOR_CHOICES if k == 'google')
        item = self.generator_combo.model().item(index)
        if google_api_key():
            item.setText(label)
            item.setData(None, Qt.ItemDataRole.ForegroundRole)
        else:
            item.setText(f"{label}: needs an API key in Settings")
            item.setForeground(QColor('gray'))

    def on_generator_changed(self):
        if self._cloud() and not google_api_key():
            box = QMessageBox(QMessageBox.Icon.Warning, "Google API Key Needed",
                              "Generating with Google Gemini needs your own Google API key.\n\n"
                              "Add it in Settings > AI Icons (get one at aistudio.google.com/apikey). "
                              "Gemini image models are paid: each icon is billed to your key.", parent=self)
            open_settings = box.addButton("Open Settings", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is open_settings:
                from settings_dialog import SettingsDialog
                dialog = SettingsDialog(self, ai_icons=True)
                dialog.tabs.setCurrentWidget(dialog.ai_tab)
                dialog.exec()
            self.update_generator_options()
            if not google_api_key():
                # Still no key: back to this PC.
                self.generator_combo.blockSignals(True)
                self.generator_combo.setCurrentIndex(self.generator_combo.findData('local'))
                self.generator_combo.blockSignals(False)
        self.refresh_state()

    def _wallpapers_needed_text(self):
        if not self.has_title:
            return ("<b>Give the theme its name first</b> (Title in the creator, not \"My New Theme\"). "
                    "The name tells the generator and the artwork search which series and characters the theme is about.")
        more = MIN_WALLPAPERS - len(self.wallpapers)
        return (f"<b>Add {more} more wallpaper{'s' if more != 1 else ''}</b> first (home pages; a lock screen counts too if the theme has one). "
                f"Icons are made from the theme's own wallpapers (style, colours and characters), so at least "
                f"{MIN_WALLPAPERS} are needed; this theme has {len(self.wallpapers)}.")

    def _cloud(self):
        return self.generator_combo.currentData() == 'google'

    def refresh_state(self, busy=False):
        cloud = self._cloud()
        installed = is_runtime_installed()
        self.btn_install.setText("Update AI Components (small download)..." if runtime_needs_update() else "Install AI Components...")
        enough = len(self.wallpapers) >= MIN_WALLPAPERS and self.has_title
        has_inputs = enough and _templates_dir() is not None and os.path.isfile(_worker_script())
        self.btn_install.setVisible(not installed and not cloud); self.btn_install.setEnabled(not busy)
        self.btn_remove.setVisible(os.path.isdir(ICON_AI_DIR) and not cloud); self.btn_remove.setEnabled(not busy)
        if not hasattr(self, '_ai_size'):
            self._measure_size()
        self._show_ai_size()
        # Community examples come from the release-only theme repository.
        self.generator_combo.setEnabled(not busy)
        # The fine controls drive the local model; Gemini follows the request on its own.
        for widget in (self.creativity_slider, self.style_slider, self.consistency_slider, self.clarity_slider,
                       self.model_combo, self.redraw_check, self.steps_spin):
            widget.setEnabled(not cloud)
        self.pose_check.setEnabled(not cloud)
        ready = (bool(google_api_key()) and enough and _templates_dir() is not None) if cloud \
            else (installed and has_inputs)
        self.btn_generate.setEnabled(ready and not busy)
        self.btn_cancel.setVisible(busy); self.progress.setVisible(False)
        # While something runs, the progress screen says it all: only Show Log,
        # Cancel and Close stay.
        for widget in (self.btn_generate, self.btn_apply):
            widget.setVisible(not busy)
        if busy:
            self.btn_remove.setVisible(False)
        self.status_label.setVisible(self.pages.currentIndex() != 2)
        self.btn_settings.setVisible(self.pages.currentIndex() == 1 and not busy)
        if busy:
            return
        if cloud:
            model = google_model()
            if not google_api_key():
                self.status_label.setText("<b>Add your Google API key</b> in Settings &gt; AI Icons to generate with Gemini "
                                          "(get one at aistudio.google.com/apikey).")
            elif not enough:
                self.status_label.setText(self._wallpapers_needed_text())
            else:
                startup, per_icon = load_timing('google')
                total = startup + per_icon * len(ICON_NAMES)
                self.status_label.setText(f"Ready. Gemini ({model}) makes all {len(ICON_NAMES)} icons in the cloud in "
                                          f"{format_eta(total)}: nothing to download, about "
                                          f"${gemini_price(model) * len(ICON_NAMES):.2f} in total "
                                          f"(${gemini_price(model):.3f} per icon) billed to your Google API key.")
            return
        if runtime_needs_update():
            self.status_label.setText("<b>AI components need a small update</b> for this version (PyTorch is kept).")
        elif not installed:
            self.status_label.setText("<b>AI components are not installed.</b> This feature is optional. Nothing is downloaded unless you click Install.")
        elif not enough:
            self.status_label.setText(self._wallpapers_needed_text())
        elif _templates_dir() is None or not os.path.isfile(_worker_script()):
            self.status_label.setText("Icon templates or the generator script are missing from this installation.")
        else:
            min_mem = bool(_settings().get('ai_min_memory'))
            startup, per_icon = load_timing(timing_key({'model': self.model_combo.currentData(),
                                                       'pose_variation': self.pose_check.isChecked(),
                                                       'min_memory': min_mem}, False))
            # Only mention a download if the chosen setup's models aren't all here (a local check, no network).
            model, poses = self.model_combo.currentData(), self.pose_check.isChecked()
            if not hasattr(self, '_models_ready'):
                self._models_ready = {}
            if (model, poses) not in self._models_ready:
                try:
                    if model in FLUX2_KEYS:
                        self._models_ready[(model, poses)] = _flux2_present(model)
                    else:
                        files = _worker_module().model_files(_runtime_uses_half_precision(), True, poses, model)
                        self._models_ready[(model, poses)] = all(_model_cached(r, f) for r, f in files)
                except Exception:
                    self._models_ready[(model, poses)] = False
            download = "" if self._models_ready[(model, poses)] else \
                " The first run with this model also downloads it (the size is shown before anything downloads)."
            # Factor in everything chosen on this screen: model, AI poses and
            # Minimal VRAM mode (via the timing key) plus the quality-steps count
            # (the defaults are measured at 20 steps).
            step_factor = max(0.4, self.steps_spin.value() / 20)
            total = startup + per_icon * step_factor * len(ICON_NAMES)
            extras = []
            if poses:
                extras.append("AI poses")
            if min_mem:
                extras.append("Minimal VRAM mode")
            with_note = f" (with {', '.join(extras)})" if extras else ""
            self.status_label.setText(f"Ready. Making all {len(ICON_NAMES)} icons takes "
                                      f"{format_eta(total)} on this PC{with_note}.{download}")

    def update_model_options(self):
        # AI poses work with both models (each has its own pose model).
        self.pose_check.setEnabled(not self._cloud())
        self.update_model_tooltip()
        if hasattr(self, 'btn_generate'):
            self.refresh_state()

    def update_model_tooltip(self):
        """The models compared, with how long a full set takes on this PC for
        each (measured runs, or the starting estimates)."""
        poses = self.pose_check.isChecked() if hasattr(self, 'pose_check') else False
        min_mem = bool(_settings().get('ai_min_memory'))
        times = {}
        for key in ('sdxl', 'sd15'):
            startup, per_icon = load_timing(timing_key({'model': key, 'pose_variation': poses, 'min_memory': min_mem}, False))
            times[key] = startup + per_icon * len(ICON_NAMES)
        ratio = times['sdxl'] / max(1.0, times['sd15'])
        measured = all(timing_measured(timing_key({'model': k, 'pose_variation': poses, 'min_memory': min_mem}, False))
                       for k in ('sdxl', 'sd15'))
        self.model_combo.setToolTip(
            f"Time for all {len(ICON_NAMES)} icons{' with AI poses' if poses else ''}"
            f"{' in Minimal VRAM mode' if min_mem else ''} "
            f"({'measured on this PC' if measured else 'estimated for this PC until it has run each model'}):\n"
            f"  High quality: {format_eta(times['sdxl'])}\n"
            f"  Standard: {format_eta(times['sd15'])}  (about {ratio:.1f}x faster)\n\n"
            "High quality: an anime-tuned SDXL model. Much better art, anatomy and prompt following; "
            "needs about 8 GB of GPU memory and about 8 GB more to download.\n"
            "Standard: smaller and faster, works on 4 GB GPUs."
            + (f"\nYour GPU has {self._vram:.0f} GB." if self._vram else ""))

    def add_extra_images(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add Character Artwork", "", "Images (*.png *.jpg *.jpeg *.webp)")
        self.extra_images += [p for p in paths if p not in self.extra_images]
        self.update_extra_label()

    def search_extra_images(self):
        dialog = ImageSearchDialog(self, self.theme_title)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.extra_images += [p for p in dialog.chosen if p not in self.extra_images]
            self.update_extra_label()

    def clear_extra_images(self):
        self.extra_images = []
        self.update_extra_label()

    def update_extra_label(self):
        n = len(self.extra_images)
        self.extra_label.setText(f"{n} image{'s' if n != 1 else ''} added" if n else "None (optional)")

    def toggle_log(self, shown):
        self.log_view.setVisible(shown); self.btn_log.setText("Hide Log" if shown else "Show Log")

    def append_log(self, line):
        self.log_view.appendPlainText(line)

    def _start(self, thread):
        self.thread = thread
        thread.log.connect(self.append_log)
        thread.finished_ok.connect(self.on_task_finished)
        self.refresh_state(busy=True)
        thread.start()

    def on_task_finished(self, ok, message):
        self.thread = None
        # Installs and first runs download things: measure again.
        self._models_ready = {}
        self._measure_size()
        self._eta_timer.stop()
        run, self._run = self._run, None
        if run and ok and run['done'] and run['first'] is not None:
            startup = run['first'] - run['start']
            per_icon = (run['last'] - run['first']) / run['done']
            # A run that downloaded models has download time baked into its
            # start-up: keep the per-icon time, but don't treat that start-up as
            # a model-load measurement (and never share network speed).
            downloaded = run.get('downloaded')
            if not downloaded:
                save_timing(run['key'], startup, per_icon)
            # Full record (hardware + settings + measured time and peak memory),
            # so this run can refine estimates and, by hardware comparison,
            # predict other machines.
            record_calibration(run['key'], run.get('job') or {}, None if downloaded else startup, per_icon,
                                run['done'], peak_vram=run.get('peak_vram'), peak_ram=run.get('peak_ram'),
                                cloud=run.get('cloud', False))
        # Generation goes on to its results; anything else back to the settings.
        self.pages.setCurrentIndex(1 if run and self.generated else 0)
        self.refresh_state()
        self.status_label.setText(message if ok else f"<b>Failed:</b> {message}")
        self.btn_apply.setEnabled(bool(self.generated))
        if ok:
            play_done_sound(self)  # generation (or install) can take a while: let the user know
        else:
            self.btn_log.setChecked(True)

    def cancel_task(self):
        if self.thread:
            self.status_label.setText("Cancelling...")
            self.thread.cancel()

    # --- Runtime management ---
    def _measure_size(self):
        """Measure the AI install's disk use in the background (the window opens
        at once); the Remove button shows it when known."""
        self._ai_size = None
        self._size_thread = EstimateThread(runtime_disk_usage, self)
        self._size_thread.result.connect(self._size_measured)
        self._size_thread.start()

    def _size_measured(self, size):
        self._ai_size = size if isinstance(size, int) else None
        self._show_ai_size()

    def _show_ai_size(self):
        self.btn_remove.setText(f"Remove AI Components ({format_size(self._ai_size)})" if self._ai_size
                                else "Remove AI Components")

    def _with_estimate(self, fn, status, then):
        """Run an estimate in the background, then call then(estimate)."""
        self.status_label.setText(status)
        self.btn_install.setEnabled(False); self.btn_generate.setEnabled(False)
        self._estimate = EstimateThread(fn, self)
        self._estimate.result.connect(lambda est: (self.refresh_state(), then(est)))
        self._estimate.start()

    @staticmethod
    def _estimate_text(est):
        if not est or 'error' in est:
            return "The download size and time could not be estimated right now."
        lines = [f"Download: about {format_size(est['total'])}"]
        if len(est.get('parts', [])) > 1:
            lines += [f"  \u2022 {name}: {format_size(size)}" for name, size in est['parts']]
        if est.get('disk_use'):
            lines.append(f"Space used on disk once installed: about {format_size(est['disk_use'])}")
        if est.get('speed'):
            dl = est.get('download_eta', est.get('eta'))
            lines.append(f"  \u2022 Downloading: {format_eta(dl)} (download server speed ~{format_size(est['speed'])}/s"
                         + (f", disk ~{format_size(est['disk'])}/s)" if est.get('disk') else ")"))
            if est.get('install_eta'):
                lines.append(f"  \u2022 Unpacking and installing: {format_eta(est['install_eta'])}")
            if est.get('load_eta'):
                lines.append(f"  \u2022 First model load from disk: {format_eta(est['load_eta'])}")
            lines.append(f"Total: {format_eta(est['eta'])}.")
        return "\n".join(lines)

    def install_runtime(self):
        self._with_estimate(estimate_install, "Checking download size and your connection speed...", self._confirm_install)

    def _confirm_install(self, est):
        rows = check_requirements('install', est, self.model_combo.currentData(), self.pose_check.isChecked())
        if RequirementsDialog(self, "Install AI Components", "Install the AI components to make icons on this PC?",
                              est, rows).exec() != QDialog.DialogCode.Accepted:
            return
        thread = RuntimeInstallThread(self)
        thread.progress.connect(self.on_install_progress)
        self._show_progress("Installing the AI components")
        self._start(thread)

    def on_install_progress(self, percent, text):
        self.status_label.setText(text)
        self.prog_phase.setText(text)
        if percent < 0:
            self.prog_bar.setRange(0, 0)
        else:
            self.prog_bar.setRange(0, 100); self.prog_bar.setValue(percent)

    def update_examples_label(self, busy=False):
        count = example_set_count()
        text = f"{count} icon set{'s' if count != 1 else ''} from community themes"
        if busy:
            text += " (adding more in the background...)"
        self.examples_label.setText(text)

    def fetch_examples(self):
        """Top up the community icon sets quietly in the background (release
        builds): they show the generator how other theme authors designed
        each app's icon, and their layouts."""
        missing = COMMUNITY_EXAMPLE_TARGET - example_set_count()
        if not ONLINE_FEATURES_AVAILABLE or missing <= 0:
            return
        # No parent: the download may outlive the dialog (closing it must not
        # wait on a theme download); _BACKGROUND keeps it alive until done.
        thread = ExampleDownloadThread(min(missing, COMMUNITY_EXAMPLES_PER_OPEN))
        _BACKGROUND.append(thread)
        thread.finished.connect(lambda: _BACKGROUND.remove(thread) if thread in _BACKGROUND else None)
        thread.finished_ok.connect(self._examples_done)
        self.examples_thread = thread
        self.update_examples_label(busy=True)
        thread.start()

    def _examples_done(self, ok, message):
        self.examples_thread = None
        try:
            self.append_log(f"Community icon sets: {message}")
            self.update_examples_label()
        except RuntimeError:
            pass  # the dialog was closed meanwhile

    def remove_runtime(self):
        size = self._ai_size if self._ai_size is not None else runtime_disk_usage()
        if QMessageBox.question(self, "Remove AI Components",
                                f"Delete the AI runtime and downloaded models ({format_size(size)})?\n\n{ICON_AI_DIR}") != QMessageBox.StandardButton.Yes:
            return
        remove_runtime()
        self._models_ready = {}
        self._measure_size()
        self.refresh_state()

    # --- Generation ---
    def generate(self):
        icons = list(ICON_NAMES)
        if not self.lock_seed.isChecked():
            # A fresh seed per Generate, so every click gives a new variation.
            self.seed_spin.setValue(random.randint(0, 999999))
        # A new attempt replaces the last one: its unused icons are deleted.
        self._discard_output()
        self.generated = {}
        self.btn_apply.setEnabled(False)
        self.output_dir = tempfile.mkdtemp(prefix='vta_icons_')
        job = {
            'title': self.theme_title,
            'theme_colors': self.theme_colors,
            'theme_color_mode': self.color_mode_combo.currentData(),
            'templates_dir': _templates_dir(),
            'wallpapers': self.wallpapers,
            'icons': icons,
            'output_dir': self.output_dir,
            'creativity': self.creativity_slider.value() / 100,
            'style_scale': self.style_slider.value() / 100,
            'consistency': self.consistency_slider.value() / 100,
            'clarity': self.clarity_slider.value() / 100,
            'layout_style': self.layout_combo.currentData(),
            'extra_images': list(self.extra_images),
            'pose_variation': self.pose_check.isChecked(),
            'model': self.model_combo.currentData(),
            'redraw': self.redraw_check.isChecked(),
            'examples_dirs': [d for d in (_examples_dir(), EXAMPLES_DIR) if d and os.path.isdir(d)],
            'cache_dir': ICON_AI_DIR,
            'steps': self.steps_spin.value(),
            'seed': self.seed_spin.value(),
        }
        self._apply_model_settings(job)
        self._remember_generator()
        if self._community_art is not None:
            # Already fetched in this session: a new seed reuses it.
            self._continue_generate(dict(job, auto_extra_images=self._community_art))
            return
        if ONLINE_FEATURES_AVAILABLE and job['layout_style'] != 'emblem' and not self._cloud():
            # More pictures of the theme's characters from community themes of
            # the same series (theme API), fetched automatically first.
            self.status_label.setText("Looking for more art of this theme's characters in community themes...")
            self.btn_generate.setEnabled(False)
            # Temporary: deleted when this generation ends (see on_task_finished).
            self._community_tmp = tempfile.mkdtemp(prefix='vta_community_art_')
            self._art_thread = CommunityArtThread(self.theme_title, self._community_tmp, self)
            self._art_thread.log.connect(self.append_log)
            self._art_thread.done.connect(lambda paths: (setattr(self, '_community_art', paths),
                                                         self._continue_generate(dict(job, auto_extra_images=paths))))
            self._art_thread.start()
            return
        self._continue_generate(job)

    def _apply_model_settings(self, job):
        """Fold the user's custom-model and Low VRAM settings into the job. A
        custom SD 1.5/SDXL checkpoint is used only when its architecture matches
        the selected quality tier; otherwise the default model for that tier is
        downloaded as usual."""
        settings = _settings()
        job['hf_token'] = (settings.get('hf_token') or '').strip()   # for the gated full FLUX.2 download
        job['min_memory'] = bool(settings.get('ai_min_memory'))       # force least-memory offload for every model
        path = (settings.get('custom_base_model') or '').strip()
        if not path or not os.path.exists(path):
            return
        arch = settings.get('custom_base_arch') or 'auto'
        if arch == 'auto':
            try:
                arch = _worker_module().detect_base_arch(path)
            except Exception:
                arch = None
        if arch == job.get('model'):
            job['custom_base_model'] = path
            self.append_log(f"Using your custom {arch.upper()} model: {os.path.basename(path)}")
        elif arch:
            self.append_log(f"Custom model looks like {arch.upper()} but the selected quality tier is "
                            f"{(job.get('model') or '').upper()}; using the default model for that tier instead.")

    def _continue_generate(self, job):
        self.refresh_state()
        if self._cloud():
            self._confirm_gemini(job)
            return
        segmenter = job['layout_style'] != 'emblem'
        poses = job['pose_variation'] and segmenter
        model = job['model']
        skip_base = bool(job.get('custom_base_model'))
        self._with_estimate(lambda: estimate_models(segmenter, poses, model, skip_base=skip_base),
                            "Checking whether AI models need downloading...",
                            lambda est: self._confirm_models(est, job))

    def _show_results(self):
        """Empty the results page's slots for the new set."""
        for preview in self.preview_labels.values():
            preview.clear()
            preview.setText("…")

    def _show_progress(self, title, job=None, cloud=False):
        """The progress screen: loading up first, then icon by icon with a
        live countdown estimated from this PC's earlier runs and adjusted as
        this run goes."""
        self.pages.setCurrentIndex(2)
        self.prog_title.setText(title)
        self.prog_phase.setText("Loading up...")
        self.prog_bar.setRange(0, 0)
        self.prog_count.setText("")
        self.prog_last.clear(); self.prog_last_name.setText("")
        self._run = None
        self._remaining = None
        if job is not None:
            key = timing_key(job, cloud)
            startup, per_icon = load_timing(key)
            self._run = {'key': key, 'total': len(job['icons']), 'done': 0, 'start': time.monotonic(),
                         'first': None, 'last': None, 'startup': startup, 'per': per_icon,
                         'cloud': cloud, 'job': dict(job), 'peak_vram': None, 'peak_ram': None}
            self._remaining = startup + per_icon * len(job['icons'])
            self.prog_count.setText(f"Icon 0 of {len(job['icons'])} finished")
        self._show_eta()
        self._eta_timer.start(1000)

    def _tick_eta(self):
        if self._remaining is not None:
            self._remaining = max(0.0, self._remaining - 1)
        self._show_eta()

    def _show_eta(self):
        if self._remaining is None:
            self.prog_eta.setText("")
        elif self._remaining <= 1:
            self.prog_eta.setText("Almost done...")
        else:
            self.prog_eta.setText(format_countdown(self._remaining))

    def _reestimate(self):
        """Time left from this run's own pace, trusted more with every icon."""
        run = self._run
        now = time.monotonic()
        left = run['total'] - run['done']
        if run['done'] and run['first'] is not None:
            measured = (now - run['first']) / run['done']
            per_icon = (run['per'] * 2 + measured * run['done']) / (2 + run['done'])
        else:
            per_icon = run['per']
        # The icon being made now is already partly done.
        self._remaining = max(3.0, per_icon * left - min(per_icon, now - (run['last'] or now))) if left else 0.0

    def _remember_generator(self):
        settings = _settings()
        settings['icon_ai_generator'] = self.generator_combo.currentData()
        try:
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=4)
        except OSError:
            pass

    def _confirm_gemini(self, job):
        model, count = google_model(), len(job['icons'])
        if QMessageBox.question(
                self, "Generate with Google Gemini",
                f"{count} icon{'s' if count != 1 else ''} will be made with {model}, one request each "
                f"(about ${gemini_price(model) * count:.2f} in total, billed to your Google API key).\n\n"
                "The theme's wallpapers and any extra artwork are sent to Google as references.\n\nContinue?"
        ) != QMessageBox.StandardButton.Yes:
            return
        self._show_results()
        self._show_progress("Making your icons with Gemini", job, cloud=True)
        thread = GeminiIconThread(job, google_api_key(), model, self)
        thread.event.connect(self.on_generation_event)
        self._start(thread)

    def _flux2_license_ok(self):
        """One-time acceptance of the FLUX.2 non-commercial licence before the
        model is downloaded."""
        settings = _settings()
        if settings.get('flux2_license_accepted'):
            return True
        if QMessageBox.question(
                self, "FLUX.2 Licence",
                "FLUX.2 [dev] is downloaded from Black Forest Labs under the FLUX.2 Non-Commercial "
                "Licence. The model weights are for personal, non-commercial use; the icons you make "
                f"with it are yours to use.\n\nFull licence:\n{FLUX2_LICENSE_URL}\n\nDownload and use FLUX.2?"
        ) != QMessageBox.StandardButton.Yes:
            return False
        try:
            settings['flux2_license_accepted'] = True
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=4)
        except OSError:
            pass
        return True

    def _install_bnb_then(self, then):
        """Install bitsandbytes (FLUX.2's 4-bit dependency) off the UI thread,
        then run `then`."""
        dlg = QProgressDialog("Installing a FLUX.2 component (bitsandbytes)…", "", 0, 0, self)
        dlg.setWindowTitle("Preparing FLUX.2")
        dlg.setCancelButton(None)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.show()
        worker = _Worker(_install_bitsandbytes, self)

        def done(result):
            dlg.close()
            if result is True:
                then()
            else:
                msg = result.get('error') if isinstance(result, dict) else None
                QMessageBox.warning(self, "FLUX.2", "Could not install the FLUX.2 component (bitsandbytes)."
                                    + (f"\n\n{msg}" if msg else ""))
        worker.done.connect(done)
        self._bnb_worker = worker
        worker.start()

    def _confirm_models(self, est, job):
        # FLUX.2 needs a one-time licence accept (both tiers); the 4-bit tier also
        # needs the bitsandbytes package before anything downloads.
        if job.get('model') in FLUX2_KEYS:
            if not self._flux2_license_ok():
                return
            # The full tier is a gated download: it needs a Hugging Face token.
            if job['model'] == 'flux2_full' and not _flux2_present('flux2_full') \
                    and not (_settings().get('hf_token') or '').strip():
                QMessageBox.information(
                    self, "Hugging Face Token Needed",
                    "The full FLUX.2 (Ultra+) model is a gated download. To use it:\n\n"
                    "1. Create a free Hugging Face access token (huggingface.co/settings/tokens).\n"
                    "2. Accept the FLUX.2 licence on its model page (huggingface.co/black-forest-labs/FLUX.2-dev).\n"
                    "3. Paste the token in Settings > AI Icons (Hugging Face token).\n\n"
                    "The 4-bit FLUX.2 tier needs none of this.")
                return
            if job['model'] == 'flux2' and not _package_installed('bitsandbytes'):
                self._install_bnb_then(lambda: self._confirm_models(est, dict(job)))
                return
        if est is not None:
            rows = check_requirements('models', est, job['model'], job.get('pose_variation'), bool(job.get('min_memory')))
            if RequirementsDialog(self, "Download AI Models", "The AI models need downloading first (one time only).",
                                  est, rows).exec() != QDialog.DialogCode.Accepted:
                return
        self._show_results()
        self._show_progress("Making your icons", job)
        thread = IconGenerationThread(job, self)
        thread.event.connect(self.on_generation_event)
        self._start(thread)

    def on_generation_event(self, data):
        event = data.get('event')
        run = self._run
        if event == 'status':
            self.status_label.setText(data.get('message', ''))
            if not run or run['first'] is None:
                self.prog_phase.setText(data.get('message', ''))
        elif event == 'progress':
            if run and run['first'] is None:
                # Loading is over: from here on it is icon by icon.
                run['first'] = run['last'] = time.monotonic()
                self.prog_bar.setRange(0, data['total'])
                self._reestimate()
            self.prog_bar.setValue(run['done'] if run else data['index'])
            name = data['name'].removeprefix('icon_').removesuffix('.png')
            self.prog_phase.setText(f"Making the {name} icon...")
            self.status_label.setText(f"Generating {data['name']} ({data['index'] + 1}/{data['total']})...")
        elif event == 'icon':
            self.generated[data['name']] = data['path']
            pixmap = QPixmap(data['path']).scaled(ICON_PREVIEW, ICON_PREVIEW, Qt.AspectRatioMode.KeepAspectRatio,
                                                  Qt.TransformationMode.SmoothTransformation)
            label = self.preview_labels.get(data['name'])
            if label:
                label.setPixmap(pixmap)
            self.prog_last.setPixmap(pixmap)
            self.prog_last_name.setText(f"Just finished: {data['name'].removeprefix('icon_').removesuffix('.png')}")
            if run:
                run['done'] += 1
                run['last'] = time.monotonic()
                self.prog_bar.setRange(0, run['total']); self.prog_bar.setValue(run['done'])
                self.prog_count.setText(f"Icon {run['done']} of {run['total']} finished")
                self._reestimate(); self._show_eta()
            self.btn_apply.setEnabled(True)
        elif event == 'peak':
            # Measured peak VRAM/RAM for this run, kept for the calibration record.
            if run:
                run['peak_vram'] = data.get('vram_gb')
                run['peak_ram'] = data.get('ram_gb')
        elif event == 'skipped':
            self.append_log(f"Skipped {data['name']}: {data.get('reason')}")
            if run:
                run['total'] = max(run['done'], run['total'] - 1)
        elif event == 'download':
            if run:
                run['downloaded'] = True   # its start-up includes download time, so don't record that as load time
            self.prog_bar.setRange(0, 1000); self.prog_bar.setValue(int(data['done'] * 1000 / max(1, data['total'])))
            self.prog_phase.setText(download_text("Downloading AI models (one time only)", data['done'], data['total'],
                                                  data.get('speed') or None))
            if data.get('eta') is not None and run:
                # Download time plus the icons still to make.
                self._remaining = data['eta'] + run['startup'] + run['per'] * run['total']
                self._show_eta()

    def _discard_output(self):
        """Delete this dialog's generated icons (an attempt that wasn't used)."""
        if self.output_dir:
            shutil.rmtree(self.output_dir, ignore_errors=True)
            self.output_dir = None

    def reject(self):
        if self.thread:
            # Wait for the thread to stop; destroying a running QThread crashes.
            self.thread.cancel()
            self.thread.wait()
        # Closed without using them: the generated icons are deleted.
        self._discard_output()
        super().reject()

    def done(self, result):
        # A size measurement still running would crash when the window goes.
        if getattr(self, '_size_thread', None):
            self._size_thread.wait()
        # However the window closes (Close, X, Use Generated Icons), the
        # temporary community theme art goes with it.
        if self._community_tmp:
            shutil.rmtree(self._community_tmp, ignore_errors=True)
            self._community_tmp = None
            self._community_art = None
        super().done(result)

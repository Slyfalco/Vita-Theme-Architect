#!/usr/bin/env python3
"""Fetch the native at9codec libraries from the codec's GitHub release into native/.

The app bundles the ATRAC9 codec per-arch from ``native/<os>-<arch>/`` (see
``setup_build.py``). Those libraries are built and released from the separate
``Slyfalco/at9codec`` repo. This script downloads that release's per-os/arch zip
assets (``at9codec-<os>-<arch>.zip``) and drops each library into the matching
``native/<os>-<arch>/`` folder, then refreshes the shared header + license. Commit
the updated ``native/`` tree afterwards so every build (local Windows/Linux and the
macOS CI) bundles the right codec.

Run it AFTER the at9codec release exists (its CI workflow / ``scripts/build-libs.ps1``
must have uploaded the assets). By default it pulls the *latest* release.

    python scripts/update_native_libs.py              # latest at9codec release
    python scripts/update_native_libs.py --tag libs-latest
    python scripts/update_native_libs.py --repo Owner/repo

Requires the GitHub CLI ``gh`` authenticated as an account with read access to the
at9codec repo (or ``GH_TOKEN`` set to such a token). While at9codec is private, the
repo's default ``gh`` login may lack access — export a read-token in ``GH_TOKEN`` for
this command, or run it once the repo is public.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

REPO_DEFAULT = "Slyfalco/at9codec"
# Per-os/arch asset names, e.g. at9codec-windows-x64.zip. The combined
# at9codec-native-libs.zip is intentionally NOT matched.
ASSET_RE = re.compile(r"^at9codec-(windows|linux|macos)-(x64|arm64)\.zip$")
LIB_SUFFIXES = (".dll", ".so", ".dylib", ".dll.lib")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NATIVE_DIR = os.path.join(REPO_ROOT, "native")


def _fail(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="Update native/ from the at9codec release.")
    ap.add_argument("--tag", default=None,
                    help="release tag to pull (default: the latest release)")
    ap.add_argument("--repo", default=REPO_DEFAULT,
                    help=f"at9codec repo (default: {REPO_DEFAULT})")
    args = ap.parse_args()

    if shutil.which("gh") is None:
        _fail("the GitHub CLI 'gh' is required but was not found on PATH.")

    with tempfile.TemporaryDirectory() as tmp:
        cmd = ["gh", "release", "download"]
        if args.tag:
            cmd.append(args.tag)
        cmd += ["--repo", args.repo, "--pattern", "at9codec-*.zip",
                "--dir", tmp, "--clobber"]
        print(f"Downloading {'tag ' + args.tag if args.tag else 'latest release'} "
              f"from {args.repo} ...")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            _fail("gh release download failed. Check that the release exists and that "
                  "gh (or GH_TOKEN) has read access to the at9codec repo.")

        zips = sorted(f for f in os.listdir(tmp) if ASSET_RE.match(f))
        if not zips:
            _fail("the release has no per-os/arch assets (at9codec-<os>-<arch>.zip). "
                  "Run the at9codec build script / workflow to upload them first.")

        updated = []
        header_written = False
        license_written = False
        for z in zips:
            m = ASSET_RE.match(z)
            tag = f"{m.group(1)}-{m.group(2)}"
            dest = os.path.join(NATIVE_DIR, tag)
            os.makedirs(dest, exist_ok=True)
            libs = []
            with zipfile.ZipFile(os.path.join(tmp, z)) as zf:
                for name in zf.namelist():
                    base = os.path.basename(name)
                    if not base:
                        continue
                    low = base.lower()
                    if low.endswith(LIB_SUFFIXES):
                        with zf.open(name) as src, open(os.path.join(dest, base), "wb") as out:
                            shutil.copyfileobj(src, out)
                        libs.append(base)
                    elif base == "at9codec.h" and not header_written:
                        with zf.open(name) as src, open(os.path.join(NATIVE_DIR, "at9codec.h"), "wb") as out:
                            shutil.copyfileobj(src, out)
                        header_written = True
                    elif base == "LICENSE" and not license_written:
                        with zf.open(name) as src, open(os.path.join(NATIVE_DIR, "LICENSE-at9codec"), "wb") as out:
                            shutil.copyfileobj(src, out)
                        license_written = True
            if libs:
                updated.append((tag, libs))
                print(f"  {tag}: {', '.join(libs)}")
            else:
                print(f"  {tag}: WARNING - no library file in {z}", file=sys.stderr)

    if not updated:
        _fail("downloaded assets but found no library files in them.")
    print(f"\nUpdated {len(updated)} platform(s) in native/. "
          f"Review and commit the changes.")


if __name__ == "__main__":
    main()

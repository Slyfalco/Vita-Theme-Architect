"""Public install-integrity check.

Detects a corrupted or incomplete install and advises the user to reinstall.
This is deliberately simple and public: it catches accidental corruption (a bad
download, a half-copied folder) and casual tampering, and refuses to run so the
user doesn't operate a broken install. It is ADVISORY only — a determined person
who modifies the build could strip it. The real enforcement of official-only
online features lives elsewhere (the release modules), not here.

A build ships an `integrity.json` next to the app files (written by
setup_build.generate_integrity_manifest on Windows/Linux; macOS relies on its
code signature instead). Running from source has no manifest, so verification is
a no-op.
"""
import hashlib
import json
import os

MANIFEST_NAME = 'integrity.json'


def _app_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def verify_integrity(base_dir=None, max_problems=20):
    """Check files under base_dir against base_dir/integrity.json.

    Returns (ok, problems). When the manifest is absent (e.g. running from
    source, or macOS) returns (True, []) — there is nothing to verify.
    """
    if base_dir is None:
        base_dir = _app_dir()
    manifest = os.path.join(base_dir, MANIFEST_NAME)
    if not os.path.isfile(manifest):
        return True, []
    try:
        with open(manifest, 'r', encoding='utf-8') as fh:
            files = (json.load(fh) or {}).get('files', {})
    except (OSError, ValueError):
        return False, ['integrity.json is unreadable']

    problems = []
    for rel, expected in files.items():
        path = os.path.join(base_dir, rel.replace('/', os.sep))
        if not os.path.isfile(path):
            problems.append('missing: ' + rel)
        else:
            try:
                if _sha256(path).lower() != str(expected).lower():
                    problems.append('changed: ' + rel)
            except OSError:
                problems.append('unreadable: ' + rel)
        if len(problems) >= max_problems:
            break
    return (not problems), problems

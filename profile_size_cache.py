"""Remembered sizes of managed browser profiles.

Measuring one profile means walking a directory tree that routinely holds a few
hundred megabytes across thousands of files. The overview shows that number on
every row, so the naive approach -- walk on every list bind, and walk every
profile again on every start -- was the single largest source of startup and
scrolling latency in this app.

Two things fix that. The measured value is written to the XDG cache directory,
so a restart shows the numbers immediately instead of walking the disk again,
and a remembered value counts as fresh until either the profile's root
directory is touched or ``MAX_CACHE_AGE_SECONDS`` passes. A slightly stale size
in a list row is a fair trade for a window that opens at once; the stale value
is still displayed while the re-measurement runs in the background.

The module deliberately knows nothing about GTK: the scheduling side lives in
``mainwindow.entries`` so the cache logic stays unit-testable without a widget
tree.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from browser_paths import get_profile_size_bytes
from logger_setup import get_logger

LOG = get_logger(__name__)

CACHE_DIR = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'webapp-manager'
CACHE_PATH = CACHE_DIR / 'profile-sizes.json'
# A profile that has not been touched for a week is re-measured anyway, so a
# value can never drift indefinitely if the mtime signal is missed.
MAX_CACHE_AGE_SECONDS = 7 * 24 * 3600

_CACHE: dict[str, dict] | None = None
_DIRTY = False
# The measuring thread stores while the UI thread looks up and flushes, so the
# dict is guarded rather than relying on which operations happen to be atomic.
_LOCK = threading.RLock()


def format_size(total_bytes) -> str:
    try:
        total = int(total_bytes or 0)
    except (TypeError, ValueError):
        return '0 MB'
    if total <= 0:
        return '0 MB'
    gb = total / (1024 ** 3)
    if gb >= 1:
        return f'{gb:.2f} GB'
    mb = total / (1024 ** 2)
    return f'{mb:.0f} MB'


def _cache_key(profile_path) -> str:
    return str(profile_path or '').strip()


def _root_mtime(profile_path) -> float:
    try:
        return float(Path(profile_path).expanduser().stat().st_mtime)
    except OSError:
        return 0.0


def _load() -> dict[str, dict]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    _CACHE = {}
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return _CACHE
    if not isinstance(raw, dict):
        return _CACHE
    for key, value in (raw.get('profiles') or {}).items():
        if not isinstance(value, dict):
            continue
        try:
            _CACHE[str(key)] = {
                'bytes': int(value.get('bytes', 0)),
                'mtime': float(value.get('mtime', 0.0)),
                'measured_at': float(value.get('measured_at', 0.0)),
            }
        except (TypeError, ValueError):
            continue
    return _CACHE


def lookup_bytes(profile_path):
    """Return ``(size_bytes, stale)`` for a profile, or ``(None, True)``.

    ``None`` means nothing is remembered and the caller has to measure before
    it can show anything. ``stale`` asks for a background re-measurement while
    the returned value is already on screen.
    """
    key = _cache_key(profile_path)
    if not key:
        return None, False
    with _LOCK:
        record = _load().get(key)
    if record is None:
        return None, True
    age = time.time() - record.get('measured_at', 0.0)
    stale = age > MAX_CACHE_AGE_SECONDS or age < 0 or _root_mtime(key) != record.get('mtime', 0.0)
    return int(record.get('bytes', 0)), stale


def lookup(profile_path):
    """``lookup_bytes`` with the size already formatted for display."""
    size_bytes, stale = lookup_bytes(profile_path)
    if size_bytes is None:
        return None, stale
    return format_size(size_bytes), stale


def store(profile_path, total_bytes) -> str:
    global _DIRTY
    key = _cache_key(profile_path)
    text = format_size(total_bytes)
    if not key:
        return text
    record = {
        'bytes': int(total_bytes or 0),
        'mtime': _root_mtime(key),
        'measured_at': time.time(),
    }
    with _LOCK:
        _load()[key] = record
        _DIRTY = True
    return text


def forget(profile_path) -> None:
    global _DIRTY
    key = _cache_key(profile_path)
    if not key:
        return
    with _LOCK:
        if _load().pop(key, None) is not None:
            _DIRTY = True


def measure(profile_path) -> str:
    """Walk the profile, remember the result and return the display text."""
    key = _cache_key(profile_path)
    if not key:
        return ''
    try:
        total = get_profile_size_bytes(key)
    except OSError:
        return ''
    return store(key, total)


def flush(known_paths=None) -> None:
    """Persist the cache, dropping records for profiles that are gone.

    ``known_paths`` lets the caller prune entries whose WebApp was deleted; a
    profile directory that no longer exists is dropped in any case.
    """
    global _DIRTY
    with _LOCK:
        cache = _load()
        if known_paths is not None:
            wanted = {_cache_key(path) for path in known_paths}
            for key in [key for key in cache if key not in wanted]:
                cache.pop(key, None)
                _DIRTY = True
        for key in [key for key in cache if not Path(key).expanduser().exists()]:
            cache.pop(key, None)
            _DIRTY = True
        if not _DIRTY:
            return
        payload = json.dumps({'version': 1, 'profiles': cache}, ensure_ascii=False)
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp_path = CACHE_PATH.with_name(CACHE_PATH.name + '.tmp')
            tmp_path.write_text(payload, encoding='utf-8')
            tmp_path.replace(CACHE_PATH)
            _DIRTY = False
        except OSError as error:
            LOG.warning('Failed to persist profile size cache: %s', error)

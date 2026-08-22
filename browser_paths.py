"""Foundational helpers for managed browser profiles.

Path/marker/family resolution, profile-root math, safe filesystem deletion and
extension-config lookup shared by browser_settings, browser_extensions and the
browser_profiles lifecycle module. This is the leaf of the browser_* dependency
graph: it imports only stdlib + webapp_constants + i18n, never its siblings.
"""
import json
import shutil
from pathlib import Path
from typing import Any

from i18n import get_app_config
from webapp_constants import CHROMIUM_PROFILE_ROOT, FIREFOX_ROOT


BROWSER_DEFAULT_CHECK_PREFS = {
    'firefox': {'browser.shell.checkDefaultBrowser': False},
    'chrome': {'browser.check_default_browser': False},
    'chromium': {'browser.check_default_browser': False},
}

COLOR_SCHEME_PREF_VALUES = {
    'dark': 0,
    'light': 1,
    'auto': 2,
}


# Values are heterogeneous (str/bool) and merged with user config, so the
# per-extension mapping is deliberately typed loosely.
DEFAULT_FIREFOX_EXTENSIONS: dict[str, dict[str, Any]] = {
    'adblock': {
        'id': 'uBlock0@raymondhill.net',
        'download_url': 'https://addons.mozilla.org/firefox/downloads/latest/uBlock0@raymondhill.net/latest.xpi',
        'marker_file': '.webapp_adblock_extension_id',
    },
    'swipe': {
        'id': 'swipe-gestures@de.cais',
        'bundle_path': 'extension/swipe-gestures.xpi',
        'dev_bundle_path': 'extension/swipe-gestures.xpi',
        'download_url': 'https://addons.mozilla.org/firefox/downloads/file/4744670/swipe_gestures-0.2.2.xpi',
        'allow_unsigned_local_bundle': True,
        'marker_file': '.webapp_secure_swipe_extension_id',
    },
}
MANAGED_PROFILE_MARKER = '.webapp-manager-profile.json'


def normalize_color_scheme(value):
    value = (value or 'auto').strip().lower()
    if value not in COLOR_SCHEME_PREF_VALUES:
        return 'auto'
    return value


def _profile_root_for_family(family):
    family = (family or '').strip().lower()
    if family == 'firefox':
        return FIREFOX_ROOT
    if family in {'chrome', 'chromium'}:
        return CHROMIUM_PROFILE_ROOT / family
    return None


def _managed_profile_marker_path(profile_dir):
    return Path(profile_dir) / MANAGED_PROFILE_MARKER


def _is_legacy_managed_profile_name(profile_dir):
    return Path(profile_dir).name.startswith('webapp_')


def _has_managed_profile_marker(profile_dir, family=''):
    marker_path = _managed_profile_marker_path(profile_dir)
    if not marker_path.exists() or not marker_path.is_file():
        return False
    try:
        data = json.loads(marker_path.read_text(encoding='utf-8'))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if str(data.get('managed_by') or '').strip() != 'webapp-manager':
        return False
    stored_family = str(data.get('family') or '').strip().lower()
    family = (family or '').strip().lower()
    return not family or stored_family == family


def _write_managed_profile_marker(profile_dir, family):
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    marker_path = _managed_profile_marker_path(profile_dir)
    payload = {
        'managed_by': 'webapp-manager',
        'family': str(family or '').strip().lower(),
        'version': 1,
    }
    content = json.dumps(payload, indent=2, sort_keys=True) + '\n'
    current = ''
    if marker_path.exists():
        try:
            current = marker_path.read_text(encoding='utf-8')
        except OSError:
            current = ''
    if current != content:
        marker_path.write_text(content, encoding='utf-8')


def _is_explicitly_managed_profile_dir(path, family):
    if not path:
        return False
    root = _profile_root_for_family(family)
    if root is None:
        return False
    try:
        resolved = Path(path).resolve()
        root = root.resolve()
    except OSError:
        return False
    if not resolved.exists() or not resolved.is_dir() or resolved == root or root not in resolved.parents:
        return False
    return _has_managed_profile_marker(resolved, family) or _is_legacy_managed_profile_name(resolved)

def normalize_default_zoom(value):
    allowed = {'50', '67', '80', '90', '100', '110', '125', '150', '175', '200'}
    normalized = str(value or '100').strip()
    return normalized if normalized in allowed else '100'

def append_unique_csv_arg(exec_parts, prefix, values):
    cleaned = []
    seen = set()
    for value in values:
        if not value or value in seen:
            continue
        cleaned.append(value)
        seen.add(value)
    if cleaned:
        exec_parts.append(prefix + ','.join(cleaned))

def get_firefox_extension_config(name):
    config = get_app_config() or {}
    extensions = config.get('firefox_extensions') or {}
    defaults = dict(DEFAULT_FIREFOX_EXTENSIONS.get(name, {}))
    merged = dict(defaults)
    merged.update(extensions.get(name) or {})

    if name == 'swipe':
        merged['bundle_path'] = str(merged.get('bundle_path') or defaults.get('bundle_path') or '').strip()
        merged['dev_bundle_path'] = ''
        merged['allow_unsigned_local_bundle'] = False
        merged['marker_file'] = defaults.get('marker_file') or merged.get('marker_file')
    return merged

def get_profile_size_bytes(profile_path):
    if not profile_path:
        return 0
    try:
        path = Path(profile_path).resolve()
    except OSError:
        return 0
    if not path.exists():
        return 0
    total = 0
    for candidate in path.rglob('*'):
        if candidate.is_file():
            try:
                total += candidate.stat().st_size
            except OSError:
                pass
    return total

def _remove_path_if_exists(path, logger, kind='cache path'):
    candidate = Path(path)
    if not candidate.exists():
        return False
    try:
        if candidate.is_dir():
            shutil.rmtree(candidate)
        else:
            candidate.unlink()
        return True
    except OSError as error:
        logger.warning('Failed to remove %s %s: %s', kind, candidate, error)
        return False


def _safe_remove_tree(path, allowed_root, logger):
    try:
        resolved = Path(path).resolve()
    except OSError:
        return
    if not resolved.exists():
        return
    if resolved == allowed_root.resolve() or allowed_root.resolve() not in resolved.parents:
        logger.warning('Refusing to delete profile path outside managed root: %s', resolved)
        return
    shutil.rmtree(resolved, ignore_errors=False)
    logger.info('Deleted managed profile directory %s', resolved)

def _path_within(path, root):
    try:
        path = Path(path).resolve()
        root = Path(root).resolve()
    except OSError:
        return False
    return path == root or root in path.parents

def _is_managed_profile_path(path, family):
    if not path:
        return False
    return _is_explicitly_managed_profile_dir(path, family)

def _detect_managed_profile_family(path):
    if not path:
        return None
    try:
        resolved = Path(path).resolve()
    except OSError:
        return None
    if _path_within(resolved, FIREFOX_ROOT):
        return 'firefox'
    if _path_within(resolved, CHROMIUM_PROFILE_ROOT / 'chrome'):
        return 'chrome'
    if _path_within(resolved, CHROMIUM_PROFILE_ROOT / 'chromium'):
        return 'chromium'
    return None

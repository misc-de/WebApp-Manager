"""Managed browser profile lifecycle and settings orchestration.

Profile creation/copy/registration (Firefox profiles.ini), deletion, the
apply_profile_settings orchestrator and browser-command resolution. Composes
the leaf modules browser_paths, browser_settings and browser_extensions and
re-exports their public symbols so existing ``from browser_profiles import X``
call sites keep working.
"""
import os
import secrets
import re
import shutil
from datetime import datetime
from pathlib import Path

from custom_assets import ensure_profile_customizations, inline_asset_text_for_options, linked_assets_for_options
from browser_option_logic import normalize_option_dict, project_options_for_family, semantic_mode_from_options
from input_validation import build_safe_slug, sanitize_desktop_value
from webapp_constants import (
    ADDRESS_KEY,
    APP_MODE_KEY,
    CHROMIUM_PROFILE_ROOT,
    COLOR_SCHEME_KEY,
    DEFAULT_ZOOM_KEY,
    FIREFOX_ROOT,
    ONLY_HTTPS_KEY,
    OPTION_ADBLOCK_KEY,
    OPTION_CLEAR_CACHE_ON_EXIT_KEY,
    OPTION_CLEAR_COOKIES_ON_EXIT_KEY,
    OPTION_DISABLE_AI_KEY,
    OPTION_FORCE_PRIVACY_KEY,
    OPTION_STARTUP_BOOSTER_KEY,
    OPTION_SAFE_GRAPHICS_KEY,
    OPTION_KEEP_IN_BACKGROUND_KEY,
    OPTION_NOTIFICATIONS_KEY,
    OPTION_OPEN_LINKS_IN_TABS_KEY,
    OPTION_PRESERVE_SESSION_KEY,
    OPTION_SWIPE_KEY,
    PROFILE_NAME_KEY,
    PROFILE_PATH_KEY,
    USER_AGENT_VALUE_KEY,
)

# Re-exported leaf helpers. Imported here so the historical browser_profiles
# public API (and the test suite) keep resolving ``browser_profiles.<name>``.
from browser_paths import (
    append_unique_csv_arg,
    get_firefox_extension_config,
    get_profile_size_bytes,
    normalize_color_scheme,
    normalize_default_zoom,
    _detect_managed_profile_family,
    _is_explicitly_managed_profile_dir,
    _profile_root_for_family,
    _safe_remove_tree,
    _write_managed_profile_marker,
)
from browser_settings import (
    ProfileSettings,
    read_profile_settings,
    _clear_chromium_runtime_caches,
    _clear_firefox_runtime_caches,
    _sync_firefox_app_mode_css,
    _write_chromium_preferences,
    _write_firefox_user_js,
)
from browser_extensions import (
    firefox_extension_installed,
    swipe_extension_mode_value,
    _assert_safe_zip_members,
    _invalidate_firefox_extension_state,
    _resolve_bundled_extension_path,
    _scope_swipe_extension_payload,
    _sync_firefox_adblock,
    _sync_firefox_signed_extension,
    _sync_firefox_swipe_extension,
)


def apply_profile_settings(profile_info, options_dict, logger):
    if not profile_info:
        return
    family = profile_info.get('browser_family')
    scoped_options = project_options_for_family(normalize_option_dict(options_dict or {}), family)
    profile_path = profile_info.get('profile_path')
    if family in {'firefox', 'chrome', 'chromium'} and profile_path and not _is_explicitly_managed_profile_dir(profile_path, family):
        logger.warning('Refusing to apply settings to non-managed %s profile %s', family, profile_path)
        return
    clear_cache = scoped_options.get(OPTION_CLEAR_CACHE_ON_EXIT_KEY, '0') == '1'
    clear_cookies = scoped_options.get(OPTION_CLEAR_COOKIES_ON_EXIT_KEY, '0') == '1'
    adblock = scoped_options.get(OPTION_ADBLOCK_KEY, '0') == '1'
    mode_value = semantic_mode_from_options(scoped_options)
    previous_session = scoped_options.get(OPTION_PRESERVE_SESSION_KEY, '0') == '1' and mode_value == 'standard'
    notifications_enabled = scoped_options.get(OPTION_NOTIFICATIONS_KEY, '0') == '1'
    swipe_enabled = scoped_options.get(OPTION_SWIPE_KEY, '0') == '1'
    user_agent_value = (scoped_options.get(USER_AGENT_VALUE_KEY, '') or '').strip()
    only_https = scoped_options.get(ONLY_HTTPS_KEY, '0') == '1'
    keep_in_background = scoped_options.get(OPTION_KEEP_IN_BACKGROUND_KEY, '0') == '1'
    open_links_in_tabs = scoped_options.get(OPTION_OPEN_LINKS_IN_TABS_KEY, '0') == '1'
    app_mode = mode_value in {'app', 'seamless'}
    frameless = mode_value == 'seamless'
    kiosk = mode_value == 'kiosk'
    disable_ai = scoped_options.get(OPTION_DISABLE_AI_KEY, '0') == '1'
    set_privacy = scoped_options.get(OPTION_FORCE_PRIVACY_KEY, '0') == '1'
    startup_booster = scoped_options.get(OPTION_STARTUP_BOOSTER_KEY, '0') == '1'
    safe_graphics = scoped_options.get(OPTION_SAFE_GRAPHICS_KEY, '0') == '1'
    color_scheme = normalize_color_scheme(scoped_options.get(COLOR_SCHEME_KEY, 'auto'))
    default_zoom = normalize_default_zoom(scoped_options.get(DEFAULT_ZOOM_KEY, '100'))
    custom_css_enabled = bool(linked_assets_for_options(options_dict, 'css') or inline_asset_text_for_options(options_dict, 'css'))
    custom_js_enabled = bool(linked_assets_for_options(options_dict, 'javascript') or inline_asset_text_for_options(options_dict, 'javascript'))
    settings = ProfileSettings(
        clear_cache=clear_cache,
        clear_cookies=clear_cookies,
        previous_session=previous_session,
        user_agent_value=user_agent_value,
        only_https=only_https,
        notifications_enabled=notifications_enabled,
        swipe_enabled=swipe_enabled,
        keep_in_background=keep_in_background,
        open_links_in_tabs=open_links_in_tabs,
        app_mode=app_mode,
        native_window_frame=(app_mode and not frameless),
        disable_ai=disable_ai,
        set_privacy=set_privacy,
        color_scheme=color_scheme,
        custom_css_enabled=custom_css_enabled,
        custom_js_enabled=custom_js_enabled,
        startup_booster=startup_booster,
        safe_graphics=safe_graphics,
        default_zoom=default_zoom,
    )
    if family == 'firefox' and profile_path:
        if clear_cache and previous_session:
            _clear_firefox_runtime_caches(profile_path, logger)
        _write_firefox_user_js(profile_path, settings)
        _sync_firefox_app_mode_css(profile_path, app_mode, frameless, logger)
        _sync_firefox_adblock(profile_path, adblock, logger)
        _sync_firefox_swipe_extension(profile_path, swipe_enabled, logger, options_dict=options_dict)
        ensure_profile_customizations(profile_info, options_dict, logger)
        _invalidate_firefox_extension_state(profile_path, logger)
        return
    if family in {'chrome', 'chromium'} and profile_path:
        if clear_cache and previous_session:
            _clear_chromium_runtime_caches(profile_path, logger)
        _write_chromium_preferences(profile_path, settings, logger)
        ensure_profile_customizations(profile_info, options_dict, logger)
        return

def resolve_browser_command(configured_command, logger):
    candidates = [configured_command]
    lower = configured_command.lower()
    if lower == 'chrome':
        candidates = ['google-chrome', 'chromium', 'chromium-browser', 'chrome']
    elif lower == 'chromium':
        candidates = ['chromium', 'chromium-browser', 'google-chrome', 'chrome']
    elif lower == 'firefox':
        candidates = ['firefox', 'firefox-esr']

    for candidate in candidates:
        if shutil.which(candidate):
            return candidate
    logger.warning("No installed browser found for configured command '%s'; using raw value", configured_command)
    return configured_command

def _browser_family(command):
    lower = (command or '').lower()
    if 'firefox' in lower:
        return 'firefox'
    if 'chromium' in lower:
        return 'chromium'
    if 'chrome' in lower:
        return 'chrome'
    return 'generic'

def append_user_agent_argument(exec_parts, engine_command, user_agent_value, logger, entry_id):
    if not user_agent_value:
        return
    browser = engine_command.lower()
    if any(token in browser for token in ['chrome', 'chromium']):
        exec_parts.append(f'--user-agent={user_agent_value}')
        return
    if 'firefox' in browser:
        # Firefox user-agent overrides are applied through the managed profile
        # via general.useragent.override in user.js, not a CLI flag.
        return
    logger.warning("User agent override is not implemented for browser command '%s'", engine_command)

def _ensure_firefox_profiles_ini(logger):
    FIREFOX_ROOT.mkdir(parents=True, exist_ok=True)
    profiles_ini = FIREFOX_ROOT / 'profiles.ini'
    return profiles_ini

def _backup_profiles_ini(profiles_ini, logger):
    if not profiles_ini.exists():
        return
    try:
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        backup_path = profiles_ini.with_name(f'profiles.ini.webapp.{timestamp}.bak')
        shutil.copy2(profiles_ini, backup_path)
        backups = sorted(
            profiles_ini.parent.glob('profiles.ini.webapp.*.bak'),
            key=lambda candidate: candidate.stat().st_mtime,
            reverse=True,
        )
        for stale in backups[10:]:
            try:
                stale.unlink()
            except OSError as prune_error:
                logger.warning('Failed to prune Firefox profiles.ini backup %s: %s', stale, prune_error)
        logger.info('Created Firefox profiles.ini backup %s', backup_path)
    except OSError as error:
        logger.warning('Failed to create Firefox profiles.ini backup %s: %s', profiles_ini, error)

def _parse_profiles_ini_sections(raw_text):
    if not raw_text:
        return []
    lines = raw_text.splitlines(keepends=True)
    sections = []
    current_name = None
    current_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            if current_name is not None:
                sections.append((current_name, current_lines))
            current_name = stripped[1:-1]
            current_lines = [line]
        else:
            if current_name is None:
                current_name = ''
                current_lines = []
            current_lines.append(line)
    if current_name is not None:
        sections.append((current_name, current_lines))
    return sections

def _parse_ini_key_values(section_lines):
    values = {}
    for line in section_lines[1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith(';') or stripped.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip()
    return values

def _write_profiles_ini_sections(profiles_ini, sections, logger):
    content = ''.join(''.join(lines) for _, lines in sections)
    if content and not content.endswith('\n'):
        content += '\n'
    current = ''
    if profiles_ini.exists():
        try:
            current = profiles_ini.read_text(encoding='utf-8')
        except OSError as error:
            logger.warning('Failed to compare Firefox profiles.ini %s before write: %s', profiles_ini, error)
    if current == content:
        logger.debug('Skipping Firefox profiles.ini write because content is unchanged: %s', profiles_ini)
        return
    _backup_profiles_ini(profiles_ini, logger)
    temp_path = profiles_ini.with_suffix('.tmp')
    temp_path.write_text(content, encoding='utf-8')
    temp_path.replace(profiles_ini)

def _upsert_firefox_profile(profile_name, profile_dir, logger):
    profiles_ini = _ensure_firefox_profiles_ini(logger)
    relative_path = os.path.relpath(profile_dir, FIREFOX_ROOT)
    try:
        raw_text = profiles_ini.read_text(encoding='utf-8') if profiles_ini.exists() else ''
    except OSError as error:
        logger.error('Failed to read Firefox profiles.ini %s: %s', profiles_ini, error)
        return

    sections = _parse_profiles_ini_sections(raw_text)
    max_index = -1
    target_index = None
    general_index = None

    for idx, (section_name, section_lines) in enumerate(sections):
        if section_name == 'General':
            general_index = idx
        if not section_name.startswith('Profile'):
            continue
        suffix = section_name[len('Profile'):]
        if suffix.isdigit():
            max_index = max(max_index, int(suffix))
        values = _parse_ini_key_values(section_lines)
        if values.get('Name', '') == profile_name or values.get('Path', '') == relative_path:
            target_index = idx

    if target_index is not None:
        section_name = sections[target_index][0]
    else:
        section_name = f'Profile{max_index + 1}'

    block_lines = [
        f'[{section_name}]\n',
        f'Name={profile_name}\n',
        'IsRelative=1\n',
        f'Path={relative_path}\n',
        'Default=0\n',
        '\n',
    ]

    if target_index is not None:
        sections[target_index] = (section_name, block_lines)
    else:
        if sections and sections[-1][1] and sections[-1][1][-1].strip():
            sections[-1][1].append('\n')
        sections.append((section_name, block_lines))

    if general_index is None:
        if sections and sections[-1][1] and sections[-1][1][-1].strip():
            sections[-1][1].append('\n')
        sections.append(('General', ['[General]\n', 'StartWithLastProfile=1\n', '\n']))
    else:
        gname, glines = sections[general_index]
        values = _parse_ini_key_values(glines)
        if 'StartWithLastProfile' not in values:
            insert_at = len(glines)
            if glines and not glines[-1].strip():
                insert_at -= 1
            glines.insert(insert_at, 'StartWithLastProfile=1\n')
            if glines and glines[-1].strip():
                glines.append('\n')
            sections[general_index] = (gname, glines)

    _write_profiles_ini_sections(profiles_ini, sections, logger)

def _remove_firefox_profile_registration(profile_name, profile_dir, logger):
    profiles_ini = _ensure_firefox_profiles_ini(logger)
    if not profiles_ini.exists():
        return
    relative_path = os.path.relpath(profile_dir, FIREFOX_ROOT)
    try:
        raw_text = profiles_ini.read_text(encoding='utf-8')
    except OSError as error:
        logger.error('Failed to read Firefox profiles.ini %s: %s', profiles_ini, error)
        return

    sections = _parse_profiles_ini_sections(raw_text)
    filtered = []
    changed = False
    for section_name, section_lines in sections:
        if not section_name.startswith('Profile'):
            filtered.append((section_name, section_lines))
            continue
        values = _parse_ini_key_values(section_lines)
        if values.get('Name', '') == profile_name or values.get('Path', '') == relative_path:
            changed = True
            continue
        filtered.append((section_name, section_lines))

    if changed:
        _write_profiles_ini_sections(profiles_ini, filtered, logger)

def _generate_profile_id():
    return f'webapp_{secrets.token_hex(6)}'

def _sanitize_profile_id(value):
    value = (value or '').strip().lower().replace(' ', '_')
    value = re.sub(r'[^a-z0-9_-]+', '_', value)
    value = re.sub(r'_+', '_', value).strip('_')
    return value or _generate_profile_id()

def _copy_profile_contents(source_dir, target_dir, logger):
    source_dir = Path(source_dir)
    target_dir = Path(target_dir)
    if not source_dir.exists() or source_dir.resolve() == target_dir.resolve():
        return
    target_dir.mkdir(parents=True, exist_ok=True)
    for child in source_dir.iterdir():
        destination = target_dir / child.name
        try:
            if child.is_dir():
                shutil.copytree(child, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(child, destination)
        except OSError as error:
            logger.warning('Failed to copy profile content from %s to %s: %s', child, destination, error)


def _firefox_profile_markers(profile_dir):
    return [
        profile_dir / 'prefs.js',
        profile_dir / 'user.js',
        profile_dir / 'places.sqlite',
        profile_dir / 'cookies.sqlite',
        profile_dir / 'extensions.json',
        profile_dir / 'compatibility.ini',
        profile_dir / 'sessionstore.jsonlz4',
    ]


def _is_valid_firefox_profile_dir(profile_dir):
    profile_dir = Path(profile_dir)
    if not profile_dir.exists() or not profile_dir.is_dir():
        return False
    return any(marker.exists() for marker in _firefox_profile_markers(profile_dir))


def _is_valid_chromium_user_data_dir(profile_dir):
    profile_dir = Path(profile_dir)
    if not profile_dir.exists() or not profile_dir.is_dir():
        return False
    local_state = profile_dir / 'Local State'
    if not local_state.exists() or not local_state.is_file():
        return False
    profile_candidates = []
    default_dir = profile_dir / 'Default'
    if default_dir.is_dir():
        profile_candidates.append(default_dir)
    try:
        profile_candidates.extend(candidate for candidate in profile_dir.iterdir() if candidate.is_dir() and candidate.name.startswith('Profile '))
    except OSError:
        return False
    for candidate in profile_candidates:
        if (candidate / 'Preferences').is_file():
            return True
    return False


def inspect_profile_copy_source(profile_path, browser_family, logger=None):
    if not profile_path:
        return {'valid': False, 'profile_path': '', 'profile_name': ''}
    try:
        resolved = Path(profile_path).expanduser().resolve()
    except OSError:
        return {'valid': False, 'profile_path': '', 'profile_name': ''}
    if not resolved.exists() or not resolved.is_dir():
        return {'valid': False, 'profile_path': '', 'profile_name': ''}

    family = (browser_family or '').strip().lower()
    valid = False
    if family == 'firefox':
        valid = _is_valid_firefox_profile_dir(resolved)
    elif family in {'chrome', 'chromium'}:
        valid = _is_valid_chromium_user_data_dir(resolved)

    if not valid:
        if logger is not None:
            logger.warning('Refusing to import browser profile from invalid %s directory %s', family or 'browser', resolved)
        return {'valid': False, 'profile_path': '', 'profile_name': ''}

    return {
        'valid': True,
        'profile_path': str(resolved),
        'profile_name': resolved.name,
    }


def rename_unused_managed_profile_directories(active_profile_paths, logger):
    active_paths = set()
    for raw_path in active_profile_paths or []:
        if not raw_path:
            continue
        try:
            active_paths.add(Path(raw_path).expanduser().resolve())
        except OSError:
            continue

    renamed = []

    def next_unused_path(profile_dir):
        base_name = profile_dir.name
        suffix = '_unused'
        candidate = profile_dir.with_name(f'{base_name}{suffix}')
        counter = 2
        while candidate.exists():
            candidate = profile_dir.with_name(f'{base_name}{suffix}_{counter}')
            counter += 1
        return candidate

    def should_skip(profile_dir, family):
        if not _is_explicitly_managed_profile_dir(profile_dir, family):
            return True
        if '_unused' in profile_dir.name:
            return True
        return profile_dir in active_paths

    roots = [
        ('firefox', FIREFOX_ROOT),
        ('chrome', CHROMIUM_PROFILE_ROOT / 'chrome'),
        ('chromium', CHROMIUM_PROFILE_ROOT / 'chromium'),
    ]
    for family, root in roots:
        if not root.exists() or not root.is_dir():
            continue
        try:
            candidates = sorted(candidate for candidate in root.iterdir() if candidate.is_dir())
        except OSError:
            continue
        for profile_dir in candidates:
            try:
                resolved = profile_dir.resolve()
            except OSError:
                continue
            if should_skip(resolved, family):
                continue
            target = next_unused_path(resolved)
            try:
                if family == 'firefox':
                    _remove_firefox_profile_registration(resolved.name, resolved, logger)
                resolved.rename(target)
                logger.info('Renamed unused managed %s profile %s -> %s', family, resolved, target)
                renamed.append({'family': family, 'old_path': str(resolved), 'new_path': str(target)})
            except OSError as error:
                logger.warning('Failed to rename unused managed %s profile %s: %s', family, resolved, error)
    return renamed


def ensure_browser_profile(title, configured_command, logger, stored_profile_name='', stored_profile_path=''):
    slug = build_safe_slug(title)
    if not slug:
        return None
    family = _browser_family(configured_command)
    stored_path = None
    if stored_profile_path:
        try:
            stored_path = Path(stored_profile_path).resolve()
        except OSError:
            stored_path = None
    managed_existing = bool(stored_path and _is_explicitly_managed_profile_dir(stored_path, family))
    stored_family = _detect_managed_profile_family(stored_path) if stored_path else None
    allow_profile_copy = False
    source_profile = {'valid': False, 'profile_path': '', 'profile_name': ''}
    if stored_path and not managed_existing and stored_path.exists() and (stored_family is None or stored_family == family):
        source_profile = inspect_profile_copy_source(stored_path, family, logger)
        allow_profile_copy = bool(source_profile.get('valid'))
    profile_name = _sanitize_profile_id(stored_profile_name) if (stored_profile_name and managed_existing) else _generate_profile_id()
    source_profile_path = source_profile.get('profile_path', '') if allow_profile_copy else ''
    source_profile_name = source_profile.get('profile_name', '') if source_profile_path else ''
    profile_migrated = False
    if family == 'firefox':
        FIREFOX_ROOT.mkdir(parents=True, exist_ok=True)
        profile_dir = FIREFOX_ROOT / profile_name
        if managed_existing and stored_path and stored_path.name == profile_name:
            profile_dir = stored_path
        profile_dir.mkdir(parents=True, exist_ok=True)
        if source_profile_path:
            _copy_profile_contents(source_profile_path, profile_dir, logger)
            profile_migrated = True
        _write_managed_profile_marker(profile_dir, family)
        _upsert_firefox_profile(profile_name, profile_dir, logger)
        return {
            'browser_family': family,
            'managed_profile': True,
            'profile_name': profile_name,
            'profile_path': str(profile_dir),
            'exec_args': ['-profile', str(profile_dir)],
            'profile_migrated': profile_migrated,
        }
    if family in {'chrome', 'chromium'}:
        family_root = CHROMIUM_PROFILE_ROOT / family
        profile_dir = family_root / profile_name
        if managed_existing and stored_path and stored_path.name == profile_name:
            profile_dir = stored_path
        profile_dir.mkdir(parents=True, exist_ok=True)
        if source_profile_path:
            _copy_profile_contents(source_profile_path, profile_dir, logger)
            profile_migrated = True
        _write_managed_profile_marker(profile_dir, family)
        return {
            'browser_family': family,
            'managed_profile': True,
            'profile_name': profile_name,
            'profile_path': str(profile_dir),
            'exec_args': [f'--user-data-dir={profile_dir}'],
            'profile_migrated': profile_migrated,
        }
    return {
        'browser_family': family,
        'managed_profile': False,
        'profile_name': profile_name,
        'profile_path': '',
        'exec_args': [],
        'profile_migrated': profile_migrated,
    }

def delete_managed_browser_profiles(title, logger, stored_profile_path='', stored_profile_name='', keep_profile_path=''):
    explicit_paths = set()
    if stored_profile_path:
        try:
            explicit_paths.add(Path(stored_profile_path).resolve())
        except OSError:
            pass
    keep_resolved = None
    if keep_profile_path:
        try:
            keep_resolved = Path(keep_profile_path).resolve()
        except OSError:
            keep_resolved = None
    for profile_dir in explicit_paths:
        if keep_resolved and profile_dir == keep_resolved:
            continue
        family = _detect_managed_profile_family(profile_dir)
        if family not in {'firefox', 'chrome', 'chromium'}:
            logger.warning('Refusing to delete profile with unknown family: %s', profile_dir)
            continue
        if not _is_explicitly_managed_profile_dir(profile_dir, family):
            logger.warning('Refusing to delete non-managed %s profile path: %s', family, profile_dir)
            continue
        profile_name = stored_profile_name or profile_dir.name
        if family == 'firefox':
            _remove_firefox_profile_registration(profile_name, profile_dir, logger)
            if profile_dir.exists():
                try:
                    _safe_remove_tree(profile_dir, _profile_root_for_family(family), logger)
                except OSError as error:
                    logger.error('Failed to delete Firefox profile %s: %s', profile_dir, error)
        else:
            if profile_dir.exists():
                try:
                    _safe_remove_tree(profile_dir, _profile_root_for_family(family), logger)
                except OSError as error:
                    logger.error('Failed to delete browser profile %s: %s', profile_dir, error)

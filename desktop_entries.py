import configparser
import shlex
from pathlib import Path
from typing import Sequence

from i18n import t
from input_validation import (
    build_safe_slug,
    is_structurally_valid_url,
    is_valid_url,
    normalize_address,
    sanitize_desktop_value,
)
from browser_option_logic import apply_semantic_mode, normalize_option_dict, project_options_for_family, semantic_mode_from_options
from browser_profiles import (
    append_unique_csv_arg,
    append_user_agent_argument,
    apply_profile_settings,
    delete_managed_browser_profiles,
    ensure_browser_profile,
    normalize_color_scheme,
    resolve_browser_command,
)
from custom_assets import chromium_runtime_extension_args
from app_identity import APP_ICON_NAME
from icon_pipeline import (
    _allowed_managed_icon_stems,
    _is_safe_managed_icon_path,
    ensure_applications_dir,
    get_managed_icon_name,
    get_managed_theme_icon_path,
    normalize_icon_to_png,
)
from webapp_constants import (
    ADDRESS_KEY,
    APP_MODE_KEY,
    APPLICATIONS_DIR,
    CHROMIUM_PROFILE_ROOT,
    COLOR_SCHEME_KEY,
    DESKTOP_NAME_SOURCE_KEY,
    FIREFOX_ROOT,
    ICON_PATH_KEY,
    ICON_THEME_APPS_DIR,
    ONLY_HTTPS_KEY,
    OPTION_DISABLE_AI_KEY,
    OPTION_FORCE_PRIVACY_KEY,
    OPTION_PRESERVE_SESSION_KEY,
    PROFILE_NAME_KEY,
    PROFILE_PATH_KEY,
    USER_AGENT_VALUE_KEY,
    OPTION_SWIPE_KEY,
)

MANAGED_BY_VALUE = t('managed_by')


def _browser_family_for_command(command: str) -> str:
    lowered = (command or '').strip().lower()
    if 'firefox' in lowered:
        return 'firefox'
    if 'chromium' in lowered:
        return 'chromium'
    if 'chrome' in lowered:
        return 'chrome'
    return 'generic'



def _chromium_launch_args_for_mode(mode_value: str, address: str, previous_session_enabled: bool) -> list[str]:
    mode = (mode_value or 'standard').strip().lower()
    if mode == 'kiosk':
        return ['--kiosk', address]
    if mode in {'app', 'seamless'}:
        return [f'--app={address}']
    if previous_session_enabled:
        return []
    return ['--new-window', address]


def _firefox_launch_args_for_mode(mode_value: str, address: str, previous_session_enabled: bool) -> list[str]:
    mode = (mode_value or 'standard').strip().lower()
    if mode == 'kiosk':
        return ['--kiosk', address]
    if previous_session_enabled:
        return []
    return [address]


def _generic_launch_args_for_mode(address: str, previous_session_enabled: bool) -> list[str]:
    if previous_session_enabled:
        return []
    return [address]


def _stored_profile_info(configured_command: str, stored_profile_name: str = '', stored_profile_path: str = ''):
    family = _browser_family_for_command(configured_command)
    profile_path = ''
    profile_name = (stored_profile_name or '').strip()
    if stored_profile_path:
        try:
            profile_path = str(Path(stored_profile_path).expanduser().resolve())
        except OSError:
            profile_path = str(Path(stored_profile_path).expanduser())
        if not profile_name and profile_path:
            profile_name = Path(profile_path).name
    elif profile_name:
        safe_name = Path(profile_name).name
        if family == 'firefox':
            profile_path = str(FIREFOX_ROOT / safe_name)
        elif family in {'chrome', 'chromium'}:
            profile_path = str((CHROMIUM_PROFILE_ROOT / family) / safe_name)
        profile_name = safe_name

    exec_args = []
    if profile_path:
        if family == 'firefox':
            exec_args = ['-profile', profile_path]
        elif family in {'chrome', 'chromium'}:
            exec_args = [f'--user-data-dir={profile_path}']

    return {
        'browser_family': family,
        'profile_name': profile_name,
        'profile_path': profile_path,
        'exec_args': exec_args,
        'profile_migrated': False,
    }


def _selected_engine(options_dict, engines_list):
    configured_command = 'firefox'
    engine_id = options_dict.get('EngineID')
    try:
        engine_id = int(engine_id) if engine_id not in (None, '') else None
    except ValueError:
        engine_id = None
    selected_engine = None
    for engine in engines_list:
        if engine.get('id') == engine_id:
            selected_engine = engine
            configured_command = engine['command']
            break
    return selected_engine, configured_command


def _window_identity_for_entry(entry) -> str:
    title = sanitize_desktop_value(getattr(entry, 'title', '') or '')[:200]
    safe_slug = build_safe_slug(title)
    if safe_slug:
        return safe_slug
    entry_id = getattr(entry, 'id', None)
    if entry_id not in (None, ''):
        return f'webapp-entry-{entry_id}'
    return 'webapp'


def desktop_name_source(options_dict) -> str:
    value = str((options_dict or {}).get(DESKTOP_NAME_SOURCE_KEY, 'title') or 'title').strip().lower()
    if value not in {'title', 'description'}:
        return 'title'
    return value


def desktop_display_name(entry, options_dict) -> str:
    title = sanitize_desktop_value(getattr(entry, 'title', '') or '')[:200]
    description = sanitize_desktop_value(getattr(entry, 'description', '') or '')[:200]
    if desktop_name_source(options_dict) == 'description' and description:
        return description
    return title


def build_launch_command(entry, options_dict, engines_list, logger, prepare_profile=False):
    title = (getattr(entry, 'title', '') or '').strip()
    raw_address = (options_dict.get(ADDRESS_KEY, '') or '').strip()
    address = normalize_address(raw_address, options_dict.get(ONLY_HTTPS_KEY, '0') == '1')
    if not title or not is_valid_url(address or raw_address, check_origin=False):
        logger.warning('Refusing to build launch command for entry %s because title or URL is invalid', getattr(entry, 'id', 'unknown'))
        return None

    selected_engine, configured_command = _selected_engine(options_dict, engines_list)
    if selected_engine is None:
        logger.warning('Refusing to build launch command for entry %s because engine selection is invalid', getattr(entry, 'id', 'unknown'))
        return None

    engine_command = resolve_browser_command(configured_command, logger)
    scoped_options = project_options_for_family(options_dict or {}, 'firefox' if 'firefox' in configured_command.lower() else ('chromium' if 'chromium' in configured_command.lower() else ('chrome' if 'chrome' in configured_command.lower() else 'generic')))
    merged_options = normalize_option_dict(options_dict or {})
    merged_options.update(scoped_options)

    if prepare_profile:
        profile_info = ensure_browser_profile(
            title,
            configured_command,
            logger,
            stored_profile_name=options_dict.get(PROFILE_NAME_KEY, ''),
            stored_profile_path=options_dict.get(PROFILE_PATH_KEY, ''),
        )
        apply_profile_settings(profile_info, merged_options, logger)
    else:
        profile_info = _stored_profile_info(
            configured_command,
            stored_profile_name=options_dict.get(PROFILE_NAME_KEY, ''),
            stored_profile_path=options_dict.get(PROFILE_PATH_KEY, ''),
        )

    exec_parts = [engine_command]
    mode_value = semantic_mode_from_options(merged_options)
    kiosk_mode = mode_value == 'kiosk'
    app_mode = mode_value in {'app', 'seamless'}
    frameless = mode_value == 'seamless'
    disable_ai = merged_options.get(OPTION_DISABLE_AI_KEY, '0') == '1'
    color_scheme = normalize_color_scheme(merged_options.get(COLOR_SCHEME_KEY, 'auto'))
    browser_family = profile_info.get('browser_family') if profile_info else ''
    window_identity = _window_identity_for_entry(entry)
    if browser_family in {'chrome', 'chromium'}:
        exec_parts.append(f'--class={window_identity}')
    if profile_info:
        exec_parts.extend(profile_info['exec_args'])
        exec_parts.extend(chromium_runtime_extension_args(profile_info, merged_options))
    chrome_feature_flags = []
    chrome_disable_feature_flags = []
    chrome_blink_settings = []
    is_chromium_family = bool(profile_info and profile_info.get('browser_family') in {'chrome', 'chromium'})
    if merged_options.get(OPTION_SWIPE_KEY, '0') == '1' and is_chromium_family:
        chrome_feature_flags.extend(['TouchpadOverscrollHistoryNavigation', 'OverscrollHistoryNavigation'])
    if disable_ai and is_chromium_family:
        chrome_disable_feature_flags.extend(['OptimizationGuideModelDownloading', 'OptimizationHintsFetching', 'Compose', 'AutofillAi', 'HistorySearch', 'TabOrganization', 'Glic'])
    if is_chromium_family:
        if color_scheme == 'dark':
            exec_parts.append('--force-dark-mode')
            chrome_feature_flags.append('WebUIDarkMode')
            chrome_blink_settings.extend(['preferredColorScheme=0', 'forceDarkModeEnabled=true'])
        elif color_scheme == 'light':
            chrome_disable_feature_flags.extend(['WebUIDarkMode', 'AutoWebContentsDarkMode'])
            chrome_blink_settings.append('preferredColorScheme=1')
    append_unique_csv_arg(exec_parts, '--enable-features=', chrome_feature_flags)
    append_unique_csv_arg(exec_parts, '--disable-features=', chrome_disable_feature_flags)
    append_unique_csv_arg(exec_parts, '--blink-settings=', chrome_blink_settings)
    append_user_agent_argument(exec_parts, engine_command, merged_options.get(USER_AGENT_VALUE_KEY, '').strip(), logger, getattr(entry, 'id', None))
    previous_session_enabled = (merged_options.get(OPTION_PRESERVE_SESSION_KEY, '0') == '1') and mode_value == 'standard'
    if browser_family in {'chrome', 'chromium'}:
        exec_parts.extend(_chromium_launch_args_for_mode(mode_value, address, previous_session_enabled))
    elif browser_family == 'firefox':
        exec_parts.extend(_firefox_launch_args_for_mode(mode_value, address, previous_session_enabled))
    else:
        exec_parts.extend(_generic_launch_args_for_mode(address, previous_session_enabled))

    return {
        'argv': exec_parts,
        'normalized_address': address,
        'profile_info': profile_info,
        'engine_command': engine_command,
        'window_identity': window_identity,
    }


def _looks_like_filesystem_path(value: str) -> bool:
    candidate = (value or '').strip()
    if not candidate:
        return False
    if candidate.startswith(('~', '/', './', '../')):
        return True
    return '/' in candidate or '\\' in candidate


def _resolve_firefox_profile_reference(value: str) -> str:
    candidate = (value or '').strip()
    if not candidate:
        return ''
    expanded = Path(candidate).expanduser()
    if _looks_like_filesystem_path(candidate):
        try:
            return str(expanded.resolve())
        except OSError:
            return str(expanded)
    direct = FIREFOX_ROOT / candidate
    if direct.exists():
        try:
            return str(direct.resolve())
        except OSError:
            return str(direct)
    profiles_ini = FIREFOX_ROOT / 'profiles.ini'
    if not profiles_ini.exists():
        return ''
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        with open(profiles_ini, 'r', encoding='utf-8') as handle:
            parser.read_file(handle)
    except OSError:
        return ''
    for section_name in parser.sections():
        if not section_name.startswith('Profile'):
            continue
        section = parser[section_name]
        name = (section.get('Name') or '').strip()
        path_value = (section.get('Path') or '').strip()
        if candidate not in {name, path_value, Path(path_value).name if path_value else ''}:
            continue
        is_relative = (section.get('IsRelative') or '1').strip() != '0'
        base_path = FIREFOX_ROOT if is_relative else Path('/')
        resolved = (base_path / path_value).expanduser() if path_value else (FIREFOX_ROOT / candidate)
        try:
            return str(resolved.resolve())
        except OSError:
            return str(resolved)
    return ''


def _extract_profile_path_from_exec_tokens(tokens: Sequence[str]) -> str:
    if not tokens:
        return ''
    for index, token in enumerate(tokens):
        if token in {'--profile', '-profile', '--user-data-dir'}:
            if index + 1 < len(tokens):
                candidate = (tokens[index + 1] or '').strip()
                if candidate:
                    return candidate
            continue
        if token == '-P':
            if index + 1 < len(tokens):
                candidate = (tokens[index + 1] or '').strip()
                if candidate:
                    resolved = _resolve_firefox_profile_reference(candidate)
                    return resolved or candidate
            continue
        for prefix in ('--profile=', '-profile=', '--user-data-dir='):
            if token.startswith(prefix):
                candidate = token.split('=', 1)[1].strip()
                if candidate:
                    return candidate
        if token.startswith('-P='):
            candidate = token.split('=', 1)[1].strip()
            if candidate:
                resolved = _resolve_firefox_profile_reference(candidate)
                return resolved or candidate
    return ''


def exportable_entry(entry, options_dict):
    title = sanitize_desktop_value(entry.title)[:200]
    raw_address = (options_dict.get(ADDRESS_KEY, '') or '').strip()
    address = normalize_address(raw_address, options_dict.get(ONLY_HTTPS_KEY, '0') == '1')
    return bool(title) and is_valid_url(address or raw_address, check_origin=False)

def get_expected_desktop_path(title):
    safe_slug = build_safe_slug(title)
    if not safe_slug:
        return None
    return APPLICATIONS_DIR / f'{safe_slug}.desktop'

def infer_engine_id_from_command(command, engines_list):
    command = (command or '').lower()
    if not command:
        return None
    for engine in engines_list:
        engine_command = (engine.get('command') or '').lower()
        if engine_command and (command == engine_command or engine_command in command or command in engine_command):
            return engine['id']
    if 'chrome' in command or 'chromium' in command:
        for engine in engines_list:
            if 'chrome' in (engine.get('command') or '').lower() or 'chromium' in (engine.get('command') or '').lower():
                return engine['id']
    if 'firefox' in command:
        for engine in engines_list:
            if 'firefox' in (engine.get('command') or '').lower():
                return engine['id']
    return None

def parse_desktop_file(path, engines_list):
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        with open(path, 'r', encoding='utf-8') as file_handle:
            parser.read_file(file_handle)
    except OSError:
        return None
    if 'Desktop Entry' not in parser:
        return None

    section = parser['Desktop Entry']
    managed_by = (section.get('ManagedBy') or '').strip()
    if managed_by != MANAGED_BY_VALUE:
        return None

    exec_cmd = section.get('Exec', '')
    command = ''
    address = ''
    user_agent_value = ''
    profile_path = ''
    profile_name = ''
    try:
        tokens = shlex.split(exec_cmd)
    except ValueError:
        tokens = []

    derived_options = {}
    explicit_mode = (section.get('X-WebApp-Mode', '') or '').strip().lower()
    if explicit_mode in {'standard', 'kiosk', 'app', 'seamless'}:
        derived_options.update({k: v for k, v in apply_semantic_mode({}, explicit_mode).items() if k in {'Kiosk', APP_MODE_KEY, 'Frameless'}})
    engine_id = None
    user_agent_name = ''
    user_agent_value = ''
    if tokens:
        command = tokens[0]
        for token in reversed(tokens):
            if is_structurally_valid_url(token):
                address = token
                break
        profile_path = _extract_profile_path_from_exec_tokens(tokens)
        for index, token in enumerate(tokens):
            if token in {'--kiosk', '-kiosk'}:
                derived_options['Kiosk'] = '1'
                derived_options.setdefault(APP_MODE_KEY, '0')
                derived_options.setdefault('Frameless', '0')
            elif token.startswith('--app='):
                derived_options[APP_MODE_KEY] = '1'
                derived_options.setdefault('Kiosk', '0')
                derived_options.setdefault('Frameless', '0')
                candidate = token.split('=', 1)[1]
                if candidate and is_structurally_valid_url(candidate):
                    address = candidate
            elif token == '--start-fullscreen' and derived_options.get(APP_MODE_KEY) == '1':
                derived_options['Frameless'] = '1'
                derived_options.setdefault('Kiosk', '0')
            elif token.startswith('--user-agent='):
                user_agent_value = token.split('=', 1)[1]
        if profile_path:
            profile_name = Path(profile_path).name

    try:
        entry_id_raw = section.get('EntryId', section.get('EntryID', ''))
        entry_id = int(entry_id_raw)
    except ValueError:
        entry_id = None

    title = sanitize_desktop_value(section.get('X-WebApp-Title', section.get('Name', '')))[:200]
    desktop_source = str(section.get('X-WebApp-DesktopNameSource', 'title') or 'title').strip().lower()
    if desktop_source not in {'title', 'description'}:
        desktop_source = 'title'

    if engine_id is None:
        engine_id = infer_engine_id_from_command(command, engines_list)
    engine_name = ''
    if engine_id is not None:
        for engine in engines_list:
            if engine.get('id') == engine_id:
                engine_name = engine.get('name', '')
                break

    icon_value = (section.get('Icon') or '').strip()
    icon_path = ''
    icon_name = ''
    if icon_value:
        if '/' in icon_value or '\\' in icon_value or Path(icon_value).expanduser().suffix:
            icon_path = icon_value
        else:
            icon_name = icon_value

    return {
        'path': Path(path),
        'entry_id': entry_id,
        'title': title,
        'address': address,
        'active': section.get('NoDisplay', 'false').lower() != 'true',
        'engine_id': engine_id,
        'engine_name': engine_name,
        'user_agent_name': user_agent_name,
        'user_agent_value': user_agent_value,
        'icon_path': icon_path,
        'icon_name': icon_name,
        'command': command,
        'profile_name': profile_name,
        'profile_path': profile_path,
        'options': {**derived_options, DESKTOP_NAME_SOURCE_KEY: desktop_source},
    }

def is_managed_desktop_file(path, engines_list=None):
    if not Path(path).exists():
        return False
    return parse_desktop_file(path, engines_list or []) is not None

def list_managed_desktop_files(engines_list):
    if not APPLICATIONS_DIR.exists():
        return []
    results = []
    for path in sorted(APPLICATIONS_DIR.glob('*.desktop')):
        entry = parse_desktop_file(path, engines_list)
        if entry is not None:
            results.append(entry)
    return results

def delete_managed_entry_artifacts(entry_id, title, engines_list, logger, keep_path=None, keep_icon_path=None, keep_icon_name='', delete_profiles=False, stored_profile_path='', stored_profile_name='', keep_profile_path=''):
    keep_path = Path(keep_path).resolve() if keep_path else None
    keep_icon_path = Path(keep_icon_path).resolve() if keep_icon_path else None
    keep_icon_name = (keep_icon_name or '').strip().lower()
    title = (title or '').strip()
    for desktop_data in list_managed_desktop_files(engines_list):
        same_entry = desktop_data.get('entry_id') == entry_id
        same_title = title and desktop_data.get('title') == title
        if not same_entry and not same_title:
            continue
        desktop_path = desktop_data['path'].resolve()
        if keep_path and desktop_path == keep_path:
            continue
        try:
            desktop_path.unlink(missing_ok=True)
            logger.info('Deleted managed desktop file %s', desktop_path)
        except OSError as error:
            logger.error('Failed to delete managed desktop file %s: %s', desktop_path, error)

        icon_path = (desktop_data.get('icon_path') or '').strip()
        if icon_path and ('/' in icon_path or '\\' in icon_path) and _is_safe_managed_icon_path(icon_path, entry_id, title):
            icon_resolved = Path(icon_path).resolve()
            if not (keep_icon_path and icon_resolved == keep_icon_path):
                try:
                    icon_resolved.unlink(missing_ok=True)
                    logger.info('Deleted managed icon file %s', icon_resolved)
                except OSError as error:
                    logger.error('Failed to delete managed icon file %s: %s', icon_resolved, error)

        allowed_stems = _allowed_managed_icon_stems(entry_id, title)
        if ICON_THEME_APPS_DIR.exists():
            for candidate in ICON_THEME_APPS_DIR.iterdir():
                if not candidate.is_file() or candidate.stem.lower() not in allowed_stems:
                    continue
                if keep_icon_path and candidate.resolve() == keep_icon_path:
                    continue
                if keep_icon_name and candidate.stem.lower() == keep_icon_name:
                    continue
                try:
                    candidate.unlink(missing_ok=True)
                    logger.info('Deleted managed theme icon %s', candidate)
                except OSError as error:
                    logger.error('Failed to delete managed theme icon %s: %s', candidate, error)
    if delete_profiles:
        delete_managed_browser_profiles(title, logger, stored_profile_path=stored_profile_path, stored_profile_name=stored_profile_name, keep_profile_path=keep_profile_path)

def _guard_target_path(target_path, engines_list, logger):
    if target_path.exists() and not is_managed_desktop_file(target_path, engines_list):
        logger.warning('Refusing to overwrite non-managed desktop file %s', target_path)
        return False
    return True

def export_desktop_file(entry, options_dict, engines_list, logger):
    ensure_applications_dir()
    title = (entry.title or '').strip()
    display_name = desktop_display_name(entry, options_dict)
    display_source = desktop_name_source(options_dict)
    raw_address = (options_dict.get(ADDRESS_KEY, '') or '').strip()
    address = normalize_address(raw_address, options_dict.get(ONLY_HTTPS_KEY, '0') == '1')
    previous_profile_name = options_dict.get(PROFILE_NAME_KEY, '')
    previous_profile_path = options_dict.get(PROFILE_PATH_KEY, '')
    if not title or not is_valid_url(address or raw_address, check_origin=False):
        logger.info('Skipping desktop export for entry %s because title or URL is invalid', entry.id)
        delete_managed_entry_artifacts(entry.id, title, engines_list, logger, delete_profiles=True, stored_profile_path=previous_profile_path, stored_profile_name=previous_profile_name)
        return None

    target_path = get_expected_desktop_path(title)
    if target_path is None:
        logger.info('Skipping desktop export for entry %s because the safe slug is empty', entry.id)
        delete_managed_entry_artifacts(entry.id, title, engines_list, logger, delete_profiles=True, stored_profile_path=previous_profile_path, stored_profile_name=previous_profile_name)
        return None
    if not _guard_target_path(target_path, engines_list, logger):
        return None

    launch_spec = build_launch_command(entry, options_dict, engines_list, logger, prepare_profile=True)
    if launch_spec is None:
        delete_managed_entry_artifacts(entry.id, title, engines_list, logger, delete_profiles=False, stored_profile_path=previous_profile_path, stored_profile_name=previous_profile_name)
        return None
    exec_cmd = shlex.join(launch_spec['argv'])
    profile_info = launch_spec['profile_info']
    address = launch_spec['normalized_address']
    window_identity = sanitize_desktop_value(launch_spec.get('window_identity', ''))

    icon_path = options_dict.get(ICON_PATH_KEY, '').strip()
    icon_field = ''
    managed_icon_path = None
    managed_icon_name = ''
    if icon_path:
        try:
            if '/' not in icon_path and '\\' not in icon_path and icon_path.strip():
                icon_field = icon_path.strip()
            else:
                icon_candidate = Path(icon_path).expanduser()
                if icon_candidate.exists():
                    icon_resolved = icon_candidate.resolve()
                    managed_icon_name = get_managed_icon_name(title, entry.id)
                    managed_icon_path = get_managed_theme_icon_path(title, '.png', entry.id)
                    normalize_icon_to_png(icon_resolved, managed_icon_path)
                    icon_field = str(managed_icon_path)
                else:
                    pass
        except OSError:
            pass

    active = bool(entry.active)

    mode_value = semantic_mode_from_options(options_dict)
    lines = [
        '[Desktop Entry]',
        f'Name={sanitize_desktop_value(display_name)}',
        f'Exec={exec_cmd}',
        'Type=Application',
        f"NoDisplay={'false' if active else 'true'}",
        f'ManagedBy={MANAGED_BY_VALUE}',
        f'EntryId={entry.id}',
        f'X-WebApp-Mode={mode_value}',
        f'X-WebApp-Title={sanitize_desktop_value(title)}',
        f'X-WebApp-DesktopNameSource={display_source}',
    ]
    if icon_field:
        lines.append(f'Icon={icon_field}')
    else:
        lines.append(f'Icon={APP_ICON_NAME}')
    if window_identity:
        lines.append(f'StartupWMClass={window_identity}')
        lines.append(f'X-GNOME-WMClass={window_identity}')
    lines.append('StartupNotify=true')
    content = '\n'.join(lines) + '\n'

    existing_content = ''
    try:
        if target_path.exists():
            existing_content = target_path.read_text(encoding='utf-8')
    except OSError:
        existing_content = ''
    if existing_content != content:
        with open(target_path, 'w', encoding='utf-8') as file_handle:
            file_handle.write(content)
        logger.info('Wrote desktop file %s', target_path)
    delete_managed_entry_artifacts(
        entry.id,
        title,
        engines_list,
        logger,
        keep_path=target_path,
        keep_icon_path=managed_icon_path or None,
        keep_icon_name=managed_icon_name,
        delete_profiles=False,
        stored_profile_path=previous_profile_path,
        stored_profile_name=previous_profile_name,
        keep_profile_path=profile_info.get('profile_path', '') if profile_info else '',
    )
    return {
        'desktop_path': target_path,
        'normalized_address': address,
        'profile_name': profile_info.get('profile_name', '') if profile_info else '',
        'profile_path': profile_info.get('profile_path', '') if profile_info else '',
        'browser_family': profile_info.get('browser_family', '') if profile_info else '',
    }

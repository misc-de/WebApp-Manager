from __future__ import annotations

from browser_option_logic import browser_family_for_engine, browser_state_key, build_family_option_state, decode_browser_state
from webapp_constants import APP_MODE_KEY, ONLY_HTTPS_KEY, OPTION_DISABLE_AI_KEY, OPTION_FORCE_PRIVACY_KEY


def ui_boolean_option_active(option_name: str, stored_value: str | None) -> bool:
    raw_value = stored_value == '1'
    if option_name == OPTION_DISABLE_AI_KEY:
        return not raw_value
    return raw_value


def store_boolean_option_value(option_name: str, active: bool) -> str:
    if option_name == OPTION_DISABLE_AI_KEY:
        return '0' if active else '1'
    return '1' if active else '0'


def current_mode_value(options: dict | None) -> str:
    options = dict(options or {})
    if options.get('Kiosk') == '1':
        return 'kiosk'
    if options.get(APP_MODE_KEY) == '1' and options.get('Frameless') == '1':
        return 'seamless'
    if options.get(APP_MODE_KEY) == '1':
        return 'app'
    return 'standard'


def normalize_mode_value(value) -> str:
    normalized = str(value or '').strip().lower().replace('-', '_').replace(' ', '_')
    aliases = {
        'default': 'standard',
        'normal': 'standard',
        'fullscreen': 'kiosk',
        'frameless': 'seamless',
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in {'standard', 'kiosk', 'app', 'seamless'} else ''


def configured_mode_values_for_engine(config: dict | None, engine) -> list[str]:
    browser_modes = (config or {}).get('browser_modes') or {}
    values: list[str] = []

    def _extend_from(candidate) -> bool:
        nonlocal values
        if not candidate:
            return False
        items = candidate if isinstance(candidate, list) else []
        normalized_items: list[str] = []
        for item in items:
            if isinstance(item, dict):
                mode_value = normalize_mode_value(item.get('value') or item.get('id') or item.get('name'))
            else:
                mode_value = normalize_mode_value(item)
            if mode_value and mode_value not in normalized_items:
                normalized_items.append(mode_value)
        if normalized_items:
            values = normalized_items
            return True
        return False

    family = browser_family_for_engine(engine) if engine else ''
    engine_id = str(engine.get('id')) if engine else ''
    engine_name = str(engine.get('name') or '').strip().lower() if engine else ''
    command = str(engine.get('command') or '').strip().lower() if engine else ''
    nested = browser_modes.get('engines') if isinstance(browser_modes, dict) else None

    candidates = []
    if isinstance(browser_modes, dict):
        candidates.extend([
            browser_modes.get(engine_id),
            browser_modes.get(engine_name),
            browser_modes.get(command),
            browser_modes.get(family),
        ])
        if isinstance(nested, dict):
            candidates.extend([
                nested.get(engine_id),
                nested.get(engine_name),
                nested.get(command),
                nested.get(family),
            ])
        candidates.append(browser_modes.get('default'))

    for candidate in candidates:
        if _extend_from(candidate):
            break

    if not values:
        values = ['standard', 'kiosk', 'app']
        if not engine or family == 'firefox':
            values.append('seamless')

    return values


def coerce_option_updates(current_browser_family: str, updates: dict | None) -> dict[str, str]:
    clean_updates = {key: '' if value is None else str(value) for key, value in (updates or {}).items()}
    if current_browser_family in {'firefox', 'chrome', 'chromium'} and clean_updates.get(OPTION_FORCE_PRIVACY_KEY) == '1':
        clean_updates[ONLY_HTTPS_KEY] = '1'
    return clean_updates


def sync_browser_state_key(family: str) -> str:
    return browser_state_key(family)


def restored_browser_state(options_cache: dict | None, family: str) -> dict[str, str]:
    if family == 'generic':
        return {}
    options_cache = dict(options_cache or {})
    state = build_family_option_state(options_cache, family)
    state.update(decode_browser_state(options_cache.get(sync_browser_state_key(family), ''), family))
    return state

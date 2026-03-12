from __future__ import annotations

import json
from dataclasses import dataclass

from i18n import t
from webapp_constants import (
    APP_MODE_KEY,
    COLOR_SCHEME_KEY,
    ONLY_HTTPS_KEY,
    OPTION_ADBLOCK_KEY,
    OPTION_CLEAR_CACHE_ON_EXIT_KEY,
    OPTION_CLEAR_COOKIES_ON_EXIT_KEY,
    OPTION_DISABLE_AI_KEY,
    OPTION_FORCE_PRIVACY_KEY,
    OPTION_KEEP_IN_BACKGROUND_KEY,
    OPTION_NOTIFICATIONS_KEY,
    OPTION_PRESERVE_SESSION_KEY,
    OPTION_SWIPE_KEY,
    OPTION_UI_LABEL_ALIASES,
    OPTION_UI_LABEL_KEYS,
    USER_AGENT_NAME_KEY,
    USER_AGENT_VALUE_KEY,
)

BROWSER_STATE_PREFIX = '__BrowserState.'


@dataclass(frozen=True)
class BrowserOptionSpec:
    key: str
    label_key: str
    families: tuple[str, ...]
    kind: str
    visible: bool = True


BROWSER_OPTION_SPECS: tuple[BrowserOptionSpec, ...] = (
    BrowserOptionSpec(OPTION_PRESERVE_SESSION_KEY, 'option_previous_session', ('firefox', 'chrome', 'chromium', 'generic'), 'app_logic'),
    BrowserOptionSpec(OPTION_KEEP_IN_BACKGROUND_KEY, 'option_keep_in_background', ('firefox',), 'profile_setting'),
    BrowserOptionSpec(OPTION_NOTIFICATIONS_KEY, 'option_notifications', ('firefox', 'chrome', 'chromium', 'generic'), 'profile_setting'),
    BrowserOptionSpec(OPTION_SWIPE_KEY, 'option_swipe', ('firefox',), 'extension_action'),
    BrowserOptionSpec(OPTION_ADBLOCK_KEY, 'option_adblock', ('firefox',), 'extension_action'),
    BrowserOptionSpec(ONLY_HTTPS_KEY, 'option_only_https', ('firefox', 'chrome', 'chromium', 'generic'), 'profile_setting'),
    BrowserOptionSpec(OPTION_CLEAR_CACHE_ON_EXIT_KEY, 'option_delete_cache', ('firefox', 'chrome', 'chromium', 'generic'), 'shutdown_cleanup'),
    BrowserOptionSpec(OPTION_CLEAR_COOKIES_ON_EXIT_KEY, 'option_delete_cookies', ('firefox', 'chrome', 'chromium', 'generic'), 'shutdown_cleanup'),
    BrowserOptionSpec(OPTION_DISABLE_AI_KEY, 'option_disable_ai', ('firefox',), 'profile_setting'),
    BrowserOptionSpec(OPTION_FORCE_PRIVACY_KEY, 'option_set_privacy', ('firefox', 'chrome', 'chromium', 'generic'), 'macro'),
)

OPTION_SPEC_BY_KEY = {spec.key: spec for spec in BROWSER_OPTION_SPECS}


def browser_family_for_command(command: str) -> str:
    lower = (command or '').lower()
    if 'firefox' in lower:
        return 'firefox'
    if 'chromium' in lower:
        return 'chromium'
    if 'chrome' in lower:
        return 'chrome'
    return 'generic'


def browser_family_for_engine(engine) -> str:
    if not engine:
        return 'generic'
    return browser_family_for_command(engine.get('command') or '')


def browser_state_key(family: str) -> str:
    family = (family or 'generic').strip().lower() or 'generic'
    return f'{BROWSER_STATE_PREFIX}{family}'


def option_ui_label(option_key: str) -> str:
    label_key = OPTION_UI_LABEL_KEYS.get(option_key)
    return t(label_key) if label_key else option_key


def option_key_from_any(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text in OPTION_SPEC_BY_KEY:
        return text
    for key, aliases in OPTION_UI_LABEL_ALIASES.items():
        if text in aliases:
            return key
    for spec in BROWSER_OPTION_SPECS:
        if text == t(spec.label_key):
            return spec.key
    return text


def normalize_option_dict(options: dict | None) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_key, raw_value in (options or {}).items():
        key = option_key_from_any(raw_key)
        if key is None:
            continue
        normalized[key] = '' if raw_value is None else str(raw_value)
    return normalized

def normalize_option_rows(rows) -> dict[str, str]:
    """Normalize DB option rows while preferring canonical keys over older aliases.

    Rows are expected in the database format: (id, entry_id, option_key, option_value).
    When both an older alias and the canonical key exist for the same logical option,
    the canonical key wins even if the alias row is newer. This prevents old imported
    labels like "Keep Session" or "Allow Notifications" from overriding the current
    stored value for "Previous Session" / "Notifications".
    """
    grouped: dict[str, dict[str, tuple[int, str] | None]] = {}
    for row in rows or ():
        try:
            row_id, _entry_id, raw_key, raw_value = row
        except Exception:
            continue
        key = option_key_from_any(raw_key)
        if key is None:
            continue
        bucket = grouped.setdefault(key, {'canonical': None, 'alias': None})
        record = (int(row_id or 0), '' if raw_value is None else str(raw_value))
        if str(raw_key) == key:
            current = bucket['canonical']
            if current is None or record[0] >= current[0]:
                bucket['canonical'] = record
        else:
            current = bucket['alias']
            if current is None or record[0] >= current[0]:
                bucket['alias'] = record
    normalized: dict[str, str] = {}
    for key, bucket in grouped.items():
        chosen = bucket['canonical'] or bucket['alias']
        if chosen is not None:
            normalized[key] = chosen[1]
    return normalized


def browser_managed_option_keys() -> set[str]:
    return {
        *(spec.key for spec in BROWSER_OPTION_SPECS),
        APP_MODE_KEY,
        'Frameless',
        'Kiosk',
        USER_AGENT_NAME_KEY,
        USER_AGENT_VALUE_KEY,
        COLOR_SCHEME_KEY,
    }


def supported_browser_option_keys(family: str, *, visible_only: bool = False) -> set[str]:
    family = (family or 'generic').strip().lower() or 'generic'
    keys = set()
    for spec in BROWSER_OPTION_SPECS:
        if visible_only and not spec.visible:
            continue
        if family in spec.families or 'generic' in spec.families:
            keys.add(spec.key)
    return keys


def default_browser_option_values(family: str) -> dict[str, str]:
    defaults = {key: '0' for key in browser_managed_option_keys()}
    defaults[USER_AGENT_NAME_KEY] = ''
    defaults[USER_AGENT_VALUE_KEY] = ''
    defaults[COLOR_SCHEME_KEY] = 'auto'
    return {key: defaults[key] for key in supported_browser_option_keys(family)}


def project_options_for_family(options: dict, family: str) -> dict[str, str]:
    normalized = normalize_option_dict(options)
    supported = supported_browser_option_keys(family)
    projected = {key: '' if normalized.get(key) is None else str(normalized.get(key)) for key in supported if key in normalized}
    for key in (APP_MODE_KEY, 'Frameless', 'Kiosk', USER_AGENT_NAME_KEY, USER_AGENT_VALUE_KEY, COLOR_SCHEME_KEY):
        if key in normalized:
            projected[key] = '' if normalized.get(key) is None else str(normalized.get(key))
    return projected


def encode_browser_state(options: dict, family: str) -> str:
    payload = project_options_for_family(options, family)
    return json.dumps(payload, sort_keys=True, separators=(',', ':'))


def decode_browser_state(raw: str, family: str) -> dict[str, str]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    normalized = normalize_option_dict(data)
    supported = supported_browser_option_keys(family)
    return {key: '' if value is None else str(value) for key, value in normalized.items() if key in supported}


def build_family_option_state(options: dict, family: str) -> dict[str, str]:
    state = default_browser_option_values(family)
    state.update(project_options_for_family(options, family))
    return state

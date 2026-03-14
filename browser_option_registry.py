from __future__ import annotations

from dataclasses import dataclass

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
    USER_AGENT_NAME_KEY,
    USER_AGENT_VALUE_KEY,
)


@dataclass(frozen=True)
class BrowserOptionBinding:
    family: str
    storage_kind: str
    supported_values: tuple[str, ...] | None = ('0', '1')
    transfer_policy: str = 'exact'
    read_channel: str = 'profile'
    write_channel: str = 'profile'
    notes: str = ''


@dataclass(frozen=True)
class BrowserOptionSpec:
    key: str
    label_key: str | None
    kind: str
    bindings: tuple[BrowserOptionBinding, ...]
    default_value: str = '0'
    visible: bool = True
    ui_control: str = 'switch'
    browser_managed: bool = True

    @property
    def families(self) -> tuple[str, ...]:
        return tuple(binding.family for binding in self.bindings)


def _bindings(*families: str, storage_kind: str, values: tuple[str, ...] | None = ('0', '1'), transfer_policy: str = 'exact', read_channel: str = 'profile', write_channel: str = 'profile', notes: str = '') -> tuple[BrowserOptionBinding, ...]:
    return tuple(
        BrowserOptionBinding(
            family=family,
            storage_kind=storage_kind,
            supported_values=values,
            transfer_policy=transfer_policy,
            read_channel=read_channel,
            write_channel=write_channel,
            notes=notes,
        )
        for family in families
    )


_VISIBLE_BROWSER_OPTION_SPECS: tuple[BrowserOptionSpec, ...] = (
    BrowserOptionSpec(
        OPTION_PRESERVE_SESSION_KEY,
        'option_previous_session',
        'app_logic',
        _bindings('firefox', 'chrome', 'chromium', 'generic', storage_kind='app_logic', notes='Restore the previous session when the engine supports a startup/session restore mode.'),
    ),
    BrowserOptionSpec(
        OPTION_KEEP_IN_BACKGROUND_KEY,
        'option_keep_in_background',
        'profile_setting',
        _bindings('firefox', storage_kind='profile_setting', notes='Firefox/Furios-specific background preload behavior.'),
    ),
    BrowserOptionSpec(
        OPTION_NOTIFICATIONS_KEY,
        'option_notifications',
        'profile_setting',
        _bindings('firefox', 'chrome', 'chromium', 'generic', storage_kind='profile_setting', notes='Map to the engine-specific default notification permission settings.'),
    ),
    BrowserOptionSpec(
        OPTION_SWIPE_KEY,
        'option_swipe',
        'extension_action',
        _bindings('firefox', storage_kind='extension_action', transfer_policy='per_engine_only', notes='Implemented by a Firefox extension bundle.'),
    ),
    BrowserOptionSpec(
        OPTION_ADBLOCK_KEY,
        'option_adblock',
        'extension_action',
        _bindings('firefox', storage_kind='extension_action', transfer_policy='per_engine_only', notes='Implemented by a Firefox extension bundle.'),
    ),
    BrowserOptionSpec(
        ONLY_HTTPS_KEY,
        'option_only_https',
        'profile_setting',
        _bindings('firefox', 'chrome', 'chromium', 'generic', storage_kind='profile_setting', notes='Enable the closest engine-specific HTTPS-only or HTTPS-upgrade behavior.'),
    ),
    BrowserOptionSpec(
        OPTION_CLEAR_CACHE_ON_EXIT_KEY,
        'option_delete_cache',
        'shutdown_cleanup',
        _bindings('firefox', 'chrome', 'chromium', 'generic', storage_kind='shutdown_cleanup', notes='Clear cache on shutdown/exit when supported.'),
    ),
    BrowserOptionSpec(
        OPTION_CLEAR_COOKIES_ON_EXIT_KEY,
        'option_delete_cookies',
        'shutdown_cleanup',
        _bindings('firefox', 'chrome', 'chromium', 'generic', storage_kind='shutdown_cleanup', notes='Clear cookies/site data on shutdown/exit when supported.'),
    ),
    BrowserOptionSpec(
        OPTION_DISABLE_AI_KEY,
        'option_disable_ai',
        'profile_setting',
        _bindings('firefox', 'chrome', 'chromium', storage_kind='profile_setting', notes='Disable engine-provided AI assistance/features where the engine exposes a manageable toggle.'),
    ),
    BrowserOptionSpec(
        OPTION_FORCE_PRIVACY_KEY,
        'option_set_privacy',
        'macro',
        _bindings('firefox', 'chrome', 'chromium', 'generic', storage_kind='macro', notes='Apply the engine-specific privacy macro/preset.'),
    ),
)


_HIDDEN_BROWSER_OPTION_SPECS: tuple[BrowserOptionSpec, ...] = (
    BrowserOptionSpec(
        APP_MODE_KEY,
        None,
        'app_logic',
        _bindings('firefox', 'chrome', 'chromium', 'generic', storage_kind='app_logic', notes='Window/app mode expressed independently from engine internals.'),
        visible=False,
        ui_control='hidden',
    ),
    BrowserOptionSpec(
        'Frameless',
        None,
        'app_logic',
        _bindings('firefox', 'chrome', 'chromium', 'generic', storage_kind='app_logic', transfer_policy='exact', notes='Frameless window hint kept as semantic UI state.'),
        visible=False,
        ui_control='hidden',
    ),
    BrowserOptionSpec(
        'Kiosk',
        None,
        'app_logic',
        _bindings('firefox', 'chrome', 'chromium', 'generic', storage_kind='app_logic', transfer_policy='exact', notes='Kiosk/app mode hint kept as semantic UI state.'),
        visible=False,
        ui_control='hidden',
    ),
    BrowserOptionSpec(
        USER_AGENT_NAME_KEY,
        None,
        'profile_setting',
        _bindings('firefox', 'chrome', 'chromium', 'generic', storage_kind='profile_setting', values=None, notes='Human-readable selected user-agent preset label.'),
        default_value='',
        visible=False,
        ui_control='hidden',
    ),
    BrowserOptionSpec(
        USER_AGENT_VALUE_KEY,
        None,
        'profile_setting',
        _bindings('firefox', 'chrome', 'chromium', 'generic', storage_kind='profile_setting', values=None, notes='Concrete user-agent override string.'),
        default_value='',
        visible=False,
        ui_control='hidden',
    ),
    BrowserOptionSpec(
        COLOR_SCHEME_KEY,
        None,
        'profile_setting',
        _bindings('firefox', 'chrome', 'chromium', 'generic', storage_kind='profile_setting', values=('auto', 'light', 'dark'), notes='Semantic color-scheme override that maps to engine-specific representation.'),
        default_value='auto',
        visible=False,
        ui_control='dropdown',
    ),
)


ALL_BROWSER_OPTION_SPECS: tuple[BrowserOptionSpec, ...] = _VISIBLE_BROWSER_OPTION_SPECS + _HIDDEN_BROWSER_OPTION_SPECS
ALL_BROWSER_OPTION_SPEC_BY_KEY = {spec.key: spec for spec in ALL_BROWSER_OPTION_SPECS}


def visible_browser_option_specs() -> tuple[BrowserOptionSpec, ...]:
    return _VISIBLE_BROWSER_OPTION_SPECS


def all_browser_option_specs() -> tuple[BrowserOptionSpec, ...]:
    return ALL_BROWSER_OPTION_SPECS


def option_spec(option_key: str) -> BrowserOptionSpec | None:
    return ALL_BROWSER_OPTION_SPEC_BY_KEY.get(option_key)


def option_binding(option_key: str, family: str) -> BrowserOptionBinding | None:
    spec = option_spec(option_key)
    if spec is None:
        return None
    normalized_family = (family or 'generic').strip().lower() or 'generic'
    for binding in spec.bindings:
        if binding.family == normalized_family:
            return binding
    if normalized_family != 'generic':
        for binding in spec.bindings:
            if binding.family == 'generic':
                return binding
    return None


def option_supported(option_key: str, family: str, *, visible_only: bool = False) -> bool:
    spec = option_spec(option_key)
    if spec is None:
        return False
    if visible_only and not spec.visible:
        return False
    return option_binding(option_key, family) is not None


def supported_option_keys(family: str, *, visible_only: bool = False) -> set[str]:
    specs = _VISIBLE_BROWSER_OPTION_SPECS if visible_only else ALL_BROWSER_OPTION_SPECS
    return {
        spec.key
        for spec in specs
        if option_binding(spec.key, family) is not None
    }


def default_option_values(family: str, *, visible_only: bool = False) -> dict[str, str]:
    specs = _VISIBLE_BROWSER_OPTION_SPECS if visible_only else ALL_BROWSER_OPTION_SPECS
    defaults: dict[str, str] = {}
    for spec in specs:
        if option_binding(spec.key, family) is None:
            continue
        defaults[spec.key] = '' if spec.default_value is None else str(spec.default_value)
    return defaults


def browser_managed_option_keys() -> set[str]:
    return {spec.key for spec in ALL_BROWSER_OPTION_SPECS if spec.browser_managed}

from __future__ import annotations

from dataclasses import dataclass

from webapp_constants import (
    APP_MODE_KEY,
    COLOR_SCHEME_KEY,
    DEFAULT_ZOOM_KEY,
    MODE_DESKTOP_KEY,
    MODE_MOBILE_KEY,
    ONLY_HTTPS_KEY,
    OPTION_ADBLOCK_KEY,
    OPTION_CLEAR_CACHE_ON_EXIT_KEY,
    OPTION_CLEAR_COOKIES_ON_EXIT_KEY,
    OPTION_DISABLE_AI_KEY,
    OPTION_FORCE_PRIVACY_KEY,
    OPTION_PREVENT_MULTIPLE_STARTS_KEY,
    OPTION_SAFE_GRAPHICS_KEY,
    OPTION_STARTUP_BOOSTER_KEY,
    OPTION_KEEP_IN_BACKGROUND_KEY,
    OPTION_NOTIFICATIONS_KEY,
    OPTION_OPEN_LINKS_IN_TABS_KEY,
    OPTION_PRESERVE_SESSION_KEY,
    OPTION_SWIPE_KEY,
    SEMANTIC_MODE_VALUES,
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
    category: str = 'comfort'

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
        category='comfort',
    ),
    BrowserOptionSpec(
        OPTION_KEEP_IN_BACKGROUND_KEY,
        'option_keep_in_background',
        'profile_setting',
        _bindings('firefox', 'chrome', 'chromium', storage_kind='profile_setting', notes='Map to the engine-specific background-running behavior when supported.'),
        category='comfort',
    ),
    BrowserOptionSpec(
        OPTION_NOTIFICATIONS_KEY,
        'option_notifications',
        'profile_setting',
        _bindings('firefox', 'chrome', 'chromium', 'generic', storage_kind='profile_setting', notes='Map to the engine-specific default notification permission settings.'),
        category='comfort',
    ),
    BrowserOptionSpec(
        OPTION_OPEN_LINKS_IN_TABS_KEY,
        'option_open_links_in_tabs',
        'profile_setting',
        _bindings('firefox', storage_kind='profile_setting', notes='Open links intended for new windows in tabs instead when Firefox supports it.'),
        category='comfort',
    ),
    BrowserOptionSpec(
        OPTION_SWIPE_KEY,
        'option_swipe',
        'extension_action',
        _bindings('firefox', storage_kind='extension_action', transfer_policy='per_engine_only', notes='Implemented by a Firefox extension bundle.'),
        category='addons',
    ),
    BrowserOptionSpec(
        OPTION_ADBLOCK_KEY,
        'option_adblock',
        'extension_action',
        _bindings('firefox', storage_kind='extension_action', transfer_policy='per_engine_only', notes='Implemented by a Firefox extension bundle.'),
        category='addons',
    ),
    BrowserOptionSpec(
        ONLY_HTTPS_KEY,
        'option_only_https',
        'profile_setting',
        _bindings('firefox', 'chrome', 'chromium', 'generic', storage_kind='profile_setting', notes='Enable the closest engine-specific HTTPS-only or HTTPS-upgrade behavior.'),
        category='security',
    ),
    BrowserOptionSpec(
        OPTION_CLEAR_CACHE_ON_EXIT_KEY,
        'option_delete_cache',
        'shutdown_cleanup',
        _bindings('firefox', 'chrome', 'chromium', 'generic', storage_kind='shutdown_cleanup', notes='Clear cache on shutdown/exit when supported.'),
        category='cleanup',
    ),
    BrowserOptionSpec(
        OPTION_CLEAR_COOKIES_ON_EXIT_KEY,
        'option_delete_cookies',
        'shutdown_cleanup',
        _bindings('firefox', 'chrome', 'chromium', 'generic', storage_kind='shutdown_cleanup', notes='Clear cookies/site data on shutdown/exit when supported.'),
        category='cleanup',
    ),
    BrowserOptionSpec(
        OPTION_DISABLE_AI_KEY,
        'option_disable_ai',
        'profile_setting',
        _bindings('firefox', 'chrome', 'chromium', storage_kind='profile_setting', notes='Disable engine-provided AI assistance/features where the engine exposes a manageable toggle.'),
        category='comfort',
    ),
    BrowserOptionSpec(
        OPTION_STARTUP_BOOSTER_KEY,
        'option_startup_booster',
        'macro',
        _bindings('firefox', 'chrome', 'chromium', storage_kind='macro', notes='Apply engine-specific startup optimizations that skip welcome/default-browser overhead without overriding startup URL, session retention, or extension selections.'),
        category='performance',
    ),
    BrowserOptionSpec(
        OPTION_PREVENT_MULTIPLE_STARTS_KEY,
        'option_prevent_multiple_starts',
        'app_logic',
        _bindings('firefox', 'chrome', 'chromium', 'generic', storage_kind='app_logic', notes='Prevent launching a second browser process for the same WebApp while the previously started one is still alive in the current manager session.'),
        category='comfort',
    ),
    BrowserOptionSpec(
        OPTION_SAFE_GRAPHICS_KEY,
        'option_safe_graphics',
        'profile_setting',
        _bindings('firefox', storage_kind='profile_setting', notes='Use conservative Firefox graphics settings for WebApps that misbehave with GPU/WebGL acceleration.'),
        category='performance',
    ),
    BrowserOptionSpec(
        OPTION_FORCE_PRIVACY_KEY,
        'option_set_privacy',
        'macro',
        _bindings('firefox', 'chrome', 'chromium', 'generic', storage_kind='macro', notes='Apply the engine-specific privacy macro/preset.'),
        category='security',
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
    BrowserOptionSpec(
        DEFAULT_ZOOM_KEY,
        None,
        'profile_setting',
        _bindings('firefox', 'chrome', 'chromium', 'generic', storage_kind='profile_setting', values=('50', '67', '80', '90', '100', '110', '125', '150', '175', '200'), notes='Semantic default zoom value kept per managed profile.'),
        default_value='100',
        visible=False,
        ui_control='dropdown',
    ),
    BrowserOptionSpec(
        MODE_MOBILE_KEY,
        None,
        'app_logic',
        _bindings('firefox', 'chrome', 'chromium', 'generic', storage_kind='app_logic', values=SEMANTIC_MODE_VALUES, notes='Semantic launch mode used when the launcher detects a mobile form factor (Phosh / plasma-mobile / FuriOS).'),
        default_value='standard',
        visible=False,
        ui_control='dropdown',
    ),
    BrowserOptionSpec(
        MODE_DESKTOP_KEY,
        None,
        'app_logic',
        _bindings('firefox', 'chrome', 'chromium', 'generic', storage_kind='app_logic', values=SEMANTIC_MODE_VALUES, notes='Semantic launch mode used when the launcher detects a desktop form factor.'),
        default_value='standard',
        visible=False,
        ui_control='dropdown',
    ),
)



OPTION_CATEGORY_ORDER: tuple[str, ...] = ('security', 'cleanup', 'performance', 'comfort', 'addons')
OPTION_CATEGORY_LABEL_KEYS: dict[str, str] = {
    'security': 'option_category_security',
    'cleanup': 'option_category_cleanup',
    'performance': 'option_category_performance',
    'comfort': 'option_category_comfort',
    'addons': 'option_category_addons',
}


def option_category(option_key: str) -> str:
    spec = option_spec(option_key)
    if spec is None:
        return 'comfort'
    category = (spec.category or 'comfort').strip().lower()
    return category if category in OPTION_CATEGORY_ORDER else 'comfort'


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

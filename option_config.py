from __future__ import annotations

from browser_option_registry import visible_browser_option_specs
from webapp_constants import (
    OPTION_ADBLOCK_KEY,
    OPTION_CLEAR_CACHE_ON_EXIT_KEY,
    OPTION_CLEAR_COOKIES_ON_EXIT_KEY,
)


def option_names() -> list[str]:
    return [spec.key for spec in visible_browser_option_specs() if spec.visible]


def overview_status_definitions() -> list[tuple[str, str, str]]:
    return [
        (OPTION_ADBLOCK_KEY, 'icons/ublock.svg', 'overview_status_adblock'),
        (OPTION_CLEAR_CACHE_ON_EXIT_KEY, 'icons/broom.svg', 'overview_status_delete_cache'),
        (OPTION_CLEAR_COOKIES_ON_EXIT_KEY, 'icons/cookie.svg', 'overview_status_delete_cookies'),
    ]

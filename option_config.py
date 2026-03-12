from __future__ import annotations

from webapp_constants import (
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
)


def option_names() -> list[str]:
    return [
        OPTION_PRESERVE_SESSION_KEY,
        OPTION_KEEP_IN_BACKGROUND_KEY,
        OPTION_NOTIFICATIONS_KEY,
        OPTION_SWIPE_KEY,
        OPTION_ADBLOCK_KEY,
        ONLY_HTTPS_KEY,
        OPTION_CLEAR_CACHE_ON_EXIT_KEY,
        OPTION_CLEAR_COOKIES_ON_EXIT_KEY,
        OPTION_DISABLE_AI_KEY,
        OPTION_FORCE_PRIVACY_KEY,
    ]


def overview_status_definitions() -> list[tuple[str, str, str]]:
    return [
        (OPTION_ADBLOCK_KEY, 'icons/ublock.svg', 'overview_status_adblock'),
        (OPTION_CLEAR_CACHE_ON_EXIT_KEY, 'icons/broom.svg', 'overview_status_delete_cache'),
        (OPTION_CLEAR_COOKIES_ON_EXIT_KEY, 'icons/cookie.svg', 'overview_status_delete_cookies'),
    ]

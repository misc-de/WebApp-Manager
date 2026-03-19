from pathlib import Path

ADDRESS_KEY = 'Address'
USER_AGENT_NAME_KEY = 'UserAgentName'
USER_AGENT_VALUE_KEY = 'UserAgentValue'
ICON_PATH_KEY = 'IconPath'
PROFILE_NAME_KEY = 'ProfileName'
PROFILE_PATH_KEY = 'ProfilePath'
ONLY_HTTPS_KEY = 'Only HTTPS'
APP_MODE_KEY = 'App Mode'
COLOR_SCHEME_KEY = 'Color Scheme'
DEFAULT_ZOOM_KEY = 'Default Zoom'
CUSTOM_CSS_LINKS_KEY = 'Custom CSS Links'
CUSTOM_JS_LINKS_KEY = 'Custom JavaScript Links'
INLINE_CUSTOM_CSS_KEY = 'Inline Custom CSS'
INLINE_CUSTOM_JS_KEY = 'Inline Custom JavaScript'

OPTION_PRESERVE_SESSION_KEY = 'Previous Session'
OPTION_KEEP_IN_BACKGROUND_KEY = 'Keep in Background'
OPTION_NOTIFICATIONS_KEY = 'Notifications'
OPTION_SWIPE_KEY = 'Swipe'
OPTION_ADBLOCK_KEY = 'Adblock'
OPTION_CLEAR_CACHE_ON_EXIT_KEY = 'Clear Cache On Exit'
OPTION_CLEAR_COOKIES_ON_EXIT_KEY = 'Clear Cookies On Exit'
OPTION_DISABLE_AI_KEY = 'Disable AI'
OPTION_FORCE_PRIVACY_KEY = 'Set Privacy'
OPTION_STARTUP_BOOSTER_KEY = 'Startup Booster'

OPTION_UI_LABEL_KEYS = {
    OPTION_PRESERVE_SESSION_KEY: 'option_previous_session',
    OPTION_KEEP_IN_BACKGROUND_KEY: 'option_keep_in_background',
    OPTION_NOTIFICATIONS_KEY: 'option_notifications',
    OPTION_SWIPE_KEY: 'option_swipe',
    OPTION_ADBLOCK_KEY: 'option_adblock',
    ONLY_HTTPS_KEY: 'option_only_https',
    OPTION_CLEAR_CACHE_ON_EXIT_KEY: 'option_delete_cache',
    OPTION_CLEAR_COOKIES_ON_EXIT_KEY: 'option_delete_cookies',
    OPTION_DISABLE_AI_KEY: 'option_disable_ai',
    OPTION_FORCE_PRIVACY_KEY: 'option_set_privacy',
    OPTION_STARTUP_BOOSTER_KEY: 'option_startup_booster',
}

OPTION_UI_LABEL_ALIASES = {
    OPTION_PRESERVE_SESSION_KEY: {'Keep Session', 'Previous Session', 'Session nach dem Schließen erhalten'},
    OPTION_KEEP_IN_BACKGROUND_KEY: {'Keep in Background', 'Keep Firefox in the Background', 'Keep App in the Background'},
    OPTION_NOTIFICATIONS_KEY: {'Allow Notifications', 'Notifications', 'Benachrichtigungen erlauben'},
    OPTION_SWIPE_KEY: {'Add Swipe Plugin', 'Swipe', 'Swipe-Plugin hinzufügen', 'Firefox Swipe-Unterstützung installieren'},
    OPTION_ADBLOCK_KEY: {'Add Adblock Plugin', 'Adblock', 'Adblock-Plugin hinzufügen', 'Firefox Adblock installieren'},
    ONLY_HTTPS_KEY: {'Only HTTPS', 'Strict HTTPS', 'HTTPS erzwingen'},
    OPTION_CLEAR_CACHE_ON_EXIT_KEY: {'Delete Cache', 'Delete Cache files', 'Cache beim Beenden der App löschen'},
    OPTION_CLEAR_COOKIES_ON_EXIT_KEY: {'Delete Cookies', 'Delete Cookies files', 'Cookies beim Beenden der App löschen'},
    OPTION_DISABLE_AI_KEY: {'Disable AI', 'KI-Funktionen deaktivieren', 'Firefox KI-Funktionen deaktivieren'},
    OPTION_FORCE_PRIVACY_KEY: {'Set Privacy', 'Force Privacy', 'Erweiterten Datenschutz aktivieren'},
    OPTION_STARTUP_BOOSTER_KEY: {'Startup Booster', 'Startbeschleunigung', 'Schneller Start'},
}

APPLICATIONS_DIR = Path.home() / '.local/share/applications'
ICON_THEME_APPS_DIR = Path.home() / '.local/share/icons' / 'hicolor' / '512x512' / 'apps'
FIREFOX_ROOT = Path.home() / '.mozilla' / 'firefox'
CHROMIUM_PROFILE_ROOT = Path.home() / '.config' / 'webapp-browser-profiles'

NON_PORTABLE_WAPP_OPTION_KEYS = frozenset({ICON_PATH_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY, CUSTOM_CSS_LINKS_KEY, CUSTOM_JS_LINKS_KEY})

"""Reading and writing managed browser profile settings.

Firefox user.js / Chromium Preferences writers, the ProfileSettings value
object, runtime-cache clearing, the app-mode userChrome.css block and the
round-trip readers. Sits above browser_paths and browser_extensions in the
dependency graph and is never imported by them.
"""
import json
import re
from dataclasses import dataclass
from typing import Any
from pathlib import Path

from distro_utils import is_furios_distribution
from webapp_constants import (
    APP_MODE_KEY,
    COLOR_SCHEME_KEY,
    DEFAULT_ZOOM_KEY,
    ONLY_HTTPS_KEY,
    OPTION_ADBLOCK_KEY,
    OPTION_CLEAR_CACHE_ON_EXIT_KEY,
    OPTION_CLEAR_COOKIES_ON_EXIT_KEY,
    OPTION_DISABLE_AI_KEY,
    OPTION_FORCE_PRIVACY_KEY,
    OPTION_KEEP_IN_BACKGROUND_KEY,
    OPTION_NOTIFICATIONS_KEY,
    OPTION_OPEN_LINKS_IN_TABS_KEY,
    OPTION_PRESERVE_SESSION_KEY,
    OPTION_SAFE_GRAPHICS_KEY,
    OPTION_STARTUP_BOOSTER_KEY,
    OPTION_SWIPE_KEY,
    USER_AGENT_VALUE_KEY,
)
from browser_paths import (
    COLOR_SCHEME_PREF_VALUES,
    normalize_color_scheme,
    normalize_default_zoom,
    _is_explicitly_managed_profile_dir,
    _remove_path_if_exists,
)
from browser_extensions import firefox_extension_installed


FIREFOX_APP_MODE_START = '/* WEBAPP APP MODE START */\n'
FIREFOX_APP_MODE_END = '/* WEBAPP APP MODE END */\n'

# Reverse lookup for layout.css.prefers-color-scheme.content-override. The
# pref is read back from an untrusted user.js, so the key type stays loose:
# anything that is not one of the four known ints falls back to 'auto'.
_COLOR_SCHEME_BY_PREF: dict[Any, str] = {0: 'dark', 1: 'light', 2: 'auto', 3: 'auto'}
def _clear_firefox_runtime_caches(profile_dir, logger):
    profile_dir = Path(profile_dir)
    changed = False
    for candidate in (
        profile_dir / 'cache2',
        profile_dir / 'startupCache',
        profile_dir / 'thumbnails',
        profile_dir / 'shader-cache',
    ):
        changed = _remove_path_if_exists(candidate, logger, 'Firefox cache') or changed
    return changed


def _clear_chromium_runtime_caches(profile_dir, logger):
    profile_dir = Path(profile_dir)
    changed = False
    for candidate in (
        profile_dir / 'Default' / 'Cache',
        profile_dir / 'Default' / 'Code Cache',
        profile_dir / 'Default' / 'GPUCache',
        profile_dir / 'Default' / 'DawnCache',
        profile_dir / 'Default' / 'GrShaderCache',
        profile_dir / 'ShaderCache',
        profile_dir / 'GraphiteDawnCache',
    ):
        changed = _remove_path_if_exists(candidate, logger, 'Chromium cache') or changed
    return changed


@dataclass(frozen=True)
class ProfileSettings:
    """Resolved per-profile browser settings derived from an entry's options.

    Bundles the ~20 flags that were previously threaded individually through
    ``_write_firefox_user_js`` / ``_write_chromium_preferences`` so the writers
    take a single value object instead of a long positional list. Build one via
    :func:`apply_profile_settings`; both writers read the fields relevant to
    their browser family and ignore the rest.
    """

    clear_cache: bool = False
    clear_cookies: bool = False
    previous_session: bool = False
    user_agent_value: str = ''
    only_https: bool = False
    notifications_enabled: bool = False
    swipe_enabled: bool = False
    keep_in_background: bool = False
    open_links_in_tabs: bool = False
    app_mode: bool = False
    native_window_frame: bool = False
    disable_ai: bool = False
    set_privacy: bool = False
    color_scheme: str = 'auto'
    custom_css_enabled: bool = False
    custom_js_enabled: bool = False
    startup_booster: bool = False
    safe_graphics: bool = False
    default_zoom: str = '100'


def _write_firefox_user_js(profile_dir, settings):
    profile_dir = Path(profile_dir)
    clear_cache = settings.clear_cache
    clear_cookies = settings.clear_cookies
    previous_session = settings.previous_session
    user_agent_value = settings.user_agent_value
    only_https = bool(settings.only_https or settings.set_privacy)
    notifications_enabled = settings.notifications_enabled
    swipe_enabled = settings.swipe_enabled
    keep_in_background = settings.keep_in_background
    open_links_in_tabs = settings.open_links_in_tabs
    app_mode = settings.app_mode
    native_window_frame = settings.native_window_frame
    disable_ai = settings.disable_ai
    set_privacy = settings.set_privacy
    color_scheme = settings.color_scheme
    custom_css_enabled = settings.custom_css_enabled
    custom_js_enabled = settings.custom_js_enabled
    startup_booster = settings.startup_booster
    safe_graphics = settings.safe_graphics
    allow_unsigned_runtime_js = bool(custom_js_enabled and _is_explicitly_managed_profile_dir(profile_dir, 'firefox'))
    user_js = profile_dir / 'user.js'
    start_marker = '// WEBAPP MANAGED START\n'
    end_marker = '// WEBAPP MANAGED END\n'
    effective_clear_cache_on_shutdown = bool(clear_cache and not previous_session)
    prefs = {
        'privacy.sanitize.sanitizeOnShutdown': effective_clear_cache_on_shutdown or clear_cookies,
        'privacy.clearOnShutdown.cache': effective_clear_cache_on_shutdown,
        'privacy.clearOnShutdown.cookies': clear_cookies,
        'privacy.clearOnShutdown_v2.cache': effective_clear_cache_on_shutdown,
        'privacy.clearOnShutdown_v2.cookiesAndStorage': clear_cookies,
        'privacy.clearOnShutdown_v2.siteSettings': False,
        'privacy.sanitize.timeSpan': 0,
        'privacy.clearOnShutdown.downloads': False,
        'privacy.clearOnShutdown.formData': False,
        'privacy.clearOnShutdown.history': False,
        'privacy.clearOnShutdown.offlineApps': False,
        'privacy.clearOnShutdown.sessions': False,
        'privacy.clearOnShutdown.siteSettings': False,
        'browser.startup.page': 3 if previous_session else 0,
        'browser.startup.homepage': 'about:blank',
        'startup.homepage_welcome_url': '',
        'startup.homepage_welcome_url.additional': '',
        'startup.homepage_override_url': '',
        'browser.aboutwelcome.enabled': False,
        'browser.newtabpage.enabled': False,
        'datareporting.policy.firstRunURL': '',
        'dom.security.https_only_mode': only_https,
        'extensions.autoDisableScopes': 0,
        'extensions.enabledScopes': 15,
        'extensions.installDistroAddons': True,
        'extensions.shownSelectionUI': True,
        'browser.shell.checkDefaultBrowser': False,
        'dom.webnotifications.enabled': bool(notifications_enabled),
        'dom.webnotifications.serviceworker.enabled': bool(notifications_enabled),
        'dom.push.enabled': bool(notifications_enabled),
        'permissions.default.desktop-notification': 1 if notifications_enabled else 0,
        'browser.link.open_newwindow': 3 if open_links_in_tabs else 2,
        'browser.gesture.swipe.left': 'Browser:BackOrBackDuplicate' if swipe_enabled else '',
        'browser.gesture.swipe.right': 'Browser:ForwardOrForwardDuplicate' if swipe_enabled else '',
        'toolkit.legacyUserProfileCustomizations.stylesheets': bool(app_mode or custom_css_enabled),
        'browser.tabs.inTitlebar': 0 if native_window_frame else 1,
        'browser.newtabpage.activity-stream.feeds.topsites': False,
        'browser.newtabpage.activity-stream.feeds.system.topsites': False,
        'browser.translations.automaticallyPopup': False,
        'xpinstall.signatures.required': False if allow_unsigned_runtime_js else True,
        'webapp.clear_cache_requested': bool(clear_cache),
    }
    if disable_ai:
        prefs.update({
            'browser.ml.enable': False,
            'browser.ml.chat.enabled': False,
            'browser.ml.chat.page': False,
            'browser.ml.chat.provider': '',
            'browser.ml.chat.sidebar': False,
            'browser.ml.chat.shortcuts': False,
            'browser.ml.linkPreview.enabled': False,
            'browser.ml.linkPreview.optin': False,
            'browser.ml.pageAssist.enabled': False,
            'browser.tabs.groups.smart.enabled': False,
            'browser.tabs.groups.smart.userEnabled': False,
            'browser.smartwindow.enabled': False,
            'browser.ai.control.sidebarChatbot': 'blocked',
            'browser.ai.control.linkPreviewKeyPoints': 'blocked',
            'browser.ai.control.smartTabGroups': 'blocked',
            'browser.ai.control.translations': 'blocked',
        })
    prefs['webapp.startup_booster.enabled'] = bool(startup_booster)
    if startup_booster:
        prefs.update({
            'browser.newtab.preload': False,
            'browser.startup.homepage_override.mstone': 'ignore',
            'browser.shell.checkDefaultBrowser': False,
            'browser.aboutwelcome.enabled': False,
            'browser.newtabpage.enabled': False,
            'browser.sessionstore.restore_on_demand': True if previous_session else prefs.get('browser.sessionstore.restore_on_demand', True),
            'browser.sessionstore.restore_hidden_tabs': False,
            'browser.sessionstore.restore_pinned_tabs_on_demand': True if previous_session else prefs.get('browser.sessionstore.restore_pinned_tabs_on_demand', False),
        })

    if is_furios_distribution():
        prefs.update({
            'furi.browser.preload.disabled': False if keep_in_background else True,
            # Furios devices can fail to render Firefox WebApps on Wayland/Mali when
            # WebRender or VA-API comes up with an incompatible GPU path.
            'gfx.webrender.all': False,
            'layers.acceleration.disabled': True,
            'gfx.canvas.accelerated': False,
            'media.ffmpeg.vaapi.enabled': False,
        })

    if safe_graphics:
        prefs.update({
            'gfx.webrender.all': False,
            'layers.acceleration.disabled': True,
            'gfx.canvas.accelerated': False,
            'media.ffmpeg.vaapi.enabled': False,
            'media.hardware-video-decoding.enabled': False,
            'webgl.disabled': True,
            'webgl.enable-webgl2': False,
        })

    color_scheme = normalize_color_scheme(color_scheme)
    prefs['layout.css.prefers-color-scheme.content-override'] = COLOR_SCHEME_PREF_VALUES[color_scheme]
    if color_scheme == 'dark':
        prefs['ui.systemUsesDarkTheme'] = 1
    elif color_scheme == 'light':
        prefs['ui.systemUsesDarkTheme'] = 0

    if set_privacy:
        prefs.update({
            'datareporting.healthreport.uploadEnabled': False,
            'toolkit.telemetry.enabled': False,
            'toolkit.telemetry.unified': False,
            'toolkit.telemetry.archive.enabled': False,
            'toolkit.telemetry.server': '',
            'toolkit.telemetry.coverage.opt-out': True,
            'toolkit.telemetry.bhrPing.enabled': False,
            'browser.discovery.enabled': False,
            'app.shield.optoutstudies.enabled': False,
            'browser.newtabpage.activity-stream.feeds.telemetry': False,
            'browser.newtabpage.activity-stream.telemetry': False,
            'browser.ping-centre.telemetry': False,
            'browser.send_pings': False,
            'beacon.enabled': False,
            'privacy.globalprivacycontrol.enabled': True,
            'privacy.globalprivacycontrol.functionality.enabled': True,
            'privacy.donottrackheader.enabled': True,
            'network.prefetch-next': False,
            'network.predictor.enabled': False,
            'privacy.trackingprotection.enabled': True,
            'privacy.trackingprotection.socialtracking.enabled': True,
            'privacy.annotate_channels.strict_list.enabled': True,
            'browser.contentblocking.category': 'strict',
            'browser.search.suggest.enabled': False,
            'browser.urlbar.suggest.searches': False,
            'browser.search.region': 'DE',
            'browser.search.countryCode': 'DE',
            'browser.search.defaultenginename': 'DuckDuckGo',
            'browser.search.defaultenginename.US': 'DuckDuckGo',
            'browser.search.order.1': 'DuckDuckGo',
            'browser.search.selectedEngine': 'DuckDuckGo',
            'browser.search.defaultEngine': 'DuckDuckGo',
            'browser.urlbar.placeholderName': 'DuckDuckGo',
            'browser.urlbar.placeholderName.private': 'DuckDuckGo',
            'layout.spellcheckDefault': 0,
            'places.history.enabled': False,
            'browser.formfill.enable': False,
            'datareporting.usage.uploadEnabled': False,
            'browser.newtabpage.activity-stream.showSearch': False,
            'browser.newtabpage.activity-stream.feeds.section.topstories': False,
            'browser.newtabpage.activity-stream.feeds.section.highlights': False,
            'browser.newtabpage.activity-stream.feeds.section.topsites': False,
            'browser.newtabpage.activity-stream.showSponsoredTopSites': False,
            'browser.newtabpage.activity-stream.showSponsored': False,
            'browser.newtabpage.activity-stream.showSponsoredCheckboxes': False,
            'browser.newtabpage.activity-stream.asrouter.userprefs.cfr': False,
            'browser.newtabpage.activity-stream.asrouter.userprefs.extensionRecommendations': False,
            'browser.newtabpage.activity-stream.feeds.snippets': False,
            'browser.aboutHomeSnippets.updateUrl': '',
            'browser.shell.checkDefaultBrowser': False,
            'browser.startup.homepage_override.mstone': 'ignore',
            'browser.safebrowsing.downloads.remote.enabled': False,
            'browser.safebrowsing.downloads.enabled': False,
            'browser.safebrowsing.malware.enabled': False,
            'browser.safebrowsing.phishing.enabled': False,
            'browser.safebrowsing.blockedURIs.enabled': False,
            'browser.safebrowsing.provider.google.gethashURL': '',
            'browser.safebrowsing.provider.google.updateURL': '',
            'browser.safebrowsing.provider.google4.gethashURL': '',
            'browser.safebrowsing.provider.google4.updateURL': '',
            'browser.tabs.groups.smart.enabled': False,
            'browser.tabs.groups.smart.userEnabled': False,
            'browser.ml.chat.enabled': False,
            'browser.ml.chat.page': False,
            'browser.ml.chat.provider': '',
            'browser.ml.chat.sidebar': False,
            'browser.ml.chat.shortcuts': False,
            'browser.ml.linkPreview.enabled': False,
            'browser.ml.linkPreview.optin': False,
            'browser.ml.pageAssist.enabled': False,
            'browser.ml.enable': False,
            'browser.smartwindow.enabled': False,
            'browser.ai.control.sidebarChatbot': 'blocked',
            'browser.ai.control.linkPreviewKeyPoints': 'blocked',
            'browser.ai.control.smartTabGroups': 'blocked',
            'browser.ai.control.translations': 'blocked',
            'browser.preferences.experimental.hidden': True,
            'browser.urlbar.quicksuggest.enabled': False,
            'browser.urlbar.quicksuggest.dataCollection.enabled': False,
            'extensions.formautofill.addresses.enabled': False,
            'extensions.formautofill.creditCards.enabled': False,
            'signon.autofillForms': False,
        })
    if user_agent_value:
        prefs['general.useragent.override'] = user_agent_value
    else:
        prefs['general.useragent.override'] = ''

    managed_lines = [start_marker]
    for key, value in prefs.items():
        if isinstance(value, bool):
            literal = 'true' if value else 'false'
        elif isinstance(value, (int, float)):
            literal = str(value)
        else:
            literal = json.dumps(value)
        managed_lines.append(f'user_pref("{key}", {literal});\n')
    managed_lines.append(end_marker)
    existing = ''
    if user_js.exists():
        existing = user_js.read_text(encoding='utf-8')
        existing = re.sub(re.escape(start_marker) + r'.*?' + re.escape(end_marker), '', existing, flags=re.S)
        existing = existing.rstrip() + ('\n' if existing.strip() else '')
    new_content = existing + ''.join(managed_lines)
    if user_js.exists():
        try:
            current_content = user_js.read_text(encoding='utf-8')
        except OSError:
            current_content = None
        if current_content == new_content:
            return
    user_js.write_text(new_content, encoding='utf-8')

def _write_chromium_preferences(profile_dir, settings, logger):
    profile_dir = Path(profile_dir)
    clear_cache = settings.clear_cache
    clear_cookies = settings.clear_cookies
    previous_session = settings.previous_session
    user_agent_value = settings.user_agent_value
    only_https = settings.only_https
    notifications_enabled = settings.notifications_enabled
    keep_in_background = settings.keep_in_background
    disable_ai = settings.disable_ai
    set_privacy = settings.set_privacy
    color_scheme = settings.color_scheme
    default_zoom = settings.default_zoom
    startup_booster = settings.startup_booster
    default_dir = profile_dir / 'Default'
    default_dir.mkdir(parents=True, exist_ok=True)
    prefs_path = default_dir / 'Preferences'
    data = {}
    if prefs_path.exists():
        try:
            data = json.loads(prefs_path.read_text(encoding='utf-8'))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            logger.warning('Failed to read Chromium preferences %s: %s', prefs_path, error)
            data = {}
    browser = data.setdefault('browser', {})
    browser['check_default_browser'] = False
    browser['enable_spellchecking'] = True
    clear_on_exit = []
    effective_clear_cache_on_exit = bool(clear_cache and not previous_session)
    if clear_cookies:
        clear_on_exit.append('cookies_and_other_site_data')
    if effective_clear_cache_on_exit:
        clear_on_exit.append('cached_images_and_files')
    browser['clear_data'] = browser.get('clear_data', {})
    browser['clear_data']['clear_on_exit'] = clear_on_exit
    session = data.setdefault('session', {})
    effective_only_https = bool(only_https or set_privacy)
    if startup_booster:
        browser['has_seen_welcome_page'] = True
        browser['first_run_finished'] = True
        data['show-welcome-page'] = False
        data.setdefault('sync_promo', {})['show_on_first_run_allowed'] = False
    else:
        browser.pop('has_seen_welcome_page', None)
        browser.pop('first_run_finished', None)
        data.pop('show-welcome-page', None)
        sync_promo = data.get('sync_promo')
        if isinstance(sync_promo, dict):
            sync_promo.pop('show_on_first_run_allowed', None)
            if not sync_promo:
                data.pop('sync_promo', None)
    session['restore_on_startup'] = 1 if previous_session else 5
    session['startup_urls'] = []
    profile = data.setdefault('profile', {})
    profile['exit_type'] = 'Normal'
    profile['exited_cleanly'] = True
    profile['block_third_party_cookies'] = True if set_privacy else profile.get('block_third_party_cookies', False)
    profile.setdefault('default_content_setting_values', {})['notifications'] = 1 if notifications_enabled else 3
    data['https_only_mode_enabled'] = effective_only_https
    data.setdefault('https_upgrades', {})['policy'] = {'upgrades_enabled': effective_only_https}
    background_mode = data.setdefault('background_mode', {})
    background_mode['enabled'] = bool(keep_in_background)
    search = data.setdefault('search', {})
    if set_privacy:
        search['suggest_enabled'] = False
    spellcheck = data.setdefault('spellcheck', {})
    spellcheck['use_spelling_service'] = False
    translate = data.setdefault('translate', {})
    if set_privacy:
        translate['enabled'] = False
    dns_over_https = data.setdefault('dns_over_https', {})
    if set_privacy:
        dns_over_https['mode'] = 'off'
        dns_over_https['templates'] = ''
    default_search_provider = data.setdefault('default_search_provider', {})
    if set_privacy:
        default_search_provider.update({
            'enabled': True,
            'name': 'DuckDuckGo',
            'keyword': 'duckduckgo.com',
            'search_url': 'https://duckduckgo.com/?q={searchTerms}',
            'suggest_url': 'https://duckduckgo.com/ac/?q={searchTerms}&type=list',
            'icon_url': 'https://duckduckgo.com/favicon.ico',
            'new_tab_url': 'https://duckduckgo.com/',
            'encodings': 'UTF-8',
            'alternate_urls': [
                'https://duckduckgo.com/?q={searchTerms}',
                'https://duckduckgo.com/html/?q={searchTerms}',
            ],
            'search_terms_replacement_key': 'q',
        })
    webapp = data.setdefault('webapp_manager', {})
    if user_agent_value:
        webapp['user_agent_override'] = user_agent_value
    else:
        webapp.pop('user_agent_override', None)
    webapp['disable_ai'] = bool(disable_ai)
    webapp['set_privacy'] = bool(set_privacy)
    webapp['color_scheme'] = normalize_color_scheme(color_scheme)
    webapp['default_zoom'] = normalize_default_zoom(default_zoom)
    webapp['notifications_enabled'] = bool(notifications_enabled)
    webapp['only_https'] = effective_only_https
    webapp['previous_session'] = bool(previous_session)
    webapp['clear_cache_requested'] = bool(clear_cache)
    webapp['keep_in_background'] = bool(keep_in_background)
    webapp['startup_booster'] = bool(startup_booster)
    webapp['default_zoom'] = normalize_default_zoom(webapp.get('default_zoom', '100'))
    data['enable_do_not_track'] = bool(set_privacy)
    data.setdefault('privacy_sandbox', {})['m1'] = {'topics_enabled': not set_privacy, 'fledge_enabled': not set_privacy, 'ad_measurement_enabled': not set_privacy}
    data.setdefault('safebrowsing', {})['enabled'] = False if set_privacy else data.setdefault('safebrowsing', {}).get('enabled', True)
    data.setdefault('safebrowsing', {})['enhanced'] = False if set_privacy else data.setdefault('safebrowsing', {}).get('enhanced', True)
    data.setdefault('safebrowsing', {})['scout_reporting_enabled'] = False if set_privacy else data.setdefault('safebrowsing', {}).get('scout_reporting_enabled', False)
    data.setdefault('alternate_error_pages', {})['enabled'] = False if set_privacy else data.setdefault('alternate_error_pages', {}).get('enabled', True)
    data.setdefault('optimization_guide', {})['model_execution_enabled'] = False if set_privacy or disable_ai else data.setdefault('optimization_guide', {}).get('model_execution_enabled', True)
    data.setdefault('browser_labs', {})['enabled_labs_experiments'] = [] if set_privacy or disable_ai else data.setdefault('browser_labs', {}).get('enabled_labs_experiments', [])
    prefs_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding='utf-8')

def _sync_firefox_app_mode_css(profile_dir, enabled, frameless, logger):
    profile_dir = Path(profile_dir)
    chrome_dir = profile_dir / 'chrome'
    css_path = chrome_dir / 'userChrome.css'
    existing = ''
    if css_path.exists():
        try:
            existing = css_path.read_text(encoding='utf-8')
        except OSError as error:
            logger.warning('Failed to read Firefox userChrome.css %s: %s', css_path, error)
            existing = ''
    pattern = re.escape(FIREFOX_APP_MODE_START) + r'.*?' + re.escape(FIREFOX_APP_MODE_END)
    cleaned = re.sub(pattern, '', existing, flags=re.S).rstrip()
    if not enabled:
        if css_path.exists():
            if cleaned:
                css_path.write_text(cleaned + '\n', encoding='utf-8')
            else:
                try:
                    css_path.unlink(missing_ok=True)
                except OSError as error:
                    logger.warning('Failed to remove empty Firefox userChrome.css %s: %s', css_path, error)
        return

    chrome_dir.mkdir(parents=True, exist_ok=True)
    mode_name = 'seamless' if frameless else 'app'
    if frameless:
        managed_block = (
            FIREFOX_APP_MODE_START
            + f'/* WEBAPP MODE: {mode_name} */\n'
            + '#toolbar-menubar, #TabsToolbar, #nav-bar, #PersonalToolbar, #sidebar-box {\n'
            + '  visibility: collapse !important;\n'
            + '}\n'
            + '#navigator-toolbox {\n'
            + '  min-height: 0 !important;\n'
            + '  max-height: 0 !important;\n'
            + '  border: 0 !important;\n'
            + '  padding: 0 !important;\n'
            + '  margin: 0 !important;\n'
            + '}\n'
            + '#browser, #appcontent, #tabbrowser-tabbox {\n'
            + '  margin: 0 !important;\n'
            + '  padding: 0 !important;\n'
            + '}\n'
            + FIREFOX_APP_MODE_END
        )
    else:
        managed_block = (
            FIREFOX_APP_MODE_START
            + '/* WEBAPP MODE: app */\n'
            + '#toolbar-menubar, #TabsToolbar, #nav-bar, #PersonalToolbar, #sidebar-box {\n'
            + '  visibility: collapse !important;\n'
            + '}\n'
            + '#TabsToolbar, #nav-bar, #PersonalToolbar {\n'
            + '  min-height: 0 !important;\n'
            + '  max-height: 0 !important;\n'
            + '  padding: 0 !important;\n'
            + '  margin: 0 !important;\n'
            + '  border: 0 !important;\n'
            + '}\n'
            + '#titlebar {\n'
            + '  appearance: auto !important;\n'
            + '}\n'
            + '#main-window[tabsintitlebar] #titlebar {\n'
            + '  margin-top: 0 !important;\n'
            + '}\n'
            + '#identity-box, #identity-icon-box, #identity-permission-box {\n'
            + '  display: none !important;\n'
            + '}\n'
            + FIREFOX_APP_MODE_END
        )
    final = (cleaned + '\n\n' + managed_block) if cleaned else managed_block
    css_path.write_text(final, encoding='utf-8')

def _read_firefox_profile_settings(profile_dir):
    profile_dir = Path(profile_dir)
    user_js = profile_dir / 'user.js'
    prefs = {}
    if user_js.exists():
        for line in user_js.read_text(encoding='utf-8', errors='ignore').splitlines():
            line = line.strip()
            match = re.match(r'user_pref\("([^"]+)",\s*(.+)\);$', line)
            if not match:
                continue
            key, raw = match.groups()
            raw = raw.strip()
            if raw in {'true', 'false'}:
                prefs[key] = raw == 'true'
            else:
                try:
                    prefs[key] = json.loads(raw)
                except (json.JSONDecodeError, TypeError, ValueError):
                    prefs[key] = raw.strip('"')
    adblock = firefox_extension_installed(profile_dir, 'adblock')
    swipe = firefox_extension_installed(profile_dir, 'swipe')
    css_text = ''
    css_path = profile_dir / 'chrome' / 'userChrome.css'
    if css_path.exists():
        try:
            css_text = css_path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            css_text = ''
    mode_marker = re.search(r'/\* WEBAPP MODE: ([a-z]+) \*/', css_text)
    mode_name = mode_marker.group(1) if mode_marker else ''
    frameless = mode_name == 'seamless'
    app_mode_enabled = mode_name in {'app', 'seamless'}
    privacy_enabled = bool(
        prefs.get('toolkit.telemetry.enabled') is False
        or prefs.get('privacy.globalprivacycontrol.enabled') is True
        or prefs.get('privacy.donottrackheader.enabled') is True
        or prefs.get('datareporting.healthreport.uploadEnabled') is False
        or prefs.get('datareporting.usage.uploadEnabled') is False
    )
    only_https_enabled = bool(prefs.get('dom.security.https_only_mode')) or privacy_enabled
    disable_ai_enabled = bool(
        prefs.get('browser.ml.chat.enabled') is False
        or prefs.get('browser.tabs.groups.smart.enabled') is False
        or prefs.get('browser.tabs.groups.smart.userEnabled') is False
        or prefs.get('browser.ml.linkPreview.enabled') is False
        or prefs.get('browser.ai.control.smartTabGroups') == 'blocked'
    )
    safe_graphics_enabled = bool(
        prefs.get('webgl.disabled') is True
        or prefs.get('webgl.enable-webgl2') is False
        or prefs.get('media.hardware-video-decoding.enabled') is False
    )
    return {
        OPTION_CLEAR_CACHE_ON_EXIT_KEY: '1' if (prefs.get('webapp.clear_cache_requested') is True or prefs.get('privacy.clearOnShutdown.cache') or prefs.get('privacy.clearOnShutdown_v2.cache')) else '0',
        OPTION_CLEAR_COOKIES_ON_EXIT_KEY: '1' if prefs.get('privacy.clearOnShutdown.cookies') or prefs.get('privacy.clearOnShutdown_v2.cookiesAndStorage') else '0',
        OPTION_ADBLOCK_KEY: '1' if adblock else '0',
        OPTION_PRESERVE_SESSION_KEY: '1' if prefs.get('browser.startup.page') == 3 else '0',
        OPTION_NOTIFICATIONS_KEY: '1' if prefs.get('permissions.default.desktop-notification') == 1 else '0',
        OPTION_OPEN_LINKS_IN_TABS_KEY: '1' if prefs.get('browser.link.open_newwindow') == 3 else '0',
        OPTION_SWIPE_KEY: '1' if swipe or bool(prefs.get('browser.gesture.swipe.left')) else '0',
        ONLY_HTTPS_KEY: '1' if only_https_enabled else '0',
        OPTION_KEEP_IN_BACKGROUND_KEY: '1' if (prefs.get('furi.browser.preload.disabled') is False or ('furi.browser.preload.disabled' not in prefs and prefs.get('browser.tabs.closeWindowWithLastTab') is False)) else '0',
        OPTION_DISABLE_AI_KEY: '1' if disable_ai_enabled else '0',
        OPTION_FORCE_PRIVACY_KEY: '1' if privacy_enabled else '0',
        OPTION_STARTUP_BOOSTER_KEY: '1' if prefs.get('webapp.startup_booster.enabled') is True else '0',
        OPTION_SAFE_GRAPHICS_KEY: '1' if safe_graphics_enabled else '0',
        APP_MODE_KEY: '1' if app_mode_enabled else '0',
        'Frameless': '1' if frameless else '0',
        USER_AGENT_VALUE_KEY: prefs.get('general.useragent.override', '') or '',
        COLOR_SCHEME_KEY: _COLOR_SCHEME_BY_PREF.get(prefs.get('layout.css.prefers-color-scheme.content-override'), 'auto'),
    }

def _read_chromium_profile_settings(profile_dir):
    prefs_path = Path(profile_dir) / 'Default' / 'Preferences'
    if not prefs_path.exists():
        return {}
    try:
        data = json.loads(prefs_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    clear_on_exit = (((data.get('browser') or {}).get('clear_data') or {}).get('clear_on_exit') or [])
    session = data.get('session') or {}
    profile = data.get('profile') or {}
    webapp_manager = data.get('webapp_manager') or {}
    return {
        OPTION_CLEAR_CACHE_ON_EXIT_KEY: '1' if ('cached_images_and_files' in clear_on_exit or (webapp_manager.get('clear_cache_requested')) is True) else '0',
        OPTION_CLEAR_COOKIES_ON_EXIT_KEY: '1' if 'cookies_and_other_site_data' in clear_on_exit else '0',
        OPTION_PRESERVE_SESSION_KEY: '1' if ((webapp_manager.get('previous_session')) is True or session.get('restore_on_startup') == 1) else '0',
        OPTION_NOTIFICATIONS_KEY: '1' if ((webapp_manager.get('notifications_enabled')) is True or ((profile.get('default_content_setting_values') or {}).get('notifications') == 1)) else '0',
        ONLY_HTTPS_KEY: '1' if ((webapp_manager.get('only_https')) is True or data.get('https_only_mode_enabled') or (webapp_manager.get('set_privacy')) is True or data.get('enable_do_not_track') is True) else '0',
        USER_AGENT_VALUE_KEY: (webapp_manager.get('user_agent_override') or ''),
        OPTION_ADBLOCK_KEY: '0',
        OPTION_SWIPE_KEY: '1' if (webapp_manager.get('swipe_enabled')) is True else '0',
        OPTION_KEEP_IN_BACKGROUND_KEY: '1' if ((webapp_manager.get('keep_in_background')) is True or ((data.get('background_mode') or {}).get('enabled') is True)) else '0',
        OPTION_DISABLE_AI_KEY: '1' if (webapp_manager.get('disable_ai')) is True else '0',
        OPTION_FORCE_PRIVACY_KEY: '1' if ((webapp_manager.get('set_privacy')) is True or data.get('enable_do_not_track') is True) else '0',
        OPTION_STARTUP_BOOSTER_KEY: '1' if (webapp_manager.get('startup_booster')) is True else '0',
        APP_MODE_KEY: '0',
        'Frameless': '0',
        'Kiosk': '0',
        COLOR_SCHEME_KEY: str(webapp_manager.get('color_scheme') or 'auto'),
        DEFAULT_ZOOM_KEY: normalize_default_zoom(webapp_manager.get('default_zoom', '100')),
    }

def read_profile_settings(profile_path, browser_family):
    if not profile_path:
        return {}
    if browser_family == 'firefox':
        return _read_firefox_profile_settings(profile_path)
    if browser_family in {'chrome', 'chromium'}:
        return _read_chromium_profile_settings(profile_path)
    return {}

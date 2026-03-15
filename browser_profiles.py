import io
import json
import os
import secrets
import re
import shutil
import tempfile
import zipfile
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

from distro_utils import is_furios_distribution

from i18n import get_app_config
from custom_assets import ensure_profile_customizations, inline_asset_text_for_options, linked_assets_for_options
from browser_option_logic import normalize_option_dict, project_options_for_family
from input_validation import build_safe_slug, sanitize_desktop_value
from webapp_constants import (
    ADDRESS_KEY,
    APP_MODE_KEY,
    CHROMIUM_PROFILE_ROOT,
    COLOR_SCHEME_KEY,
    FIREFOX_ROOT,
    ONLY_HTTPS_KEY,
    OPTION_ADBLOCK_KEY,
    OPTION_CLEAR_CACHE_ON_EXIT_KEY,
    OPTION_CLEAR_COOKIES_ON_EXIT_KEY,
    OPTION_DISABLE_AI_KEY,
    OPTION_FORCE_PRIVACY_KEY,
    OPTION_STARTUP_BOOSTER_KEY,
    OPTION_KEEP_IN_BACKGROUND_KEY,
    OPTION_NOTIFICATIONS_KEY,
    OPTION_PRESERVE_SESSION_KEY,
    OPTION_SWIPE_KEY,
    PROFILE_NAME_KEY,
    PROFILE_PATH_KEY,
    USER_AGENT_VALUE_KEY,
)

BROWSER_DEFAULT_CHECK_PREFS = {
    'firefox': {'browser.shell.checkDefaultBrowser': False},
    'chrome': {'browser.check_default_browser': False},
    'chromium': {'browser.check_default_browser': False},
}

COLOR_SCHEME_PREF_VALUES = {
    'dark': 0,
    'light': 1,
    'auto': 2,
}


FIREFOX_APP_MODE_START = '/* WEBAPP APP MODE START */\n'
FIREFOX_APP_MODE_END = '/* WEBAPP APP MODE END */\n'
DEFAULT_FIREFOX_EXTENSIONS = {
    'adblock': {
        'id': 'uBlock0@raymondhill.net',
        'download_url': 'https://addons.mozilla.org/firefox/downloads/latest/uBlock0@raymondhill.net/latest.xpi',
        'marker_file': '.webapp_adblock_extension_id',
    },
    'swipe': {
        'id': '{6f3ab763-a4c2-4183-b596-984bf5b7ac31}',
        'bundle_path': 'extensions/simple-swipe-navigator-1.6.xpi',
        'download_url': 'https://addons.mozilla.org/android/downloads/file/4666831/simple_swipe_navigator-1.6.xpi',
        'marker_file': '.webapp_simple_swipe_navigator_extension_id',
    },
}


def normalize_color_scheme(value):
    value = (value or 'auto').strip().lower()
    if value not in COLOR_SCHEME_PREF_VALUES:
        return 'auto'
    return value

def append_unique_csv_arg(exec_parts, prefix, values):
    cleaned = []
    seen = set()
    for value in values:
        if not value or value in seen:
            continue
        cleaned.append(value)
        seen.add(value)
    if cleaned:
        exec_parts.append(prefix + ','.join(cleaned))

def get_firefox_extension_config(name):
    config = get_app_config() or {}
    extensions = config.get('firefox_extensions') or {}
    defaults = dict(DEFAULT_FIREFOX_EXTENSIONS.get(name, {}))
    merged = dict(defaults)
    merged.update(extensions.get(name) or {})

    if name == 'swipe':
        bundle_path = str(merged.get('bundle_path') or defaults.get('bundle_path') or '').strip()
        configured_bundle_name = Path(bundle_path).name if bundle_path else ''
        if configured_bundle_name == 'swipe-navigator-minimal.xpi' or not bundle_path:
            merged['bundle_path'] = str(defaults.get('bundle_path') or '').strip()
        merged['marker_file'] = defaults.get('marker_file') or merged.get('marker_file')
    return merged

def get_profile_size_bytes(profile_path):
    if not profile_path:
        return 0
    try:
        path = Path(profile_path).resolve()
    except OSError:
        return 0
    if not path.exists():
        return 0
    total = 0
    for candidate in path.rglob('*'):
        if candidate.is_file():
            try:
                total += candidate.stat().st_size
            except OSError:
                pass
    return total

def _write_firefox_user_js(profile_dir, clear_cache, clear_cookies, previous_session, user_agent_value='', only_https=False, notifications_enabled=False, swipe_enabled=False, keep_in_background=False, startup_url='', app_mode=False, native_window_frame=False, disable_ai=False, set_privacy=False, color_scheme='auto', custom_css_enabled=False, custom_js_enabled=False, startup_booster=False):
    profile_dir = Path(profile_dir)
    only_https = bool(only_https or set_privacy)
    user_js = profile_dir / 'user.js'
    start_marker = '// WEBAPP MANAGED START\n'
    end_marker = '// WEBAPP MANAGED END\n'
    prefs = {
        'privacy.sanitize.sanitizeOnShutdown': clear_cache or clear_cookies,
        'privacy.clearOnShutdown.cache': clear_cache,
        'privacy.clearOnShutdown.cookies': clear_cookies,
        'privacy.clearOnShutdown_v2.cache': clear_cache,
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
        'browser.gesture.swipe.left': 'Browser:BackOrBackDuplicate' if swipe_enabled else '',
        'browser.gesture.swipe.right': 'Browser:ForwardOrForwardDuplicate' if swipe_enabled else '',
        'toolkit.legacyUserProfileCustomizations.stylesheets': bool(app_mode or custom_css_enabled),
        'browser.tabs.inTitlebar': 0 if native_window_frame else 1,
        'browser.newtabpage.activity-stream.feeds.topsites': False,
        'browser.newtabpage.activity-stream.feeds.system.topsites': False,
        'browser.translations.automaticallyPopup': False,
        'xpinstall.signatures.required': False if custom_js_enabled else True,
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
        prefs['furi.browser.preload.disabled'] = False if keep_in_background else True

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

def _write_chromium_preferences(profile_dir, clear_cache, clear_cookies, previous_session, logger, user_agent_value='', only_https=False, notifications_enabled=False, keep_in_background=False, disable_ai=False, set_privacy=False, color_scheme='auto', startup_booster=False):
    profile_dir = Path(profile_dir)
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
    if clear_cookies:
        clear_on_exit.append('cookies_and_other_site_data')
    if clear_cache:
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
    webapp['notifications_enabled'] = bool(notifications_enabled)
    webapp['only_https'] = effective_only_https
    webapp['previous_session'] = bool(previous_session)
    webapp['keep_in_background'] = bool(keep_in_background)
    webapp['startup_booster'] = bool(startup_booster)
    data['enable_do_not_track'] = bool(set_privacy)
    data.setdefault('privacy_sandbox', {})['m1'] = {'topics_enabled': not set_privacy, 'fledge_enabled': not set_privacy, 'ad_measurement_enabled': not set_privacy}
    data.setdefault('safebrowsing', {})['enabled'] = False if set_privacy else data.setdefault('safebrowsing', {}).get('enabled', True)
    data.setdefault('safebrowsing', {})['enhanced'] = False if set_privacy else data.setdefault('safebrowsing', {}).get('enhanced', True)
    data.setdefault('safebrowsing', {})['scout_reporting_enabled'] = False if set_privacy else data.setdefault('safebrowsing', {}).get('scout_reporting_enabled', False)
    data.setdefault('alternate_error_pages', {})['enabled'] = False if set_privacy else data.setdefault('alternate_error_pages', {}).get('enabled', True)
    data.setdefault('optimization_guide', {})['model_execution_enabled'] = False if set_privacy or disable_ai else data.setdefault('optimization_guide', {}).get('model_execution_enabled', True)
    data.setdefault('browser_labs', {})['enabled_labs_experiments'] = [] if set_privacy or disable_ai else data.setdefault('browser_labs', {}).get('enabled_labs_experiments', [])
    prefs_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding='utf-8')

def _extract_firefox_extension_id(xpi_bytes, fallback_id):
    try:
        with zipfile.ZipFile(io.BytesIO(xpi_bytes)) as archive:
            for candidate in ('manifest.json', 'package/manifest.json'):
                if candidate in archive.namelist():
                    manifest = json.loads(archive.read(candidate).decode('utf-8'))
                    gecko = (manifest.get('browser_specific_settings') or {}).get('gecko', {})
                    addon_id = gecko.get('id') or ((manifest.get('applications') or {}).get('gecko', {}) or {}).get('id')
                    if addon_id:
                        return addon_id
    except (zipfile.BadZipFile, OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    return fallback_id

def _firefox_extension_candidates(extension_name):
    config = get_firefox_extension_config(extension_name)
    defaults = DEFAULT_FIREFOX_EXTENSIONS.get(extension_name, {})
    configured_id = (config.get('id') or defaults.get('id') or '').strip()
    configured_marker = (config.get('marker_file') or defaults.get('marker_file') or '').strip()
    return {
        'id': configured_id,
        'bundle_path': (config.get('bundle_path') or defaults.get('bundle_path') or '').strip(),
        'download_url': (config.get('download_url') or defaults.get('download_url') or '').strip(),
        'marker_file': configured_marker,
    }

def _managed_firefox_extension_paths(profile_dir, extension_name):
    profile_dir = Path(profile_dir)
    extensions_dir = profile_dir / 'extensions'
    candidates = _firefox_extension_candidates(extension_name)
    configured_id = (candidates.get('id') or '').strip()
    marker_name = (candidates.get('marker_file') or '').strip()
    marker_paths = [extensions_dir / marker_name] if marker_name else []
    ids = [configured_id] if configured_id else []
    for marker_path in marker_paths:
        if not marker_path.exists():
            continue
        try:
            addon_id = marker_path.read_text(encoding='utf-8').strip()
        except OSError:
            addon_id = ''
        if addon_id and addon_id not in ids:
            ids.append(addon_id)
    xpi_paths = [extensions_dir / f'{addon_id}.xpi' for addon_id in ids if addon_id]
    return {
        'extensions_dir': extensions_dir,
        'primary_marker_path': marker_paths[0] if marker_paths else None,
        'marker_paths': marker_paths,
        'ids': ids,
        'xpi_paths': xpi_paths,
        'configured_id': configured_id,
        'bundle_path': candidates.get('bundle_path') or '',
        'download_url': candidates.get('download_url') or '',
    }

def _firefox_extension_paths(profile_dir, marker_name, fallback_id):
    profile_dir = Path(profile_dir)
    extensions_dir = profile_dir / 'extensions'
    marker_path = extensions_dir / marker_name
    managed_addon_id = fallback_id
    if marker_path.exists():
        try:
            managed_addon_id = marker_path.read_text(encoding='utf-8').strip() or fallback_id
        except OSError:
            managed_addon_id = fallback_id
    target = extensions_dir / f'{managed_addon_id}.xpi'
    return extensions_dir, marker_path, managed_addon_id, target


def firefox_extension_installed(profile_dir, extension_name):
    if not profile_dir:
        return False
    managed = _managed_firefox_extension_paths(profile_dir, extension_name)
    profile_dir = Path(profile_dir)
    ids = set(managed['ids'])
    state_path = profile_dir / 'extensions.json'
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding='utf-8'))
            for addon in data.get('addons') or []:
                addon_id = str(addon.get('id') or '').strip()
                if addon_id in ids and addon.get('active') is True and addon.get('hidden') is not True:
                    return True
            # If Firefox has written a state file but the add-on is not active there,
            # do not treat a stale XPI alone as installed.
            return False
        except (OSError, ValueError, json.JSONDecodeError) as error:
            logger = None
    return any(path.exists() for path in managed['xpi_paths'])


def _resolve_bundled_extension_path(bundle_path):
    bundle_path = (bundle_path or '').strip()
    if not bundle_path:
        return None
    candidate = Path(bundle_path)
    if not candidate.is_absolute():
        candidate = Path(__file__).resolve().parent / candidate
    try:
        candidate = candidate.resolve()
    except OSError:
        return None
    if not candidate.exists() or not candidate.is_file():
        return None
    return candidate


def _xpi_has_signature(xpi_bytes):
    try:
        with zipfile.ZipFile(io.BytesIO(xpi_bytes)) as archive:
            names = {name.upper() for name in archive.namelist()}
    except (zipfile.BadZipFile, OSError):
        return False
    return any(name.startswith('META-INF/') for name in names)

def _load_firefox_extension_payload(managed, logger, extension_name):
    bundle_path = _resolve_bundled_extension_path(managed.get('bundle_path') or '')
    if bundle_path is not None:
        try:
            payload = bundle_path.read_bytes()
        except OSError as error:
            logger.warning('Failed to read bundled Firefox extension %s from %s: %s', extension_name, bundle_path, error)
            return None, f'bundle-read-error:{error}', False
        return payload, f'bundle:{bundle_path}', _xpi_has_signature(payload)
    download_url = (managed.get('download_url') or '').strip()
    if not download_url:
        return None, 'missing-extension-source', False
    try:
        request = urllib.request.Request(download_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read()
        return payload, download_url, _xpi_has_signature(payload)
    except (OSError, ValueError, urllib.error.URLError) as error:
        logger.warning('Failed to download Firefox extension %s from %s: %s', extension_name, download_url, error)
        return None, str(error), False


def _invalidate_firefox_extension_state(profile_dir, logger):
    profile_dir = Path(profile_dir)
    changed = False
    for state_file in (
        profile_dir / 'addonStartup.json.lz4',
        profile_dir / 'extensions.json',
        profile_dir / 'extension-preferences.json',
    ):
        try:
            if state_file.exists():
                state_file.unlink()
                changed = True
        except OSError as error:
            logger.warning('Failed to remove Firefox extension state file %s: %s', state_file, error)
    startup_cache_dir = profile_dir / 'startupCache'
    if startup_cache_dir.exists():
        try:
            shutil.rmtree(startup_cache_dir)
            changed = True
        except OSError as error:
            logger.warning('Failed to remove Firefox startup cache %s: %s', startup_cache_dir, error)
    return changed


def _sync_firefox_signed_extension(profile_dir, enabled, logger, extension_name):
    if not profile_dir:
        return {'requested': bool(enabled), 'installed': False, 'changed': False, 'error': 'missing-profile'}
    managed = _managed_firefox_extension_paths(profile_dir, extension_name)
    extensions_dir = managed['extensions_dir']
    extensions_dir.mkdir(parents=True, exist_ok=True)
    primary_marker_path = managed['primary_marker_path']
    configured_id = (managed['configured_id'] or '').strip()
    bundle_path = (managed.get('bundle_path') or '').strip()
    download_url = (managed.get('download_url') or '').strip()

    def cleanup_marker_paths(keep_text=''):
        keep_text = (keep_text or '').strip()
        for marker_path in managed['marker_paths']:
            try:
                if marker_path == primary_marker_path and keep_text:
                    marker_path.write_text(keep_text, encoding='utf-8')
                else:
                    marker_path.unlink(missing_ok=True)
            except OSError as error:
                logger.warning('Failed to update Firefox extension marker %s: %s', marker_path, error)

    def remove_existing():
        changed = False
        for target in managed['xpi_paths']:
            try:
                if target.exists():
                    target.unlink()
                    changed = True
            except OSError as error:
                logger.warning('Failed to remove Firefox extension %s: %s', target, error)
        for marker_path in managed['marker_paths']:
            try:
                if marker_path.exists():
                    marker_path.unlink()
                    changed = True
            except OSError as error:
                logger.warning('Failed to remove Firefox extension marker %s: %s', marker_path, error)
        changed = _invalidate_firefox_extension_state(profile_dir, logger) or changed
        return {'requested': False, 'installed': False, 'changed': changed, 'error': None}

    if not enabled:
        return remove_existing()

    existing_target = next((path for path in managed['xpi_paths'] if path.exists()), None)
    if existing_target is not None:
        existing_id = existing_target.stem
        cleanup_marker_paths(existing_id)
        _invalidate_firefox_extension_state(profile_dir, logger)
        return {'requested': True, 'installed': True, 'changed': False, 'error': None}

    if not configured_id:
        logger.warning('Missing Firefox extension ID for %s', extension_name)
        return {'requested': True, 'installed': False, 'changed': False, 'error': 'missing-addon-id'}
    if not bundle_path and not download_url:
        logger.warning('Missing Firefox extension source for %s', extension_name)
        return {'requested': True, 'installed': False, 'changed': False, 'error': 'missing-extension-source'}

    try:
        payload, payload_source, payload_signed = _load_firefox_extension_payload(managed, logger, extension_name)
        if payload is None:
            return {'requested': True, 'installed': False, 'changed': False, 'error': 'missing-extension-payload'}
        if not payload_signed:
            logger.warning('Firefox extension %s payload from %s does not appear to be Mozilla-signed; release Firefox builds usually block unsigned add-ons', extension_name, payload_source)
            return {'requested': True, 'installed': False, 'changed': False, 'error': 'unsigned-extension-payload'}
        detected_id = _extract_firefox_extension_id(payload, configured_id)
        install_id = configured_id
        if detected_id and detected_id != configured_id:
            logger.info('Firefox extension %s manifest ID %s differs from configured ID %s; installing under manifest ID', extension_name, detected_id, configured_id)
            install_id = detected_id
        target = extensions_dir / f'{install_id}.xpi'
        with tempfile.NamedTemporaryFile(dir=extensions_dir, delete=False) as tmp_file:
            tmp_file.write(payload)
            temp_name = tmp_file.name
        Path(temp_name).replace(target)
        refreshed = _managed_firefox_extension_paths(profile_dir, extension_name)
        for stale in refreshed['xpi_paths']:
            if stale == target:
                continue
            try:
                stale.unlink(missing_ok=True)
            except OSError as error:
                logger.warning('Failed to remove stale Firefox extension %s: %s', stale, error)
        cleanup_marker_paths(install_id)
        _invalidate_firefox_extension_state(profile_dir, logger)
        logger.info('Installed Firefox extension %s into %s from %s', configured_id, target, payload_source)
        return {'requested': True, 'installed': True, 'changed': True, 'error': None}
    except (OSError, ValueError, zipfile.BadZipFile, urllib.error.URLError) as error:
        logger.warning('Failed to install Firefox extension %s from %s: %s', extension_name, bundle_path or download_url, error)
        return {'requested': True, 'installed': False, 'changed': False, 'error': str(error)}

def _sync_firefox_swipe_extension(profile_dir, enabled, logger):
    return _sync_firefox_signed_extension(profile_dir, enabled, logger, 'swipe')


def _sync_firefox_adblock(profile_dir, enabled, logger):
    return _sync_firefox_signed_extension(profile_dir, enabled, logger, 'adblock')

def _sync_firefox_app_mode_css(profile_dir, enabled, frameless, kiosk, logger):
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
    mode_name = 'kiosk' if kiosk else ('seamless' if frameless else 'app')
    if frameless or kiosk:
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
    frameless = mode_name in {'seamless', 'kiosk'}
    app_mode_enabled = mode_name in {'app', 'seamless', 'kiosk'}
    privacy_enabled = bool(
        prefs.get('toolkit.telemetry.enabled') is False
        or prefs.get('privacy.globalprivacycontrol.enabled') is True
        or prefs.get('privacy.donottrackheader.enabled') is True
        or prefs.get('datareporting.healthreport.uploadEnabled') is False
        or prefs.get('datareporting.usage.uploadEnabled') is False
    )
    only_https_enabled = bool(prefs.get('dom.security.https_only_mode')) or privacy_enabled
    return {
        OPTION_CLEAR_CACHE_ON_EXIT_KEY: '1' if prefs.get('privacy.clearOnShutdown.cache') or prefs.get('privacy.clearOnShutdown_v2.cache') else '0',
        OPTION_CLEAR_COOKIES_ON_EXIT_KEY: '1' if prefs.get('privacy.clearOnShutdown.cookies') or prefs.get('privacy.clearOnShutdown_v2.cookiesAndStorage') else '0',
        OPTION_ADBLOCK_KEY: '1' if adblock else '0',
        OPTION_PRESERVE_SESSION_KEY: '1' if prefs.get('browser.startup.page') == 3 else '0',
        OPTION_NOTIFICATIONS_KEY: '1' if prefs.get('permissions.default.desktop-notification') == 1 else '0',
        OPTION_SWIPE_KEY: '1' if swipe or bool(prefs.get('browser.gesture.swipe.left')) else '0',
        ONLY_HTTPS_KEY: '1' if only_https_enabled else '0',
        OPTION_KEEP_IN_BACKGROUND_KEY: '1' if (prefs.get('furi.browser.preload.disabled') is False or ('furi.browser.preload.disabled' not in prefs and prefs.get('browser.tabs.closeWindowWithLastTab') is False)) else '0',
        OPTION_DISABLE_AI_KEY: '1' if (prefs.get('browser.ml.chat.enabled') is False or prefs.get('browser.tabs.groups.smart.enabled') is False or prefs.get('browser.ml.linkPreview.enabled') is False) else '0',
        OPTION_FORCE_PRIVACY_KEY: '1' if privacy_enabled else '0',
        OPTION_STARTUP_BOOSTER_KEY: '1' if prefs.get('webapp.startup_booster.enabled') is True else '0',
        APP_MODE_KEY: '1' if app_mode_enabled or prefs.get('toolkit.legacyUserProfileCustomizations.stylesheets') else '0',
        'Frameless': '1' if frameless else '0',
        USER_AGENT_VALUE_KEY: prefs.get('general.useragent.override', '') or '',
        COLOR_SCHEME_KEY: {0: 'dark', 1: 'light', 2: 'auto', 3: 'auto'}.get(prefs.get('layout.css.prefers-color-scheme.content-override'), 'auto'),
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
        OPTION_CLEAR_CACHE_ON_EXIT_KEY: '1' if 'cached_images_and_files' in clear_on_exit else '0',
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
    }

def read_profile_settings(profile_path, browser_family):
    if not profile_path:
        return {}
    if browser_family == 'firefox':
        return _read_firefox_profile_settings(profile_path)
    if browser_family in {'chrome', 'chromium'}:
        return _read_chromium_profile_settings(profile_path)
    return {}

def apply_profile_settings(profile_info, options_dict, logger):
    if not profile_info:
        return
    family = profile_info.get('browser_family')
    scoped_options = project_options_for_family(normalize_option_dict(options_dict or {}), family)
    profile_path = profile_info.get('profile_path')
    clear_cache = scoped_options.get(OPTION_CLEAR_CACHE_ON_EXIT_KEY, '0') == '1'
    clear_cookies = scoped_options.get(OPTION_CLEAR_COOKIES_ON_EXIT_KEY, '0') == '1'
    adblock = scoped_options.get(OPTION_ADBLOCK_KEY, '0') == '1'
    previous_session = scoped_options.get(OPTION_PRESERVE_SESSION_KEY, '0') == '1'
    notifications_enabled = scoped_options.get(OPTION_NOTIFICATIONS_KEY, '0') == '1'
    swipe_enabled = scoped_options.get(OPTION_SWIPE_KEY, '0') == '1'
    user_agent_value = (scoped_options.get(USER_AGENT_VALUE_KEY, '') or '').strip()
    only_https = scoped_options.get(ONLY_HTTPS_KEY, '0') == '1'
    keep_in_background = scoped_options.get(OPTION_KEEP_IN_BACKGROUND_KEY, '0') == '1'
    app_mode = scoped_options.get(APP_MODE_KEY, '0') == '1'
    frameless = scoped_options.get('Frameless', '0') == '1'
    kiosk = scoped_options.get('Kiosk', '0') == '1'
    disable_ai = scoped_options.get(OPTION_DISABLE_AI_KEY, '0') == '1'
    set_privacy = scoped_options.get(OPTION_FORCE_PRIVACY_KEY, '0') == '1'
    startup_booster = scoped_options.get(OPTION_STARTUP_BOOSTER_KEY, '0') == '1'
    color_scheme = normalize_color_scheme(scoped_options.get(COLOR_SCHEME_KEY, 'auto'))
    custom_css_enabled = bool(linked_assets_for_options(options_dict, 'css') or inline_asset_text_for_options(options_dict, 'css'))
    custom_js_enabled = bool(linked_assets_for_options(options_dict, 'javascript') or inline_asset_text_for_options(options_dict, 'javascript'))
    if family == 'firefox' and profile_path:
        if set_privacy:
            only_https = True
        _write_firefox_user_js(
            profile_path,
            clear_cache,
            clear_cookies,
            previous_session,
            user_agent_value=user_agent_value,
            only_https=only_https,
            notifications_enabled=notifications_enabled,
            swipe_enabled=swipe_enabled,
            keep_in_background=keep_in_background,
            startup_url=(options_dict.get(ADDRESS_KEY, '') or '').strip(),
            app_mode=(app_mode or kiosk),
            native_window_frame=(app_mode and not frameless and not kiosk),
            disable_ai=disable_ai,
            set_privacy=set_privacy,
            color_scheme=color_scheme,
            custom_css_enabled=custom_css_enabled,
            custom_js_enabled=custom_js_enabled,
            startup_booster=startup_booster,
        )
        _sync_firefox_app_mode_css(profile_path, app_mode or kiosk, frameless, kiosk, logger)
        _sync_firefox_adblock(profile_path, adblock, logger)
        _sync_firefox_swipe_extension(profile_path, swipe_enabled, logger)
        ensure_profile_customizations(profile_info, options_dict, logger)
        _invalidate_firefox_extension_state(profile_path, logger)
        return
    if family in {'chrome', 'chromium'} and profile_path:
        _write_chromium_preferences(
            profile_path,
            clear_cache,
            clear_cookies,
            previous_session,
            logger,
            user_agent_value=user_agent_value,
            only_https=only_https,
            notifications_enabled=notifications_enabled,
            keep_in_background=keep_in_background,
            disable_ai=disable_ai,
            set_privacy=set_privacy,
            color_scheme=color_scheme,
            startup_booster=startup_booster,
        )
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

def _safe_remove_tree(path, allowed_root, logger):
    try:
        resolved = Path(path).resolve()
    except OSError:
        return
    if not resolved.exists():
        return
    if resolved == allowed_root.resolve() or allowed_root.resolve() not in resolved.parents:
        logger.warning('Refusing to delete profile path outside managed root: %s', resolved)
        return
    shutil.rmtree(resolved, ignore_errors=False)
    logger.info('Deleted managed profile directory %s', resolved)

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

def _path_within(path, root):
    try:
        path = Path(path).resolve()
        root = Path(root).resolve()
    except OSError:
        return False
    return path == root or root in path.parents

def _is_managed_profile_path(path, family):
    if not path:
        return False
    root = FIREFOX_ROOT if family == 'firefox' else CHROMIUM_PROFILE_ROOT
    return _path_within(path, root)

def _detect_managed_profile_family(path):
    if not path:
        return None
    try:
        resolved = Path(path).resolve()
    except OSError:
        return None
    if _path_within(resolved, FIREFOX_ROOT):
        return 'firefox'
    if _path_within(resolved, CHROMIUM_PROFILE_ROOT / 'chrome'):
        return 'chrome'
    if _path_within(resolved, CHROMIUM_PROFILE_ROOT / 'chromium'):
        return 'chromium'
    return None

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

    def should_skip(profile_dir):
        name = profile_dir.name
        if not name.startswith('webapp_'):
            return True
        if '_unused' in name:
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
            if should_skip(resolved):
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
    managed_existing = bool(stored_path and _is_managed_profile_path(stored_path, family))
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
        _upsert_firefox_profile(profile_name, profile_dir, logger)
        return {
            'browser_family': family,
            'profile_name': profile_name,
            'profile_path': str(profile_dir),
            'exec_args': ['--profile', str(profile_dir), '--new-instance'],
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
        return {
            'browser_family': family,
            'profile_name': profile_name,
            'profile_path': str(profile_dir),
            'exec_args': [f'--user-data-dir={profile_dir}'],
            'profile_migrated': profile_migrated,
        }
    return {
        'browser_family': family,
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
        is_firefox_profile = profile_dir == FIREFOX_ROOT or FIREFOX_ROOT.resolve() in profile_dir.parents
        profile_name = stored_profile_name or profile_dir.name
        if is_firefox_profile:
            _remove_firefox_profile_registration(profile_name, profile_dir, logger)
            if profile_dir.exists():
                try:
                    _safe_remove_tree(profile_dir, FIREFOX_ROOT, logger)
                except OSError as error:
                    logger.error('Failed to delete Firefox profile %s: %s', profile_dir, error)
        else:
            if profile_dir.exists():
                try:
                    _safe_remove_tree(profile_dir, CHROMIUM_PROFILE_ROOT, logger)
                except OSError as error:
                    logger.error('Failed to delete browser profile %s: %s', profile_dir, error)

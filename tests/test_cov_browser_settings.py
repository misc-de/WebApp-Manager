"""Coverage tests for browser_settings: runtime-cache clearing, the Firefox
user.js and Chromium Preferences writers, the app-mode userChrome.css block
and the round-trip readers.

All profiles live in temporary directories; `is_furios_distribution` is pinned
so the output does not depend on the machine the suite runs on.
"""
import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_browser_settings.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import browser_settings
from browser_settings import ProfileSettings
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


class _TempProfileMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.profile = Path(self._tmp.name) / 'profile'
        self.profile.mkdir()
        self.logger = _build_test_logger(self.id())
        patcher = mock.patch.object(browser_settings, 'is_furios_distribution', return_value=False)
        self.furios = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(browser_settings, 'firefox_extension_installed', return_value=False)
        self.extension_installed = patcher.start()
        self.addCleanup(patcher.stop)

    def _user_prefs(self):
        prefs = {}
        for line in (self.profile / 'user.js').read_text(encoding='utf-8').splitlines():
            if line.startswith('user_pref("'):
                key, _, value = line[len('user_pref("'):].partition('", ')
                prefs[key] = value[:-2]
        return prefs


class RuntimeCacheTests(_TempProfileMixin, unittest.TestCase):
    def test_firefox_caches_are_removed_and_reported(self):
        for name in ('cache2', 'startupCache', 'thumbnails'):
            (self.profile / name / 'x').mkdir(parents=True)
        (self.profile / 'prefs.js').write_text('keep', encoding='utf-8')
        self.assertTrue(browser_settings._clear_firefox_runtime_caches(self.profile, self.logger))
        self.assertEqual(sorted(p.name for p in self.profile.iterdir()), ['prefs.js'])
        self.assertFalse(browser_settings._clear_firefox_runtime_caches(self.profile, self.logger))

    def test_chromium_caches_are_removed_and_reported(self):
        (self.profile / 'Default' / 'GPUCache').mkdir(parents=True)
        (self.profile / 'ShaderCache').mkdir()
        (self.profile / 'Default' / 'Preferences').write_text('{}', encoding='utf-8')
        self.assertTrue(browser_settings._clear_chromium_runtime_caches(self.profile, self.logger))
        self.assertFalse((self.profile / 'Default' / 'GPUCache').exists())
        self.assertFalse((self.profile / 'ShaderCache').exists())
        self.assertTrue((self.profile / 'Default' / 'Preferences').exists())
        self.assertFalse(browser_settings._clear_chromium_runtime_caches(self.profile, self.logger))


class FirefoxUserJsTests(_TempProfileMixin, unittest.TestCase):
    def test_dark_scheme_privacy_booster_and_user_agent(self):
        settings = ProfileSettings(
            color_scheme='dark',
            set_privacy=True,
            startup_booster=True,
            previous_session=True,
            user_agent_value='Custom/1.0',
        )
        browser_settings._write_firefox_user_js(self.profile, settings)
        prefs = self._user_prefs()
        self.assertEqual(prefs['ui.systemUsesDarkTheme'], '1')
        self.assertEqual(prefs['layout.css.prefers-color-scheme.content-override'], '0')
        self.assertEqual(prefs['toolkit.telemetry.enabled'], 'false')
        self.assertEqual(prefs['dom.security.https_only_mode'], 'true')
        self.assertEqual(prefs['browser.newtab.preload'], 'false')
        self.assertEqual(prefs['browser.sessionstore.restore_on_demand'], 'true')
        self.assertEqual(prefs['general.useragent.override'], '"Custom/1.0"')
        self.assertEqual(prefs['browser.startup.page'], '3')

    def test_light_scheme_sets_light_system_theme(self):
        browser_settings._write_firefox_user_js(self.profile, ProfileSettings(color_scheme='light'))
        prefs = self._user_prefs()
        self.assertEqual(prefs['ui.systemUsesDarkTheme'], '0')
        self.assertEqual(prefs['general.useragent.override'], '""')

    def test_furios_and_safe_graphics_disable_gpu_paths(self):
        self.furios.return_value = True
        browser_settings._write_firefox_user_js(self.profile, ProfileSettings(keep_in_background=True, safe_graphics=True))
        prefs = self._user_prefs()
        self.assertEqual(prefs['furi.browser.preload.disabled'], 'false')
        self.assertEqual(prefs['gfx.webrender.all'], 'false')
        self.assertEqual(prefs['webgl.disabled'], 'true')

    def test_foreign_lines_are_kept_and_managed_block_is_replaced(self):
        user_js = self.profile / 'user.js'
        user_js.write_text(
            'user_pref("my.own", 1);\n'
            '// WEBAPP MANAGED START\nuser_pref("stale", true);\n// WEBAPP MANAGED END\n',
            encoding='utf-8',
        )
        browser_settings._write_firefox_user_js(self.profile, ProfileSettings())
        content = user_js.read_text(encoding='utf-8')
        self.assertTrue(content.startswith('user_pref("my.own", 1);\n// WEBAPP MANAGED START\n'))
        self.assertNotIn('stale', content)
        self.assertEqual(content.count('// WEBAPP MANAGED START'), 1)

    def test_unchanged_content_is_not_rewritten(self):
        browser_settings._write_firefox_user_js(self.profile, ProfileSettings())
        with mock.patch.object(Path, 'write_text') as write_text:
            browser_settings._write_firefox_user_js(self.profile, ProfileSettings())
        write_text.assert_not_called()

    def test_second_read_failure_still_writes(self):
        browser_settings._write_firefox_user_js(self.profile, ProfileSettings())
        original = Path.read_text
        calls = {'n': 0}

        def flaky_read(path, *args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 2:
                raise OSError('gone')
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, 'read_text', flaky_read):
            browser_settings._write_firefox_user_js(self.profile, ProfileSettings(clear_cookies=True))
        self.assertEqual(self._user_prefs()['privacy.clearOnShutdown.cookies'], 'true')


class ChromiumPreferencesTests(_TempProfileMixin, unittest.TestCase):
    def _prefs(self):
        return json.loads((self.profile / 'Default' / 'Preferences').read_text(encoding='utf-8'))

    def test_privacy_booster_and_clear_on_exit(self):
        settings = ProfileSettings(
            clear_cache=True,
            clear_cookies=True,
            set_privacy=True,
            startup_booster=True,
            disable_ai=True,
            notifications_enabled=True,
            keep_in_background=True,
            user_agent_value='UA/2',
            color_scheme='DARK',
            default_zoom='125',
        )
        browser_settings._write_chromium_preferences(self.profile, settings, self.logger)
        data = self._prefs()
        self.assertEqual(data['browser']['clear_data']['clear_on_exit'], ['cookies_and_other_site_data', 'cached_images_and_files'])
        self.assertTrue(data['browser']['first_run_finished'])
        self.assertFalse(data['show-welcome-page'])
        self.assertFalse(data['sync_promo']['show_on_first_run_allowed'])
        self.assertEqual(data['session']['restore_on_startup'], 5)
        self.assertTrue(data['profile']['block_third_party_cookies'])
        self.assertEqual(data['profile']['default_content_setting_values']['notifications'], 1)
        self.assertTrue(data['https_only_mode_enabled'])
        self.assertTrue(data['background_mode']['enabled'])
        self.assertFalse(data['search']['suggest_enabled'])
        self.assertFalse(data['translate']['enabled'])
        self.assertEqual(data['dns_over_https']['mode'], 'off')
        self.assertEqual(data['default_search_provider']['name'], 'DuckDuckGo')
        self.assertEqual(data['webapp_manager']['user_agent_override'], 'UA/2')
        self.assertEqual(data['webapp_manager']['color_scheme'], 'dark')
        self.assertEqual(data['webapp_manager']['default_zoom'], '125')
        self.assertFalse(data['safebrowsing']['enabled'])
        self.assertFalse(data['optimization_guide']['model_execution_enabled'])
        self.assertEqual(data['browser_labs']['enabled_labs_experiments'], [])
        self.assertTrue(data['enable_do_not_track'])

    def test_disabling_booster_removes_its_keys_and_keeps_foreign_prefs(self):
        default_dir = self.profile / 'Default'
        default_dir.mkdir()
        (default_dir / 'Preferences').write_text(json.dumps({
            'browser': {'first_run_finished': True, 'has_seen_welcome_page': True},
            'show-welcome-page': False,
            'sync_promo': {'show_on_first_run_allowed': False},
            'webapp_manager': {'user_agent_override': 'old'},
            'extensions': {'foo': 1},
            'safebrowsing': {'enabled': False},
        }), encoding='utf-8')
        browser_settings._write_chromium_preferences(self.profile, ProfileSettings(previous_session=True), self.logger)
        data = self._prefs()
        self.assertNotIn('first_run_finished', data['browser'])
        self.assertNotIn('show-welcome-page', data)
        self.assertNotIn('sync_promo', data)
        self.assertNotIn('user_agent_override', data['webapp_manager'])
        self.assertEqual(data['extensions'], {'foo': 1})
        self.assertFalse(data['safebrowsing']['enabled'])
        self.assertEqual(data['session']['restore_on_startup'], 1)
        self.assertEqual(data['profile']['default_content_setting_values']['notifications'], 3)

    def test_non_empty_sync_promo_is_kept(self):
        default_dir = self.profile / 'Default'
        default_dir.mkdir()
        (default_dir / 'Preferences').write_text(json.dumps({'sync_promo': {'show_on_first_run_allowed': False, 'other': 1}}), encoding='utf-8')
        browser_settings._write_chromium_preferences(self.profile, ProfileSettings(), self.logger)
        self.assertEqual(self._prefs()['sync_promo'], {'other': 1})

    def test_corrupt_preferences_are_replaced_with_a_warning(self):
        default_dir = self.profile / 'Default'
        default_dir.mkdir()
        (default_dir / 'Preferences').write_text('{broken', encoding='utf-8')
        logger = mock.Mock()
        browser_settings._write_chromium_preferences(self.profile, ProfileSettings(), logger)
        logger.warning.assert_called_once()
        self.assertEqual(self._prefs()['session']['restore_on_startup'], 5)


class AppModeCssTests(_TempProfileMixin, unittest.TestCase):
    def _css(self):
        return self.profile / 'chrome' / 'userChrome.css'

    def test_app_mode_writes_block_and_keeps_user_css(self):
        self._css().parent.mkdir()
        self._css().write_text('/* mine */\n', encoding='utf-8')
        browser_settings._sync_firefox_app_mode_css(self.profile, True, False, self.logger)
        content = self._css().read_text(encoding='utf-8')
        self.assertTrue(content.startswith('/* mine */\n\n' + browser_settings.FIREFOX_APP_MODE_START))
        self.assertIn('/* WEBAPP MODE: app */', content)

    def test_seamless_mode_replaces_previous_block(self):
        browser_settings._sync_firefox_app_mode_css(self.profile, True, False, self.logger)
        browser_settings._sync_firefox_app_mode_css(self.profile, True, True, self.logger)
        content = self._css().read_text(encoding='utf-8')
        self.assertTrue(content.startswith(browser_settings.FIREFOX_APP_MODE_START))
        self.assertIn('/* WEBAPP MODE: seamless */', content)
        self.assertNotIn('/* WEBAPP MODE: app */', content)
        self.assertEqual(content.count(browser_settings.FIREFOX_APP_MODE_START), 1)

    def test_disabling_keeps_user_css_only(self):
        self._css().parent.mkdir()
        self._css().write_text('/* mine */\n', encoding='utf-8')
        browser_settings._sync_firefox_app_mode_css(self.profile, True, False, self.logger)
        browser_settings._sync_firefox_app_mode_css(self.profile, False, False, self.logger)
        self.assertEqual(self._css().read_text(encoding='utf-8'), '/* mine */\n')

    def test_disabling_removes_a_file_that_only_held_the_block(self):
        browser_settings._sync_firefox_app_mode_css(self.profile, True, True, self.logger)
        browser_settings._sync_firefox_app_mode_css(self.profile, False, False, self.logger)
        self.assertFalse(self._css().exists())

    def test_disabling_without_a_file_does_nothing(self):
        browser_settings._sync_firefox_app_mode_css(self.profile, False, False, self.logger)
        self.assertFalse((self.profile / 'chrome').exists())

    def test_read_and_unlink_failures_are_logged(self):
        browser_settings._sync_firefox_app_mode_css(self.profile, True, False, self.logger)
        logger = mock.Mock()
        with mock.patch.object(Path, 'read_text', side_effect=OSError('denied')), \
                mock.patch.object(Path, 'unlink', side_effect=OSError('busy')):
            browser_settings._sync_firefox_app_mode_css(self.profile, False, False, logger)
        self.assertEqual(logger.warning.call_count, 2)
        self.assertTrue(self._css().exists())


class ReadProfileSettingsTests(_TempProfileMixin, unittest.TestCase):
    def test_dispatch_by_family(self):
        self.assertEqual(browser_settings.read_profile_settings('', 'firefox'), {})
        self.assertEqual(browser_settings.read_profile_settings(str(self.profile), 'opera'), {})
        self.assertEqual(browser_settings.read_profile_settings(str(self.profile), 'chrome'), {})
        self.assertEqual(browser_settings.read_profile_settings(str(self.profile), 'firefox')[COLOR_SCHEME_KEY], 'auto')

    def test_firefox_round_trip(self):
        self.furios.return_value = True
        settings = ProfileSettings(
            clear_cache=True,
            clear_cookies=True,
            previous_session=False,
            user_agent_value='UA/3',
            notifications_enabled=True,
            open_links_in_tabs=True,
            keep_in_background=True,
            swipe_enabled=True,
            disable_ai=True,
            set_privacy=True,
            startup_booster=True,
            safe_graphics=True,
            color_scheme='light',
        )
        browser_settings._write_firefox_user_js(self.profile, settings)
        browser_settings._sync_firefox_app_mode_css(self.profile, True, True, self.logger)
        self.extension_installed.side_effect = lambda _profile, name: name == 'adblock'
        result = browser_settings.read_profile_settings(str(self.profile), 'firefox')
        for key in (
            OPTION_CLEAR_CACHE_ON_EXIT_KEY, OPTION_CLEAR_COOKIES_ON_EXIT_KEY, OPTION_ADBLOCK_KEY,
            OPTION_NOTIFICATIONS_KEY, OPTION_OPEN_LINKS_IN_TABS_KEY, OPTION_SWIPE_KEY, ONLY_HTTPS_KEY,
            OPTION_KEEP_IN_BACKGROUND_KEY, OPTION_DISABLE_AI_KEY, OPTION_FORCE_PRIVACY_KEY,
            OPTION_STARTUP_BOOSTER_KEY, OPTION_SAFE_GRAPHICS_KEY, APP_MODE_KEY, 'Frameless',
        ):
            self.assertEqual(result[key], '1', key)
        self.assertEqual(result[OPTION_PRESERVE_SESSION_KEY], '0')
        self.assertEqual(result[USER_AGENT_VALUE_KEY], 'UA/3')
        self.assertEqual(result[COLOR_SCHEME_KEY], 'light')

    def test_firefox_reader_tolerates_odd_values_and_unreadable_css(self):
        (self.profile / 'user.js').write_text(
            'garbage line\n'
            'user_pref("general.useragent.override", "Bare);\n'
            'user_pref("browser.tabs.closeWindowWithLastTab", false);\n',
            encoding='utf-8',
        )
        (self.profile / 'chrome').mkdir()
        (self.profile / 'chrome' / 'userChrome.css').write_text('/* WEBAPP MODE: app */', encoding='utf-8')
        original = Path.read_text

        def fail_on_css(path, *args, **kwargs):
            if path.name == 'userChrome.css':
                raise OSError('denied')
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, 'read_text', fail_on_css):
            result = browser_settings.read_profile_settings(str(self.profile), 'firefox')
        self.assertEqual(result[USER_AGENT_VALUE_KEY], 'Bare')
        self.assertEqual(result[OPTION_KEEP_IN_BACKGROUND_KEY], '1')
        self.assertEqual(result[APP_MODE_KEY], '0')

    def test_chromium_round_trip(self):
        settings = ProfileSettings(
            clear_cache=True,
            clear_cookies=True,
            previous_session=True,
            notifications_enabled=True,
            only_https=True,
            keep_in_background=True,
            disable_ai=True,
            set_privacy=True,
            startup_booster=True,
            user_agent_value='UA/4',
            color_scheme='dark',
            default_zoom='150',
        )
        browser_settings._write_chromium_preferences(self.profile, settings, self.logger)
        result = browser_settings.read_profile_settings(str(self.profile), 'chromium')
        for key in (
            OPTION_CLEAR_CACHE_ON_EXIT_KEY, OPTION_CLEAR_COOKIES_ON_EXIT_KEY, OPTION_PRESERVE_SESSION_KEY,
            OPTION_NOTIFICATIONS_KEY, ONLY_HTTPS_KEY, OPTION_KEEP_IN_BACKGROUND_KEY, OPTION_DISABLE_AI_KEY,
            OPTION_FORCE_PRIVACY_KEY, OPTION_STARTUP_BOOSTER_KEY,
        ):
            self.assertEqual(result[key], '1', key)
        self.assertEqual(result[OPTION_ADBLOCK_KEY], '0')
        self.assertEqual(result[USER_AGENT_VALUE_KEY], 'UA/4')
        self.assertEqual(result[COLOR_SCHEME_KEY], 'dark')
        self.assertEqual(result[DEFAULT_ZOOM_KEY], '150')

    def test_chromium_reader_rejects_corrupt_preferences(self):
        (self.profile / 'Default').mkdir()
        (self.profile / 'Default' / 'Preferences').write_text('[not an object', encoding='utf-8')
        self.assertEqual(browser_settings.read_profile_settings(str(self.profile), 'chrome'), {})


if __name__ == '__main__':
    unittest.main()

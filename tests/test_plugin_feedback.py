"""Tests for the plugin-install feedback path.

An add-on that cannot be installed is reported as a *result* by
`_sync_firefox_signed_extension`, not raised. Before, apply_profile_settings
dropped that result, so the specific messages ("the add-on is unsigned", "no
download source is configured") could never reach the user -- the detail page
only ever compared against exception text. These tests pin the chain that now
carries the reason from the extension sync up to the banner.
"""
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.plugin_feedback.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

from browser_profiles import _extension_errors_from_results, apply_profile_settings
from detail_page import DetailPage
from webapp_constants import ADDRESS_KEY, OPTION_SWIPE_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY


class ExtensionErrorCollectionTests(unittest.TestCase):
    def test_only_failures_are_reported(self):
        errors = _extension_errors_from_results(
            adblock={'requested': True, 'installed': True, 'changed': True, 'error': None},
            swipe={'requested': True, 'installed': False, 'changed': False, 'error': 'unsigned-extension-payload'},
        )
        self.assertEqual(errors, {'swipe': 'unsigned-extension-payload'})

    def test_non_dict_results_are_ignored(self):
        self.assertEqual(_extension_errors_from_results(adblock=None, swipe='nonsense'), {})


class ApplyProfileSettingsResultTests(unittest.TestCase):
    def test_missing_profile_still_returns_the_result_shape(self):
        self.assertEqual(apply_profile_settings(None, {}, _build_test_logger('none')), {'extension_errors': {}})

    def test_firefox_extension_error_is_forwarded_to_the_caller(self):
        logger = _build_test_logger('forwarded')
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir) / 'webapp_testprofile'
            profile_dir.mkdir(parents=True)
            profile_info = {'browser_family': 'firefox', 'profile_path': str(profile_dir)}
            failure = {'requested': True, 'installed': False, 'changed': False, 'error': 'unsigned-extension-payload'}
            success = {'requested': False, 'installed': False, 'changed': False, 'error': None}

            with mock.patch('browser_profiles._is_explicitly_managed_profile_dir', return_value=True), \
                mock.patch('browser_profiles._write_firefox_user_js'), \
                mock.patch('browser_profiles._sync_firefox_app_mode_css'), \
                mock.patch('browser_profiles.ensure_profile_customizations'), \
                mock.patch('browser_profiles._invalidate_firefox_extension_state'), \
                mock.patch('browser_profiles._sync_firefox_adblock', return_value=success), \
                mock.patch('browser_profiles._sync_firefox_swipe_extension', return_value=failure):
                result = apply_profile_settings(profile_info, {OPTION_SWIPE_KEY: '1'}, logger)

        self.assertEqual(result['extension_errors'], {'swipe': 'unsigned-extension-payload'})

    def test_chromium_profile_reports_no_extension_errors(self):
        logger = _build_test_logger('chromium')
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir) / 'chromium-profile'
            profile_dir.mkdir(parents=True)
            profile_info = {'browser_family': 'chromium', 'profile_path': str(profile_dir)}
            with mock.patch('browser_profiles._is_explicitly_managed_profile_dir', return_value=True), \
                mock.patch('browser_profiles._write_chromium_preferences'), \
                mock.patch('browser_profiles.ensure_profile_customizations'):
                result = apply_profile_settings(profile_info, {}, logger)

        self.assertEqual(result, {'extension_errors': {}})


class PluginBannerSelectionTests(unittest.TestCase):
    """_plugin_result_banner is a pure static method, so it is testable without
    constructing a GTK widget."""

    def test_unsigned_payload_uses_the_swipe_specific_message(self):
        key, timeout = DetailPage._plugin_result_banner('unsigned-extension-payload', 'swipe', True, False)
        self.assertEqual(key, 'plugin_install_unsigned_swipe')
        self.assertEqual(timeout, 4200)

    def test_unsigned_payload_uses_the_generic_message_for_other_addons(self):
        key, _timeout = DetailPage._plugin_result_banner('unsigned-extension-payload', 'adblock', True, False)
        self.assertEqual(key, 'plugin_install_unsigned')

    def test_missing_source_is_swipe_specific(self):
        key, timeout = DetailPage._plugin_result_banner('missing-extension-source', 'swipe', True, False)
        self.assertEqual(key, 'plugin_install_swipe_production_unavailable')
        self.assertEqual(timeout, 4200)
        # For any other add-on the same code falls through to the generic error.
        other, _ = DetailPage._plugin_result_banner('missing-extension-source', 'adblock', True, False)
        self.assertEqual(other, 'plugin_install_failed')

    def test_success_and_removal_messages(self):
        self.assertEqual(DetailPage._plugin_result_banner('', 'swipe', True, True)[0], 'plugin_install_ready_restart')
        self.assertEqual(DetailPage._plugin_result_banner('', 'swipe', False, False)[0], 'plugin_remove_ready_restart')
        self.assertEqual(DetailPage._plugin_result_banner('', 'swipe', True, False)[0], 'plugin_install_failed')

    def test_stale_removal_state_shows_nothing(self):
        # Switch off but the add-on is still present: no message rather than a
        # misleading success banner.
        self.assertEqual(DetailPage._plugin_result_banner('', 'swipe', False, True), (None, 0))

    def test_every_banner_key_exists_in_the_reference_language(self):
        import json
        reference = json.loads(Path('lang/en.json').read_text(encoding='utf-8'))
        cases = [
            ('unsigned-extension-payload', 'swipe', True, False),
            ('unsigned-extension-payload', 'adblock', True, False),
            ('missing-extension-source', 'swipe', True, False),
            ('boom', 'swipe', True, False),
            ('', 'swipe', True, True),
            ('', 'swipe', False, False),
        ]
        for error_text, option_name, enabled, installed in cases:
            key, _timeout = DetailPage._plugin_result_banner(error_text, option_name, enabled, installed)
            with self.subTest(error_text=error_text, option_name=option_name):
                self.assertIn(key, reference)


if __name__ == '__main__':
    unittest.main()


class _StubSwitch:
    def __init__(self):
        self.sensitive = False

    def set_sensitive(self, value):
        self.sensitive = bool(value)


class _StubEntry:
    id = 7
    title = 'Test App'


class _StubAddressEntry:
    def __init__(self, text=''):
        self._text = text

    def get_text(self):
        return self._text


class _StubDetailPage:
    """Minimal stand-in for DetailPage.

    It borrows the real methods under test and supplies hand-written versions
    of everything they call back into, which lets the UI-facing logic be
    exercised without constructing a GTK widget tree.
    """

    _plugin_option_updates = DetailPage._plugin_option_updates
    _plugin_export_needed = DetailPage._plugin_export_needed
    _verified_plugin_state = DetailPage._verified_plugin_state
    # staticmethod() is required: plain assignment would rebind it as an
    # instance method and pass `self` as the first argument.
    _plugin_result_banner = staticmethod(DetailPage._plugin_result_banner)
    _finish_plugin_save = DetailPage._finish_plugin_save

    def __init__(self, option_values=None):
        self.entry = _StubEntry()
        self.address_entry = _StubAddressEntry()
        self.switches = {'a': _StubSwitch(), 'b': _StubSwitch()}
        self.on_title_changed = None
        self._plugin_operation_serial = 3
        self._plugin_operation_in_progress = True
        self._option_values = dict(option_values or {})
        self.added_options = []
        self.banners = []
        self.inline_busy = True
        self.activity = ('busy', True)

    # --- collaborators the methods under test call back into ---------------
    def _get_option_value(self, key):
        return self._option_values.get(key, '')

    def _add_options(self, updates):
        self.added_options.append(dict(updates))
        self._option_values.update(updates)

    def _reload_options_cache_from_db(self):
        pass

    def _apply_option_values_to_controls(self):
        pass

    def _visible_option_names_in_order(self):
        return ['a', 'b']

    def _get_current_engine(self):
        return {'command': 'firefox'}

    def _set_inline_busy(self, busy, *args):
        self.inline_busy = busy

    def _set_plugin_activity(self, text, active=False):
        self.activity = (text, active)

    def _show_plugin_banner(self, text, timeout_ms=None):
        self.banners.append((text, timeout_ms))

    def _emit_visual_changed(self):
        pass


class PluginOptionUpdateTests(unittest.TestCase):
    def test_profile_and_address_updates_are_collected(self):
        page = _StubDetailPage()
        page.address_entry = _StubAddressEntry('https://old.example')
        updates = page._plugin_option_updates(
            {'profile_name': 'webapp_test', 'profile_path': '/tmp/webapp_test'},
            {'normalized_address': 'https://new.example'},
        )
        self.assertEqual(updates[PROFILE_NAME_KEY], 'webapp_test')
        self.assertEqual(updates[PROFILE_PATH_KEY], '/tmp/webapp_test')
        self.assertEqual(updates[ADDRESS_KEY], 'https://new.example')

    def test_unchanged_address_is_not_written_back(self):
        page = _StubDetailPage()
        page.address_entry = _StubAddressEntry('https://same.example')
        updates = page._plugin_option_updates(None, {'normalized_address': 'https://same.example'})
        self.assertEqual(updates, {})

    def test_export_is_needed_when_the_profile_path_changed(self):
        page = _StubDetailPage()
        self.assertTrue(page._plugin_export_needed({'profile_path': '/tmp/new'}, '/tmp/old'))


class FinishPluginSaveTests(unittest.TestCase):
    def test_stale_serial_is_ignored(self):
        page = _StubDetailPage()
        result = page._finish_plugin_save(1, 'Swipe', 'swipe', None, None, '')
        self.assertFalse(result)
        self.assertTrue(page._plugin_operation_in_progress)
        self.assertEqual(page.banners, [])

    def test_ui_is_always_unlocked_even_after_a_failure(self):
        # The reason _finish_plugin_save exists as its own method: whatever the
        # worker ran into, the switches must become usable again.
        page = _StubDetailPage()
        with mock.patch('detail_page.options.firefox_extension_installed', return_value=False), \
            mock.patch('detail_page.options.read_profile_settings', return_value={}), \
            mock.patch('detail_page.options.t', side_effect=lambda key, **kw: key):
            page._finish_plugin_save(3, 'Swipe', 'swipe', None, None, 'unsigned-extension-payload')

        self.assertFalse(page._plugin_operation_in_progress)
        self.assertFalse(page.inline_busy)
        self.assertTrue(all(switch.sensitive for switch in page.switches.values()))
        self.assertEqual(page.activity, ('', False))

    def test_extension_error_reaches_the_banner(self):
        page = _StubDetailPage()
        with mock.patch('detail_page.options.firefox_extension_installed', return_value=False), \
            mock.patch('detail_page.options.read_profile_settings', return_value={}), \
            mock.patch('detail_page.options.t', side_effect=lambda key, **kw: key):
            page._finish_plugin_save(3, 'Swipe', 'swipe', None, None, 'unsigned-extension-payload')

        self.assertIn(('plugin_install_unsigned_swipe', 4200), page.banners)

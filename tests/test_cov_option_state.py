"""Coverage tests for the pure helpers in detail_page/option_state.py.

These functions carry the UI's option semantics (the inverted "Disable AI"
switch, launch-mode detection, per-engine mode lists from config, restoring a
browser family's remembered state) without touching GTK widgets.
"""
import json
import logging
import sys
import types
import unittest


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_option_state.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

from browser_option_logic import browser_state_key
from detail_page.option_state import (
    coerce_option_updates,
    configured_mode_values_for_engine,
    current_mode_value,
    normalize_mode_value,
    restored_browser_state,
    store_boolean_option_value,
    sync_browser_state_key,
    ui_boolean_option_active,
)
from webapp_constants import (
    APP_MODE_KEY,
    ONLY_HTTPS_KEY,
    OPTION_DISABLE_AI_KEY,
    OPTION_FORCE_PRIVACY_KEY,
    OPTION_NOTIFICATIONS_KEY,
)

FIREFOX = {'id': 1, 'name': 'Firefox', 'command': 'firefox'}
CHROME = {'id': 2, 'name': 'Google Chrome', 'command': 'google-chrome'}


class BooleanOptionTests(unittest.TestCase):
    def test_regular_options_map_one_to_active(self):
        self.assertTrue(ui_boolean_option_active(OPTION_NOTIFICATIONS_KEY, '1'))
        self.assertFalse(ui_boolean_option_active(OPTION_NOTIFICATIONS_KEY, '0'))
        self.assertFalse(ui_boolean_option_active(OPTION_NOTIFICATIONS_KEY, None))

    def test_disable_ai_switch_is_inverted(self):
        # The switch reads "AI features" while the stored key says "Disable AI".
        self.assertFalse(ui_boolean_option_active(OPTION_DISABLE_AI_KEY, '1'))
        self.assertTrue(ui_boolean_option_active(OPTION_DISABLE_AI_KEY, '0'))
        self.assertTrue(ui_boolean_option_active(OPTION_DISABLE_AI_KEY, None))

    def test_store_value_round_trips_the_inversion(self):
        self.assertEqual(store_boolean_option_value(OPTION_DISABLE_AI_KEY, True), '0')
        self.assertEqual(store_boolean_option_value(OPTION_DISABLE_AI_KEY, False), '1')
        self.assertEqual(store_boolean_option_value(OPTION_NOTIFICATIONS_KEY, True), '1')
        self.assertEqual(store_boolean_option_value(OPTION_NOTIFICATIONS_KEY, False), '0')
        for name in (OPTION_DISABLE_AI_KEY, OPTION_NOTIFICATIONS_KEY):
            for active in (True, False):
                with self.subTest(name=name, active=active):
                    self.assertEqual(ui_boolean_option_active(name, store_boolean_option_value(name, active)), active)


class ModeValueTests(unittest.TestCase):
    def test_current_mode_value_priorities(self):
        self.assertEqual(current_mode_value(None), 'standard')
        self.assertEqual(current_mode_value({'Kiosk': '1', APP_MODE_KEY: '1', 'Frameless': '1'}), 'kiosk')
        self.assertEqual(current_mode_value({APP_MODE_KEY: '1', 'Frameless': '1'}), 'seamless')
        self.assertEqual(current_mode_value({APP_MODE_KEY: '1'}), 'app')

    def test_normalize_mode_value_aliases_and_rejects_unknown(self):
        self.assertEqual(normalize_mode_value(' Default '), 'standard')
        self.assertEqual(normalize_mode_value('normal'), 'standard')
        self.assertEqual(normalize_mode_value('FullScreen'), 'kiosk')
        self.assertEqual(normalize_mode_value('frameless'), 'seamless')
        self.assertEqual(normalize_mode_value('app'), 'app')
        self.assertEqual(normalize_mode_value('bogus'), '')
        self.assertEqual(normalize_mode_value(None), '')


class ConfiguredModeValuesTests(unittest.TestCase):
    def test_default_list_includes_seamless_only_for_firefox_or_no_engine(self):
        self.assertEqual(configured_mode_values_for_engine({}, None), ['standard', 'kiosk', 'app', 'seamless'])
        self.assertEqual(configured_mode_values_for_engine(None, FIREFOX), ['standard', 'kiosk', 'app', 'seamless'])
        self.assertEqual(configured_mode_values_for_engine({}, CHROME), ['standard', 'kiosk', 'app'])

    def test_engine_id_entry_wins_and_items_are_deduplicated(self):
        config = {'browser_modes': {'1': ['app', 'App', 'fullscreen', 'nonsense'], 'default': ['standard']}}
        self.assertEqual(configured_mode_values_for_engine(config, FIREFOX), ['app', 'kiosk'])

    def test_dict_items_use_value_then_id_then_name(self):
        config = {'browser_modes': {'firefox': [{'value': 'kiosk'}, {'id': 'app'}, {'name': 'normal'}, {}]}}
        self.assertEqual(configured_mode_values_for_engine(config, FIREFOX), ['kiosk', 'app', 'standard'])

    def test_candidate_with_only_invalid_items_falls_through(self):
        config = {'browser_modes': {'firefox': ['bogus'], 'default': ['app']}}
        self.assertEqual(configured_mode_values_for_engine(config, FIREFOX), ['app'])

    def test_non_list_candidate_is_ignored(self):
        config = {'browser_modes': {'firefox': 'kiosk'}}
        self.assertEqual(configured_mode_values_for_engine(config, FIREFOX), ['standard', 'kiosk', 'app', 'seamless'])

    def test_nested_engines_mapping_is_consulted(self):
        config = {'browser_modes': {'engines': {'google-chrome': ['kiosk']}}}
        self.assertEqual(configured_mode_values_for_engine(config, CHROME), ['kiosk'])

    def test_non_dict_browser_modes_fall_back_to_defaults(self):
        config = {'browser_modes': ['kiosk']}
        self.assertEqual(configured_mode_values_for_engine(config, CHROME), ['standard', 'kiosk', 'app'])


class CoerceAndRestoreTests(unittest.TestCase):
    def test_none_becomes_empty_and_values_become_strings(self):
        self.assertEqual(coerce_option_updates('generic', {'a': None, 'b': 5}), {'a': '', 'b': '5'})
        self.assertEqual(coerce_option_updates('generic', None), {})

    def test_force_privacy_implies_https_only_for_real_browsers(self):
        updates = coerce_option_updates('firefox', {OPTION_FORCE_PRIVACY_KEY: 1})
        self.assertEqual(updates[ONLY_HTTPS_KEY], '1')
        self.assertNotIn(ONLY_HTTPS_KEY, coerce_option_updates('generic', {OPTION_FORCE_PRIVACY_KEY: '1'}))
        self.assertNotIn(ONLY_HTTPS_KEY, coerce_option_updates('chrome', {OPTION_FORCE_PRIVACY_KEY: '0'}))

    def test_sync_browser_state_key_delegates(self):
        self.assertEqual(sync_browser_state_key('firefox'), browser_state_key('firefox'))

    def test_generic_family_restores_nothing(self):
        self.assertEqual(restored_browser_state({OPTION_NOTIFICATIONS_KEY: '1'}, 'generic'), {})

    def test_remembered_family_state_overrides_current_values(self):
        cache = {
            OPTION_NOTIFICATIONS_KEY: '0',
            browser_state_key('firefox'): json.dumps({OPTION_NOTIFICATIONS_KEY: '1'}),
        }
        state = restored_browser_state(cache, 'firefox')
        self.assertEqual(state[OPTION_NOTIFICATIONS_KEY], '1')
        self.assertNotIn(browser_state_key('firefox'), state)

    def test_missing_cache_yields_family_defaults(self):
        state = restored_browser_state(None, 'chrome')
        self.assertIsInstance(state, dict)
        self.assertTrue(all(isinstance(value, str) for value in state.values()))


if __name__ == '__main__':
    unittest.main()

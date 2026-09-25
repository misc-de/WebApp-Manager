"""Coverage-driven tests for browser_option_logic, browser_option_registry and option_config."""
import json
import logging
import sys
import types
import unittest
from dataclasses import replace
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_browser_options.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import browser_option_logic as logic
import browser_option_registry as registry
import option_config


class _LabelCacheIsolation(unittest.TestCase):
    """Keeps the label->key table from leaking between tests."""

    def setUp(self):
        saved = logic._LABEL_KEY_CACHE
        self.addCleanup(setattr, logic, '_LABEL_KEY_CACHE', saved)
        logic._LABEL_KEY_CACHE = None


class SemanticModeTests(unittest.TestCase):
    def test_mode_option_keys_returns_an_independent_copy(self):
        keys = logic.mode_option_keys()
        self.assertEqual(keys, {'Kiosk', 'App Mode', 'Frameless'})
        keys.add('mutated')
        self.assertNotIn('mutated', logic.MODE_OPTION_KEYS)

    def test_semantic_mode_from_legacy_triple(self):
        self.assertEqual(logic.semantic_mode_from_options({'Kiosk': '1', 'App Mode': '1'}), 'kiosk')
        self.assertEqual(logic.semantic_mode_from_options({'App Mode': '1', 'Frameless': '1'}), 'seamless')
        self.assertEqual(logic.semantic_mode_from_options({'App Mode': '1', 'Frameless': '0'}), 'app')
        self.assertEqual(logic.semantic_mode_from_options(None), 'standard')

    def test_normalize_semantic_mode(self):
        self.assertEqual(logic.normalize_semantic_mode(' KIOSK '), 'kiosk')
        self.assertEqual(logic.normalize_semantic_mode('fullscreen'), 'standard')
        self.assertEqual(logic.normalize_semantic_mode(None), 'standard')

    def test_explicit_form_factor_modes_override_the_legacy_triple(self):
        options = {'App Mode': '1', 'Mode (Mobile)': 'Kiosk', 'Mode (Desktop)': 'bogus'}
        self.assertEqual(logic.mobile_mode_value(options), 'kiosk')
        self.assertEqual(logic.desktop_mode_value(options), 'standard')
        self.assertTrue(logic.per_form_factor_modes_differ(options))
        self.assertFalse(logic.per_form_factor_modes_differ({'App Mode': '1'}))

    def test_apply_semantic_mode_rewrites_the_triple(self):
        result = logic.apply_semantic_mode({'Kiosk': '1', 'Address': 'x'}, ' Seamless ')
        self.assertEqual(result, {'Kiosk': '0', 'App Mode': '1', 'Frameless': '1', 'Address': 'x'})
        self.assertEqual(logic.apply_semantic_mode({}, 'nonsense')['App Mode'], '0')
        self.assertEqual(logic.apply_semantic_mode({}, None)['Kiosk'], '0')


class BrowserFamilyTests(unittest.TestCase):
    def test_family_from_command(self):
        self.assertEqual(logic.browser_family_for_command('/usr/bin/firefox-esr'), 'firefox')
        self.assertEqual(logic.browser_family_for_command('chromium-browser'), 'chromium')
        self.assertEqual(logic.browser_family_for_command('google-chrome-stable'), 'chrome')
        self.assertEqual(logic.browser_family_for_command('epiphany'), 'generic')
        self.assertEqual(logic.browser_family_for_command(None), 'generic')

    def test_family_from_engine(self):
        self.assertEqual(logic.browser_family_for_engine(None), 'generic')
        self.assertEqual(logic.browser_family_for_engine({}), 'generic')
        self.assertEqual(logic.browser_family_for_engine({'command': None}), 'generic')
        self.assertEqual(logic.browser_family_for_engine({'command': 'chromium'}), 'chromium')

    def test_browser_state_key(self):
        self.assertEqual(logic.browser_state_key(' Firefox '), '__BrowserState.firefox')
        self.assertEqual(logic.browser_state_key('   '), '__BrowserState.generic')
        self.assertEqual(logic.browser_state_key(None), '__BrowserState.generic')


class OptionLabelTests(unittest.TestCase):
    def _with_label(self, label):
        return mock.patch.object(logic, 't', lambda key, **kwargs: label)

    def test_label_of_unknown_key_is_the_key(self):
        self.assertEqual(logic.option_ui_label('Not An Option'), 'Not An Option')

    def test_label_is_translated(self):
        with mock.patch.object(logic, 't', lambda key, **kwargs: f'<{key}>'):
            self.assertEqual(logic.option_ui_label('Notifications'), '<option_notifications>')

    def test_markup_dims_a_trailing_parenthetical(self):
        with self._with_label('Clear <cache> (on exit & more)'):
            self.assertEqual(
                logic.option_ui_label_markup('Clear Cache On Exit'),
                'Clear &lt;cache&gt; <span size="smaller" alpha="75%">(on exit &amp; more)</span>',
            )

    def test_markup_without_a_closed_parenthetical_is_only_escaped(self):
        with self._with_label('Tom & Jerry (open'):
            self.assertEqual(logic.option_ui_label_markup('Notifications'), 'Tom &amp; Jerry (open')
        with self._with_label('Plain'):
            self.assertEqual(logic.option_ui_label_markup('Notifications'), 'Plain')


class OptionKeyFromAnyTests(_LabelCacheIsolation):
    def test_empty_values_have_no_key(self):
        self.assertIsNone(logic.option_key_from_any(None))
        self.assertIsNone(logic.option_key_from_any('   '))

    def test_canonical_and_alias_keys(self):
        self.assertEqual(logic.option_key_from_any(' Notifications '), 'Notifications')
        self.assertEqual(logic.option_key_from_any('Keep Session'), 'Previous Session')

    def test_unknown_text_is_returned_stripped(self):
        self.assertEqual(logic.option_key_from_any('  Address '), 'Address')

    def test_translated_label_maps_back_to_its_key(self):
        translations = {'marker': 'fake'}
        labels = {'option_swipe': 'Wischgesten', 'option_adblock': 'Werbeblocker'}
        with mock.patch.object(logic, 'get_translations', return_value=translations), \
                mock.patch.object(logic, 't', lambda key, **kwargs: labels.get(key, key)):
            self.assertEqual(logic.option_key_from_any('Wischgesten'), 'Swipe')
            self.assertEqual(logic.option_key_from_any('Werbeblocker'), 'Adblock')

    def test_label_table_is_built_once_per_translation_table(self):
        translations = {}
        calls = []

        def counting_t(key, **kwargs):
            calls.append(key)
            return f'label:{key}'

        with mock.patch.object(logic, 'get_translations', return_value=translations), \
                mock.patch.object(logic, 't', counting_t):
            for _ in range(3):
                self.assertEqual(logic.option_key_from_any('label:option_swipe'), 'Swipe')
                self.assertEqual(logic.option_key_from_any('something else'), 'something else')
        labelled_specs = [spec for spec in logic.BROWSER_OPTION_SPECS if spec.label_key]
        self.assertEqual(len(calls), len(labelled_specs))

    def test_label_table_is_rebuilt_when_translations_change(self):
        first, second = {'lang': 'de'}, {'lang': 'fr'}
        current = {'table': first, 'labels': {'option_swipe': 'Wischen'}}
        with mock.patch.object(logic, 'get_translations', side_effect=lambda: current['table']), \
                mock.patch.object(logic, 't', lambda key, **kwargs: current['labels'].get(key, key)):
            self.assertEqual(logic.option_key_from_any('Wischen'), 'Swipe')
            current['table'] = second
            current['labels'] = {'option_swipe': 'Balayer'}
            self.assertEqual(logic.option_key_from_any('Balayer'), 'Swipe')
            self.assertEqual(logic.option_key_from_any('Wischen'), 'Wischen')

    def test_an_equal_but_distinct_table_counts_as_a_change(self):
        # The cache compares identity, so a reloaded (equal) dict rebuilds the table.
        tables = iter([{'a': 1}, {'a': 1}])
        calls = []

        def counting_t(key, **kwargs):
            calls.append(key)
            return key

        with mock.patch.object(logic, 'get_translations', side_effect=lambda: next(tables)), \
                mock.patch.object(logic, 't', counting_t):
            logic.option_key_from_any('x')
            logic.option_key_from_any('y')
        labelled_specs = [spec for spec in logic.BROWSER_OPTION_SPECS if spec.label_key]
        self.assertEqual(len(calls), 2 * len(labelled_specs))

    def test_shared_label_keeps_the_first_spec(self):
        with mock.patch.object(logic, 'get_translations', return_value={}), \
                mock.patch.object(logic, 't', lambda key, **kwargs: 'Same Label'):
            first_labelled = next(spec.key for spec in logic.BROWSER_OPTION_SPECS if spec.label_key)
            self.assertEqual(logic.option_key_from_any('Same Label'), first_labelled)


class NormalizationTests(_LabelCacheIsolation):
    def test_normalize_option_dict_drops_blank_keys_and_stringifies(self):
        result = logic.normalize_option_dict({'': 'x', 'Keep Session': 1, 'Address': None})
        self.assertEqual(result, {'Previous Session': '1', 'Address': ''})

    def test_normalize_option_rows_skips_malformed_rows(self):
        rows = [None, ('too', 'short'), (1, 1, '  ', 'x'), (2, 1, 'Address', 'https://a')]
        self.assertEqual(logic.normalize_option_rows(rows), {'Address': 'https://a'})

    def test_canonical_row_beats_newer_alias(self):
        rows = [(1, 1, 'Previous Session', '0'), (5, 1, 'Keep Session', '1'), (3, 1, 'Previous Session', '0')]
        self.assertEqual(logic.normalize_option_rows(rows), {'Previous Session': '0'})

    def test_newest_alias_wins_without_canonical_row(self):
        rows = [(4, 1, 'Keep Session', '1'), (2, 1, 'Session nach dem Schließen erhalten', '0'), (None, 1, 'Address', None)]
        self.assertEqual(logic.normalize_option_rows(rows), {'Previous Session': '1', 'Address': ''})

    def test_no_rows(self):
        self.assertEqual(logic.normalize_option_rows(None), {})


class FamilyProjectionTests(_LabelCacheIsolation):
    def test_browser_managed_option_keys_is_a_copy(self):
        keys = logic.browser_managed_option_keys()
        self.assertIn('Swipe', keys)
        keys.clear()
        self.assertIn('Swipe', logic.browser_managed_option_keys())

    def test_supported_keys_and_defaults_normalize_family(self):
        self.assertIn('Swipe', logic.supported_browser_option_keys(' FIREFOX '))
        self.assertNotIn('Swipe', logic.supported_browser_option_keys(''))
        self.assertEqual(logic.default_browser_option_values(None)['Default Zoom'], '100')

    def test_projection_keeps_only_supported_keys(self):
        options = {'Swipe': '1', 'Notifications': '1', 'Address': 'x'}
        self.assertEqual(logic.project_options_for_family(options, 'chrome'), {'Notifications': '1'})

    def test_browser_state_projection_drops_mode_keys(self):
        options = {'Kiosk': '1', 'Notifications': '1'}
        self.assertEqual(logic.project_browser_state_options(options, 'firefox'), {'Notifications': '1'})

    def test_encode_decode_round_trip(self):
        options = {'Swipe': '1', 'Keep Session': '1', 'Kiosk': '1'}
        raw = logic.encode_browser_state(options, 'firefox')
        self.assertEqual(json.loads(raw), {'Previous Session': '1', 'Swipe': '1'})
        self.assertEqual(logic.decode_browser_state(raw, 'firefox'), {'Previous Session': '1', 'Swipe': '1'})
        self.assertEqual(logic.decode_browser_state(raw, 'chrome'), {'Previous Session': '1'})

    def test_decode_rejects_garbage(self):
        self.assertEqual(logic.decode_browser_state('', 'firefox'), {})
        self.assertEqual(logic.decode_browser_state('{not json', 'firefox'), {})
        self.assertEqual(logic.decode_browser_state('[1, 2]', 'firefox'), {})

    def test_decode_stringifies_values(self):
        self.assertEqual(logic.decode_browser_state('{"Swipe": 1, "Adblock": null}', 'firefox'), {'Swipe': '1', 'Adblock': ''})

    def test_family_state_layers_options_over_defaults(self):
        state = logic.build_family_option_state({'Swipe': '1', 'Address': 'x'}, 'firefox')
        self.assertEqual(state['Swipe'], '1')
        self.assertEqual(state['Adblock'], '0')
        self.assertNotIn('Address', state)


class RegistryTests(unittest.TestCase):
    def test_spec_families(self):
        self.assertEqual(registry.option_spec('Swipe').families, ('firefox',))

    def test_category_of_unknown_option_is_comfort(self):
        self.assertEqual(registry.option_category('Not An Option'), 'comfort')
        self.assertEqual(registry.option_category('Only HTTPS'), 'security')

    def test_unknown_or_blank_category_falls_back_to_comfort(self):
        spec = registry.option_spec('Only HTTPS')
        for category in ('bogus', '', None):
            with mock.patch.object(registry, 'option_spec', return_value=replace(spec, category=category)):
                self.assertEqual(registry.option_category('Only HTTPS'), 'comfort')
        with mock.patch.object(registry, 'option_spec', return_value=replace(spec, category=' CLEANUP ')):
            self.assertEqual(registry.option_category('Only HTTPS'), 'cleanup')

    def test_all_specs_include_hidden_ones(self):
        keys = {spec.key for spec in registry.all_browser_option_specs()}
        self.assertIn('Kiosk', keys)
        self.assertNotIn('Kiosk', {spec.key for spec in registry.visible_browser_option_specs()})

    def test_binding_lookup(self):
        self.assertIsNone(registry.option_binding('Not An Option', 'firefox'))
        self.assertEqual(registry.option_binding('Swipe', ' Firefox ').family, 'firefox')
        # Unknown families borrow the generic binding when there is one ...
        self.assertEqual(registry.option_binding('Previous Session', 'epiphany').family, 'generic')
        # ... and get nothing when the option has none.
        self.assertIsNone(registry.option_binding('Swipe', 'chrome'))
        self.assertIsNone(registry.option_binding('Keep in Background', ''))

    def test_option_supported(self):
        self.assertFalse(registry.option_supported('Not An Option', 'firefox'))
        self.assertFalse(registry.option_supported('Kiosk', 'firefox', visible_only=True))
        self.assertTrue(registry.option_supported('Kiosk', 'firefox'))
        self.assertFalse(registry.option_supported('Swipe', 'chrome'))

    def test_supported_keys_visible_only(self):
        self.assertNotIn('Kiosk', registry.supported_option_keys('firefox', visible_only=True))
        self.assertIn('Kiosk', registry.supported_option_keys('firefox'))

    def test_default_values_skip_unsupported_options(self):
        chrome = registry.default_option_values('chrome')
        self.assertNotIn('Swipe', chrome)
        self.assertEqual(chrome['Color Scheme'], 'auto')
        visible = registry.default_option_values('firefox', visible_only=True)
        self.assertIn('Swipe', visible)
        self.assertNotIn('Kiosk', visible)

    def test_default_value_none_becomes_empty_string(self):
        spec = replace(registry.option_spec('Swipe'), default_value=None)
        with mock.patch.object(registry, 'ALL_BROWSER_OPTION_SPECS', (spec,)):
            self.assertEqual(registry.default_option_values('firefox'), {'Swipe': ''})


class OptionConfigTests(unittest.TestCase):
    def test_option_names_are_the_visible_specs_in_order(self):
        self.assertEqual(option_config.option_names(), [spec.key for spec in registry.visible_browser_option_specs()])
        self.assertNotIn('Kiosk', option_config.option_names())

    def test_option_names_skip_specs_marked_invisible(self):
        specs = registry.visible_browser_option_specs()
        hidden = replace(specs[0], visible=False)
        with mock.patch.object(option_config, 'visible_browser_option_specs', return_value=(hidden,) + specs[1:]):
            self.assertNotIn(specs[0].key, option_config.option_names())

    def test_overview_status_definitions(self):
        definitions = option_config.overview_status_definitions()
        self.assertEqual([item[0] for item in definitions], ['Adblock', 'Clear Cache On Exit', 'Clear Cookies On Exit'])
        for _key, icon, label_key in definitions:
            self.assertTrue(icon.startswith('icons/') and icon.endswith('.svg'))
            self.assertTrue(label_key.startswith('overview_status_'))


if __name__ == '__main__':
    unittest.main()

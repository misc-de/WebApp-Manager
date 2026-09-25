"""Coverage-driven tests for engine_support: engine definitions and availability."""
import logging
import sys
import types
import unittest
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_engine_support.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import engine_support
from engine_support import EngineDefinition


class EngineDefinitionTests(unittest.TestCase):
    def test_firefox_properties(self):
        engine = EngineDefinition(1, 'Firefox', ' /usr/bin/Firefox-ESR ')
        self.assertEqual(engine.command_lower, '/usr/bin/firefox-esr')
        self.assertTrue(engine.is_firefox)
        self.assertFalse(engine.is_chromium_family)
        self.assertTrue(engine.supports_adblock)
        self.assertTrue(engine.supports_background_mode)

    def test_chromium_family_properties(self):
        for command in ('google-chrome', 'chromium-browser'):
            engine = EngineDefinition(2, 'X', command)
            self.assertTrue(engine.is_chromium_family, command)
            self.assertFalse(engine.is_firefox)
            self.assertFalse(engine.supports_adblock)
            self.assertFalse(engine.supports_background_mode)

    def test_empty_command(self):
        engine = EngineDefinition(3, 'None', None)
        self.assertEqual(engine.command_lower, '')
        self.assertFalse(engine.is_firefox or engine.is_chromium_family)


class CommandCandidateTests(unittest.TestCase):
    def test_firefox_candidates(self):
        self.assertEqual(engine_support._command_candidates('Firefox'), ['firefox', 'firefox-esr'])

    def test_chrome_candidates_start_with_the_configured_command(self):
        self.assertEqual(
            engine_support._command_candidates('google-chrome-beta'),
            ['google-chrome-beta', 'google-chrome', 'google-chrome-stable', 'chrome', 'chromium', 'chromium-browser'],
        )

    def test_chromium_candidates_prefer_chromium(self):
        self.assertEqual(
            engine_support._command_candidates('chromium'),
            ['chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable', 'chrome'],
        )

    def test_other_and_empty_commands(self):
        self.assertEqual(engine_support._command_candidates('epiphany'), ['epiphany'])
        self.assertEqual(engine_support._command_candidates(None), [])


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        saved = engine_support._AVAILABLE_ENGINES_CACHE
        self.addCleanup(setattr, engine_support, '_AVAILABLE_ENGINES_CACHE', saved)
        engine_support._AVAILABLE_ENGINES_CACHE = None

    def test_engine_available_tries_fallback_binaries(self):
        installed = {'firefox-esr'}
        with mock.patch.object(engine_support, 'host_which', side_effect=lambda name: f'/usr/bin/{name}' if name in installed else None) as which:
            self.assertTrue(engine_support.engine_available({'command': 'firefox'}))
            self.assertTrue(engine_support.engine_available(EngineDefinition(1, 'FF', 'firefox')))
            self.assertFalse(engine_support.engine_available({'command': 'google-chrome'}))
            self.assertFalse(engine_support.engine_available({}))
        self.assertIn(mock.call('firefox-esr'), which.call_args_list)

    def test_configured_engines_default_when_config_has_none(self):
        with mock.patch.object(engine_support, 'get_app_config', return_value={'engines': []}):
            engines = engine_support.configured_engines()
        self.assertEqual(engines, [
            EngineDefinition(1, 'Firefox', 'firefox'),
            EngineDefinition(2, 'Chrome', 'google-chrome'),
        ])

    def test_configured_engines_from_config(self):
        config = {'engines': [{'id': '7', 'name': 'Chromium'}, {'id': 8, 'name': 'FF', 'command': 'firefox'}]}
        with mock.patch.object(engine_support, 'get_app_config', return_value=config):
            engines = engine_support.configured_engines()
        self.assertEqual(engines, [EngineDefinition(7, 'Chromium', ''), EngineDefinition(8, 'FF', 'firefox')])

    def test_available_engines_filters_caches_and_returns_copies(self):
        config = {'engines': [
            {'id': 1, 'name': 'Firefox', 'command': 'firefox'},
            {'id': 2, 'name': 'Chrome', 'command': 'google-chrome'},
        ]}
        with mock.patch.object(engine_support, 'get_app_config', return_value=config), \
                mock.patch.object(engine_support, 'host_which', side_effect=lambda name: '/usr/bin/firefox' if name == 'firefox' else None) as which:
            first = engine_support.available_engines()
            lookups = which.call_count
            first[0]['name'] = 'mutated'
            second = engine_support.available_engines()
        self.assertEqual(second, [{'id': 1, 'name': 'Firefox', 'command': 'firefox'}])
        self.assertEqual(which.call_count, lookups, 'second call must be served from the cache')


class EngineIconTests(unittest.TestCase):
    def test_icon_names(self):
        self.assertEqual(engine_support.engine_icon_name('Firefox Nightly'), 'firefox')
        self.assertEqual(engine_support.engine_icon_name('Google Chrome'), 'google-chrome')
        self.assertEqual(engine_support.engine_icon_name('Chromium'), 'chromium-browser')
        self.assertEqual(engine_support.engine_icon_name('Epiphany'), 'applications-internet-symbolic')
        self.assertEqual(engine_support.engine_icon_name(None), 'applications-internet-symbolic')


if __name__ == '__main__':
    unittest.main()

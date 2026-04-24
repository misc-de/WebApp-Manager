import logging
import sys
import types
import unittest


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.form_factor.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

from browser_option_logic import (
    apply_semantic_mode,
    desktop_mode_value,
    mobile_mode_value,
    normalize_semantic_mode,
    per_form_factor_modes_differ,
)
from webapp_constants import MODE_DESKTOP_KEY, MODE_MOBILE_KEY


class NormalizeSemanticModeTests(unittest.TestCase):
    def test_accepts_known_values(self):
        for value in ('standard', 'kiosk', 'app', 'seamless'):
            self.assertEqual(normalize_semantic_mode(value), value)

    def test_lowercases_input(self):
        self.assertEqual(normalize_semantic_mode('Kiosk'), 'kiosk')
        self.assertEqual(normalize_semantic_mode('SEAMLESS'), 'seamless')

    def test_unknown_becomes_standard(self):
        self.assertEqual(normalize_semantic_mode('fullscreen'), 'standard')
        self.assertEqual(normalize_semantic_mode(''), 'standard')
        self.assertEqual(normalize_semantic_mode(None), 'standard')


class FormFactorFallbackTests(unittest.TestCase):
    """When explicit mobile/desktop keys are absent, fall back to the legacy semantic mode."""

    def test_mobile_falls_back_to_legacy_seamless(self):
        options = apply_semantic_mode({}, 'seamless')
        self.assertEqual(mobile_mode_value(options), 'seamless')
        self.assertEqual(desktop_mode_value(options), 'seamless')

    def test_mobile_falls_back_to_legacy_kiosk(self):
        options = apply_semantic_mode({}, 'kiosk')
        self.assertEqual(mobile_mode_value(options), 'kiosk')

    def test_legacy_default_is_standard(self):
        self.assertEqual(mobile_mode_value({}), 'standard')
        self.assertEqual(desktop_mode_value({}), 'standard')

    def test_none_options_is_safe(self):
        self.assertEqual(mobile_mode_value(None), 'standard')
        self.assertEqual(desktop_mode_value(None), 'standard')


class FormFactorExplicitTests(unittest.TestCase):
    def test_explicit_mobile_overrides_legacy(self):
        options = apply_semantic_mode({}, 'seamless')
        options[MODE_MOBILE_KEY] = 'app'
        self.assertEqual(mobile_mode_value(options), 'app')
        # desktop still falls back to legacy since its own key is absent
        self.assertEqual(desktop_mode_value(options), 'seamless')

    def test_explicit_desktop_overrides_legacy(self):
        options = apply_semantic_mode({}, 'seamless')
        options[MODE_DESKTOP_KEY] = 'standard'
        self.assertEqual(desktop_mode_value(options), 'standard')

    def test_both_explicit(self):
        options = {MODE_MOBILE_KEY: 'seamless', MODE_DESKTOP_KEY: 'standard'}
        self.assertEqual(mobile_mode_value(options), 'seamless')
        self.assertEqual(desktop_mode_value(options), 'standard')

    def test_invalid_explicit_value_normalizes_to_standard(self):
        options = {MODE_MOBILE_KEY: 'bogus'}
        self.assertEqual(mobile_mode_value(options), 'standard')


class PerFormFactorModesDifferTests(unittest.TestCase):
    def test_no_difference_when_empty(self):
        self.assertFalse(per_form_factor_modes_differ({}))

    def test_no_difference_when_legacy_only(self):
        options = apply_semantic_mode({}, 'seamless')
        self.assertFalse(per_form_factor_modes_differ(options))

    def test_different_when_explicit_diverges_from_legacy(self):
        options = apply_semantic_mode({}, 'seamless')
        options[MODE_DESKTOP_KEY] = 'standard'
        self.assertTrue(per_form_factor_modes_differ(options))

    def test_same_explicit_is_not_different(self):
        options = {MODE_MOBILE_KEY: 'seamless', MODE_DESKTOP_KEY: 'seamless'}
        self.assertFalse(per_form_factor_modes_differ(options))


if __name__ == '__main__':
    unittest.main()

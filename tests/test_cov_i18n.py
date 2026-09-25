"""Coverage-driven tests for i18n: config handling, language resolution, caches.

Every test points the module at a private temp directory (language files,
default config, user config) and restores the module-level caches afterwards,
so nothing leaks into other test files and the real user config is never read.
"""
import json
import locale
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_i18n.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import i18n

_PATH_ATTRS = ('LANG_DIR', 'DEFAULT_CONFIG_PATH', 'USER_CONFIG_DIR', 'USER_CONFIG_PATH')
_CACHE_ATTRS = ('_CONFIG_CACHE', '_TRANSLATION_CACHE', '_LANGUAGE_METADATA_CACHE', '_LANGUAGE_CODE_CACHE')


class _I18nSandbox(unittest.TestCase):
    """Redirects every path i18n touches and snapshots its caches."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self._saved = {name: getattr(i18n, name) for name in _PATH_ATTRS + _CACHE_ATTRS}
        self.addCleanup(self._restore)
        self.lang_dir = root / 'lang'
        self.lang_dir.mkdir()
        i18n.LANG_DIR = self.lang_dir
        i18n.DEFAULT_CONFIG_PATH = root / 'default-config.json'
        i18n.USER_CONFIG_DIR = root / 'user'
        i18n.USER_CONFIG_PATH = i18n.USER_CONFIG_DIR / 'config.json'
        i18n._CONFIG_CACHE = None
        i18n._TRANSLATION_CACHE = {}
        i18n._LANGUAGE_METADATA_CACHE = None
        i18n._LANGUAGE_CODE_CACHE = None

    def _restore(self):
        for name, value in self._saved.items():
            setattr(i18n, name, value)

    def write_lang(self, code, data):
        path = self.lang_dir / f'{code}.json'
        path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding='utf-8')
        return path

    def write_user_config(self, data):
        i18n.USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        text = data if isinstance(data, str) else json.dumps(data)
        i18n.USER_CONFIG_PATH.write_text(text, encoding='utf-8')


class LanguageCodeNormalizationTests(unittest.TestCase):
    def test_locale_suffixes_and_modifiers_are_dropped(self):
        self.assertEqual(i18n._normalize_language_code('de-AT.UTF-8@euro'), 'de_at')

    def test_extra_parts_are_truncated_to_language_and_region(self):
        self.assertEqual(i18n._normalize_language_code('zh_Hant_TW'), 'zh_hant')

    def test_single_part_and_empty_values(self):
        self.assertEqual(i18n._normalize_language_code('FR'), 'fr')
        self.assertEqual(i18n._normalize_language_code(None), '')
        self.assertEqual(i18n._normalize_language_code('__'), '')

    def test_base_code_of_empty_value_is_empty(self):
        self.assertEqual(i18n._base_language_code(''), '')
        self.assertEqual(i18n._base_language_code('pt-BR'), 'pt')


class DeepMergeTests(unittest.TestCase):
    def test_nested_dicts_are_merged_without_mutating_the_base(self):
        base = {'settings': {'a': 1, 'b': 2}, 'language': 'en'}
        merged = i18n._deep_merge(base, {'settings': {'b': 3}, 'language': 'de'})
        self.assertEqual(merged, {'settings': {'a': 1, 'b': 3}, 'language': 'de'})
        self.assertEqual(base['settings'], {'a': 1, 'b': 2})

    def test_none_override_returns_a_copy(self):
        base = {'x': {'y': 1}}
        merged = i18n._deep_merge(base, None)
        self.assertEqual(merged, base)
        self.assertIsNot(merged['x'], base['x'])


class LanguageDiscoveryTests(_I18nSandbox):
    def test_languages_are_listed_with_names_and_english_is_always_present(self):
        self.write_lang('de', {'_meta_language_name': 'Deutsch'})
        self.write_lang('fr', {})
        self.write_lang('broken', '{not json')
        languages = i18n.available_languages()
        self.assertEqual(languages, [
            {'code': 'de', 'name': 'Deutsch'},
            {'code': 'fr', 'name': 'FR'},
            {'code': 'en', 'name': 'English'},
        ])

    def test_file_names_normalizing_to_the_same_code_are_listed_once(self):
        self.write_lang('pt-BR', {'_meta_language_name': 'first'})
        self.write_lang('pt_br', {'_meta_language_name': 'second'})
        codes = [item['code'] for item in i18n.available_languages()]
        self.assertEqual(codes.count('pt_br'), 1)

    def test_empty_stem_is_skipped(self):
        self.write_lang('_', {'_meta_language_name': 'nothing'})
        self.assertEqual([item['code'] for item in i18n.available_languages()], ['en'])

    def test_missing_language_dir_still_offers_english(self):
        i18n.LANG_DIR = self.lang_dir / 'missing'
        self.assertEqual(i18n.available_languages(), [{'code': 'en', 'name': 'English'}])

    def test_result_is_cached_and_callers_get_copies(self):
        self.write_lang('de', {})
        first = i18n.available_languages()
        first.append({'code': 'xx', 'name': 'mutated'})
        self.write_lang('fr', {})
        self.assertEqual([item['code'] for item in i18n.available_languages()], ['de', 'en'])
        self.assertEqual([item['code'] for item in i18n.available_languages(force_reload=True)], ['de', 'fr', 'en'])


class SystemLanguageTests(_I18nSandbox):
    def setUp(self):
        super().setUp()
        self.write_lang('de', {})
        self.write_lang('pt_br', {})

    def _detect(self, getlocale, getdefaultlocale, lang_env):
        env = {} if lang_env is None else {'LANG': lang_env}
        with mock.patch.object(i18n.locale, 'getlocale', getlocale), \
                mock.patch.object(i18n.locale, 'getdefaultlocale', getdefaultlocale, create=True), \
                mock.patch.dict(i18n.os.environ, env, clear=True):
            return i18n.get_system_language_code()

    def test_exact_regional_match_wins(self):
        code = self._detect(lambda: ('pt_BR', 'UTF-8'), lambda: (None, None), None)
        self.assertEqual(code, 'pt_br')

    def test_base_language_is_used_for_an_unknown_region(self):
        code = self._detect(lambda: (None, None), lambda: ('de_AT', 'UTF-8'), None)
        self.assertEqual(code, 'de')

    def test_locale_errors_fall_through_to_the_environment(self):
        def broken_getlocale():
            raise locale.Error('unsupported')

        def missing_getdefaultlocale():
            raise AttributeError('removed in newer Pythons')

        code = self._detect(broken_getlocale, missing_getdefaultlocale, 'de_CH.UTF-8')
        self.assertEqual(code, 'de')

    def test_unknown_language_falls_back_to_english(self):
        code = self._detect(lambda: ('ja_JP', 'UTF-8'), lambda: ('ja_JP', 'UTF-8'), 'ja_JP.UTF-8')
        self.assertEqual(code, 'en')


class AppConfigTests(_I18nSandbox):
    def test_missing_files_yield_system_language(self):
        self.assertEqual(i18n.get_app_config(), {'language': 'system'})

    def test_user_config_overrides_defaults_but_only_mutable_keys(self):
        i18n.DEFAULT_CONFIG_PATH.write_text(json.dumps({
            'language': 'system',
            'engines': [{'id': 1, 'name': 'Firefox', 'command': 'firefox'}],
            'settings': {'a': 1, 'b': 2},
        }), encoding='utf-8')
        self.write_user_config({
            'language': 'de',
            'engines': [{'id': 9, 'name': 'Evil', 'command': 'rm'}],
            'settings': {'b': 3},
            'window_state': {'width': 400},
            'unrelated': True,
        })
        config = i18n.get_app_config()
        self.assertEqual(config['language'], 'de')
        self.assertEqual(config['engines'], [{'id': 1, 'name': 'Firefox', 'command': 'firefox'}])
        self.assertEqual(config['settings'], {'a': 1, 'b': 3})
        self.assertEqual(config['window_state'], {'width': 400})
        self.assertNotIn('unrelated', config)

    def test_non_dict_user_and_default_configs_are_ignored(self):
        i18n.DEFAULT_CONFIG_PATH.write_text('[1, 2]', encoding='utf-8')
        self.write_user_config('"just a string"')
        self.assertEqual(i18n.get_app_config(), {'language': 'system'})

    def test_cached_config_is_returned_as_a_copy(self):
        self.write_user_config({'settings': {'x': 1}})
        config = i18n.get_app_config()
        config['settings']['x'] = 99
        self.assertEqual(i18n.get_app_config()['settings'], {'x': 1})

    def test_force_reload_rereads_the_file(self):
        self.write_user_config({'language': 'de'})
        self.assertEqual(i18n.get_app_config()['language'], 'de')
        self.write_user_config({'language': 'fr'})
        self.assertEqual(i18n.get_app_config()['language'], 'de')
        self.assertEqual(i18n.get_app_config(force_reload=True)['language'], 'fr')

    def test_corrupt_user_config_falls_back_instead_of_raising(self):
        # Regression: a truncated config.json used to raise on every config read, including
        # engine_support's at import time, so the app could not start.
        self.write_user_config('{"language": "de"')
        self.assertEqual(i18n.get_app_config()['language'], 'system')

    def test_save_rejects_non_dict(self):
        with self.assertRaises(TypeError):
            i18n.save_app_config(['language', 'de'])

    def test_save_replaces_the_file_atomically(self):
        self.write_user_config({'language': 'de'})
        with mock.patch.object(i18n.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                i18n.save_app_config({'language': 'fr'})
        # The old file is untouched and no temp file is left behind.
        self.assertEqual(json.loads(i18n.USER_CONFIG_PATH.read_text(encoding='utf-8')), {'language': 'de'})
        self.assertEqual([p.name for p in i18n.USER_CONFIG_DIR.iterdir()], [i18n.USER_CONFIG_PATH.name])

    def test_save_writes_file_and_updates_cache(self):
        saved = i18n.save_app_config({'language': 'de', 'settings': {'ä': 'ö'}})
        self.assertEqual(saved, {'language': 'de', 'settings': {'ä': 'ö'}})
        text = i18n.USER_CONFIG_PATH.read_text(encoding='utf-8')
        self.assertIn('"ä": "ö"', text)
        self.assertTrue(text.endswith('\n'))
        self.assertEqual(i18n.get_app_config()['language'], 'de')

    def test_update_applies_in_place_mutation(self):
        self.write_user_config({'language': 'fr'})

        def mutate(config):
            config['language'] = 'de'

        self.assertEqual(i18n.update_app_config(mutate)['language'], 'de')
        self.assertEqual(json.loads(i18n.USER_CONFIG_PATH.read_text(encoding='utf-8'))['language'], 'de')

    def test_update_uses_returned_config(self):
        result = i18n.update_app_config(lambda config: {'language': 'es'})
        self.assertEqual(result, {'language': 'es'})


class ConfiguredLanguageTests(_I18nSandbox):
    def setUp(self):
        super().setUp()
        self.write_lang('de', {})
        self.write_lang('pt_br', {})

    def test_blank_or_system_means_system(self):
        for value in ('', 'System', None):
            i18n._CONFIG_CACHE = {'language': value}
            self.assertEqual(i18n.get_configured_language_value(), 'system')

    def test_explicit_value_is_normalized(self):
        i18n._CONFIG_CACHE = {'language': 'PT-br'}
        self.assertEqual(i18n.get_configured_language_value(), 'pt_br')

    def test_resolution_prefers_exact_then_base_then_english(self):
        cases = {'pt-BR': 'pt_br', 'de_CH': 'de', 'xx': 'en'}
        for configured, expected in cases.items():
            i18n._CONFIG_CACHE = {'language': configured}
            i18n._LANGUAGE_CODE_CACHE = None
            self.assertEqual(i18n.get_language_code(), expected, configured)

    def test_system_setting_uses_system_detection(self):
        i18n._CONFIG_CACHE = {'language': 'system'}
        with mock.patch.object(i18n, 'get_system_language_code', return_value='de') as detect:
            self.assertEqual(i18n.get_language_code(), 'de')
        detect.assert_called_once()


class LanguageCodeCacheTests(_I18nSandbox):
    def setUp(self):
        super().setUp()
        self.write_lang('de', {})
        self.write_lang('fr', {})

    def test_resolution_runs_once_until_something_changes(self):
        i18n._CONFIG_CACHE = {'language': 'de'}
        with mock.patch.object(i18n, '_resolve_language_code', wraps=i18n._resolve_language_code) as resolve:
            for _ in range(5):
                self.assertEqual(i18n.get_language_code(), 'de')
        self.assertEqual(resolve.call_count, 1)

    def test_saving_the_config_drops_the_cached_code(self):
        i18n._CONFIG_CACHE = {'language': 'de'}
        self.assertEqual(i18n.get_language_code(), 'de')
        i18n.save_app_config({'language': 'fr'})
        self.assertEqual(i18n.get_language_code(), 'fr')

    def test_reloading_the_config_drops_the_cached_code(self):
        self.write_user_config({'language': 'de'})
        self.assertEqual(i18n.get_language_code(), 'de')
        self.write_user_config({'language': 'fr'})
        i18n.get_app_config(force_reload=True)
        self.assertEqual(i18n.get_language_code(), 'fr')

    def test_a_cached_config_read_keeps_the_cached_code(self):
        i18n._CONFIG_CACHE = {'language': 'de'}
        self.assertEqual(i18n.get_language_code(), 'de')
        i18n.get_app_config()
        self.assertEqual(i18n._LANGUAGE_CODE_CACHE, 'de')

    def test_invalidate_drops_code_and_translations_but_keeps_config(self):
        i18n._CONFIG_CACHE = {'language': 'de'}
        i18n.get_translations()
        i18n.invalidate_i18n_cache()
        self.assertIsNone(i18n._LANGUAGE_CODE_CACHE)
        self.assertEqual(i18n._TRANSLATION_CACHE, {})
        self.assertIsNone(i18n._LANGUAGE_METADATA_CACHE)
        self.assertEqual(i18n._CONFIG_CACHE, {'language': 'de'})

    def test_invalidate_with_reload_drops_config(self):
        i18n._CONFIG_CACHE = {'language': 'de'}
        i18n.invalidate_i18n_cache(reload_config=True)
        self.assertIsNone(i18n._CONFIG_CACHE)


class TranslationTests(_I18nSandbox):
    def setUp(self):
        super().setUp()
        self.write_lang('en', {'hello': 'Hello {name}', 'bye': 'Bye', 'color': 'Color'})
        self.write_lang('de', {'hello': 'Hallo {name}', 'color': 'Farbe'})
        self.write_lang('de_ch', {'color': 'Farb'})

    def test_regional_file_layers_over_base_and_english(self):
        translations = i18n.get_translations('de-CH')
        self.assertEqual(translations['color'], 'Farb')
        self.assertEqual(translations['hello'], 'Hallo {name}')
        self.assertEqual(translations['bye'], 'Bye')

    def test_english_returns_the_fallback_table(self):
        self.assertEqual(i18n.get_translations('en'), {'hello': 'Hello {name}', 'bye': 'Bye', 'color': 'Color'})

    def test_result_is_cached_per_code(self):
        first = i18n.get_translations('de')
        self.assertIs(i18n.get_translations('de'), first)

    def test_non_dict_language_file_is_ignored(self):
        self.write_lang('fr', '["not", "a", "dict"]')
        self.assertEqual(i18n.get_translations('fr')['bye'], 'Bye')

    def test_missing_english_file_yields_only_local_strings(self):
        (self.lang_dir / 'en.json').unlink()
        self.assertEqual(i18n.get_translations('de'), {'hello': 'Hallo {name}', 'color': 'Farbe'})
        self.assertEqual(i18n.get_translations('en'), {})

    def test_default_code_comes_from_configured_language(self):
        i18n._CONFIG_CACHE = {'language': 'de'}
        self.assertEqual(i18n.get_translations()['color'], 'Farbe')

    def test_t_formats_placeholders_and_falls_back_to_the_key(self):
        i18n._CONFIG_CACHE = {'language': 'de'}
        self.assertEqual(i18n.t('hello', name='Welt'), 'Hallo Welt')
        self.assertEqual(i18n.t('bye'), 'Bye')
        self.assertEqual(i18n.t('no_such_key'), 'no_such_key')


if __name__ == '__main__':
    unittest.main()

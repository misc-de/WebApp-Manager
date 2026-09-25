import logging
import sys
import types
import unittest
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.perf.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import engine_support
from database import Database
from mainwindow import MainWindowEntriesMixin


class _DummyStore:
    def __init__(self, items=None):
        self._items = list(items or [])

    def get_n_items(self):
        return len(self._items)

    def get_item(self, index):
        return self._items[index]

    def insert(self, index, item):
        self._items.insert(index, item)

    def append(self, item):
        self._items.append(item)

    def remove(self, index):
        self._items.pop(index)

    def remove_all(self):
        self._items.clear()


class _CacheHarness(MainWindowEntriesMixin):
    def __init__(self, db):
        self.entries_store = _DummyStore()
        self.db = db
        self._options_cache = {}
        self._profile_size_cache = {}
        self._profile_size_pending = set()


class AvailableEnginesCachingTests(unittest.TestCase):
    def setUp(self):
        engine_support._AVAILABLE_ENGINES_CACHE = None

    def tearDown(self):
        engine_support._AVAILABLE_ENGINES_CACHE = None

    def test_second_call_does_not_re_probe_path(self):
        """After the first call, the binary lookup must not run again."""
        with mock.patch('engine_support.host_which') as which_mock:
            which_mock.side_effect = lambda cmd: f'/usr/bin/{cmd}'
            first = engine_support.available_engines()
            which_mock.reset_mock()
            second = engine_support.available_engines()

            which_mock.assert_not_called()
            self.assertEqual(first, second)

    def test_returns_independent_copies(self):
        """Callers must not be able to mutate each other's data through the cache."""
        with mock.patch('engine_support.host_which', side_effect=lambda cmd: f'/usr/bin/{cmd}'):
            first = engine_support.available_engines()
            if not first:
                self.skipTest('no engines configured on this system')
            first[0]['command'] = 'mutated'
            second = engine_support.available_engines()

        self.assertNotEqual(second[0]['command'], 'mutated')

    def test_cache_respects_availability(self):
        """Engines whose command cannot be resolved must not appear."""
        with mock.patch('engine_support.host_which', return_value=None):
            engine_support._AVAILABLE_ENGINES_CACHE = None
            result = engine_support.available_engines()
        self.assertEqual(result, [])


class LoadEntriesCacheConsistencyTests(unittest.TestCase):
    """Perf-6 regression: the cache populated by load_entries_from_db must use canonical keys."""

    def test_cache_uses_canonical_keys(self):
        db = Database(':memory:')
        entry_id = db.add_entry('Sample', 'desc')
        db.add_options(entry_id, {
            'Address': 'https://sample.example/',
            'Notifications': '1',
        })

        harness = _CacheHarness(db)
        harness.load_entries_from_db()

        cached = harness._options_cache[entry_id]
        self.assertEqual(cached.get('Address'), 'https://sample.example/')
        self.assertEqual(cached.get('Notifications'), '1')

    def test_cache_collapses_aliases_to_canonical_key(self):
        """Legacy alias rows (e.g. 'Allow Notifications') must collapse to their canonical key."""
        db = Database(':memory:')
        entry_id = db.add_entry('Aliased')
        db.cursor.execute(
            'INSERT INTO options (entry_id, option_key, option_value) VALUES (?, ?, ?)',
            (entry_id, 'Allow Notifications', '1'),
        )
        db.cursor.execute(
            'INSERT INTO options (entry_id, option_key, option_value) VALUES (?, ?, ?)',
            (entry_id, 'Address', 'https://alias.example/'),
        )
        db.conn.commit()

        harness = _CacheHarness(db)
        harness.load_entries_from_db()

        cached = harness._options_cache[entry_id]
        self.assertEqual(cached.get('Notifications'), '1')
        self.assertNotIn('Allow Notifications', cached)
        self.assertEqual(cached.get('Address'), 'https://alias.example/')

    def test_cache_survives_reload(self):
        db = Database(':memory:')
        entry_id = db.add_entry('Reload')
        db.add_option(entry_id, 'Address', 'https://reload.example/')

        harness = _CacheHarness(db)
        harness.load_entries_from_db()
        first_snapshot = dict(harness._options_cache[entry_id])

        harness._options_cache['stale'] = {'x': 'y'}
        harness.load_entries_from_db()

        self.assertNotIn('stale', harness._options_cache)
        self.assertEqual(harness._options_cache[entry_id], first_snapshot)

    def test_multiple_entries_isolated_in_cache(self):
        db = Database(':memory:')
        id_a = db.add_entry('A')
        id_b = db.add_entry('B')
        db.add_option(id_a, 'Address', 'https://a/')
        db.add_option(id_b, 'Address', 'https://b/')

        harness = _CacheHarness(db)
        harness.load_entries_from_db()

        self.assertEqual(harness._options_cache[id_a]['Address'], 'https://a/')
        self.assertEqual(harness._options_cache[id_b]['Address'], 'https://b/')


class LoadEntriesStorePopulationTests(unittest.TestCase):
    def test_entries_store_populated_from_db(self):
        db = Database(':memory:')
        db.add_entry('Alpha', 'first')
        db.add_entry('Bravo', 'second')

        harness = _CacheHarness(db)
        harness.load_entries_from_db()

        titles = [harness.entries_store.get_item(i).title for i in range(harness.entries_store.get_n_items())]
        self.assertEqual(titles, ['Alpha', 'Bravo'])

    def test_second_load_replaces_store(self):
        db = Database(':memory:')
        db.add_entry('Alpha')
        harness = _CacheHarness(db)
        harness.load_entries_from_db()
        self.assertEqual(harness.entries_store.get_n_items(), 1)

        db.add_entry('Bravo')
        harness.load_entries_from_db()
        self.assertEqual(harness.entries_store.get_n_items(), 2)


class LanguageResolutionCachingTests(unittest.TestCase):
    """t() must not re-resolve the language (and deep-copy the config) per call."""

    def setUp(self):
        import i18n
        self.i18n = i18n
        i18n.invalidate_i18n_cache()

    def tearDown(self):
        self.i18n.invalidate_i18n_cache()

    def test_repeated_t_resolves_language_once(self):
        with mock.patch.object(self.i18n, '_resolve_language_code', return_value='en') as resolve:
            for _ in range(50):
                self.i18n.t('app_title')
        self.assertEqual(resolve.call_count, 1)

    def test_invalidation_resolves_again(self):
        with mock.patch.object(self.i18n, '_resolve_language_code', return_value='en') as resolve:
            self.i18n.get_language_code()
            self.i18n.invalidate_i18n_cache()
            self.i18n.get_language_code()
        self.assertEqual(resolve.call_count, 2)

    def test_saving_config_resolves_again(self):
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(self.i18n, '_resolve_language_code', return_value='en') as resolve, \
                mock.patch.object(self.i18n, 'USER_CONFIG_DIR', Path(tmp.name)), \
                mock.patch.object(self.i18n, 'USER_CONFIG_PATH', Path(tmp.name) / 'config.json'), \
                mock.patch.object(self.i18n, '_CONFIG_CACHE', None):
            self.i18n.get_language_code()
            self.i18n.save_app_config({'language': 'de'})
            self.i18n.get_language_code()
        self.assertEqual(resolve.call_count, 2)


class OptionKeyLabelLookupTests(unittest.TestCase):
    """option_key_from_any must match translated labels without translating per lookup."""

    def test_translated_label_maps_to_key(self):
        import browser_option_logic as bol
        spec = next(spec for spec in bol.BROWSER_OPTION_SPECS if spec.label_key)
        label = bol.t(spec.label_key)
        if label in bol.registry_browser_managed_option_keys():
            self.skipTest('label coincides with a canonical key')
        self.assertEqual(bol.option_key_from_any(label), spec.key)

    def test_lookup_does_not_translate_again(self):
        import browser_option_logic as bol
        bol.option_key_from_any('No Such Option')
        with mock.patch.object(bol, 't', side_effect=AssertionError('t() called')):
            self.assertEqual(bol.option_key_from_any('Still No Such Option'), 'Still No Such Option')


class _BusyHarness(MainWindowEntriesMixin):
    def __init__(self):
        self._profile_size_cache = {}
        self._profile_size_pending = set()
        self._startup_waiting_for_profile_sizes = True
        self.busy_hidden = 0

    def _hide_busy(self):
        self.busy_hidden += 1


class StartupSpinnerProfileSizeTests(unittest.TestCase):
    """A stale size already on screen must not hold the startup spinner."""

    def test_refresh_of_shown_size_does_not_block(self):
        harness = _BusyHarness()
        harness._profile_size_cache[1] = {'path': '/p', 'text': '80 MB'}
        harness._profile_size_pending.add(1)
        harness._maybe_finish_startup_busy()
        self.assertEqual(harness.busy_hidden, 1)

    def test_first_measurement_blocks(self):
        harness = _BusyHarness()
        harness._profile_size_pending.add(1)
        harness._maybe_finish_startup_busy()
        self.assertEqual(harness.busy_hidden, 0)
        harness._profile_size_pending.discard(1)
        harness._profile_size_cache[1] = {'path': '/p', 'text': '80 MB'}
        harness._maybe_finish_startup_busy()
        self.assertEqual(harness.busy_hidden, 1)


if __name__ == '__main__':
    unittest.main()

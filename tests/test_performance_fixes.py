import logging
import sys
import types
import unittest
from types import SimpleNamespace
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
        """After the first call, shutil.which must not be consulted again."""
        with mock.patch('engine_support.shutil.which') as which_mock:
            which_mock.side_effect = lambda cmd: f'/usr/bin/{cmd}'
            first = engine_support.available_engines()
            which_mock.reset_mock()
            second = engine_support.available_engines()

            which_mock.assert_not_called()
            self.assertEqual(first, second)

    def test_returns_independent_copies(self):
        """Callers must not be able to mutate each other's data through the cache."""
        with mock.patch('engine_support.shutil.which', side_effect=lambda cmd: f'/usr/bin/{cmd}'):
            first = engine_support.available_engines()
            if not first:
                self.skipTest('no engines configured on this system')
            first[0]['command'] = 'mutated'
            second = engine_support.available_engines()

        self.assertNotEqual(second[0]['command'], 'mutated')

    def test_cache_respects_availability(self):
        """Engines whose command cannot be resolved must not appear."""
        with mock.patch('engine_support.shutil.which', return_value=None):
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


if __name__ == '__main__':
    unittest.main()

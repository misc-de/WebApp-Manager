"""Coverage-driven tests for profile_size_cache: corrupt files, empty keys, write failures."""
import json
import logging
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_profile_size_cache.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import profile_size_cache as psc


class _CacheCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        previous = (psc.CACHE_DIR, psc.CACHE_PATH, psc._CACHE, psc._DIRTY)

        def restore():
            psc.CACHE_DIR, psc.CACHE_PATH, psc._CACHE, psc._DIRTY = previous

        self.addCleanup(restore)
        psc.CACHE_DIR = root / 'cache'
        psc.CACHE_PATH = psc.CACHE_DIR / 'profile-sizes.json'
        psc._CACHE = None
        psc._DIRTY = False
        self.profile = root / 'profile'
        self.profile.mkdir()

    def write_cache_file(self, data):
        psc.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        psc.CACHE_PATH.write_text(data if isinstance(data, str) else json.dumps(data), encoding='utf-8')


class FormatSizeTests(unittest.TestCase):
    def test_garbage_and_non_positive_values_are_zero(self):
        for value in ('lots', object(), None, 0, -5):
            self.assertEqual(psc.format_size(value), '0 MB')

    def test_megabytes_and_gigabytes(self):
        self.assertEqual(psc.format_size(5 * 1024 ** 2), '5 MB')
        self.assertEqual(psc.format_size('2147483648'), '2.00 GB')


class LoadTests(_CacheCase):
    def test_corrupt_file_starts_an_empty_cache(self):
        self.write_cache_file('{broken')
        self.assertEqual(psc.lookup_bytes(str(self.profile)), (None, True))

    def test_non_object_file_is_ignored(self):
        self.write_cache_file([1, 2, 3])
        self.assertEqual(psc._load(), {})

    def test_malformed_records_are_skipped(self):
        key = str(self.profile)
        self.write_cache_file({'profiles': {
            'not-a-dict': 5,
            'bad-number': {'bytes': 'many'},
            key: {'bytes': '2048', 'mtime': 1, 'measured_at': 2},
        }})
        self.assertEqual(psc._load(), {key: {'bytes': 2048, 'mtime': 1.0, 'measured_at': 2.0}})

    def test_missing_profiles_section_is_empty(self):
        self.write_cache_file({'version': 1, 'profiles': None})
        self.assertEqual(psc._load(), {})


class EmptyKeyTests(_CacheCase):
    def test_blank_path_is_never_cached(self):
        self.assertEqual(psc.lookup_bytes('  '), (None, False))
        self.assertEqual(psc.lookup(None), (None, False))
        self.assertEqual(psc.store('', 5 * 1024 ** 2), '5 MB')
        psc.forget('')
        self.assertEqual(psc.measure(''), '')
        self.assertEqual(psc._load(), {})
        self.assertFalse(psc._DIRTY)


class StalenessTests(_CacheCase):
    def test_record_from_the_future_is_stale(self):
        key = str(self.profile)
        psc.store(key, 1024 ** 2)
        psc._load()[key]['measured_at'] = time.time() + 3600
        self.assertEqual(psc.lookup(key), ('1 MB', True))

    def test_vanished_root_keeps_comparing_as_unchanged(self):
        # Both mtimes read as 0.0; flush() is what drops such records.
        key = str(self.profile / 'gone')
        psc.store(key, 1024 ** 2)
        self.assertEqual(psc._load()[key]['mtime'], 0.0)
        self.assertEqual(psc.lookup_bytes(key), (1024 ** 2, False))


class MeasureTests(_CacheCase):
    def test_measure_remembers_the_walk_result(self):
        with mock.patch.object(psc, 'get_profile_size_bytes', return_value=3 * 1024 ** 2) as walk:
            self.assertEqual(psc.measure(str(self.profile)), '3 MB')
        walk.assert_called_once_with(str(self.profile))
        self.assertEqual(psc.lookup_bytes(str(self.profile)), (3 * 1024 ** 2, False))

    def test_walk_failure_is_not_remembered(self):
        with mock.patch.object(psc, 'get_profile_size_bytes', side_effect=OSError('denied')):
            self.assertEqual(psc.measure(str(self.profile)), '')
        self.assertEqual(psc.lookup_bytes(str(self.profile)), (None, True))


class ForgetTests(_CacheCase):
    def test_forgetting_an_unknown_profile_does_not_mark_dirty(self):
        psc.forget(str(self.profile))
        self.assertFalse(psc._DIRTY)


class FlushTests(_CacheCase):
    def test_clean_cache_is_not_written(self):
        psc.flush()
        self.assertFalse(psc.CACHE_PATH.exists())

    def test_flush_writes_atomically_and_clears_dirty(self):
        psc.store(str(self.profile), 1024 ** 2)
        psc.flush([str(self.profile)])
        self.assertFalse(psc._DIRTY)
        data = json.loads(psc.CACHE_PATH.read_text(encoding='utf-8'))
        self.assertEqual(data['version'], 1)
        self.assertEqual(data['profiles'][str(self.profile)]['bytes'], 1024 ** 2)
        self.assertFalse(psc.CACHE_PATH.with_name('profile-sizes.json.tmp').exists())

    def test_write_failure_is_logged_and_retried_later(self):
        psc.store(str(self.profile), 1024 ** 2)
        with mock.patch.object(Path, 'write_text', side_effect=OSError('read-only')), \
                self.assertLogs(psc.LOG, level='WARNING') as logs:
            psc.flush()
        self.assertIn('Failed to persist', logs.output[0])
        self.assertTrue(psc._DIRTY)
        psc.flush()
        self.assertTrue(psc.CACHE_PATH.exists())
        self.assertFalse(psc._DIRTY)


if __name__ == '__main__':
    unittest.main()

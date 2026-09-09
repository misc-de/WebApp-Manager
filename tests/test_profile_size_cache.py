"""Tests for the remembered browser-profile sizes.

The point of this cache is that the app must not walk a browser profile just
to put a number on a list row: once measured, a size survives a restart, and a
value only counts as stale when the profile's root directory was touched or
the record has aged out. These tests pin exactly that, plus the scandir walk
underneath it.
"""
import logging
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.profile_size_cache.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import profile_size_cache
from browser_paths import get_profile_size_bytes


class ProfileSizeWalkTests(unittest.TestCase):
    def test_sums_nested_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'a').write_bytes(b'x' * 100)
            (root / 'sub' / 'deep').mkdir(parents=True)
            (root / 'sub' / 'b').write_bytes(b'y' * 50)
            (root / 'sub' / 'deep' / 'c').write_bytes(b'z' * 25)
            self.assertEqual(get_profile_size_bytes(str(root)), 175)

    def test_does_not_follow_symlinked_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / 'outside'
            outside.mkdir()
            (outside / 'big').write_bytes(b'x' * 1000)
            profile = root / 'profile'
            profile.mkdir()
            (profile / 'own').write_bytes(b'y' * 10)
            try:
                (profile / 'link').symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest('symlinks are not available here')
            self.assertEqual(get_profile_size_bytes(str(profile)), 10)

    def test_missing_path_is_zero(self):
        self.assertEqual(get_profile_size_bytes('/nonexistent/webapp/profile'), 0)
        self.assertEqual(get_profile_size_bytes(''), 0)


class ProfileSizeCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self._previous = (profile_size_cache.CACHE_DIR, profile_size_cache.CACHE_PATH, profile_size_cache._CACHE, profile_size_cache._DIRTY)
        profile_size_cache.CACHE_DIR = root / 'cache'
        profile_size_cache.CACHE_PATH = profile_size_cache.CACHE_DIR / 'profile-sizes.json'
        profile_size_cache._CACHE = None
        profile_size_cache._DIRTY = False
        self.profile = root / 'profile'
        self.profile.mkdir()
        (self.profile / 'file').write_bytes(b'x' * 2048)

    def tearDown(self):
        profile_size_cache.CACHE_DIR, profile_size_cache.CACHE_PATH, profile_size_cache._CACHE, profile_size_cache._DIRTY = self._previous

    def test_unknown_profile_asks_for_measurement(self):
        text, stale = profile_size_cache.lookup(str(self.profile))
        self.assertIsNone(text)
        self.assertTrue(stale)

    def test_measured_value_is_fresh_and_formatted(self):
        self.assertEqual(profile_size_cache.measure(str(self.profile)), '0 MB')
        text, stale = profile_size_cache.lookup(str(self.profile))
        self.assertEqual(text, '0 MB')
        self.assertFalse(stale)

    def test_value_survives_a_restart(self):
        profile_size_cache.measure(str(self.profile))
        profile_size_cache.flush()
        self.assertTrue(profile_size_cache.CACHE_PATH.exists())

        profile_size_cache._CACHE = None  # simulate a fresh process
        size_bytes, stale = profile_size_cache.lookup_bytes(str(self.profile))
        self.assertEqual(size_bytes, 2048)
        self.assertFalse(stale)

    def test_touching_the_profile_root_marks_it_stale(self):
        profile_size_cache.measure(str(self.profile))
        _text, stale = profile_size_cache.lookup(str(self.profile))
        self.assertFalse(stale)

        future = time.time() + 120
        os.utime(self.profile, (future, future))
        _text, stale = profile_size_cache.lookup(str(self.profile))
        self.assertTrue(stale)

    def test_aged_out_record_is_stale(self):
        profile_size_cache.measure(str(self.profile))
        record = profile_size_cache._load()[str(self.profile)]
        record['measured_at'] = time.time() - profile_size_cache.MAX_CACHE_AGE_SECONDS - 1
        _text, stale = profile_size_cache.lookup(str(self.profile))
        self.assertTrue(stale)

    def test_flush_prunes_profiles_that_are_no_longer_known(self):
        profile_size_cache.measure(str(self.profile))
        other = Path(self._tmp.name) / 'other'
        other.mkdir()
        profile_size_cache.store(str(other), 4096)

        profile_size_cache.flush([str(self.profile)])
        self.assertIn(str(self.profile), profile_size_cache._load())
        self.assertNotIn(str(other), profile_size_cache._load())

    def test_flush_drops_profiles_that_disappeared(self):
        profile_size_cache.store('/nonexistent/webapp/profile', 1234)
        profile_size_cache.flush()
        self.assertNotIn('/nonexistent/webapp/profile', profile_size_cache._load())

    def test_forget_removes_a_single_record(self):
        profile_size_cache.measure(str(self.profile))
        profile_size_cache.forget(str(self.profile))
        text, _stale = profile_size_cache.lookup(str(self.profile))
        self.assertIsNone(text)

    def test_format_size_switches_to_gigabytes(self):
        self.assertEqual(profile_size_cache.format_size(0), '0 MB')
        self.assertEqual(profile_size_cache.format_size(5 * 1024 ** 2), '5 MB')
        self.assertEqual(profile_size_cache.format_size(2 * 1024 ** 3), '2.00 GB')


if __name__ == '__main__':
    unittest.main()

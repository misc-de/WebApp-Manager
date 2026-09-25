"""Coverage tests for browser_paths: profile-root math, managed-profile
markers, family detection, safe deletion and the extension-config merge.

Every test redirects FIREFOX_ROOT / CHROMIUM_PROFILE_ROOT into a temporary
directory, so nothing here can touch the real user's browser profiles.
"""
import json
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_browser_paths.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import browser_paths


class _SandboxedRootsMixin:
    """Points both managed-profile roots at a throwaway directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.firefox_root = self.base / 'firefox'
        self.chromium_root = self.base / 'chromium-profiles'
        self.firefox_root.mkdir()
        (self.chromium_root / 'chrome').mkdir(parents=True)
        (self.chromium_root / 'chromium').mkdir(parents=True)
        for name, value in (('FIREFOX_ROOT', self.firefox_root), ('CHROMIUM_PROFILE_ROOT', self.chromium_root)):
            patcher = mock.patch.object(browser_paths, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class NormalizationTests(unittest.TestCase):
    def test_color_scheme_is_trimmed_lowercased_and_defaults_to_auto(self):
        self.assertEqual(browser_paths.normalize_color_scheme(' DARK '), 'dark')
        self.assertEqual(browser_paths.normalize_color_scheme('light'), 'light')
        self.assertEqual(browser_paths.normalize_color_scheme(None), 'auto')
        self.assertEqual(browser_paths.normalize_color_scheme('sepia'), 'auto')

    def test_default_zoom_only_accepts_listed_steps(self):
        self.assertEqual(browser_paths.normalize_default_zoom(' 125 '), '125')
        self.assertEqual(browser_paths.normalize_default_zoom(None), '100')
        self.assertEqual(browser_paths.normalize_default_zoom('133'), '100')

    def test_append_unique_csv_arg_deduplicates_and_skips_empty_values(self):
        parts = ['browser']
        browser_paths.append_unique_csv_arg(parts, '--enable-features=', ['A', '', 'B', 'A', None])
        self.assertEqual(parts, ['browser', '--enable-features=A,B'])

    def test_append_unique_csv_arg_appends_nothing_without_values(self):
        parts = ['browser']
        browser_paths.append_unique_csv_arg(parts, '--x=', ['', None])
        self.assertEqual(parts, ['browser'])


class ProfileRootTests(_SandboxedRootsMixin, unittest.TestCase):
    def test_root_per_family(self):
        self.assertEqual(browser_paths._profile_root_for_family(' Firefox '), self.firefox_root)
        self.assertEqual(browser_paths._profile_root_for_family('chrome'), self.chromium_root / 'chrome')
        self.assertEqual(browser_paths._profile_root_for_family('CHROMIUM'), self.chromium_root / 'chromium')
        self.assertIsNone(browser_paths._profile_root_for_family('safari'))
        self.assertIsNone(browser_paths._profile_root_for_family(None))


class ManagedMarkerTests(_SandboxedRootsMixin, unittest.TestCase):
    def _marker(self, profile_dir):
        return profile_dir / browser_paths.MANAGED_PROFILE_MARKER

    def test_missing_marker_is_not_managed(self):
        profile = self.firefox_root / 'plain'
        profile.mkdir()
        self.assertFalse(browser_paths._has_managed_profile_marker(profile))

    def test_marker_that_is_a_directory_is_not_managed(self):
        profile = self.firefox_root / 'plain'
        self._marker(profile).mkdir(parents=True)
        self.assertFalse(browser_paths._has_managed_profile_marker(profile))

    def test_corrupt_marker_is_not_managed(self):
        profile = self.firefox_root / 'broken'
        profile.mkdir()
        self._marker(profile).write_text('{not json', encoding='utf-8')
        self.assertFalse(browser_paths._has_managed_profile_marker(profile))

    def test_marker_from_another_tool_is_not_managed(self):
        profile = self.firefox_root / 'foreign'
        profile.mkdir()
        self._marker(profile).write_text(json.dumps({'managed_by': 'someone-else', 'family': 'firefox'}), encoding='utf-8')
        self.assertFalse(browser_paths._has_managed_profile_marker(profile, 'firefox'))

    def test_family_must_match_when_given(self):
        profile = self.firefox_root / 'mine'
        browser_paths._write_managed_profile_marker(profile, 'Firefox')
        self.assertTrue(browser_paths._has_managed_profile_marker(profile))
        self.assertTrue(browser_paths._has_managed_profile_marker(profile, 'FIREFOX'))
        self.assertFalse(browser_paths._has_managed_profile_marker(profile, 'chrome'))

    def test_write_marker_creates_directory_and_payload(self):
        profile = self.chromium_root / 'chrome' / 'new' / 'nested'
        browser_paths._write_managed_profile_marker(profile, ' Chrome ')
        data = json.loads(self._marker(profile).read_text(encoding='utf-8'))
        self.assertEqual(data, {'managed_by': 'webapp-manager', 'family': 'chrome', 'version': 1})

    def test_write_marker_skips_identical_content(self):
        profile = self.firefox_root / 'same'
        browser_paths._write_managed_profile_marker(profile, 'firefox')
        with mock.patch.object(Path, 'write_text') as write_text:
            browser_paths._write_managed_profile_marker(profile, 'firefox')
        write_text.assert_not_called()

    def test_write_marker_rewrites_when_current_marker_is_unreadable(self):
        profile = self.firefox_root / 'unreadable'
        browser_paths._write_managed_profile_marker(profile, 'firefox')
        with mock.patch.object(Path, 'read_text', side_effect=OSError('denied')), \
                mock.patch.object(Path, 'write_text') as write_text:
            browser_paths._write_managed_profile_marker(profile, 'firefox')
        write_text.assert_called_once()


class ExplicitlyManagedTests(_SandboxedRootsMixin, unittest.TestCase):
    def test_empty_path_or_unknown_family_is_rejected(self):
        self.assertFalse(browser_paths._is_explicitly_managed_profile_dir('', 'firefox'))
        self.assertFalse(browser_paths._is_explicitly_managed_profile_dir(str(self.firefox_root), 'opera'))

    def test_unresolvable_path_is_rejected(self):
        with mock.patch.object(Path, 'resolve', side_effect=OSError('loop')):
            self.assertFalse(browser_paths._is_explicitly_managed_profile_dir(str(self.firefox_root / 'x'), 'firefox'))

    def test_root_itself_and_outside_paths_are_rejected(self):
        outside = self.base / 'webapp_outside'
        outside.mkdir()
        self.assertFalse(browser_paths._is_explicitly_managed_profile_dir(str(self.firefox_root), 'firefox'))
        self.assertFalse(browser_paths._is_explicitly_managed_profile_dir(str(outside), 'firefox'))
        self.assertFalse(browser_paths._is_explicitly_managed_profile_dir(str(self.firefox_root / 'missing'), 'firefox'))

    def test_legacy_name_or_marker_marks_a_profile_as_managed(self):
        legacy = self.firefox_root / 'webapp_legacy'
        legacy.mkdir()
        marked = self.chromium_root / 'chromium' / 'fresh'
        browser_paths._write_managed_profile_marker(marked, 'chromium')
        unmarked = self.firefox_root / 'personal'
        unmarked.mkdir()
        self.assertTrue(browser_paths._is_explicitly_managed_profile_dir(str(legacy), 'firefox'))
        self.assertTrue(browser_paths._is_explicitly_managed_profile_dir(str(marked), 'chromium'))
        self.assertFalse(browser_paths._is_explicitly_managed_profile_dir(str(unmarked), 'firefox'))

    def test_is_managed_profile_path_wraps_the_explicit_check(self):
        legacy = self.firefox_root / 'webapp_legacy'
        legacy.mkdir()
        self.assertFalse(browser_paths._is_managed_profile_path('', 'firefox'))
        self.assertTrue(browser_paths._is_managed_profile_path(str(legacy), 'firefox'))


class ExtensionConfigTests(unittest.TestCase):
    def test_defaults_when_no_user_config(self):
        with mock.patch.object(browser_paths, 'get_app_config', return_value=None):
            adblock = browser_paths.get_firefox_extension_config('adblock')
        self.assertEqual(adblock, browser_paths.DEFAULT_FIREFOX_EXTENSIONS['adblock'])
        self.assertIsNot(adblock, browser_paths.DEFAULT_FIREFOX_EXTENSIONS['adblock'])

    def test_user_config_overrides_generic_extension(self):
        config = {'firefox_extensions': {'adblock': {'id': 'custom@id'}}}
        with mock.patch.object(browser_paths, 'get_app_config', return_value=config):
            adblock = browser_paths.get_firefox_extension_config('adblock')
        self.assertEqual(adblock['id'], 'custom@id')
        self.assertEqual(adblock['marker_file'], '.webapp_adblock_extension_id')

    def test_swipe_config_cannot_enable_unsigned_dev_bundles(self):
        config = {'firefox_extensions': {'swipe': {
            'bundle_path': '',
            'dev_bundle_path': '/tmp/evil.xpi',
            'allow_unsigned_local_bundle': True,
            'marker_file': '',
        }}}
        with mock.patch.object(browser_paths, 'get_app_config', return_value=config):
            swipe = browser_paths.get_firefox_extension_config('swipe')
        self.assertEqual(swipe['bundle_path'], 'extension/swipe-gestures.xpi')
        self.assertEqual(swipe['dev_bundle_path'], '')
        self.assertFalse(swipe['allow_unsigned_local_bundle'])
        self.assertEqual(swipe['marker_file'], '.webapp_secure_swipe_extension_id')

    def test_unknown_extension_yields_empty_config(self):
        with mock.patch.object(browser_paths, 'get_app_config', return_value={}):
            self.assertEqual(browser_paths.get_firefox_extension_config('nope'), {})


class ProfileSizeTests(unittest.TestCase):
    def test_empty_or_unresolvable_path_is_zero(self):
        self.assertEqual(browser_paths.get_profile_size_bytes(''), 0)
        with mock.patch.object(Path, 'resolve', side_effect=OSError('loop')):
            self.assertEqual(browser_paths.get_profile_size_bytes('/anything'), 0)

    def test_missing_directory_is_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(browser_paths.get_profile_size_bytes(Path(tmpdir) / 'missing'), 0)

    def test_nested_files_are_summed_without_following_symlinks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'profile'
            (root / 'a' / 'b').mkdir(parents=True)
            (root / 'top.bin').write_bytes(b'x' * 10)
            (root / 'a' / 'b' / 'deep.bin').write_bytes(b'y' * 5)
            outside = Path(tmpdir) / 'outside.bin'
            outside.write_bytes(b'z' * 1000)
            os.symlink(outside, root / 'link.bin')
            os.symlink(Path(tmpdir), root / 'a' / 'loop')
            self.assertEqual(browser_paths.get_profile_size_bytes(root), 15)

    def test_entry_that_fails_to_stat_is_skipped(self):
        class _Entry:
            def __init__(self, path, size, fail=False):
                self.path = path
                self._size = size
                self._fail = fail

            def is_dir(self, follow_symlinks=True):
                return False

            def is_file(self, follow_symlinks=True):
                if self._fail:
                    raise OSError('vanished')
                return True

            def stat(self, follow_symlinks=True):
                return types.SimpleNamespace(st_size=self._size)

        class _Scan:
            def __enter__(self):
                return iter([_Entry('/p/gone', 99, fail=True), _Entry('/p/ok', 7)])

            def __exit__(self, *exc):
                return False

        with tempfile.TemporaryDirectory() as tmpdir, \
                mock.patch.object(browser_paths.os, 'scandir', return_value=_Scan()):
            self.assertEqual(browser_paths.get_profile_size_bytes(tmpdir), 7)


class RemovalTests(_SandboxedRootsMixin, unittest.TestCase):
    def test_remove_path_if_exists_handles_missing_dir_and_file(self):
        logger = _build_test_logger('remove')
        missing = self.base / 'missing'
        directory = self.base / 'cache'
        (directory / 'sub').mkdir(parents=True)
        single = self.base / 'file.bin'
        single.write_bytes(b'1')
        self.assertFalse(browser_paths._remove_path_if_exists(missing, logger))
        self.assertTrue(browser_paths._remove_path_if_exists(directory, logger))
        self.assertTrue(browser_paths._remove_path_if_exists(single, logger))
        self.assertFalse(directory.exists())
        self.assertFalse(single.exists())

    def test_remove_path_if_exists_reports_failure(self):
        logger = mock.Mock()
        directory = self.base / 'stuck'
        directory.mkdir()
        with mock.patch.object(browser_paths.shutil, 'rmtree', side_effect=OSError('busy')):
            self.assertFalse(browser_paths._remove_path_if_exists(directory, logger, 'Firefox cache'))
        logger.warning.assert_called_once()
        self.assertIn('Firefox cache', logger.warning.call_args.args)

    def test_safe_remove_tree_deletes_inside_the_root(self):
        logger = mock.Mock()
        profile = self.firefox_root / 'webapp_gone'
        (profile / 'sub').mkdir(parents=True)
        browser_paths._safe_remove_tree(profile, self.firefox_root, logger)
        self.assertFalse(profile.exists())
        logger.info.assert_called_once()

    def test_safe_remove_tree_refuses_the_root_and_outside_paths(self):
        logger = mock.Mock()
        outside = self.base / 'outside'
        outside.mkdir()
        browser_paths._safe_remove_tree(self.firefox_root, self.firefox_root, logger)
        browser_paths._safe_remove_tree(outside, self.firefox_root, logger)
        self.assertTrue(self.firefox_root.exists())
        self.assertTrue(outside.exists())
        self.assertEqual(logger.warning.call_count, 2)

    def test_safe_remove_tree_ignores_missing_and_unresolvable_paths(self):
        logger = mock.Mock()
        browser_paths._safe_remove_tree(self.firefox_root / 'missing', self.firefox_root, logger)
        with mock.patch.object(Path, 'resolve', side_effect=OSError('loop')):
            browser_paths._safe_remove_tree(self.firefox_root / 'x', self.firefox_root, logger)
        logger.warning.assert_not_called()
        logger.info.assert_not_called()


class FamilyDetectionTests(_SandboxedRootsMixin, unittest.TestCase):
    def test_path_within(self):
        self.assertTrue(browser_paths._path_within(self.firefox_root, self.firefox_root))
        self.assertTrue(browser_paths._path_within(self.firefox_root / 'a' / 'b', self.firefox_root))
        self.assertFalse(browser_paths._path_within(self.base, self.firefox_root))
        with mock.patch.object(Path, 'resolve', side_effect=OSError('loop')):
            self.assertFalse(browser_paths._path_within(self.firefox_root, self.firefox_root))

    def test_family_detected_from_root(self):
        self.assertEqual(browser_paths._detect_managed_profile_family(self.firefox_root / 'p'), 'firefox')
        self.assertEqual(browser_paths._detect_managed_profile_family(self.chromium_root / 'chrome' / 'p'), 'chrome')
        self.assertEqual(browser_paths._detect_managed_profile_family(self.chromium_root / 'chromium' / 'p'), 'chromium')
        self.assertIsNone(browser_paths._detect_managed_profile_family(self.base / 'elsewhere'))
        self.assertIsNone(browser_paths._detect_managed_profile_family(''))

    def test_family_detection_survives_unresolvable_path(self):
        with mock.patch.object(Path, 'resolve', side_effect=OSError('loop')):
            self.assertIsNone(browser_paths._detect_managed_profile_family(self.firefox_root / 'p'))


if __name__ == '__main__':
    unittest.main()

"""Coverage tests for browser_profiles: the apply_profile_settings
orchestrator, browser command resolution, Firefox profiles.ini registration,
profile creation/copy, the unused-profile renamer and deletion.

FIREFOX_ROOT / CHROMIUM_PROFILE_ROOT are redirected into a temporary directory
in both modules that read them (browser_paths and browser_profiles), and
host_which is stubbed, so no real profile is touched and no browser is probed.
"""
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_browser_profiles.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import browser_paths
import browser_profiles as bp
from webapp_constants import (
    APP_MODE_KEY,
    OPTION_CLEAR_CACHE_ON_EXIT_KEY,
    OPTION_PRESERVE_SESSION_KEY,
)


class _SandboxMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.firefox_root = self.base / 'mozilla' / 'firefox'
        self.chromium_root = self.base / 'profiles'
        for module in (browser_paths, bp):
            for name, value in (('FIREFOX_ROOT', self.firefox_root), ('CHROMIUM_PROFILE_ROOT', self.chromium_root)):
                patcher = mock.patch.object(module, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)
        self.logger = mock.Mock()
        self.profiles_ini = self.firefox_root / 'profiles.ini'

    def _managed(self, family, name):
        root = self.firefox_root if family == 'firefox' else self.chromium_root / family
        path = root / name
        browser_paths._write_managed_profile_marker(path, family)
        return path


class ApplyProfileSettingsTests(_SandboxMixin, unittest.TestCase):
    def test_missing_profile_info(self):
        self.assertEqual(bp.apply_profile_settings(None, {}, self.logger), {'extension_errors': {}})

    def test_extension_errors_keep_failures_only(self):
        errors = bp._extension_errors_from_results(a={'error': 'x'}, b={'error': None}, c='not a dict')
        self.assertEqual(errors, {'a': 'x'})

    def test_non_managed_profile_is_refused(self):
        foreign = self.base / 'foreign'
        foreign.mkdir()
        with mock.patch.object(bp, '_write_firefox_user_js') as write:
            result = bp.apply_profile_settings({'browser_family': 'firefox', 'profile_path': str(foreign)}, {}, self.logger)
        self.assertEqual(result, {'extension_errors': {}})
        write.assert_not_called()
        self.logger.warning.assert_called_once()

    def _preserve_and_clear(self):
        return {OPTION_CLEAR_CACHE_ON_EXIT_KEY: '1', OPTION_PRESERVE_SESSION_KEY: '1'}

    def test_firefox_clears_runtime_caches_when_session_is_preserved(self):
        profile = self._managed('firefox', 'webapp_one')
        ok = {'requested': False, 'installed': False, 'changed': False, 'error': None}
        with mock.patch.object(bp, '_clear_firefox_runtime_caches') as clear, \
                mock.patch.object(bp, '_write_firefox_user_js') as write, \
                mock.patch.object(bp, '_sync_firefox_app_mode_css') as css, \
                mock.patch.object(bp, '_sync_firefox_adblock', return_value=ok), \
                mock.patch.object(bp, '_sync_firefox_swipe_extension', return_value=ok), \
                mock.patch.object(bp, 'ensure_profile_customizations'), \
                mock.patch.object(bp, '_invalidate_firefox_extension_state'):
            result = bp.apply_profile_settings({'browser_family': 'firefox', 'profile_path': str(profile)}, self._preserve_and_clear(), self.logger)
        self.assertEqual(result, {'extension_errors': {}})
        clear.assert_called_once_with(str(profile), self.logger)
        settings = write.call_args.args[1]
        self.assertTrue(settings.clear_cache)
        self.assertTrue(settings.previous_session)
        css.assert_called_once_with(str(profile), False, False, self.logger)

    def test_chromium_writes_preferences_and_clears_caches(self):
        profile = self._managed('chromium', 'p1')
        with mock.patch.object(bp, '_clear_chromium_runtime_caches') as clear, \
                mock.patch.object(bp, '_write_chromium_preferences') as write, \
                mock.patch.object(bp, 'ensure_profile_customizations') as custom:
            result = bp.apply_profile_settings({'browser_family': 'chromium', 'profile_path': str(profile)}, self._preserve_and_clear(), self.logger)
        self.assertEqual(result, {'extension_errors': {}})
        clear.assert_called_once()
        write.assert_called_once()
        custom.assert_called_once()

    def test_app_mode_disables_session_restore(self):
        profile = self._managed('chrome', 'p2')
        options = {**self._preserve_and_clear(), APP_MODE_KEY: '1'}
        with mock.patch.object(bp, '_clear_chromium_runtime_caches') as clear, \
                mock.patch.object(bp, '_write_chromium_preferences') as write, \
                mock.patch.object(bp, 'ensure_profile_customizations'):
            bp.apply_profile_settings({'browser_family': 'chrome', 'profile_path': str(profile)}, options, self.logger)
        clear.assert_not_called()
        self.assertFalse(write.call_args.args[1].previous_session)

    def test_generic_family_does_nothing(self):
        self.assertEqual(bp.apply_profile_settings({'browser_family': 'generic', 'profile_path': ''}, {}, self.logger), {'extension_errors': {}})


class CommandTests(unittest.TestCase):
    def test_resolve_prefers_installed_alias(self):
        logger = mock.Mock()
        with mock.patch.object(bp, 'host_which', side_effect=lambda name: name == 'chromium-browser'):
            self.assertEqual(bp.resolve_browser_command('Chrome', logger), 'chromium-browser')
            self.assertEqual(bp.resolve_browser_command('chromium', logger), 'chromium-browser')
        with mock.patch.object(bp, 'host_which', side_effect=lambda name: name == 'firefox-esr'):
            self.assertEqual(bp.resolve_browser_command('firefox', logger), 'firefox-esr')
        logger.warning.assert_not_called()

    def test_resolve_falls_back_to_raw_value(self):
        logger = mock.Mock()
        with mock.patch.object(bp, 'host_which', return_value=None):
            self.assertEqual(bp.resolve_browser_command('/opt/browser', logger), '/opt/browser')
        logger.warning.assert_called_once()

    def test_browser_family(self):
        self.assertEqual(bp._browser_family('/usr/bin/firefox-esr'), 'firefox')
        self.assertEqual(bp._browser_family('chromium-browser'), 'chromium')
        self.assertEqual(bp._browser_family('google-chrome'), 'chrome')
        self.assertEqual(bp._browser_family(None), 'generic')

    def test_user_agent_argument(self):
        logger = mock.Mock()
        parts = []
        bp.append_user_agent_argument(parts, 'firefox', '', logger, 1)
        bp.append_user_agent_argument(parts, 'google-chrome', 'UA/1', logger, 1)
        bp.append_user_agent_argument(parts, 'firefox', 'UA/2', logger, 1)
        self.assertEqual(parts, ['--user-agent=UA/1'])
        logger.warning.assert_not_called()
        bp.append_user_agent_argument(parts, 'epiphany', 'UA/3', logger, 1)
        self.assertEqual(parts, ['--user-agent=UA/1'])
        logger.warning.assert_called_once()


class ProfilesIniTests(_SandboxMixin, unittest.TestCase):
    def test_parse_sections_with_preamble(self):
        sections = bp._parse_profiles_ini_sections('; comment\n[General]\nA=1\n[Profile0]\nName=x\n')
        self.assertEqual([name for name, _ in sections], ['', 'General', 'Profile0'])
        self.assertEqual(bp._parse_profiles_ini_sections(''), [])
        values = bp._parse_ini_key_values(['[Profile0]\n', '# c\n', '\n', 'noequals\n', ' Name = x \n'])
        self.assertEqual(values, {'Name': 'x'})

    def test_upsert_creates_file_with_general_section(self):
        profile = self.firefox_root / 'webapp_a'
        bp._upsert_firefox_profile('webapp_a', profile, self.logger)
        self.assertEqual(
            self.profiles_ini.read_text(encoding='utf-8'),
            '[Profile0]\nName=webapp_a\nIsRelative=1\nPath=webapp_a\nDefault=0\n\n'
            '[General]\nStartWithLastProfile=1\n\n',
        )

    def test_upsert_appends_next_index_and_fills_general(self):
        self.firefox_root.mkdir(parents=True)
        self.profiles_ini.write_text(
            '[General]\nVersion=2\n[Profile3]\nName=default\nIsRelative=1\nPath=abc.default\n',
            encoding='utf-8',
        )
        bp._upsert_firefox_profile('webapp_b', self.firefox_root / 'webapp_b', self.logger)
        content = self.profiles_ini.read_text(encoding='utf-8')
        self.assertIn('[General]\nVersion=2\nStartWithLastProfile=1\n\n', content)
        self.assertIn('Path=abc.default\n\n[Profile4]\nName=webapp_b\n', content)
        backups = list(self.firefox_root.glob('profiles.ini.webapp.*.bak'))
        self.assertEqual(len(backups), 1)

    def test_upsert_replaces_existing_section_and_is_idempotent(self):
        self.firefox_root.mkdir(parents=True)
        self.profiles_ini.write_text(
            '[Profile0]\nName=webapp_c\nIsRelative=1\nPath=old\n\n[General]\nStartWithLastProfile=1\n',
            encoding='utf-8',
        )
        bp._upsert_firefox_profile('webapp_c', self.firefox_root / 'webapp_c', self.logger)
        content = self.profiles_ini.read_text(encoding='utf-8')
        self.assertIn('[Profile0]\nName=webapp_c\nIsRelative=1\nPath=webapp_c\n', content)
        self.assertNotIn('Path=old', content)
        with mock.patch.object(bp, '_backup_profiles_ini') as backup:
            bp._upsert_firefox_profile('webapp_c', self.firefox_root / 'webapp_c', self.logger)
        backup.assert_not_called()

    def test_upsert_with_general_lacking_trailing_blank_line(self):
        self.firefox_root.mkdir(parents=True)
        self.profiles_ini.write_text('[General]\nVersion=2\n\n', encoding='utf-8')
        bp._upsert_firefox_profile('webapp_d', self.firefox_root / 'webapp_d', self.logger)
        self.assertTrue(self.profiles_ini.read_text(encoding='utf-8').startswith('[General]\nVersion=2\nStartWithLastProfile=1\n\n'))

    def test_upsert_aborts_when_ini_is_unreadable(self):
        self.firefox_root.mkdir(parents=True)
        self.profiles_ini.write_text('[General]\n', encoding='utf-8')
        with mock.patch.object(Path, 'read_text', side_effect=OSError('denied')), \
                mock.patch.object(bp, '_write_profiles_ini_sections') as write:
            bp._upsert_firefox_profile('webapp_e', self.firefox_root / 'webapp_e', self.logger)
        write.assert_not_called()
        self.logger.error.assert_called_once()

    def test_write_sections_adds_newline_and_survives_unreadable_current(self):
        self.firefox_root.mkdir(parents=True)
        self.profiles_ini.write_text('old', encoding='utf-8')
        original = Path.read_text
        with mock.patch.object(Path, 'read_text', side_effect=OSError('denied')):
            bp._write_profiles_ini_sections(self.profiles_ini, [('General', ['[General]'])], self.logger)
        self.assertEqual(original(self.profiles_ini, encoding='utf-8'), '[General]\n')
        self.logger.warning.assert_called()

    def test_remove_registration(self):
        self.firefox_root.mkdir(parents=True)
        self.profiles_ini.write_text(
            '[Profile0]\nName=keep\nPath=keep\n\n[Profile1]\nName=webapp_x\nPath=webapp_x\n\n[General]\nStartWithLastProfile=1\n',
            encoding='utf-8',
        )
        bp._remove_firefox_profile_registration('webapp_x', self.firefox_root / 'webapp_x', self.logger)
        content = self.profiles_ini.read_text(encoding='utf-8')
        self.assertNotIn('webapp_x', content)
        self.assertIn('Name=keep', content)
        with mock.patch.object(bp, '_write_profiles_ini_sections') as write:
            bp._remove_firefox_profile_registration('absent', self.firefox_root / 'absent', self.logger)
        write.assert_not_called()

    def test_remove_registration_without_or_with_unreadable_ini(self):
        bp._remove_firefox_profile_registration('x', self.firefox_root / 'x', self.logger)
        self.assertFalse(self.profiles_ini.exists())
        self.profiles_ini.write_text('[Profile0]\nName=x\n', encoding='utf-8')
        with mock.patch.object(Path, 'read_text', side_effect=OSError('denied')):
            bp._remove_firefox_profile_registration('x', self.firefox_root / 'x', self.logger)
        self.logger.error.assert_called_once()
        self.assertIn('Name=x', self.profiles_ini.read_text(encoding='utf-8'))

    def test_backup_prunes_to_ten_and_reports_failures(self):
        self.firefox_root.mkdir(parents=True)
        self.profiles_ini.write_text('x', encoding='utf-8')
        for index in range(12):
            (self.firefox_root / f'profiles.ini.webapp.2000010{index:02d}-000000.bak').write_text('old', encoding='utf-8')
        bp._backup_profiles_ini(self.profiles_ini, self.logger)
        self.assertEqual(len(list(self.firefox_root.glob('profiles.ini.webapp.*.bak'))), 10)
        (self.firefox_root / 'profiles.ini.webapp.19990101-000000.bak').write_text('older', encoding='utf-8')
        with mock.patch.object(Path, 'unlink', side_effect=OSError('busy')):
            bp._backup_profiles_ini(self.profiles_ini, self.logger)
        self.assertTrue(self.logger.warning.called)
        with mock.patch.object(bp.shutil, 'copy2', side_effect=OSError('ro')):
            bp._backup_profiles_ini(self.profiles_ini, self.logger)
        self.assertIn('Failed to create', self.logger.warning.call_args.args[0])

    def test_backup_without_ini_does_nothing(self):
        bp._backup_profiles_ini(self.profiles_ini, self.logger)
        self.assertFalse(self.firefox_root.exists())


class ProfileIdTests(unittest.TestCase):
    def test_generated_ids(self):
        first = bp._generate_profile_id()
        self.assertRegex(first, r'^webapp_[0-9a-f]{12}$')
        self.assertNotEqual(first, bp._generate_profile_id())

    def test_sanitize(self):
        self.assertEqual(bp._sanitize_profile_id(' My Profile!! 2 '), 'my_profile_2')
        self.assertRegex(bp._sanitize_profile_id('!!!'), r'^webapp_[0-9a-f]{12}$')


class CopyAndInspectTests(_SandboxMixin, unittest.TestCase):
    def test_copy_profile_contents(self):
        source = self.base / 'src'
        (source / 'sub').mkdir(parents=True)
        (source / 'prefs.js').write_text('p', encoding='utf-8')
        (source / 'sub' / 'x').write_text('x', encoding='utf-8')
        target = self.base / 'dst'
        bp._copy_profile_contents(source, target, self.logger)
        self.assertEqual((target / 'prefs.js').read_text(encoding='utf-8'), 'p')
        self.assertEqual((target / 'sub' / 'x').read_text(encoding='utf-8'), 'x')

    def test_copy_skips_missing_or_identical_source_and_logs_failures(self):
        target = self.base / 'dst'
        bp._copy_profile_contents(self.base / 'missing', target, self.logger)
        self.assertFalse(target.exists())
        target.mkdir()
        bp._copy_profile_contents(target, target, self.logger)
        source = self.base / 'src'
        source.mkdir()
        (source / 'a').write_text('a', encoding='utf-8')
        with mock.patch.object(bp.shutil, 'copy2', side_effect=OSError('ro')):
            bp._copy_profile_contents(source, target, self.logger)
        self.logger.warning.assert_called_once()

    def test_inspect_firefox_source(self):
        source = self.base / 'ff'
        source.mkdir()
        self.assertFalse(bp.inspect_profile_copy_source(source, 'firefox', self.logger)['valid'])
        self.logger.warning.assert_called_once()
        (source / 'prefs.js').write_text('', encoding='utf-8')
        self.assertEqual(
            bp.inspect_profile_copy_source(str(source), ' Firefox ', self.logger),
            {'valid': True, 'profile_path': str(source), 'profile_name': 'ff'},
        )

    def test_inspect_chromium_source(self):
        source = self.base / 'chr'
        source.mkdir()
        self.assertFalse(bp.inspect_profile_copy_source(source, 'chrome')['valid'])
        (source / 'Local State').write_text('{}', encoding='utf-8')
        self.assertFalse(bp.inspect_profile_copy_source(source, 'chrome')['valid'])
        (source / 'Profile 1').mkdir()
        (source / 'Profile 1' / 'Preferences').write_text('{}', encoding='utf-8')
        self.assertTrue(bp.inspect_profile_copy_source(source, 'chromium')['valid'])
        other = self.base / 'chr2'
        (other / 'Default').mkdir(parents=True)
        (other / 'Local State').write_text('{}', encoding='utf-8')
        (other / 'Default' / 'Preferences').write_text('{}', encoding='utf-8')
        self.assertTrue(bp.inspect_profile_copy_source(other, 'chrome')['valid'])
        with mock.patch.object(Path, 'iterdir', side_effect=OSError('denied')):
            self.assertFalse(bp._is_valid_chromium_user_data_dir(other))

    def test_inspect_rejects_empty_missing_unknown_and_unresolvable(self):
        invalid = {'valid': False, 'profile_path': '', 'profile_name': ''}
        self.assertEqual(bp.inspect_profile_copy_source('', 'firefox'), invalid)
        self.assertEqual(bp.inspect_profile_copy_source(self.base / 'missing', 'firefox'), invalid)
        self.assertEqual(bp.inspect_profile_copy_source(self.base, 'opera'), invalid)
        with mock.patch.object(Path, 'resolve', side_effect=OSError('loop')):
            self.assertEqual(bp.inspect_profile_copy_source(self.base, 'firefox'), invalid)

    def test_validity_helpers_reject_non_directories(self):
        self.assertFalse(bp._is_valid_firefox_profile_dir(self.base / 'missing'))
        self.assertFalse(bp._is_valid_chromium_user_data_dir(self.base / 'missing'))


class RenameUnusedTests(_SandboxMixin, unittest.TestCase):
    def test_unused_managed_profiles_are_renamed(self):
        active = self._managed('firefox', 'webapp_active')
        unused = self._managed('firefox', 'webapp_unused_me')
        stale = self._managed('firefox', 'webapp_stale')
        (self.firefox_root / 'webapp_stale_unused').mkdir()
        personal = self.firefox_root / 'personal'
        personal.mkdir()
        chrome_unused = self._managed('chrome', 'c1')
        self.profiles_ini.write_text('[Profile0]\nName=webapp_stale\nPath=webapp_stale\n', encoding='utf-8')

        renamed = bp.rename_unused_managed_profile_directories([str(active), '', None], self.logger)

        self.assertEqual(
            sorted((item['family'], Path(item['new_path']).name) for item in renamed),
            [('chrome', 'c1_unused'), ('firefox', 'webapp_stale_unused_2')],
        )
        self.assertTrue(active.exists())
        self.assertTrue(unused.exists())
        self.assertTrue(personal.exists())
        self.assertFalse(stale.exists())
        self.assertFalse(chrome_unused.exists())
        self.assertNotIn('webapp_stale', self.profiles_ini.read_text(encoding='utf-8'))

    def test_rename_failures_and_unreadable_roots_are_tolerated(self):
        self._managed('chromium', 'c2')
        self.firefox_root.mkdir(parents=True)
        with mock.patch.object(Path, 'rename', side_effect=OSError('busy')):
            self.assertEqual(bp.rename_unused_managed_profile_directories([], self.logger), [])
        self.logger.warning.assert_called_once()
        with mock.patch.object(Path, 'iterdir', side_effect=OSError('denied')):
            self.assertEqual(bp.rename_unused_managed_profile_directories([], self.logger), [])

    def test_unresolvable_paths_are_skipped(self):
        self._managed('chromium', 'c3')
        with mock.patch.object(Path, 'resolve', side_effect=OSError('loop')):
            self.assertEqual(bp.rename_unused_managed_profile_directories(['/x'], self.logger), [])
        self.assertTrue((self.chromium_root / 'chromium' / 'c3').exists())


class EnsureProfileTests(_SandboxMixin, unittest.TestCase):
    def test_empty_slug_yields_none(self):
        self.assertIsNone(bp.ensure_browser_profile('!!!', 'firefox', self.logger))

    def test_new_firefox_profile_is_created_and_registered(self):
        info = bp.ensure_browser_profile('Mail', 'firefox', self.logger)
        profile = Path(info['profile_path'])
        self.assertEqual(profile.parent, self.firefox_root)
        self.assertTrue(browser_paths._has_managed_profile_marker(profile, 'firefox'))
        self.assertEqual(info['exec_args'], ['-profile', str(profile)])
        self.assertFalse(info['profile_migrated'])
        self.assertIn(f'Name={info["profile_name"]}', self.profiles_ini.read_text(encoding='utf-8'))

    def test_existing_managed_profile_is_reused(self):
        existing = self._managed('chrome', 'webapp_keep')
        info = bp.ensure_browser_profile('Mail', 'google-chrome', self.logger, stored_profile_name='webapp_keep', stored_profile_path=str(existing))
        self.assertEqual(info['profile_path'], str(existing))
        self.assertEqual(info['exec_args'], [f'--user-data-dir={existing}'])
        firefox_existing = self._managed('firefox', 'webapp_ff')
        info = bp.ensure_browser_profile('Mail', 'firefox', self.logger, stored_profile_name='webapp_ff', stored_profile_path=str(firefox_existing))
        self.assertEqual(info['profile_path'], str(firefox_existing))

    def test_foreign_profile_is_copied_into_a_new_managed_one(self):
        source = self.base / 'personal-chromium'
        (source / 'Default').mkdir(parents=True)
        (source / 'Local State').write_text('{}', encoding='utf-8')
        (source / 'Default' / 'Preferences').write_text('{"x": 1}', encoding='utf-8')
        info = bp.ensure_browser_profile('Mail', 'chromium', self.logger, stored_profile_path=str(source))
        target = Path(info['profile_path'])
        self.assertTrue(info['profile_migrated'])
        self.assertEqual(target.parent, self.chromium_root / 'chromium')
        self.assertEqual((target / 'Default' / 'Preferences').read_text(encoding='utf-8'), '{"x": 1}')
        self.assertTrue(source.exists())

        ff_source = self.base / 'personal-ff'
        ff_source.mkdir()
        (ff_source / 'prefs.js').write_text('p', encoding='utf-8')
        info = bp.ensure_browser_profile('Mail', 'firefox', self.logger, stored_profile_path=str(ff_source))
        self.assertTrue(info['profile_migrated'])
        self.assertEqual((Path(info['profile_path']) / 'prefs.js').read_text(encoding='utf-8'), 'p')

    def test_generic_browser_gets_no_profile(self):
        with mock.patch.object(Path, 'resolve', side_effect=OSError('loop')):
            info = bp.ensure_browser_profile('Mail', 'epiphany', self.logger, stored_profile_path='/x')
        self.assertFalse(info['managed_profile'])
        self.assertEqual(info['exec_args'], [])
        self.assertFalse(self.firefox_root.exists())


class DeleteProfilesTests(_SandboxMixin, unittest.TestCase):
    def test_firefox_profile_is_unregistered_and_deleted(self):
        profile = self._managed('firefox', 'webapp_del')
        self.profiles_ini.write_text('[Profile0]\nName=webapp_del\nPath=webapp_del\n', encoding='utf-8')
        bp.delete_managed_browser_profiles('Mail', self.logger, stored_profile_path=str(profile))
        self.assertFalse(profile.exists())
        self.assertNotIn('webapp_del', self.profiles_ini.read_text(encoding='utf-8'))

    def test_chromium_profile_is_deleted(self):
        profile = self._managed('chromium', 'p')
        bp.delete_managed_browser_profiles('Mail', self.logger, stored_profile_path=str(profile))
        self.assertFalse(profile.exists())

    def test_kept_foreign_and_unknown_paths_survive(self):
        kept = self._managed('chrome', 'kept')
        bp.delete_managed_browser_profiles('Mail', self.logger, stored_profile_path=str(kept), keep_profile_path=str(kept))
        self.assertTrue(kept.exists())
        foreign = self.chromium_root / 'chrome' / 'personal'
        foreign.mkdir()
        bp.delete_managed_browser_profiles('Mail', self.logger, stored_profile_path=str(foreign))
        self.assertTrue(foreign.exists())
        outside = self.base / 'webapp_elsewhere'
        outside.mkdir()
        bp.delete_managed_browser_profiles('Mail', self.logger, stored_profile_path=str(outside))
        self.assertTrue(outside.exists())
        self.assertEqual(self.logger.warning.call_count, 2)

    def test_deletion_errors_are_logged(self):
        ff = self._managed('firefox', 'webapp_err')
        chromium = self._managed('chromium', 'err')
        with mock.patch.object(bp, '_safe_remove_tree', side_effect=OSError('busy')):
            bp.delete_managed_browser_profiles('Mail', self.logger, stored_profile_path=str(ff))
            bp.delete_managed_browser_profiles('Mail', self.logger, stored_profile_path=str(chromium))
        self.assertEqual(self.logger.error.call_count, 2)
        self.assertTrue(ff.exists())

    def test_unresolvable_inputs_delete_nothing(self):
        with mock.patch.object(Path, 'resolve', side_effect=OSError('loop')):
            bp.delete_managed_browser_profiles('Mail', self.logger, stored_profile_path='/x', keep_profile_path='/y')
        self.logger.warning.assert_not_called()
        self.logger.error.assert_not_called()


if __name__ == '__main__':
    unittest.main()

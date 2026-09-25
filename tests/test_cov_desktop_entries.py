"""Coverage tests for desktop_entries: launch-argument building, profile
references in Exec= lines, .desktop parsing, artifact cleanup and export.

Every path constant that desktop_entries and its collaborators read
(APPLICATIONS_DIR, ICON_THEME_APPS_DIR, FIREFOX_ROOT, CHROMIUM_PROFILE_ROOT,
LAUNCHER_DIR) is redirected into a temporary directory, and browser lookup is
stubbed, so no real launcher, icon or profile is ever touched.
"""
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_desktop_entries.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import browser_paths
import browser_profiles
import desktop_entries as de
import icon_pipeline
import launcher_wrapper
from app_identity import APP_ICON_NAME
from webapp_constants import (
    ADDRESS_KEY,
    APP_MODE_KEY,
    COLOR_SCHEME_KEY,
    DESKTOP_NAME_SOURCE_KEY,
    ICON_PATH_KEY,
    MODE_DESKTOP_KEY,
    MODE_MOBILE_KEY,
    OPTION_DISABLE_AI_KEY,
    OPTION_PRESERVE_SESSION_KEY,
    OPTION_SWIPE_KEY,
    PROFILE_NAME_KEY,
    PROFILE_PATH_KEY,
    USER_AGENT_VALUE_KEY,
)

ENGINES = [
    {'id': 1, 'name': 'Firefox', 'command': 'firefox'},
    {'id': 2, 'name': 'Chromium', 'command': 'chromium'},
    {'id': 3, 'name': 'Chrome', 'command': 'google-chrome'},
    {'id': 4, 'name': 'Epiphany', 'command': 'epiphany'},
]


class _SandboxMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.apps_dir = self.base / 'applications'
        self.theme_dir = self.base / 'icons' / 'apps'
        self.firefox_root = self.base / 'firefox'
        self.chromium_root = self.base / 'profiles'
        self.launcher_dir = self.base / 'launchers'
        patches = [
            (de, 'APPLICATIONS_DIR', self.apps_dir),
            (de, 'ICON_THEME_APPS_DIR', self.theme_dir),
            (de, 'FIREFOX_ROOT', self.firefox_root),
            (de, 'CHROMIUM_PROFILE_ROOT', self.chromium_root),
            (icon_pipeline, 'APPLICATIONS_DIR', self.apps_dir),
            (icon_pipeline, 'ICON_THEME_APPS_DIR', self.theme_dir),
            (browser_paths, 'FIREFOX_ROOT', self.firefox_root),
            (browser_paths, 'CHROMIUM_PROFILE_ROOT', self.chromium_root),
            (browser_profiles, 'FIREFOX_ROOT', self.firefox_root),
            (browser_profiles, 'CHROMIUM_PROFILE_ROOT', self.chromium_root),
            (launcher_wrapper, 'LAUNCHER_DIR', self.launcher_dir),
        ]
        for module, name, value in patches:
            patcher = mock.patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The configured command is taken as-is: no host lookup.
        patcher = mock.patch.object(de, 'resolve_browser_command', side_effect=lambda command, _logger: command)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logger = mock.Mock()

    def _desktop(self, name, exec_line='firefox https://example.com/', extra='', managed=True):
        self.apps_dir.mkdir(parents=True, exist_ok=True)
        lines = ['[Desktop Entry]', 'Name=Example', f'Exec={exec_line}', 'Type=Application']
        if managed:
            lines.append(f'ManagedBy={de.MANAGED_BY_VALUE}')
        path = self.apps_dir / name
        path.write_text('\n'.join(lines) + '\n' + extra, encoding='utf-8')
        return path


def _entry(title='Example', entry_id=7, description='', active=True):
    return SimpleNamespace(id=entry_id, title=title, description=description, active=active)


class LaunchArgTests(unittest.TestCase):
    def test_chromium_modes(self):
        url = 'https://a.example/'
        self.assertEqual(de._chromium_launch_args_for_mode('KIOSK', url, False), ['--kiosk', url])
        self.assertEqual(de._chromium_launch_args_for_mode('seamless', url, False), [f'--app={url}'])
        self.assertEqual(de._chromium_launch_args_for_mode('', url, True), [])
        self.assertEqual(de._chromium_launch_args_for_mode(None, url, False), ['--new-window', url])

    def test_firefox_and_generic_modes(self):
        url = 'https://a.example/'
        self.assertEqual(de._firefox_launch_args_for_mode('kiosk', url, False), ['--kiosk', url])
        self.assertEqual(de._firefox_launch_args_for_mode('app', url, True), [])
        self.assertEqual(de._firefox_launch_args_for_mode(None, url, False), [url])
        self.assertEqual(de._generic_launch_args_for_mode(url, True), [])
        self.assertEqual(de._generic_launch_args_for_mode(url, False), [url])


class StoredProfileInfoTests(_SandboxMixin, unittest.TestCase):
    def test_explicit_path_wins_and_names_the_profile(self):
        info = de._stored_profile_info('firefox', stored_profile_path=str(self.firefox_root / 'webapp_x'))
        self.assertEqual(info['profile_name'], 'webapp_x')
        self.assertEqual(info['exec_args'], ['-profile', str(self.firefox_root / 'webapp_x')])

    def test_unresolvable_path_is_used_unresolved(self):
        with mock.patch.object(Path, 'resolve', side_effect=OSError('loop')):
            info = de._stored_profile_info('chromium', stored_profile_name='n', stored_profile_path='/p/q')
        self.assertEqual(info['profile_path'], '/p/q')
        self.assertEqual(info['profile_name'], 'n')
        self.assertEqual(info['exec_args'], ['--user-data-dir=/p/q'])

    def test_name_only_is_placed_under_the_family_root(self):
        ff = de._stored_profile_info('firefox', stored_profile_name='../../evil')
        self.assertEqual(ff['profile_path'], str(self.firefox_root / 'evil'))
        self.assertEqual(ff['profile_name'], 'evil')
        chrome = de._stored_profile_info('google-chrome', stored_profile_name='c')
        self.assertEqual(chrome['profile_path'], str(self.chromium_root / 'chrome' / 'c'))
        generic = de._stored_profile_info('epiphany', stored_profile_name='g')
        self.assertEqual((generic['profile_path'], generic['exec_args']), ('', []))

    def test_nothing_stored(self):
        info = de._stored_profile_info('firefox')
        self.assertEqual(info, {'browser_family': 'firefox', 'profile_name': '', 'profile_path': '', 'exec_args': [], 'profile_migrated': False})


class SmallHelperTests(_SandboxMixin, unittest.TestCase):
    def test_selected_engine(self):
        self.assertEqual(de._selected_engine({'EngineID': '2'}, ENGINES), (ENGINES[1], 'chromium'))
        self.assertEqual(de._selected_engine({'EngineID': 'abc'}, ENGINES), (None, 'firefox'))
        self.assertEqual(de._selected_engine({}, ENGINES), (None, 'firefox'))

    def test_window_identity(self):
        self.assertEqual(de._window_identity_for_entry(_entry('My App')), 'my_app')
        self.assertEqual(de._window_identity_for_entry(_entry('!!!', 9)), 'webapp-entry-9')
        self.assertEqual(de._window_identity_for_entry(SimpleNamespace(title='', id=None)), 'webapp')

    def test_desktop_name(self):
        entry = _entry('Title', description='Desc')
        self.assertEqual(de.desktop_name_source(None), 'title')
        self.assertEqual(de.desktop_name_source({DESKTOP_NAME_SOURCE_KEY: 'bogus'}), 'title')
        self.assertEqual(de.desktop_display_name(entry, {DESKTOP_NAME_SOURCE_KEY: 'Description'}), 'Desc')
        self.assertEqual(de.desktop_display_name(_entry('Title'), {DESKTOP_NAME_SOURCE_KEY: 'description'}), 'Title')

    def test_exportable_and_expected_path(self):
        self.assertTrue(de.exportable_entry(_entry(), {ADDRESS_KEY: 'https://a.example/'}))
        self.assertFalse(de.exportable_entry(_entry(''), {ADDRESS_KEY: 'https://a.example/'}))
        self.assertFalse(de.exportable_entry(_entry(), {ADDRESS_KEY: 'javascript:alert(1)'}))
        self.assertEqual(de.get_expected_desktop_path('My App'), self.apps_dir / 'my_app.desktop')
        self.assertIsNone(de.get_expected_desktop_path('!!!'))

    def test_infer_engine_id(self):
        self.assertIsNone(de.infer_engine_id_from_command('', ENGINES))
        self.assertEqual(de.infer_engine_id_from_command('/usr/bin/chromium', ENGINES), 2)
        engines = [{'id': 5, 'command': 'google-chrome-stable'}, {'id': 6, 'command': 'firefox-esr'}]
        self.assertEqual(de.infer_engine_id_from_command('chrome', engines), 5)
        self.assertEqual(de.infer_engine_id_from_command('/opt/chromium-dev', [{'id': 8, 'command': 'chrome-beta'}]), 8)
        self.assertEqual(de.infer_engine_id_from_command('firefox', [{'id': 9, 'command': 'FIREFOX-nightly'}]), 9)
        self.assertEqual(de.infer_engine_id_from_command('mozilla-firefox-bin', [{'id': 1}, {'id': 9, 'command': 'firefox-nightly'}]), 9)
        self.assertIsNone(de.infer_engine_id_from_command('librewolf', [{'id': 9, 'command': 'firefox'}, {'id': 1}]))
        self.assertIsNone(de.infer_engine_id_from_command('mozilla-firefox', [{'id': 1, 'command': 'epiphany'}]))
        self.assertIsNone(de.infer_engine_id_from_command('chromium-x', [{'id': 1, 'command': 'epiphany'}]))


class BuildLaunchCommandTests(_SandboxMixin, unittest.TestCase):
    def _build(self, options, **kwargs):
        return de.build_launch_command(_entry(), {ADDRESS_KEY: 'https://a.example/', **options}, ENGINES, self.logger, **kwargs)

    def test_invalid_title_url_or_engine_is_refused(self):
        self.assertIsNone(de.build_launch_command(_entry(''), {ADDRESS_KEY: 'https://a.example/', 'EngineID': '1'}, ENGINES, self.logger))
        self.assertIsNone(self._build({ADDRESS_KEY: 'file:///etc/passwd', 'EngineID': '1'}))
        self.assertIsNone(self._build({'EngineID': '99'}))
        self.assertEqual(self.logger.warning.call_count, 3)

    def test_chromium_dark_with_swipe_ai_and_user_agent(self):
        spec = self._build({
            'EngineID': '2',
            COLOR_SCHEME_KEY: 'dark',
            OPTION_SWIPE_KEY: '1',
            OPTION_DISABLE_AI_KEY: '1',
            USER_AGENT_VALUE_KEY: ' UA/1 ',
            PROFILE_NAME_KEY: 'p',
        })
        argv = spec['argv']
        profile = self.chromium_root / 'chromium' / 'p'
        self.assertEqual(argv[:3], ['chromium', '--class=example', f'--user-data-dir={profile}'])
        self.assertIn('--force-dark-mode', argv)
        features = next(a for a in argv if a.startswith('--enable-features='))
        self.assertEqual(features, '--enable-features=TouchpadOverscrollHistoryNavigation,OverscrollHistoryNavigation,WebUIDarkMode')
        self.assertTrue(any(a.startswith('--disable-features=OptimizationGuideModelDownloading') for a in argv))
        self.assertIn('--blink-settings=preferredColorScheme=0,forceDarkModeEnabled=true', argv)
        self.assertIn('--user-agent=UA/1', argv)
        self.assertEqual(argv[-2:], ['--new-window', 'https://a.example/'])
        self.assertEqual(spec['window_identity'], 'example')

    def test_chrome_light_app_mode(self):
        spec = self._build({'EngineID': '3', COLOR_SCHEME_KEY: 'light', APP_MODE_KEY: '1'})
        argv = spec['argv']
        self.assertIn('--disable-features=WebUIDarkMode,AutoWebContentsDarkMode', argv)
        self.assertIn('--blink-settings=preferredColorScheme=1', argv)
        self.assertEqual(argv[-1], '--app=https://a.example/')

    def test_firefox_session_restore_and_mode_override(self):
        spec = self._build({'EngineID': '1', OPTION_PRESERVE_SESSION_KEY: '1'})
        self.assertEqual(spec['argv'], ['firefox'])
        spec = self._build({'EngineID': '1', OPTION_PRESERVE_SESSION_KEY: '1'}, mode_override='kiosk')
        self.assertEqual(spec['argv'], ['firefox', '--kiosk', 'https://a.example/'])

    def test_generic_browser(self):
        spec = self._build({'EngineID': '4'})
        self.assertEqual(spec['argv'], ['epiphany', 'https://a.example/'])

    def test_prepare_profile_creates_and_configures(self):
        info = {'browser_family': 'firefox', 'profile_path': '/p', 'profile_name': 'p', 'exec_args': ['-profile', '/p']}
        with mock.patch.object(de, 'ensure_browser_profile', return_value=info) as ensure, \
                mock.patch.object(de, 'apply_profile_settings') as apply:
            spec = self._build({'EngineID': '1', PROFILE_NAME_KEY: 'n', PROFILE_PATH_KEY: '/s'}, prepare_profile=True)
        ensure.assert_called_once_with('Example', 'firefox', self.logger, stored_profile_name='n', stored_profile_path='/s')
        apply.assert_called_once()
        self.assertEqual(spec['argv'], ['firefox', '-profile', '/p', 'https://a.example/'])

    def test_missing_profile_info_still_builds_argv(self):
        with mock.patch.object(de, 'ensure_browser_profile', return_value=None), \
                mock.patch.object(de, 'apply_profile_settings'):
            spec = self._build({'EngineID': '2'}, prepare_profile=True)
        self.assertEqual(spec['argv'], ['chromium', 'https://a.example/'])


class ProfileReferenceTests(_SandboxMixin, unittest.TestCase):
    def test_filesystem_path_detection(self):
        self.assertFalse(de._looks_like_filesystem_path(' '))
        self.assertTrue(de._looks_like_filesystem_path('~/x'))
        self.assertTrue(de._looks_like_filesystem_path('a/b'))
        self.assertTrue(de._looks_like_filesystem_path('a\\b'))
        self.assertFalse(de._looks_like_filesystem_path('default-release'))

    def test_path_like_reference_is_resolved(self):
        self.assertEqual(de._resolve_firefox_profile_reference(''), '')
        self.assertEqual(de._resolve_firefox_profile_reference(str(self.base / 'x')), str(self.base / 'x'))
        with mock.patch.object(Path, 'resolve', side_effect=OSError('loop')):
            self.assertEqual(de._resolve_firefox_profile_reference('/a/b'), '/a/b')

    def test_direct_directory_under_root(self):
        (self.firefox_root / 'abc.default').mkdir(parents=True)
        self.assertEqual(de._resolve_firefox_profile_reference('abc.default'), str(self.firefox_root / 'abc.default'))
        with mock.patch.object(Path, 'resolve', side_effect=OSError('loop')):
            self.assertEqual(de._resolve_firefox_profile_reference('abc.default'), str(self.firefox_root / 'abc.default'))

    def test_lookup_through_profiles_ini(self):
        self.assertEqual(de._resolve_firefox_profile_reference('work'), '')
        self.firefox_root.mkdir(parents=True)
        (self.firefox_root / 'profiles.ini').write_text(
            '[General]\nStartWithLastProfile=1\n'
            '[Profile0]\nName=work\nIsRelative=1\nPath=Profiles/w.work\n'
            '[Profile1]\nName=abs\nIsRelative=0\nPath=/opt/ff/abs\n'
            '[Profile2]\nName=nopath\n',
            encoding='utf-8',
        )
        self.assertEqual(de._resolve_firefox_profile_reference('work'), str(self.firefox_root / 'Profiles' / 'w.work'))
        self.assertEqual(de._resolve_firefox_profile_reference('w.work'), str(self.firefox_root / 'Profiles' / 'w.work'))
        self.assertEqual(de._resolve_firefox_profile_reference('abs'), '/opt/ff/abs')
        self.assertEqual(de._resolve_firefox_profile_reference('nopath'), str(self.firefox_root / 'nopath'))
        self.assertEqual(de._resolve_firefox_profile_reference('unknown'), '')
        with mock.patch.object(Path, 'resolve', side_effect=OSError('loop')):
            self.assertEqual(de._resolve_firefox_profile_reference('work'), str(self.firefox_root / 'Profiles' / 'w.work'))

    def test_unreadable_profiles_ini(self):
        self.firefox_root.mkdir(parents=True)
        (self.firefox_root / 'profiles.ini').write_text('[Profile0]\nName=work\n', encoding='utf-8')
        with mock.patch('builtins.open', side_effect=OSError('denied')):
            self.assertEqual(de._resolve_firefox_profile_reference('work'), '')

    def test_exec_token_extraction(self):
        extract = de._extract_profile_path_from_exec_tokens
        self.assertEqual(extract([]), '')
        self.assertEqual(extract(['ff', '-profile', '/p1']), '/p1')
        self.assertEqual(extract(['ff', '--profile', ' ', 'x']), '')
        self.assertEqual(extract(['ff', '--user-data-dir']), '')
        self.assertEqual(extract(['ff', '--user-data-dir=/p2']), '/p2')
        self.assertEqual(extract(['ff', '--profile=']), '')
        self.assertEqual(extract(['ff', '-P', 'unknown']), 'unknown')
        self.assertEqual(extract(['ff', '-P']), '')
        self.assertEqual(extract(['ff', '-P', ' ']), '')
        self.assertEqual(extract(['ff', '-P=/abs/p']), '/abs/p')
        self.assertEqual(extract(['ff', '-P=']), '')
        (self.firefox_root / 'named').mkdir(parents=True)
        self.assertEqual(extract(['ff', '-P', 'named']), str(self.firefox_root / 'named'))
        self.assertEqual(extract(['ff', '-P=named']), str(self.firefox_root / 'named'))


class ParseDesktopFileTests(_SandboxMixin, unittest.TestCase):
    def test_unreadable_foreign_or_sectionless_files_are_ignored(self):
        self.assertIsNone(de.parse_desktop_file(self.base / 'missing.desktop', ENGINES))
        self.assertIsNone(de.parse_desktop_file(self._desktop('foreign.desktop', managed=False), ENGINES))
        other = self.base / 'other.desktop'
        other.write_text('[Other]\nManagedBy=x\n', encoding='utf-8')
        self.assertIsNone(de.parse_desktop_file(other, ENGINES))

    def test_full_managed_entry(self):
        path = self._desktop(
            'mail.desktop',
            exec_line='chromium --user-data-dir=/p/mail --app=https://mail.example/ --start-fullscreen --user-agent=UA/9',
            extra='EntryId=12\nX-WebApp-Title=Mail\nX-WebApp-DesktopNameSource=Description\nIcon=/icons/mail.png\nNoDisplay=true\n',
        )
        data = de.parse_desktop_file(path, ENGINES)
        self.assertEqual(data['entry_id'], 12)
        self.assertEqual(data['title'], 'Mail')
        self.assertEqual(data['address'], 'https://mail.example/')
        self.assertFalse(data['active'])
        self.assertEqual((data['engine_id'], data['engine_name']), (2, 'Chromium'))
        self.assertEqual(data['user_agent_value'], 'UA/9')
        self.assertEqual((data['icon_path'], data['icon_name']), ('/icons/mail.png', ''))
        self.assertEqual((data['profile_path'], data['profile_name']), ('/p/mail', 'mail'))
        self.assertEqual(data['options'], {APP_MODE_KEY: '1', 'Kiosk': '0', 'Frameless': '1', DESKTOP_NAME_SOURCE_KEY: 'description'})

    def test_kiosk_explicit_mode_and_icon_name(self):
        path = self._desktop(
            'k.desktop',
            exec_line='firefox --kiosk https://k.example/',
            extra='EntryId=abc\nX-WebApp-Mode=kiosk\nX-WebApp-DesktopNameSource=bogus\nIcon=mail-app\n',
        )
        data = de.parse_desktop_file(path, ENGINES)
        self.assertIsNone(data['entry_id'])
        self.assertEqual(data['options']['Kiosk'], '1')
        self.assertEqual(data['options'][DESKTOP_NAME_SOURCE_KEY], 'title')
        self.assertEqual((data['icon_path'], data['icon_name']), ('', 'mail-app'))
        self.assertEqual(data['address'], 'https://k.example/')

    def test_unparseable_exec_and_unknown_engine(self):
        path = self._desktop('bad.desktop', exec_line='"unterminated', extra='Icon=\n')
        data = de.parse_desktop_file(path, [])
        self.assertEqual((data['command'], data['address'], data['engine_id'], data['engine_name']), ('', '', None, ''))
        self.assertEqual(data['options'], {DESKTOP_NAME_SOURCE_KEY: 'title'})

    def test_is_managed_and_listing(self):
        self.assertEqual(de.list_managed_desktop_files(ENGINES), [])
        managed = self._desktop('a.desktop')
        self._desktop('b.desktop', managed=False)
        (self.apps_dir / 'c.desktop').write_text('[Desktop Entry]\nManagedBy=someone-else\n', encoding='utf-8')
        self.assertTrue(de.is_managed_desktop_file(managed))
        self.assertFalse(de.is_managed_desktop_file(self.apps_dir / 'missing.desktop'))
        self.assertEqual([item['path'] for item in de.list_managed_desktop_files(ENGINES)], [managed])

    def test_marker_sniff_tolerates_unreadable_files(self):
        with mock.patch('builtins.open', side_effect=OSError('denied')):
            self.assertFalse(de._may_be_managed_desktop_file(self.base / 'x.desktop'))


class DeleteArtifactsTests(_SandboxMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.theme_dir.mkdir(parents=True)

    def test_matching_desktop_wrapper_and_icons_are_removed(self):
        icon = self.theme_dir / 'mail.png'
        icon.write_bytes(b'x')
        entry_icon = self.theme_dir / 'webapp-entry-7.svg'
        entry_icon.write_bytes(b'x')
        foreign_icon = self.theme_dir / 'other.png'
        foreign_icon.write_bytes(b'x')
        (self.theme_dir / 'mail.dir').mkdir()
        desktop = self._desktop('mail.desktop', extra=f'EntryId=7\nX-WebApp-Title=Mail\nIcon={icon}\n')
        other = self._desktop('other.desktop', extra='EntryId=8\nX-WebApp-Title=Other\n')
        wrapper = launcher_wrapper.write_wrapper('mail', ['a'], ['b'])
        de.delete_managed_entry_artifacts(7, 'Mail', ENGINES, self.logger)
        self.assertFalse(desktop.exists())
        self.assertFalse(wrapper.exists())
        self.assertFalse(icon.exists())
        self.assertFalse(entry_icon.exists())
        self.assertTrue(foreign_icon.exists())
        self.assertTrue(other.exists())

    def test_keep_arguments_protect_files(self):
        kept_desktop = self._desktop('mail.desktop', extra='EntryId=7\nX-WebApp-Title=Mail\n')
        old_desktop = self._desktop('old.desktop', extra=f'EntryId=7\nX-WebApp-Title=Mail\nIcon={self.theme_dir / "mail.png"}\n')
        kept_icon = self.theme_dir / 'mail.png'
        kept_icon.write_bytes(b'x')
        named_icon = self.theme_dir / 'webapp-entry-7.png'
        named_icon.write_bytes(b'x')
        de.delete_managed_entry_artifacts(
            7, 'Mail', ENGINES, self.logger,
            keep_path=kept_desktop, keep_icon_path=kept_icon, keep_icon_name='WEBAPP-ENTRY-7',
        )
        self.assertTrue(kept_desktop.exists())
        self.assertFalse(old_desktop.exists())
        self.assertTrue(kept_icon.exists())
        self.assertTrue(named_icon.exists())

    def test_unsafe_icon_path_is_not_deleted(self):
        outside_icon = self.base / 'mail.png'
        outside_icon.write_bytes(b'x')
        self._desktop('mail.desktop', extra=f'EntryId=7\nX-WebApp-Title=Mail\nIcon={outside_icon}\n')
        de.delete_managed_entry_artifacts(7, 'Mail', ENGINES, self.logger)
        self.assertTrue(outside_icon.exists())

    def test_unlink_failures_are_logged(self):
        icon = self.theme_dir / 'mail.png'
        icon.write_bytes(b'x')
        self._desktop('mail.desktop', extra=f'EntryId=7\nX-WebApp-Title=Mail\nIcon={icon}\n')
        with mock.patch.object(Path, 'unlink', side_effect=OSError('busy')):
            de.delete_managed_entry_artifacts(7, 'Mail', ENGINES, self.logger)
        self.assertEqual(self.logger.error.call_count, 3)

    def test_profiles_are_deleted_only_on_request(self):
        with mock.patch.object(de, 'delete_managed_browser_profiles') as delete:
            de.delete_managed_entry_artifacts(7, 'Mail', ENGINES, self.logger)
            delete.assert_not_called()
            de.delete_managed_entry_artifacts(7, ' Mail ', ENGINES, self.logger, delete_profiles=True, stored_profile_path='/p', stored_profile_name='n', keep_profile_path='/k')
        delete.assert_called_once_with('Mail', self.logger, stored_profile_path='/p', stored_profile_name='n', keep_profile_path='/k')


class ExportDesktopFileTests(_SandboxMixin, unittest.TestCase):
    def _options(self, **extra):
        return {ADDRESS_KEY: 'https://mail.example/', 'EngineID': '4', **extra}

    def _content(self, result):
        return result['desktop_path'].read_text(encoding='utf-8')

    def test_writes_a_complete_desktop_file(self):
        result = de.export_desktop_file(_entry('Mail', active=False), self._options(), ENGINES, self.logger)
        content = self._content(result)
        self.assertIn('Exec=epiphany https://mail.example/\n', content)
        self.assertIn('NoDisplay=true\n', content)
        self.assertIn(f'ManagedBy={de.MANAGED_BY_VALUE}\n', content)
        self.assertIn('EntryId=7\n', content)
        self.assertIn(f'Icon={APP_ICON_NAME}\n', content)
        self.assertIn('StartupWMClass=mail\n', content)
        self.assertEqual(result['browser_family'], 'generic')

    def test_invalid_entries_clean_up_instead_of_exporting(self):
        with mock.patch.object(de, 'delete_managed_entry_artifacts') as delete:
            self.assertIsNone(de.export_desktop_file(_entry('Mail'), {ADDRESS_KEY: 'nope'}, ENGINES, self.logger))
            self.assertIsNone(de.export_desktop_file(_entry('!!!'), self._options(), ENGINES, self.logger))
            self.assertIsNone(de.export_desktop_file(_entry('Mail'), self._options(EngineID='99'), ENGINES, self.logger))
        self.assertEqual([c.kwargs['delete_profiles'] for c in delete.call_args_list], [True, True, False])

    def test_foreign_file_at_target_is_not_overwritten(self):
        foreign = self._desktop('mail.desktop', managed=False)
        before = foreign.read_text(encoding='utf-8')
        self.assertIsNone(de.export_desktop_file(_entry('Mail'), self._options(), ENGINES, self.logger))
        self.assertEqual(foreign.read_text(encoding='utf-8'), before)

    def test_failed_desktop_spec_falls_back_to_mobile(self):
        mobile = {'argv': ['epiphany', 'https://mail.example/'], 'normalized_address': 'https://mail.example/', 'profile_info': None, 'window_identity': ''}
        with mock.patch.object(de, 'build_launch_command', side_effect=[mobile, None]):
            result = de.export_desktop_file(_entry('Mail'), self._options(**{MODE_MOBILE_KEY: 'kiosk', MODE_DESKTOP_KEY: 'standard'}), ENGINES, self.logger)
        content = self._content(result)
        self.assertIn('Exec=epiphany https://mail.example/\n', content)
        self.assertNotIn('StartupWMClass', content)
        self.assertEqual((result['profile_path'], result['browser_family']), ('', ''))

    def test_divergent_modes_point_exec_at_the_wrapper(self):
        def spec(*_args, mode_override=None, **_kwargs):
            return {'argv': ['epiphany', mode_override], 'normalized_address': 'https://mail.example/', 'profile_info': None, 'window_identity': 'mail'}

        with mock.patch.object(de, 'build_launch_command', side_effect=spec):
            result = de.export_desktop_file(_entry('Mail'), self._options(**{MODE_MOBILE_KEY: 'kiosk', MODE_DESKTOP_KEY: 'standard'}), ENGINES, self.logger)
        wrapper = self.launcher_dir / 'mail.sh'
        self.assertTrue(wrapper.is_file())
        self.assertIn(f'Exec={wrapper}\n', self._content(result))
        self.assertIn('X-WebApp-Mode=kiosk\n', self._content(result))

    def test_themed_icon_name_is_used_verbatim(self):
        result = de.export_desktop_file(_entry('Mail'), self._options(**{ICON_PATH_KEY: 'mail-symbolic'}), ENGINES, self.logger)
        self.assertIn('Icon=mail-symbolic\n', self._content(result))

    def test_icon_file_is_normalised_into_the_theme(self):
        source = self.base / 'source.png'
        source.write_bytes(b'png')
        with mock.patch.object(de, 'normalize_icon_to_png') as normalize:
            result = de.export_desktop_file(_entry('Mail'), self._options(**{ICON_PATH_KEY: str(source)}), ENGINES, self.logger)
        normalize.assert_called_once_with(source, self.theme_dir / 'mail.png')
        self.assertIn(f'Icon={self.theme_dir / "mail.png"}\n', self._content(result))

    def test_missing_or_broken_icon_falls_back_to_app_icon(self):
        result = de.export_desktop_file(_entry('Mail'), self._options(**{ICON_PATH_KEY: str(self.base / 'missing.png')}), ENGINES, self.logger)
        self.assertIn(f'Icon={APP_ICON_NAME}\n', self._content(result))
        source = self.base / 'source.png'
        source.write_bytes(b'png')
        with mock.patch.object(de, 'normalize_icon_to_png', side_effect=OSError('bad image')):
            result = de.export_desktop_file(_entry('Mail'), self._options(**{ICON_PATH_KEY: str(source)}), ENGINES, self.logger)
        self.assertIn(f'Icon={APP_ICON_NAME}\n', self._content(result))

    def test_unchanged_file_is_not_rewritten_and_unreadable_file_is(self):
        entry = _entry('Mail')
        de.export_desktop_file(entry, self._options(), ENGINES, self.logger)
        with mock.patch('builtins.open', wraps=open) as opened:
            de.export_desktop_file(entry, self._options(), ENGINES, self.logger)
        self.assertFalse(any(len(c.args) > 1 and c.args[1] == 'w' for c in opened.call_args_list))
        original = Path.read_text

        def fail_target(path, *args, **kwargs):
            if path.suffix == '.desktop':
                raise OSError('denied')
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, 'read_text', fail_target), \
                mock.patch.object(de, '_guard_target_path', return_value=True), \
                mock.patch('builtins.open', wraps=open) as opened:
            de.export_desktop_file(entry, self._options(), ENGINES, self.logger)
        self.assertTrue(any(len(c.args) > 1 and c.args[1] == 'w' for c in opened.call_args_list))


if __name__ == '__main__':
    unittest.main()

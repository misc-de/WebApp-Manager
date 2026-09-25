"""Coverage-driven tests for the small helper modules.

launcher_wrapper, distro_utils, host_commands, app_state, ui_flow_state,
wapp_transfer and manager_integration. All filesystem work happens in temp
directories; no subprocess is started and no real host file is read.
"""
import base64
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_small_modules.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import distro_utils
import host_commands
import launcher_wrapper
import manager_integration
from app_state import WebAppState
from ui_flow_state import (
    detail_neutral_focus_slot,
    main_neutral_focus_candidates,
    next_search_toggle_state,
)
from wapp_transfer import (
    build_wapp_export_bundle_payload,
    build_wapp_export_payload,
    sanitized_export_options,
)


class _TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)


# --- launcher_wrapper -------------------------------------------------------

class LauncherWrapperTests(_TempDirCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(launcher_wrapper, 'LAUNCHER_DIR', self.root / 'launchers')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_empty_slug_is_refused(self):
        with self.assertRaises(ValueError):
            launcher_wrapper.write_wrapper('', ['a'], ['b'])
        self.assertFalse(launcher_wrapper.delete_wrapper(''))

    def test_unreadable_existing_wrapper_is_rewritten(self):
        target = launcher_wrapper.write_wrapper('mail', ['firefox', '--kiosk'], ['firefox'])
        real_read_text = Path.read_text

        def failing_read_text(self, *args, **kwargs):
            if self == target:
                raise PermissionError('denied')
            return real_read_text(self, *args, **kwargs)

        with mock.patch.object(Path, 'read_text', failing_read_text), \
                mock.patch.object(Path, 'write_text', autospec=True, side_effect=Path.write_text) as write:
            launcher_wrapper.write_wrapper('mail', ['firefox', '--kiosk'], ['firefox'])
        write.assert_called_once()
        self.assertIn("exec firefox --kiosk", target.read_text(encoding='utf-8'))

    def test_chmod_failure_still_returns_the_wrapper(self):
        with mock.patch.object(Path, 'chmod', side_effect=PermissionError('denied')):
            target = launcher_wrapper.write_wrapper('mail', ['a'], ['b'])
        self.assertTrue(target.is_file())
        self.assertIn('exec a', target.read_text(encoding='utf-8'))

    def test_delete_reports_missing_and_failing_wrappers(self):
        self.assertFalse(launcher_wrapper.delete_wrapper('missing'))
        launcher_wrapper.write_wrapper('mail', ['a'], ['b'])
        with mock.patch.object(Path, 'unlink', side_effect=PermissionError('denied')):
            self.assertFalse(launcher_wrapper.delete_wrapper('mail'))
        self.assertTrue(launcher_wrapper.wrapper_path_for_slug('mail').exists())
        self.assertTrue(launcher_wrapper.delete_wrapper('mail'))
        self.assertFalse(launcher_wrapper.wrapper_path_for_slug('mail').exists())

    def test_list_without_launcher_dir_is_empty(self):
        self.assertEqual(launcher_wrapper.list_wrappers(), [])

    def test_list_only_returns_shell_scripts(self):
        launcher_wrapper.write_wrapper('b', ['a'], ['b'])
        launcher_wrapper.write_wrapper('a', ['a'], ['b'])
        (launcher_wrapper.LAUNCHER_DIR / 'notes.txt').write_text('x', encoding='utf-8')
        (launcher_wrapper.LAUNCHER_DIR / 'dir.sh').mkdir()
        self.assertEqual([p.name for p in launcher_wrapper.list_wrappers()], ['a.sh', 'b.sh'])

    def test_cleanup_removes_orphans_and_survives_unlink_failures(self):
        for slug in ('keep', 'orphan', 'stuck'):
            launcher_wrapper.write_wrapper(slug, ['a'], ['b'])
        real_unlink = Path.unlink

        def selective_unlink(self, *args, **kwargs):
            if self.stem == 'stuck':
                raise PermissionError('denied')
            return real_unlink(self, *args, **kwargs)

        with mock.patch.object(Path, 'unlink', selective_unlink):
            removed = launcher_wrapper.cleanup_orphaned_wrappers(['keep', '', None])
        self.assertEqual([p.stem for p in removed], ['orphan'])
        self.assertEqual([p.stem for p in launcher_wrapper.list_wrappers()], ['keep', 'stuck'])


# --- distro_utils -----------------------------------------------------------

class DistroUtilsTests(_TempDirCase):
    def setUp(self):
        super().setUp()
        self._clear()
        self.addCleanup(self._clear)

    @staticmethod
    def _clear():
        distro_utils.os_release_data.cache_clear()
        distro_utils._os_release_text.cache_clear()
        distro_utils.is_furios_distribution.cache_clear()

    def _use(self, *paths):
        patcher = mock.patch.object(distro_utils, '_OS_RELEASE_PATHS', tuple(paths))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_parser_skips_comments_blank_and_malformed_lines_and_strips_quotes(self):
        text = '# comment\n\nNOEQUALS\nID="furios"\nNAME=\'Furi OS\'\nVERSION_ID=1\nODD="x\nEMPTY=\n'
        self.assertEqual(distro_utils._parse_os_release_text(text), {
            'ID': 'furios', 'NAME': 'Furi OS', 'VERSION_ID': '1', 'ODD': '"x', 'EMPTY': '',
        })

    def test_unreadable_candidates_are_skipped(self):
        class _Broken:
            def exists(self):
                raise PermissionError('denied')

        good = self.root / 'os-release'
        good.write_text('ID=debian\n', encoding='utf-8')
        self._use(_Broken(), good)
        self.assertEqual(distro_utils.os_release_data(), {'ID': 'debian'})
        self.assertEqual(distro_utils._os_release_text(), 'ID=debian\n')

    def test_no_os_release_at_all(self):
        self._use(self.root / 'missing')
        self.assertEqual(distro_utils.os_release_data(), {})
        self.assertFalse(distro_utils.is_furios_distribution())

    def test_raw_text_marker_is_enough(self):
        path = self.root / 'os-release'
        path.write_text('ID=debian\nHOME_URL=https://furilabs.com\n', encoding='utf-8')
        self._use(path)
        self.assertTrue(distro_utils.is_furios_distribution())

    def test_parsed_fields_are_consulted_when_raw_text_has_no_marker(self):
        path = self.root / 'os-release'
        path.write_text('ID=debian\n', encoding='utf-8')
        self._use(path)
        for data, expected in (
            ({'VENDOR_NAME': 'Furi Labs'}, True),
            ({'ID': 'debian'}, False),
            ({}, False),
        ):
            distro_utils.is_furios_distribution.cache_clear()
            with mock.patch.object(distro_utils, 'os_release_data', return_value=data):
                self.assertIs(distro_utils.is_furios_distribution(), expected, data)


# --- host_commands ----------------------------------------------------------

class HostCommandsTests(unittest.TestCase):
    def setUp(self):
        self._clear()
        self.addCleanup(self._clear)

    @staticmethod
    def _clear():
        host_commands.running_in_flatpak.cache_clear()
        host_commands.host_which.cache_clear()

    def test_unreadable_flatpak_marker_means_not_sandboxed(self):
        class _Broken:
            def exists(self):
                raise PermissionError('denied')

        with mock.patch.object(host_commands, 'FLATPAK_INFO_PATH', _Broken()):
            self.assertFalse(host_commands.running_in_flatpak())

    def test_blank_lookup_output_means_not_found(self):
        result = mock.Mock(returncode=0, stdout='\n   \n')
        with mock.patch.object(host_commands, 'running_in_flatpak', return_value=True), \
                mock.patch.object(host_commands.subprocess, 'run', return_value=result) as run:
            self.assertIsNone(host_commands.host_which('firefox'))
        run.assert_called_once()

    def test_first_non_blank_line_is_the_answer(self):
        result = mock.Mock(returncode=0, stdout='\n/usr/bin/firefox\n/other\n')
        with mock.patch.object(host_commands, 'running_in_flatpak', return_value=True), \
                mock.patch.object(host_commands.subprocess, 'run', return_value=result):
            self.assertEqual(host_commands.host_which('firefox'), '/usr/bin/firefox')


# --- app_state --------------------------------------------------------------

class WebAppStateTests(unittest.TestCase):
    def test_from_entry_and_options(self):
        entry = types.SimpleNamespace(title='Mail', active=0)
        from webapp_constants import (
            ADDRESS_KEY,
            ICON_PATH_KEY,
            PROFILE_NAME_KEY,
            PROFILE_PATH_KEY,
        )
        options = {
            ADDRESS_KEY: 'https://mail.example',
            'EngineID': '2',
            'UserAgentName': 'Mobile',
            'UserAgentValue': 'UA',
            ICON_PATH_KEY: '/icons/mail.png',
            PROFILE_NAME_KEY: 'mail',
            PROFILE_PATH_KEY: '/profiles/mail',
        }
        state = WebAppState.from_entry_and_options(entry, options)
        self.assertEqual(state, WebAppState(
            'Mail', 'https://mail.example', '2', False, 'Mobile', 'UA', '/icons/mail.png', 'mail', '/profiles/mail',
        ))

    def test_from_entry_with_no_options_uses_blanks(self):
        state = WebAppState.from_entry_and_options(types.SimpleNamespace(title='X', active=1), {})
        self.assertEqual(state, WebAppState('X', '', '', True, '', '', '', '', ''))

    def test_from_file_data_without_fallback(self):
        state = WebAppState.from_file_data({'title': 'T', 'engine_id': 3, 'active': False})
        self.assertEqual(state, WebAppState('T', '', '3', False, '', '', '', '', ''))

    def test_from_file_data_falls_back_field_by_field(self):
        fallback = WebAppState('Old', 'https://old', '1', True, 'N', 'V', '/i', 'p', '/pp')
        state = WebAppState.from_file_data({'title': '', 'address': 'https://new', 'profile_name': 'q'}, fallback)
        self.assertEqual(state, WebAppState('Old', 'https://new', '1', True, 'N', 'V', '/i', 'q', '/pp'))

    def test_engine_id_zero_is_kept(self):
        fallback = WebAppState('Old', '', '1', True, '', '', '', '', '')
        self.assertEqual(WebAppState.from_file_data({'engine_id': 0}, fallback).engine_id, '0')


# --- ui_flow_state ----------------------------------------------------------

class UiFlowStateTests(unittest.TestCase):
    def _main(self, page, search=False, split=False, detail=False):
        return main_neutral_focus_candidates(
            visible_page=page, search_visible=search,
            adaptive_split_enabled=split, adaptive_real_detail_visible=detail,
        )

    def test_search_takes_precedence(self):
        self.assertEqual(self._main('settings_page', search=True), ('search_button', 'home_button', 'add_button'))

    def test_settings_pages_start_at_back(self):
        for page in ('settings_page', 'settings_assets_page'):
            self.assertEqual(self._main(page), ('back_button', 'home_button', 'search_button', 'add_button'))

    def test_overview_depends_on_split_detail(self):
        self.assertEqual(self._main('overview_page', split=True, detail=True)[0], 'back_button')
        self.assertEqual(self._main('overview_page', split=True, detail=False)[0], 'home_button')
        self.assertEqual(self._main('detail_page')[0], 'home_button')

    def test_detail_slots(self):
        self.assertEqual(detail_neutral_focus_slot(None), ('icon_button',))
        self.assertEqual(detail_neutral_focus_slot(' options '), ('options_tab_button', 'icon_button'))
        self.assertEqual(detail_neutral_focus_slot('icon'), ('first_icon_page_button', 'icon_button'))
        self.assertEqual(detail_neutral_focus_slot('css_assets')[0], 'css_tab_button')
        self.assertEqual(detail_neutral_focus_slot('javascript_assets')[0], 'javascript_tab_button')
        self.assertEqual(detail_neutral_focus_slot('unknown'), ('icon_button',))

    def test_search_toggle(self):
        opened = next_search_toggle_state(current_visible=False, current_text='x')
        self.assertTrue(opened['search_visible'] and opened['autofocus_search_entry'])
        self.assertFalse(opened['clear_entry_text'])
        closed = next_search_toggle_state(current_visible=True, current_text='')
        self.assertFalse(closed['search_visible'])
        self.assertFalse(closed['clear_entry_text'])
        self.assertTrue(closed['reset_search_text'] and closed['restore_header_actions'])
        self.assertTrue(next_search_toggle_state(current_visible=True, current_text='abc')['clear_entry_text'])


# --- wapp_transfer ----------------------------------------------------------

class WappTransferTests(_TempDirCase):
    def test_transient_keys_are_removed_without_touching_the_input(self):
        from webapp_constants import ICON_PATH_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY
        options = {ICON_PATH_KEY: '/i', PROFILE_NAME_KEY: 'p', PROFILE_PATH_KEY: '/pp', 'Address': 'x'}
        self.assertEqual(sanitized_export_options(options), {'Address': 'x'})
        self.assertIn(ICON_PATH_KEY, options)
        self.assertEqual(sanitized_export_options(None), {})

    def test_existing_icon_is_embedded(self):
        from webapp_constants import ICON_PATH_KEY
        icon = self.root / 'mail.png'
        icon.write_bytes(b'\x89PNGdata')
        payload = build_wapp_export_payload(title='Mail', options_dict={ICON_PATH_KEY: f' {icon} ', 'Address': 'x'})
        self.assertEqual(payload['icon'], {
            'filename': 'mail.png',
            'mime': 'image/png',
            'data_base64': base64.b64encode(b'\x89PNGdata').decode('ascii'),
        })
        self.assertEqual(payload['options'], {'Address': 'x'})

    def test_missing_icon_is_left_out(self):
        from webapp_constants import ICON_PATH_KEY
        payload = build_wapp_export_payload(title=None, description=None, active=0,
                                            options_dict={ICON_PATH_KEY: str(self.root / 'nope.png')})
        self.assertIsNone(payload['icon'])
        self.assertEqual((payload['title'], payload['description'], payload['active']), ('', '', False))

    def test_bundle_timestamp_is_utc_without_microseconds(self):
        bundle = build_wapp_export_bundle_payload(None)
        self.assertRegex(bundle['created_at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertEqual(bundle['entries'], [])


# --- manager_integration ----------------------------------------------------

class _FakeSettings:
    def __init__(self, layout):
        self._layout = layout

    def get_property(self, name):
        assert name == 'gtk-decoration-layout'
        return self._layout


def _fake_gi_repository(settings):
    gtk = types.SimpleNamespace(Settings=types.SimpleNamespace(get_default=lambda: settings))
    module = types.ModuleType('gi.repository')
    module.Gtk = gtk
    return module


class DecorationLayoutTests(unittest.TestCase):
    def _layout(self, settings):
        with mock.patch.dict(sys.modules, {'gi.repository': _fake_gi_repository(settings)}):
            return manager_integration.headerbar_decoration_layout_without_icon()

    def test_icon_button_is_removed(self):
        self.assertEqual(self._layout(_FakeSettings('icon,menu:minimize, close')), 'menu:minimize,close')

    def test_layout_of_only_the_icon_falls_back_to_the_default(self):
        self.assertEqual(self._layout(_FakeSettings('icon:')), ':minimize,maximize,close')

    def test_missing_settings_or_layout_use_the_default(self):
        self.assertEqual(self._layout(None), ':minimize,maximize,close')
        self.assertEqual(self._layout(_FakeSettings('   ')), ':minimize,maximize,close')

    def test_missing_gtk_uses_the_default(self):
        with mock.patch.dict(sys.modules, {'gi.repository': None}):
            self.assertEqual(manager_integration.headerbar_decoration_layout_without_icon(), ':minimize,maximize,close')


class DesktopIntegrationTests(_TempDirCase):
    def setUp(self):
        super().setUp()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.app_dir = self.root / 'app'
        self.app_dir.mkdir()
        self.icon_source = self.root / 'source.png'
        self.icon_source.write_bytes(b'icon-v1')
        for patcher in (
            mock.patch.dict(os.environ, {'HOME': str(self.home)}),
            mock.patch.object(manager_integration, 'APP_ICON_SOURCE', self.icon_source),
            mock.patch.object(manager_integration, 'running_in_flatpak', return_value=False),
            mock.patch.object(manager_integration, 't', lambda key, **kwargs: 'WebApp Manager'),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.logger = mock.Mock()
        self.desktop_path = self.home / '.local/share/applications' / f'{manager_integration.APP_ID}.desktop'
        self.icon_path = self.home / '.local/share/icons/hicolor/512x512/apps' / f'{manager_integration.APP_ICON_NAME}.png'

    def run_integration(self):
        manager_integration.ensure_manager_desktop_integration(self.app_dir, self.logger)

    def test_flatpak_leaves_the_host_alone(self):
        with mock.patch.object(manager_integration, 'running_in_flatpak', return_value=True):
            self.run_integration()
        self.assertFalse((self.home / '.local').exists())
        self.logger.debug.assert_called_once()

    def test_installs_icon_and_desktop_entry(self):
        self.run_integration()
        self.assertEqual(self.icon_path.read_bytes(), b'icon-v1')
        text = self.desktop_path.read_text(encoding='utf-8')
        self.assertIn('Name=WebApp Manager\n', text)
        self.assertIn(f"Exec=python3 {self.app_dir.resolve() / 'webapp-manager.py'}\n", text)
        self.assertIn(f'Icon={self.icon_path}\n', text)
        self.assertIn(f'StartupWMClass={manager_integration.APP_ID}\n', text)
        self.logger.warning.assert_not_called()

    def test_exec_path_with_spaces_is_quoted(self):
        self.app_dir = self.root / 'my app'
        self.app_dir.mkdir()
        self.run_integration()
        text = self.desktop_path.read_text(encoding='utf-8')
        self.assertIn(f"Exec=python3 '{self.app_dir.resolve() / 'webapp-manager.py'}'\n", text)

    def test_unchanged_files_are_not_rewritten(self):
        self.run_integration()
        with mock.patch.object(Path, 'write_text') as write_text, \
                mock.patch.object(Path, 'write_bytes') as write_bytes:
            self.run_integration()
        write_text.assert_not_called()
        write_bytes.assert_not_called()

    def test_changed_icon_is_replaced(self):
        self.run_integration()
        self.icon_source.write_bytes(b'icon-v2')
        self.run_integration()
        self.assertEqual(self.icon_path.read_bytes(), b'icon-v2')

    def test_missing_icon_source_still_writes_the_entry(self):
        self.icon_source.unlink()
        self.run_integration()
        self.assertFalse(self.icon_path.exists())
        self.assertTrue(self.desktop_path.exists())

    def test_icon_failure_is_logged_and_entry_still_written(self):
        self.icon_path.mkdir(parents=True)  # a directory where the icon should go
        self.run_integration()
        self.logger.warning.assert_called_once()
        self.assertIn('manager icon', self.logger.warning.call_args.args[0])
        self.assertTrue(self.desktop_path.exists())

    def test_unreadable_entry_and_failed_write_are_logged(self):
        self.desktop_path.mkdir(parents=True)  # neither readable nor writable as a file
        self.run_integration()
        self.logger.warning.assert_called_once()
        self.assertIn('desktop integration', self.logger.warning.call_args.args[0])

    def test_uncreatable_directories_are_logged(self):
        (self.home / '.local').write_text('not a directory', encoding='utf-8')
        self.run_integration()
        self.logger.warning.assert_called_once()
        self.assertIn('desktop integration', self.logger.warning.call_args.args[0])


if __name__ == '__main__':
    unittest.main()

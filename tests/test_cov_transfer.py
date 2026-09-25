"""Coverage tests for DetailPageTransferMixin (detail_page/transfer.py).

Covers .wapp export/import on the detail page, managed-profile deletion and
save_desktop_file. `_TransferHarness` inherits the real mixin and fakes every
widget and every collaborator from the other mixins, so no GTK widget tree is
built. Files only ever live in temporary directories.
"""
import base64
import json
import logging
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_transfer.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

from gi.repository import Gio, Gtk

import gi_versions  # noqa: F401
from detail_page import DetailPage
from detail_page.transfer import DetailPageTransferMixin
from icon_pipeline import SVG_CAIRO_MISSING_ERROR
from webapp_constants import (
    ADDRESS_KEY,
    COLOR_SCHEME_KEY,
    DEFAULT_ZOOM_KEY,
    ICON_PATH_KEY,
    OPTION_NOTIFICATIONS_KEY,
    PROFILE_NAME_KEY,
    PROFILE_PATH_KEY,
    USER_AGENT_NAME_KEY,
    USER_AGENT_VALUE_KEY,
)

MODULE = 'detail_page.transfer'
ENGINES = [{'id': 1, 'name': 'Firefox', 'command': 'firefox'}, {'id': 2, 'name': 'Chrome', 'command': 'google-chrome'}]


class FakeWidget:
    def __init__(self, text=''):
        self.text = text
        self.active = False
        self.selected = 0
        self.visible = True
        self.selected_history = []

    def set_text(self, text):
        self.text = text

    def get_text(self):
        return self.text

    def set_active(self, value):
        self.active = bool(value)

    def get_active(self):
        return self.active

    def set_selected(self, index):
        self.selected = index
        self.selected_history.append(index)

    def set_visible(self, value):
        self.visible = bool(value)


class FakeSpinner:
    def __init__(self):
        self.spinning = None

    def start(self):
        self.spinning = True

    def stop(self):
        self.spinning = False


COLLABORATORS = (
    'refresh_user_agent_options', '_show_plugin_banner', '_trigger_address_validation', '_set_url_status',
    '_sync_icon_filename', 'refresh_icon_preview', 'refresh_icon_page', '_refresh_asset_pages',
    '_update_export_button_state', '_emit_visual_changed', '_update_browser_dependent_controls',
    '_refresh_profile_button_label', '_refresh_header_meta', '_set_detail_action_status',
    '_save_file_dialog', '_open_file_dialog', '_write_text_to_gfile', '_copy_gfile_to_temp_path',
    '_present_choice_dialog', '_reload_options_cache_from_db', '_apply_option_values_to_controls',
)


class _TransferHarness(DetailPageTransferMixin):
    _safe_int = DetailPage._safe_int

    def __init__(self, options=None, icon_target=None):
        self.entry = SimpleNamespace(id=5, title='My App', description='Desc', active=True)
        self.options = dict(options or {})
        self.set_calls = []
        self.added = []
        self.engines_list = [dict(engine) for engine in ENGINES]
        self.engine_user_agents = {1: [{'name': 'Mobile', 'value': 'UA-m'}], 2: []}
        self.title_entry = FakeWidget()
        self.description_entry = FakeWidget()
        self.switch = FakeWidget()
        self.address_entry = FakeWidget()
        self.engine_dropdown = FakeWidget()
        self.user_agent_dropdown = FakeWidget()
        self.color_scheme_dropdown = FakeWidget()
        self.default_zoom_dropdown = FakeWidget()
        self.mode_dropdown = FakeWidget()
        self.color_scheme_values = ['auto', 'light', 'dark']
        self.default_zoom_values = ['50', '100', '125']
        self.switches = {OPTION_NOTIFICATIONS_KEY: FakeWidget()}
        self.plugin_activity_label = FakeWidget('busy')
        self.plugin_activity_row = FakeWidget()
        self.plugin_activity_spinner = FakeSpinner()
        self._suspend_change_handlers = False
        self._suspend_address_processing = False
        self.icon_target = icon_target
        self.on_title_changed = mock.Mock()
        self.on_visual_changed = mock.Mock()
        for name in COLLABORATORS:
            setattr(self, name, mock.Mock(name=name))
        self._resolve_user_agent_selection = mock.Mock(return_value=(1, {'name': 'Mobile'}))
        self._current_mode_index = mock.Mock(return_value=2)

    # --- collaborators with real behaviour ---------------------------------
    def _options_dict(self):
        return dict(self.options)

    def _get_option_value(self, key):
        return self.options.get(key)

    def _set_option_value(self, key, value, commit=True):
        self.set_calls.append((key, value, commit))
        self.options[key] = value

    def _add_options(self, updates):
        self.added.append(dict(updates))
        self.options.update(updates)

    def _get_current_engine(self):
        index = self.engine_dropdown.selected
        return self.engines_list[index - 1] if index > 0 else None

    def _normalize_address_for_ui(self, value):
        value = (value or '').strip()
        return 'https://' + value[len('http://'):] if value.startswith('http://') else value

    def _managed_icon_target(self):
        return self.icon_target


class _PatchedTestCase(unittest.TestCase):
    def setUp(self):
        self.idle = []
        for target, replacement in (
            ('t', lambda key, **kwargs: key),
            ('GLib', SimpleNamespace(idle_add=lambda callback, *args: self.idle.append((callback, args)))),
        ):
            patcher = mock.patch(f'{MODULE}.{target}', replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)


class BuildPayloadTests(_PatchedTestCase):
    def test_payload_carries_entry_fields_and_portable_options(self):
        page = _TransferHarness({ADDRESS_KEY: 'https://a.example', PROFILE_PATH_KEY: '/p', PROFILE_NAME_KEY: 'n'})
        page.entry.active = 0
        payload = page._build_wapp_payload()
        self.assertEqual(payload['title'], 'My App')
        self.assertEqual(payload['description'], 'Desc')
        self.assertIs(payload['active'], False)
        self.assertEqual(payload['options'][ADDRESS_KEY], 'https://a.example')
        self.assertNotIn(PROFILE_PATH_KEY, payload['options'])
        self.assertIsNone(payload['icon'])

    def test_missing_title_and_description_become_empty(self):
        page = _TransferHarness()
        page.entry.title = None
        page.entry.description = None
        payload = page._build_wapp_payload()
        self.assertEqual((payload['title'], payload['description']), ('', ''))


class ApplyPayloadTests(_PatchedTestCase):
    def _apply(self, page, payload):
        page.save_desktop_file = mock.Mock()
        return page._apply_wapp_payload(payload)

    def test_rejected_payload_changes_nothing(self):
        page = _TransferHarness()
        self.assertFalse(self._apply(page, ['not', 'an', 'object']))
        self.assertEqual(page.set_calls, [])
        page.save_desktop_file.assert_not_called()

    def test_full_payload_updates_every_control(self):
        page = _TransferHarness(icon_target=self.tmp / 'icon.png')

        page._resolve_user_agent_selection = mock.Mock(side_effect=lambda engine, available, persist_default: (1, available[0]))
        payload = {
            'title': 'Imported',
            'description': 'Imported description',
            'active': False,
            'options': {
                ADDRESS_KEY: 'http://imported.example',
                'EngineID': '1',
                USER_AGENT_NAME_KEY: 'Mobile',
                USER_AGENT_VALUE_KEY: 'UA-m',
                'Inline Custom CSS': 'a{}\r\nb{}\rc{}',
                'Inline Custom CSS Hash': ' abc ',
                COLOR_SCHEME_KEY: 'Dark',
                DEFAULT_ZOOM_KEY: '125',
                OPTION_NOTIFICATIONS_KEY: True,
            },
            'icon': {'filename': 'i.png', 'mime': 'image/png', 'data_base64': base64.b64encode(b'png-bytes').decode()},
        }
        with mock.patch(f'{MODULE}.normalize_icon_bytes_to_png') as normalize:
            self.assertTrue(self._apply(page, payload))

        self.assertEqual(page.title_entry.text, 'Imported')
        self.assertEqual(page.description_entry.text, 'Imported description')
        self.assertFalse(page.switch.active)
        # The address is first taken verbatim, then normalized for the UI.
        self.assertEqual(page.address_entry.text, 'https://imported.example')
        self.assertEqual(page.options[ADDRESS_KEY], 'https://imported.example')
        self.assertFalse(page._suspend_address_processing)
        page._trigger_address_validation.assert_called_once_with('https://imported.example', debounce=False, export_after_validation=False)
        self.assertEqual(page.engine_dropdown.selected, 1)
        self.assertEqual((page.options['EngineID'], page.options['EngineName']), ('1', 'Firefox'))
        page.refresh_user_agent_options.assert_called_once_with()
        self.assertEqual(page.options[USER_AGENT_VALUE_KEY], 'UA-m')
        self.assertEqual(page.user_agent_dropdown.selected, 1)
        self.assertEqual(page._resolve_user_agent_selection.call_args.args[0]['id'], 1)
        self.assertTrue(page._resolve_user_agent_selection.call_args.kwargs['persist_default'])
        self.assertFalse(page._suspend_change_handlers)
        self.assertIn(('Inline Custom CSS', 'a{}\nb{}\nc{}', False), page.set_calls)
        self.assertIn(('Inline Custom CSS Hash', 'abc', False), page.set_calls)
        self.assertIn(('Inline Custom JavaScript', '', False), page.set_calls)
        self.assertEqual(page.color_scheme_dropdown.selected, 2)
        self.assertEqual(page.default_zoom_dropdown.selected, 2)
        self.assertTrue(page.switches[OPTION_NOTIFICATIONS_KEY].active)
        self.assertEqual(page.mode_dropdown.selected, 2)
        self.assertEqual(normalize.call_args.args[:2], (b'png-bytes', self.tmp / 'icon.png'))
        self.assertEqual(normalize.call_args.kwargs, {'source_name': 'i.png', 'content_type': 'image/png'})
        self.assertEqual(page.options[ICON_PATH_KEY], str(self.tmp / 'icon.png'))
        for name in ('_sync_icon_filename', 'refresh_icon_preview', 'refresh_icon_page', '_refresh_asset_pages',
                     '_update_export_button_state', '_emit_visual_changed', '_update_browser_dependent_controls',
                     '_refresh_profile_button_label'):
            getattr(page, name).assert_called_once_with()
        page.save_desktop_file.assert_called_once_with()

    def test_unknown_or_empty_engine_selects_no_engine(self):
        for raw in ('99', ''):
            with self.subTest(engine_id=raw):
                page = _TransferHarness()
                page.engine_dropdown.selected = 2
                self.assertTrue(self._apply(page, {'options': {'EngineID': raw}}))
                self.assertEqual(page.engine_dropdown.selected, 0)
                self.assertNotIn('EngineName', page.options)

    def test_no_address_clears_url_status_and_missing_icon_clears_icon(self):
        page = _TransferHarness({ICON_PATH_KEY: '/old/icon.png'})
        del page.mode_dropdown
        self.assertTrue(self._apply(page, {'title': 'x', 'options': {COLOR_SCHEME_KEY: 'sepia', DEFAULT_ZOOM_KEY: '333'}}))
        page._set_url_status.assert_called_once_with('', None)
        page._trigger_address_validation.assert_not_called()
        self.assertEqual(page.options[ICON_PATH_KEY], '')
        self.assertEqual(page.color_scheme_dropdown.selected_history, [])
        self.assertEqual(page.default_zoom_dropdown.selected_history, [])
        page.refresh_user_agent_options.assert_not_called()

    def test_https_address_is_not_rewritten_twice(self):
        page = _TransferHarness()
        self.assertTrue(self._apply(page, {'options': {ADDRESS_KEY: 'https://ok.example'}}))
        self.assertEqual([call for call in page.set_calls if call[0] == ADDRESS_KEY], [(ADDRESS_KEY, 'https://ok.example', True)])
        page._trigger_address_validation.assert_called_once()

    def test_icon_without_data_leaves_the_icon_alone(self):
        page = _TransferHarness({ICON_PATH_KEY: '/old/icon.png'})
        self.assertTrue(self._apply(page, {'icon': {'filename': 'x.png', 'data_base64': ''}}))
        self.assertEqual(page.options[ICON_PATH_KEY], '/old/icon.png')

    def test_broken_icon_data_is_logged_not_raised(self):
        page = _TransferHarness(icon_target=self.tmp / 'icon.png')
        with mock.patch(f'{MODULE}.normalize_icon_bytes_to_png') as normalize:
            self.assertTrue(self._apply(page, {'icon': {'data_base64': 'abc'}}))
        normalize.assert_not_called()
        page._show_plugin_banner.assert_not_called()
        self.assertNotIn(ICON_PATH_KEY, page.options)

    def test_missing_svg_support_shows_a_banner(self):
        page = _TransferHarness(icon_target=self.tmp / 'icon.png')
        error = ValueError(SVG_CAIRO_MISSING_ERROR)
        with mock.patch(f'{MODULE}.normalize_icon_bytes_to_png', side_effect=error):
            self.assertTrue(self._apply(page, {'icon': {'data_base64': base64.b64encode(b'<svg/>').decode()}}))
        page._show_plugin_banner.assert_called_once_with('svg_import_requires_cairo', timeout_ms=4200)


class ExportTests(_PatchedTestCase):
    def test_export_click_suggests_a_dated_file_name(self):
        page = _TransferHarness()
        page.on_export_webapp_clicked(None)
        title, name, callback = page._save_file_dialog.call_args.args
        self.assertEqual(title, 'export_webapp_button')
        self.assertRegex(name, r'^My App_\d{4}-\d{2}-\d{2}\.wapp$')
        self.assertEqual(callback, page.on_export_wapp_selected)
        page.entry.title = '   '
        page.on_export_webapp_clicked(None)
        self.assertTrue(re.match(r'^webapp_', page._save_file_dialog.call_args.args[1]))

    def test_export_to_gio_file_writes_json(self):
        page = _TransferHarness({ADDRESS_KEY: 'https://a.example'})
        page._write_text_to_gfile.return_value = True
        target = Gio.File.new_for_path(str(self.tmp / 'out.wapp'))
        page.on_export_wapp_selected(target)
        file_obj, text = page._write_text_to_gfile.call_args.args
        self.assertIs(file_obj, target)
        self.assertEqual(json.loads(text)['options'][ADDRESS_KEY], 'https://a.example')
        page._set_detail_action_status.assert_called_once_with('export_webapp_success')

    def test_export_failure_paths(self):
        page = _TransferHarness()
        target = Gio.File.new_for_path(str(self.tmp / 'out.wapp'))
        page._write_text_to_gfile.return_value = False
        page.on_export_wapp_selected(target)
        page._write_text_to_gfile.side_effect = OSError('denied')
        page.on_export_wapp_selected(target)
        self.assertEqual([c.args[0] for c in page._set_detail_action_status.call_args_list], ['export_webapp_failed'] * 2)

    def test_legacy_dialog_results(self):
        page = _TransferHarness()
        dialog = mock.Mock()
        page.on_export_wapp_selected(dialog, Gtk.ResponseType.CANCEL)
        dialog.destroy.assert_called_once_with()
        page._write_text_to_gfile.assert_not_called()
        page.on_export_wapp_selected(None, Gtk.ResponseType.CANCEL)  # no dialog: nothing to destroy
        page._write_text_to_gfile.return_value = True
        dialog = mock.Mock()
        page.on_export_wapp_selected(dialog, Gtk.ResponseType.ACCEPT)
        dialog.destroy.assert_called_once_with()
        self.assertIs(page._write_text_to_gfile.call_args.args[0], dialog.get_file.return_value)


class ImportTests(_PatchedTestCase):
    def _write(self, name, data):
        path = self.tmp / name
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding='utf-8')
        return path

    def _import(self, page, temp_path, local_name='chosen.wapp'):
        page._copy_gfile_to_temp_path.return_value = temp_path
        source = Gio.File.new_for_path(str(self.tmp / local_name))
        page.on_import_wapp_selected(source)
        return source

    def test_import_click_opens_a_wapp_filter_dialog(self):
        page = _TransferHarness()
        page.on_import_webapp_clicked(None)
        args, kwargs = page._open_file_dialog.call_args
        self.assertEqual(args, ('import_webapp_button', page.on_import_wapp_selected))
        self.assertEqual(kwargs, {'patterns': [('wapp_filter_name', '*.wapp')]})

    def test_single_payload_is_applied_and_temp_copy_removed(self):
        page = _TransferHarness()
        page._apply_wapp_payload = mock.Mock(return_value=True)
        temp = self._write('temp-copy.wapp', {'title': 'T', 'options': {}})
        self._import(page, temp)
        self.assertEqual(page._apply_wapp_payload.call_args.args[0]['title'], 'T')
        page._set_detail_action_status.assert_called_once_with('import_webapp_success')
        self.assertFalse(temp.exists())

    def test_temp_path_equal_to_source_is_kept(self):
        page = _TransferHarness()
        page._apply_wapp_payload = mock.Mock(return_value=False)
        temp = self._write('chosen.wapp', {'title': 'T'})
        self._import(page, temp, local_name='chosen.wapp')
        page._set_detail_action_status.assert_called_once_with('import_webapp_failed')
        self.assertTrue(temp.exists())

    def test_bundles_are_redirected_to_the_main_import(self):
        page = _TransferHarness()
        page._apply_wapp_payload = mock.Mock()
        temp = self._write('bundle.wapp', {'format': 'webapp-export-bundle-v1', 'entries': [{'title': 'a'}, {'title': 'b'}]})
        self._import(page, temp)
        page._apply_wapp_payload.assert_not_called()
        page._set_detail_action_status.assert_called_once_with('import_bundle_use_main_import')

    def test_inline_javascript_asks_for_confirmation(self):
        page = _TransferHarness()
        page._apply_wapp_payload = mock.Mock(return_value=True)
        temp = self._write('js.wapp', {'title': 'T', 'options': {'Inline Custom JavaScript': 'alert(1)'}})
        source = self._import(page, temp)
        page._apply_wapp_payload.assert_not_called()
        anchor, message, callback = page._present_choice_dialog.call_args.args
        self.assertIs(anchor, source)
        self.assertEqual(message, 'import_javascript_warning')
        self.assertEqual(page._present_choice_dialog.call_args.kwargs, {'destructive': False})
        callback(True)
        self.assertEqual(page._apply_wapp_payload.call_args.args[0]['options']['Inline Custom JavaScript'], 'alert(1)')
        page._set_detail_action_status.assert_called_once_with('import_webapp_success')

    def test_missing_temp_copy_and_broken_files_fail(self):
        page = _TransferHarness()
        self._import(page, None)
        broken = self._write('broken.wapp', '{not json')
        self._import(page, broken)
        self.assertFalse(broken.exists())
        self.assertEqual([c.args[0] for c in page._set_detail_action_status.call_args_list], ['import_webapp_failed'] * 2)

    def test_legacy_dialog_results(self):
        page = _TransferHarness()
        dialog = mock.Mock()
        page.on_import_wapp_selected(dialog, Gtk.ResponseType.CANCEL)
        dialog.destroy.assert_called_once_with()
        page._copy_gfile_to_temp_path.assert_not_called()
        page.on_import_wapp_selected(None, Gtk.ResponseType.CANCEL)
        dialog = mock.Mock()
        dialog.get_file.return_value = None
        page._copy_gfile_to_temp_path.return_value = None
        page.on_import_wapp_selected(dialog, Gtk.ResponseType.ACCEPT)
        dialog.destroy.assert_called_once_with()
        page._copy_gfile_to_temp_path.assert_called_once_with(None, '.wapp')
        page._set_detail_action_status.assert_called_once_with('import_webapp_failed')

    def test_complete_single_import(self):
        page = _TransferHarness()
        page._apply_wapp_payload = mock.Mock(side_effect=[True, False])
        page._complete_single_wapp_import({'title': 'x'}, False)
        page._apply_wapp_payload.assert_not_called()
        page._complete_single_wapp_import({'title': 'x'}, True)
        page._complete_single_wapp_import({'title': 'x'}, True)
        self.assertEqual(
            [c.args[0] for c in page._set_detail_action_status.call_args_list],
            ['import_webapp_failed', 'import_webapp_success', 'import_webapp_failed'],
        )


class DeleteProfileTests(_PatchedTestCase):
    def test_click_asks_destructively(self):
        page = _TransferHarness()
        page.on_delete_profile_clicked('button')
        page._present_choice_dialog.assert_called_once_with(
            'button', 'profile_delete_confirm', page._handle_delete_profile_confirmed, destructive=True)

    def test_declined_confirmation_deletes_nothing(self):
        page = _TransferHarness()
        with mock.patch(f'{MODULE}.delete_managed_browser_profiles') as delete:
            page._handle_delete_profile_confirmed(False)
        delete.assert_not_called()

    def test_confirmed_deletion_resets_profile_and_engine(self):
        page = _TransferHarness({PROFILE_PATH_KEY: '/p', PROFILE_NAME_KEY: 'n', 'EngineID': '1'})
        page.save_desktop_file = mock.Mock()
        page.engine_dropdown.selected = 1
        with mock.patch(f'{MODULE}.delete_managed_browser_profiles') as delete:
            page._handle_delete_profile_confirmed(True)
        self.assertEqual(delete.call_args.args[0], 'My App')
        self.assertEqual(delete.call_args.kwargs, {'stored_profile_path': '/p', 'stored_profile_name': 'n'})
        self.assertEqual(page.options[PROFILE_PATH_KEY], '')
        self.assertEqual(page.options['EngineID'], '')
        self.assertEqual(page.engine_dropdown.selected, 0)
        page._set_detail_action_status.assert_called_once_with('profile_delete_success')
        page.save_desktop_file.assert_called_once_with()
        self.assertEqual(self.idle, [(page._emit_visual_changed, ())])

    def test_failed_deletion_is_reported(self):
        page = _TransferHarness()
        page.save_desktop_file = mock.Mock()
        with mock.patch(f'{MODULE}.delete_managed_browser_profiles', side_effect=ValueError('outside root')) as delete:
            page._handle_delete_profile_confirmed(True)
        self.assertEqual(delete.call_args.kwargs, {'stored_profile_path': '', 'stored_profile_name': ''})
        page._set_detail_action_status.assert_called_once_with('profile_delete_failed')
        page.save_desktop_file.assert_not_called()


class PluginActivityTests(_PatchedTestCase):
    def test_activity_row_stays_hidden_and_spinner_follows_state(self):
        page = _TransferHarness()
        page._set_plugin_activity('installing', active=True)
        self.assertTrue(page.plugin_activity_spinner.spinning)
        self.assertFalse(page.plugin_activity_row.visible)
        self.assertEqual(page.plugin_activity_label.text, '')
        page._set_plugin_activity()
        self.assertFalse(page.plugin_activity_spinner.spinning)


class SaveDesktopFileTests(_PatchedTestCase):
    def test_result_updates_profile_and_address(self):
        page = _TransferHarness({ADDRESS_KEY: 'http://a.example'})
        page.address_entry.text = 'http://a.example'
        result = {'profile_name': 'n', 'profile_path': '/p', 'normalized_address': 'https://a.example', 'profile_migrated': True}
        with mock.patch(f'{MODULE}.export_desktop_file', return_value=result) as export:
            page.save_desktop_file()
        self.assertIs(export.call_args.args[0], page.entry)
        self.assertIs(export.call_args.args[2], page.engines_list)
        self.assertEqual(page.added, [{PROFILE_NAME_KEY: 'n', PROFILE_PATH_KEY: '/p', ADDRESS_KEY: 'https://a.example'}])
        self.assertEqual(page.address_entry.text, 'https://a.example')
        self.assertFalse(page._suspend_address_processing)
        page._trigger_address_validation.assert_called_once_with('https://a.example', debounce=False, export_after_validation=False)
        page._show_plugin_banner.assert_called_once_with('profile_import_completed')
        page._sync_icon_filename.assert_called_once_with()
        page._reload_options_cache_from_db.assert_called_once_with()
        page._apply_option_values_to_controls.assert_called_once_with()
        page.on_visual_changed.assert_called_once_with(page.entry)
        page.on_title_changed.assert_called_once_with(page.entry)

    def test_result_with_same_address_only_stores_profile(self):
        page = _TransferHarness()
        page.address_entry.text = 'https://a.example'
        page.on_visual_changed = None
        page.on_title_changed = None
        with mock.patch(f'{MODULE}.export_desktop_file', return_value={'normalized_address': 'https://a.example', 'profile_path': None}):
            page.save_desktop_file()
        self.assertEqual(page.added, [{PROFILE_NAME_KEY: '', PROFILE_PATH_KEY: ''}])
        page._trigger_address_validation.assert_not_called()
        page._show_plugin_banner.assert_not_called()

    def test_non_dict_result_still_refreshes(self):
        page = _TransferHarness()
        with mock.patch(f'{MODULE}.export_desktop_file', return_value=None):
            page.save_desktop_file()
        self.assertEqual(page.added, [])
        page._apply_option_values_to_controls.assert_called_once_with()

    def test_export_error_is_logged_and_swallowed(self):
        page = _TransferHarness()
        with mock.patch(f'{MODULE}.export_desktop_file', side_effect=OSError('read-only')):
            page.save_desktop_file()
        page._reload_options_cache_from_db.assert_not_called()
        page.on_title_changed.assert_not_called()


if __name__ == '__main__':
    unittest.main()

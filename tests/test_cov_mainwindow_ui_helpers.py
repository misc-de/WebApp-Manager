"""Coverage for the small MainWindow mixins: notifications, dialogs, window state.

The mixins are exercised without a widget tree: a harness class borrows the
real methods, and the GTK/Adw/GLib names each module reads are replaced by
mocks, so nothing here needs a display.
"""
import json
import logging
import sys
import types
import unittest
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov.ui.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

from mainwindow import dialogs as dialogs_mod  # noqa: E402
from mainwindow import notifications as notifications_mod  # noqa: E402
from mainwindow import window_state as window_state_mod  # noqa: E402
from mainwindow import MainWindowDialogsMixin, MainWindowNotificationsMixin, MainWindowWindowStateMixin  # noqa: E402


def _fake_t(key, **kwargs):
    if kwargs:
        return f'{key}|' + ','.join(f'{k}={v}' for k, v in sorted(kwargs.items()))
    return key


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

class _NotifyHarness(MainWindowNotificationsMixin):
    def __init__(self):
        self.busy_label = mock.MagicMock()
        self.busy_overlay = mock.MagicMock()
        self.busy_spinner = mock.MagicMock()
        self.global_toast_label = mock.MagicMock()
        self.global_toast_revealer = mock.MagicMock()


class NotificationsMixinTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(notifications_mod, 't', side_effect=_fake_t)
        patcher.start()
        self.addCleanup(patcher.stop)
        glib_patcher = mock.patch.object(notifications_mod, 'GLib')
        self.glib = glib_patcher.start()
        self.addCleanup(glib_patcher.stop)
        self.h = _NotifyHarness()

    def test_show_busy_uses_given_message_and_normal_size(self):
        self.h._show_busy('Working')
        self.h.busy_label.set_text.assert_called_once_with('Working')
        self.h.busy_overlay.remove_css_class.assert_called_once_with('startup-busy')
        self.h.busy_label.remove_css_class.assert_called_once_with('startup-busy-label')
        self.h.busy_spinner.set_size_request.assert_called_once_with(32, 32)
        self.h.busy_overlay.set_visible.assert_called_once_with(True)
        self.h.busy_spinner.start.assert_called_once_with()

    def test_show_busy_falls_back_to_loading_text(self):
        self.h._show_busy()
        self.h.busy_label.set_text.assert_called_once_with('loading')

    def test_hide_busy_stops_spinner_and_resets_startup_style(self):
        self.h._hide_busy()
        self.h.busy_spinner.stop.assert_called_once_with()
        self.h.busy_overlay.set_visible.assert_called_once_with(False)
        self.h.busy_overlay.remove_css_class.assert_called_once_with('startup-busy')
        self.h.busy_label.remove_css_class.assert_called_once_with('startup-busy-label')
        self.h.busy_spinner.set_size_request.assert_called_once_with(32, 32)

    def test_show_startup_busy_uses_large_spinner_and_startup_classes(self):
        self.h._show_startup_busy()
        self.h.busy_label.set_text.assert_called_once_with('startup_profile_loading')
        self.h.busy_overlay.add_css_class.assert_called_once_with('startup-busy')
        self.h.busy_label.add_css_class.assert_called_once_with('startup-busy-label')
        self.h.busy_spinner.set_size_request.assert_called_once_with(64, 64)
        self.h.busy_overlay.set_visible.assert_called_once_with(True)
        self.h.busy_spinner.start.assert_called_once_with()

    def test_cancel_timeout_without_pending_source_does_nothing(self):
        self.h._cancel_global_toast_timeout()
        self.glib.source_remove.assert_not_called()

    def test_cancel_timeout_removes_pending_source(self):
        self.h.global_toast_timeout_id = 42
        self.h._cancel_global_toast_timeout()
        self.glib.source_remove.assert_called_once_with(42)
        self.assertEqual(self.h.global_toast_timeout_id, 0)

    def test_hide_global_toast_hides_revealer_and_returns_false(self):
        self.h.global_toast_timeout_id = 7
        self.assertFalse(self.h._hide_global_toast())
        self.glib.source_remove.assert_called_once_with(7)
        self.h.global_toast_revealer.set_reveal_child.assert_called_once_with(False)

    def test_hide_global_toast_without_revealer_is_safe(self):
        del self.h.global_toast_revealer
        self.assertFalse(self.h._hide_global_toast())

    def test_overlay_notification_shows_stripped_text_and_arms_timeout(self):
        self.glib.timeout_add.return_value = 99
        self.h.show_overlay_notification('  Saved  ', timeout_ms=1234)
        self.h.global_toast_label.set_text.assert_called_once_with('Saved')
        self.h.global_toast_revealer.set_reveal_child.assert_called_once_with(True)
        self.glib.timeout_add.assert_called_once_with(1234, self.h._hide_global_toast)
        self.assertEqual(self.h.global_toast_timeout_id, 99)

    def test_overlay_notification_replaces_previous_timeout(self):
        self.h.global_toast_timeout_id = 5
        self.glib.timeout_add.return_value = 6
        self.h.show_overlay_notification('Again')
        self.glib.source_remove.assert_called_once_with(5)
        self.assertEqual(self.h.global_toast_timeout_id, 6)
        self.assertEqual(self.glib.timeout_add.call_args[0][0], 3000)

    def test_blank_or_none_message_hides_toast(self):
        for message in ('   ', None):
            self.h.global_toast_revealer.reset_mock()
            self.h.show_overlay_notification(message)
            self.h.global_toast_revealer.set_reveal_child.assert_called_once_with(False)
        self.h.global_toast_label.set_text.assert_not_called()
        self.glib.timeout_add.assert_not_called()


# --------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------

class _FakeDialog:
    """Records every call and lets a test fire the 'response' signal."""

    def __init__(self):
        self.calls = []
        self.handlers = []
        self.presented_with = None

    def __getattr__(self, name):
        def record(*args):
            self.calls.append((name, args))
        return record

    def connect(self, signal, handler):
        self.calls.append(('connect', (signal,)))
        self.handlers.append(handler)

    def present(self, *args):
        self.presented_with = args

    def fire(self, response):
        for handler in self.handlers:
            handler(self, response)


def _fake_adw(with_alert_dialog):
    created = []

    def alert_dialog():
        dialog = _FakeDialog()
        created.append(dialog)
        return dialog

    def message_dialog_new(parent, heading, body):
        dialog = _FakeDialog()
        dialog.ctor = (parent, heading, body)
        created.append(dialog)
        return dialog

    adw = types.SimpleNamespace(
        MessageDialog=types.SimpleNamespace(new=message_dialog_new),
        ResponseAppearance=types.SimpleNamespace(DESTRUCTIVE='destructive'),
    )
    if with_alert_dialog:
        adw.AlertDialog = alert_dialog
    return adw, created


class _DialogHarness(MainWindowDialogsMixin):
    def __init__(self):
        self.next_conflicts = 0
        self.upserts = []
        self.options = {}

    def _show_next_conflict(self):
        self.next_conflicts += 1

    def _upsert_entry_from_file(self, file_data, existing_entry=None):
        self.upserts.append((file_data, existing_entry))

    def _get_options_dict(self, entry_id):
        return {'id': entry_id}


class DialogsMixinTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(dialogs_mod, 't', side_effect=_fake_t)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.h = _DialogHarness()

    def _use_adw(self, with_alert_dialog):
        adw, created = _fake_adw(with_alert_dialog)
        patcher = mock.patch.object(dialogs_mod, 'Adw', adw)
        patcher.start()
        self.addCleanup(patcher.stop)
        return created

    def test_info_dialog_uses_alert_dialog_when_available(self):
        created = self._use_adw(True)
        self.h._present_info_dialog('Hello')
        dialog = created[0]
        self.assertIn(('set_heading', ('app_title',)), dialog.calls)
        self.assertIn(('set_body', ('Hello',)), dialog.calls)
        self.assertIn(('add_response', ('close', 'dialog_close')), dialog.calls)
        self.assertIn(('set_close_response', ('close',)), dialog.calls)
        self.assertEqual(dialog.presented_with, (self.h,))

    def test_info_dialog_falls_back_to_message_dialog(self):
        created = self._use_adw(False)
        self.h._present_info_dialog('Hello')
        dialog = created[0]
        self.assertEqual(dialog.ctor, (self.h, 'app_title', 'Hello'))
        self.assertIn(('set_default_response', ('close',)), dialog.calls)
        self.assertEqual(dialog.presented_with, ())

    def test_choice_dialog_reports_yes_once(self):
        for with_alert in (True, False):
            with self.subTest(alert_dialog=with_alert):
                created = self._use_adw(with_alert)
                results = []
                self.h._present_choice_dialog('Sure?', results.append)
                dialog = created[-1]
                self.assertIn(('add_response', ('yes', 'dialog_yes')), dialog.calls)
                self.assertIn(('set_close_response', ('no',)), dialog.calls)
                dialog.fire('yes')
                dialog.fire('no')
                self.assertEqual(results, [True])

    def test_choice_dialog_close_counts_as_no(self):
        created = self._use_adw(True)
        results = []
        self.h._present_choice_dialog('Sure?', results.append)
        created[0].fire('close')
        self.assertEqual(results, [False])

    def test_destructive_choice_marks_yes_destructive(self):
        for with_alert in (True, False):
            with self.subTest(alert_dialog=with_alert):
                created = self._use_adw(with_alert)
                self.h._present_choice_dialog('Delete?', lambda _v: None, destructive=True)
                self.assertIn(('set_response_appearance', ('yes', 'destructive')), created[-1].calls)

    def test_non_destructive_choice_leaves_appearance(self):
        created = self._use_adw(True)
        self.h._present_choice_dialog('Sure?', lambda _v: None)
        self.assertNotIn('set_response_appearance', [name for name, _ in created[0].calls])

    def test_yes_no_dialog_runs_callback_then_advances_queue(self):
        created = self._use_adw(True)
        answers = []
        self.h._present_yes_no_dialog('Q?', answers.append)
        self.assertEqual(self.h.next_conflicts, 0)
        created[0].fire('no')
        self.assertEqual(answers, [False])
        self.assertEqual(self.h.next_conflicts, 1)

    def test_orphan_file_imports_only_when_accepted(self):
        conflict = {'file': {'path': '/x.desktop'}}
        self.h._handle_orphan_file(conflict, False)
        self.assertEqual(self.h.upserts, [])
        self.h._handle_orphan_file(conflict, True)
        self.assertEqual(self.h.upserts, [({'path': '/x.desktop'}, None)])

    def test_missing_file_recreates_only_when_accepted(self):
        entry = types.SimpleNamespace(id=3)
        with mock.patch.object(dialogs_mod, 'export_desktop_file') as export:
            self.h._handle_missing_file({'entry': entry}, False)
            export.assert_not_called()
            self.h._handle_missing_file({'entry': entry}, True)
            export.assert_called_once_with(entry, {'id': 3}, dialogs_mod.ENGINES, dialogs_mod.LOG)

    def test_mismatch_prefers_file_or_rewrites_from_db(self):
        entry = types.SimpleNamespace(id=4)
        conflict = {'entry': entry, 'file': {'title': 'F'}}
        with mock.patch.object(dialogs_mod, 'export_desktop_file') as export:
            self.h._handle_mismatch(conflict, True)
            export.assert_not_called()
            self.assertEqual(self.h.upserts, [({'title': 'F'}, entry)])
            self.h._handle_mismatch(conflict, False)
            export.assert_called_once_with(entry, {'id': 4}, dialogs_mod.ENGINES, dialogs_mod.LOG)


# --------------------------------------------------------------------------
# Window state
# --------------------------------------------------------------------------

class _StateHarness(MainWindowWindowStateMixin):
    def __init__(self):
        self._window_state = {}
        self._window_state_save_source_id = 0
        self.ui_settings = {}
        self.maximized_calls = 0
        self.size = (800, 700)
        self.is_max = False

    def maximize(self):
        self.maximized_calls += 1

    def get_default_size(self):
        return self.size

    def is_maximized(self):
        return self.is_max


class WindowStateMixinTests(unittest.TestCase):
    def setUp(self):
        self.h = _StateHarness()

    def _patch(self, name, **kwargs):
        patcher = mock.patch.object(window_state_mod, name, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def test_load_window_state_returns_stored_dict(self):
        self._patch('get_app_config', return_value={'window_state': {'width': 900}})
        self.assertEqual(self.h._load_window_state(), {'width': 900})

    def test_load_window_state_rejects_bad_shapes_and_errors(self):
        cases = [
            {'return_value': ['not', 'a', 'dict']},
            {'return_value': {'window_state': 'broken'}},
            {'side_effect': OSError('unreadable')},
            {'side_effect': json.JSONDecodeError('bad', 'x', 0)},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), mock.patch.object(window_state_mod, 'get_app_config', **kwargs):
                self.assertEqual(self.h._load_window_state(), {})

    def test_apply_window_state_maximizes_only_when_stored(self):
        self.h._apply_window_state()
        self.assertEqual(self.h.maximized_calls, 0)
        self.h._window_state = {'maximized': True}
        self.h._apply_window_state()
        self.assertEqual(self.h.maximized_calls, 1)

    def test_apply_window_state_tolerates_missing_maximize(self):
        harness = types.SimpleNamespace(_window_state={'maximized': True})
        MainWindowWindowStateMixin._apply_window_state(harness)  # must not raise

    def test_load_ui_settings_reads_appearance(self):
        self._patch('get_app_config', return_value={'settings': {'appearance': 'dark'}})
        self.assertEqual(self.h._load_ui_settings(), {'appearance': 'dark'})

    def test_load_ui_settings_defaults(self):
        cases = [
            {'return_value': {'settings': {'appearance': ''}}},
            {'return_value': {'settings': 'nope'}},
            {'return_value': None},
            {'side_effect': ValueError('bad')},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), mock.patch.object(window_state_mod, 'get_app_config', **kwargs):
                self.assertEqual(self.h._load_ui_settings(), {'appearance': 'auto'})

    def test_load_language_setting(self):
        with mock.patch.object(window_state_mod, 'get_configured_language_value', return_value='de'):
            self.assertEqual(self.h._load_language_setting(), 'de')
        with mock.patch.object(window_state_mod, 'get_configured_language_value', side_effect=OSError):
            self.assertEqual(self.h._load_language_setting(), 'system')

    def test_save_ui_settings_merges_into_config(self):
        self._patch('get_app_config', return_value={'language': 'de', 'settings': {'other': 1}})
        save = self._patch('save_app_config')
        self.h.ui_settings = {'appearance': 'Light '}
        self.h._save_ui_settings()
        save.assert_called_once_with({'language': 'de', 'settings': {'other': 1, 'appearance': 'light'}})

    def test_save_ui_settings_handles_empty_config_and_errors(self):
        self._patch('get_app_config', return_value=None)
        save = self._patch('save_app_config', side_effect=OSError('read-only'))
        self.h._save_ui_settings()  # error is logged, not raised
        save.assert_called_once_with({'settings': {'appearance': 'auto'}})

    def test_appearance_value_normalises(self):
        for raw, expected in (('DARK', 'dark'), (' light ', 'light'), ('purple', 'auto'), (None, 'auto')):
            self.h.ui_settings = {'appearance': raw} if raw is not None else None
            self.assertEqual(self.h._appearance_value(), expected)

    def test_apply_ui_appearance_maps_to_color_scheme(self):
        adw = self._patch('Adw')
        manager = adw.StyleManager.get_default.return_value
        for value, attr in (('dark', 'FORCE_DARK'), ('light', 'FORCE_LIGHT'), ('auto', 'DEFAULT')):
            manager.reset_mock()
            self.h.ui_settings = {'appearance': value}
            self.h._apply_ui_appearance_setting()
            manager.set_color_scheme.assert_called_once_with(getattr(adw.ColorScheme, attr))

    def test_apply_ui_appearance_survives_missing_style_manager(self):
        adw = self._patch('Adw')
        adw.StyleManager.get_default.side_effect = AttributeError('no display')
        self.h._apply_ui_appearance_setting()  # logged, not raised

    def test_schedule_save_debounces_and_saves_once(self):
        glib = self._patch('GLib')
        glib.timeout_add.return_value = 17
        with mock.patch.object(self.h, '_save_window_state') as save:
            self.h._schedule_window_state_save()
            self.h._schedule_window_state_save()
            glib.timeout_add.assert_called_once()
            self.assertEqual(self.h._window_state_save_source_id, 17)
            delay, callback = glib.timeout_add.call_args[0]
            self.assertEqual(delay, 300)
            self.assertFalse(callback())
            save.assert_called_once_with()
            self.assertEqual(self.h._window_state_save_source_id, 0)

    def test_size_notify_schedules_save(self):
        with mock.patch.object(self.h, '_schedule_window_state_save') as schedule:
            self.h._on_window_size_notify(object(), object())
        schedule.assert_called_once_with()

    def test_collect_window_state_clamps_and_defaults(self):
        self.h.size = (100, 0)
        self.h.is_max = 1
        self.assertEqual(self.h._collect_window_state(), {'width': 320, 'height': 600, 'maximized': True})
        self.h.size = (1024, 768)
        self.h.is_max = False
        self.assertEqual(self.h._collect_window_state(), {'width': 1024, 'height': 768, 'maximized': False})

    def test_save_window_state_writes_config(self):
        self._patch('get_app_config', return_value={'language': 'en'})
        save = self._patch('save_app_config')
        self.h._save_window_state()
        save.assert_called_once_with({'language': 'en', 'window_state': {'width': 800, 'height': 700, 'maximized': False}})

    def test_save_window_state_swallows_write_errors(self):
        self._patch('get_app_config', return_value={})
        self._patch('save_app_config', side_effect=OSError('disk full'))
        self.h._save_window_state()  # must not raise

    def test_close_request_saves_and_allows_close(self):
        with mock.patch.object(self.h, '_save_window_state') as save:
            self.assertFalse(self.h._on_close_request(object()))
        save.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()

"""Tests for MainWindowProfileImportMixin (mainwindow/profile_import.py).

The mixin is exercised without a widget tree: a harness class inherits the
real methods and hand-implements the MainWindow API they call back into.
GTK widget construction is redirected to a recording fake, GLib.idle_add to a
queue the test drains by hand, and the resync worker thread runs inline, so
every test is synchronous and headless.
"""
import json
import logging
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_profile_import.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

from gi.repository import Gio, GLib, Gtk

import gi_versions  # noqa: F401
from browser_option_logic import browser_state_key
from i18n import t
from mainwindow import profile_import
from mainwindow.profile_import import MainWindowProfileImportMixin
from webapp_constants import PROFILE_PATH_KEY

# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

def _make_widget(*args, **kwargs):
    widget = mock.MagicMock()
    widget.init_args = args
    widget.init_kwargs = kwargs
    widget.children = []
    widget.append.side_effect = widget.children.append
    return widget


def _fake_gtk():
    fake = mock.MagicMock()
    for name in ('Window', 'Box', 'Label', 'ProgressBar', 'Button', 'Dialog', 'FileDialog', 'FileFilter'):
        getattr(fake, name).side_effect = _make_widget
    fake.FileChooserNative.new.side_effect = _make_widget
    return fake


class IdleQueue:
    """Stands in for GLib.idle_add: records callbacks and runs them on drain()."""

    def __init__(self):
        self.calls = []

    def add(self, func, *args):
        self.calls.append((func, args))
        return len(self.calls)

    def drain(self, limit=500):
        ran = 0
        while self.calls:
            func, args = self.calls.pop(0)
            func(*args)
            ran += 1
            if ran > limit:
                raise AssertionError('idle queue did not settle')
        return ran


class InlineThread:
    def __init__(self, target=None, args=(), daemon=None):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        self.target(*self.args)


class FakeDatabase:
    instances: ClassVar[list] = []

    def __init__(self, path=None, options=None):
        self.path = path
        self.options = options or {}
        self.added = []
        self.closed = False
        self.close_error = None
        FakeDatabase.instances.append(self)

    def get_options_for_entry(self, entry_id):
        return [(index, entry_id, key, value) for index, (key, value) in enumerate(self.options.get(entry_id, {}).items(), start=1)]

    def add_options(self, entry_id, updates):
        self.added.append((entry_id, dict(updates)))

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class FakeStore:
    def __init__(self, entries):
        self.entries = list(entries)

    def get_n_items(self):
        return len(self.entries)

    def get_item(self, index):
        return self.entries[index]


class Harness(MainWindowProfileImportMixin):
    def __init__(self):
        self.notifications = []
        self.choices = []
        self.reloads = 0
        self.loaded_from_db = 0
        self.empty_state_updates = 0
        self._profile_resync_running = False
        self._profile_resync_cancel_event = None
        self._profile_resync_dialog = None
        self._profile_resync_progress_label = None
        self._profile_resync_progress_bar = None
        self._import_progress_dialog = None
        self._import_progress_label = None
        self._import_progress_bar = None
        self._import_cancel_requested = False
        self.refresh_button = mock.MagicMock()
        self.add_button = mock.MagicMock()
        self.custom_filter = mock.MagicMock()
        self.detail_pages = {}
        self.entries_store = FakeStore([])
        self.options_by_id = {}
        self.collisions = set()
        self.collision_shown = []
        self.db = mock.MagicMock()

    # MainWindow API the mixin calls back into
    def show_overlay_notification(self, message, timeout_ms=None):
        self.notifications.append((message, timeout_ms))

    def _present_choice_dialog(self, message, on_result, destructive=False):
        self.choices.append((message, on_result, destructive))

    def _get_options_dict(self, entry_id):
        return dict(self.options_by_id.get(entry_id, {}))

    def _browser_family_for_options(self, options):
        return options.get('family', '')

    def load_entries_from_db(self):
        self.loaded_from_db += 1

    def update_empty_state(self):
        self.empty_state_updates += 1

    def _reload_entries(self):
        self.reloads += 1

    def _find_import_collision(self, payload):
        return 'existing' if payload.get('title') in self.collisions else None

    def _show_import_collision(self, entry, payload):
        self.collision_shown.append((entry, payload))


class PatchedModuleTestCase(unittest.TestCase):
    """Replaces GLib (idle queue only) and threading in the module under test."""

    def setUp(self):
        self.idle = IdleQueue()
        fake_glib = SimpleNamespace(Error=GLib.Error, idle_add=self.idle.add)
        patcher = mock.patch.object(profile_import, 'GLib', fake_glib)
        patcher.start()
        self.addCleanup(patcher.stop)
        fake_threading = SimpleNamespace(Thread=InlineThread, Event=threading.Event)
        patcher = mock.patch.object(profile_import, 'threading', fake_threading)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.window = Harness()


# --------------------------------------------------------------------------
# Profile resync
# --------------------------------------------------------------------------

class RefreshClickedTests(PatchedModuleTestCase):
    def test_ignored_while_a_resync_is_running(self):
        self.window._profile_resync_running = True
        self.window.on_refresh_clicked(None)
        self.assertEqual(self.window.choices, [])

    def test_asks_for_confirmation_and_starts_only_when_accepted(self):
        with mock.patch.object(Harness, '_start_profile_resync') as start:
            self.window.on_refresh_clicked(None)
            self.assertEqual(len(self.window.choices), 1)
            message, callback, destructive = self.window.choices[0]
            self.assertEqual(message, t('profile_resync_confirm_body'))
            self.assertFalse(destructive)
            callback(False)
            start.assert_not_called()
            callback(True)
            start.assert_called_once_with()


class ResyncDialogLifecycleTests(PatchedModuleTestCase):
    def test_destroy_without_dialog_clears_widget_references(self):
        self.window._profile_resync_progress_label = object()
        self.window._profile_resync_progress_bar = object()
        self.window._destroy_profile_resync_dialog()
        self.assertIsNone(self.window._profile_resync_progress_label)
        self.assertIsNone(self.window._profile_resync_progress_bar)

    def test_destroy_closes_the_dialog(self):
        dialog = mock.MagicMock()
        self.window._profile_resync_dialog = dialog
        self.window._destroy_profile_resync_dialog()
        dialog.close.assert_called_once_with()
        dialog.destroy.assert_not_called()
        self.assertIsNone(self.window._profile_resync_dialog)

    def test_destroy_falls_back_to_destroy_when_close_fails(self):
        dialog = mock.MagicMock()
        dialog.close.side_effect = AttributeError
        self.window._profile_resync_dialog = dialog
        self.window._destroy_profile_resync_dialog()
        dialog.destroy.assert_called_once_with()

    def test_destroy_swallows_a_failing_destroy(self):
        dialog = mock.MagicMock()
        dialog.close.side_effect = GLib.Error('gone')
        dialog.destroy.side_effect = AttributeError
        self.window._profile_resync_dialog = dialog
        self.window._destroy_profile_resync_dialog()
        self.assertIsNone(self.window._profile_resync_dialog)

    def test_cancel_sets_the_event_and_keeps_the_window_open(self):
        event = threading.Event()
        self.window._profile_resync_cancel_event = event
        self.assertFalse(self.window._cancel_profile_resync('close-request'))
        self.assertTrue(event.is_set())

    def test_cancel_without_event_is_harmless(self):
        self.assertFalse(self.window._cancel_profile_resync())

    def test_progress_dialog_wires_cancel_and_stores_widgets(self):
        old_dialog = mock.MagicMock()
        self.window._profile_resync_dialog = old_dialog
        with mock.patch.object(profile_import, 'Gtk', _fake_gtk()):
            self.window._show_profile_resync_progress_dialog('3')
        old_dialog.close.assert_called_once_with()
        dialog = self.window._profile_resync_dialog
        self.assertEqual(dialog.init_kwargs['title'], t('profile_resync_title'))
        self.assertIs(dialog.init_kwargs['transient_for'], self.window)
        dialog.connect.assert_called_once_with('close-request', self.window._cancel_profile_resync)
        dialog.present.assert_called_once_with()
        self.assertEqual(self.window._profile_resync_total, 3)
        self.assertEqual(self.window._profile_resync_progress_label.init_kwargs['label'], t('profile_resync_progress_preparing'))
        self.window._profile_resync_progress_bar.set_fraction.assert_called_with(0.0)
        box = dialog.set_child.call_args[0][0]
        buttons = box.children[3]
        cancel_button = buttons.children[0]
        cancel_button.connect.assert_called_once_with('clicked', self.window._cancel_profile_resync)

    def test_progress_dialog_treats_missing_total_as_zero(self):
        with mock.patch.object(profile_import, 'Gtk', _fake_gtk()):
            self.window._show_profile_resync_progress_dialog(None)
        self.assertEqual(self.window._profile_resync_total, 0)


class ResyncProgressUpdateTests(PatchedModuleTestCase):
    def test_no_widgets_means_no_update(self):
        self.assertFalse(self.window._update_profile_resync_progress(1, 2, 'x'))

    def test_current_entry_text_and_fraction(self):
        label, bar = mock.MagicMock(), mock.MagicMock()
        self.window._profile_resync_progress_label = label
        self.window._profile_resync_progress_bar = bar
        self.assertFalse(self.window._update_profile_resync_progress(1, 4, 'Mail'))
        label.set_text.assert_called_once_with(t('profile_resync_progress_current', current=1, total=4, title='Mail'))
        bar.set_fraction.assert_called_once_with(0.25)

    def test_values_are_clamped(self):
        label, bar = mock.MagicMock(), mock.MagicMock()
        self.window._profile_resync_progress_label = label
        self.window._profile_resync_progress_bar = bar
        self.window._update_profile_resync_progress(9, 0)
        label.set_text.assert_called_once_with(t('profile_resync_progress_completed', current=1, total=1))
        bar.set_fraction.assert_called_once_with(1.0)
        self.window._update_profile_resync_progress(-5, 2)
        bar.set_fraction.assert_called_with(0.0)


class CollectResyncCandidatesTests(PatchedModuleTestCase):
    def test_only_entries_with_a_supported_profile_qualify(self):
        entries = [
            SimpleNamespace(id=1, title='Firefox app'),
            SimpleNamespace(id=2, title=None),
            SimpleNamespace(id=3, title='No profile'),
            SimpleNamespace(id=4, title='Unknown engine'),
        ]
        self.window.entries_store = FakeStore(entries)
        self.window.options_by_id = {
            1: {PROFILE_PATH_KEY: ' /p/one ', 'family': 'firefox'},
            2: {PROFILE_PATH_KEY: '/p/two', 'family': 'chromium'},
            3: {PROFILE_PATH_KEY: '  ', 'family': 'chrome'},
            4: {PROFILE_PATH_KEY: '/p/four', 'family': 'epiphany'},
        }
        self.assertEqual(
            self.window._collect_profile_resync_candidates(),
            [(1, 'Firefox app', 'firefox', '/p/one'), (2, '', 'chromium', '/p/two')],
        )


class StartProfileResyncTests(PatchedModuleTestCase):
    def setUp(self):
        super().setUp()
        FakeDatabase.instances = []
        patcher = mock.patch.object(profile_import, 'Database', FakeDatabase)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dialog_totals = []
        self.window._show_profile_resync_progress_dialog = self.dialog_totals.append
        self.window.entries_store = FakeStore([SimpleNamespace(id=1, title='One'), SimpleNamespace(id=2, title='Two')])
        self.window.options_by_id = {
            1: {PROFILE_PATH_KEY: '/p/one', 'family': 'firefox'},
            2: {PROFILE_PATH_KEY: '/p/two', 'family': 'chromium'},
        }

    def _run(self, read_side_effect):
        with mock.patch.object(profile_import, 'read_profile_settings', side_effect=read_side_effect) as reader:
            self.window._start_profile_resync()
            self.idle.drain()
        return reader

    def test_does_nothing_while_running(self):
        self.window._profile_resync_running = True
        self.window._start_profile_resync()
        self.assertEqual(self.dialog_totals, [])
        self.assertEqual(FakeDatabase.instances, [])

    def test_reports_when_no_entry_has_a_profile(self):
        self.window.entries_store = FakeStore([])
        self.window._start_profile_resync()
        self.assertEqual(self.window.notifications, [(t('profile_resync_none'), 2600)])
        self.assertFalse(self.window._profile_resync_running)

    def test_successful_resync_stores_managed_options_but_not_modes(self):
        page = mock.MagicMock()
        broken_page = mock.MagicMock()
        broken_page.reload_from_db.side_effect = AttributeError
        self.window.detail_pages = {1: page, 2: broken_page}
        states = {'/p/one': {'Adblock': '1', 'Kiosk': '1', 'Unknown Key': 'x'}, '/p/two': {}}
        reader = self._run(lambda path, family: states[path])

        self.assertEqual(reader.call_args_list, [mock.call('/p/one', 'firefox'), mock.call('/p/two', 'chromium')])
        self.assertEqual(self.dialog_totals, [2])
        db = FakeDatabase.instances[0]
        self.assertTrue(db.closed)
        # Entry 2 had nothing managed to store, so only entry 1 was written.
        self.assertEqual(len(db.added), 1)
        entry_id, updates = db.added[0]
        self.assertEqual(entry_id, 1)
        self.assertEqual(updates['Adblock'], '1')
        self.assertNotIn('Kiosk', updates)
        self.assertIn(browser_state_key('firefox'), updates)
        self.assertEqual(json.loads(updates[browser_state_key('firefox')]).get('Adblock'), '1')

        self.assertFalse(self.window._profile_resync_running)
        self.assertIsNone(self.window._profile_resync_cancel_event)
        self.window.refresh_button.set_sensitive.assert_has_calls([mock.call(False), mock.call(True)])
        self.assertEqual(self.window.loaded_from_db, 1)
        self.assertEqual(self.window.empty_state_updates, 1)
        self.window.custom_filter.changed.assert_called_once_with(Gtk.FilterChange.DIFFERENT)
        page.reload_from_db.assert_called_once_with()
        self.assertEqual(self.window.notifications, [(t('profile_resync_completed_success', completed=2, total=2), 2800)])

    def test_existing_options_are_merged_into_the_browser_state(self):
        def make_db(path):
            return FakeDatabase(path, options={1: {'Clear Cookies On Exit': '1'}})

        with mock.patch.object(profile_import, 'Database', side_effect=make_db):
            self._run(lambda path, family: {'Adblock': '1'})
        state = json.loads(FakeDatabase.instances[0].added[0][1][browser_state_key('firefox')])
        self.assertEqual(state.get('Clear Cookies On Exit'), '1')
        self.assertEqual(state.get('Adblock'), '1')

    def test_failures_are_counted_and_reported(self):
        def reader(path, family):
            if path == '/p/one':
                raise OSError('unreadable')
            return {'Adblock': '0'}

        self._run(reader)
        self.assertEqual(
            self.window.notifications,
            [(t('profile_resync_completed_with_failures', completed=2, total=2, failures=1), 3600)],
        )

    def test_cancel_during_an_entry_stops_after_it(self):
        def reader(path, family):
            self.window._profile_resync_cancel_event.set()
            return {}

        reader_mock = self._run(reader)
        self.assertEqual(reader_mock.call_count, 1)
        self.assertEqual(self.window.notifications, [(t('profile_resync_cancelled', completed=1, total=2), 3200)])

    def test_cancel_before_the_first_entry_processes_nothing(self):
        original_event = threading.Event

        class PreSetEvent:
            def __new__(cls):
                event = original_event()
                event.set()
                return event

        with mock.patch.object(profile_import.threading, 'Event', PreSetEvent):
            reader_mock = self._run(lambda path, family: {})
        reader_mock.assert_not_called()
        self.assertEqual(self.window.notifications, [(t('profile_resync_cancelled', completed=0, total=2), 3200)])

    def test_failing_database_close_does_not_abort_the_run(self):
        def make_db(path):
            db = FakeDatabase(path)
            db.close_error = OSError('busy')
            return db

        with mock.patch.object(profile_import, 'Database', side_effect=make_db):
            self._run(lambda path, family: {})
        self.assertFalse(self.window._profile_resync_running)
        self.assertEqual(self.window.notifications, [(t('profile_resync_completed_success', completed=2, total=2), 2800)])


# --------------------------------------------------------------------------
# .wapp import
# --------------------------------------------------------------------------

class OpenImportDialogTests(PatchedModuleTestCase):
    def _capturing_gtk(self):
        fake_gtk = _fake_gtk()
        self.captured = {}

        def capture(kind):
            def make(*args, **kwargs):
                widget = _make_widget(*args, **kwargs)
                self.captured[kind] = widget
                return widget
            return make

        fake_gtk.FileDialog.side_effect = capture('file_dialog')
        fake_gtk.FileChooserNative.new.side_effect = capture('native')
        return fake_gtk

    def test_file_dialog_path_installs_the_wapp_filter(self):
        fake_gtk = self._capturing_gtk()
        fake_gio = mock.MagicMock()
        store = mock.MagicMock()
        store.get_n_items.return_value = 1
        fake_gio.ListStore.new.return_value = store
        with mock.patch.object(profile_import, 'Gtk', fake_gtk), mock.patch.object(profile_import, 'Gio', fake_gio):
            self.window._open_import_wapp_dialog()
        dialog = self.captured['file_dialog']
        self.assertEqual(dialog.init_kwargs['title'], t('import_webapp_button'))
        filt = store.append.call_args[0][0]
        filt.set_name.assert_called_once_with(t('wapp_filter_name'))
        filt.add_pattern.assert_called_once_with('*.wapp')
        dialog.set_filters.assert_called_once_with(store)
        dialog.set_default_filter.assert_called_once_with(filt)
        args = dialog.open.call_args[0]
        self.assertIs(args[0], self.window)
        self.assertIsNone(args[1])

        handle_open = args[2]
        responses = []
        self.window._on_import_wapp_dialog_response = lambda *a: responses.append(a)
        chosen = object()
        finisher = mock.MagicMock()
        finisher.open_finish.return_value = chosen
        handle_open(finisher, 'result')
        finisher.open_finish.side_effect = GLib.Error('dismissed')
        handle_open(finisher, 'result')
        self.assertEqual(responses, [(chosen,), (None, Gtk.ResponseType.CANCEL)])

    def test_empty_filter_store_is_not_installed(self):
        fake_gtk = self._capturing_gtk()
        fake_gio = mock.MagicMock()
        fake_gio.ListStore.new.return_value.get_n_items.return_value = 0
        with mock.patch.object(profile_import, 'Gtk', fake_gtk), mock.patch.object(profile_import, 'Gio', fake_gio):
            self.window._open_import_wapp_dialog()
        self.captured['file_dialog'].set_filters.assert_not_called()

    def test_native_chooser_fallback_when_file_dialog_is_missing(self):
        fake_gtk = self._capturing_gtk()
        del fake_gtk.FileDialog
        with mock.patch.object(profile_import, 'Gtk', fake_gtk):
            self.window._open_import_wapp_dialog()
        native = self.captured['native']
        self.assertEqual(native.init_args[0], t('import_webapp_button'))
        self.assertIs(native.init_args[1], self.window)
        self.assertEqual(native.init_args[2], fake_gtk.FileChooserAction.OPEN)
        filt = native.add_filter.call_args[0][0]
        filt.set_name.assert_called_once_with(t('wapp_filter_name'))
        filt.add_pattern.assert_called_once_with('*.wapp')
        native.set_filter.assert_called_once_with(filt)
        native.connect.assert_called_once_with('response', self.window._on_import_wapp_dialog_response)
        native.show.assert_called_once_with()


class CopyGFileToTempPathTests(PatchedModuleTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        real_mkstemp = tempfile.mkstemp
        patcher = mock.patch('tempfile.mkstemp', side_effect=lambda suffix='': real_mkstemp(suffix=suffix, dir=self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_none_returns_none(self):
        self.assertIsNone(self.window._copy_gfile_to_temp_path(None))

    def test_local_file_is_used_in_place(self):
        file_obj = mock.MagicMock()
        file_obj.get_path.return_value = '/some/where.wapp'
        self.assertEqual(self.window._copy_gfile_to_temp_path(file_obj), Path('/some/where.wapp'))
        file_obj.read.assert_not_called()

    def test_remote_file_is_streamed_into_a_temp_copy(self):
        stream = mock.MagicMock()
        stream.read_bytes.side_effect = [b'ab', b'cd', b'']
        stream.close.side_effect = TypeError
        file_obj = mock.MagicMock()
        file_obj.get_path.return_value = None
        file_obj.read.return_value = stream
        result = self.window._copy_gfile_to_temp_path(file_obj, '.wapp')
        self.assertEqual(result.suffix, '.wapp')
        self.assertEqual(result.parent, Path(self.tmp.name))
        self.assertEqual(result.read_bytes(), b'abcd')

    def test_failed_open_returns_none_and_logs_the_uri(self):
        file_obj = mock.MagicMock()
        file_obj.get_path.return_value = ''
        file_obj.read.side_effect = GLib.Error('no such file')
        file_obj.get_uri.return_value = 'sftp://host/x.wapp'
        with self.assertLogs(profile_import.LOG, level='WARNING') as logs:
            self.assertIsNone(self.window._copy_gfile_to_temp_path(file_obj))
        self.assertIn('sftp://host/x.wapp', logs.output[0])

    def test_failed_copy_removes_the_partial_temp_file(self):
        stream = mock.MagicMock()
        stream.read_bytes.side_effect = [b'ab', OSError('broken pipe')]
        file_obj = mock.MagicMock()
        file_obj.get_path.return_value = None
        file_obj.read.return_value = stream
        file_obj.get_uri.side_effect = AttributeError
        self.assertIsNone(self.window._copy_gfile_to_temp_path(file_obj))
        stream.close.assert_called_once_with(None)
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_cleanup_failure_is_tolerated(self):
        stream = mock.MagicMock()
        stream.read_bytes.side_effect = OSError('broken pipe')
        file_obj = mock.MagicMock()
        file_obj.get_path.return_value = None
        file_obj.read.return_value = stream
        with mock.patch.object(Path, 'unlink', side_effect=OSError('read-only')) as unlink:
            self.assertIsNone(self.window._copy_gfile_to_temp_path(file_obj))
        unlink.assert_called_once_with(missing_ok=True)


class ImportProgressDialogTests(PatchedModuleTestCase):
    def test_destroy_without_dialog(self):
        self.window._import_progress_label = object()
        self.window._destroy_import_progress_dialog()
        self.assertIsNone(self.window._import_progress_label)
        self.assertIsNone(self.window._import_progress_bar)

    def test_destroy_closes_or_falls_back(self):
        dialog = mock.MagicMock()
        self.window._import_progress_dialog = dialog
        self.window._destroy_import_progress_dialog()
        dialog.close.assert_called_once_with()

        dialog = mock.MagicMock()
        dialog.close.side_effect = AttributeError
        dialog.destroy.side_effect = AttributeError
        self.window._import_progress_dialog = dialog
        self.window._destroy_import_progress_dialog()
        dialog.destroy.assert_called_once_with()
        self.assertIsNone(self.window._import_progress_dialog)

    def test_cancel_sets_the_flag(self):
        self.assertFalse(self.window._cancel_import_progress('close-request'))
        self.assertTrue(self.window._import_cancel_requested)

    def test_show_builds_dialog_with_default_texts(self):
        fake_gtk = _fake_gtk()
        with mock.patch.object(profile_import, 'Gtk', fake_gtk):
            self.window._show_import_progress_dialog(-4)
        dialog = self.window._import_progress_dialog
        dialog.set_title.assert_called_once_with(t('import_progress_title'))
        dialog.connect.assert_called_once_with('close-request', self.window._cancel_import_progress)
        dialog.present.assert_called_once_with()
        self.assertEqual(self.window._import_progress_label.init_kwargs['label'], t('import_progress_preparing'))
        self.assertEqual(self.window._import_total, 0)
        box = dialog.get_content_area.return_value
        appended = [call.args[0] for call in box.append.call_args_list]
        self.assertEqual(len(appended), 4)
        cancel_button = appended[3].children[0]
        cancel_button.connect.assert_called_once_with('clicked', self.window._cancel_import_progress)

    def test_show_accepts_custom_texts(self):
        with mock.patch.object(profile_import, 'Gtk', _fake_gtk()):
            self.window._show_import_progress_dialog(5, title_text='Export', preparing_text='Packing')
        self.window._import_progress_dialog.set_title.assert_called_once_with('Export')
        self.assertEqual(self.window._import_progress_label.init_kwargs['label'], 'Packing')
        self.assertEqual(self.window._import_total, 5)

    def test_update_progress(self):
        self.assertFalse(self.window._update_import_progress(1, 2))
        label, bar = mock.MagicMock(), mock.MagicMock()
        self.window._import_progress_label = label
        self.window._import_progress_bar = bar
        self.window._update_import_progress(1, 2, 'Mail')
        label.set_text.assert_called_with(t('import_progress_current', current=1, total=2, title='Mail'))
        bar.set_fraction.assert_called_with(0.5)
        self.window._update_import_progress(None, None)
        label.set_text.assert_called_with(t('import_progress_completed', current=0, total=1))
        bar.set_fraction.assert_called_with(0.0)


class FinishImportPayloadsTests(PatchedModuleTestCase):
    def _finish(self, imported, duplicates, cancelled=False):
        self.window._import_cancel_requested = True
        self.window._import_progress_dialog = mock.MagicMock()
        self.assertFalse(self.window._finish_import_payloads(imported, duplicates, cancelled))
        self.assertFalse(self.window._import_cancel_requested)
        self.assertIsNone(self.window._import_progress_dialog)
        self.assertEqual(self.window.reloads, 1)
        return self.window.notifications

    def test_cancelled(self):
        self.assertEqual(self._finish(2, 1, True), [(t('import_bundle_cancelled', imported=2, duplicates=1), 3400)])

    def test_imported_with_duplicates(self):
        self.assertEqual(self._finish(2, 1), [(t('import_bundle_result_with_duplicates', imported=2, duplicates=1), 3200)])

    def test_several_imported(self):
        self.assertEqual(self._finish(3, 0), [(t('import_bundle_result', imported=3), 2800)])

    def test_single_imported(self):
        self.assertEqual(self._finish(1, 0), [(t('import_webapp_success'), 2400)])

    def test_only_duplicates(self):
        self.assertEqual(self._finish(0, 2), [(t('import_bundle_none_with_duplicates', duplicates=2), 3200)])

    def test_nothing_at_all_is_silent(self):
        self.assertEqual(self._finish(0, 0), [])


class StartImportPayloadsTests(PatchedModuleTestCase):
    def setUp(self):
        super().setUp()
        self.created = []
        self.progress_totals = []
        self.finished = []
        self.window._show_import_progress_dialog = self.progress_totals.append
        self.window._finish_import_payloads = lambda *a: self.finished.append(a)
        self.outcomes = {}

        def create(payload, reload_after_success=True, on_complete=None):
            self.created.append((payload['title'], reload_after_success, on_complete is not None))
            if on_complete is not None:
                success = self.outcomes.get(payload['title'], True)
                self.idle.add(on_complete, success, 7 if success else None)
            return True

        self.window._create_entry_from_wapp_payload = create

    def test_empty_input_does_nothing(self):
        self.window._start_import_payloads(None)
        self.window._start_import_payloads([])
        self.assertEqual(self.created, [])
        self.assertEqual(self.window.notifications, [])

    def test_only_duplicates_are_reported(self):
        self.window.collisions = {'A', 'B'}
        self.window._start_import_payloads([{'title': 'A'}, {'title': 'B'}])
        self.assertEqual(self.created, [])
        self.assertEqual(self.window.notifications, [(t('import_bundle_none_with_duplicates', duplicates=2), 3200)])

    def test_a_single_payload_is_created_directly_without_progress(self):
        self.window.collisions = {'Dup'}
        self.window._start_import_payloads([{'title': 'Dup'}, {'title': 'New'}])
        self.assertEqual(self.created, [('New', True, False)])
        self.assertEqual(self.progress_totals, [])

    def test_bundle_is_imported_one_after_another(self):
        self.window.collisions = {'Dup'}
        self.outcomes = {'B': False}
        updates = []
        self.window._update_import_progress = lambda *a: updates.append(a)
        payloads = [{'title': 'A'}, {'title': 'Dup'}, {'title': ''}, {'title': 'B', 'description': 'ignored'}]
        payloads[2]['description'] = ' Described '
        self.window._start_import_payloads(payloads)
        self.idle.drain()
        self.assertEqual([c[0] for c in self.created], ['A', '', 'B'])
        self.assertTrue(all(c[2] for c in self.created))
        self.assertEqual(self.progress_totals, [3])
        self.assertIn((1, 3, 'A'), updates)
        self.assertIn((2, 3, 'Described'), updates)
        self.assertIn((3, 3, 'B'), updates)
        self.assertEqual(self.finished, [(2, 1, False)])

    def test_cancel_stops_before_the_next_payload(self):
        payloads = [{'title': 'A'}, {'title': 'B'}, {'title': 'C'}]
        original_create = self.window._create_entry_from_wapp_payload

        def create_and_cancel(payload, **kwargs):
            self.window._import_cancel_requested = True
            return original_create(payload, **kwargs)

        self.window._create_entry_from_wapp_payload = create_and_cancel
        self.window._start_import_payloads(payloads)
        self.idle.drain()
        self.assertEqual([c[0] for c in self.created], ['A'])
        self.assertEqual(self.finished, [(1, 0, True)])


class ImportDialogResponseTests(PatchedModuleTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.started = []
        self.window._start_import_payloads = self.started.append

    def _write(self, name, payload):
        path = Path(self.tmp.name) / name
        path.write_text(json.dumps(payload), encoding='utf-8')
        return path

    def test_dialog_cancel_only_destroys_the_dialog(self):
        dialog = mock.MagicMock()
        self.window._on_import_wapp_dialog_response(dialog, Gtk.ResponseType.CANCEL)
        dialog.destroy.assert_called_once_with()
        dialog.get_file.assert_not_called()

        dialog.destroy.side_effect = AttributeError
        self.window._on_import_wapp_dialog_response(dialog, Gtk.ResponseType.DELETE_EVENT)
        self.assertEqual(self.started, [])

    def test_accepted_native_dialog_imports_its_file(self):
        path = self._write('one.wapp', {'title': 'Mail', 'options': {'Address': 'https://mail.example'}})
        dialog = mock.MagicMock()
        dialog.get_file.return_value = Gio.File.new_for_path(str(path))
        dialog.destroy.side_effect = GLib.Error('already gone')
        self.window._on_import_wapp_dialog_response(dialog, Gtk.ResponseType.ACCEPT)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.started[0][0]['title'], 'Mail')
        # A local file is read in place and must never be deleted.
        self.assertTrue(path.exists())

    def test_gio_file_bundle_is_imported(self):
        path = self._write('bundle.wapp', {'format': 'webapp-export-bundle-v1', 'entries': [{'title': 'A'}, {'title': 'B'}]})
        self.window._on_import_wapp_dialog_response(Gio.File.new_for_path(str(path)))
        self.assertEqual([p['title'] for p in self.started[0]], ['A', 'B'])
        self.assertEqual(self.window.choices, [])

    def test_inline_javascript_needs_confirmation(self):
        path = self._write('js.wapp', {'title': 'JS', 'options': {'Inline Custom JavaScript': 'alert(1)'}})
        self.window._on_import_wapp_dialog_response(Gio.File.new_for_path(str(path)))
        self.assertEqual(self.started, [])
        message, callback, destructive = self.window.choices[0]
        self.assertEqual(message, t('import_javascript_warning'))
        self.assertFalse(destructive)
        callback(False)
        self.assertEqual(self.started, [])
        callback(True)
        self.assertEqual(self.started[0][0]['title'], 'JS')

    def test_invalid_file_is_logged_not_raised(self):
        path = Path(self.tmp.name) / 'broken.wapp'
        path.write_text('{not json', encoding='utf-8')
        with self.assertLogs(profile_import.LOG, level='WARNING') as logs:
            self.window._on_import_wapp_dialog_response(Gio.File.new_for_path(str(path)))
        self.assertIn(str(path), logs.output[0])
        self.assertEqual(self.started, [])

    def test_uncopyable_file_is_ignored(self):
        self.window._copy_gfile_to_temp_path = lambda *_a: None
        self.window._on_import_wapp_dialog_response(Gio.File.new_for_path('/nonexistent/x.wapp'))
        self.assertEqual(self.started, [])

    def test_temporary_copy_is_removed_afterwards(self):
        temp_copy = self._write('copy.wapp', {'title': 'Remote'})
        self.window._copy_gfile_to_temp_path = lambda *_a: temp_copy
        remote = mock.MagicMock(spec=Gio.File)
        remote.get_path.return_value = None
        with mock.patch.object(profile_import, 'Gio', SimpleNamespace(File=type(remote))):
            self.window._on_import_wapp_dialog_response(remote)
        self.assertEqual(self.started[0][0]['title'], 'Remote')
        self.assertFalse(temp_copy.exists())

    def test_failed_import_of_a_temporary_copy_logs_the_copy_path(self):
        temp_copy = Path(self.tmp.name) / 'copy.wapp'
        temp_copy.write_text('[]', encoding='utf-8')
        self.window._copy_gfile_to_temp_path = lambda *_a: temp_copy
        remote = mock.MagicMock(spec=Gio.File)
        remote.get_path.return_value = None
        with mock.patch.object(profile_import, 'Gio', SimpleNamespace(File=type(remote))), \
                self.assertLogs(profile_import.LOG, level='WARNING') as logs:
            self.window._on_import_wapp_dialog_response(remote)
        self.assertIn(str(temp_copy), logs.output[0])
        self.assertFalse(temp_copy.exists())


class FakeDetailPage:
    instances: ClassVar[list] = []
    apply_result = True
    apply_error = None
    unparent_error = None

    def __init__(self, entry, db, **callbacks):
        self.entry = entry
        self.db = db
        self.callbacks = callbacks
        self.unparented = False
        FakeDetailPage.instances.append(self)

    def _apply_wapp_payload(self, payload):
        self.payload = payload
        if FakeDetailPage.apply_error is not None:
            raise FakeDetailPage.apply_error
        return FakeDetailPage.apply_result

    def unparent(self):
        self.unparented = True
        if FakeDetailPage.unparent_error is not None:
            raise FakeDetailPage.unparent_error


class CreateEntryFromPayloadTests(PatchedModuleTestCase):
    def setUp(self):
        super().setUp()
        FakeDetailPage.instances = []
        FakeDetailPage.apply_result = True
        FakeDetailPage.apply_error = None
        FakeDetailPage.unparent_error = None
        patcher = mock.patch.object(profile_import, 'DetailPage', FakeDetailPage)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.window.db.add_entry.return_value = 42
        self.completions = []

    def _complete(self, success, entry_id):
        self.completions.append((success, entry_id))

    def test_collision_shows_the_existing_entry(self):
        self.window.collisions = {'Mail'}
        payload = {'title': 'Mail'}
        self.assertFalse(self.window._create_entry_from_wapp_payload(payload, on_complete=self._complete))
        self.idle.drain()
        self.assertEqual(self.window.collision_shown, [('existing', payload)])
        self.assertEqual(self.completions, [(False, None)])
        self.window.db.add_entry.assert_not_called()

    def test_collision_without_callback(self):
        self.window.collisions = {'Mail'}
        self.assertFalse(self.window._create_entry_from_wapp_payload({'title': 'Mail'}))
        self.assertEqual(self.idle.calls, [])

    def test_database_refusal_reports_failure(self):
        self.window.db.add_entry.return_value = None
        self.assertFalse(self.window._create_entry_from_wapp_payload({'title': 'X'}, on_complete=self._complete))
        self.idle.drain()
        self.assertEqual(self.completions, [(False, None)])
        self.assertFalse(self.window._creating_entry)
        self.window.add_button.set_sensitive.assert_has_calls([mock.call(False), mock.call(True)])

    def test_database_refusal_without_callback(self):
        self.window.db.add_entry.return_value = None
        self.assertFalse(self.window._create_entry_from_wapp_payload({'title': 'X'}))
        self.assertEqual(self.idle.calls, [])

    def test_successful_import_applies_payload_and_reloads(self):
        payload = {'title': 'Mail'}
        self.assertTrue(self.window._create_entry_from_wapp_payload(payload, on_complete=self._complete))
        self.assertFalse(self.window._creating_entry)
        self.idle.drain()
        page = FakeDetailPage.instances[0]
        self.assertEqual(page.entry.id, 42)
        self.assertIs(page.db, self.window.db)
        self.assertEqual(page.callbacks['on_overlay_notification'], self.window.show_overlay_notification)
        for name in ('on_back', 'on_delete', 'on_title_changed', 'on_visual_changed'):
            self.assertIsNone(page.callbacks[name]('ignored'))
        self.assertIs(page.payload, payload)
        self.assertTrue(page.unparented)
        self.assertEqual(self.window.reloads, 1)
        self.assertEqual(self.completions, [(True, 42)])
        self.window.db.delete_entry.assert_not_called()

    def test_rejected_payload_removes_the_placeholder_entry(self):
        FakeDetailPage.apply_result = False
        self.window.db.delete_entry.side_effect = sqlite3.OperationalError('locked')
        self.window._create_entry_from_wapp_payload({'title': 'X'}, reload_after_success=False, on_complete=self._complete)
        self.idle.drain()
        self.window.db.delete_entry.assert_called_once_with(42)
        self.assertEqual(self.window.reloads, 0)
        self.assertEqual(self.completions, [(False, None)])

    def test_apply_error_is_logged_and_entry_removed(self):
        FakeDetailPage.apply_error = ValueError('bad icon')
        FakeDetailPage.unparent_error = AttributeError
        with self.assertLogs(profile_import.LOG, level='WARNING'):
            self.window._create_entry_from_wapp_payload({'title': 'X'}, on_complete=self._complete)
            self.idle.drain()
        self.window.db.delete_entry.assert_called_once_with(42)
        self.assertEqual(self.completions, [(False, None)])

    def test_construction_error_skips_unparent_and_tolerates_delete_failure(self):
        self.window.db.delete_entry.side_effect = sqlite3.DatabaseError('corrupt')
        with mock.patch.object(profile_import, 'DetailPage', side_effect=OSError('no icon dir')), \
                self.assertLogs(profile_import.LOG, level='WARNING'):
            self.window._create_entry_from_wapp_payload({'title': 'X'})
            self.idle.drain()
        self.window.db.delete_entry.assert_called_once_with(42)
        self.assertEqual(self.window.reloads, 1)

    def test_bulk_helper_delegates_to_start_import(self):
        seen = []
        self.window._start_import_payloads = seen.append
        self.window._create_entries_from_import_payloads(['p'])
        self.assertEqual(seen, [['p']])


if __name__ == '__main__':
    unittest.main()

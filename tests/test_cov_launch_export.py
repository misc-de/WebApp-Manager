"""Coverage tests for MainWindowLaunchExportMixin.

A harness borrows the real mixin methods and hand-implements what they call
back into (options cache, notifications, entries store). No browser or other
subprocess is ever started: Popen, host_argv and the monitor thread are
replaced, and /proc lookups are stubbed where a test needs control over them.
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
    logger = logging.getLogger(f'test.cov_launch_export.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

from mainwindow import launch_export as le  # noqa: E402
from mainwindow import MainWindowLaunchExportMixin  # noqa: E402
from webapp_constants import OPTION_PREVENT_MULTIPLE_STARTS_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY  # noqa: E402


class _Entry:
    def __init__(self, entry_id=1, title='My App', description='d', active=True):
        self.id = entry_id
        self.title = title
        self.description = description
        self.active = active


class _Store:
    def __init__(self, items):
        self._items = list(items)

    def get_n_items(self):
        return len(self._items)

    def get_item(self, index):
        return self._items[index]


class _Harness(MainWindowLaunchExportMixin):
    def __init__(self, options=None, entries=()):
        self.options = dict(options or {})
        self.notifications = []
        self.added_options = []
        self.entries_store = _Store(entries)
        self.selection = mock.Mock()
        self.launched = []

    def _get_options_dict(self, entry_id, force_refresh=False):
        return dict(self.options)

    def _add_options(self, entry_id, updates):
        self.added_options.append((entry_id, dict(updates)))
        self.options.update(updates)

    def show_overlay_notification(self, message, timeout_ms=0):
        self.notifications.append((message, timeout_ms))

    def get_display(self):
        raise AttributeError('no widget')


def _t(key, **kwargs):
    return key if not kwargs else f'{key}:{json.dumps(kwargs, sort_keys=True)}'


class _Base(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(le, 't', side_effect=_t)
        patcher.start()
        self.addCleanup(patcher.stop)


class HostEnvOverrideTests(unittest.TestCase):
    def test_native_run_forwards_nothing(self):
        with mock.patch.object(le, 'running_in_flatpak', return_value=False):
            self.assertEqual(le._host_env_overrides({'MOZ_ENABLE_WAYLAND': '1'}), {})

    def test_flatpak_forwards_only_set_whitelisted_keys(self):
        env = {'MOZ_ENABLE_WAYLAND': '1', 'GDK_BACKEND': '', 'DISPLAY': ':0'}
        with mock.patch.object(le, 'running_in_flatpak', return_value=True):
            self.assertEqual(le._host_env_overrides(env), {'MOZ_ENABLE_WAYLAND': '1'})


class ProfileHelpersTests(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def test_needs_profile_prepare(self):
        self.assertFalse(self.h._launch_needs_profile_prepare({PROFILE_PATH_KEY: '/p'}, {'profile_info': {'browser_family': 'firefox'}}))
        self.assertTrue(self.h._launch_needs_profile_prepare({}, {'profile_info': {'browser_family': ' Chrome '}}))
        self.assertFalse(self.h._launch_needs_profile_prepare(None, {'profile_info': {'browser_family': 'epiphany'}}))
        self.assertFalse(self.h._launch_needs_profile_prepare({}, None))

    def test_process_argv_for_pid_reads_cmdline(self):
        argv = self.h._process_argv_for_pid(os.getpid())
        self.assertTrue(argv)
        self.assertIn('python', argv[0])

    def test_process_argv_for_pid_handles_bad_input(self):
        self.assertEqual(self.h._process_argv_for_pid('notapid'), [])
        with mock.patch.object(le.Path, 'read_bytes', side_effect=OSError('gone')):
            self.assertEqual(self.h._process_argv_for_pid(1), [])
        with mock.patch.object(le.Path, 'read_bytes', return_value=b''):
            self.assertEqual(self.h._process_argv_for_pid(1), [])
        with mock.patch.object(le.Path, 'read_bytes', return_value=b'ff\x00-profile\x00/p\x00'):
            self.assertEqual(self.h._process_argv_for_pid(1), ['ff', '-profile', '/p'])

    def test_profile_path_in_argv(self):
        f = self.h._profile_path_in_argv
        self.assertFalse(f(['x'], ''))
        self.assertFalse(f([], '/p'))
        self.assertTrue(f(['ff', '-profile', '/p'], '/p'))
        self.assertTrue(f(['cr', '--user-data-dir', ' /p '], '/p'))
        self.assertFalse(f(['ff', '-profile'], '/p'))
        self.assertFalse(f(['ff', '--profile', '/other'], '/p'))
        self.assertTrue(f(['cr', '--user-data-dir=/p'], '/p'))
        self.assertTrue(f(['ff', '-profile=/p'], '/p'))
        self.assertFalse(f(['cr', '--user-data-dir=/q'], '/p'))

    def test_system_process_running_for_profile(self):
        h = self.h
        self.assertIsNone(h._system_process_running_for_profile(''))
        with mock.patch.object(le.os, 'listdir', side_effect=OSError):
            self.assertIsNone(h._system_process_running_for_profile('/p'))
        own = str(os.getpid())
        argvs = {'10': ['ff'], '11': ['ff', '-profile', '/p']}
        with mock.patch.object(le.os, 'listdir', return_value=['self', own, '10', '11']), \
                mock.patch.object(h, '_process_argv_for_pid', side_effect=lambda pid: argvs[str(pid)]) as argv_mock:
            found = h._system_process_running_for_profile('/p')
        self.assertEqual(found, {'pid': 11, 'argv': ['ff', '-profile', '/p']})
        # Our own pid is never inspected.
        self.assertNotIn(mock.call(int(own)), argv_mock.call_args_list)
        with mock.patch.object(le.os, 'listdir', return_value=['10']), \
                mock.patch.object(h, '_process_argv_for_pid', return_value=['ff']):
            self.assertIsNone(h._system_process_running_for_profile('/p'))

    def test_system_process_skips_digit_names_that_int_rejects(self):
        # '²' passes str.isdigit() but int() refuses it.
        with mock.patch.object(le.os, 'listdir', return_value=['²']), \
                mock.patch.object(self.h, '_process_argv_for_pid') as argv_mock:
            self.assertIsNone(self.h._system_process_running_for_profile('/p'))
        argv_mock.assert_not_called()


class RunningProcessTrackingTests(unittest.TestCase):
    def test_creates_registry_when_missing(self):
        h = _Harness()
        self.assertIsNone(h._running_launch_process_for_entry(1))
        self.assertEqual(h._running_launch_processes, {})

    def test_entry_without_process_is_dropped(self):
        h = _Harness()
        h._running_launch_processes = {1: {'process': None}}
        self.assertIsNone(h._running_launch_process_for_entry(1))
        self.assertNotIn(1, h._running_launch_processes)

    def test_poll_failure_drops_entry(self):
        h = _Harness()
        proc = mock.Mock()
        proc.poll.side_effect = RuntimeError('boom')
        h._running_launch_processes = {1: {'process': proc}}
        self.assertIsNone(h._running_launch_process_for_entry(1))
        self.assertEqual(h._running_launch_processes, {})

    def test_live_and_finished_processes(self):
        h = _Harness()
        live = mock.Mock()
        live.poll.return_value = None
        done = mock.Mock()
        done.poll.return_value = 0
        h._running_launch_processes = {1: {'process': live}, 2: {'process': done}}
        self.assertIs(h._running_launch_process_for_entry(1)['process'], live)
        self.assertIsNone(h._running_launch_process_for_entry(2))
        self.assertEqual(list(h._running_launch_processes), [1])


class LaunchEnvTests(unittest.TestCase):
    def env_for(self, h, argv, base_env, default_display=None):
        with mock.patch.dict(le.os.environ, base_env, clear=True), \
                mock.patch.object(le.Gdk.Display, 'get_default', return_value=default_display):
            return h._launch_env_for_command(argv)

    def test_existing_display_is_kept(self):
        env = self.env_for(_Harness(), ['firefox'], {'DISPLAY': ':1'})
        self.assertEqual(env, {'DISPLAY': ':1'})

    def test_x11_display_name_from_gdk(self):
        display = mock.Mock()
        display.get_name.return_value = ':0'
        env = self.env_for(_Harness(), ['firefox'], {}, default_display=display)
        self.assertEqual(env, {'DISPLAY': ':0'})

    def test_wayland_display_name_from_window_sets_firefox_flags(self):
        h = _Harness()
        display = mock.Mock()
        display.get_name.return_value = 'wayland-0'
        h.get_display = lambda: display
        env = self.env_for(h, ['/usr/bin/firefox'], {})
        self.assertEqual(env, {'WAYLAND_DISPLAY': 'wayland-0', 'MOZ_ENABLE_WAYLAND': '1', 'GDK_BACKEND': 'wayland'})
        env = self.env_for(h, ['chromium'], {})
        self.assertEqual(env, {'WAYLAND_DISPLAY': 'wayland-0'})

    def test_no_display_anywhere_returns_plain_env(self):
        self.assertEqual(self.env_for(_Harness(), [], {'HOME': '/h'}), {'HOME': '/h'})

    def test_broken_display_objects_are_tolerated(self):
        display = mock.Mock()
        display.get_name.side_effect = TypeError
        self.assertEqual(self.env_for(_Harness(), ['x'], {}, default_display=display), {})
        with mock.patch.dict(le.os.environ, {}, clear=True), \
                mock.patch.object(le.Gdk.Display, 'get_default', side_effect=AttributeError):
            self.assertEqual(_Harness()._launch_env_for_command(['x']), {})


class ExportTests(_Base):
    def test_export_payload_and_filtering(self):
        entries = [_Entry(1, 'A'), _Entry(2, 'B')]
        h = _Harness({'Address': 'https://a'}, entries)
        with mock.patch.object(le, 'exportable_entry', side_effect=lambda entry, options: entry.id == 2):
            self.assertEqual(h._iter_exportable_entries(), [entries[1]])
        with mock.patch.object(le, 'build_wapp_export_payload', return_value={'p': 1}) as build:
            self.assertEqual(h._build_export_payload_for_entry(_Entry(3, None, None, 0)), {'p': 1})
        build.assert_called_once_with(title='', description='', active=False, options_dict={'Address': 'https://a'})
        with mock.patch.object(le, 'build_wapp_export_payload', side_effect=lambda **kw: kw['title']), \
                mock.patch.object(le, 'build_wapp_export_bundle_payload', side_effect=lambda items: {'items': items}):
            self.assertEqual(h._build_export_bundle_payload(entries), {'items': ['A', 'B']})

    def test_safe_export_name(self):
        h = _Harness()
        self.assertEqual(h._safe_export_name(_Entry(1, 'Mail')), 'Mail.wapp')
        with mock.patch.object(le, 'sanitize_desktop_value', return_value=''):
            self.assertEqual(h._safe_export_name(_Entry(7, '')), 'webapp-7.wapp')

    def test_export_all_without_entries(self):
        h = _Harness()
        with mock.patch.object(h, '_iter_exportable_entries', return_value=[]):
            h.on_export_all_single_file_clicked(None)
        self.assertEqual(h.notifications, [('settings_export_none', 2600)])

    def test_export_all_without_file_dialog_support(self):
        h = _Harness()
        fake_gtk = types.SimpleNamespace()
        with mock.patch.object(h, '_iter_exportable_entries', return_value=[_Entry()]), \
                mock.patch.object(le, 'Gtk', fake_gtk):
            h.on_export_all_single_file_clicked(None)
        self.assertEqual(h.notifications, [('settings_export_failed', 3200)])

    def test_export_all_dialog_success_and_cancel(self):
        h = _Harness()
        entries = [_Entry()]
        dialogs = []

        class FakeDialog:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                dialogs.append(self)

            def save(self, parent, cancellable, callback):
                self.callback = callback

        fake_gtk = types.SimpleNamespace(FileDialog=FakeDialog)
        with mock.patch.object(h, '_iter_exportable_entries', return_value=entries), \
                mock.patch.object(le, 'Gtk', fake_gtk), \
                mock.patch.object(h, '_on_export_all_single_file_response') as response:
            h.on_export_all_single_file_clicked(None)
            dialog = dialogs[0]
            self.assertTrue(dialog.kwargs['initial_name'].startswith('webapps_export_'))
            self.assertTrue(dialog.kwargs['initial_name'].endswith('.wapp'))
            file_obj = object()
            finisher = mock.Mock()
            finisher.save_finish.return_value = file_obj
            dialog.callback(finisher, 'res')
            response.assert_called_with(file_obj, None, entries)
            finisher.save_finish.side_effect = le.GLib.Error('cancelled')
            dialog.callback(finisher, 'res')
            response.assert_called_with(None, None, entries)

    def test_export_response_variants(self):
        h = _Harness()
        h._on_export_all_single_file_response(None, None, [])
        self.assertEqual(h.notifications, [])
        no_path = mock.Mock()
        no_path.get_path.return_value = None
        h._on_export_all_single_file_response(no_path, None, [])
        self.assertEqual(h.notifications, [('settings_export_path_error', 2600)])

    def test_export_response_writes_bundle_with_wapp_suffix(self):
        h = _Harness()
        with tempfile.TemporaryDirectory() as tmp:
            file_obj = mock.Mock()
            file_obj.get_path.return_value = str(Path(tmp) / 'out.json')
            with mock.patch.object(h, '_build_export_bundle_payload', return_value={'bundle': [1]}):
                h._on_export_all_single_file_response(file_obj, None, [_Entry(), _Entry(2)])
            target = Path(tmp) / 'out.wapp'
            self.assertEqual(json.loads(target.read_text()), {'bundle': [1]})
            self.assertFalse((Path(tmp) / 'out.json').exists())
        self.assertEqual(h.notifications, [('settings_export_success:{"count": 2}', 2600)])

    def test_export_response_reports_write_error(self):
        h = _Harness()
        file_obj = mock.Mock()
        file_obj.get_path.return_value = '/nonexistent-dir-for-test/x.wapp'
        with mock.patch.object(h, '_build_export_bundle_payload', return_value={}):
            h._on_export_all_single_file_response(file_obj, None, [])
        self.assertEqual(h.notifications, [('settings_export_failed', 3200)])


class LaunchCommandTests(unittest.TestCase):
    def setUp(self):
        self.threads = []
        test = self

        class FakeThread:
            def __init__(self, target, daemon):
                self.target = target
                self.daemon = daemon
                test.threads.append(self)

            def start(self):
                self.started = True

        patches = [
            mock.patch.object(le.threading, 'Thread', FakeThread),
            mock.patch.object(le.time, 'sleep'),
            mock.patch.object(le, 'running_in_flatpak', return_value=False),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_empty_argv_is_refused(self):
        self.assertFalse(_Harness()._launch_command_args([]))

    def test_launch_tracks_process_and_monitor_clears_it(self):
        h = _Harness()
        proc = mock.Mock(pid=42)
        proc.poll.return_value = None
        env = {'WAYLAND_DISPLAY': 'wayland-0'}
        with mock.patch.object(h, '_launch_env_for_command', return_value=env), \
                mock.patch.object(le, 'host_argv', side_effect=lambda argv, env_overrides: ['host'] + list(argv)) as host, \
                mock.patch.object(le.subprocess, 'Popen', return_value=proc) as popen:
            self.assertTrue(h._launch_command_args(['firefox', 'https://x'], entry=_Entry(5, 'T')))
        host.assert_called_once_with(['firefox', 'https://x'], env_overrides={})
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ['host', 'firefox', 'https://x'])
        self.assertTrue(kwargs['start_new_session'])
        self.assertEqual(kwargs['env']['MOZ_ENABLE_WAYLAND'], '1')
        self.assertEqual(kwargs['env']['GDK_BACKEND'], 'wayland')
        self.assertEqual(h._running_launch_processes[5], {'process': proc, 'argv': ['host', 'firefox', 'https://x'], 'title': 'T'})
        monitor = self.threads[0]
        self.assertTrue(monitor.daemon and monitor.started)
        # Still running after the grace period: the record stays.
        monitor.target()
        self.assertIn(5, h._running_launch_processes)
        # Exited: the record is cleared.
        proc.poll.return_value = 1
        monitor.target()
        self.assertNotIn(5, h._running_launch_processes)

    def test_monitor_keeps_record_of_a_newer_process(self):
        h = _Harness()
        h._running_launch_processes = {}
        proc = mock.Mock(pid=1)
        proc.poll.return_value = 3
        with mock.patch.object(h, '_launch_env_for_command', return_value={}), \
                mock.patch.object(le, 'host_argv', side_effect=lambda argv, env_overrides: list(argv)), \
                mock.patch.object(le.subprocess, 'Popen', return_value=proc):
            h._launch_command_args(['chromium'], entry=_Entry(9))
        newer = {'process': mock.Mock()}
        h._running_launch_processes[9] = newer
        self.threads[0].target()
        self.assertIs(h._running_launch_processes[9], newer)

    def test_monitor_survives_poll_and_cleanup_errors(self):
        h = _Harness()
        proc = mock.Mock(pid=1)
        with mock.patch.object(h, '_launch_env_for_command', return_value={}), \
                mock.patch.object(le, 'host_argv', side_effect=lambda argv, env_overrides: list(argv)), \
                mock.patch.object(le.subprocess, 'Popen', return_value=proc):
            h._launch_command_args(['chromium'], entry=_Entry(9))
        proc.poll.side_effect = RuntimeError('poll')
        self.threads[0].target()  # logged, not raised
        proc.poll.side_effect = None
        proc.poll.return_value = 0
        h._running_launch_processes = None  # .get() on None raises inside the guarded block
        self.threads[0].target()

    def test_launch_without_entry_does_not_track(self):
        h = _Harness()
        proc = mock.Mock()
        proc.poll.return_value = 0
        with mock.patch.object(h, '_launch_env_for_command', return_value={'DISPLAY': ':0'}), \
                mock.patch.object(le, 'host_argv', side_effect=lambda argv, env_overrides: list(argv)), \
                mock.patch.object(le.subprocess, 'Popen', return_value=proc) as popen:
            self.assertTrue(h._launch_command_args(['firefox']))
        self.assertNotIn('MOZ_ENABLE_WAYLAND', popen.call_args.kwargs['env'])
        self.assertFalse(hasattr(h, '_running_launch_processes'))
        self.threads[0].target()  # exited quickly, no entry to clear

    def test_popen_failure_returns_false(self):
        h = _Harness()
        with mock.patch.object(h, '_launch_env_for_command', return_value={}), \
                mock.patch.object(le, 'host_argv', side_effect=lambda argv, env_overrides: list(argv)), \
                mock.patch.object(le.subprocess, 'Popen', side_effect=OSError('nope')):
            self.assertFalse(h._launch_command_args(['firefox'], entry=_Entry()))
        self.assertEqual(self.threads, [])


class LaunchFromIconTests(unittest.TestCase):
    def test_none_entry_is_ignored(self):
        h = _Harness()
        with mock.patch.object(h, 'launch_entry') as launch:
            h._launch_entry_from_icon(None)
        launch.assert_not_called()

    def test_sets_suppression_window_and_launches(self):
        h = _Harness()
        entry = _Entry(4)
        with mock.patch.object(le.GLib, 'get_monotonic_time', return_value=1000), \
                mock.patch.object(h, 'launch_entry') as launch:
            h._launch_entry_from_icon(entry)
        self.assertEqual(h._suppress_next_overview_activate_entry_id, 4)
        self.assertEqual(h._suppress_next_overview_activate_until_us, 601000)
        h.selection.set_selected.assert_called_once_with(le.Gtk.INVALID_LIST_POSITION)
        launch.assert_called_once_with(entry)

    def test_tolerates_bad_clock_and_missing_selection(self):
        h = _Harness()
        h.selection = None
        with mock.patch.object(le.GLib, 'get_monotonic_time', return_value='bad'), \
                mock.patch.object(h, 'launch_entry') as launch:
            h._launch_entry_from_icon(_Entry(8))
        self.assertEqual(h._suppress_next_overview_activate_until_us, 0)
        launch.assert_called_once()


class ResolveDesktopPathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.existing = self.root / 'a.desktop'
        self.existing.write_text('x')
        self.missing = self.root / 'gone.desktop'

    def resolve(self, entry, files, expected=None):
        with mock.patch.object(le, 'list_managed_desktop_files', return_value=files), \
                mock.patch.object(le, 'get_expected_desktop_path', return_value=expected):
            return _Harness()._resolve_desktop_path_for_entry(entry)

    def test_match_by_entry_id(self):
        files = [{'entry_id': 1, 'path': self.missing}, {'entry_id': 1, 'path': None}, {'entry_id': 1, 'path': self.existing}]
        self.assertEqual(self.resolve(_Entry(1, 'X'), files), self.existing)

    def test_match_by_title(self):
        files = [{'entry_id': 2, 'title': 'My App', 'path': self.missing}, {'entry_id': 3, 'title': ' My App ', 'path': self.existing}]
        self.assertEqual(self.resolve(_Entry(1, 'My App'), files), self.existing)

    def test_expected_path_fallback(self):
        self.assertEqual(self.resolve(_Entry(1, ''), [], expected=self.existing), self.existing)
        self.assertIsNone(self.resolve(_Entry(1, 'Other'), [{'title': 'X', 'path': self.existing}], expected=self.missing))
        self.assertIsNone(self.resolve(_Entry(1, 'Other'), [], expected=None))


class LaunchEntryTests(_Base):
    def run_launch(self, h, specs, launch_ok=True, running=None, system=None):
        entry = _Entry(1)
        with mock.patch.object(le, 'build_launch_command', side_effect=list(specs)) as build, \
                mock.patch.object(h, '_launch_command_args', return_value=launch_ok) as launch, \
                mock.patch.object(h, '_running_launch_process_for_entry', return_value=running), \
                mock.patch.object(h, '_system_process_running_for_profile', return_value=system) as system_check:
            h.launch_entry(entry)
        return build, launch, system_check

    def test_plain_launch(self):
        h = _Harness({PROFILE_PATH_KEY: '/p'})
        build, launch, _ = self.run_launch(h, [{'argv': ['ff'], 'profile_info': {'browser_family': 'firefox'}}])
        self.assertEqual(build.call_count, 1)
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[0], ['ff'])
        self.assertEqual(h.notifications, [])

    def test_profile_is_prepared_and_stored(self):
        h = _Harness()
        first = {'argv': ['ff'], 'profile_info': {'browser_family': 'firefox'}}
        second = {'argv': ['ff', '-profile', '/new'], 'profile_info': {'profile_name': 'n', 'profile_path': '/new'}}
        build, launch, _ = self.run_launch(h, [first, second])
        self.assertTrue(build.call_args_list[1].kwargs['prepare_profile'])
        self.assertEqual(h.added_options, [(1, {PROFILE_NAME_KEY: 'n', PROFILE_PATH_KEY: '/new'})])
        self.assertEqual(launch.call_args.args[0], ['ff', '-profile', '/new'])

    def test_prepared_spec_without_profile_info_stores_nothing(self):
        h = _Harness()
        first = {'argv': ['ff'], 'profile_info': {'browser_family': 'firefox'}}
        self.run_launch(h, [first, {'argv': ['ff2']}])
        self.assertEqual(h.added_options, [])

    def test_missing_launch_spec_reports_failure(self):
        h = _Harness()
        _, launch, _ = self.run_launch(h, [None])
        launch.assert_not_called()
        self.assertEqual(h.notifications, [('launch_failed', 3200)])

    def test_failed_launch_reports_failure(self):
        h = _Harness({PROFILE_PATH_KEY: '/p'})
        self.run_launch(h, [{'argv': ['ff']}], launch_ok=False)
        self.assertEqual(h.notifications, [('launch_failed', 3200)])

    def test_single_instance_blocks_tracked_process(self):
        h = _Harness({PROFILE_PATH_KEY: '/p', OPTION_PREVENT_MULTIPLE_STARTS_KEY: '1'})
        _, launch, system_check = self.run_launch(h, [{'argv': ['ff']}], running={'process': mock.Mock(pid=3)})
        launch.assert_not_called()
        system_check.assert_not_called()
        self.assertEqual(h.notifications, [('launch_already_running', 2200)])

    def test_single_instance_blocks_foreign_system_process(self):
        h = _Harness({PROFILE_PATH_KEY: '/p', OPTION_PREVENT_MULTIPLE_STARTS_KEY: '1'})
        spec = {'argv': ['ff'], 'profile_info': {'profile_path': '/p'}}
        _, launch, system_check = self.run_launch(h, [spec], system={'pid': 77})
        system_check.assert_called_once_with('/p')
        launch.assert_not_called()
        self.assertEqual(h.notifications, [('launch_already_running', 2200)])

    def test_single_instance_allows_launch_when_nothing_runs(self):
        h = _Harness({PROFILE_PATH_KEY: '/p', OPTION_PREVENT_MULTIPLE_STARTS_KEY: '1'})
        _, launch, _ = self.run_launch(h, [{'argv': ['ff']}])
        launch.assert_called_once()

    def test_single_instance_without_spec_reports_failure(self):
        h = _Harness({OPTION_PREVENT_MULTIPLE_STARTS_KEY: '1'})
        _, launch, _ = self.run_launch(h, [None])
        launch.assert_not_called()
        self.assertEqual(h.notifications, [('launch_failed', 3200)])


if __name__ == '__main__':
    unittest.main()

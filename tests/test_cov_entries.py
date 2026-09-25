"""Coverage for mainwindow/entries.py (MainWindowEntriesMixin and its helpers).

Follows the harness pattern of tests/test_plugin_feedback.py: a stub class
borrows the real mixin methods and hand-implements what they call back into.
Every filesystem touch goes to a temporary directory; profile-size caching,
desktop-file export and icon normalisation are mocked at the name the module
reads.
"""
import json
import logging
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov.entries.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

from app_models import Entry  # noqa: E402
from mainwindow import entries as entries_mod  # noqa: E402
from mainwindow.entries import MainWindowEntriesMixin  # noqa: E402
from webapp_constants import (  # noqa: E402
    ADDRESS_KEY, APP_MODE_KEY, DESKTOP_NAME_SOURCE_KEY, ICON_PATH_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY,
    USER_AGENT_NAME_KEY, USER_AGENT_VALUE_KEY,
)

ENGINES = [
    {'id': 1, 'name': 'Firefox', 'command': 'firefox'},
    {'id': 'broken', 'name': 'Broken', 'command': 'x'},
    {'id': 2, 'name': 'Chromium', 'command': 'chromium'},
]


def _fake_t(key, **kwargs):
    if kwargs:
        return f'{key}|' + ','.join(f'{k}={v}' for k, v in sorted(kwargs.items()))
    return key


class _Store:
    def __init__(self, items=None):
        self._items = list(items or [])

    def get_n_items(self):
        return len(self._items)

    def get_item(self, index):
        return self._items[index]

    def insert(self, index, item):
        self._items.insert(index, item)

    def append(self, item):
        self._items.append(item)

    def remove(self, index):
        self._items.pop(index)

    def remove_all(self):
        self._items.clear()

    def ids(self):
        return [item.id for item in self._items]


class _Harness(MainWindowEntriesMixin):
    def __init__(self, entries=None, options=None):
        self.entries_store = _Store(entries)
        self.filtered_model = _Store()
        self.db = mock.MagicMock()
        self._options_cache = dict(options or {})
        self._profile_size_cache = {}
        self._profile_size_pending = set()
        self._profile_size_measured = set()
        self._startup_profile_cleanup_done = False
        self.detail_pages = {}
        self.custom_filter = mock.MagicMock()
        self.stack = mock.MagicMock()
        self.calls = []
        self.reconcile_queue = []

    # What the mixin calls back into ---------------------------------------
    def _hide_busy(self):
        self.calls.append('hide_busy')

    def _show_startup_busy(self):
        self.calls.append('show_startup_busy')

    def update_empty_state(self):
        self.calls.append('update_empty_state')

    def _show_overview_root_page(self):
        self.calls.append('show_overview_root')

    def _remove_overview_page_widget(self, child):
        self.calls.append(('remove_page', child))

    def refresh_entry_visual(self, entry):
        self.calls.append(('refresh', entry.id))

    def _present_info_dialog(self, message):
        self.calls.append(('info', message))

    def show_overlay_notification(self, message, timeout_ms=3000):
        self.calls.append(('toast', message, timeout_ms))

    def _destroy_import_progress_dialog(self):
        self.calls.append('destroy_progress')

    def _show_import_progress_dialog(self, total, title_text='', preparing_text=''):
        self.calls.append(('show_progress', total, title_text, preparing_text))

    def _update_import_progress(self, *args):
        self.calls.append(('progress',) + args)

    def _present_choice_dialog(self, message, on_result, destructive=False):
        self.calls.append(('choice', message, destructive))
        self.choice_callback = on_result

    def _present_yes_no_dialog(self, text, callback):
        self.calls.append(('yes_no', text))
        self.yes_no_callback = callback


class _PatchMixin:
    def _patch(self, name, *args, **kwargs):
        patcher = mock.patch.object(entries_mod, name, *args, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def _tmpdir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)


# --------------------------------------------------------------------------
# Module-level helpers
# --------------------------------------------------------------------------

class FormatProfileSizeTests(unittest.TestCase):
    def test_blank_path_is_zero_without_measuring(self):
        with mock.patch.object(entries_mod.profile_size_cache, 'measure') as measure:
            self.assertEqual(entries_mod.format_profile_size('   '), '0 MB')
            self.assertEqual(entries_mod.format_profile_size(None), '0 MB')
        measure.assert_not_called()

    def test_measured_text_and_empty_fallback(self):
        with mock.patch.object(entries_mod.profile_size_cache, 'measure', return_value='12 MB') as measure:
            self.assertEqual(entries_mod.format_profile_size(' /p '), '12 MB')
        measure.assert_called_once_with('/p')
        with mock.patch.object(entries_mod.profile_size_cache, 'measure', return_value=''):
            self.assertEqual(entries_mod.format_profile_size('/p'), '0 MB')


class ProfileSizeWorkerTests(unittest.TestCase):
    def _run_measurement(self, format_kwargs):
        done = threading.Event()
        delivered = []

        def fake_idle_add(callback, value):
            delivered.append((callback, value))
            done.set()

        on_done = object()
        with mock.patch.object(entries_mod, 'format_profile_size', **format_kwargs), \
                mock.patch.object(entries_mod.GLib, 'idle_add', side_effect=fake_idle_add):
            entries_mod.queue_profile_size_measurement('/profile', on_done)
            self.assertTrue(done.wait(5), 'worker never delivered a result')
        return on_done, delivered

    def test_worker_measures_and_hands_result_to_main_loop(self):
        on_done, delivered = self._run_measurement({'return_value': '3 MB'})
        self.assertEqual(delivered, [(on_done, '3 MB')])

    def test_worker_reports_empty_text_on_oserror(self):
        on_done, delivered = self._run_measurement({'side_effect': OSError('gone')})
        self.assertEqual(delivered, [(on_done, '')])

    def test_dead_worker_is_replaced(self):
        dead = mock.MagicMock()
        dead.is_alive.return_value = False
        entries_mod._PROFILE_SIZE_WORKER = dead
        on_done, delivered = self._run_measurement({'return_value': '1 MB'})
        self.assertIsNot(entries_mod._PROFILE_SIZE_WORKER, dead)
        self.assertTrue(entries_mod._PROFILE_SIZE_WORKER.is_alive())
        self.assertEqual(delivered, [(on_done, '1 MB')])


class FindFilesNamedTests(_PatchMixin, unittest.TestCase):
    def test_walks_tree_and_matches_only_files(self):
        root = self._tmpdir()
        (root / 'a' / 'b').mkdir(parents=True)
        (root / 'a' / 'b' / 'app.png').write_bytes(b'x')
        (root / 'app.svg').write_bytes(b'x')
        (root / 'other.png').write_bytes(b'x')
        (root / 'app.xpm').mkdir()  # a directory with a wanted name is not a match
        found = entries_mod._find_files_named(root, {'app.png', 'app.svg', 'app.xpm'})
        self.assertEqual(sorted(p.name for p in found), ['app.png', 'app.svg'])

    def test_missing_root_yields_nothing(self):
        root = self._tmpdir()
        self.assertEqual(entries_mod._find_files_named(root / 'missing', {'x'}), [])

    def test_entry_level_oserror_is_skipped(self):
        bad = mock.MagicMock()
        bad.is_dir.side_effect = OSError('vanished')
        good = mock.MagicMock()
        good.is_dir.return_value = False
        good.name = 'app.png'
        good.is_file.return_value = True
        good.path = '/fake/app.png'
        scandir_cm = mock.MagicMock()
        scandir_cm.__enter__.return_value = [bad, good]
        with mock.patch.object(entries_mod.os, 'scandir', return_value=scandir_cm):
            self.assertEqual(entries_mod._find_files_named('/fake', {'app.png'}), [Path('/fake/app.png')])


# --------------------------------------------------------------------------
# Store handling and lookups
# --------------------------------------------------------------------------

class StoreOrderingTests(unittest.TestCase):
    def test_insert_sorted_by_title_then_id(self):
        h = _Harness()
        self.assertEqual(h._insert_entry_sorted(Entry(2, 'beta')), 0)
        self.assertEqual(h._insert_entry_sorted(Entry(1, 'Alpha')), 0)
        self.assertEqual(h._insert_entry_sorted(Entry(3, 'gamma')), 2)
        self.assertEqual(h._insert_entry_sorted(Entry(0, 'beta')), 1)
        self.assertEqual(h.entries_store.ids(), [1, 0, 2, 3])

    def test_reposition_moves_renamed_entry(self):
        a, b, c = Entry(1, 'a'), Entry(2, 'b'), Entry(3, 'c')
        h = _Harness([a, b, c])
        a.title = 'z'
        self.assertEqual(h._reposition_entry_in_store(a), 2)
        self.assertEqual(h.entries_store.ids(), [2, 3, 1])

    def test_reposition_inserts_unknown_entry(self):
        h = _Harness([Entry(1, 'a')])
        h._reposition_entry_in_store(Entry(9, 'b'))
        self.assertEqual(h.entries_store.ids(), [1, 9])

    def test_find_by_id_and_title(self):
        a, b, c = Entry(1, 'Same'), Entry(2, 'Same'), Entry(3, 'Other')
        h = _Harness([a, b, c])
        self.assertIs(h._find_entry_by_id(3), c)
        self.assertIsNone(h._find_entry_by_id(99))
        self.assertEqual(h._find_entry_by_title('Same'), [a, b])
        self.assertEqual(h._find_entry_by_title('None'), [])

    def test_entry_by_id_prefers_filtered_model_then_store(self):
        in_filter, only_store = Entry(1, 'a'), Entry(2, 'b')
        h = _Harness([Entry(1, 'store copy'), only_store])
        h.filtered_model = _Store([None, in_filter])
        self.assertIs(h._entry_by_id('1'), in_filter)
        self.assertIs(h._entry_by_id(2), only_store)
        self.assertIsNone(h._entry_by_id(3))

    def test_profile_display_name(self):
        h = _Harness()
        self.assertEqual(h._profile_display_name({PROFILE_PATH_KEY: ' /x/webapp_abc ', PROFILE_NAME_KEY: 'n'}), 'webapp_abc')
        self.assertEqual(h._profile_display_name({PROFILE_NAME_KEY: ' name '}), 'name')
        self.assertEqual(h._profile_display_name({}), '')


class LoadAndReloadTests(_PatchMixin, unittest.TestCase):
    def test_load_entries_fills_store_options_and_remembered_sizes(self):
        h = _Harness([Entry(99, 'old')])
        h._profile_size_pending = {99}
        h.db.list_entries.return_value = [(1, 'One', 'd', 1), (2, 'Two', '', 0), (3, 'Three', '', 1)]
        h.db.list_option_values.return_value = [
            (10, 1, PROFILE_PATH_KEY, '/p1'),
            (11, 2, PROFILE_PATH_KEY, '/p2'),
            (12, 3, ADDRESS_KEY, 'https://x'),
        ]
        lookup = self._patch('profile_size_cache')
        lookup.lookup.side_effect = lambda path: ('5 MB', True) if path == '/p1' else (None, True)
        h.load_entries_from_db()
        self.assertEqual(h.entries_store.ids(), [1, 2, 3])
        self.assertFalse(h.entries_store.get_item(1).active)
        self.assertEqual(h._options_cache[1], {PROFILE_PATH_KEY: '/p1'})
        self.assertEqual(h._options_cache[3], {ADDRESS_KEY: 'https://x'})
        self.assertEqual(h._profile_size_cache, {1: {'path': '/p1', 'text': '5 MB'}})
        self.assertEqual(h._profile_size_pending, set())

    def test_cleanup_detail_pages_hides_and_removes(self):
        h = _Harness()
        good = mock.MagicMock()
        broken = mock.MagicMock()
        broken.set_visible.side_effect = AttributeError
        self.assertFalse(h._cleanup_detail_pages([good, broken]))
        good.set_visible.assert_called_once_with(False)
        self.assertEqual(h.calls, [('remove_page', good), ('remove_page', broken)])

    def test_reload_with_open_pages_returns_to_overview(self):
        glib = self._patch('GLib')
        glib.Error = type('FakeGError', (Exception,), {})
        h = _Harness()
        h.load_entries_from_db = mock.MagicMock()
        page = object()
        h.detail_pages = {1: page}
        h._creating_entry = True
        h._reload_entries()
        h.stack.set_visible_child_name.assert_called_once_with('overview_page')
        glib.idle_add.assert_called_once_with(h._cleanup_detail_pages, [page])
        self.assertEqual(h.detail_pages, {})
        self.assertFalse(h._creating_entry)
        h.load_entries_from_db.assert_called_once_with()
        h.custom_filter.changed.assert_called_once()
        self.assertIn('update_empty_state', h.calls)

    def test_reload_survives_navigation_errors_and_skips_cleanup_without_pages(self):
        glib = self._patch('GLib')
        glib.Error = type('FakeGError', (Exception,), {})
        h = _Harness()
        h.load_entries_from_db = mock.MagicMock()
        h._show_overview_root_page = mock.MagicMock(side_effect=AttributeError)
        h.detail_pages = {1: object()}
        h._reload_entries()
        glib.idle_add.assert_called_once()
        glib.idle_add.reset_mock()
        h._reload_entries()
        glib.idle_add.assert_not_called()


class ImportCollisionTests(unittest.TestCase):
    def setUp(self):
        self.a = Entry(1, 'Mail')
        self.b = Entry(2, 'Chat')
        self.c = Entry(3, 'Chat')
        self.h = _Harness([self.a, self.b, self.c], {
            1: {ADDRESS_KEY: 'https://mail.example', 'EngineID': '1'},
            2: {ADDRESS_KEY: 'https://chat.example', 'EngineID': '1'},
            3: {ADDRESS_KEY: 'https://chat.example', 'EngineID': '2'},
        })

    def test_exact_title_and_address_wins(self):
        payload = {'title': ' chat ', 'options': {ADDRESS_KEY: 'https://chat.example', 'EngineID': '9'}}
        self.assertIs(self.h._find_import_collision(payload), self.b)

    def test_same_address_and_engine_is_a_fallback_match(self):
        payload = {'title': 'Renamed', 'options': {ADDRESS_KEY: 'https://chat.example', 'EngineID': '2'}}
        self.assertIs(self.h._find_import_collision(payload), self.c)

    def test_same_title_and_engine_is_a_fallback_match(self):
        payload = {'title': 'Mail', 'options': {ADDRESS_KEY: 'https://new.example', 'EngineID': '1'}}
        self.assertIs(self.h._find_import_collision(payload), self.a)

    def test_first_fallback_match_is_kept(self):
        payload = {'title': 'chat', 'options': {'EngineID': '1', ADDRESS_KEY: 'https://other'}}
        self.assertIs(self.h._find_import_collision(payload), self.b)

    def test_no_match_and_empty_payload(self):
        self.assertIsNone(self.h._find_import_collision({'title': 'Nope', 'options': {ADDRESS_KEY: 'https://z'}}))
        self.assertIsNone(self.h._find_import_collision({'title': '', 'options': 'not a dict'}))

    def test_non_dict_payload_is_tolerated(self):
        # Regression: a non-dict payload raised AttributeError instead of meaning "no collision".
        self.assertIsNone(self.h._find_import_collision(['not', 'a', 'dict']))


class ShowImportCollisionTests(_PatchMixin, unittest.TestCase):
    def setUp(self):
        self._patch('t', side_effect=_fake_t)

    def test_opens_entry_and_reports_on_detail_page(self):
        h = _Harness()
        entry = Entry(4, 'Stored')
        page = mock.MagicMock()

        def activate(e, show_busy=True):
            self.assertFalse(show_busy)
            h.detail_pages[e.id] = page
        h.on_entry_activated = activate
        h._show_import_collision(entry, {'title': 'Incoming'})
        page._set_detail_action_status.assert_called_once_with('import_duplicate_detected|title=Incoming')
        self.assertIn(('info', 'import_duplicate_detected|title=Incoming'), h.calls)

    def test_falls_back_to_entry_title_without_page(self):
        h = _Harness()  # no on_entry_activated -> AttributeError is swallowed
        h._show_import_collision(Entry(4, 'Stored'), {'title': '   '})
        self.assertEqual(h.calls, [('info', 'import_duplicate_detected|title=Stored')])


class OptionCacheTests(unittest.TestCase):
    def test_get_options_dict_uses_cache_and_returns_copy(self):
        h = _Harness(options={1: {'a': '1'}})
        result = h._get_options_dict(1)
        result['a'] = 'changed'
        self.assertEqual(h._options_cache[1], {'a': '1'})
        h.db.get_options_for_entry.assert_not_called()

    def test_get_options_dict_loads_on_miss_or_force(self):
        h = _Harness(options={1: {'a': '1'}})
        h.db.get_options_for_entry.return_value = [(1, 1, ADDRESS_KEY, 'https://x')]
        self.assertEqual(h._get_options_dict(1, force_refresh=True), {ADDRESS_KEY: 'https://x'})
        self.assertEqual(h._options_cache[1], {ADDRESS_KEY: 'https://x'})
        h.db.get_options_for_entry.return_value = []
        self.assertEqual(h._get_options_dict(2), {})
        h.db.get_options_for_entry.assert_called_with(2)

    def test_drop_profile_size_cache_forgets_measurement(self):
        h = _Harness()
        h._profile_size_cache = {1: {'path': '/p', 'text': '1 MB'}, 2: {'path': '', 'text': ''}}
        h._profile_size_pending = {1, 2}
        h._profile_size_measured = {'/p', '/q'}
        h._drop_profile_size_cache(1)
        h._drop_profile_size_cache(2)
        h._drop_profile_size_cache(3)
        self.assertEqual(h._profile_size_cache, {})
        self.assertEqual(h._profile_size_pending, set())
        self.assertEqual(h._profile_size_measured, {'/q'})

    def test_invalidate_entry_cache(self):
        h = _Harness(options={1: {'a': '1'}, 2: {'b': '2'}})
        h._profile_size_cache = {1: {'path': '/p', 'text': 'x'}, 2: {'path': '/q', 'text': 'y'}}
        h._invalidate_entry_cache(1)
        self.assertNotIn(1, h._options_cache)
        self.assertIn(1, h._profile_size_cache)
        h._invalidate_entry_cache(2, clear_profile_size=True)
        self.assertNotIn(2, h._profile_size_cache)

    def test_cache_options_stringifies_and_drops_size_on_profile_change(self):
        h = _Harness(options={1: {'a': '1'}})
        h._profile_size_cache = {1: {'path': '/p', 'text': 'x'}}
        h._cache_options(1, {'n': 5, 'none': None})
        self.assertEqual(h._options_cache[1], {'a': '1', 'n': '5', 'none': ''})
        self.assertIn(1, h._profile_size_cache)
        h._cache_options(1, {PROFILE_PATH_KEY: '/new'})
        self.assertNotIn(1, h._profile_size_cache)

    def test_add_options_writes_db_and_cache(self):
        h = _Harness(options={1: {}})
        h._add_options(1, {})
        h.db.add_options.assert_not_called()
        h._add_options(1, {'k': 3, 'v': None})
        h.db.add_options.assert_called_once_with(1, {'k': '3', 'v': ''})
        self.assertEqual(h._options_cache[1], {'k': '3', 'v': ''})


# --------------------------------------------------------------------------
# Icon discovery for imported desktop files
# --------------------------------------------------------------------------

class IconCandidateTests(_PatchMixin, unittest.TestCase):
    def setUp(self):
        self.root = self._tmpdir()
        self.h = _Harness()

    def test_blank_candidate(self):
        self.assertEqual(self.h._iter_icon_candidates('  '), [])

    def test_direct_path_with_suffix(self):
        icon = self.root / 'app.png'
        icon.write_bytes(b'x')
        self.assertEqual(self.h._iter_icon_candidates(str(icon)), [icon])
        self.assertEqual(self.h._iter_icon_candidates(str(self.root / 'missing.png')), [])

    def test_suffixless_name_tries_known_extensions(self):
        (self.root / 'app.svg').write_bytes(b'x')
        (self.root / 'app.png').write_bytes(b'x')
        found = self.h._iter_icon_candidates(str(self.root / 'app'))
        self.assertEqual(sorted(p.name for p in found), ['app.png', 'app.svg'])
        self.assertEqual(len(found), 2, 'the same file must not be reported twice')

    def test_symlinked_duplicate_is_reported_once(self):
        (self.root / 'app.svg').write_bytes(b'x')
        os.symlink(self.root / 'app.svg', self.root / 'app')
        found = self.h._iter_icon_candidates(str(self.root / 'app'))
        self.assertEqual(found, [self.root / 'app'])

    def test_base_dir_file_and_icon_subdirs_are_searched(self):
        desktop = self.root / 'app.desktop'
        desktop.write_text('')
        (self.root / 'icons').mkdir()
        (self.root / 'icons' / 'logo.png').write_bytes(b'x')
        (self.root / 'pixmaps').mkdir()
        (self.root / 'pixmaps' / 'logo.xpm').write_bytes(b'x')
        found = self.h._iter_icon_candidates('logo', base_dir=str(desktop))
        self.assertEqual(sorted(p.name for p in found), ['logo.png', 'logo.xpm'])

    def test_resolve_errors_fall_back_to_unresolved_paths(self):
        (self.root / 'logo.png').write_bytes(b'x')
        with mock.patch.object(Path, 'resolve', side_effect=RuntimeError('loop')):
            found = self.h._iter_icon_candidates('logo.png', base_dir=str(self.root))
        self.assertEqual(found, [self.root / 'logo.png'])

    def test_lookup_prefers_local_candidate(self):
        (self.root / 'logo.png').write_bytes(b'x')
        find = self._patch('_find_files_named')
        self.assertEqual(self.h._lookup_system_icon_file('logo', base_dir=str(self.root)), self.root / 'logo.png')
        find.assert_not_called()
        self.assertIsNone(self.h._lookup_system_icon_file('  '))

    def test_lookup_scans_theme_dirs_and_scores_results(self):
        paths = {
            '/usr/share/icons': [
                Path('/usr/share/icons/hicolor/48x48/apps/app.png'),
                Path('/usr/share/icons/hicolor/256x256/apps/app.png'),
                Path('/usr/share/icons/hicolor/32x32/apps/app.svg'),
            ],
        }
        find = self._patch('_find_files_named', side_effect=lambda root, wanted: paths.get(str(root), []))
        self.assertEqual(self.h._lookup_system_icon_file('app'), Path('/usr/share/icons/hicolor/32x32/apps/app.svg'))
        wanted = find.call_args[0][1]
        self.assertEqual(wanted, {'app.svg', 'app.png', 'app.ico', 'app.xpm'})
        del paths['/usr/share/icons'][2]
        self.assertEqual(self.h._lookup_system_icon_file('app.png'), Path('/usr/share/icons/hicolor/256x256/apps/app.png'))
        self.assertEqual(find.call_args[0][1], {'app.png'})

    def test_lookup_returns_none_when_nothing_found(self):
        self._patch('_find_files_named', return_value=[])
        self.assertIsNone(self.h._lookup_system_icon_file('nothing'))

    def test_lookup_ranks_unsized_paths(self):
        found = [Path('/usr/share/icons/Adwaita/scalable/apps/app.ico'), Path('/usr/share/icons/Adwaita/48x48/apps/app.ico')]
        self._patch('_find_files_named', side_effect=lambda root, wanted: found if str(root) == '/usr/share/icons' else [])
        self.assertEqual(self.h._lookup_system_icon_file('app'), found[1])

    def test_lookup_handles_pixmaps_directory(self):
        # Regression: 'pixmaps' contains an 'x' and was parsed as a size (int('pi')), which
        # crashed the lookup for every icon found in /usr/share/pixmaps.
        pix = Path('/usr/share/pixmaps/app.png')
        self._patch('_find_files_named', side_effect=lambda root, wanted: [pix] if str(root) == '/usr/share/pixmaps' else [])
        self.assertEqual(self.h._lookup_system_icon_file('app'), pix)


class ResolveImportIconTests(_PatchMixin, unittest.TestCase):
    def setUp(self):
        self.root = self._tmpdir()
        self.managed = self.root / 'managed'
        self.managed.mkdir()
        self.get_path = self._patch('get_managed_icon_path', side_effect=lambda title, suffix, entry_id: self.managed / f'{entry_id}{suffix}')
        self.normalize = self._patch('normalize_icon_to_png')
        self._patch('t', side_effect=_fake_t)
        self.h = _Harness()

    def test_icon_path_is_normalised_into_managed_dir(self):
        src = self.root / 'icon.png'
        src.write_bytes(b'png')
        result = self.h._resolve_import_icon_reference({'icon_path': str(src)}, 'T', 7)
        self.assertEqual(result, str(self.managed / '7.png'))
        self.normalize.assert_called_once_with(src, self.managed / '7.png')

    def test_failed_normalisation_copies_raw_file_and_warns_about_svg(self):
        src = self.root / 'icon.svg'
        src.write_bytes(b'<svg/>')
        self.normalize.side_effect = ValueError('svg')
        self._patch('is_svg_support_missing_error', return_value=True)
        result = self.h._resolve_import_icon_reference({'icon_path': str(src)}, 'T', 7)
        self.assertEqual(result, str(self.managed / '7.svg'))
        self.assertEqual((self.managed / '7.svg').read_bytes(), b'<svg/>')
        self.assertIn(('toast', 'svg_import_requires_cairo', 4200), self.h.calls)

    def test_svg_warning_without_toast_support_still_copies(self):
        src = self.root / 'icon.svg'
        src.write_bytes(b'<svg/>')
        self.normalize.side_effect = OSError('svg')
        self._patch('is_svg_support_missing_error', return_value=True)
        self.h.show_overlay_notification = mock.MagicMock(side_effect=AttributeError)
        self.assertEqual(self.h._resolve_import_icon_reference({'icon_path': str(src)}, 'T', 7), str(self.managed / '7.svg'))

    def test_unreadable_candidate_falls_through_to_icon_name(self):
        src = self.root / 'icon.ico'
        src.write_bytes(b'ico')
        themed = self.root / 'themed.png'
        calls = []

        def normalize(candidate, target):
            calls.append(candidate)
            if candidate == src:
                raise OSError('bad')
        self.normalize.side_effect = normalize
        self._patch('is_svg_support_missing_error', return_value=False)
        self.h._lookup_system_icon_file = mock.MagicMock(return_value=themed)
        # the raw .ico fallback copy targets a directory that does not exist -> OSError -> ''
        self.get_path.side_effect = lambda title, suffix, entry_id: (self.root / 'nope' / 'x.ico') if suffix == '.ico' else self.managed / 'ok.png'
        result = self.h._resolve_import_icon_reference({'icon_path': str(src), 'icon_name': 'themed'}, 'T', 7)
        self.assertEqual(calls, [src, themed])
        self.assertEqual(result, str(self.managed / 'ok.png'))

    def test_title_and_desktop_stem_are_tried_last(self):
        found = self.root / 'match.png'
        tried = []

        def lookup(name, base_dir=None):
            tried.append(name)
            return found if name == 'mail-app' else None
        self.h._lookup_system_icon_file = lookup
        data = {'path': str(self.root / 'mail-app.desktop'), 'icon_name': 'missing-theme-icon'}
        result = self.h._resolve_import_icon_reference(data, 'My Mail', 3)
        self.assertEqual(result, str(self.managed / '3.png'))
        self.assertEqual(tried[0], 'missing-theme-icon')
        self.assertIn('My Mail', tried)
        self.assertEqual(tried[-1], 'mail-app')

    def test_nothing_found_returns_empty(self):
        self.h._lookup_system_icon_file = mock.MagicMock(return_value=None)
        self.assertEqual(self.h._resolve_import_icon_reference({}, '', 3), '')

    def test_failing_icon_name_copy_moves_on_to_title(self):
        themed = self.root / 'themed.png'
        self.normalize.side_effect = OSError('broken')
        self._patch('is_svg_support_missing_error', return_value=False)
        self.h._lookup_system_icon_file = mock.MagicMock(return_value=themed)
        # themed does not exist, so the raw fallback copy fails as well
        self.assertEqual(self.h._resolve_import_icon_reference({'icon_name': 'x'}, 'T', 3), '')
        tried = [c.args[0] for c in self.h._lookup_system_icon_file.call_args_list]
        self.assertEqual(tried[0], 'x')
        self.assertIn('T', tried[1:])


# --------------------------------------------------------------------------
# Profile size scheduling and the startup spinner
# --------------------------------------------------------------------------

class ProfileSizeSchedulingTests(_PatchMixin, unittest.TestCase):
    def setUp(self):
        self.cache = self._patch('profile_size_cache')
        self.cache.lookup.return_value = ('9 MB', False)
        self.queued = []
        self._patch('queue_profile_size_measurement', side_effect=lambda path, cb: self.queued.append((path, cb)))
        self.h = _Harness()

    def test_cached_text_lookup(self):
        self.h._profile_size_cache = {1: {'path': '/p', 'text': '4 MB'}, 2: {'path': '/p'}}
        self.assertEqual(self.h._get_profile_size_text_cached(1, '/p'), '4 MB')
        self.assertEqual(self.h._get_profile_size_text_cached(2, '/p'), '0 MB')
        self.assertEqual(self.h._get_profile_size_text_cached(1, '/other'), '0 MB')
        self.assertEqual(self.h._get_profile_size_text_cached(3, ''), '')

    def test_needs_measurement_decisions(self):
        h = self.h
        self.assertTrue(h._profile_size_needs_measurement(1, '/p'))
        h._profile_size_cache[1] = {'path': '/p', 'text': '4 MB'}
        self.cache.lookup.return_value = ('4 MB', True)
        self.assertTrue(h._profile_size_needs_measurement(1, '/p'))
        self.cache.lookup.return_value = ('4 MB', False)
        self.assertFalse(h._profile_size_needs_measurement(1, '/p'))
        self.assertIn('/p', h._profile_size_measured)
        self.cache.lookup.reset_mock()
        self.assertFalse(h._profile_size_needs_measurement(1, '/p'))
        self.cache.lookup.assert_not_called()

    def test_empty_path_clears_label(self):
        label = mock.MagicMock()
        self.h._schedule_profile_size_refresh(1, '', label)
        self.assertEqual(self.h._profile_size_cache[1], {'path': '', 'text': ''})
        label.set_text.assert_called_once_with('')
        label.set_visible.assert_called_once_with(False)
        self.h._schedule_profile_size_refresh(2, '', None)
        self.assertEqual(self.queued, [])

    def test_pending_and_fresh_entries_are_not_requeued(self):
        self.h._profile_size_pending = {1}
        self.h._schedule_profile_size_refresh(1, '/p', None)
        self.h._profile_size_cache[2] = {'path': '/q', 'text': '1 MB'}
        self.h._schedule_profile_size_refresh(2, '/q', None)
        self.assertEqual(self.queued, [])

    def test_measurement_result_updates_matching_label_and_flushes(self):
        label = mock.MagicMock()
        label._entry_id = 1
        label._profile_path = '/p'
        self.h._schedule_profile_size_refresh(1, '/p', label)
        self.assertEqual(self.h._profile_size_pending, {1})
        path, apply = self.queued[0]
        self.assertEqual(path, '/p')
        self.assertFalse(apply('6 MB'))
        label.set_text.assert_called_once_with('6 MB')
        label.set_visible.assert_called_once_with(True)
        self.assertEqual(self.h._profile_size_cache[1], {'path': '/p', 'text': '6 MB'})
        self.assertIn('/p', self.h._profile_size_measured)
        self.cache.flush.assert_called_once_with()

    def test_recycled_label_is_left_alone_and_flush_waits_for_others(self):
        label = mock.MagicMock()
        label._entry_id = 2  # the row now shows another entry
        label._profile_path = '/p'
        self.h._schedule_profile_size_refresh(1, '/p', label)
        self.h._schedule_profile_size_refresh(3, '/r', None)
        self.queued[0][1]('6 MB')
        label.set_text.assert_not_called()
        self.cache.flush.assert_not_called()
        self.queued[1][1]('')
        self.cache.flush.assert_called_once_with()

    def test_startup_sync_waits_for_rows_without_size(self):
        h = _Harness([Entry(1, 'a'), Entry(2, 'b'), Entry(3, 'c')], {
            1: {PROFILE_PATH_KEY: '/p1'},
            2: {PROFILE_PATH_KEY: '/p2'},
            3: {},
        })
        h._profile_size_cache[1] = {'path': '/p1', 'text': '3 MB'}
        self.cache.lookup.return_value = ('3 MB', True)  # stale -> refresh
        h._start_startup_profile_size_sync()
        self.assertEqual([p for p, _ in self.queued], ['/p1', '/p2'])
        self.assertTrue(h._startup_waiting_for_profile_sizes)
        self.assertNotIn('hide_busy', h.calls)
        self.queued[1][1]('7 MB')  # the row without a size is done
        self.assertEqual(h.calls.count('hide_busy'), 1)
        self.assertFalse(h._startup_waiting_for_profile_sizes)
        self.queued[0][1]('3 MB')  # later refresh no longer touches the spinner
        self.assertEqual(h.calls.count('hide_busy'), 1)

    def test_startup_sync_with_only_refreshes_releases_spinner_at_once(self):
        h = _Harness([Entry(1, 'a')], {1: {PROFILE_PATH_KEY: '/p1'}})
        h._profile_size_cache[1] = {'path': '/p1', 'text': '3 MB'}
        self.cache.lookup.return_value = ('3 MB', True)
        h._start_startup_profile_size_sync()
        self.assertEqual(len(self.queued), 1)
        self.assertEqual(h.calls.count('hide_busy'), 1)

    def test_finish_startup_busy_is_noop_when_not_waiting(self):
        self.h._maybe_finish_startup_busy()
        self.assertEqual(self.h.calls, [])


# --------------------------------------------------------------------------
# Engines, profile sync and startup cleanup
# --------------------------------------------------------------------------

class EngineAndProfileSyncTests(_PatchMixin, unittest.TestCase):
    def setUp(self):
        self._patch('ENGINES', ENGINES)
        self.h = _Harness()

    def test_engine_lookup_by_id_then_name(self):
        self.assertEqual(self.h._engine_for_options({'EngineID': '2'})['name'], 'Chromium')
        self.assertEqual(self.h._engine_for_options({'EngineID': 'abc', 'EngineName': ' firefox '})['id'], 1)
        self.assertEqual(self.h._engine_for_options({'EngineID': '77', 'EngineName': 'Chromium'})['id'], 2)
        self.assertIsNone(self.h._engine_for_options({'EngineName': 'Opera'}))
        self.assertIsNone(self.h._engine_for_options(None))

    def test_browser_family(self):
        self.assertEqual(self.h._browser_family_for_options({'EngineID': '1'}), 'firefox')
        self.assertEqual(self.h._browser_family_for_options({'EngineID': '2'}), 'chromium')
        self.assertEqual(self.h._browser_family_for_options(None), 'generic')

    def test_build_detail_header(self):
        gtk = self._patch('Gtk')
        self._patch('t', side_effect=_fake_t)
        label = self.h._build_detail_header(Entry(1, 'a'))
        self.assertIs(label, gtk.Label.return_value)
        gtk.Label.assert_called_once_with(xalign=0.5)
        label.set_text.assert_called_once_with('app_title')
        label.add_css_class.assert_called_once_with('title-4')
        label.set_max_width_chars.assert_called_once_with(40)

    def test_profile_sync_skips_generic_or_missing_path(self):
        read = self._patch('read_profile_settings')
        self.assertEqual(self.h._profile_sync_updates_for_entry(1, '', 'firefox'), {})
        self.assertEqual(self.h._profile_sync_updates_for_entry(1, '/p', None), {})
        read.assert_not_called()

    def test_profile_sync_read_error_yields_nothing(self):
        self._patch('read_profile_settings', side_effect=json.JSONDecodeError('bad', 'x', 0))
        self.assertEqual(self.h._profile_sync_updates_for_entry(1, '/p', 'Firefox'), {})

    def test_profile_sync_returns_managed_keys_and_encoded_state(self):
        self._patch('read_profile_settings', return_value={'raw': True})
        self._patch('normalize_option_dict', return_value={'Managed': 'true', 'Mode': 'app', 'Foreign': 'x'})
        self._patch('browser_managed_option_keys', return_value={'Managed', 'Mode'})
        self._patch('mode_option_keys', return_value={'Mode'})
        self._patch('browser_state_key', side_effect=lambda family: f'state:{family}')
        encode = self._patch('encode_browser_state', return_value='ENC')
        self.h._options_cache[1] = {'Managed': 'false', 'Other': '1'}
        updates = self.h._profile_sync_updates_for_entry(1, ' /p ', 'firefox')
        self.assertEqual(updates, {'Managed': 'true', 'state:firefox': 'ENC'})
        encode.assert_called_once_with({'Managed': 'true', 'Other': '1'}, 'firefox')

    def test_profile_sync_without_managed_keys(self):
        self._patch('read_profile_settings', return_value={})
        self._patch('normalize_option_dict', return_value={'Foreign': 'x'})
        self.assertEqual(self.h._profile_sync_updates_for_entry(1, '/p', 'chrome'), {})

    def test_startup_cleanup_runs_once_with_active_paths(self):
        cache = self._patch('profile_size_cache')
        rename = self._patch('rename_unused_managed_profile_directories')
        h = _Harness([Entry(1, 'a'), Entry(2, 'b')], {1: {PROFILE_PATH_KEY: ' /p1 '}, 2: {}})
        h._run_startup_profile_cleanup()
        h._run_startup_profile_cleanup()
        cache.flush.assert_called_once_with(['/p1'])
        rename.assert_called_once_with(['/p1'], entries_mod.LOG)

    def test_finalize_reconcile_reloads_only_when_dirty(self):
        h = _Harness()
        h._reload_entries = mock.MagicMock()
        h._run_startup_profile_cleanup = mock.MagicMock()
        h._start_startup_profile_size_sync = mock.MagicMock()
        h._finalize_startup_reconcile()
        h._reload_entries.assert_not_called()
        h._reconcile_dirty = True
        h._finalize_startup_reconcile()
        h._reload_entries.assert_called_once_with()
        self.assertFalse(h._reconcile_dirty)
        self.assertEqual(h._start_startup_profile_size_sync.call_count, 2)
        self.assertEqual(h._run_startup_profile_cleanup.call_count, 2)


# --------------------------------------------------------------------------
# Importing desktop files into the database
# --------------------------------------------------------------------------

class UpsertEntryFromFileTests(_PatchMixin, unittest.TestCase):
    def setUp(self):
        self._patch('ENGINES', ENGINES)
        self.export = self._patch('export_desktop_file', return_value=None)
        self.inspect = self._patch('inspect_profile_copy_source')
        self.read = self._patch('read_profile_settings', return_value={})
        self.h = _Harness()
        self.h._resolve_import_icon_reference = mock.MagicMock(return_value='')
        self.h.db.get_options_for_entry.return_value = []

    def test_failed_insert_stops_early(self):
        self.h.db.add_entry.return_value = None
        self.h._upsert_entry_from_file({'title': 'X'})
        self.h.db.update_entry.assert_not_called()
        self.assertEqual(self.h.entries_store.ids(), [])

    def test_new_entry_collects_all_options(self):
        self.h.db.add_entry.return_value = 5
        self.h._resolve_import_icon_reference.return_value = '/icons/5.png'
        self.inspect.return_value = {'valid': True, 'profile_path': '/prof', 'profile_name': 'webapp_x'}
        data = {
            'title': ' Mail ', 'active': False, 'address': 'https://mail',
            'engine_id': 1, 'user_agent_name': 'UA', 'user_agent_value': 'Mozilla',
            'profile_path': '/src', 'options': {DESKTOP_NAME_SOURCE_KEY: 'Description', 'Kiosk': 'true', APP_MODE_KEY: 'false'},
        }
        self.h._upsert_entry_from_file(data)
        self.h.db.add_entry.assert_called_once_with('Mail', '')
        options = self.h._options_cache[5]
        self.assertEqual(options[ADDRESS_KEY], 'https://mail')
        self.assertEqual(options[ICON_PATH_KEY], '/icons/5.png')
        self.assertEqual(options['EngineID'], '1')
        self.assertEqual(options['EngineName'], 'Firefox')
        self.assertEqual(options[USER_AGENT_NAME_KEY], 'UA')
        self.assertEqual(options[USER_AGENT_VALUE_KEY], 'Mozilla')
        self.assertEqual(options[DESKTOP_NAME_SOURCE_KEY], 'description')
        self.assertEqual(options[PROFILE_PATH_KEY], '/prof')
        self.assertEqual(options[PROFILE_NAME_KEY], 'webapp_x')
        self.assertEqual(options['Kiosk'], 'true')
        self.assertEqual(options[APP_MODE_KEY], 'false')
        self.inspect.assert_called_once_with('/src', 'firefox', entries_mod.LOG)
        self.h.db.update_entry.assert_called_once_with(5, title='Mail', active=False)
        entry = self.h._find_entry_by_id(5)
        self.assertFalse(entry.active)
        self.assertIn(('refresh', 5), self.h.calls)

    def test_invalid_profile_source_and_generic_engine(self):
        self.h.db.add_entry.return_value = 6
        self.inspect.return_value = {'valid': False}
        self.h._upsert_entry_from_file({'title': 'A', 'engine_id': 2, 'profile_path': '/src', 'options': {DESKTOP_NAME_SOURCE_KEY: 'bogus'}})
        options = self.h._options_cache[6]
        self.assertEqual(options['EngineName'], 'Chromium')
        self.assertNotIn(PROFILE_PATH_KEY, options)
        self.assertNotIn(DESKTOP_NAME_SOURCE_KEY, options)

    def test_profile_name_used_when_family_unknown(self):
        self.h.db.add_entry.return_value = 7
        self.h._upsert_entry_from_file({'title': 'A', 'engine_name': 'Other', 'profile_path': '/src', 'profile_name': 'plain'})
        options = self.h._options_cache[7]
        self.assertEqual(options['EngineName'], 'Other')
        self.assertEqual(options[PROFILE_NAME_KEY], 'plain')
        self.inspect.assert_not_called()
        self.assertEqual(options[DESKTOP_NAME_SOURCE_KEY], 'title')

    def test_unknown_engine_id_leaves_name_unset(self):
        self.h.db.add_entry.return_value = 8
        self.h._upsert_entry_from_file({'title': 'A', 'engine_id': 42})
        self.assertNotIn('EngineName', self.h._options_cache[8])

    def test_existing_store_entry_is_reused(self):
        existing = Entry(9, 'Old')
        self.h.entries_store.append(existing)
        self.h.db.add_entry.return_value = 9
        self.h._upsert_entry_from_file({'title': 'New'})
        self.assertEqual(self.h.entries_store.ids(), [9])
        self.assertEqual(existing.title, 'New')

    def test_update_existing_entry_exports_and_syncs_profile(self):
        existing = Entry(3, 'Old')
        self.h.entries_store.append(existing)
        self.h._options_cache[3] = {'EngineID': '1', 'EngineName': 'Firefox'}
        self.export.return_value = {'profile_name': 'webapp_3', 'profile_path': '/profiles/webapp_3'}
        self.h._profile_sync_updates_for_entry = mock.MagicMock(return_value={'Synced': '1'})
        page = mock.MagicMock()
        self.h.detail_pages = {3: page}
        self.h._upsert_entry_from_file({'title': 'Renamed'}, existing_entry=existing)
        self.h.db.add_entry.assert_not_called()
        self.export.assert_called_once()
        self.assertEqual(self.export.call_args[0][0], existing)
        options = self.h._options_cache[3]
        self.assertEqual(options[PROFILE_PATH_KEY], '/profiles/webapp_3')
        self.assertEqual(options['Synced'], '1')
        self.h._profile_sync_updates_for_entry.assert_called_once_with(3, '/profiles/webapp_3', 'firefox')
        page.reload_from_db.assert_called_once_with()

    def test_detail_page_reload_errors_are_swallowed(self):
        existing = Entry(3, 'Old')
        self.h.entries_store.append(existing)
        page = mock.MagicMock()
        page.reload_from_db.side_effect = AttributeError
        self.h.detail_pages = {3: page}
        self.h._upsert_entry_from_file({'title': 'x'}, existing_entry=existing)
        page.reload_from_db.assert_called_once_with()

    def test_export_result_with_missing_values_stores_blanks(self):
        existing = Entry(3, 'Old')
        self.h.entries_store.append(existing)
        self.export.return_value = {'profile_name': None}
        self.h._upsert_entry_from_file({'title': 'x'}, existing_entry=existing)
        self.assertEqual(self.h._options_cache[3][PROFILE_PATH_KEY], '')
        self.assertEqual(self.h._options_cache[3][PROFILE_NAME_KEY], '')


class CompareDbAndFileTests(unittest.TestCase):
    def test_detects_mismatch_and_reports_both_sides(self):
        entry = Entry(1, 'Mail', active=True)
        h = _Harness(options={1: {ADDRESS_KEY: 'https://a', 'EngineID': '1', ICON_PATH_KEY: '/i.png'}})
        same, db_values, file_values = h._compare_db_and_file(entry, {'title': 'Mail'})
        self.assertFalse(same)
        self.assertEqual(db_values, file_values)
        self.assertTrue(db_values['icon_path'])
        mismatch, db_values, file_values = h._compare_db_and_file(entry, {'title': 'Mail', 'address': 'https://b', 'engine_id': 2})
        self.assertTrue(mismatch)
        self.assertEqual(file_values['address'], 'https://b')
        self.assertEqual(file_values['engine_id'], '2')
        self.assertEqual(db_values['address'], 'https://a')


# --------------------------------------------------------------------------
# Reconciling desktop files against the database
# --------------------------------------------------------------------------

class StartReconcileTests(_PatchMixin, unittest.TestCase):
    def _run(self, list_kwargs):
        done = threading.Event()
        glib = self._patch('GLib')
        glib.idle_add.side_effect = lambda *args: done.set()
        self._patch('list_managed_desktop_files', **list_kwargs)
        h = _Harness()
        self.assertFalse(h.start_reconcile_desktop_files())
        self.assertTrue(done.wait(5), 'worker never scheduled the reconcile')
        self.assertEqual(h.calls, ['show_startup_busy'])
        return h, glib.idle_add.call_args[0]

    def test_worker_hands_scanned_files_to_main_thread(self):
        h, args = self._run({'return_value': [{'path': '/a.desktop'}]})
        self.assertEqual(args, (h.reconcile_desktop_files, [{'path': '/a.desktop'}]))

    def test_scan_error_reconciles_with_empty_list(self):
        h, args = self._run({'side_effect': OSError('no dir')})
        self.assertEqual(args, (h.reconcile_desktop_files, []))


class ReconcileDesktopFilesTests(_PatchMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = self._tmpdir()
        existing = self.tmp / 'exists.desktop'
        existing.write_text('')
        self.exportable = self._patch('exportable_entry', side_effect=lambda entry, options: entry.id != 6)
        self._patch('get_expected_desktop_path', side_effect=lambda title: {
            'Present': existing, 'Gone': self.tmp / 'gone.desktop', 'NoPath': None,
        }.get(title))
        self.entries = [
            Entry(1, 'Mail'), Entry(2, 'Chat'), Entry(3, 'Dup'), Entry(4, 'Dup'),
            Entry(5, 'Present'), Entry(6, 'Unexportable'), Entry(7, 'Gone'), Entry(8, 'NoPath'),
        ]
        self.h = _Harness(self.entries, {1: {ADDRESS_KEY: 'https://mail'}, 2: {ADDRESS_KEY: 'https://chat'}})
        self.h._show_next_conflict = mock.MagicMock()
        self.h._prompt_detected_desktop_imports = mock.MagicMock()

    def test_classifies_every_kind_of_conflict(self):
        files = [
            {'entry_id': 1, 'title': 'Mail', 'address': 'https://mail'},        # clean match by id
            {'entry_id': '', 'title': 'Chat', 'address': 'https://changed'},   # title match, mismatch
            {'title': 'Dup', 'path': '/dup.desktop'},                          # ambiguous title -> orphan
            {'path': '/untitled.desktop'},                                     # no title -> orphan
        ]
        self.assertFalse(self.h.reconcile_desktop_files(files))
        kinds = [(c['type'], (c.get('entry') or types.SimpleNamespace(id=None)).id) for c in self.h.reconcile_queue]
        self.assertEqual(kinds, [
            ('mismatch', 2), ('orphan_file', None), ('orphan_file', None),
            ('missing_file', 3), ('missing_file', 4), ('missing_file', 7), ('missing_file', 8),
        ])
        mismatch = self.h.reconcile_queue[0]
        self.assertEqual(mismatch['db']['address'], 'https://chat')
        self.assertEqual(mismatch['file_values']['address'], 'https://changed')
        self.h._show_next_conflict.assert_called_once_with()
        self.h._prompt_detected_desktop_imports.assert_not_called()

    def test_unknown_explicit_id_becomes_import(self):
        files = [{'entry_id': 99, 'title': 'Mail', 'path': '/new.desktop'}]
        self.h.reconcile_desktop_files(files)
        self.h._prompt_detected_desktop_imports.assert_called_once_with(files)
        self.h._show_next_conflict.assert_not_called()
        self.assertNotIn('orphan_file', [c['type'] for c in self.h.reconcile_queue])

    def test_scans_when_no_list_given(self):
        scan = self._patch('list_managed_desktop_files', return_value=[])
        self.h.reconcile_desktop_files()
        scan.assert_called_once_with(entries_mod.ENGINES)


class DetectedImportFlowTests(_PatchMixin, unittest.TestCase):
    def setUp(self):
        self._patch('t', side_effect=_fake_t)
        self.h = _Harness()
        self.h._show_next_conflict = mock.MagicMock()
        self.h._reload_entries = mock.MagicMock()

    def test_finish_reports_cancel_success_or_nothing(self):
        h = self.h
        h._import_cancel_requested = True
        self.assertFalse(h._finish_detected_desktop_imports(1, 3, cancelled=True))
        self.assertFalse(h._import_cancel_requested)
        self.assertIn(('toast', 'desktop_detected_import_cancelled|imported=1,total=3', 3200), h.calls)
        h._finish_detected_desktop_imports(2, 2)
        self.assertIn(('toast', 'desktop_detected_import_done|imported=2,total=2', 2800), h.calls)
        h.calls.clear()
        h._finish_detected_desktop_imports(0, 2)
        self.assertEqual(h.calls, ['destroy_progress'])
        self.assertEqual(h._reload_entries.call_count, 3)
        self.assertEqual(h._show_next_conflict.call_count, 3)

    def test_prompt_with_nothing_moves_on(self):
        self.h._prompt_detected_desktop_imports(None)
        self.h._show_next_conflict.assert_called_once_with()

    def test_prompt_accept_starts_import(self):
        self.h._start_detected_desktop_imports = mock.MagicMock()
        self.h._prompt_detected_desktop_imports([{'a': 1}, {'b': 2}])
        self.assertIn(('choice', 'desktop_detected_import_prompt|total=2', False), self.h.calls)
        self.h.choice_callback(True)
        self.h._start_detected_desktop_imports.assert_called_once_with([{'a': 1}, {'b': 2}])
        self.h._show_next_conflict.assert_not_called()

    def test_prompt_decline_reloads_and_moves_on(self):
        self.h._prompt_detected_desktop_imports([{'a': 1}])
        self.h.choice_callback(False)
        self.h._reload_entries.assert_called_once_with()
        self.h._show_next_conflict.assert_called_once_with()


class StartDetectedImportsTests(_PatchMixin, unittest.TestCase):
    def setUp(self):
        self._patch('t', side_effect=_fake_t)
        self.pending = []
        glib = self._patch('GLib')
        glib.idle_add.side_effect = lambda callback, *args: self.pending.append((callback, args))
        self.h = _Harness()
        self.h._show_next_conflict = mock.MagicMock()
        self.h._finish_detected_desktop_imports = mock.MagicMock()

    def _drain(self, limit=100):
        while self.pending and limit:
            callback, args = self.pending.pop(0)
            callback(*args)
            limit -= 1

    def test_empty_list_moves_on(self):
        self.h._start_detected_desktop_imports([])
        self.h._show_next_conflict.assert_called_once_with()
        self.assertEqual(self.pending, [])

    def test_imports_each_file_and_counts_failures_out(self):
        imported = []

        def upsert(file_data):
            if file_data.get('bad'):
                raise ValueError('broken file')
            imported.append(file_data['title'])
        self.h._upsert_entry_from_file = upsert
        items = [{'title': 'A'}, {'path': '/b.desktop', 'bad': True}, {'title': 'C'}]
        self.h._start_detected_desktop_imports(items)
        self.assertIn(('show_progress', 3, 'desktop_detected_import_title', 'desktop_detected_import_found|total=3'), self.h.calls)
        self._drain()
        self.assertEqual(imported, ['A', 'C'])
        self.h._finish_detected_desktop_imports.assert_called_once_with(2, 3, False)
        progress = [c for c in self.h.calls if c[0] == 'progress']
        self.assertIn(('progress', 2, 3, '/b.desktop'), progress)
        self.assertEqual(progress[0], ('progress', 0, 3, ''))

    def test_cancel_stops_before_next_file(self):
        imported = []

        def upsert(file_data):
            imported.append(file_data['title'])
            self.h._import_cancel_requested = True
        self.h._upsert_entry_from_file = upsert
        self.h._start_detected_desktop_imports([{'title': 'A'}, {'title': 'B'}])
        self._drain()
        self.assertEqual(imported, ['A'])
        self.h._finish_detected_desktop_imports.assert_called_once_with(1, 2, True)


class ShowNextConflictTests(_PatchMixin, unittest.TestCase):
    def setUp(self):
        self._patch('t', side_effect=_fake_t)
        self.h = _Harness()
        self.h._finalize_startup_reconcile = mock.MagicMock()

    def test_empty_queue_finalizes(self):
        self.h._show_next_conflict()
        self.h._finalize_startup_reconcile.assert_called_once_with()
        self.assertNotIn('hide_busy', self.h.calls)

    def test_orphan_conflict_prompts_and_imports_on_yes(self):
        file_data = {'path': '/o.desktop', 'title': 'Orphan'}
        self.h.reconcile_queue = [{'type': 'orphan_file', 'file': file_data}]
        self.h._upsert_entry_from_file = mock.MagicMock()
        self.h._show_next_conflict()
        self.assertTrue(self.h._reconcile_dirty)
        self.assertEqual(self.h.calls, ['hide_busy', ('yes_no', 'reconcile_orphan_file|path=/o.desktop,title=Orphan')])
        self.h.yes_no_callback(True)
        self.h._upsert_entry_from_file.assert_called_once_with(file_data)

    def test_missing_conflict_recreates_on_yes(self):
        entry = Entry(2, 'Lost')
        self.h.reconcile_queue = [{'type': 'missing_file', 'entry': entry}]
        export = self._patch('export_desktop_file')
        self.h._options_cache[2] = {'k': 'v'}
        self.h._show_next_conflict()
        self.assertIn(('yes_no', 'reconcile_missing_file|title=Lost'), self.h.calls)
        self.h.yes_no_callback(False)
        export.assert_not_called()
        self.h.yes_no_callback(True)
        export.assert_called_once_with(entry, {'k': 'v'}, entries_mod.ENGINES, entries_mod.LOG)

    def test_mismatch_conflict_uses_file_or_rewrites(self):
        entry = Entry(3, 'Db')
        conflict = {'type': 'mismatch', 'entry': entry, 'file': {'title': 'File'},
                    'db': {'title': 'Db', 'address': 'a'}, 'file_values': {'title': 'File', 'address': 'b'}}
        self.h.reconcile_queue = [conflict]
        self.h._upsert_entry_from_file = mock.MagicMock()
        export = self._patch('export_desktop_file')
        self.h._show_next_conflict()
        self.assertIn(('yes_no', 'reconcile_mismatch|db_address=a,db_title=Db,file_address=b,file_title=File'), self.h.calls)
        self.h.yes_no_callback(True)
        self.h._upsert_entry_from_file.assert_called_once_with({'title': 'File'}, existing_entry=entry)
        export.assert_not_called()
        self.h.yes_no_callback(False)
        export.assert_called_once()

    def test_unknown_conflict_type_is_dropped_silently(self):
        self.h.reconcile_queue = [{'type': 'weird'}]
        self.h._show_next_conflict()
        self.assertEqual(self.h.reconcile_queue, [])
        self.assertEqual(self.h.calls, ['hide_busy'])

    def test_orphan_decline_does_nothing(self):
        self.h._upsert_entry_from_file = mock.MagicMock()
        self.h._handle_orphan_file({'file': {}}, False)
        self.h._upsert_entry_from_file.assert_not_called()


if __name__ == '__main__':
    unittest.main()

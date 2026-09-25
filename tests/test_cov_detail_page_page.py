"""Coverage tests for detail_page/page.py.

The DetailPage constructor is exercised for real: GTK 4 widgets can be built
without a display, so the page is assembled against an in-memory database with
every outside collaborator (app config, engine detection, the custom-asset
library, main-loop scheduling, desktop-file export) replaced. The remaining
methods are driven on that real page, with the side-effecting collaborators
swapped for mocks on the instance.
"""
import logging
import sys
import types
import unittest
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_detail_page_page.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import gi_versions  # noqa: F401, I001 -- pins the typelib versions before gi.repository loads
from gi.repository import Adw, Gdk, GLib, Gtk

import detail_page.page as page_module
from database import Database
from detail_page import DetailPage
from webapp_constants import (
    ADDRESS_KEY,
    COLOR_SCHEME_KEY,
    DEFAULT_ZOOM_KEY,
    DESKTOP_NAME_SOURCE_KEY,
    PROFILE_NAME_KEY,
    PROFILE_PATH_KEY,
)


def _widgets_constructible():
    # Without a display, constructing a widget does not raise -- it
    # segfaults (GTK 4.14 on the CI runner), so check for one first.
    if Gdk.Display.get_default() is None:
        return False
    try:
        Gtk.Box()
        return True
    except Exception:  # noqa: BLE001  # pragma: no cover - depends on the host
        return False


WIDGETS_AVAILABLE = _widgets_constructible()

ENGINES = [
    {'id': 1, 'name': 'Firefox', 'command': 'firefox'},
    {'id': 2, 'name': 'Chromium', 'command': 'chromium'},
]


class _Entry:
    def __init__(self, entry_id=1, title='Test App', description='Desc', active=1):
        self.id = entry_id
        self.title = title
        self.description = description
        self.active = active


def build_page(options=None, entry=None, config=None, engine_available=None, style_manager=mock.DEFAULT, cleanup=None, **kwargs):
    """Construct a real DetailPage against an in-memory database."""
    entry = entry or _Entry()
    db = Database(':memory:')
    if cleanup is not None:
        cleanup(db.conn.close)
    db.cursor.execute(
        'INSERT INTO entries(id, title, description, active) VALUES (?, ?, ?, ?)',
        (entry.id, entry.title, entry.description, entry.active),
    )
    db.conn.commit()
    for key, value in (options or {}).items():
        db.add_option(entry.id, key, value)
    if config is None:
        config = {'engines': ENGINES}
    patches = [
        mock.patch.object(page_module, 'get_app_config', return_value=config),
        mock.patch.object(page_module, 'engine_available', side_effect=engine_available or (lambda engine: True)),
        mock.patch('detail_page.assets.list_custom_assets', return_value=[]),
        mock.patch('detail_page.assets.get_custom_asset', return_value=None),
        mock.patch.object(GLib, 'idle_add', return_value=0),
    ]
    if style_manager is not mock.DEFAULT:
        patches.append(mock.patch.object(Adw.StyleManager, 'get_default', return_value=style_manager))
    for patcher in patches:
        patcher.start()
    try:
        page = DetailPage(entry, db, kwargs.pop('on_back', mock.Mock()), kwargs.pop('on_delete', mock.Mock()), **kwargs)
    finally:
        for patcher in reversed(patches):
            patcher.stop()
    # Never write a .desktop file or start a favicon download from a test.
    page.save_desktop_file = mock.Mock()
    page._maybe_autofetch_icon = mock.Mock()
    return page


class _FakeEntryWidget:
    def __init__(self, text=''):
        self.text = text
        self.set_calls = []

    def get_text(self):
        return self.text

    def set_text(self, value):
        self.set_calls.append(value)
        self.text = value


@unittest.skipUnless(WIDGETS_AVAILABLE, 'GTK widgets cannot be constructed here')
class ConstructorTests(unittest.TestCase):
    def test_defaults_without_stored_options(self):
        page = build_page(cleanup=self.addCleanup)
        self.assertEqual(page.engines_names, ['Firefox', 'Chromium'])
        self.assertEqual(len(page.engine_dropdown_labels), 3)
        self.assertEqual(page.engine_dropdown.get_selected(), 0)
        self.assertEqual(page.address_entry.get_text(), '')
        self.assertEqual(page.color_scheme_dropdown.get_selected(), 0)
        self.assertEqual(page.default_zoom_dropdown.get_selected(), page.default_zoom_values.index('100'))
        self.assertEqual(page.title_entry.get_text(), 'Test App')
        self.assertEqual(page.description_entry.get_text(), 'Desc')
        self.assertEqual(page.header_name_label.get_text(), 'Test App')
        self.assertTrue(page.switch.get_active())
        self.assertEqual(page.page_stack.get_visible_child_name(), 'main')
        self.assertEqual(set(page.desktop_tab_buttons), {'main', 'options', 'css_assets', 'javascript_assets'})
        self.assertIsNotNone(page.page_stack.get_child_by_name('icon'))
        self.assertTrue(page._suspend_change_handlers)

    def test_stored_values_are_reflected_in_the_controls(self):
        page = build_page(options={
            ADDRESS_KEY: 'https://example.org',
            'EngineID': '2',
            COLOR_SCHEME_KEY: ' DARK ',
            DEFAULT_ZOOM_KEY: '150',
            PROFILE_PATH_KEY: '/tmp/profiles/webapp_abc',
        }, cleanup=self.addCleanup)
        self.assertEqual(page.address_entry.get_text(), 'https://example.org')
        self.assertEqual(page.engine_dropdown.get_selected(), 2)
        self.assertEqual(page.color_scheme_dropdown.get_selected(), 1)
        self.assertEqual(page.default_zoom_dropdown.get_selected(), page.default_zoom_values.index('150'))
        self.assertEqual(page.header_profile_label.get_text(), 'webapp_abc')

    def test_unknown_stored_values_fall_back_to_defaults(self):
        page = build_page(options={
            'EngineID': 'not-a-number',
            COLOR_SCHEME_KEY: 'purple',
            DEFAULT_ZOOM_KEY: '333',
        }, cleanup=self.addCleanup)
        self.assertEqual(page.engine_dropdown.get_selected(), 0)
        self.assertEqual(page.color_scheme_dropdown.get_selected(), 0)
        self.assertEqual(page.default_zoom_dropdown.get_selected(), page.default_zoom_values.index('100'))

    def test_unavailable_engines_are_filtered_out(self):
        page = build_page(engine_available=lambda engine: engine['id'] == 2, cleanup=self.addCleanup)
        self.assertEqual(page.engines_names, ['Chromium'])

    def test_missing_engine_config_uses_built_in_defaults(self):
        page = build_page(config={}, cleanup=self.addCleanup)
        self.assertEqual(page.engines_names, ['Firefox', 'Chrome'])

    def test_missing_style_manager_is_tolerated(self):
        page = build_page(style_manager=None, cleanup=self.addCleanup)
        self.assertIsNone(page._style_manager)

    def test_style_manager_connect_failure_is_tolerated(self):
        manager = mock.Mock()
        manager.connect.side_effect = TypeError('no such signal')
        page = build_page(style_manager=manager, cleanup=self.addCleanup)
        self.assertIs(page._style_manager, manager)
        manager.connect.assert_called_once()


@unittest.skipUnless(WIDGETS_AVAILABLE, 'GTK widgets cannot be constructed here')
class SmallHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = build_page(cleanup=cls.addClassCleanup)

    def test_safe_int(self):
        self.assertEqual(self.page._safe_int('7'), 7)
        self.assertEqual(self.page._safe_int(None, default=3), 3)
        self.assertEqual(self.page._safe_int('x', default=-1), -1)

    def test_current_browser_family_follows_the_engine(self):
        page = self.page
        with mock.patch.object(page, '_get_current_engine', return_value={'command': 'firefox'}):
            self.assertEqual(page._current_browser_family(), 'firefox')
        with mock.patch.object(page, '_get_current_engine', return_value=None):
            self.assertEqual(page._current_browser_family(), 'generic')

    def test_looks_ready_for_url_check(self):
        check = self.page._looks_ready_for_url_check
        accepted = [
            'https://example.org',
            'http://192.168.1.10',
            'https://sub.example.co.uk/path',
        ]
        rejected = [
            '', None, 'http://', 'https://', 'https://exa mple.org', 'https://example.',
            'https://example.org:', 'https://example.org?', 'https://example.org#',
            'not a url', 'https://example', 'https://-bad.example.org', 'https://bad-.example.org',
            'https://example.c', 'https://example.o_g',
        ]
        for value in accepted:
            with self.subTest(value=value):
                self.assertTrue(check(value))
        for value in rejected:
            with self.subTest(value=value):
                self.assertFalse(check(value))

    def test_looks_ready_host_rules_behind_the_structural_check(self):
        # is_structurally_valid_url currently rejects every localhost URL, so
        # the localhost shortcut is only reachable with that check relaxed.
        check = self.page._looks_ready_for_url_check
        with mock.patch.object(page_module, 'is_structurally_valid_url', return_value=True):
            self.assertTrue(check('http://localhost:8080'))
            self.assertFalse(check('https://intranet/x'))
            self.assertFalse(check('https://a..example.org'))
            self.assertFalse(check('https://example.org./x'))
            self.assertFalse(check('https:///nohost'))

    def test_looks_ready_rejects_unparsable_urls(self):
        with mock.patch.object(page_module, 'is_structurally_valid_url', return_value=True), \
            mock.patch.object(page_module, 'urlparse', side_effect=ValueError('bad')):
            self.assertFalse(self.page._looks_ready_for_url_check('https://example.org'))

    def test_set_url_status_replaces_the_css_class(self):
        page = self.page
        page._set_url_status('ok', 'url-status-ok')
        self.assertEqual(page.url_status_label.get_text(), 'ok')
        self.assertTrue(page.url_status_label.has_css_class('url-status-ok'))
        page._set_url_status('bad', 'url-status-error')
        self.assertFalse(page.url_status_label.has_css_class('url-status-ok'))
        self.assertTrue(page.url_status_label.has_css_class('url-status-error'))
        page._set_url_status('')
        self.assertEqual(page.url_status_label.get_text(), '')
        self.assertFalse(page.url_status_label.has_css_class('url-status-error'))

    def test_address_is_never_validated_on_open(self):
        self.assertFalse(self.page._should_validate_address_on_open())

    def test_desktop_name_source_normalisation(self):
        page = self.page
        for stored, expected in ((None, 'title'), (' Description ', 'description'), ('bogus', 'title')):
            with self.subTest(stored=stored), mock.patch.object(page, '_get_option_value', return_value=stored):
                self.assertEqual(page._desktop_name_source(), expected)

    def test_profile_display_name_prefers_the_path(self):
        page = self.page
        values = {PROFILE_PATH_KEY: '/x/y/webapp_zz', PROFILE_NAME_KEY: 'named'}
        with mock.patch.object(page, '_get_option_value', side_effect=values.get):
            self.assertEqual(page._profile_display_name(), 'webapp_zz')
        values = {PROFILE_PATH_KEY: '  ', PROFILE_NAME_KEY: ' named '}
        with mock.patch.object(page, '_get_option_value', side_effect=values.get):
            self.assertEqual(page._profile_display_name(), 'named')
        with mock.patch.object(page, '_get_option_value', return_value=None):
            self.assertEqual(page._profile_display_name(), '')


@unittest.skipUnless(WIDGETS_AVAILABLE, 'GTK widgets cannot be constructed here')
class UrlValidationTests(unittest.TestCase):
    def setUp(self):
        self.page = build_page(cleanup=self.addCleanup)
        self.page._update_export_button_state = mock.Mock()
        self.source_remove = mock.patch.object(GLib, 'source_remove').start()
        self.timeouts = []

        def fake_timeout_add(delay, callback):
            self.timeouts.append((delay, callback))
            return len(self.timeouts) + 100

        self.timeout_add = mock.patch.object(GLib, 'timeout_add', side_effect=fake_timeout_add).start()
        self.addCleanup(mock.patch.stopall)

    def test_cancel_initial_validation_removes_the_source(self):
        page = self.page
        page._initial_address_validation_source_id = 42
        page._set_url_status('x', 'dim-label')
        page._maybe_validate_initial_address()
        self.source_remove.assert_called_once_with(42)
        self.assertEqual(page._initial_address_validation_source_id, 0)
        self.assertEqual(page.url_status_label.get_text(), '')
        self.source_remove.reset_mock()
        page._cancel_initial_address_validation()
        self.source_remove.assert_not_called()

    def test_update_url_status_empty_and_not_ready(self):
        page = self.page
        with mock.patch.object(page, '_validate_url_in_background') as validate:
            page._update_url_status('')
            page._update_url_status('https://half')
        validate.assert_not_called()
        self.assertEqual(page.url_status_label.get_text(), '')

    def test_update_url_status_starts_background_check(self):
        page = self.page
        with mock.patch.object(page, '_validate_url_in_background') as validate:
            page._update_url_status('https://example.org')
        validate.assert_called_once_with('https://example.org')
        self.assertTrue(page.url_status_label.has_css_class('dim-label'))
        self.assertNotEqual(page.url_status_label.get_text(), '')

    def test_validate_in_background_posts_the_result_to_the_main_loop(self):
        page = self.page
        started = []

        class _InlineThread:
            def __init__(self, target, daemon):
                self.target = target
                self.daemon = daemon

            def start(self):
                started.append(self.daemon)
                self.target()

        with mock.patch.object(page_module, 'check_origin_status', return_value='ok') as status, \
            mock.patch.object(page_module.threading, 'Thread', _InlineThread), \
            mock.patch.object(GLib, 'idle_add') as idle_add:
            page._validate_url_in_background('https://example.org')
        status.assert_called_once_with('https://example.org')
        idle_add.assert_called_once_with(page._finish_url_validation, 'https://example.org', 'ok')
        self.assertEqual(started, [True])

    def _finish(self, status, value='https://example.org'):
        self.page.address_entry.get_text = lambda: value  # avoid the 'changed' handler
        return self.page._finish_url_validation(value, status)

    def test_finish_ignores_a_stale_value(self):
        page = self.page
        page.address_entry.get_text = lambda: 'https://other.example'
        self.assertFalse(page._finish_url_validation('https://example.org', 'ok'))
        page._update_export_button_state.assert_not_called()

    def test_finish_is_ignored_while_the_upload_dialog_is_open(self):
        page = self.page
        page._icon_upload_dialog_active = True
        page._address_export_after_validation = False
        self.assertFalse(self._finish('ok'))
        page._update_export_button_state.assert_not_called()

    def test_finish_maps_every_status(self):
        page = self.page
        expectations = {
            'ok': ('url-status-ok', True),
            'blocked': ('url-status-warning', True),
            'unverified': ('url-status-warning', True),
            'invalid': ('url-status-error', False),
        }
        for status, (css_class, valid) in expectations.items():
            with self.subTest(status=status):
                page._maybe_autofetch_icon.reset_mock()
                self.assertFalse(self._finish(status))
                self.assertTrue(page.url_status_label.has_css_class(css_class))
                self.assertEqual(page._address_last_validated_value, 'https://example.org' if valid else '')
                self.assertEqual(page._maybe_autofetch_icon.called, valid)
        page.save_desktop_file.assert_not_called()

    def test_finish_exports_once_when_requested(self):
        page = self.page
        page._icon_upload_dialog_active = True
        page._address_export_after_validation = True
        self._finish('ok')
        page.save_desktop_file.assert_called_once_with()
        self.assertFalse(page._address_export_after_validation)

    def test_flush_pending_address_write_persists_the_entry_text(self):
        page = self.page
        page._address_persist_source_id = 9
        page.address_entry.get_text = lambda: ' https://example.org '
        page.db = mock.Mock()
        page._flush_pending_address_option_write()
        self.source_remove.assert_called_once_with(9)
        self.assertEqual(page._address_persist_source_id, 0)
        page.db.add_option.assert_called_once_with(page.entry.id, ADDRESS_KEY, 'https://example.org', commit=True)

    def test_flush_without_an_entry_widget_uses_the_cache(self):
        page = self.page
        page.db = mock.Mock()
        page._options_cache[ADDRESS_KEY] = ' https://cached.example '
        entry_widget = page.address_entry
        del page.address_entry
        try:
            page._flush_pending_address_option_write()
        finally:
            page.address_entry = entry_widget
        self.source_remove.assert_not_called()
        page.db.add_option.assert_called_once_with(page.entry.id, ADDRESS_KEY, 'https://cached.example', commit=True)

    def test_cancel_address_timers_clears_all_three(self):
        page = self.page
        page._address_validation_source_id = 1
        page._address_export_source_id = 2
        page._address_persist_source_id = 3
        page._cancel_address_timers()
        self.assertEqual([c.args[0] for c in self.source_remove.call_args_list], [1, 2, 3])
        self.assertEqual(
            (page._address_validation_source_id, page._address_export_source_id, page._address_persist_source_id),
            (0, 0, 0),
        )

    def test_schedule_address_processing_runs_both_timers(self):
        page = self.page
        page.db = mock.Mock()
        page._schedule_address_processing('https://example.org', export_after_validation=False)
        self.assertFalse(page._address_export_after_validation)
        delays = [delay for delay, _cb in self.timeouts]
        self.assertEqual(delays, [page._address_persist_ms, page._address_debounce_ms])
        persist, validate = (cb for _delay, cb in self.timeouts)
        with mock.patch.object(page, '_update_url_status') as update:
            self.assertFalse(persist())
            self.assertFalse(validate())
        page.db.add_option.assert_called_once_with(page.entry.id, ADDRESS_KEY, 'https://example.org', commit=True)
        update.assert_called_once_with('https://example.org')
        self.assertEqual(page._address_persist_source_id, 0)
        self.assertEqual(page._address_validation_source_id, 0)

    def test_superseded_timers_do_nothing(self):
        page = self.page
        page.db = mock.Mock()
        page._schedule_address_processing('https://old.example')
        stale_persist, stale_validate = (cb for _delay, cb in self.timeouts)
        page._schedule_address_processing('https://new.example')
        with mock.patch.object(page, '_update_url_status') as update:
            self.assertFalse(stale_persist())
            self.assertFalse(stale_validate())
        page.db.add_option.assert_not_called()
        update.assert_not_called()

    def test_trigger_validation_with_empty_value(self):
        page = self.page
        with mock.patch.object(page, '_update_url_status') as update:
            page._trigger_address_validation('', export_after_validation=True)
        update.assert_called_once_with('')
        page._update_export_button_state.assert_called_once_with()
        self.assertTrue(page._address_export_after_validation)
        self.assertEqual(page._address_last_validated_value, '')

    def test_trigger_validation_with_incomplete_value(self):
        page = self.page
        page._set_url_status('stale', 'url-status-ok')
        with mock.patch.object(page, '_schedule_address_processing') as schedule:
            page._trigger_address_validation('https://half')
        schedule.assert_not_called()
        self.assertEqual(page.url_status_label.get_text(), '')
        page._update_export_button_state.assert_called_once_with()

    def test_trigger_validation_debounced_or_immediate(self):
        page = self.page
        with mock.patch.object(page, '_schedule_address_processing') as schedule, \
            mock.patch.object(page, '_update_url_status') as update:
            page._trigger_address_validation(' https://example.org ', export_after_validation=True)
            schedule.assert_called_once_with('https://example.org', export_after_validation=True)
            update.assert_not_called()
            page._trigger_address_validation('https://example.org', debounce=False)
            update.assert_called_once_with('https://example.org')


@unittest.skipUnless(WIDGETS_AVAILABLE, 'GTK widgets cannot be constructed here')
class EntryHandlerTests(unittest.TestCase):
    def setUp(self):
        self.page = build_page(cleanup=self.addCleanup)
        self.page._update_export_button_state = mock.Mock()
        self.page._sync_icon_filename = mock.Mock()
        self.page.db = mock.Mock()

    def test_name_change_persists_and_notifies(self):
        page = self.page
        page.on_title_changed = mock.Mock()
        page.on_name_changed(_FakeEntryWidget('  New Name '))
        self.assertEqual(page.entry.title, 'New Name')
        self.assertEqual(page.header_name_label.get_text(), 'New Name')
        page.db.update_entry.assert_called_once_with(page.entry.id, title='New Name')
        page._sync_icon_filename.assert_called_once_with()
        page.on_title_changed.assert_called_once_with(page.entry)
        page.save_desktop_file.assert_called_once_with()

    def test_name_change_without_title_callback(self):
        page = self.page
        page.on_title_changed = None
        page.on_name_changed(_FakeEntryWidget('X'))
        page.save_desktop_file.assert_called_once_with()

    def test_description_change_exports_only_when_it_names_the_launcher(self):
        page = self.page
        with mock.patch.object(page, '_desktop_name_source', return_value='title'):
            page.on_description_changed(_FakeEntryWidget(' about '))
        self.assertEqual(page.entry.description, 'about')
        page.db.update_entry.assert_called_once_with(page.entry.id, description='about')
        page.save_desktop_file.assert_not_called()
        with mock.patch.object(page, '_desktop_name_source', return_value='description'):
            page.on_description_changed(_FakeEntryWidget('about'))
        page.save_desktop_file.assert_called_once_with()

    def test_name_source_icons_follow_the_selection(self):
        page = self.page
        page.entry.active = True
        pos = Gtk.EntryIconPosition.SECONDARY
        with mock.patch.object(page, '_desktop_name_source', return_value='description'):
            page._update_desktop_name_source_buttons()
        self.assertEqual(page.title_entry.get_icon_name(pos), 'radio-symbolic')
        self.assertEqual(page.description_entry.get_icon_name(pos), 'radio-checked-symbolic')
        self.assertTrue(page.title_entry.get_icon_activatable(pos))

    def test_name_source_icons_hidden_for_inactive_entries(self):
        page = self.page
        page.entry.active = False
        page._update_desktop_name_source_buttons()
        pos = Gtk.EntryIconPosition.SECONDARY
        self.assertIsNone(page.title_entry.get_icon_name(pos))
        self.assertIsNone(page.description_entry.get_icon_name(pos))
        self.assertFalse(page.title_entry.get_icon_activatable(pos))

    def test_name_source_update_skips_missing_entries(self):
        page = self.page
        page.entry.active = True
        title_entry = page.title_entry
        page.title_entry = None
        try:
            page._update_desktop_name_source_buttons()
        finally:
            page.title_entry = title_entry
        self.assertEqual(page.description_entry.get_icon_name(Gtk.EntryIconPosition.SECONDARY), 'radio-symbolic')

    def test_name_source_click(self):
        page = self.page
        with mock.patch.object(page, '_set_option_value') as set_option, \
            mock.patch.object(page, '_update_desktop_name_source_buttons') as update_icons:
            page.on_desktop_name_source_clicked(None, 'nonsense')
            set_option.assert_not_called()
            update_icons.assert_not_called()

            with mock.patch.object(page, '_desktop_name_source', return_value='title'):
                page.on_desktop_name_source_clicked(None, ' Title ')
            set_option.assert_not_called()
            page.save_desktop_file.assert_not_called()
            self.assertEqual(update_icons.call_count, 1)

            with mock.patch.object(page, '_desktop_name_source', return_value='title'):
                page.on_desktop_name_source_clicked(None, 'description')
            set_option.assert_called_once_with(DESKTOP_NAME_SOURCE_KEY, 'description')
            page.save_desktop_file.assert_called_once_with()
            self.assertEqual(update_icons.call_count, 2)
            self.assertEqual(page._update_export_button_state.call_count, 2)

    def test_name_icon_press_only_reacts_to_the_secondary_icon(self):
        page = self.page
        with mock.patch.object(page, 'on_desktop_name_source_clicked') as clicked:
            page.on_desktop_name_icon_pressed(None, Gtk.EntryIconPosition.PRIMARY, 'title')
            clicked.assert_not_called()
            page.on_desktop_name_icon_pressed(None, Gtk.EntryIconPosition.SECONDARY, 'title')
            clicked.assert_called_once_with(None, 'title')

    def test_delete_asks_for_confirmation(self):
        page = self.page
        page.on_delete_callback = mock.Mock()
        with mock.patch.object(page, '_present_choice_dialog') as dialog:
            page.on_delete_clicked('button')
        anchor, _message, on_result = dialog.call_args.args
        self.assertEqual(anchor, 'button')
        self.assertTrue(dialog.call_args.kwargs['destructive'])
        on_result(False)
        page.on_delete_callback.assert_not_called()
        on_result(True)
        page.on_delete_callback.assert_called_once_with(page.entry)


@unittest.skipUnless(WIDGETS_AVAILABLE, 'GTK widgets cannot be constructed here')
class AddressChangedTests(unittest.TestCase):
    def setUp(self):
        self.page = build_page(cleanup=self.addCleanup)
        page = self.page
        page.refresh_icon_page = mock.Mock()
        page._update_export_button_state = mock.Mock()
        page._schedule_address_processing = mock.Mock()
        page._cancel_address_timers = mock.Mock()
        page._update_url_status = mock.Mock()

    def test_normalised_text_is_written_back_first(self):
        widget = _FakeEntryWidget('  https://example.org ')
        self.page.on_address_changed(widget)
        self.assertEqual(widget.set_calls, ['https://example.org'])
        self.page.refresh_icon_page.assert_not_called()

    def test_suspended_processing_only_refreshes(self):
        page = self.page
        page._suspend_address_processing = True
        page._address_export_after_validation = True
        page._address_last_validated_value = 'x'
        page.on_address_changed(_FakeEntryWidget('https://example.org'))
        self.assertEqual(page._options_cache[ADDRESS_KEY], 'https://example.org')
        self.assertFalse(page._address_export_after_validation)
        self.assertEqual(page._address_last_validated_value, '')
        page._schedule_address_processing.assert_not_called()
        page.refresh_icon_page.assert_called_once_with()
        page._update_export_button_state.assert_called_once_with()

    def test_incomplete_address_clears_the_status(self):
        page = self.page
        page._set_url_status('stale', 'url-status-ok')
        page._address_export_after_validation = True
        page.on_address_changed(_FakeEntryWidget('https://half'))
        self.assertEqual(page.url_status_label.get_text(), '')
        self.assertFalse(page._address_export_after_validation)
        page._schedule_address_processing.assert_not_called()
        page.refresh_icon_page.assert_called_once_with()

    def test_complete_address_is_scheduled(self):
        page = self.page
        page.on_address_changed(_FakeEntryWidget('https://example.org'))
        page._schedule_address_processing.assert_called_once_with('https://example.org', export_after_validation=True)
        page._cancel_address_timers.assert_not_called()

    def test_cleared_address_cancels_everything(self):
        page = self.page
        page._address_last_validated_value = 'https://old.example'
        page.on_address_changed(_FakeEntryWidget(''))
        page._schedule_address_processing.assert_called_once_with('', export_after_validation=True)
        page._cancel_address_timers.assert_called_once_with()
        self.assertFalse(page._address_export_after_validation)
        self.assertEqual(page._address_last_validated_value, '')
        page._update_url_status.assert_called_once_with('')


@unittest.skipUnless(WIDGETS_AVAILABLE, 'GTK widgets cannot be constructed here')
class SwipeAndReleaseTests(unittest.TestCase):
    def setUp(self):
        self.page = build_page(cleanup=self.addCleanup)

    def test_swipe_right_goes_back_or_leaves_the_subpage(self):
        page = self.page
        page.on_back = mock.Mock()
        with mock.patch.object(page, 'is_subpage_visible', return_value=False), \
            mock.patch.object(page, 'show_main_page') as show_main:
            page.on_swipe(None, -50, 0)
            page.on_back.assert_not_called()
            page.on_swipe(None, 50, 0)
            page.on_back.assert_called_once_with()
            show_main.assert_not_called()
        with mock.patch.object(page, 'is_subpage_visible', return_value=True), \
            mock.patch.object(page, 'show_main_page') as show_main:
            page.on_swipe(None, 50, 0)
            show_main.assert_called_once_with()
        self.assertEqual(page.on_back.call_count, 1)

    def test_release_resources_stops_pending_work(self):
        page = self.page
        page._icon_page_preview_refresh_source_id = 77
        page._plugin_operation_serial = 4
        page._plugin_operation_in_progress = True
        page._icon_texture_cache = {'a': object()}
        page.db = mock.Mock()
        with mock.patch.object(GLib, 'source_remove') as source_remove, \
            mock.patch.object(page, '_set_inline_busy') as busy:
            page.release_resources()
        source_remove.assert_called_once_with(77)
        self.assertEqual(page._icon_page_preview_refresh_source_id, 0)
        self.assertEqual(page._plugin_operation_serial, 5)
        self.assertFalse(page._plugin_operation_in_progress)
        busy.assert_called_once_with(False)
        self.assertEqual(page._icon_texture_cache, {})
        page.db.add_option.assert_called_once_with(page.entry.id, ADDRESS_KEY, '', commit=True)

    def test_release_resources_without_a_preview_refresh(self):
        page = self.page
        page.db = mock.Mock()
        with mock.patch.object(GLib, 'source_remove') as source_remove:
            page.release_resources()
        source_remove.assert_not_called()


if __name__ == '__main__':
    unittest.main()

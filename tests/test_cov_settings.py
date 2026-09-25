"""Tests for MainWindowSettingsMixin (mainwindow/settings.py).

No real widget tree is built: Gtk/Adw inside the module are replaced with a
recording fake, and a harness class inherits the real mixin methods while the
MainWindow API they call back into is provided as mocks. Custom-asset storage
and the i18n config writers are patched, so nothing touches the user's files.
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
    logger = logging.getLogger(f'test.cov_settings.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

from gi.repository import Gio, GLib

import gi_versions  # noqa: F401
from i18n import t
from mainwindow import settings
from mainwindow.settings import MainWindowSettingsMixin


def _make_widget(*args, **kwargs):
    widget = mock.MagicMock()
    widget.init_args = args
    widget.init_kwargs = kwargs
    widget.children = []
    widget.append.side_effect = widget.children.append
    widget.css = set()
    widget.add_css_class.side_effect = widget.css.add
    return widget


class FakeGtkFactory:
    """Builds a Gtk stand-in whose widget constructors return recording mocks."""

    WIDGETS = ('Label', 'Box', 'Button', 'ScrolledWindow', 'FileDialog')

    def __init__(self):
        self.gtk = mock.MagicMock()
        self.created = {}
        for name in self.WIDGETS:
            getattr(self.gtk, name).side_effect = self._recorder(name)
        self.gtk.DropDown.new_from_strings.side_effect = self._recorder('DropDown')
        self.gtk.GestureSwipe.new.side_effect = self._recorder('GestureSwipe')
        self.gtk.Image.new_from_icon_name.side_effect = self._recorder('Image')

    def _recorder(self, kind):
        def make(*args, **kwargs):
            widget = _make_widget(*args, **kwargs)
            self.created.setdefault(kind, []).append(widget)
            return widget
        return make

    def labels(self):
        return [w.init_kwargs.get('label') for w in self.created.get('Label', [])]

    def swipe_handler(self, index=0):
        gesture = self.created['GestureSwipe'][index]
        name, handler = gesture.connect.call_args[0]
        assert name == 'swipe'
        return handler


class FakeDetailPage:
    def __init__(self, title=''):
        self.entry = SimpleNamespace(title=title)


class SettingsHarness(MainWindowSettingsMixin):
    def __init__(self):
        self.notifications = []
        self._adaptive_split_enabled = False
        self._adaptive_narrow_mode = False
        self.search_visible = False
        self.overview_split_view = None
        self.stack = mock.MagicMock()
        self.header_bar = mock.MagicMock()
        for name in ('search_button', 'refresh_button', 'home_button', 'settings_button',
                     'assets_button', 'add_button', 'delete_button', 'back_button',
                     'list_title_widget'):
            setattr(self, name, mock.MagicMock(name=name))
        self.detail_pages = {}
        self.visible_detail = None
        self.db = mock.MagicMock()
        self.ui_settings = {}
        self.language_setting = 'system'
        self._options_cache = {'stale': True}
        self.entries = {}
        self.options = {}
        for name in ('_remove_overview_page_widget', '_add_overview_detail_page', '_set_overview_detail_visible',
                     '_adaptive_real_detail_visible', '_show_overview_root_page', '_hide_global_toast',
                     'show_list_page', '_save_ui_settings', '_apply_ui_appearance_setting',
                     '_copy_gfile_to_temp_path', '_present_choice_dialog', 'on_export_all_single_file_clicked',
                     '_load_language_setting', '_read_app_version_label'):
            setattr(self, name, mock.MagicMock(name=name))
        self._adaptive_real_detail_visible.return_value = False
        self._read_app_version_label.return_value = '9.9'

    def show_overlay_notification(self, message, timeout_ms=None):
        self.notifications.append((message, timeout_ms))

    def _overview_detail_visible_child(self):
        return self.visible_detail

    def _appearance_value(self):
        return self.ui_settings.get('appearance', 'auto')

    def _entry_by_id(self, entry_id):
        return self.entries.get(entry_id)

    def _get_options_dict(self, entry_id):
        return dict(self.options.get(entry_id, {}))


class GtkPatchedTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGtkFactory()
        patcher = mock.patch.object(settings, 'Gtk', self.fake.gtk)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(settings, 'DetailPage', FakeDetailPage)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.window = SettingsHarness()


# --------------------------------------------------------------------------
# Small builders
# --------------------------------------------------------------------------

class SmallBuilderTests(GtkPatchedTestCase):
    def test_text_block_is_dimmed_by_default(self):
        label = self.window._build_settings_text_block('hello')
        self.assertEqual(label.init_kwargs['label'], 'hello')
        label.set_wrap.assert_called_once_with(True)
        self.assertEqual(label.css, {'dim-label'})

    def test_text_block_can_stay_undimmed(self):
        label = self.window._build_settings_text_block('hello', dim=False)
        self.assertEqual(label.css, set())

    def test_section_has_header_and_one_block_per_line(self):
        section = self.window._build_settings_section('Title', ['a', 'b', 'c'])
        self.assertIn('preferences-group', section.css)
        self.assertEqual([child.init_kwargs['label'] for child in section.children], ['Title', 'a', 'b', 'c'])
        self.assertIn('heading', section.children[0].css)

    def test_navigation_button_wires_the_callback(self):
        callback = mock.MagicMock()
        button = self.window._build_settings_navigation_button('About', 'More info', callback)
        button.connect.assert_called_once_with('clicked', callback)
        self.assertIn('settings-nav-button', button.css)
        row = button.set_child.call_args[0][0]
        text_box, chevron = row.children
        self.assertEqual([c.init_kwargs['label'] for c in text_box.children], ['About', 'More info'])
        self.assertEqual(chevron.init_args, ('go-next-symbolic',))

    def test_labeled_row_puts_label_above_widget(self):
        widget = mock.MagicMock()
        row = self.window._build_settings_labeled_row('Language', widget)
        self.assertEqual(row.children[0].init_kwargs['label'], 'Language')
        self.assertIs(row.children[1], widget)
        widget.set_hexpand.assert_called_once_with(True)


class ClampTests(GtkPatchedTestCase):
    def test_clamp_wraps_child_with_limits(self):
        fake_adw = mock.MagicMock()
        child = object()
        with mock.patch.object(settings, 'Adw', fake_adw):
            clamp = self.window._wrap_page_with_clamp(child, maximum_size=500, tightening_threshold=300)
        self.assertIs(clamp, fake_adw.Clamp.return_value)
        clamp.set_maximum_size.assert_called_once_with(500)
        clamp.set_tightening_threshold.assert_called_once_with(300)
        clamp.set_child.assert_called_once_with(child)

    def test_old_clamp_without_size_setters(self):
        fake_adw = mock.MagicMock()
        fake_adw.Clamp.return_value = mock.MagicMock(spec=['set_hexpand', 'set_valign', 'set_child'])
        child = object()
        with mock.patch.object(settings, 'Adw', fake_adw):
            clamp = self.window._wrap_page_with_clamp(child)
        clamp.set_child.assert_called_once_with(child)

    def test_without_clamp_the_child_is_returned(self):
        child = object()
        with mock.patch.object(settings, 'Adw', SimpleNamespace()):
            self.assertIs(self.window._wrap_page_with_clamp(child), child)


class LanguageRowsTests(GtkPatchedTestCase):
    def test_rows_are_labelled_deduplicated_and_start_with_system(self):
        languages = [
            {'code': 'en', 'name': 'English'},
            {'code': ' DE ', 'name': 'Deutsch'},
            {'code': 'fr', 'name': 'Français'},
            {'code': 'system', 'name': 'System'},
            {'code': '', 'name': 'Empty'},
            {'code': 'xx'},
            {'code': 'fr', 'name': 'Duplicate'},
        ]
        with mock.patch.object(settings, 'available_languages', return_value=languages) as available:
            rows = self.window._available_language_rows()
        available.assert_called_once_with(force_reload=True)
        self.assertEqual(rows, [
            ('system', t('language_system')),
            ('en', t('language_english')),
            ('de', t('language_german')),
            ('fr', 'Français'),
            ('xx', 'XX'),
        ])

    def test_empty_translation_falls_back_to_the_language_name(self):
        with mock.patch.object(settings, 'available_languages', return_value=[{'code': 'en', 'name': 'English'}]), \
                mock.patch.object(settings, 't', side_effect=lambda key, **_: '' if key == 'language_english' else key):
            rows = self.window._available_language_rows()
        self.assertEqual(rows, [('system', 'language_system'), ('en', 'English')])


# --------------------------------------------------------------------------
# Page builders
# --------------------------------------------------------------------------

class SettingsPageTests(GtkPatchedTestCase):
    def setUp(self):
        super().setUp()
        self.window._available_language_rows = lambda: [('system', 'System'), ('en', 'English'), ('de', 'Deutsch')]

    def test_dropdowns_reflect_current_settings(self):
        self.window.ui_settings['appearance'] = 'light'
        self.window.language_setting = ' DE '
        page = self.window._build_settings_page()
        self.assertIs(page, self.fake.created['ScrolledWindow'][0])
        self.window.ui_mode_dropdown.set_selected.assert_called_once_with(2)
        self.window.ui_mode_dropdown.connect.assert_called_once_with('notify::selected', self.window.on_ui_mode_changed)
        self.assertEqual(self.window.language_values, ['system', 'en', 'de'])
        self.window.language_dropdown.set_selected.assert_called_once_with(2)
        self.window.language_dropdown.connect.assert_called_once_with('notify::selected', self.window.on_language_changed)
        self.assertEqual(self.window.language_dropdown.init_args, (['System', 'English', 'Deutsch'],))
        self.assertEqual(self.window.ui_mode_labels, [t('color_scheme_auto'), t('color_scheme_dark'), t('color_scheme_light')])

    def test_unknown_or_empty_language_selects_system(self):
        self.window.language_setting = 'klingon'
        self.window._build_settings_page()
        self.window.language_dropdown.set_selected.assert_called_once_with(0)
        self.window.language_setting = None
        self.window._build_settings_page()
        self.window.language_dropdown.set_selected.assert_called_once_with(0)

    def test_buttons_are_connected(self):
        self.window._build_settings_page()
        buttons = self.fake.created['Button']
        export_button = next(b for b in buttons if b.init_kwargs.get('label') == t('settings_export_all_button'))
        export_button.connect.assert_called_once_with('clicked', self.window.on_export_all_single_file_clicked)
        callbacks = [b.connect.call_args[0][1] for b in buttons if not b.init_kwargs]
        self.assertEqual(callbacks, [self.window.show_about_settings_page, self.window.show_security_privacy_settings_page])

    def test_swipe_right_returns_to_the_list(self):
        self.window._build_settings_page()
        handler = self.fake.swipe_handler()
        handler(None, -50.0, 0.0)
        self.window.show_list_page.assert_not_called()
        handler(None, 50.0, 0.0)
        self.window.show_list_page.assert_called_once_with()


class InfoPagesTests(GtkPatchedTestCase):
    def test_about_page_lists_all_sections_and_version(self):
        self.window._build_about_settings_page()
        labels = self.fake.labels()
        self.assertEqual(labels[0], t('settings_about_title'))
        self.assertIn(t('settings_about_version', version='9.9'), labels)
        for header in ('app', 'profiles', 'integrations', 'repositories', 'exports'):
            self.assertIn(t(f'settings_about_{header}_header'), labels)

    def test_security_page_lists_all_sections(self):
        self.window._build_security_privacy_settings_page()
        labels = self.fake.labels()
        self.assertEqual(labels[0], t('settings_security_privacy_title'))
        for header in ('profiles', 'storage', 'assets', 'addons', 'recommendations'):
            self.assertIn(t(f'settings_security_privacy_{header}_header'), labels)

    def test_info_page_swipes_return_to_the_settings_overview(self):
        self.window._return_to_overview_from_settings_subpage = mock.MagicMock()
        self.window._build_about_settings_page()
        self.window._build_security_privacy_settings_page()
        for index in (0, 1):
            handler = self.fake.swipe_handler(index)
            handler(None, -1.0, 0.0)
            handler(None, 1.0, 0.0)
        self.assertEqual(self.window._return_to_overview_from_settings_subpage.call_count, 2)

    def test_assets_page_builds_list_and_upload_button(self):
        self.window._refresh_assets_settings_list = mock.MagicMock()
        self.window._return_to_overview_from_settings_assets = mock.MagicMock()
        self.window._build_assets_settings_page()
        self.window._refresh_assets_settings_list.assert_called_once_with()
        upload = self.fake.created['Button'][0]
        self.assertEqual(upload.init_kwargs['label'], t('settings_assets_upload_button'))
        upload.connect.assert_called_once_with('clicked', self.window.on_upload_custom_asset_clicked)
        self.assertEqual(self.window.settings_assets_empty_label.init_kwargs['label'], t('settings_assets_empty'))
        handler = self.fake.swipe_handler()
        handler(None, 0.0, 0.0)
        handler(None, 3.0, 0.0)
        self.window._return_to_overview_from_settings_assets.assert_called_once_with()


class FakeContainer:
    def __init__(self, count):
        self.children = []
        previous = None
        for index in range(count):
            child = mock.MagicMock(name=f'child{index}')
            child.get_next_sibling.return_value = None
            if previous is not None:
                previous.get_next_sibling.return_value = child
            self.children.append(child)
            previous = child
        self.removed = []
        self.appended = []

    def get_first_child(self):
        return self.children[0] if self.children else None

    def remove(self, child):
        self.removed.append(child)

    def append(self, child):
        self.appended.append(child)


class RefreshAssetsListTests(GtkPatchedTestCase):
    def test_without_list_widget_nothing_happens(self):
        with mock.patch.object(settings, 'list_custom_assets') as listing:
            self.window._refresh_assets_settings_list()
        listing.assert_not_called()

    def test_existing_rows_are_replaced_by_one_row_per_asset(self):
        box = FakeContainer(2)
        old_children = list(box.children)
        self.window.settings_assets_list = box
        self.window.settings_assets_empty_label = mock.MagicMock()
        self.window._confirm_delete_custom_asset = mock.MagicMock()
        assets = [
            {'id': 'a1', 'name': 'dark.css', 'type': 'css', 'imported_at': 't1'},
            {'id': 'a2', 'name': None, 'type': None, 'imported_at': None},
        ]
        with mock.patch.object(settings, 'list_custom_assets', return_value=assets), \
                mock.patch.object(settings, 'format_asset_date', side_effect=lambda value: f'D({value})'):
            self.window._refresh_assets_settings_list()
        self.assertEqual(box.removed, old_children)
        self.assertEqual(len(box.appended), 2)
        self.window.settings_assets_empty_label.set_visible.assert_called_once_with(False)
        labels = self.fake.labels()
        self.assertEqual(labels, ['dark.css', 'CSS · D(t1)', '', ' · D(None)'])

        delete_button = box.appended[1].children[1]
        self.assertEqual(delete_button.init_kwargs['icon_name'], 'user-trash-symbolic')
        name, handler = delete_button.connect.call_args[0]
        self.assertEqual(name, 'clicked')
        handler(delete_button)
        self.window._confirm_delete_custom_asset.assert_called_once_with(delete_button, 'a2')

    def test_empty_library_shows_the_hint(self):
        self.window.settings_assets_list = FakeContainer(0)
        self.window.settings_assets_empty_label = mock.MagicMock()
        with mock.patch.object(settings, 'list_custom_assets', return_value=[]):
            self.window._refresh_assets_settings_list()
        self.window.settings_assets_empty_label.set_visible.assert_called_once_with(True)

    def test_missing_empty_label_is_tolerated(self):
        box = FakeContainer(0)
        self.window.settings_assets_list = box
        with mock.patch.object(settings, 'list_custom_assets', return_value=[{'id': 'x', 'name': 'x.js', 'type': 'js'}]), \
                mock.patch.object(settings, 'format_asset_date', return_value=''):
            self.window._refresh_assets_settings_list()
        self.assertEqual(len(box.appended), 1)


# --------------------------------------------------------------------------
# Rebuild / translation refresh
# --------------------------------------------------------------------------

class RebuildSettingsPagesTests(GtkPatchedTestCase):
    PAGES = ('settings_page', 'settings_assets_page', 'settings_about_page', 'settings_security_privacy_page')

    def setUp(self):
        super().setUp()
        self.new_pages = {name: mock.MagicMock(name=f'new_{name}') for name in self.PAGES}
        self.window._build_settings_page = lambda: self.new_pages['settings_page']
        self.window._build_assets_settings_page = lambda: self.new_pages['settings_assets_page']
        self.window._build_about_settings_page = lambda: self.new_pages['settings_about_page']
        self.window._build_security_privacy_settings_page = lambda: self.new_pages['settings_security_privacy_page']
        self.old_pages = {name: mock.MagicMock(name=f'old_{name}') for name in self.PAGES}
        for name, page in self.old_pages.items():
            setattr(self.window, name, page)

    def test_without_stack_nothing_is_rebuilt(self):
        del self.window.stack
        self.window._rebuild_settings_page_view()
        self.assertIs(self.window.settings_page, self.old_pages['settings_page'])

    def test_stack_mode_replaces_pages_and_keeps_the_visible_one(self):
        for visible in self.PAGES:
            with self.subTest(visible=visible):
                self.window.stack = mock.MagicMock()
                self.window.stack.get_visible_child_name.return_value = visible
                for name, page in self.old_pages.items():
                    setattr(self.window, name, page)
                self.window._rebuild_settings_page_view()
                removed = [c.args[0] for c in self.window.stack.remove.call_args_list]
                self.assertEqual(removed, [self.old_pages[name] for name in self.PAGES])
                added = [c.args for c in self.window.stack.add_named.call_args_list]
                self.assertEqual(added, [(self.new_pages[name], name) for name in self.PAGES])
                self.window.stack.set_visible_child_name.assert_called_once_with(visible)
                for name in self.PAGES:
                    self.assertIs(getattr(self.window, name), self.new_pages[name])

    def test_stack_mode_with_other_visible_page_and_failing_removal(self):
        self.window.stack.get_visible_child_name.side_effect = AttributeError
        self.window.stack.remove.side_effect = TypeError
        self.window._rebuild_settings_page_view()
        self.window.stack.set_visible_child_name.assert_not_called()
        self.assertEqual(self.window.stack.add_named.call_count, 4)

    def test_first_build_without_old_pages(self):
        for name in self.PAGES:
            delattr(self.window, name)
        self.window.stack.get_visible_child_name.return_value = 'overview_page'
        self.window._rebuild_settings_page_view()
        self.window.stack.remove.assert_not_called()
        self.window.stack.set_visible_child_name.assert_not_called()

    def test_adaptive_mode_restores_the_visible_detail(self):
        titles = {
            'settings_page': t('settings_title'),
            'settings_assets_page': t('settings_assets_title'),
            'settings_about_page': t('settings_about_title'),
            'settings_security_privacy_page': t('settings_security_privacy_title'),
        }
        self.window._adaptive_split_enabled = True
        for visible in self.PAGES:
            with self.subTest(visible=visible):
                for name, page in self.old_pages.items():
                    setattr(self.window, name, page)
                self.window.visible_detail = self.old_pages[visible]
                self.window._remove_overview_page_widget.reset_mock()
                self.window._add_overview_detail_page.reset_mock()
                self.window._set_overview_detail_visible.reset_mock()
                self.window._rebuild_settings_page_view()
                removed = [c.args[0] for c in self.window._remove_overview_page_widget.call_args_list]
                self.assertEqual(removed, [self.old_pages[name] for name in self.PAGES])
                added = [c.args for c in self.window._add_overview_detail_page.call_args_list]
                self.assertEqual(added, [(self.new_pages[name], name) for name in self.PAGES])
                self.window._set_overview_detail_visible.assert_called_once_with(self.new_pages[visible], titles[visible])

    def test_adaptive_mode_with_unrelated_detail_and_failing_removal(self):
        self.window._adaptive_split_enabled = True
        self.window.visible_detail = object()
        self.window._remove_overview_page_widget.side_effect = AttributeError
        self.window._rebuild_settings_page_view()
        self.window._set_overview_detail_visible.assert_not_called()
        self.assertEqual(self.window._add_overview_detail_page.call_count, 4)


class RefreshTranslatedUiTests(GtkPatchedTestCase):
    def setUp(self):
        super().setUp()
        self.window._rebuild_settings_page_view = mock.MagicMock()
        self.window._show_overview_header = mock.MagicMock()
        for name in ('search_entry', 'empty_label', 'busy_label', 'sidebar_navigation_page', 'content_navigation_page'):
            setattr(self.window, name, mock.MagicMock(name=name))

    def test_all_texts_are_refreshed(self):
        self.window._refresh_translated_ui()
        self.window.list_title_widget.set_text.assert_called_once_with(t('app_title'))
        self.window.search_entry.set_placeholder_text.assert_called_once_with(t('search_placeholder'))
        self.window.empty_label.set_text.assert_called_once_with(t('search_empty'))
        self.window.refresh_button.set_tooltip_text.assert_called_once_with(t('resync_profiles_button'))
        self.window.home_button.set_tooltip_text.assert_called_once_with(t('welcome_title'))
        self.window.settings_button.set_tooltip_text.assert_called_once_with(t('settings_title'))
        self.window.busy_label.set_text.assert_called_once_with(t('loading'))
        self.window.sidebar_navigation_page.set_title.assert_called_once_with(t('app_title'))
        self.window.content_navigation_page.set_title.assert_called_once_with(t('app_title'))
        self.window._rebuild_settings_page_view.assert_called_once_with()
        self.window._show_overview_header.assert_called_once_with()

    def test_visible_detail_page_keeps_its_title(self):
        self.window.visible_detail = FakeDetailPage('Mail')
        self.window._refresh_translated_ui()
        self.window.content_navigation_page.set_title.assert_called_once_with('Mail')
        self.window.visible_detail = FakeDetailPage('')
        self.window._refresh_translated_ui()
        self.window.content_navigation_page.set_title.assert_called_with(t('app_title'))

    def test_missing_widgets_do_not_stop_the_refresh(self):
        for name in ('list_title_widget', 'search_entry', 'empty_label', 'refresh_button', 'settings_button',
                     'busy_label', 'sidebar_navigation_page', 'content_navigation_page'):
            delattr(self.window, name)
        self.window._refresh_translated_ui()
        self.window._rebuild_settings_page_view.assert_called_once_with()
        self.window._show_overview_header.assert_called_once_with()


# --------------------------------------------------------------------------
# Custom assets
# --------------------------------------------------------------------------

class AssetsPageNavigationTests(GtkPatchedTestCase):
    def setUp(self):
        super().setUp()
        self.window._refresh_assets_settings_list = mock.MagicMock()
        self.window._show_back_only_header = mock.MagicMock()
        self.window.settings_assets_page = mock.MagicMock()

    def test_narrow_adaptive_mode_keeps_an_open_detail_page(self):
        self.window._adaptive_split_enabled = True
        self.window._adaptive_narrow_mode = True
        self.window._adaptive_real_detail_visible.return_value = True
        self.window.visible_detail = FakeDetailPage('x')
        self.window.show_assets_settings_page()
        self.window._refresh_assets_settings_list.assert_not_called()
        self.window.stack.set_visible_child_name.assert_not_called()

    def test_adaptive_mode_shows_assets_as_detail(self):
        self.window._adaptive_split_enabled = True
        self.window.show_assets_settings_page()
        self.window._refresh_assets_settings_list.assert_called_once_with()
        self.window._set_overview_detail_visible.assert_called_once_with(self.window.settings_assets_page, t('settings_assets_title'))
        self.window.stack.set_visible_child_name.assert_called_once_with('overview_page')

    def test_stack_mode_switches_page(self):
        self.window.show_assets_settings_page()
        self.window._show_back_only_header.assert_called_once_with()
        self.window._refresh_assets_settings_list.assert_called_once_with()
        self.window.stack.set_visible_child_name.assert_called_once_with('settings_assets_page')


class UploadAssetTests(GtkPatchedTestCase):
    def test_upload_opens_a_file_dialog(self):
        self.window.on_upload_custom_asset_clicked(None)
        dialog = self.fake.created['FileDialog'][0]
        self.assertEqual(dialog.init_kwargs['title'], t('settings_assets_upload_dialog_title'))
        dialog.open.assert_called_once_with(self.window, None, self.window._on_upload_custom_asset_selected)

    def test_upload_retries_once_after_a_type_error(self):
        self.fake.gtk.FileDialog.side_effect = None
        dialog = self.fake.gtk.FileDialog.return_value
        dialog.open.side_effect = [TypeError('old binding'), None]
        self.window.on_upload_custom_asset_clicked(None)
        self.assertEqual(dialog.open.call_count, 2)


class UploadAssetSelectedTests(GtkPatchedTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.window._refresh_assets_settings_list = mock.MagicMock()
        self.local = Path(self.tmp.name) / 'theme.css'
        self.local.write_text('body{}', encoding='utf-8')

    def test_dismissed_dialog_does_nothing(self):
        dialog = mock.MagicMock()
        dialog.open_finish.side_effect = GLib.Error('dismissed')
        self.window._on_upload_custom_asset_selected(dialog, 'result')
        self.window._copy_gfile_to_temp_path.assert_not_called()
        self.assertEqual(self.window.notifications, [])

    def test_local_file_is_imported_in_place(self):
        file_obj = Gio.File.new_for_path(str(self.local))
        self.window._copy_gfile_to_temp_path.return_value = self.local
        with mock.patch.object(settings, 'import_custom_asset', return_value={'name': 'theme.css'}) as importer:
            self.window._on_upload_custom_asset_selected(None, file_obj)
        self.window._copy_gfile_to_temp_path.assert_called_once_with(file_obj, suffix='.css')
        importer.assert_called_once_with(self.local)
        self.window._refresh_assets_settings_list.assert_called_once_with()
        self.assertEqual(self.window.notifications, [(t('settings_assets_upload_success', name='theme.css'), 2600)])
        self.assertTrue(self.local.exists())

    def test_dialog_result_is_resolved_and_temp_copy_removed(self):
        temp_copy = Path(self.tmp.name) / 'copy.css'
        temp_copy.write_text('x', encoding='utf-8')
        remote = mock.MagicMock()
        remote.get_path.return_value = None
        dialog = mock.MagicMock()
        dialog.open_finish.return_value = remote
        self.window._copy_gfile_to_temp_path.return_value = temp_copy
        with mock.patch.object(settings, 'import_custom_asset', return_value={'name': None}):
            self.window._on_upload_custom_asset_selected(dialog, 'result')
        self.window._copy_gfile_to_temp_path.assert_called_once_with(remote, suffix='')
        self.assertEqual(self.window.notifications, [(t('settings_assets_upload_success', name=''), 2600)])
        self.assertFalse(temp_copy.exists())

    def test_copy_failure_is_reported(self):
        self.window._copy_gfile_to_temp_path.return_value = None
        with mock.patch.object(settings, 'import_custom_asset') as importer:
            self.window._on_upload_custom_asset_selected(None, Gio.File.new_for_path(str(self.local)))
        importer.assert_not_called()
        self.assertEqual(self.window.notifications, [(t('settings_assets_upload_failed'), 3200)])

    def test_rejected_asset_is_reported_and_cleanup_failure_tolerated(self):
        temp_copy = Path(self.tmp.name) / 'copy.css'
        remote = mock.MagicMock()
        remote.get_path.return_value = None
        self.window._copy_gfile_to_temp_path.return_value = temp_copy
        with mock.patch.object(settings, 'import_custom_asset', side_effect=ValueError('unsupported type')), \
                mock.patch.object(Path, 'unlink', side_effect=OSError('read-only')) as unlink, \
                self.assertLogs(settings.LOG, level='WARNING'):
            dialog = mock.MagicMock()
            dialog.open_finish.return_value = remote
            self.window._on_upload_custom_asset_selected(dialog, 'result')
        unlink.assert_called_once_with(missing_ok=True)
        self.assertEqual(self.window.notifications, [(t('settings_assets_upload_failed'), 3200)])


class DeleteAssetTests(GtkPatchedTestCase):
    def test_confirm_for_unknown_asset_does_nothing(self):
        with mock.patch.object(settings, 'list_custom_assets', return_value=[{'id': 'other'}]):
            self.window._confirm_delete_custom_asset(None, 'missing')
        self.window._present_choice_dialog.assert_not_called()

    def test_confirm_shows_usage_count_and_deletes_on_yes(self):
        self.window._delete_custom_asset = mock.MagicMock()
        with mock.patch.object(settings, 'list_custom_assets', return_value=[{'id': 'a1', 'name': 'dark.css'}]), \
                mock.patch.object(settings, 'count_asset_references', return_value=3) as counter:
            self.window._confirm_delete_custom_asset(None, 'a1')
        counter.assert_called_once_with(self.window.db, 'a1')
        message, callback = self.window._present_choice_dialog.call_args[0]
        self.assertEqual(message, t('settings_assets_delete_confirm', name='dark.css', count=3))
        self.assertTrue(self.window._present_choice_dialog.call_args.kwargs['destructive'])
        callback(False)
        self.window._delete_custom_asset.assert_not_called()
        callback(True)
        self.window._delete_custom_asset.assert_called_once_with('a1')

    def test_confirm_with_nameless_asset(self):
        with mock.patch.object(settings, 'list_custom_assets', return_value=[{'id': 'a1'}]), \
                mock.patch.object(settings, 'count_asset_references', return_value=0):
            self.window._confirm_delete_custom_asset(None, 'a1')
        self.assertEqual(self.window._present_choice_dialog.call_args[0][0], t('settings_assets_delete_confirm', name='', count=0))

    def test_delete_of_already_missing_asset_stops_early(self):
        self.window._refresh_assets_settings_list = mock.MagicMock()
        with mock.patch.object(settings, 'detach_asset_from_entries', return_value=[1]), \
                mock.patch.object(settings, 'remove_custom_asset', return_value=None), \
                mock.patch.object(settings, 'export_desktop_file') as exporter:
            self.window._delete_custom_asset('a1')
        exporter.assert_not_called()
        self.assertEqual(self.window._options_cache, {'stale': True})
        self.assertEqual(self.window.notifications, [])

    def test_delete_reexports_affected_launchers(self):
        self.window._refresh_assets_settings_list = mock.MagicMock()
        known = SimpleNamespace(id=1, title='Known')
        self.window.entries = {1: known}
        self.window.options = {1: {'a': '1'}, 2: {'b': '2'}, 4: {'c': '3'}}
        rows = {2: (2, 'From DB', None, 1), 3: None, 4: (4, None, 'd', 0)}
        self.window.db.get_entry.side_effect = rows.get
        exported = []
        with mock.patch.object(settings, 'detach_asset_from_entries', return_value=[1, 2, 3, 4]) as detach, \
                mock.patch.object(settings, 'remove_custom_asset', return_value={'name': 'dark.css'}), \
                mock.patch.object(settings, 'exportable_entry', side_effect=lambda entry, options: entry.id != 4), \
                mock.patch.object(settings, 'export_desktop_file', side_effect=lambda entry, options, engines, log: exported.append((entry, options))):
            self.window._delete_custom_asset('a1')
        detach.assert_called_once_with(self.window.db, 'a1')
        self.assertEqual(self.window._options_cache, {})
        self.assertEqual(len(exported), 2)
        self.assertIs(exported[0][0], known)
        self.assertEqual(exported[0][1], {'a': '1'})
        rebuilt = exported[1][0]
        self.assertEqual((rebuilt.id, rebuilt.title, rebuilt.description, rebuilt.active), (2, 'From DB', '', True))
        self.window._refresh_assets_settings_list.assert_called_once_with()
        self.assertEqual(self.window.notifications, [(t('settings_assets_delete_success', name='dark.css'), 2600)])

    def test_delete_notification_without_name(self):
        self.window._refresh_assets_settings_list = mock.MagicMock()
        with mock.patch.object(settings, 'detach_asset_from_entries', return_value=[]), \
                mock.patch.object(settings, 'remove_custom_asset', return_value={}):
            self.window._delete_custom_asset('a1')
        self.assertEqual(self.window.notifications, [(t('settings_assets_delete_success', name=''), 2600)])


# --------------------------------------------------------------------------
# Appearance and language
# --------------------------------------------------------------------------

class UiModeChangedTests(GtkPatchedTestCase):
    def setUp(self):
        super().setUp()
        self.window.ui_mode_values = ['auto', 'dark', 'light']
        self.window.ui_mode_labels = ['Auto', 'Dark', 'Light']

    def _dropdown(self, index):
        dropdown = mock.MagicMock()
        dropdown.get_selected.return_value = index
        return dropdown

    def test_out_of_range_selection_is_ignored(self):
        for index in (-1, 3, 4294967295):
            self.window.on_ui_mode_changed(self._dropdown(index), None)
        self.window._save_ui_settings.assert_not_called()
        self.assertEqual(self.window.ui_settings, {})

    def test_selection_is_saved_and_applied(self):
        self.window.on_ui_mode_changed(self._dropdown(1), None)
        self.assertEqual(self.window.ui_settings['appearance'], 'dark')
        self.window._save_ui_settings.assert_called_once_with()
        self.window._apply_ui_appearance_setting.assert_called_once_with()
        self.assertEqual(self.window.notifications, [(t('settings_ui_changed', mode='Dark'), 2200)])


class LanguageChangedTests(GtkPatchedTestCase):
    def setUp(self):
        super().setUp()
        self.window.language_values = ['system', 'en', 'de']
        self.window._refresh_translated_ui = mock.MagicMock()
        self.window._available_language_rows = lambda: [('system', 'System'), ('en', 'English'), ('de', 'Deutsch')]

    def _dropdown(self, index):
        dropdown = mock.MagicMock()
        dropdown.get_selected.return_value = index
        return dropdown

    def test_out_of_range_or_unchanged_selection_is_ignored(self):
        with mock.patch.object(settings, 'save_app_config') as saver:
            self.window.on_language_changed(self._dropdown(7), None)
            self.window.on_language_changed(self._dropdown(0), None)
            self.window.language_setting = None
            self.window.on_language_changed(self._dropdown(0), None)
            del self.window.language_values
            self.window.on_language_changed(self._dropdown(0), None)
        saver.assert_not_called()

    def test_new_language_is_saved_and_ui_refreshed(self):
        self.window._load_language_setting.return_value = 'de'
        with mock.patch.object(settings, 'get_app_config', return_value={'other': 1}) as getter, \
                mock.patch.object(settings, 'save_app_config') as saver, \
                mock.patch.object(settings, 'invalidate_i18n_cache') as invalidate:
            self.window.on_language_changed(self._dropdown(2), None)
        getter.assert_called_once_with(force_reload=True)
        saver.assert_called_once_with({'other': 1, 'language': 'de'})
        invalidate.assert_called_once_with(reload_config=True)
        self.assertEqual(self.window.language_setting, 'de')
        self.window._refresh_translated_ui.assert_called_once_with()
        self.assertEqual(self.window.notifications, [(t('settings_language_changed', language='Deutsch'), 2200)])

    def test_missing_config_starts_from_empty(self):
        with mock.patch.object(settings, 'get_app_config', return_value=None), \
                mock.patch.object(settings, 'save_app_config') as saver, \
                mock.patch.object(settings, 'invalidate_i18n_cache'):
            self.window.on_language_changed(self._dropdown(1), None)
        saver.assert_called_once_with({'language': 'en'})

    def test_save_failure_is_logged(self):
        with mock.patch.object(settings, 'get_app_config', return_value={}), \
                mock.patch.object(settings, 'save_app_config', side_effect=OSError('read-only')), \
                mock.patch.object(settings, 'invalidate_i18n_cache') as invalidate, \
                self.assertLogs(settings.LOG, level='ERROR'):
            self.window.on_language_changed(self._dropdown(1), None)
        invalidate.assert_not_called()
        self.window._refresh_translated_ui.assert_not_called()
        self.assertEqual(self.window.notifications, [])


# --------------------------------------------------------------------------
# Header bar and navigation
# --------------------------------------------------------------------------

def _visibility(window):
    names = ('search_button', 'refresh_button', 'home_button', 'settings_button',
             'assets_button', 'add_button', 'delete_button', 'back_button')
    return {name: getattr(window, name).set_visible.call_args[0][0] for name in names}


class HeaderTests(GtkPatchedTestCase):
    def test_titlebar_buttons(self):
        self.window._set_titlebar_button_visibility(1, 0)
        self.window.header_bar.set_show_start_title_buttons.assert_called_once_with(True)
        self.window.header_bar.set_show_end_title_buttons.assert_called_once_with(False)

    def test_titlebar_button_failure_is_logged_at_debug(self):
        self.window.header_bar.set_show_start_title_buttons.side_effect = AttributeError
        with self.assertLogs(settings.LOG, level='DEBUG'):
            self.window._set_titlebar_button_visibility(True, True)

    def test_back_only_header_on_desktop_keeps_actions(self):
        self.window._adaptive_split_enabled = True
        page = object()
        self.window.detail_pages = {1: page}
        self.window.visible_detail = page
        self.window._show_back_only_header()
        self.window.header_bar.set_title_widget.assert_called_once_with(None)
        self.assertEqual(_visibility(self.window), {
            'search_button': True, 'refresh_button': True, 'home_button': True, 'settings_button': True,
            'assets_button': True, 'add_button': True, 'delete_button': True, 'back_button': False,
        })

    def test_back_only_header_on_mobile_hides_actions(self):
        self.window._show_back_only_header()
        self.assertEqual(_visibility(self.window), {
            'search_button': False, 'refresh_button': False, 'home_button': False, 'settings_button': False,
            'assets_button': False, 'add_button': False, 'delete_button': False, 'back_button': True,
        })
        self.window.header_bar.set_show_end_title_buttons.assert_called_with(True)

    def test_back_only_header_without_overview_helper(self):
        # A window that has not built the overview yet has neither the helper nor detail pages.
        class BareWindow(MainWindowSettingsMixin):
            pass

        window = BareWindow()
        window._adaptive_split_enabled = False
        window._adaptive_narrow_mode = False
        window.header_bar = mock.MagicMock()
        for name in ('search_button', 'refresh_button', 'home_button', 'settings_button',
                     'assets_button', 'add_button', 'delete_button', 'back_button'):
            setattr(window, name, mock.MagicMock())
        window._show_back_only_header()
        self.assertFalse(_visibility(window)['delete_button'])
        self.assertTrue(_visibility(window)['back_button'])

    def test_overview_header_during_search_uses_back_only(self):
        self.window.search_visible = True
        self.window._show_back_only_header = mock.MagicMock()
        self.window._show_overview_header()
        self.window._show_back_only_header.assert_called_once_with()

    def test_overview_header_with_real_detail_uses_back_only(self):
        self.window._adaptive_split_enabled = True
        self.window.overview_split_view = object()
        self.window._adaptive_real_detail_visible.return_value = True
        self.window._show_back_only_header = mock.MagicMock()
        self.window._show_overview_header()
        self.window._show_back_only_header.assert_called_once_with()

    def test_overview_header_in_split_view(self):
        self.window._adaptive_split_enabled = True
        self.window.overview_split_view = object()
        self.window._show_overview_header()
        self.window.header_bar.set_title_widget.assert_called_once_with(None)
        visibility = _visibility(self.window)
        self.assertTrue(visibility['home_button'])
        self.assertFalse(visibility['delete_button'])
        self.assertFalse(visibility['back_button'])

    def test_overview_header_in_single_pane(self):
        self.window._adaptive_split_enabled = True
        self.window._adaptive_narrow_mode = True
        self.window._restore_overview_header_actions()
        self.window.header_bar.set_title_widget.assert_called_once_with(self.window.list_title_widget)
        visibility = _visibility(self.window)
        self.assertFalse(visibility['home_button'])
        self.assertTrue(visibility['search_button'])
        self.assertTrue(visibility['add_button'])


class ReturnNavigationTests(GtkPatchedTestCase):
    def setUp(self):
        super().setUp()
        self.window._restore_overview_header_actions = mock.MagicMock()
        self.window._show_back_only_header = mock.MagicMock()
        self.window.settings_page = mock.MagicMock()

    def test_return_from_assets_in_adaptive_mode(self):
        self.window._adaptive_split_enabled = True
        self.window.stack.set_visible_child_name.side_effect = GLib.Error('no child')
        self.window._return_to_overview_from_settings_assets()
        self.window._hide_global_toast.assert_called_once_with()
        self.window._show_overview_root_page.assert_called_once_with()
        self.window._restore_overview_header_actions.assert_called_once_with()

    def test_return_from_assets_in_stack_mode(self):
        self.window._return_to_overview_from_settings_assets()
        self.window._show_overview_root_page.assert_not_called()
        self.window._restore_overview_header_actions.assert_called_once_with()
        self.window.stack.set_visible_child_name.assert_called_once_with('overview_page')
        self.window.stack.set_visible_child_name.side_effect = TypeError
        self.window._return_to_overview_from_settings_assets()

    def test_return_from_subpage_in_adaptive_mode(self):
        self.window._adaptive_split_enabled = True
        self.window._return_to_overview_from_settings_subpage()
        self.window._set_overview_detail_visible.assert_called_once_with(self.window.settings_page, t('settings_title'))
        self.window.stack.set_visible_child_name.assert_called_once_with('overview_page')
        self.window._restore_overview_header_actions.assert_called_once_with()
        self.window.stack.set_visible_child_name.side_effect = AttributeError
        self.window._return_to_overview_from_settings_subpage()

    def test_return_from_subpage_in_stack_mode(self):
        self.window._return_to_overview_from_settings_subpage()
        self.window._show_back_only_header.assert_called_once_with()
        self.window.stack.set_visible_child_name.assert_called_once_with('settings_page')
        self.window.stack.set_visible_child_name.side_effect = GLib.Error('x')
        self.window._return_to_overview_from_settings_subpage()


class ShowSettingsPagesTests(GtkPatchedTestCase):
    def setUp(self):
        super().setUp()
        self.window._show_back_only_header = mock.MagicMock()
        for name in ('settings_page', 'settings_about_page', 'settings_security_privacy_page'):
            setattr(self.window, name, mock.MagicMock(name=name))

    def test_settings_page_is_blocked_by_an_open_detail_on_phones(self):
        self.window._adaptive_split_enabled = True
        self.window._adaptive_narrow_mode = True
        self.window._adaptive_real_detail_visible.return_value = True
        self.window.visible_detail = FakeDetailPage()
        self.window.show_settings_page()
        self.window._set_overview_detail_visible.assert_not_called()

    def test_adaptive_pages_open_as_detail(self):
        self.window._adaptive_split_enabled = True
        self.window.show_settings_page()
        self.window.show_about_settings_page()
        self.window.show_security_privacy_settings_page()
        self.assertEqual(self.window._set_overview_detail_visible.call_args_list, [
            mock.call(self.window.settings_page, t('settings_title')),
            mock.call(self.window.settings_about_page, t('settings_about_title')),
            mock.call(self.window.settings_security_privacy_page, t('settings_security_privacy_title')),
        ])
        self.assertEqual({c.args[0] for c in self.window.stack.set_visible_child_name.call_args_list}, {'overview_page'})
        self.window._show_back_only_header.assert_not_called()

    def test_stack_pages_switch_by_name(self):
        self.window.show_settings_page()
        self.window.show_about_settings_page()
        self.window.show_security_privacy_settings_page()
        self.assertEqual([c.args[0] for c in self.window.stack.set_visible_child_name.call_args_list],
                         ['settings_page', 'settings_about_page', 'settings_security_privacy_page'])
        self.assertEqual(self.window._show_back_only_header.call_count, 3)


if __name__ == '__main__':
    unittest.main()

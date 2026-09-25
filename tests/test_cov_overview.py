"""Tests for mainwindow/overview.py (MainWindowOverviewMixin).

Following the pattern of test_plugin_feedback.py, a harness class borrows the
real mixin methods and hand-implements the window state and the callbacks
they reach into (header helpers, busy spinner, entry cache ...). The module's
GLib, DetailPage and -- where widgets are built -- Gtk/Adw references are
replaced with fakes, so nothing here needs a display or a main loop.
"""
import logging
import sys
import types
import unittest
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_overview.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

from app_identity import APP_VERSION
from app_models import Entry
from mainwindow import (
    MainWindowOverviewMixin,
    overview,
)
from webapp_constants import ICON_PATH_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY

# The module pins the typelib versions itself; keep the real bindings before any test patches them.
GLib = overview.GLib
Gtk = overview.Gtk

APP_TITLE = overview.t('app_title')
INVALID = Gtk.INVALID_LIST_POSITION


class FakeDetailPage:
    """Stands in for detail_page.DetailPage (patched into the module)."""

    init_error = None

    def __init__(self, entry=None, db=None, **callbacks):
        if FakeDetailPage.init_error is not None:
            raise FakeDetailPage.init_error
        self.entry = entry
        self.db = db
        self.callbacks = callbacks
        self.set_compact_mode_override = mock.Mock()
        self.on_delete_clicked = mock.Mock()
        self.is_subpage_visible = mock.Mock(return_value=False)
        self.show_main_page = mock.Mock()
        self.release_resources = mock.Mock()


class FakeChild:
    def __init__(self, parent=None, name='child'):
        self.parent = parent
        self.name = name

    def get_next_sibling(self):
        siblings = self.parent.children
        index = siblings.index(self)
        return siblings[index + 1] if index + 1 < len(siblings) else None


class FakeContainer:
    def __init__(self, child_count=0):
        self.children = [FakeChild(self, f'c{index}') for index in range(child_count)]

    def get_first_child(self):
        return self.children[0] if self.children else None

    def remove(self, child):
        self.children.remove(child)

    def prepend(self, child):
        self.children.insert(0, child)


class FakeListModel:
    def __init__(self, items=()):
        self.items = list(items)

    def get_item(self, index):
        if 0 <= index < len(self.items):
            return self.items[index]
        return None

    def get_n_items(self):
        return len(self.items)

    def append(self, item):
        self.items.append(item)

    def remove(self, index):
        del self.items[index]


class _Harness(MainWindowOverviewMixin):
    def __init__(self, adaptive=False, narrow=False):
        self._adaptive_split_enabled = adaptive
        self._adaptive_narrow_mode = narrow
        self._adaptive_collapse_condition = 'max-width: 860sp'
        self._adaptive_breakpoint_fallback_id = 0
        self.overview_split_view = mock.Mock() if adaptive else None
        self.content_stack = mock.Mock()
        self.content_stack.get_visible_child.return_value = None
        self.stack = mock.Mock()
        self.stack.get_visible_child_name.return_value = 'overview_page'
        self.detail_placeholder = mock.Mock(name='placeholder')
        self.content_navigation_page = mock.Mock()
        self.delete_button = mock.Mock()
        self.add_button = mock.Mock()
        self.search_visible = False
        self.search_entry = mock.Mock()
        self.search_entry.get_text.return_value = ''
        self.search_text = ''
        self.custom_filter = mock.Mock()
        self.empty_label = mock.Mock()
        self.filtered_model = FakeListModel()
        self.entries_store = FakeListModel()
        self.selection = mock.Mock()
        self.selection.get_selected.return_value = INVALID
        self.detail_pages = {}
        self.db = mock.Mock()
        self.options = {}
        self._creating_entry = False
        self._main_neutral_focus_target = mock.Mock(name='neutral_target')
        # Callbacks provided by the other mixins / the window itself.
        for name in (
            '_show_overview_header', '_show_back_only_header', '_restore_overview_header_actions',
            'launch_entry', '_launch_entry_from_icon', '_schedule_profile_size_refresh',
            '_reposition_entry_in_store', '_invalidate_entry_cache', '_show_busy', '_hide_busy',
            'show_overlay_notification', '_cleanup_detail_pages', '_return_to_overview_from_settings_assets',
            '_return_to_overview_from_settings_subpage', '_hide_global_toast', '_open_import_wapp_dialog',
            'add_breakpoint', 'get_width',
        ):
            setattr(self, name, mock.Mock(name=name))
        self._get_profile_size_text_cached = mock.Mock(return_value='')

    def _get_options_dict(self, entry_id):
        return self.options.get(entry_id, {})

    def show_detail(self, page):
        """Make `page` the visible overview child for the current layout."""
        self.content_stack.get_visible_child.return_value = page
        self.stack.get_visible_child_name.return_value = 'overview_page'
        if self.overview_split_view is not None:
            self.overview_split_view.get_show_content.return_value = True


class _OverviewTestCase(unittest.TestCase):
    def setUp(self):
        self.glib = types.SimpleNamespace(
            Error=GLib.Error,
            idle_add=mock.Mock(return_value=11),
            timeout_add=mock.Mock(return_value=22),
            get_monotonic_time=mock.Mock(return_value=1_000_000),
        )
        for target, value in (('GLib', self.glib), ('DetailPage', FakeDetailPage)):
            patcher = mock.patch.object(overview, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        FakeDetailPage.init_error = None


def _fake_gtk():
    gtk = mock.MagicMock(name='Gtk')
    for name in ('Box', 'Label', 'Button', 'GestureClick'):
        getattr(gtk, name).side_effect = (lambda *_a, _n=name, **_k: mock.MagicMock(name=_n))
    gtk.Image.new_from_icon_name.side_effect = lambda *_a, **_k: mock.MagicMock(name='Image')
    gtk.INVALID_LIST_POSITION = INVALID
    return gtk


class HeaderDeleteButtonTests(_OverviewTestCase):
    def test_visible_detail_page_is_the_delete_target(self):
        harness = _Harness()
        page = FakeDetailPage()
        harness.show_detail(page)
        self.assertIs(harness._header_detail_delete_target(), page)

    def test_non_detail_child_or_hidden_overview_has_no_target(self):
        harness = _Harness()
        harness.show_detail(mock.Mock())
        self.assertIsNone(harness._header_detail_delete_target())
        page = FakeDetailPage()
        harness.show_detail(page)
        harness.stack.get_visible_child_name.return_value = 'settings_page'
        self.assertIsNone(harness._header_detail_delete_target())

    def test_visibility_follows_the_target_when_not_given(self):
        harness = _Harness()
        harness._set_header_detail_delete_visible()
        harness.delete_button.set_visible.assert_called_with(False)
        harness.show_detail(FakeDetailPage())
        harness._set_header_detail_delete_visible()
        harness.delete_button.set_visible.assert_called_with(True)
        harness._set_header_detail_delete_visible(0)
        harness.delete_button.set_visible.assert_called_with(False)

    def test_missing_button_is_ignored(self):
        harness = _Harness()
        harness.delete_button = None
        harness._set_header_detail_delete_visible(True)  # must not raise

    def test_header_delete_forwards_to_the_visible_detail_page(self):
        harness = _Harness()
        button = object()
        harness.on_header_delete_clicked(button)  # no target: nothing happens
        page = FakeDetailPage()
        harness.show_detail(page)
        harness.on_header_delete_clicked(button)
        page.on_delete_clicked.assert_called_once_with(button)


class VisibilityHelperTests(_OverviewTestCase):
    def test_visible_child_is_none_without_content_stack(self):
        harness = _Harness()
        harness.content_stack = None
        self.assertIsNone(harness._overview_detail_visible_child())

    def test_showing_content_requires_adaptive_split(self):
        self.assertFalse(_Harness()._adaptive_overview_showing_content())
        harness = _Harness(adaptive=True)
        harness.overview_split_view.get_show_content.return_value = 1
        self.assertTrue(harness._adaptive_overview_showing_content())
        harness.overview_split_view.get_show_content.side_effect = TypeError
        self.assertFalse(harness._adaptive_overview_showing_content())

    def test_real_detail_visible_excludes_the_placeholder(self):
        harness = _Harness(adaptive=True)
        harness.show_detail(harness.detail_placeholder)
        self.assertFalse(harness._adaptive_real_detail_visible())
        harness.show_detail(FakeDetailPage())
        self.assertTrue(harness._adaptive_real_detail_visible())
        harness.overview_split_view.get_show_content.return_value = False
        self.assertFalse(harness._adaptive_real_detail_visible())

    def test_child_visibility_in_adaptive_mode(self):
        harness = _Harness(adaptive=True)
        page = FakeDetailPage()
        self.assertFalse(harness._is_overview_child_visible(None))
        harness.show_detail(page)
        self.assertTrue(harness._is_overview_child_visible(page))
        self.assertFalse(harness._is_overview_child_visible(FakeDetailPage()))

    def test_child_visibility_without_stack_is_false(self):
        harness = _Harness()
        harness.stack = None
        self.assertFalse(harness._is_overview_child_visible(FakeDetailPage()))

    def test_add_detail_page_names_it_in_the_content_stack(self):
        harness = _Harness()
        page = object()
        harness._add_overview_detail_page(page, 'detail_3')
        harness.content_stack.add_named.assert_called_once_with(page, 'detail_3')
        harness.content_stack = None
        harness._add_overview_detail_page(page, 'detail_3')  # swallowed

    def test_remove_only_touches_children_of_the_content_stack(self):
        harness = _Harness()
        own = mock.Mock()
        own.get_parent.return_value = harness.content_stack
        foreign = mock.Mock()
        foreign.get_parent.return_value = object()
        harness._remove_overview_page_widget(own)
        harness._remove_overview_page_widget(foreign)
        harness._remove_overview_page_widget(None)
        harness.content_stack.remove.assert_called_once_with(own)
        broken = mock.Mock()
        broken.get_parent.side_effect = TypeError
        harness._remove_overview_page_widget(broken)  # swallowed


class PlaceholderAndDetailVisibilityTests(_OverviewTestCase):
    def test_narrow_adaptive_placeholder_hides_the_content_pane(self):
        harness = _Harness(adaptive=True, narrow=True)
        harness._set_overview_placeholder_visible()
        harness.overview_split_view.set_show_content.assert_called_once_with(False)
        harness.content_stack.set_visible_child_name.assert_not_called()
        harness.content_navigation_page.set_title.assert_called_once_with(APP_TITLE)
        harness._show_overview_header.assert_called_once_with()
        harness.delete_button.set_visible.assert_called_once_with(False)

    def test_wide_adaptive_placeholder_shows_placeholder_in_content_pane(self):
        harness = _Harness(adaptive=True)
        harness._set_overview_placeholder_visible()
        harness.content_stack.set_visible_child_name.assert_called_once_with('detail_placeholder')
        harness.overview_split_view.set_show_content.assert_called_once_with(True)

    def test_plain_placeholder_survives_failing_widgets(self):
        harness = _Harness()
        harness.content_stack.set_visible_child_name.side_effect = TypeError
        harness.content_navigation_page = None
        harness._set_overview_placeholder_visible()
        harness._show_overview_header.assert_called_once_with()

    def test_adaptive_split_view_errors_are_swallowed(self):
        narrow = _Harness(adaptive=True, narrow=True)
        narrow.overview_split_view.set_show_content.side_effect = TypeError
        narrow._set_overview_placeholder_visible()
        wide = _Harness(adaptive=True)
        wide.overview_split_view.set_show_content.side_effect = AttributeError
        wide._set_overview_placeholder_visible()
        narrow._show_overview_header.assert_called_once_with()
        wide._show_overview_header.assert_called_once_with()

    def test_detail_visible_in_adaptive_mode_sets_title_and_content(self):
        harness = _Harness(adaptive=True)
        page = FakeDetailPage()
        harness._set_overview_detail_visible(page, 'Mail')
        harness.content_stack.set_visible_child.assert_called_once_with(page)
        harness.content_navigation_page.set_title.assert_called_once_with('Mail')
        harness.overview_split_view.set_show_content.assert_called_once_with(True)
        harness._show_overview_header.assert_called_once_with()
        harness._show_back_only_header.assert_not_called()

    def test_detail_visible_in_adaptive_mode_defaults_title_and_tolerates_errors(self):
        harness = _Harness(adaptive=True)
        harness.content_navigation_page.set_title.side_effect = TypeError
        harness.overview_split_view.set_show_content.side_effect = TypeError
        harness._set_overview_detail_visible(FakeDetailPage())
        harness.content_navigation_page.set_title.assert_called_once_with(APP_TITLE)
        harness._show_overview_header.assert_called_once_with()

    def test_detail_visible_in_stack_mode_uses_back_only_header(self):
        harness = _Harness()
        page = FakeDetailPage()
        harness.show_detail(page)
        harness._set_overview_detail_visible(page, 'Mail')
        harness._show_back_only_header.assert_called_once_with()
        harness.delete_button.set_visible.assert_called_once_with(True)

    def test_failing_switch_leaves_the_header_alone(self):
        harness = _Harness(adaptive=True)
        harness.content_stack.set_visible_child.side_effect = TypeError
        harness._set_overview_detail_visible(FakeDetailPage(), 'Mail')
        harness._show_overview_header.assert_not_called()
        harness._show_back_only_header.assert_not_called()

    def test_root_page_in_adaptive_mode_shows_placeholder(self):
        harness = _Harness(adaptive=True)
        harness._show_overview_root_page()
        harness.content_stack.set_visible_child_name.assert_called_once_with('detail_placeholder')
        self.assertEqual(harness._show_overview_header.call_count, 2)
        harness._show_overview_header.reset_mock()
        harness.search_visible = True
        harness._show_overview_root_page()
        # Only the placeholder helper shows the header, not the search guard.
        self.assertEqual(harness._show_overview_header.call_count, 1)

    def test_root_page_in_stack_mode_shows_list_unless_searching(self):
        harness = _Harness()
        harness._show_overview_root_page()
        harness.content_stack.set_visible_child_name.assert_called_once_with('list_page')
        harness._show_overview_header.assert_called_once_with()
        harness.search_visible = True
        harness.content_stack.set_visible_child_name.side_effect = TypeError
        harness._show_overview_root_page()
        harness._show_overview_header.assert_called_once_with()

    def test_home_resets_stack_and_selection(self):
        harness = _Harness()
        harness.on_home_clicked(None)
        harness.stack.set_visible_child_name.assert_called_once_with('overview_page')
        harness.selection.set_selected.assert_called_once_with(INVALID)
        harness.stack.set_visible_child_name.side_effect = TypeError
        harness.selection = None
        harness.on_home_clicked(None)  # errors swallowed


class AdaptiveBreakpointTests(_OverviewTestCase):
    def test_nothing_is_configured_without_adaptive_split(self):
        harness = _Harness()
        with mock.patch.object(overview, 'Adw') as adw:
            harness._configure_adaptive_breakpoints()
        adw.Breakpoint.new.assert_not_called()
        self.glib.timeout_add.assert_not_called()

    def test_breakpoint_is_created_connected_and_added(self):
        harness = _Harness(adaptive=True, narrow=True)
        with mock.patch.object(overview, 'Adw') as adw:
            harness._configure_adaptive_breakpoints()
        adw.BreakpointCondition.parse.assert_called_once_with('max-width: 860sp')
        breakpoint = adw.Breakpoint.new.return_value
        self.assertIs(harness._adaptive_breakpoint, breakpoint)
        breakpoint.connect.assert_any_call('apply', harness._on_adaptive_breakpoint_apply)
        breakpoint.connect.assert_any_call('unapply', harness._on_adaptive_breakpoint_unapply)
        harness.add_breakpoint.assert_called_once_with(breakpoint)
        harness.overview_split_view.set_collapsed.assert_called_once_with(True)
        harness.overview_split_view.set_show_content.assert_called_once_with(False)
        self.glib.timeout_add.assert_called_once_with(250, harness._adaptive_breakpoint_fallback_tick)
        self.assertEqual(harness._adaptive_breakpoint_fallback_id, 22)

    def test_breakpoint_failure_is_tolerated(self):
        harness = _Harness(adaptive=True)
        harness.overview_split_view.set_collapsed.side_effect = TypeError
        with mock.patch.object(overview, 'Adw') as adw:
            adw.BreakpointCondition.parse.side_effect = GLib.Error('bad condition')
            harness._configure_adaptive_breakpoints()
        self.assertIsNone(harness._adaptive_breakpoint)
        self.glib.timeout_add.assert_called_once()

    def test_fallback_is_not_scheduled_twice_or_when_disabled(self):
        harness = _Harness(adaptive=True)
        harness._adaptive_breakpoint_fallback_id = 5
        harness._schedule_adaptive_breakpoint_fallback()
        _Harness()._schedule_adaptive_breakpoint_fallback()
        self.glib.timeout_add.assert_not_called()

    def test_fallback_tick_reschedules_until_the_window_has_a_width(self):
        harness = _Harness(adaptive=True)
        harness.get_width.return_value = 0
        self.assertFalse(harness._adaptive_breakpoint_fallback_tick())
        self.glib.timeout_add.assert_called_once_with(250, harness._adaptive_breakpoint_fallback_tick)
        self.assertEqual(harness._adaptive_breakpoint_fallback_id, 22)
        harness.get_width.side_effect = ValueError
        harness._adaptive_breakpoint_fallback_tick()
        self.assertEqual(self.glib.timeout_add.call_count, 2)

    def test_fallback_tick_derives_narrow_mode_from_width(self):
        harness = _Harness(adaptive=True)
        harness.get_width.return_value = 860
        self.assertFalse(harness._adaptive_breakpoint_fallback_tick())
        self.assertTrue(harness._adaptive_narrow_mode)
        self.assertEqual(harness._adaptive_breakpoint_fallback_id, 0)
        harness.get_width.return_value = 861
        harness._adaptive_breakpoint_fallback_tick()
        self.assertFalse(harness._adaptive_narrow_mode)

    def test_fallback_tick_stops_when_adaptive_split_is_disabled(self):
        harness = _Harness()
        harness._adaptive_breakpoint_fallback_id = 9
        self.assertFalse(harness._adaptive_breakpoint_fallback_tick())
        self.assertEqual(harness._adaptive_breakpoint_fallback_id, 0)
        harness.get_width.assert_not_called()

    def test_apply_and_unapply_toggle_narrow_mode(self):
        harness = _Harness(adaptive=True)
        harness._on_adaptive_breakpoint_apply(object())
        self.assertTrue(harness._adaptive_narrow_mode)
        harness._on_adaptive_breakpoint_unapply(object())
        self.assertFalse(harness._adaptive_narrow_mode)

    def test_narrow_mode_updates_split_view_and_detail_pages(self):
        harness = _Harness(adaptive=True)
        page = FakeDetailPage()
        harness.detail_pages = {1: page, 2: object()}
        harness.show_detail(page)
        harness._set_adaptive_narrow_mode(True)
        harness.overview_split_view.set_collapsed.assert_called_once_with(True)
        harness.overview_split_view.set_show_content.assert_called_once_with(True)
        page.set_compact_mode_override.assert_called_once_with(True)
        harness._show_overview_header.assert_called_once_with()

    def test_narrow_mode_hides_content_when_only_the_placeholder_is_shown(self):
        harness = _Harness(adaptive=True)
        harness.show_detail(harness.detail_placeholder)
        harness.overview_split_view.set_collapsed.side_effect = TypeError
        harness.overview_split_view.set_show_content.side_effect = TypeError
        harness._set_adaptive_narrow_mode(1)
        self.assertIs(harness._adaptive_narrow_mode, True)

    def test_wide_mode_does_not_touch_show_content(self):
        harness = _Harness(adaptive=True, narrow=True)
        harness._set_adaptive_narrow_mode(False)
        self.assertFalse(harness._adaptive_narrow_mode)
        harness.overview_split_view.set_show_content.assert_not_called()

    def test_narrow_mode_is_ignored_without_split_view(self):
        harness = _Harness()
        harness._set_adaptive_narrow_mode(True)
        self.assertFalse(harness._adaptive_narrow_mode)
        harness._show_overview_header.assert_not_called()

    def test_split_change_refreshes_the_header(self):
        harness = _Harness(adaptive=True)
        harness._on_overview_split_changed(object(), object())
        harness._show_overview_header.assert_called_once_with()


class WidgetBuildingTests(_OverviewTestCase):
    def setUp(self):
        super().setUp()
        self.gtk = _fake_gtk()
        patcher = mock.patch.object(overview, 'Gtk', self.gtk)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_list_title_widget_shows_the_app_title(self):
        label = _Harness()._build_list_title_widget()
        label.set_text.assert_called_once_with(APP_TITLE)
        label.add_css_class.assert_any_call('overview-title')

    def test_welcome_page_buttons_create_or_import_entries(self):
        harness = _Harness()
        harness._create_empty_entry = mock.Mock()
        page = harness._build_welcome_page()
        content = page.append.call_args[0][0]
        actions = content.append.call_args_list[-1][0][0]
        new_button, import_button = [call[0][0] for call in actions.append.call_args_list]
        new_handler = new_button.connect.call_args[0][1]
        import_handler = import_button.connect.call_args[0][1]
        self.assertEqual(new_button.connect.call_args[0][0], 'clicked')
        new_handler(new_button)
        harness._create_empty_entry.assert_called_once_with()
        harness._open_import_wapp_dialog.assert_not_called()
        import_handler(import_button)
        harness._open_import_wapp_dialog.assert_called_once_with()

    def test_factory_setup_builds_and_remembers_the_row_widgets(self):
        harness = _Harness()
        list_item = mock.Mock()
        list_item.set_activatable.side_effect = TypeError
        list_item.set_selectable.side_effect = AttributeError
        with mock.patch.object(overview, 'create_image_from_ref', return_value='placeholder-icon') as make_icon:
            harness.on_factory_setup(object(), list_item)
        make_icon.assert_called_once_with('', pixel_size=28)
        widgets = list_item._overview_widgets
        self.assertEqual(set(widgets), {
            'icon_button', 'icon_frame', 'content_box', 'text_box', 'status_column', 'title_row',
            'subtitle_row', 'title_label', 'status_box', 'description_label', 'active_dot',
            'engine_image', 'profile_size_label',
        })
        self.assertEqual(len({id(widget) for widget in widgets.values()}), len(widgets))
        widgets['icon_frame'].append.assert_called_once_with('placeholder-icon')
        widgets['icon_button'].set_child.assert_called_once_with(widgets['icon_frame'])
        box = list_item.set_child.call_args[0][0]
        self.assertEqual([c[0][0] for c in box.append.call_args_list], [widgets['icon_button'], widgets['content_box']])
        gesture = widgets['content_box'].add_controller.call_args[0][0]
        gesture.connect.assert_called_once_with('released', harness._on_overview_content_released)

    def test_close_event_closes_database_then_window(self):
        harness = _Harness()
        harness.close_event('arg')
        harness.db.close.assert_called_once_with()
        self.gtk.Window.close.assert_called_once_with(harness, 'arg')


class AddDialogTests(_OverviewTestCase):
    def _response_handler(self, dialog):
        signal, handler = dialog.connect.call_args[0]
        self.assertEqual(signal, 'response')
        return handler

    def test_add_is_ignored_while_an_entry_is_being_created(self):
        harness = _Harness()
        harness._present_add_choice_dialog = mock.Mock()
        harness._creating_entry = True
        harness.on_add_entry(None)
        harness._present_add_choice_dialog.assert_not_called()
        harness._creating_entry = False
        harness.on_add_entry(None)
        harness._present_add_choice_dialog.assert_called_once_with()

    def test_alert_dialog_responses_dispatch(self):
        harness = _Harness()
        harness._create_empty_entry = mock.Mock()
        with mock.patch.object(overview, 'Adw') as adw:
            harness._present_add_choice_dialog()
        dialog = adw.AlertDialog.new.return_value
        dialog.present.assert_called_once_with(harness)
        dialog.set_default_response.assert_called_once_with('new')
        dialog.set_close_response.assert_called_once_with('cancel')
        handler = self._response_handler(dialog)
        handler(dialog, 'cancel')
        harness._create_empty_entry.assert_not_called()
        harness._open_import_wapp_dialog.assert_not_called()
        handler(dialog, 'import')
        harness._open_import_wapp_dialog.assert_called_once_with()
        handler(dialog, 'new')
        harness._create_empty_entry.assert_called_once_with()

    def test_message_dialog_fallback_without_alert_dialog(self):
        harness = _Harness()
        harness._create_empty_entry = mock.Mock()
        adw = types.SimpleNamespace(MessageDialog=mock.Mock(), ResponseAppearance=mock.Mock())
        with mock.patch.object(overview, 'Adw', adw):
            harness._present_add_choice_dialog()
        adw.MessageDialog.new.assert_called_once()
        self.assertIs(adw.MessageDialog.new.call_args[0][0], harness)
        dialog = adw.MessageDialog.new.return_value
        dialog.present.assert_called_once_with()
        self._response_handler(dialog)(dialog, 'new')
        harness._create_empty_entry.assert_called_once_with()


class LogoSearchAndFilterTests(_OverviewTestCase):
    def test_logo_launches_the_selected_entry(self):
        harness = _Harness()
        first, second = Entry(1, 'A'), Entry(2, 'B')
        harness.filtered_model = FakeListModel([first, second])
        harness.selection.get_selected.return_value = 1
        harness.on_overview_logo_clicked(None)
        harness.launch_entry.assert_called_once_with(second)

    def test_logo_launches_the_only_entry_without_selection(self):
        harness = _Harness()
        only = Entry(1, 'A')
        harness.filtered_model = FakeListModel([only])
        harness.on_overview_logo_clicked(None)
        harness.launch_entry.assert_called_once_with(only)

    def test_logo_does_nothing_with_several_unselected_entries(self):
        harness = _Harness()
        harness.filtered_model = FakeListModel([Entry(1, 'A'), Entry(2, 'B')])
        harness.on_overview_logo_clicked(None)
        harness.selection.get_selected.return_value = 7  # stale position, no item
        harness.on_overview_logo_clicked(None)
        harness.launch_entry.assert_not_called()

    def test_logo_with_single_empty_slot_does_nothing(self):
        harness = _Harness()
        harness.filtered_model = FakeListModel([None])
        harness.on_overview_logo_clicked(None)
        harness.launch_entry.assert_not_called()

    def test_opening_search_focuses_the_entry_when_allowed(self):
        harness = _Harness()
        with mock.patch.object(overview, 'should_prevent_input_autofocus', return_value=False), \
                mock.patch.object(overview, 'schedule_neutral_focus') as schedule:
            harness.on_search_clicked(None)
        self.assertTrue(harness.search_visible)
        harness.search_entry.set_visible.assert_called_once_with(True)
        harness._show_back_only_header.assert_called_once_with()
        harness.search_entry.grab_focus.assert_called_once_with()
        schedule.assert_not_called()
        harness.custom_filter.changed.assert_not_called()

    def test_opening_search_on_phosh_schedules_neutral_focus(self):
        harness = _Harness()
        with mock.patch.object(overview, 'should_prevent_input_autofocus', return_value=True), \
                mock.patch.object(overview, 'schedule_neutral_focus') as schedule:
            harness.on_search_clicked(None)
        harness.search_entry.grab_focus.assert_not_called()
        schedule.assert_called_once_with(harness, harness._main_neutral_focus_target)

    def test_closing_search_clears_text_and_filter(self):
        harness = _Harness()
        harness.search_visible = True
        harness.search_text = 'mail'
        harness.search_entry.get_text.return_value = 'mail'
        harness.on_search_clicked(None)
        self.assertFalse(harness.search_visible)
        harness.search_entry.set_text.assert_called_once_with('')
        self.assertEqual(harness.search_text, '')
        harness.custom_filter.changed.assert_called_once_with(Gtk.FilterChange.DIFFERENT)
        harness.empty_label.set_visible.assert_called_once_with(True)
        harness._restore_overview_header_actions.assert_called_once_with()

    def test_closing_empty_search_does_not_rewrite_the_entry(self):
        harness = _Harness()
        harness.search_visible = True
        harness.on_search_clicked(None)
        harness.search_entry.set_text.assert_not_called()

    def test_search_text_is_normalised_and_filters(self):
        harness = _Harness()
        entry_widget = mock.Mock()
        entry_widget.get_text.return_value = '  MaIL '
        harness.filtered_model = FakeListModel([Entry(1, 'A')])
        harness.on_search_entry_changed(entry_widget)
        self.assertEqual(harness.search_text, 'mail')
        harness.custom_filter.changed.assert_called_once_with(Gtk.FilterChange.DIFFERENT)
        harness.empty_label.set_visible.assert_called_once_with(False)
        self.assertTrue(harness.filter_entries(Entry(1, 'Webmail')))
        self.assertTrue(harness.filter_entries(Entry(2, 'Other', 'my MAIL client')))
        self.assertFalse(harness.filter_entries(Entry(3, 'Calendar', 'dates')))

    def test_empty_search_matches_everything(self):
        self.assertTrue(_Harness().filter_entries(Entry(1, 'x')))


class OverviewIconEventTests(_OverviewTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(overview, 'LOG')
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def _gesture(self, widget):
        gesture = mock.Mock()
        gesture.get_widget.return_value = widget
        return gesture

    def test_icon_event_log_records_entry_and_state(self):
        harness = _Harness()
        harness.selection.get_selected.side_effect = TypeError
        button = mock.Mock()
        button._bound_entry = Entry(4, 'Mail')
        button.get_sensitive.return_value = True
        button.get_visible.return_value = False
        harness._log_overview_icon_event('clicked', button)
        args = self.log.info.call_args[0]
        self.assertEqual(args[1:], ('clicked', id(button), 4, 'Mail', None, None, 0, True, False))

    def test_icon_event_log_without_button(self):
        _Harness()._log_overview_icon_event('pressed', None)
        args = self.log.info.call_args[0]
        self.assertEqual(args[2:4], (None, None))
        self.assertEqual(args[-2:], (None, None))

    def test_press_launches_the_bound_entry(self):
        harness = _Harness()
        button = mock.Mock()
        button._bound_entry = Entry(1, 'A')
        harness._on_overview_icon_pressed(self._gesture(button), 1, 0, 0)
        harness._launch_entry_from_icon.assert_called_once_with(button._bound_entry)

    def test_press_and_release_without_widget_only_log(self):
        harness = _Harness()
        broken = mock.Mock()
        broken.get_widget.side_effect = AttributeError
        harness._on_overview_icon_pressed(broken, 1, 0, 0)
        harness._on_overview_icon_released(self._gesture(None), 1, 0, 0)
        harness._on_overview_icon_released(broken, 1, 0, 0)
        harness._launch_entry_from_icon.assert_not_called()
        self.assertIn('no widget', self.log.info.call_args[0][0])

    def test_release_logs_the_event(self):
        harness = _Harness()
        button = mock.Mock()
        button._bound_entry = None
        harness._on_overview_icon_released(self._gesture(button), 1, 0, 0)
        self.assertEqual(self.log.info.call_args[0][1], 'released')
        harness._launch_entry_from_icon.assert_not_called()

    def test_click_launches_only_with_a_bound_entry(self):
        harness = _Harness()
        button = mock.Mock()
        button._bound_entry = None
        harness._on_overview_icon_clicked(button)
        harness._launch_entry_from_icon.assert_not_called()
        button._bound_entry = Entry(2, 'B')
        harness._on_overview_icon_clicked(button)
        harness._launch_entry_from_icon.assert_called_once_with(button._bound_entry)

    def test_content_tap_opens_the_detail_page(self):
        harness = _Harness()
        harness.on_entry_activated = mock.Mock()
        widget = mock.Mock()
        widget._bound_entry = Entry(3, 'C')
        harness._on_overview_content_released(self._gesture(widget), 1, 0, 0)
        harness.on_entry_activated.assert_called_once_with(widget._bound_entry)

    def test_content_tap_without_entry_is_ignored(self):
        harness = _Harness()
        harness.on_entry_activated = mock.Mock()
        broken = mock.Mock()
        broken.get_widget.side_effect = TypeError
        harness._on_overview_content_released(broken, 1, 0, 0)
        harness._on_overview_content_released(self._gesture(mock.Mock(_bound_entry=None)), 1, 0, 0)
        harness.on_entry_activated.assert_not_called()

    def test_binding_replaces_the_previous_click_handler(self):
        harness = _Harness()
        button = mock.Mock()
        button._click_handler_id = None
        button.connect.side_effect = [5, 6]
        first, second = Entry(1, 'A'), Entry(2, 'B')
        harness._bind_overview_icon_button(button, first)
        button.disconnect.assert_not_called()
        harness._bind_overview_icon_button(button, second)
        button.disconnect.assert_called_once_with(5)
        self.assertIs(button._bound_entry, second)
        self.assertEqual(button._click_handler_id, 6)
        button.connect.assert_called_with('clicked', harness._on_overview_icon_clicked)

    def test_clearing_tolerates_disconnect_errors(self):
        harness = _Harness()
        button = mock.Mock()
        button._click_handler_id = 9
        button.disconnect.side_effect = TypeError
        harness._clear_overview_icon_button_handler(button)
        self.assertIsNone(button._click_handler_id)


class ListActivationTests(_OverviewTestCase):
    def setUp(self):
        super().setUp()
        self.harness = _Harness()
        self.harness.on_entry_activated = mock.Mock()
        self.entry = Entry(8, 'Mail')
        self.harness.filtered_model = FakeListModel([self.entry])

    def test_activation_opens_detail_and_clears_selection(self):
        self.harness.on_list_view_activate(None, 0)
        self.harness.on_entry_activated.assert_called_once_with(self.entry)
        self.harness.selection.set_selected.assert_called_once_with(INVALID)

    def test_missing_item_or_model_is_ignored(self):
        self.harness.on_list_view_activate(None, 5)
        self.harness.filtered_model = None
        self.harness.on_list_view_activate(None, 0)
        self.harness.on_entry_activated.assert_not_called()

    def test_suppressed_activation_is_consumed_once(self):
        self.harness._suppress_next_overview_activate_entry_id = 8
        self.harness._suppress_next_overview_activate_until_us = 2_000_000
        self.harness.on_list_view_activate(None, 0)
        self.harness.on_entry_activated.assert_not_called()
        self.harness.selection.set_selected.assert_called_once_with(INVALID)
        self.assertIsNone(self.harness._suppress_next_overview_activate_entry_id)
        self.assertEqual(self.harness._suppress_next_overview_activate_until_us, 0)
        self.harness.on_list_view_activate(None, 0)
        self.harness.on_entry_activated.assert_called_once_with(self.entry)

    def test_expired_suppression_does_not_block(self):
        self.harness._suppress_next_overview_activate_entry_id = 8
        self.harness._suppress_next_overview_activate_until_us = 999_999
        self.harness.on_list_view_activate(None, 0)
        self.harness.on_entry_activated.assert_called_once_with(self.entry)

    def test_clock_failure_disables_suppression(self):
        self.glib.get_monotonic_time.side_effect = ValueError
        self.harness._suppress_next_overview_activate_entry_id = 8
        self.harness._suppress_next_overview_activate_until_us = 2_000_000
        self.harness.selection.set_selected.side_effect = TypeError
        self.harness.on_list_view_activate(None, 0)
        self.harness.on_entry_activated.assert_called_once_with(self.entry)

    def test_selection_errors_on_suppressed_path_are_swallowed(self):
        self.harness._suppress_next_overview_activate_entry_id = 8
        self.harness._suppress_next_overview_activate_until_us = 2_000_000
        self.harness.selection.set_selected.side_effect = AttributeError
        self.harness.on_list_view_activate(None, 0)
        self.harness.on_entry_activated.assert_not_called()


class RowBindingTests(_OverviewTestCase):
    def setUp(self):
        super().setUp()
        self.harness = _Harness()
        self.harness._get_profile_size_text_cached.return_value = '12 MB'
        patcher = mock.patch.object(overview, 'create_image_from_ref', side_effect=lambda ref, pixel_size: ('icon', ref, pixel_size))
        self.make_icon = patcher.start()
        self.addCleanup(patcher.stop)

    def _widgets(self):
        return {
            'icon_button': mock.Mock(_click_handler_id=None),
            'icon_frame': FakeContainer(1),
            'content_box': mock.Mock(),
            'status_box': FakeContainer(2),
            'title_label': mock.Mock(),
            'description_label': mock.Mock(),
            'engine_image': mock.Mock(),
            'active_dot': mock.Mock(),
            'profile_size_label': mock.Mock(),
        }

    def _list_item(self, entry, widgets):
        item = mock.Mock()
        item.get_item.return_value = entry
        item._overview_widgets = widgets
        item._entry_handlers = []
        item._bound_entry = None
        return item

    def test_incomplete_row_is_not_bound(self):
        widgets = self._widgets()
        del widgets['active_dot']
        item = self._list_item(Entry(1, 'A'), widgets)
        self.harness.on_factory_bind(None, item)
        widgets['title_label'].set_text.assert_not_called()
        self.assertIsNone(item._bound_entry)

    def test_bind_fills_the_row_from_entry_and_options(self):
        entry = Entry(1, 'Mail', 'Webmail', active=False)
        self.harness.options = {1: {ICON_PATH_KEY: '/icons/mail.png', PROFILE_PATH_KEY: '/p/mail', 'EngineName': 'Firefox'}}
        widgets = self._widgets()
        item = self._list_item(entry, widgets)
        self.harness.on_factory_bind(None, item)
        self.assertIs(item._bound_entry, entry)
        self.assertIs(widgets['content_box']._bound_entry, entry)
        self.assertIs(widgets['icon_button']._bound_entry, entry)
        widgets['title_label'].set_text.assert_called_once_with('Mail')
        widgets['description_label'].set_text.assert_called_once_with('Webmail')
        self.assertEqual([c for c in widgets['icon_frame'].children], [('icon', '/icons/mail.png', 40)])
        self.assertEqual(widgets['status_box'].children, [])
        widgets['engine_image'].set_from_icon_name.assert_called_once_with('firefox')
        widgets['engine_image'].set_visible.assert_called_once_with(True)
        widgets['active_dot'].add_css_class.assert_called_once_with('inactive')
        label = widgets['profile_size_label']
        self.assertEqual((label._entry_id, label._profile_path), (1, '/p/mail'))
        label.set_text.assert_called_once_with('12 MB')
        label.set_visible.assert_called_once_with(True)
        self.harness._schedule_profile_size_refresh.assert_called_once_with(1, '/p/mail', label)
        self.assertEqual(len(item._entry_handlers), 3)

    def test_entry_property_change_refreshes_the_row(self):
        entry = Entry(1, 'Mail')
        widgets = self._widgets()
        item = self._list_item(entry, widgets)
        self.harness.on_factory_bind(None, item)
        entry.title = 'Post'
        widgets['title_label'].set_text.assert_called_with('Post')
        self.harness.custom_filter.changed.assert_called_with(Gtk.FilterChange.DIFFERENT)
        self.harness.empty_label.set_visible.assert_called_with(True)
        before = widgets['title_label'].set_text.call_count
        entry.description = 'x'
        entry.active = False
        self.assertEqual(widgets['title_label'].set_text.call_count, before + 2)
        widgets['active_dot'].add_css_class.assert_called_with('inactive')

    def test_rebinding_disconnects_the_previous_entry(self):
        first, second = Entry(1, 'First'), Entry(2, 'Second')
        widgets = self._widgets()
        item = self._list_item(first, widgets)
        self.harness.on_factory_bind(None, item)
        item.get_item.return_value = second
        self.harness.on_factory_bind(None, item)
        widgets['title_label'].set_text.reset_mock()
        first.title = 'Changed elsewhere'
        widgets['title_label'].set_text.assert_not_called()
        second.title = 'Renamed'
        widgets['title_label'].set_text.assert_called_once_with('Renamed')

    def test_disconnect_errors_of_a_stale_entry_are_ignored(self):
        stale = mock.Mock()
        stale.disconnect.side_effect = TypeError
        widgets = self._widgets()
        item = self._list_item(Entry(1, 'A'), widgets)
        item._bound_entry = stale
        item._entry_handlers = [1, 2]
        self.harness.on_factory_bind(None, item)
        self.assertEqual(stale.disconnect.call_count, 2)
        widgets['title_label'].set_text.assert_called_once_with('A')

    def test_overview_icon_without_custom_icon_uses_small_fallback(self):
        frame = FakeContainer(0)
        self.harness._set_overview_icon(frame, 1)
        self.assertEqual(frame.children, [('icon', '', 28)])

    def test_status_indicators_without_engine_hide_the_engine_icon(self):
        status_box = FakeContainer(3)
        engine = mock.Mock()
        dot = mock.Mock()
        self.harness._set_status_indicators(status_box, 1, True, engine, dot)
        self.assertEqual(status_box.children, [])
        engine.set_from_icon_name.assert_called_once_with('applications-internet-symbolic')
        engine.set_visible.assert_called_once_with(False)
        dot.remove_css_class.assert_any_call('active')
        dot.remove_css_class.assert_any_call('inactive')
        dot.add_css_class.assert_called_once_with('active')
        dot.set_visible.assert_called_once_with(True)
        # Engine and dot widgets are optional.
        self.harness._set_status_indicators(FakeContainer(1), 1)

    def test_profile_size_label_hidden_without_size(self):
        label = mock.Mock()
        self.harness._get_profile_size_text_cached.return_value = ''
        self.harness._set_profile_size_label(label, 5)
        label.set_visible.assert_called_once_with(False)
        self.harness._get_profile_size_text_cached.assert_called_once_with(5, '')
        self.harness._set_profile_size_label(None, 5)
        self.harness._schedule_profile_size_refresh.assert_called_once()


class TitleAndNavigationTests(_OverviewTestCase):
    def test_header_title_updates_only_for_the_visible_detail(self):
        harness = _Harness()
        entry = Entry(1, '')
        harness.update_header_title(entry)
        harness._reposition_entry_in_store.assert_called_once_with(entry)
        harness.content_navigation_page.set_title.assert_not_called()
        page = FakeDetailPage(entry)
        harness.detail_pages[1] = page
        harness.show_detail(page)
        harness.update_header_title(entry)
        harness.content_navigation_page.set_title.assert_called_once_with(APP_TITLE)
        harness._show_overview_header.assert_called_once_with()
        entry.title = 'Mail'
        harness.content_navigation_page.set_title.side_effect = TypeError
        harness.update_header_title(entry)
        harness.content_navigation_page.set_title.assert_called_with('Mail')

    def test_refresh_visual_invalidates_cache_and_notifies(self):
        harness = _Harness()
        entry = mock.Mock(id=3)
        harness.refresh_entry_visual(entry)
        harness._invalidate_entry_cache.assert_called_once_with(3, clear_profile_size=True)
        self.assertEqual(entry.notify.call_args_list, [mock.call('title'), mock.call('description')])

    def test_navigation_change_of_hidden_page_is_ignored(self):
        harness = _Harness(adaptive=True)
        harness._on_detail_navigation_changed(FakeDetailPage())
        harness.delete_button.set_visible.assert_not_called()

    def test_navigation_change_in_wide_adaptive_mode_restores_actions(self):
        harness = _Harness(adaptive=True)
        page = FakeDetailPage(Entry(1, 'Mail'))
        harness.show_detail(page)
        harness._on_detail_navigation_changed(page)
        harness.content_navigation_page.set_title.assert_called_once_with('Mail')
        harness._restore_overview_header_actions.assert_called_once_with()
        harness.delete_button.set_visible.assert_called_once_with(True)
        harness._show_overview_header.assert_not_called()

    def test_navigation_change_in_wide_mode_without_entry_uses_app_title(self):
        harness = _Harness(adaptive=True)
        page = FakeDetailPage(None)
        harness.show_detail(page)
        harness.content_navigation_page.set_title.side_effect = AttributeError
        harness._on_detail_navigation_changed(page)
        harness.content_navigation_page.set_title.assert_called_once_with(APP_TITLE)

    def test_navigation_change_in_narrow_mode_shows_header(self):
        harness = _Harness(adaptive=True, narrow=True)
        page = FakeDetailPage(Entry(1, 'Mail'))
        harness.show_detail(page)
        harness._on_detail_navigation_changed(page)
        harness._show_overview_header.assert_called_once_with()
        harness._restore_overview_header_actions.assert_not_called()
        harness.delete_button.set_visible.assert_called_once_with(True)

    def test_navigation_change_in_stack_mode_updates_delete_button(self):
        harness = _Harness()
        page = FakeDetailPage(Entry(1, 'Mail'))
        harness.show_detail(page)
        harness._on_detail_navigation_changed(page)
        harness._show_overview_header.assert_not_called()
        harness.delete_button.set_visible.assert_called_once_with(True)


class EntryActivationTests(_OverviewTestCase):
    def _run_idle(self):
        callback = self.glib.idle_add.call_args[0][0]
        self.assertFalse(callback())

    def test_first_activation_creates_and_shows_a_detail_page(self):
        harness = _Harness(adaptive=True, narrow=True)
        entry = Entry(4, 'Mail')
        harness.on_entry_activated(entry)
        harness._show_busy.assert_called_once_with(overview.t('loading'))
        harness.stack.set_visible_child_name.assert_called_with('overview_page')
        harness._hide_busy.assert_not_called()
        self._run_idle()
        page = harness.detail_pages[4]
        self.assertIsInstance(page, FakeDetailPage)
        self.assertIs(page.entry, entry)
        self.assertIs(page.db, harness.db)
        self.assertEqual(page.callbacks['on_back'], harness.show_list_page)
        self.assertEqual(page.callbacks['on_delete'], harness.confirm_delete)
        self.assertEqual(page.callbacks['on_title_changed'], harness.update_header_title)
        self.assertEqual(page.callbacks['on_visual_changed'], harness.refresh_entry_visual)
        self.assertEqual(page.callbacks['on_navigation_changed'], harness._on_detail_navigation_changed)
        self.assertIs(page.callbacks['on_overlay_notification'], harness.show_overlay_notification)
        page.set_compact_mode_override.assert_called_once_with(True)
        harness.content_stack.add_named.assert_called_once_with(page, 'detail_4')
        harness.content_stack.set_visible_child.assert_called_once_with(page)
        harness.content_navigation_page.set_title.assert_called_with('Mail')
        harness._hide_busy.assert_called_once_with()

    def test_existing_page_is_reused_without_busy_indicator(self):
        harness = _Harness()
        entry = Entry(4, '')
        page = FakeDetailPage(entry)
        harness.detail_pages[4] = page
        harness.on_entry_activated(entry, show_busy=False)
        self._run_idle()
        harness._show_busy.assert_not_called()
        harness._hide_busy.assert_not_called()
        page.set_compact_mode_override.assert_called_once_with(None)
        harness.content_stack.add_named.assert_not_called()
        harness.content_stack.set_visible_child.assert_called_once_with(page)

    def test_failing_detail_page_reports_and_returns_to_overview(self):
        harness = _Harness()
        FakeDetailPage.init_error = OSError('no profile')
        with mock.patch.object(overview, 'LOG') as log:
            harness.on_entry_activated(Entry(4, 'Mail'))
            self._run_idle()
        log.error.assert_called_once()
        harness.show_overlay_notification.assert_called_once_with(overview.t('detail_view_load_failed'), timeout_ms=3500)
        self.assertNotIn(4, harness.detail_pages)
        harness._hide_busy.assert_called_once_with()
        self.assertEqual(harness.content_stack.set_visible_child_name.call_args_list[-1], mock.call('list_page'))


class DeleteAndReleaseTests(_OverviewTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(overview, 'delete_managed_entry_artifacts')
        self.delete_artifacts = patcher.start()
        self.addCleanup(patcher.stop)

    def test_confirm_delete_deletes(self):
        harness = _Harness()
        harness.delete_entry = mock.Mock()
        entry = Entry(1, 'A')
        harness.confirm_delete(entry)
        harness.delete_entry.assert_called_once_with(entry)

    def test_delete_removes_artifacts_row_and_visible_page(self):
        harness = _Harness()
        harness.show_list_page = mock.Mock()
        keep, doomed = Entry(1, 'Keep'), Entry(2, 'Doomed')
        harness.entries_store = FakeListModel([keep, doomed])
        harness.options = {2: {PROFILE_PATH_KEY: '/p/doomed', PROFILE_NAME_KEY: 'webapp_doomed'}}
        page = FakeDetailPage(doomed)
        harness.detail_pages[2] = page
        harness.show_detail(page)
        harness.delete_entry(doomed)
        self.delete_artifacts.assert_called_once_with(
            2, 'Doomed', overview.ENGINES, overview.LOG, delete_profiles=True,
            stored_profile_path='/p/doomed', stored_profile_name='webapp_doomed',
        )
        harness.db.delete_entry.assert_called_once_with(2)
        self.assertEqual(harness.entries_store.items, [keep])
        self.assertEqual(harness.detail_pages, {})
        self.glib.idle_add.assert_called_once_with(harness._cleanup_detail_pages, [page])
        harness.content_stack.set_visible_child_name.assert_called_with('list_page')
        harness.show_list_page.assert_called_once_with()

    def test_delete_of_entry_without_row_or_page(self):
        harness = _Harness()
        harness.show_list_page = mock.Mock()
        entry = Entry(9, 'Gone')
        harness.entries_store = FakeListModel([Entry(1, 'Other')])
        harness.delete_entry(entry)
        self.assertEqual(len(harness.entries_store.items), 1)
        self.glib.idle_add.assert_not_called()
        self.delete_artifacts.assert_called_once()
        self.assertEqual(self.delete_artifacts.call_args.kwargs['stored_profile_path'], '')
        harness.empty_label.set_visible.assert_called_once_with(True)

    def test_delete_of_hidden_page_keeps_current_view(self):
        harness = _Harness()
        harness.show_list_page = mock.Mock()
        entry = Entry(2, 'Hidden')
        page = FakeDetailPage(entry)
        harness.detail_pages[2] = page
        harness.delete_entry(entry)
        harness.content_stack.set_visible_child_name.assert_not_called()
        self.glib.idle_add.assert_called_once_with(harness._cleanup_detail_pages, [page])

    def test_release_detail_page(self):
        harness = _Harness()
        harness._release_detail_page(None)
        self.glib.idle_add.assert_not_called()
        entry = Entry(3, 'X')
        page = FakeDetailPage(entry)
        page.release_resources.side_effect = AttributeError
        harness.detail_pages[3] = page
        harness._release_detail_page(page)
        page.release_resources.assert_called_once_with()
        self.assertEqual(harness.detail_pages, {})
        self.glib.idle_add.assert_called_once_with(harness._cleanup_detail_pages, [page])

    def test_release_keeps_a_newer_page_for_the_same_entry(self):
        harness = _Harness()
        entry = Entry(3, 'X')
        old, new = FakeDetailPage(entry), FakeDetailPage(entry)
        harness.detail_pages[3] = new
        harness._release_detail_page(old)
        self.assertIs(harness.detail_pages[3], new)


class ShowListPageTests(_OverviewTestCase):
    def test_open_search_is_closed_first(self):
        harness = _Harness()
        harness.search_visible = True
        harness.search_text = 'mail'
        harness.search_entry.get_text.return_value = 'mail'
        harness.show_list_page()
        self.assertFalse(harness.search_visible)
        harness.search_entry.set_visible.assert_called_once_with(False)
        harness.search_entry.set_text.assert_called_once_with('')
        self.assertEqual(harness.search_text, '')
        harness.custom_filter.changed.assert_called_once_with(Gtk.FilterChange.DIFFERENT)
        harness._restore_overview_header_actions.assert_called_once_with()
        harness.stack.set_visible_child_name.assert_not_called()

    def test_empty_search_text_is_not_rewritten(self):
        harness = _Harness()
        harness.search_visible = True
        harness.stack = None
        harness.show_list_page()
        harness.search_entry.set_text.assert_not_called()

    def test_detail_subpage_returns_to_detail_main_page(self):
        harness = _Harness()
        page = FakeDetailPage(Entry(1, 'A'))
        page.is_subpage_visible.return_value = True
        harness.show_detail(page)
        harness.show_list_page()
        page.show_main_page.assert_called_once_with()
        harness._show_overview_header.assert_called_once_with()
        page.release_resources.assert_not_called()

    def test_adaptive_settings_subpages(self):
        cases = (
            ('settings_assets_page', '_return_to_overview_from_settings_assets'),
            ('settings_about_page', '_return_to_overview_from_settings_subpage'),
            ('settings_security_privacy_page', '_return_to_overview_from_settings_subpage'),
        )
        for attribute, expected in cases:
            with self.subTest(page=attribute):
                harness = _Harness(adaptive=True)
                subpage = mock.Mock()
                setattr(harness, attribute, subpage)
                harness.show_detail(subpage)
                harness.show_list_page()
                getattr(harness, expected).assert_called_once_with()
                harness._hide_global_toast.assert_not_called()

    def test_adaptive_settings_page_returns_to_overview(self):
        harness = _Harness(adaptive=True)
        harness.settings_page = mock.Mock()
        harness.show_detail(harness.settings_page)
        harness.show_list_page()
        harness._hide_global_toast.assert_called_once_with()
        harness.stack.set_visible_child_name.assert_called_once_with('overview_page')
        harness._restore_overview_header_actions.assert_called_once_with()

    def test_stack_mode_settings_pages_by_name(self):
        cases = (
            ('settings_assets_page', '_return_to_overview_from_settings_assets'),
            ('settings_about_page', '_return_to_overview_from_settings_subpage'),
            ('settings_security_privacy_page', '_return_to_overview_from_settings_subpage'),
        )
        for name, expected in cases:
            with self.subTest(page=name):
                harness = _Harness()
                harness.stack.get_visible_child_name.return_value = name
                harness.show_list_page()
                getattr(harness, expected).assert_called_once_with()
                harness._hide_global_toast.assert_not_called()

    def test_stack_mode_settings_page_goes_back_to_overview(self):
        harness = _Harness()
        harness.stack.get_visible_child_name.return_value = 'settings_page'
        harness.show_list_page()
        harness._restore_overview_header_actions.assert_called_once_with()
        harness.stack.set_visible_child_name.assert_called_once_with('overview_page')
        harness._hide_global_toast.assert_not_called()

    def test_leaving_a_detail_page_releases_it(self):
        harness = _Harness()
        page = FakeDetailPage(Entry(1, 'A'))
        harness.detail_pages[1] = page
        harness.show_detail(page)
        harness.show_list_page()
        page.release_resources.assert_called_once_with()
        self.assertEqual(harness.detail_pages, {})
        harness._hide_global_toast.assert_called_once_with()
        harness.content_stack.set_visible_child_name.assert_called_once_with('list_page')
        harness.stack.set_visible_child_name.assert_called_once_with('overview_page')
        harness._restore_overview_header_actions.assert_called_once_with()

    def test_plain_list_page_without_detail(self):
        harness = _Harness(adaptive=True)
        # The real window always builds its settings pages at start-up.
        for name in ('settings_page', 'settings_assets_page', 'settings_about_page', 'settings_security_privacy_page'):
            setattr(harness, name, mock.Mock(name=name))
        harness.show_detail(harness.detail_placeholder)
        harness.show_list_page()
        harness._hide_global_toast.assert_called_once_with()
        harness._return_to_overview_from_settings_subpage.assert_not_called()
        self.glib.idle_add.assert_not_called()
        harness.stack.set_visible_child_name.assert_called_once_with('overview_page')


class CreateEmptyEntryTests(_OverviewTestCase):
    def test_new_entry_is_stored_listed_and_opened(self):
        harness = _Harness()
        harness.on_entry_activated = mock.Mock()
        harness.db.add_entry.return_value = 12

        def check_flags(entry, show_busy):
            self.assertTrue(harness._creating_entry)
            harness.add_button.set_sensitive.assert_called_once_with(False)

        harness.on_entry_activated.side_effect = check_flags
        harness._create_empty_entry()
        harness.db.add_entry.assert_called_once_with('')
        (entry,) = harness.entries_store.items
        self.assertEqual((entry.id, entry.title), (12, ''))
        harness.on_entry_activated.assert_called_once_with(entry, show_busy=False)
        harness.selection.set_selected.assert_called_once_with(INVALID)
        self.assertFalse(harness._creating_entry)
        harness.add_button.set_sensitive.assert_called_with(True)

    def test_failed_insert_adds_nothing(self):
        harness = _Harness()
        harness.on_entry_activated = mock.Mock()
        harness.db.add_entry.return_value = None
        harness.selection.set_selected.side_effect = TypeError
        harness._create_empty_entry()
        self.assertEqual(harness.entries_store.items, [])
        harness.on_entry_activated.assert_not_called()
        self.assertFalse(harness._creating_entry)

    def test_database_error_still_reenables_the_add_button(self):
        harness = _Harness()
        harness.db.add_entry.side_effect = OSError('disk full')
        with self.assertRaises(OSError):
            harness._create_empty_entry()
        self.assertFalse(harness._creating_entry)
        harness.add_button.set_sensitive.assert_called_with(True)


class VersionLabelTests(_OverviewTestCase):
    def test_version_label_is_the_app_version(self):
        self.assertEqual(_Harness()._read_app_version_label(), APP_VERSION)


if __name__ == '__main__':
    unittest.main()

"""Coverage tests for detail_page/layout.py.

Most of DetailPageLayoutMixin is decision logic (which page is showing, is the
layout compact, where does focus go, how is the scroll position kept), so it is
tested on a harness that inherits the mixin and supplies hand-written stand-ins
for the widgets it touches. The methods that restyle the whole widget tree are
additionally run against a real DetailPage, which GTK 4 can build without a
display, and checked through the widgets' own getters.
"""
import logging
import sys
import types
import unittest
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_detail_page_layout.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import gi_versions  # noqa: F401, I001 -- pins the typelib versions before gi.repository loads
from gi.repository import Gdk, GLib, Gtk

import detail_page.layout as layout_module
import detail_page.page as page_module
from database import Database
from detail_page import DetailPage
from detail_page.layout import DetailPageLayoutMixin


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


class _Sized:
    def __init__(self, width):
        self.width = width

    def get_width(self):
        return self.width


class _Toggle:
    def __init__(self, active=False):
        self.active = active

    def set_active(self, value):
        self.active = bool(value)

    def get_active(self):
        return self.active


class _Stack:
    def __init__(self, name='main'):
        self.name = name
        self.shown = []

    def get_visible_child_name(self):
        return self.name

    def get_width(self):
        return 0

    def set_visible_child_name(self, name):
        self.shown.append(name)
        self.name = name


class _Box:
    def __init__(self):
        self.children = []

    def append(self, widget):
        self.children.append(widget)
        widget.parent = self

    def remove(self, widget):
        self.children.remove(widget)
        widget.parent = None


class _Child:
    def __init__(self, parent=None):
        self.parent = parent

    def get_parent(self):
        return self.parent


class _Adjustment:
    def __init__(self, value=0.0, upper=1000.0, page_size=200.0, fail=False):
        self.value = value
        self.upper = upper
        self.page_size = page_size
        self.fail = fail
        self.set_values = []

    def get_value(self):
        if self.fail:
            raise RuntimeError('broken adjustment')
        return self.value

    def get_upper(self):
        if self.fail:
            raise RuntimeError('broken adjustment')
        return self.upper

    def get_page_size(self):
        return self.page_size

    def set_value(self, value):
        self.set_values.append(value)


class _Scrolled:
    def __init__(self, adjustment=None, fail=False):
        self.adjustment = adjustment
        self.fail = fail

    def get_vadjustment(self):
        if self.fail:
            raise RuntimeError('no adjustment')
        return self.adjustment


class _Harness(DetailPageLayoutMixin):
    def __init__(self, page_name='main', width=0):
        self.width = width
        self.page_stack = _Stack(page_name)
        self.grid = _Sized(0)
        self._compact_mode_override = None
        self._desktop_tabs_syncing = False
        self.desktop_tab_buttons = {name: _Toggle() for name in ('main', 'options', 'css_assets', 'javascript_assets')}
        self.detail_tab_scroller = mock.Mock()
        self.desktop_tab_bar = mock.Mock()
        self.custom_assets_row = mock.Mock()
        self.on_navigation_changed = None
        self.calls = []

    def get_width(self):
        return self.width

    # Collaborators owned by other mixins.
    def _apply_adaptive_layout(self, force=False):
        self.calls.append(('apply', force))

    def _refresh_asset_page(self, asset_type):
        self.calls.append(('refresh_asset', asset_type))

    def _reload_options_cache_from_db(self):
        self.calls.append('reload')

    def _apply_option_values_to_controls(self):
        self.calls.append('apply_values')


class WidthAndCompactTests(unittest.TestCase):
    def test_effective_width_is_the_widest_of_page_stack_and_grid(self):
        harness = _Harness(width=300)
        harness.page_stack = _Sized(500)
        harness.grid = _Sized(None)
        self.assertEqual(harness._effective_layout_width(), 500)
        harness.page_stack = None
        harness.grid = None
        self.assertEqual(harness._effective_layout_width(), 300)

    def test_compact_layout_threshold(self):
        harness = _Harness(width=0)
        self.assertFalse(harness._is_compact_layout())  # not yet allocated
        harness.width = 619
        self.assertTrue(harness._is_compact_layout())
        harness.width = 620
        self.assertFalse(harness._is_compact_layout())

    def test_override_wins_over_the_width(self):
        harness = _Harness(width=1000)
        harness._compact_mode_override = True
        self.assertTrue(harness._is_compact_layout())

    def test_set_override_relayouts_only_on_change(self):
        harness = _Harness()
        with mock.patch.object(harness, '_update_tabbed_navigation_state') as update:
            harness.set_compact_mode_override(None)
            self.assertEqual(harness.calls, [])
            harness.set_compact_mode_override(1)
            self.assertIs(harness._compact_mode_override, True)
            self.assertEqual(harness.calls, [('apply', True)])
            update.assert_called_once_with()
            harness.set_compact_mode_override(True)
            self.assertEqual(len(harness.calls), 1)

    def test_subpage_side_inset(self):
        harness = _Harness()
        self.assertEqual(harness._subpage_side_inset(True), 0)
        self.assertEqual(harness._subpage_side_inset(False), 18)


class PageNameTests(unittest.TestCase):
    def test_current_page_name(self):
        harness = _Harness('options')
        self.assertEqual(harness._current_page_name(), 'options')
        harness.page_stack.name = None
        self.assertEqual(harness._current_page_name(), 'main')
        harness.page_stack = None
        self.assertEqual(harness._current_page_name(), 'main')

    def test_desktop_tab_target_maps_subpages_to_main(self):
        harness = _Harness('icon')
        self.assertEqual(harness._desktop_tab_target(), 'main')
        self.assertEqual(harness._desktop_tab_target('css_assets'), 'css_assets')

    def test_sync_tab_buttons_activates_exactly_one(self):
        harness = _Harness('javascript_assets')
        harness._sync_desktop_tab_buttons()
        self.assertEqual([n for n, b in harness.desktop_tab_buttons.items() if b.active], ['javascript_assets'])
        self.assertFalse(harness._desktop_tabs_syncing)

    def test_tab_toggle_is_ignored_while_syncing_or_when_released(self):
        harness = _Harness()
        with mock.patch.object(harness, '_show_tab_page') as show:
            harness._desktop_tabs_syncing = True
            harness._on_desktop_tab_toggled(_Toggle(True), 'options')
            harness._desktop_tabs_syncing = False
            harness._on_desktop_tab_toggled(_Toggle(False), 'options')
            show.assert_not_called()
            harness._on_desktop_tab_toggled(_Toggle(True), 'options')
            show.assert_called_once_with('options')

    def test_is_subpage_visible(self):
        for name, expected in (('main', False), ('options', False), ('css_assets', False), ('icon', True)):
            with self.subTest(name=name):
                self.assertEqual(_Harness(name).is_subpage_visible(), expected)


class MoveWidgetTests(unittest.TestCase):
    def test_none_arguments_are_ignored(self):
        harness = _Harness()
        box = _Box()
        harness._move_widget_to_box(None, box)
        harness._move_widget_to_box(_Child(), None)
        self.assertEqual(box.children, [])

    def test_widget_already_in_the_box_stays(self):
        harness = _Harness()
        box = _Box()
        child = _Child()
        box.append(child)
        harness._move_widget_to_box(child, box)
        self.assertEqual(box.children, [child])

    def test_widget_is_reparented(self):
        harness = _Harness()
        old, new = _Box(), _Box()
        child = _Child()
        old.append(child)
        harness._move_widget_to_box(child, new)
        self.assertEqual(old.children, [])
        self.assertEqual(new.children, [child])

    def test_failing_removal_does_not_stop_the_move(self):
        harness = _Harness()
        old = mock.Mock()
        old.remove.side_effect = RuntimeError('not a child')
        new = _Box()
        child = _Child(parent=old)
        harness._move_widget_to_box(child, new)
        self.assertEqual(new.children, [child])

    def test_mount_options_section_only_on_change(self):
        harness = _Harness()
        harness.options_page_content = _Box()
        harness.options_section = _Child()
        harness._options_section_compact = True
        harness._mount_options_section(True)
        self.assertEqual(harness.options_page_content.children, [])
        harness._mount_options_section(True, force=True)
        self.assertEqual(harness.options_page_content.children, [harness.options_section])
        harness._mount_options_section(False)
        self.assertFalse(harness._options_section_compact)


class LayoutRebuildQueueTests(unittest.TestCase):
    def test_rebuild_is_queued_once(self):
        harness = _Harness()
        harness._options_rebuild_source_id = 0
        callbacks = []
        with mock.patch.object(GLib, 'timeout_add', side_effect=lambda delay, cb: callbacks.append((delay, cb)) or 55):
            harness._on_layout_width_changed(None, None)
            harness._queue_options_layout_rebuild()
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(callbacks[0][0], 60)
        self.assertEqual(harness._options_rebuild_source_id, 55)
        self.assertFalse(callbacks[0][1]())
        self.assertEqual(harness._options_rebuild_source_id, 0)
        self.assertEqual(harness.calls, [('apply', False)])

    def test_finish_initial_setup(self):
        harness = _Harness()
        harness._suspend_change_handlers = True
        with mock.patch.object(harness, '_update_tabbed_navigation_state') as nav, \
            mock.patch.object(harness, '_schedule_mobile_focus_reset') as focus:
            self.assertFalse(harness._finish_initial_detail_setup())
        self.assertEqual(harness.calls, ['reload', ('apply', True), 'apply_values'])
        nav.assert_called_once_with()
        focus.assert_called_once_with()
        self.assertFalse(harness._suspend_change_handlers)


class FocusTests(unittest.TestCase):
    def _harness(self, page_name, compact=True):
        harness = _Harness(page_name)
        harness._compact_mode_override = compact
        harness.icon_button = 'icon-button'
        harness._icon_page_buttons = ['download-button']
        harness.desktop_tab_buttons = {'main': 'main-tab', 'options': 'options-tab'}
        harness._asset_page_state = {'css': {'add_button': 'css-add', 'dropdown': 'css-dd'}, 'javascript': {}}
        return harness

    def test_desktop_layout_leaves_focus_alone(self):
        harness = self._harness('main', compact=False)
        with mock.patch.object(layout_module, 'should_prevent_input_autofocus', return_value=False), \
            mock.patch.object(layout_module, 'focus_neutral_widget') as focus, \
            mock.patch.object(GLib, 'idle_add') as idle_add:
            self.assertFalse(harness._focus_mobile_neutral_target())
            harness._schedule_mobile_focus_reset()
        focus.assert_not_called()
        idle_add.assert_not_called()

    def test_first_available_slot_gets_focus(self):
        expectations = {
            'main': 'icon-button',
            'options': 'options-tab',
            'icon': 'download-button',
            'css_assets': 'css-add',
            'javascript_assets': 'icon-button',
        }
        for page_name, target in expectations.items():
            with self.subTest(page_name=page_name):
                harness = self._harness(page_name)
                with mock.patch.object(layout_module, 'should_prevent_input_autofocus', return_value=False), \
                    mock.patch.object(layout_module, 'focus_neutral_widget', return_value=True) as focus:
                    self.assertTrue(harness._focus_mobile_neutral_target())
                focus.assert_called_once_with(harness, target)

    def test_phosh_forces_neutral_focus_even_on_wide_layouts(self):
        harness = self._harness('main', compact=False)
        with mock.patch.object(layout_module, 'should_prevent_input_autofocus', return_value=True), \
            mock.patch.object(layout_module, 'focus_neutral_widget', return_value=True) as focus:
            self.assertTrue(harness._focus_mobile_neutral_target())
        focus.assert_called_once_with(harness, 'icon-button')

    def test_no_candidate_means_no_focus(self):
        harness = _Harness('icon')
        harness._compact_mode_override = True
        with mock.patch.object(layout_module, 'should_prevent_input_autofocus', return_value=False), \
            mock.patch.object(layout_module, 'focus_neutral_widget') as focus:
            self.assertFalse(harness._focus_mobile_neutral_target())
        focus.assert_not_called()

    def test_compact_layout_schedules_the_focus_reset(self):
        harness = self._harness('main')
        with mock.patch.object(layout_module, 'should_prevent_input_autofocus', return_value=False), \
            mock.patch.object(GLib, 'idle_add') as idle_add:
            harness._schedule_mobile_focus_reset()
        idle_add.assert_called_once_with(harness._focus_mobile_neutral_target)


class NavigationTests(unittest.TestCase):
    def test_notify_calls_the_callback_and_swallows_errors(self):
        harness = _Harness()
        harness._notify_navigation_changed()  # no callback: nothing happens
        harness.on_navigation_changed = mock.Mock()
        harness._notify_navigation_changed()
        harness.on_navigation_changed.assert_called_once_with(harness)
        harness.on_navigation_changed = mock.Mock(side_effect=RuntimeError('boom'))
        harness._notify_navigation_changed()  # must not raise
        harness.on_navigation_changed.assert_called_once_with(harness)

    def test_visible_child_change_resets_focus_and_notifies(self):
        harness = _Harness()
        with mock.patch.object(harness, '_schedule_mobile_focus_reset') as focus, \
            mock.patch.object(harness, '_notify_navigation_changed') as notify:
            harness._on_page_stack_visible_child_changed(None, None)
        focus.assert_called_once_with()
        notify.assert_called_once_with()

    def test_tab_bar_is_hidden_on_the_icon_page(self):
        harness = _Harness('icon')
        harness.on_navigation_changed = mock.Mock()
        harness._update_tabbed_navigation_state()
        harness.detail_tab_scroller.set_visible.assert_called_once_with(False)
        harness.desktop_tab_bar.set_visible.assert_called_once_with(False)
        harness.custom_assets_row.set_visible.assert_called_once_with(False)
        self.assertTrue(harness.desktop_tab_buttons['main'].active)
        harness.on_navigation_changed.assert_called_once_with(harness)

    def test_tab_bar_is_shown_on_tab_pages(self):
        harness = _Harness('options')
        harness._update_tabbed_navigation_state()
        harness.desktop_tab_bar.set_visible.assert_called_once_with(True)
        self.assertTrue(harness.desktop_tab_buttons['options'].active)


class ScrollPositionTests(unittest.TestCase):
    def test_capture(self):
        harness = _Harness()
        cases = [
            (_Scrolled(_Adjustment(value=321.5)), 321.5),
            (_Scrolled(_Adjustment(value=-4)), 0.0),
            (_Scrolled(None), 0.0),
            (_Scrolled(fail=True), 0.0),
            (_Scrolled(_Adjustment(fail=True)), 0.0),
        ]
        for scrolled, expected in cases:
            with self.subTest(expected=expected):
                harness._detail_main_scroll_position = 99.0
                harness.scrolled = scrolled
                harness._capture_main_page_scroll_position()
                self.assertEqual(harness._detail_main_scroll_position, expected)

    def _restore(self, harness):
        callbacks = []
        with mock.patch.object(GLib, 'idle_add', side_effect=lambda cb: callbacks.append(cb) or 12), \
            mock.patch.object(GLib, 'source_remove') as source_remove:
            harness._restore_main_page_scroll_position()
        self.assertEqual(harness._detail_main_scroll_restore_source_id, 12)
        self.assertEqual(len(callbacks), 1)
        self.assertFalse(callbacks[0]())
        self.assertEqual(harness._detail_main_scroll_restore_source_id, 0)
        return source_remove

    def test_restore_clamps_to_the_scrollable_range(self):
        harness = _Harness()
        adjustment = _Adjustment(upper=500.0, page_size=200.0)
        harness.scrolled = _Scrolled(adjustment)
        harness._detail_main_scroll_position = 450.0
        harness._detail_main_scroll_restore_source_id = 7
        source_remove = self._restore(harness)
        source_remove.assert_called_once_with(7)
        self.assertEqual(adjustment.set_values, [300.0])

    def test_restore_tolerates_missing_or_broken_adjustments(self):
        for scrolled in (_Scrolled(None), _Scrolled(fail=True), _Scrolled(_Adjustment(fail=True))):
            harness = _Harness()
            harness.scrolled = scrolled
            source_remove = self._restore(harness)
            source_remove.assert_not_called()
            if scrolled.adjustment is not None:
                self.assertEqual(scrolled.adjustment.set_values, [])


class ShowPageTests(unittest.TestCase):
    def _harness(self, page_name):
        harness = _Harness(page_name)
        harness._capture_main_page_scroll_position = mock.Mock()
        harness._restore_main_page_scroll_position = mock.Mock()
        harness._update_tabbed_navigation_state = mock.Mock()
        return harness

    def test_leaving_main_captures_the_scroll_position(self):
        harness = self._harness('main')
        harness._show_tab_page('options')
        harness._capture_main_page_scroll_position.assert_called_once_with()
        harness._restore_main_page_scroll_position.assert_not_called()
        self.assertEqual(harness.page_stack.shown, ['options'])
        self.assertTrue(harness.desktop_tab_buttons['options'].active)
        harness._update_tabbed_navigation_state.assert_called_once_with()

    def test_returning_to_main_restores_the_scroll_position(self):
        harness = self._harness('options')
        harness._suspend_change_handlers = True
        harness.show_main_page()
        harness._capture_main_page_scroll_position.assert_not_called()
        harness._restore_main_page_scroll_position.assert_called_once_with()
        self.assertFalse(harness._suspend_change_handlers)

    def test_asset_pages_are_refreshed(self):
        harness = self._harness('options')
        harness.show_asset_page('css')
        harness.show_asset_page('javascript')
        self.assertEqual(harness.page_stack.shown, ['css_assets', 'javascript_assets'])
        self.assertEqual(harness.calls, [('refresh_asset', 'css'), ('refresh_asset', 'javascript')])


class AdaptiveWrapTests(unittest.TestCase):
    def test_without_clamp_the_child_is_returned(self):
        harness = _Harness()
        child = object()
        with mock.patch.object(layout_module, 'Adw', types.SimpleNamespace()):
            self.assertIs(harness._adaptive_wrap_page(child), child)

    def test_clamp_is_configured(self):
        harness = _Harness()
        clamp = mock.Mock()
        fake_adw = types.SimpleNamespace(Clamp=mock.Mock(return_value=clamp))
        with mock.patch.object(layout_module, 'Adw', fake_adw):
            result = harness._adaptive_wrap_page('child', maximum_size=700, tightening_threshold=400)
        self.assertIs(result, clamp)
        clamp.set_maximum_size.assert_called_once_with(700)
        clamp.set_tightening_threshold.assert_called_once_with(400)
        clamp.set_child.assert_called_once_with('child')


def _build_real_page(testcase):
    db = Database(':memory:')
    testcase.addCleanup(db.conn.close)
    db.cursor.execute("INSERT INTO entries(id, title, description, active) VALUES (1, 'T', 'D', 1)")
    db.conn.commit()
    entry = types.SimpleNamespace(id=1, title='T', description='D', active=1)
    engines = [{'id': 1, 'name': 'Firefox', 'command': 'firefox'}]
    with mock.patch.object(page_module, 'get_app_config', return_value={'engines': engines}), \
        mock.patch.object(page_module, 'engine_available', return_value=True), \
        mock.patch('detail_page.assets.list_custom_assets', return_value=[]), \
        mock.patch('detail_page.assets.get_custom_asset', return_value=None), \
        mock.patch.object(GLib, 'idle_add', return_value=0):
        page = DetailPage(entry, db, mock.Mock(), mock.Mock())
    page.save_desktop_file = mock.Mock()
    page._maybe_autofetch_icon = mock.Mock()
    return page


@unittest.skipUnless(WIDGETS_AVAILABLE, 'GTK widgets cannot be constructed here')
class RealPageLayoutTests(unittest.TestCase):
    def setUp(self):
        self.page = _build_real_page(self)
        patcher = mock.patch.object(GLib, 'idle_add', return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(layout_module, 'should_prevent_input_autofocus', return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_compact_layout_values(self):
        page = self.page
        page._compact_mode_override = True
        page._apply_adaptive_layout(force=True)
        self.assertEqual(page.top_row.get_spacing(), 10)
        self.assertEqual(page.icon_button.get_size_request(), (64, 64))
        self.assertEqual(page.export_import_row.get_orientation(), Gtk.Orientation.VERTICAL)
        self.assertEqual(page.add_js_button.get_margin_start(), 20)
        self.assertEqual(page.grid.get_margin_top(), 16)
        self.assertEqual(page.content_box.get_margin_start(), 20)
        self.assertEqual(page.options_page_content.get_margin_start(), 0)
        self.assertEqual(page.icon_page_preview_frame.get_size_request(), (80, 80))
        for button in page._icon_page_buttons:
            self.assertEqual(button.get_halign(), Gtk.Align.FILL)
        css_state = page._asset_page_state['css']
        self.assertEqual(css_state['selector_row'].get_orientation(), Gtk.Orientation.VERTICAL)
        self.assertTrue(css_state['add_button'].get_hexpand())
        self.assertFalse(page.custom_assets_row.get_visible())
        self.assertTrue(page.desktop_tab_bar.get_visible())
        self.assertTrue(page.desktop_tab_buttons['main'].get_active())
        self.assertIs(page.options_section.get_parent(), page.options_page_content)

    def test_wide_layout_values(self):
        page = self.page
        page._compact_mode_override = False
        page._apply_adaptive_layout(force=True)
        self.assertEqual(page.top_row.get_spacing(), 12)
        self.assertEqual(page.icon_button.get_size_request(), (72, 72))
        self.assertEqual(page.export_import_row.get_orientation(), Gtk.Orientation.HORIZONTAL)
        self.assertEqual(page.grid.get_margin_top(), 22)
        self.assertEqual(page.grid.get_column_spacing(), 10)
        self.assertEqual(page.options_page_content.get_margin_start(), 18)
        self.assertEqual(page.icon_page_preview_canvas.get_size_request(), (92, 92))
        css_state = page._asset_page_state['css']
        self.assertEqual(css_state['selector_row'].get_orientation(), Gtk.Orientation.HORIZONTAL)
        self.assertEqual(css_state['add_button'].get_halign(), Gtk.Align.START)

    def test_unchanged_mode_does_not_rebuild(self):
        page = self.page
        page._compact_mode_override = True
        page._apply_adaptive_layout(force=True)
        with mock.patch.object(page, '_clear_grid', wraps=page._clear_grid) as clear:
            page._rebuild_form_layout()
            clear.assert_not_called()
            page._apply_subpage_adaptive_layout()
            page._compact_mode_override = False
            page._rebuild_form_layout()
            clear.assert_called_once_with()

    def test_form_rebuild_places_the_status_row_under_the_address(self):
        page = self.page
        page._rebuild_form_layout(force=True)
        self.assertIs(page.grid.get_child_at(1, 2), page.address_entry)
        self.assertIs(page.grid.get_child_at(1, 3), page.url_status_label)
        self.assertIs(page.grid.get_child_at(0, 4), page.engine_spacer)
        self.assertIs(page.grid.get_child_at(1, 10), page.default_zoom_dropdown)

    def test_clear_grid_removes_every_child(self):
        page = self.page
        self.assertIsNotNone(page.grid.get_first_child())
        page._clear_grid()
        self.assertIsNone(page.grid.get_first_child())

    def test_inline_editor_gutter_is_resynced(self):
        page = self.page
        buffer = object()
        editor = {'buffer': buffer}
        page._code_editors = [{'buffer': object()}, editor]
        state = page._asset_page_state['css']
        state['inline_scrolled'] = mock.Mock()
        state['inline_buffer'] = buffer
        page._asset_page_state['javascript'].pop('inline_scrolled', None)
        page._asset_page_state['javascript'].pop('inline_buffer', None)
        page._compact_mode_override = True
        with mock.patch.object(page, '_sync_code_editor_line_number_visibility') as sync:
            page._apply_subpage_adaptive_layout(force=True)
        sync.assert_called_once_with(editor)
        state['inline_scrolled'].set_min_content_height.assert_called_once_with(150)

    def test_show_icon_page_hides_the_tab_bar(self):
        page = self.page
        page.page_stack.set_visible_child_name('icon')
        page._update_tabbed_navigation_state()
        self.assertFalse(page.desktop_tab_bar.get_visible())
        self.assertTrue(page.is_subpage_visible())
        page.show_main_page()
        self.assertEqual(page._current_page_name(), 'main')
        self.assertTrue(page.desktop_tab_bar.get_visible())


if __name__ == '__main__':
    unittest.main()

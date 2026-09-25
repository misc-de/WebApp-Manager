"""Coverage tests for DetailPageOptionsMixin (detail_page/options.py).

The mixin is exercised without a GTK widget tree: `_OptionsHarness` inherits
the real mixin and supplies hand-written fakes for the widgets, the database
and every collaborator that lives in another mixin. GTK widget constructors,
GLib.idle_add and threading.Thread are replaced inside the module under test,
so worker threads run synchronously and idle callbacks are drained by hand.
"""
import json
import logging
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_options.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

from browser_option_logic import OPTION_SPEC_BY_KEY, browser_state_key, option_ui_label
from browser_option_registry import OPTION_CATEGORY_ORDER, option_category
from detail_page import DetailPage
from detail_page.options import DetailPageOptionsMixin
from webapp_constants import (
    ADDRESS_KEY,
    APP_MODE_KEY,
    COLOR_SCHEME_KEY,
    DEFAULT_ZOOM_KEY,
    MODE_DESKTOP_KEY,
    MODE_MOBILE_KEY,
    ONLY_HTTPS_KEY,
    OPTION_ADBLOCK_KEY,
    OPTION_CLEAR_CACHE_ON_EXIT_KEY,
    OPTION_DISABLE_AI_KEY,
    OPTION_FORCE_PRIVACY_KEY,
    OPTION_KEEP_IN_BACKGROUND_KEY,
    OPTION_NOTIFICATIONS_KEY,
    OPTION_PREVENT_MULTIPLE_STARTS_KEY,
    OPTION_STARTUP_BOOSTER_KEY,
    OPTION_SWIPE_KEY,
    PROFILE_NAME_KEY,
    PROFILE_PATH_KEY,
    USER_AGENT_NAME_KEY,
    USER_AGENT_VALUE_KEY,
)

MODULE = 'detail_page.options'

FIREFOX = {
    'id': 1, 'name': 'Firefox', 'command': 'firefox',
    'user_agents': [{'name': 'Mobile', 'value': 'UA-mobile'}, {'label': 'Desktop', 'value': 'UA-desktop'}],
}
CHROME = {'id': 2, 'name': 'Chrome', 'command': 'google-chrome', 'user_agents': {'Tablet': 'UA-tablet', 'Empty': ''}}
GENERIC = {'id': 3, 'name': 'Web', 'command': 'epiphany'}
ENGINES = [FIREFOX, CHROME, GENERIC]


# --- fakes -----------------------------------------------------------------

class FakeWidget:
    """Stand-in for any GTK widget. Records state and emits notify signals
    like GTK does when a property actually changes."""

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.text = kwargs.get('label', '')
        self.active = False
        self.selected = 0
        self.sensitive = True
        self.visible = True
        self.handlers = {}
        self.css = set()
        self.children = []
        self.parent = None
        self.controllers = []
        self.attached = []
        self.labels = None
        self.markup = None

    def __getattr__(self, name):
        # Layout setters (set_halign, set_margin_top, ...) are irrelevant here.
        if name.startswith('set_'):
            return lambda *args, **kwargs: None
        raise AttributeError(name)

    def connect(self, signal, callback):
        self.handlers.setdefault(signal, []).append(callback)

    def emit(self, signal, *args):
        for callback in list(self.handlers.get(signal, [])):
            callback(self, *args)

    def set_text(self, text):
        self.text = text

    def get_text(self):
        return self.text

    def set_markup(self, markup):
        self.markup = markup

    def set_active(self, value):
        value = bool(value)
        changed = value != self.active
        self.active = value
        if changed:
            self.emit('notify::active', None)

    def get_active(self):
        return self.active

    def set_selected(self, index):
        changed = index != self.selected
        self.selected = index
        if changed:
            self.emit('notify::selected', None)

    def get_selected(self):
        return self.selected

    def set_sensitive(self, value):
        self.sensitive = bool(value)

    def set_visible(self, value):
        self.visible = bool(value)

    def add_css_class(self, name):
        self.css.add(name)

    def remove_css_class(self, name):
        self.css.discard(name)

    def append(self, child):
        child.parent = self
        self.children.append(child)

    def remove(self, child):
        # Containers hold appended children, grids hold attached ones.
        in_children = child in self.children
        in_grid = any(item[0] is child for item in self.attached)
        if not (in_children or in_grid):
            raise ValueError('widget is not a child of this container')
        if in_children:
            self.children.remove(child)
        self.attached = [item for item in self.attached if item[0] is not child]

    def get_first_child(self):
        return self.children[0] if self.children else None

    def get_next_sibling(self):
        siblings = self.parent.children
        index = siblings.index(self)
        return siblings[index + 1] if index + 1 < len(siblings) else None

    def add_controller(self, controller):
        self.controllers.append(controller)

    def attach(self, widget, column, row, width, height):
        self.attached.append((widget, column, row, width, height))


class _FakeDropDown:
    @staticmethod
    def new_from_strings(labels):
        widget = FakeWidget()
        widget.labels = list(labels)
        return widget


FAKE_GTK = SimpleNamespace(
    Switch=FakeWidget,
    Box=FakeWidget,
    Label=FakeWidget,
    EventControllerMotion=FakeWidget,
    DropDown=_FakeDropDown,
    Align=SimpleNamespace(START='start', END='end', CENTER='center', FILL='fill'),
    Orientation=SimpleNamespace(VERTICAL='vertical', HORIZONTAL='horizontal'),
)
FAKE_PANGO = SimpleNamespace(EllipsizeMode=SimpleNamespace(END='end'))


class FakeGLib:
    def __init__(self):
        self.queue = []

    def idle_add(self, callback, *args):
        self.queue.append((callback, args))
        return len(self.queue)

    def drain(self):
        while self.queue:
            callback, args = self.queue.pop(0)
            callback(*args)


class SyncThread:
    """threading.Thread replacement that runs the target on start()."""

    def __init__(self, target=None, daemon=None):
        self.target = target
        self.daemon = daemon

    def start(self):
        self.target()


class FakeDB:
    def __init__(self, options=None):
        self.options = dict(options or {})
        self.add_option_calls = []
        self.add_options_calls = []
        self.update_entry_calls = []
        self.fail_reload = None

    def add_option(self, entry_id, key, value, commit=True):
        self.add_option_calls.append((entry_id, key, value, commit))
        self.options[key] = value

    def add_options(self, entry_id, updates):
        self.add_options_calls.append((entry_id, dict(updates)))
        self.options.update(updates)

    def get_options_for_entry(self, entry_id):
        if self.fail_reload is not None:
            raise self.fail_reload
        return [(index, entry_id, key, value) for index, (key, value) in enumerate(self.options.items(), start=1)]

    def update_entry(self, entry_id, **fields):
        self.update_entry_calls.append((entry_id, fields))


COLLABORATORS = (
    'save_desktop_file', '_set_inline_busy', '_show_plugin_banner', '_set_plugin_activity',
    '_refresh_profile_button_label', '_refresh_header_meta', '_refresh_asset_pages',
    '_emit_visual_changed', '_trigger_address_validation', '_set_detail_action_status',
    '_sync_icon_filename', '_update_url_status',
)


class _OptionsHarness(DetailPageOptionsMixin):
    _safe_int = DetailPage._safe_int

    def __init__(self, options=None, selected_engine=0, compact=False):
        self.entry = SimpleNamespace(id=7, title='Test App', description='', active=True)
        self.db = FakeDB(options)
        self._options_cache = dict(options or {})
        self.engines_list = [dict(engine) for engine in ENGINES]
        self.engine_user_agents = self._build_engine_user_agents()
        self.engine_dropdown = FakeWidget()
        self.engine_dropdown.selected = selected_engine
        self.option_names = list(OPTION_SPEC_BY_KEY)
        self.switches = {}
        self._option_row_widgets = {}
        self._suspend_change_handlers = False
        self._suspend_address_processing = False
        self._syncing_browser_state = False
        self._plugin_operation_serial = 0
        self._plugin_operation_in_progress = False
        self._options_compact = None
        self.compact = compact
        self.config = {}
        self.options_container = FakeWidget()
        self.grid = FakeWidget()
        self.user_agent_dropdown = FakeWidget()
        self.mode_dropdown = FakeWidget()
        self.mode_desktop_dropdown = FakeWidget()
        for widget in (self.user_agent_dropdown, self.mode_dropdown, self.mode_desktop_dropdown):
            self.grid.attach(widget, 1, 0, 1, 1)
        self.user_agent_status = FakeWidget()
        self.browser_option_status = FakeWidget()
        self.address_entry = FakeWidget()
        self.color_scheme_dropdown = FakeWidget()
        self.default_zoom_dropdown = FakeWidget()
        self.color_scheme_values = ['auto', 'light', 'dark']
        self.default_zoom_values = ['50', '67', '80', '90', '100', '110', '125', '150', '175', '200']
        self.mode_values = ['standard', 'kiosk', 'app', 'seamless']
        self.on_title_changed = mock.Mock()
        self.on_visual_changed = None
        self.exportable = True
        for name in COLLABORATORS:
            setattr(self, name, mock.Mock(name=name))

    def _is_compact_layout(self):
        return self.compact

    def _has_exportable_webapp(self):
        return self.exportable


class _PatchedTestCase(unittest.TestCase):
    """Replaces GTK constructors, GLib, threading and i18n in the module."""

    def setUp(self):
        self.glib = FakeGLib()
        for target, replacement in (
            ('Gtk', FAKE_GTK),
            ('Pango', FAKE_PANGO),
            ('GLib', self.glib),
            ('threading', SimpleNamespace(Thread=SyncThread)),
            ('t', lambda key, **kwargs: key),
        ):
            patcher = mock.patch(f'{MODULE}.{target}', replacement)
            patcher.start()
            self.addCleanup(patcher.stop)


# --- simple accessors ------------------------------------------------------

class SimpleAccessorTests(_PatchedTestCase):
    def test_option_names_in_order_is_a_copy(self):
        page = _OptionsHarness()
        names = page._option_names_in_order()
        names.append('extra')
        self.assertNotIn('extra', page.option_names)

    def test_boolean_helpers_delegate_to_option_state(self):
        page = _OptionsHarness({OPTION_DISABLE_AI_KEY: '1', OPTION_NOTIFICATIONS_KEY: '1'})
        self.assertFalse(page._ui_boolean_option_active(OPTION_DISABLE_AI_KEY))
        self.assertTrue(page._ui_boolean_option_active(OPTION_NOTIFICATIONS_KEY))
        self.assertEqual(page._store_boolean_option_value(OPTION_DISABLE_AI_KEY, True), '0')

    def test_options_dict_is_a_copy(self):
        page = _OptionsHarness({'a': '1'})
        copy = page._options_dict()
        copy['a'] = '2'
        self.assertEqual(page._options_cache['a'], '1')

    def test_get_current_engine_uses_one_based_dropdown_index(self):
        page = _OptionsHarness()
        self.assertIsNone(page._get_current_engine())
        page.engine_dropdown.selected = 2
        self.assertEqual(page._get_current_engine()['id'], 2)
        page.engine_dropdown.selected = 99
        self.assertIsNone(page._get_current_engine())

    def test_engine_by_id(self):
        page = _OptionsHarness()
        self.assertEqual(page._engine_by_id('3')['name'], 'Web')
        self.assertIsNone(page._engine_by_id('42'))
        self.assertIsNone(page._engine_by_id(None))
        self.assertIsNone(page._engine_by_id('abc'))

    def test_browser_family_and_state_key(self):
        page = _OptionsHarness(selected_engine=1)
        self.assertEqual(page._current_browser_family(), 'firefox')
        self.assertEqual(page._sync_browser_state_key(), browser_state_key('firefox'))
        self.assertEqual(page._sync_browser_state_key('chrome'), browser_state_key('chrome'))

    def test_plugin_option_name(self):
        page = _OptionsHarness()
        self.assertEqual(page._plugin_option_name(OPTION_ADBLOCK_KEY), 'adblock')
        self.assertEqual(page._plugin_option_name(OPTION_SWIPE_KEY), 'swipe')
        self.assertEqual(page._plugin_option_name(OPTION_NOTIFICATIONS_KEY), '')

    def test_mode_label_for_value(self):
        page = _OptionsHarness()
        self.assertEqual(page._mode_label_for_value('kiosk'), 'mode_kiosk')
        self.assertEqual(page._mode_label_for_value('seamless'), 'mode_seamless')
        self.assertEqual(page._mode_label_for_value('bogus'), 'mode_standard')
        self.assertEqual(page._normalize_mode_value('fullscreen'), 'kiosk')

    def test_only_https_and_address_normalization(self):
        page = _OptionsHarness()
        self.assertFalse(page._is_only_https_enabled())
        self.assertEqual(page._normalize_address_for_ui(' http://x.example '), 'http://x.example')
        page.switches[ONLY_HTTPS_KEY] = FakeWidget()
        page.switches[ONLY_HTTPS_KEY].active = True
        self.assertTrue(page._is_only_https_enabled())
        self.assertEqual(page._normalize_address_for_ui('http://x.example'), 'https://x.example')
        self.assertEqual(page._normalize_address_for_ui(None), '')

    def test_update_export_button_state(self):
        page = _OptionsHarness()
        page._update_export_button_state()  # no export_button attribute: silently nothing
        page.export_button = FakeWidget()
        page.exportable = False
        page._update_export_button_state()
        self.assertFalse(page.export_button.sensitive)


# --- user agents -------------------------------------------------------------

class UserAgentTests(_PatchedTestCase):
    def test_normalize_user_agents_accepts_dicts_lists_and_strings(self):
        page = _OptionsHarness()
        self.assertEqual(page._normalize_user_agents({'A': 'ua-a', 'B': ''}), [{'name': 'A', 'value': 'ua-a'}])
        self.assertEqual(
            page._normalize_user_agents([{'label': 'L', 'value': 'v1'}, {'value': 'v2'}, {'name': 'x'}, 'plain', '', 5]),
            [{'name': 'L', 'value': 'v1'}, {'name': 'v2', 'value': 'v2'}, {'name': 'plain', 'value': 'plain'}],
        )
        self.assertEqual(page._normalize_user_agents(None), [])

    def test_engine_user_agents_are_built_per_engine(self):
        page = _OptionsHarness()
        self.assertEqual([item['name'] for item in page.engine_user_agents[1]], ['Mobile', 'Desktop'])
        self.assertEqual(page.engine_user_agents[2], [{'name': 'Tablet', 'value': 'UA-tablet'}])
        self.assertEqual(page.engine_user_agents[3], [])

    def test_default_user_agent_for_engine(self):
        page = _OptionsHarness()
        self.assertIsNone(page._default_user_agent_for_engine(None))
        self.assertEqual(page._default_user_agent_for_engine(FIREFOX)['value'], 'UA-mobile')
        self.assertIsNone(page._default_user_agent_for_engine(GENERIC))

    def test_resolve_without_engine(self):
        self.assertEqual(_OptionsHarness()._resolve_user_agent_selection(None), (0, None))

    def test_resolve_by_name_and_value(self):
        page = _OptionsHarness({USER_AGENT_NAME_KEY: 'Desktop', USER_AGENT_VALUE_KEY: 'UA-desktop'})
        index, item = page._resolve_user_agent_selection(FIREFOX)
        self.assertEqual((index, item['name']), (2, 'Desktop'))

    def test_resolve_by_value_renames_stale_stored_name(self):
        page = _OptionsHarness({USER_AGENT_NAME_KEY: 'Old Name', USER_AGENT_VALUE_KEY: 'UA-desktop'})
        index, _item = page._resolve_user_agent_selection(FIREFOX)
        self.assertEqual(index, 2)
        self.assertEqual(page._options_cache[USER_AGENT_NAME_KEY], 'Desktop')

    def test_resolve_by_value_with_matching_name_does_not_write(self):
        page = _OptionsHarness({USER_AGENT_VALUE_KEY: 'UA-mobile', USER_AGENT_NAME_KEY: 'Mobile '})
        self.assertEqual(page._resolve_user_agent_selection(FIREFOX)[0], 1)
        self.assertEqual(page.db.add_options_calls, [])

    def test_resolve_persists_default_only_when_nothing_is_stored(self):
        page = _OptionsHarness()
        self.assertEqual(page._resolve_user_agent_selection(FIREFOX)[0], 0)
        index, _item = page._resolve_user_agent_selection(FIREFOX, persist_default=True)
        self.assertEqual(index, 1)
        self.assertEqual(page._options_cache[USER_AGENT_VALUE_KEY], 'UA-mobile')

    def test_resolve_unknown_stored_name_returns_none(self):
        page = _OptionsHarness({USER_AGENT_NAME_KEY: 'Gone'})
        self.assertEqual(page._resolve_user_agent_selection(FIREFOX, persist_default=True), (0, None))

    def test_refresh_user_agent_options_replaces_the_dropdown(self):
        page = _OptionsHarness({USER_AGENT_NAME_KEY: 'Desktop'}, selected_engine=1)
        old = page.user_agent_dropdown
        page.on_user_agent_changed = mock.Mock()
        page.refresh_user_agent_options()
        new = page.user_agent_dropdown
        self.assertIsNot(new, old)
        self.assertEqual(new.labels, ['user_agent_none', 'Mobile', 'Desktop'])
        self.assertEqual(new.selected, 2)
        self.assertIn((new, 1, 6, 1, 1), page.grid.attached)
        self.assertNotIn(old, [item[0] for item in page.grid.attached])
        self.assertTrue(new.sensitive)
        self.assertEqual(page.user_agent_status.text, '')
        self.assertFalse(page.user_agent_status.visible)
        # The programmatic selection must not look like a user change.
        self.assertFalse(page._suspend_change_handlers)

    def test_refresh_user_agent_options_for_engine_without_presets(self):
        page = _OptionsHarness(selected_engine=3)
        page.refresh_user_agent_options()
        self.assertEqual(page.user_agent_dropdown.labels, ['user_agent_none'])
        self.assertEqual(page.user_agent_status.text, 'user_agent_unavailable')

    def test_refresh_user_agent_options_without_engine_is_insensitive(self):
        page = _OptionsHarness()
        page.refresh_user_agent_options()
        self.assertFalse(page.user_agent_dropdown.sensitive)
        self.assertEqual(page.user_agent_status.text, '')

    def test_on_user_agent_changed(self):
        page = _OptionsHarness(selected_engine=1)
        page._suspend_change_handlers = True
        page.on_user_agent_changed(SimpleNamespace(get_selected=lambda: 1), None)
        page.save_desktop_file.assert_not_called()
        page._suspend_change_handlers = False
        page.on_user_agent_changed(SimpleNamespace(get_selected=lambda: 2), None)
        self.assertEqual(page._options_cache[USER_AGENT_VALUE_KEY], 'UA-desktop')
        page.on_user_agent_changed(SimpleNamespace(get_selected=lambda: 0), None)
        self.assertEqual(page._options_cache[USER_AGENT_NAME_KEY], '')
        self.assertEqual(page._options_cache[USER_AGENT_VALUE_KEY], '')
        self.assertEqual(page.save_desktop_file.call_count, 2)


# --- option layout -----------------------------------------------------------

class OptionLayoutTests(_PatchedTestCase):
    def test_create_option_switch_reflects_stored_value_and_saves_on_toggle(self):
        page = _OptionsHarness({OPTION_NOTIFICATIONS_KEY: '1'})
        page.save_boolean_option = mock.Mock()
        switch = page._create_option_switch(OPTION_NOTIFICATIONS_KEY)
        self.assertTrue(switch.active)
        self.assertIs(page.switches[OPTION_NOTIFICATIONS_KEY], switch)
        self.assertIn('boolean-switch', switch.css)
        switch.set_active(False)
        page.save_boolean_option.assert_called_once_with(OPTION_NOTIFICATIONS_KEY, False)

    def test_create_option_switch_defaults_for_missing_values(self):
        page = _OptionsHarness()
        page.save_boolean_option = mock.Mock()
        self.assertFalse(page._create_option_switch(OPTION_KEEP_IN_BACKGROUND_KEY).active)
        self.assertTrue(page._create_option_switch(OPTION_DISABLE_AI_KEY).active)
        self.assertFalse(page._create_option_switch(OPTION_NOTIFICATIONS_KEY).active)

    def test_clear_options_container_removes_every_child(self):
        page = _OptionsHarness()
        for _ in range(3):
            page.options_container.append(FakeWidget())
        page.switches = {'x': FakeWidget()}
        page._clear_options_container()
        self.assertEqual(page.options_container.children, [])
        self.assertEqual(page.switches, {})

    def test_supported_option_names_without_engine_is_empty(self):
        self.assertEqual(_OptionsHarness()._supported_option_names(None), set())

    def test_keep_in_background_needs_furios_for_firefox(self):
        page = _OptionsHarness()
        with mock.patch(f'{MODULE}.is_furios_distribution', return_value=False):
            self.assertNotIn(OPTION_KEEP_IN_BACKGROUND_KEY, page._supported_option_names(FIREFOX))
            self.assertIn(OPTION_KEEP_IN_BACKGROUND_KEY, page._supported_option_names(CHROME))
        with mock.patch(f'{MODULE}.is_furios_distribution', return_value=True):
            self.assertIn(OPTION_KEEP_IN_BACKGROUND_KEY, page._supported_option_names(FIREFOX))

    def test_grouped_visible_option_names_follow_category_order_and_label_sort(self):
        page = _OptionsHarness(selected_engine=1)
        with mock.patch(f'{MODULE}.is_furios_distribution', return_value=True):
            groups = page._grouped_visible_option_names()
            flat = page._visible_option_names_in_order()
        categories = [category for category, _ in groups]
        self.assertEqual(categories, [c for c in OPTION_CATEGORY_ORDER if c in categories])
        for category, names in groups:
            self.assertTrue(all(option_category(name) == category for name in names))
            self.assertEqual(names, sorted(names, key=lambda name: option_ui_label(name).casefold()))
        self.assertEqual(flat, [name for _, names in groups for name in names])
        self.assertIn(OPTION_SWIPE_KEY, flat)

    def test_no_engine_means_no_visible_options(self):
        page = _OptionsHarness()
        self.assertEqual(page._grouped_visible_option_names(), [])
        self.assertEqual(page._visible_option_names_in_order(), [])

    def test_build_options_layout_creates_headers_rows_and_spacers(self):
        page = _OptionsHarness({OPTION_NOTIFICATIONS_KEY: '1'}, selected_engine=2)
        column = page._build_options_layout(compact=True)
        groups = page._grouped_visible_option_names()
        row_count = sum(len(names) for _, names in groups)
        self.assertEqual(len(column.children), len(groups) * 2 - 1 + row_count)
        headers = [child for child in column.children if 'heading' in child.css]
        self.assertEqual([h.text for h in headers], [f'option_category_{c}' for c, _ in groups])
        self.assertEqual(set(page._option_row_widgets), {name for _, names in groups for name in names})
        label, switch_wrap, switch, row = page._option_row_widgets[OPTION_NOTIFICATIONS_KEY]
        self.assertIs(switch, page.switches[OPTION_NOTIFICATIONS_KEY])
        self.assertTrue(switch.active)
        self.assertIsNotNone(label.markup)
        self.assertIn(switch, switch_wrap.children)
        self.assertIn('option-row', row.css)
        motion = row.controllers[0]
        motion.emit('enter', 0, 0)
        self.assertIn('option-row-hover', row.css)
        motion.emit('leave')
        self.assertNotIn('option-row-hover', row.css)

    def test_build_options_layout_survives_a_failing_css_class(self):
        class RaisingBox(FakeWidget):
            def add_css_class(self, name):
                if name == 'option-row':
                    raise RuntimeError('theme without option-row')
                super().add_css_class(name)

        page = _OptionsHarness(selected_engine=2)
        with mock.patch.object(FAKE_GTK, 'Box', RaisingBox):
            column = page._build_options_layout()
        self.assertTrue(page._option_row_widgets)
        self.assertTrue(column.children)

    def test_apply_boolean_switch_values_does_not_trigger_saves(self):
        page = _OptionsHarness({OPTION_NOTIFICATIONS_KEY: '1'})
        switch = FakeWidget()
        switch.connect('notify::active', lambda s, p: page.save_boolean_option(OPTION_NOTIFICATIONS_KEY, s.get_active()))
        page.switches = {OPTION_NOTIFICATIONS_KEY: switch}
        page._apply_boolean_switch_values()
        self.assertTrue(switch.active)
        self.assertEqual(page.db.add_option_calls, [])
        self.assertFalse(page._suspend_change_handlers)

    def test_rebuild_options_layout_only_when_layout_changes(self):
        page = _OptionsHarness(selected_engine=2, compact=True)
        page.export_button = FakeWidget()
        page._rebuild_options_layout()
        self.assertEqual(len(page.options_container.children), 1)
        self.assertTrue(page._options_compact)
        first_column = page.options_container.children[0]
        page._rebuild_options_layout()
        self.assertIs(page.options_container.children[0], first_column)
        page._rebuild_options_layout(force=True)
        self.assertIsNot(page.options_container.children[0], first_column)
        self.assertEqual(len(page.options_container.children), 1)


# --- modes -------------------------------------------------------------------

class ModeTests(_PatchedTestCase):
    def test_mode_value_accessors(self):
        page = _OptionsHarness({APP_MODE_KEY: '1', MODE_DESKTOP_KEY: 'kiosk'})
        self.assertEqual(page._current_mode_value(), 'app')
        self.assertEqual(page._current_mobile_mode_value(), 'app')
        self.assertEqual(page._current_desktop_mode_value(), 'kiosk')
        self.assertEqual(page._current_mode_index(), 2)
        page.mode_values = ['standard']
        self.assertEqual(page._current_mode_index(), 0)
        self.assertEqual(page._index_for_mode_value('kiosk'), 0)
        page.mode_values = ['standard', 'kiosk']
        self.assertEqual(page._index_for_mode_value('kiosk'), 1)

    def test_apply_mode_value_writes_flags_and_mobile_mode(self):
        page = _OptionsHarness()
        page._apply_mode_value('seamless')
        self.assertEqual(
            {key: page._options_cache[key] for key in ('Kiosk', APP_MODE_KEY, 'Frameless', MODE_MOBILE_KEY)},
            {'Kiosk': '0', APP_MODE_KEY: '1', 'Frameless': '1', MODE_MOBILE_KEY: 'seamless'},
        )
        page._apply_mode_value('nonsense')
        self.assertEqual(page._options_cache[MODE_MOBILE_KEY], 'standard')
        self.assertEqual(page._options_cache[APP_MODE_KEY], '0')

    def test_apply_desktop_mode_value(self):
        page = _OptionsHarness()
        page._apply_desktop_mode_value('KIOSK')
        self.assertEqual(page._options_cache[MODE_DESKTOP_KEY], 'kiosk')

    def test_on_mode_changed(self):
        page = _OptionsHarness()
        page._suspend_change_handlers = True
        page.on_mode_changed(SimpleNamespace(get_selected=lambda: 1), None)
        self.assertNotIn(MODE_MOBILE_KEY, page._options_cache)
        page._suspend_change_handlers = False
        page.on_mode_changed(SimpleNamespace(get_selected=lambda: 1), None)
        self.assertEqual(page._options_cache[MODE_MOBILE_KEY], 'kiosk')
        page.on_mode_changed(SimpleNamespace(get_selected=lambda: 17), None)
        self.assertEqual(page._options_cache[MODE_MOBILE_KEY], 'standard')
        self.assertEqual(page.save_desktop_file.call_count, 2)

    def test_on_desktop_mode_changed(self):
        page = _OptionsHarness()
        page._suspend_change_handlers = True
        page.on_desktop_mode_changed(SimpleNamespace(get_selected=lambda: 2), None)
        self.assertNotIn(MODE_DESKTOP_KEY, page._options_cache)
        page._suspend_change_handlers = False
        page.on_desktop_mode_changed(SimpleNamespace(get_selected=lambda: 2), None)
        self.assertEqual(page._options_cache[MODE_DESKTOP_KEY], 'app')
        page.on_desktop_mode_changed(SimpleNamespace(get_selected=lambda: None), None)
        self.assertEqual(page._options_cache[MODE_DESKTOP_KEY], 'standard')
        self.assertEqual(page.save_desktop_file.call_count, 2)

    def test_available_mode_items_keep_the_current_mode_visible(self):
        page = _OptionsHarness({APP_MODE_KEY: '1', 'Frameless': '1'}, selected_engine=2)
        values = [value for value, _ in page._available_mode_items()]
        self.assertEqual(values, ['standard', 'kiosk', 'app', 'seamless'])
        page = _OptionsHarness(selected_engine=2)
        self.assertEqual([value for value, _ in page._available_mode_items()], ['standard', 'kiosk', 'app'])

    def test_refresh_mode_options_rebuilds_both_dropdowns(self):
        page = _OptionsHarness({MODE_MOBILE_KEY: 'app', MODE_DESKTOP_KEY: 'kiosk'}, selected_engine=2)
        page.refresh_mode_options()
        self.assertEqual(page.mode_values, ['standard', 'kiosk', 'app'])
        self.assertEqual(page.mode_dropdown.labels, ['mode_standard', 'mode_kiosk', 'mode_app'])
        self.assertEqual(page.mode_dropdown.selected, 2)
        self.assertEqual(page.mode_desktop_dropdown.selected, 1)
        self.assertIn((page.mode_dropdown, 1, 7, 1, 1), page.grid.attached)
        self.assertIn((page.mode_desktop_dropdown, 1, 8, 1, 1), page.grid.attached)
        self.assertTrue(page.mode_dropdown.sensitive)
        # Programmatic selection must not have been saved as a user change.
        self.assertEqual(page.db.add_option_calls, [])

    def test_refresh_mode_options_without_engine_is_insensitive(self):
        page = _OptionsHarness()
        page.refresh_mode_options()
        self.assertFalse(page.mode_dropdown.sensitive)
        self.assertFalse(page.mode_desktop_dropdown.sensitive)


# --- option storage ----------------------------------------------------------

class OptionStorageTests(_PatchedTestCase):
    def test_set_option_value_single_key(self):
        page = _OptionsHarness()
        page._set_option_value(ADDRESS_KEY, 'https://a.example', commit=False)
        self.assertEqual(page._options_cache[ADDRESS_KEY], 'https://a.example')
        self.assertEqual(page.db.add_option_calls, [(7, ADDRESS_KEY, 'https://a.example', False)])

    def test_set_option_value_managed_key_syncs_browser_state(self):
        page = _OptionsHarness(selected_engine=1)
        page._set_option_value(OPTION_NOTIFICATIONS_KEY, '1')
        state_key = browser_state_key('firefox')
        self.assertEqual(json.loads(page._options_cache[state_key])[OPTION_NOTIFICATIONS_KEY], '1')
        self.assertIn(state_key, [call[1] for call in page.db.add_option_calls])

    def test_set_option_value_force_privacy_writes_two_keys(self):
        page = _OptionsHarness(selected_engine=1)
        page._set_option_value(OPTION_FORCE_PRIVACY_KEY, '1')
        self.assertEqual(page.db.add_options_calls[0][1], {OPTION_FORCE_PRIVACY_KEY: '1', ONLY_HTTPS_KEY: '1'})
        self.assertEqual(page._options_cache[ONLY_HTTPS_KEY], '1')

    def test_add_options(self):
        page = _OptionsHarness(selected_engine=2)
        page._add_options({})
        self.assertEqual(page.db.add_options_calls, [])
        page._add_options({'EngineName': None})
        self.assertEqual(page._options_cache['EngineName'], '')
        self.assertNotIn(browser_state_key('chrome'), page._options_cache)
        page._add_options({OPTION_NOTIFICATIONS_KEY: '1'})
        self.assertIn(browser_state_key('chrome'), page._options_cache)

    def test_sync_current_browser_state_guards(self):
        page = _OptionsHarness()
        page._sync_current_browser_state()  # generic family
        self.assertEqual(page.db.add_option_calls, [])
        page = _OptionsHarness(selected_engine=1)
        page._syncing_browser_state = True
        page._sync_current_browser_state()
        self.assertEqual(page.db.add_option_calls, [])

    def test_sync_current_browser_state_writes_and_resets_the_flag(self):
        page = _OptionsHarness({OPTION_NOTIFICATIONS_KEY: '1', 'Kiosk': '1'}, selected_engine=1)
        page._sync_current_browser_state(commit=False)
        key, payload, commit = page.db.add_option_calls[0][1:]
        self.assertEqual(key, browser_state_key('firefox'))
        self.assertFalse(commit)
        self.assertEqual(json.loads(payload).get(OPTION_NOTIFICATIONS_KEY), '1')
        self.assertNotIn('Kiosk', json.loads(payload))
        self.assertFalse(page._syncing_browser_state)

    def test_restore_browser_state_for_family(self):
        stored = json.dumps({OPTION_NOTIFICATIONS_KEY: '1'})
        page = _OptionsHarness({browser_state_key('chrome'): stored})
        page._restore_browser_state_for_family('generic')
        self.assertEqual(page.db.add_options_calls, [])
        page._restore_browser_state_for_family('chrome')
        self.assertEqual(page._options_cache[OPTION_NOTIFICATIONS_KEY], '1')

    def test_reload_options_cache_from_db(self):
        page = _OptionsHarness()
        page.db.options = {ADDRESS_KEY: 'https://db.example'}
        page._reload_options_cache_from_db()
        self.assertEqual(page._options_cache, {ADDRESS_KEY: 'https://db.example'})
        page.db.fail_reload = OSError('db gone')
        page._reload_options_cache_from_db()
        self.assertEqual(page._options_cache, {ADDRESS_KEY: 'https://db.example'})

    def test_reload_from_db_reapplies_controls(self):
        page = _OptionsHarness()
        page.db.options = {ADDRESS_KEY: 'https://db.example'}
        page.reload_from_db()
        self.assertEqual(page.address_entry.text, 'https://db.example')


class ApplyOptionValuesTests(_PatchedTestCase):
    def test_controls_follow_the_cache(self):
        options = {
            ADDRESS_KEY: 'https://x.example',
            'EngineID': '2',
            COLOR_SCHEME_KEY: ' Dark ',
            DEFAULT_ZOOM_KEY: '999',
            OPTION_NOTIFICATIONS_KEY: '1',
            APP_MODE_KEY: '1',
        }
        page = _OptionsHarness(options)
        page._update_desktop_name_source_buttons = mock.Mock()
        page.switches = {OPTION_NOTIFICATIONS_KEY: FakeWidget()}
        page._apply_option_values_to_controls()
        self.assertEqual(page.address_entry.text, 'https://x.example')
        self.assertEqual(page.engine_dropdown.selected, 2)
        self.assertEqual(page.color_scheme_dropdown.selected, 2)
        self.assertEqual(page.default_zoom_dropdown.selected, page.default_zoom_values.index('100'))
        self.assertEqual(page.user_agent_dropdown.selected, 1)
        self.assertEqual(page._options_cache[USER_AGENT_VALUE_KEY], 'UA-tablet')
        self.assertTrue(page.switches[OPTION_NOTIFICATIONS_KEY].active)
        self.assertEqual(page.mode_dropdown.selected, page.mode_values.index('app'))
        self.assertFalse(page._suspend_change_handlers)
        page._update_desktop_name_source_buttons.assert_called_once_with()
        page._refresh_profile_button_label.assert_called_once_with()
        page._refresh_header_meta.assert_called_once_with()
        page._refresh_asset_pages.assert_called_once_with()

    def test_missing_values_fall_back_to_defaults(self):
        page = _OptionsHarness({'EngineID': '', COLOR_SCHEME_KEY: 'sepia'})
        page.engine_dropdown.selected = 1
        page.address_entry.text = ''
        page._apply_option_values_to_controls()
        self.assertEqual(page.engine_dropdown.selected, 0)
        self.assertEqual(page.color_scheme_dropdown.selected, 0)
        self.assertEqual(page.default_zoom_dropdown.selected, page.default_zoom_values.index('100'))
        self.assertEqual(page.user_agent_dropdown.selected, 0)

    def test_suspend_flag_is_restored_after_an_error(self):
        page = _OptionsHarness()
        page.refresh_user_agent_options = mock.Mock(side_effect=RuntimeError('boom'))
        with self.assertRaises(RuntimeError):
            page._apply_option_values_to_controls()
        self.assertFalse(page._suspend_change_handlers)


class BrowserDependentControlsTests(_PatchedTestCase):
    def test_widgets_follow_the_engine(self):
        page = _OptionsHarness(selected_engine=2)
        page._engine_option_widgets = [FakeWidget(), FakeWidget()]
        switch = FakeWidget()
        switch.sensitive = False
        page._option_row_widgets = {'a': [None, None, switch, None], 'b': [None, None]}
        page._update_browser_dependent_controls()
        self.assertTrue(all(widget.visible for widget in page._engine_option_widgets))
        self.assertTrue(switch.sensitive)
        self.assertEqual(page.browser_option_status.text, '')

        page.engine_dropdown.selected = 0
        page._update_browser_dependent_controls()
        self.assertFalse(any(widget.visible for widget in page._engine_option_widgets))
        self.assertFalse(switch.sensitive)

    def test_adblock_hint_for_non_firefox_engine_listing_adblock(self):
        page = _OptionsHarness(selected_engine=2)
        page._visible_option_names_in_order = lambda: [OPTION_ADBLOCK_KEY]
        page._update_browser_dependent_controls()
        self.assertEqual(page.browser_option_status.text, 'option_adblock_unavailable')
        page.engine_dropdown.selected = 1
        page._update_browser_dependent_controls()
        self.assertEqual(page.browser_option_status.text, '')


# --- plugin (extension) operations ------------------------------------------

class PluginSaveTests(_PatchedTestCase):
    def _firefox_page(self, **options):
        page = _OptionsHarness(dict({'EngineID': '1'}, **options), selected_engine=1)
        page.switches = {OPTION_SWIPE_KEY: FakeWidget(), OPTION_NOTIFICATIONS_KEY: FakeWidget()}
        return page

    def test_non_firefox_engine_just_saves(self):
        page = _OptionsHarness(selected_engine=2)
        page._run_plugin_save_async(OPTION_SWIPE_KEY)
        page.save_desktop_file.assert_called_once_with()
        self.assertEqual(page._plugin_operation_serial, 0)

    def test_non_plugin_option_just_saves(self):
        page = self._firefox_page()
        page._run_plugin_save_async(OPTION_NOTIFICATIONS_KEY)
        page.save_desktop_file.assert_called_once_with()

    def test_worker_applies_settings_and_schedules_finish(self):
        page = self._firefox_page(**{PROFILE_PATH_KEY: '/p/old', PROFILE_NAME_KEY: 'old'})
        profile_info = {'profile_name': 'new', 'profile_path': '/p/new'}
        with mock.patch(f'{MODULE}.ensure_browser_profile', return_value=profile_info) as ensure, \
                mock.patch(f'{MODULE}.apply_profile_settings', return_value={'extension_errors': {'swipe': 'unsigned-extension-payload'}}), \
                mock.patch(f'{MODULE}.export_desktop_file', return_value={'normalized_address': ''}) as export:
            page._run_plugin_save_async(OPTION_SWIPE_KEY)
        self.assertEqual(page._plugin_operation_serial, 1)
        self.assertTrue(page._plugin_operation_in_progress)
        self.assertFalse(any(switch.sensitive for switch in page.switches.values()))
        page._set_plugin_activity.assert_called_once_with('plugin_installing', active=True)
        page._set_inline_busy.assert_called_once_with(True, 'plugin_installing')
        page._show_plugin_banner.assert_called_once_with('plugin_install_info')
        self.assertEqual(ensure.call_args.kwargs, {'stored_profile_name': 'old', 'stored_profile_path': '/p/old'})
        export.assert_called_once()
        callback, args = self.glib.queue[0]
        self.assertEqual(callback, page._finish_plugin_save)
        self.assertEqual(args, (1, OPTION_SWIPE_KEY, 'swipe', profile_info, {'normalized_address': ''}, 'unsigned-extension-payload'))

    def test_worker_skips_export_when_not_needed_and_profile_missing(self):
        page = self._firefox_page()
        with mock.patch(f'{MODULE}.ensure_browser_profile', return_value=None), \
                mock.patch(f'{MODULE}.apply_profile_settings') as apply:
            page._run_plugin_save_async(OPTION_ADBLOCK_KEY)
        apply.assert_not_called()
        self.assertEqual(self.glib.queue[0][1], (1, OPTION_ADBLOCK_KEY, 'adblock', None, None, ''))

    def test_worker_reports_expected_and_unexpected_errors(self):
        for error in (OSError('disk full'), RuntimeError('surprise')):
            with self.subTest(error=type(error).__name__):
                self.glib.queue.clear()
                page = self._firefox_page()
                with mock.patch(f'{MODULE}.ensure_browser_profile', side_effect=error):
                    page._run_plugin_save_async(OPTION_SWIPE_KEY)
                self.assertEqual(self.glib.queue[0][1][-1], str(error))

    def test_worker_tolerates_a_missing_apply_result(self):
        page = self._firefox_page(**{PROFILE_PATH_KEY: '/p/same'})
        with mock.patch(f'{MODULE}.ensure_browser_profile', return_value={'profile_path': '/p/same'}), \
                mock.patch(f'{MODULE}.apply_profile_settings', return_value=None), \
                mock.patch.object(page, '_plugin_export_needed', return_value=False), \
                mock.patch(f'{MODULE}.export_desktop_file') as export:
            page._run_plugin_save_async(OPTION_SWIPE_KEY)
        export.assert_not_called()
        self.assertEqual(self.glib.queue[0][1][-1], '')

    def test_plugin_export_needed_checks_the_desktop_file(self):
        page = _OptionsHarness()
        with mock.patch(f'{MODULE}.get_expected_desktop_path', return_value=None):
            self.assertFalse(page._plugin_export_needed({'profile_path': '/p'}, '/p'))
        missing = mock.Mock(spec=Path)
        missing.exists.return_value = False
        with mock.patch(f'{MODULE}.get_expected_desktop_path', return_value=missing):
            self.assertTrue(page._plugin_export_needed({'profile_path': None}, ''))
        missing.exists.return_value = True
        with mock.patch(f'{MODULE}.get_expected_desktop_path', return_value=missing):
            self.assertFalse(page._plugin_export_needed({'profile_path': '/p'}, '/p'))

    def test_verified_plugin_state_without_profile(self):
        page = _OptionsHarness({OPTION_SWIPE_KEY: '1'})
        with mock.patch(f'{MODULE}.firefox_extension_installed') as installed, \
                mock.patch(f'{MODULE}.read_profile_settings') as read:
            self.assertEqual(page._verified_plugin_state(OPTION_SWIPE_KEY, 'swipe'), (True, False, '0'))
        installed.assert_not_called()
        read.assert_not_called()

    def test_verified_plugin_state_reads_the_profile(self):
        page = _OptionsHarness({OPTION_SWIPE_KEY: '0', PROFILE_PATH_KEY: '/p'})
        with mock.patch(f'{MODULE}.firefox_extension_installed', return_value=True), \
                mock.patch(f'{MODULE}.read_profile_settings', return_value={OPTION_SWIPE_KEY: 1}):
            self.assertEqual(page._verified_plugin_state(OPTION_SWIPE_KEY, 'swipe'), (False, True, '1'))

    def test_verified_plugin_state_survives_unreadable_settings(self):
        page = _OptionsHarness({PROFILE_PATH_KEY: '/p'})
        with mock.patch(f'{MODULE}.firefox_extension_installed', return_value=True), \
                mock.patch(f'{MODULE}.read_profile_settings', side_effect=ValueError('bad json')):
            self.assertEqual(page._verified_plugin_state(OPTION_SWIPE_KEY, 'swipe'), (False, True, '1'))

    def test_finish_ignores_a_stale_serial(self):
        page = self._firefox_page()
        page._plugin_operation_serial = 5
        page._plugin_operation_in_progress = True
        self.assertFalse(page._finish_plugin_save(4, OPTION_SWIPE_KEY, 'swipe', {'profile_path': '/p'}, None, ''))
        self.assertTrue(page._plugin_operation_in_progress)
        self.assertEqual(page.db.add_options_calls, [])

    def test_plugin_option_updates_include_a_changed_address(self):
        page = _OptionsHarness()
        page.address_entry.text = 'http://a.example'
        self.assertEqual(page._plugin_option_updates(None, {'normalized_address': 'https://a.example'}), {ADDRESS_KEY: 'https://a.example'})
        page.address_entry.text = 'https://a.example '
        self.assertEqual(page._plugin_option_updates('junk', {'normalized_address': 'https://a.example'}), {})
        self.assertEqual(page._plugin_option_updates(None, {'normalized_address': None}), {})

    def test_plugin_result_banner_covers_every_outcome(self):
        banner = _OptionsHarness._plugin_result_banner
        self.assertEqual(banner('unsigned-extension-payload', 'adblock', True, False), ('plugin_install_unsigned', 4200))
        self.assertEqual(banner('missing-extension-source', 'swipe', True, False), ('plugin_install_swipe_production_unavailable', 4200))
        self.assertEqual(banner('boom', 'adblock', False, False), ('plugin_install_failed', 0))
        self.assertEqual(banner('', 'adblock', True, True), ('plugin_install_ready_restart', 0))
        self.assertEqual(banner('', 'adblock', True, False), ('plugin_install_failed', 0))
        self.assertEqual(banner('', 'adblock', False, False), ('plugin_remove_ready_restart', 0))
        self.assertEqual(banner('', 'adblock', False, True), (None, 0))

    def test_finish_corrects_the_switch_and_shows_a_timed_banner(self):
        page = self._firefox_page(**{OPTION_SWIPE_KEY: '1', PROFILE_PATH_KEY: '/p'})
        page.db.options = dict(page._options_cache)
        page._plugin_operation_serial = 4
        page._plugin_operation_in_progress = True
        with mock.patch(f'{MODULE}.firefox_extension_installed', return_value=False), \
                mock.patch(f'{MODULE}.read_profile_settings', return_value={}), \
                mock.patch(f'{MODULE}.is_furios_distribution', return_value=True):
            result = page._finish_plugin_save(4, OPTION_SWIPE_KEY, 'swipe', None, None, 'unsigned-extension-payload')
        self.assertFalse(result)
        self.assertEqual(page._options_cache[OPTION_SWIPE_KEY], '0')
        self.assertFalse(page._plugin_operation_in_progress)
        page._set_inline_busy.assert_called_with(False)
        page._set_plugin_activity.assert_called_with('', active=False)
        page._show_plugin_banner.assert_called_with('plugin_install_unsigned_swipe', timeout_ms=4200)
        page.on_title_changed.assert_called_once_with(page.entry)
        self.assertTrue(page.switches[OPTION_SWIPE_KEY].sensitive)
        self.assertIn((page._emit_visual_changed, ()), self.glib.queue)

    def test_finish_success_shows_untimed_banner_and_writes_profile(self):
        page = self._firefox_page(**{OPTION_SWIPE_KEY: '1'})
        page.db.options = dict(page._options_cache)
        page._plugin_operation_serial = 1
        page.on_title_changed = None
        with mock.patch(f'{MODULE}.firefox_extension_installed', return_value=True), \
                mock.patch(f'{MODULE}.read_profile_settings', return_value={OPTION_SWIPE_KEY: '1'}):
            page._finish_plugin_save(1, OPTION_SWIPE_KEY, 'swipe', {'profile_name': 'n', 'profile_path': '/p'}, None, '')
        self.assertEqual(page._options_cache[PROFILE_PATH_KEY], '/p')
        page._show_plugin_banner.assert_called_with('plugin_install_ready_restart')

    def test_finish_with_stale_removal_state_shows_no_banner(self):
        page = self._firefox_page(**{OPTION_SWIPE_KEY: '0', PROFILE_PATH_KEY: '/p'})
        page.db.options = dict(page._options_cache)
        page._plugin_operation_serial = 1
        with mock.patch(f'{MODULE}.firefox_extension_installed', return_value=True), \
                mock.patch(f'{MODULE}.read_profile_settings', return_value={OPTION_SWIPE_KEY: '0'}):
            page._finish_plugin_save(1, OPTION_SWIPE_KEY, 'swipe', None, None, '')
        page._show_plugin_banner.assert_not_called()


# --- event handlers ----------------------------------------------------------

class HandlerTests(_PatchedTestCase):
    def test_on_switch_toggled_updates_entry_and_db(self):
        page = _OptionsHarness()
        page._update_desktop_name_source_buttons = mock.Mock()
        page.on_switch_toggled(SimpleNamespace(get_active=lambda: False), None)
        self.assertFalse(page.entry.active)
        self.assertEqual(page.db.update_entry_calls, [(7, {'active': False})])
        page._update_desktop_name_source_buttons.assert_called_once_with()
        page.save_desktop_file.assert_called_once_with()
        self.assertEqual(self.glib.queue, [(page._emit_visual_changed, ())])

    def test_on_switch_toggled_without_name_source_buttons(self):
        page = _OptionsHarness()
        page.on_switch_toggled(SimpleNamespace(get_active=lambda: True), None)
        self.assertTrue(page.entry.active)

    def test_color_scheme_and_zoom_handlers(self):
        page = _OptionsHarness()
        page._suspend_change_handlers = True
        page.on_color_scheme_changed(SimpleNamespace(get_selected=lambda: 1), None)
        page.on_default_zoom_changed(SimpleNamespace(get_selected=lambda: 1), None)
        self.assertEqual(page.db.add_option_calls, [])
        page._suspend_change_handlers = False
        page.on_color_scheme_changed(SimpleNamespace(get_selected=lambda: 2), None)
        self.assertEqual(page._options_cache[COLOR_SCHEME_KEY], 'dark')
        page.on_color_scheme_changed(SimpleNamespace(get_selected=lambda: 9), None)
        self.assertEqual(page._options_cache[COLOR_SCHEME_KEY], 'auto')
        page.on_default_zoom_changed(SimpleNamespace(get_selected=lambda: 0), None)
        self.assertEqual(page._options_cache[DEFAULT_ZOOM_KEY], '50')
        page.on_default_zoom_changed(SimpleNamespace(get_selected=lambda: 99), None)
        self.assertEqual(page._options_cache[DEFAULT_ZOOM_KEY], '100')
        self.assertEqual(page.save_desktop_file.call_count, 4)


class EngineChangeTests(_PatchedTestCase):
    def test_suspended_handler_does_nothing(self):
        page = _OptionsHarness()
        page._suspend_change_handlers = True
        page.on_engine_changed(page.engine_dropdown, None)
        self.assertEqual(page.db.add_options_calls, [])

    def test_clearing_the_engine(self):
        page = _OptionsHarness({'EngineID': '1', 'EngineName': 'Firefox', USER_AGENT_NAME_KEY: 'Mobile'})
        page.export_button = FakeWidget()
        page.exportable = False
        with mock.patch(f'{MODULE}.export_desktop_file') as export:
            page.on_engine_changed(page.engine_dropdown, None)
        export.assert_not_called()
        self.assertEqual(page._options_cache['EngineID'], '')
        self.assertEqual(page._options_cache[USER_AGENT_NAME_KEY], '')
        self.assertFalse(page.export_button.sensitive)
        page._set_inline_busy.assert_not_called()

    def test_switching_engine_keeps_mode_and_exports_in_worker(self):
        options = {'EngineID': '2', APP_MODE_KEY: '1', ADDRESS_KEY: 'http://x.example'}
        page = _OptionsHarness(options, selected_engine=1)
        page.address_entry.text = 'http://x.example'
        result = {'profile_name': 'n', 'profile_path': '/p', 'normalized_address': 'https://x.example', 'profile_migrated': True}
        with mock.patch(f'{MODULE}.export_desktop_file', return_value=result), \
                mock.patch(f'{MODULE}.is_furios_distribution', return_value=False):
            page.on_engine_changed(page.engine_dropdown, None)
            self.assertEqual(page._options_cache['EngineID'], '1')
            self.assertEqual(page._options_cache['EngineName'], 'Firefox')
            self.assertEqual(page._options_cache[APP_MODE_KEY], '1')
            self.assertTrue(page.options_container.children)
            page._set_inline_busy.assert_called_once_with(True, 'engine_switch_loading')
            page._sync_icon_filename.assert_called_once_with()
            self.glib.drain()
        page._set_inline_busy.assert_called_with(False)
        self.assertEqual(page._options_cache[PROFILE_PATH_KEY], '/p')
        self.assertEqual(page._options_cache[ADDRESS_KEY], 'https://x.example')
        self.assertEqual(page.address_entry.text, 'https://x.example')
        self.assertFalse(page._suspend_address_processing)
        page._trigger_address_validation.assert_called_once_with('https://x.example', debounce=False, export_after_validation=False)
        page._show_plugin_banner.assert_called_once_with('profile_import_completed')
        page._refresh_header_meta.assert_called()
        page._refresh_profile_button_label.assert_called()
        page.on_title_changed.assert_called_once_with(page.entry)
        page._emit_visual_changed.assert_called_once_with()
        page._set_detail_action_status.assert_not_called()

    def test_switching_engine_restores_the_new_family_state(self):
        # Regression: the previous engine's state was saved under the new family (read from the
        # dropdown) and overwrote the state the new engine should restore.
        options = {
            'EngineID': '1',
            OPTION_NOTIFICATIONS_KEY: '1',
            browser_state_key('chrome'): json.dumps({OPTION_NOTIFICATIONS_KEY: '0'}),
        }
        page = _OptionsHarness(options, selected_engine=2)
        with mock.patch(f'{MODULE}.export_desktop_file', return_value=None):
            page.on_engine_changed(page.engine_dropdown, None)
        self.assertEqual(page._options_cache[OPTION_NOTIFICATIONS_KEY], '0')
        self.assertEqual(json.loads(page._options_cache[browser_state_key('firefox')])[OPTION_NOTIFICATIONS_KEY], '1')

    def test_export_failure_after_engine_change_is_reported(self):
        page = _OptionsHarness(selected_engine=2)
        page.on_title_changed = None
        with mock.patch(f'{MODULE}.export_desktop_file', side_effect=OSError('read-only')):
            page.on_engine_changed(page.engine_dropdown, None)
            self.glib.drain()
        page._set_detail_action_status.assert_called_once_with('read-only')
        page._show_plugin_banner.assert_not_called()
        self.assertNotIn(PROFILE_PATH_KEY, page._options_cache)

    def test_export_result_with_unchanged_address(self):
        page = _OptionsHarness(selected_engine=2)
        page.address_entry.text = 'https://same.example'
        with mock.patch(f'{MODULE}.export_desktop_file', return_value={'normalized_address': 'https://same.example'}):
            page.on_engine_changed(page.engine_dropdown, None)
            self.glib.drain()
        page._trigger_address_validation.assert_not_called()
        self.assertEqual(page._options_cache[PROFILE_NAME_KEY], '')


class ApplyProfileSettingsOnlyTests(_PatchedTestCase):
    def test_without_engine_only_reloads(self):
        page = _OptionsHarness()
        page.db.options = {ADDRESS_KEY: 'https://db.example'}
        with mock.patch(f'{MODULE}.ensure_browser_profile') as ensure:
            page._apply_profile_settings_only()
        ensure.assert_not_called()
        self.assertEqual(page.address_entry.text, 'https://db.example')

    def test_applies_settings_and_stores_profile(self):
        page = _OptionsHarness({PROFILE_NAME_KEY: 'old'}, selected_engine=1)
        info = {'profile_name': 'new', 'profile_path': '/p/new', 'profile_migrated': True}
        with mock.patch(f'{MODULE}.ensure_browser_profile', return_value=info) as ensure, \
                mock.patch(f'{MODULE}.apply_profile_settings') as apply:
            page._apply_profile_settings_only()
        self.assertEqual(ensure.call_args.args[1], 'firefox')
        self.assertEqual(ensure.call_args.kwargs['stored_profile_name'], 'old')
        apply.assert_called_once()
        self.assertEqual(page.db.options[PROFILE_PATH_KEY], '/p/new')
        self.assertEqual(page._options_cache[PROFILE_PATH_KEY], '/p/new')
        page._show_plugin_banner.assert_called_once_with('profile_import_completed')
        page.on_title_changed.assert_called_once_with(page.entry)

    def test_missing_profile_and_os_errors_still_refresh(self):
        for side_effect in (None, OSError('nope')):
            with self.subTest(side_effect=side_effect):
                page = _OptionsHarness(selected_engine=2)
                page.on_title_changed = None
                with mock.patch(f'{MODULE}.ensure_browser_profile', return_value=None, side_effect=side_effect), \
                        mock.patch(f'{MODULE}.apply_profile_settings') as apply:
                    page._apply_profile_settings_only()
                apply.assert_not_called()
                page._refresh_profile_button_label.assert_called()
                page._show_plugin_banner.assert_not_called()


class SaveBooleanOptionTests(_PatchedTestCase):
    def test_suspended_handler_writes_nothing(self):
        page = _OptionsHarness()
        page._suspend_change_handlers = True
        page.save_boolean_option(OPTION_NOTIFICATIONS_KEY, True)
        self.assertEqual(page.db.add_option_calls, [])

    def test_disable_ai_is_stored_inverted(self):
        page = _OptionsHarness()
        page._apply_profile_settings_only = mock.Mock()
        page.save_boolean_option(OPTION_DISABLE_AI_KEY, True)
        self.assertEqual(page._options_cache[OPTION_DISABLE_AI_KEY], '0')
        page._apply_profile_settings_only.assert_called_once_with()
        self.assertEqual(self.glib.queue, [(page._emit_visual_changed, ())])

    def test_only_https_rewrites_an_http_address(self):
        page = _OptionsHarness()
        page._apply_profile_settings_only = mock.Mock()
        page.switches[ONLY_HTTPS_KEY] = FakeWidget()
        page.switches[ONLY_HTTPS_KEY].active = True
        page.address_entry.text = 'http://x.example'
        page.save_boolean_option(ONLY_HTTPS_KEY, True)
        self.assertEqual(page.address_entry.text, 'https://x.example')
        page._update_url_status.assert_not_called()

    def test_force_privacy_on_https_address_revalidates(self):
        page = _OptionsHarness(selected_engine=2)
        page._apply_profile_settings_only = mock.Mock()
        page.address_entry.text = 'https://x.example'
        page.save_boolean_option(OPTION_FORCE_PRIVACY_KEY, True)
        self.assertEqual(page._options_cache[ADDRESS_KEY], 'https://x.example')
        self.assertEqual(page._options_cache[ONLY_HTTPS_KEY], '1')
        page._update_url_status.assert_called_once_with('https://x.example')
        page._apply_profile_settings_only.assert_called_once_with()

    def test_force_privacy_for_generic_engine_does_not_touch_address(self):
        page = _OptionsHarness(selected_engine=3)
        page._apply_profile_settings_only = mock.Mock()
        page.save_boolean_option(OPTION_FORCE_PRIVACY_KEY, True)
        self.assertNotIn(ADDRESS_KEY, page._options_cache)

    def test_extension_options_run_the_plugin_path(self):
        page = _OptionsHarness(selected_engine=1)
        page._run_plugin_save_async = mock.Mock()
        page.save_boolean_option(OPTION_SWIPE_KEY, True)
        page._run_plugin_save_async.assert_called_once_with(OPTION_SWIPE_KEY)

    def test_extension_option_during_running_operation_only_repaints(self):
        page = _OptionsHarness(selected_engine=1)
        page._plugin_operation_in_progress = True
        page._run_plugin_save_async = mock.Mock()
        page.save_boolean_option(OPTION_ADBLOCK_KEY, True)
        page._run_plugin_save_async.assert_not_called()
        self.assertEqual(page._options_cache[OPTION_ADBLOCK_KEY], '1')
        self.assertEqual(self.glib.queue, [(page._emit_visual_changed, ())])

    def test_profile_like_kinds_apply_profile_settings(self):
        for key in (OPTION_CLEAR_CACHE_ON_EXIT_KEY, OPTION_STARTUP_BOOSTER_KEY, OPTION_PREVENT_MULTIPLE_STARTS_KEY):
            with self.subTest(key=key):
                page = _OptionsHarness()
                page._apply_profile_settings_only = mock.Mock()
                page.save_boolean_option(key, True)
                page._apply_profile_settings_only.assert_called_once_with()
                page.save_desktop_file.assert_not_called()

    def test_unknown_option_just_saves_the_desktop_file(self):
        page = _OptionsHarness()
        page.save_boolean_option('Frameless', True)
        self.assertEqual(page._options_cache['Frameless'], '1')
        page.save_desktop_file.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()

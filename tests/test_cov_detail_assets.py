"""Coverage tests for DetailPageAssetsMixin (custom CSS/JS pages of the editor).

No widget tree is built: Gtk and GtkSource are replaced inside
detail_page.assets by fakes that hand out fresh mock widgets, and a harness
borrows the real mixin methods while hand-implementing what they call back
into (option storage, desktop-file save, layout hooks). The asset library is
served from an in-memory list, so no user config is touched.
"""
import logging
import sys
import types
import unittest
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_detail_assets.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import custom_assets  # noqa: E402
from detail_page import assets as da  # noqa: E402
from detail_page.assets import DetailPageAssetsMixin  # noqa: E402


class FakeBuffer:
    def __init__(self, text=''):
        self.text = text
        self.set_calls = []
        self.handlers = {}
        self.scheme = 'unset'
        self.language = None

    def get_start_iter(self):
        return 'start'

    def get_end_iter(self):
        return 'end'

    def get_text(self, start, end, include_hidden):
        assert (start, end, include_hidden) == ('start', 'end', True)
        return self.text

    def set_text(self, text):
        self.set_calls.append(text)
        self.text = text

    def connect(self, signal, callback):
        self.handlers[signal] = callback


class SchemeBuffer(FakeBuffer):
    def set_style_scheme(self, scheme):
        self.scheme = scheme


def _widget(*_args, **_kwargs):
    return mock.MagicMock()


def make_fake_gtk():
    gtk = mock.MagicMock()
    for name in ('Box', 'Label', 'Button', 'ScrolledWindow'):
        getattr(gtk, name).side_effect = _widget

    def text_view(*_a, **_k):
        view = mock.MagicMock()
        view.get_buffer.return_value = FakeBuffer()
        return view

    gtk.TextView.side_effect = text_view

    def dropdown(labels):
        widget = mock.MagicMock()
        widget.labels = list(labels)
        return widget

    gtk.DropDown.new_from_strings.side_effect = dropdown
    return gtk


class FakeSchemeManager:
    def __init__(self, schemes, ids=(), raise_on=(), ids_error=False):
        self.schemes = schemes
        self.ids = list(ids)
        self.raise_on = set(raise_on)
        self.ids_error = ids_error

    def get_scheme(self, name):
        if name in self.raise_on:
            raise RuntimeError(name)
        return self.schemes.get(name)

    def get_scheme_ids(self):
        if self.ids_error:
            raise RuntimeError('ids')
        return self.ids


def make_fake_gtksource(manager, languages=None, language_error=False, view_error=False):
    source = types.SimpleNamespace()
    source.StyleSchemeManager = types.SimpleNamespace(get_default=lambda: manager)

    class LanguageManager:
        def get_language(self, name):
            if language_error:
                raise TypeError('no languages')
            return (languages or {}).get(name)

    source.LanguageManager = types.SimpleNamespace(get_default=lambda: LanguageManager())
    source.Buffer = SchemeBuffer

    def new_with_buffer(buffer):
        view = mock.MagicMock()
        view.buffer = buffer
        if view_error:
            view.set_show_line_numbers.side_effect = AttributeError
        return view

    source.View = types.SimpleNamespace(new_with_buffer=new_with_buffer)
    return source


class _Harness(DetailPageAssetsMixin):
    def __init__(self, options=None, dark=False, compact=False, family='chromium'):
        self.options = dict(options or {})
        self.set_calls = []
        self.saves = 0
        self._code_editors = []
        self._suspend_change_handlers = False
        self._inline_editor_save_source_ids = {'css': 0, 'javascript': 0}
        self._asset_page_state = {}
        self._style_manager = mock.Mock()
        self._style_manager.get_dark.return_value = dark
        self.compact = compact
        self.family = family
        self.page_stack = mock.Mock()
        self.layout_refreshes = []
        self.dialogs = []

    def _get_option_value(self, key):
        return self.options.get(key)

    def _set_option_value(self, key, value, commit=True):
        self.set_calls.append((key, value, commit))
        self.options[key] = value

    def save_desktop_file(self):
        self.saves += 1

    def _is_compact_layout(self):
        return self.compact

    def _adaptive_wrap_page(self, page):
        return ('wrapped', page)

    def _apply_subpage_adaptive_layout(self, force=False):
        self.layout_refreshes.append(force)

    def _current_browser_family(self):
        return self.family

    def _present_choice_dialog(self, anchor, message, callback, destructive=False):
        self.dialogs.append((anchor, message, callback, destructive))


LIBRARY = [
    {'id': 'c1', 'name': 'Dark', 'type': 'css', 'filename': 'c1.css', 'imported_at': '2024-01-02T03:04:05+00:00', 'sha256': ''},
    {'id': 'c2', 'name': 'Wide', 'type': 'css', 'filename': 'c2.css', 'imported_at': '', 'sha256': ''},
    {'id': 'j1', 'name': 'Helper', 'type': 'javascript', 'filename': 'j1.js', 'imported_at': '', 'sha256': ''},
]


class _Base(unittest.TestCase):
    def setUp(self):
        self.gtk = make_fake_gtk()
        self.glib = mock.MagicMock()
        self.glib.timeout_add.return_value = 99
        patches = [
            mock.patch.object(da, 'Gtk', self.gtk),
            mock.patch.object(da, 'GLib', self.glib),
            mock.patch.object(da, 'GtkSource', None),
            mock.patch.object(da, 't', side_effect=lambda key, **kw: key if not kw else f"{key}|{kw['name']}"),
            mock.patch.object(custom_assets, '_library_metadata', side_effect=lambda settings=None: [dict(item) for item in LIBRARY]),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)


class LinkedAssetTests(_Base):
    def test_option_keys(self):
        h = _Harness()
        self.assertEqual(h._asset_option_key('css'), custom_assets.CUSTOM_CSS_LINKS_KEY)
        self.assertEqual(h._asset_option_key('anything'), custom_assets.CUSTOM_JS_LINKS_KEY)
        self.assertEqual(h._inline_asset_option_key('css'), custom_assets.INLINE_CUSTOM_CSS_KEY)
        self.assertEqual(h._inline_asset_option_key('javascript'), custom_assets.INLINE_CUSTOM_JS_KEY)
        self.assertEqual(h._inline_asset_hash_option_key('css'), custom_assets.INLINE_CUSTOM_CSS_HASH_KEY)
        self.assertEqual(h._inline_asset_hash_option_key('javascript'), custom_assets.INLINE_CUSTOM_JS_HASH_KEY)

    def test_linked_ids_filter_by_type_and_library(self):
        h = _Harness({custom_assets.CUSTOM_CSS_LINKS_KEY: '["c1","j1","zz","c2"]'})
        self.assertEqual(h._linked_asset_ids('css'), ['c1', 'c2'])

    def test_linked_assets_skip_ids_that_disappeared(self):
        h = _Harness()
        with mock.patch.object(h, '_linked_asset_ids', return_value=['c1', 'gone']), \
                mock.patch.object(da, 'get_custom_asset', side_effect=lambda asset_id: {'id': asset_id} if asset_id == 'c1' else None):
            self.assertEqual(h._linked_assets('css'), [{'id': 'c1'}])

    def test_set_linked_assets_encodes_refreshes_and_saves(self):
        h = _Harness()
        with mock.patch.object(h, '_refresh_asset_page') as refresh:
            h._set_linked_assets('javascript', ['j1', 'c1', 'j1'])
        self.assertEqual(h.set_calls, [(custom_assets.CUSTOM_JS_LINKS_KEY, '["j1"]', True)])
        refresh.assert_called_once_with('javascript')
        self.assertEqual(h.saves, 1)

    def test_inline_text_normalises_line_endings(self):
        h = _Harness({custom_assets.INLINE_CUSTOM_CSS_KEY: 'a\r\nb\rc'})
        self.assertEqual(h._get_inline_asset_text('css'), 'a\nb\nc')
        self.assertEqual(h._get_inline_asset_text('javascript'), '')


class BufferTests(_Base):
    def test_set_buffer_text_skips_identical_text(self):
        h = _Harness()
        buffer = FakeBuffer('same')
        h._set_buffer_text_if_needed(buffer, 'same')
        self.assertEqual(buffer.set_calls, [])

    def test_set_buffer_text_suspends_handlers_and_updates_gutter(self):
        h = _Harness()
        buffer = FakeBuffer('old')
        other = {'buffer': FakeBuffer(), 'line_number_buffer': FakeBuffer()}
        editor = {'buffer': buffer, 'line_number_buffer': FakeBuffer(), 'line_number_scrolled': mock.Mock(), 'view': mock.Mock(), 'uses_source_view': False}
        h._code_editors = [other, editor]
        seen = []
        original = buffer.set_text

        def spy(text):
            seen.append(h._suspend_change_handlers)
            original(text)

        buffer.set_text = spy
        h._set_buffer_text_if_needed(buffer, 'a\nb\nc')
        self.assertEqual(seen, [True])
        self.assertFalse(h._suspend_change_handlers)
        self.assertEqual(editor['line_number_buffer'].text, '1\n2\n3')
        self.assertEqual(other['line_number_buffer'].text, '')

    def test_set_buffer_text_restores_flag_on_error(self):
        h = _Harness()
        h._suspend_change_handlers = 'prev'
        buffer = FakeBuffer('old')
        buffer.set_text = mock.Mock(side_effect=RuntimeError('x'))
        with self.assertRaises(RuntimeError):
            h._set_buffer_text_if_needed(buffer, 'new')
        self.assertEqual(h._suspend_change_handlers, 'prev')

    def test_line_count(self):
        h = _Harness()
        self.assertEqual(h._buffer_line_count(FakeBuffer('')), 1)
        self.assertEqual(h._buffer_line_count(FakeBuffer('a\nb\n')), 3)


class StyleSchemeTests(_Base):
    def test_no_gtksource_means_no_scheme(self):
        self.assertIsNone(_Harness()._source_style_scheme_name())

    def test_manager_errors_and_absence(self):
        broken = types.SimpleNamespace(StyleSchemeManager=types.SimpleNamespace(get_default=mock.Mock(side_effect=RuntimeError)))
        with mock.patch.object(da, 'GtkSource', broken):
            self.assertIsNone(_Harness()._source_style_scheme_name())
        with mock.patch.object(da, 'GtkSource', make_fake_gtksource(None)):
            self.assertIsNone(_Harness()._source_style_scheme_name())

    def test_dark_preference_order(self):
        manager = FakeSchemeManager({'oblivion': 'O', 'Adwaita': 'A'}, raise_on={'Adwaita-dark'})
        with mock.patch.object(da, 'GtkSource', make_fake_gtksource(manager)):
            self.assertEqual(_Harness(dark=True)._source_style_scheme_name(), 'oblivion')
            self.assertEqual(_Harness(dark=False)._source_style_scheme_name(), 'Adwaita')
            h = _Harness()
            h._style_manager = None
            self.assertEqual(h._source_style_scheme_name(), 'Adwaita')

    def test_falls_back_to_first_scheme_id(self):
        with mock.patch.object(da, 'GtkSource', make_fake_gtksource(FakeSchemeManager({}, ids=['kate', 'x']))):
            self.assertEqual(_Harness()._source_style_scheme_name(), 'kate')
        with mock.patch.object(da, 'GtkSource', make_fake_gtksource(FakeSchemeManager({}, ids=[]))):
            self.assertIsNone(_Harness()._source_style_scheme_name())
        with mock.patch.object(da, 'GtkSource', make_fake_gtksource(FakeSchemeManager({}, ids_error=True))):
            self.assertIsNone(_Harness()._source_style_scheme_name())


class EditorThemeTests(_Base):
    def test_css_classes_follow_dark_mode(self):
        h = _Harness(dark=True)
        view, scrolled, gutter = mock.Mock(), mock.Mock(), mock.Mock()
        broken = mock.Mock()
        broken.remove_css_class.side_effect = TypeError
        broken.add_css_class.side_effect = AttributeError
        h._code_editors = [
            {'view': None, 'buffer': FakeBuffer(), 'scrolled': scrolled},
            {'view': view, 'buffer': FakeBuffer(), 'scrolled': scrolled, 'line_number_view': broken, 'line_number_scrolled': None},
            {'view': view, 'buffer': FakeBuffer(), 'scrolled': gutter},
        ]
        with mock.patch.object(h, '_apply_code_editor_theme', wraps=h._apply_code_editor_theme) as apply:
            h._on_style_manager_dark_changed('ignored', 'args')
        apply.assert_called_once()
        view.add_css_class.assert_called_with('inline-editor-dark')
        view.remove_css_class.assert_any_call('inline-editor-light')
        gutter.add_css_class.assert_called_once_with('inline-editor-dark')
        h._style_manager.get_dark.return_value = False
        h._apply_code_editor_theme()
        view.add_css_class.assert_called_with('inline-editor-light')

    def test_source_buffers_get_the_style_scheme(self):
        manager = FakeSchemeManager({'Adwaita': 'SCHEME'})
        h = _Harness()
        source_buffer = SchemeBuffer()
        plain_buffer = FakeBuffer()
        failing = SchemeBuffer()
        failing.set_style_scheme = mock.Mock(side_effect=TypeError)
        h._code_editors = [
            {'view': mock.Mock(), 'buffer': source_buffer, 'scrolled': mock.Mock()},
            {'view': mock.Mock(), 'buffer': plain_buffer, 'scrolled': mock.Mock()},
            {'view': mock.Mock(), 'buffer': failing, 'scrolled': mock.Mock()},
        ]
        with mock.patch.object(da, 'GtkSource', make_fake_gtksource(manager)):
            h._apply_code_editor_theme()
        self.assertEqual(source_buffer.scheme, 'SCHEME')
        self.assertEqual(plain_buffer.scheme, 'unset')
        failing.set_style_scheme.assert_called_once_with('SCHEME')

    def test_scheme_lookup_skipped_when_manager_vanishes(self):
        # The name is resolved first; if the manager is gone on the second
        # lookup, the buffer is handed None rather than crashing.
        h = _Harness()
        buffer = SchemeBuffer()
        h._code_editors = [{'view': mock.Mock(), 'buffer': buffer, 'scrolled': mock.Mock()}]
        source = make_fake_gtksource(None)
        with mock.patch.object(da, 'GtkSource', source), \
                mock.patch.object(h, '_source_style_scheme_name', return_value='Adwaita'):
            h._apply_code_editor_theme()
        self.assertIsNone(buffer.scheme)


class LineNumberTests(_Base):
    def test_gutter_visibility(self):
        view = mock.Mock()
        gutter = mock.Mock()
        editor = {'line_number_scrolled': gutter, 'view': view, 'uses_source_view': True}
        _Harness(compact=False)._sync_code_editor_line_number_visibility(editor)
        gutter.set_visible.assert_called_with(False)
        view.set_show_line_numbers.assert_called_with(True)
        _Harness(compact=True)._sync_code_editor_line_number_visibility(editor)
        gutter.set_visible.assert_called_with(True)
        view.set_show_line_numbers.assert_called_with(False)

    def test_plain_view_always_uses_own_gutter(self):
        view = mock.Mock()
        gutter = mock.Mock()
        _Harness()._sync_code_editor_line_number_visibility({'line_number_scrolled': gutter, 'view': view, 'uses_source_view': False})
        gutter.set_visible.assert_called_once_with(True)
        view.set_show_line_numbers.assert_not_called()

    def test_source_view_errors_and_missing_widgets_are_tolerated(self):
        view = mock.Mock()
        view.set_show_line_numbers.side_effect = TypeError
        _Harness()._sync_code_editor_line_number_visibility({'line_number_scrolled': None, 'view': view, 'uses_source_view': True})
        view.set_show_line_numbers.assert_called_once()

    def test_update_line_numbers_requires_both_buffers(self):
        h = _Harness()
        with mock.patch.object(h, '_sync_code_editor_line_number_visibility') as sync:
            h._update_code_editor_line_numbers({'buffer': None, 'line_number_buffer': FakeBuffer()})
            h._update_code_editor_line_numbers({'buffer': FakeBuffer(), 'line_number_buffer': None})
        sync.assert_not_called()


class BuildCodeEditorTests(_Base):
    def test_plain_textview_editor(self):
        h = _Harness({custom_assets.INLINE_CUSTOM_CSS_KEY: ''})
        box, scrolled, view, buffer = h._build_code_editor('css')
        self.assertIsInstance(buffer, FakeBuffer)
        view.set_monospace.assert_called_once_with(True)
        scrolled.set_child.assert_called_once_with(view)
        self.assertEqual(len(h._code_editors), 1)
        editor = h._code_editors[0]
        self.assertFalse(editor['uses_source_view'])
        self.assertEqual(editor['line_number_buffer'].text, '1')
        editor['line_number_scrolled'].set_vadjustment.assert_called_once_with(scrolled.get_vadjustment.return_value)
        box.append.assert_any_call(scrolled)
        with mock.patch.object(h, '_on_inline_editor_changed') as changed:
            buffer.handlers['changed'](buffer)
        changed.assert_called_once_with('css', buffer, editor)

    def test_editor_tolerates_missing_css_class_support_and_adjustment(self):
        h = _Harness()
        views = []

        def text_view(*_a, **_k):
            view = mock.MagicMock()
            view.get_buffer.return_value = FakeBuffer()
            view.add_css_class.side_effect = AttributeError
            views.append(view)
            return view

        self.gtk.TextView.side_effect = text_view

        def scrolled_window(*_a, **_k):
            widget = mock.MagicMock()
            widget.get_vadjustment.return_value = None
            return widget

        self.gtk.ScrolledWindow.side_effect = scrolled_window
        _box, _scrolled, _view, _buffer = h._build_code_editor('javascript')
        self.assertEqual(len(views), 2)
        h._code_editors[0]['line_number_scrolled'].set_vadjustment.assert_not_called()

    def test_source_view_editor_with_language_fallback(self):
        manager = FakeSchemeManager({'Adwaita': 'S'})
        source = make_fake_gtksource(manager, languages={'javascript': 'JSLANG'})
        h = _Harness()
        with mock.patch.object(da, 'GtkSource', source):
            _box, _scrolled, view, buffer = h._build_code_editor('javascript')
        self.assertIsInstance(buffer, SchemeBuffer)
        self.assertIs(view.buffer, buffer)
        view.set_tab_width.assert_called_once_with(2)
        self.assertTrue(h._code_editors[0]['uses_source_view'])
        self.assertEqual(buffer.scheme, 'S')

    def test_source_view_language_sets_on_buffer(self):
        source = make_fake_gtksource(FakeSchemeManager({}), languages={'css': 'CSSLANG'})
        buffers = []

        class RecordingBuffer(SchemeBuffer):
            def __init__(self):
                super().__init__()
                buffers.append(self)

            def set_language(self, language):
                self.language = language

        source.Buffer = RecordingBuffer
        with mock.patch.object(da, 'GtkSource', source):
            _Harness()._build_code_editor('css')
        self.assertEqual(buffers[0].language, 'CSSLANG')

    def test_source_view_errors_are_tolerated(self):
        source = make_fake_gtksource(FakeSchemeManager({}), language_error=True, view_error=True)
        h = _Harness()
        with mock.patch.object(da, 'GtkSource', source):
            _box, _scrolled, view, buffer = h._build_code_editor('css')
        self.assertIsNone(buffer.language)
        view.set_tab_width.assert_not_called()
        view.set_monospace.assert_called_once_with(True)

    def test_unknown_language_leaves_buffer_plain(self):
        source = make_fake_gtksource(FakeSchemeManager({}), languages={})
        with mock.patch.object(da, 'GtkSource', source):
            _box, _scrolled, _view, buffer = _Harness()._build_code_editor('javascript')
        self.assertIsNone(buffer.language)


class InlineEditorChangeTests(_Base):
    def test_change_schedules_debounced_save_and_replaces_pending_one(self):
        h = _Harness()
        editor = {'buffer': FakeBuffer('x'), 'line_number_buffer': FakeBuffer(), 'line_number_scrolled': None, 'view': None}
        h._on_inline_editor_changed('css', editor['buffer'], editor)
        self.assertEqual(editor['line_number_buffer'].text, '1')
        self.assertEqual(h._inline_editor_save_source_ids['css'], 99)
        self.glib.source_remove.assert_not_called()
        h._on_inline_editor_changed('css', editor['buffer'])
        self.glib.source_remove.assert_called_once_with(99)
        delay, flush = self.glib.timeout_add.call_args.args
        self.assertEqual(delay, 450)
        with mock.patch.object(h, '_persist_inline_asset_text') as persist:
            self.assertFalse(flush())
        persist.assert_called_once_with('css')
        self.assertEqual(h._inline_editor_save_source_ids['css'], 0)

    def test_suspended_handlers_do_not_schedule(self):
        h = _Harness()
        h._suspend_change_handlers = True
        h._on_inline_editor_changed('javascript', FakeBuffer())
        self.glib.timeout_add.assert_not_called()


class PersistInlineTextTests(_Base):
    def test_missing_state_or_buffer_is_a_no_op(self):
        h = _Harness()
        h._persist_inline_asset_text('css')
        h._asset_page_state['css'] = {'inline_buffer': None}
        h._persist_inline_asset_text('css')
        self.assertEqual((h.set_calls, h.saves), ([], 0))

    def test_text_and_hash_are_stored(self):
        h = _Harness()
        h._asset_page_state['javascript'] = {'inline_buffer': FakeBuffer('a()\r\nb()')}
        h._persist_inline_asset_text('javascript')
        digest = custom_assets.asset_content_sha256_from_text('a()\nb()')
        self.assertEqual(h.set_calls, [
            (custom_assets.INLINE_CUSTOM_JS_KEY, 'a()\nb()', False),
            (custom_assets.INLINE_CUSTOM_JS_HASH_KEY, digest, False),
        ])
        self.assertEqual(h.saves, 1)
        # Unchanged text and hash: nothing written again.
        h._persist_inline_asset_text('javascript')
        self.assertEqual(h.saves, 1)

    def test_whitespace_only_text_is_stored_empty(self):
        h = _Harness()
        h._asset_page_state['css'] = {'inline_buffer': FakeBuffer('  \n ')}
        h._persist_inline_asset_text('css')
        self.assertEqual(h.options[custom_assets.INLINE_CUSTOM_CSS_KEY], '')
        self.assertEqual(h.options[custom_assets.INLINE_CUSTOM_CSS_HASH_KEY], custom_assets.asset_content_sha256_from_text(''))


class AssetPageTests(_Base):
    def build(self, h, asset_type):
        with mock.patch.object(h, '_add_selected_asset') as add:
            h._build_asset_page(asset_type)
        return add

    def test_build_page_registers_state_and_stack_child(self):
        h = _Harness()
        self.build(h, 'css')
        state = h._asset_page_state['css']
        self.assertEqual(state['dropdown'].labels, ['detail_asset_dropdown_none'])
        self.assertEqual(state['dropdown_ids'], [])
        self.assertIsInstance(state['inline_buffer'], FakeBuffer)
        h.page_stack.add_named.assert_called_once_with(('wrapped', state['page']), 'css_assets')
        self.build(h, 'javascript')
        self.assertEqual(h.page_stack.add_named.call_args.args[1], 'javascript_assets')
        label_keys = [c.kwargs.get('label') for c in self.gtk.Label.call_args_list]
        self.assertIn('detail_asset_page_title_javascript', label_keys)
        self.assertIn('detail_asset_inline_hint_css', label_keys)

    def test_add_button_adds_the_selected_asset(self):
        h = _Harness()
        buttons = []

        def button(*_a, **kw):
            widget = mock.MagicMock()
            buttons.append(widget)
            return widget

        self.gtk.Button.side_effect = button
        h._build_asset_page('javascript')
        callback = buttons[0].connect.call_args.args[1]
        with mock.patch.object(h, '_add_selected_asset') as add:
            callback(buttons[0])
        add.assert_called_once_with('javascript')

    def test_refresh_all_pages(self):
        h = _Harness()
        h._asset_page_state = {'css': {}, 'javascript': {}}
        with mock.patch.object(h, '_refresh_asset_page') as refresh:
            h._refresh_asset_pages()
        self.assertEqual([c.args[0] for c in refresh.call_args_list], ['css', 'javascript'])

    def test_refresh_unknown_page_is_a_no_op(self):
        h = _Harness()
        h._refresh_asset_page('css')
        self.assertEqual(h.layout_refreshes, [])

    def test_refresh_rebuilds_dropdown_rows_editor_and_note(self):
        h = _Harness({
            custom_assets.CUSTOM_JS_LINKS_KEY: '["j1"]',
            custom_assets.INLINE_CUSTOM_JS_KEY: 'go()',
        }, family='firefox')
        self.build(h, 'javascript')
        state = h._asset_page_state['javascript']
        old_dropdown = state['dropdown']
        parent = old_dropdown.get_parent.return_value
        stale_child = mock.MagicMock()
        stale_child.get_next_sibling.return_value = None
        state['selected_list'].get_first_child.return_value = stale_child
        rows = []

        def box(*_a, **_k):
            widget = mock.MagicMock()
            rows.append(widget)
            return widget

        self.gtk.Box.side_effect = box
        buttons = []
        self.gtk.Button.side_effect = lambda *a, **k: buttons.append(mock.MagicMock()) or buttons[-1]
        with mock.patch.object(custom_assets, 'asset_file_path', side_effect=lambda asset: f"/lib/{asset['filename']}"):
            h._refresh_asset_page('javascript')

        new_dropdown = state['dropdown']
        self.assertIsNot(new_dropdown, old_dropdown)
        self.assertEqual(new_dropdown.labels, ['detail_asset_dropdown_none', 'Helper (JAVASCRIPT)'])
        self.assertEqual(state['dropdown_ids'], ['', 'j1'])
        parent.remove.assert_called_once_with(old_dropdown)
        parent.prepend.assert_called_once_with(new_dropdown)
        self.assertEqual(h.layout_refreshes, [True])
        state['selected_list'].remove.assert_called_once_with(stale_child)
        state['empty_label'].set_visible.assert_called_once_with(False)
        self.assertEqual(state['inline_buffer'].text, 'go()')
        state['note_label'].set_text.assert_called_once_with('detail_asset_firefox_js_note')
        state['note_label'].set_visible.assert_called_with(True)
        # The delete button opens the confirmation for exactly this asset.
        delete_callback = buttons[0].connect.call_args.args[1]
        with mock.patch.object(h, '_confirm_remove_linked_asset') as confirm:
            delete_callback(buttons[0])
        confirm.assert_called_once_with(buttons[0], 'javascript', 'j1', 'Helper')

    def test_refresh_without_links_or_parent_hides_note(self):
        h = _Harness(family='firefox')
        self.build(h, 'css')
        state = h._asset_page_state['css']
        state['dropdown'].get_parent.return_value = None
        state['selected_list'].get_first_child.return_value = None
        state['inline_buffer'] = None
        h._refresh_asset_page('css')
        self.assertEqual(state['dropdown'].labels, ['detail_asset_dropdown_none', 'Dark (CSS)', 'Wide (CSS)'])
        state['empty_label'].set_visible.assert_called_once_with(True)
        state['note_label'].set_visible.assert_called_with(False)

    def test_refresh_tolerates_dropdown_without_parent_api(self):
        h = _Harness()
        self.build(h, 'css')
        state = h._asset_page_state['css']
        state['dropdown'] = object()
        state['selected_list'].get_first_child.return_value = None
        h._refresh_asset_page('css')
        self.assertEqual(state['dropdown_ids'], ['', 'c1', 'c2'])


class AddRemoveTests(_Base):
    def state(self, h, selected, ids):
        dropdown = mock.Mock()
        dropdown.get_selected.return_value = selected
        h._asset_page_state['css'] = {'dropdown': dropdown, 'dropdown_ids': ids}

    def test_add_without_state_or_valid_selection_does_nothing(self):
        h = _Harness()
        with mock.patch.object(h, '_set_linked_assets') as set_linked:
            h._add_selected_asset('css')
            self.state(h, 0, ['', 'c1'])
            h._add_selected_asset('css')
            self.state(h, 5, ['', 'c1'])
            h._add_selected_asset('css')
        set_linked.assert_not_called()

    def test_add_appends_new_asset_once(self):
        h = _Harness({custom_assets.CUSTOM_CSS_LINKS_KEY: '["c1"]'})
        with mock.patch.object(h, '_set_linked_assets') as set_linked:
            self.state(h, 1, ['', 'c1', 'c2'])
            h._add_selected_asset('css')
            set_linked.assert_not_called()
            self.state(h, 2, ['', 'c1', 'c2'])
            h._add_selected_asset('css')
        set_linked.assert_called_once_with('css', ['c1', 'c2'])

    def test_confirm_and_remove(self):
        h = _Harness({custom_assets.CUSTOM_CSS_LINKS_KEY: '["c1","c2"]'})
        h._confirm_remove_linked_asset('anchor', 'css', 'c1', 'Dark')
        anchor, message, callback, destructive = h.dialogs[0]
        self.assertEqual((anchor, message, destructive), ('anchor', 'detail_asset_remove_css_confirm|Dark', True))
        h._confirm_remove_linked_asset('a2', 'javascript', 'j1', 'Helper')
        self.assertEqual(h.dialogs[1][1], 'detail_asset_remove_javascript_confirm|Helper')
        with mock.patch.object(h, '_set_linked_assets') as set_linked:
            self.assertIsNone(callback(False))
            set_linked.assert_not_called()
            callback(True)
        set_linked.assert_called_once_with('css', ['c2'])


if __name__ == '__main__':
    unittest.main()

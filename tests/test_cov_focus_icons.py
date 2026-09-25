"""Tests for focus_guard and ui_icons.

Both modules are thin GTK helpers. Widget constructors and GLib's main loop
are replaced with mocks so the tests run headless and only pin the decisions
the helpers make (which widget gets focus, which icon source is chosen, when
the texture cache is reused or reset).
"""
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_focus_icons.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import focus_guard
import ui_icons
from app_identity import APP_ICON_NAME
from option_config import overview_status_definitions

# focus_guard pins the typelib versions itself before loading GLib.
GLib = focus_guard.GLib


_DESKTOP_VARS = ('XDG_CURRENT_DESKTOP', 'XDG_SESSION_DESKTOP', 'DESKTOP_SESSION')


def _env_without_desktop(**extra):
    env = {key: value for key, value in os.environ.items() if key not in _DESKTOP_VARS}
    env.update(extra)
    return env


class ShouldPreventInputAutofocusTests(unittest.TestCase):
    def test_no_desktop_variables_means_autofocus_is_allowed(self):
        with mock.patch.dict(os.environ, _env_without_desktop(), clear=True):
            self.assertFalse(focus_guard.should_prevent_input_autofocus())

    def test_phosh_in_any_variable_prevents_autofocus(self):
        for name in _DESKTOP_VARS:
            with self.subTest(variable=name):
                env = _env_without_desktop(**{name: ' GNOME:Phosh '})
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertTrue(focus_guard.should_prevent_input_autofocus())

    def test_other_desktops_do_not_prevent_autofocus(self):
        env = _env_without_desktop(XDG_CURRENT_DESKTOP='GNOME', XDG_SESSION_DESKTOP='', DESKTOP_SESSION='plasma')
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(focus_guard.should_prevent_input_autofocus())


class FocusNeutralWidgetTests(unittest.TestCase):
    def test_missing_target_does_nothing(self):
        owner = mock.Mock()
        self.assertFalse(focus_guard.focus_neutral_widget(owner, None))
        owner.get_root.assert_not_called()

    def test_root_focus_is_set_and_target_grabs_focus(self):
        root = mock.Mock()
        owner = mock.Mock()
        owner.get_root.return_value = root
        target = mock.Mock()
        self.assertFalse(focus_guard.focus_neutral_widget(owner, target))
        root.set_focus.assert_called_once_with(target)
        target.grab_focus.assert_called_once_with()

    def test_failing_get_root_still_grabs_focus(self):
        owner = mock.Mock()
        owner.get_root.side_effect = RuntimeError('unrealized')
        target = mock.Mock()
        self.assertFalse(focus_guard.focus_neutral_widget(owner, target))
        target.grab_focus.assert_called_once_with()

    def test_root_without_set_focus_is_skipped(self):
        owner = mock.Mock()
        owner.get_root.return_value = object()
        target = mock.Mock()
        focus_guard.focus_neutral_widget(owner, target)
        target.grab_focus.assert_called_once_with()

    def test_errors_from_set_focus_and_grab_focus_are_swallowed(self):
        root = mock.Mock()
        root.set_focus.side_effect = RuntimeError('boom')
        owner = mock.Mock()
        owner.get_root.return_value = root
        target = mock.Mock()
        target.grab_focus.side_effect = RuntimeError('boom')
        self.assertFalse(focus_guard.focus_neutral_widget(owner, target))
        root.set_focus.assert_called_once_with(target)
        target.grab_focus.assert_called_once_with()


class ScheduleNeutralFocusTests(unittest.TestCase):
    def test_nothing_is_scheduled_outside_phosh(self):
        with mock.patch.object(focus_guard, 'should_prevent_input_autofocus', return_value=False), \
                mock.patch.object(focus_guard.GLib, 'idle_add') as idle_add:
            self.assertEqual(focus_guard.schedule_neutral_focus(mock.Mock(), mock.Mock()), 0)
        idle_add.assert_not_called()

    def _schedule(self, owner, getter):
        with mock.patch.object(focus_guard, 'should_prevent_input_autofocus', return_value=True), \
                mock.patch.object(focus_guard.GLib, 'idle_add', return_value=42) as idle_add:
            source_id = focus_guard.schedule_neutral_focus(owner, getter)
        self.assertEqual(source_id, 42)
        idle_add.assert_called_once()
        return idle_add.call_args[0][0]

    def test_callable_getter_is_resolved_when_the_idle_callback_runs(self):
        owner = mock.Mock()
        owner.get_root.return_value = None
        target = mock.Mock()
        getter = mock.Mock(return_value=target)
        callback = self._schedule(owner, getter)
        getter.assert_not_called()
        self.assertFalse(callback())
        getter.assert_called_once_with()
        target.grab_focus.assert_called_once_with()

    def test_plain_widget_is_accepted_instead_of_a_getter(self):
        owner = mock.Mock()
        owner.get_root.return_value = None
        target = mock.NonCallableMock()
        callback = self._schedule(owner, target)
        self.assertFalse(callback())
        target.grab_focus.assert_called_once_with()

    def test_failing_getter_focuses_nothing(self):
        owner = mock.Mock()
        callback = self._schedule(owner, mock.Mock(side_effect=RuntimeError('gone')))
        self.assertFalse(callback())
        owner.get_root.assert_not_called()


class TextureCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.icon = Path(self._tmp.name) / 'icon.png'
        self.icon.write_bytes(b'not really a png')
        cache_patch = mock.patch.dict(ui_icons._TEXTURE_CACHE, clear=True)
        cache_patch.start()
        self.addCleanup(cache_patch.stop)

    def test_cache_key_uses_path_mtime_and_size(self):
        key = ui_icons._texture_cache_key(self.icon)
        stat_result = self.icon.stat()
        self.assertEqual(key, (str(self.icon), stat_result.st_mtime_ns, stat_result.st_size))

    def test_cache_key_is_none_for_missing_file(self):
        self.assertIsNone(ui_icons._texture_cache_key(self.icon.with_name('missing.png')))

    def test_texture_is_decoded_once_and_then_reused(self):
        texture = object()
        with mock.patch.object(ui_icons.Gdk.Texture, 'new_from_filename', return_value=texture) as loader:
            first = ui_icons.load_icon_paintable(self.icon)
            second = ui_icons.load_icon_paintable(str(self.icon))
        self.assertIs(first, texture)
        self.assertIs(second, texture)
        loader.assert_called_once_with(str(self.icon))

    def test_changed_file_is_decoded_again(self):
        with mock.patch.object(ui_icons.Gdk.Texture, 'new_from_filename', side_effect=[object(), object()]) as loader:
            first = ui_icons.load_icon_paintable(self.icon)
            self.icon.write_bytes(b'a different and longer payload')
            second = ui_icons.load_icon_paintable(self.icon)
        self.assertIsNot(first, second)
        self.assertEqual(loader.call_count, 2)

    def test_decode_error_returns_none_and_caches_nothing(self):
        error = GLib.Error('bad image')
        with mock.patch.object(ui_icons.Gdk.Texture, 'new_from_filename', side_effect=error):
            self.assertIsNone(ui_icons.load_icon_paintable(self.icon))
        self.assertEqual(ui_icons._TEXTURE_CACHE, {})

    def test_missing_file_is_decoded_but_not_cached(self):
        missing = self.icon.with_name('missing.png')
        texture = object()
        with mock.patch.object(ui_icons.Gdk.Texture, 'new_from_filename', return_value=texture):
            self.assertIs(ui_icons.load_icon_paintable(missing), texture)
        self.assertEqual(ui_icons._TEXTURE_CACHE, {})

    def test_full_cache_is_reset_before_storing_a_new_texture(self):
        for index in range(ui_icons._TEXTURE_CACHE_LIMIT):
            ui_icons._TEXTURE_CACHE[(f'/other/{index}', 0, 0)] = object()
        texture = object()
        with mock.patch.object(ui_icons.Gdk.Texture, 'new_from_filename', return_value=texture):
            ui_icons.load_icon_paintable(self.icon)
        self.assertEqual(list(ui_icons._TEXTURE_CACHE.values()), [texture])


class CreateImageFromRefTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.icon = Path(self._tmp.name) / 'icon.png'
        self.icon.write_bytes(b'png')
        self.gtk = mock.MagicMock(name='Gtk')
        gtk_patch = mock.patch.object(ui_icons, 'Gtk', self.gtk)
        gtk_patch.start()
        self.addCleanup(gtk_patch.stop)

    def test_empty_ref_uses_the_fallback_icon_name(self):
        image = ui_icons.create_image_from_ref('  ', pixel_size=28)
        self.assertIs(image, self.gtk.Image.new_from_icon_name.return_value)
        self.gtk.Image.new_from_icon_name.assert_called_once_with(APP_ICON_NAME)
        image.set_pixel_size.assert_called_once_with(28)

    def test_none_ref_uses_a_custom_fallback(self):
        ui_icons.create_image_from_ref(None, fallback_icon='web-browser')
        self.gtk.Image.new_from_icon_name.assert_called_once_with('web-browser')

    def test_non_path_ref_is_treated_as_icon_name(self):
        image = ui_icons.create_image_from_ref('firefox', pixel_size=40)
        self.gtk.Image.new_from_icon_name.assert_called_once_with('firefox')
        image.set_pixel_size.assert_called_once_with(40)

    def test_existing_file_with_texture_becomes_a_picture(self):
        texture = object()
        with mock.patch.object(ui_icons, 'load_icon_paintable', return_value=texture) as loader:
            widget = ui_icons.create_image_from_ref(str(self.icon), pixel_size=40)
        loader.assert_called_once_with(self.icon)
        self.gtk.Picture.new_for_paintable.assert_called_once_with(texture)
        self.assertIs(widget, self.gtk.Picture.new_for_paintable.return_value)
        widget.set_size_request.assert_called_once_with(40, 40)
        widget.set_can_shrink.assert_called_once_with(True)
        widget.set_content_fit.assert_called_once_with(self.gtk.ContentFit.CONTAIN)
        self.gtk.Image.new_from_file.assert_not_called()

    def test_existing_file_without_texture_falls_back_to_image_from_file(self):
        with mock.patch.object(ui_icons, 'load_icon_paintable', return_value=None):
            widget = ui_icons.create_image_from_ref(str(self.icon), pixel_size=20)
        self.gtk.Image.new_from_file.assert_called_once_with(str(self.icon))
        self.assertIs(widget, self.gtk.Image.new_from_file.return_value)
        widget.set_pixel_size.assert_called_once_with(20)
        self.gtk.Picture.new_for_paintable.assert_not_called()


class ActiveStatusIconsTests(unittest.TestCase):
    def test_only_enabled_options_produce_icons(self):
        definitions = overview_status_definitions()
        first_key, first_icon, first_tooltip = definitions[0]
        options = {first_key: '1'}
        for key, _icon, _tooltip in definitions[1:]:
            options[key] = '0'
        self.assertEqual(
            ui_icons.active_status_icons(options),
            [(str(ui_icons.APP_DIR / first_icon), first_tooltip)],
        )

    def test_all_enabled_options_keep_definition_order(self):
        definitions = overview_status_definitions()
        options = {key: '1' for key, _icon, _tooltip in definitions}
        icons = ui_icons.active_status_icons(options)
        self.assertEqual([tooltip for _path, tooltip in icons], [tooltip for _k, _i, tooltip in definitions])

    def test_no_options_produce_no_icons(self):
        self.assertEqual(ui_icons.active_status_icons({}), [])


if __name__ == '__main__':
    unittest.main()

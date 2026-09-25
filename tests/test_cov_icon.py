"""Coverage tests for detail_page.icon (DetailPageIconMixin).

The mixin is exercised without a widget tree: `_IconHarness` borrows the real
methods and hand-implements what they call back into (option storage, export
button, desktop file). Widgets are MagicMocks; where a method *builds* widgets,
the module's `Gtk`/`Adw`/`Gdk` names are replaced by mocks so the test runs
headless. Threads run synchronously, `GLib.idle_add` is recorded instead of
scheduled, and every path points into a temporary directory. No request ever
leaves the process: the network helpers are replaced per test.
"""
import io
import json
import logging
import sys
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_icon.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

from PIL import Image

import detail_page.icon as icon_module
from app_identity import APP_ICON_NAME
from detail_page.icon import DetailPageIconMixin
from i18n import t
from icon_pipeline import SVG_CAIRO_MISSING_ERROR
from input_validation import DESKTOP_CHROME_USER_AGENT, MAX_ICON_FILE_SIZE
from webapp_constants import (
    ICON_PATH_KEY,
    PROFILE_NAME_KEY,
    PROFILE_PATH_KEY,
    USER_AGENT_VALUE_KEY,
)

# Taken from the module under test, which pins the typelib versions first.
Gio, GLib, Gtk = icon_module.Gio, icon_module.GLib, icon_module.Gtk


class _SyncThread:
    """Stand-in for threading.Thread that runs the target on start()."""

    started: list = []  # noqa: RUF012 -- shared on purpose, reset in setUp

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = daemon

    def start(self):
        _SyncThread.started.append(self)
        self.target(*self.args, **self.kwargs)


class _RecordingThread(_SyncThread):
    """Stand-in for threading.Thread that only records the start."""

    def start(self):
        _SyncThread.started.append(self)


def _glib_stub(idle_return=0):
    return SimpleNamespace(
        Error=GLib.Error,
        idle_add=mock.Mock(return_value=idle_return),
        source_remove=mock.Mock(),
    )


def _write_png(path, size=(40, 20), color=(255, 0, 0, 255)):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGBA', size, color).save(path, 'PNG')
    return path


def _fresh_mock_factory(*_args, **kwargs):
    widget = mock.MagicMock()
    widget.ctor_kwargs = kwargs
    return widget


def _mock_gtk():
    gtk = mock.MagicMock()
    for name in ('Box', 'Button', 'Label', 'Spinner', 'Fixed'):
        getattr(gtk, name).side_effect = _fresh_mock_factory
    return gtk


class _IconHarness(DetailPageIconMixin):
    def __init__(self, options=None, title='Example App', entry_id=7):
        self.entry = SimpleNamespace(id=entry_id, title=title)
        self.options = dict(options or {})
        self._auto_icon_fetch_url = ''
        self._icon_texture_cache = {}
        self._profile_size_cache = {}
        self._profile_size_request_serial = 0
        self._profile_size_pending_path = ''
        self._icon_download_in_progress = False
        self._icon_page_preview_refresh_source_id = 0
        self._icon_page_preview_signature = None
        self._detail_toast_timeout_id = 0
        self._icon_upload_dialog_active = False
        self.on_overlay_notification = None
        self.on_visual_changed = None
        self.on_title_changed = None
        self.root = object()
        self.calls = []
        for name in (
            'icon_page_status', 'icon_download_button', 'icon_delete_button',
            'icon_page_search_spinner', 'icon_page_progress_box', 'icon_page_preview_canvas',
            'icon_button', 'address_entry', 'title_entry', 'description_entry',
            'delete_profile_button', 'detail_action_status', 'inline_busy_label',
            'inline_busy_overlay', 'inline_busy_spinner', 'header_name_label',
            'header_profile_label', 'page_stack',
        ):
            setattr(self, name, mock.MagicMock(name=name))
        self.address_entry.get_text.return_value = ''
        self.title_entry.get_text.return_value = ''
        self.description_entry.get_text.return_value = ''

    # --- what the mixin calls back into -------------------------------
    def _get_option_value(self, key):
        return self.options.get(key)

    def _set_option_value(self, key, value):
        self.options[key] = value

    def _options_dict(self):
        return dict(self.options)

    def _update_export_button_state(self):
        self.calls.append('export_state')

    def save_desktop_file(self):
        self.calls.append('save_desktop')

    def _update_tabbed_navigation_state(self):
        self.calls.append('tabbed_nav')

    def _cancel_address_timers(self):
        self.calls.append('cancel_timers')

    def _cancel_initial_address_validation(self):
        self.calls.append('cancel_validation')

    def _adaptive_wrap_page(self, page):
        return ('wrapped', page)

    def _profile_display_name(self):
        return 'Profile X'

    def _normalize_address_for_ui(self, value):
        return (value or '').strip()

    def _looks_ready_for_url_check(self, value):
        return value.startswith('http')

    def get_root(self):
        return self.root


class _TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        _SyncThread.started = []
        self.icon_dir = self.tmp / 'applications'
        patcher = mock.patch.object(
            icon_module, 'get_managed_icon_path',
            side_effect=lambda title, ext='.png', entry_id=None: self.icon_dir / f'webapp-entry-{entry_id}{ext}',
        )
        self.get_managed_icon_path = patcher.start()
        self.addCleanup(patcher.stop)


# ---------------------------------------------------------------------------
# small pure helpers
# ---------------------------------------------------------------------------


class UserAgentAndDomainTests(unittest.TestCase):
    def test_configured_user_agent_wins_over_default(self):
        harness = _IconHarness({USER_AGENT_VALUE_KEY: '  MyAgent/1.0  '})
        self.assertEqual(harness._icon_request_user_agent(), 'MyAgent/1.0')

    def test_blank_user_agent_falls_back_to_desktop_chrome(self):
        harness = _IconHarness({USER_AGENT_VALUE_KEY: '   '})
        self.assertEqual(harness._icon_request_user_agent(), DESKTOP_CHROME_USER_AGENT)

    def test_registrable_domain_rejects_non_public_hosts(self):
        harness = _IconHarness()
        for host in (None, '', 'localhost', '192.168.1.10', 'intranet', '..'):
            self.assertEqual(harness._registrable_domain_host(host), '', host)

    def test_registrable_domain_keeps_two_labels(self):
        harness = _IconHarness()
        self.assertEqual(harness._registrable_domain_host('Mail.Example.COM.'), 'example.com')
        self.assertEqual(harness._registrable_domain_host('example.com'), 'example.com')

    def test_registrable_domain_honours_multi_part_suffixes(self):
        harness = _IconHarness()
        self.assertEqual(harness._registrable_domain_host('www.bbc.co.uk'), 'bbc.co.uk')
        self.assertEqual(harness._registrable_domain_host('a.b.shop.com.au'), 'shop.com.au')
        # Only two labels: the suffix rule needs a third one to apply.
        self.assertEqual(harness._registrable_domain_host('co.uk'), 'co.uk')

    def test_public_root_hosts_are_deduplicated_in_order(self):
        harness = _IconHarness()
        self.assertEqual(
            harness._public_root_hosts_for_icon_fallback('WWW.Example.com'),
            ['www.example.com', 'example.com'],
        )
        self.assertEqual(harness._public_root_hosts_for_icon_fallback('example.com'), ['example.com'])
        self.assertEqual(harness._public_root_hosts_for_icon_fallback('localhost'), ['localhost'])
        self.assertEqual(harness._public_root_hosts_for_icon_fallback(None), [])

    def test_format_size_picks_the_right_unit(self):
        harness = _IconHarness()
        self.assertEqual(harness._format_size(0), '0 MB')
        self.assertEqual(harness._format_size(-5), '0 MB')
        self.assertEqual(harness._format_size(512), '512 B')
        self.assertEqual(harness._format_size(4096), '4 KB')
        self.assertEqual(harness._format_size(5 * 1024 ** 2), '5 MB')
        self.assertEqual(harness._format_size(3 * 1024 ** 3), '3.00 GB')


class CustomIconDetectionTests(_TempDirCase):
    def test_empty_and_default_theme_names_are_not_custom(self):
        for value in (None, '', '   ', 'applications-internet', APP_ICON_NAME):
            self.assertFalse(_IconHarness({ICON_PATH_KEY: value})._has_custom_icon(), value)

    def test_other_theme_name_counts_as_custom(self):
        self.assertTrue(_IconHarness({ICON_PATH_KEY: 'firefox'})._has_custom_icon())

    def test_path_counts_only_when_the_file_exists(self):
        existing = _write_png(self.tmp / 'icon.png')
        self.assertTrue(_IconHarness({ICON_PATH_KEY: str(existing)})._has_custom_icon())
        self.assertFalse(_IconHarness({ICON_PATH_KEY: str(self.tmp / 'missing.png')})._has_custom_icon())

    def test_icon_path_defaults_to_empty_string(self):
        self.assertEqual(_IconHarness()._icon_path(), '')
        self.assertEqual(_IconHarness({ICON_PATH_KEY: 'x'})._icon_path(), 'x')


# ---------------------------------------------------------------------------
# automatic icon fetch
# ---------------------------------------------------------------------------


class AutoFetchTests(_TempDirCase):
    def setUp(self):
        super().setUp()
        self.glib = _glib_stub()
        patch_glib = mock.patch.object(icon_module, 'GLib', self.glib)
        patch_thread = mock.patch.object(icon_module, 'threading', SimpleNamespace(Thread=_SyncThread))
        patch_glib.start()
        patch_thread.start()
        self.addCleanup(patch_glib.stop)
        self.addCleanup(patch_thread.stop)

    def test_skips_when_there_is_nothing_to_do(self):
        harness = _IconHarness({ICON_PATH_KEY: 'firefox'})
        harness._download_favicon = mock.Mock()
        harness._maybe_autofetch_icon('')
        harness._maybe_autofetch_icon('https://example.com')
        self.assertEqual(_SyncThread.started, [])

        harness = _IconHarness()
        harness._auto_icon_fetch_url = 'https://example.com'
        harness._maybe_autofetch_icon('https://example.com')
        self.assertEqual(_SyncThread.started, [])

    def test_successful_fetch_schedules_silent_apply(self):
        downloaded = _write_png(self.tmp / 'dl.png')
        harness = _IconHarness()
        harness._download_favicon = mock.Mock(return_value=downloaded)
        harness._maybe_autofetch_icon('https://example.com')
        self.assertEqual(harness._auto_icon_fetch_url, 'https://example.com')
        self.glib.idle_add.assert_called_once_with(harness._apply_downloaded_icon_silent, str(downloaded), 'https://example.com')

    def test_failed_or_empty_fetch_schedules_reset(self):
        for outcome in (None, OSError('boom'), urllib.error.URLError('down'), str(self.tmp / 'gone.png')):
            self.glib.idle_add.reset_mock()
            harness = _IconHarness()
            if isinstance(outcome, Exception):
                harness._download_favicon = mock.Mock(side_effect=outcome)
            else:
                harness._download_favicon = mock.Mock(return_value=outcome)
            harness._maybe_autofetch_icon('https://example.com')
            self.glib.idle_add.assert_called_once_with(harness._reset_auto_icon_fetch, 'https://example.com')

    def test_reset_only_clears_the_matching_url(self):
        harness = _IconHarness()
        harness._auto_icon_fetch_url = 'https://a'
        self.assertFalse(harness._reset_auto_icon_fetch('https://b'))
        self.assertEqual(harness._auto_icon_fetch_url, 'https://a')
        self.assertFalse(harness._reset_auto_icon_fetch('https://a'))
        self.assertEqual(harness._auto_icon_fetch_url, '')
        harness._auto_icon_fetch_url = 'https://c'
        harness._reset_auto_icon_fetch()
        self.assertEqual(harness._auto_icon_fetch_url, '')

    def test_silent_apply_moves_icon_into_place(self):
        temp_icon = _write_png(self.tmp / 'tmp-download.png')
        harness = _IconHarness()
        harness._auto_icon_fetch_url = 'https://example.com'
        harness._apply_icon_path = mock.Mock()
        self.assertFalse(harness._apply_downloaded_icon_silent(str(temp_icon), 'https://example.com'))
        target = self.icon_dir / 'webapp-entry-7.png'
        self.assertTrue(target.exists())
        self.assertFalse(temp_icon.exists())
        harness._apply_icon_path.assert_called_once_with(target)
        self.assertEqual(harness._auto_icon_fetch_url, '')

    def test_silent_apply_never_overrides_a_custom_icon(self):
        temp_icon = _write_png(self.tmp / 'tmp-download.png')
        harness = _IconHarness({ICON_PATH_KEY: 'firefox'})
        harness._auto_icon_fetch_url = 'https://example.com'
        harness._apply_icon_path = mock.Mock()
        self.assertFalse(harness._apply_downloaded_icon_silent(str(temp_icon), 'https://example.com'))
        harness._apply_icon_path.assert_not_called()
        self.assertFalse(temp_icon.exists())
        self.assertEqual(harness._auto_icon_fetch_url, '')


# ---------------------------------------------------------------------------
# page construction and small UI state helpers
# ---------------------------------------------------------------------------


class BuildIconPageTests(unittest.TestCase):
    def test_builds_three_buttons_wired_to_handlers(self):
        harness = _IconHarness()
        gtk = _mock_gtk()
        with mock.patch.object(icon_module, 'Gtk', gtk):
            harness._build_icon_page()
        download, upload, delete = harness._icon_page_buttons
        self.assertEqual(download.ctor_kwargs['label'], t('icon_action_download'))
        self.assertEqual(upload.ctor_kwargs['label'], t('icon_action_upload'))
        self.assertEqual(delete.ctor_kwargs['label'], t('icon_action_delete'))
        download.connect.assert_called_once_with('clicked', harness.on_icon_download_clicked)
        upload.connect.assert_called_once_with('clicked', harness.on_icon_upload_clicked)
        delete.connect.assert_called_once_with('clicked', harness.on_icon_delete_clicked)
        delete.add_css_class.assert_called_once_with('wam-destructive')
        harness.icon_page_search_spinner.set_visible.assert_called_once_with(False)
        harness.icon_page_preview_frame.append.assert_called_once_with(harness.icon_page_preview_canvas)
        harness.page_stack.add_named.assert_called_once_with(('wrapped', harness.icon_page), 'icon')


class HeaderAndBannerTests(unittest.TestCase):
    def test_refresh_header_meta_sets_title_and_profile(self):
        harness = _IconHarness(title='My App')
        harness._refresh_header_meta()
        harness.header_name_label.set_text.assert_called_once_with('My App')
        harness.header_profile_label.set_text.assert_called_once_with('Profile X')

    def test_refresh_header_meta_tolerates_missing_labels(self):
        # Regression: set_valign() ran outside the hasattr() guards.
        harness = _IconHarness()
        del harness.header_name_label
        del harness.header_profile_label
        harness._refresh_header_meta()

    def test_emit_visual_changed_notifies_both_callbacks(self):
        harness = _IconHarness()
        harness.on_visual_changed = mock.Mock()
        harness.on_title_changed = mock.Mock()
        harness._emit_visual_changed()
        harness.on_visual_changed.assert_called_once_with(harness.entry)
        harness.on_title_changed.assert_called_once_with(harness.entry)

    def test_emit_visual_changed_without_callbacks(self):
        harness = _IconHarness(title='T')
        harness._emit_visual_changed()
        harness.header_name_label.set_text.assert_called_once_with('T')

    def test_set_inline_busy_toggles_overlay_and_spinner(self):
        harness = _IconHarness()
        harness._set_inline_busy(True)
        harness.inline_busy_label.set_text.assert_called_with(t('loading'))
        harness.inline_busy_overlay.set_visible.assert_called_with(True)
        harness.inline_busy_spinner.start.assert_called_once()
        harness._set_inline_busy(True, 'Working')
        harness.inline_busy_label.set_text.assert_called_with('Working')
        harness._set_inline_busy(False, 'ignored')
        harness.inline_busy_label.set_text.assert_called_with('')
        harness.inline_busy_overlay.set_visible.assert_called_with(False)
        harness.inline_busy_spinner.stop.assert_called_once()

    def test_set_icon_download_busy(self):
        harness = _IconHarness()
        harness._set_icon_download_busy(True)
        harness.icon_page_search_spinner.start.assert_called_once()
        harness.icon_page_status.set_text.assert_called_with(t('icon_page_status_searching'))
        harness.icon_download_button.set_sensitive.assert_called_with(False)
        harness._set_icon_download_busy(True, 'custom')
        harness.icon_page_status.set_text.assert_called_with('custom')
        harness._set_icon_download_busy(False)
        harness.icon_page_search_spinner.stop.assert_called_once()
        harness.icon_page_search_spinner.set_visible.assert_called_with(False)
        harness.icon_download_button.set_sensitive.assert_called_with(True)

    def test_detail_action_status_visibility_follows_text(self):
        harness = _IconHarness()
        harness._set_detail_action_status('  ')
        harness.detail_action_status.set_visible.assert_called_with(False)
        harness._set_detail_action_status('Saved')
        harness.detail_action_status.set_text.assert_called_with('Saved')
        harness.detail_action_status.set_visible.assert_called_with(True)
        harness._set_detail_action_status(None)
        harness.detail_action_status.set_text.assert_called_with('')

    def test_cancel_and_hide_detail_toast(self):
        harness = _IconHarness()
        glib = _glib_stub()
        with mock.patch.object(icon_module, 'GLib', glib):
            harness._cancel_detail_toast()
            glib.source_remove.assert_not_called()
            harness._detail_toast_timeout_id = 42
            self.assertFalse(harness._hide_detail_toast())
        glib.source_remove.assert_called_once_with(42)
        self.assertEqual(harness._detail_toast_timeout_id, 0)

    def test_plugin_banner_forwards_trimmed_message(self):
        harness = _IconHarness()
        harness.on_overlay_notification = mock.Mock()
        harness._show_plugin_banner('   ')
        harness.on_overlay_notification.assert_not_called()
        harness._show_plugin_banner('  Hello ', timeout_ms=100)
        harness.on_overlay_notification.assert_called_once_with('Hello', timeout_ms=100)

    def test_plugin_banner_without_callback_is_silent(self):
        harness = _IconHarness()
        harness.on_overlay_notification = 'not callable'
        harness._show_plugin_banner('Hello')  # must not raise


class ProfileButtonLabelTests(unittest.TestCase):
    def setUp(self):
        self.glib = _glib_stub()
        self.cache = SimpleNamespace(lookup_bytes=mock.Mock(return_value=(None, True)), store=mock.Mock(), flush=mock.Mock())
        patches = [
            mock.patch.object(icon_module, 'GLib', self.glib),
            mock.patch.object(icon_module, 'threading', SimpleNamespace(Thread=_SyncThread)),
            mock.patch.object(icon_module, 'profile_size_cache', self.cache),
            mock.patch.object(icon_module, 'get_profile_size_bytes', return_value=2048),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        _SyncThread.started = []

    def test_label_shows_size_only_with_profile(self):
        harness = _IconHarness()
        harness._apply_profile_button_label('/p', 5 * 1024 ** 2)
        harness.delete_profile_button.set_label.assert_called_with(t('profile_delete_button', size=' (5 MB)'))
        harness.delete_profile_button.set_sensitive.assert_called_with(True)
        harness._apply_profile_button_label('/p', None)
        harness.delete_profile_button.set_label.assert_called_with(t('profile_delete_button', size=' (0 MB)'))
        harness._apply_profile_button_label('', 123)
        harness.delete_profile_button.set_label.assert_called_with(t('profile_delete_button', size=''))
        harness.delete_profile_button.set_sensitive.assert_called_with(False)

    def test_finish_ignores_stale_serial(self):
        harness = _IconHarness({PROFILE_PATH_KEY: '/p'})
        harness._profile_size_request_serial = 3
        harness._profile_size_pending_path = '/p'
        self.assertFalse(harness._finish_profile_size_refresh(2, '/p', 10))
        self.assertEqual(harness._profile_size_cache, {})
        self.assertEqual(harness._profile_size_pending_path, '/p')

    def test_finish_caches_and_applies_for_current_profile(self):
        harness = _IconHarness({PROFILE_PATH_KEY: '/p'})
        harness._profile_size_request_serial = 3
        harness._finish_profile_size_refresh(3, '/p', 4096)
        self.assertEqual(harness._profile_size_cache, {'/p': 4096})
        harness.delete_profile_button.set_label.assert_called_once_with(t('profile_delete_button', size=' (4 KB)'))

    def test_finish_for_another_profile_only_caches(self):
        harness = _IconHarness({PROFILE_PATH_KEY: '/other'})
        harness._profile_size_request_serial = 1
        harness._finish_profile_size_refresh(1, '/p', None)
        self.assertEqual(harness._profile_size_cache, {'/p': 0})
        harness.delete_profile_button.set_label.assert_not_called()

    def test_refresh_without_profile_clears_label(self):
        harness = _IconHarness({PROFILE_PATH_KEY: '  '})
        harness._refresh_profile_button_label()
        self.assertEqual(harness._profile_size_request_serial, 1)
        harness.delete_profile_button.set_sensitive.assert_called_with(False)
        self.assertEqual(_SyncThread.started, [])

    def test_refresh_uses_in_memory_cache(self):
        harness = _IconHarness({PROFILE_PATH_KEY: '/p'})
        harness._profile_size_cache['/p'] = 1024
        harness._refresh_profile_button_label()
        harness.delete_profile_button.set_label.assert_called_once_with(t('profile_delete_button', size=' (1 KB)'))
        self.cache.lookup_bytes.assert_not_called()
        self.assertEqual(_SyncThread.started, [])

    def test_refresh_uses_fresh_remembered_size_without_measuring(self):
        self.cache.lookup_bytes.return_value = (2048, False)
        harness = _IconHarness({PROFILE_PATH_KEY: '/p'})
        harness._refresh_profile_button_label()
        self.assertEqual(harness._profile_size_cache, {'/p': 2048})
        self.assertEqual(_SyncThread.started, [])

    def test_refresh_with_stale_size_shows_it_and_remeasures(self):
        self.cache.lookup_bytes.return_value = (1024, True)
        harness = _IconHarness({PROFILE_PATH_KEY: '/p'})
        harness._refresh_profile_button_label()
        harness.delete_profile_button.set_label.assert_called_once_with(t('profile_delete_button', size=' (1 KB)'))
        self.assertEqual(harness._profile_size_pending_path, '/p')
        self.cache.store.assert_called_once_with('/p', 2048)
        self.cache.flush.assert_called_once_with()
        self.glib.idle_add.assert_called_once_with(harness._finish_profile_size_refresh, 1, '/p', 2048)

    def test_refresh_without_remembered_size_measures(self):
        harness = _IconHarness({PROFILE_PATH_KEY: '/p'})
        harness._refresh_profile_button_label()
        harness.delete_profile_button.set_label.assert_called_once_with(t('profile_delete_button', size=' (0 MB)'))
        self.assertEqual(len(_SyncThread.started), 1)
        self.assertTrue(_SyncThread.started[0].daemon)


class ExportableWebappTests(unittest.TestCase):
    def test_only_transient_options_are_not_exportable(self):
        harness = _IconHarness({ICON_PATH_KEY: '', PROFILE_NAME_KEY: 'x', PROFILE_PATH_KEY: '/p', 'Other': '  '})
        self.assertFalse(harness._has_exportable_webapp())

    def test_any_meaningful_field_makes_it_exportable(self):
        harness = _IconHarness()
        harness.title_entry.get_text.return_value = 'Title'
        self.assertTrue(harness._has_exportable_webapp())

        harness = _IconHarness({ICON_PATH_KEY: '/icon.png'})
        self.assertTrue(harness._has_exportable_webapp())

        harness = _IconHarness({'Kiosk': '1'})
        self.assertTrue(harness._has_exportable_webapp())

    def test_missing_description_entry_is_tolerated(self):
        harness = _IconHarness()
        del harness.description_entry
        harness.address_entry.get_text.return_value = 'https://example.com'
        self.assertTrue(harness._has_exportable_webapp())


# ---------------------------------------------------------------------------
# texture / preview rendering
# ---------------------------------------------------------------------------


class TextureAndPreviewTests(_TempDirCase):
    def test_load_texture_caches_by_path_and_mtime(self):
        icon = _write_png(self.tmp / 'a.png')
        gdk = mock.MagicMock()
        gdk.Texture.new_from_filename.side_effect = lambda path: ('texture', path)
        harness = _IconHarness()
        with mock.patch.object(icon_module, 'Gdk', gdk):
            first = harness._load_texture(icon)
            second = harness._load_texture(str(icon))
        self.assertEqual(first, ('texture', str(icon)))
        self.assertIs(first, second)
        gdk.Texture.new_from_filename.assert_called_once_with(str(icon))

    def test_load_texture_for_missing_file_is_not_cached(self):
        gdk = mock.MagicMock()
        harness = _IconHarness()
        with mock.patch.object(icon_module, 'Gdk', gdk):
            harness._load_texture(self.tmp / 'missing.png')
        self.assertEqual(harness._icon_texture_cache, {})

    def test_load_texture_failure_returns_none(self):
        gdk = mock.MagicMock()
        gdk.Texture.new_from_filename.side_effect = GLib.Error('broken')
        with mock.patch.object(icon_module, 'Gdk', gdk):
            self.assertIsNone(_IconHarness()._load_texture(self.tmp / 'x.png'))

    def _tempfile_stub(self):
        return mock.patch.object(icon_module, 'tempfile', SimpleNamespace(gettempdir=lambda: str(self.tmp / 'tmp')))

    def test_prepare_display_icon_path_renders_square_png(self):
        source = _write_png(self.tmp / 'wide.png', size=(80, 40))
        harness = _IconHarness()
        with self._tempfile_stub():
            target = harness._prepare_display_icon_path(source, 32)
            self.assertEqual(target.parent, self.tmp / 'tmp' / 'webapp_icon_previews')
            self.assertTrue(target.name.startswith('entry-7-32-'))
            with Image.open(target) as rendered:
                self.assertEqual(rendered.size, (32, 32))
                # Letterboxed: transparent top row, opaque centre.
                self.assertEqual(rendered.getpixel((16, 0))[3], 0)
                self.assertEqual(rendered.getpixel((16, 16)), (255, 0, 0, 255))
            # Second call reuses the rendered file instead of re-rendering.
            with mock.patch.object(icon_module.Image, 'open') as image_open:
                self.assertEqual(harness._prepare_display_icon_path(source, 32), target)
            image_open.assert_not_called()

    def test_prepare_display_icon_path_routes_svg_through_pipeline(self):
        source = self.tmp / 'logo.svg'
        source.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        with self._tempfile_stub(), mock.patch.object(icon_module, 'normalize_icon_to_png') as normalize:
            target = _IconHarness()._prepare_display_icon_path(source, 64)
        normalize.assert_called_once_with(source, target)

    def test_prepare_display_icon_path_falls_back_to_source(self):
        broken = self.tmp / 'broken.png'
        broken.write_bytes(b'not an image')
        with self._tempfile_stub():
            self.assertEqual(_IconHarness()._prepare_display_icon_path(broken, 32), broken)
            missing = self.tmp / 'missing.png'
            self.assertEqual(_IconHarness()._prepare_display_icon_path(str(missing), 32), missing)


class IconWidgetTests(_TempDirCase):
    def setUp(self):
        super().setUp()
        self.gtk = _mock_gtk()
        patcher = mock.patch.object(icon_module, 'Gtk', self.gtk)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_existing_file_with_texture_becomes_picture(self):
        icon = _write_png(self.tmp / 'a.png')
        harness = _IconHarness({ICON_PATH_KEY: str(icon)})
        harness._prepare_display_icon_path = mock.Mock(return_value=icon)
        harness._load_texture = mock.Mock(return_value='texture')
        wrapper = harness._create_icon_widget(56)
        self.gtk.Picture.new_for_paintable.assert_called_once_with('texture')
        picture = self.gtk.Picture.new_for_paintable.return_value
        picture.set_size_request.assert_called_once_with(56, 56)
        wrapper.append.assert_called_once_with(picture)
        wrapper.set_size_request.assert_called_once_with(56, 56)

    def test_unloadable_file_falls_back_to_theme_lookup(self):
        icon = _write_png(self.tmp / 'a.png')
        harness = _IconHarness({ICON_PATH_KEY: str(icon)})
        harness._prepare_display_icon_path = mock.Mock(return_value=icon)
        harness._load_texture = mock.Mock(return_value=None)
        harness._create_icon_widget(64)
        self.gtk.Image.new_from_icon_name.assert_called_once_with(str(icon))
        self.gtk.Image.new_from_icon_name.return_value.set_pixel_size.assert_called_once_with(48)

    def test_theme_icon_name_uses_minimum_pixel_size(self):
        harness = _IconHarness({ICON_PATH_KEY: 'firefox'})
        harness._create_icon_widget(40)
        self.gtk.Image.new_from_icon_name.assert_called_once_with('firefox')
        self.gtk.Image.new_from_icon_name.return_value.set_pixel_size.assert_called_once_with(32)

    def test_no_icon_uses_app_icon(self):
        wrapper = _IconHarness()._create_icon_widget(56)
        self.gtk.Image.new_from_icon_name.assert_called_once_with(APP_ICON_NAME)
        wrapper.append.assert_called_once_with(self.gtk.Image.new_from_icon_name.return_value)

    def test_refresh_icon_preview_sets_button_child(self):
        harness = _IconHarness()
        harness._create_icon_widget = mock.Mock(return_value='widget-mock')
        harness._create_icon_widget.return_value = mock.MagicMock()
        harness.refresh_icon_preview()
        harness._create_icon_widget.assert_called_once_with(56)
        harness.icon_button.set_child.assert_called_once_with(harness._create_icon_widget.return_value)
        harness.icon_button.set_size_request.assert_called_once_with(72, 72)

    def test_placeholder_is_put_on_a_cleared_canvas(self):
        harness = _IconHarness()
        harness._clear_icon_page_preview_canvas = mock.Mock()
        harness._set_icon_page_preview_placeholder()
        harness._clear_icon_page_preview_canvas.assert_called_once_with()
        placeholder = self.gtk.Image.new_from_icon_name.return_value
        harness.icon_page_preview_canvas.put.assert_called_once_with(placeholder, 14, 14)


class PreviewCanvasTests(_TempDirCase):
    def test_clear_canvas_removes_every_child(self):
        class Child:
            def __init__(self, nxt=None):
                self.nxt = nxt

            def get_next_sibling(self):
                return self.nxt

        third = Child()
        second = Child(third)
        first = Child(second)
        harness = _IconHarness()
        harness.icon_page_preview_canvas.get_first_child.return_value = first
        harness._clear_icon_page_preview_canvas()
        removed = [call.args[0] for call in harness.icon_page_preview_canvas.remove.call_args_list]
        self.assertEqual(removed, [first, second, third])

    def test_preview_signature_tracks_mtime_for_files(self):
        icon = _write_png(self.tmp / 'a.png')
        harness = _IconHarness({ICON_PATH_KEY: str(icon)})
        self.assertEqual(harness._icon_preview_signature(64), (str(icon), icon.stat().st_mtime_ns, 64))
        self.assertEqual(_IconHarness({ICON_PATH_KEY: 'firefox'})._icon_preview_signature(64), ('firefox', None, 64))
        self.assertEqual(_IconHarness()._icon_preview_signature(8), ('', None, 8))

    def test_schedule_replaces_pending_refresh(self):
        glib = _glib_stub(idle_return=99)
        harness = _IconHarness({ICON_PATH_KEY: 'firefox'})
        harness._icon_page_preview_refresh_source_id = 5
        with mock.patch.object(icon_module, 'GLib', glib):
            harness._schedule_icon_page_preview_refresh()
        glib.source_remove.assert_called_once_with(5)
        glib.idle_add.assert_called_once_with(harness._refresh_icon_page_preview_idle, ('firefox', None, 64))
        self.assertEqual(harness._icon_page_preview_refresh_source_id, 99)
        self.assertEqual(harness._icon_page_preview_signature, ('firefox', None, 64))

    def test_idle_refresh_skips_stale_signature(self):
        harness = _IconHarness()
        harness._icon_page_preview_signature = ('new', None, 64)
        harness._icon_page_preview_refresh_source_id = 3
        harness._create_icon_widget = mock.Mock()
        self.assertFalse(harness._refresh_icon_page_preview_idle(('old', None, 64)))
        self.assertEqual(harness._icon_page_preview_refresh_source_id, 0)
        harness._create_icon_widget.assert_not_called()

    def test_idle_refresh_puts_current_preview(self):
        harness = _IconHarness()
        harness._icon_page_preview_signature = ('x', None, 64)
        harness._create_icon_widget = mock.Mock(return_value=mock.MagicMock())
        harness._clear_icon_page_preview_canvas = mock.Mock()
        self.assertFalse(harness._refresh_icon_page_preview_idle(('x', None, 64)))
        harness._create_icon_widget.assert_called_once_with(64)
        harness._clear_icon_page_preview_canvas.assert_called_once_with()
        harness.icon_page_preview_canvas.put.assert_called_once_with(harness._create_icon_widget.return_value, 14, 14)


class RefreshIconPageTests(unittest.TestCase):
    def _harness(self, **options):
        harness = _IconHarness(options)
        harness._set_icon_page_preview_placeholder = mock.Mock()
        harness._schedule_icon_page_preview_refresh = mock.Mock()
        return harness

    def test_busy_download_keeps_searching_state(self):
        harness = self._harness()
        harness._icon_download_in_progress = True
        harness.refresh_icon_page()
        harness.icon_page_status.set_text.assert_called_with(t('icon_page_status_searching'))
        harness.icon_download_button.set_sensitive.assert_called_with(False)
        harness._schedule_icon_page_preview_refresh.assert_called_once_with()

    def test_valid_url_enables_download(self):
        harness = self._harness()
        harness.address_entry.get_text.return_value = ' https://example.com '
        harness.refresh_icon_page()
        harness.icon_download_button.set_sensitive.assert_called_with(True)
        harness.icon_page_status.set_text.assert_called_with(t('icon_page_status_url'))
        harness.icon_delete_button.set_sensitive.assert_called_once_with(False)

    def test_invalid_url_disables_download_and_custom_icon_enables_delete(self):
        harness = self._harness(**{ICON_PATH_KEY: 'firefox'})
        harness.address_entry.get_text.return_value = 'not a url'
        harness.refresh_icon_page()
        harness.icon_download_button.set_sensitive.assert_called_with(False)
        harness.icon_page_status.set_text.assert_called_with(t('icon_page_status_no_url'))
        harness.icon_delete_button.set_sensitive.assert_called_once_with(True)


# ---------------------------------------------------------------------------
# storing icons
# ---------------------------------------------------------------------------


class StoreIconTests(_TempDirCase):
    def test_managed_icon_target_uses_title_and_id(self):
        harness = _IconHarness(title='Mail', entry_id=3)
        self.assertEqual(harness._managed_icon_target(), self.icon_dir / 'webapp-entry-3.png')
        self.get_managed_icon_path.assert_called_with('Mail', '.png', 3)

    def test_sync_filename_ignores_theme_names_and_missing_files(self):
        for value in ('', 'firefox', str(self.tmp / 'missing.png')):
            harness = _IconHarness({ICON_PATH_KEY: value})
            harness._sync_icon_filename()
            self.assertEqual(harness.options[ICON_PATH_KEY], value)
        self.assertFalse(self.icon_dir.exists())

    def test_sync_filename_is_noop_when_already_managed(self):
        target = _write_png(self.icon_dir / 'webapp-entry-7.png')
        harness = _IconHarness({ICON_PATH_KEY: str(target)})
        harness._sync_icon_filename()
        self.assertEqual(harness.options[ICON_PATH_KEY], str(target))
        self.assertTrue(target.exists())

    def test_sync_filename_renames_within_managed_dir(self):
        old = _write_png(self.icon_dir / 'old-name.png')
        payload = old.read_bytes()
        harness = _IconHarness({ICON_PATH_KEY: str(old)})
        harness._sync_icon_filename()
        target = self.icon_dir / 'webapp-entry-7.png'
        self.assertEqual(harness.options[ICON_PATH_KEY], str(target))
        self.assertEqual(target.read_bytes(), payload)
        self.assertFalse(old.exists())

    def test_sync_filename_copies_foreign_file_and_keeps_it(self):
        foreign = _write_png(self.tmp / 'elsewhere' / 'logo.png')
        harness = _IconHarness({ICON_PATH_KEY: str(foreign)})
        harness._sync_icon_filename()
        self.assertTrue(foreign.exists())
        self.assertTrue((self.icon_dir / 'webapp-entry-7.png').exists())

    def test_sync_filename_ignores_failed_unlink_of_old_file(self):
        old = _write_png(self.icon_dir / 'old-name.png')
        harness = _IconHarness({ICON_PATH_KEY: str(old)})
        real_unlink = Path.unlink

        def failing_unlink(path, missing_ok=False):
            if path == old:
                raise OSError('read-only')
            return real_unlink(path, missing_ok=missing_ok)

        with mock.patch.object(Path, 'unlink', failing_unlink):
            harness._sync_icon_filename()
        self.assertEqual(harness.options[ICON_PATH_KEY], str(self.icon_dir / 'webapp-entry-7.png'))

    def test_sync_filename_logs_write_failure_and_keeps_option(self):
        foreign = _write_png(self.tmp / 'logo.png')
        self.icon_dir.parent.mkdir(parents=True, exist_ok=True)
        self.icon_dir.write_text('a file where the directory should be')
        harness = _IconHarness({ICON_PATH_KEY: str(foreign)})
        with self.assertLogs(icon_module.LOG, level='WARNING'):
            harness._sync_icon_filename()
        self.assertEqual(harness.options[ICON_PATH_KEY], str(foreign))

    def test_store_pil_image_fits_into_transparent_square(self):
        harness = _IconHarness()
        target = harness._store_pil_image(Image.new('RGB', (110, 55), (0, 255, 0)))
        self.assertEqual(target, self.icon_dir / 'webapp-entry-7.png')
        with Image.open(target) as stored:
            self.assertEqual(stored.size, (256, 256))
            self.assertEqual(stored.mode, 'RGBA')
            self.assertEqual(stored.getbbox(), (18, 73, 238, 183))

    def test_store_pil_image_honours_explicit_target(self):
        target = self.tmp / 'custom' / 'x.png'
        result = _IconHarness()._store_pil_image(Image.new('RGBA', (300, 300)), target)
        self.assertEqual(result, target)
        self.assertTrue(target.exists())

    def test_store_pil_image_rejects_empty_image(self):
        with self.assertRaises(OSError):
            _IconHarness()._store_pil_image(Image.new('RGBA', (0, 0)))

    def test_apply_icon_path_updates_everything(self):
        harness = _IconHarness()
        harness.refresh_icon_preview = mock.Mock()
        harness.refresh_icon_page = mock.Mock()
        harness._emit_visual_changed = mock.Mock()
        glib = _glib_stub()
        with mock.patch.object(icon_module, 'GLib', glib):
            harness._apply_icon_path(self.tmp / 'i.png')
        self.assertEqual(harness.options[ICON_PATH_KEY], str(self.tmp / 'i.png'))
        harness.refresh_icon_preview.assert_called_once_with()
        harness.refresh_icon_page.assert_called_once_with()
        harness._emit_visual_changed.assert_called_once_with()
        self.assertEqual(harness.calls, ['export_state', 'save_desktop'])
        glib.idle_add.assert_called_once_with(harness._emit_visual_changed)

    def test_store_icon_file_normalizes_and_applies(self):
        source = _write_png(self.tmp / 'upload.png')
        harness = _IconHarness()
        harness._apply_icon_path = mock.Mock()
        self.assertTrue(harness._store_icon_file(source))
        target = self.icon_dir / 'webapp-entry-7.png'
        harness._apply_icon_path.assert_called_once_with(target)
        with Image.open(target) as stored:
            self.assertEqual(stored.mode, 'RGBA')

    def test_store_icon_file_rejects_invalid_source(self):
        harness = _IconHarness()
        harness.on_overlay_notification = mock.Mock()
        harness._apply_icon_path = mock.Mock()
        self.assertFalse(harness._store_icon_file(self.tmp / 'missing.png'))
        harness._apply_icon_path.assert_not_called()
        harness.on_overlay_notification.assert_called_once_with(t('icon_page_status_upload_failed'), timeout_ms=3200)

    def test_store_icon_file_reports_missing_svg_support(self):
        source = self.tmp / 'logo.svg'
        source.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        harness = _IconHarness()
        harness.on_overlay_notification = mock.Mock()
        with mock.patch.object(icon_module, 'normalize_icon_to_png', side_effect=OSError(SVG_CAIRO_MISSING_ERROR)):
            self.assertFalse(harness._store_icon_file(source))
        harness.on_overlay_notification.assert_called_once_with(t('svg_import_requires_cairo'), timeout_ms=4200)


# ---------------------------------------------------------------------------
# HTML / manifest parsing and candidate ranking
# ---------------------------------------------------------------------------


class CandidateScoringTests(unittest.TestCase):
    def setUp(self):
        self.h = _IconHarness()

    def test_parse_html_tag_attributes_handles_all_quote_styles(self):
        attrs = self.h._parse_html_tag_attributes('<link REL="icon" href=\'/a.png\' sizes=32x32 data-x="">')
        self.assertEqual(attrs, {'rel': 'icon', 'href': '/a.png', 'sizes': '32x32', 'data-x': ''})

    def test_size_score_from_string(self):
        self.assertEqual(self.h._size_score_from_string('16x16  64x32 bad axb'), 32)
        self.assertEqual(self.h._size_score_from_string('any 16x16'), 4096)
        self.assertEqual(self.h._size_score_from_string(None), 0)

    def test_size_score_inferred_from_url(self):
        self.assertEqual(self.h._infer_size_score_from_url('https://x/icons/icon-192x144.png?v=1'), 144)
        self.assertEqual(self.h._infer_size_score_from_url('https://x/logo_128.png'), 128)
        self.assertEqual(self.h._infer_size_score_from_url('https://x/favicon.ico'), 0)
        self.assertEqual(self.h._infer_size_score_from_url(None), 0)

    def test_type_priority(self):
        cases = [
            (('https://x/a.svg',), 5),
            (('https://x/a', '', 'image/svg+xml'), 5),
            (('https://x/a', 'mask-icon'), 5),
            (('https://x/a', '', '', '', 'monochrome'), 5),
            (('https://x/a', '', '', ' any '), 5),
            (('https://x/a.webp',), 4),
            (('https://x/a', '', 'image/png'), 4),
            (('https://x/a', 'apple-touch-icon'), 4),
            (('https://x/a', 'fluid-icon'), 4),
            (('https://x/favicon.ico',), 2),
            (('https://x/a', '', 'image/vnd.microsoft.icon'), 2),
            (('https://x/a',), 3),
        ]
        for args, expected in cases:
            self.assertEqual(self.h._icon_type_priority(*args), expected, args)

    def test_source_priority(self):
        self.assertEqual(self.h._source_priority_for_candidate('manifest'), 60)
        self.assertEqual(self.h._source_priority_for_candidate('unknown'), 20)
        self.assertEqual(self.h._source_priority_for_candidate('icon_link', 'apple-touch-icon'), 58)
        self.assertEqual(self.h._source_priority_for_candidate('meta_image', 'mask-icon'), 54)
        self.assertEqual(self.h._source_priority_for_candidate('icon_link', '', 'print'), 36)
        self.assertEqual(self.h._source_priority_for_candidate('icon_link', '', 'screen and (x)'), 57)
        self.assertEqual(self.h._source_priority_for_candidate('icon_link', '', 'all'), 57)
        self.assertEqual(self.h._source_priority_for_candidate('icon_link', '', '(prefers-color-scheme: dark)'), 56)

    def test_make_icon_candidate_combines_scores(self):
        candidate = self.h._make_icon_candidate('https://x/icon-96x96.png', source_kind='manifest', order='3')
        self.assertEqual(candidate, {'href': 'https://x/icon-96x96.png', 'type_priority': 4, 'source_priority': 60, 'size_score': 96, 'order': 3})
        candidate = self.h._make_icon_candidate('https://x/a.png', sizes_value='48x48')
        self.assertEqual(candidate['size_score'], 48)

    def test_order_icon_candidates_dedupes_and_ranks(self):
        candidates = [
            {'href': 'https://x/a.ico', 'type_priority': 2, 'source_priority': 56, 'size_score': 0, 'order': 1},
            {'href': 'https://x/b.png', 'type_priority': 4, 'source_priority': 56, 'size_score': 32, 'order': 2},
            {'href': 'https://x/c.png', 'type_priority': 4, 'source_priority': 56, 'size_score': 192, 'order': 3},
            {'href': 'https://x/b.png', 'type_priority': 5, 'source_priority': 99, 'size_score': 999, 'order': 4},
            {'href': '  '},
            None,
            {'href': 'https://x/d.svg', 'type_priority': 5, 'source_priority': 40, 'size_score': 0, 'order': 5},
        ]
        self.assertEqual(
            self.h._order_icon_candidates(candidates),
            ['https://x/d.svg', 'https://x/c.png', 'https://x/b.png', 'https://x/a.ico'],
        )


class HtmlExtractionTests(unittest.TestCase):
    BASE = 'https://example.com/app/page.html'

    def test_malformed_href_is_skipped_not_raised(self):
        # Regression: urljoin raises ValueError for a host like 'http://[x', and
        # one such attribute on a foreign page aborted the whole icon search.
        html = ('<link rel="icon" href="http://[x/bad.png">'
                '<link rel="icon" href="/good.png">'
                '<link rel="manifest" href="http://[x/m.json">'
                '<meta name="msapplication-config" content="http://[x/bc.xml">'
                '<meta property="og:image" content="http://[x/og.png">')
        hrefs = [c['href'] for c in self.h._extract_icon_candidates(html, self.BASE)]
        self.assertEqual(hrefs, ['https://example.com/good.png'])
        self.assertIsNone(self.h._extract_manifest_url(html, self.BASE))
        self.assertIsNone(self.h._extract_browserconfig_url(html, self.BASE))
        self.assertEqual(self.h._extract_meta_image_candidates(html, self.BASE), [])
        self.assertEqual(self.h._extract_favicon_asset_candidates('"http://[x/favicon.png"', self.BASE), [])
        self.assertIsNone(self.h._extract_base_href('<base href="http://[x/">', self.BASE))
        manifest = '{"icons": [{"src": "http://[x/a.png"}, {"src": "b.png"}]}'
        self.assertEqual([c['href'] for c in self.h._extract_manifest_icon_candidates(manifest, 'https://example.com/m.json')],
                         ['https://example.com/b.png'])
        browserconfig = '<square150x150logo src="http://[x/a.png"/><square70x70logo src="/b.png"/>'
        self.assertEqual([c['href'] for c in self.h._extract_browserconfig_icon_candidates(browserconfig, 'https://example.com/bc.xml')],
                         ['https://example.com/b.png'])

    def setUp(self):
        self.h = _IconHarness()

    def test_extract_base_href(self):
        self.assertEqual(self.h._extract_base_href('<base target="_blank"><BASE href="/static/">', self.BASE), 'https://example.com/static/')
        self.assertIsNone(self.h._extract_base_href('<html></html>', self.BASE))

    def test_extract_icon_candidates_classifies_link_tags(self):
        html = (
            '<link rel="stylesheet" href="/s.css">'
            '<link rel="manifest" href="/m.json">'
            '<link rel="icon">'
            '<link rel="icon" href="fav.ico" type="image/x-icon">'
            '<link rel="apple-touch-icon" href="/apple.png" sizes="180x180">'
            '<link rel="mask-icon" href="/mask.svg">'
            '<link rel="fluid-icon" href="/fluid.png">'
            '<link rel="shortcut icon" href="/print.png" media="print">'
        )
        candidates = self.h._extract_icon_candidates(html, self.BASE)
        by_href = {c['href']: c for c in candidates}
        self.assertEqual(list(by_href), [
            'https://example.com/app/fav.ico',
            'https://example.com/apple.png',
            'https://example.com/mask.svg',
            'https://example.com/fluid.png',
            'https://example.com/print.png',
        ])
        self.assertEqual(by_href['https://example.com/apple.png']['source_priority'], 58)
        self.assertEqual(by_href['https://example.com/apple.png']['size_score'], 180)
        self.assertEqual(by_href['https://example.com/mask.svg']['source_priority'], 54)
        self.assertEqual(by_href['https://example.com/fluid.png']['source_priority'], 52)
        self.assertEqual(by_href['https://example.com/print.png']['source_priority'], 36)
        self.assertEqual([c['order'] for c in candidates], [1, 2, 3, 4, 5])

    def test_extract_manifest_url(self):
        html = '<link rel="icon" href="/i.png"><link rel="manifest">' '<link rel="Manifest" href="site.webmanifest">'
        self.assertEqual(self.h._extract_manifest_url(html, self.BASE), 'https://example.com/app/site.webmanifest')
        self.assertIsNone(self.h._extract_manifest_url('<link rel="icon" href="/i.png">', self.BASE))

    def test_extract_favicon_asset_candidates(self):
        html = (
            '<script>var a = "/assets/favicon-32.png"; var b = \'favicons are fun\';'
            ' var c = "https://cdn.example.com/favicon?v=2"; var d = "img/favicon.x";</script>'
        )
        hrefs = [c['href'] for c in self.h._extract_favicon_asset_candidates(html, self.BASE)]
        self.assertEqual(hrefs, [
            'https://example.com/assets/favicon-32.png',
            'https://cdn.example.com/favicon?v=2',
            'https://example.com/app/img/favicon.x',
        ])
        self.assertEqual(self.h._extract_favicon_asset_candidates(None, self.BASE), [])

    def test_extract_manifest_icon_candidates(self):
        manifest = json.dumps({'icons': [
            'not-a-dict',
            {'sizes': '48x48'},
            {'src': 'icons/192.png', 'sizes': '192x192', 'type': 'image/png'},
            {'src': '/mask.png', 'purpose': 'any Maskable'},
        ]})
        candidates = self.h._extract_manifest_icon_candidates(manifest, 'https://example.com/m/manifest.json')
        self.assertEqual([c['href'] for c in candidates], ['https://example.com/m/icons/192.png', 'https://example.com/mask.png'])
        self.assertEqual(candidates[0]['source_priority'], 60)
        self.assertEqual(candidates[0]['size_score'], 192)
        self.assertEqual(candidates[1]['source_priority'], 65)

    def test_extract_manifest_icon_candidates_rejects_garbage(self):
        self.assertEqual(self.h._extract_manifest_icon_candidates('{not json', 'https://x/m.json'), [])
        self.assertEqual(self.h._extract_manifest_icon_candidates(None, 'https://x/m.json'), [])
        self.assertEqual(self.h._extract_manifest_icon_candidates('{"icons": null}', 'https://x/m.json'), [])

    def test_extract_manifest_icon_candidates_ignores_non_object_manifest(self):
        # Regression: a manifest that is not a JSON object raised AttributeError and aborted the
        # icon search.
        self.assertEqual(self.h._extract_manifest_icon_candidates('[]', 'https://x/m.json'), [])

    def test_extract_browserconfig_url(self):
        html = '<meta name="viewport" content="x"><meta name="msapplication-config" content="">' '<meta name="MSApplication-Config" content="/browserconfig.xml">'
        self.assertEqual(self.h._extract_browserconfig_url(html, self.BASE), 'https://example.com/browserconfig.xml')
        self.assertIsNone(self.h._extract_browserconfig_url('<meta name="x" content="y">', self.BASE))

    def test_extract_browserconfig_icon_candidates(self):
        xml = (
            '<browserconfig><msapplication><tile>'
            '<square150x150logo src="/mstile-150x150.png"/>'
            '<TileImage src="tile.png"/>'
            '</tile></msapplication></browserconfig>'
        )
        candidates = self.h._extract_browserconfig_icon_candidates(xml, 'https://example.com/cfg/browserconfig.xml')
        self.assertEqual([c['href'] for c in candidates], ['https://example.com/mstile-150x150.png', 'https://example.com/cfg/tile.png'])
        self.assertEqual(candidates[0]['size_score'], 150)
        self.assertEqual(candidates[1]['source_priority'], 46)

    def test_extract_meta_image_candidates(self):
        html = (
            '<meta property="og:image" content="/og.png">'
            '<meta name="twitter:image" content="https://cdn.example.com/tw.jpg">'
            '<meta name="description" content="text">'
            '<meta property="og:image" content="">'
        )
        candidates = self.h._extract_meta_image_candidates(html, self.BASE)
        self.assertEqual([c['href'] for c in candidates], ['https://example.com/og.png', 'https://cdn.example.com/tw.jpg'])
        self.assertEqual(candidates[0]['source_priority'], 10)

    def test_special_icon_fallbacks(self):
        maps = self.h._special_icon_fallback_candidates('https://www.google.com/maps/@1,2')
        self.assertEqual(len(maps), 2)
        self.assertTrue(all('google.com' in href for href in maps))
        self.assertEqual(self.h._special_icon_fallback_candidates('https://www.google.com/search'), [])
        booking = self.h._special_icon_fallback_candidates('https://secure.booking.com/x')
        self.assertEqual(len(booking), 3)
        self.assertEqual(self.h._special_icon_fallback_candidates('https://example.com/'), [])

    def test_icon_source_page_candidates(self):
        self.assertEqual(self.h._icon_source_page_candidates('example.com'), [])
        self.assertEqual(
            self.h._icon_source_page_candidates('https://app.example.com/dir/index.html?x=1#frag'),
            [
                'https://app.example.com/dir/index.html?x=1',
                'https://app.example.com/dir/index.html',
                'https://app.example.com/dir',
                'https://app.example.com/',
                'https://example.com/',
            ],
        )
        self.assertEqual(
            self.h._icon_source_page_candidates('https://example.com/app/'),
            ['https://example.com/app/', 'https://example.com/app', 'https://example.com/'],
        )
        self.assertEqual(self.h._icon_source_page_candidates('https://example.com'), ['https://example.com'])


# ---------------------------------------------------------------------------
# network helpers (open_guarded_url is always mocked)
# ---------------------------------------------------------------------------


class _FakeHeaders(dict):
    def __init__(self, content_type, **values):
        super().__init__(values)
        self._content_type = content_type

    def get_content_type(self):
        return self._content_type


class _FakeResponse:
    def __init__(self, body, content_type='text/html', content_length=None, final_url='https://final.example/'):
        self._body = io.BytesIO(body)
        headers = {}
        if content_length is not None:
            headers['Content-Length'] = content_length
        self.headers = _FakeHeaders(content_type, **headers)
        self._final_url = final_url

    def read(self, amount=-1):
        return self._body.read(amount)

    def geturl(self):
        return self._final_url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DownloadHelperTests(unittest.TestCase):
    def test_download_image_bytes_returns_payload_and_type(self):
        harness = _IconHarness({USER_AGENT_VALUE_KEY: 'UA/1'})
        response = _FakeResponse(b'PNGDATA', content_type='image/png', content_length='nonsense')
        with mock.patch.object(icon_module, 'open_guarded_url', return_value=response) as opener:
            self.assertEqual(harness._download_image_bytes('https://x/a.png'), (b'PNGDATA', 'image/png'))
        args, kwargs = opener.call_args
        self.assertEqual(args, ('https://x/a.png',))
        self.assertEqual(kwargs['headers']['User-Agent'], 'UA/1')
        self.assertEqual(kwargs['timeout'], 10)

    def test_download_image_bytes_rejects_oversized_payloads(self):
        harness = _IconHarness()
        declared = _FakeResponse(b'x', content_type='image/png', content_length=str(MAX_ICON_FILE_SIZE + 1))
        with mock.patch.object(icon_module, 'open_guarded_url', return_value=declared), self.assertRaisesRegex(OSError, 'too large'):
                harness._download_image_bytes('https://x/a.png')
        undeclared = _FakeResponse(b'x' * (MAX_ICON_FILE_SIZE + 1), content_type='image/png')
        with mock.patch.object(icon_module, 'open_guarded_url', return_value=undeclared), self.assertRaisesRegex(OSError, 'too large'):
                harness._download_image_bytes('https://x/a.png')

    def test_download_text_response_decodes_body(self):
        harness = _IconHarness()
        response = _FakeResponse('<html>é</html>'.encode() + b'\xff', content_length='bogus', final_url='https://x/final')
        with mock.patch.object(icon_module, 'open_guarded_url', return_value=response) as opener:
            text, content_type, final_url = harness._download_text_response('https://x/', 'text/html', timeout=3)
        self.assertEqual(text, '<html>é</html>')
        self.assertEqual(content_type, 'text/html')
        self.assertEqual(final_url, 'https://x/final')
        self.assertEqual(opener.call_args.kwargs['headers']['Accept'], 'text/html')
        self.assertEqual(opener.call_args.kwargs['timeout'], 3)

    def test_download_text_response_rejects_oversized_bodies(self):
        harness = _IconHarness()
        declared = _FakeResponse(b'x', content_length=str(512 * 1024 + 1))
        with mock.patch.object(icon_module, 'open_guarded_url', return_value=declared), self.assertRaisesRegex(OSError, 'too large'):
                harness._download_text_response('https://x/', 'text/html')
        undeclared = _FakeResponse(b'x' * (512 * 1024 + 1))
        with mock.patch.object(icon_module, 'open_guarded_url', return_value=undeclared), self.assertRaisesRegex(OSError, 'too large'):
                harness._download_text_response('https://x/', 'text/html')


class DownloadFaviconTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        tmpdir = str(self.tmp)
        tempfile_stub = SimpleNamespace(mkstemp=lambda suffix='': tempfile.mkstemp(suffix=suffix, dir=tmpdir))
        self.normalized = []

        def fake_normalize(payload, target, source_name='', content_type=''):
            self.normalized.append((payload, source_name, content_type))
            target.write_bytes(payload)
            return target

        for patcher in (
            mock.patch.object(icon_module, 'tempfile', tempfile_stub),
            mock.patch.object(icon_module, 'normalize_icon_bytes_to_png', side_effect=fake_normalize),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.harness = _IconHarness()
        self.pages = {}
        self.images = {}
        self.tried_images = []
        self.fetched_documents = []

        def fake_text(url, accept, timeout=8):
            self.fetched_documents.append(url)
            value = self.pages.get(url)
            if value is None:
                raise urllib.error.URLError('unreachable')
            if isinstance(value, Exception):
                raise value
            return value

        def fake_image(url):
            self.tried_images.append(url)
            value = self.images.get(url)
            if value is None:
                raise OSError('404')
            if isinstance(value, Exception):
                raise value
            return value

        self.harness._download_text_response = fake_text
        self.harness._download_image_bytes = fake_image

    def test_url_without_scheme_returns_none(self):
        self.assertIsNone(self.harness._download_favicon('example.com'))
        self.assertEqual(self.fetched_documents, [])

    def test_collects_every_source_and_picks_best_available(self):
        html = (
            '<html><head>'
            '<link rel="icon" href="/fav.ico">'
            '<link rel="apple-touch-icon" href="/apple.png" sizes="180x180">'
            '<link rel="manifest" href="/manifest.json">'
            '<meta name="msapplication-config" content="/browserconfig.xml">'
            '<meta property="og:image" content="/og.jpg">'
            '</head></html>'
        )
        self.pages['https://www.example.com/app'] = (html, 'text/html', 'https://www.example.com/app')
        self.pages['https://www.example.com/manifest.json'] = (
            json.dumps({'icons': [{'src': '/icon-512.png', 'sizes': '512x512'}]}), 'application/manifest+json', 'https://www.example.com/manifest.json')
        self.pages['https://www.example.com/browserconfig.xml'] = (
            '<square70x70logo src="/tile70.png"/>', 'application/xml', 'https://www.example.com/browserconfig.xml')
        self.images['https://www.example.com/apple.png'] = (b'APPLE', 'image/png')

        result = self.harness._download_favicon('https://www.example.com/app')

        self.assertEqual(result.read_bytes(), b'APPLE')
        self.assertEqual(result.parent, self.tmp)
        self.assertEqual(self.normalized, [(b'APPLE', 'https://www.example.com/apple.png', 'image/png')])
        # SVG root fallbacks are tried first, then the large manifest icon,
        # which outranks the apple-touch icon; the meta image is never needed.
        self.assertTrue(self.tried_images[0].endswith('/favicon.svg'))
        self.assertLess(self.tried_images.index('https://www.example.com/icon-512.png'), self.tried_images.index('https://www.example.com/apple.png'))
        self.assertEqual(self.tried_images[-1], 'https://www.example.com/apple.png')
        self.assertNotIn('https://www.example.com/og.jpg', self.tried_images)
        self.assertNotIn('https://www.example.com/fav.ico', self.tried_images)
        # Root fallbacks are generated for the host and its registrable domain.
        self.assertIn('https://example.com/favicon.svg', self.tried_images)
        # The document list also covers the bare domain.
        self.assertIn('https://example.com/', self.fetched_documents)

    def test_meta_image_is_the_last_resort(self):
        html = '<meta property="og:image" content="/og.jpg"><base href="https://cdn.example.com/">'
        self.pages['https://example.com/'] = (html, 'text/html; charset=utf-8', '')
        self.images['https://cdn.example.com/og.jpg'] = (b'OG', 'image/jpeg')
        result = self.harness._download_favicon('https://example.com/')
        self.assertEqual(result.read_bytes(), b'OG')
        self.assertEqual(self.tried_images[-1], 'https://cdn.example.com/og.jpg')

    def test_non_html_documents_and_broken_side_files_are_skipped(self):
        html = '<link rel="manifest" href="/m.json"><meta name="msapplication-config" content="/bc.xml">'
        self.pages['https://example.com/a.json'] = ('{}', 'application/json', 'https://example.com/a.json')
        self.pages['https://example.com/'] = (html, 'text/html', 'https://example.com/')
        self.pages['https://example.com/m.json'] = ('not json at all', 'text/plain', 'https://example.com/m.json')
        self.pages['https://example.com/bc.xml'] = OSError('gone')
        self.assertIsNone(self.harness._download_favicon('https://example.com/a.json'))
        self.assertIn('https://example.com/m.json', self.fetched_documents)
        self.assertIn('https://example.com/bc.xml', self.fetched_documents)
        self.assertTrue(all(url.startswith(('https://example.com/favicon', 'https://example.com/apple-touch')) for url in self.tried_images))

    def test_manifest_download_error_is_swallowed(self):
        self.pages['https://example.com/'] = ('<link rel="manifest" href="/m.json">', 'text/html', 'https://example.com/')
        self.pages['https://example.com/m.json'] = ValueError('bad url')
        self.images['https://example.com/favicon.ico'] = (b'ICO', 'image/x-icon')
        self.assertEqual(self.harness._download_favicon('https://example.com/').read_bytes(), b'ICO')

    def test_special_fallbacks_join_the_candidates(self):
        maps_icon = 'https://www.google.com/images/branding/product/ico/maps15_bnuw3a_32dp.ico'
        self.images[maps_icon] = (b'MAPS', 'image/x-icon')
        result = self.harness._download_favicon('https://www.google.com/maps')
        self.assertEqual(result.read_bytes(), b'MAPS')
        # The special fallbacks rank below the generic root favicons.
        self.assertEqual(self.tried_images[-2:], ['https://maps.google.com/favicon.ico', maps_icon])

    def test_missing_svg_support_propagates(self):
        self.images['https://example.com/favicon.svg'] = OSError(SVG_CAIRO_MISSING_ERROR)
        with self.assertRaisesRegex(OSError, 'SVG support is unavailable'):
            self.harness._download_favicon('https://example.com/')


# ---------------------------------------------------------------------------
# button handlers and dialogs
# ---------------------------------------------------------------------------


class IconPageNavigationTests(unittest.TestCase):
    def test_on_icon_clicked_switches_page_and_schedules_refresh(self):
        harness = _IconHarness()
        glib = _glib_stub()
        with mock.patch.object(icon_module, 'GLib', glib):
            harness.on_icon_clicked(None)
        harness.page_stack.set_visible_child_name.assert_called_once_with('icon')
        self.assertEqual(harness.calls, ['tabbed_nav', 'export_state'])
        glib.idle_add.assert_called_once_with(harness._refresh_icon_page_after_open)

    def test_refresh_after_open_only_when_visible(self):
        harness = _IconHarness()
        harness.refresh_icon_page = mock.Mock()
        harness.page_stack.get_visible_child_name.return_value = 'general'
        self.assertFalse(harness._refresh_icon_page_after_open())
        harness.refresh_icon_page.assert_not_called()
        harness.page_stack.get_visible_child_name.return_value = 'icon'
        self.assertTrue(harness.is_icon_page_visible())
        self.assertFalse(harness._refresh_icon_page_after_open())
        harness.refresh_icon_page.assert_called_once_with()


class DownloadButtonTests(unittest.TestCase):
    def setUp(self):
        _SyncThread.started = []
        patcher = mock.patch.object(icon_module, 'threading', SimpleNamespace(Thread=_RecordingThread))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ignored_while_download_runs(self):
        harness = _IconHarness()
        harness._icon_download_in_progress = True
        harness.refresh_icon_page = mock.Mock()
        harness.on_icon_download_clicked(None)
        harness.refresh_icon_page.assert_not_called()
        self.assertEqual(_SyncThread.started, [])

    def test_not_ready_url_only_refreshes(self):
        harness = _IconHarness()
        harness.refresh_icon_page = mock.Mock()
        harness.address_entry.get_text.return_value = 'nothing'
        harness.on_icon_download_clicked(None)
        harness.refresh_icon_page.assert_called_once_with()
        self.assertEqual(harness.calls, ['export_state'])
        self.assertFalse(harness._icon_download_in_progress)

    def test_ready_url_starts_worker(self):
        harness = _IconHarness()
        harness.address_entry.get_text.return_value = ' https://example.com '
        harness.on_icon_download_clicked(None)
        self.assertTrue(harness._icon_download_in_progress)
        harness.icon_page_status.set_text.assert_called_with(t('icon_page_status_searching'))
        (thread,) = _SyncThread.started
        self.assertEqual(thread.target, harness._load_icon_from_url)
        self.assertEqual(thread.args, ('https://example.com',))
        self.assertTrue(thread.daemon)


class ChoiceDialogTests(unittest.TestCase):
    def test_without_root_the_answer_is_no(self):
        harness = _IconHarness()
        harness.root = None
        answers = []
        harness._present_choice_dialog(None, 'Sure?', answers.append)
        self.assertEqual(answers, [False])

    def test_alert_dialog_path(self):
        adw = mock.MagicMock()
        harness = _IconHarness()
        answers = []
        with mock.patch.object(icon_module, 'Adw', adw):
            harness._present_choice_dialog(None, 'Sure?', answers.append, destructive=True)
        dialog = adw.AlertDialog.new.return_value
        adw.AlertDialog.new.assert_called_once_with(t('app_title'), 'Sure?')
        dialog.set_response_appearance.assert_called_once_with('yes', adw.ResponseAppearance.DESTRUCTIVE)
        dialog.present.assert_called_once_with(harness.root)
        handler = dialog.connect.call_args.args[1]
        handler(dialog, 'yes')
        handler(dialog, 'no')
        self.assertEqual(answers, [True, False])

    def test_message_dialog_fallback(self):
        adw = mock.MagicMock(spec=['MessageDialog', 'ResponseAppearance'])
        harness = _IconHarness()
        answers = []
        with mock.patch.object(icon_module, 'Adw', adw):
            harness._present_choice_dialog(None, 'Sure?', answers.append)
            harness._present_choice_dialog(None, 'Really?', answers.append, destructive=True)
        dialog = adw.MessageDialog.new.return_value
        adw.MessageDialog.new.assert_any_call(harness.root, t('app_title'), 'Sure?')
        dialog.set_response_appearance.assert_called_once_with('yes', adw.ResponseAppearance.DESTRUCTIVE)
        self.assertEqual(dialog.present.call_count, 2)
        dialog.connect.call_args.args[1](dialog, 'yes')
        self.assertEqual(answers, [True])

    def test_upload_click_opens_file_dialog(self):
        harness = _IconHarness()
        harness.open_icon_file_dialog = mock.Mock()
        harness.on_icon_upload_clicked(None)
        self.assertTrue(harness._icon_upload_dialog_active)
        self.assertEqual(harness.calls, ['cancel_timers', 'cancel_validation'])
        harness.open_icon_file_dialog.assert_called_once_with()

    def test_delete_click_deletes_only_after_confirmation(self):
        for confirmed in (True, False):
            harness = _IconHarness()
            harness.delete_icon = mock.Mock()
            harness._present_choice_dialog = lambda anchor, message, on_result, destructive=False, answer=confirmed: on_result(answer)
            harness.on_icon_delete_clicked('button')
            self.assertEqual(harness.delete_icon.called, confirmed)


class DeleteIconTests(_TempDirCase):
    def _harness(self, value):
        harness = _IconHarness({ICON_PATH_KEY: value})
        harness.refresh_icon_preview = mock.Mock()
        harness.refresh_icon_page = mock.Mock()
        harness._emit_visual_changed = mock.Mock()
        return harness

    def test_deletes_managed_icon_file(self):
        managed = _write_png(self.icon_dir / 'webapp-entry-7.png')
        harness = self._harness(str(managed))
        harness.delete_icon()
        self.assertFalse(managed.exists())
        self.assertEqual(harness.options[ICON_PATH_KEY], '')
        harness.icon_page_status.set_text.assert_called_once_with(t('icon_page_status_deleted'))
        self.assertEqual(harness.calls, ['export_state', 'save_desktop'])
        harness._emit_visual_changed.assert_called_once_with()

    def test_deletes_icon_named_after_title_slug(self):
        managed = _write_png(self.icon_dir / 'example_app.png')
        self._harness(str(managed)).delete_icon()
        self.assertFalse(managed.exists())

    def test_keeps_foreign_files(self):
        foreign = _write_png(self.tmp / 'mine' / 'webapp-entry-7.png')
        other = _write_png(self.icon_dir / 'someone-else.png')
        for path in (foreign, other):
            harness = self._harness(str(path))
            harness.delete_icon()
            self.assertTrue(path.exists())
            self.assertEqual(harness.options[ICON_PATH_KEY], '')

    def test_theme_name_just_clears_option(self):
        harness = self._harness('firefox')
        harness.delete_icon()
        self.assertEqual(harness.options[ICON_PATH_KEY], '')

    def test_unlink_failure_is_logged(self):
        managed = _write_png(self.icon_dir / 'webapp-entry-7.png')
        harness = self._harness(str(managed))
        with mock.patch.object(Path, 'unlink', side_effect=OSError('busy')), self.assertLogs(icon_module.LOG, level='WARNING'):
            harness.delete_icon()
        self.assertEqual(harness.options[ICON_PATH_KEY], '')


class LoadIconFromUrlTests(_TempDirCase):
    def setUp(self):
        super().setUp()
        self.glib = _glib_stub()
        patcher = mock.patch.object(icon_module, 'GLib', self.glib)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_first_working_candidate_is_applied(self):
        icon = _write_png(self.tmp / 'dl.png')
        harness = _IconHarness()
        harness._download_favicon = mock.Mock(side_effect=[OSError('no'), RuntimeError('odd'), str(icon)])
        with mock.patch.object(icon_module, 'candidate_urls_for_input', return_value=['https://a', 'https://b', 'http://c']) as candidates:
            harness._load_icon_from_url('a')
        candidates.assert_called_once_with('a', prefer_https=True, include_http_fallback=True)
        self.glib.idle_add.assert_called_once_with(harness._apply_downloaded_icon, str(icon))

    def test_all_candidates_failing_reports_failure(self):
        harness = _IconHarness()
        harness._download_favicon = mock.Mock(return_value=None)
        with mock.patch.object(icon_module, 'candidate_urls_for_input', return_value=['https://a']):
            harness._load_icon_from_url('a')
        self.glib.idle_add.assert_called_once_with(harness._finish_icon_download, t('icon_page_status_download_failed'))

    def test_candidate_generation_crash_reports_failure(self):
        harness = _IconHarness()
        with mock.patch.object(icon_module, 'candidate_urls_for_input', side_effect=RuntimeError('boom')):
            harness._load_icon_from_url('a')
        self.glib.idle_add.assert_called_once_with(harness._finish_icon_download, t('icon_page_status_download_failed'))

    def test_finish_icon_download_resets_state(self):
        harness = _IconHarness()
        harness.refresh_icon_page = mock.Mock()
        harness._icon_download_in_progress = True
        self.assertFalse(harness._finish_icon_download('done'))
        self.assertFalse(harness._icon_download_in_progress)
        self.assertIsNone(harness._compact_mode_override)
        self.assertEqual(harness._inline_editor_save_source_ids, {'css': 0, 'javascript': 0})
        harness.icon_download_button.set_sensitive.assert_called_with(True)
        harness.icon_page_status.set_text.assert_called_with('done')
        harness.refresh_icon_page.assert_called_once_with()
        harness.icon_page_status.set_text.reset_mock()
        harness._finish_icon_download()
        harness.icon_page_status.set_text.assert_not_called()

    def test_set_icon_page_status(self):
        harness = _IconHarness()
        self.assertFalse(harness._set_icon_page_status('hi'))
        harness.icon_page_status.set_text.assert_called_once_with('hi')
        self.assertEqual(harness.calls, ['export_state'])

    def test_apply_downloaded_icon_moves_and_finishes(self):
        temp_icon = _write_png(self.tmp / 'dl.png')
        harness = _IconHarness()
        harness._apply_icon_path = mock.Mock()
        harness._finish_icon_download = mock.Mock(return_value=False)
        self.assertFalse(harness._apply_downloaded_icon(str(temp_icon)))
        target = self.icon_dir / 'webapp-entry-7.png'
        self.assertTrue(target.exists())
        self.assertFalse(temp_icon.exists())
        harness._apply_icon_path.assert_called_once_with(target)
        harness._finish_icon_download.assert_called_once_with(t('icon_page_status_downloaded'))


# ---------------------------------------------------------------------------
# Gio file helpers and file dialogs
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self, chunks, fail_after=None, close_error=False):
        self.chunks = list(chunks)
        self.fail_after = fail_after
        self.close_error = close_error
        self.closed = False
        self.reads = 0

    def read_bytes(self, _size, _cancellable):
        self.reads += 1
        if self.fail_after is not None and self.reads > self.fail_after:
            raise GLib.Error('read failed')
        return self.chunks.pop(0) if self.chunks else b''

    def close(self, _cancellable):
        self.closed = True
        if self.close_error:
            raise GLib.Error('close failed')


class _FakeGFile:
    def __init__(self, path=None, stream=None, read_error=None, uri='sftp://host/file', uri_error=False):
        self._path = path
        self._stream = stream
        self._read_error = read_error
        self._uri = uri
        self._uri_error = uri_error

    def get_path(self):
        return self._path

    def read(self, _cancellable):
        if self._read_error is not None:
            raise self._read_error
        return self._stream

    def get_uri(self):
        if self._uri_error:
            raise GLib.Error('no uri')
        return self._uri


class CopyGFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        tmpdir = str(self.tmp)
        patcher = mock.patch.object(icon_module, 'tempfile', SimpleNamespace(mkstemp=lambda suffix='': tempfile.mkstemp(suffix=suffix, dir=tmpdir)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_none_and_local_files(self):
        harness = _IconHarness()
        self.assertIsNone(harness._copy_gfile_to_temp_path(None))
        self.assertEqual(harness._copy_gfile_to_temp_path(_FakeGFile(path='/some/file.png')), Path('/some/file.png'))

    def test_remote_file_is_streamed_into_temp_file(self):
        stream = _FakeStream([b'abc', b'def'], close_error=True)
        result = _IconHarness()._copy_gfile_to_temp_path(_FakeGFile(stream=stream), '.icon')
        self.assertEqual(result.parent, self.tmp)
        self.assertEqual(result.suffix, '.icon')
        self.assertEqual(result.read_bytes(), b'abcdef')
        self.assertTrue(stream.closed)

    def test_read_error_returns_none_and_cleans_up(self):
        harness = _IconHarness()
        with self.assertLogs(icon_module.LOG, level='WARNING'):
            self.assertIsNone(harness._copy_gfile_to_temp_path(_FakeGFile(read_error=GLib.Error('denied'), uri_error=True)))
        stream = _FakeStream([b'abc'], fail_after=1)
        with self.assertLogs(icon_module.LOG, level='WARNING'):
            self.assertIsNone(harness._copy_gfile_to_temp_path(_FakeGFile(stream=stream)))
        self.assertTrue(stream.closed)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_failed_cleanup_of_partial_copy_is_tolerated(self):
        stream = _FakeStream([], fail_after=0)
        with mock.patch.object(Path, 'unlink', side_effect=OSError('busy')), self.assertLogs(icon_module.LOG, level='WARNING'):
            self.assertIsNone(_IconHarness()._copy_gfile_to_temp_path(_FakeGFile(stream=stream)))

    def test_real_non_local_gio_file_is_copied(self):
        # Regression: read_bytes() returns GLib.Bytes (not writable as-is, and truthy when
        # empty), so files without a local path (portal/GVFS) could not be copied.
        source = Gio.File.new_for_uri('resource:///org/gtk/libgtk/theme/Empty/gtk.css')
        expected = source.load_contents(None)[1]
        result = _IconHarness()._copy_gfile_to_temp_path(source, '.css')
        self.assertIsNotNone(result)
        self.assertEqual(result.read_bytes(), bytes(expected))


class WriteGFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_none_is_rejected(self):
        self.assertFalse(_IconHarness()._write_text_to_gfile(None, 'x'))

    def test_local_path_gets_wapp_suffix(self):
        harness = _IconHarness()
        self.assertTrue(harness._write_text_to_gfile(Gio.File.new_for_path(str(self.tmp / 'export.json')), 'ä'))
        self.assertEqual((self.tmp / 'export.wapp').read_bytes(), 'ä'.encode())
        self.assertTrue(harness._write_text_to_gfile(Gio.File.new_for_path(str(self.tmp / 'keep.WAPP')), 'y'))
        self.assertEqual((self.tmp / 'keep.WAPP').read_text(), 'y')

    def _remote(self, basename='export', parent=True):
        remote = mock.MagicMock()
        remote.get_path.return_value = None
        remote.get_basename.return_value = basename
        if not parent:
            remote.get_parent.return_value = None
        return remote

    def test_remote_file_gets_wapp_child(self):
        remote = self._remote('export')
        self.assertTrue(_IconHarness()._write_text_to_gfile(remote, 'data'))
        remote.get_parent.return_value.get_child.assert_called_once_with('export.wapp')
        child = remote.get_parent.return_value.get_child.return_value
        stream = child.replace.return_value
        stream.write_all.assert_called_once_with(b'data', None)
        stream.close.assert_called_once_with(None)

    def test_remote_file_already_named_wapp(self):
        remote = self._remote('export.wapp')
        self.assertTrue(_IconHarness()._write_text_to_gfile(remote, 'data'))
        remote.get_parent.assert_not_called()
        remote.replace.return_value.write_all.assert_called_once_with(b'data', None)

    def test_remote_without_parent_writes_in_place(self):
        remote = self._remote('', parent=False)
        remote.get_basename.return_value = None
        self.assertTrue(_IconHarness()._write_text_to_gfile(remote, 'data'))
        remote.replace.assert_called_once()

    def test_remote_write_error_returns_false(self):
        remote = self._remote('export.wapp')
        remote.replace.side_effect = GLib.Error('denied')
        remote.get_uri.side_effect = GLib.Error('no uri')
        with self.assertLogs(icon_module.LOG, level='WARNING'):
            self.assertFalse(_IconHarness()._write_text_to_gfile(remote, 'data'))


class FileDialogTests(unittest.TestCase):
    def test_filter_store_keeps_first_filter(self):
        store, first = _IconHarness()._build_file_filter_store([('PNG', '*.png'), ('SVG', '*.svg')])
        self.assertEqual(store.get_n_items(), 2)
        self.assertIs(store.get_item(0), first)
        self.assertEqual(first.get_name(), 'PNG')
        empty_store, none_filter = _IconHarness()._build_file_filter_store(None)
        self.assertEqual(empty_store.get_n_items(), 0)
        self.assertIsNone(none_filter)

    def test_open_file_dialog_with_gtk_file_dialog(self):
        gtk = mock.MagicMock()
        harness = _IconHarness()
        store = mock.MagicMock()
        store.get_n_items.return_value = 1
        harness._build_file_filter_store = mock.Mock(return_value=(store, 'first'))
        results = []
        def callback(*args):
            results.append(args)
        with mock.patch.object(icon_module, 'Gtk', gtk):
            harness._open_file_dialog('Pick', callback, [('PNG', '*.png')])
        dialog = gtk.FileDialog.return_value
        gtk.FileDialog.assert_called_once_with(title='Pick', modal=True)
        dialog.set_filters.assert_called_once_with(store)
        dialog.set_default_filter.assert_called_once_with('first')
        parent, _cancellable, handle_open = dialog.open.call_args.args
        self.assertIs(parent, harness.root)
        source = mock.MagicMock()
        source.open_finish.return_value = 'file'
        handle_open(source, 'result')
        source.open_finish.side_effect = GLib.Error('dismissed')
        handle_open(source, 'result')
        self.assertEqual(results, [('file',), (None, Gtk.ResponseType.CANCEL)])

    def test_open_file_dialog_without_filters(self):
        gtk = mock.MagicMock()
        harness = _IconHarness()
        # Gio.ListStore needs a real GType, so build the empty store up front.
        harness._build_file_filter_store = mock.Mock(return_value=_IconHarness()._build_file_filter_store([]))
        with mock.patch.object(icon_module, 'Gtk', gtk):
            harness._open_file_dialog('Pick', lambda *a: None)
        harness._build_file_filter_store.assert_called_once_with([])
        gtk.FileDialog.return_value.set_filters.assert_not_called()
        gtk.FileDialog.return_value.set_default_filter.assert_not_called()

    def test_open_file_dialog_native_fallback(self):
        gtk = mock.MagicMock(spec=['FileChooserNative', 'FileChooserAction', 'FileFilter'])
        gtk.FileFilter.side_effect = _fresh_mock_factory
        harness = _IconHarness()
        callback = mock.Mock()
        with mock.patch.object(icon_module, 'Gtk', gtk):
            harness._open_file_dialog('Pick', callback, [('PNG', '*.png'), ('SVG', '*.svg')])
        gtk.FileChooserNative.new.assert_called_once_with('Pick', harness.root, gtk.FileChooserAction.OPEN, t('icon_dialog_open'), t('icon_dialog_cancel'))
        dialog = gtk.FileChooserNative.new.return_value
        self.assertEqual(dialog.add_filter.call_count, 2)
        first = dialog.add_filter.call_args_list[0].args[0]
        first.add_pattern.assert_called_once_with('*.png')
        dialog.set_filter.assert_called_once_with(first)
        dialog.connect.assert_called_once_with('response', callback)
        dialog.show.assert_called_once_with()

    def test_open_file_dialog_native_without_patterns(self):
        gtk = mock.MagicMock(spec=['FileChooserNative', 'FileChooserAction', 'FileFilter'])
        with mock.patch.object(icon_module, 'Gtk', gtk):
            _IconHarness()._open_file_dialog('Pick', mock.Mock())
        gtk.FileChooserNative.new.return_value.set_filter.assert_not_called()

    def test_save_file_dialog_with_gtk_file_dialog(self):
        gtk = mock.MagicMock()
        harness = _IconHarness()
        results = []
        with mock.patch.object(icon_module, 'Gtk', gtk):
            harness._save_file_dialog('Save', 'x.wapp', lambda *a: results.append(a))
        gtk.FileDialog.assert_called_once_with(title='Save', modal=True, initial_name='x.wapp')
        handle_save = gtk.FileDialog.return_value.save.call_args.args[2]
        source = mock.MagicMock()
        source.save_finish.return_value = 'file'
        handle_save(source, 'r')
        source.save_finish.side_effect = GLib.Error('dismissed')
        handle_save(source, 'r')
        self.assertEqual(results, [('file',), (None, Gtk.ResponseType.CANCEL)])

    def test_save_file_dialog_native_fallback(self):
        gtk = mock.MagicMock(spec=['FileChooserNative', 'FileChooserAction'])
        callback = mock.Mock()
        harness = _IconHarness()
        with mock.patch.object(icon_module, 'Gtk', gtk):
            harness._save_file_dialog('Save', 'x.wapp', callback)
        dialog = gtk.FileChooserNative.new.return_value
        gtk.FileChooserNative.new.assert_called_once_with('Save', harness.root, gtk.FileChooserAction.SAVE, t('icon_dialog_open'), t('icon_dialog_cancel'))
        dialog.set_current_name.assert_called_once_with('x.wapp')
        dialog.connect.assert_called_once_with('response', callback)
        dialog.show.assert_called_once_with()

    def test_open_icon_file_dialog(self):
        harness = _IconHarness()
        harness._open_file_dialog = mock.Mock()
        self.assertFalse(harness.open_icon_file_dialog())
        harness._open_file_dialog.assert_called_once_with(t('icon_dialog_title'), harness.on_icon_file_selected)


class IconFileSelectedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _harness(self, store_result=True):
        harness = _IconHarness()
        harness._icon_upload_dialog_active = True
        harness._store_icon_file = mock.Mock(return_value=store_result)
        harness.refresh_icon_page = mock.Mock()
        return harness

    def test_local_gio_file_is_stored_and_kept(self):
        source = _write_png(self.tmp / 'pick.png')
        harness = self._harness()
        harness.on_icon_file_selected(Gio.File.new_for_path(str(source)))
        self.assertFalse(harness._icon_upload_dialog_active)
        harness._store_icon_file.assert_called_once_with(source)
        harness.icon_page_status.set_text.assert_called_once_with(t('icon_page_status_uploaded'))
        harness.refresh_icon_page.assert_called_once_with()
        self.assertEqual(harness.calls, ['export_state'])
        self.assertTrue(source.exists())

    def test_failed_store_reports_upload_failure(self):
        source = _write_png(self.tmp / 'pick.png')
        harness = self._harness(store_result=False)
        harness.on_icon_file_selected(Gio.File.new_for_path(str(source)))
        harness.icon_page_status.set_text.assert_called_once_with(t('icon_page_status_upload_failed'))

    def test_native_dialog_cancel_destroys_dialog(self):
        harness = self._harness()
        dialog = mock.MagicMock()
        harness.on_icon_file_selected(dialog, Gtk.ResponseType.CANCEL)
        dialog.destroy.assert_called_once_with()
        harness._store_icon_file.assert_not_called()
        harness.on_icon_file_selected(None, Gtk.ResponseType.CANCEL)  # file dialog dismissed
        harness._store_icon_file.assert_not_called()

    def test_native_dialog_accept_uses_selected_file(self):
        source = _write_png(self.tmp / 'pick.png')
        harness = self._harness()
        dialog = mock.MagicMock()
        dialog.get_file.return_value = Gio.File.new_for_path(str(source))
        harness.on_icon_file_selected(dialog, Gtk.ResponseType.ACCEPT)
        dialog.destroy.assert_called_once_with()
        harness._store_icon_file.assert_called_once_with(source)

    def test_accept_without_file_reports_failure(self):
        harness = self._harness()
        dialog = mock.MagicMock()
        dialog.get_file.return_value = None
        harness.on_icon_file_selected(dialog, Gtk.ResponseType.ACCEPT)
        harness._store_icon_file.assert_not_called()
        harness.icon_page_status.set_text.assert_called_once_with(t('icon_page_status_upload_failed'))

    def test_temp_copy_of_remote_file_is_removed(self):
        temp_copy = self.tmp / 'copy.icon'
        temp_copy.write_bytes(b'x')
        harness = self._harness()
        harness._copy_gfile_to_temp_path = mock.Mock(return_value=temp_copy)
        dialog = mock.MagicMock()
        dialog.get_file.return_value.get_path.return_value = None
        harness.on_icon_file_selected(dialog, Gtk.ResponseType.ACCEPT)
        harness._store_icon_file.assert_called_once_with(temp_copy)
        self.assertFalse(temp_copy.exists())


if __name__ == '__main__':
    unittest.main()

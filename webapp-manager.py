import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from pathlib import Path
import math
import sqlite3
import json
import tempfile
import shutil
import base64
import os
import subprocess
import threading
from datetime import datetime, timezone
from PIL import Image
from gi.repository import Adw, Gdk, Gio, GObject, Gtk, GLib, Pango

from database import Database
from desktop_entries import (
    build_launch_command,
    delete_managed_entry_artifacts,
    export_desktop_file,
    exportable_entry,
    get_expected_desktop_path,
    list_managed_desktop_files,
)
from icon_pipeline import get_managed_icon_path, normalize_icon_to_png
from input_validation import load_import_payloads_from_path, load_and_normalize_wapp_payload_from_path, sanitize_desktop_value, validate_icon_source_path
from browser_option_logic import (
    normalize_option_dict,
    normalize_option_rows,
    browser_family_for_command,
    browser_managed_option_keys,
    browser_state_key,
    encode_browser_state,
)
from webapp_constants import (
    ADDRESS_KEY,
    ICON_PATH_KEY,
    USER_AGENT_NAME_KEY,
    USER_AGENT_VALUE_KEY,
    PROFILE_NAME_KEY,
    PROFILE_PATH_KEY,
    APP_MODE_KEY,
    ONLY_HTTPS_KEY,
    COLOR_SCHEME_KEY,
    OPTION_ADBLOCK_KEY,
    OPTION_CLEAR_CACHE_ON_EXIT_KEY,
    OPTION_CLEAR_COOKIES_ON_EXIT_KEY,
    OPTION_DISABLE_AI_KEY,
    OPTION_FORCE_PRIVACY_KEY,
    OPTION_KEEP_IN_BACKGROUND_KEY,
    OPTION_NOTIFICATIONS_KEY,
    OPTION_PRESERVE_SESSION_KEY,
    OPTION_SWIPE_KEY,
)
from detail_page import DetailPage
from custom_assets import count_asset_references, detach_asset_from_entries, format_asset_date, import_custom_asset, list_custom_assets, remove_custom_asset
from i18n import available_languages, get_app_config, get_configured_language_value, invalidate_i18n_cache, save_app_config, t
from logger_setup import get_logger
from engine_support import available_engines, engine_icon_name
from browser_profiles import inspect_profile_copy_source, read_profile_settings, rename_unused_managed_profile_directories
from ui_icons import create_image_from_ref
from app_state import WebAppState
from app_identity import APP_DIR, APP_ID, APP_ICON_NAME, APP_DB_PATH
from manager_integration import ensure_manager_desktop_integration, headerbar_decoration_layout_without_icon

Adw.init()
LOG = get_logger(__name__)
APP_VERSION = '64d'


MANAGED_IMPORT_OPTION_KEYS = [
    'Kiosk',
    APP_MODE_KEY,
    'Frameless',
    OPTION_PRESERVE_SESSION_KEY,
    OPTION_KEEP_IN_BACKGROUND_KEY,
    OPTION_NOTIFICATIONS_KEY,
    OPTION_SWIPE_KEY,
    OPTION_ADBLOCK_KEY,
    ONLY_HTTPS_KEY,
    OPTION_CLEAR_CACHE_ON_EXIT_KEY,
    OPTION_CLEAR_COOKIES_ON_EXIT_KEY,
    OPTION_DISABLE_AI_KEY,
    OPTION_FORCE_PRIVACY_KEY,
    COLOR_SCHEME_KEY,
]


def format_profile_size(profile_path: str) -> str:
    try:
        path = Path((profile_path or '').strip()).expanduser()
        if not path.exists():
            return ''
        total = 0
        if path.is_file():
            total = path.stat().st_size
        else:
            for child in path.rglob('*'):
                try:
                    if child.is_file():
                        total += child.stat().st_size
                except OSError:
                    continue
        if total <= 0:
            return ''
        gb = total / (1024 ** 3)
        if gb >= 1:
            return f'{gb:.2f} GB'
        mb = total / (1024 ** 2)
        return f'{mb:.0f} MB'
    except OSError:
        return ''




CONFIG = {}
ENGINES = available_engines()
css_provider = Gtk.CssProvider()
try:
    css_provider.load_from_path(str(APP_DIR / 'style.css'))
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
except (GLib.Error, TypeError, ValueError, AttributeError) as error:
    LOG.error('Failed to load CSS: %s', error)



class Entry(GObject.GObject):
    id = GObject.Property(type=int)
    title = GObject.Property(type=str, default='')
    description = GObject.Property(type=str, default='')
    active = GObject.Property(type=bool, default=True)

    def __init__(self, id, title, description='', active=True):
        super().__init__()
        self.id = id
        self.title = title
        self.description = description
        self.active = active


class MainWindow(Adw.ApplicationWindow):

    def __init__(self, app):
        super().__init__(application=app)
        self.set_title(t('app_title'))
        self._window_state_save_source_id = 0
        self._window_state = self._load_window_state()
        self.set_default_size(
            int(self._window_state.get('width', 500) or 500),
            int(self._window_state.get('height', 600) or 600),
        )
        try:
            self.set_icon_name(APP_ICON_NAME)
        except (TypeError, ValueError):
            pass
        self.db = Database(str(APP_DB_PATH))
        self.search_text = ''
        self.search_visible = False
        self.reconcile_queue = []
        self._options_cache = {}
        self._profile_size_cache = {}
        self._profile_size_pending = set()
        self.ui_settings = self._load_ui_settings()
        self.language_setting = self._load_language_setting()
        self._profile_resync_running = False
        self._profile_resync_cancel_event = None
        self._profile_resync_dialog = None
        self._profile_resync_progress_label = None
        self._profile_resync_progress_bar = None
        self._profile_resync_total = 0
        self._import_progress_dialog = None
        self._import_progress_label = None
        self._import_progress_bar = None
        self._import_cancel_requested = False
        self._import_total = 0
        self._startup_profile_cleanup_done = False

        self.header_bar = Adw.HeaderBar()
        self.header_bar.set_decoration_layout(headerbar_decoration_layout_without_icon())
        self.search_button = Gtk.Button(icon_name='system-search-symbolic')
        self.search_button.connect('clicked', self.on_search_clicked)
        self.refresh_button = Gtk.Button(icon_name='view-refresh-symbolic')
        self.refresh_button.set_tooltip_text(t('resync_profiles_button'))
        self.refresh_button.connect('clicked', self.on_refresh_clicked)
        self.add_button = Gtk.Button(icon_name='list-add-symbolic')
        self.add_button.connect('clicked', self.on_add_entry)
        self.settings_button = Gtk.Button(icon_name='emblem-system-symbolic')
        self.settings_button.set_tooltip_text(t('settings_title'))
        self.settings_button.connect('clicked', self.show_settings_page)
        self.back_button = Gtk.Button.new_from_icon_name('go-previous-symbolic')
        self.back_button.connect('clicked', self.show_list_page)
        self.header_bar.pack_start(self.search_button)
        self.header_bar.pack_start(self.refresh_button)
        self.header_bar.pack_start(self.back_button)
        self.header_bar.pack_start(self.settings_button)
        self.header_bar.pack_end(self.add_button)
        self.list_title_widget = self._build_list_title_widget()
        self.header_bar.set_title_widget(self.list_title_widget)
        self._show_overview_header()

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_vexpand(True)
        self.stack.set_hexpand(True)

        self.stack_overlay = Gtk.Overlay()
        self.stack_overlay.set_child(self.stack)
        self.stack_overlay.set_vexpand(True)
        self.stack_overlay.set_hexpand(True)

        self.busy_overlay = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.busy_overlay.add_css_class('busy-overlay')
        self.busy_overlay.set_halign(Gtk.Align.CENTER)
        self.busy_overlay.set_valign(Gtk.Align.CENTER)
        self.busy_overlay.set_visible(False)
        self.busy_overlay.set_can_target(True)
        self.busy_spinner = Gtk.Spinner()
        self.busy_spinner.set_size_request(32, 32)
        self.busy_label = Gtk.Label(label=t('loading'))
        self.busy_label.add_css_class('dim-label')
        self.busy_overlay.append(self.busy_spinner)
        self.busy_overlay.append(self.busy_label)
        self.stack_overlay.add_overlay(self.busy_overlay)

        self.global_toast_revealer = Gtk.Revealer()
        self.global_toast_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.global_toast_revealer.set_transition_duration(180)
        self.global_toast_revealer.set_halign(Gtk.Align.CENTER)
        self.global_toast_revealer.set_valign(Gtk.Align.START)
        self.global_toast_revealer.set_margin_top(12)
        self.global_toast_revealer.set_can_target(False)
        self.global_toast_revealer.set_reveal_child(False)
        self.global_toast_timeout_id = 0
        self.global_toast_label = Gtk.Label(label='')
        self.global_toast_label.set_xalign(0.5)
        self.global_toast_label.add_css_class('detail-toast-label')
        self.global_toast_revealer.set_child(self.global_toast_label)
        self.stack_overlay.add_overlay(self.global_toast_revealer)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main_box.append(self.header_bar)
        main_box.append(self.stack_overlay)
        self.set_content(main_box)
        self.connect('close-request', self._on_close_request)
        self.connect('notify::default-width', self._on_window_size_notify)
        self.connect('notify::default-height', self._on_window_size_notify)
        self.connect('notify::maximized', self._on_window_size_notify)
        self._apply_window_state()

        self.list_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.list_page.set_vexpand(True)
        self.stack.add_named(self.list_page, 'list_page')

        self.detail_placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.detail_placeholder.set_vexpand(True)
        self.detail_placeholder.set_hexpand(True)
        placeholder_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        placeholder_content.set_margin_top(24)
        placeholder_content.set_margin_start(20)
        placeholder_content.set_margin_end(20)
        placeholder_content.set_margin_bottom(20)
        placeholder_title = Gtk.Label(label=t('app_title'))
        placeholder_title.add_css_class('title-3')
        placeholder_title.set_xalign(0)
        placeholder_title.set_visible(False)
        placeholder_spinner = Gtk.Spinner()
        placeholder_spinner.start()
        placeholder_spinner.set_halign(Gtk.Align.CENTER)
        placeholder_spinner.set_margin_top(24)
        placeholder_content.append(placeholder_spinner)
        self.detail_placeholder.append(placeholder_content)
        self.stack.add_named(self.detail_placeholder, 'detail_placeholder')

        self.settings_page = self._build_settings_page()
        self.stack.add_named(self.settings_page, 'settings_page')
        self.settings_assets_page = self._build_assets_settings_page()
        self.stack.add_named(self.settings_assets_page, 'settings_assets_page')

        self.list_scrolled = Gtk.ScrolledWindow()
        self.list_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.list_scrolled.set_vexpand(True)
        self.list_page.append(self.list_scrolled)

        self.list_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.list_content.set_margin_top(8)
        self.list_content.set_margin_start(8)
        self.list_content.set_margin_end(8)
        self.list_content.set_margin_bottom(8)
        self.list_content.set_vexpand(True)
        self.list_scrolled.set_child(self.list_content)

        self.search_entry = Gtk.Entry()
        self.search_entry.set_placeholder_text(t('search_placeholder'))
        self.search_entry.connect('changed', self.on_search_entry_changed)
        self.search_entry.set_visible(False)
        self.list_content.append(self.search_entry)

        self.empty_label = Gtk.Label(label=t('search_empty'))
        self.empty_label.add_css_class('dim-label')
        self.empty_label.set_visible(False)
        self.list_content.append(self.empty_label)

        self.entries_store = Gio.ListStore(item_type=Entry)
        self.load_entries_from_db()
        self.custom_filter = Gtk.CustomFilter.new(self.filter_entries)
        self.filtered_model = Gtk.FilterListModel(model=self.entries_store, filter=self.custom_filter)

        factory = Gtk.SignalListItemFactory()
        factory.connect('setup', self.on_factory_setup)
        factory.connect('bind', self.on_factory_bind)
        self.selection = Gtk.SingleSelection.new(self.filtered_model)
        self.selection.set_can_unselect(True)
        self.autoselect = False if hasattr(self.selection, 'set_autoselect') else None
        if hasattr(self.selection, 'set_autoselect'):
            self.selection.set_autoselect(False)
        self.list_view = Gtk.ListView.new(self.selection, factory)
        self.list_view.set_single_click_activate(True)
        self.list_view.connect('activate', self.on_list_view_activate)
        self.list_view.set_vexpand(True)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(self.list_view)
        scrolled.set_vexpand(True)
        self.list_content.append(scrolled)
        self.detail_pages = {}
        self._creating_entry = False
        self.connect('destroy', self.close_event)
        self.update_empty_state()
        self._apply_ui_appearance_setting()

    def _load_window_state(self):
        try:
            data = get_app_config(force_reload=True)
            state = data.get('window_state', {}) if isinstance(data, dict) else {}
            if isinstance(state, dict):
                return state
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return {}

    def _apply_window_state(self):
        try:
            if bool(self._window_state.get('maximized')):
                self.maximize()
        except AttributeError:
            pass

    def _load_ui_settings(self):
        try:
            data = get_app_config(force_reload=True)
            settings = data.get('settings', {}) if isinstance(data, dict) else {}
            if isinstance(settings, dict):
                return {'appearance': str(settings.get('appearance', 'auto') or 'auto')}
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            LOG.debug('Failed to load UI settings', exc_info=True)
        return {'appearance': 'auto'}

    def _load_language_setting(self):
        try:
            return get_configured_language_value()
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            LOG.debug('Failed to load language setting', exc_info=True)
        return 'system'

    def _save_ui_settings(self):
        try:
            config = dict(get_app_config(force_reload=True) or {})
            settings = dict(config.get('settings', {}) or {})
            settings['appearance'] = self._appearance_value()
            config['settings'] = settings
            save_app_config(config)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            LOG.error('Failed to save UI settings', exc_info=True)

    def _appearance_value(self):
        value = str((self.ui_settings or {}).get('appearance', 'auto')).strip().lower()
        if value not in {'auto', 'dark', 'light'}:
            return 'auto'
        return value

    def _apply_ui_appearance_setting(self):
        try:
            style_manager = Adw.StyleManager.get_default()
            appearance = self._appearance_value()
            mapping = {
                'auto': Adw.ColorScheme.DEFAULT,
                'dark': Adw.ColorScheme.FORCE_DARK,
                'light': Adw.ColorScheme.FORCE_LIGHT,
            }
            style_manager.set_color_scheme(mapping.get(appearance, Adw.ColorScheme.DEFAULT))
        except AttributeError:
            LOG.error('Failed to apply UI appearance', exc_info=True)

    def _schedule_window_state_save(self):
        if self._window_state_save_source_id:
            return

        def do_save():
            self._window_state_save_source_id = 0
            self._save_window_state()
            return False

        self._window_state_save_source_id = GLib.timeout_add(300, do_save)

    def _collect_window_state(self):
        width = int(self.get_default_size()[0] or 500)
        height = int(self.get_default_size()[1] or 600)
        return {
            'width': max(320, width),
            'height': max(320, height),
            'maximized': bool(self.is_maximized()),
        }

    def _save_window_state(self):
        try:
            state = self._collect_window_state()
            config = dict(get_app_config(force_reload=True) or {})
            config['window_state'] = state
            save_app_config(config)
        except (OSError, TypeError, ValueError) as error:
            LOG.debug('Failed to save window state: %s', error)

    def _on_window_size_notify(self, *_args):
        self._schedule_window_state_save()

    def _on_close_request(self, *_args):
        self._save_window_state()
        return False

    def _build_list_title_widget(self):
        label = Gtk.Label(xalign=0)
        label.set_text(t('app_title'))
        label.add_css_class('title-4')
        label.add_css_class('overview-title')
        label.set_halign(Gtk.Align.CENTER)
        label.set_valign(Gtk.Align.CENTER)
        return label

    def _available_language_rows(self):
        rows = [('system', t('language_system'))]
        label_key_map = {
            'en': 'language_english',
            'de': 'language_german',
        }
        for item in available_languages(force_reload=True):
            code = str(item.get('code') or '').strip().lower()
            if not code or code == 'system':
                continue
            label = t(label_key_map.get(code, '')) if label_key_map.get(code) else ''
            label = label or str(item.get('name') or code.upper())
            rows.append((code, label))
        seen = set()
        deduped = []
        for code, label in rows:
            if code in seen:
                continue
            seen.add(code)
            deduped.append((code, label))
        return deduped

    def _rebuild_settings_page_view(self):
        if not hasattr(self, 'stack'):
            return
        previous_visible = None
        try:
            previous_visible = self.stack.get_visible_child_name()
        except (AttributeError, TypeError):
            previous_visible = None
        old_page = getattr(self, 'settings_page', None)
        if old_page is not None:
            try:
                self.stack.remove(old_page)
            except (AttributeError, TypeError):
                pass
        old_assets_page = getattr(self, 'settings_assets_page', None)
        if old_assets_page is not None:
            try:
                self.stack.remove(old_assets_page)
            except (AttributeError, TypeError):
                pass
        self.settings_page = self._build_settings_page()
        self.stack.add_named(self.settings_page, 'settings_page')
        self.settings_assets_page = self._build_assets_settings_page()
        self.stack.add_named(self.settings_assets_page, 'settings_assets_page')
        if previous_visible == 'settings_assets_page':
            self.stack.set_visible_child_name('settings_assets_page')
        elif previous_visible == 'settings_page':
            self.stack.set_visible_child_name('settings_page')

    def _refresh_translated_ui(self):
        try:
            self.list_title_widget.set_text(t('app_title'))
        except (AttributeError, TypeError):
            pass
        try:
            self.search_entry.set_placeholder_text(t('search_placeholder'))
        except (AttributeError, TypeError):
            pass
        try:
            self.empty_label.set_text(t('search_empty'))
        except (AttributeError, TypeError):
            pass
        try:
            self.refresh_button.set_tooltip_text(t('resync_profiles_button'))
        except (AttributeError, TypeError):
            pass
        try:
            self.settings_button.set_tooltip_text(t('settings_title'))
        except (AttributeError, TypeError):
            pass
        try:
            self.busy_label.set_text(t('loading'))
        except (AttributeError, TypeError):
            pass
        self._rebuild_settings_page_view()

    def _show_busy(self, message=None):
        self.busy_label.set_text(message or t('loading'))
        self.busy_overlay.set_visible(True)
        self.busy_spinner.start()

    def _build_settings_page(self):
        outer = Gtk.ScrolledWindow()
        outer.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outer.set_vexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        outer.set_child(content)

        swipe_back = Gtk.GestureSwipe.new()
        swipe_back.connect('swipe', lambda _g, vx, _vy: self.show_list_page() if vx > 0 else None)
        outer.add_controller(swipe_back)

        ui_group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        ui_group.add_css_class('preferences-group')
        ui_header = Gtk.Label(label=t('settings_ui_header'))
        ui_header.add_css_class('heading')
        ui_header.set_xalign(0)
        ui_group.append(ui_header)

        ui_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        ui_row.set_hexpand(True)
        ui_label = Gtk.Label(label=t('settings_appearance_label'))
        ui_label.set_xalign(0)
        ui_label.set_hexpand(True)
        self.ui_mode_labels = [t('color_scheme_auto'), t('color_scheme_dark'), t('color_scheme_light')]
        self.ui_mode_values = ['auto', 'dark', 'light']
        self.ui_mode_dropdown = Gtk.DropDown.new_from_strings(self.ui_mode_labels)
        self.ui_mode_dropdown.set_selected(self.ui_mode_values.index(self._appearance_value()))
        self.ui_mode_dropdown.connect('notify::selected', self.on_ui_mode_changed)
        ui_row.append(ui_label)
        ui_row.append(self.ui_mode_dropdown)
        ui_group.append(ui_row)

        language_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        language_row.set_hexpand(True)
        language_label = Gtk.Label(label=t('settings_language_label'))
        language_label.set_xalign(0)
        language_label.set_hexpand(True)
        language_rows = self._available_language_rows()
        self.language_values = [code for code, _label in language_rows]
        language_labels = [label for _code, label in language_rows]
        current_language = (self.language_setting or 'system').strip().lower() or 'system'
        try:
            language_index = self.language_values.index(current_language)
        except ValueError:
            language_index = 0
        self.language_dropdown = Gtk.DropDown.new_from_strings(language_labels)
        self.language_dropdown.set_selected(language_index)
        self.language_dropdown.connect('notify::selected', self.on_language_changed)
        language_row.append(language_label)
        language_row.append(self.language_dropdown)
        ui_group.append(language_row)
        content.append(ui_group)

        export_group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        export_group.add_css_class('preferences-group')
        export_header = Gtk.Label(label=t('settings_export_header'))
        export_header.add_css_class('heading')
        export_header.set_xalign(0)
        export_group.append(export_header)

        export_hint = Gtk.Label(label=t('settings_export_hint'))
        export_hint.add_css_class('dim-label')
        export_hint.set_wrap(True)
        export_hint.set_xalign(0)
        export_group.append(export_hint)

        export_zip_button = Gtk.Button(label=t('settings_export_all_button'))
        export_zip_button.connect('clicked', self.on_export_all_single_file_clicked)
        export_group.append(export_zip_button)
        content.append(export_group)

        assets_group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        assets_group.add_css_class('preferences-group')
        assets_header = Gtk.Label(label=t('settings_assets_header'))
        assets_header.add_css_class('heading')
        assets_header.set_xalign(0)
        assets_group.append(assets_header)

        assets_hint = Gtk.Label(label=t('settings_assets_hint'))
        assets_hint.add_css_class('dim-label')
        assets_hint.set_wrap(True)
        assets_hint.set_xalign(0)
        assets_group.append(assets_hint)

        assets_button = Gtk.Button(label=t('settings_assets_button'))
        assets_button.connect('clicked', self.show_assets_settings_page)
        assets_group.append(assets_button)
        content.append(assets_group)

        about_group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        about_group.add_css_class('preferences-group')
        about_header = Gtk.Label(label=t('settings_about_header'))
        about_header.add_css_class('heading')
        about_header.set_xalign(0)
        about_group.append(about_header)

        self.version_label = Gtk.Label(label=t('settings_about_version', version=self._read_app_version_label()))
        self.version_label.set_xalign(0)
        about_group.append(self.version_label)
        content.append(about_group)

        return outer

    def _build_assets_settings_page(self):
        outer = Gtk.ScrolledWindow()
        outer.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outer.set_vexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        outer.set_child(content)

        swipe_back = Gtk.GestureSwipe.new()
        swipe_back.connect('swipe', lambda _g, vx, _vy: self.show_settings_page() if vx > 0 else None)
        outer.add_controller(swipe_back)

        title = Gtk.Label(label=t('settings_assets_title'))
        title.add_css_class('heading')
        title.set_xalign(0)
        content.append(title)

        hint = Gtk.Label(label=t('settings_assets_subpage_hint'))
        hint.add_css_class('dim-label')
        hint.set_wrap(True)
        hint.set_xalign(0)
        content.append(hint)

        upload_button = Gtk.Button(label=t('settings_assets_upload_button'))
        upload_button.connect('clicked', self.on_upload_custom_asset_clicked)
        content.append(upload_button)

        self.settings_assets_empty_label = Gtk.Label(label=t('settings_assets_empty'))
        self.settings_assets_empty_label.add_css_class('dim-label')
        self.settings_assets_empty_label.set_wrap(True)
        self.settings_assets_empty_label.set_xalign(0)
        content.append(self.settings_assets_empty_label)

        self.settings_assets_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.append(self.settings_assets_list)

        self._refresh_assets_settings_list()
        return outer

    def _refresh_assets_settings_list(self):
        assets_box = getattr(self, 'settings_assets_list', None)
        if assets_box is None:
            return
        child = assets_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            assets_box.remove(child)
            child = next_child
        assets = list_custom_assets()
        if hasattr(self, 'settings_assets_empty_label'):
            self.settings_assets_empty_label.set_visible(not assets)
        for asset in assets:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.set_hexpand(True)
            row.add_css_class('preferences-group')

            name_label = Gtk.Label(label=str(asset.get('name') or ''), xalign=0)
            name_label.set_hexpand(True)
            name_label.set_wrap(True)
            row.append(name_label)

            type_label = Gtk.Label(label=str(asset.get('type') or '').upper(), xalign=0)
            type_label.add_css_class('dim-label')
            row.append(type_label)

            date_label = Gtk.Label(label=format_asset_date(asset.get('imported_at')), xalign=0)
            date_label.add_css_class('dim-label')
            row.append(date_label)

            delete_button = Gtk.Button(icon_name='user-trash-symbolic')
            delete_button.add_css_class('flat')
            delete_button.connect('clicked', lambda button, current_asset_id=asset['id']: self._confirm_delete_custom_asset(button, current_asset_id))
            row.append(delete_button)
            assets_box.append(row)

    def show_assets_settings_page(self, *args):
        current_child = self.stack.get_visible_child()
        if isinstance(current_child, DetailPage):
            return
        self._show_back_only_header()
        self._refresh_assets_settings_list()
        self.stack.set_visible_child_name('settings_assets_page')

    def on_upload_custom_asset_clicked(self, _button):
        dialog = Gtk.FileDialog(title=t('settings_assets_upload_dialog_title'), modal=True)
        try:
            dialog.open(self, None, self._on_upload_custom_asset_selected)
        except TypeError:
            dialog.open(self, None, self._on_upload_custom_asset_selected)

    def _on_upload_custom_asset_selected(self, dialog, result):
        file_obj = None
        temp_path = None
        try:
            if isinstance(result, Gio.File):
                file_obj = result
            else:
                file_obj = dialog.open_finish(result)
        except (AttributeError, GLib.Error, TypeError):
            return
        try:
            temp_path = self._copy_gfile_to_temp_path(file_obj, suffix=Path(file_obj.get_path() or '').suffix)
            if temp_path is None:
                self.show_overlay_notification(t('settings_assets_upload_failed'), timeout_ms=3200)
                return
            asset = import_custom_asset(temp_path)
            self._refresh_assets_settings_list()
            self.show_overlay_notification(t('settings_assets_upload_success', name=str(asset.get('name') or '')), timeout_ms=2600)
        except (FileNotFoundError, OSError, ValueError) as error:
            LOG.warning('Failed to import custom asset: %s', error)
            self.show_overlay_notification(t('settings_assets_upload_failed'), timeout_ms=3200)
        finally:
            if temp_path is not None and (not file_obj or not file_obj.get_path() or str(temp_path) != file_obj.get_path()):
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _confirm_delete_custom_asset(self, anchor, asset_id):
        asset = next((item for item in list_custom_assets() if item.get('id') == asset_id), None)
        if asset is None:
            return
        count = count_asset_references(self.db, asset_id)
        message = t('settings_assets_delete_confirm', name=str(asset.get('name') or ''), count=count)
        self._present_choice_dialog(message, lambda confirmed: self._delete_custom_asset(asset_id) if confirmed else None, destructive=True)

    def _delete_custom_asset(self, asset_id):
        affected_entry_ids = detach_asset_from_entries(self.db, asset_id)
        removed = remove_custom_asset(asset_id)
        if removed is None:
            return
        self._options_cache = {}
        for entry_id in affected_entry_ids:
            entry = self._entry_by_id(entry_id)
            if entry is None:
                row = self.db.cursor.execute('SELECT id, title, description, active FROM entries WHERE id=?', (entry_id,)).fetchone()
                if row is None:
                    continue
                entry = Entry(int(row[0]), str(row[1] or ''), str(row[2] or ''), bool(row[3]))
            options = self._get_options_dict(entry_id)
            if exportable_entry(entry, options):
                export_desktop_file(entry, options, ENGINES, LOG)
        self._refresh_assets_settings_list()
        self.show_overlay_notification(t('settings_assets_delete_success', name=str(removed.get('name') or '')), timeout_ms=2600)

    def _read_app_version_label(self):
        return APP_VERSION

    def on_ui_mode_changed(self, dropdown, _param):
        idx = int(dropdown.get_selected())
        if idx < 0 or idx >= len(self.ui_mode_values):
            return
        self.ui_settings['appearance'] = self.ui_mode_values[idx]
        self._save_ui_settings()
        self._apply_ui_appearance_setting()
        self.show_overlay_notification(t('settings_ui_changed', mode=self.ui_mode_labels[idx]), timeout_ms=2200)

    def on_language_changed(self, dropdown, _param):
        idx = int(dropdown.get_selected())
        if idx < 0 or idx >= len(getattr(self, 'language_values', [])):
            return
        selected_value = self.language_values[idx]
        if selected_value == (self.language_setting or 'system'):
            return
        try:
            config = dict(get_app_config(force_reload=True) or {})
            config['language'] = selected_value
            save_app_config(config)
            invalidate_i18n_cache(reload_config=True)
            self.language_setting = self._load_language_setting()
            self._refresh_translated_ui()
            language_label = self._available_language_rows()[idx][1]
            self.show_overlay_notification(t('settings_language_changed', language=language_label), timeout_ms=2200)
        except (OSError, TypeError, ValueError):
            LOG.error('Failed to save language setting', exc_info=True)

    def _build_export_payload_for_entry(self, entry):
        options = dict(self._get_options_dict(entry.id))
        icon_path = str(options.get(ICON_PATH_KEY, '') or '').strip()
        for transient_key in (ICON_PATH_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY):
            options.pop(transient_key, None)
        payload = {
            'format': 'webapp-export-v1',
            'title': entry.title or '',
            'description': entry.description or '',
            'active': bool(entry.active),
            'options': options,
            'icon': None,
        }
        validated_icon = validate_icon_source_path(icon_path) if icon_path else None
        if validated_icon is not None:
            icon_bytes = validated_icon.read_bytes()
            payload['icon'] = {
                'filename': validated_icon.name,
                'mime': 'image/png',
                'data_base64': base64.b64encode(icon_bytes).decode('ascii'),
            }
        return payload

    def _iter_exportable_entries(self):
        items = []
        for index in range(self.entries_store.get_n_items()):
            entry = self.entries_store.get_item(index)
            options = self._get_options_dict(entry.id)
            if exportable_entry(entry, options):
                items.append(entry)
        return items

    def _safe_export_name(self, entry):
        base = sanitize_desktop_value((entry.title or 'webapp').strip()) or f'webapp-{entry.id}'
        return f'{base}.wapp'

    def on_export_all_single_file_clicked(self, _button):
        entries = self._iter_exportable_entries()
        if not entries:
            self.show_overlay_notification(t('settings_export_none'), timeout_ms=2600)
            return
        if not hasattr(Gtk, 'FileDialog'):
            self.show_overlay_notification(t('settings_export_failed'), timeout_ms=3200)
            return
        dialog = Gtk.FileDialog(title=t('settings_export_dialog_title'), modal=True, initial_name='webapps_export.wapp')

        def handle_save(_dialog, result):
            try:
                file_obj = _dialog.save_finish(result)
            except GLib.Error:
                self._on_export_all_single_file_response(None, None, entries)
                return
            self._on_export_all_single_file_response(file_obj, None, entries)

        dialog.save(self, None, handle_save)

    def _build_export_bundle_payload(self, entries):
        return {
            'format': 'webapp-export-bundle-v1',
            'version': 1,
            'created_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z'),
            'entries': [self._build_export_payload_for_entry(entry) for entry in entries],
        }

    def _on_export_all_single_file_response(self, file_obj, response, entries):
        try:
            if file_obj is None:
                return
            if file_obj.get_path() is None:
                self.show_overlay_notification(t('settings_export_path_error'), timeout_ms=2600)
                return
            target = Path(file_obj.get_path())
            if target.suffix.lower() != '.wapp':
                target = target.with_suffix('.wapp')
            payload = self._build_export_bundle_payload(entries)
            target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')
            self.show_overlay_notification(t('settings_export_success', count=len(entries)), timeout_ms=2600)
        except (OSError, TypeError, ValueError) as error:
            LOG.error('Failed to export all WebApps into single file: %s', error, exc_info=True)
            self.show_overlay_notification(t('settings_export_failed'), timeout_ms=3200)

    def _set_titlebar_button_visibility(self, start_visible, end_visible):
        try:
            self.header_bar.set_show_start_title_buttons(bool(start_visible))
            self.header_bar.set_show_end_title_buttons(bool(end_visible))
        except (AttributeError, TypeError):
            LOG.debug('Failed to adjust titlebar button visibility', exc_info=True)

    def _show_back_only_header(self):
        self.search_button.set_visible(False)
        self.refresh_button.set_visible(False)
        self.settings_button.set_visible(False)
        self.add_button.set_visible(False)
        self.back_button.set_visible(True)
        self.header_bar.set_title_widget(None)
        self._set_titlebar_button_visibility(True, True)

    def _show_overview_header(self):
        self.search_button.set_visible(True)
        self.refresh_button.set_visible(True)
        self.settings_button.set_visible(True)
        self.add_button.set_visible(True)
        self.back_button.set_visible(False)
        self.header_bar.set_title_widget(self.list_title_widget)
        self._set_titlebar_button_visibility(True, True)

    def _restore_overview_header_actions(self):
        self._show_overview_header()


    def _launch_command_args(self, argv):
        if not argv:
            return False
        try:
            subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                cwd=str(Path.home()),
            )
            return True
        except OSError:
            LOG.error('Failed to launch command: %r', argv, exc_info=True)
            return False

    def show_settings_page(self, *args):
        current_child = self.stack.get_visible_child()
        if isinstance(current_child, DetailPage):
            return
        self._show_back_only_header()
        self.stack.set_visible_child_name('settings_page')

    def on_overview_logo_clicked(self, button):
        selection = self.selection.get_selected()
        if selection != Gtk.INVALID_LIST_POSITION:
            entry = self.filtered_model.get_item(selection)
            if entry is not None:
                self.launch_entry(entry)
                return
        if self.filtered_model.get_n_items() == 1:
            entry = self.filtered_model.get_item(0)
            if entry is not None:
                self.launch_entry(entry)

    def _launch_entry_from_icon(self, entry):
        try:
            self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
        except (AttributeError, TypeError):
            pass
        self.launch_entry(entry)

    def _resolve_desktop_path_for_entry(self, entry):
        entry_id = getattr(entry, 'id', None)
        title = sanitize_desktop_value(getattr(entry, 'title', ''), getattr(entry, 'title', '')).strip()
        for desktop_data in list_managed_desktop_files(ENGINES):
            if entry_id is not None and desktop_data.get('entry_id') == entry_id:
                path = desktop_data.get('path')
                if path is not None and path.exists():
                    return path
        if title:
            for desktop_data in list_managed_desktop_files(ENGINES):
                if (desktop_data.get('title') or '').strip() == title:
                    path = desktop_data.get('path')
                    if path is not None and path.exists():
                        return path
        desktop_path = get_expected_desktop_path(getattr(entry, 'title', ''))
        if desktop_path is not None and desktop_path.exists():
            return desktop_path
        return None

    def launch_entry(self, entry):
        desktop_path = self._resolve_desktop_path_for_entry(entry)
        if desktop_path is None or not desktop_path.exists():
            LOG.warning('Refusing to launch entry %s because its managed desktop file is missing', getattr(entry, 'id', 'unknown'))
            return
        options = self._get_options_dict(entry.id)
        launch_spec = build_launch_command(entry, options, ENGINES, LOG, prepare_profile=False)
        if launch_spec is None:
            LOG.warning('Refusing to launch entry %s because no validated launch command could be built', getattr(entry, 'id', 'unknown'))
            return
        self._launch_command_args(launch_spec['argv'])


    def _hide_busy(self):
        self.busy_spinner.stop()
        self.busy_overlay.set_visible(False)


    def _cancel_global_toast_timeout(self):
        if getattr(self, 'global_toast_timeout_id', 0):
            GLib.source_remove(self.global_toast_timeout_id)
            self.global_toast_timeout_id = 0

    def _hide_global_toast(self):
        self._cancel_global_toast_timeout()
        if hasattr(self, 'global_toast_revealer'):
            self.global_toast_revealer.set_reveal_child(False)
        return False

    def show_overlay_notification(self, message, timeout_ms=3000):
        text = (message or '').strip()
        if not text:
            self._hide_global_toast()
            return
        self._cancel_global_toast_timeout()
        self.global_toast_label.set_text(text)
        self.global_toast_revealer.set_reveal_child(True)
        self.global_toast_timeout_id = GLib.timeout_add(timeout_ms, self._hide_global_toast)

    def close_event(self, *args):
        self.db.close()
        Gtk.Window.close(self, *args)

    def on_search_clicked(self, button):
        self.search_visible = not self.search_visible
        self.search_entry.set_visible(self.search_visible)
        if self.search_visible:
            self.search_entry.grab_focus()
            return
        if self.search_entry.get_text():
            self.search_entry.set_text('')
        self.search_text = ''
        self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)
        self.update_empty_state()

    def on_refresh_clicked(self, button):
        if self._profile_resync_running:
            return
        message = t('profile_resync_confirm_body')
        self._present_choice_dialog(message, lambda accepted: self._start_profile_resync() if accepted else None, destructive=False)

    def on_search_entry_changed(self, entry):
        self.search_text = entry.get_text().strip().lower()
        self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)
        self.update_empty_state()

    def filter_entries(self, item):
        if not self.search_text:
            return True
        haystack = f'{item.title} {item.description}'.lower()
        return self.search_text in haystack

    def update_empty_state(self):
        self.empty_label.set_visible(self.filtered_model.get_n_items() == 0)

    def _destroy_profile_resync_dialog(self):
        dialog = self._profile_resync_dialog
        self._profile_resync_dialog = None
        self._profile_resync_progress_label = None
        self._profile_resync_progress_bar = None
        if dialog is None:
            return
        try:
            dialog.close()
        except (AttributeError, GLib.Error):
            try:
                dialog.destroy()
            except AttributeError:
                pass

    def _cancel_profile_resync(self, *_args):
        if self._profile_resync_cancel_event is not None:
            self._profile_resync_cancel_event.set()
        return False

    def _show_profile_resync_progress_dialog(self, total):
        self._destroy_profile_resync_dialog()
        dialog = Gtk.Window(transient_for=self, modal=True, title=t('profile_resync_title'))
        dialog.set_resizable(False)
        dialog.set_default_size(420, 1)
        dialog.connect('close-request', self._cancel_profile_resync)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)

        title = Gtk.Label(label=t('profile_resync_title'), xalign=0)
        title.add_css_class('title-4')
        title.set_wrap(True)
        body = Gtk.Label(label=t('profile_resync_progress_preparing'), xalign=0)
        body.set_wrap(True)
        body.add_css_class('dim-label')
        progress = Gtk.ProgressBar()
        progress.set_show_text(False)
        progress.set_fraction(0.0)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel_button = Gtk.Button(label=t('dialog_cancel'))
        cancel_button.connect('clicked', self._cancel_profile_resync)
        buttons.append(cancel_button)

        box.append(title)
        box.append(body)
        box.append(progress)
        box.append(buttons)
        dialog.set_child(box)
        dialog.present()

        self._profile_resync_dialog = dialog
        self._profile_resync_progress_label = body
        self._profile_resync_progress_bar = progress
        self._profile_resync_total = int(total or 0)

    def _update_profile_resync_progress(self, current, total, title=''):
        if self._profile_resync_progress_label is None or self._profile_resync_progress_bar is None:
            return False
        safe_total = max(int(total or 0), 1)
        safe_current = max(0, min(int(current or 0), safe_total))
        if title:
            text = t('profile_resync_progress_current', current=safe_current, total=safe_total, title=title)
        else:
            text = t('profile_resync_progress_completed', current=safe_current, total=safe_total)
        self._profile_resync_progress_label.set_text(text)
        self._profile_resync_progress_bar.set_fraction(safe_current / safe_total)
        return False

    def _collect_profile_resync_candidates(self):
        candidates = []
        for index in range(self.entries_store.get_n_items()):
            entry = self.entries_store.get_item(index)
            options = self._get_options_dict(entry.id)
            profile_path = str(options.get(PROFILE_PATH_KEY) or '').strip()
            family = self._browser_family_for_options(options)
            if not profile_path or family not in {'firefox', 'chrome', 'chromium'}:
                continue
            candidates.append((entry.id, entry.title or '', family, profile_path))
        return candidates

    def _start_profile_resync(self):
        if self._profile_resync_running:
            return
        candidates = self._collect_profile_resync_candidates()
        if not candidates:
            self.show_overlay_notification(t('profile_resync_none'), timeout_ms=2600)
            return
        self._profile_resync_running = True
        self._profile_resync_cancel_event = threading.Event()
        self.refresh_button.set_sensitive(False)
        self._show_profile_resync_progress_dialog(len(candidates))
        GLib.idle_add(self._update_profile_resync_progress, 0, len(candidates), '')

        def worker(items):
            db = Database(str(APP_DB_PATH))
            processed = 0
            cancelled = False
            failures = 0
            try:
                for index, (entry_id, entry_title, family, profile_path) in enumerate(items, start=1):
                    if self._profile_resync_cancel_event.is_set():
                        cancelled = True
                        break
                    GLib.idle_add(self._update_profile_resync_progress, index, len(items), entry_title)
                    try:
                        raw_state = read_profile_settings(profile_path, family)
                        normalized_state = normalize_option_dict(raw_state)
                        updates = {key: value for key, value in normalized_state.items() if key in browser_managed_option_keys()}
                        if updates:
                            existing = normalize_option_rows(db.get_options_for_entry(entry_id))
                            merged = dict(existing)
                            merged.update(updates)
                            updates[browser_state_key(family)] = encode_browser_state(merged, family)
                            db.add_options(entry_id, updates)
                    except (OSError, TypeError, ValueError) as error:
                        failures += 1
                        LOG.warning('Profile resync failed for entry %s (%s): %s', entry_id, profile_path, error)
                    processed = index
                    GLib.idle_add(self._update_profile_resync_progress, processed, len(items), '')
                    if self._profile_resync_cancel_event.is_set():
                        cancelled = True
                        break
            finally:
                try:
                    db.close()
                except OSError:
                    pass

            def finish():
                self._profile_resync_running = False
                self.refresh_button.set_sensitive(True)
                self._destroy_profile_resync_dialog()
                self.load_entries_from_db()
                self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)
                self.update_empty_state()
                for page in list(self.detail_pages.values()):
                    try:
                        page.reload_from_db()
                    except (AttributeError, TypeError):
                        pass
                if cancelled:
                    self.show_overlay_notification(t('profile_resync_cancelled', completed=processed, total=len(items)), timeout_ms=3200)
                elif failures:
                    self.show_overlay_notification(t('profile_resync_completed_with_failures', completed=processed, total=len(items), failures=failures), timeout_ms=3600)
                else:
                    self.show_overlay_notification(t('profile_resync_completed_success', completed=processed, total=len(items)), timeout_ms=2800)
                self._profile_resync_cancel_event = None
                return False

            GLib.idle_add(finish)

        threading.Thread(target=worker, args=(candidates,), daemon=True).start()

    def load_entries_from_db(self):
        self.entries_store.remove_all()
        self._options_cache = {}
        self._profile_size_cache = {}
        self._profile_size_pending = set()
        self.db.cursor.execute('SELECT * FROM entries ORDER BY title COLLATE NOCASE ASC')
        entry_rows = self.db.cursor.fetchall()
        for row in entry_rows:
            self.entries_store.append(Entry(row[0], row[1], row[2], bool(row[3])))
        self.db.cursor.execute('SELECT entry_id, option_key, option_value FROM options')
        for entry_id, option_key, option_value in self.db.cursor.fetchall():
            self._options_cache.setdefault(entry_id, {})[option_key] = option_value

    def _cleanup_detail_pages(self, pages):
        for child in pages:
            try:
                if child.get_parent() is self.stack:
                    self.stack.remove(child)
            except (AttributeError, TypeError):
                pass
            try:
                child.set_visible(False)
            except (AttributeError, TypeError):
                pass
        return False

    def _reload_entries(self):
        pages = list(self.detail_pages.values())
        if pages:
            try:
                self.stack.set_visible_child_name('list_page')
            except (AttributeError, TypeError, GLib.Error):
                pass
            GLib.idle_add(self._cleanup_detail_pages, pages)
        self.detail_pages = {}
        self._creating_entry = False
        self.load_entries_from_db()
        self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)
        self.update_empty_state()

    def _find_entry_by_id(self, entry_id):
        for index in range(self.entries_store.get_n_items()):
            entry = self.entries_store.get_item(index)
            if entry.id == entry_id:
                return entry
        return None

    def _find_entry_by_title(self, title):
        matches = []
        for index in range(self.entries_store.get_n_items()):
            entry = self.entries_store.get_item(index)
            if entry.title == title:
                matches.append(entry)
        return matches

    def _normalized_compare_text(self, value):
        return sanitize_desktop_value(value).strip().casefold()

    def _find_import_collision(self, payload):
        options = payload.get('options', {}) if isinstance(payload, dict) else {}
        if not isinstance(options, dict):
            options = {}
        target_title = self._normalized_compare_text(payload.get('title', ''))
        target_address = self._normalized_compare_text(options.get(ADDRESS_KEY, ''))
        target_engine = self._normalized_compare_text(options.get('EngineID', ''))
        if not target_title and not target_address:
            return None

        best_match = None
        for index in range(self.entries_store.get_n_items()):
            entry = self.entries_store.get_item(index)
            existing_options = self._get_options_dict(entry.id)
            existing_title = self._normalized_compare_text(entry.title)
            existing_address = self._normalized_compare_text(existing_options.get(ADDRESS_KEY, ''))
            existing_engine = self._normalized_compare_text(existing_options.get('EngineID', ''))

            exact_title_and_address = bool(target_title and target_address and existing_title == target_title and existing_address == target_address)
            same_address_and_engine = bool(target_address and existing_address == target_address and target_engine and existing_engine == target_engine)
            same_title_and_engine = bool(target_title and existing_title == target_title and target_engine and existing_engine == target_engine)

            if exact_title_and_address:
                return entry
            if best_match is None and (same_address_and_engine or same_title_and_engine):
                best_match = entry
        return best_match

    def _show_import_collision(self, entry, payload):
        try:
            self.on_entry_activated(entry, show_busy=False)
        except (AttributeError, TypeError):
            pass

        detail_page = self.detail_pages.get(entry.id)
        title = sanitize_desktop_value(payload.get('title', ''), entry.title).strip() or entry.title
        message = t('import_duplicate_detected', title=title)
        if detail_page is not None:
            detail_page._set_detail_action_status(message)
        self._present_info_dialog(message)
        LOG.info('Blocked duplicate .wapp import for entry %s (%s)', entry.id, title)

    def _invalidate_entry_cache(self, entry_id, clear_profile_size=False):
        self._options_cache.pop(entry_id, None)
        if clear_profile_size:
            self._profile_size_cache.pop(entry_id, None)
            self._profile_size_pending.discard(entry_id)

    def _cache_options(self, entry_id, updates):
        cached = dict(self._get_options_dict(entry_id))
        cached.update({key: '' if value is None else str(value) for key, value in updates.items()})
        self._options_cache[entry_id] = cached
        if any(key in updates for key in (PROFILE_PATH_KEY, PROFILE_NAME_KEY, ICON_PATH_KEY, 'EngineName')):
            self._profile_size_cache.pop(entry_id, None)
            self._profile_size_pending.discard(entry_id)

    def _add_options(self, entry_id, updates):
        clean_updates = {key: '' if value is None else str(value) for key, value in updates.items()}
        if not clean_updates:
            return
        self.db.add_options(entry_id, clean_updates)
        self._cache_options(entry_id, clean_updates)

    def _iter_icon_candidates(self, candidate, base_dir=None):
        raw = str(candidate or '').strip()
        if not raw:
            return []
        candidate_path = Path(raw).expanduser()
        suffix = candidate_path.suffix.lower()
        basenames = [candidate_path.name] if suffix else [f'{candidate_path.name}.svg', f'{candidate_path.name}.png', f'{candidate_path.name}.ico', f'{candidate_path.name}.xpm']
        search_dirs = []
        if candidate_path.parent != Path('.'):
            search_dirs.append(candidate_path.parent)
        if base_dir:
            base_path = Path(base_dir).expanduser()
            if base_path.is_file():
                base_path = base_path.parent
            search_dirs.extend([
                base_path,
                base_path / 'icons',
                base_path / 'pixmaps',
            ])
        found = []
        seen = set()
        direct_candidates = [candidate_path]
        if not suffix:
            direct_candidates.extend(candidate_path.with_suffix(ext) for ext in ('.svg', '.png', '.ico', '.xpm'))
        for direct in direct_candidates:
            try:
                resolved = direct.resolve()
            except (OSError, RuntimeError):
                resolved = direct
            if resolved in seen:
                continue
            seen.add(resolved)
            if direct.exists() and direct.is_file():
                found.append(direct)
        for root in search_dirs:
            try:
                root = root.resolve()
            except (OSError, RuntimeError):
                root = Path(root)
            if not root.exists() or not root.is_dir():
                continue
            for basename in basenames:
                candidate = root / basename
                try:
                    resolved = candidate.resolve()
                except (OSError, RuntimeError):
                    resolved = candidate
                if resolved in seen:
                    continue
                seen.add(resolved)
                if candidate.exists() and candidate.is_file():
                    found.append(candidate)
        return found

    def _lookup_system_icon_file(self, icon_name, base_dir=None):
        candidate = str(icon_name or '').strip()
        if not candidate:
            return None
        local_found = self._iter_icon_candidates(candidate, base_dir=base_dir)
        if local_found:
            return local_found[0]
        name = Path(candidate).name
        stem = Path(name).stem if Path(name).suffix else name
        explicit_suffix = Path(name).suffix.lower()
        icon_dirs = [
            Path.home() / '.local/share/icons',
            Path.home() / '.icons',
            Path('/usr/local/share/icons'),
            Path('/usr/share/icons'),
            Path('/usr/share/pixmaps'),
        ]
        found = []
        for root in icon_dirs:
            if not root.exists():
                continue
            patterns = [name] if explicit_suffix else [f'{stem}.svg', f'{stem}.png', f'{stem}.ico', f'{stem}.xpm']
            for pattern in patterns:
                try:
                    found.extend(path for path in root.rglob(pattern) if path.is_file())
                except OSError:
                    continue
        if not found:
            return None

        def score(path):
            suffix = path.suffix.lower()
            suffix_score = {'.svg': 0, '.png': 1, '.ico': 2, '.xpm': 3}.get(suffix, 9)
            size_score = 9999
            for part in path.parts:
                if 'x' in part:
                    try:
                        size_score = -int(part.split('x', 1)[0])
                        break
                    except (AttributeError, TypeError):
                        pass
            return (suffix_score, size_score, len(path.parts), len(str(path)))

        return sorted(found, key=score)[0]

    def _resolve_import_icon_reference(self, file_data, title, entry_id):
        desktop_path = file_data.get('path')
        desktop_dir = Path(desktop_path).parent if desktop_path else None

        def _copy_icon_candidate(icon_candidate):
            managed_target = get_managed_icon_path(title, '.png', entry_id)
            try:
                normalize_icon_to_png(icon_candidate, managed_target)
                return str(managed_target)
            except (OSError, ValueError):
                try:
                    suffix = icon_candidate.suffix or '.png'
                    fallback_target = get_managed_icon_path(title, suffix, entry_id)
                    fallback_target.write_bytes(icon_candidate.read_bytes())
                    return str(fallback_target)
                except OSError:
                    return ''

        icon_ref = str(file_data.get('icon_path') or '').strip()
        if icon_ref:
            icon_candidates = self._iter_icon_candidates(icon_ref, base_dir=desktop_dir)
            for icon_candidate in icon_candidates:
                copied = _copy_icon_candidate(icon_candidate)
                if copied:
                    return copied

        icon_name = str(file_data.get('icon_name') or '').strip()
        if icon_name:
            resolved = self._lookup_system_icon_file(icon_name, base_dir=desktop_dir)
            if resolved is not None:
                copied = _copy_icon_candidate(resolved)
                if copied:
                    return copied

        title_candidates = []
        safe_title = build_safe_slug(title)
        raw_title = str(title or '').strip()
        if raw_title:
            title_candidates.append(raw_title)
        if safe_title and safe_title not in title_candidates:
            title_candidates.append(safe_title)
        if desktop_path:
            desktop_stem = Path(desktop_path).stem
            if desktop_stem and desktop_stem not in title_candidates:
                title_candidates.append(desktop_stem)
        for candidate_name in title_candidates:
            resolved = self._lookup_system_icon_file(candidate_name, base_dir=desktop_dir)
            if resolved is None:
                continue
            copied = _copy_icon_candidate(resolved)
            if copied:
                return copied

        return ''

    def _get_profile_size_text_cached(self, entry_id, profile_path):
        cached = self._profile_size_cache.get(entry_id)
        if cached and cached.get('path') == profile_path:
            return cached.get('text', '')
        return ''

    def _schedule_profile_size_refresh(self, entry_id, profile_path, profile_size_label):
        if not profile_path:
            self._profile_size_cache[entry_id] = {'path': '', 'text': ''}
            profile_size_label.set_text('')
            profile_size_label.set_visible(False)
            return
        if entry_id in self._profile_size_pending:
            return
        self._profile_size_pending.add(entry_id)

        def _compute():
            try:
                size_text = format_profile_size(profile_path)
            except OSError:
                size_text = ''
            self._profile_size_cache[entry_id] = {'path': profile_path, 'text': size_text}
            self._profile_size_pending.discard(entry_id)
            current_entry = getattr(profile_size_label, '_entry_id', None)
            current_path = getattr(profile_size_label, '_profile_path', '')
            if current_entry == entry_id and current_path == profile_path:
                profile_size_label.set_text(size_text)
                profile_size_label.set_visible(bool(size_text))
            return False

        GLib.idle_add(_compute, priority=GLib.PRIORITY_LOW)

    def _get_options_dict(self, entry_id):
        cached = self._options_cache.get(entry_id)
        if cached is not None:
            return dict(cached)
        loaded = normalize_option_rows(self.db.get_options_for_entry(entry_id))
        self._options_cache[entry_id] = dict(loaded)
        return loaded

    def _entry_by_id(self, entry_id):
        for index in range(self.filtered_model.get_n_items()):
            candidate = self.filtered_model.get_item(index)
            if candidate is not None and int(getattr(candidate, 'id', -1)) == int(entry_id):
                return candidate
        for index in range(self.entries_store.get_n_items()):
            candidate = self.entries_store.get_item(index)
            if candidate is not None and int(getattr(candidate, 'id', -1)) == int(entry_id):
                return candidate
        return None

    def _profile_display_name(self, options):
        profile_path = (options.get(PROFILE_PATH_KEY) or '').strip()
        if profile_path:
            return Path(profile_path).name
        return (options.get(PROFILE_NAME_KEY) or '').strip()

    def _build_detail_header(self, entry):
        title = Gtk.Label(xalign=0.5)
        title.set_text(t('app_title'))
        title.add_css_class('title-4')
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(40)
        return title

    def _normalized_option_state(self, values, fallback=None):
        normalized = {}
        fallback = fallback or {}
        for key in MANAGED_IMPORT_OPTION_KEYS:
            value = values.get(key)
            if value in (None, ''):
                value = fallback.get(key)
            if key == COLOR_SCHEME_KEY:
                normalized[key] = (value or 'auto')
            else:
                normalized[key] = '1' if str(value) == '1' else '0'
        return normalized

    def _engine_for_options(self, options):
        try:
            target_id = int((options or {}).get('EngineID') or 0)
        except (TypeError, ValueError):
            target_id = 0
        if target_id:
            for engine in ENGINES:
                try:
                    if int(engine.get('id', -1)) == target_id:
                        return engine
                except (TypeError, ValueError):
                    continue
        target_name = str((options or {}).get('EngineName') or '').strip().lower()
        if target_name:
            for engine in ENGINES:
                if str(engine.get('name') or '').strip().lower() == target_name:
                    return engine
        return None

    def _browser_family_for_options(self, options):
        engine = self._engine_for_options(options or {})
        if engine is None:
            return 'generic'
        return browser_family_for_command(engine.get('command') or '')

    def _profile_sync_updates_for_entry(self, entry_id, profile_path, family):
        profile_path = str(profile_path or '').strip()
        family = (family or 'generic').strip().lower()
        if not profile_path or family == 'generic':
            return {}
        try:
            raw_state = read_profile_settings(profile_path, family)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            LOG.warning('Failed to read profile settings for entry %s from %s: %s', entry_id, profile_path, error)
            return {}
        normalized_state = normalize_option_dict(raw_state)
        updates = {key: value for key, value in normalized_state.items() if key in browser_managed_option_keys()}
        if not updates:
            return {}
        existing = self._get_options_dict(entry_id)
        merged = dict(existing)
        merged.update(updates)
        updates[browser_state_key(family)] = encode_browser_state(merged, family)
        return updates

    def _reset_imported_option_state(self, entry_id):
        reset_values = {
            ADDRESS_KEY: '',
            ICON_PATH_KEY: '',
            'EngineID': '',
            'EngineName': '',
            USER_AGENT_NAME_KEY: '',
            USER_AGENT_VALUE_KEY: '',
            PROFILE_NAME_KEY: '',
            PROFILE_PATH_KEY: '',
            COLOR_SCHEME_KEY: 'auto',
        }
        for key in MANAGED_IMPORT_OPTION_KEYS:
            reset_values.setdefault(key, '0')
        self._add_options(entry_id, reset_values)

    def _collect_active_profile_paths(self):
        active_paths = []
        for index in range(self.entries_store.get_n_items()):
            entry = self.entries_store.get_item(index)
            options = self._get_options_dict(entry.id)
            profile_path = str(options.get(PROFILE_PATH_KEY) or '').strip()
            if profile_path:
                active_paths.append(profile_path)
        return active_paths

    def _run_startup_profile_cleanup(self):
        if self._startup_profile_cleanup_done:
            return
        self._startup_profile_cleanup_done = True
        rename_unused_managed_profile_directories(self._collect_active_profile_paths(), LOG)

    def _finalize_startup_reconcile(self):
        self._reload_entries()
        self._run_startup_profile_cleanup()

    def _upsert_entry_from_file(self, file_data, existing_entry=None):
        title = (file_data.get('title') or '').strip()
        active = 1 if file_data.get('active', True) else 0
        if existing_entry is None:
            entry_id = self.db.add_entry(title, '')
            if entry_id is None:
                return
            entry_obj = self._find_entry_by_id(entry_id)
            if entry_obj is None:
                entry_obj = Entry(entry_id, title, '', bool(active))
                self.entries_store.append(entry_obj)
        else:
            entry_id = existing_entry.id
            entry_obj = existing_entry
            self.db.cursor.execute('UPDATE entries SET title=?, active=? WHERE id=?', (title, active, entry_id))
            self.db.conn.commit()

        option_updates = {}
        if file_data.get('address'):
            option_updates[ADDRESS_KEY] = file_data['address']
        icon_ref = self._resolve_import_icon_reference(file_data, title, entry_id)
        if icon_ref:
            option_updates[ICON_PATH_KEY] = icon_ref
        if file_data.get('engine_id') is not None:
            option_updates['EngineID'] = str(file_data['engine_id'])
        if file_data.get('engine_name'):
            option_updates['EngineName'] = file_data['engine_name']
        elif file_data.get('engine_id'):
            for engine in ENGINES:
                if engine['id'] == file_data['engine_id']:
                    option_updates['EngineName'] = engine['name']
                    break
        if file_data.get('user_agent_name') is not None:
            option_updates[USER_AGENT_NAME_KEY] = file_data.get('user_agent_name', '')
        if file_data.get('user_agent_value') is not None:
            option_updates[USER_AGENT_VALUE_KEY] = file_data.get('user_agent_value', '')
        profile_family = self._browser_family_for_options({
            'EngineID': option_updates.get('EngineID', ''),
            'EngineName': option_updates.get('EngineName', ''),
        })
        if file_data.get('profile_path') and profile_family in {'firefox', 'chrome', 'chromium'}:
            profile_source = inspect_profile_copy_source(file_data['profile_path'], profile_family, LOG)
            if profile_source.get('valid'):
                option_updates[PROFILE_PATH_KEY] = profile_source['profile_path']
                option_updates[PROFILE_NAME_KEY] = profile_source['profile_name']
        elif file_data.get('profile_name'):
            option_updates[PROFILE_NAME_KEY] = file_data['profile_name']
        for key in ('Kiosk', APP_MODE_KEY, 'Frameless'):
            value = (file_data.get('options') or {}).get(key)
            if value is not None:
                option_updates[key] = value
        self._add_options(entry_id, option_updates)
        self.db.cursor.execute('UPDATE entries SET title=?, active=? WHERE id=?', (title, active, entry_id))
        self.db.conn.commit()
        entry_obj.title = title
        entry_obj.active = bool(active)

        result = export_desktop_file(entry_obj, self._get_options_dict(entry_id), ENGINES, LOG)
        if result:
            self._add_options(entry_id, {
                PROFILE_NAME_KEY: result.get('profile_name', '') or '',
                PROFILE_PATH_KEY: result.get('profile_path', '') or '',
            })

        current_options = self._get_options_dict(entry_id)
        profile_path = (current_options.get(PROFILE_PATH_KEY) or '').strip()
        profile_family = self._browser_family_for_options(current_options)
        profile_updates = self._profile_sync_updates_for_entry(entry_id, profile_path, profile_family)
        if profile_updates:
            self._add_options(entry_id, profile_updates)

        self.refresh_entry_visual(entry_obj)
        if entry_id in self.detail_pages:
            try:
                self.detail_pages[entry_id].reload_from_db()
            except (AttributeError, GLib.Error):
                pass

    def _compare_db_and_file(self, entry, file_data):
        options = self._get_options_dict(entry.id)
        db_state = WebAppState.from_entry_and_options(entry, options)
        file_state = WebAppState.from_file_data(file_data, fallback=db_state)
        db_values = {
            'title': db_state.title,
            'address': db_state.address,
            'engine_id': db_state.engine_id,
            'active': db_state.active,
            'icon_path': bool(db_state.icon_path),
        }
        file_values = {
            'title': file_state.title,
            'address': file_state.address,
            'engine_id': file_state.engine_id,
            'active': file_state.active,
            'icon_path': bool(file_state.icon_path),
        }
        return db_values != file_values, db_values, file_values

    def reconcile_desktop_files(self):
        managed_files = list_managed_desktop_files(ENGINES)
        matched_ids = set()
        conflicts = []
        imports = []

        for file_data in managed_files:
            entry = None
            file_entry_id = file_data.get('entry_id')
            had_explicit_entry_id = file_entry_id not in (None, '')
            if had_explicit_entry_id:
                entry = self._find_entry_by_id(file_entry_id)
            if entry is None and (not had_explicit_entry_id) and file_data.get('title'):
                matches = self._find_entry_by_title(file_data['title'])
                if len(matches) == 1:
                    entry = matches[0]
            if entry is None:
                if had_explicit_entry_id:
                    imports.append(file_data)
                    continue
                conflicts.append({'type': 'orphan_file', 'file': file_data})
                continue
            matched_ids.add(entry.id)
            is_mismatch, db_values, file_values = self._compare_db_and_file(entry, file_data)
            if is_mismatch:
                conflicts.append({'type': 'mismatch', 'entry': entry, 'file': file_data, 'db': db_values, 'file_values': file_values})

        for index in range(self.entries_store.get_n_items()):
            entry = self.entries_store.get_item(index)
            if entry.id in matched_ids:
                continue
            options = self._get_options_dict(entry.id)
            if not exportable_entry(entry, options):
                continue
            expected_path = get_expected_desktop_path(entry.title)
            if expected_path is None or not expected_path.exists():
                conflicts.append({'type': 'missing_file', 'entry': entry})

        self.reconcile_queue = conflicts
        if imports:
            self._prompt_detected_desktop_imports(imports)
        else:
            self._show_next_conflict()
        return False

    def _finish_detected_desktop_imports(self, imported_count, total, cancelled=False):
        self._destroy_import_progress_dialog()
        self._import_cancel_requested = False
        self._reload_entries()
        if cancelled:
            self.show_overlay_notification(t('desktop_detected_import_cancelled', imported=imported_count, total=total), timeout_ms=3200)
        elif imported_count:
            self.show_overlay_notification(t('desktop_detected_import_done', imported=imported_count, total=total), timeout_ms=2800)
        self._show_next_conflict()
        return False

    def _prompt_detected_desktop_imports(self, file_datas):
        items = list(file_datas or [])
        if not items:
            self._show_next_conflict()
            return
        total = len(items)
        message = t('desktop_detected_import_prompt', total=total)

        def handle_import_choice(accepted):
            if accepted:
                self._start_detected_desktop_imports(items)
                return
            self._reload_entries()
            self._show_next_conflict()

        self._present_choice_dialog(message, handle_import_choice, destructive=False)

    def _start_detected_desktop_imports(self, file_datas):
        items = list(file_datas or [])
        if not items:
            self._show_next_conflict()
            return
        total = len(items)
        imported_count = 0
        state = {'index': 0}
        self._import_cancel_requested = False
        self._show_import_progress_dialog(total, title_text=t('desktop_detected_import_title'), preparing_text=t('desktop_detected_import_found', total=total))
        GLib.idle_add(self._update_import_progress, 0, total, '')

        def process_next():
            nonlocal imported_count
            if self._import_cancel_requested:
                GLib.idle_add(self._finish_detected_desktop_imports, imported_count, total, True)
                return False
            if state['index'] >= total:
                GLib.idle_add(self._finish_detected_desktop_imports, imported_count, total, False)
                return False
            file_data = items[state['index']]
            state['index'] += 1
            title = str(file_data.get('title') or file_data.get('path') or '').strip()
            GLib.idle_add(self._update_import_progress, state['index'], total, title)
            try:
                self._upsert_entry_from_file(file_data)
                imported_count += 1
            except (OSError, ValueError, json.JSONDecodeError) as error:
                LOG.warning('Failed to import managed desktop file %s: %s', file_data.get('path'), error)
            self._reload_entries()
            GLib.idle_add(self._update_import_progress, state['index'], total, '')
            GLib.idle_add(process_next)
            return False

        GLib.idle_add(process_next)

    def _show_next_conflict(self):
        if not self.reconcile_queue:
            self._finalize_startup_reconcile()
            return
        conflict = self.reconcile_queue.pop(0)
        if conflict['type'] == 'orphan_file':
            text = t('reconcile_orphan_file', path=str(conflict['file']['path']), title=conflict['file'].get('title', ''))
            self._present_yes_no_dialog(text, lambda use_file: self._handle_orphan_file(conflict, use_file))
            return
        if conflict['type'] == 'missing_file':
            text = t('reconcile_missing_file', title=conflict['entry'].title)
            self._present_yes_no_dialog(text, lambda recreate: self._handle_missing_file(conflict, recreate))
            return
        if conflict['type'] == 'mismatch':
            text = t('reconcile_mismatch', db_title=conflict['db']['title'], file_title=conflict['file_values']['title'], db_address=conflict['db']['address'], file_address=conflict['file_values']['address'])
            self._present_yes_no_dialog(text, lambda use_file: self._handle_mismatch(conflict, use_file))
            return

    def _present_yes_no_dialog(self, text, callback):
        self._present_choice_dialog(
            text,
            lambda accepted: (callback(accepted), self._show_next_conflict()),
            destructive=False,
        )

    def _present_info_dialog(self, message):
        if hasattr(Adw, 'AlertDialog'):
            dialog = Adw.AlertDialog()
            dialog.set_heading(t('app_title'))
            dialog.set_body(message)
            dialog.add_response('close', t('dialog_close'))
            dialog.set_default_response('close')
            dialog.set_close_response('close')
            dialog.present(self)
            return

        dialog = Adw.MessageDialog.new(self, t('app_title'), message)
        dialog.add_response('close', t('dialog_close'))
        dialog.set_default_response('close')
        dialog.set_close_response('close')
        dialog.present()

    def _handle_orphan_file(self, conflict, use_file):
        if use_file:
            self._upsert_entry_from_file(conflict['file'])

    def _handle_missing_file(self, conflict, recreate):
        if recreate:
            export_desktop_file(conflict['entry'], self._get_options_dict(conflict['entry'].id), ENGINES, LOG)

    def _handle_mismatch(self, conflict, use_file):
        if use_file:
            self._upsert_entry_from_file(conflict['file'], existing_entry=conflict['entry'])
            return
        export_desktop_file(conflict['entry'], self._get_options_dict(conflict['entry'].id), ENGINES, LOG)

    def _present_choice_dialog(self, message, on_result, destructive=False):
        handled = {'done': False}

        def respond(value):
            if handled['done']:
                return
            handled['done'] = True
            on_result(value)

        if hasattr(Adw, 'AlertDialog'):
            dialog = Adw.AlertDialog()
            dialog.set_heading(t('app_title'))
            dialog.set_body(message)
            dialog.add_response('no', t('dialog_no'))
            dialog.add_response('yes', t('dialog_yes'))
            dialog.set_default_response('yes')
            dialog.set_close_response('no')
            if destructive:
                dialog.set_response_appearance('yes', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.connect('response', lambda _d, response: respond(response == 'yes'))
            dialog.present(self)
            return

        dialog = Adw.MessageDialog.new(self, t('app_title'), message)
        dialog.add_response('no', t('dialog_no'))
        dialog.add_response('yes', t('dialog_yes'))
        dialog.set_default_response('yes')
        dialog.set_close_response('no')
        if destructive:
            dialog.set_response_appearance('yes', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect('response', lambda _d, response: respond(response == 'yes'))
        dialog.present()

    def on_factory_setup(self, factory, list_item):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.add_css_class('entry-card')
        box.set_margin_top(0)
        box.set_margin_bottom(0)
        box.set_margin_start(0)
        box.set_margin_end(0)
        box.set_halign(Gtk.Align.FILL)
        box.set_valign(Gtk.Align.START)

        icon_frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        icon_frame.add_css_class('overview-icon-frame')
        icon_frame.set_halign(Gtk.Align.START)
        icon_frame.set_valign(Gtk.Align.START)
        icon_frame.append(create_image_from_ref('', pixel_size=28, fallback_icon='applications-internet'))

        icon_button = Gtk.Button()
        icon_button.add_css_class('flat')
        icon_button.add_css_class('overview-icon-button')
        icon_button.set_focus_on_click(False)
        icon_button.set_can_focus(False)
        icon_button.set_tooltip_text(t('launch_webapp'))
        icon_button.set_child(icon_frame)

        icon_click_gesture = Gtk.GestureClick()
        icon_click_gesture.set_button(Gdk.BUTTON_PRIMARY)
        icon_click_gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        icon_click_gesture.connect('pressed', self._on_overview_icon_pressed)
        icon_button.add_controller(icon_click_gesture)

        status_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        status_column.set_halign(Gtk.Align.END)
        status_column.set_valign(Gtk.Align.START)
        status_column.set_hexpand(False)
        status_column.set_vexpand(False)

        active_dot = Gtk.Box()
        active_dot.add_css_class('overview-active-dot')
        active_dot.set_size_request(10, 10)
        active_dot.set_halign(Gtk.Align.END)
        active_dot.set_valign(Gtk.Align.START)
        active_dot.set_margin_bottom(2)

        engine_image = Gtk.Image.new_from_icon_name('applications-internet-symbolic')
        engine_image.set_pixel_size(18)
        engine_image.add_css_class('overview-engine-icon')
        engine_image.set_halign(Gtk.Align.END)
        engine_image.set_valign(Gtk.Align.START)
        engine_image.set_margin_top(2)
        engine_image.set_margin_bottom(2)

        profile_size_label = Gtk.Label(xalign=1.0, yalign=0.0)
        profile_size_label.add_css_class('profile-size-label')
        profile_size_label.set_halign(Gtk.Align.END)
        profile_size_label.set_valign(Gtk.Align.START)
        profile_size_label.set_ellipsize(Pango.EllipsizeMode.END)

        status_column.append(active_dot)
        status_column.append(engine_image)
        status_column.append(profile_size_label)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        text_box.set_hexpand(True)
        text_box.set_valign(Gtk.Align.START)

        title = Gtk.Label(xalign=0, yalign=0.0)
        title.add_css_class('title-4')
        title.add_css_class('entry-title')
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_hexpand(True)
        title.set_halign(Gtk.Align.START)
        title.set_valign(Gtk.Align.START)

        description = Gtk.Label(xalign=0, yalign=0.0)
        description.add_css_class('dim-label')
        description.add_css_class('entry-subtitle')
        description.set_wrap(True)
        description.set_wrap_mode(2)
        description.set_max_width_chars(80)
        description.set_valign(Gtk.Align.START)
        description.set_halign(Gtk.Align.START)
        description.set_hexpand(True)

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_row.set_hexpand(True)
        title_row.set_valign(Gtk.Align.START)
        title_row.append(title)

        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        status_box.set_halign(Gtk.Align.END)
        status_box.set_valign(Gtk.Align.START)
        status_box.set_hexpand(False)
        title_row.append(status_box)

        subtitle_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        subtitle_row.set_hexpand(True)
        subtitle_row.set_valign(Gtk.Align.START)
        subtitle_row.append(description)

        text_box.append(title_row)
        text_box.append(subtitle_row)
        box.append(icon_button)
        box.append(text_box)
        box.append(status_column)
        list_item.set_child(box)



    def _on_overview_icon_pressed(self, gesture, _n_press, _x, _y):
        try:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        except (AttributeError, TypeError):
            pass

    def _on_overview_icon_clicked(self, button):
        entry = getattr(button, '_bound_entry', None)
        if entry is None:
            return
        self._launch_entry_from_icon(entry)

    def _clear_overview_icon_button_handler(self, icon_button):
        button_handler = getattr(icon_button, '_click_handler_id', None)
        if button_handler is None:
            return
        try:
            icon_button.disconnect(button_handler)
        except (AttributeError, TypeError, GLib.Error):
            pass
        icon_button._click_handler_id = None

    def _bind_overview_icon_button(self, icon_button, entry):
        icon_button._bound_entry = entry
        self._clear_overview_icon_button_handler(icon_button)
        icon_button._click_handler_id = icon_button.connect('clicked', self._on_overview_icon_clicked)

    def on_list_view_activate(self, list_view, position):
        try:
            entry = self.filtered_model.get_item(position)
        except (AttributeError, TypeError):
            entry = None
        if entry is None:
            return
        self.on_entry_activated(entry)
        try:
            self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
        except (AttributeError, TypeError, GLib.Error):
            pass

    def on_factory_bind(self, factory, list_item):
        entry = list_item.get_item()
        box = list_item.get_child()
        icon_button = box.get_first_child()
        icon_frame = icon_button.get_child()
        text_box = icon_button.get_next_sibling()
        status_column = text_box.get_next_sibling()
        title_row = text_box.get_first_child()
        subtitle_row = title_row.get_next_sibling()
        title_label = title_row.get_first_child()
        status_box = title_label.get_next_sibling()
        description_label = subtitle_row.get_first_child()
        active_dot = status_column.get_first_child()
        engine_image = active_dot.get_next_sibling()
        profile_size_label = engine_image.get_next_sibling()
        handlers = getattr(list_item, '_entry_handlers', [])
        old_entry = getattr(list_item, '_bound_entry', None)
        if old_entry is not None:
            for handler_id in handlers:
                try:
                    old_entry.disconnect(handler_id)
                except (AttributeError, TypeError):
                    pass
        self._clear_overview_icon_button_handler(icon_button)
        list_item._bound_entry = entry
        self._bind_overview_icon_button(icon_button, entry)
        title_label.set_text(entry.title)
        description_label.set_text(entry.description)
        self._set_overview_icon(icon_frame, entry.id)
        self._set_status_indicators(status_box, entry.id, entry.active, engine_image, active_dot)
        self._set_profile_size_label(profile_size_label, entry.id)
        list_item._entry_handlers = [
            entry.connect('notify::title', lambda e, pspec: self._on_entry_changed(e, icon_frame, status_box, profile_size_label, title_label, description_label, engine_image, active_dot)),
            entry.connect('notify::description', lambda e, pspec: self._on_entry_changed(e, icon_frame, status_box, profile_size_label, title_label, description_label, engine_image, active_dot)),
            entry.connect('notify::active', lambda e, pspec: self._on_entry_changed(e, icon_frame, status_box, profile_size_label, title_label, description_label, engine_image, active_dot)),
        ]

    def _set_overview_icon(self, icon_frame, entry_id):
        old_icon = icon_frame.get_first_child()
        if old_icon is not None:
            icon_frame.remove(old_icon)
        icon_ref = self._get_options_dict(entry_id).get(ICON_PATH_KEY, '')
        if icon_ref:
            new_icon = create_image_from_ref(icon_ref, pixel_size=40, fallback_icon='applications-internet')
        else:
            new_icon = create_image_from_ref('', pixel_size=28, fallback_icon='applications-internet')
        icon_frame.prepend(new_icon)

    def _set_profile_size_label(self, profile_size_label, entry_id):
        if profile_size_label is None:
            return
        options = self._get_options_dict(entry_id)
        profile_path = options.get(PROFILE_PATH_KEY, '')
        profile_size_label._entry_id = entry_id
        profile_size_label._profile_path = profile_path
        size_text = self._get_profile_size_text_cached(entry_id, profile_path)
        profile_size_label.set_text(size_text)
        profile_size_label.set_visible(bool(size_text))
        self._schedule_profile_size_refresh(entry_id, profile_path, profile_size_label)

    def _set_status_indicators(self, status_box, entry_id, active=False, engine_widget=None, active_dot=None):
        child = status_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            status_box.remove(child)
            child = next_child
        options = self._get_options_dict(entry_id)
        engine_name = options.get('EngineName', '') or ''
        if engine_widget is not None:
            engine_widget.set_from_icon_name(engine_icon_name(engine_name) if engine_name else 'applications-internet-symbolic')
            engine_widget.set_visible(bool(engine_name))
        if active_dot is not None:
            active_dot.remove_css_class('active')
            active_dot.remove_css_class('inactive')
            active_dot.add_css_class('active' if active else 'inactive')
            active_dot.set_visible(True)


    def _on_entry_changed(self, entry, icon_frame, status_box, profile_size_label, title_label, description_label, engine_image=None, active_dot=None):
        title_label.set_text(entry.title)
        description_label.set_text(entry.description)
        self._set_overview_icon(icon_frame, entry.id)
        self._set_status_indicators(status_box, entry.id, entry.active, engine_image, active_dot)
        self._set_profile_size_label(profile_size_label, entry.id)
        self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)
        self.update_empty_state()

    def update_header_title(self, entry):
        if self.stack.get_visible_child() == self.detail_pages.get(entry.id):
            self.header_bar.set_title_widget(None)


    def refresh_entry_visual(self, entry):
        self._invalidate_entry_cache(entry.id, clear_profile_size=True)
        entry.notify('title')
        entry.notify('description')

    def on_entry_activated(self, entry, show_busy=True):
        if show_busy:
            self._show_busy(t('loading'))
        self.search_button.set_visible(False)
        self.refresh_button.set_visible(False)
        self.add_button.set_visible(False)
        self.settings_button.set_visible(False)
        self.stack.set_visible_child_name('detail_placeholder')

        def _open_detail():
            try:
                self._show_back_only_header()
                if entry.id not in self.detail_pages:
                    detail_page = DetailPage(
                        entry,
                        self.db,
                        on_back=self.show_list_page,
                        on_delete=self.confirm_delete,
                        on_title_changed=self.update_header_title,
                        on_visual_changed=self.refresh_entry_visual,
                        on_overlay_notification=self.show_overlay_notification,
                    )
                    self.detail_pages[entry.id] = detail_page
                    self.stack.add_named(detail_page, f'detail_{entry.id}')
                self.stack.set_visible_child(self.detail_pages[entry.id])
            except (GLib.Error, OSError, TypeError, ValueError) as error:
                LOG.error('Failed to open detail page for entry %s: %s', entry.id, error, exc_info=True)
                self.show_overlay_notification(t('detail_view_load_failed'), timeout_ms=3500)
                self.stack.set_visible_child_name('list_page')
            finally:
                if show_busy:
                    self._hide_busy()
            return False

        GLib.idle_add(_open_detail)

    def confirm_delete(self, entry):
        self.delete_entry(entry)

    def delete_entry(self, entry):
        options = self._get_options_dict(entry.id)
        delete_managed_entry_artifacts(
            entry.id,
            entry.title,
            ENGINES,
            LOG,
            delete_profiles=True,
            stored_profile_path=options.get(PROFILE_PATH_KEY, ''),
            stored_profile_name=options.get(PROFILE_NAME_KEY, ''),
        )
        self.db.cursor.execute('DELETE FROM options WHERE entry_id=?', (entry.id,))
        self.db.cursor.execute('DELETE FROM entries WHERE id=?', (entry.id,))
        self.db.conn.commit()
        index_to_remove = None
        for index in range(self.entries_store.get_n_items()):
            if self.entries_store.get_item(index).id == entry.id:
                index_to_remove = index
                break
        if index_to_remove is not None:
            self.entries_store.remove(index_to_remove)
        if entry.id in self.detail_pages:
            page = self.detail_pages[entry.id]
            if self.stack.get_visible_child() is page:
                try:
                    self.stack.set_visible_child_name('list_page')
                except (AttributeError, TypeError):
                    pass
            GLib.idle_add(self._cleanup_detail_pages, [page])
            del self.detail_pages[entry.id]
        self.update_empty_state()
        self.show_list_page()

    def _release_detail_page(self, page):
        if page is None:
            return
        try:
            page.release_resources()
        except (AttributeError, TypeError):
            LOG.debug('Detail page cleanup failed before release', exc_info=True)
        entry_id = getattr(getattr(page, 'entry', None), 'id', None)
        if entry_id in self.detail_pages and self.detail_pages.get(entry_id) is page:
            del self.detail_pages[entry_id]
        GLib.idle_add(self._cleanup_detail_pages, [page])

    def show_list_page(self, *args):
        current_child = self.stack.get_visible_child()
        current_name = None
        try:
            current_name = self.stack.get_visible_child_name()
        except (AttributeError, TypeError):
            current_name = None
        if isinstance(current_child, DetailPage) and current_child.is_subpage_visible():
            current_child.show_main_page()
            return
        if current_name == 'settings_assets_page':
            self._show_back_only_header()
            self.stack.set_visible_child_name('settings_page')
            return
        self._restore_overview_header_actions()
        if isinstance(current_child, DetailPage):
            self._release_detail_page(current_child)
        self._hide_global_toast()
        self.stack.set_visible_child_name('list_page')

    def on_add_entry(self, button):
        if self._creating_entry:
            return
        self._present_add_choice_dialog()

    def _present_add_choice_dialog(self):
        def handle_response(response_id):
            if response_id == 'new':
                self._create_empty_entry()
            elif response_id == 'import':
                self._open_import_wapp_dialog()

        if hasattr(Adw, 'AlertDialog'):
            dialog = Adw.AlertDialog.new(
                t('add_webapp_dialog_title'),
                t('add_webapp_dialog_body'),
            )
            dialog.add_response('cancel', t('dialog_cancel'))
            dialog.add_response('import', t('add_webapp_dialog_import_wapp'))
            dialog.add_response('new', t('add_webapp_dialog_manual'))
            dialog.set_default_response('new')
            dialog.set_close_response('cancel')
            dialog.set_response_appearance('cancel', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.connect('response', lambda _d, response: handle_response(response))
            dialog.present(self)
            return

        dialog = Adw.MessageDialog.new(self, t('add_webapp_dialog_title'), t('add_webapp_dialog_body'))
        dialog.add_response('cancel', t('dialog_cancel'))
        dialog.add_response('import', t('add_webapp_dialog_import_wapp'))
        dialog.add_response('new', t('add_webapp_dialog_manual'))
        dialog.set_default_response('new')
        dialog.set_close_response('cancel')
        dialog.set_response_appearance('cancel', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect('response', lambda _d, response: handle_response(response))
        dialog.present()

    def _create_empty_entry(self):
        self._creating_entry = True
        self.add_button.set_sensitive(False)
        try:
            new_id = self.db.add_entry('')
            if new_id is not None:
                entry = Entry(new_id, '')
                self.entries_store.append(entry)
                self.on_entry_activated(entry, show_busy=False)
            try:
                self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
            except (AttributeError, TypeError, GLib.Error):
                pass
        finally:
            self._creating_entry = False
            self.add_button.set_sensitive(True)

    def _open_import_wapp_dialog(self):
        patterns = [(t('wapp_filter_name'), '*.wapp')]
        if hasattr(Gtk, 'FileDialog'):
            dialog = Gtk.FileDialog(title=t('import_webapp_button'), modal=True)
            filters = Gio.ListStore.new(Gtk.FileFilter)
            first_filter = None
            for name, pattern in patterns:
                filt = Gtk.FileFilter()
                filt.set_name(name)
                filt.add_pattern(pattern)
                filters.append(filt)
                if first_filter is None:
                    first_filter = filt
            if filters.get_n_items() > 0:
                dialog.set_filters(filters)
            if first_filter is not None:
                dialog.set_default_filter(first_filter)

            def handle_open(_dialog, result):
                try:
                    file_obj = _dialog.open_finish(result)
                except GLib.Error:
                    self._on_import_wapp_dialog_response(None, Gtk.ResponseType.CANCEL)
                    return
                self._on_import_wapp_dialog_response(file_obj)

            dialog.open(self, None, handle_open)
            return

        dialog = Gtk.FileChooserNative.new(
            t('import_webapp_button'),
            self,
            Gtk.FileChooserAction.OPEN,
            t('icon_dialog_open'),
            t('icon_dialog_cancel'),
        )
        first_filter = None
        for name, pattern in patterns:
            filt = Gtk.FileFilter()
            filt.set_name(name)
            filt.add_pattern(pattern)
            dialog.add_filter(filt)
            if first_filter is None:
                first_filter = filt
        if first_filter is not None:
            dialog.set_filter(first_filter)
        dialog.connect('response', self._on_import_wapp_dialog_response)
        dialog.show()

    def _copy_gfile_to_temp_path(self, file_obj, suffix=''):
        if file_obj is None:
            return None
        local_path = file_obj.get_path()
        if local_path:
            return Path(local_path)
        tmp_name = None
        try:
            stream = file_obj.read(None)
            import tempfile
            fd, tmp_name = tempfile.mkstemp(suffix=suffix)
            try:
                with os.fdopen(fd, 'wb') as handle:
                    while True:
                        chunk = stream.read_bytes(65536, None)
                        if not chunk:
                            break
                        handle.write(chunk)
            finally:
                try:
                    stream.close(None)
                except (AttributeError, TypeError):
                    pass
            return Path(tmp_name)
        except (AttributeError, TypeError, OSError, GLib.Error) as error:
            if tmp_name:
                try:
                    Path(tmp_name).unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                uri = file_obj.get_uri()
            except (AttributeError, GLib.Error):
                uri = ''
            LOG.warning('Failed to copy selected file %s: %s', uri, error)
            return None

    def _destroy_import_progress_dialog(self):
        dialog = self._import_progress_dialog
        self._import_progress_dialog = None
        self._import_progress_label = None
        self._import_progress_bar = None
        if dialog is None:
            return
        try:
            dialog.close()
        except (AttributeError, GLib.Error):
            try:
                dialog.destroy()
            except AttributeError:
                pass

    def _cancel_import_progress(self, *_args):
        self._import_cancel_requested = True
        return False

    def _show_import_progress_dialog(self, total, title_text=None, preparing_text=None):
        self._destroy_import_progress_dialog()
        dialog_title = title_text or t('import_progress_title')
        body_text = preparing_text or t('import_progress_preparing')
        dialog = Gtk.Dialog(transient_for=self, modal=True)
        dialog.set_title(dialog_title)
        dialog.set_resizable(False)
        dialog.set_default_size(420, 1)
        dialog.connect('close-request', self._cancel_import_progress)
        box = dialog.get_content_area()
        box.set_spacing(12)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)

        title = Gtk.Label(label=dialog_title, xalign=0)
        title.add_css_class('title-4')
        title.set_wrap(True)
        body = Gtk.Label(label=body_text, xalign=0)
        body.set_wrap(True)
        body.add_css_class('dim-label')
        progress = Gtk.ProgressBar()
        progress.set_show_text(False)
        progress.set_fraction(0.0)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel_button = Gtk.Button(label=t('dialog_cancel'))
        cancel_button.connect('clicked', self._cancel_import_progress)
        buttons.append(cancel_button)

        box.append(title)
        box.append(body)
        box.append(progress)
        box.append(buttons)
        dialog.present()

        self._import_progress_dialog = dialog
        self._import_progress_label = body
        self._import_progress_bar = progress
        self._import_total = max(int(total or 0), 0)

    def _update_import_progress(self, current, total, title=''):
        if self._import_progress_label is None or self._import_progress_bar is None:
            return False
        safe_total = max(int(total or 0), 1)
        safe_current = max(0, min(int(current or 0), safe_total))
        if title:
            text = t('import_progress_current', current=safe_current, total=safe_total, title=title)
        else:
            text = t('import_progress_completed', current=safe_current, total=safe_total)
        self._import_progress_label.set_text(text)
        self._import_progress_bar.set_fraction(safe_current / safe_total)
        return False

    def _finish_import_payloads(self, imported_count, duplicate_count, cancelled=False):
        self._destroy_import_progress_dialog()
        self._import_cancel_requested = False
        self._reload_entries()
        if cancelled:
            self.show_overlay_notification(t('import_bundle_cancelled', imported=imported_count, duplicates=duplicate_count), timeout_ms=3400)
            return False
        if imported_count and duplicate_count:
            self.show_overlay_notification(t('import_bundle_result_with_duplicates', imported=imported_count, duplicates=duplicate_count), timeout_ms=3200)
        elif imported_count > 1:
            self.show_overlay_notification(t('import_bundle_result', imported=imported_count), timeout_ms=2800)
        elif imported_count == 1:
            self.show_overlay_notification(t('import_webapp_success'), timeout_ms=2400)
        elif duplicate_count and not imported_count:
            self.show_overlay_notification(t('import_bundle_none_with_duplicates', duplicates=duplicate_count), timeout_ms=3200)
        return False

    def _start_import_payloads(self, payloads):
        payloads = list(payloads or [])
        if not payloads:
            return
        importable_payloads = []
        duplicate_count = 0
        for payload in payloads:
            if self._find_import_collision(payload) is not None:
                duplicate_count += 1
                continue
            importable_payloads.append(payload)
        if not importable_payloads:
            if duplicate_count:
                self.show_overlay_notification(t('import_bundle_none_with_duplicates', duplicates=duplicate_count), timeout_ms=3200)
            return
        if len(importable_payloads) == 1:
            self._create_entry_from_wapp_payload(importable_payloads[0], reload_after_success=True)
            return

        total = len(importable_payloads)
        imported_count = 0
        state = {'index': 0}
        self._import_cancel_requested = False
        self._show_import_progress_dialog(total)
        GLib.idle_add(self._update_import_progress, 0, total, '')

        def process_next(success=False, _entry_id=None):
            nonlocal imported_count
            if success:
                imported_count += 1
            completed = state['index']
            GLib.idle_add(self._update_import_progress, completed, total, '')
            if self._import_cancel_requested:
                GLib.idle_add(self._finish_import_payloads, imported_count, duplicate_count, True)
                return False
            if state['index'] >= total:
                GLib.idle_add(self._finish_import_payloads, imported_count, duplicate_count, False)
                return False
            payload = importable_payloads[state['index']]
            state['index'] += 1
            title = str(payload.get('title') or payload.get('description') or '').strip()
            GLib.idle_add(self._update_import_progress, state['index'], total, title)
            self._create_entry_from_wapp_payload(payload, reload_after_success=True, on_complete=process_next)
            return False

        GLib.idle_add(process_next, False, None)

    def _on_import_wapp_dialog_response(self, result, response=None):
        if isinstance(result, Gio.File):
            file_obj = result
        else:
            dialog = result
            if response != Gtk.ResponseType.ACCEPT:
                try:
                    dialog.destroy()
                except (AttributeError, GLib.Error):
                    pass
                return
            file_obj = dialog.get_file()
            try:
                dialog.destroy()
            except (AttributeError, GLib.Error):
                pass
        local_path = file_obj.get_path() if file_obj is not None else None
        temp_path = self._copy_gfile_to_temp_path(file_obj, '.wapp')
        try:
            if temp_path is None:
                return
            payloads = load_import_payloads_from_path(temp_path)
            self._start_import_payloads(payloads)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            path_for_log = local_path or (str(temp_path) if temp_path else '')
            LOG.warning('Failed to import .wapp file %s: %s', path_for_log, error)
        finally:
            if temp_path is not None and (not local_path or str(temp_path) != local_path):
                temp_path.unlink(missing_ok=True)

    def _create_entry_from_wapp_payload(self, payload, reload_after_success=True, on_complete=None):
        collision_entry = self._find_import_collision(payload)
        if collision_entry is not None:
            self._show_import_collision(collision_entry, payload)
            if on_complete is not None:
                GLib.idle_add(on_complete, False, None)
            return False

        self._creating_entry = True
        self.add_button.set_sensitive(False)
        try:
            new_id = self.db.add_entry('')
            if new_id is None:
                if on_complete is not None:
                    GLib.idle_add(on_complete, False, None)
                return False
            entry = Entry(new_id, '')

            def _complete(success):
                if reload_after_success:
                    self._reload_entries()
                if on_complete is not None:
                    GLib.idle_add(on_complete, bool(success), entry.id if success else None)
                return False

            def apply_import():
                detail_page = None
                try:
                    detail_page = DetailPage(
                        entry,
                        self.db,
                        on_back=lambda *_args: None,
                        on_delete=lambda *_args: None,
                        on_title_changed=lambda *_args: None,
                        on_visual_changed=lambda *_args: None,
                        on_overlay_notification=self.show_overlay_notification,
                    )
                    if detail_page._apply_wapp_payload(payload):
                        return _complete(True)
                    try:
                        self.db.delete_entry(entry.id)
                    except sqlite3.Error:
                        pass
                    return _complete(False)
                except (GLib.Error, OSError, ValueError, sqlite3.Error) as error:
                    LOG.warning('Failed to apply imported .wapp to entry %s: %s', entry.id, error)
                    try:
                        self.db.delete_entry(entry.id)
                    except sqlite3.Error:
                        pass
                    return _complete(False)
                finally:
                    try:
                        if detail_page is not None:
                            detail_page.unparent()
                    except (AttributeError, TypeError, GLib.Error):
                        pass

            GLib.idle_add(apply_import)
            return True
        finally:
            self._creating_entry = False
            self.add_button.set_sensitive(True)

    def _create_entries_from_import_payloads(self, payloads):
        self._start_import_payloads(payloads)


class WebAppManager(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_startup(self):
        Adw.Application.do_startup(self)
        ensure_manager_desktop_integration(APP_DIR, LOG)

    def do_activate(self):
        win = MainWindow(self)
        win.present()
        GLib.timeout_add(200, win.reconcile_desktop_files)


app = WebAppManager()
app.run([])

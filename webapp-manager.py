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
from app_models import Entry
from app_identity import APP_DIR, APP_ID, APP_ICON_NAME, APP_DB_PATH
from manager_integration import ensure_manager_desktop_integration, headerbar_decoration_layout_without_icon
from mainwindow_window_state import MainWindowWindowStateMixin
from mainwindow_launch_export import MainWindowLaunchExportMixin
from mainwindow_notifications import MainWindowNotificationsMixin
from mainwindow_settings import MainWindowSettingsMixin
from mainwindow_dialogs import MainWindowDialogsMixin
from mainwindow_profile_import import MainWindowProfileImportMixin
from mainwindow_overview import MainWindowOverviewMixin
from mainwindow_entries import MainWindowEntriesMixin

Adw.init()
LOG = get_logger(__name__)
APP_VERSION = '68a'


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
            return '0 MB'
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
        return '0 MB'




CONFIG = {}
ENGINES = available_engines()
css_provider = Gtk.CssProvider()
try:
    css_provider.load_from_path(str(APP_DIR / 'style.css'))
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
except (GLib.Error, TypeError, ValueError, AttributeError) as error:
    LOG.error('Failed to load CSS: %s', error)



class MainWindow(MainWindowWindowStateMixin, MainWindowLaunchExportMixin, MainWindowNotificationsMixin, MainWindowSettingsMixin, MainWindowDialogsMixin, MainWindowProfileImportMixin, MainWindowOverviewMixin, MainWindowEntriesMixin, Adw.ApplicationWindow):

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

        self._adaptive_split_enabled = bool(
            hasattr(Adw, 'ToolbarView')
            and hasattr(Adw, 'NavigationSplitView')
            and hasattr(Adw, 'NavigationPage')
            and hasattr(Adw, 'Breakpoint')
            and hasattr(Adw, 'BreakpointCondition')
        )
        self._adaptive_collapse_condition = 'max-width: 860sp'
        initial_window_width = int(self._window_state.get('width', 500) or 500)
        self._adaptive_narrow_mode = initial_window_width <= 860
        self._adaptive_breakpoint = None
        self._adaptive_breakpoint_fallback_id = 0

        self.header_bar = Adw.HeaderBar()
        self.header_bar.set_decoration_layout(headerbar_decoration_layout_without_icon())
        self.search_button = Gtk.Button(icon_name='system-search-symbolic')
        if hasattr(self.search_button, 'set_can_shrink'):
            self.search_button.set_can_shrink(True)
        self.search_button.connect('clicked', self.on_search_clicked)
        self.refresh_button = Gtk.Button(icon_name='view-refresh-symbolic')
        if hasattr(self.refresh_button, 'set_can_shrink'):
            self.refresh_button.set_can_shrink(True)
        self.refresh_button.set_tooltip_text(t('resync_profiles_button'))
        self.refresh_button.connect('clicked', self.on_refresh_clicked)
        self.home_button = Gtk.Button(icon_name='go-home-symbolic')
        if hasattr(self.home_button, 'set_can_shrink'):
            self.home_button.set_can_shrink(True)
        self.home_button.set_tooltip_text(t('welcome_title'))
        self.home_button.connect('clicked', self.on_home_clicked)
        self.add_button = Gtk.Button(icon_name='list-add-symbolic')
        if hasattr(self.add_button, 'set_can_shrink'):
            self.add_button.set_can_shrink(True)
        self.add_button.connect('clicked', self.on_add_entry)
        self.settings_button = Gtk.Button(icon_name='emblem-system-symbolic')
        if hasattr(self.settings_button, 'set_can_shrink'):
            self.settings_button.set_can_shrink(True)
        self.settings_button.set_tooltip_text(t('settings_title'))
        self.settings_button.connect('clicked', self.show_settings_page)
        self.assets_button = Gtk.Button(icon_name='folder-download-symbolic')
        if hasattr(self.assets_button, 'set_can_shrink'):
            self.assets_button.set_can_shrink(True)
        self.assets_button.set_tooltip_text(t('settings_assets_title'))
        self.assets_button.connect('clicked', self.show_assets_settings_page)
        self.back_button = Gtk.Button.new_from_icon_name('go-previous-symbolic')
        if hasattr(self.back_button, 'set_can_shrink'):
            self.back_button.set_can_shrink(True)
        self.back_button.connect('clicked', self.show_list_page)
        self.header_bar.pack_start(self.search_button)
        self.header_bar.pack_start(self.home_button)
        self.header_bar.pack_start(self.refresh_button)
        self.header_bar.pack_start(self.back_button)
        self.header_bar.pack_start(self.settings_button)
        self.header_bar.pack_end(self.assets_button)
        self.header_bar.pack_end(self.add_button)
        self.list_title_widget = self._build_list_title_widget()
        self.header_bar.set_title_widget(self.list_title_widget)

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

        if self._adaptive_split_enabled:
            self.toolbar_view = Adw.ToolbarView()
            self.toolbar_view.add_top_bar(self.header_bar)
            self.toolbar_view.set_content(self.stack_overlay)
            self.set_content(self.toolbar_view)
            self.set_size_request(360, 420)
        else:
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

        self.detail_placeholder = self._build_welcome_page()

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

        self.content_stack = Gtk.Stack()
        self.content_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.content_stack.set_vexpand(True)
        self.content_stack.set_hexpand(True)

        if self._adaptive_split_enabled:
            self.content_stack.add_named(self.detail_placeholder, 'detail_placeholder')
            self.sidebar_navigation_page = Adw.NavigationPage.new(self.list_page, t('app_title'))
            self.sidebar_navigation_page.set_tag('overview-list')
            self.content_navigation_page = Adw.NavigationPage.new(self.content_stack, t('app_title'))
            self.content_navigation_page.set_tag('overview-content')
            self.overview_split_view = Adw.NavigationSplitView()
            self.overview_split_view.set_sidebar(self.sidebar_navigation_page)
            self.overview_split_view.set_content(self.content_navigation_page)
            self.overview_split_view.set_show_content(False)
            self.overview_split_view.set_min_sidebar_width(240)
            self.overview_split_view.set_max_sidebar_width(360)
            self.overview_split_view.set_sidebar_width_fraction(0.30)
            self.overview_split_view.connect('notify::show-content', self._on_overview_split_changed)
            self.overview_split_view.connect('notify::collapsed', self._on_overview_split_changed)
            self.stack.add_named(self.overview_split_view, 'overview_page')
            self._configure_adaptive_breakpoints()
        else:
            self.content_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
            self.content_stack.add_named(self.list_page, 'list_page')
            self.content_stack.add_named(self.detail_placeholder, 'detail_placeholder')
            self.stack.add_named(self.content_stack, 'overview_page')
            self.overview_split_view = None
            self.sidebar_navigation_page = None
            self.content_navigation_page = None

        self.settings_page = self._build_settings_page()
        self.settings_assets_page = self._build_assets_settings_page()
        if self._adaptive_split_enabled:
            self._add_overview_detail_page(self.settings_page, 'settings_page')
            self._add_overview_detail_page(self.settings_assets_page, 'settings_assets_page')
        else:
            self.stack.add_named(self.settings_page, 'settings_page')
            self.stack.add_named(self.settings_assets_page, 'settings_assets_page')
        self.detail_pages = {}
        self._creating_entry = False
        self.connect('destroy', self.close_event)
        self.update_empty_state()
        self._apply_ui_appearance_setting()
        self._show_overview_root_page()



































































































































































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

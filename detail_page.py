import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
try:
    gi.require_version('GtkSource', '5')
except (ValueError, ImportError):
    pass
from gi.repository import Adw, Gtk, GLib, Gio, Gdk, Pango
try:
    from gi.repository import GtkSource
except (ImportError, ValueError):
    GtkSource = None
import base64
import binascii
import io
import os
import json
import re
import shutil
import tempfile
import threading
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

from PIL import Image, UnidentifiedImageError

from browser_profiles import delete_managed_browser_profiles, get_profile_size_bytes, firefox_extension_installed, apply_profile_settings, ensure_browser_profile, read_profile_settings
from custom_assets import (
    ASSET_OPTION_KEY_BY_TYPE,
    CUSTOM_CSS_LINKS_KEY,
    CUSTOM_JS_LINKS_KEY,
    INLINE_CUSTOM_CSS_KEY,
    INLINE_CUSTOM_JS_KEY,
    INLINE_CUSTOM_CSS_HASH_KEY,
    INLINE_CUSTOM_JS_HASH_KEY,
    asset_content_sha256_from_text,
    encode_linked_asset_ids,
    format_asset_date,
    get_custom_asset,
    list_custom_assets,
    normalize_linked_asset_ids,
)
from distro_utils import is_furios_distribution
from desktop_entries import export_desktop_file, get_expected_desktop_path
from icon_pipeline import get_managed_icon_path, normalize_icon_bytes_to_png, normalize_icon_to_png
from webapp_constants import (
    APPLICATIONS_DIR,
    ADDRESS_KEY,
    DESKTOP_NAME_SOURCE_KEY,
    ICON_PATH_KEY,
    USER_AGENT_NAME_KEY,
    USER_AGENT_VALUE_KEY,
    PROFILE_NAME_KEY,
    PROFILE_PATH_KEY,
    ONLY_HTTPS_KEY,
    OPTION_FORCE_PRIVACY_KEY,
    APP_MODE_KEY,
    COLOR_SCHEME_KEY,
    DEFAULT_ZOOM_KEY,
    OPTION_ADBLOCK_KEY,
    OPTION_DISABLE_AI_KEY,
    OPTION_KEEP_IN_BACKGROUND_KEY,
    OPTION_SWIPE_KEY,
    OPTION_PRESERVE_SESSION_KEY,
    OPTION_STARTUP_BOOSTER_KEY,
)
from input_validation import (
    DESKTOP_CHROME_USER_AGENT,
    MAX_ICON_FILE_SIZE,
    build_safe_slug,
    check_origin_status,
    candidate_urls_for_input,
    is_structurally_valid_url,
    is_valid_url,
    load_and_normalize_wapp_payload_from_path,
    normalize_wapp_payload,
    validate_icon_source_path,
)
from i18n import get_app_config, t
from logger_setup import get_logger
from engine_support import engine_available
from browser_option_logic import (
    browser_family_for_engine,
    browser_state_key,
    build_family_option_state,
    decode_browser_state,
    encode_browser_state,
    browser_managed_option_keys,
    normalize_option_dict,
    normalize_option_rows,
    option_ui_label,
    option_ui_label_markup,
    supported_browser_option_keys,
    OPTION_SPEC_BY_KEY,
)
from option_config import option_names
from browser_option_registry import OPTION_CATEGORY_ORDER, OPTION_CATEGORY_LABEL_KEYS, option_category
from detail_page_layout import DetailPageLayoutMixin
from detail_page_assets import DetailPageAssetsMixin
from detail_page_options import DetailPageOptionsMixin
from detail_page_icon import DetailPageIconMixin
from detail_page_transfer import DetailPageTransferMixin

LOG = get_logger(__name__)





class DetailPage(DetailPageLayoutMixin, DetailPageAssetsMixin, DetailPageOptionsMixin, DetailPageIconMixin, DetailPageTransferMixin, Gtk.Box):
    def __init__(self, entry, db, on_back, on_delete, on_title_changed=None, on_visual_changed=None, on_overlay_notification=None, on_navigation_changed=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self.entry = entry
        self.db = db
        self.on_back = on_back
        self.on_delete_callback = on_delete
        self.on_title_changed = on_title_changed
        self.on_visual_changed = on_visual_changed
        self.on_overlay_notification = on_overlay_notification
        self.on_navigation_changed = on_navigation_changed
        self.config = get_app_config()
        configured_engines = self.config.get('engines', []) or [
            {'id': 1, 'name': 'Firefox', 'command': 'firefox'},
            {'id': 2, 'name': 'Chrome', 'command': 'google-chrome'},
        ]
        self.engines_list = [engine for engine in configured_engines if engine_available(engine)]
        self.engines_names = [engine['name'] for engine in self.engines_list]
        self.engine_dropdown_labels = [t('engine_none')] + self.engines_names
        self.engine_user_agents = self._build_engine_user_agents()
        self._address_validation_source_id = 0
        self._address_export_source_id = 0
        self._address_persist_source_id = 0
        self._address_validation_serial = 0
        self._address_last_validated_value = ''
        self._initial_address_validation_source_id = 0
        self._icon_upload_dialog_active = False
        self._address_debounce_ms = 1200
        self._address_persist_ms = 700
        self._suspend_address_processing = False
        self._address_export_after_validation = False
        self._auto_icon_fetch_url = ''
        self._profile_size_request_serial = 0
        self._profile_size_pending_path = ''
        self._profile_size_cache = {}
        self._suspend_change_handlers = True
        self.db.canonicalize_option_keys(self.entry.id)
        self._options_cache = normalize_option_rows(self.db.get_options_for_entry(self.entry.id))
        self._syncing_browser_state = False
        self._options_rebuild_source_id = 0
        self._icon_texture_cache = {}
        self._icon_page_preview_refresh_source_id = 0
        self._icon_page_preview_signature = None
        self._icon_page_preview_worker_serial = 0
        self._plugin_operation_serial = 0
        self._plugin_operation_in_progress = False
        self._detail_toast_timeout_id = 0
        self._icon_download_in_progress = False
        self._compact_mode_override = None
        self._inline_editor_save_source_ids = {'css': 0, 'javascript': 0}
        self._detail_main_scroll_position = 0.0
        self._detail_main_scroll_restore_source_id = 0
        self._code_editors = []
        self._style_manager = Adw.StyleManager.get_default()
        if self._style_manager is not None:
            try:
                self._style_manager.connect('notify::dark', self._on_style_manager_dark_changed)
            except Exception:
                pass

        swipe_back = Gtk.GestureSwipe.new()
        swipe_back.connect('swipe', self.on_swipe)
        self.add_controller(swipe_back)

        self.content_overlay = Gtk.Overlay()
        self.content_overlay.set_hexpand(True)
        self.content_overlay.set_vexpand(True)
        self.append(self.content_overlay)

        self.detail_shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.detail_shell.set_hexpand(True)
        self.detail_shell.set_vexpand(True)
        self.content_overlay.set_child(self.detail_shell)

        self.detail_tab_scroller = Gtk.ScrolledWindow()
        self.detail_tab_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self.detail_tab_scroller.set_overlay_scrolling(True)
        self.detail_tab_scroller.set_hexpand(True)
        self.detail_tab_scroller.set_vexpand(False)
        self.detail_tab_scroller.add_css_class('detail-tab-scroller')
        self.detail_shell.append(self.detail_tab_scroller)

        self.desktop_tab_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.desktop_tab_bar.add_css_class('detail-desktop-tab-bar')
        self.desktop_tab_bar.set_margin_top(10)
        self.desktop_tab_bar.set_margin_start(12)
        self.desktop_tab_bar.set_margin_end(12)
        self.desktop_tab_bar.set_margin_bottom(10)
        self.desktop_tab_bar.set_halign(Gtk.Align.START)
        self.desktop_tab_bar.set_visible(False)
        self.detail_tab_scroller.set_child(self.desktop_tab_bar)

        self.desktop_tab_buttons = {}
        for tab_name, label_key in (
            ('main', 'detail_tab_basic'),
            ('options', 'detail_tab_options'),
            ('css_assets', 'detail_tab_css'),
            ('javascript_assets', 'detail_tab_javascript'),
        ):
            button = Gtk.ToggleButton(label=t(label_key))
            button.add_css_class('flat')
            button.add_css_class('detail-tab-button')
            button.set_hexpand(True)
            button.connect('toggled', self._on_desktop_tab_toggled, tab_name)
            self.desktop_tab_buttons[tab_name] = button
            self.desktop_tab_bar.append(button)

        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scrolled.set_overlay_scrolling(True)
        self.scrolled.add_css_class('detail-scroll-area')
        self.scrolled.set_propagate_natural_width(True)
        self.scrolled.set_propagate_natural_height(True)
        self.scrolled.set_vexpand(True)
        self.detail_shell.append(self.scrolled)

        self.inline_busy_overlay = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.inline_busy_overlay.add_css_class('busy-overlay')
        self.inline_busy_overlay.set_halign(Gtk.Align.CENTER)
        self.inline_busy_overlay.set_valign(Gtk.Align.CENTER)
        self.inline_busy_overlay.set_visible(False)
        self.inline_busy_overlay.set_can_target(True)
        self.inline_busy_spinner = Gtk.Spinner()
        self.inline_busy_spinner.set_size_request(28, 28)
        self.inline_busy_label = Gtk.Label(label=t('loading'))
        self.inline_busy_label.add_css_class('dim-label')
        self.inline_busy_overlay.append(self.inline_busy_spinner)
        self.inline_busy_overlay.append(self.inline_busy_label)
        self.content_overlay.add_overlay(self.inline_busy_overlay)


        self.page_stack = Gtk.Stack()
        self.page_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.page_stack.set_vhomogeneous(False)
        self.page_stack.set_hhomogeneous(False)
        self.page_stack.set_valign(Gtk.Align.START)
        self.page_stack.set_hexpand(True)
        self.page_stack.set_vexpand(False)
        self.page_stack.connect('notify::visible-child-name', self._on_page_stack_visible_child_changed)
        self.scrolled.set_child(self.page_stack)

        self.content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.content_box.set_hexpand(True)
        self.content_box.set_halign(Gtk.Align.FILL)
        self.content_box.set_vexpand(False)
        self.content_box.set_margin_top(12)
        self.content_box.set_margin_bottom(12)
        self.content_box.set_margin_start(12)
        self.content_box.set_margin_end(12)
        self.page_stack.add_named(self._adaptive_wrap_page(self.content_box), 'main')

        self.options_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.options_page.set_margin_top(12)
        self.options_page.set_margin_bottom(12)
        self.options_page.set_margin_start(12)
        self.options_page.set_margin_end(12)
        self.options_page.set_valign(Gtk.Align.START)
        self.options_page.set_vexpand(False)
        self.options_page.set_hexpand(True)

        self.options_page_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.options_page_content.set_halign(Gtk.Align.FILL)
        self.options_page_content.set_margin_top(12)
        self.options_page_content.set_margin_bottom(12)
        self.options_page.append(self.options_page_content)
        self.page_stack.add_named(self._adaptive_wrap_page(self.options_page), 'options')

        self.top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.top_row.set_valign(Gtk.Align.START)

        self.header_main_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.header_main_row.set_hexpand(True)
        self.header_main_row.set_valign(Gtk.Align.START)
        self.header_main_row.set_halign(Gtk.Align.FILL)

        self.icon_button = Gtk.Button()
        self.icon_button.add_css_class('icon-tile')
        self.icon_button.set_size_request(72, 72)
        self.icon_button.set_halign(Gtk.Align.START)
        self.icon_button.set_overflow(Gtk.Overflow.HIDDEN)
        self.icon_button.set_valign(Gtk.Align.START)
        self.icon_button.set_hexpand(False)
        self.icon_button.set_vexpand(False)
        self.icon_button.connect('clicked', self.on_icon_clicked)
        self.header_main_row.append(self.icon_button)

        self.title_meta_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.title_meta_box.set_halign(Gtk.Align.START)
        self.title_meta_box.set_valign(Gtk.Align.START)
        self.title_meta_box.set_hexpand(True)

        self.header_name_label = Gtk.Label(xalign=0)
        self.header_name_label.add_css_class('title-4')
        self.header_name_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.header_name_label.set_max_width_chars(28)
        self.header_name_label.set_hexpand(True)
        self.header_name_label.set_wrap(False)
        self.header_name_label.set_text(entry.title)

        self.header_profile_label = Gtk.Label(xalign=0)
        self.header_profile_label.add_css_class('dim-label')
        self.header_profile_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.header_profile_label.set_max_width_chars(28)
        self.header_profile_label.set_hexpand(True)
        self.header_profile_label.set_wrap(False)
        self.header_profile_label.set_text(self._profile_display_name())
        self.header_name_label.set_valign(Gtk.Align.START)
        self.header_profile_label.set_valign(Gtk.Align.START)

        self.title_meta_box.append(self.header_name_label)
        self.title_meta_box.append(self.header_profile_label)
        self.header_main_row.append(self.title_meta_box)
        self.top_row.append(self.header_main_row)

        self.switch_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.switch_box.set_halign(Gtk.Align.END)
        self.switch_box.set_hexpand(False)
        self.switch_box.set_valign(Gtk.Align.END)
        self.switch_box.set_margin_top(0)
        self.switch_box.set_margin_bottom(2)

        switch_label = Gtk.Label(label=t('label_active'))
        switch_label.set_xalign(0)
        switch_label.set_halign(Gtk.Align.START)
        switch_label.set_valign(Gtk.Align.END)
        switch_label.set_margin_top(0)

        self.switch = Gtk.Switch()
        self.switch.add_css_class('boolean-switch')
        self.switch.set_halign(Gtk.Align.END)
        self.switch.set_valign(Gtk.Align.END)
        self.switch.set_active(bool(entry.active))
        self.switch.connect('notify::active', self.on_switch_toggled)

        self.switch_box.append(switch_label)
        self.switch_box.append(self.switch)

        self.top_row.append(self.switch_box)
        self.content_box.append(self.top_row)

        self.grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        self.grid.set_margin_top(22)
        self.grid.set_hexpand(True)
        self.grid.set_column_homogeneous(False)
        self.grid.set_row_homogeneous(False)

        self.title_entry = Gtk.Entry()
        self.title_entry.set_placeholder_text(t('placeholder_name'))
        self.title_entry.set_text(entry.title or '')
        self.title_entry.set_hexpand(True)
        self.title_entry.connect('changed', self.on_name_changed)
        self.title_entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, 'selection-mode-symbolic')
        self.title_entry.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, t('desktop_name_source_use_name'))
        self.title_entry.set_icon_activatable(Gtk.EntryIconPosition.SECONDARY, True)
        self.title_entry.connect('icon-press', self.on_desktop_name_icon_pressed, 'title')
        self.title_label = Gtk.Label(label=t('label_name'), halign=Gtk.Align.START)
        self.grid.attach(self.title_label, 0, 0, 1, 1)
        self.grid.attach(self.title_entry, 1, 0, 1, 1)

        self.description_entry = Gtk.Entry()
        self.description_entry.set_placeholder_text(t('placeholder_description'))
        self.description_entry.set_text(entry.description or '')
        self.description_entry.set_hexpand(True)
        self.description_entry.connect('changed', self.on_description_changed)
        self.description_entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, 'selection-mode-symbolic')
        self.description_entry.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, t('desktop_name_source_use_description'))
        self.description_entry.set_icon_activatable(Gtk.EntryIconPosition.SECONDARY, True)
        self.description_entry.connect('icon-press', self.on_desktop_name_icon_pressed, 'description')
        self.description_label = Gtk.Label(label=t('label_description'), halign=Gtk.Align.START)
        self.grid.attach(self.description_label, 0, 1, 1, 1)
        self.grid.attach(self.description_entry, 1, 1, 1, 1)

        self.address_entry = Gtk.Entry()
        self.address_entry.set_placeholder_text(t('placeholder_address'))
        addr = self._get_option_value(ADDRESS_KEY) or self._get_option_value('Adresse')
        if addr:
            self.address_entry.set_text(addr)
        self.address_entry.set_hexpand(True)
        self.address_entry.connect('changed', self.on_address_changed)
        self.address_label = Gtk.Label(label=t('label_address'), halign=Gtk.Align.START)
        self.grid.attach(self.address_label, 0, 2, 1, 1)
        self.grid.attach(self.address_entry, 1, 2, 1, 1)

        self.url_status_label = Gtk.Label(label='', halign=Gtk.Align.START)
        self.url_status_label.set_xalign(0)
        self.url_status_label.set_wrap(True)
        self.url_status_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.url_status_label.set_lines(2)
        self.url_status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.url_status_label.set_hexpand(True)
        self.url_status_label.set_margin_start(10)
        self.url_status_label.remove_css_class('heading')
        self.url_status_label.add_css_class('url-status-subtle')
        self.grid.attach(self.url_status_label, 1, 3, 1, 1)

        self.engine_spacer = Gtk.Box()
        self.engine_spacer.set_size_request(-1, 8)
        self.grid.attach(self.engine_spacer, 0, 4, 2, 1)

        self.engine_dropdown = Gtk.DropDown.new_from_strings(self.engine_dropdown_labels)
        stored_engine_id = self._get_option_value('EngineID')
        engine_id = self._safe_int(stored_engine_id, default=0) if stored_engine_id not in (None, '') else 0
        selected_engine_index = 0
        for idx, engine in enumerate(self.engines_list, start=1):
            if engine['id'] == engine_id:
                selected_engine_index = idx
                break
        self.engine_dropdown.set_selected(selected_engine_index)
        self.engine_dropdown.connect('notify::selected', self.on_engine_changed)
        self.engine_label = Gtk.Label(label=t('label_engine'), halign=Gtk.Align.START)
        self.grid.attach(self.engine_label, 0, 5, 1, 1)
        self.grid.attach(self.engine_dropdown, 1, 5, 1, 1)

        self.user_agent_dropdown = Gtk.DropDown.new_from_strings([t('user_agent_none')])
        self.user_agent_dropdown.connect('notify::selected', self.on_user_agent_changed)
        self.user_agent_status = Gtk.Label(label='', halign=Gtk.Align.START)
        self.user_agent_status.set_xalign(0)
        self.user_agent_status.set_wrap(True)
        self.user_agent_status.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.user_agent_status.set_hexpand(True)
        self.user_agent_status.add_css_class('dim-label')

        self.browser_option_status = Gtk.Label(label='', halign=Gtk.Align.START)
        self.browser_option_status.set_xalign(0)
        self.browser_option_status.set_wrap(True)
        self.browser_option_status.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.browser_option_status.set_hexpand(True)
        self.browser_option_status.add_css_class('dim-label')
        self.user_agent_label = Gtk.Label(label=t('label_user_agent'), halign=Gtk.Align.START)
        self.grid.attach(self.user_agent_label, 0, 6, 1, 1)
        self.grid.attach(self.user_agent_dropdown, 1, 6, 1, 1)
        self.refresh_user_agent_options()

        self.mode_labels = []
        self.mode_values = []
        self.mode_dropdown = Gtk.DropDown.new_from_strings([t('mode_standard')])
        self.mode_dropdown.connect('notify::selected', self.on_mode_changed)
        self.mode_label = Gtk.Label(label=t('label_mode'), halign=Gtk.Align.START)
        self.grid.attach(self.mode_label, 0, 7, 1, 1)
        self.grid.attach(self.mode_dropdown, 1, 7, 1, 1)
        self.refresh_mode_options()

        self.color_scheme_labels = [t('color_scheme_auto'), t('color_scheme_dark'), t('color_scheme_light')]
        self.color_scheme_values = ['auto', 'dark', 'light']
        self.color_scheme_dropdown = Gtk.DropDown.new_from_strings(self.color_scheme_labels)
        stored_color_scheme = (self._get_option_value(COLOR_SCHEME_KEY) or 'auto').strip().lower()
        try:
            color_index = self.color_scheme_values.index(stored_color_scheme)
        except ValueError:
            color_index = 0
        self.color_scheme_dropdown.set_selected(color_index)
        self.color_scheme_dropdown.connect('notify::selected', self.on_color_scheme_changed)
        self.color_scheme_label = Gtk.Label(label=t('label_color_scheme'), halign=Gtk.Align.START)
        self.grid.attach(self.color_scheme_label, 0, 9, 1, 1)
        self.grid.attach(self.color_scheme_dropdown, 1, 9, 1, 1)

        self.default_zoom_labels = [
            t('default_zoom_50'),
            t('default_zoom_67'),
            t('default_zoom_80'),
            t('default_zoom_90'),
            t('default_zoom_100'),
            t('default_zoom_110'),
            t('default_zoom_125'),
            t('default_zoom_150'),
            t('default_zoom_175'),
            t('default_zoom_200'),
        ]
        self.default_zoom_values = ['50', '67', '80', '90', '100', '110', '125', '150', '175', '200']
        self.default_zoom_dropdown = Gtk.DropDown.new_from_strings(self.default_zoom_labels)
        stored_default_zoom = (self._get_option_value(DEFAULT_ZOOM_KEY) or '100').strip()
        try:
            default_zoom_index = self.default_zoom_values.index(stored_default_zoom)
        except ValueError:
            default_zoom_index = self.default_zoom_values.index('100')
        self.default_zoom_dropdown.set_selected(default_zoom_index)
        self.default_zoom_dropdown.connect('notify::selected', self.on_default_zoom_changed)
        self.default_zoom_label = Gtk.Label(label=t('label_default_zoom'), halign=Gtk.Align.START)
        self.grid.attach(self.default_zoom_label, 0, 10, 1, 1)
        self.grid.attach(self.default_zoom_dropdown, 1, 10, 1, 1)

        self.color_scheme_spacer = Gtk.Box()
        self.color_scheme_spacer.set_size_request(-1, 8)
        self.grid.attach(self.color_scheme_spacer, 0, 11, 2, 1)

        self.option_names = option_names()
        self.switches = {}
        self._option_row_widgets = {}
        self._subpage_compact = None
        self._options_compact = None
        self._form_compact = None
        self._top_row_compact = None
        self._action_row_compact = None
        self._options_section_compact = None
        self.options_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.options_container.set_hexpand(True)
        self.options_container.set_vexpand(False)
        self.options_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.options_section.set_hexpand(True)
        self.options_section.set_vexpand(False)
        self.options_section.append(self.options_container)
        self.options_section.append(self.browser_option_status)
        self.main_options_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.main_options_slot.set_hexpand(True)

        self.content_box.append(self.grid)
        self.content_box.append(self.main_options_slot)
        self._rebuild_options_layout(force=True)
        self._mount_options_section(compact=True, force=True)

        self.plugin_activity_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.plugin_activity_row.set_visible(False)
        self.plugin_activity_spinner = Gtk.Spinner()
        self.plugin_activity_spinner.set_size_request(16, 16)
        self.plugin_activity_label = Gtk.Label(label='', halign=Gtk.Align.START)
        self.plugin_activity_label.set_xalign(0)
        self.plugin_activity_label.add_css_class('dim-label')
        self.plugin_activity_row.append(self.plugin_activity_spinner)
        self.plugin_activity_row.append(self.plugin_activity_label)

        self.detail_action_status = Gtk.Label(label='', halign=Gtk.Align.START)
        self.detail_action_status.set_xalign(0)
        self.detail_action_status.add_css_class('dim-label')
        self.detail_action_status.set_margin_top(0)
        self.detail_action_status.set_visible(False)
        self.content_box.append(self.detail_action_status)

        self.custom_assets_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.custom_assets_row.set_homogeneous(True)
        self.custom_assets_row.set_margin_top(6)

        self.add_css_button = Gtk.Button(label=t('detail_add_css_button'))
        self.add_css_button.set_hexpand(True)
        self.add_css_button.connect('clicked', lambda _button: self.show_asset_page('css'))
        self.custom_assets_row.append(self.add_css_button)

        self.add_js_button = Gtk.Button(label=t('detail_add_javascript_button'))
        self.add_js_button.set_hexpand(True)
        self.add_js_button.set_margin_start(0)
        self.add_js_button.connect('clicked', lambda _button: self.show_asset_page('javascript'))
        self.custom_assets_row.append(self.add_js_button)

        self.content_box.append(self.custom_assets_row)
        self._desktop_tabs_syncing = False

        self.export_import_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.export_import_row.set_homogeneous(True)
        self.export_import_row.set_margin_top(14)

        self.export_button = Gtk.Button(label=t('export_webapp_button'))
        self.export_button.set_hexpand(True)
        self.export_button.connect('clicked', self.on_export_webapp_clicked)
        self.export_import_row.append(self.export_button)

        self.content_box.append(self.export_import_row)

        self.delete_profile_button = Gtk.Button()
        self.delete_profile_button.set_hexpand(True)
        self.delete_profile_button.add_css_class('warning-button')
        self.delete_profile_button.set_margin_top(6)
        self.delete_profile_button.connect('clicked', self.on_delete_profile_clicked)
        self.content_box.append(self.delete_profile_button)

        self._engine_option_widgets = [
            self.user_agent_label, self.user_agent_dropdown, self.mode_label, self.mode_dropdown,
            self.color_scheme_label, self.color_scheme_dropdown, self.default_zoom_label, self.default_zoom_dropdown, self.color_scheme_spacer,
            self.options_section, self.export_import_row, self.delete_profile_button,
        ]
        self._option_row_widgets = {}

        self._icon_page_buttons = []
        self._build_icon_page()
        self._asset_page_state = {}
        self._build_asset_page('css')
        self._build_asset_page('javascript')
        self.refresh_icon_preview()
        self._update_desktop_name_source_buttons()
        self._update_export_button_state()
        self._update_browser_dependent_controls()
        self.page_stack.set_visible_child_name('main')
        self.connect('notify::width', self._on_layout_width_changed)
        self.page_stack.connect('notify::width', self._on_layout_width_changed)
        self.grid.connect('notify::width', self._on_layout_width_changed)
        GLib.idle_add(self._finish_initial_detail_setup)
















    def on_delete_clicked(self, button):
        self._present_choice_dialog(
            button,
            t('delete_webapp_confirm', title=self.entry.title),
            lambda confirmed: self.on_delete_callback(self.entry) if confirmed else None,
            destructive=True,
        )






















































    def _current_browser_family(self):
        return browser_family_for_engine(self._get_current_engine())








    def _safe_int(self, value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _looks_ready_for_url_check(self, value):
        value = (value or '').strip()
        if not value or value in {'http://', 'https://'}:
            return False
        if ' ' in value or value.endswith(('.', ':', '?', '#')):
            return False
        if not is_structurally_valid_url(value):
            return False
        try:
            parsed = urlparse(value)
        except ValueError:
            return False
        host = (parsed.hostname or '').strip().lower()
        if not host:
            return False
        if host == 'localhost':
            return True
        if re.fullmatch(r'\d{1,3}(?:\.\d{1,3}){3}', host):
            return True
        if host.endswith('.') or '..' in host:
            return False
        labels = host.split('.')
        if len(labels) < 2:
            return False
        if any((not label) or label.startswith('-') or label.endswith('-') for label in labels):
            return False
        return len(labels[-1]) >= 2 and labels[-1].isalnum()

    def _set_url_status(self, text, css_class=None):
        self.url_status_label.remove_css_class('dim-label')
        self.url_status_label.remove_css_class('url-status-error')
        self.url_status_label.remove_css_class('url-status-ok')
        self.url_status_label.remove_css_class('url-status-warning')
        self.url_status_label.set_text(text)
        if css_class:
            self.url_status_label.add_css_class(css_class)

    def _should_validate_address_on_open(self):
        return False

    def _cancel_initial_address_validation(self):
        if self._initial_address_validation_source_id:
            GLib.source_remove(self._initial_address_validation_source_id)
            self._initial_address_validation_source_id = 0

    def _maybe_validate_initial_address(self, delay_ms=650):
        self._cancel_initial_address_validation()
        self._set_url_status('')

    def _update_url_status(self, value):
        if not value:
            self._set_url_status('', None)
            return
        if not self._looks_ready_for_url_check(value):
            self._set_url_status('')
            return
        self._set_url_status(t('url_status_checking'), 'dim-label')
        self._validate_url_in_background(value)

    def _finish_url_validation(self, value, status):
        current_value = self.address_entry.get_text().strip()
        if value != current_value:
            return False
        if self._icon_upload_dialog_active and not self._address_export_after_validation:
            return False
        valid = status in {'ok', 'blocked', 'unverified'}
        self._address_last_validated_value = value if valid else ''
        if status == 'ok':
            self._set_url_status(t('url_status_valid'), 'url-status-ok')
        elif status == 'blocked':
            self._set_url_status(t('url_status_blocked'), 'url-status-warning')
        elif status == 'unverified':
            self._set_url_status(t('url_status_unverified'), 'url-status-warning')
        else:
            self._set_url_status(t('url_status_invalid'), 'url-status-error')
        if valid:
            self._maybe_autofetch_icon(value)
        self._update_export_button_state()
        if valid and self._address_export_after_validation:
            self._address_export_after_validation = False
            self.save_desktop_file()
        return False

    def _validate_url_in_background(self, value):
        def worker():
            status = check_origin_status(value)
            GLib.idle_add(self._finish_url_validation, value, status)
        threading.Thread(target=worker, daemon=True).start()

    def _flush_pending_address_option_write(self):
        if self._address_persist_source_id:
            GLib.source_remove(self._address_persist_source_id)
            self._address_persist_source_id = 0
        value = self._normalize_address_for_ui(self.address_entry.get_text().strip()) if hasattr(self, 'address_entry') else self._normalize_address_for_ui(self._options_cache.get(ADDRESS_KEY, ''))
        self.db.add_option(self.entry.id, ADDRESS_KEY, value, commit=True)

    def _cancel_address_timers(self):
        if self._address_validation_source_id:
            GLib.source_remove(self._address_validation_source_id)
            self._address_validation_source_id = 0
        if self._address_export_source_id:
            GLib.source_remove(self._address_export_source_id)
            self._address_export_source_id = 0
        if self._address_persist_source_id:
            GLib.source_remove(self._address_persist_source_id)
            self._address_persist_source_id = 0

    def _schedule_address_processing(self, value, export_after_validation=True):
        self._address_validation_serial += 1
        self._address_export_after_validation = export_after_validation
        serial = self._address_validation_serial
        self._cancel_address_timers()

        def persist_address():
            if serial != self._address_validation_serial:
                return False
            self._address_persist_source_id = 0
            self.db.add_option(self.entry.id, ADDRESS_KEY, value, commit=True)
            return False

        def run_validation():
            if serial != self._address_validation_serial:
                return False
            self._address_validation_source_id = 0
            self._update_url_status(value)
            return False

        self._address_persist_source_id = GLib.timeout_add(self._address_persist_ms, persist_address)
        self._address_validation_source_id = GLib.timeout_add(self._address_debounce_ms, run_validation)

    def _trigger_address_validation(self, value, debounce=True, export_after_validation=False):
        value = self._normalize_address_for_ui(value)
        self._cancel_address_timers()
        self._address_last_validated_value = ''
        self._address_export_after_validation = export_after_validation
        if not value:
            self._update_url_status(value)
            self._update_export_button_state()
            return
        if not self._looks_ready_for_url_check(value):
            self._set_url_status('')
            self._update_export_button_state()
            return
        if debounce:
            self._schedule_address_processing(value, export_after_validation=export_after_validation)
        else:
            self._update_url_status(value)












    def _profile_display_name(self):
        profile_path = (self._get_option_value(PROFILE_PATH_KEY) or '').strip()
        if profile_path:
            return Path(profile_path).name
        return (self._get_option_value(PROFILE_NAME_KEY) or '').strip()

































































































    def on_name_changed(self, entry_widget):
        new_title = entry_widget.get_text().strip()
        self.entry.title = new_title
        self._refresh_header_meta()
        self.db.update_entry(self.entry.id, title=new_title)
        self._sync_icon_filename()
        if self.on_title_changed:
            self.on_title_changed(self.entry)
        self._update_export_button_state()
        self.save_desktop_file()

    def on_description_changed(self, entry_widget):
        new_description = entry_widget.get_text().strip()
        self.entry.description = new_description
        self.db.update_entry(self.entry.id, description=new_description)
        self._update_export_button_state()
        if self._desktop_name_source() == 'description':
            self.save_desktop_file()

    def _desktop_name_source(self):
        value = str(self._get_option_value(DESKTOP_NAME_SOURCE_KEY) or 'title').strip().lower()
        if value not in {'title', 'description'}:
            return 'title'
        return value

    def _update_desktop_name_source_buttons(self):
        icons_visible = bool(getattr(self.entry, 'active', False))
        source = self._desktop_name_source()
        icon_states = (
            (getattr(self, 'title_entry', None), source == 'title', t('desktop_name_source_use_name')),
            (getattr(self, 'description_entry', None), source == 'description', t('desktop_name_source_use_description')),
        )
        for entry, selected, tooltip in icon_states:
            if entry is None:
                continue
            entry.set_icon_activatable(Gtk.EntryIconPosition.SECONDARY, icons_visible)
            entry.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, tooltip)
            if not icons_visible:
                entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, None)
                continue
            if selected:
                entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, 'radio-checked-symbolic')
            else:
                entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, 'radio-symbolic')

    def on_desktop_name_source_clicked(self, _button, source):
        normalized = str(source or '').strip().lower()
        if normalized not in {'title', 'description'}:
            return
        if normalized != self._desktop_name_source():
            self._set_option_value(DESKTOP_NAME_SOURCE_KEY, normalized)
            self.save_desktop_file()
        self._update_desktop_name_source_buttons()
        self._update_export_button_state()

    def on_desktop_name_icon_pressed(self, _entry, icon_pos, source):
        if icon_pos != Gtk.EntryIconPosition.SECONDARY:
            return
        self.on_desktop_name_source_clicked(None, source)

    def on_address_changed(self, entry_widget):
        value = self._normalize_address_for_ui(entry_widget.get_text())
        if value != entry_widget.get_text():
            entry_widget.set_text(value)
            return
        self._options_cache[ADDRESS_KEY] = value
        if self._suspend_address_processing:
            self._address_export_after_validation = False
            self._address_last_validated_value = ''
            self.refresh_icon_page()
            self._update_export_button_state()
            return
        if value and not self._looks_ready_for_url_check(value):
            self._address_export_after_validation = False
            self._set_url_status('')
        else:
            self._schedule_address_processing(value, export_after_validation=True)
        if not value:
            self._cancel_address_timers()
            self._address_export_after_validation = False
            self._address_last_validated_value = ''
            self._update_url_status(value)
        self.refresh_icon_page()
        self._update_export_button_state()









    def on_swipe(self, gesture, velocity_x, velocity_y):
        if velocity_x > 0:
            if self.is_subpage_visible():
                self.show_main_page()
            else:
                self.on_back()
    def release_resources(self):
        self._cancel_initial_address_validation()
        self._flush_pending_address_option_write()
        self._cancel_address_timers()
        self._cancel_detail_toast()
        if self._icon_page_preview_refresh_source_id:
            GLib.source_remove(self._icon_page_preview_refresh_source_id)
            self._icon_page_preview_refresh_source_id = 0
        self._plugin_operation_serial += 1
        self._plugin_operation_in_progress = False
        self._set_inline_busy(False)
        self._hide_detail_toast()
        self._icon_texture_cache = {}

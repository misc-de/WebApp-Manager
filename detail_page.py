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
    ICON_PATH_KEY,
    USER_AGENT_NAME_KEY,
    USER_AGENT_VALUE_KEY,
    PROFILE_NAME_KEY,
    PROFILE_PATH_KEY,
    ONLY_HTTPS_KEY,
    OPTION_FORCE_PRIVACY_KEY,
    APP_MODE_KEY,
    COLOR_SCHEME_KEY,
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

LOG = get_logger(__name__)





class DetailPage(Gtk.Box):
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

        self.desktop_tab_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.desktop_tab_bar.set_margin_top(10)
        self.desktop_tab_bar.set_margin_start(12)
        self.desktop_tab_bar.set_margin_end(12)
        self.desktop_tab_bar.set_margin_bottom(10)
        self.desktop_tab_bar.set_halign(Gtk.Align.START)
        self.desktop_tab_bar.set_visible(False)
        self.detail_shell.append(self.desktop_tab_bar)

        self.desktop_tab_buttons = {}
        for tab_name, label_key in (
            ('main', 'detail_tab_basic'),
            ('options', 'detail_tab_options'),
            ('css_assets', 'detail_tab_css'),
            ('javascript_assets', 'detail_tab_javascript'),
        ):
            button = Gtk.ToggleButton(label=t(label_key))
            button.add_css_class('flat')
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
        self.title_label = Gtk.Label(label=t('label_name'), halign=Gtk.Align.START)
        self.grid.attach(self.title_label, 0, 0, 1, 1)
        self.grid.attach(self.title_entry, 1, 0, 1, 1)

        self.description_entry = Gtk.Entry()
        self.description_entry.set_placeholder_text(t('placeholder_description'))
        self.description_entry.set_text(entry.description or '')
        self.description_entry.set_hexpand(True)
        self.description_entry.connect('changed', self.on_description_changed)
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
        self.engine_dropdown.connect('notify::selected-item', self.on_engine_changed)
        self.engine_label = Gtk.Label(label=t('label_engine'), halign=Gtk.Align.START)
        self.grid.attach(self.engine_label, 0, 5, 1, 1)
        self.grid.attach(self.engine_dropdown, 1, 5, 1, 1)

        self.user_agent_dropdown = Gtk.DropDown.new_from_strings([t('user_agent_none')])
        self.user_agent_dropdown.connect('notify::selected-item', self.on_user_agent_changed)
        self.user_agent_status = Gtk.Label(label='', halign=Gtk.Align.START)
        self.user_agent_status.set_xalign(0)
        self.user_agent_status.add_css_class('dim-label')

        self.browser_option_status = Gtk.Label(label='', halign=Gtk.Align.START)
        self.browser_option_status.set_xalign(0)
        self.browser_option_status.add_css_class('dim-label')
        self.user_agent_label = Gtk.Label(label=t('label_user_agent'), halign=Gtk.Align.START)
        self.grid.attach(self.user_agent_label, 0, 6, 1, 1)
        self.grid.attach(self.user_agent_dropdown, 1, 6, 1, 1)
        self.refresh_user_agent_options()

        self.mode_labels = []
        self.mode_values = []
        self.mode_dropdown = Gtk.DropDown.new_from_strings([t('mode_standard')])
        self.mode_dropdown.connect('notify::selected-item', self.on_mode_changed)
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
        self.color_scheme_dropdown.connect('notify::selected-item', self.on_color_scheme_changed)
        self.color_scheme_label = Gtk.Label(label=t('label_color_scheme'), halign=Gtk.Align.START)
        self.grid.attach(self.color_scheme_label, 0, 8, 1, 1)
        self.grid.attach(self.color_scheme_dropdown, 1, 8, 1, 1)

        self.color_scheme_spacer = Gtk.Box()
        self.color_scheme_spacer.set_size_request(-1, 8)
        self.grid.attach(self.color_scheme_spacer, 0, 9, 2, 1)

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
        self.add_js_button.connect('clicked', lambda _button: self.show_asset_page('javascript'))
        self.custom_assets_row.append(self.add_js_button)

        self.content_box.append(self.custom_assets_row)
        self._desktop_tabs_syncing = False

        self.export_import_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.export_import_row.set_homogeneous(True)
        self.export_import_row.set_margin_top(6)

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

        self.delete_button = Gtk.Button(label=t('delete_webapp_button'))
        self.delete_button.set_hexpand(True)
        self.delete_button.add_css_class('destructive-action')
        self.delete_button.set_margin_top(6)
        self.delete_button.connect('clicked', self.on_delete_clicked)
        self.content_box.append(self.delete_button)

        self._engine_option_widgets = [
            self.user_agent_label, self.user_agent_dropdown, self.mode_label, self.mode_dropdown,
            self.color_scheme_label, self.color_scheme_dropdown, self.color_scheme_spacer,
            self.options_section, self.export_import_row, self.delete_profile_button,
        ]
        self._option_row_widgets = {}

        self._icon_page_buttons = []
        self._build_icon_page()
        self._asset_page_state = {}
        self._build_asset_page('css')
        self._build_asset_page('javascript')
        self.refresh_icon_preview()
        self._update_export_button_state()
        self._update_browser_dependent_controls()
        self.page_stack.set_visible_child_name('main')
        self.connect('notify::width', self._on_layout_width_changed)
        self.page_stack.connect('notify::width', self._on_layout_width_changed)
        self.grid.connect('notify::width', self._on_layout_width_changed)
        GLib.idle_add(self._finish_initial_detail_setup)

    def _adaptive_wrap_page(self, child, maximum_size=820, tightening_threshold=560):
        return child

    def _effective_layout_width(self):
        page_stack = getattr(self, 'page_stack', None)
        grid = getattr(self, 'grid', None)
        return max(
            int(self.get_width() or 0),
            int(page_stack.get_width() or 0) if page_stack is not None else 0,
            int(grid.get_width() or 0) if grid is not None else 0,
        )

    def set_compact_mode_override(self, enabled=None):
        override = None if enabled is None else bool(enabled)
        if self._compact_mode_override == override:
            return
        self._compact_mode_override = override
        self._apply_adaptive_layout(force=True)
        self._update_tabbed_navigation_state()

    def _is_compact_layout(self):
        if self._compact_mode_override is not None:
            return self._compact_mode_override
        width = self._effective_layout_width()
        return width > 0 and width < 620

    def _current_page_name(self):
        try:
            return self.page_stack.get_visible_child_name() or 'main'
        except (AttributeError, TypeError):
            return 'main'

    def _desktop_tab_target(self, page_name=None):
        current = page_name or self._current_page_name()
        return current if current in {'main', 'options', 'css_assets', 'javascript_assets'} else 'main'

    def _sync_desktop_tab_buttons(self, page_name=None):
        target = self._desktop_tab_target(page_name)
        self._desktop_tabs_syncing = True
        try:
            for name, button in self.desktop_tab_buttons.items():
                button.set_active(name == target)
        finally:
            self._desktop_tabs_syncing = False

    def _on_desktop_tab_toggled(self, button, page_name):
        if self._desktop_tabs_syncing or not button.get_active():
            return
        self.page_stack.set_visible_child_name(page_name)
        self._sync_desktop_tab_buttons(page_name)
        self._update_tabbed_navigation_state()

    def _move_widget_to_box(self, widget, box):
        if widget is None or box is None:
            return
        parent = widget.get_parent()
        if parent is box:
            return
        if parent is not None:
            try:
                parent.remove(widget)
            except Exception:
                pass
        box.append(widget)

    def _mount_options_section(self, compact, force=False):
        if not force and self._options_section_compact == compact:
            return
        self._options_section_compact = compact
        target_box = self.main_options_slot if compact else self.options_page_content
        self._move_widget_to_box(self.options_section, target_box)
        if compact and self._current_page_name() == 'options':
            self.page_stack.set_visible_child_name('main')

    def _clear_grid(self):
        child = self.grid.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.grid.remove(child)
            child = next_child

    def _rebuild_form_layout(self, force=False):
        compact = self._is_compact_layout()
        if not force and getattr(self, '_form_compact', None) == compact:
            return
        self._form_compact = compact
        self._clear_grid()
        self.grid.set_margin_top(22 if not compact else 16)
        self.grid.set_column_spacing(10 if not compact else 8)
        self.grid.set_row_spacing(8 if not compact else 8)

        fields = [
            (self.title_label, self.title_entry),
            (self.description_label, self.description_entry),
            (self.address_label, self.address_entry),
            (self.engine_label, self.engine_dropdown),
            (self.user_agent_label, self.user_agent_dropdown),
            (self.mode_label, self.mode_dropdown),
            (self.color_scheme_label, self.color_scheme_dropdown),
        ]
        row = 0
        for label, widget in fields:
            label.set_wrap(False)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            label.set_valign(Gtk.Align.CENTER)
            label.set_hexpand(False)
            widget.set_hexpand(True)
            self.grid.attach(label, 0, row, 1, 1)
            self.grid.attach(widget, 1, row, 1, 1)
            row += 1
            if widget is self.address_entry:
                self.url_status_label.set_margin_start(0 if compact else 10)
                self.grid.attach(self.url_status_label, 1, row, 1, 1)
                row += 1
                self.grid.attach(self.engine_spacer, 0, row, 2, 1)
                row += 1

    def _subpage_side_inset(self, compact):
        return 0

    def _apply_subpage_adaptive_layout(self, force=False):
        compact = self._is_compact_layout()
        if not force and self._subpage_compact == compact:
            return
        self._subpage_compact = compact

        vertical_margin = 8 if compact else 12
        side_margin = 20 if compact else 12
        inner_margin = 8 if compact else 12
        side_inset = self._subpage_side_inset(compact)

        self.content_box.set_margin_top(vertical_margin)
        self.content_box.set_margin_bottom(vertical_margin)
        self.content_box.set_margin_start(side_margin)
        self.content_box.set_margin_end(side_margin)

        self.options_page.set_margin_top(vertical_margin)
        self.options_page.set_margin_bottom(vertical_margin)
        self.options_page.set_margin_start(side_margin)
        self.options_page.set_margin_end(side_margin)
        self.options_page_content.set_margin_top(inner_margin)
        self.options_page_content.set_margin_bottom(inner_margin)
        self.options_page_content.set_margin_start(side_inset)
        self.options_page_content.set_margin_end(side_inset)

        self.icon_page.set_margin_top(vertical_margin)
        self.icon_page.set_margin_bottom(vertical_margin)
        self.icon_page.set_margin_start(side_margin)
        self.icon_page.set_margin_end(side_margin)
        self.icon_page_content.set_margin_top(inner_margin)
        self.icon_page_content.set_margin_bottom(inner_margin)
        self.icon_page_content.set_margin_start(side_inset)
        self.icon_page_content.set_margin_end(side_inset)
        self.icon_page_content.set_spacing(8 if compact else 4)
        self.icon_page_progress_box.set_margin_top(8 if compact else 10)
        self.icon_page_progress_box.set_margin_bottom(10 if compact else 12)
        self.icon_page_preview_frame.set_size_request(80 if compact else 92, 80 if compact else 92)
        self.icon_page_preview_canvas.set_size_request(80 if compact else 92, 80 if compact else 92)
        for button in self._icon_page_buttons:
            button.set_hexpand(compact)
            button.set_halign(Gtk.Align.FILL if compact else Gtk.Align.CENTER)

        for state in getattr(self, '_asset_page_state', {}).values():
            page = state['page']
            content = state['content']
            selector_row = state['selector_row']
            add_button = state['add_button']
            page.set_margin_top(vertical_margin)
            page.set_margin_bottom(vertical_margin)
            page.set_margin_start(side_margin)
            page.set_margin_end(side_margin)
            content.set_margin_top(inner_margin)
            content.set_margin_bottom(inner_margin)
            content.set_margin_start(side_inset)
            content.set_margin_end(side_inset)
            content.set_spacing(8 if compact else 10)
            selector_row.set_orientation(Gtk.Orientation.VERTICAL if compact else Gtk.Orientation.HORIZONTAL)
            selector_row.set_spacing(8)
            add_button.set_hexpand(compact)
            add_button.set_halign(Gtk.Align.FILL if compact else Gtk.Align.START)
            inline_scrolled = state.get('inline_scrolled')
            if inline_scrolled is not None:
                inline_scrolled.set_min_content_height(150 if compact else 220)
            inline_buffer = state.get('inline_buffer')
            if inline_buffer is not None:
                for editor in getattr(self, '_code_editors', []):
                    if editor.get('buffer') is inline_buffer:
                        self._sync_code_editor_line_number_visibility(editor)
                        break

    def _apply_adaptive_layout(self, force=False):
        compact = self._is_compact_layout()
        if force or getattr(self, '_top_row_compact', None) != compact:
            self._top_row_compact = compact
            self.top_row.set_orientation(Gtk.Orientation.HORIZONTAL)
            self.top_row.set_spacing(10 if compact else 12)
            self.top_row.set_halign(Gtk.Align.FILL)
            self.top_row.set_hexpand(True)
            self.header_main_row.set_spacing(10 if compact else 12)
            self.header_main_row.set_hexpand(True)
            self.header_main_row.set_halign(Gtk.Align.FILL)
            self.icon_button.set_size_request(64 if compact else 72, 64 if compact else 72)
            self.icon_button.set_valign(Gtk.Align.START)
            self.title_meta_box.set_valign(Gtk.Align.START)
            self.title_meta_box.set_margin_bottom(2 if compact else 0)
            self.header_name_label.set_max_width_chars(36 if compact else 28)
            self.header_profile_label.set_max_width_chars(36 if compact else 28)
            self.header_name_label.set_wrap(False)
            self.header_profile_label.set_wrap(False)
            self.header_name_label.set_valign(Gtk.Align.START)
            self.header_profile_label.set_valign(Gtk.Align.START)
            self.switch_box.set_halign(Gtk.Align.END)
            self.switch_box.set_hexpand(False)
            self.switch_box.set_valign(Gtk.Align.END)
            self.switch_box.set_margin_top(0)
            self.switch_box.set_margin_bottom(2 if compact else 0)
        if force or getattr(self, '_action_row_compact', None) != compact:
            self._action_row_compact = compact
            self.custom_assets_row.set_orientation(Gtk.Orientation.HORIZONTAL)
            self.custom_assets_row.set_spacing(0)
            self.custom_assets_row.set_homogeneous(True)
            self.export_import_row.set_orientation(Gtk.Orientation.VERTICAL if compact else Gtk.Orientation.HORIZONTAL)
            self.export_import_row.set_spacing(8 if compact else 0)
            self.export_import_row.set_homogeneous(not compact)
        self._mount_options_section(compact, force=force)
        self.custom_assets_row.set_visible(compact)
        self.desktop_tab_bar.set_visible(not compact and self._current_page_name() != 'icon')
        self._sync_desktop_tab_buttons()
        self._apply_subpage_adaptive_layout(force=force)
        self._rebuild_form_layout(force=force)
        self._rebuild_options_layout(force=force)

    def on_delete_clicked(self, button):
        self._present_choice_dialog(
            button,
            t('delete_webapp_confirm', title=self.entry.title),
            lambda confirmed: self.on_delete_callback(self.entry) if confirmed else None,
            destructive=True,
        )

    def _option_names_in_order(self):
        return list(self.option_names)

    def _ui_boolean_option_active(self, option_name):
        raw_value = self._get_option_value(option_name) == '1'
        if option_name == OPTION_DISABLE_AI_KEY:
            return not raw_value
        return raw_value

    def _store_boolean_option_value(self, option_name, active):
        if option_name == OPTION_DISABLE_AI_KEY:
            return '0' if active else '1'
        return '1' if active else '0'

    def _create_option_switch(self, option_name):
        switch = Gtk.Switch()
        switch.add_css_class('boolean-switch')
        switch.set_halign(Gtk.Align.START)
        switch.set_valign(Gtk.Align.START)
        switch.set_hexpand(False)
        value = self._get_option_value(option_name)
        if value is not None:
            switch.set_active(self._ui_boolean_option_active(option_name))
        elif option_name == OPTION_KEEP_IN_BACKGROUND_KEY:
            switch.set_active(False)
        elif option_name == OPTION_DISABLE_AI_KEY:
            switch.set_active(True)
        switch.connect('notify::active', lambda s, pspec, name=option_name: self.save_boolean_option(name, s.get_active()))
        self.switches[option_name] = switch
        return switch

    def _clear_options_container(self):
        child = self.options_container.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.options_container.remove(child)
            child = next_child
        self.switches = {}

    def _visible_option_names_in_order(self):
        grouped = self._grouped_visible_option_names()
        ordered = []
        for _, option_names in grouped:
            ordered.extend(option_names)
        return ordered

    def _grouped_visible_option_names(self):
        engine = self._get_current_engine()
        supported_names = self._supported_option_names(engine)
        supported = [name for name in self.option_names if name in supported_names]

        def sort_key(option_name):
            return option_ui_label(option_name).casefold()

        grouped = {category: [] for category in OPTION_CATEGORY_ORDER}
        for option_name in supported:
            grouped.setdefault(option_category(option_name), []).append(option_name)
        ordered_groups = []
        for category in OPTION_CATEGORY_ORDER:
            names = sorted(grouped.get(category, []), key=sort_key)
            if names:
                ordered_groups.append((category, names))
        return ordered_groups

    def _build_options_layout(self, compact=False):
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8 if compact else 6)
        column.set_hexpand(True)
        grouped_option_names = self._grouped_visible_option_names()
        if compact:
            top_spacer = Gtk.Box()
            top_spacer.set_size_request(-1, 18)
            top_spacer.set_hexpand(True)
            column.append(top_spacer)
        first_group = True
        for category_name, option_names in grouped_option_names:
            if not first_group:
                spacer = Gtk.Box()
                spacer.set_size_request(-1, 18)
                spacer.set_hexpand(True)
                column.append(spacer)
            header = Gtk.Label(label=t(OPTION_CATEGORY_LABEL_KEYS.get(category_name, '')), halign=Gtk.Align.START)
            header.set_xalign(0)
            header.set_hexpand(True)
            header.add_css_class('heading')
            header.add_css_class('dim-label')
            column.append(header)
            for option_name in option_names:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8 if compact else 10)
                row.set_hexpand(True)
                row.set_halign(Gtk.Align.FILL)
                row.set_valign(Gtk.Align.CENTER)
                row.set_margin_top(0)
                row.set_margin_bottom(0)
                row.set_margin_start(0)
                row.set_margin_end(0)
                try:
                    row.add_css_class('option-row')
                except Exception:
                    pass
                label = Gtk.Label(halign=Gtk.Align.START)
                label.set_use_markup(True)
                label.set_markup(option_ui_label_markup(option_name))
                label.set_valign(Gtk.Align.CENTER)
                label.set_wrap(False)
                label.set_ellipsize(Pango.EllipsizeMode.END)
                label.set_hexpand(True)
                switch_wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
                switch_wrap.set_hexpand(False)
                switch_wrap.set_halign(Gtk.Align.END)
                switch_wrap.set_valign(Gtk.Align.CENTER)
                switch_wrap.append(self._create_option_switch(option_name))
                row.append(label)
                row.append(switch_wrap)
                controller = Gtk.EventControllerMotion()
                controller.connect('enter', lambda ctrl, x, y, current_row=row: current_row.add_css_class('option-row-hover'))
                controller.connect('leave', lambda ctrl, current_row=row: current_row.remove_css_class('option-row-hover'))
                row.add_controller(controller)
                column.append(row)
                self._option_row_widgets[option_name] = [label, switch_wrap, self.switches[option_name], row]
            first_group = False
        return column

    def _apply_boolean_switch_values(self):
        previous_suspend = self._suspend_change_handlers
        self._suspend_change_handlers = True
        try:
            for option_name, switch in list(self.switches.items()):
                switch.set_active(self._ui_boolean_option_active(option_name))
        finally:
            self._suspend_change_handlers = previous_suspend

    def _rebuild_options_layout(self, force=False):
        compact = self._is_compact_layout()
        if not force and self._options_compact == compact:
            return
        self._options_compact = compact
        self._clear_options_container()
        self.options_container.append(self._build_options_layout(compact=compact))
        self._apply_boolean_switch_values()
        self._update_export_button_state()

    def _queue_options_layout_rebuild(self):
        if self._options_rebuild_source_id:
            return

        def run_rebuild():
            self._options_rebuild_source_id = 0
            self._apply_adaptive_layout(force=False)
            return False

        self._options_rebuild_source_id = GLib.timeout_add(60, run_rebuild)

    def _on_layout_width_changed(self, *args):
        self._queue_options_layout_rebuild()

    def _finish_initial_detail_setup(self):
        self._reload_options_cache_from_db()
        self._apply_adaptive_layout(force=True)
        self._apply_option_values_to_controls()
        self._update_tabbed_navigation_state()
        self._schedule_mobile_focus_reset()
        self._suspend_change_handlers = False
        return False

    def _current_mode_value(self):
        if self._get_option_value('Kiosk') == '1':
            return 'kiosk'
        if self._get_option_value(APP_MODE_KEY) == '1' and self._get_option_value('Frameless') == '1':
            return 'seamless'
        if self._get_option_value(APP_MODE_KEY) == '1':
            return 'app'
        return 'standard'

    def _current_mode_index(self):
        value = self._current_mode_value()
        try:
            return self.mode_values.index(value)
        except ValueError:
            return 0

    def _apply_mode_value(self, mode_value):
        mapping = {
            'standard': {'Kiosk': '0', APP_MODE_KEY: '0', 'Frameless': '0'},
            'kiosk': {'Kiosk': '1', APP_MODE_KEY: '0', 'Frameless': '0'},
            'app': {'Kiosk': '0', APP_MODE_KEY: '1', 'Frameless': '0'},
            'seamless': {'Kiosk': '0', APP_MODE_KEY: '1', 'Frameless': '1'},
        }
        selected = mapping.get(mode_value, mapping['standard'])
        for key, value in selected.items():
            self._set_option_value(key, value)

    def on_mode_changed(self, dropdown, pspec):
        if self._suspend_change_handlers:
            return
        index = dropdown.get_selected()
        try:
            mode_value = self.mode_values[index]
        except (IndexError, TypeError):
            mode_value = 'standard'
        self._apply_mode_value(mode_value)
        self.save_desktop_file()

    def _has_custom_icon(self):
        icon_ref = (self._icon_path() or '').strip()
        if not icon_ref:
            return False
        if '/' not in icon_ref and '\\' not in icon_ref:
            return icon_ref != 'applications-internet'
        return Path(icon_ref).exists()

    def _maybe_autofetch_icon(self, value):
        if not value or self._has_custom_icon() or self._auto_icon_fetch_url == value:
            return
        self._auto_icon_fetch_url = value

        def worker():
            try:
                path = self._download_favicon(value)
                if path and Path(path).exists():
                    GLib.idle_add(self._apply_downloaded_icon_silent, str(path), value)
                    return
            except (OSError, ValueError, urllib.error.URLError) as error:
                LOG.debug('Automatic icon fetch failed for %s: %s', value, error)
            GLib.idle_add(self._reset_auto_icon_fetch, value)

        threading.Thread(target=worker, daemon=True).start()

    def _reset_auto_icon_fetch(self, value=''):
        if not value or self._auto_icon_fetch_url == value:
            self._auto_icon_fetch_url = ''
        return False

    def _apply_downloaded_icon_silent(self, path, value):
        temp_path = Path(path)
        try:
            if self._has_custom_icon():
                return False
            target_path = self._managed_icon_target()
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(temp_path), str(target_path))
            self._apply_icon_path(target_path)
        finally:
            temp_path.unlink(missing_ok=True)
            self._reset_auto_icon_fetch(value)
        return False

    def _build_icon_page(self):
        self.icon_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.icon_page.set_margin_top(12)
        self.icon_page.set_margin_bottom(12)
        self.icon_page.set_margin_start(12)
        self.icon_page.set_margin_end(12)
        self.icon_page.set_valign(Gtk.Align.START)
        self.icon_page.set_vexpand(False)
        self.icon_page.set_hexpand(True)

        self.icon_page_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.icon_page_content.set_halign(Gtk.Align.FILL)
        self.icon_page_content.set_margin_top(12)
        self.icon_page_content.set_margin_bottom(12)
        self.icon_page_content.set_valign(Gtk.Align.START)
        self.icon_page_content.set_vexpand(False)
        self.icon_page_content.set_hexpand(True)
        self.icon_page.append(self.icon_page_content)

        self.icon_page_preview_frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.icon_page_preview_frame.add_css_class('icon-preview-frame')
        self.icon_page_preview_frame.set_halign(Gtk.Align.CENTER)
        self.icon_page_preview_frame.set_valign(Gtk.Align.START)
        self.icon_page_preview_frame.set_hexpand(False)
        self.icon_page_preview_frame.set_vexpand(False)
        self.icon_page_preview_frame.set_size_request(92, 92)
        self.icon_page_preview_frame.set_overflow(Gtk.Overflow.HIDDEN)
        self.icon_page_preview_frame.set_margin_bottom(2)
        self.icon_page_preview_canvas = Gtk.Fixed()
        self.icon_page_preview_canvas.set_size_request(92, 92)
        self.icon_page_preview_canvas.set_hexpand(False)
        self.icon_page_preview_canvas.set_vexpand(False)
        self.icon_page_preview_frame.append(self.icon_page_preview_canvas)
        self.icon_page_content.append(self.icon_page_preview_frame)

        self.icon_page_progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.icon_page_progress_box.set_halign(Gtk.Align.CENTER)
        self.icon_page_progress_box.set_valign(Gtk.Align.START)
        self.icon_page_progress_box.set_hexpand(True)
        self.icon_page_progress_box.set_vexpand(False)
        self.icon_page_progress_box.set_margin_top(10)
        self.icon_page_progress_box.set_margin_bottom(12)
        self.icon_page_content.append(self.icon_page_progress_box)

        self.icon_page_search_spinner = Gtk.Spinner()
        self.icon_page_search_spinner.set_size_request(28, 28)
        self.icon_page_search_spinner.set_halign(Gtk.Align.CENTER)
        self.icon_page_search_spinner.set_valign(Gtk.Align.START)
        self.icon_page_search_spinner.set_visible(False)
        self.icon_page_progress_box.append(self.icon_page_search_spinner)

        self.icon_page_status = Gtk.Label(label='', halign=Gtk.Align.CENTER)
        self.icon_page_status.set_vexpand(False)
        self.icon_page_status.set_xalign(0.5)
        self.icon_page_status.set_justify(Gtk.Justification.CENTER)
        self.icon_page_status.set_wrap(True)
        self.icon_page_status.add_css_class('dim-label')
        self.icon_page_status.set_margin_top(0)
        self.icon_page_status.set_margin_bottom(0)
        self.icon_page_progress_box.append(self.icon_page_status)

        self.icon_download_button = Gtk.Button(label=t('icon_action_download'))
        self.icon_download_button.set_margin_top(0)
        self.icon_download_button.set_vexpand(False)
        self.icon_download_button.connect('clicked', self.on_icon_download_clicked)
        self.icon_page_content.append(self.icon_download_button)

        self.icon_upload_button = Gtk.Button(label=t('icon_action_upload'))
        self.icon_upload_button.set_vexpand(False)
        self.icon_upload_button.connect('clicked', self.on_icon_upload_clicked)
        self.icon_page_content.append(self.icon_upload_button)

        self.icon_delete_button = Gtk.Button(label=t('icon_action_delete'))
        self.icon_delete_button.set_vexpand(False)
        self.icon_delete_button.add_css_class('destructive-action')
        self.icon_delete_button.connect('clicked', self.on_icon_delete_clicked)
        self.icon_page_content.append(self.icon_delete_button)
        self._icon_page_buttons = [self.icon_download_button, self.icon_upload_button, self.icon_delete_button]

        self.page_stack.add_named(self._adaptive_wrap_page(self.icon_page), 'icon')

    def _asset_option_key(self, asset_type):
        return ASSET_OPTION_KEY_BY_TYPE['css' if asset_type == 'css' else 'javascript']

    def _linked_asset_ids(self, asset_type):
        key = self._asset_option_key(asset_type)
        return normalize_linked_asset_ids(self._get_option_value(key), asset_type=asset_type)

    def _linked_assets(self, asset_type):
        items = []
        for asset_id in self._linked_asset_ids(asset_type):
            asset = get_custom_asset(asset_id)
            if asset is not None:
                items.append(asset)
        return items

    def _set_linked_assets(self, asset_type, asset_ids):
        key = self._asset_option_key(asset_type)
        encoded = encode_linked_asset_ids(asset_ids, asset_type=asset_type)
        self._set_option_value(key, encoded)
        self._refresh_asset_page(asset_type)
        self.save_desktop_file()

    def _inline_asset_option_key(self, asset_type):
        return INLINE_CUSTOM_CSS_KEY if asset_type == 'css' else INLINE_CUSTOM_JS_KEY

    def _inline_asset_hash_option_key(self, asset_type):
        return INLINE_CUSTOM_CSS_HASH_KEY if asset_type == 'css' else INLINE_CUSTOM_JS_HASH_KEY

    def _get_inline_asset_text(self, asset_type):
        return (self._get_option_value(self._inline_asset_option_key(asset_type)) or '').replace('\r\n', '\n').replace('\r', '\n')

    def _get_buffer_text(self, buffer):
        start_iter = buffer.get_start_iter()
        end_iter = buffer.get_end_iter()
        return buffer.get_text(start_iter, end_iter, True)

    def _set_buffer_text_if_needed(self, buffer, text):
        current = self._get_buffer_text(buffer)
        if current == text:
            return
        previous_suspend = self._suspend_change_handlers
        self._suspend_change_handlers = True
        try:
            buffer.set_text(text)
        finally:
            self._suspend_change_handlers = previous_suspend
        for editor in getattr(self, '_code_editors', []):
            if editor.get('buffer') is buffer:
                self._update_code_editor_line_numbers(editor)
                break

    def _on_style_manager_dark_changed(self, *_args):
        self._apply_code_editor_theme()

    def _source_style_scheme_name(self):
        if GtkSource is None:
            return None
        try:
            manager = GtkSource.StyleSchemeManager.get_default()
        except Exception:
            return None
        if manager is None:
            return None
        preferred = [
            'Adwaita-dark', 'adwaita-dark', 'oblivion', 'solarized-dark',
        ] if self._style_manager is not None and self._style_manager.get_dark() else [
            'Adwaita', 'adwaita', 'classic', 'solarized-light',
        ]
        for name in preferred:
            try:
                scheme = manager.get_scheme(name)
            except Exception:
                scheme = None
            if scheme is not None:
                return name
        try:
            scheme_ids = manager.get_scheme_ids()
        except Exception:
            scheme_ids = []
        return scheme_ids[0] if scheme_ids else None

    def _apply_code_editor_theme(self):
        use_dark = bool(self._style_manager is not None and self._style_manager.get_dark())
        scheme_name = self._source_style_scheme_name()
        for editor in getattr(self, '_code_editors', []):
            view = editor.get('view')
            buffer = editor.get('buffer')
            scrolled = editor.get('scrolled')
            line_number_view = editor.get('line_number_view')
            line_number_scrolled = editor.get('line_number_scrolled')
            if view is None or buffer is None or scrolled is None:
                continue
            for widget in (view, scrolled, line_number_view, line_number_scrolled):
                if widget is None:
                    continue
                try:
                    widget.remove_css_class('inline-editor-dark')
                    widget.remove_css_class('inline-editor-light')
                except (AttributeError, TypeError):
                    pass
                try:
                    widget.add_css_class('inline-editor-dark' if use_dark else 'inline-editor-light')
                except (AttributeError, TypeError):
                    pass
            if GtkSource is not None and hasattr(buffer, 'set_style_scheme') and scheme_name:
                try:
                    manager = GtkSource.StyleSchemeManager.get_default()
                    scheme = manager.get_scheme(scheme_name) if manager is not None else None
                    buffer.set_style_scheme(scheme)
                except (AttributeError, TypeError):
                    pass

    def _buffer_line_count(self, buffer):
        text = self._get_buffer_text(buffer)
        return 1 if not text else text.count('\n') + 1

    def _sync_code_editor_line_number_visibility(self, editor):
        line_number_scrolled = editor.get('line_number_scrolled')
        view = editor.get('view')
        uses_source_view = bool(editor.get('uses_source_view'))
        show_compact_gutter = self._is_compact_layout() or not uses_source_view
        if line_number_scrolled is not None:
            line_number_scrolled.set_visible(show_compact_gutter)
        if uses_source_view and view is not None and hasattr(view, 'set_show_line_numbers'):
            try:
                view.set_show_line_numbers(not show_compact_gutter)
            except (AttributeError, TypeError):
                pass

    def _update_code_editor_line_numbers(self, editor):
        buffer = editor.get('buffer')
        line_number_buffer = editor.get('line_number_buffer')
        if buffer is None or line_number_buffer is None:
            return
        line_total = self._buffer_line_count(buffer)
        line_number_buffer.set_text('\n'.join(str(index) for index in range(1, line_total + 1)))
        self._sync_code_editor_line_number_visibility(editor)

    def _register_code_editor(self, asset_type, view, buffer, scrolled, line_number_view, line_number_buffer, line_number_scrolled, uses_source_view):
        editor = {
            'asset_type': asset_type,
            'view': view,
            'buffer': buffer,
            'scrolled': scrolled,
            'line_number_view': line_number_view,
            'line_number_buffer': line_number_buffer,
            'line_number_scrolled': line_number_scrolled,
            'uses_source_view': uses_source_view,
        }
        self._code_editors.append(editor)
        self._update_code_editor_line_numbers(editor)
        self._apply_code_editor_theme()
        return editor

    def _build_code_editor(self, asset_type):
        uses_source_view = GtkSource is not None
        if uses_source_view:
            buffer = GtkSource.Buffer()
            try:
                language_manager = GtkSource.LanguageManager.get_default()
                language = language_manager.get_language('css' if asset_type == 'css' else 'js')
                if language is None and asset_type == 'javascript':
                    language = language_manager.get_language('javascript')
                if language is not None:
                    buffer.set_language(language)
            except (AttributeError, TypeError):
                pass
            view = GtkSource.View.new_with_buffer(buffer)
            try:
                view.set_show_line_numbers(True)
                view.set_highlight_current_line(False)
                view.set_auto_indent(True)
                view.set_tab_width(2)
                view.set_insert_spaces_instead_of_tabs(True)
            except (AttributeError, TypeError):
                pass
        else:
            view = Gtk.TextView()
            buffer = view.get_buffer()
        view.set_monospace(True)
        view.set_wrap_mode(Gtk.WrapMode.NONE)
        view.set_top_margin(8)
        view.set_bottom_margin(8)
        view.set_left_margin(8)
        view.set_right_margin(8)
        view.set_hexpand(True)
        view.set_vexpand(True)
        try:
            view.add_css_class('inline-code-editor')
        except (AttributeError, TypeError):
            pass

        line_number_view = Gtk.TextView()
        line_number_view.set_editable(False)
        line_number_view.set_cursor_visible(False)
        line_number_view.set_focusable(False)
        line_number_view.set_can_target(False)
        line_number_view.set_monospace(True)
        line_number_view.set_wrap_mode(Gtk.WrapMode.NONE)
        line_number_view.set_justification(Gtk.Justification.RIGHT)
        line_number_view.set_top_margin(8)
        line_number_view.set_bottom_margin(8)
        line_number_view.set_left_margin(6)
        line_number_view.set_right_margin(6)
        try:
            line_number_view.add_css_class('inline-code-line-numbers')
        except (AttributeError, TypeError):
            pass
        line_number_buffer = line_number_view.get_buffer()

        line_number_scrolled = Gtk.ScrolledWindow()
        line_number_scrolled.set_hexpand(False)
        line_number_scrolled.set_vexpand(False)
        line_number_scrolled.set_min_content_width(52)
        line_number_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
        line_number_scrolled.add_css_class('inline-code-line-number-rail')
        line_number_scrolled.set_child(line_number_view)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(False)
        scrolled.set_min_content_height(180)
        scrolled.add_css_class('card')
        scrolled.add_css_class('inline-code-editor-frame')
        scrolled.set_child(view)

        shared_adjustment = scrolled.get_vadjustment()
        if shared_adjustment is not None:
            line_number_scrolled.set_vadjustment(shared_adjustment)

        editor_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        editor_box.set_hexpand(True)
        editor_box.append(line_number_scrolled)
        editor_box.append(scrolled)

        editor = self._register_code_editor(asset_type, view, buffer, scrolled, line_number_view, line_number_buffer, line_number_scrolled, uses_source_view)
        buffer.connect('changed', lambda buf, current_type=asset_type, current_editor=editor: self._on_inline_editor_changed(current_type, buf, current_editor))
        return editor_box, scrolled, view, buffer

    def _on_inline_editor_changed(self, asset_type, buffer, editor=None):
        if editor is not None:
            self._update_code_editor_line_numbers(editor)
        if self._suspend_change_handlers:
            return
        source_id = self._inline_editor_save_source_ids.get(asset_type, 0)
        if source_id:
            GLib.source_remove(source_id)

        def flush_changes():
            self._inline_editor_save_source_ids[asset_type] = 0
            self._persist_inline_asset_text(asset_type)
            return False

        self._inline_editor_save_source_ids[asset_type] = GLib.timeout_add(450, flush_changes)

    def _persist_inline_asset_text(self, asset_type):
        state = getattr(self, '_asset_page_state', {}).get(asset_type)
        if not state:
            return
        buffer = state.get('inline_buffer')
        if buffer is None:
            return
        text_value = self._get_buffer_text(buffer).replace('\r\n', '\n').replace('\r', '\n')
        if not text_value.strip():
            text_value = ''
        key = self._inline_asset_option_key(asset_type)
        hash_key = self._inline_asset_hash_option_key(asset_type)
        text_hash = asset_content_sha256_from_text(text_value)
        if (self._get_option_value(key) or '') == text_value and (self._get_option_value(hash_key) or '') == text_hash:
            return
        self._set_option_value(key, text_value, commit=False)
        self._set_option_value(hash_key, text_hash, commit=False)
        self.save_desktop_file()

    def _build_asset_page(self, asset_type):
        page_name = 'css_assets' if asset_type == 'css' else 'javascript_assets'
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        page.set_margin_top(12)
        page.set_margin_bottom(12)
        page.set_margin_start(12)
        page.set_margin_end(12)
        page.set_valign(Gtk.Align.START)
        page.set_vexpand(False)
        page.set_hexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_halign(Gtk.Align.FILL)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        page.append(content)

        title = Gtk.Label(label=t('detail_asset_page_title_css' if asset_type == 'css' else 'detail_asset_page_title_javascript'))
        title.add_css_class('heading')
        title.set_xalign(0)
        content.append(title)

        hint = Gtk.Label(label=t('detail_asset_page_hint_css' if asset_type == 'css' else 'detail_asset_page_hint_javascript'))
        hint.set_xalign(0)
        hint.set_wrap(True)
        hint.add_css_class('dim-label')
        content.append(hint)

        selector_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        selector_row.set_hexpand(True)
        content.append(selector_row)

        dropdown = Gtk.DropDown.new_from_strings([t('detail_asset_dropdown_none')])
        dropdown.set_hexpand(True)
        selector_row.append(dropdown)

        add_button = Gtk.Button(label=t('detail_asset_add_selected_css' if asset_type == 'css' else 'detail_asset_add_selected_javascript'))
        add_button.connect('clicked', lambda _button, current_type=asset_type: self._add_selected_asset(current_type))
        selector_row.append(add_button)

        current_header = Gtk.Label(label=t('detail_asset_linked_css' if asset_type == 'css' else 'detail_asset_linked_javascript'))
        current_header.add_css_class('heading')
        current_header.set_xalign(0)
        current_header.set_margin_top(4)
        content.append(current_header)

        empty_label = Gtk.Label(label=t('detail_asset_empty_css' if asset_type == 'css' else 'detail_asset_empty_javascript'))
        empty_label.set_xalign(0)
        empty_label.add_css_class('dim-label')
        empty_label.set_wrap(True)
        content.append(empty_label)

        selected_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.append(selected_list)

        inline_header = Gtk.Label(label=t('detail_asset_inline_css' if asset_type == 'css' else 'detail_asset_inline_javascript'))
        inline_header.add_css_class('heading')
        inline_header.set_xalign(0)
        inline_header.set_margin_top(8)
        content.append(inline_header)

        inline_hint = Gtk.Label(label=t('detail_asset_inline_hint_css' if asset_type == 'css' else 'detail_asset_inline_hint_javascript'))
        inline_hint.set_xalign(0)
        inline_hint.set_wrap(True)
        inline_hint.add_css_class('dim-label')
        content.append(inline_hint)

        inline_editor_widget, inline_scrolled, inline_view, inline_buffer = self._build_code_editor(asset_type)
        content.append(inline_editor_widget)

        note_label = Gtk.Label(label='')
        note_label.set_xalign(0)
        note_label.set_wrap(True)
        note_label.add_css_class('dim-label')
        note_label.set_visible(False)
        content.append(note_label)

        self._asset_page_state[asset_type] = {
            'page': page,
            'content': content,
            'selector_row': selector_row,
            'add_button': add_button,
            'dropdown': dropdown,
            'dropdown_ids': [],
            'selected_list': selected_list,
            'empty_label': empty_label,
            'inline_header': inline_header,
            'inline_hint': inline_hint,
            'inline_scrolled': inline_scrolled,
            'inline_view': inline_view,
            'inline_buffer': inline_buffer,
            'note_label': note_label,
        }
        self.page_stack.add_named(self._adaptive_wrap_page(page), page_name)

    def _refresh_asset_pages(self):
        for asset_type in list(getattr(self, '_asset_page_state', {}).keys()):
            self._refresh_asset_page(asset_type)

    def _refresh_asset_page(self, asset_type):
        state = getattr(self, '_asset_page_state', {}).get(asset_type)
        if not state:
            return
        available_assets = [asset for asset in list_custom_assets() if asset.get('type') == asset_type]
        labels = [t('detail_asset_dropdown_none')] + [f"{asset['name']} ({asset.get('type', '').upper()})" for asset in available_assets]
        dropdown_ids = [''] + [asset['id'] for asset in available_assets]
        new_dropdown = Gtk.DropDown.new_from_strings(labels)
        new_dropdown.set_hexpand(True)
        try:
            old_dropdown = state['dropdown']
            parent = old_dropdown.get_parent()
            if parent is not None:
                parent.remove(old_dropdown)
                parent.prepend(new_dropdown)
        except (AttributeError, TypeError):
            pass
        state['dropdown'] = new_dropdown
        state['dropdown_ids'] = dropdown_ids
        self._apply_subpage_adaptive_layout(force=True)

        selected_list = state['selected_list']
        child = selected_list.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            selected_list.remove(child)
            child = next_child

        linked_assets = self._linked_assets(asset_type)
        state['empty_label'].set_visible(not linked_assets)
        for asset in linked_assets:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.set_hexpand(True)

            text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            text_box.set_hexpand(True)
            name_label = Gtk.Label(label=str(asset.get('name') or ''), xalign=0)
            name_label.set_wrap(True)
            meta_label = Gtk.Label(label=f"{asset.get('type', '').upper()} - {format_asset_date(asset.get('imported_at'))}", xalign=0)
            meta_label.add_css_class('dim-label')
            text_box.append(name_label)
            text_box.append(meta_label)
            row.append(text_box)

            delete_button = Gtk.Button(icon_name='user-trash-symbolic')
            delete_button.add_css_class('flat')
            delete_button.connect('clicked', lambda button, current_type=asset_type, current_asset_id=asset['id'], current_name=str(asset.get('name') or ''): self._confirm_remove_linked_asset(button, current_type, current_asset_id, current_name))
            row.append(delete_button)
            selected_list.append(row)

        inline_buffer = state.get('inline_buffer')
        if inline_buffer is not None:
            self._set_buffer_text_if_needed(inline_buffer, self._get_inline_asset_text(asset_type))

        note_label = state['note_label']
        if asset_type == 'javascript' and self._current_browser_family() == 'firefox':
            note_label.set_text(t('detail_asset_firefox_js_note'))
            note_label.set_visible(True)
        else:
            note_label.set_visible(False)

    def _add_selected_asset(self, asset_type):
        state = self._asset_page_state.get(asset_type)
        if not state:
            return
        dropdown = state['dropdown']
        index = int(dropdown.get_selected())
        if index <= 0 or index >= len(state['dropdown_ids']):
            return
        asset_id = state['dropdown_ids'][index]
        current = self._linked_asset_ids(asset_type)
        if asset_id not in current:
            current.append(asset_id)
            self._set_linked_assets(asset_type, current)

    def _confirm_remove_linked_asset(self, anchor, asset_type, asset_id, asset_name):
        label_key = 'detail_asset_remove_css_confirm' if asset_type == 'css' else 'detail_asset_remove_javascript_confirm'
        self._present_choice_dialog(anchor, t(label_key, name=asset_name), lambda confirmed: self._remove_linked_asset(asset_type, asset_id) if confirmed else None, destructive=True)

    def _remove_linked_asset(self, asset_type, asset_id):
        current = [item for item in self._linked_asset_ids(asset_type) if item != asset_id]
        self._set_linked_assets(asset_type, current)

    def _build_engine_user_agents(self):
        by_engine = {}
        for engine in self.engines_list:
            by_engine[engine['id']] = self._normalize_user_agents(engine.get('user_agents'))
        return by_engine

    def _normalize_user_agents(self, user_agents):
        normalized = []
        if isinstance(user_agents, dict):
            for name, value in user_agents.items():
                if value:
                    normalized.append({'name': str(name), 'value': str(value)})
        elif isinstance(user_agents, list):
            for item in user_agents:
                if isinstance(item, dict):
                    name = item.get('name') or item.get('label') or item.get('value')
                    value = item.get('value')
                    if name and value:
                        normalized.append({'name': str(name), 'value': str(value)})
                elif isinstance(item, str) and item:
                    normalized.append({'name': item, 'value': item})
        return normalized

    def _default_user_agent_for_engine(self, engine):
        if not engine:
            return None
        options = list(self.engine_user_agents.get(engine['id'], []))
        return options[0] if options else None

    def _resolve_user_agent_selection(self, engine, options=None, persist_default=False):
        if not engine:
            return 0, None
        options = list(options if options is not None else self.engine_user_agents.get(engine['id'], []))
        stored_name = (self._get_option_value(USER_AGENT_NAME_KEY) or '').strip()
        stored_value = (self._get_option_value(USER_AGENT_VALUE_KEY) or '').strip()
        for idx, item in enumerate(options, start=1):
            item_name = str(item.get('name') or '').strip()
            item_value = str(item.get('value') or '').strip()
            if stored_name and item_name == stored_name and ((not stored_value) or item_value == stored_value):
                return idx, item
        if stored_value:
            for idx, item in enumerate(options, start=1):
                item_value = str(item.get('value') or '').strip()
                if item_value == stored_value:
                    item_name = str(item.get('name') or '').strip()
                    if item_name and item_name != stored_name:
                        self._add_options({USER_AGENT_NAME_KEY: item_name})
                    return idx, item
        if options and persist_default and not stored_name and not stored_value:
            default_item = options[0]
            self._add_options({
                USER_AGENT_NAME_KEY: default_item.get('name', ''),
                USER_AGENT_VALUE_KEY: default_item.get('value', ''),
            })
            return 1, default_item
        return 0, None

    def _get_current_engine(self):
        index = self.engine_dropdown.get_selected()
        if index <= 0:
            return None
        real_index = index - 1
        if 0 <= real_index < len(self.engines_list):
            return self.engines_list[real_index]
        return None

    def _engine_by_id(self, engine_id):
        try:
            target = int(engine_id)
        except (TypeError, ValueError):
            return None
        for engine in self.engines_list:
            if int(engine.get('id', -1)) == target:
                return engine
        return None

    def _current_browser_family(self):
        return browser_family_for_engine(self._get_current_engine())

    def _sync_browser_state_key(self, family=None):
        return browser_state_key(family or self._current_browser_family())

    def _sync_current_browser_state(self, commit=True):
        if self._syncing_browser_state:
            return
        family = self._current_browser_family()
        if family == 'generic':
            return
        self._syncing_browser_state = True
        try:
            payload = encode_browser_state(self._options_cache, family)
            key = self._sync_browser_state_key(family)
            self._options_cache[key] = payload
            self.db.add_option(self.entry.id, key, payload, commit=commit)
        finally:
            self._syncing_browser_state = False

    def _restore_browser_state_for_family(self, family):
        if family == 'generic':
            return
        state = build_family_option_state(self._options_cache, family)
        state.update(decode_browser_state(self._options_cache.get(self._sync_browser_state_key(family), ''), family))
        self._add_options(state)

    def _get_option_value(self, option_key):
        return self._options_cache.get(option_key)

    def _reload_options_cache_from_db(self):
        try:
            rows = self.db.get_options_for_entry(self.entry.id)
        except (TypeError, ValueError, OSError):
            LOG.error('Failed to reload options for entry %s', self.entry.id, exc_info=True)
            return
        self._options_cache = normalize_option_rows(rows)

    def _apply_option_values_to_controls(self):
        previous_suspend = self._suspend_change_handlers
        self._suspend_change_handlers = True
        try:
            addr = self._get_option_value(ADDRESS_KEY) or ''
            if self.address_entry.get_text() != addr:
                self.address_entry.set_text(addr)

            stored_engine_id = self._get_option_value('EngineID')
            engine_id = self._safe_int(stored_engine_id, default=0) if stored_engine_id not in (None, '') else 0
            engine_index = 0
            for idx, engine in enumerate(self.engines_list, start=1):
                if engine['id'] == engine_id:
                    engine_index = idx
                    break
            self.engine_dropdown.set_selected(engine_index)
            self.refresh_user_agent_options()
            self.refresh_mode_options()

            stored_color_scheme = (self._get_option_value(COLOR_SCHEME_KEY) or 'auto').strip().lower()
            try:
                color_index = self.color_scheme_values.index(stored_color_scheme)
            except ValueError:
                color_index = 0
            self.color_scheme_dropdown.set_selected(color_index)

            current_engine = self._get_current_engine()
            available = list(self.engine_user_agents.get(current_engine['id'], [])) if current_engine else []
            ua_index, _selected_item = self._resolve_user_agent_selection(current_engine, available, persist_default=True)
            self.user_agent_dropdown.set_selected(ua_index)

            for option_name, switch in list(self.switches.items()):
                switch.set_active(self._ui_boolean_option_active(option_name))

            if hasattr(self, 'mode_dropdown'):
                self.mode_dropdown.set_selected(self._current_mode_index())
        finally:
            self._suspend_change_handlers = previous_suspend
        self._refresh_profile_button_label()
        self._refresh_header_meta()
        self._update_browser_dependent_controls()
        self._refresh_asset_pages()
        self._update_export_button_state()

    def _set_option_value(self, option_key, value, commit=True):
        updates = self._coerce_option_updates({option_key: value})
        self._options_cache.update(updates)
        if len(updates) == 1:
            normalized = next(iter(updates.values()))
            self.db.add_option(self.entry.id, option_key, normalized, commit=commit)
        else:
            self.db.add_options(self.entry.id, updates)
        if any((key in browser_managed_option_keys()) and (not key.startswith('__BrowserState.')) for key in updates):
            self._sync_current_browser_state(commit=commit)

    def _safe_int(self, value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _looks_ready_for_url_check(self, value):
        value = (value or '').strip()
        if not value or value in {'http://', 'https://'}:
            return False
        if ' ' in value or value.endswith(('.', ':', '/', '?', '#')):
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

    def refresh_user_agent_options(self):
        engine = self._get_current_engine()
        options = list(self.engine_user_agents.get(engine['id'], [])) if engine else []
        labels = [t('user_agent_none')] + [item['name'] for item in options]
        selected_index, _selected_item = self._resolve_user_agent_selection(engine, options, persist_default=True)
        new_dropdown = Gtk.DropDown.new_from_strings(labels)
        new_dropdown.connect('notify::selected-item', self.on_user_agent_changed)
        self.grid.remove(self.user_agent_dropdown)
        self.user_agent_dropdown = new_dropdown
        self.grid.attach(self.user_agent_dropdown, 1, 6, 1, 1)
        previous_suspend = self._suspend_change_handlers
        self._suspend_change_handlers = True
        self.user_agent_dropdown.set_selected(selected_index)
        self._suspend_change_handlers = previous_suspend
        self.user_agent_dropdown.set_sensitive(bool(engine))
        self.user_agent_status.set_text('' if (engine and options) else (t('user_agent_unavailable') if engine else ''))
        self.user_agent_status.set_visible(False)

    def _normalize_mode_value(self, value):
        normalized = str(value or '').strip().lower().replace('-', '_').replace(' ', '_')
        aliases = {
            'default': 'standard',
            'normal': 'standard',
            'fullscreen': 'kiosk',
            'frameless': 'seamless',
        }
        normalized = aliases.get(normalized, normalized)
        return normalized if normalized in {'standard', 'kiosk', 'app', 'seamless'} else ''

    def _mode_label_for_value(self, value):
        mapping = {
            'standard': t('mode_standard'),
            'kiosk': t('mode_kiosk'),
            'app': t('mode_app'),
            'seamless': t('mode_seamless'),
        }
        return mapping.get(value, t('mode_standard'))

    def _configured_mode_values_for_engine(self, engine):
        browser_modes = self.config.get('browser_modes') or {}
        values = []

        def _extend_from(candidate):
            nonlocal values
            if not candidate:
                return False
            items = candidate if isinstance(candidate, list) else []
            normalized_items = []
            for item in items:
                if isinstance(item, dict):
                    mode_value = self._normalize_mode_value(item.get('value') or item.get('id') or item.get('name'))
                else:
                    mode_value = self._normalize_mode_value(item)
                if mode_value and mode_value not in normalized_items:
                    normalized_items.append(mode_value)
            if normalized_items:
                values = normalized_items
                return True
            return False

        family = browser_family_for_engine(engine) if engine else ''
        engine_id = str(engine.get('id')) if engine else ''
        engine_name = str(engine.get('name') or '').strip().lower() if engine else ''
        command = str(engine.get('command') or '').strip().lower() if engine else ''
        nested = browser_modes.get('engines') if isinstance(browser_modes, dict) else None

        candidates = []
        if isinstance(browser_modes, dict):
            candidates.extend([
                browser_modes.get(engine_id),
                browser_modes.get(engine_name),
                browser_modes.get(command),
                browser_modes.get(family),
            ])
            if isinstance(nested, dict):
                candidates.extend([
                    nested.get(engine_id),
                    nested.get(engine_name),
                    nested.get(command),
                    nested.get(family),
                ])
            candidates.append(browser_modes.get('default'))

        for candidate in candidates:
            if _extend_from(candidate):
                break

        if not values:
            values = ['standard', 'kiosk', 'app']
            if not engine or family == 'firefox':
                values.append('seamless')

        return values

    def _available_mode_items(self):
        engine = self._get_current_engine()
        values = self._configured_mode_values_for_engine(engine)
        current_value = self._current_mode_value()
        if current_value and current_value not in values:
            values.append(current_value)
        return [(value, self._mode_label_for_value(value)) for value in values]

    def refresh_mode_options(self):
        items = self._available_mode_items()
        self.mode_values = [value for value, _ in items]
        self.mode_labels = [label for _, label in items]
        current_value = self._current_mode_value()
        new_dropdown = Gtk.DropDown.new_from_strings(self.mode_labels or [t('mode_standard')])
        new_dropdown.connect('notify::selected-item', self.on_mode_changed)
        self.grid.remove(self.mode_dropdown)
        self.mode_dropdown = new_dropdown
        self.grid.attach(self.mode_dropdown, 1, 7, 1, 1)
        try:
            selected_index = self.mode_values.index(current_value)
        except ValueError:
            selected_index = 0
        previous_suspend = self._suspend_change_handlers
        self._suspend_change_handlers = True
        self.mode_dropdown.set_selected(selected_index)
        self._suspend_change_handlers = previous_suspend
        self.mode_dropdown.set_sensitive(bool(self._get_current_engine()))

    def _options_dict(self):
        return dict(self._options_cache)

    def _current_browser_family(self):
        return browser_family_for_engine(self._get_current_engine())

    def _coerce_option_updates(self, updates):
        clean_updates = {key: '' if value is None else str(value) for key, value in updates.items()}
        if self._current_browser_family() in {'firefox', 'chrome', 'chromium'} and clean_updates.get(OPTION_FORCE_PRIVACY_KEY) == '1':
            clean_updates[ONLY_HTTPS_KEY] = '1'
        return clean_updates

    def _add_options(self, updates):
        clean_updates = self._coerce_option_updates(updates)
        if clean_updates:
            self._options_cache.update(clean_updates)
            self.db.add_options(self.entry.id, clean_updates)
            if any((key in browser_managed_option_keys()) and (not key.startswith('__BrowserState.')) for key in clean_updates):
                self._sync_current_browser_state(commit=True)

    def _icon_path(self):
        return self._get_option_value(ICON_PATH_KEY) or ''

    def _profile_display_name(self):
        profile_path = (self._get_option_value(PROFILE_PATH_KEY) or '').strip()
        if profile_path:
            return Path(profile_path).name
        return (self._get_option_value(PROFILE_NAME_KEY) or '').strip()

    def _refresh_header_meta(self):
        if hasattr(self, 'header_name_label'):
            self.header_name_label.set_text(self.entry.title or '')
        if hasattr(self, 'header_profile_label'):
            self.header_profile_label.set_text(self._profile_display_name())
        self.header_name_label.set_valign(Gtk.Align.START)
        self.header_profile_label.set_valign(Gtk.Align.START)

    def _emit_visual_changed(self):
        self._refresh_header_meta()
        if self.on_visual_changed:
            self.on_visual_changed(self.entry)
        if self.on_title_changed:
            self.on_title_changed(self.entry)


    def _format_size(self, size_bytes):
        if size_bytes <= 0:
            return t('profile_size_unknown')
        gb = float(size_bytes) / (1024 ** 3)
        if gb >= 1:
            return f'{gb:.2f} GB'
        mb = float(size_bytes) / (1024 ** 2)
        if mb >= 1:
            return f'{mb:.0f} MB'
        kb = float(size_bytes) / 1024.0
        if kb >= 1:
            return f'{kb:.0f} KB'
        return f'{int(size_bytes)} B'

    def _apply_profile_button_label(self, profile_path, size_bytes=None):
        size_text = ''
        if profile_path and size_bytes is not None and size_bytes >= 0:
            size_text = f" ({self._format_size(size_bytes)})"
        self.delete_profile_button.set_label(t('profile_delete_button', size=size_text))
        self.delete_profile_button.set_sensitive(bool(profile_path))

    def _finish_profile_size_refresh(self, serial, profile_path, size_bytes):
        if serial != self._profile_size_request_serial:
            return False
        self._profile_size_pending_path = ''
        self._profile_size_cache[profile_path] = max(0, int(size_bytes or 0))
        current_profile_path = self._get_option_value(PROFILE_PATH_KEY) or ''
        if current_profile_path == profile_path:
            self._apply_profile_button_label(profile_path, self._profile_size_cache[profile_path])
        return False

    def _refresh_profile_button_label(self):
        profile_path = (self._get_option_value(PROFILE_PATH_KEY) or '').strip()
        self._profile_size_request_serial += 1
        serial = self._profile_size_request_serial
        if not profile_path:
            self._profile_size_pending_path = ''
            self._apply_profile_button_label('', None)
            return
        cached_size = self._profile_size_cache.get(profile_path)
        if cached_size is not None:
            self._profile_size_pending_path = ''
            self._apply_profile_button_label(profile_path, cached_size)
            return
        self._profile_size_pending_path = profile_path
        self._apply_profile_button_label(profile_path, None)

        def worker(path_value, token):
            size_bytes = get_profile_size_bytes(path_value)
            GLib.idle_add(self._finish_profile_size_refresh, token, path_value, size_bytes)

        threading.Thread(target=worker, args=(profile_path, serial), daemon=True).start()

    def _supported_option_names(self, engine):
        if not engine:
            return set()
        family = browser_family_for_engine(engine)
        supported = supported_browser_option_keys(family, visible_only=True)
        if family == 'firefox' and OPTION_KEEP_IN_BACKGROUND_KEY in supported and not is_furios_distribution():
            supported.discard(OPTION_KEEP_IN_BACKGROUND_KEY)
        return {name for name in self._option_names_in_order() if name in supported}

    def _has_exportable_webapp(self):
        title = (self.title_entry.get_text() or '').strip()
        address = self._normalize_address_for_ui(self.address_entry.get_text())
        description = (self.description_entry.get_text() or '').strip() if hasattr(self, 'description_entry') else ''
        icon_path = str(self._get_option_value(ICON_PATH_KEY) or '').strip()
        options = dict(self._options_dict())
        for transient_key in (ICON_PATH_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY):
            options.pop(transient_key, None)
        has_meaningful_options = any(str(value or '').strip() for value in options.values())
        return any((title, address, description, icon_path, has_meaningful_options))

    def _update_export_button_state(self):
        if hasattr(self, 'export_button'):
            self.export_button.set_sensitive(self._has_exportable_webapp())


    def _set_inline_busy(self, active, message=None):
        is_active = bool(active)
        self.inline_busy_label.set_text((message or t('loading')) if is_active else '')
        self.inline_busy_overlay.set_visible(is_active)
        if is_active:
            self.inline_busy_spinner.start()
        else:
            self.inline_busy_spinner.stop()

    def _set_icon_download_busy(self, active, message=None):
        is_active = bool(active)
        self.icon_page_progress_box.set_visible(True)
        if is_active:
            self.icon_page_search_spinner.set_visible(True)
            self.icon_page_search_spinner.start()
            self.icon_page_status.set_text(message or t('icon_page_status_searching'))
            self.icon_download_button.set_sensitive(False)
            return
        self.icon_page_search_spinner.stop()
        self.icon_page_search_spinner.set_visible(False)
        self.icon_download_button.set_sensitive(True)

    def _set_detail_action_status(self, text=''):
        text = text or ''
        self.detail_action_status.set_text(text)
        self.detail_action_status.set_visible(bool(text.strip()))

    def _cancel_detail_toast(self):
        if self._detail_toast_timeout_id:
            GLib.source_remove(self._detail_toast_timeout_id)
            self._detail_toast_timeout_id = 0

    def _hide_detail_toast(self):
        self._cancel_detail_toast()
        return False

    def _show_plugin_banner(self, text, timeout_ms=3000):
        message = (text or '').strip()
        if not message:
            return
        self._cancel_detail_toast()
        if callable(self.on_overlay_notification):
            self.on_overlay_notification(message, timeout_ms=timeout_ms)

    def _update_browser_dependent_controls(self):
        engine = self._get_current_engine()
        command = (engine.get('command') or '').lower() if engine else ''
        is_firefox = bool(engine and 'firefox' in command)
        for widget in getattr(self, '_engine_option_widgets', []):
            widget.set_visible(bool(engine))
        for _, widgets in getattr(self, '_option_row_widgets', {}).items():
            if len(widgets) > 2 and widgets[2] is not None:
                widgets[2].set_sensitive(bool(engine))
        self.browser_option_status.set_text(t('option_adblock_unavailable') if engine and not is_firefox and OPTION_ADBLOCK_KEY in self._visible_option_names_in_order() else '')
        self._update_export_button_state()

    def _load_texture(self, icon_path):
        try:
            path = Path(icon_path)
            cache_key = None
            if path.exists():
                cache_key = (str(path), int(path.stat().st_mtime_ns))
                cached = self._icon_texture_cache.get(cache_key)
                if cached is not None:
                    return cached
            texture = Gdk.Texture.new_from_filename(str(icon_path))
            if cache_key is not None:
                self._icon_texture_cache = {cache_key: texture}
            return texture
        except (GLib.Error, OSError, ValueError) as error:
            LOG.warning('Failed to load icon texture %s: %s', icon_path, error)
            return None

    def _prepare_display_icon_path(self, icon_path, size):
        try:
            preview_dir = Path(tempfile.gettempdir()) / 'webapp_icon_previews'
            preview_dir.mkdir(parents=True, exist_ok=True)
            source = Path(icon_path)
            target = preview_dir / f'entry-{self.entry.id}-{size}-{int(source.stat().st_mtime_ns)}.png'
            if not target.exists():
                source_suffix = source.suffix.lower()
                if source_suffix == '.svg':
                    normalize_icon_to_png(source, target)
                else:
                    with Image.open(source) as image:
                        image = image.convert('RGBA')
                        image.thumbnail((size, size), Image.Resampling.LANCZOS)
                        canvas = Image.new('RGBA', (size, size), (0, 0, 0, 0))
                        x = (size - image.width) // 2
                        y = (size - image.height) // 2
                        canvas.alpha_composite(image, (x, y))
                        canvas.save(target, 'PNG')
            return target
        except (OSError, ValueError, UnidentifiedImageError):
            return Path(icon_path)

    def _create_icon_widget(self, size):
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        wrapper.set_size_request(size, size)
        wrapper.set_halign(Gtk.Align.CENTER)
        wrapper.set_valign(Gtk.Align.CENTER)
        wrapper.set_hexpand(False)
        wrapper.set_vexpand(False)
        wrapper.set_overflow(Gtk.Overflow.HIDDEN)

        def _append(child):
            child.set_halign(Gtk.Align.CENTER)
            child.set_valign(Gtk.Align.CENTER)
            child.set_hexpand(False)
            child.set_vexpand(False)
            wrapper.append(child)
            return wrapper

        icon_ref = self._icon_path()
        if icon_ref:
            icon_path = Path(icon_ref)
            if icon_path.exists():
                display_path = self._prepare_display_icon_path(icon_path, size)
                texture = self._load_texture(display_path)
                if texture is not None:
                    picture = Gtk.Picture.new_for_paintable(texture)
                    picture.set_size_request(size, size)
                    picture.set_can_shrink(True)
                    picture.set_content_fit(Gtk.ContentFit.CONTAIN)
                    picture.set_keep_aspect_ratio(True)
                    picture.set_overflow(Gtk.Overflow.HIDDEN)
                    return _append(picture)
            image = Gtk.Image.new_from_icon_name(icon_ref)
            image.set_pixel_size(max(32, size - 16))
            image.set_size_request(size, size)
            return _append(image)

        image = Gtk.Image.new_from_icon_name('applications-internet')
        image.set_pixel_size(max(32, size - 16))
        image.set_size_request(size, size)
        return _append(image)

    def refresh_icon_preview(self):
        widget = self._create_icon_widget(56)
        self.icon_button.set_size_request(72, 72)
        widget.set_valign(Gtk.Align.CENTER)
        widget.set_halign(Gtk.Align.CENTER)
        self.icon_button.set_child(widget)

    def _clear_icon_page_preview_canvas(self):
        child = self.icon_page_preview_canvas.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.icon_page_preview_canvas.remove(child)
            child = next_child

    def _icon_preview_signature(self, size):
        icon_ref = self._icon_path() or ''
        if icon_ref:
            icon_path = Path(icon_ref)
            if icon_path.exists():
                return (str(icon_path), int(icon_path.stat().st_mtime_ns), size)
        return (icon_ref, None, size)

    def _set_icon_page_preview_placeholder(self):
        self._clear_icon_page_preview_canvas()
        placeholder = Gtk.Image.new_from_icon_name('applications-internet')
        placeholder.set_pixel_size(48)
        placeholder.set_size_request(64, 64)
        placeholder.set_halign(Gtk.Align.CENTER)
        placeholder.set_valign(Gtk.Align.CENTER)
        self.icon_page_preview_canvas.put(placeholder, 14, 14)

    def _schedule_icon_page_preview_refresh(self):
        if self._icon_page_preview_refresh_source_id:
            GLib.source_remove(self._icon_page_preview_refresh_source_id)
            self._icon_page_preview_refresh_source_id = 0
        signature = self._icon_preview_signature(64)
        self._icon_page_preview_signature = signature
        self._icon_page_preview_refresh_source_id = GLib.idle_add(self._refresh_icon_page_preview_idle, signature)

    def _refresh_icon_page_preview_idle(self, signature):
        self._icon_page_preview_refresh_source_id = 0
        if signature != self._icon_page_preview_signature:
            return False
        preview = self._create_icon_widget(64)
        preview.set_halign(Gtk.Align.CENTER)
        preview.set_valign(Gtk.Align.CENTER)
        self._clear_icon_page_preview_canvas()
        self.icon_page_preview_canvas.put(preview, 14, 14)
        return False

    def refresh_icon_page(self):
        self._set_icon_page_preview_placeholder()

        if self._icon_download_in_progress:
            self._set_icon_download_busy(True, t('icon_page_status_searching'))
        else:
            self._set_icon_download_busy(False)
            url = self.address_entry.get_text().strip()
            if is_structurally_valid_url(url):
                self.icon_download_button.set_sensitive(True)
                self.icon_page_status.set_text(t('icon_page_status_url'))
            else:
                self.icon_download_button.set_sensitive(False)
                self.icon_page_status.set_text(t('icon_page_status_no_url'))

        has_icon = self._has_custom_icon()
        self.icon_delete_button.set_sensitive(has_icon)
        self._schedule_icon_page_preview_refresh()

    def _managed_icon_target(self):
        return get_managed_icon_path(self.entry.title, '.png', self.entry.id)

    def _sync_icon_filename(self):
        icon_path = self._icon_path()
        if not icon_path:
            return
        if '/' not in icon_path and '\\' not in icon_path:
            return
        try:
            current = Path(icon_path).expanduser()
            if not current.exists():
                return
            target = self._managed_icon_target()
            if current.resolve() == target.resolve():
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(current.read_bytes())
            if current.parent == target.parent and current.name != target.name:
                try:
                    current.unlink(missing_ok=True)
                except OSError:
                    pass
            self._set_option_value(ICON_PATH_KEY, str(target))
        except OSError as error:
            LOG.warning('Failed to sync icon filename for entry %s: %s', self.entry.id, error)

    def _store_pil_image(self, pil_image, target_path=None):
        target_path = target_path or self._managed_icon_target()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        image = pil_image.convert('RGBA')
        max_size = 256
        fit_size = 220
        width, height = image.size
        if width <= 0 or height <= 0:
            raise OSError('Invalid image size')
        scale = min(fit_size / width, fit_size / height)
        if scale <= 0:
            scale = 1.0
        new_size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
        image = image.resize(new_size, Image.Resampling.LANCZOS)
        canvas = Image.new('RGBA', (max_size, max_size), (0, 0, 0, 0))
        x = (max_size - image.width) // 2
        y = (max_size - image.height) // 2
        canvas.alpha_composite(image, (x, y))
        canvas.save(target_path, 'PNG')
        return target_path

    def _apply_icon_path(self, path):
        self._set_option_value(ICON_PATH_KEY, str(path))
        self.refresh_icon_preview()
        self.refresh_icon_page()
        self._update_export_button_state()
        self._emit_visual_changed()
        self.save_desktop_file()
        GLib.idle_add(self._emit_visual_changed)

    def _store_icon_file(self, source_path):
        try:
            validated_source = validate_icon_source_path(source_path)
            if validated_source is None:
                raise OSError('Invalid or oversized icon file')
            target_path = self._managed_icon_target()
            normalize_icon_to_png(validated_source, target_path)
            self._apply_icon_path(target_path)
            return True
        except (UnidentifiedImageError, OSError) as error:
            LOG.warning('Failed to normalize icon file %s: %s', source_path, error)
        return False

    def _parse_html_tag_attributes(self, tag):
        attrs = {}
        attr_pattern = re.compile(r"([a-zA-Z_:][a-zA-Z0-9_:\-.]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))")
        for match in attr_pattern.finditer(tag):
            value = match.group(2)
            if value is None:
                value = match.group(3)
            if value is None:
                value = match.group(4) or ''
            attrs[match.group(1).lower()] = value
        return attrs

    def _size_score_from_string(self, sizes_value):
        size_score = 0
        saw_any = False
        for token in str(sizes_value or '').lower().split():
            token = token.strip()
            if not token:
                continue
            if token == 'any':
                saw_any = True
                continue
            if 'x' in token:
                try:
                    width, height = token.split('x', 1)
                    size_score = max(size_score, min(int(width), int(height)))
                except ValueError:
                    continue
        if saw_any:
            size_score = max(size_score, 4096)
        return size_score

    def _infer_size_score_from_url(self, href):
        href_path = urlparse(str(href or '')).path.lower()
        for pattern in (r'(\d{2,4})x(\d{2,4})', r'[-_](\d{2,4})[.-](?:png|svg|ico|webp|jpg|jpeg|avif)$'):
            match = re.search(pattern, href_path)
            if not match:
                continue
            try:
                if len(match.groups()) >= 2:
                    return min(int(match.group(1)), int(match.group(2)))
                return int(match.group(1))
            except ValueError:
                continue
        return 0

    def _icon_type_priority(self, href, rel_value='', type_value='', sizes_value='', purpose_value=''):
        href_path = urlparse(str(href or '')).path.lower()
        rel_value = str(rel_value or '').lower()
        type_value = str(type_value or '').lower()
        sizes_value = str(sizes_value or '').lower()
        purpose_value = str(purpose_value or '').lower()
        if (
            href_path.endswith('.svg')
            or 'image/svg+xml' in type_value
            or 'mask-icon' in rel_value
            or 'monochrome' in purpose_value
            or sizes_value.strip() == 'any'
        ):
            return 5
        if any(href_path.endswith(ext) for ext in ('.png', '.webp', '.avif', '.jpg', '.jpeg')) or any(image_type in type_value for image_type in ('image/png', 'image/webp', 'image/avif', 'image/jpeg')):
            return 4
        if 'apple-touch-icon' in rel_value or 'fluid-icon' in rel_value:
            return 4
        if href_path.endswith('.ico') or 'image/x-icon' in type_value or 'image/vnd.microsoft.icon' in type_value:
            return 2
        return 3

    def _source_priority_for_candidate(self, source_kind, rel_value='', media_value=''):
        rel_value = str(rel_value or '').lower()
        media_value = str(media_value or '').lower().strip()
        priority_map = {
            'manifest_maskable': 65,
            'manifest': 60,
            'apple_touch': 58,
            'icon_link': 56,
            'mask_icon': 54,
            'fluid_icon': 52,
            'browserconfig': 46,
            'root_fallback': 40,
            'special_fallback': 38,
            'meta_image': 10,
        }
        priority = priority_map.get(source_kind, 20)
        if 'apple-touch-icon' in rel_value:
            priority = max(priority, priority_map['apple_touch'])
        if 'mask-icon' in rel_value:
            priority = max(priority, priority_map['mask_icon'])
        if media_value:
            if 'print' in media_value:
                priority -= 20
            elif 'screen' in media_value or 'all' in media_value:
                priority += 1
        return priority

    def _make_icon_candidate(self, href, *, source_kind='icon_link', rel_value='', type_value='', sizes_value='', purpose_value='', media_value='', order=0):
        return {
            'href': href,
            'type_priority': self._icon_type_priority(href, rel_value, type_value, sizes_value, purpose_value),
            'source_priority': self._source_priority_for_candidate(source_kind, rel_value, media_value),
            'size_score': self._size_score_from_string(sizes_value) or self._infer_size_score_from_url(href),
            'order': int(order),
        }

    def _order_icon_candidates(self, candidates):
        normalized = []
        seen = set()
        for item in candidates:
            href = str((item or {}).get('href') or '').strip()
            if not href or href in seen:
                continue
            seen.add(href)
            normalized.append(item)
        normalized.sort(
            key=lambda item: (
                int(item.get('type_priority', 0)),
                int(item.get('source_priority', 0)),
                int(item.get('size_score', 0)),
                int(item.get('order', 0)),
            ),
            reverse=True,
        )
        return [item['href'] for item in normalized]

    def _extract_base_href(self, html, base_url):
        pattern = re.compile(r'<base\b[^>]*>', re.IGNORECASE)
        for match in pattern.finditer(html):
            attrs = self._parse_html_tag_attributes(match.group(0))
            href_value = str(attrs.get('href') or '').strip()
            if href_value:
                return urljoin(base_url, href_value)
        return None

    def _extract_icon_candidates(self, html, base_url):
        candidates = []
        pattern = re.compile(r'<link\b[^>]*>', re.IGNORECASE)
        order = 0
        for match in pattern.finditer(html):
            tag = match.group(0)
            attrs = self._parse_html_tag_attributes(tag)
            href_value = str(attrs.get('href') or '').strip()
            if not href_value:
                continue
            rel_value = str(attrs.get('rel') or '').lower().strip()
            href = urljoin(base_url, href_value)
            source_kind = None
            if 'manifest' in rel_value:
                continue
            if 'apple-touch-icon' in rel_value:
                source_kind = 'apple_touch'
            elif 'mask-icon' in rel_value:
                source_kind = 'mask_icon'
            elif 'fluid-icon' in rel_value:
                source_kind = 'fluid_icon'
            elif 'icon' in rel_value:
                source_kind = 'icon_link'
            if source_kind is None:
                continue
            order += 1
            candidates.append(
                self._make_icon_candidate(
                    href,
                    source_kind=source_kind,
                    rel_value=rel_value,
                    type_value=attrs.get('type', ''),
                    sizes_value=attrs.get('sizes', ''),
                    media_value=attrs.get('media', ''),
                    order=order,
                )
            )
        return candidates

    def _extract_manifest_url(self, html, base_url):
        pattern = re.compile(r'<link\b[^>]*>', re.IGNORECASE)
        for match in pattern.finditer(html):
            attrs = self._parse_html_tag_attributes(match.group(0))
            rel_value = str(attrs.get('rel') or '').lower().strip()
            href_value = str(attrs.get('href') or '').strip()
            if href_value and 'manifest' in rel_value:
                return urljoin(base_url, href_value)
        return None

    def _extract_manifest_icon_candidates(self, manifest_text, manifest_url):
        try:
            manifest = json.loads(manifest_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        candidates = []
        order = 0
        for icon in manifest.get('icons', []) or []:
            if not isinstance(icon, dict):
                continue
            src = str(icon.get('src') or '').strip()
            if not src:
                continue
            href = urljoin(manifest_url, src)
            purpose_value = str(icon.get('purpose') or '')
            order += 1
            candidates.append(
                self._make_icon_candidate(
                    href,
                    source_kind='manifest_maskable' if 'maskable' in purpose_value.lower() else 'manifest',
                    type_value=icon.get('type', ''),
                    sizes_value=icon.get('sizes', ''),
                    purpose_value=purpose_value,
                    order=order,
                )
            )
        return candidates

    def _extract_browserconfig_url(self, html, base_url):
        pattern = re.compile(r'<meta\b[^>]*>', re.IGNORECASE)
        for match in pattern.finditer(html):
            attrs = self._parse_html_tag_attributes(match.group(0))
            key = str(attrs.get('name') or '').lower().strip()
            content = str(attrs.get('content') or '').strip()
            if key == 'msapplication-config' and content:
                return urljoin(base_url, content)
        return None

    def _extract_browserconfig_icon_candidates(self, browserconfig_text, browserconfig_url):
        pattern = re.compile(r'<(square\d+x\d+logo|wide\d+x\d+logo|tileimage)\b[^>]*\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)
        candidates = []
        order = 0
        for match in pattern.finditer(browserconfig_text):
            tag_name = str(match.group(1) or '').lower()
            href = urljoin(browserconfig_url, str(match.group(2) or '').strip())
            sizes_value = ''
            size_match = re.search(r'(\d+x\d+)', tag_name)
            if size_match:
                sizes_value = size_match.group(1)
            order += 1
            candidates.append(
                self._make_icon_candidate(
                    href,
                    source_kind='browserconfig',
                    sizes_value=sizes_value,
                    order=order,
                )
            )
        return candidates

    def _extract_meta_image_candidates(self, html, base_url):
        candidates = []
        pattern = re.compile(r'<meta\b[^>]*>', re.IGNORECASE)
        supported_keys = {
            'og:image',
            'og:image:url',
            'og:image:secure_url',
            'twitter:image',
            'twitter:image:src',
            'msapplication-tileimage',
        }
        order = 0
        for match in pattern.finditer(html):
            attrs = self._parse_html_tag_attributes(match.group(0))
            content = str(attrs.get('content') or '').strip()
            if not content:
                continue
            key = str(attrs.get('property') or attrs.get('name') or '').lower().strip()
            if key not in supported_keys:
                continue
            href = urljoin(base_url, content)
            order += 1
            candidates.append(
                self._make_icon_candidate(
                    href,
                    source_kind='meta_image',
                    order=order,
                )
            )
        return candidates

    def _download_image_bytes(self, url):
        request = urllib.request.Request(url, headers={'User-Agent': DESKTOP_CHROME_USER_AGENT, 'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8', 'Accept-Language': 'en-US,en;q=0.9', 'Connection': 'close'})
        with urllib.request.urlopen(request, timeout=10) as response:
            content_length = response.headers.get('Content-Length')
            if content_length:
                try:
                    if int(content_length) > MAX_ICON_FILE_SIZE:
                        raise OSError('Icon download too large')
                except ValueError:
                    pass
            payload = response.read(MAX_ICON_FILE_SIZE + 1)
            if len(payload) > MAX_ICON_FILE_SIZE:
                raise OSError('Icon download too large')
            content_type = response.headers.get_content_type()
            return payload, content_type

    def _public_root_hosts_for_icon_fallback(self, host):
        host = (host or '').lower().strip()
        hosts = []
        if host:
            hosts.append(host)
        if host.startswith('www.'):
            hosts.append(host[4:])
        return [item for index, item in enumerate(hosts) if item and item not in hosts[:index]]

    def _special_icon_fallback_candidates(self, url):
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower()
        path = parsed.path or ''
        candidates = []
        if host in {'www.google.com', 'google.com', 'maps.google.com'} and path.startswith('/maps'):
            candidates.extend([
                'https://www.google.com/images/branding/product/ico/maps15_bnuw3a_32dp.ico',
                'https://maps.google.com/favicon.ico',
            ])
        if host.endswith('booking.com'):
            candidates.extend([
                'https://cf.bstatic.com/static/img/favicon/9ca83ba2a5a3293ff07452cb24949a5843af4592.svg',
                'https://cf.bstatic.com/static/img/apple-touch-icon/5db9fd30d96b1796883ee94be7dddce50b73bb38.png',
                'https://cf.bstatic.com/static/img/favicon/40749a316c45e239a7149b6711ea4c48d10f8d89.ico',
            ])
        return candidates

    def _icon_source_page_candidates(self, url):
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return []
        candidates = []

        def add_candidate(value):
            value = str(value or '').strip()
            if value and value not in candidates:
                candidates.append(value)

        clean = parsed._replace(fragment='')
        add_candidate(urlunparse(clean))

        normalized = clean._replace(params='', query='')
        add_candidate(urlunparse(normalized))

        path = normalized.path or '/'
        if path not in ('', '/'):
            stripped_path = path.rstrip('/') or '/'
            if stripped_path != path:
                add_candidate(urlunparse(normalized._replace(path=stripped_path)))
            leaf = stripped_path.rsplit('/', 1)[-1]
            if '.' in leaf:
                parent_path = stripped_path.rsplit('/', 1)[0] or '/'
                add_candidate(urlunparse(normalized._replace(path=parent_path)))
            add_candidate(urlunparse(normalized._replace(path='/')))

        return candidates

    def _download_text_response(self, url, accept_header, timeout=8):
        request = urllib.request.Request(url, headers={'User-Agent': DESKTOP_CHROME_USER_AGENT, 'Accept': accept_header, 'Accept-Language': 'en-US,en;q=0.9', 'Connection': 'close'})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            content_length = response.headers.get('Content-Length')
            if content_length:
                try:
                    if int(content_length) > 512 * 1024:
                        raise OSError('Response too large for icon discovery')
                except ValueError:
                    pass
            body = response.read(512 * 1024 + 1)
            final_url = response.geturl()
        if len(body) > 512 * 1024:
            raise OSError('Response too large for icon discovery')
        return body.decode('utf-8', errors='ignore'), content_type, final_url

    def _download_favicon(self, url):
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return None

        primary_icon_candidates = []
        fallback_meta_candidates = []
        final_url = url
        seen_document_urls = set()

        for document_url in self._icon_source_page_candidates(url):
            if document_url in seen_document_urls:
                continue
            seen_document_urls.add(document_url)
            try:
                html, content_type, resolved_final_url = self._download_text_response(document_url, 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8')
            except (OSError, urllib.error.URLError, ValueError):
                continue
            if 'html' not in content_type:
                continue
            final_url = resolved_final_url or document_url
            document_base_url = final_url
            base_href = self._extract_base_href(html, document_base_url)
            if base_href:
                document_base_url = base_href
            primary_icon_candidates.extend(self._extract_icon_candidates(html, document_base_url))
            manifest_url = self._extract_manifest_url(html, document_base_url)
            if manifest_url:
                try:
                    manifest_text, manifest_content_type, resolved_manifest_url = self._download_text_response(manifest_url, 'application/manifest+json,application/json,text/plain;q=0.9,*/*;q=0.8')
                    if 'json' in manifest_content_type or manifest_text.lstrip().startswith('{'):
                        primary_icon_candidates.extend(self._extract_manifest_icon_candidates(manifest_text, resolved_manifest_url))
                except (OSError, urllib.error.URLError, ValueError):
                    pass
            browserconfig_url = self._extract_browserconfig_url(html, document_base_url)
            if browserconfig_url:
                try:
                    browserconfig_text, _browserconfig_content_type, resolved_browserconfig_url = self._download_text_response(browserconfig_url, 'application/xml,text/xml,text/plain;q=0.9,*/*;q=0.8')
                    primary_icon_candidates.extend(self._extract_browserconfig_icon_candidates(browserconfig_text, resolved_browserconfig_url))
                except (OSError, urllib.error.URLError, ValueError):
                    pass
            fallback_meta_candidates.extend(self._extract_meta_image_candidates(html, document_base_url))

        root_hosts = self._public_root_hosts_for_icon_fallback(urlparse(final_url).hostname or parsed.hostname or '')
        order = 0
        for host in root_hosts:
            for fallback in (
                '/favicon.svg',
                '/favicon.png',
                '/favicon-512x512.png',
                '/favicon-192x192.png',
                '/favicon-180x180.png',
                '/favicon-64x64.png',
                '/favicon-32x32.png',
                '/favicon-16x16.png',
                '/apple-touch-icon.png',
                '/apple-touch-icon-precomposed.png',
                '/apple-touch-icon-180x180.png',
                '/favicon.ico',
            ):
                href = f'https://{host}{fallback}'
                order += 1
                primary_icon_candidates.append(self._make_icon_candidate(href, source_kind='root_fallback', order=order))
        for href in self._special_icon_fallback_candidates(final_url):
            order += 1
            primary_icon_candidates.append(self._make_icon_candidate(href, source_kind='special_fallback', order=order))

        def _try_candidates(candidate_list):
            for icon_url in self._order_icon_candidates(candidate_list):
                try:
                    payload, content_type = self._download_image_bytes(icon_url)
                    temp_fd, temp_name = tempfile.mkstemp(suffix='.png')
                    os.close(temp_fd)
                    temp_target = Path(temp_name)
                    return normalize_icon_bytes_to_png(payload, temp_target, source_name=icon_url, content_type=content_type)
                except (OSError, ValueError, urllib.error.URLError, UnidentifiedImageError):
                    continue
            return None

        primary_result = _try_candidates(primary_icon_candidates)
        if primary_result is not None:
            return primary_result
        return _try_candidates(fallback_meta_candidates)

    def _build_wapp_payload(self):
        raw_options = dict(self._options_dict())
        icon_path = str(raw_options.get(ICON_PATH_KEY, '') or '').strip()
        options = dict(raw_options)
        for transient_key in (ICON_PATH_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY):
            options.pop(transient_key, None)
        payload = {
            'format': 'webapp-export-v1',
            'title': self.entry.title or '',
            'description': self.entry.description or '',
            'active': bool(self.entry.active),
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

    def _apply_wapp_payload(self, payload):
        try:
            payload = normalize_wapp_payload(payload)
        except ValueError as error:
            LOG.warning('Rejected unsafe .wapp payload: %s', error)
            return False

        if 'title' in payload:
            self.title_entry.set_text(str(payload.get('title', '')))
        if 'description' in payload:
            self.description_entry.set_text(str(payload.get('description', '')))
        if 'active' in payload:
            self.switch.set_active(bool(payload.get('active')))

        options = normalize_option_dict(payload.get('options', {}))
        if not isinstance(options, dict):
            options = {}
        for transient_key in (ICON_PATH_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY):
            options.pop(transient_key, None)

        imported_address = str(options.get(ADDRESS_KEY, '')) if ADDRESS_KEY in options else ''
        if imported_address:
            self._suspend_address_processing = True
            try:
                self.address_entry.set_text(imported_address)
                self._set_option_value(ADDRESS_KEY, imported_address)
            finally:
                self._suspend_address_processing = False

        engine_id = None
        if 'EngineID' in options:
            raw_engine_id = options.get('EngineID')
            engine_id = self._safe_int(raw_engine_id, default=0) if raw_engine_id not in (None, '') else 0
            selected_index = 0
            for idx, engine in enumerate(self.engines_list, start=1):
                if engine['id'] == engine_id:
                    selected_index = idx
                    self._set_option_value('EngineID', str(engine_id))
                    self._set_option_value('EngineName', engine['name'])
                    break
            self.engine_dropdown.set_selected(selected_index)
            self.refresh_user_agent_options()

        if USER_AGENT_NAME_KEY in options or USER_AGENT_VALUE_KEY in options:
            ua_name = str(options.get(USER_AGENT_NAME_KEY, ''))
            ua_value = str(options.get(USER_AGENT_VALUE_KEY, ''))
            self._set_option_value(USER_AGENT_NAME_KEY, ua_name)
            self._set_option_value(USER_AGENT_VALUE_KEY, ua_value)
            current_engine_obj = self._get_current_engine()
            available = list(self.engine_user_agents.get(current_engine_obj['id'], [])) if current_engine_obj else []
            selected_index, _selected_item = self._resolve_user_agent_selection(current_engine_obj, available, persist_default=True)
            previous_suspend = self._suspend_change_handlers
            self._suspend_change_handlers = True
            self.user_agent_dropdown.set_selected(selected_index)
            self._suspend_change_handlers = previous_suspend

        color_scheme_value = str(options.get(COLOR_SCHEME_KEY, 'auto')).strip().lower()
        if color_scheme_value in self.color_scheme_values:
            self.color_scheme_dropdown.set_selected(self.color_scheme_values.index(color_scheme_value))

        for opt_name, switch in self.switches.items():
            if opt_name in options:
                switch.set_active(str(options.get(opt_name, '0')) == '1')

        if hasattr(self, 'mode_dropdown'):
            previous_suspend = self._suspend_change_handlers
            self._suspend_change_handlers = True
            self.mode_dropdown.set_selected(self._current_mode_index())
            self._suspend_change_handlers = previous_suspend

        icon_meta = payload.get('icon')
        if isinstance(icon_meta, dict) and icon_meta.get('data_base64'):
            try:
                raw = base64.b64decode(icon_meta['data_base64'])
                target_path = self._managed_icon_target()
                normalize_icon_bytes_to_png(
                    raw,
                    target_path,
                    source_name=str(icon_meta.get('filename') or 'icon'),
                    content_type=str(icon_meta.get('mime') or ''),
                )
                self._set_option_value(ICON_PATH_KEY, str(target_path))
            except (binascii.Error, OSError, ValueError) as error:
                LOG.warning('Failed to import icon from .wapp: %s', error)
        elif 'icon' in payload and icon_meta is None:
            self._set_option_value(ICON_PATH_KEY, '')

        normalized_imported_address = self._normalize_address_for_ui(imported_address) if imported_address else ''
        if normalized_imported_address and normalized_imported_address != self.address_entry.get_text().strip():
            self._suspend_address_processing = True
            try:
                self.address_entry.set_text(normalized_imported_address)
                self._set_option_value(ADDRESS_KEY, normalized_imported_address)
            finally:
                self._suspend_address_processing = False
        if normalized_imported_address:
            self._trigger_address_validation(normalized_imported_address, debounce=False, export_after_validation=False)
        else:
            self._set_url_status('', None)
        self._sync_icon_filename()
        self.refresh_icon_preview()
        self.refresh_icon_page()
        self._update_export_button_state()
        self._emit_visual_changed()
        self._update_browser_dependent_controls()
        self._refresh_profile_button_label()
        self.save_desktop_file()
        return True

    def on_icon_clicked(self, button):
        self.page_stack.set_visible_child_name('icon')
        self._update_tabbed_navigation_state()
        self._update_export_button_state()
        GLib.idle_add(self._refresh_icon_page_after_open)

    def _refresh_icon_page_after_open(self):
        if self.is_icon_page_visible():
            self.refresh_icon_page()
        return False

    def is_icon_page_visible(self):
        return self.page_stack.get_visible_child_name() == 'icon'

    def _focus_mobile_neutral_target(self):
        if not self._is_compact_layout():
            return False
        page_name = self._current_page_name()
        target = None
        if page_name == 'main':
            target = getattr(self, 'icon_button', None)
        elif page_name == 'icon':
            buttons = getattr(self, '_icon_page_buttons', [])
            target = buttons[0] if buttons else getattr(self, 'icon_button', None)
        elif page_name in {'css_assets', 'javascript_assets'}:
            asset_type = 'css' if page_name == 'css_assets' else 'javascript'
            state = getattr(self, '_asset_page_state', {}).get(asset_type, {})
            target = state.get('add_button') or state.get('dropdown') or getattr(self, 'icon_button', None)
        else:
            target = getattr(self, 'icon_button', None)
        if target is None:
            return False
        root = None
        try:
            root = self.get_root()
        except Exception:
            root = None
        if root is not None and hasattr(root, 'set_focus'):
            try:
                root.set_focus(target)
            except Exception:
                pass
        try:
            target.grab_focus()
        except Exception:
            pass
        return False

    def _schedule_mobile_focus_reset(self):
        if not self._is_compact_layout():
            return
        GLib.idle_add(self._focus_mobile_neutral_target)

    def _notify_navigation_changed(self):
        if callable(self.on_navigation_changed):
            try:
                self.on_navigation_changed(self)
            except Exception:
                LOG.debug('Failed to notify detail navigation change', exc_info=True)

    def _on_page_stack_visible_child_changed(self, *args):
        self._schedule_mobile_focus_reset()
        self._notify_navigation_changed()

    def _update_tabbed_navigation_state(self):
        current_name = self._current_page_name()
        compact = self._is_compact_layout()
        page_changed = False
        if compact and current_name == 'options':
            self.page_stack.set_visible_child_name('main')
            current_name = 'main'
            page_changed = True
        self.desktop_tab_bar.set_visible((not compact) and current_name != 'icon')
        self.custom_assets_row.set_visible(compact)
        self._sync_desktop_tab_buttons(current_name)
        if not page_changed:
            self._notify_navigation_changed()

    def is_subpage_visible(self):
        current_name = self._current_page_name()
        if current_name == 'icon':
            return True
        return self._is_compact_layout() and current_name != 'main'

    def show_main_page(self):
        self.page_stack.set_visible_child_name('main')
        self._update_tabbed_navigation_state()
        self._suspend_change_handlers = False

    def show_asset_page(self, asset_type):
        page_name = 'css_assets' if asset_type == 'css' else 'javascript_assets'
        self.page_stack.set_visible_child_name(page_name)
        self._refresh_asset_page(asset_type)
        self._update_tabbed_navigation_state()

    def on_icon_download_clicked(self, button):
        if self._icon_download_in_progress:
            return
        raw_url = self.address_entry.get_text().strip()
        if not self._looks_ready_for_url_check(raw_url):
            self.refresh_icon_page()
            self._update_export_button_state()
            return
        self._icon_download_in_progress = True
        self._set_icon_download_busy(True, t('icon_page_status_searching'))
        thread = threading.Thread(target=self._load_icon_from_url, args=(raw_url,), daemon=True)
        thread.start()

    def _present_choice_dialog(self, anchor, message, on_result, destructive=False):
        def handle_response(response_id):
            on_result(response_id == 'yes')

        root = self.get_root()
        if root is None:
            on_result(False)
            return

        if hasattr(Adw, 'AlertDialog'):
            dialog = Adw.AlertDialog.new(t('app_title'), message)
            dialog.add_response('no', t('dialog_no'))
            dialog.add_response('yes', t('dialog_yes'))
            dialog.set_default_response('no')
            dialog.set_close_response('no')
            if destructive:
                dialog.set_response_appearance('yes', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.connect('response', lambda _d, response: handle_response(response))
            dialog.present(root)
            return

        dialog = Adw.MessageDialog.new(root, t('app_title'), message)
        dialog.add_response('no', t('dialog_no'))
        dialog.add_response('yes', t('dialog_yes'))
        dialog.set_default_response('no')
        dialog.set_close_response('no')
        if destructive:
            dialog.set_response_appearance('yes', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect('response', lambda _d, response: handle_response(response))
        dialog.present()

    def on_icon_upload_clicked(self, button):
        self._icon_upload_dialog_active = True
        self._cancel_address_timers()
        self._cancel_initial_address_validation()
        self.open_icon_file_dialog()

    def on_icon_delete_clicked(self, button):
        self._present_choice_dialog(button, t('icon_delete_confirm'), lambda confirmed: self.delete_icon() if confirmed else None, destructive=True)

    def delete_icon(self):
        icon_path = self._icon_path()
        if icon_path:
            path = Path(icon_path)
            safe_name = f'webapp-entry-{self.entry.id}'
            managed_dir = get_managed_icon_path(self.entry.title, '.png', self.entry.id).parent
            if path.exists() and path.parent == managed_dir and (path.stem == build_safe_slug(self.entry.title) or path.name.startswith(safe_name)):
                try:
                    path.unlink(missing_ok=True)
                except OSError as error:
                    LOG.warning('Failed to delete icon %s: %s', path, error)
        self._set_option_value(ICON_PATH_KEY, '')
        self.icon_page_status.set_text(t('icon_page_status_deleted'))
        self.refresh_icon_preview()
        self.refresh_icon_page()
        self._update_export_button_state()
        self._emit_visual_changed()
        self.save_desktop_file()

    def _load_icon_from_url(self, url):
        for candidate_url in candidate_urls_for_input(url, prefer_https=True, include_http_fallback=True):
            try:
                path = self._download_favicon(candidate_url)
                if path and Path(path).exists():
                    GLib.idle_add(self._apply_downloaded_icon, str(path))
                    return
            except (OSError, ValueError, urllib.error.URLError) as error:
                LOG.debug('Failed to download favicon for %s: %s', candidate_url, error)
        GLib.idle_add(self._finish_icon_download, t('icon_page_status_download_failed'))

    def _finish_icon_download(self, status_text=None):
        self._icon_download_in_progress = False
        self._compact_mode_override = None
        self._inline_editor_save_source_ids = {'css': 0, 'javascript': 0}
        self._set_icon_download_busy(False)
        self.refresh_icon_page()
        self.icon_download_button.set_sensitive(True)
        if status_text is not None:
            self.icon_page_status.set_text(status_text)
        self._update_export_button_state()
        return False

    def _set_icon_page_status(self, text):
        self.icon_page_status.set_text(text)
        self._update_export_button_state()
        return False

    def _apply_downloaded_icon(self, path):
        temp_path = Path(path)
        try:
            target_path = self._managed_icon_target()
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(temp_path), str(target_path))
            self._apply_icon_path(target_path)
        finally:
            temp_path.unlink(missing_ok=True)
        return self._finish_icon_download(t('icon_page_status_downloaded'))

    def _copy_gfile_to_temp_path(self, file_obj, suffix=''):
        if file_obj is None:
            return None
        local_path = file_obj.get_path()
        if local_path:
            return Path(local_path)
        tmp_name = None
        try:
            stream = file_obj.read(None)
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
                except GLib.Error:
                    pass
            return Path(tmp_name)
        except (GLib.Error, OSError, AttributeError) as error:
            if tmp_name:
                try:
                    Path(tmp_name).unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                uri = file_obj.get_uri()
            except GLib.Error:
                uri = ''
            LOG.warning('Failed to copy selected file %s: %s', uri, error)
            return None

    def _write_text_to_gfile(self, file_obj, text):
        if file_obj is None:
            return False
        payload = text.encode('utf-8')
        local_path = file_obj.get_path()
        if local_path:
            target = Path(local_path)
            if target.suffix.lower() != '.wapp':
                target = target.with_suffix('.wapp')
            target.write_bytes(payload)
            return True
        try:
            basename = file_obj.get_basename() or 'webapp.wapp'
            if not basename.lower().endswith('.wapp'):
                parent = file_obj.get_parent()
                if parent is not None:
                    file_obj = parent.get_child(f'{basename}.wapp')
            stream = file_obj.replace(None, False, Gio.FileCreateFlags.REPLACE_DESTINATION, None)
            stream.write_all(payload, None)
            stream.close(None)
            return True
        except (GLib.Error, OSError, AttributeError) as error:
            try:
                uri = file_obj.get_uri()
            except GLib.Error:
                uri = ''
            LOG.warning('Failed to write selected file %s: %s', uri, error)
            return False

    def _build_file_filter_store(self, patterns):
        store = Gio.ListStore.new(Gtk.FileFilter)
        first_filter = None
        for name, pattern in (patterns or []):
            filt = Gtk.FileFilter()
            filt.set_name(name)
            filt.add_pattern(pattern)
            store.append(filt)
            if first_filter is None:
                first_filter = filt
        return store, first_filter

    def _open_file_dialog(self, title, callback, patterns=None):
        patterns = patterns or []
        parent = self.get_root()
        if hasattr(Gtk, 'FileDialog'):
            dialog = Gtk.FileDialog(title=title, modal=True)
            filters, first_filter = self._build_file_filter_store(patterns)
            if filters.get_n_items() > 0:
                dialog.set_filters(filters)
            if first_filter is not None:
                dialog.set_default_filter(first_filter)

            def handle_open(_dialog, result):
                try:
                    file_obj = _dialog.open_finish(result)
                except GLib.Error:
                    callback(None, Gtk.ResponseType.CANCEL)
                    return
                callback(file_obj)

            dialog.open(parent, None, handle_open)
            return
        dialog = Gtk.FileChooserNative.new(
            title,
            parent,
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
        dialog.connect('response', callback)
        dialog.show()

    def _save_file_dialog(self, title, suggested_name, callback):
        parent = self.get_root()
        if hasattr(Gtk, 'FileDialog'):
            dialog = Gtk.FileDialog(title=title, modal=True, initial_name=suggested_name)

            def handle_save(_dialog, result):
                try:
                    file_obj = _dialog.save_finish(result)
                except GLib.Error:
                    callback(None, Gtk.ResponseType.CANCEL)
                    return
                callback(file_obj)

            dialog.save(parent, None, handle_save)
            return
        dialog = Gtk.FileChooserNative.new(
            title,
            parent,
            Gtk.FileChooserAction.SAVE,
            t('icon_dialog_open'),
            t('icon_dialog_cancel'),
        )
        dialog.set_current_name(suggested_name)
        dialog.connect('response', callback)
        dialog.show()

    def open_icon_file_dialog(self):
        self._open_file_dialog(t('icon_dialog_title'), self.on_icon_file_selected)
        return False

    def on_icon_file_selected(self, result, response=None):
        self._icon_upload_dialog_active = False
        if isinstance(result, Gio.File):
            file_obj = result
        else:
            dialog = result
            if response != Gtk.ResponseType.ACCEPT:
                if dialog is not None:
                    dialog.destroy()
                return
            file_obj = dialog.get_file()
            dialog.destroy()
        local_path = file_obj.get_path() if file_obj is not None else None
        temp_path = self._copy_gfile_to_temp_path(file_obj, '.icon')
        try:
            if temp_path is not None and self._store_icon_file(temp_path):
                self.icon_page_status.set_text(t('icon_page_status_uploaded'))
            else:
                self.icon_page_status.set_text(t('icon_page_status_upload_failed'))
            self.refresh_icon_page()
            self._update_export_button_state()
        finally:
            if temp_path is not None and (not local_path or str(temp_path) != local_path):
                temp_path.unlink(missing_ok=True)

    def on_export_webapp_clicked(self, button):
        export_date = datetime.now().strftime('%Y-%m-%d')
        base_name = (self.entry.title or 'webapp').strip() or 'webapp'
        suggested_name = f"{base_name}_{export_date}.wapp"
        self._save_file_dialog(t('export_webapp_button'), suggested_name, self.on_export_wapp_selected)

    def on_export_wapp_selected(self, result, response=None):
        if isinstance(result, Gio.File):
            file_obj = result
        else:
            dialog = result
            if response != Gtk.ResponseType.ACCEPT:
                if dialog is not None:
                    dialog.destroy()
                return
            file_obj = dialog.get_file()
            dialog.destroy()
        try:
            ok = self._write_text_to_gfile(file_obj, json.dumps(self._build_wapp_payload(), indent=2))
            self._set_detail_action_status(t('export_webapp_success') if ok else t('export_webapp_failed'))
        except OSError as error:
            LOG.warning('Failed to export .wapp file: %s', error)
            self._set_detail_action_status(t('export_webapp_failed'))

    def on_import_webapp_clicked(self, button):
        self._open_file_dialog(
            t('import_webapp_button'),
            self.on_import_wapp_selected,
            patterns=[(t('wapp_filter_name'), '*.wapp')],
        )

    def on_import_wapp_selected(self, result, response=None):
        if isinstance(result, Gio.File):
            file_obj = result
        else:
            dialog = result
            if response != Gtk.ResponseType.ACCEPT:
                if dialog is not None:
                    dialog.destroy()
                return
            file_obj = dialog.get_file()
            dialog.destroy()
        local_path = file_obj.get_path() if file_obj is not None else None
        temp_path = self._copy_gfile_to_temp_path(file_obj, '.wapp')
        try:
            if temp_path is not None:
                payloads = load_import_payloads_from_path(temp_path)
                if len(payloads) > 1:
                    self._set_detail_action_status(t('import_bundle_use_main_import'))
                elif self._apply_wapp_payload(payloads[0]):
                    self._set_detail_action_status(t('import_webapp_success'))
                else:
                    self._set_detail_action_status(t('import_webapp_failed'))
            else:
                self._set_detail_action_status(t('import_webapp_failed'))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            path_for_log = local_path or (str(temp_path) if temp_path else '')
            LOG.warning('Failed to import .wapp file %s: %s', path_for_log, error)
            self._set_detail_action_status(t('import_webapp_failed'))
        finally:
            if temp_path is not None and (not local_path or str(temp_path) != local_path):
                temp_path.unlink(missing_ok=True)

    def on_delete_profile_clicked(self, button):
        self._present_choice_dialog(button, t('profile_delete_confirm'), self._handle_delete_profile_confirmed, destructive=True)

    def _handle_delete_profile_confirmed(self, confirmed):
        if not confirmed:
            return
        profile_path = self._get_option_value(PROFILE_PATH_KEY) or ''
        profile_name = self._get_option_value(PROFILE_NAME_KEY) or ''
        try:
            delete_managed_browser_profiles(
                self.entry.title,
                LOG,
                stored_profile_path=profile_path,
                stored_profile_name=profile_name,
            )
            self._add_options({
                PROFILE_NAME_KEY: '',
                PROFILE_PATH_KEY: '',
                'EngineID': '',
                'EngineName': '',
                USER_AGENT_NAME_KEY: '',
                USER_AGENT_VALUE_KEY: '',
            })
            self._refresh_header_meta()
            self.engine_dropdown.set_selected(0)
            self.refresh_user_agent_options()
            self._update_browser_dependent_controls()
            self._refresh_profile_button_label()
            self._set_detail_action_status(t('profile_delete_success'))
            self.save_desktop_file()
            GLib.idle_add(self._emit_visual_changed)
        except (OSError, ValueError) as error:
            LOG.warning('Failed to delete managed profile for entry %s: %s', self.entry.id, error)
            self._set_detail_action_status(t('profile_delete_failed'))


    def _set_plugin_activity(self, message='', active=False):
        self.plugin_activity_label.set_text('')
        self.plugin_activity_row.set_visible(False)
        if active:
            self.plugin_activity_spinner.start()
        else:
            self.plugin_activity_spinner.stop()
            self.plugin_activity_label.set_text('')

    def _plugin_option_name(self, option_key):
        if option_key == OPTION_ADBLOCK_KEY:
            return 'adblock'
        if option_key == OPTION_SWIPE_KEY:
            return 'swipe'
        return ''

    def _run_plugin_save_async(self, option_key):
        engine = self._get_current_engine()
        if browser_family_for_engine(engine) != 'firefox':
            self.save_desktop_file()
            return
        option_name = self._plugin_option_name(option_key)
        if not option_name:
            self.save_desktop_file()
            return
        self._plugin_operation_serial += 1
        serial = self._plugin_operation_serial
        self._plugin_operation_in_progress = True
        previous_profile_path = (self._get_option_value(PROFILE_PATH_KEY) or '').strip()
        for sw in self.switches.values():
            sw.set_sensitive(False)
        self._set_plugin_activity(t('plugin_installing'), active=True)
        self._set_inline_busy(True, t('plugin_installing'))
        self._show_plugin_banner(t('plugin_install_info'))

        def worker():
            error_text = ''
            profile_info = None
            export_result = None
            try:
                profile_info = ensure_browser_profile(
                    self.entry.title,
                    engine.get('command') or '',
                    LOG,
                    stored_profile_name=self._get_option_value(PROFILE_NAME_KEY) or '',
                    stored_profile_path=previous_profile_path,
                )
                if profile_info:
                    apply_profile_settings(profile_info, self._options_dict(), LOG)
                    needs_export = False
                    new_profile_path = (profile_info.get('profile_path') or '').strip()
                    if new_profile_path and new_profile_path != previous_profile_path:
                        needs_export = True
                    else:
                        target_path = get_expected_desktop_path(self.entry.title)
                        if target_path is not None and not target_path.exists():
                            needs_export = True
                    if needs_export:
                        export_result = export_desktop_file(self.entry, self._options_dict(), self.engines_list, LOG)
            except OSError as error:
                error_text = str(error)
                LOG.error('Failed to apply Firefox plugin change for entry %s: %s', self.entry.id, error)
            except (ValueError, OSError) as error:
                error_text = str(error)
                LOG.error('Unexpected Firefox plugin failure for entry %s: %s', self.entry.id, error, exc_info=True)

            def finish():
                if serial != self._plugin_operation_serial:
                    return False
                updates = {}
                if isinstance(profile_info, dict):
                    updates.update({
                        PROFILE_NAME_KEY: profile_info.get('profile_name', '') or '',
                        PROFILE_PATH_KEY: profile_info.get('profile_path', '') or '',
                    })
                if isinstance(export_result, dict):
                    normalized_address = export_result.get('normalized_address', '') or ''
                    if normalized_address and normalized_address != self.address_entry.get_text().strip():
                        updates[ADDRESS_KEY] = normalized_address
                if updates:
                    self._add_options(updates)
                self._reload_options_cache_from_db()
                self._apply_option_values_to_controls()
                for name, sw in self.switches.items():
                    if name in self._visible_option_names_in_order():
                        sw.set_sensitive(bool(self._get_current_engine()))
                self._plugin_operation_in_progress = False
                self._set_inline_busy(False)
                plugin_profile = self._get_option_value(PROFILE_PATH_KEY) or ''
                enabled = self._get_option_value(option_key) == '1'
                installed = firefox_extension_installed(plugin_profile, option_name) if plugin_profile else False
                verified_value = '1' if installed else '0'
                if plugin_profile:
                    try:
                        verified_state = read_profile_settings(plugin_profile, 'firefox')
                        verified_value = '1' if str(verified_state.get(option_key, verified_value)) == '1' else '0'
                    except (OSError, ValueError, json.JSONDecodeError) as error:
                        LOG.warning('Failed to verify Firefox plugin state for entry %s: %s', self.entry.id, error)
                if verified_value != self._get_option_value(option_key):
                    self._add_options({option_key: verified_value})
                    enabled = verified_value == '1'
                    installed = enabled
                self._set_plugin_activity('', active=False)
                if error_text == 'unsigned-extension-payload':
                    self._show_plugin_banner(t('plugin_install_unsigned'), timeout_ms=4200)
                elif error_text:
                    self._show_plugin_banner(t('plugin_install_failed'))
                elif enabled and installed:
                    self._show_plugin_banner(t('plugin_install_ready_restart'))
                elif enabled and not installed:
                    self._show_plugin_banner(t('plugin_install_failed'))
                elif not enabled and not installed:
                    self._show_plugin_banner(t('plugin_remove_ready_restart'))
                if self.on_title_changed:
                    self.on_title_changed(self.entry)
                GLib.idle_add(self._emit_visual_changed)
                return False

            GLib.idle_add(finish)

        threading.Thread(target=worker, daemon=True).start()

    def save_desktop_file(self):
        try:
            self._sync_icon_filename()
            result = export_desktop_file(self.entry, self._options_dict(), self.engines_list, LOG)
            if isinstance(result, dict):
                if result.get('profile_migrated'):
                    self._show_plugin_banner(t('profile_import_completed'))
                updates = {
                    PROFILE_NAME_KEY: result.get('profile_name', '') or '',
                    PROFILE_PATH_KEY: result.get('profile_path', '') or '',
                }
                normalized_address = result.get('normalized_address', '') or ''
                if normalized_address and normalized_address != self.address_entry.get_text().strip():
                    updates[ADDRESS_KEY] = normalized_address
                self._add_options(updates)
                if normalized_address and normalized_address != self.address_entry.get_text().strip():
                    self._suspend_address_processing = True
                    try:
                        self.address_entry.set_text(normalized_address)
                    finally:
                        self._suspend_address_processing = False
                    self._trigger_address_validation(normalized_address, debounce=False, export_after_validation=False)
            self._reload_options_cache_from_db()
            self._apply_option_values_to_controls()
            if self.on_title_changed:
                self.on_title_changed(self.entry)
        except OSError as error:
            LOG.error('Failed to export desktop file for entry %s: %s', self.entry.id, error)

    def _is_only_https_enabled(self):
        switch = self.switches.get(ONLY_HTTPS_KEY)
        return bool(switch and switch.get_active())

    def _normalize_address_for_ui(self, value):
        value = (value or '').strip()
        if self._is_only_https_enabled() and value.startswith('http://'):
            value = 'https://' + value[len('http://'):]
        return value

    def on_switch_toggled(self, switch, pspec):
        self.entry.active = switch.get_active()
        self.db.cursor.execute('UPDATE entries SET active=? WHERE id=?', (1 if switch.get_active() else 0, self.entry.id))
        self.db.conn.commit()
        self.save_desktop_file()
        GLib.idle_add(self._emit_visual_changed)

    def on_name_changed(self, entry_widget):
        new_title = entry_widget.get_text().strip()
        self.entry.title = new_title
        self._refresh_header_meta()
        self.db.cursor.execute('UPDATE entries SET title=? WHERE id=?', (new_title, self.entry.id))
        self.db.conn.commit()
        self._sync_icon_filename()
        if self.on_title_changed:
            self.on_title_changed(self.entry)
        self._update_export_button_state()
        self.save_desktop_file()

    def on_description_changed(self, entry_widget):
        new_description = entry_widget.get_text().strip()
        self.entry.description = new_description
        self.db.cursor.execute('UPDATE entries SET description=? WHERE id=?', (new_description, self.entry.id))
        self.db.conn.commit()
        self._update_export_button_state()

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

    def on_engine_changed(self, dropdown, pspec):
        if self._suspend_change_handlers:
            return
        previous_engine = self._engine_by_id(self._get_option_value('EngineID'))
        previous_family = browser_family_for_engine(previous_engine)
        if previous_family != 'generic':
            self._sync_current_browser_state(commit=True)
        engine = self._get_current_engine()
        if engine is None:
            self._add_options({
                'EngineID': '',
                'EngineName': '',
                USER_AGENT_NAME_KEY: '',
                USER_AGENT_VALUE_KEY: '',
            })
        else:
            self._add_options({
                'EngineID': str(engine['id']),
                'EngineName': engine['name'],
            })
            self._restore_browser_state_for_family(browser_family_for_engine(engine))
        self.refresh_user_agent_options()
        self.refresh_mode_options()
        self._rebuild_options_layout(force=True)
        self._update_browser_dependent_controls()
        self._sync_current_browser_state(commit=True)
        if engine is None:
            self._update_export_button_state()
            return
        self._set_inline_busy(True, t('engine_switch_loading'))
        def worker():
            error_text = ''
            try:
                self._sync_icon_filename()
                result = export_desktop_file(self.entry, self._options_dict(), self.engines_list, LOG)
            except OSError as error:
                error_text = str(error)
                LOG.error('Failed to export desktop file for entry %s after engine change: %s', self.entry.id, error)
                result = None
            def finish():
                self._set_inline_busy(False)
                if isinstance(result, dict):
                    updates = {
                        PROFILE_NAME_KEY: result.get('profile_name', '') or '',
                        PROFILE_PATH_KEY: result.get('profile_path', '') or '',
                    }
                    normalized_address = result.get('normalized_address', '') or ''
                    if normalized_address and normalized_address != self.address_entry.get_text().strip():
                        updates[ADDRESS_KEY] = normalized_address
                    self._add_options(updates)
                    self._refresh_header_meta()
                    if result.get('profile_migrated'):
                        self._show_plugin_banner(t('profile_import_completed'))
                    if normalized_address and normalized_address != self.address_entry.get_text().strip():
                        self._suspend_address_processing = True
                        try:
                            self.address_entry.set_text(normalized_address)
                        finally:
                            self._suspend_address_processing = False
                        self._trigger_address_validation(normalized_address, debounce=False, export_after_validation=False)
                    self._refresh_profile_button_label()
                if error_text:
                    self._set_detail_action_status(error_text)
                if self.on_title_changed:
                    self.on_title_changed(self.entry)
                GLib.idle_add(self._emit_visual_changed)
                return False
            GLib.idle_add(finish)
        threading.Thread(target=worker, daemon=True).start()

    def on_user_agent_changed(self, dropdown, pspec):
        if self._suspend_change_handlers:
            return
        engine = self._get_current_engine()
        options = list(self.engine_user_agents.get(engine['id'], [])) if engine else []
        selected = dropdown.get_selected()
        if selected <= 0:
            self._add_options({
                USER_AGENT_NAME_KEY: '',
                USER_AGENT_VALUE_KEY: '',
            })
        else:
            user_agent = options[selected - 1]
            self._add_options({
                USER_AGENT_NAME_KEY: user_agent['name'],
                USER_AGENT_VALUE_KEY: user_agent['value'],
            })
        self.save_desktop_file()


    def on_color_scheme_changed(self, dropdown, pspec):
        if self._suspend_change_handlers:
            return
        selected = dropdown.get_selected()
        try:
            value = self.color_scheme_values[selected]
        except IndexError:
            value = 'auto'
        self._set_option_value(COLOR_SCHEME_KEY, value)
        self.save_desktop_file()

    def _apply_profile_settings_only(self):
        engine = self._get_current_engine()
        if engine is None:
            self._reload_options_cache_from_db()
            self._apply_option_values_to_controls()
            return
        result_updates = {}
        profile_info = None
        try:
            profile_info = ensure_browser_profile(
                self.entry.title,
                engine.get('command') or '',
                LOG,
                stored_profile_name=self._get_option_value(PROFILE_NAME_KEY) or '',
                stored_profile_path=self._get_option_value(PROFILE_PATH_KEY) or '',
            )
            if profile_info:
                apply_profile_settings(profile_info, self._options_dict(), LOG)
                result_updates = {
                    PROFILE_NAME_KEY: profile_info.get('profile_name', '') or '',
                    PROFILE_PATH_KEY: profile_info.get('profile_path', '') or '',
                }
                self._add_options(result_updates)
                if profile_info.get('profile_migrated'):
                    self._show_plugin_banner(t('profile_import_completed'))
        except OSError as error:
            LOG.error('Failed to apply profile settings for entry %s: %s', self.entry.id, error)
        self._reload_options_cache_from_db()
        self._apply_option_values_to_controls()
        self._refresh_profile_button_label()
        if self.on_title_changed:
            self.on_title_changed(self.entry)

    def save_boolean_option(self, name, value):
        if self._suspend_change_handlers:
            return
        option_spec = OPTION_SPEC_BY_KEY.get(name)
        option_kind = option_spec.kind if option_spec else ''
        self._set_option_value(name, self._store_boolean_option_value(name, value))
        effective_https_enabled = bool(
            (name == ONLY_HTTPS_KEY and value)
            or (name == OPTION_FORCE_PRIVACY_KEY and value and self._current_browser_family() in {'firefox', 'chrome', 'chromium'})
        )
        if effective_https_enabled:
            normalized = self._normalize_address_for_ui(self.address_entry.get_text())
            if normalized != self.address_entry.get_text():
                self.address_entry.set_text(normalized)
            else:
                self._set_option_value(ADDRESS_KEY, normalized)
                self._update_url_status(normalized)
        if self._plugin_operation_in_progress and option_kind == 'extension_action':
            GLib.idle_add(self._emit_visual_changed)
            return
        if option_kind == 'extension_action':
            self._run_plugin_save_async(name)
        elif option_kind in {'profile_setting', 'shutdown_cleanup', 'macro', 'app_logic'}:
            self._apply_profile_settings_only()
        else:
            self.save_desktop_file()
        GLib.idle_add(self._emit_visual_changed)


    def reload_from_db(self):
        self._reload_options_cache_from_db()
        self._apply_option_values_to_controls()

    def on_swipe(self, gesture, velocity_x, velocity_y):
        if velocity_x > 0:
            current_name = self._current_page_name()
            if current_name in {'css_assets', 'javascript_assets'}:
                self.on_back()
            elif self.is_subpage_visible():
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


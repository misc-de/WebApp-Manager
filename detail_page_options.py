import json
import threading
from gi.repository import GLib, Gtk, Pango
from browser_option_logic import (
    apply_semantic_mode,
    mode_option_keys,
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
from browser_option_registry import OPTION_CATEGORY_ORDER, OPTION_CATEGORY_LABEL_KEYS, option_category
from browser_profiles import apply_profile_settings, ensure_browser_profile, firefox_extension_installed, read_profile_settings
from desktop_entries import export_desktop_file, get_expected_desktop_path
from distro_utils import is_furios_distribution
from detail_page_option_state import (
    coerce_option_updates,
    configured_mode_values_for_engine,
    current_mode_value,
    normalize_mode_value,
    restored_browser_state,
    store_boolean_option_value,
    sync_browser_state_key,
    ui_boolean_option_active,
)
from i18n import t
from input_validation import DESKTOP_CHROME_USER_AGENT
from logger_setup import get_logger
from option_config import option_names
from webapp_constants import (
    ADDRESS_KEY,
    APP_MODE_KEY,
    COLOR_SCHEME_KEY,
    DEFAULT_ZOOM_KEY,
    ONLY_HTTPS_KEY,
    OPTION_ADBLOCK_KEY,
    OPTION_DISABLE_AI_KEY,
    OPTION_FORCE_PRIVACY_KEY,
    OPTION_KEEP_IN_BACKGROUND_KEY,
    OPTION_SWIPE_KEY,
    PROFILE_NAME_KEY,
    PROFILE_PATH_KEY,
    USER_AGENT_NAME_KEY,
    USER_AGENT_VALUE_KEY,
)

LOG = get_logger(__name__)


class DetailPageOptionsMixin:
    def _option_names_in_order(self):
            return list(self.option_names)

    def _ui_boolean_option_active(self, option_name):
            return ui_boolean_option_active(option_name, self._get_option_value(option_name))

    def _store_boolean_option_value(self, option_name, active):
            return store_boolean_option_value(option_name, active)

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

    def _current_mode_value(self):
            return current_mode_value(self._options_cache)

    def _current_mode_index(self):
            value = self._current_mode_value()
            try:
                return self.mode_values.index(value)
            except ValueError:
                return 0

    def _apply_mode_value(self, mode_value):
            selected = {key: value for key, value in apply_semantic_mode({}, mode_value).items() if key in mode_option_keys()}
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
            return sync_browser_state_key(family or self._current_browser_family())

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
            self._add_options(restored_browser_state(self._options_cache, family))

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

                stored_default_zoom = (self._get_option_value(DEFAULT_ZOOM_KEY) or '100').strip()
                try:
                    default_zoom_index = self.default_zoom_values.index(stored_default_zoom)
                except ValueError:
                    default_zoom_index = self.default_zoom_values.index('100')
                self.default_zoom_dropdown.set_selected(default_zoom_index)

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

    def refresh_user_agent_options(self):
            engine = self._get_current_engine()
            options = list(self.engine_user_agents.get(engine['id'], [])) if engine else []
            labels = [t('user_agent_none')] + [item['name'] for item in options]
            selected_index, _selected_item = self._resolve_user_agent_selection(engine, options, persist_default=True)
            new_dropdown = Gtk.DropDown.new_from_strings(labels)
            new_dropdown.connect('notify::selected', self.on_user_agent_changed)
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
            return normalize_mode_value(value)

    def _mode_label_for_value(self, value):
            mapping = {
                'standard': t('mode_standard'),
                'kiosk': t('mode_kiosk'),
                'app': t('mode_app'),
                'seamless': t('mode_seamless'),
            }
            return mapping.get(value, t('mode_standard'))

    def _configured_mode_values_for_engine(self, engine):
            return configured_mode_values_for_engine(self.config, engine)

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
            new_dropdown.connect('notify::selected', self.on_mode_changed)
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

    def _coerce_option_updates(self, updates):
            return coerce_option_updates(self._current_browser_family(), updates)

    def _add_options(self, updates):
            clean_updates = self._coerce_option_updates(updates)
            if clean_updates:
                self._options_cache.update(clean_updates)
                self.db.add_options(self.entry.id, clean_updates)
                if any((key in browser_managed_option_keys()) and (not key.startswith('__BrowserState.')) for key in clean_updates):
                    self._sync_current_browser_state(commit=True)

    def _supported_option_names(self, engine):
            if not engine:
                return set()
            family = browser_family_for_engine(engine)
            supported = supported_browser_option_keys(family, visible_only=True)
            if family == 'firefox' and OPTION_KEEP_IN_BACKGROUND_KEY in supported and not is_furios_distribution():
                supported.discard(OPTION_KEEP_IN_BACKGROUND_KEY)
            return {name for name in self._option_names_in_order() if name in supported}

    def _update_export_button_state(self):
            if hasattr(self, 'export_button'):
                self.export_button.set_sensitive(self._has_exportable_webapp())

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
            self.db.update_entry(self.entry.id, active=bool(switch.get_active()))
            self.save_desktop_file()
            GLib.idle_add(self._emit_visual_changed)

    def on_engine_changed(self, dropdown, pspec):
            if self._suspend_change_handlers:
                return
            previous_mode_state = {
                'Kiosk': self._get_option_value('Kiosk') or '0',
                APP_MODE_KEY: self._get_option_value(APP_MODE_KEY) or '0',
                'Frameless': self._get_option_value('Frameless') or '0',
            }
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
                self._add_options(previous_mode_state)
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

    def on_default_zoom_changed(self, dropdown, pspec):
            if self._suspend_change_handlers:
                return
            selected = dropdown.get_selected()
            try:
                value = self.default_zoom_values[selected]
            except IndexError:
                value = '100'
            self._set_option_value(DEFAULT_ZOOM_KEY, value)
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

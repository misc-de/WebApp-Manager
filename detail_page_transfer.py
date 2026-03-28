from browser_profiles import delete_managed_browser_profiles
from desktop_entries import export_desktop_file, get_expected_desktop_path
from i18n import t
from logger_setup import get_logger
import binascii
import json
from datetime import datetime
from gi.repository import Gio, GLib, Gtk
from browser_option_logic import normalize_option_dict
from icon_pipeline import is_svg_support_missing_error, normalize_icon_bytes_to_png
from input_validation import load_and_normalize_wapp_payload_from_path, load_import_payloads_from_path, normalize_wapp_payload, payload_contains_inline_javascript
from webapp_constants import ADDRESS_KEY, COLOR_SCHEME_KEY, DEFAULT_ZOOM_KEY, ICON_PATH_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY, USER_AGENT_NAME_KEY, USER_AGENT_VALUE_KEY
from wapp_transfer import build_wapp_export_payload

LOG = get_logger(__name__)


class DetailPageTransferMixin:
    def _build_wapp_payload(self):
        return build_wapp_export_payload(
            title=self.entry.title or '',
            description=self.entry.description or '',
            active=bool(self.entry.active),
            options_dict=self._options_dict(),
        )

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

        inline_css_value = str(options.get('Inline Custom CSS', '') or '').replace('\r\n', '\n').replace('\r', '\n')
        inline_css_hash = str(options.get('Inline Custom CSS Hash', '') or '').strip()
        inline_js_value = str(options.get('Inline Custom JavaScript', '') or '').replace('\r\n', '\n').replace('\r', '\n')
        inline_js_hash = str(options.get('Inline Custom JavaScript Hash', '') or '').strip()

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

        self._set_option_value('Inline Custom CSS', inline_css_value, commit=False)
        self._set_option_value('Inline Custom CSS Hash', inline_css_hash, commit=False)
        self._set_option_value('Inline Custom JavaScript', inline_js_value, commit=False)
        self._set_option_value('Inline Custom JavaScript Hash', inline_js_hash, commit=False)

        color_scheme_value = str(options.get(COLOR_SCHEME_KEY, 'auto')).strip().lower()
        if color_scheme_value in self.color_scheme_values:
            self.color_scheme_dropdown.set_selected(self.color_scheme_values.index(color_scheme_value))

        default_zoom_value = str(options.get(DEFAULT_ZOOM_KEY, '100')).strip()
        if default_zoom_value in self.default_zoom_values:
            self.default_zoom_dropdown.set_selected(self.default_zoom_values.index(default_zoom_value))

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
                if is_svg_support_missing_error(error):
                    self._show_plugin_banner(t('svg_import_requires_cairo'), timeout_ms=4200)
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
        self._refresh_asset_pages()
        self._update_export_button_state()
        self._emit_visual_changed()
        self._update_browser_dependent_controls()
        self._refresh_profile_button_label()
        self.save_desktop_file()
        return True

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
                elif payload_contains_inline_javascript(payloads[0]):
                    self._present_choice_dialog(file_obj, t('import_javascript_warning'), lambda confirmed: self._complete_single_wapp_import(payloads[0], confirmed), destructive=False)
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


    def _complete_single_wapp_import(self, payload, confirmed):
        if not confirmed:
            self._set_detail_action_status(t('import_webapp_failed'))
            return
        if self._apply_wapp_payload(payload):
            self._set_detail_action_status(t('import_webapp_success'))
        else:
            self._set_detail_action_status(t('import_webapp_failed'))

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
            if self.on_visual_changed:
                self.on_visual_changed(self.entry)
            if self.on_title_changed:
                self.on_title_changed(self.entry)
        except OSError as error:
            LOG.error('Failed to export desktop file for entry %s: %s', self.entry.id, error)

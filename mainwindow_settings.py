from pathlib import Path
from types import SimpleNamespace
from gi.repository import Adw, Gio, Gtk, GLib
from custom_assets import count_asset_references, detach_asset_from_entries, format_asset_date, import_custom_asset, list_custom_assets, remove_custom_asset
from desktop_entries import export_desktop_file, exportable_entry
from detail_page import DetailPage
from engine_support import available_engines
from i18n import available_languages, get_app_config, invalidate_i18n_cache, save_app_config, t
from logger_setup import get_logger

LOG = get_logger(__name__)
ENGINES = available_engines()


class MainWindowSettingsMixin:
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
        previous_detail = self._overview_detail_visible_child()
        try:
            previous_visible = self.stack.get_visible_child_name()
        except (AttributeError, TypeError):
            previous_visible = None
        was_settings_overview = previous_detail is getattr(self, 'settings_page', None)
        was_assets_overview = previous_detail is getattr(self, 'settings_assets_page', None)
        old_page = getattr(self, 'settings_page', None)
        if old_page is not None:
            try:
                if self._adaptive_split_enabled:
                    self._remove_overview_page_widget(old_page)
                else:
                    self.stack.remove(old_page)
            except (AttributeError, TypeError):
                pass
        old_assets_page = getattr(self, 'settings_assets_page', None)
        if old_assets_page is not None:
            try:
                if self._adaptive_split_enabled:
                    self._remove_overview_page_widget(old_assets_page)
                else:
                    self.stack.remove(old_assets_page)
            except (AttributeError, TypeError):
                pass
        self.settings_page = self._build_settings_page()
        self.settings_assets_page = self._build_assets_settings_page()
        if self._adaptive_split_enabled:
            self._add_overview_detail_page(self.settings_page, 'settings_page')
            self._add_overview_detail_page(self.settings_assets_page, 'settings_assets_page')
            if was_assets_overview:
                self._set_overview_detail_visible(self.settings_assets_page, t('settings_assets_title'))
            elif was_settings_overview:
                self._set_overview_detail_visible(self.settings_page, t('settings_title'))
        else:
            self.stack.add_named(self.settings_page, 'settings_page')
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
            self.home_button.set_tooltip_text(t('welcome_title'))
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
        try:
            self.sidebar_navigation_page.set_title(t('app_title'))
        except (AttributeError, TypeError):
            pass
        try:
            visible_detail = self._overview_detail_visible_child()
            if isinstance(visible_detail, DetailPage):
                self.content_navigation_page.set_title(visible_detail.entry.title or t('app_title'))
            else:
                self.content_navigation_page.set_title(t('app_title'))
        except (AttributeError, TypeError):
            pass
        self._rebuild_settings_page_view()
        self._show_overview_header()

    def _wrap_page_with_clamp(self, child, maximum_size=760, tightening_threshold=520):
        if hasattr(Adw, 'Clamp'):
            clamp = Adw.Clamp()
            clamp.set_hexpand(True)
            clamp.set_valign(Gtk.Align.START)
            if hasattr(clamp, 'set_maximum_size'):
                clamp.set_maximum_size(maximum_size)
            if hasattr(clamp, 'set_tightening_threshold'):
                clamp.set_tightening_threshold(tightening_threshold)
            clamp.set_child(child)
            return clamp
        return child

    def _build_settings_labeled_row(self, label_text, widget):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        row.set_hexpand(True)
        label = Gtk.Label(label=label_text)
        label.set_xalign(0)
        label.set_wrap(True)
        widget.set_hexpand(True)
        row.append(label)
        row.append(widget)
        return row

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

        self.ui_mode_labels = [t('color_scheme_auto'), t('color_scheme_dark'), t('color_scheme_light')]
        self.ui_mode_values = ['auto', 'dark', 'light']
        self.ui_mode_dropdown = Gtk.DropDown.new_from_strings(self.ui_mode_labels)
        self.ui_mode_dropdown.set_selected(self.ui_mode_values.index(self._appearance_value()))
        self.ui_mode_dropdown.connect('notify::selected', self.on_ui_mode_changed)
        ui_group.append(self._build_settings_labeled_row(t('settings_appearance_label'), self.ui_mode_dropdown))

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
        ui_group.append(self._build_settings_labeled_row(t('settings_language_label'), self.language_dropdown))
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
        export_zip_button.set_hexpand(True)
        export_zip_button.connect('clicked', self.on_export_all_single_file_clicked)
        export_group.append(export_zip_button)
        content.append(export_group)

        about_group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        about_group.add_css_class('preferences-group')
        about_header = Gtk.Label(label=t('settings_about_header'))
        about_header.add_css_class('heading')
        about_header.set_xalign(0)
        about_group.append(about_header)

        self.version_label = Gtk.Label(label=t('settings_about_version', version=self._read_app_version_label()))
        self.version_label.set_xalign(0)
        self.version_label.set_wrap(True)
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
        swipe_back.connect('swipe', lambda _g, vx, _vy: self._return_to_overview_from_settings_assets() if vx > 0 else None)
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
        upload_button.set_hexpand(True)
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
            row.set_valign(Gtk.Align.CENTER)
            row.add_css_class('preferences-group')

            text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            text_box.set_hexpand(True)

            name_label = Gtk.Label(label=str(asset.get('name') or ''), xalign=0)
            name_label.set_hexpand(True)
            name_label.set_wrap(True)
            text_box.append(name_label)

            meta_label = Gtk.Label(label=f"{str(asset.get('type') or '').upper()} · {format_asset_date(asset.get('imported_at'))}", xalign=0)
            meta_label.add_css_class('dim-label')
            meta_label.set_wrap(True)
            text_box.append(meta_label)

            row.append(text_box)

            delete_button = Gtk.Button(icon_name='user-trash-symbolic')
            delete_button.add_css_class('flat')
            delete_button.connect('clicked', lambda button, current_asset_id=asset['id']: self._confirm_delete_custom_asset(button, current_asset_id))
            row.append(delete_button)
            assets_box.append(row)

    def show_assets_settings_page(self, *args):
        if self._adaptive_split_enabled:
            if self._adaptive_narrow_mode and self._adaptive_real_detail_visible() and isinstance(self._overview_detail_visible_child(), DetailPage):
                return
            self._refresh_assets_settings_list()
            self._set_overview_detail_visible(self.settings_assets_page, t('settings_assets_title'))
            self.stack.set_visible_child_name('overview_page')
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
                row = self.db.get_entry(entry_id)
                if row is None:
                    continue
                entry = SimpleNamespace(id=int(row[0]), title=str(row[1] or ''), description=str(row[2] or ''), active=bool(row[3]))
            options = self._get_options_dict(entry_id)
            if exportable_entry(entry, options):
                export_desktop_file(entry, options, ENGINES, LOG)
        self._refresh_assets_settings_list()
        self.show_overlay_notification(t('settings_assets_delete_success', name=str(removed.get('name') or '')), timeout_ms=2600)

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

    def _set_titlebar_button_visibility(self, start_visible, end_visible):
        try:
            self.header_bar.set_show_start_title_buttons(bool(start_visible))
            self.header_bar.set_show_end_title_buttons(bool(end_visible))
        except (AttributeError, TypeError):
            LOG.debug('Failed to adjust titlebar button visibility', exc_info=True)

    def _show_back_only_header(self):
        desktop_mode = bool(self._adaptive_split_enabled and not self._adaptive_narrow_mode)
        if desktop_mode:
            self.header_bar.set_title_widget(None)
            self.search_button.set_visible(True)
            self.refresh_button.set_visible(True)
            self.home_button.set_visible(True)
            self.settings_button.set_visible(True)
            self.assets_button.set_visible(True)
            self.add_button.set_visible(True)
            self.back_button.set_visible(False)
            self._set_titlebar_button_visibility(True, True)
            return
        self.search_button.set_visible(False)
        self.refresh_button.set_visible(False)
        self.home_button.set_visible(False)
        self.settings_button.set_visible(False)
        self.assets_button.set_visible(False)
        self.add_button.set_visible(False)
        self.back_button.set_visible(True)
        self.header_bar.set_title_widget(None)
        self._set_titlebar_button_visibility(True, True)

    def _show_overview_header(self):
        current_detail = self._overview_detail_visible_child()
        if self.search_visible:
            self._show_back_only_header()
            return
        if self._adaptive_split_enabled and self.overview_split_view is not None:
            if self._adaptive_real_detail_visible():
                self._show_back_only_header()
                return
            self.header_bar.set_title_widget(None)
        else:
            self.header_bar.set_title_widget(self.list_title_widget)
        self.search_button.set_visible(True)
        self.refresh_button.set_visible(True)
        self.home_button.set_visible(bool(self._adaptive_split_enabled and not self._adaptive_narrow_mode))
        self.settings_button.set_visible(True)
        self.assets_button.set_visible(True)
        self.add_button.set_visible(True)
        self.back_button.set_visible(False)
        self._set_titlebar_button_visibility(True, True)

    def _restore_overview_header_actions(self):
        self._show_overview_header()

    def _return_to_overview_from_settings_assets(self):
        self._hide_global_toast()
        if self._adaptive_split_enabled:
            self._show_overview_root_page()
            try:
                self.stack.set_visible_child_name('overview_page')
            except (AttributeError, TypeError, GLib.Error):
                pass
            self._restore_overview_header_actions()
            return
        self._restore_overview_header_actions()
        try:
            self.stack.set_visible_child_name('overview_page')
        except (AttributeError, TypeError, GLib.Error):
            pass

    def show_settings_page(self, *args):
        if self._adaptive_split_enabled:
            if self._adaptive_narrow_mode and self._adaptive_real_detail_visible() and isinstance(self._overview_detail_visible_child(), DetailPage):
                return
            self._set_overview_detail_visible(self.settings_page, t('settings_title'))
            self.stack.set_visible_child_name('overview_page')
            return
        self._show_back_only_header()
        self.stack.set_visible_child_name('settings_page')

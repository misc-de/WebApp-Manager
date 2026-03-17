import json
import os
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from gi.repository import Gio, GLib, Gtk
from browser_option_logic import browser_managed_option_keys, browser_state_key, encode_browser_state, mode_option_keys, normalize_option_dict, normalize_option_rows
from database import Database
from detail_page import DetailPage
from app_identity import APP_DB_PATH
from browser_profiles import read_profile_settings
from desktop_entries import export_desktop_file, exportable_entry
from engine_support import available_engines
from i18n import t
from input_validation import load_import_payloads_from_path
from logger_setup import get_logger
from webapp_constants import PROFILE_PATH_KEY

LOG = get_logger(__name__)
ENGINES = available_engines()


class MainWindowProfileImportMixin:
    def on_refresh_clicked(self, button):
        if self._profile_resync_running:
            return
        message = t('profile_resync_confirm_body')
        self._present_choice_dialog(message, lambda accepted: self._start_profile_resync() if accepted else None, destructive=False)

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
                        updates = {key: value for key, value in normalized_state.items() if key in browser_managed_option_keys() and key not in mode_option_keys()}
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
            entry = SimpleNamespace(id=new_id, title='', description='', active=True)

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

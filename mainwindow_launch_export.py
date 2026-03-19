from datetime import datetime
from gi.repository import Gio, GLib, Gtk

from desktop_entries import exportable_entry
from engine_support import available_engines
from i18n import t
from logger_setup import get_logger

LOG = get_logger(__name__)
ENGINES = available_engines()


class MainWindowLaunchExportMixin:
    def _build_export_payload_for_entry(self, entry):
        return self.export_service.build_export_payload_for_entry(entry)

    def _iter_exportable_entries(self):
        items = []
        for index in range(self.entries_store.get_n_items()):
            entry = self.entries_store.get_item(index)
            options = self._get_options_dict(entry.id)
            if exportable_entry(entry, options):
                items.append(entry)
        return items

    def _safe_export_name(self, entry):
        return self.export_service.safe_export_name(entry)

    def on_export_all_single_file_clicked(self, _button):
        entries = self._iter_exportable_entries()
        if not entries:
            self.show_overlay_notification(t('settings_export_none'), timeout_ms=2600)
            return
        if not hasattr(Gtk, 'FileDialog'):
            self.show_overlay_notification(t('settings_export_failed'), timeout_ms=3200)
            return
        export_date = datetime.now().strftime('%Y-%m-%d')
        dialog = Gtk.FileDialog(title=t('settings_export_dialog_title'), modal=True, initial_name=f'webapps_export_{export_date}.wapp')

        def handle_save(_dialog, result):
            try:
                file_obj = _dialog.save_finish(result)
            except GLib.Error:
                self._on_export_all_single_file_response(None, None, entries)
                return
            self._on_export_all_single_file_response(file_obj, None, entries)

        dialog.save(self, None, handle_save)

    def _build_export_bundle_payload(self, entries):
        return self.export_service.build_export_bundle_payload(entries)

    def _on_export_all_single_file_response(self, file_obj, response, entries):
        try:
            if file_obj is None:
                return
            if file_obj.get_path() is None:
                self.show_overlay_notification(t('settings_export_path_error'), timeout_ms=2600)
                return
            self.export_service.write_bundle(file_obj.get_path(), entries)
            self.show_overlay_notification(t('settings_export_success', count=len(entries)), timeout_ms=2600)
        except (OSError, TypeError, ValueError) as error:
            LOG.error('Failed to export all WebApps into single file: %s', error, exc_info=True)
            self.show_overlay_notification(t('settings_export_failed'), timeout_ms=3200)

    def _launch_command_args(self, argv):
        return self.subprocess_runner.popen(argv)

    def _launch_entry_from_icon(self, entry):
        try:
            self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
        except (AttributeError, TypeError):
            pass
        self.launch_entry(entry)

    def _resolve_desktop_path_for_entry(self, entry):
        return self.launch_service.resolve_desktop_path_for_entry(entry)

    def launch_entry(self, entry):
        self.launch_service.launch_entry(entry)

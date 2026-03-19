import base64
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from gi.repository import Gio, GLib, Gtk
from desktop_entries import build_launch_command, export_desktop_file, exportable_entry, get_expected_desktop_path, list_managed_desktop_files
from engine_support import available_engines
from i18n import t
from input_validation import sanitize_desktop_value, validate_icon_source_path
from logger_setup import get_logger
from webapp_constants import ICON_PATH_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY

LOG = get_logger(__name__)
ENGINES = available_engines()


class MainWindowLaunchExportMixin:
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
            options = self._get_options_dict(entry.id, force_refresh=True)
            launch_spec = build_launch_command(entry, options, ENGINES, LOG, prepare_profile=True)
            if launch_spec is None:
                LOG.warning('Refusing to launch entry %s because no validated launch command could be built', getattr(entry, 'id', 'unknown'))
                return
            self._launch_command_args(launch_spec['argv'])


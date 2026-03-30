import json
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from gi.repository import Gdk, Gio, GLib, Gtk
from desktop_entries import build_launch_command, export_desktop_file, exportable_entry, get_expected_desktop_path, list_managed_desktop_files
from engine_support import available_engines
from i18n import t
from input_validation import sanitize_desktop_value
from logger_setup import get_logger
from webapp_constants import OPTION_PREVENT_MULTIPLE_STARTS_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY
from wapp_transfer import build_wapp_export_bundle_payload, build_wapp_export_payload

LOG = get_logger(__name__)
ENGINES = available_engines()


class MainWindowLaunchExportMixin:
    def _process_argv_for_pid(self, pid):
            try:
                raw = Path(f'/proc/{int(pid)}/cmdline').read_bytes()
            except (OSError, ValueError, TypeError):
                return []
            if not raw:
                return []
            return [token for token in raw.decode('utf-8', errors='ignore').split('\x00') if token]

    def _profile_path_in_argv(self, argv, profile_path):
            candidate = str(profile_path or '').strip()
            if not candidate or not argv:
                return False
            for index, token in enumerate(argv):
                if token in {'-profile', '--profile', '--user-data-dir'}:
                    if index + 1 < len(argv) and str(argv[index + 1]).strip() == candidate:
                        return True
                    continue
                for prefix in ('-profile=', '--profile=', '--user-data-dir='):
                    if str(token).startswith(prefix) and str(token).split('=', 1)[1].strip() == candidate:
                        return True
            return False

    def _system_process_running_for_profile(self, profile_path):
            candidate = str(profile_path or '').strip()
            if not candidate:
                return None
            current_pid = os.getpid()
            try:
                proc_entries = os.listdir('/proc')
            except OSError:
                return None
            for name in proc_entries:
                if not name.isdigit():
                    continue
                try:
                    pid = int(name)
                except ValueError:
                    continue
                if pid == current_pid:
                    continue
                argv = self._process_argv_for_pid(pid)
                if self._profile_path_in_argv(argv, candidate):
                    return {
                        'pid': pid,
                        'argv': argv,
                    }
            return None

    def _running_launch_process_for_entry(self, entry_id):
            try:
                running = self._running_launch_processes
            except AttributeError:
                running = {}
                self._running_launch_processes = running
            state = running.get(entry_id)
            if not state:
                return None
            process = state.get('process')
            if process is None:
                running.pop(entry_id, None)
                return None
            try:
                return_code = process.poll()
            except Exception:
                LOG.exception('Failed to poll stored launch process for entry_id=%s', entry_id)
                running.pop(entry_id, None)
                return None
            if return_code is None:
                return state
            running.pop(entry_id, None)
            return None

    def _launch_env_for_command(self, argv):
            env = os.environ.copy()
            display = env.get('DISPLAY')
            wayland_display = env.get('WAYLAND_DISPLAY')
            if display or wayland_display:
                return env

            gdk_display = None
            try:
                gdk_display = self.get_display()
            except (AttributeError, TypeError):
                gdk_display = None
            if gdk_display is None:
                try:
                    gdk_display = Gdk.Display.get_default()
                except (AttributeError, TypeError):
                    gdk_display = None

            display_name = ''
            if gdk_display is not None:
                try:
                    display_name = str(gdk_display.get_name() or '').strip()
                except (AttributeError, TypeError):
                    display_name = ''

            engine = str(argv[0] or '').lower() if argv else ''
            if display_name.startswith(':'):
                env['DISPLAY'] = display_name
                return env
            if display_name:
                env['WAYLAND_DISPLAY'] = display_name
                if 'firefox' in engine:
                    env['MOZ_ENABLE_WAYLAND'] = '1'
                    env.setdefault('GDK_BACKEND', 'wayland')
                return env
            return env

    def _build_export_payload_for_entry(self, entry):
            return build_wapp_export_payload(
                title=entry.title or '',
                description=entry.description or '',
                active=bool(entry.active),
                options_dict=self._get_options_dict(entry.id),
            )

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
            return build_wapp_export_bundle_payload(
                [self._build_export_payload_for_entry(entry) for entry in entries]
            )

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

    def _launch_command_args(self, argv, *, entry=None):
            if not argv:
                return False
            try:
                env = self._launch_env_for_command(argv)
                engine = str(argv[0] or '').lower()
                display = env.get('DISPLAY')
                wayland_display = env.get('WAYLAND_DISPLAY')
                if 'firefox' in engine and not display and wayland_display:
                    env['MOZ_ENABLE_WAYLAND'] = '1'
                    env.setdefault('GDK_BACKEND', 'wayland')
                process = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=None,
                    stderr=None,
                    close_fds=True,
                    start_new_session=True,
                    cwd=str(Path.home()),
                    env=env,
                )
                entry_id = getattr(entry, 'id', None)
                if entry_id is not None:
                    try:
                        running = self._running_launch_processes
                    except AttributeError:
                        running = {}
                        self._running_launch_processes = running
                    running[entry_id] = {
                        'process': process,
                        'argv': list(argv),
                        'title': getattr(entry, 'title', ''),
                    }

                def _monitor_process():
                    time.sleep(1.5)
                    try:
                        return_code = process.poll()
                    except Exception:
                        LOG.exception('Failed to poll launch process status for argv=%r', argv)
                        return
                    if return_code is not None:
                        if entry_id is not None:
                            try:
                                running = getattr(self, '_running_launch_processes', {})
                                current = running.get(entry_id)
                                if current and current.get('process') is process:
                                    running.pop(entry_id, None)
                            except Exception:
                                LOG.exception('Failed to clear finished launch process for entry_id=%s', entry_id)
                        LOG.warning(
                            'Launch process pid=%s exited quickly with returncode=%s for argv=%r',
                            getattr(process, 'pid', None),
                            return_code,
                            argv,
                        )

                threading.Thread(target=_monitor_process, daemon=True).start()
                return True
            except OSError:
                LOG.error('Failed to launch command: %r', argv, exc_info=True)
                return False

    def _launch_entry_from_icon(self, entry):
            if entry is None:
                return
            try:
                self._suppress_next_overview_activate_entry_id = getattr(entry, 'id', None)
                self._suppress_next_overview_activate_until_us = int(GLib.get_monotonic_time()) + 600000
            except (AttributeError, TypeError, ValueError):
                self._suppress_next_overview_activate_entry_id = getattr(entry, 'id', None)
                self._suppress_next_overview_activate_until_us = 0
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
            options = self._get_options_dict(entry.id, force_refresh=True)
            if str(options.get(OPTION_PREVENT_MULTIPLE_STARTS_KEY, '0')) == '1':
                running_state = self._running_launch_process_for_entry(getattr(entry, 'id', None))
                block_reason = None
                if running_state is not None:
                    block_reason = f"tracked pid={getattr(running_state.get('process'), 'pid', None)}"
                launch_spec = build_launch_command(entry, options, ENGINES, LOG, prepare_profile=False)
                if launch_spec is None:
                    LOG.warning('Refusing to launch entry %s because no validated launch command could be built', getattr(entry, 'id', 'unknown'))
                    if hasattr(self, 'show_overlay_notification'):
                        self.show_overlay_notification(t('launch_failed'), timeout_ms=3200)
                    return
                if block_reason is None:
                    profile_info = launch_spec.get('profile_info') or {}
                    system_running = self._system_process_running_for_profile(profile_info.get('profile_path', ''))
                    if system_running is not None:
                        block_reason = f"system pid={system_running.get('pid')}"
                if block_reason is not None:
                    if hasattr(self, 'show_overlay_notification'):
                        self.show_overlay_notification(t('launch_already_running'), timeout_ms=2200)
                    return
            else:
                launch_spec = build_launch_command(entry, options, ENGINES, LOG, prepare_profile=False)
            if launch_spec is None:
                LOG.warning('Refusing to launch entry %s because no validated launch command could be built', getattr(entry, 'id', 'unknown'))
                if hasattr(self, 'show_overlay_notification'):
                    self.show_overlay_notification(t('launch_failed'), timeout_ms=3200)
                return
            if not self._launch_command_args(launch_spec['argv'], entry=entry):
                if hasattr(self, 'show_overlay_notification'):
                    self.show_overlay_notification(t('launch_failed'), timeout_ms=3200)
                return

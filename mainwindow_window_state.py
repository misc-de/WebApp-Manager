import json
from gi.repository import GLib, Adw
from app_identity import APP_DIR
from i18n import get_app_config, get_configured_language_value, save_app_config
from logger_setup import get_logger

LOG = get_logger(__name__)


class MainWindowWindowStateMixin:
    def _load_window_state(self):
            try:
                data = get_app_config(force_reload=True)
                state = data.get('window_state', {}) if isinstance(data, dict) else {}
                if isinstance(state, dict):
                    return state
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                pass
            return {}

    def _apply_window_state(self):
            try:
                if bool(self._window_state.get('maximized')):
                    self.maximize()
            except AttributeError:
                pass

    def _load_ui_settings(self):
            try:
                data = get_app_config(force_reload=True)
                settings = data.get('settings', {}) if isinstance(data, dict) else {}
                if isinstance(settings, dict):
                    return {'appearance': str(settings.get('appearance', 'auto') or 'auto')}
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                LOG.debug('Failed to load UI settings', exc_info=True)
            return {'appearance': 'auto'}

    def _load_language_setting(self):
            try:
                return get_configured_language_value()
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                LOG.debug('Failed to load language setting', exc_info=True)
            return 'system'

    def _save_ui_settings(self):
            try:
                config = dict(get_app_config(force_reload=True) or {})
                settings = dict(config.get('settings', {}) or {})
                settings['appearance'] = self._appearance_value()
                config['settings'] = settings
                save_app_config(config)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                LOG.error('Failed to save UI settings', exc_info=True)

    def _appearance_value(self):
            value = str((self.ui_settings or {}).get('appearance', 'auto')).strip().lower()
            if value not in {'auto', 'dark', 'light'}:
                return 'auto'
            return value

    def _apply_ui_appearance_setting(self):
            try:
                style_manager = Adw.StyleManager.get_default()
                appearance = self._appearance_value()
                mapping = {
                    'auto': Adw.ColorScheme.DEFAULT,
                    'dark': Adw.ColorScheme.FORCE_DARK,
                    'light': Adw.ColorScheme.FORCE_LIGHT,
                }
                style_manager.set_color_scheme(mapping.get(appearance, Adw.ColorScheme.DEFAULT))
            except AttributeError:
                LOG.error('Failed to apply UI appearance', exc_info=True)

    def _schedule_window_state_save(self):
            if self._window_state_save_source_id:
                return

            def do_save():
                self._window_state_save_source_id = 0
                self._save_window_state()
                return False

            self._window_state_save_source_id = GLib.timeout_add(300, do_save)

    def _collect_window_state(self):
            width = int(self.get_default_size()[0] or 500)
            height = int(self.get_default_size()[1] or 600)
            return {
                'width': max(320, width),
                'height': max(320, height),
                'maximized': bool(self.is_maximized()),
            }

    def _save_window_state(self):
            try:
                state = self._collect_window_state()
                config = dict(get_app_config(force_reload=True) or {})
                config['window_state'] = state
                save_app_config(config)
            except (OSError, TypeError, ValueError) as error:
                LOG.debug('Failed to save window state: %s', error)

    def _on_window_size_notify(self, *_args):
            self._schedule_window_state_save()

    def _on_close_request(self, *_args):
            self._save_window_state()
            return False


from gi.repository import GLib
from i18n import t


class MainWindowNotificationsMixin:
    def _show_busy(self, message=None):
            self.busy_label.set_text(message or t('loading'))
            self.busy_overlay.set_visible(True)
            self.busy_spinner.start()

    def _hide_busy(self):
            self.busy_spinner.stop()
            self.busy_overlay.set_visible(False)

    def _cancel_global_toast_timeout(self):
            if getattr(self, 'global_toast_timeout_id', 0):
                GLib.source_remove(self.global_toast_timeout_id)
                self.global_toast_timeout_id = 0

    def _hide_global_toast(self):
            self._cancel_global_toast_timeout()
            if hasattr(self, 'global_toast_revealer'):
                self.global_toast_revealer.set_reveal_child(False)
            return False

    def show_overlay_notification(self, message, timeout_ms=3000):
            text = (message or '').strip()
            if not text:
                self._hide_global_toast()
                return
            self._cancel_global_toast_timeout()
            self.global_toast_label.set_text(text)
            self.global_toast_revealer.set_reveal_child(True)
            self.global_toast_timeout_id = GLib.timeout_add(timeout_ms, self._hide_global_toast)


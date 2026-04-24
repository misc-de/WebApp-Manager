from gi.repository import Adw
from desktop_entries import export_desktop_file
from engine_support import available_engines
from i18n import t
from logger_setup import get_logger

LOG = get_logger(__name__)
ENGINES = available_engines()


class MainWindowDialogsMixin:
    def _present_yes_no_dialog(self, text, callback):
        self._present_choice_dialog(
            text,
            lambda accepted: (callback(accepted), self._show_next_conflict()),
            destructive=False,
        )

    def _present_info_dialog(self, message):
        if hasattr(Adw, 'AlertDialog'):
            dialog = Adw.AlertDialog()
            dialog.set_heading(t('app_title'))
            dialog.set_body(message)
            dialog.add_response('close', t('dialog_close'))
            dialog.set_default_response('close')
            dialog.set_close_response('close')
            dialog.present(self)
            return

        dialog = Adw.MessageDialog.new(self, t('app_title'), message)
        dialog.add_response('close', t('dialog_close'))
        dialog.set_default_response('close')
        dialog.set_close_response('close')
        dialog.present()

    def _handle_orphan_file(self, conflict, use_file):
        if use_file:
            self._upsert_entry_from_file(conflict['file'])

    def _handle_missing_file(self, conflict, recreate):
        if recreate:
            export_desktop_file(conflict['entry'], self._get_options_dict(conflict['entry'].id), ENGINES, LOG)

    def _handle_mismatch(self, conflict, use_file):
        if use_file:
            self._upsert_entry_from_file(conflict['file'], existing_entry=conflict['entry'])
            return
        export_desktop_file(conflict['entry'], self._get_options_dict(conflict['entry'].id), ENGINES, LOG)

    def _present_choice_dialog(self, message, on_result, destructive=False):
        handled = {'done': False}

        def respond(value):
            if handled['done']:
                return
            handled['done'] = True
            on_result(value)

        if hasattr(Adw, 'AlertDialog'):
            dialog = Adw.AlertDialog()
            dialog.set_heading(t('app_title'))
            dialog.set_body(message)
            dialog.add_response('no', t('dialog_no'))
            dialog.add_response('yes', t('dialog_yes'))
            dialog.set_default_response('yes')
            dialog.set_close_response('no')
            if destructive:
                dialog.set_response_appearance('yes', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.connect('response', lambda _d, response: respond(response == 'yes'))
            dialog.present(self)
            return

        dialog = Adw.MessageDialog.new(self, t('app_title'), message)
        dialog.add_response('no', t('dialog_no'))
        dialog.add_response('yes', t('dialog_yes'))
        dialog.set_default_response('yes')
        dialog.set_close_response('no')
        if destructive:
            dialog.set_response_appearance('yes', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect('response', lambda _d, response: respond(response == 'yes'))
        dialog.present()

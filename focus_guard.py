from __future__ import annotations

import os

from gi.repository import GLib


def should_prevent_input_autofocus() -> bool:
    for env_name in ('XDG_CURRENT_DESKTOP', 'XDG_SESSION_DESKTOP', 'DESKTOP_SESSION'):
        value = str(os.environ.get(env_name, '') or '').strip().lower()
        if 'phosh' in value:
            return True
    return False


def focus_neutral_widget(owner, target):
    if target is None:
        return False
    root = None
    try:
        root = owner.get_root()
    except Exception:
        root = None
    if root is not None and hasattr(root, 'set_focus'):
        try:
            root.set_focus(target)
        except Exception:
            pass
    try:
        target.grab_focus()
    except Exception:
        pass
    return False


def schedule_neutral_focus(owner, target_getter):
    if not should_prevent_input_autofocus():
        return 0

    def apply_focus():
        try:
            target = target_getter() if callable(target_getter) else target_getter
        except Exception:
            target = None
        return focus_neutral_widget(owner, target)

    return GLib.idle_add(apply_focus)

from __future__ import annotations

import shlex
from pathlib import Path

from app_identity import APP_ID, APP_ICON_NAME, APP_ICON_SOURCE, LEGACY_APP_IDS
from i18n import t


def headerbar_decoration_layout_without_icon() -> str:
    try:
        import gi
        gi.require_version('Gtk', '4.0')
        from gi.repository import Gtk
        settings = Gtk.Settings.get_default()
        layout = settings.get_property('gtk-decoration-layout') if settings is not None else None
    except Exception:
        layout = None
    layout = (layout or ':minimize,maximize,close').strip() or ':minimize,maximize,close'
    parts: list[str] = []
    for segment in layout.split(':'):
        buttons = [button.strip() for button in segment.split(',') if button.strip() and button.strip() != 'icon']
        parts.append(','.join(buttons))
    sanitized = ':'.join(parts)
    return sanitized if sanitized != ':' else ':minimize,maximize,close'


def ensure_manager_desktop_integration(app_dir: Path, logger) -> None:
    try:
        local_applications = Path.home() / '.local/share/applications'
        local_icons = Path.home() / '.local/share/icons/hicolor/512x512/apps'
        local_applications.mkdir(parents=True, exist_ok=True)
        local_icons.mkdir(parents=True, exist_ok=True)

        installed_icon_path = local_icons / f'{APP_ICON_NAME}.png'
        if APP_ICON_SOURCE.exists():
            try:
                source_bytes = APP_ICON_SOURCE.read_bytes()
                if (not installed_icon_path.exists()) or installed_icon_path.read_bytes() != source_bytes:
                    installed_icon_path.write_bytes(source_bytes)
            except Exception as error:
                logger.warning('Failed to install manager icon: %s', error)

        for legacy_id in LEGACY_APP_IDS:
            for legacy_path in (
                local_applications / f'{legacy_id}.desktop',
                local_icons / f'{legacy_id}.png',
            ):
                if legacy_path.exists():
                    try:
                        legacy_path.unlink()
                    except Exception:
                        pass

        desktop_entry_path = local_applications / f'{APP_ID}.desktop'
        exec_target = (app_dir / 'webapp-manager.py').resolve()
        exec_command = f"python3 {shlex.quote(str(exec_target))}"
        desktop_entry = '\n'.join([
            '[Desktop Entry]',
            'Type=Application',
            f'Name={t("app_title")}',
            f'Exec={exec_command}',
            f'Path={app_dir.resolve()}',
            f'Icon={installed_icon_path}',
            'Terminal=false',
            'Categories=Utility;',
            f'StartupWMClass={APP_ID}',
            f'X-GNOME-WMClass={APP_ID}',
            'StartupNotify=true',
            ''
        ])
        current_desktop_entry = ''
        if desktop_entry_path.exists():
            try:
                current_desktop_entry = desktop_entry_path.read_text(encoding='utf-8')
            except Exception:
                current_desktop_entry = ''
        if current_desktop_entry != desktop_entry:
            desktop_entry_path.write_text(desktop_entry, encoding='utf-8')
    except Exception as error:
        logger.warning('Failed to prepare desktop integration: %s', error)

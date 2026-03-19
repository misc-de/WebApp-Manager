from __future__ import annotations

from pathlib import Path
from typing import Callable

from desktop_entries import build_launch_command, get_expected_desktop_path, list_managed_desktop_files
from input_validation import sanitize_desktop_value


class LaunchService:
    def __init__(self, engines, logger, options_provider: Callable[[int], dict], subprocess_runner):
        self._engines = engines
        self._logger = logger
        self._options_provider = options_provider
        self._subprocess_runner = subprocess_runner

    def resolve_desktop_path_for_entry(self, entry):
        entry_id = getattr(entry, 'id', None)
        title = sanitize_desktop_value(getattr(entry, 'title', ''), getattr(entry, 'title', '')).strip()
        managed_files = list_managed_desktop_files(self._engines)
        for desktop_data in managed_files:
            if entry_id is not None and desktop_data.get('entry_id') == entry_id:
                path = desktop_data.get('path')
                if path is not None and path.exists():
                    return path
        if title:
            for desktop_data in managed_files:
                if (desktop_data.get('title') or '').strip() == title:
                    path = desktop_data.get('path')
                    if path is not None and path.exists():
                        return path
        desktop_path = get_expected_desktop_path(getattr(entry, 'title', ''))
        if desktop_path is not None and desktop_path.exists():
            return desktop_path
        return None

    def launch_entry(self, entry) -> bool:
        desktop_path = self.resolve_desktop_path_for_entry(entry)
        if desktop_path is None or not desktop_path.exists():
            self._logger.warning('Refusing to launch entry %s because its managed desktop file is missing', getattr(entry, 'id', 'unknown'))
            return False
        options = self._options_provider(entry.id, force_refresh=True)
        launch_spec = build_launch_command(entry, options, self._engines, self._logger, prepare_profile=True)
        if launch_spec is None:
            self._logger.warning('Refusing to launch entry %s because no validated launch command could be built', getattr(entry, 'id', 'unknown'))
            return False
        return self._subprocess_runner.popen(launch_spec['argv'], cwd=str(Path.home()))

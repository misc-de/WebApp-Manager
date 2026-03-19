from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from logger_setup import get_logger

LOG = get_logger(__name__)


@dataclass(frozen=True)
class LaunchRequest:
    argv: tuple[str, ...]
    cwd: str


class SafeSubprocessRunner:
    def __init__(self, default_cwd: str | None = None):
        self._default_cwd = str(Path(default_cwd).expanduser()) if default_cwd else str(Path.home())

    def _normalize_argv(self, argv: Sequence[str]) -> tuple[str, ...]:
        normalized: list[str] = []
        for item in argv:
            value = str(item or '').strip()
            if not value:
                continue
            if '\x00' in value:
                raise ValueError('command contains NUL byte')
            normalized.append(value)
        if not normalized:
            raise ValueError('empty command')
        return tuple(normalized)

    def build_launch_request(self, argv: Sequence[str], cwd: str | None = None) -> LaunchRequest:
        normalized_argv = self._normalize_argv(argv)
        working_dir = str(Path(cwd or self._default_cwd).expanduser())
        return LaunchRequest(argv=normalized_argv, cwd=working_dir)

    def popen(self, argv: Sequence[str], cwd: str | None = None) -> bool:
        request = self.build_launch_request(argv, cwd=cwd)
        try:
            subprocess.Popen(
                list(request.argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                cwd=request.cwd,
                env=self._sanitized_env(),
            )
            return True
        except (OSError, ValueError):
            LOG.error('Failed to launch command: %r', list(argv), exc_info=True)
            return False

    def _sanitized_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.pop('PYTHONSTARTUP', None)
        env.pop('PYTHONINSPECT', None)
        return env

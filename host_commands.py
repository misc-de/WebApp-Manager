"""Running host commands from inside a sandbox.

Everything this app does with a browser happens on the host: it looks for
installed browsers, resolves which binary to use, and launches them. Inside a
Flatpak sandbox none of that works directly -- the browser is not in the
sandbox image, so ``shutil.which('firefox')`` finds nothing and
``subprocess.Popen(['firefox', ...])`` fails.

This module is the single place that knows about the difference. Outside a
sandbox every function is a thin pass-through, so the native run is unchanged.
Inside one, lookups and launches are routed through ``flatpak-spawn --host``.

Note what is deliberately *not* routed through here: the ``Exec=`` line of a
generated .desktop file and the launcher wrapper scripts. Those are started by
the host's desktop shell, never from inside the sandbox, and must stay plain
host commands.
"""
from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

FLATPAK_INFO_PATH = Path('/.flatpak-info')
HOST_SPAWN_COMMAND = 'flatpak-spawn'
HOST_LOOKUP_TIMEOUT_SECONDS = 5
# flatpak-spawn hands the current working directory to the host process. The
# app's own directory is /app/share/webapp-manager, which does not exist
# outside the sandbox, so a lookup inherited from there fails and every browser
# looks uninstalled. "/" exists on both sides.
HOST_SPAWN_CWD = '/'


@lru_cache(maxsize=1)
def running_in_flatpak() -> bool:
    """True when this process runs inside a Flatpak sandbox.

    /.flatpak-info is created by the runtime in every sandbox and is the
    documented way to detect one.
    """
    try:
        return FLATPAK_INFO_PATH.exists()
    except OSError:
        return False


def host_argv(argv, env_overrides=None) -> list[str]:
    """Wrap `argv` so it executes on the host when sandboxed.

    `env_overrides` are passed as --env=, because flatpak-spawn does not carry
    the sandbox environment over to the host process.
    """
    argv = [str(part) for part in (argv or [])]
    if not argv or not running_in_flatpak():
        return argv
    prefix = [HOST_SPAWN_COMMAND, '--host']
    for key, value in sorted((env_overrides or {}).items()):
        prefix.append(f'--env={key}={value}')
    return [*prefix, *argv]


@lru_cache(maxsize=64)
def host_which(command: str) -> str | None:
    """shutil.which() that also sees host binaries when sandboxed.

    Cached because engine detection asks about the same handful of browser
    names repeatedly, and each sandboxed lookup costs a process spawn.
    """
    command = (command or '').strip()
    if not command:
        return None
    if not running_in_flatpak():
        return shutil.which(command)
    try:
        result = subprocess.run(
            # The command name is passed as an argument, never interpolated
            # into the script text -- same rule as everywhere else in this
            # codebase: no shell string built from a value.
            [HOST_SPAWN_COMMAND, '--host', 'sh', '-c', 'command -v -- "$1"', 'sh', command],
            capture_output=True,
            text=True,
            timeout=HOST_LOOKUP_TIMEOUT_SECONDS,
            check=False,
            cwd=HOST_SPAWN_CWD,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in (result.stdout or '').splitlines():
        found = line.strip()
        if found:
            return found
    return None

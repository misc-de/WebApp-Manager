from __future__ import annotations

from pathlib import Path


class PathSecurityError(ValueError):
    pass


def resolve_existing_file(path_value: str | Path | None) -> Path | None:
    if not path_value:
        return None
    try:
        candidate = Path(str(path_value)).expanduser().resolve()
    except OSError:
        return None
    if not candidate.exists() or not candidate.is_file():
        return None
    return candidate


def ensure_path_in_base(base_dir: str | Path, target: str | Path) -> Path:
    base = Path(base_dir).expanduser().resolve()
    resolved = Path(target).expanduser().resolve()
    try:
        resolved.relative_to(base)
    except ValueError as error:
        raise PathSecurityError(f'path escapes base directory: {resolved}') from error
    return resolved

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_OS_RELEASE_PATHS = (
    Path('/etc/os-release'),
    Path('/usr/lib/os-release'),
)


def _parse_os_release_text(text: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        data[key.strip()] = value
    return data


@lru_cache(maxsize=1)
def os_release_data() -> dict[str, str]:
    for path in _OS_RELEASE_PATHS:
        try:
            if path.exists():
                return _parse_os_release_text(path.read_text(encoding='utf-8', errors='ignore'))
        except OSError:
            continue
    return {}


@lru_cache(maxsize=1)
def _os_release_text() -> str:
    for path in _OS_RELEASE_PATHS:
        try:
            if path.exists():
                return path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
    return ''


@lru_cache(maxsize=1)
def is_furios_distribution() -> bool:
    raw_text = _os_release_text().lower()
    if raw_text and (('furios' in raw_text) or ('furi labs' in raw_text) or ('furilabs' in raw_text)):
        return True

    data = os_release_data()
    haystacks = [
        data.get('ID', ''),
        data.get('ID_LIKE', ''),
        data.get('NAME', ''),
        data.get('PRETTY_NAME', ''),
        data.get('VARIANT', ''),
        data.get('VARIANT_ID', ''),
        data.get('VENDOR_NAME', ''),
    ]
    text = ' '.join(haystacks).strip().lower()
    if not text:
        return False
    return ('furios' in text) or ('furi labs' in text) or ('furilabs' in text)

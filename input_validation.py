from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse
import urllib.error
import urllib.request
import re
import ipaddress

from webapp_constants import NON_PORTABLE_WAPP_OPTION_KEYS

MAX_WAPP_FILE_SIZE = 20 * 1024 * 1024
MAX_WAPP_TEXT_LENGTH = 20_000
MAX_WAPP_OPTION_VALUE_LENGTH = 4_000
MAX_ICON_BASE64_SIZE = 1_500_000
MAX_ICON_FILE_SIZE = 10 * 1024 * 1024
MAX_URL_LENGTH = 2048
DESKTOP_CHROME_USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
ORIGIN_CHECK_TIMEOUT_SECONDS = 2


def _coerce_candidate_urls(value: str, prefer_https: bool = True, include_http_fallback: bool = True) -> list[str]:
    raw = str(value or '').strip()
    if not raw or len(raw) > MAX_URL_LENGTH or contains_unsafe_text(raw) or any(char.isspace() for char in raw):
        return []
    parsed = urlparse(raw)
    if parsed.scheme:
        if parsed.scheme not in {'http', 'https'}:
            return []
        return [parsed.geturl()] if parsed.netloc else []
    if raw.startswith('//'):
        raw = raw.lstrip('/')
    if not raw:
        return []
    candidates = []
    primary_scheme = 'https' if prefer_https else 'http'
    candidates.append(f'{primary_scheme}://{raw}')
    if include_http_fallback:
        secondary_scheme = 'http' if primary_scheme == 'https' else 'https'
        fallback = f'{secondary_scheme}://{raw}'
        if fallback not in candidates:
            candidates.append(fallback)
    return candidates


def candidate_urls_for_input(value: str, prefer_https: bool = True, include_http_fallback: bool = True) -> list[str]:
    return _coerce_candidate_urls(value, prefer_https=prefer_https, include_http_fallback=include_http_fallback)


def _parsed_candidate_for_validation(value: str):
    candidates = _coerce_candidate_urls(value, prefer_https=True, include_http_fallback=False)
    if not candidates:
        return None
    try:
        return urlparse(candidates[0])
    except (TypeError, ValueError):
        return None


def contains_unsafe_text(value) -> bool:
    if value is None:
        return False
    for char in str(value):
        if ord(char) < 32 and char not in {'\t'}:
            return True
    return False


def sanitize_desktop_value(value, fallback: str = '') -> str:
    value = str(value if value is not None else fallback)
    value = value.replace('\x00', '').replace('\r', ' ').replace('\n', ' ').strip()
    return value or fallback


def validate_icon_source_path(path_value):
    if not path_value:
        return None
    try:
        candidate = Path(str(path_value)).expanduser().resolve()
    except OSError:
        return None
    if not candidate.exists() or not candidate.is_file():
        return None
    try:
        if candidate.stat().st_size > MAX_ICON_FILE_SIZE:
            return None
    except OSError:
        return None
    return candidate


def _read_import_payload_from_path(path):
    target = Path(path)
    if not target.exists() or not target.is_file():
        raise ValueError('Import file is missing')
    if target.stat().st_size > MAX_WAPP_FILE_SIZE:
        raise ValueError('Import file is too large')
    return json.loads(target.read_text(encoding='utf-8'))


def load_and_normalize_wapp_payload_from_path(path):
    return normalize_wapp_payload(_read_import_payload_from_path(path))


def load_import_payloads_from_path(path):
    payload = _read_import_payload_from_path(path)
    if isinstance(payload, dict) and str(payload.get('format', '')).strip().lower() == 'webapp-export-bundle-v1':
        raw_entries = payload.get('entries', [])
        if not isinstance(raw_entries, list):
            raise ValueError('Bundle entries must be an array')
        if len(raw_entries) > 500:
            raise ValueError('Bundle contains too many entries')
        return [normalize_wapp_payload(item) for item in raw_entries]
    return [normalize_wapp_payload(payload)]


def normalize_wapp_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError('Payload must be an object')

    title = payload.get('title', '')
    description = payload.get('description', '')
    if not isinstance(title, str):
        title = str(title)
    if not isinstance(description, str):
        description = str(description)
    title = sanitize_desktop_value(title)[:200]
    description = description.replace('\x00', '').strip()[:MAX_WAPP_TEXT_LENGTH]

    raw_options = payload.get('options', {})
    if raw_options is None:
        raw_options = {}
    if not isinstance(raw_options, dict):
        raise ValueError('Options must be an object')
    options = {}
    for key, value in raw_options.items():
        if not isinstance(key, str):
            continue
        key = sanitize_desktop_value(key)[:100]
        if not key or key in NON_PORTABLE_WAPP_OPTION_KEYS:
            continue
        if isinstance(value, bool):
            normalized = '1' if value else '0'
        elif isinstance(value, (int, float)):
            normalized = str(value)
        elif value is None:
            normalized = ''
        else:
            normalized = str(value)
        normalized = normalized.replace('\x00', '').strip()[:MAX_WAPP_OPTION_VALUE_LENGTH]
        options[key] = normalized

    normalized_payload = {
        'format': sanitize_desktop_value(payload.get('format', 'webapp-export-v1'), 'webapp-export-v1')[:64],
        'title': title,
        'description': description,
        'active': bool(payload.get('active', True)),
        'options': options,
        'icon': None,
    }

    icon = payload.get('icon')
    if icon is not None:
        if not isinstance(icon, dict):
            raise ValueError('Icon payload must be an object')
        raw_b64 = icon.get('data_base64') or ''
        if not isinstance(raw_b64, str):
            raise ValueError('Icon data must be base64 text')
        if len(raw_b64) > MAX_ICON_BASE64_SIZE * 2:
            raise ValueError('Icon payload is too large')
        normalized_payload['icon'] = {
            'filename': sanitize_desktop_value(icon.get('filename', 'icon.png'), 'icon.png')[:128],
            'mime': sanitize_desktop_value(icon.get('mime', 'image/png'), 'image/png')[:64],
            'data_base64': raw_b64,
        }
    return normalized_payload


def build_safe_slug(value) -> str:
    value = (value or '').strip().lower().replace(' ', '_')
    value = re.sub(r'[^a-z0-9._-]+', '_', value)
    value = re.sub(r'_+', '_', value)
    return value.strip('._-')


def _origin_request_headers() -> dict[str, str]:
    return {
        "User-Agent": DESKTOP_CHROME_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Connection": "close",
    }


def check_origin_status(url):
    candidates = _coerce_candidate_urls(url, prefer_https=True, include_http_fallback=True)
    if not candidates:
        return 'invalid'
    last_status = 'invalid'
    for candidate in candidates:
        try:
            parsed = urlparse(candidate)
        except ValueError:
            continue
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
            continue
        origin = f"{parsed.scheme}://{parsed.netloc}/"
        request = urllib.request.Request(origin, headers=_origin_request_headers())
        try:
            with urllib.request.urlopen(request, timeout=ORIGIN_CHECK_TIMEOUT_SECONDS) as response:
                status = getattr(response, 'status', 200) or 200
                return 'ok' if 200 <= status < 400 else 'unverified'
        except urllib.error.HTTPError as error:
            code = int(getattr(error, 'code', 0) or 0)
            if 200 <= code < 400:
                return 'ok'
            if code in {401, 403, 405, 406, 407, 409, 410, 412, 415, 418, 421, 425, 429, 451}:
                last_status = 'blocked'
            elif 400 <= code < 600:
                last_status = 'unverified'
            else:
                last_status = 'unverified'
        except (OSError, ValueError, urllib.error.URLError):
            if last_status == 'invalid':
                last_status = 'unverified'
    return last_status


def origin_returns_200(url):
    return check_origin_status(url) in {'ok', 'blocked', 'unverified'}


def is_structurally_valid_url(value) -> bool:
    if value is None:
        return False
    value = str(value).strip()
    if not value or len(value) > MAX_URL_LENGTH or contains_unsafe_text(value):
        return False
    if any(char.isspace() for char in value):
        return False
    parsed = _parsed_candidate_for_validation(value)
    if parsed is None:
        return False
    host = (parsed.hostname or '').strip('.').lower()
    if parsed.scheme not in {'http', 'https'} or not host:
        return False
    if parsed.username or parsed.password:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if '.' not in host:
        return False
    suffix = host.rsplit('.', 1)[-1]
    return len(suffix) >= 2


def is_valid_url(value, check_origin: bool = True) -> bool:
    if not is_structurally_valid_url(value):
        return False
    if not check_origin:
        return True
    return check_origin_status(value) in {'ok', 'blocked', 'unverified'}


def normalize_address(value, force_https: bool = False) -> str:
    value = (value or '').strip()
    if not value or len(value) > MAX_URL_LENGTH or contains_unsafe_text(value):
        return ''
    parsed = urlparse(value)
    if not parsed.scheme:
        return value
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username or parsed.password:
        return value
    if force_https and parsed.scheme == 'http':
        parsed = parsed._replace(scheme='https')
    return parsed.geturl()

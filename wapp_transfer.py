from __future__ import annotations

import base64
from datetime import datetime, timezone

from input_validation import validate_icon_source_path
from webapp_constants import ICON_PATH_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY


TRANSIENT_EXPORT_OPTION_KEYS = (ICON_PATH_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY)


def sanitized_export_options(options_dict: dict | None) -> dict[str, str]:
    options = dict(options_dict or {})
    for transient_key in TRANSIENT_EXPORT_OPTION_KEYS:
        options.pop(transient_key, None)
    return options


def build_wapp_export_payload(*, title: str, description: str = '', active: bool = True, options_dict: dict | None = None) -> dict:
    raw_options = dict(options_dict or {})
    icon_path = str(raw_options.get(ICON_PATH_KEY, '') or '').strip()
    payload = {
        'format': 'webapp-export-v1',
        'title': title or '',
        'description': description or '',
        'active': bool(active),
        'options': sanitized_export_options(raw_options),
        'icon': None,
    }

    validated_icon = validate_icon_source_path(icon_path) if icon_path else None
    if validated_icon is not None:
        icon_bytes = validated_icon.read_bytes()
        payload['icon'] = {
            'filename': validated_icon.name,
            'mime': 'image/png',
            'data_base64': base64.b64encode(icon_bytes).decode('ascii'),
        }
    return payload


def build_wapp_export_bundle_payload(entry_payloads: list[dict], created_at: str | None = None) -> dict:
    if created_at is None:
        created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    return {
        'format': 'webapp-export-bundle-v1',
        'version': 1,
        'created_at': created_at,
        'entries': list(entry_payloads or []),
    }

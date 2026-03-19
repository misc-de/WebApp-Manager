from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from input_validation import sanitize_desktop_value, validate_icon_source_path
from webapp_constants import ICON_PATH_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY


class ExportService:
    def __init__(self, options_provider: Callable[[int], dict]):
        self._options_provider = options_provider

    def build_export_payload_for_entry(self, entry):
        options = dict(self._options_provider(entry.id))
        icon_path = str(options.get(ICON_PATH_KEY, '') or '').strip()
        for transient_key in (ICON_PATH_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY):
            options.pop(transient_key, None)
        payload = {
            'format': 'webapp-export-v1',
            'title': entry.title or '',
            'description': entry.description or '',
            'active': bool(entry.active),
            'options': options,
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

    def build_export_bundle_payload(self, entries):
        return {
            'format': 'webapp-export-bundle-v1',
            'version': 1,
            'created_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z'),
            'entries': [self.build_export_payload_for_entry(entry) for entry in entries],
        }

    def safe_export_name(self, entry):
        base = sanitize_desktop_value((entry.title or 'webapp').strip()) or f'webapp-{entry.id}'
        return f'{base}.wapp'

    def write_bundle(self, target: str | Path, entries) -> Path:
        output = Path(target)
        if output.suffix.lower() != '.wapp':
            output = output.with_suffix('.wapp')
        payload = self.build_export_bundle_payload(entries)
        output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')
        return output

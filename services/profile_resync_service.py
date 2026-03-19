from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app_identity import APP_DB_PATH
from browser_option_logic import browser_managed_option_keys, browser_state_key, encode_browser_state, mode_option_keys, normalize_option_dict, normalize_option_rows
from browser_profiles import read_profile_settings
from database import Database


@dataclass(frozen=True)
class ProfileCandidate:
    entry_id: int
    title: str
    family: str
    profile_path: str


class ProfileResyncService:
    def __init__(self, logger, options_provider: Callable[[int], dict]):
        self._logger = logger
        self._options_provider = options_provider

    def collect_candidates(self, entries_store, browser_family_for_options: Callable[[dict], str]):
        candidates: list[ProfileCandidate] = []
        for index in range(entries_store.get_n_items()):
            entry = entries_store.get_item(index)
            options = self._options_provider(entry.id)
            profile_path = str(options.get('ProfilePath') or '').strip()
            family = browser_family_for_options(options)
            if not profile_path or family not in {'firefox', 'chrome', 'chromium'}:
                continue
            candidates.append(ProfileCandidate(entry_id=entry.id, title=entry.title or '', family=family, profile_path=profile_path))
        return candidates

    def run(self, items, cancel_event=None, progress_callback: Callable[[int, int, str], None] | None = None):
        db = Database(str(APP_DB_PATH))
        processed = 0
        failures = 0
        cancelled = False
        total = len(items)
        try:
            for index, candidate in enumerate(items, start=1):
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                if progress_callback is not None:
                    progress_callback(index, total, candidate.title)
                try:
                    raw_state = read_profile_settings(candidate.profile_path, candidate.family)
                    normalized_state = normalize_option_dict(raw_state)
                    updates = {key: value for key, value in normalized_state.items() if key in browser_managed_option_keys() and key not in mode_option_keys()}
                    if updates:
                        existing = normalize_option_rows(db.get_options_for_entry(candidate.entry_id))
                        merged = dict(existing)
                        merged.update(updates)
                        updates[browser_state_key(candidate.family)] = encode_browser_state(merged, candidate.family)
                        db.add_options(candidate.entry_id, updates)
                except (OSError, TypeError, ValueError) as error:
                    failures += 1
                    self._logger.warning('Profile resync failed for entry %s (%s): %s', candidate.entry_id, candidate.profile_path, error)
                processed = index
                if progress_callback is not None:
                    progress_callback(processed, total, '')
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
        finally:
            db.close()
        return {
            'processed': processed,
            'total': total,
            'failures': failures,
            'cancelled': cancelled,
        }

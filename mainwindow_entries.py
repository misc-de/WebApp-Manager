import base64
import json
from pathlib import Path

from gi.repository import GLib, Gtk, Pango

from app_models import Entry
from app_state import WebAppState
from browser_profiles import inspect_profile_copy_source, read_profile_settings, rename_unused_managed_profile_directories
from browser_option_logic import browser_family_for_command, browser_managed_option_keys, browser_state_key, encode_browser_state, mode_option_keys, normalize_option_dict, normalize_option_rows
from desktop_entries import export_desktop_file, exportable_entry, get_expected_desktop_path, list_managed_desktop_files
from engine_support import available_engines
from i18n import t
from icon_pipeline import get_managed_icon_path, is_svg_support_missing_error, normalize_icon_to_png
from input_validation import build_safe_slug, sanitize_desktop_value, validate_icon_source_path
from logger_setup import get_logger
from webapp_constants import ADDRESS_KEY, APP_MODE_KEY, COLOR_SCHEME_KEY, DEFAULT_ZOOM_KEY, ICON_PATH_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY, USER_AGENT_NAME_KEY, USER_AGENT_VALUE_KEY

LOG = get_logger(__name__)
ENGINES = available_engines()

MANAGED_IMPORT_OPTION_KEYS = [
    'Kiosk',
    APP_MODE_KEY,
    'Frameless',
    'PreserveSession',
    'KeepInBackground',
    'Notifications',
    'SwipeNavigation',
    'AdBlock',
    'OnlyHTTPS',
    'ClearCacheOnExit',
    'ClearCookiesOnExit',
    'DisableAI',
    'ForcePrivacy',
    COLOR_SCHEME_KEY,
    DEFAULT_ZOOM_KEY,
]


def format_profile_size(profile_path: str) -> str:
    try:
        path = Path((profile_path or '').strip()).expanduser()
        if not path.exists():
            return '0 MB'
        total = 0
        if path.is_file():
            total = path.stat().st_size
        else:
            for child in path.rglob('*'):
                try:
                    if child.is_file():
                        total += child.stat().st_size
                except OSError:
                    continue
        if total <= 0:
            return '0 MB'
        gb = total / (1024 ** 3)
        if gb >= 1:
            return f'{gb:.2f} GB'
        mb = total / (1024 ** 2)
        return f'{mb:.0f} MB'
    except OSError:
        return '0 MB'


class MainWindowEntriesMixin:
    def load_entries_from_db(self):
        self.entries_store.remove_all()
        self._options_cache = {}
        self._profile_size_cache = {}
        self._profile_size_pending = set()
        entry_rows = self.db.list_entries()
        for row in entry_rows:
            self.entries_store.append(Entry(row[0], row[1], row[2], bool(row[3])))
        for entry_id, option_key, option_value in self.db.list_option_values():
            self._options_cache.setdefault(entry_id, {})[option_key] = option_value

    def _cleanup_detail_pages(self, pages):
        for child in pages:
            self._remove_overview_page_widget(child)
            try:
                child.set_visible(False)
            except (AttributeError, TypeError):
                pass
        return False

    def _reload_entries(self):
        pages = list(self.detail_pages.values())
        if pages:
            try:
                self._show_overview_root_page()
                self.stack.set_visible_child_name('overview_page')
            except (AttributeError, TypeError, GLib.Error):
                pass
            GLib.idle_add(self._cleanup_detail_pages, pages)
        self.detail_pages = {}
        self._creating_entry = False
        self.load_entries_from_db()
        self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)
        self.update_empty_state()

    def _find_entry_by_id(self, entry_id):
        for index in range(self.entries_store.get_n_items()):
            entry = self.entries_store.get_item(index)
            if entry.id == entry_id:
                return entry
        return None

    def _find_entry_by_title(self, title):
        matches = []
        for index in range(self.entries_store.get_n_items()):
            entry = self.entries_store.get_item(index)
            if entry.title == title:
                matches.append(entry)
        return matches

    def _normalized_compare_text(self, value):
        return sanitize_desktop_value(value).strip().casefold()

    def _find_import_collision(self, payload):
        options = payload.get('options', {}) if isinstance(payload, dict) else {}
        if not isinstance(options, dict):
            options = {}
        target_title = self._normalized_compare_text(payload.get('title', ''))
        target_address = self._normalized_compare_text(options.get(ADDRESS_KEY, ''))
        target_engine = self._normalized_compare_text(options.get('EngineID', ''))
        if not target_title and not target_address:
            return None

        best_match = None
        for index in range(self.entries_store.get_n_items()):
            entry = self.entries_store.get_item(index)
            existing_options = self._get_options_dict(entry.id)
            existing_title = self._normalized_compare_text(entry.title)
            existing_address = self._normalized_compare_text(existing_options.get(ADDRESS_KEY, ''))
            existing_engine = self._normalized_compare_text(existing_options.get('EngineID', ''))

            exact_title_and_address = bool(target_title and target_address and existing_title == target_title and existing_address == target_address)
            same_address_and_engine = bool(target_address and existing_address == target_address and target_engine and existing_engine == target_engine)
            same_title_and_engine = bool(target_title and existing_title == target_title and target_engine and existing_engine == target_engine)

            if exact_title_and_address:
                return entry
            if best_match is None and (same_address_and_engine or same_title_and_engine):
                best_match = entry
        return best_match

    def _show_import_collision(self, entry, payload):
        try:
            self.on_entry_activated(entry, show_busy=False)
        except (AttributeError, TypeError):
            pass

        detail_page = self.detail_pages.get(entry.id)
        title = sanitize_desktop_value(payload.get('title', ''), entry.title).strip() or entry.title
        message = t('import_duplicate_detected', title=title)
        if detail_page is not None:
            detail_page._set_detail_action_status(message)
        self._present_info_dialog(message)
        LOG.info('Blocked duplicate .wapp import for entry %s (%s)', entry.id, title)

    def _invalidate_entry_cache(self, entry_id, clear_profile_size=False):
        self._options_cache.pop(entry_id, None)
        if clear_profile_size:
            self._profile_size_cache.pop(entry_id, None)
            self._profile_size_pending.discard(entry_id)

    def _cache_options(self, entry_id, updates):
        cached = dict(self._get_options_dict(entry_id))
        cached.update({key: '' if value is None else str(value) for key, value in updates.items()})
        self._options_cache[entry_id] = cached
        if any(key in updates for key in (PROFILE_PATH_KEY, PROFILE_NAME_KEY, ICON_PATH_KEY, 'EngineName')):
            self._profile_size_cache.pop(entry_id, None)
            self._profile_size_pending.discard(entry_id)

    def _add_options(self, entry_id, updates):
        clean_updates = {key: '' if value is None else str(value) for key, value in updates.items()}
        if not clean_updates:
            return
        self.db.add_options(entry_id, clean_updates)
        self._cache_options(entry_id, clean_updates)

    def _iter_icon_candidates(self, candidate, base_dir=None):
        raw = str(candidate or '').strip()
        if not raw:
            return []
        candidate_path = Path(raw).expanduser()
        suffix = candidate_path.suffix.lower()
        basenames = [candidate_path.name] if suffix else [f'{candidate_path.name}.svg', f'{candidate_path.name}.png', f'{candidate_path.name}.ico', f'{candidate_path.name}.xpm']
        search_dirs = []
        if candidate_path.parent != Path('.'):
            search_dirs.append(candidate_path.parent)
        if base_dir:
            base_path = Path(base_dir).expanduser()
            if base_path.is_file():
                base_path = base_path.parent
            search_dirs.extend([
                base_path,
                base_path / 'icons',
                base_path / 'pixmaps',
            ])
        found = []
        seen = set()
        direct_candidates = [candidate_path]
        if not suffix:
            direct_candidates.extend(candidate_path.with_suffix(ext) for ext in ('.svg', '.png', '.ico', '.xpm'))
        for direct in direct_candidates:
            try:
                resolved = direct.resolve()
            except (OSError, RuntimeError):
                resolved = direct
            if resolved in seen:
                continue
            seen.add(resolved)
            if direct.exists() and direct.is_file():
                found.append(direct)
        for root in search_dirs:
            try:
                root = root.resolve()
            except (OSError, RuntimeError):
                root = Path(root)
            if not root.exists() or not root.is_dir():
                continue
            for basename in basenames:
                candidate = root / basename
                try:
                    resolved = candidate.resolve()
                except (OSError, RuntimeError):
                    resolved = candidate
                if resolved in seen:
                    continue
                seen.add(resolved)
                if candidate.exists() and candidate.is_file():
                    found.append(candidate)
        return found

    def _lookup_system_icon_file(self, icon_name, base_dir=None):
        candidate = str(icon_name or '').strip()
        if not candidate:
            return None
        local_found = self._iter_icon_candidates(candidate, base_dir=base_dir)
        if local_found:
            return local_found[0]
        name = Path(candidate).name
        stem = Path(name).stem if Path(name).suffix else name
        explicit_suffix = Path(name).suffix.lower()
        icon_dirs = [
            Path.home() / '.local/share/icons',
            Path.home() / '.icons',
            Path('/usr/local/share/icons'),
            Path('/usr/share/icons'),
            Path('/usr/share/pixmaps'),
        ]
        found = []
        for root in icon_dirs:
            if not root.exists():
                continue
            patterns = [name] if explicit_suffix else [f'{stem}.svg', f'{stem}.png', f'{stem}.ico', f'{stem}.xpm']
            for pattern in patterns:
                try:
                    found.extend(path for path in root.rglob(pattern) if path.is_file())
                except OSError:
                    continue
        if not found:
            return None

        def score(path):
            suffix = path.suffix.lower()
            suffix_score = {'.svg': 0, '.png': 1, '.ico': 2, '.xpm': 3}.get(suffix, 9)
            size_score = 9999
            for part in path.parts:
                if 'x' in part:
                    try:
                        size_score = -int(part.split('x', 1)[0])
                        break
                    except (AttributeError, TypeError):
                        pass
            return (suffix_score, size_score, len(path.parts), len(str(path)))

        return sorted(found, key=score)[0]

    def _resolve_import_icon_reference(self, file_data, title, entry_id):
        desktop_path = file_data.get('path')
        desktop_dir = Path(desktop_path).parent if desktop_path else None

        def _copy_icon_candidate(icon_candidate):
            managed_target = get_managed_icon_path(title, '.png', entry_id)
            try:
                normalize_icon_to_png(icon_candidate, managed_target)
                return str(managed_target)
            except (OSError, ValueError) as error:
                if is_svg_support_missing_error(error):
                    try:
                        self.show_overlay_notification(t('svg_import_requires_cairo'), timeout_ms=4200)
                    except AttributeError:
                        pass
                try:
                    suffix = icon_candidate.suffix or '.png'
                    fallback_target = get_managed_icon_path(title, suffix, entry_id)
                    fallback_target.write_bytes(icon_candidate.read_bytes())
                    return str(fallback_target)
                except OSError:
                    return ''

        icon_ref = str(file_data.get('icon_path') or '').strip()
        if icon_ref:
            icon_candidates = self._iter_icon_candidates(icon_ref, base_dir=desktop_dir)
            for icon_candidate in icon_candidates:
                copied = _copy_icon_candidate(icon_candidate)
                if copied:
                    return copied

        icon_name = str(file_data.get('icon_name') or '').strip()
        if icon_name:
            resolved = self._lookup_system_icon_file(icon_name, base_dir=desktop_dir)
            if resolved is not None:
                copied = _copy_icon_candidate(resolved)
                if copied:
                    return copied

        title_candidates = []
        safe_title = build_safe_slug(title)
        raw_title = str(title or '').strip()
        if raw_title:
            title_candidates.append(raw_title)
        if safe_title and safe_title not in title_candidates:
            title_candidates.append(safe_title)
        if desktop_path:
            desktop_stem = Path(desktop_path).stem
            if desktop_stem and desktop_stem not in title_candidates:
                title_candidates.append(desktop_stem)
        for candidate_name in title_candidates:
            resolved = self._lookup_system_icon_file(candidate_name, base_dir=desktop_dir)
            if resolved is None:
                continue
            copied = _copy_icon_candidate(resolved)
            if copied:
                return copied

        return ''

    def _get_profile_size_text_cached(self, entry_id, profile_path):
        cached = self._profile_size_cache.get(entry_id)
        if cached and cached.get('path') == profile_path:
            return cached.get('text', '0 MB' if profile_path else '')
        return '0 MB' if profile_path else ''

    def _schedule_profile_size_refresh(self, entry_id, profile_path, profile_size_label):
        if not profile_path:
            self._profile_size_cache[entry_id] = {'path': '', 'text': ''}
            profile_size_label.set_text('')
            profile_size_label.set_visible(False)
            return
        if entry_id in self._profile_size_pending:
            return
        self._profile_size_pending.add(entry_id)

        def _compute():
            try:
                size_text = format_profile_size(profile_path)
            except OSError:
                size_text = ''
            self._profile_size_cache[entry_id] = {'path': profile_path, 'text': size_text}
            self._profile_size_pending.discard(entry_id)
            current_entry = getattr(profile_size_label, '_entry_id', None)
            current_path = getattr(profile_size_label, '_profile_path', '')
            if current_entry == entry_id and current_path == profile_path:
                profile_size_label.set_text(size_text)
                profile_size_label.set_visible(bool(size_text))
            return False

        GLib.idle_add(_compute, priority=GLib.PRIORITY_LOW)

    def _get_options_dict(self, entry_id, force_refresh=False):
        if not force_refresh:
            cached = self._options_cache.get(entry_id)
            if cached is not None:
                return dict(cached)
        loaded = normalize_option_rows(self.db.get_options_for_entry(entry_id))
        self._options_cache[entry_id] = dict(loaded)
        return loaded

    def _entry_by_id(self, entry_id):
        for index in range(self.filtered_model.get_n_items()):
            candidate = self.filtered_model.get_item(index)
            if candidate is not None and int(getattr(candidate, 'id', -1)) == int(entry_id):
                return candidate
        for index in range(self.entries_store.get_n_items()):
            candidate = self.entries_store.get_item(index)
            if candidate is not None and int(getattr(candidate, 'id', -1)) == int(entry_id):
                return candidate
        return None

    def _profile_display_name(self, options):
        profile_path = (options.get(PROFILE_PATH_KEY) or '').strip()
        if profile_path:
            return Path(profile_path).name
        return (options.get(PROFILE_NAME_KEY) or '').strip()

    def _build_detail_header(self, entry):
        title = Gtk.Label(xalign=0.5)
        title.set_text(t('app_title'))
        title.add_css_class('title-4')
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(40)
        return title

    def _normalized_option_state(self, values, fallback=None):
        normalized = {}
        fallback = fallback or {}
        for key in MANAGED_IMPORT_OPTION_KEYS:
            value = values.get(key)
            if value in (None, ''):
                value = fallback.get(key)
            if key == COLOR_SCHEME_KEY:
                normalized[key] = (value or 'auto')
            elif key == DEFAULT_ZOOM_KEY:
                normalized[key] = str(value or '100')
            else:
                normalized[key] = '1' if str(value) == '1' else '0'
        return normalized

    def _engine_for_options(self, options):
        try:
            target_id = int((options or {}).get('EngineID') or 0)
        except (TypeError, ValueError):
            target_id = 0
        if target_id:
            for engine in ENGINES:
                try:
                    if int(engine.get('id', -1)) == target_id:
                        return engine
                except (TypeError, ValueError):
                    continue
        target_name = str((options or {}).get('EngineName') or '').strip().lower()
        if target_name:
            for engine in ENGINES:
                if str(engine.get('name') or '').strip().lower() == target_name:
                    return engine
        return None

    def _browser_family_for_options(self, options):
        engine = self._engine_for_options(options or {})
        if engine is None:
            return 'generic'
        return browser_family_for_command(engine.get('command') or '')

    def _profile_sync_updates_for_entry(self, entry_id, profile_path, family):
        profile_path = str(profile_path or '').strip()
        family = (family or 'generic').strip().lower()
        if not profile_path or family == 'generic':
            return {}
        try:
            raw_state = read_profile_settings(profile_path, family)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            LOG.warning('Failed to read profile settings for entry %s from %s: %s', entry_id, profile_path, error)
            return {}
        normalized_state = normalize_option_dict(raw_state)
        updates = {key: value for key, value in normalized_state.items() if key in browser_managed_option_keys() and key not in mode_option_keys()}
        if not updates:
            return {}
        existing = self._get_options_dict(entry_id)
        merged = dict(existing)
        merged.update(updates)
        updates[browser_state_key(family)] = encode_browser_state(merged, family)
        return updates

    def _reset_imported_option_state(self, entry_id):
        reset_values = {
            ADDRESS_KEY: '',
            ICON_PATH_KEY: '',
            'EngineID': '',
            'EngineName': '',
            USER_AGENT_NAME_KEY: '',
            USER_AGENT_VALUE_KEY: '',
            PROFILE_NAME_KEY: '',
            PROFILE_PATH_KEY: '',
            COLOR_SCHEME_KEY: 'auto',
            DEFAULT_ZOOM_KEY: '100',
        }
        for key in MANAGED_IMPORT_OPTION_KEYS:
            reset_values.setdefault(key, '0')
        self._add_options(entry_id, reset_values)

    def _collect_active_profile_paths(self):
        active_paths = []
        for index in range(self.entries_store.get_n_items()):
            entry = self.entries_store.get_item(index)
            options = self._get_options_dict(entry.id)
            profile_path = str(options.get(PROFILE_PATH_KEY) or '').strip()
            if profile_path:
                active_paths.append(profile_path)
        return active_paths

    def _run_startup_profile_cleanup(self):
        if self._startup_profile_cleanup_done:
            return
        self._startup_profile_cleanup_done = True
        rename_unused_managed_profile_directories(self._collect_active_profile_paths(), LOG)

    def _finalize_startup_reconcile(self):
        self._reload_entries()
        self._run_startup_profile_cleanup()

    def _upsert_entry_from_file(self, file_data, existing_entry=None):
        title = (file_data.get('title') or '').strip()
        active = 1 if file_data.get('active', True) else 0
        if existing_entry is None:
            entry_id = self.db.add_entry(title, '')
            if entry_id is None:
                return
            entry_obj = self._find_entry_by_id(entry_id)
            if entry_obj is None:
                entry_obj = Entry(entry_id, title, '', bool(active))
                self.entries_store.append(entry_obj)
        else:
            entry_id = existing_entry.id
            entry_obj = existing_entry

        option_updates = {}
        if file_data.get('address'):
            option_updates[ADDRESS_KEY] = file_data['address']
        icon_ref = self._resolve_import_icon_reference(file_data, title, entry_id)
        if icon_ref:
            option_updates[ICON_PATH_KEY] = icon_ref
        if file_data.get('engine_id') is not None:
            option_updates['EngineID'] = str(file_data['engine_id'])
        if file_data.get('engine_name'):
            option_updates['EngineName'] = file_data['engine_name']
        elif file_data.get('engine_id'):
            for engine in ENGINES:
                if engine['id'] == file_data['engine_id']:
                    option_updates['EngineName'] = engine['name']
                    break
        if file_data.get('user_agent_name') is not None:
            option_updates[USER_AGENT_NAME_KEY] = file_data.get('user_agent_name', '')
        if file_data.get('user_agent_value') is not None:
            option_updates[USER_AGENT_VALUE_KEY] = file_data.get('user_agent_value', '')
        profile_family = self._browser_family_for_options({
            'EngineID': option_updates.get('EngineID', ''),
            'EngineName': option_updates.get('EngineName', ''),
        })
        if file_data.get('profile_path') and profile_family in {'firefox', 'chrome', 'chromium'}:
            profile_source = inspect_profile_copy_source(file_data['profile_path'], profile_family, LOG)
            if profile_source.get('valid'):
                option_updates[PROFILE_PATH_KEY] = profile_source['profile_path']
                option_updates[PROFILE_NAME_KEY] = profile_source['profile_name']
        elif file_data.get('profile_name'):
            option_updates[PROFILE_NAME_KEY] = file_data['profile_name']
        for key in ('Kiosk', APP_MODE_KEY, 'Frameless'):
            value = (file_data.get('options') or {}).get(key)
            if value is not None:
                option_updates[key] = value
        self._add_options(entry_id, option_updates)
        self.db.update_entry(entry_id, title=title, active=bool(active))
        entry_obj.title = title
        entry_obj.active = bool(active)

        result = export_desktop_file(entry_obj, self._get_options_dict(entry_id), ENGINES, LOG)
        if result:
            self._add_options(entry_id, {
                PROFILE_NAME_KEY: result.get('profile_name', '') or '',
                PROFILE_PATH_KEY: result.get('profile_path', '') or '',
            })

        current_options = self._get_options_dict(entry_id)
        profile_path = (current_options.get(PROFILE_PATH_KEY) or '').strip()
        profile_family = self._browser_family_for_options(current_options)
        profile_updates = self._profile_sync_updates_for_entry(entry_id, profile_path, profile_family)
        if profile_updates:
            self._add_options(entry_id, profile_updates)

        self.refresh_entry_visual(entry_obj)
        if entry_id in self.detail_pages:
            try:
                self.detail_pages[entry_id].reload_from_db()
            except (AttributeError, GLib.Error):
                pass

    def _compare_db_and_file(self, entry, file_data):
        options = self._get_options_dict(entry.id)
        db_state = WebAppState.from_entry_and_options(entry, options)
        file_state = WebAppState.from_file_data(file_data, fallback=db_state)
        db_values = {
            'title': db_state.title,
            'address': db_state.address,
            'engine_id': db_state.engine_id,
            'active': db_state.active,
            'icon_path': bool(db_state.icon_path),
        }
        file_values = {
            'title': file_state.title,
            'address': file_state.address,
            'engine_id': file_state.engine_id,
            'active': file_state.active,
            'icon_path': bool(file_state.icon_path),
        }
        return db_values != file_values, db_values, file_values

    def reconcile_desktop_files(self):
        managed_files = list_managed_desktop_files(ENGINES)
        matched_ids = set()
        conflicts = []
        imports = []

        for file_data in managed_files:
            entry = None
            file_entry_id = file_data.get('entry_id')
            had_explicit_entry_id = file_entry_id not in (None, '')
            if had_explicit_entry_id:
                entry = self._find_entry_by_id(file_entry_id)
            if entry is None and (not had_explicit_entry_id) and file_data.get('title'):
                matches = self._find_entry_by_title(file_data['title'])
                if len(matches) == 1:
                    entry = matches[0]
            if entry is None:
                if had_explicit_entry_id:
                    imports.append(file_data)
                    continue
                conflicts.append({'type': 'orphan_file', 'file': file_data})
                continue
            matched_ids.add(entry.id)
            is_mismatch, db_values, file_values = self._compare_db_and_file(entry, file_data)
            if is_mismatch:
                conflicts.append({'type': 'mismatch', 'entry': entry, 'file': file_data, 'db': db_values, 'file_values': file_values})

        for index in range(self.entries_store.get_n_items()):
            entry = self.entries_store.get_item(index)
            if entry.id in matched_ids:
                continue
            options = self._get_options_dict(entry.id)
            if not exportable_entry(entry, options):
                continue
            expected_path = get_expected_desktop_path(entry.title)
            if expected_path is None or not expected_path.exists():
                conflicts.append({'type': 'missing_file', 'entry': entry})

        self.reconcile_queue = conflicts
        if imports:
            self._prompt_detected_desktop_imports(imports)
        else:
            self._show_next_conflict()
        return False

    def _finish_detected_desktop_imports(self, imported_count, total, cancelled=False):
        self._destroy_import_progress_dialog()
        self._import_cancel_requested = False
        self._reload_entries()
        if cancelled:
            self.show_overlay_notification(t('desktop_detected_import_cancelled', imported=imported_count, total=total), timeout_ms=3200)
        elif imported_count:
            self.show_overlay_notification(t('desktop_detected_import_done', imported=imported_count, total=total), timeout_ms=2800)
        self._show_next_conflict()
        return False

    def _prompt_detected_desktop_imports(self, file_datas):
        items = list(file_datas or [])
        if not items:
            self._show_next_conflict()
            return
        total = len(items)
        message = t('desktop_detected_import_prompt', total=total)

        def handle_import_choice(accepted):
            if accepted:
                self._start_detected_desktop_imports(items)
                return
            self._reload_entries()
            self._show_next_conflict()

        self._present_choice_dialog(message, handle_import_choice, destructive=False)

    def _start_detected_desktop_imports(self, file_datas):
        items = list(file_datas or [])
        if not items:
            self._show_next_conflict()
            return
        total = len(items)
        imported_count = 0
        state = {'index': 0}
        self._import_cancel_requested = False
        self._show_import_progress_dialog(total, title_text=t('desktop_detected_import_title'), preparing_text=t('desktop_detected_import_found', total=total))
        GLib.idle_add(self._update_import_progress, 0, total, '')

        def process_next():
            nonlocal imported_count
            if self._import_cancel_requested:
                GLib.idle_add(self._finish_detected_desktop_imports, imported_count, total, True)
                return False
            if state['index'] >= total:
                GLib.idle_add(self._finish_detected_desktop_imports, imported_count, total, False)
                return False
            file_data = items[state['index']]
            state['index'] += 1
            title = str(file_data.get('title') or file_data.get('path') or '').strip()
            GLib.idle_add(self._update_import_progress, state['index'], total, title)
            try:
                self._upsert_entry_from_file(file_data)
                imported_count += 1
            except (OSError, ValueError, json.JSONDecodeError) as error:
                LOG.warning('Failed to import managed desktop file %s: %s', file_data.get('path'), error)
            self._reload_entries()
            GLib.idle_add(self._update_import_progress, state['index'], total, '')
            GLib.idle_add(process_next)
            return False

        GLib.idle_add(process_next)

    def _show_next_conflict(self):
        if not self.reconcile_queue:
            self._finalize_startup_reconcile()
            return
        conflict = self.reconcile_queue.pop(0)
        if conflict['type'] == 'orphan_file':
            text = t('reconcile_orphan_file', path=str(conflict['file']['path']), title=conflict['file'].get('title', ''))
            self._present_yes_no_dialog(text, lambda use_file: self._handle_orphan_file(conflict, use_file))
            return
        if conflict['type'] == 'missing_file':
            text = t('reconcile_missing_file', title=conflict['entry'].title)
            self._present_yes_no_dialog(text, lambda recreate: self._handle_missing_file(conflict, recreate))
            return
        if conflict['type'] == 'mismatch':
            text = t('reconcile_mismatch', db_title=conflict['db']['title'], file_title=conflict['file_values']['title'], db_address=conflict['db']['address'], file_address=conflict['file_values']['address'])
            self._present_yes_no_dialog(text, lambda use_file: self._handle_mismatch(conflict, use_file))
            return

    def _handle_orphan_file(self, conflict, use_file):
        if use_file:
            self._upsert_entry_from_file(conflict['file'])

    def _handle_missing_file(self, conflict, recreate):
        if recreate:
            export_desktop_file(conflict['entry'], self._get_options_dict(conflict['entry'].id), ENGINES, LOG)

    def _handle_mismatch(self, conflict, use_file):
        if use_file:
            self._upsert_entry_from_file(conflict['file'], existing_entry=conflict['entry'])
            return
        export_desktop_file(conflict['entry'], self._get_options_dict(conflict['entry'].id), ENGINES, LOG)

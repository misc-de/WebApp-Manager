from copy import deepcopy
import json
import locale
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
LANG_DIR = APP_DIR / 'lang'
DEFAULT_CONFIG_PATH = APP_DIR / 'config.json'
USER_CONFIG_DIR = Path.home() / '.config' / 'webapp-manager'
USER_CONFIG_PATH = USER_CONFIG_DIR / 'config.json'
LEGACY_USER_CONFIG_PATH = Path.home() / '.local/share/vibecode/config.json'
DEFAULT_LANGUAGE_CODE = 'en'
MUTABLE_CONFIG_KEYS = {'language', 'settings', 'window_state'}


_CONFIG_CACHE = None
_TRANSLATION_CACHE = {}
_LANGUAGE_METADATA_CACHE = None


def _load_json_file(path: Path):
    with open(path, 'r', encoding='utf-8') as file_handle:
        return json.load(file_handle)


def _deep_merge(base, override):
    result = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _normalize_language_code(value):
    raw = str(value or '').strip().lower().replace('-', '_')
    if not raw:
        return ''
    raw = raw.split('.', 1)[0]
    raw = raw.split('@', 1)[0]
    parts = [part for part in raw.split('_') if part]
    if not parts:
        return ''
    if len(parts) == 1:
        return parts[0]
    return f'{parts[0]}_{parts[1]}'


def _base_language_code(code: str) -> str:
    normalized = _normalize_language_code(code)
    if not normalized:
        return ''
    return normalized.split('_', 1)[0]


def _language_path(code: str) -> Path:
    return LANG_DIR / f'{code}.json'


def _discover_language_metadata(force_reload=False):
    global _LANGUAGE_METADATA_CACHE
    if _LANGUAGE_METADATA_CACHE is not None and not force_reload:
        return deepcopy(_LANGUAGE_METADATA_CACHE)

    metadata = []
    seen = set()
    if LANG_DIR.exists():
        for path in sorted(LANG_DIR.glob('*.json')):
            code = _normalize_language_code(path.stem)
            if not code or code in seen:
                continue
            try:
                data = _load_json_file(path)
            except Exception:
                continue
            seen.add(code)
            metadata.append({
                'code': code,
                'name': str(data.get('_meta_language_name') or code.upper()),
            })
    if DEFAULT_LANGUAGE_CODE not in seen:
        metadata.append({'code': DEFAULT_LANGUAGE_CODE, 'name': 'English'})
    _LANGUAGE_METADATA_CACHE = metadata
    return deepcopy(_LANGUAGE_METADATA_CACHE)


def available_languages(force_reload=False):
    return _discover_language_metadata(force_reload=force_reload)


def get_system_language_code():
    candidates = []
    try:
        loc = locale.getlocale()[0]
        if loc:
            candidates.append(loc)
    except Exception:
        pass
    try:
        loc = locale.getdefaultlocale()[0]
        if loc:
            candidates.append(loc)
    except Exception:
        pass
    env_lang = os.environ.get('LANG')
    if env_lang:
        candidates.append(env_lang)

    available = {item['code'] for item in available_languages()}
    for candidate in candidates:
        exact = _normalize_language_code(candidate)
        base = _base_language_code(candidate)
        if exact in available:
            return exact
        if base in available:
            return base
    return DEFAULT_LANGUAGE_CODE


def _filter_mutable_config(data):
    filtered = {}
    if not isinstance(data, dict):
        return filtered
    for key in MUTABLE_CONFIG_KEYS:
        value = data.get(key)
        if isinstance(value, dict):
            filtered[key] = deepcopy(value)
        elif value is not None:
            filtered[key] = value
    return filtered


def save_app_config(data):
    global _CONFIG_CACHE
    if not isinstance(data, dict):
        raise TypeError('config data must be a dict')
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    USER_CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    _CONFIG_CACHE = deepcopy(data)
    return _CONFIG_CACHE


def update_app_config(mutator):
    config = dict(get_app_config(force_reload=True) or {})
    updated = mutator(config)
    if updated is None:
        updated = config
    return save_app_config(updated)


def get_app_config(force_reload=False):
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None and not force_reload:
        return deepcopy(_CONFIG_CACHE)

    defaults = {}
    if DEFAULT_CONFIG_PATH.exists():
        defaults = _load_json_file(DEFAULT_CONFIG_PATH)

    loaded = deepcopy(defaults) if isinstance(defaults, dict) else {}
    for path in (LEGACY_USER_CONFIG_PATH, USER_CONFIG_PATH):
        if path.exists():
            user_data = _filter_mutable_config(_load_json_file(path))
            loaded = _deep_merge(loaded, user_data)

    if not loaded:
        loaded = {'language': 'system'}

    _CONFIG_CACHE = loaded
    return deepcopy(_CONFIG_CACHE)


def get_configured_language_value():
    config = get_app_config()
    raw = str(config.get('language') or '').strip()
    if raw.lower() == 'system' or not raw:
        return 'system'
    return _normalize_language_code(raw)


def get_language_code():
    configured = get_configured_language_value()
    if configured == 'system':
        return get_system_language_code()
    available = {item['code'] for item in available_languages()}
    if configured in available:
        return configured
    base = _base_language_code(configured)
    return base if base in available else DEFAULT_LANGUAGE_CODE


def invalidate_i18n_cache(reload_config=False):
    global _CONFIG_CACHE, _TRANSLATION_CACHE, _LANGUAGE_METADATA_CACHE
    _TRANSLATION_CACHE = {}
    _LANGUAGE_METADATA_CACHE = None
    if reload_config:
        _CONFIG_CACHE = None


def get_translations(language_code=None):
    code = _normalize_language_code(language_code) or get_language_code()
    if code in _TRANSLATION_CACHE:
        return _TRANSLATION_CACHE[code]

    fallback = {}
    fallback_path = _language_path(DEFAULT_LANGUAGE_CODE)
    if fallback_path.exists():
        fallback = _load_json_file(fallback_path)

    translations = deepcopy(fallback) if isinstance(fallback, dict) else {}
    chain = []
    base = _base_language_code(code)
    if base and base != DEFAULT_LANGUAGE_CODE:
        chain.append(base)
    if code and code != base and code != DEFAULT_LANGUAGE_CODE:
        chain.append(code)

    for item in chain:
        target_path = _language_path(item)
        if target_path.exists():
            local = _load_json_file(target_path)
            if isinstance(local, dict):
                translations.update(local)
    if code == DEFAULT_LANGUAGE_CODE and fallback_path.exists():
        translations = fallback

    _TRANSLATION_CACHE[code] = translations
    return translations


def t(key, **kwargs):
    text = get_translations().get(key, key)
    if kwargs:
        return text.format(**kwargs)
    return text

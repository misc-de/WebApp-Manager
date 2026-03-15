import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from app_identity import APP_DATA_DIR
from i18n import get_app_config, save_app_config
from logger_setup import get_logger

LOG = get_logger(__name__)

CUSTOM_CSS_LINKS_KEY = 'Custom CSS Links'
CUSTOM_JS_LINKS_KEY = 'Custom JavaScript Links'
INLINE_CUSTOM_CSS_KEY = 'Inline Custom CSS'
INLINE_CUSTOM_JS_KEY = 'Inline Custom JavaScript'
CUSTOMIZER_FIREFOX_EXTENSION_ID = 'webapp-manager-customizer@de.cais'
CUSTOMIZER_FIREFOX_XPI_NAME = f'{CUSTOMIZER_FIREFOX_EXTENSION_ID}.xpi'
CHROMIUM_CUSTOMIZER_DIRNAME = 'webapp-manager-customizer'
FIREFOX_USER_CONTENT_START = '/* WEBAPP CUSTOM CSS START */\n'
FIREFOX_USER_CONTENT_END = '/* WEBAPP CUSTOM CSS END */\n'
ASSET_LIBRARY_DIR = APP_DATA_DIR / 'assets'
ASSET_LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
ASSET_TYPE_LABELS = {'css': 'CSS', 'javascript': 'JavaScript'}
ASSET_EXTENSION_MAP = {'.css': 'css', '.js': 'javascript'}
ASSET_OPTION_KEY_BY_TYPE = {'css': CUSTOM_CSS_LINKS_KEY, 'javascript': CUSTOM_JS_LINKS_KEY}
INLINE_OPTION_KEY_BY_TYPE = {'css': INLINE_CUSTOM_CSS_KEY, 'javascript': INLINE_CUSTOM_JS_KEY}
INLINE_CUSTOM_CSS_HASH_KEY = 'Inline Custom CSS Hash'
INLINE_CUSTOM_JS_HASH_KEY = 'Inline Custom JavaScript Hash'
INLINE_HASH_KEY_BY_TYPE = {'css': INLINE_CUSTOM_CSS_HASH_KEY, 'javascript': INLINE_CUSTOM_JS_HASH_KEY}


def _settings_dict(config=None):
    config = dict(config or get_app_config() or {})
    settings = config.get('settings')
    if not isinstance(settings, dict):
        settings = {}
        config['settings'] = settings
    return config, settings


def _library_metadata(settings=None):
    if settings is None:
        _config, settings = _settings_dict()
    library = settings.get('custom_assets')
    if not isinstance(library, list):
        library = []
    normalized = []
    seen = set()
    for item in library:
        if not isinstance(item, dict):
            continue
        asset_id = str(item.get('id') or '').strip()
        asset_type = str(item.get('type') or '').strip().lower()
        filename = str(item.get('filename') or '').strip()
        if not asset_id or asset_id in seen or asset_type not in ASSET_TYPE_LABELS or not filename:
            continue
        seen.add(asset_id)
        normalized.append({
            'id': asset_id,
            'name': str(item.get('name') or filename),
            'type': asset_type,
            'filename': filename,
            'imported_at': str(item.get('imported_at') or ''),
            'sha256': str(item.get('sha256') or '').strip().lower(),
        })
    return normalized


def _save_library_metadata(library):
    config, settings = _settings_dict()
    settings['custom_assets'] = [{
        'id': item['id'],
        'name': item['name'],
        'type': item['type'],
        'filename': item['filename'],
        'imported_at': item.get('imported_at', ''),
        'sha256': str(item.get('sha256') or '').strip().lower(),
    } for item in library]
    save_app_config(config)
    return list(library)


def list_custom_assets():
    library = _library_metadata()
    for item in library:
        item['path'] = str(asset_file_path(item))
    return sorted(library, key=lambda item: ((item.get('name') or '').lower(), item.get('imported_at') or '', item.get('id') or ''))


def asset_file_path(asset):
    asset_type = str((asset or {}).get('type') or '').strip().lower()
    filename = str((asset or {}).get('filename') or '').strip()
    return ASSET_LIBRARY_DIR / asset_type / filename


def get_custom_asset(asset_id):
    target = str(asset_id or '').strip()
    if not target:
        return None
    for item in _library_metadata():
        if item['id'] == target:
            item['path'] = str(asset_file_path(item))
            return item
    return None


def _asset_type_for_path(path):
    suffix = Path(path).suffix.lower()
    return ASSET_EXTENSION_MAP.get(suffix)


def _sha256_bytes(data):
    digest = hashlib.sha256()
    digest.update(data)
    return digest.hexdigest()


def asset_content_sha256_from_text(text):
    normalized = (text or '').replace('\r\n', '\n').replace('\r', '\n')
    return _sha256_bytes(normalized.encode('utf-8'))


def asset_file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def inline_asset_hash_for_options(options_dict, asset_type):
    option_key = INLINE_HASH_KEY_BY_TYPE.get(asset_type)
    if not option_key:
        return ''
    value = (options_dict or {}).get(option_key)
    return str(value or '').strip().lower()


def verify_asset_integrity(asset, logger=None):
    path = Path(asset['path'])
    expected = str(asset.get('sha256') or '').strip().lower()
    if not expected:
        return True
    try:
        actual = asset_file_sha256(path)
    except OSError:
        if logger is not None:
            logger.warning('Failed to read custom asset for integrity verification: %s', path)
        return False
    if actual != expected:
        if logger is not None:
            logger.warning('Custom asset hash mismatch for %s: expected=%s actual=%s', path, expected, actual)
        return False
    return True


def import_custom_asset(source_path):
    source = Path(source_path).expanduser()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(str(source))
    asset_type = _asset_type_for_path(source)
    if asset_type is None:
        raise ValueError('unsupported-asset-type')
    asset_id = f"asset-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    target_dir = ASSET_LIBRARY_DIR / asset_type
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f'{asset_id}{source.suffix.lower()}'
    target_path = target_dir / filename
    shutil.copy2(source, target_path)
    asset = {
        'id': asset_id,
        'name': source.name,
        'type': asset_type,
        'filename': filename,
        'imported_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'sha256': asset_file_sha256(target_path),
    }
    library = _library_metadata()
    library.append(asset)
    _save_library_metadata(library)
    asset['path'] = str(target_path)
    return asset


def remove_custom_asset(asset_id):
    target = str(asset_id or '').strip()
    if not target:
        return None
    library = _library_metadata()
    kept = []
    removed = None
    for item in library:
        if item['id'] == target and removed is None:
            removed = item
        else:
            kept.append(item)
    if removed is None:
        return None
    try:
        asset_file_path(removed).unlink(missing_ok=True)
    except OSError:
        LOG.warning('Failed to remove asset file for %s', target, exc_info=True)
    _save_library_metadata(kept)
    return removed


def _decode_raw_asset_ids(raw_value):
    if raw_value in (None, ''):
        return []
    if isinstance(raw_value, list):
        raw_items = raw_value
    else:
        text = str(raw_value).strip()
        if not text:
            return []
        try:
            decoded = json.loads(text)
            raw_items = decoded if isinstance(decoded, list) else [decoded]
        except (json.JSONDecodeError, TypeError, ValueError):
            raw_items = [segment.strip() for segment in text.split(',')]
    normalized = []
    seen = set()
    for item in raw_items:
        asset_id = str(item or '').strip()
        if not asset_id or asset_id in seen:
            continue
        seen.add(asset_id)
        normalized.append(asset_id)
    return normalized


def normalize_linked_asset_ids(raw_value, asset_type=None):
    allowed = {item['id']: item for item in _library_metadata()}
    normalized = []
    for asset_id in _decode_raw_asset_ids(raw_value):
        meta = allowed.get(asset_id)
        if meta is None:
            continue
        if asset_type and meta.get('type') != asset_type:
            continue
        normalized.append(asset_id)
    return normalized


def encode_linked_asset_ids(asset_ids, asset_type=None):
    return json.dumps(normalize_linked_asset_ids(asset_ids, asset_type=asset_type), ensure_ascii=False)


def _normalize_inline_asset_text(raw_value):
    if raw_value is None:
        return ''
    text = str(raw_value).replace('\r\n', '\n').replace('\r', '\n')
    return '' if not text.strip() else text


def inline_asset_text_for_options(options_dict, asset_type=None):
    options = dict(options_dict or {})
    if asset_type:
        key = INLINE_OPTION_KEY_BY_TYPE[asset_type]
        return _normalize_inline_asset_text(options.get(key))
    return {
        current_type: _normalize_inline_asset_text(options.get(key))
        for current_type, key in INLINE_OPTION_KEY_BY_TYPE.items()
    }


def has_runtime_customizations(options_dict):
    if linked_assets_for_options(options_dict):
        return True
    inline_values = inline_asset_text_for_options(options_dict)
    return any(bool(value) for value in inline_values.values())


def linked_assets_for_options(options_dict, asset_type=None):
    options = dict(options_dict or {})
    library = {item['id']: item for item in _library_metadata()}
    results = []
    types = [asset_type] if asset_type else ['css', 'javascript']
    for current_type in types:
        key = ASSET_OPTION_KEY_BY_TYPE[current_type]
        for asset_id in normalize_linked_asset_ids(options.get(key), asset_type=current_type):
            meta = library.get(asset_id)
            if meta is None:
                continue
            path = asset_file_path(meta)
            if not path.exists():
                continue
            item = dict(meta)
            item['path'] = str(path)
            if verify_asset_integrity(item, logger=LOG):
                results.append(item)
    return results


def count_asset_references(db, asset_id):
    target = str(asset_id or '').strip()
    if not target:
        return 0
    count = 0
    rows = db.cursor.execute(
        'SELECT DISTINCT entry_id, option_key, option_value FROM options WHERE option_key IN (?, ?)',
        (CUSTOM_CSS_LINKS_KEY, CUSTOM_JS_LINKS_KEY),
    ).fetchall()
    for entry_id, option_key, option_value in rows:
        if target in _decode_raw_asset_ids(option_value):
            count += 1
    return count


def detach_asset_from_entries(db, asset_id):
    target = str(asset_id or '').strip()
    affected_entry_ids = []
    if not target:
        return affected_entry_ids
    rows = db.cursor.execute(
        'SELECT entry_id, option_key, option_value FROM options WHERE option_key IN (?, ?)',
        (CUSTOM_CSS_LINKS_KEY, CUSTOM_JS_LINKS_KEY),
    ).fetchall()
    for entry_id, option_key, option_value in rows:
        current = _decode_raw_asset_ids(option_value)
        if target not in current:
            continue
        updated = [item for item in current if item != target]
        db.add_option(entry_id, option_key, json.dumps(updated, ensure_ascii=False))
        affected_entry_ids.append(int(entry_id))
    return sorted(set(affected_entry_ids))


def format_asset_date(value):
    raw = str(value or '').strip()
    if not raw:
        return ''
    try:
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        return raw
    return dt.astimezone().strftime('%Y-%m-%d %H:%M')


def _css_scope_start(address):
    parsed = urlparse((address or '').strip())
    scheme = (parsed.scheme or '').strip().lower()
    if scheme not in {'http', 'https'}:
        return ''
    netloc = (parsed.netloc or '').strip()
    if not netloc:
        return ''
    return f'{scheme}://{netloc}/'


def _content_script_matches(address):
    parsed = urlparse((address or '').strip())
    scheme = (parsed.scheme or '').strip().lower()
    if scheme not in {'http', 'https'}:
        return []
    netloc = (parsed.netloc or '').strip()
    if not netloc:
        return []
    return [f'{scheme}://{netloc}/*']


def _sanitize_extension_filename(asset_id, name, suffix):
    stem = re.sub(r'[^A-Za-z0-9._-]+', '-', Path(name or asset_id).stem).strip('-') or asset_id
    return f'{stem}{suffix}'


def _read_asset_text(asset):
    if not verify_asset_integrity(asset, logger=LOG):
        raise ValueError(f"custom asset integrity check failed: {asset.get('id')}")
    return Path(asset['path']).read_text(encoding='utf-8', errors='ignore')


def _write_firefox_user_content(profile_dir, address, css_assets, inline_css_text=''):
    profile_dir = Path(profile_dir)
    chrome_dir = profile_dir / 'chrome'
    chrome_dir.mkdir(parents=True, exist_ok=True)
    user_content_path = chrome_dir / 'userContent.css'
    existing = ''
    if user_content_path.exists():
        try:
            existing = user_content_path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            existing = ''
    start = FIREFOX_USER_CONTENT_START
    end = FIREFOX_USER_CONTENT_END
    pattern = re.compile(re.escape(start) + r'.*?' + re.escape(end), re.DOTALL)
    cleaned = pattern.sub('', existing).rstrip()
    scope = _css_scope_start(address)
    block = ''
    inline_css_text = _normalize_inline_asset_text(inline_css_text)
    if (css_assets or inline_css_text) and scope:
        sections = []
        for asset in css_assets:
            sections.append(f'/* Asset: {asset["name"]} */\n{_read_asset_text(asset).rstrip()}\n')
        if inline_css_text:
            sections.append(f'/* Inline CSS */\n{inline_css_text.rstrip()}\n')
        joined = '\n'.join(section.rstrip() for section in sections if section).rstrip()
        block = (
            start
            + f'@-moz-document url-prefix("{scope}") {{\n{joined}\n}}\n'
            + end
        )
    final_text = cleaned
    if block:
        final_text = (cleaned + '\n\n' + block).lstrip('\n') if cleaned else block
    if final_text.strip():
        user_content_path.write_text(final_text.rstrip() + '\n', encoding='utf-8')
    else:
        user_content_path.unlink(missing_ok=True)


def _remove_firefox_customizer_xpi(profile_dir):
    profile_dir = Path(profile_dir)
    try:
        (profile_dir / 'extensions' / CUSTOMIZER_FIREFOX_XPI_NAME).unlink(missing_ok=True)
    except OSError:
        LOG.debug('Failed to remove Firefox customizer extension', exc_info=True)


def _write_firefox_customizer_xpi(profile_dir, address, js_assets, inline_js_text=''):
    profile_dir = Path(profile_dir)
    extensions_dir = profile_dir / 'extensions'
    extensions_dir.mkdir(parents=True, exist_ok=True)
    target = extensions_dir / CUSTOMIZER_FIREFOX_XPI_NAME
    matches = _content_script_matches(address)
    inline_js_text = _normalize_inline_asset_text(inline_js_text)
    if not matches or not (js_assets or inline_js_text):
        target.unlink(missing_ok=True)
        return False
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        asset_rel_paths = []
        for index, asset in enumerate(js_assets, start=1):
            filename = _sanitize_extension_filename(asset['id'], asset['name'], f'-{index}.js')
            rel_path = Path('assets') / filename
            target_path = tmp_root / rel_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(_read_asset_text(asset), encoding='utf-8')
            asset_rel_paths.append(str(rel_path).replace('\\', '/'))
        if inline_js_text:
            inline_rel_path = Path('assets') / 'inline-runtime.js'
            inline_target = tmp_root / inline_rel_path
            inline_target.parent.mkdir(parents=True, exist_ok=True)
            inline_target.write_text(inline_js_text.rstrip() + '\n', encoding='utf-8')
            asset_rel_paths.append(inline_rel_path.as_posix())
        manifest = {
            'manifest_version': 3,
            'name': 'WebApp Manager Runtime Customizations',
            'version': '1.0',
            'description': 'Managed local JavaScript customizations for a WebApp Manager profile.',
            'browser_specific_settings': {
                'gecko': {
                    'id': CUSTOMIZER_FIREFOX_EXTENSION_ID,
                }
            },
            'host_permissions': matches,
            'content_scripts': [{
                'matches': matches,
                'js': asset_rel_paths,
                'run_at': 'document_idle',
            }],
        }
        (tmp_root / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        with tempfile.NamedTemporaryFile(dir=extensions_dir, delete=False, suffix='.xpi') as handle:
            tmp_xpi = Path(handle.name)
        try:
            with zipfile.ZipFile(tmp_xpi, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for candidate in tmp_root.rglob('*'):
                    if candidate.is_file():
                        archive.write(candidate, candidate.relative_to(tmp_root).as_posix())
            tmp_xpi.replace(target)
        finally:
            tmp_xpi.unlink(missing_ok=True)
    return True


def _chromium_extension_dir(profile_dir):
    return Path(profile_dir) / CHROMIUM_CUSTOMIZER_DIRNAME


def _remove_chromium_extension(profile_dir):
    target_dir = _chromium_extension_dir(profile_dir)
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)


def _write_chromium_customizer(profile_dir, address, css_assets, js_assets, inline_css_text='', inline_js_text=''):
    target_dir = _chromium_extension_dir(profile_dir)
    matches = _content_script_matches(address)
    inline_css_text = _normalize_inline_asset_text(inline_css_text)
    inline_js_text = _normalize_inline_asset_text(inline_js_text)
    if not matches or not (css_assets or js_assets or inline_css_text or inline_js_text):
        _remove_chromium_extension(profile_dir)
        return False
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    target_dir.mkdir(parents=True, exist_ok=True)
    rel_css = []
    rel_js = []
    for index, asset in enumerate(css_assets, start=1):
        filename = _sanitize_extension_filename(asset['id'], asset['name'], f'-{index}.css')
        rel_path = Path('assets') / filename
        target_path = target_dir / rel_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(_read_asset_text(asset), encoding='utf-8')
        rel_css.append(rel_path.as_posix())
    if inline_css_text:
        inline_css_rel = Path('assets') / 'inline-runtime.css'
        inline_css_target = target_dir / inline_css_rel
        inline_css_target.parent.mkdir(parents=True, exist_ok=True)
        inline_css_target.write_text(inline_css_text.rstrip() + '\n', encoding='utf-8')
        rel_css.append(inline_css_rel.as_posix())
    for index, asset in enumerate(js_assets, start=1):
        filename = _sanitize_extension_filename(asset['id'], asset['name'], f'-{index}.js')
        rel_path = Path('assets') / filename
        target_path = target_dir / rel_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(_read_asset_text(asset), encoding='utf-8')
        rel_js.append(rel_path.as_posix())
    if inline_js_text:
        inline_js_rel = Path('assets') / 'inline-runtime.js'
        inline_js_target = target_dir / inline_js_rel
        inline_js_target.parent.mkdir(parents=True, exist_ok=True)
        inline_js_target.write_text(inline_js_text.rstrip() + '\n', encoding='utf-8')
        rel_js.append(inline_js_rel.as_posix())
    manifest = {
        'manifest_version': 3,
        'name': 'WebApp Manager Runtime Customizations',
        'version': '1.0',
        'description': 'Managed local CSS and JavaScript customizations for a WebApp Manager profile.',
        'host_permissions': matches,
        'content_scripts': [{
            'matches': matches,
            'run_at': 'document_idle',
        }],
    }
    script_block = manifest['content_scripts'][0]
    if rel_css:
        script_block['css'] = rel_css
    if rel_js:
        script_block['js'] = rel_js
    (target_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return True


def ensure_profile_customizations(profile_info, options_dict, logger):
    if not profile_info or not profile_info.get('profile_path'):
        return {'css_applied': False, 'js_applied': False}
    family = str(profile_info.get('browser_family') or '').strip().lower()
    profile_path = profile_info.get('profile_path')
    address = (options_dict or {}).get('Address') or ''
    css_assets = linked_assets_for_options(options_dict, 'css')
    js_assets = linked_assets_for_options(options_dict, 'javascript')
    inline_css_text = inline_asset_text_for_options(options_dict, 'css')
    inline_js_text = inline_asset_text_for_options(options_dict, 'javascript')
    applied_css = False
    applied_js = False
    if family == 'firefox':
        try:
            _write_firefox_user_content(profile_path, address, css_assets, inline_css_text=inline_css_text)
            applied_css = bool((css_assets or inline_css_text) and _css_scope_start(address))
        except OSError as error:
            logger.warning('Failed to write Firefox custom CSS for %s: %s', profile_path, error)
        try:
            applied_js = _write_firefox_customizer_xpi(profile_path, address, js_assets, inline_js_text=inline_js_text)
        except OSError as error:
            logger.warning('Failed to write Firefox custom JS extension for %s: %s', profile_path, error)
            applied_js = False
        return {'css_applied': applied_css, 'js_applied': applied_js}
    if family in {'chrome', 'chromium'}:
        try:
            applied = _write_chromium_customizer(profile_path, address, css_assets, js_assets, inline_css_text=inline_css_text, inline_js_text=inline_js_text)
            applied_css = bool(applied and (css_assets or inline_css_text))
            applied_js = bool(applied and (js_assets or inline_js_text))
        except OSError as error:
            logger.warning('Failed to write Chromium customizations for %s: %s', profile_path, error)
        return {'css_applied': applied_css, 'js_applied': applied_js}
    return {'css_applied': False, 'js_applied': False}


def chromium_runtime_extension_args(profile_info, options_dict):
    if not profile_info or str(profile_info.get('browser_family') or '').strip().lower() not in {'chrome', 'chromium'}:
        return []
    if not has_runtime_customizations(options_dict):
        return []
    target_dir = _chromium_extension_dir(profile_info.get('profile_path') or '')
    if not target_dir.exists():
        return []
    return [f'--load-extension={target_dir}']


def firefox_requires_signed_runtime_js():
    return True

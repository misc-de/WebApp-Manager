"""Firefox extension management for managed profiles.

Extension discovery, payload loading/scoping, signature heuristics, zip-slip
guards and the install/sync state machine (uBlock Origin, swipe gestures).
Depends only on browser_paths plus the leaf utility input_validation.
"""
import io
import json
import shutil
import tempfile
import zipfile
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlparse

from webapp_constants import ADDRESS_KEY
from input_validation import open_guarded_url
from browser_paths import (
    DEFAULT_FIREFOX_EXTENSIONS,
    get_firefox_extension_config,
    _is_explicitly_managed_profile_dir,
)


# Upper bound for a downloaded XPI. uBlock Origin is ~4 MB, so this is
# generous; it exists so a hostile or misconfigured source cannot stream an
# unbounded amount of data into memory.
MAX_EXTENSION_DOWNLOAD_SIZE = 32 * 1024 * 1024


def _extract_firefox_extension_id(xpi_bytes, fallback_id):
    try:
        with zipfile.ZipFile(io.BytesIO(xpi_bytes)) as archive:
            for candidate in ('manifest.json', 'package/manifest.json'):
                if candidate in archive.namelist():
                    manifest = json.loads(archive.read(candidate).decode('utf-8'))
                    gecko = (manifest.get('browser_specific_settings') or {}).get('gecko', {})
                    addon_id = gecko.get('id') or ((manifest.get('applications') or {}).get('gecko', {}) or {}).get('id')
                    if addon_id:
                        return addon_id
    except (zipfile.BadZipFile, OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    return fallback_id

def swipe_extension_mode_value(options_dict=None):
    return 'production'


def _firefox_extension_candidates(extension_name, local_development_override=False):
    config = get_firefox_extension_config(extension_name)
    defaults = DEFAULT_FIREFOX_EXTENSIONS.get(extension_name, {})
    configured_id = (config.get('id') or defaults.get('id') or '').strip()
    configured_marker = (config.get('marker_file') or defaults.get('marker_file') or '').strip()
    return {
        'id': configured_id,
        'bundle_path': (config.get('bundle_path') or defaults.get('bundle_path') or '').strip(),
        'dev_bundle_path': str(config.get('dev_bundle_path') or defaults.get('dev_bundle_path') or '').strip(),
        'download_url': (config.get('download_url') or defaults.get('download_url') or '').strip(),
        'allow_unsigned_local_bundle': bool(
            local_development_override
            and (config.get('allow_unsigned_local_bundle') if 'allow_unsigned_local_bundle' in config else defaults.get('allow_unsigned_local_bundle', False))
        ),
        'marker_file': configured_marker,
    }

def _managed_firefox_extension_paths(profile_dir, extension_name, local_development_override=False):
    profile_dir = Path(profile_dir)
    extensions_dir = profile_dir / 'extensions'
    candidates = _firefox_extension_candidates(extension_name, local_development_override=local_development_override)
    configured_id = (candidates.get('id') or '').strip()
    marker_name = (candidates.get('marker_file') or '').strip()
    marker_paths = [extensions_dir / marker_name] if marker_name else []
    ids = [configured_id] if configured_id else []
    legacy_marker_paths = []
    legacy_ids = []
    if extension_name == 'swipe':
        legacy_marker_paths.append(extensions_dir / '.webapp_simple_swipe_navigator_extension_id')
        legacy_ids.append('{6f3ab763-a4c2-4183-b596-984bf5b7ac31}')
    for marker_path in marker_paths:
        if not marker_path.exists():
            continue
        try:
            addon_id = marker_path.read_text(encoding='utf-8').strip()
        except OSError:
            addon_id = ''
        if addon_id and addon_id not in ids:
            ids.append(addon_id)
    for marker_path in legacy_marker_paths:
        if not marker_path.exists():
            continue
        try:
            addon_id = marker_path.read_text(encoding='utf-8').strip()
        except OSError:
            addon_id = ''
        if addon_id and addon_id not in legacy_ids:
            legacy_ids.append(addon_id)
    xpi_paths = [extensions_dir / f'{addon_id}.xpi' for addon_id in ids if addon_id]
    legacy_xpi_paths = [extensions_dir / f'{addon_id}.xpi' for addon_id in legacy_ids if addon_id]
    return {
        'extensions_dir': extensions_dir,
        'primary_marker_path': marker_paths[0] if marker_paths else None,
        'marker_paths': marker_paths,
        'ids': ids,
        'xpi_paths': xpi_paths,
        'legacy_marker_paths': legacy_marker_paths,
        'legacy_ids': legacy_ids,
        'legacy_xpi_paths': legacy_xpi_paths,
        'configured_id': configured_id,
        'bundle_path': candidates.get('bundle_path') or '',
        'dev_bundle_path': candidates.get('dev_bundle_path') or '',
        'download_url': candidates.get('download_url') or '',
        'allow_unsigned_local_bundle': bool(candidates.get('allow_unsigned_local_bundle', False)),
    }

def _firefox_extension_paths(profile_dir, marker_name, fallback_id):
    profile_dir = Path(profile_dir)
    extensions_dir = profile_dir / 'extensions'
    marker_path = extensions_dir / marker_name
    managed_addon_id = fallback_id
    if marker_path.exists():
        try:
            managed_addon_id = marker_path.read_text(encoding='utf-8').strip() or fallback_id
        except OSError:
            managed_addon_id = fallback_id
    target = extensions_dir / f'{managed_addon_id}.xpi'
    return extensions_dir, marker_path, managed_addon_id, target


def firefox_extension_installed(profile_dir, extension_name):
    if not profile_dir:
        return False
    managed = _managed_firefox_extension_paths(profile_dir, extension_name)
    profile_dir = Path(profile_dir)
    ids = set(managed['ids']) | set(managed.get('legacy_ids') or [])
    state_path = profile_dir / 'extensions.json'
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding='utf-8'))
            for addon in data.get('addons') or []:
                addon_id = str(addon.get('id') or '').strip()
                if addon_id in ids and addon.get('active') is True and addon.get('hidden') is not True:
                    return True
            # If Firefox has written a state file but the add-on is not active there,
            # do not treat a stale XPI alone as installed.
            return False
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return any(path.exists() for path in [*managed['xpi_paths'], *(managed.get('legacy_xpi_paths') or [])])


def _resolve_bundled_extension_path(bundle_path):
    bundle_path = (bundle_path or '').strip()
    if not bundle_path:
        return None
    candidates = [Path(bundle_path)]
    normalized = bundle_path.replace('\\', '/')
    if normalized.startswith('extensions/'):
        candidates.append(Path('extension') / normalized[len('extensions/'):])
    elif normalized.startswith('extension/'):
        candidates.append(Path('extensions') / normalized[len('extension/'):])

    app_root = Path(__file__).resolve().parent
    for candidate in candidates:
        resolved_candidate = candidate
        if not resolved_candidate.is_absolute():
            resolved_candidate = app_root / resolved_candidate
        try:
            resolved_candidate = resolved_candidate.resolve()
        except OSError:
            continue
        if resolved_candidate.exists() and resolved_candidate.is_file():
            return resolved_candidate
    return None


def _xpi_has_signature(xpi_bytes):
    try:
        with zipfile.ZipFile(io.BytesIO(xpi_bytes)) as archive:
            names = {name.upper() for name in archive.namelist()}
    except (zipfile.BadZipFile, OSError):
        return False
    return any(name.startswith('META-INF/') for name in names)

def _content_script_matches_for_address(address):
    parsed = urlparse((address or '').strip())
    scheme = (parsed.scheme or '').strip().lower()
    if scheme not in {'http', 'https'}:
        return []
    netloc = (parsed.netloc or '').strip()
    if not netloc:
        return []
    return [f'{scheme}://{netloc}/*']


def _assert_safe_zip_members(archive, tmp_root):
    root = tmp_root.resolve()
    for member in archive.infolist():
        name = member.filename
        if name.startswith('/') or '\\' in name or '..' in Path(name).parts:
            raise ValueError(f'unsafe archive member path: {name!r}')
        target = (root / name).resolve()
        if target != root and root not in target.parents:
            raise ValueError(f'archive member escapes extraction root: {name!r}')


# Manifest metadata for the locally re-scoped copy of the swipe-gestures
# add-on. Deliberately English and independent of the UI language: the manifest
# is written into the browser profile once at install time, so a translated
# name would go stale as soon as the user switches the app language.
SCOPED_SWIPE_EXTENSION_NAME = 'Swipe Gestures (WebApp Manager)'
SCOPED_SWIPE_EXTENSION_SHORT_NAME = 'Swipe Gestures'
SCOPED_SWIPE_EXTENSION_DESCRIPTION = (
    'Locally re-scoped swipe-gesture add-on, restricted to the configured WebApp domain.'
)


def _scope_swipe_extension_payload(xpi_bytes, address):
    matches = _content_script_matches_for_address(address)
    if not matches:
        return xpi_bytes
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        with zipfile.ZipFile(io.BytesIO(xpi_bytes)) as archive:
            _assert_safe_zip_members(archive, tmp_root)
            archive.extractall(tmp_root)
        manifest_path = tmp_root / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['name'] = SCOPED_SWIPE_EXTENSION_NAME
        manifest['short_name'] = SCOPED_SWIPE_EXTENSION_SHORT_NAME
        manifest['description'] = SCOPED_SWIPE_EXTENSION_DESCRIPTION
        manifest['host_permissions'] = matches
        content_scripts = list(manifest.get('content_scripts') or [])
        if content_scripts:
            content_scripts[0]['matches'] = matches
        manifest['content_scripts'] = content_scripts
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for candidate in sorted(tmp_root.rglob('*')):
                if not candidate.is_file():
                    continue
                rel_path = candidate.relative_to(tmp_root).as_posix()
                if rel_path.upper().startswith('META-INF/'):
                    continue
                archive.write(candidate, rel_path)
        return payload.getvalue()


def _load_firefox_extension_payload(managed, logger, extension_name, address=''):
    if bool(managed.get('allow_unsigned_local_bundle', False)):
        dev_bundle_path = _resolve_bundled_extension_path(managed.get('dev_bundle_path') or '')
        if dev_bundle_path is not None:
            try:
                payload = dev_bundle_path.read_bytes()
            except OSError as error:
                logger.warning('Failed to read local Firefox extension %s from %s: %s', extension_name, dev_bundle_path, error)
                return None, f'dev-bundle-read-error:{error}', False
            if extension_name == 'swipe':
                payload = _scope_swipe_extension_payload(payload, address)
            return payload, f'bundle:{dev_bundle_path}', _xpi_has_signature(payload)
    bundle_path = _resolve_bundled_extension_path(managed.get('bundle_path') or '')
    download_url = (managed.get('download_url') or '').strip()
    if download_url:
        try:
            # Extension sources are AMO-style public URLs, so this is the
            # strict variant of the guard: no private/loopback target is
            # legitimate here, on the first request or after a redirect.
            with open_guarded_url(
                download_url,
                headers={'User-Agent': 'Mozilla/5.0'},
                timeout=20,
                allow_private_targets=False,
            ) as response:
                payload = response.read(MAX_EXTENSION_DOWNLOAD_SIZE + 1)
            if len(payload) > MAX_EXTENSION_DOWNLOAD_SIZE:
                logger.warning('Firefox extension download for %s exceeds %d bytes', extension_name, MAX_EXTENSION_DOWNLOAD_SIZE)
                return None, f'download-too-large:{download_url}', False
            return payload, download_url, _xpi_has_signature(payload)
        except (OSError, ValueError, urllib.error.URLError) as error:
            logger.warning('Failed to download Firefox extension %s from %s: %s', extension_name, download_url, error)
            if bundle_path is None:
                return None, str(error), False
            logger.info('Falling back to local Firefox extension %s from %s after download failure', extension_name, bundle_path)
    if bundle_path is not None:
        try:
            payload = bundle_path.read_bytes()
        except OSError as error:
            logger.warning('Failed to read bundled Firefox extension %s from %s: %s', extension_name, bundle_path, error)
            return None, f'bundle-read-error:{error}', False
        return payload, f'bundle:{bundle_path}', _xpi_has_signature(payload)
    if not download_url:
        return None, 'missing-extension-source', False
    return None, 'missing-extension-payload', False


def _allows_unsigned_local_extension_payload(managed, payload_source, profile_dir):
    if not bool(managed.get('allow_unsigned_local_bundle', False)):
        return False
    if not profile_dir or not _is_explicitly_managed_profile_dir(profile_dir, 'firefox'):
        return False
    dev_bundle_path = _resolve_bundled_extension_path(managed.get('dev_bundle_path') or '')
    if dev_bundle_path is None:
        return False
    return payload_source == f'bundle:{dev_bundle_path}'


def _invalidate_firefox_extension_state(profile_dir, logger):
    profile_dir = Path(profile_dir)
    changed = False
    for state_file in (
        profile_dir / 'addonStartup.json.lz4',
        profile_dir / 'extensions.json',
        profile_dir / 'extension-preferences.json',
    ):
        try:
            if state_file.exists():
                state_file.unlink()
                changed = True
        except OSError as error:
            logger.warning('Failed to remove Firefox extension state file %s: %s', state_file, error)
    startup_cache_dir = profile_dir / 'startupCache'
    if startup_cache_dir.exists():
        try:
            shutil.rmtree(startup_cache_dir)
            changed = True
        except OSError as error:
            logger.warning('Failed to remove Firefox startup cache %s: %s', startup_cache_dir, error)
    return changed


def _extension_sync_result(requested, installed, changed, error=None):
    """The single result shape every extension sync path returns."""
    return {'requested': requested, 'installed': installed, 'changed': changed, 'error': error}


def _write_extension_markers(managed, logger, keep_text=''):
    """Point the primary marker at `keep_text`, drop every other marker."""
    primary_marker_path = managed['primary_marker_path']
    keep_text = (keep_text or '').strip()
    for marker_path in [*managed['marker_paths'], *(managed.get('legacy_marker_paths') or [])]:
        try:
            if marker_path == primary_marker_path and keep_text:
                marker_path.write_text(keep_text, encoding='utf-8')
            else:
                marker_path.unlink(missing_ok=True)
        except OSError as error:
            logger.warning('Failed to update Firefox extension marker %s: %s', marker_path, error)


def _remove_extension_artifacts(managed, profile_dir, logger):
    """Teardown path: remove every managed XPI and marker, current or legacy."""
    changed = False
    for target in [*managed['xpi_paths'], *(managed.get('legacy_xpi_paths') or [])]:
        try:
            if target.exists():
                target.unlink()
                changed = True
        except OSError as error:
            logger.warning('Failed to remove Firefox extension %s: %s', target, error)
    for marker_path in [*managed['marker_paths'], *(managed.get('legacy_marker_paths') or [])]:
        try:
            if marker_path.exists():
                marker_path.unlink()
                changed = True
        except OSError as error:
            logger.warning('Failed to remove Firefox extension marker %s: %s', marker_path, error)
    changed = _invalidate_firefox_extension_state(profile_dir, logger) or changed
    return _extension_sync_result(False, False, changed)


def _keep_existing_extension(existing_target, managed, profile_dir, logger):
    """Adopt the XPI already in the profile: re-point markers, report no change."""
    _write_extension_markers(managed, logger, existing_target.stem)
    _invalidate_firefox_extension_state(profile_dir, logger)
    return _extension_sync_result(True, True, False)


def _resolve_extension_payload(managed, logger, extension_name, options_dict):
    try:
        return _load_firefox_extension_payload(
            managed,
            logger,
            extension_name,
            address=(options_dict or {}).get(ADDRESS_KEY, ''),
        )
    except Exception:
        logger.exception('Failed to resolve desired Firefox extension payload for %s', extension_name)
        return None, 'extension-payload-resolution-error', False


def _decide_on_existing_extension(existing_target, managed, profile_dir, extension_name, options_dict, logger, should_refresh):
    """Decide what to do about an XPI that is already installed.

    Returns `(result, payload)`. A non-None `result` means "keep what is there"
    and is the caller's return value. A non-None `payload` is the freshly
    resolved `(bytes, source, signed)` triple, handed back so the install path
    does not resolve -- and possibly re-download -- the very same payload again.
    """
    if not should_refresh:
        return _keep_existing_extension(existing_target, managed, profile_dir, logger), None

    desired_payload, payload_source_hint, payload_signed_hint = _resolve_extension_payload(
        managed, logger, extension_name, options_dict
    )
    if desired_payload is None:
        return _keep_existing_extension(existing_target, managed, profile_dir, logger), None

    try:
        existing_payload = existing_target.read_bytes()
    except OSError as error:
        logger.warning('Failed to read installed Firefox extension %s from %s: %s', extension_name, existing_target, error)
        existing_payload = None
    if existing_payload == desired_payload:
        return _keep_existing_extension(existing_target, managed, profile_dir, logger), None

    logger.info(
        'Refreshing Firefox extension %s in %s because the installed payload differs from %s',
        extension_name,
        profile_dir,
        payload_source_hint,
    )
    return None, (desired_payload, payload_source_hint, payload_signed_hint)


def _store_extension_payload(payload, install_id, managed, profile_dir, extension_name, local_development_override, logger):
    """Write the XPI atomically and drop whatever older copies remain."""
    extensions_dir = managed['extensions_dir']
    target = extensions_dir / f'{install_id}.xpi'
    with tempfile.NamedTemporaryFile(dir=extensions_dir, delete=False) as tmp_file:
        tmp_file.write(payload)
        temp_name = tmp_file.name
    Path(temp_name).replace(target)
    refreshed = _managed_firefox_extension_paths(profile_dir, extension_name, local_development_override=local_development_override)
    for stale in [*refreshed['xpi_paths'], *(refreshed.get('legacy_xpi_paths') or [])]:
        if stale == target:
            continue
        try:
            stale.unlink(missing_ok=True)
        except OSError as error:
            logger.warning('Failed to remove stale Firefox extension %s: %s', stale, error)
    return target


def _extension_payload_is_installable(payload_signed, managed, payload_source, profile_dir, extension_name, logger):
    """Signature gate. Unsigned payloads pass only for a local bundle in a
    managed profile -- release Firefox refuses them otherwise."""
    if payload_signed:
        return True
    if _allows_unsigned_local_extension_payload(managed, payload_source, profile_dir):
        logger.warning(
            'Allowing unsigned local Firefox extension %s from %s only for managed profile %s',
            extension_name,
            payload_source,
            profile_dir,
        )
        return True
    logger.warning('Firefox extension %s payload from %s does not appear to be Mozilla-signed; release Firefox builds usually block unsigned add-ons', extension_name, payload_source)
    return False


def _sync_firefox_signed_extension(profile_dir, enabled, logger, extension_name, local_development_override=False, options_dict=None):
    if not profile_dir:
        return _extension_sync_result(bool(enabled), False, False, 'missing-profile')
    managed = _managed_firefox_extension_paths(profile_dir, extension_name, local_development_override=local_development_override)
    managed['extensions_dir'].mkdir(parents=True, exist_ok=True)
    configured_id = (managed['configured_id'] or '').strip()
    bundle_path = (managed.get('bundle_path') or '').strip()
    dev_bundle_path = (managed.get('dev_bundle_path') or '').strip()
    download_url = (managed.get('download_url') or '').strip()

    if not enabled:
        return _remove_extension_artifacts(managed, profile_dir, logger)

    resolved_payload = None
    existing_target = next((path for path in managed['xpi_paths'] if path.exists()), None)
    if existing_target is not None:
        keep_result, resolved_payload = _decide_on_existing_extension(
            existing_target,
            managed,
            profile_dir,
            extension_name,
            options_dict,
            logger,
            should_refresh=bool(local_development_override or bundle_path),
        )
        if keep_result is not None:
            return keep_result

    if not configured_id:
        logger.warning('Missing Firefox extension ID for %s', extension_name)
        return _extension_sync_result(True, False, False, 'missing-addon-id')
    source_available = bool(bundle_path or download_url or (managed.get('allow_unsigned_local_bundle') and dev_bundle_path))
    if not source_available:
        logger.warning('Missing Firefox extension source for %s', extension_name)
        return _extension_sync_result(True, False, False, 'missing-extension-source')

    try:
        if resolved_payload is None:
            resolved_payload = _resolve_extension_payload(managed, logger, extension_name, options_dict)
        payload, payload_source, payload_signed = resolved_payload
        if payload is None:
            if payload_source == 'missing-extension-source':
                return _extension_sync_result(True, False, False, 'missing-extension-source')
            return _extension_sync_result(True, False, False, 'missing-extension-payload')
        if not _extension_payload_is_installable(payload_signed, managed, payload_source, profile_dir, extension_name, logger):
            return _extension_sync_result(True, False, False, 'unsigned-extension-payload')

        install_id = configured_id
        detected_id = _extract_firefox_extension_id(payload, configured_id)
        if detected_id and detected_id != configured_id:
            logger.info('Firefox extension %s manifest ID %s differs from configured ID %s; installing under manifest ID', extension_name, detected_id, configured_id)
            install_id = detected_id

        target = _store_extension_payload(payload, install_id, managed, profile_dir, extension_name, local_development_override, logger)
        _write_extension_markers(managed, logger, install_id)
        _invalidate_firefox_extension_state(profile_dir, logger)
        logger.info('Installed Firefox extension %s into %s from %s', configured_id, target, payload_source)
        return _extension_sync_result(True, True, True)
    except (OSError, ValueError, zipfile.BadZipFile, urllib.error.URLError) as error:
        logger.warning('Failed to install Firefox extension %s from %s: %s', extension_name, dev_bundle_path or bundle_path or download_url, error)
        return _extension_sync_result(True, False, False, str(error))

def _sync_firefox_swipe_extension(profile_dir, enabled, logger, options_dict=None):
    if not enabled:
        return _sync_firefox_signed_extension(profile_dir, False, logger, 'swipe', local_development_override=True, options_dict=options_dict)
    return _sync_firefox_signed_extension(profile_dir, True, logger, 'swipe', local_development_override=True, options_dict=options_dict)


def _sync_firefox_adblock(profile_dir, enabled, logger):
    return _sync_firefox_signed_extension(profile_dir, enabled, logger, 'adblock')

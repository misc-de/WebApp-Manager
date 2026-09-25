"""Coverage tests for browser_extensions: manifest/ID extraction, marker and
XPI discovery, payload loading, the signature gate and the install/sync state
machine.

Profiles live in temporary directories, the extension config is stubbed per
test and `open_guarded_url` is always replaced, so nothing is downloaded and
nothing outside the sandbox is read or written.
"""
import io
import json
import logging
import os
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_browser_extensions.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import browser_extensions as be
from webapp_constants import ADDRESS_KEY


def _xpi(addon_id='addon@test', signed=True, legacy_applications=False, extra=None, manifest=None):
    buffer = io.BytesIO()
    if manifest is None:
        key = 'applications' if legacy_applications else 'browser_specific_settings'
        manifest = {'name': 'x', key: {'gecko': {'id': addon_id}} if addon_id else {}}
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('manifest.json', json.dumps(manifest))
        if signed:
            archive.writestr('META-INF/mozilla.rsa', b'sig')
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self, limit=-1):
        return self._payload if limit < 0 else self._payload[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _ProfileMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.profile = self.base / 'webapp_profile'
        self.profile.mkdir()
        self.ext_dir = self.profile / 'extensions'
        self.logger = mock.Mock()
        self.configs = {
            'adblock': {'id': 'adblock@test', 'marker_file': '.adblock_marker', 'download_url': ''},
            'swipe': {'id': 'swipe@test', 'marker_file': '.swipe_marker', 'bundle_path': '', 'download_url': '', 'allow_unsigned_local_bundle': False},
        }
        patcher = mock.patch.object(be, 'get_firefox_extension_config', side_effect=lambda name: dict(self.configs.get(name, {})))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(be, 'open_guarded_url', side_effect=AssertionError('network must be mocked explicitly'))
        self.open_url = patcher.start()
        self.addCleanup(patcher.stop)

    def _defaults(self, **overrides):
        """Replace DEFAULT_FIREFOX_EXTENSIONS so no real default leaks in."""
        patcher = mock.patch.object(be, 'DEFAULT_FIREFOX_EXTENSIONS', overrides)
        patcher.start()
        self.addCleanup(patcher.stop)


class ExtensionIdTests(unittest.TestCase):
    def test_id_from_browser_specific_settings_and_applications(self):
        self.assertEqual(be._extract_firefox_extension_id(_xpi('a@b'), 'fallback'), 'a@b')
        self.assertEqual(be._extract_firefox_extension_id(_xpi('c@d', legacy_applications=True), 'fallback'), 'c@d')

    def test_fallback_for_missing_id_or_broken_archive(self):
        self.assertEqual(be._extract_firefox_extension_id(_xpi(None), 'fallback'), 'fallback')
        self.assertEqual(be._extract_firefox_extension_id(b'not a zip', 'fallback'), 'fallback')

    def test_swipe_mode_is_always_production(self):
        self.assertEqual(be.swipe_extension_mode_value({'anything': '1'}), 'production')


class ManagedPathsTests(_ProfileMixin, unittest.TestCase):
    def test_marker_ids_are_added_to_the_configured_id(self):
        self.ext_dir.mkdir()
        (self.ext_dir / '.adblock_marker').write_text('other@test\n', encoding='utf-8')
        managed = be._managed_firefox_extension_paths(self.profile, 'adblock')
        self.assertEqual(managed['ids'], ['adblock@test', 'other@test'])
        self.assertEqual(managed['xpi_paths'], [self.ext_dir / 'adblock@test.xpi', self.ext_dir / 'other@test.xpi'])
        self.assertEqual(managed['primary_marker_path'], self.ext_dir / '.adblock_marker')

    def test_unreadable_marker_is_ignored(self):
        self.ext_dir.mkdir()
        (self.ext_dir / '.adblock_marker').write_text('other@test', encoding='utf-8')
        with mock.patch.object(Path, 'read_text', side_effect=OSError('denied')):
            managed = be._managed_firefox_extension_paths(self.profile, 'adblock')
        self.assertEqual(managed['ids'], ['adblock@test'])

    def test_swipe_legacy_marker_is_discovered(self):
        self.ext_dir.mkdir()
        legacy = self.ext_dir / '.webapp_simple_swipe_navigator_extension_id'
        legacy.write_text('legacy@old', encoding='utf-8')
        managed = be._managed_firefox_extension_paths(self.profile, 'swipe')
        self.assertEqual(managed['legacy_ids'], ['{6f3ab763-a4c2-4183-b596-984bf5b7ac31}', 'legacy@old'])
        self.assertIn(self.ext_dir / 'legacy@old.xpi', managed['legacy_xpi_paths'])
        with mock.patch.object(Path, 'read_text', side_effect=OSError('denied')):
            managed = be._managed_firefox_extension_paths(self.profile, 'swipe')
        self.assertEqual(managed['legacy_ids'], ['{6f3ab763-a4c2-4183-b596-984bf5b7ac31}'])

    def test_no_marker_and_no_id(self):
        self._defaults()
        self.configs['adblock'] = {}
        managed = be._managed_firefox_extension_paths(self.profile, 'adblock')
        self.assertIsNone(managed['primary_marker_path'])
        self.assertEqual(managed['ids'], [])

    def test_unsigned_local_bundle_needs_the_override(self):
        self.configs['swipe']['allow_unsigned_local_bundle'] = True
        self.assertFalse(be._firefox_extension_candidates('swipe')['allow_unsigned_local_bundle'])
        self.assertTrue(be._firefox_extension_candidates('swipe', local_development_override=True)['allow_unsigned_local_bundle'])

    def test_legacy_single_marker_paths(self):
        extensions_dir, marker, addon_id, target = be._firefox_extension_paths(self.profile, '.m', 'fallback@id')
        self.assertEqual((extensions_dir, addon_id, target), (self.ext_dir, 'fallback@id', self.ext_dir / 'fallback@id.xpi'))
        self.ext_dir.mkdir()
        marker.write_text('stored@id', encoding='utf-8')
        self.assertEqual(be._firefox_extension_paths(self.profile, '.m', 'fallback@id')[2], 'stored@id')
        marker.write_text('   ', encoding='utf-8')
        self.assertEqual(be._firefox_extension_paths(self.profile, '.m', 'fallback@id')[2], 'fallback@id')
        with mock.patch.object(Path, 'read_text', side_effect=OSError('denied')):
            self.assertEqual(be._firefox_extension_paths(self.profile, '.m', 'fallback@id')[2], 'fallback@id')


class InstalledDetectionTests(_ProfileMixin, unittest.TestCase):
    def _state(self, addons):
        (self.profile / 'extensions.json').write_text(json.dumps({'addons': addons}), encoding='utf-8')

    def test_no_profile_is_not_installed(self):
        self.assertFalse(be.firefox_extension_installed('', 'adblock'))

    def test_active_addon_in_state_file_counts(self):
        self._state([{'id': 'adblock@test', 'active': True}])
        self.assertTrue(be.firefox_extension_installed(self.profile, 'adblock'))

    def test_state_file_overrules_a_stale_xpi(self):
        self.ext_dir.mkdir()
        (self.ext_dir / 'adblock@test.xpi').write_bytes(b'x')
        self._state([{'id': 'adblock@test', 'active': False}, {'id': 'adblock@test', 'active': True, 'hidden': True}])
        self.assertFalse(be.firefox_extension_installed(self.profile, 'adblock'))

    def test_xpi_counts_without_or_with_corrupt_state_file(self):
        self.ext_dir.mkdir()
        self.assertFalse(be.firefox_extension_installed(self.profile, 'adblock'))
        (self.ext_dir / 'adblock@test.xpi').write_bytes(b'x')
        self.assertTrue(be.firefox_extension_installed(self.profile, 'adblock'))
        (self.profile / 'extensions.json').write_text('{broken', encoding='utf-8')
        self.assertTrue(be.firefox_extension_installed(self.profile, 'adblock'))


class BundlePathTests(unittest.TestCase):
    def test_empty_path(self):
        self.assertIsNone(be._resolve_bundled_extension_path('  '))

    def test_absolute_file(self):
        with tempfile.NamedTemporaryFile(suffix='.xpi') as handle:
            self.assertEqual(be._resolve_bundled_extension_path(handle.name), Path(handle.name).resolve())

    def test_relative_path_resolves_against_app_root_with_alias(self):
        app_root = Path(be.__file__).resolve().parent
        expected = app_root / 'extension' / 'swipe-gestures.xpi'
        self.assertEqual(be._resolve_bundled_extension_path('extension/swipe-gestures.xpi'), expected)
        self.assertEqual(be._resolve_bundled_extension_path('extensions/swipe-gestures.xpi'), expected)

    def test_missing_or_unresolvable_candidates(self):
        self.assertIsNone(be._resolve_bundled_extension_path('extension/does-not-exist.xpi'))
        original = Path.resolve

        def fail_for_candidate(path, *args, **kwargs):
            if str(path) == '/abs/thing.xpi':
                raise OSError('loop')
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, 'resolve', fail_for_candidate):
            self.assertIsNone(be._resolve_bundled_extension_path('/abs/thing.xpi'))


class SignatureAndScopeTests(unittest.TestCase):
    def test_signature_heuristic(self):
        self.assertTrue(be._xpi_has_signature(_xpi(signed=True)))
        self.assertFalse(be._xpi_has_signature(_xpi(signed=False)))
        self.assertFalse(be._xpi_has_signature(b'garbage'))

    def test_content_script_matches(self):
        self.assertEqual(be._content_script_matches_for_address('https://mail.example.com/inbox'), ['https://mail.example.com/*'])
        self.assertEqual(be._content_script_matches_for_address('ftp://example.com/'), [])
        self.assertEqual(be._content_script_matches_for_address('https:///path'), [])
        self.assertEqual(be._content_script_matches_for_address(None), [])

    def test_zip_member_escaping_through_a_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'root'
            root.mkdir()
            outside = Path(tmpdir) / 'outside'
            outside.mkdir()
            os.symlink(outside, root / 'link')
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w') as archive:
                archive.writestr('link/evil.js', 'x')
            with zipfile.ZipFile(buffer) as archive:
                with self.assertRaisesRegex(ValueError, 'escapes extraction root'):
                    be._assert_safe_zip_members(archive, root)

    def test_unsafe_member_names_are_rejected(self):
        for name in ('/etc/passwd', 'a\\b.js', '../escape.js', 'ok/../../escape.js'):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w') as archive:
                archive.writestr(name, 'x')
            with zipfile.ZipFile(buffer) as archive, tempfile.TemporaryDirectory() as tmpdir:
                with self.assertRaisesRegex(ValueError, 'unsafe archive member path'):
                    be._assert_safe_zip_members(archive, Path(tmpdir))

    def test_scoping_without_valid_address_returns_payload_unchanged(self):
        payload = _xpi()
        self.assertIs(be._scope_swipe_extension_payload(payload, 'not a url'), payload)

    def test_scoping_rewrites_manifest_and_drops_signature(self):
        manifest = {'name': 'orig', 'content_scripts': [{'matches': ['<all_urls>'], 'js': ['a.js']}]}
        payload = _xpi(manifest=manifest, extra={'a.js': 'x', 'sub/b.js': 'y'})
        scoped = be._scope_swipe_extension_payload(payload, 'https://example.com/app')
        with zipfile.ZipFile(io.BytesIO(scoped)) as archive:
            names = set(archive.namelist())
            result = json.loads(archive.read('manifest.json'))
        self.assertEqual(names, {'manifest.json', 'a.js', 'sub/b.js'})
        self.assertEqual(result['name'], be.SCOPED_SWIPE_EXTENSION_NAME)
        self.assertEqual(result['host_permissions'], ['https://example.com/*'])
        self.assertEqual(result['content_scripts'][0]['matches'], ['https://example.com/*'])


class PayloadLoadingTests(_ProfileMixin, unittest.TestCase):
    def test_dev_bundle_is_used_and_scoped_for_swipe(self):
        bundle = self.base / 'dev.xpi'
        bundle.write_bytes(_xpi(signed=False))
        managed = {'allow_unsigned_local_bundle': True, 'dev_bundle_path': str(bundle)}
        with mock.patch.object(be, '_scope_swipe_extension_payload', return_value=b'scoped') as scope:
            payload, source, signed = be._load_firefox_extension_payload(managed, self.logger, 'swipe', address='https://a.example/')
        scope.assert_called_once()
        self.assertEqual((payload, source, signed), (b'scoped', f'bundle:{bundle}', False))

    def test_dev_bundle_read_error(self):
        bundle = self.base / 'dev.xpi'
        bundle.write_bytes(b'x')
        managed = {'allow_unsigned_local_bundle': True, 'dev_bundle_path': str(bundle)}
        with mock.patch.object(Path, 'read_bytes', side_effect=OSError('denied')):
            payload, source, signed = be._load_firefox_extension_payload(managed, self.logger, 'adblock')
        self.assertIsNone(payload)
        self.assertTrue(source.startswith('dev-bundle-read-error:'))
        self.assertFalse(signed)

    def test_download_success_and_size_limit(self):
        signed_payload = _xpi()
        self.open_url.side_effect = None
        self.open_url.return_value = _FakeResponse(signed_payload)
        managed = {'download_url': 'https://addons.example/x.xpi'}
        self.assertEqual(be._load_firefox_extension_payload(managed, self.logger, 'adblock'), (signed_payload, 'https://addons.example/x.xpi', True))
        self.assertFalse(self.open_url.call_args.kwargs['allow_private_targets'])
        self.open_url.return_value = _FakeResponse(b'x' * 11)
        with mock.patch.object(be, 'MAX_EXTENSION_DOWNLOAD_SIZE', 10):
            payload, source, _ = be._load_firefox_extension_payload(managed, self.logger, 'adblock')
        self.assertIsNone(payload)
        self.assertEqual(source, 'download-too-large:https://addons.example/x.xpi')

    def test_download_failure_without_bundle(self):
        self.open_url.side_effect = OSError('offline')
        payload, source, _ = be._load_firefox_extension_payload({'download_url': 'https://a.example/x'}, self.logger, 'adblock')
        self.assertIsNone(payload)
        self.assertEqual(source, 'offline')

    def test_download_failure_falls_back_to_bundle(self):
        bundle = self.base / 'bundle.xpi'
        bundle.write_bytes(_xpi(signed=False))
        self.open_url.side_effect = OSError('offline')
        managed = {'download_url': 'https://a.example/x', 'bundle_path': str(bundle)}
        payload, source, signed = be._load_firefox_extension_payload(managed, self.logger, 'adblock')
        self.assertEqual((payload, source, signed), (bundle.read_bytes(), f'bundle:{bundle}', False))

    def test_bundle_read_error(self):
        bundle = self.base / 'bundle.xpi'
        bundle.write_bytes(b'x')
        with mock.patch.object(Path, 'read_bytes', side_effect=OSError('denied')):
            payload, source, _ = be._load_firefox_extension_payload({'bundle_path': str(bundle)}, self.logger, 'adblock')
        self.assertIsNone(payload)
        self.assertTrue(source.startswith('bundle-read-error:'))

    def test_missing_sources(self):
        self.assertEqual(be._load_firefox_extension_payload({}, self.logger, 'adblock'), (None, 'missing-extension-source', False))
        self.assertEqual(
            be._load_firefox_extension_payload({'bundle_path': str(self.base / 'nope.xpi')}, self.logger, 'adblock'),
            (None, 'missing-extension-source', False),
        )

    def test_resolution_exception_is_contained(self):
        with mock.patch.object(be, '_load_firefox_extension_payload', side_effect=RuntimeError('boom')):
            result = be._resolve_extension_payload({}, self.logger, 'adblock', {ADDRESS_KEY: 'https://x.example/'})
        self.assertEqual(result, (None, 'extension-payload-resolution-error', False))
        self.logger.exception.assert_called_once()


class UnsignedGateTests(_ProfileMixin, unittest.TestCase):
    def test_requires_flag_managed_profile_and_matching_dev_bundle(self):
        bundle = self.base / 'dev.xpi'
        bundle.write_bytes(b'x')
        managed = {'allow_unsigned_local_bundle': True, 'dev_bundle_path': str(bundle)}
        source = f'bundle:{bundle}'
        self.assertFalse(be._allows_unsigned_local_extension_payload({}, source, self.profile))
        with mock.patch.object(be, '_is_explicitly_managed_profile_dir', return_value=False):
            self.assertFalse(be._allows_unsigned_local_extension_payload(managed, source, self.profile))
        with mock.patch.object(be, '_is_explicitly_managed_profile_dir', return_value=True):
            self.assertFalse(be._allows_unsigned_local_extension_payload(managed, source, ''))
            self.assertTrue(be._allows_unsigned_local_extension_payload(managed, source, self.profile))
            self.assertFalse(be._allows_unsigned_local_extension_payload(managed, 'https://x', self.profile))
            self.assertFalse(be._allows_unsigned_local_extension_payload({**managed, 'dev_bundle_path': ''}, source, self.profile))

    def test_installable_gate(self):
        self.assertTrue(be._extension_payload_is_installable(True, {}, 'x', self.profile, 'adblock', self.logger))
        with mock.patch.object(be, '_allows_unsigned_local_extension_payload', return_value=True):
            self.assertTrue(be._extension_payload_is_installable(False, {}, 'x', self.profile, 'swipe', self.logger))
        self.assertFalse(be._extension_payload_is_installable(False, {}, 'x', self.profile, 'adblock', self.logger))


class StateInvalidationTests(_ProfileMixin, unittest.TestCase):
    def test_state_files_and_startup_cache_are_removed(self):
        for name in ('addonStartup.json.lz4', 'extensions.json'):
            (self.profile / name).write_text('x', encoding='utf-8')
        (self.profile / 'startupCache').mkdir()
        self.assertTrue(be._invalidate_firefox_extension_state(self.profile, self.logger))
        self.assertEqual(list(self.profile.iterdir()), [])
        self.assertFalse(be._invalidate_firefox_extension_state(self.profile, self.logger))

    def test_removal_failures_are_logged(self):
        (self.profile / 'extensions.json').write_text('x', encoding='utf-8')
        (self.profile / 'startupCache').mkdir()
        with mock.patch.object(Path, 'unlink', side_effect=OSError('busy')), \
                mock.patch.object(be.shutil, 'rmtree', side_effect=OSError('busy')):
            self.assertFalse(be._invalidate_firefox_extension_state(self.profile, self.logger))
        self.assertEqual(self.logger.warning.call_count, 2)


class SyncStateMachineTests(_ProfileMixin, unittest.TestCase):
    def _signed_download(self, payload):
        self.configs['adblock']['download_url'] = 'https://addons.example/ublock.xpi'
        self.open_url.side_effect = None
        self.open_url.return_value = _FakeResponse(payload)

    def test_missing_profile(self):
        self.assertEqual(be._sync_firefox_adblock('', True, self.logger)['error'], 'missing-profile')

    def test_fresh_install_from_download_writes_xpi_and_marker(self):
        self._signed_download(_xpi('adblock@test'))
        result = be._sync_firefox_adblock(self.profile, True, self.logger)
        self.assertEqual(result, {'requested': True, 'installed': True, 'changed': True, 'error': None})
        self.assertTrue((self.ext_dir / 'adblock@test.xpi').is_file())
        self.assertEqual((self.ext_dir / '.adblock_marker').read_text(encoding='utf-8'), 'adblock@test')

    def test_copy_named_by_marker_is_kept_without_refresh_source(self):
        self.ext_dir.mkdir()
        (self.ext_dir / '.adblock_marker').write_text('old@test', encoding='utf-8')
        (self.ext_dir / 'old@test.xpi').write_bytes(b'old')
        self._signed_download(_xpi('real@test'))
        result = be._sync_firefox_adblock(self.profile, True, self.logger)
        # The marker pointed at old@test, so that XPI counted as installed and
        # adblock (no bundle, no override) keeps it rather than refreshing.
        self.assertEqual(result, {'requested': True, 'installed': True, 'changed': False, 'error': None})
        self.assertEqual((self.ext_dir / '.adblock_marker').read_text(encoding='utf-8'), 'old@test')
        self.open_url.assert_not_called()

    def test_install_under_manifest_id_removes_other_copies(self):
        self._signed_download(_xpi('real@test'))
        original = be._managed_firefox_extension_paths

        def with_stale(*args, **kwargs):
            managed = original(*args, **kwargs)
            stale = managed['extensions_dir'] / 'stale@test.xpi'
            managed['xpi_paths'] = [*managed['xpi_paths'], stale]
            return managed

        self.ext_dir.mkdir()
        with mock.patch.object(be, '_managed_firefox_extension_paths', side_effect=with_stale):
            result = be._sync_firefox_adblock(self.profile, True, self.logger)
        self.assertTrue(result['installed'])
        self.assertTrue((self.ext_dir / 'real@test.xpi').is_file())
        self.assertEqual((self.ext_dir / '.adblock_marker').read_text(encoding='utf-8'), 'real@test')

    def test_stale_copy_removal_failure_is_logged(self):
        self.ext_dir.mkdir()
        target = self.ext_dir / 'adblock@test.xpi'
        stale = self.ext_dir / 'stale@test.xpi'
        managed = {'extensions_dir': self.ext_dir}
        refreshed = {'xpi_paths': [target, stale], 'legacy_xpi_paths': []}
        with mock.patch.object(be, '_managed_firefox_extension_paths', return_value=refreshed), \
                mock.patch.object(Path, 'unlink', side_effect=OSError('busy')):
            result = be._store_extension_payload(b'data', 'adblock@test', managed, self.profile, 'adblock', False, self.logger)
        self.assertEqual(result, target)
        self.assertEqual(target.read_bytes(), b'data')
        self.logger.warning.assert_called_once()

    def test_disable_removes_xpis_markers_and_state(self):
        self.ext_dir.mkdir()
        (self.ext_dir / 'adblock@test.xpi').write_bytes(b'x')
        (self.ext_dir / '.adblock_marker').write_text('adblock@test', encoding='utf-8')
        (self.profile / 'extensions.json').write_text('{}', encoding='utf-8')
        result = be._sync_firefox_adblock(self.profile, False, self.logger)
        self.assertEqual(result, {'requested': False, 'installed': False, 'changed': True, 'error': None})
        self.assertEqual(list(self.ext_dir.iterdir()), [])
        self.assertFalse((self.profile / 'extensions.json').exists())

    def test_disable_with_nothing_installed_reports_no_change(self):
        self.assertFalse(be._sync_firefox_swipe_extension(self.profile, False, self.logger)['changed'])

    def test_disable_logs_removal_failures(self):
        self.ext_dir.mkdir()
        (self.ext_dir / 'adblock@test.xpi').write_bytes(b'x')
        (self.ext_dir / '.adblock_marker').write_text('adblock@test', encoding='utf-8')
        with mock.patch.object(Path, 'unlink', side_effect=OSError('busy')):
            result = be._sync_firefox_adblock(self.profile, False, self.logger)
        self.assertFalse(result['changed'])
        self.assertEqual(self.logger.warning.call_count, 2)

    def test_missing_addon_id(self):
        self._defaults()
        self.configs['adblock'] = {'download_url': 'https://a.example/x'}
        self.assertEqual(be._sync_firefox_adblock(self.profile, True, self.logger)['error'], 'missing-addon-id')

    def test_missing_source(self):
        self._defaults()
        self.assertEqual(be._sync_firefox_adblock(self.profile, True, self.logger)['error'], 'missing-extension-source')

    def test_resolved_missing_source_is_reported(self):
        with mock.patch.object(be, '_resolve_extension_payload', return_value=(None, 'missing-extension-source', False)):
            self.configs['adblock']['download_url'] = 'https://a.example/x'
            self.assertEqual(be._sync_firefox_adblock(self.profile, True, self.logger)['error'], 'missing-extension-source')

    def test_failed_download_reports_missing_payload(self):
        self.configs['adblock']['download_url'] = 'https://a.example/x'
        self.open_url.side_effect = OSError('offline')
        self.assertEqual(be._sync_firefox_adblock(self.profile, True, self.logger)['error'], 'missing-extension-payload')

    def test_unsigned_download_is_refused(self):
        self._signed_download(_xpi('adblock@test', signed=False))
        result = be._sync_firefox_adblock(self.profile, True, self.logger)
        self.assertEqual(result['error'], 'unsigned-extension-payload')
        self.assertFalse((self.ext_dir / 'adblock@test.xpi').exists())

    def test_write_failure_is_reported_as_error_text(self):
        self._signed_download(_xpi('adblock@test'))
        with mock.patch.object(be, '_store_extension_payload', side_effect=OSError('disk full')):
            result = be._sync_firefox_adblock(self.profile, True, self.logger)
        self.assertEqual(result, {'requested': True, 'installed': False, 'changed': False, 'error': 'disk full'})

    def test_swipe_enable_uses_the_override(self):
        with mock.patch.object(be, '_sync_firefox_signed_extension', return_value='r') as sync:
            self.assertEqual(be._sync_firefox_swipe_extension(self.profile, True, self.logger, {'a': 1}), 'r')
        sync.assert_called_once_with(self.profile, True, self.logger, 'swipe', local_development_override=True, options_dict={'a': 1})


class ExistingExtensionTests(_ProfileMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # Without this, the empty download_url would fall back to the real
        # AMO default and the sync would try to download.
        self._defaults()
        self.ext_dir.mkdir()
        self.bundle = self.base / 'bundle.xpi'
        self.configs['swipe'] = {'id': 'swipe@test', 'marker_file': '.swipe_marker', 'bundle_path': str(self.bundle), 'allow_unsigned_local_bundle': False}
        self.installed = self.ext_dir / 'swipe@test.xpi'

    def test_identical_payload_is_kept(self):
        payload = _xpi('swipe@test')
        self.bundle.write_bytes(payload)
        self.installed.write_bytes(payload)
        result = be._sync_firefox_swipe_extension(self.profile, True, self.logger)
        self.assertEqual(result, {'requested': True, 'installed': True, 'changed': False, 'error': None})
        self.assertEqual((self.ext_dir / '.swipe_marker').read_text(encoding='utf-8'), 'swipe@test')

    def test_different_payload_is_refreshed_without_resolving_twice(self):
        payload = _xpi('swipe@test')
        self.bundle.write_bytes(payload)
        self.installed.write_bytes(b'old')
        with mock.patch.object(be, '_resolve_extension_payload', wraps=be._resolve_extension_payload) as resolve:
            result = be._sync_firefox_swipe_extension(self.profile, True, self.logger)
        self.assertTrue(result['changed'])
        self.assertEqual(self.installed.read_bytes(), payload)
        self.assertEqual(resolve.call_count, 1)

    def test_unresolvable_payload_keeps_what_is_installed(self):
        self.installed.write_bytes(b'old')
        result = be._sync_firefox_swipe_extension(self.profile, True, self.logger)
        self.assertEqual(result, {'requested': True, 'installed': True, 'changed': False, 'error': None})
        self.assertEqual(self.installed.read_bytes(), b'old')

    def test_unreadable_installed_payload_is_replaced(self):
        payload = _xpi('swipe@test')
        self.bundle.write_bytes(payload)
        self.installed.write_bytes(b'old')
        original = Path.read_bytes

        def fail_installed(path):
            if path == self.installed:
                raise OSError('denied')
            return original(path)

        with mock.patch.object(Path, 'read_bytes', fail_installed):
            result = be._sync_firefox_swipe_extension(self.profile, True, self.logger)
        self.assertTrue(result['changed'])
        self.assertEqual(self.installed.read_bytes(), payload)

    def test_marker_write_failure_is_logged(self):
        payload = _xpi('swipe@test')
        self.bundle.write_bytes(payload)
        self.installed.write_bytes(payload)
        with mock.patch.object(Path, 'write_text', side_effect=OSError('ro')):
            result = be._sync_firefox_swipe_extension(self.profile, True, self.logger)
        self.assertTrue(result['installed'])
        self.assertTrue(self.logger.warning.called)


if __name__ == '__main__':
    unittest.main()

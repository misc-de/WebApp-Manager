import io
import json
import logging
import sys
import tempfile
import types
import urllib.error
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from browser_option_logic import (
    decode_browser_state,
    encode_browser_state,
    normalize_option_rows,
    supported_browser_option_keys,
)
from detail_page_option_state import (
    coerce_option_updates,
    configured_mode_values_for_engine,
    current_mode_value,
    normalize_mode_value,
    restored_browser_state,
    store_boolean_option_value,
    sync_browser_state_key,
    ui_boolean_option_active,
)
from input_validation import (
    load_import_payloads_from_path,
    normalize_wapp_payload,
    payload_contains_inline_javascript,
)
from webapp_constants import (
    ICON_PATH_KEY,
    INLINE_CUSTOM_JS_KEY,
    ONLY_HTTPS_KEY,
    OPTION_DISABLE_AI_KEY,
    OPTION_NOTIFICATIONS_KEY,
    OPTION_OPEN_LINKS_IN_TABS_KEY,
    OPTION_PREVENT_MULTIPLE_STARTS_KEY,
    OPTION_PRESERVE_SESSION_KEY,
    OPTION_SAFE_GRAPHICS_KEY,
    PROFILE_PATH_KEY,
)
from ui_flow_state import detail_neutral_focus_slot, main_neutral_focus_candidates, next_search_toggle_state
from wapp_transfer import build_wapp_export_bundle_payload, build_wapp_export_payload


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

from browser_profiles import (
    _resolve_bundled_extension_path,
    _scope_swipe_extension_payload,
    _write_firefox_user_js,
    _write_managed_profile_marker,
    _sync_firefox_signed_extension,
    _sync_firefox_swipe_extension,
    delete_managed_browser_profiles,
    ensure_browser_profile,
    get_firefox_extension_config,
    read_profile_settings,
    swipe_extension_mode_value,
)


class BrowserOptionLogicTests(unittest.TestCase):
    def test_open_links_option_is_firefox_only_visible_option(self):
        firefox_supported = supported_browser_option_keys('firefox', visible_only=True)
        chrome_supported = supported_browser_option_keys('chrome', visible_only=True)

        self.assertIn(OPTION_OPEN_LINKS_IN_TABS_KEY, firefox_supported)
        self.assertNotIn(OPTION_OPEN_LINKS_IN_TABS_KEY, chrome_supported)

    def test_normalize_option_rows_prefers_canonical_option_key(self):
        rows = [
            (1, 7, 'Allow Notifications', '0'),
            (2, 7, OPTION_NOTIFICATIONS_KEY, '1'),
            (3, 7, 'Allow Notifications', '0'),
        ]

        normalized = normalize_option_rows(rows)

        self.assertEqual(normalized[OPTION_NOTIFICATIONS_KEY], '1')

    def test_browser_state_round_trip_keeps_firefox_specific_option(self):
        options = {
            OPTION_OPEN_LINKS_IN_TABS_KEY: '1',
            OPTION_PRESERVE_SESSION_KEY: '1',
            OPTION_PREVENT_MULTIPLE_STARTS_KEY: '1',
            ONLY_HTTPS_KEY: '1',
            'Kiosk': '1',
        }

        encoded = encode_browser_state(options, 'firefox')
        decoded = decode_browser_state(encoded, 'firefox')

        self.assertEqual(decoded[OPTION_OPEN_LINKS_IN_TABS_KEY], '1')
        self.assertEqual(decoded[OPTION_PRESERVE_SESSION_KEY], '1')
        self.assertEqual(decoded[OPTION_PREVENT_MULTIPLE_STARTS_KEY], '1')
        self.assertEqual(decoded[ONLY_HTTPS_KEY], '1')
        self.assertNotIn('Kiosk', decoded)


class DetailPageOptionStateTests(unittest.TestCase):
    def test_disable_ai_boolean_mapping_is_inverted_for_ui_and_storage(self):
        self.assertTrue(ui_boolean_option_active(OPTION_DISABLE_AI_KEY, '0'))
        self.assertFalse(ui_boolean_option_active(OPTION_DISABLE_AI_KEY, '1'))
        self.assertEqual(store_boolean_option_value(OPTION_DISABLE_AI_KEY, True), '0')
        self.assertEqual(store_boolean_option_value(OPTION_DISABLE_AI_KEY, False), '1')

    def test_current_mode_value_and_normalize_mode_value_follow_semantic_aliases(self):
        self.assertEqual(current_mode_value({'Kiosk': '1'}), 'kiosk')
        self.assertEqual(current_mode_value({'App Mode': '1', 'Frameless': '1'}), 'seamless')
        self.assertEqual(current_mode_value({'App Mode': '1'}), 'app')
        self.assertEqual(current_mode_value({}), 'standard')
        self.assertEqual(normalize_mode_value('fullscreen'), 'kiosk')
        self.assertEqual(normalize_mode_value('frameless'), 'seamless')
        self.assertEqual(normalize_mode_value('normal'), 'standard')
        self.assertEqual(normalize_mode_value('unsupported'), '')

    def test_configured_mode_values_for_engine_uses_specific_then_default_modes(self):
        config = {
            'browser_modes': {
                'default': ['standard', 'app'],
                'firefox': ['standard', 'kiosk', 'seamless'],
            }
        }

        firefox_values = configured_mode_values_for_engine(config, {'id': 1, 'name': 'Firefox', 'command': 'firefox'})
        chrome_values = configured_mode_values_for_engine(config, {'id': 2, 'name': 'Chrome', 'command': 'chrome'})

        self.assertEqual(firefox_values, ['standard', 'kiosk', 'seamless'])
        self.assertEqual(chrome_values, ['standard', 'app'])

    def test_coerce_option_updates_enforces_https_for_privacy_on_supported_families(self):
        firefox_updates = coerce_option_updates('firefox', {'Set Privacy': '1'})
        generic_updates = coerce_option_updates('generic', {'Set Privacy': '1'})

        self.assertEqual(firefox_updates['Set Privacy'], '1')
        self.assertEqual(firefox_updates[ONLY_HTTPS_KEY], '1')
        self.assertEqual(generic_updates['Set Privacy'], '1')
        self.assertNotIn(ONLY_HTTPS_KEY, generic_updates)

    def test_sync_browser_state_key_and_restored_browser_state_use_family_snapshots(self):
        options_cache = {
            OPTION_PRESERVE_SESSION_KEY: '0',
            OPTION_OPEN_LINKS_IN_TABS_KEY: '0',
            sync_browser_state_key('firefox'): '{"Open Links In Tabs":"1","Previous Session":"1"}',
        }

        restored = restored_browser_state(options_cache, 'firefox')

        self.assertEqual(sync_browser_state_key('firefox'), '__BrowserState.firefox')
        self.assertEqual(restored[OPTION_OPEN_LINKS_IN_TABS_KEY], '1')
        self.assertEqual(restored[OPTION_PRESERVE_SESSION_KEY], '1')


class UiFlowStateTests(unittest.TestCase):
    def test_main_neutral_focus_candidates_follow_visible_ui_context(self):
        self.assertEqual(
            main_neutral_focus_candidates(
                visible_page='overview_page',
                search_visible=True,
                adaptive_split_enabled=True,
                adaptive_real_detail_visible=False,
            ),
            ('search_button', 'home_button', 'add_button'),
        )
        self.assertEqual(
            main_neutral_focus_candidates(
                visible_page='settings_page',
                search_visible=False,
                adaptive_split_enabled=True,
                adaptive_real_detail_visible=False,
            ),
            ('back_button', 'home_button', 'search_button', 'add_button'),
        )
        self.assertEqual(
            main_neutral_focus_candidates(
                visible_page='overview_page',
                search_visible=False,
                adaptive_split_enabled=True,
                adaptive_real_detail_visible=True,
            ),
            ('back_button', 'home_button', 'search_button', 'add_button'),
        )

    def test_detail_neutral_focus_slot_follows_current_subpage(self):
        self.assertEqual(detail_neutral_focus_slot('main'), ('icon_button',))
        self.assertEqual(detail_neutral_focus_slot('options'), ('options_tab_button', 'icon_button'))
        self.assertEqual(detail_neutral_focus_slot('icon'), ('first_icon_page_button', 'icon_button'))
        self.assertEqual(detail_neutral_focus_slot('css_assets'), ('css_tab_button', 'css_add_button', 'css_dropdown', 'icon_button'))
        self.assertEqual(detail_neutral_focus_slot('javascript_assets'), ('javascript_tab_button', 'javascript_add_button', 'javascript_dropdown', 'icon_button'))
        self.assertEqual(detail_neutral_focus_slot('unknown'), ('icon_button',))

    def test_next_search_toggle_state_describes_open_and_close_flow(self):
        opening = next_search_toggle_state(current_visible=False, current_text='')
        closing = next_search_toggle_state(current_visible=True, current_text='query')

        self.assertTrue(opening['search_visible'])
        self.assertTrue(opening['show_back_header'])
        self.assertTrue(opening['autofocus_search_entry'])
        self.assertFalse(opening['clear_entry_text'])

        self.assertFalse(closing['search_visible'])
        self.assertFalse(closing['show_back_header'])
        self.assertTrue(closing['clear_entry_text'])
        self.assertTrue(closing['reset_search_text'])
        self.assertTrue(closing['restore_header_actions'])


class FirefoxProfileOptionTests(unittest.TestCase):
    class _FakeUrlopenResponse:
        def __init__(self, payload: bytes):
            self.payload = payload

        def read(self):
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def _write_test_xpi(self, path: Path, addon_id: str, version: str = '0.1.0', signed: bool = False):
        manifest = {
            'manifest_version': 2,
            'name': 'Test Swipe',
            'version': version,
            'permissions': ['tabs', 'http://*/*', 'https://*/*'],
            'background': {'scripts': ['background.js']},
            'content_scripts': [{
                'matches': ['http://*/*', 'https://*/*'],
                'js': ['swipe.js'],
                'run_at': 'document_idle',
            }],
            'browser_specific_settings': {
                'gecko': {
                    'id': addon_id,
                }
            },
        }
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json', json.dumps(manifest))
            archive.writestr('background.js', 'browser.runtime.onMessage.addListener(() => {});')
            archive.writestr('swipe.js', 'console.log("swipe");')
            if signed:
                archive.writestr('META-INF/mozilla.rsa', 'signed')

    def test_read_profile_settings_round_trip_for_open_links_and_disable_ai(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir)
            _write_firefox_user_js(
                profile_dir,
                clear_cache=False,
                clear_cookies=False,
                previous_session=False,
                notifications_enabled=True,
                open_links_in_tabs=True,
                disable_ai=True,
            )

            state = read_profile_settings(str(profile_dir), 'firefox')

        self.assertEqual(state[OPTION_OPEN_LINKS_IN_TABS_KEY], '1')
        self.assertEqual(state[OPTION_DISABLE_AI_KEY], '1')
        self.assertEqual(state[OPTION_NOTIFICATIONS_KEY], '1')

    def test_read_profile_settings_detects_disable_ai_from_tab_group_pref(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir)
            profile_dir.mkdir(parents=True, exist_ok=True)
            (profile_dir / 'user.js').write_text(
                '\n'.join(
                    [
                        '// WEBAPP MANAGED START',
                        'user_pref("browser.tabs.groups.smart.userEnabled", false);',
                        'user_pref("browser.link.open_newwindow", 2);',
                        '// WEBAPP MANAGED END',
                        '',
                    ]
                ),
                encoding='utf-8',
            )

            state = read_profile_settings(str(profile_dir), 'firefox')

        self.assertEqual(state[OPTION_DISABLE_AI_KEY], '1')
        self.assertEqual(state[OPTION_OPEN_LINKS_IN_TABS_KEY], '0')

    def test_unsigned_runtime_js_pref_is_only_relaxed_for_managed_firefox_profiles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            firefox_root = tmp_root / 'firefox-root'
            chromium_root = tmp_root / 'chromium-root'
            unmanaged_profile = tmp_root / 'unmanaged'
            unmanaged_profile.mkdir(parents=True, exist_ok=True)

            with mock.patch('browser_profiles.FIREFOX_ROOT', firefox_root), mock.patch('browser_profiles.CHROMIUM_PROFILE_ROOT', chromium_root):
                _write_firefox_user_js(
                    unmanaged_profile,
                    clear_cache=False,
                    clear_cookies=False,
                    previous_session=False,
                    custom_js_enabled=True,
                )
                unmanaged_user_js = (unmanaged_profile / 'user.js').read_text(encoding='utf-8')

                managed_profile = firefox_root / 'webapp_managed'
                managed_profile.mkdir(parents=True, exist_ok=True)
                _write_managed_profile_marker(managed_profile, 'firefox')
                _write_firefox_user_js(
                    managed_profile,
                    clear_cache=False,
                    clear_cookies=False,
                    previous_session=False,
                    custom_js_enabled=True,
                )
                managed_user_js = (managed_profile / 'user.js').read_text(encoding='utf-8')

        self.assertIn('user_pref("xpinstall.signatures.required", true);', unmanaged_user_js)
        self.assertIn('user_pref("xpinstall.signatures.required", false);', managed_user_js)

    def test_furios_firefox_profiles_disable_problematic_gpu_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir)

            with mock.patch('browser_profiles.is_furios_distribution', return_value=True):
                _write_firefox_user_js(
                    profile_dir,
                    clear_cache=False,
                    clear_cookies=False,
                    previous_session=False,
                )

            user_js = (profile_dir / 'user.js').read_text(encoding='utf-8')

        self.assertIn('user_pref("gfx.webrender.all", false);', user_js)
        self.assertIn('user_pref("layers.acceleration.disabled", true);', user_js)
        self.assertIn('user_pref("gfx.canvas.accelerated", false);', user_js)
        self.assertIn('user_pref("media.ffmpeg.vaapi.enabled", false);', user_js)

    def test_safe_graphics_adds_extra_firefox_fallback_prefs_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir)

            _write_firefox_user_js(
                profile_dir,
                clear_cache=False,
                clear_cookies=False,
                previous_session=False,
                safe_graphics=True,
            )

            user_js = (profile_dir / 'user.js').read_text(encoding='utf-8')
            state = read_profile_settings(str(profile_dir), 'firefox')

        self.assertIn('user_pref("webgl.disabled", true);', user_js)
        self.assertIn('user_pref("webgl.enable-webgl2", false);', user_js)
        self.assertIn('user_pref("media.hardware-video-decoding.enabled", false);', user_js)
        self.assertEqual(state[OPTION_SAFE_GRAPHICS_KEY], '1')

    def test_ensure_browser_profile_copies_external_firefox_profile_into_managed_dir(self):
        logger = _build_test_logger('profile-copy')
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            firefox_root = tmp_root / 'firefox-root'
            chromium_root = tmp_root / 'chromium-root'
            external_profile = firefox_root / 'abcd1234.default-release'
            external_profile.mkdir(parents=True, exist_ok=True)
            (external_profile / 'prefs.js').write_text('// existing profile\n', encoding='utf-8')
            (external_profile / 'cookies.sqlite').write_text('db', encoding='utf-8')

            with mock.patch('browser_profiles.FIREFOX_ROOT', firefox_root), mock.patch('browser_profiles.CHROMIUM_PROFILE_ROOT', chromium_root):
                profile_info = ensure_browser_profile(
                    'Copied App',
                    'firefox',
                    logger,
                    stored_profile_path=str(external_profile),
                )
                self.assertIsNotNone(profile_info)
                self.assertNotEqual(profile_info['profile_path'], str(external_profile))
                self.assertTrue(Path(profile_info['profile_path']).name.startswith('webapp_'))
                self.assertTrue((Path(profile_info['profile_path']) / 'prefs.js').exists())
                self.assertTrue((Path(profile_info['profile_path']) / '.webapp-manager-profile.json').exists())

    def test_delete_managed_browser_profiles_skips_regular_firefox_profiles(self):
        logger = _build_test_logger('profile-delete')
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            firefox_root = tmp_root / 'firefox-root'
            chromium_root = tmp_root / 'chromium-root'
            regular_profile = firefox_root / 'abcd1234.default-release'
            regular_profile.mkdir(parents=True, exist_ok=True)
            (regular_profile / 'prefs.js').write_text('// keep me\n', encoding='utf-8')

            managed_profile = firefox_root / 'webapp_123456'
            managed_profile.mkdir(parents=True, exist_ok=True)
            (managed_profile / 'prefs.js').write_text('// delete me\n', encoding='utf-8')
            _write_managed_profile_marker(managed_profile, 'firefox')

            with mock.patch('browser_profiles.FIREFOX_ROOT', firefox_root), mock.patch('browser_profiles.CHROMIUM_PROFILE_ROOT', chromium_root):
                delete_managed_browser_profiles('Regular App', logger, stored_profile_path=str(regular_profile))
                delete_managed_browser_profiles('Managed App', logger, stored_profile_path=str(managed_profile))

            self.assertTrue(regular_profile.exists())
            self.assertFalse(managed_profile.exists())

    def test_downloaded_signed_swipe_bundle_can_be_installed(self):
        logger = _build_test_logger('downloaded-signed-swipe')
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            firefox_root = tmp_root / 'firefox-root'
            chromium_root = tmp_root / 'chromium-root'
            profile_dir = firefox_root / 'webapp_testprofile'
            profile_dir.mkdir(parents=True, exist_ok=True)
            download_bundle = tmp_root / 'downloaded-swipe.xpi'
            self._write_test_xpi(download_bundle, 'swipe-gestures@de.cais', signed=True)
            payload = download_bundle.read_bytes()

            config = {
                'id': 'swipe-gestures@de.cais',
                'marker_file': '.webapp_secure_swipe_extension_id',
                'bundle_path': '',
                'download_url': 'https://example.invalid/swipe.xpi',
            }
            with mock.patch('browser_profiles.FIREFOX_ROOT', firefox_root), \
                mock.patch('browser_profiles.CHROMIUM_PROFILE_ROOT', chromium_root), \
                mock.patch('browser_profiles.get_firefox_extension_config', return_value=config), \
                mock.patch('browser_profiles.urllib.request.urlopen', return_value=self._FakeUrlopenResponse(payload)):
                result = _sync_firefox_swipe_extension(
                    profile_dir,
                    True,
                    logger,
                )

            self.assertTrue(result['installed'])
            self.assertIsNone(result['error'])
            self.assertTrue((profile_dir / 'extensions' / 'swipe-gestures@de.cais.xpi').exists())

    def test_downloaded_unsigned_swipe_bundle_is_rejected(self):
        logger = _build_test_logger('downloaded-unsigned-swipe')
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            firefox_root = tmp_root / 'firefox-root'
            chromium_root = tmp_root / 'chromium-root'
            profile_dir = firefox_root / 'webapp_testprofile'
            profile_dir.mkdir(parents=True, exist_ok=True)
            download_bundle = tmp_root / 'downloaded-swipe.xpi'
            self._write_test_xpi(download_bundle, 'swipe-gestures@de.cais', signed=False)
            payload = download_bundle.read_bytes()

            config = {
                'id': 'swipe-gestures@de.cais',
                'marker_file': '.webapp_secure_swipe_extension_id',
                'bundle_path': '',
                'download_url': 'https://example.invalid/swipe.xpi',
            }
            with mock.patch('browser_profiles.FIREFOX_ROOT', firefox_root), \
                mock.patch('browser_profiles.CHROMIUM_PROFILE_ROOT', chromium_root), \
                mock.patch('browser_profiles.get_firefox_extension_config', return_value=config), \
                mock.patch('browser_profiles.urllib.request.urlopen', return_value=self._FakeUrlopenResponse(payload)):
                result = _sync_firefox_swipe_extension(
                    profile_dir,
                    True,
                    logger,
                )

            self.assertFalse(result['installed'])
            self.assertEqual(result['error'], 'unsigned-extension-payload')

    def test_secure_swipe_install_replaces_legacy_swipe_bundle(self):
        logger = _build_test_logger('secure-swipe-migration')
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            firefox_root = tmp_root / 'firefox-root'
            chromium_root = tmp_root / 'chromium-root'
            profile_dir = firefox_root / 'webapp_testprofile'
            extensions_dir = profile_dir / 'extensions'
            extensions_dir.mkdir(parents=True, exist_ok=True)
            _write_managed_profile_marker(profile_dir, 'firefox')

            legacy_id = '{6f3ab763-a4c2-4183-b596-984bf5b7ac31}'
            (extensions_dir / f'{legacy_id}.xpi').write_text('legacy', encoding='utf-8')
            (extensions_dir / '.webapp_simple_swipe_navigator_extension_id').write_text(legacy_id, encoding='utf-8')

            download_bundle = tmp_root / 'downloaded-swipe.xpi'
            self._write_test_xpi(download_bundle, 'swipe-gestures@de.cais', signed=True)
            payload = download_bundle.read_bytes()

            config = {
                'id': 'swipe-gestures@de.cais',
                'marker_file': '.webapp_secure_swipe_extension_id',
                'bundle_path': '',
                'download_url': 'https://example.invalid/swipe.xpi',
            }
            with mock.patch('browser_profiles.FIREFOX_ROOT', firefox_root), \
                mock.patch('browser_profiles.CHROMIUM_PROFILE_ROOT', chromium_root), \
                mock.patch('browser_profiles.get_firefox_extension_config', return_value=config), \
                mock.patch('browser_profiles.urllib.request.urlopen', return_value=self._FakeUrlopenResponse(payload)):
                result = _sync_firefox_swipe_extension(
                    profile_dir,
                    True,
                    logger,
                )

            self.assertTrue(result['installed'])
            self.assertFalse((extensions_dir / f'{legacy_id}.xpi').exists())
            self.assertFalse((extensions_dir / '.webapp_simple_swipe_navigator_extension_id').exists())
            self.assertTrue((extensions_dir / 'swipe-gestures@de.cais.xpi').exists())

    def test_swipe_falls_back_to_local_bundle_when_download_fails(self):
        logger = _build_test_logger('secure-swipe-download-fallback')
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            firefox_root = tmp_root / 'firefox-root'
            chromium_root = tmp_root / 'chromium-root'
            profile_dir = firefox_root / 'webapp_testprofile'
            profile_dir.mkdir(parents=True, exist_ok=True)
            _write_managed_profile_marker(profile_dir, 'firefox')
            local_bundle = tmp_root / 'swipe-gestures.xpi'
            self._write_test_xpi(local_bundle, 'swipe-gestures@de.cais', signed=True)

            config = {
                'id': 'swipe-gestures@de.cais',
                'marker_file': '.webapp_secure_swipe_extension_id',
                'bundle_path': str(local_bundle),
                'download_url': 'https://example.invalid/swipe.xpi',
                'dev_bundle_path': '',
                'allow_unsigned_local_bundle': False,
            }

            with mock.patch('browser_profiles.FIREFOX_ROOT', firefox_root), \
                mock.patch('browser_profiles.CHROMIUM_PROFILE_ROOT', chromium_root), \
                mock.patch('browser_profiles.get_firefox_extension_config', return_value=config), \
                mock.patch('browser_profiles.urllib.request.urlopen', side_effect=urllib.error.URLError('offline')):
                result = _sync_firefox_swipe_extension(
                    profile_dir,
                    True,
                    logger,
                )
            self.assertTrue(result['installed'])
            self.assertIsNone(result['error'])
            self.assertTrue((profile_dir / 'extensions' / 'swipe-gestures@de.cais.xpi').exists())

    def test_legacy_plural_extensions_bundle_path_resolves_to_local_extension_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            project_root = tmp_root / 'project'
            project_root.mkdir(parents=True, exist_ok=True)
            module_path = project_root / 'browser_profiles.py'
            module_path.write_text('# test module marker\n', encoding='utf-8')

            local_bundle = project_root / 'extension' / 'swipe-gestures.xpi'
            local_bundle.parent.mkdir(parents=True, exist_ok=True)
            self._write_test_xpi(local_bundle, 'swipe-gestures@de.cais', signed=True)

            with mock.patch('browser_profiles.__file__', str(module_path)):
                resolved = _resolve_bundled_extension_path('extensions/swipe-gestures.xpi')

            self.assertEqual(resolved, local_bundle.resolve())

    def test_swipe_extension_mode_value_is_pinned_to_production(self):
        self.assertEqual(swipe_extension_mode_value({}), 'production')
        self.assertEqual(swipe_extension_mode_value({'Swipe Mode': 'development'}), 'production')
        self.assertEqual(swipe_extension_mode_value({'Swipe Mode': 'production'}), 'production')

    def test_scope_swipe_extension_payload_limits_matches_to_webapp_domain(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bundle_path = Path(tmpdir) / 'swipe-gestures.xpi'
            self._write_test_xpi(bundle_path, 'swipe-gestures@de.cais', signed=True)

            scoped_payload = _scope_swipe_extension_payload(
                bundle_path.read_bytes(),
                'https://example.com/app',
            )

            with zipfile.ZipFile(io.BytesIO(scoped_payload)) as archive:
                manifest = json.loads(archive.read('manifest.json').decode('utf-8'))
                names = archive.namelist()

        self.assertEqual(manifest['name'], 'Swipe Gesten (Eigenes Addon)')
        self.assertEqual(manifest['host_permissions'], ['https://example.com/*'])
        self.assertEqual(manifest['content_scripts'][0]['matches'], ['https://example.com/*'])
        self.assertFalse(any(name.upper().startswith('META-INF/') for name in names))

    def test_swipe_installs_local_scoped_bundle_for_managed_profile(self):
        logger = _build_test_logger('secure-swipe-local-scoped')
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            firefox_root = tmp_root / 'firefox-root'
            chromium_root = tmp_root / 'chromium-root'
            profile_dir = firefox_root / 'webapp_testprofile'
            profile_dir.mkdir(parents=True, exist_ok=True)
            _write_managed_profile_marker(profile_dir, 'firefox')
            local_bundle = tmp_root / 'swipe-gestures.xpi'
            self._write_test_xpi(local_bundle, 'swipe-gestures@de.cais', signed=True)

            config = {
                'id': 'swipe-gestures@de.cais',
                'marker_file': '.webapp_secure_swipe_extension_id',
                'bundle_path': str(local_bundle),
                'dev_bundle_path': str(local_bundle),
                'download_url': 'https://example.invalid/swipe.xpi',
                'allow_unsigned_local_bundle': True,
            }

            with mock.patch('browser_profiles.FIREFOX_ROOT', firefox_root), \
                mock.patch('browser_profiles.CHROMIUM_PROFILE_ROOT', chromium_root), \
                mock.patch('browser_profiles.get_firefox_extension_config', return_value=config):
                result = _sync_firefox_swipe_extension(
                    profile_dir,
                    True,
                    logger,
                    options_dict={'Address': 'https://example.com/app'},
                )

            self.assertTrue(result['installed'])
            installed_xpi = profile_dir / 'extensions' / 'swipe-gestures@de.cais.xpi'
            self.assertTrue(installed_xpi.exists())
            with zipfile.ZipFile(installed_xpi) as archive:
                manifest = json.loads(archive.read('manifest.json').decode('utf-8'))
                names = archive.namelist()
            self.assertEqual(manifest['host_permissions'], ['https://example.com/*'])
            self.assertEqual(manifest['content_scripts'][0]['matches'], ['https://example.com/*'])
            self.assertFalse(any(name.upper().startswith('META-INF/') for name in names))


class WappImportValidationTests(unittest.TestCase):
    def test_normalize_wapp_payload_strips_non_portable_options_and_normalizes_values(self):
        payload = {
            'title': '  My App\n',
            'description': ' demo\x00text ',
            'active': 1,
            'options': {
                OPTION_OPEN_LINKS_IN_TABS_KEY: True,
                OPTION_PRESERVE_SESSION_KEY: 0,
                ICON_PATH_KEY: '/tmp/icon.png',
                PROFILE_PATH_KEY: '/tmp/profile',
                'Custom Number': 42,
                'Nullable': None,
            },
        }

        normalized = normalize_wapp_payload(payload)

        self.assertEqual(normalized['title'], 'My App')
        self.assertEqual(normalized['description'], 'demotext')
        self.assertTrue(normalized['active'])
        self.assertEqual(normalized['options'][OPTION_OPEN_LINKS_IN_TABS_KEY], '1')
        self.assertEqual(normalized['options'][OPTION_PRESERVE_SESSION_KEY], '0')
        self.assertEqual(normalized['options']['Custom Number'], '42')
        self.assertEqual(normalized['options']['Nullable'], '')
        self.assertNotIn(ICON_PATH_KEY, normalized['options'])
        self.assertNotIn(PROFILE_PATH_KEY, normalized['options'])

    def test_load_import_payloads_from_path_reads_bundle_entries(self):
        bundle_payload = {
            'format': 'webapp-export-bundle-v1',
            'entries': [
                {
                    'title': 'One',
                    'options': {
                        OPTION_OPEN_LINKS_IN_TABS_KEY: '1',
                    },
                },
                {
                    'title': 'Two',
                    'options': {
                        OPTION_PRESERVE_SESSION_KEY: True,
                    },
                },
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            bundle_path = Path(tmpdir) / 'bundle.wapp'
            bundle_path.write_text(json.dumps(bundle_payload), encoding='utf-8')

            items = load_import_payloads_from_path(bundle_path)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['title'], 'One')
        self.assertEqual(items[0]['options'][OPTION_OPEN_LINKS_IN_TABS_KEY], '1')
        self.assertEqual(items[1]['title'], 'Two')
        self.assertEqual(items[1]['options'][OPTION_PRESERVE_SESSION_KEY], '1')

    def test_payload_contains_inline_javascript_detects_normalized_inline_code(self):
        payload = {
            'title': 'Unsafe demo',
            'options': {
                INLINE_CUSTOM_JS_KEY: '  console.log("hi")  ',
            },
        }

        self.assertTrue(payload_contains_inline_javascript(payload))
        self.assertFalse(payload_contains_inline_javascript({'title': 'Safe demo', 'options': {}}))


class WappExportTests(unittest.TestCase):
    def test_build_wapp_export_payload_removes_transient_options_and_embeds_icon(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            icon_path = Path(tmpdir) / 'icon.png'
            icon_path.write_bytes(b'png-bytes')

            payload = build_wapp_export_payload(
                title='Export Me',
                description='Demo export',
                active=True,
                options_dict={
                    OPTION_OPEN_LINKS_IN_TABS_KEY: '1',
                    ICON_PATH_KEY: str(icon_path),
                    PROFILE_PATH_KEY: '/tmp/profile',
                },
            )

        self.assertEqual(payload['title'], 'Export Me')
        self.assertEqual(payload['description'], 'Demo export')
        self.assertTrue(payload['active'])
        self.assertEqual(payload['options'][OPTION_OPEN_LINKS_IN_TABS_KEY], '1')
        self.assertNotIn(ICON_PATH_KEY, payload['options'])
        self.assertNotIn(PROFILE_PATH_KEY, payload['options'])
        self.assertEqual(payload['icon']['filename'], 'icon.png')
        self.assertEqual(payload['icon']['mime'], 'image/png')
        self.assertEqual(payload['icon']['data_base64'], 'cG5nLWJ5dGVz')

    def test_build_wapp_export_bundle_payload_wraps_entries_with_metadata(self):
        entry_payloads = [
            {'title': 'One'},
            {'title': 'Two'},
        ]

        bundle = build_wapp_export_bundle_payload(entry_payloads, created_at='2026-03-28T12:00:00Z')

        self.assertEqual(bundle['format'], 'webapp-export-bundle-v1')
        self.assertEqual(bundle['version'], 1)
        self.assertEqual(bundle['created_at'], '2026-03-28T12:00:00Z')
        self.assertEqual(bundle['entries'], entry_payloads)

    def test_single_wapp_export_import_round_trip_preserves_portable_options(self):
        export_payload = build_wapp_export_payload(
            title='Round Trip App',
            description='Portable options survive',
            active=False,
            options_dict={
                OPTION_OPEN_LINKS_IN_TABS_KEY: '1',
                OPTION_PRESERVE_SESSION_KEY: '1',
                ONLY_HTTPS_KEY: '1',
                ICON_PATH_KEY: '/tmp/icon.png',
                PROFILE_PATH_KEY: '/tmp/profile',
            },
        )

        imported_payload = normalize_wapp_payload(export_payload)

        self.assertEqual(imported_payload['title'], 'Round Trip App')
        self.assertEqual(imported_payload['description'], 'Portable options survive')
        self.assertFalse(imported_payload['active'])
        self.assertEqual(imported_payload['options'][OPTION_OPEN_LINKS_IN_TABS_KEY], '1')
        self.assertEqual(imported_payload['options'][OPTION_PRESERVE_SESSION_KEY], '1')
        self.assertEqual(imported_payload['options'][ONLY_HTTPS_KEY], '1')
        self.assertNotIn(ICON_PATH_KEY, imported_payload['options'])
        self.assertNotIn(PROFILE_PATH_KEY, imported_payload['options'])

    def test_bundle_export_import_round_trip_keeps_entry_payloads(self):
        first_entry = build_wapp_export_payload(
            title='One',
            description='First entry',
            active=True,
            options_dict={
                OPTION_OPEN_LINKS_IN_TABS_KEY: '1',
            },
        )
        second_entry = build_wapp_export_payload(
            title='Two',
            description='Second entry',
            active=False,
            options_dict={
                OPTION_PRESERVE_SESSION_KEY: '1',
                ONLY_HTTPS_KEY: '1',
            },
        )
        bundle = build_wapp_export_bundle_payload(
            [first_entry, second_entry],
            created_at='2026-03-28T12:00:00Z',
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            bundle_path = Path(tmpdir) / 'roundtrip-bundle.wapp'
            bundle_path.write_text(json.dumps(bundle), encoding='utf-8')

            items = load_import_payloads_from_path(bundle_path)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['title'], 'One')
        self.assertEqual(items[0]['options'][OPTION_OPEN_LINKS_IN_TABS_KEY], '1')
        self.assertEqual(items[1]['title'], 'Two')
        self.assertEqual(items[1]['description'], 'Second entry')
        self.assertFalse(items[1]['active'])
        self.assertEqual(items[1]['options'][OPTION_PRESERVE_SESSION_KEY], '1')
        self.assertEqual(items[1]['options'][ONLY_HTTPS_KEY], '1')


if __name__ == '__main__':
    unittest.main()

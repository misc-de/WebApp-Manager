import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path

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
    OPTION_PRESERVE_SESSION_KEY,
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

from browser_profiles import _write_firefox_user_js, read_profile_settings


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
            ONLY_HTTPS_KEY: '1',
            'Kiosk': '1',
        }

        encoded = encode_browser_state(options, 'firefox')
        decoded = decode_browser_state(encoded, 'firefox')

        self.assertEqual(decoded[OPTION_OPEN_LINKS_IN_TABS_KEY], '1')
        self.assertEqual(decoded[OPTION_PRESERVE_SESSION_KEY], '1')
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
        self.assertEqual(detail_neutral_focus_slot('icon'), ('first_icon_page_button', 'icon_button'))
        self.assertEqual(detail_neutral_focus_slot('css_assets'), ('css_add_button', 'css_dropdown', 'icon_button'))
        self.assertEqual(detail_neutral_focus_slot('javascript_assets'), ('javascript_add_button', 'javascript_dropdown', 'icon_button'))
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

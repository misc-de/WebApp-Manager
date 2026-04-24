import base64
import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.input_validation.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

from input_validation import (
    MAX_ICON_BASE64_SIZE,
    MAX_URL_LENGTH,
    build_safe_slug,
    contains_unsafe_text,
    is_structurally_valid_url,
    load_import_payloads_from_path,
    normalize_address,
    normalize_wapp_payload,
    sanitize_desktop_value,
)
from webapp_constants import ICON_PATH_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY


class SanitizeDesktopValueTests(unittest.TestCase):
    def test_strips_null_and_newline(self):
        self.assertEqual(sanitize_desktop_value('foo\x00bar\nbaz\r!'), 'foobar baz !')

    def test_fallback_on_empty(self):
        self.assertEqual(sanitize_desktop_value('', fallback='default'), 'default')
        self.assertEqual(sanitize_desktop_value(None, fallback='fb'), 'fb')

    def test_strips_outer_whitespace(self):
        self.assertEqual(sanitize_desktop_value('  value  '), 'value')

    def test_preserves_printable_unicode(self):
        self.assertEqual(sanitize_desktop_value('Grüße'), 'Grüße')


class BuildSafeSlugTests(unittest.TestCase):
    def test_basic_lowercase_underscore(self):
        self.assertEqual(build_safe_slug('My WebApp'), 'my_webapp')

    def test_collapses_specials_to_single_underscore(self):
        self.assertEqual(build_safe_slug('foo!!@@bar'), 'foo_bar')

    def test_preserves_safe_chars(self):
        self.assertEqual(build_safe_slug('foo.bar-baz_qux'), 'foo.bar-baz_qux')

    def test_strips_edge_symbols(self):
        self.assertEqual(build_safe_slug('__--foo--__'), 'foo')

    def test_empty_input(self):
        self.assertEqual(build_safe_slug(''), '')
        self.assertEqual(build_safe_slug(None), '')

    def test_only_specials_returns_empty(self):
        self.assertEqual(build_safe_slug('!!!'), '')


class ContainsUnsafeTextTests(unittest.TestCase):
    def test_accepts_plain_text(self):
        self.assertFalse(contains_unsafe_text('hello world'))

    def test_accepts_tab(self):
        self.assertFalse(contains_unsafe_text('hello\tworld'))

    def test_detects_nul(self):
        self.assertTrue(contains_unsafe_text('foo\x00bar'))

    def test_detects_newline(self):
        self.assertTrue(contains_unsafe_text('foo\nbar'))

    def test_detects_escape(self):
        self.assertTrue(contains_unsafe_text('foo\x1bbar'))

    def test_none_is_safe(self):
        self.assertFalse(contains_unsafe_text(None))


class IsStructurallyValidUrlTests(unittest.TestCase):
    def test_accepts_https(self):
        self.assertTrue(is_structurally_valid_url('https://example.com/'))

    def test_accepts_http(self):
        self.assertTrue(is_structurally_valid_url('http://example.com/'))

    def test_accepts_path_and_query(self):
        self.assertTrue(is_structurally_valid_url('https://example.com/path?a=b#frag'))

    def test_accepts_ipv4(self):
        self.assertTrue(is_structurally_valid_url('http://1.2.3.4/'))

    def test_rejects_javascript_scheme(self):
        self.assertFalse(is_structurally_valid_url('javascript:alert(1)'))

    def test_rejects_file_scheme(self):
        self.assertFalse(is_structurally_valid_url('file:///etc/passwd'))

    def test_rejects_data_scheme(self):
        self.assertFalse(is_structurally_valid_url('data:text/html,<script>'))

    def test_rejects_ftp_scheme(self):
        self.assertFalse(is_structurally_valid_url('ftp://example.com/'))

    def test_rejects_url_with_userinfo(self):
        self.assertFalse(is_structurally_valid_url('https://user:pass@example.com/'))

    def test_rejects_empty_host(self):
        self.assertFalse(is_structurally_valid_url('https:///path'))

    def test_rejects_whitespace(self):
        self.assertFalse(is_structurally_valid_url('https://example.com /path'))
        self.assertFalse(is_structurally_valid_url('https://example.com\tpath'))

    def test_rejects_control_char(self):
        self.assertFalse(is_structurally_valid_url('https://example.com/\x00bad'))

    def test_rejects_too_long(self):
        url = 'https://example.com/' + 'a' * (MAX_URL_LENGTH + 10)
        self.assertFalse(is_structurally_valid_url(url))

    def test_rejects_none(self):
        self.assertFalse(is_structurally_valid_url(None))

    def test_rejects_single_label_host(self):
        self.assertFalse(is_structurally_valid_url('http://localhost-only'))


class NormalizeAddressTests(unittest.TestCase):
    def test_keeps_https_unchanged(self):
        self.assertEqual(normalize_address('https://example.com/'), 'https://example.com/')

    def test_forces_http_to_https_when_requested(self):
        self.assertEqual(normalize_address('http://example.com/', force_https=True), 'https://example.com/')

    def test_preserves_http_when_not_forced(self):
        self.assertEqual(normalize_address('http://example.com/'), 'http://example.com/')

    def test_returns_unchanged_when_no_scheme(self):
        self.assertEqual(normalize_address('example.com/path'), 'example.com/path')

    def test_returns_empty_for_control_chars(self):
        self.assertEqual(normalize_address('http://example.com/\x00'), '')

    def test_returns_empty_for_overlong(self):
        self.assertEqual(normalize_address('http://' + 'a' * (MAX_URL_LENGTH + 10)), '')

    def test_rejects_unknown_scheme(self):
        """normalize_address returns the raw value for unknown schemes (caller is expected to validate)."""
        self.assertEqual(normalize_address('ftp://example.com/'), 'ftp://example.com/')

    def test_strips_outer_whitespace(self):
        self.assertEqual(normalize_address('  https://example.com/  '), 'https://example.com/')


class NormalizeWappPayloadTests(unittest.TestCase):
    def test_rejects_non_dict(self):
        with self.assertRaises(ValueError):
            normalize_wapp_payload('not-a-dict')

    def test_rejects_non_dict_options(self):
        with self.assertRaises(ValueError):
            normalize_wapp_payload({'title': 'X', 'options': 'bad'})

    def test_title_and_description_sanitized(self):
        result = normalize_wapp_payload({'title': 'Foo\x00\nBar', 'description': 'line1\nline2\x00'})
        self.assertNotIn('\x00', result['title'])
        self.assertNotIn('\x00', result['description'])

    def test_coerces_bool_options_to_string_flags(self):
        result = normalize_wapp_payload({'title': 'X', 'options': {'Notifications': True, 'Kiosk': False}})
        self.assertEqual(result['options']['Notifications'], '1')
        self.assertEqual(result['options']['Kiosk'], '0')

    def test_coerces_numeric_option_values(self):
        result = normalize_wapp_payload({'title': 'X', 'options': {'Default Zoom': 125}})
        self.assertEqual(result['options']['Default Zoom'], '125')

    def test_coerces_none_option_to_empty_string(self):
        result = normalize_wapp_payload({'title': 'X', 'options': {'Foo': None}})
        self.assertEqual(result['options']['Foo'], '')

    def test_drops_non_portable_option_keys(self):
        result = normalize_wapp_payload({
            'title': 'X',
            'options': {
                ICON_PATH_KEY: '/tmp/icon.png',
                PROFILE_NAME_KEY: 'secret',
                PROFILE_PATH_KEY: '/tmp/profile',
                'Notifications': '1',
            },
        })
        self.assertNotIn(ICON_PATH_KEY, result['options'])
        self.assertNotIn(PROFILE_NAME_KEY, result['options'])
        self.assertNotIn(PROFILE_PATH_KEY, result['options'])
        self.assertEqual(result['options']['Notifications'], '1')

    def test_drops_non_string_option_keys(self):
        result = normalize_wapp_payload({'title': 'X', 'options': {42: '1'}})
        self.assertEqual(result['options'], {})

    def test_active_defaults_to_true(self):
        self.assertTrue(normalize_wapp_payload({'title': 'X'})['active'])

    def test_active_coerced_to_bool(self):
        self.assertFalse(normalize_wapp_payload({'title': 'X', 'active': 0})['active'])
        self.assertTrue(normalize_wapp_payload({'title': 'X', 'active': 'anything'})['active'])

    def test_icon_payload_rejects_non_dict(self):
        with self.assertRaises(ValueError):
            normalize_wapp_payload({'title': 'X', 'icon': 'not-a-dict'})

    def test_icon_payload_rejects_non_string_data(self):
        with self.assertRaises(ValueError):
            normalize_wapp_payload({'title': 'X', 'icon': {'data_base64': 42}})

    def test_icon_payload_rejects_oversize(self):
        too_big = 'a' * (MAX_ICON_BASE64_SIZE * 2 + 1)
        with self.assertRaises(ValueError):
            normalize_wapp_payload({'title': 'X', 'icon': {'data_base64': too_big}})

    def test_accepts_icon_payload(self):
        data = base64.b64encode(b'\x89PNGsmall').decode('ascii')
        result = normalize_wapp_payload({'title': 'X', 'icon': {'data_base64': data}})
        self.assertEqual(result['icon']['data_base64'], data)
        self.assertEqual(result['icon']['mime'], 'image/png')
        self.assertEqual(result['icon']['filename'], 'icon.png')


class LoadImportPayloadsTests(unittest.TestCase):
    def _write_json(self, payload):
        tmpfile = tempfile.NamedTemporaryFile(suffix='.wapp', delete=False)
        path = Path(tmpfile.name)
        tmpfile.close()
        path.write_text(json.dumps(payload), encoding='utf-8')
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_single_payload_returns_list_with_one(self):
        path = self._write_json({'title': 'Alpha'})
        result = load_import_payloads_from_path(path)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['title'], 'Alpha')

    def test_bundle_format_returns_all_entries(self):
        path = self._write_json({
            'format': 'webapp-export-bundle-v1',
            'entries': [{'title': 'Alpha'}, {'title': 'Beta'}],
        })
        result = load_import_payloads_from_path(path)
        self.assertEqual([item['title'] for item in result], ['Alpha', 'Beta'])

    def test_bundle_with_non_list_entries_raises(self):
        path = self._write_json({
            'format': 'webapp-export-bundle-v1',
            'entries': {'foo': 'bar'},
        })
        with self.assertRaises(ValueError):
            load_import_payloads_from_path(path)

    def test_bundle_rejects_too_many_entries(self):
        path = self._write_json({
            'format': 'webapp-export-bundle-v1',
            'entries': [{'title': f'entry-{i}'} for i in range(501)],
        })
        with self.assertRaises(ValueError):
            load_import_payloads_from_path(path)

    def test_missing_file_raises(self):
        bogus = Path(tempfile.gettempdir()) / 'does-not-exist-12345.wapp'
        if bogus.exists():
            bogus.unlink()
        with self.assertRaises(ValueError):
            load_import_payloads_from_path(bogus)


if __name__ == '__main__':
    unittest.main()

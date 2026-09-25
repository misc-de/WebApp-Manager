"""Coverage-driven tests for the less travelled paths of input_validation."""
import io
import json
import logging
import socket
import sys
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_input_validation.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import input_validation
from input_validation import (
    UnsafeRedirectError,
    _GuardedRedirectHandler,
    _origin_request_headers,
    candidate_urls_for_input,
    check_origin_status,
    host_is_private_or_local,
    is_structurally_valid_url,
    is_valid_url,
    load_and_normalize_wapp_payload_from_path,
    load_import_payloads_from_path,
    normalize_address,
    normalize_wapp_payload,
    payload_contains_inline_javascript,
    validate_icon_source_path,
)


class CandidateUrlTests(unittest.TestCase):
    def test_whitespace_inside_input_yields_no_candidates(self):
        self.assertEqual(candidate_urls_for_input('exa mple.com'), [])

    def test_explicit_scheme_without_host_yields_no_candidates(self):
        self.assertEqual(candidate_urls_for_input('https:'), [])

    def test_protocol_relative_input_is_stripped_before_prefixing(self):
        self.assertEqual(
            candidate_urls_for_input('//example.com/x'),
            ['https://example.com/x', 'http://example.com/x'],
        )

    def test_only_slashes_yield_no_candidates(self):
        self.assertEqual(candidate_urls_for_input('///'), [])

    def test_http_first_when_https_not_preferred(self):
        self.assertEqual(
            candidate_urls_for_input('example.com', prefer_https=False),
            ['http://example.com', 'https://example.com'],
        )

    def test_no_fallback_when_disabled(self):
        self.assertEqual(
            candidate_urls_for_input('example.com', include_http_fallback=False),
            ['https://example.com'],
        )

    def test_explicit_http_scheme_is_kept_as_is(self):
        self.assertEqual(candidate_urls_for_input('http://example.com'), ['http://example.com'])


class StructuralValidationTests(unittest.TestCase):
    def test_schemeless_unparseable_host_is_rejected(self):
        # 'https://[::1' cannot be parsed; the validator must say no, not raise.
        self.assertFalse(is_structurally_valid_url('[::1'))

    def test_empty_and_none_are_rejected(self):
        self.assertFalse(is_structurally_valid_url(None))
        self.assertFalse(is_structurally_valid_url('   '))

    def test_embedded_whitespace_is_rejected(self):
        self.assertFalse(is_structurally_valid_url('https://exa mple.com'))

    def test_port_without_host_is_rejected(self):
        self.assertFalse(is_structurally_valid_url('https://:8080/'))

    def test_single_label_host_is_rejected(self):
        self.assertFalse(is_structurally_valid_url('https://intranet'))

    def test_one_letter_tld_is_rejected(self):
        self.assertFalse(is_structurally_valid_url('https://example.c'))

    def test_unbalanced_ipv6_bracket_with_scheme_is_rejected_not_raised(self):
        # Regression: urlparse raises ValueError('Invalid IPv6 URL') here; the validator must reject, not raise.
        self.assertFalse(is_structurally_valid_url('http://[::1'))

    def test_normalize_address_does_not_raise_on_unbalanced_ipv6_bracket(self):
        # Regression: an unparseable address is returned as typed instead of raising.
        self.assertIsInstance(normalize_address('http://[::1'), str)


class IsValidUrlTests(unittest.TestCase):
    def test_structurally_invalid_url_never_checks_origin(self):
        with mock.patch.object(input_validation, 'check_origin_status') as check:
            self.assertFalse(is_valid_url('javascript:alert(1)'))
        check.assert_not_called()

    def test_skip_origin_check(self):
        with mock.patch.object(input_validation, 'check_origin_status') as check:
            self.assertTrue(is_valid_url('https://example.com', check_origin=False))
        check.assert_not_called()

    def test_origin_status_decides(self):
        with mock.patch.object(input_validation, 'check_origin_status', return_value='blocked'):
            self.assertTrue(is_valid_url('https://example.com'))
        with mock.patch.object(input_validation, 'check_origin_status', return_value='invalid'):
            self.assertFalse(is_valid_url('https://example.com'))


class NormalizeAddressTests(unittest.TestCase):
    def test_rejects_unsafe_and_oversized_values(self):
        self.assertEqual(normalize_address('https://a.de/\x01x'), '')
        self.assertEqual(normalize_address('x' * (input_validation.MAX_URL_LENGTH + 1)), '')

    def test_schemeless_value_is_returned_unchanged(self):
        self.assertEqual(normalize_address(' example.com '), 'example.com')

    def test_foreign_scheme_or_userinfo_is_not_rewritten(self):
        self.assertEqual(normalize_address('ftp://example.com', force_https=True), 'ftp://example.com')
        self.assertEqual(normalize_address('http://u:p@example.com', force_https=True), 'http://u:p@example.com')

    def test_force_https_upgrades_http(self):
        self.assertEqual(normalize_address('http://example.com/a', force_https=True), 'https://example.com/a')
        self.assertEqual(normalize_address('http://example.com/a'), 'http://example.com/a')


class IconSourcePathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.icon = Path(self._tmp.name) / 'icon.png'
        self.icon.write_bytes(b'\x89PNG' + b'0' * 64)

    def test_empty_value_is_rejected(self):
        self.assertIsNone(validate_icon_source_path(''))
        self.assertIsNone(validate_icon_source_path(None))

    def test_existing_small_file_is_accepted(self):
        self.assertEqual(validate_icon_source_path(str(self.icon)), self.icon.resolve())

    def test_directory_is_rejected(self):
        self.assertIsNone(validate_icon_source_path(self._tmp.name))

    def test_oversized_file_is_rejected(self):
        with mock.patch.object(input_validation, 'MAX_ICON_FILE_SIZE', 10):
            self.assertIsNone(validate_icon_source_path(str(self.icon)))

    def test_unresolvable_path_is_rejected(self):
        class _Unresolvable:
            def __init__(self, value):
                pass

            def expanduser(self):
                return self

            def resolve(self):
                raise OSError('loop')

        with mock.patch.object(input_validation, 'Path', _Unresolvable):
            self.assertIsNone(validate_icon_source_path('/whatever'))

    def test_stat_failure_after_existence_check_is_rejected(self):
        class _Vanishing:
            def __init__(self, value):
                pass

            def expanduser(self):
                return self

            def resolve(self):
                return self

            def exists(self):
                return True

            def is_file(self):
                return True

            def stat(self):
                raise OSError('gone')

        with mock.patch.object(input_validation, 'Path', _Vanishing):
            self.assertIsNone(validate_icon_source_path('/whatever'))


class ImportPayloadFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write(self, payload) -> Path:
        path = self.root / 'import.wapp'
        path.write_text(json.dumps(payload), encoding='utf-8')
        return path

    def test_missing_file_raises(self):
        with self.assertRaisesRegex(ValueError, 'missing'):
            load_import_payloads_from_path(self.root / 'nope.wapp')

    def test_oversized_file_raises(self):
        path = self._write({'title': 'x'})
        with mock.patch.object(input_validation, 'MAX_WAPP_FILE_SIZE', 1), \
                self.assertRaisesRegex(ValueError, 'too large'):
            load_import_payloads_from_path(path)

    def test_single_payload_loader_normalizes(self):
        path = self._write({'title': ' Mail\n', 'options': {'Address': 'https://a.de'}})
        payload = load_and_normalize_wapp_payload_from_path(path)
        self.assertEqual(payload['title'], 'Mail')
        self.assertEqual(payload['options'], {'Address': 'https://a.de'})

    def test_bundle_entries_must_be_a_list(self):
        path = self._write({'format': 'webapp-export-bundle-v1', 'entries': {'a': 1}})
        with self.assertRaisesRegex(ValueError, 'array'):
            load_import_payloads_from_path(path)

    def test_bundle_with_too_many_entries_is_refused(self):
        path = self._write({'format': 'webapp-export-bundle-v1', 'entries': [{}] * 501})
        with self.assertRaisesRegex(ValueError, 'too many'):
            load_import_payloads_from_path(path)


class NormalizePayloadTests(unittest.TestCase):
    def test_non_string_title_and_description_are_stringified(self):
        payload = normalize_wapp_payload({'title': 42, 'description': 3.5})
        self.assertEqual(payload['title'], '42')
        self.assertEqual(payload['description'], '3.5')

    def test_none_options_become_empty(self):
        self.assertEqual(normalize_wapp_payload({'options': None})['options'], {})

    def test_option_values_are_normalized_by_type(self):
        payload = normalize_wapp_payload({'options': {
            'Flag': True, 'Off': False, 'Num': 7, 'Nothing': None, 1: 'dropped', '': 'dropped',
        }})
        self.assertEqual(payload['options'], {'Flag': '1', 'Off': '0', 'Num': '7', 'Nothing': ''})

    def test_icon_must_be_an_object_with_text_data(self):
        with self.assertRaisesRegex(ValueError, 'object'):
            normalize_wapp_payload({'icon': 'x'})
        with self.assertRaisesRegex(ValueError, 'base64'):
            normalize_wapp_payload({'icon': {'data_base64': 123}})

    def test_icon_defaults_are_filled_in(self):
        icon = normalize_wapp_payload({'icon': {'data_base64': 'QUJD'}})['icon']
        self.assertEqual(icon, {'filename': 'icon.png', 'mime': 'image/png', 'data_base64': 'QUJD'})


class InlineJavascriptDetectionTests(unittest.TestCase):
    def test_invalid_payload_is_not_reported_as_javascript(self):
        self.assertFalse(payload_contains_inline_javascript('not a dict'))

    def test_detects_non_blank_inline_script(self):
        self.assertTrue(payload_contains_inline_javascript({'options': {'Inline Custom JavaScript': 'alert(1)'}}))
        self.assertFalse(payload_contains_inline_javascript({'options': {'Inline Custom JavaScript': '   '}}))


class HostResolutionTests(unittest.TestCase):
    def test_name_resolving_to_nothing_is_not_local(self):
        with mock.patch.object(input_validation.socket, 'getaddrinfo', return_value=[]):
            self.assertFalse(host_is_private_or_local('example.test'))

    def test_unparseable_resolved_addresses_are_ignored(self):
        infos = [(socket.AF_INET, 0, 0, '', ('not-an-ip', 0))]
        with mock.patch.object(input_validation.socket, 'getaddrinfo', return_value=infos):
            self.assertFalse(host_is_private_or_local('example.test'))

    def test_any_private_resolution_makes_host_local(self):
        infos = [
            (socket.AF_INET, 0, 0, '', ('93.184.216.34', 0)),
            (socket.AF_INET6, 0, 0, '', ('fe80::1%eth0', 0, 0, 0)),
        ]
        with mock.patch.object(input_validation.socket, 'getaddrinfo', return_value=infos):
            self.assertTrue(host_is_private_or_local('example.test'))

    def test_public_resolution_is_not_local(self):
        infos = [(socket.AF_INET, 0, 0, '', ('93.184.216.34', 0))]
        with mock.patch.object(input_validation.socket, 'getaddrinfo', return_value=infos):
            self.assertFalse(host_is_private_or_local('example.test'))


class RedirectHandlerTests(unittest.TestCase):
    def test_malformed_redirect_target_is_refused(self):
        handler = _GuardedRedirectHandler(allow_private_targets=True)
        with self.assertRaisesRegex(UnsafeRedirectError, 'malformed'):
            handler.redirect_request(None, None, 302, 'Found', {}, 'http://[::1')

    def test_strict_mode_refuses_redirect_to_private_host(self):
        handler = _GuardedRedirectHandler(allow_private_targets=False)
        with self.assertRaisesRegex(UnsafeRedirectError, 'private host'):
            handler.redirect_request(None, None, 302, 'Found', {}, 'http://127.0.0.1/admin')


def _http_error(code):
    return urllib.error.HTTPError('https://example.com/', code, 'msg', {}, io.BytesIO(b''))


class _Response:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class CheckOriginStatusTests(unittest.TestCase):
    def _run(self, url, side_effect):
        with mock.patch.object(input_validation.urllib.request, 'urlopen', side_effect=side_effect) as urlopen:
            status = check_origin_status(url)
        return status, urlopen

    def test_invalid_input_makes_no_request(self):
        status, urlopen = self._run('javascript:x', AssertionError('must not be called'))
        self.assertEqual(status, 'invalid')
        urlopen.assert_not_called()

    def test_success_status_is_ok_and_only_origin_is_requested(self):
        status, urlopen = self._run('example.com/deep/path?q=1', [_Response(204)])
        self.assertEqual(status, 'ok')
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, 'https://example.com/')
        self.assertEqual(request.get_header('User-agent'), input_validation.DESKTOP_CHROME_USER_AGENT)
        self.assertEqual(urlopen.call_args.kwargs['timeout'], input_validation.ORIGIN_CHECK_TIMEOUT_SECONDS)

    def test_response_without_status_counts_as_ok(self):
        status, _ = self._run('https://example.com', [_Response(None)])
        self.assertEqual(status, 'ok')

    def test_non_success_response_status_is_unverified(self):
        status, _ = self._run('https://example.com', [_Response(500)])
        self.assertEqual(status, 'unverified')

    def test_redirect_http_error_counts_as_ok(self):
        status, _ = self._run('https://example.com', [_http_error(302)])
        self.assertEqual(status, 'ok')

    def test_blocking_code_is_reported_as_blocked(self):
        status, urlopen = self._run('example.com', [_http_error(403), urllib.error.URLError('down')])
        # The https attempt says "blocked"; the failed http fallback must not
        # downgrade that to "unverified".
        self.assertEqual(status, 'blocked')
        self.assertEqual(urlopen.call_count, 2)

    def test_server_error_is_unverified(self):
        status, _ = self._run('https://example.com', [_http_error(503)])
        self.assertEqual(status, 'unverified')

    def test_unusual_code_is_unverified(self):
        status, _ = self._run('https://example.com', [_http_error(999)])
        self.assertEqual(status, 'unverified')

    def test_network_failure_is_unverified(self):
        status, _ = self._run('example.com', [OSError('unreachable'), ValueError('bad')])
        self.assertEqual(status, 'unverified')

    def test_unparseable_candidate_is_skipped(self):
        status, urlopen = self._run('[::1', AssertionError('must not be called'))
        self.assertEqual(status, 'invalid')
        urlopen.assert_not_called()

    def test_candidate_without_host_is_skipped(self):
        status, urlopen = self._run('/only/a/path', AssertionError('must not be called'))
        self.assertEqual(status, 'invalid')
        urlopen.assert_not_called()

    def test_origin_returns_200_accepts_every_reachable_status(self):
        for value, expected in (('ok', True), ('blocked', True), ('unverified', True), ('invalid', False)):
            with mock.patch.object(input_validation, 'check_origin_status', return_value=value):
                self.assertIs(input_validation.origin_returns_200('https://example.com'), expected)

    def test_origin_headers_ask_to_close_and_skip_caches(self):
        headers = _origin_request_headers()
        self.assertEqual(headers['Connection'], 'close')
        self.assertEqual(headers['Cache-Control'], 'no-cache')


if __name__ == '__main__':
    unittest.main()

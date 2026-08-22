"""Tests for the outbound request guard in input_validation.

The guard exists because every URL this app fetches can originate from
attacker-influenced data (a .wapp import file, a redirect chain). It must
keep two properties:

  * a redirect can never leave http/https, and
  * a redirect can never pivot from a public host into a private one,

while still allowing a *directly* requested LAN host, which is a legitimate
way to run a web app.
"""
import http.server
import logging
import socket
import sys
import threading
import types
import unittest
import urllib.error


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.guard.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

from input_validation import (
    MAX_REDIRECTS,
    UnsafeRedirectError,
    build_guarded_opener,
    host_is_private_or_local,
    open_guarded_url,
)


class HostClassificationTests(unittest.TestCase):
    def test_loopback_and_private_literals_are_local(self):
        for host in ('127.0.0.1', '127.5.5.5', '::1', '[::1]', '10.0.0.1', '192.168.1.10', '172.16.0.1', '169.254.1.1', '0.0.0.0'):
            with self.subTest(host=host):
                self.assertTrue(host_is_private_or_local(host))

    def test_local_names_are_local(self):
        for host in ('localhost', 'LOCALHOST', 'app.localhost', 'nas.local', 'nas.local.'):
            with self.subTest(host=host):
                self.assertTrue(host_is_private_or_local(host))

    def test_empty_host_is_treated_as_local(self):
        self.assertTrue(host_is_private_or_local(''))
        self.assertTrue(host_is_private_or_local(None))

    def test_public_literal_is_not_local(self):
        self.assertFalse(host_is_private_or_local('93.184.216.34'))
        self.assertFalse(host_is_private_or_local('2606:2800:220:1:248:1893:25c8:1946'))

    def test_unresolvable_name_is_not_reported_as_local(self):
        # No evidence of a private target: the connection will fail anyway.
        self.assertFalse(host_is_private_or_local('nx-guard-test-8f21a.invalid'))


class SchemeGuardTests(unittest.TestCase):
    def test_non_http_schemes_are_refused_before_any_io(self):
        for url in ('file:///etc/passwd', 'data:text/plain,x', 'ftp://example.com/a', 'gopher://example.com'):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeRedirectError):
                    open_guarded_url(url, timeout=1)

    def test_strict_mode_refuses_private_target_up_front(self):
        with self.assertRaises(UnsafeRedirectError):
            open_guarded_url('http://127.0.0.1:1/x', timeout=1, allow_private_targets=False)

    def test_guarded_opener_has_no_file_or_ftp_handler(self):
        opener = build_guarded_opener()
        installed = {type(handler).__name__ for handler in opener.handlers}
        self.assertNotIn('FileHandler', installed)
        self.assertNotIn('FTPHandler', installed)
        self.assertNotIn('DataHandler', installed)


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    """Serves whatever redirect target the test class puts in `redirect_to`."""

    redirect_to = ''
    protocol_version = 'HTTP/1.0'

    def do_GET(self):
        if self.path == '/target':
            body = b'reached-target'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(302)
        self.send_header('Location', type(self).redirect_to)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


class RedirectGuardTests(unittest.TestCase):
    """Drives a real loopback HTTP server, because the guard hooks into the
    redirect machinery rather than into a function we could simply stub."""

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(('127.0.0.1', 0), _RedirectHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _base(self):
        return f'http://127.0.0.1:{self.port}'

    def test_lan_target_is_reachable_by_default(self):
        # The whole point of allow_private_targets=True: running a web app
        # against a host on the local network must keep working.
        with open_guarded_url(f'{self._base()}/target', timeout=5) as response:
            self.assertEqual(response.read(), b'reached-target')

    def test_redirect_to_file_scheme_is_blocked(self):
        # urllib's own redirect handler already refuses file:; assert the
        # outcome rather than the message, which is not ours.
        _RedirectHandler.redirect_to = 'file:///etc/passwd'
        with self.assertRaises(urllib.error.URLError):
            open_guarded_url(f'{self._base()}/start', timeout=5)

    def test_redirect_to_ftp_scheme_is_blocked_by_the_guard(self):
        # urllib's HTTPRedirectHandler explicitly allows ftp: as a redirect
        # target, so this hop is stopped by our guard, not by the stdlib.
        _RedirectHandler.redirect_to = 'ftp://example.invalid/payload'
        with self.assertRaises(urllib.error.URLError) as caught:
            open_guarded_url(f'{self._base()}/start', timeout=5)
        self.assertIn('non-http scheme', str(caught.exception.reason))

    def test_public_to_private_pivot_is_blocked_in_strict_mode(self):
        _RedirectHandler.redirect_to = f'{self._base()}/target'
        with self.assertRaises(urllib.error.URLError) as caught:
            open_guarded_url(f'{self._base()}/start', timeout=5, allow_private_targets=False)
        self.assertIn('private host', str(caught.exception.reason))

    def test_redirect_within_lan_is_allowed_in_default_mode(self):
        _RedirectHandler.redirect_to = f'{self._base()}/target'
        with open_guarded_url(f'{self._base()}/start', timeout=5) as response:
            self.assertEqual(response.read(), b'reached-target')

    def test_redirect_loop_is_bounded(self):
        _RedirectHandler.redirect_to = f'{self._base()}/start'
        with self.assertRaises(urllib.error.HTTPError):
            open_guarded_url(f'{self._base()}/start', timeout=5)
        self.assertEqual(MAX_REDIRECTS, 5)


def _loopback_available() -> bool:
    try:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
        return True
    except OSError:
        return False


if not _loopback_available():  # pragma: no cover - only on locked-down builders
    RedirectGuardTests = unittest.skip('loopback networking unavailable')(RedirectGuardTests)


if __name__ == '__main__':
    unittest.main()

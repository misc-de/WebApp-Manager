import http.server
import io
import json
import logging
import sys
import tempfile
import threading
import types
import unittest
import zipfile
from pathlib import Path


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.security.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

from browser_profiles import _assert_safe_zip_members, _scope_swipe_extension_payload

try:
    import cairosvg
except ImportError:
    cairosvg = None

if cairosvg is not None:
    from icon_pipeline import _render_svg_bytes_to_png
else:
    _render_svg_bytes_to_png = None


def _zip_bytes(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            if isinstance(payload, str):
                payload = payload.encode('utf-8')
            archive.writestr(name, payload)
    return buffer.getvalue()


class ZipSlipGuardTests(unittest.TestCase):
    def test_accepts_benign_members(self):
        data = _zip_bytes({
            'manifest.json': '{}',
            'icons/app.png': b'\x89PNG',
            'subdir/nested/deep/file.txt': 'ok',
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                _assert_safe_zip_members(archive, Path(tmpdir))

    def test_rejects_parent_escape(self):
        data = _zip_bytes({'../evil.txt': 'pwned'})
        with tempfile.TemporaryDirectory() as tmpdir:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                with self.assertRaises(ValueError) as context:
                    _assert_safe_zip_members(archive, Path(tmpdir))
        self.assertIn('../evil.txt', str(context.exception))

    def test_rejects_nested_parent_escape(self):
        data = _zip_bytes({'sub/../../evil.txt': 'pwned'})
        with tempfile.TemporaryDirectory() as tmpdir:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                with self.assertRaises(ValueError):
                    _assert_safe_zip_members(archive, Path(tmpdir))

    def test_rejects_absolute_unix_path(self):
        data = _zip_bytes({'/tmp/owned.txt': 'pwned'})
        with tempfile.TemporaryDirectory() as tmpdir:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                with self.assertRaises(ValueError):
                    _assert_safe_zip_members(archive, Path(tmpdir))

    def test_rejects_backslash_path(self):
        data = _zip_bytes({'sub\\..\\evil.txt': 'pwned'})
        with tempfile.TemporaryDirectory() as tmpdir:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                with self.assertRaises(ValueError):
                    _assert_safe_zip_members(archive, Path(tmpdir))

    def test_allows_plain_file_at_root(self):
        data = _zip_bytes({'manifest.json': '{}'})
        with tempfile.TemporaryDirectory() as tmpdir:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                _assert_safe_zip_members(archive, Path(tmpdir))


class ScopeSwipeXpiTests(unittest.TestCase):
    def _benign_xpi(self):
        manifest = {
            'manifest_version': 2,
            'name': 'Swipe',
            'version': '0.0.1',
            'content_scripts': [
                {'matches': ['<all_urls>'], 'js': ['content.js']},
            ],
        }
        return _zip_bytes({
            'manifest.json': json.dumps(manifest),
            'content.js': '// noop',
        })

    def test_returns_original_bytes_when_address_has_no_matches(self):
        xpi = self._benign_xpi()
        self.assertEqual(_scope_swipe_extension_payload(xpi, ''), xpi)
        self.assertEqual(_scope_swipe_extension_payload(xpi, 'ftp://unsupported/'), xpi)

    def test_rescopes_manifest_for_valid_address(self):
        xpi = self._benign_xpi()
        result = _scope_swipe_extension_payload(xpi, 'https://app.example.com/dashboard')

        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            manifest = json.loads(archive.read('manifest.json').decode('utf-8'))
            names = [item.filename for item in archive.infolist()]

        self.assertEqual(manifest['host_permissions'], ['https://app.example.com/*'])
        self.assertEqual(manifest['content_scripts'][0]['matches'], ['https://app.example.com/*'])
        self.assertTrue(all(not name.upper().startswith('META-INF/') for name in names))

    def test_rejects_malicious_archive_before_extraction(self):
        manifest = {'manifest_version': 2, 'name': 'Swipe', 'version': '0.0.1'}
        xpi = _zip_bytes({
            'manifest.json': json.dumps(manifest),
            '../evil.js': 'throw "bad"',
        })
        with self.assertRaises(ValueError):
            _scope_swipe_extension_payload(xpi, 'https://app.example.com/')

    def test_rejects_absolute_path_member(self):
        xpi = _zip_bytes({
            'manifest.json': json.dumps({'manifest_version': 2, 'name': 'X', 'version': '1'}),
            '/etc/owned.conf': 'pwned',
        })
        with self.assertRaises(ValueError):
            _scope_swipe_extension_payload(xpi, 'https://app.example.com/')


@unittest.skipUnless(cairosvg is not None, 'cairosvg is not installed')
class RenderSvgExternalBlockingTests(unittest.TestCase):
    """What protects the SVG import path is cairosvg's unsafe=False, not a
    fetcher callback of ours. These tests assert the behaviour that actually
    matters: nothing is fetched, and entities are refused."""

    MINIMAL_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><rect width="16" height="16" fill="red"/></svg>'

    SVG_WITH_ENTITY = (
        b'<?xml version="1.0"?>\n'
        b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/hostname">]>\n'
        b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><text x="0" y="10">&xxe;</text></svg>'
    )

    def test_plain_svg_renders(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / 'out.png'
            _render_svg_bytes_to_png(self.MINIMAL_SVG, target)
            self.assertTrue(target.exists())
            self.assertGreater(target.stat().st_size, 0)

    def test_xml_entities_are_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / 'xxe.png'
            with self.assertRaises(Exception):
                _render_svg_bytes_to_png(self.SVG_WITH_ENTITY, target)

    def test_external_image_reference_is_never_fetched(self):
        """The SSRF-relevant property: rendering must not perform the request.

        Checked against a real server rather than a stub, because the fetch
        would happen inside cairosvg, where a stub of ours cannot observe it.
        """
        requested = []

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requested.append(self.path)
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Content-Length', '0')
                self.end_headers()

            def log_message(self, fmt, *args):
                pass

        server = http.server.HTTPServer(('127.0.0.1', 0), _Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            svg = (
                '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="16" height="16">'
                f'<image xlink:href="http://127.0.0.1:{port}/tracker.png" width="16" height="16"/>'
                '</svg>'
            ).encode('utf-8')
            with tempfile.TemporaryDirectory() as tmpdir:
                target = Path(tmpdir) / 'external.png'
                try:
                    _render_svg_bytes_to_png(svg, target)
                except Exception:
                    # Refusing outright is also acceptable -- the assertion
                    # below is about the request never being made.
                    pass
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(requested, [])


if __name__ == '__main__':
    unittest.main()

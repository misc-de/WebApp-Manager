import io
import json
import logging
import sys
import tempfile
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
from icon_pipeline import _block_external_svg_resource

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


class BlockExternalSvgResourceTests(unittest.TestCase):
    def test_raises_for_http_url(self):
        with self.assertRaises(ValueError) as context:
            _block_external_svg_resource('http://169.254.169.254/latest/meta-data/')
        self.assertIn('not allowed', str(context.exception))

    def test_raises_for_https_url(self):
        with self.assertRaises(ValueError):
            _block_external_svg_resource('https://evil.example.com/leak')

    def test_raises_for_file_scheme(self):
        with self.assertRaises(ValueError):
            _block_external_svg_resource('file:///etc/passwd')

    def test_raises_for_ftp_scheme(self):
        with self.assertRaises(ValueError):
            _block_external_svg_resource('ftp://example.com/resource')

    def test_accepts_extra_args(self):
        with self.assertRaises(ValueError):
            _block_external_svg_resource('http://x/', resource_type='image')


@unittest.skipUnless(cairosvg is not None, 'cairosvg is not installed')
class RenderSvgExternalBlockingTests(unittest.TestCase):
    MINIMAL_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><rect width="16" height="16" fill="red"/></svg>'

    SVG_WITH_EXTERNAL_IMAGE = (
        b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="16" height="16">'
        b'<image xlink:href="http://169.254.169.254/latest/meta-data/" width="16" height="16"/>'
        b'</svg>'
    )

    def test_plain_svg_renders(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / 'out.png'
            _render_svg_bytes_to_png(self.MINIMAL_SVG, target)
            self.assertTrue(target.exists())
            self.assertGreater(target.stat().st_size, 0)

    def test_svg_with_external_image_ref_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / 'blocked.png'
            with self.assertRaises(Exception):
                _render_svg_bytes_to_png(self.SVG_WITH_EXTERNAL_IMAGE, target)


if __name__ == '__main__':
    unittest.main()

"""Coverage tests for icon_pipeline: lazy renderer loading, managed icon
naming, SVG sniffing, both SVG renderers and PNG normalisation.

APPLICATIONS_DIR / ICON_THEME_APPS_DIR are redirected to a temporary directory
in every test that touches them; the renderers are replaced by fakes so no
real cairosvg or librsvg is required.
"""
import io
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_icon_pipeline.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import icon_pipeline

try:
    from PIL import Image as _PILImage
except ImportError:  # pragma: no cover - Pillow is a hard runtime dependency
    _PILImage = None

MINIMAL_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>'


def _png_bytes(mode='P'):
    image = _PILImage.new(mode, (3, 2))
    buffer = io.BytesIO()
    image.save(buffer, 'PNG')
    return buffer.getvalue()


class _SandboxedIconDirsMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.apps_dir = self.base / 'applications'
        self.theme_dir = self.base / 'icons' / 'hicolor' / '512x512' / 'apps'
        for name, value in (('APPLICATIONS_DIR', self.apps_dir), ('ICON_THEME_APPS_DIR', self.theme_dir)):
            patcher = mock.patch.object(icon_pipeline, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class LazyLoaderTests(unittest.TestCase):
    def _clear(self, loader):
        loader.cache_clear()
        self.addCleanup(loader.cache_clear)

    @unittest.skipIf(_PILImage is None, 'Pillow not installed')
    def test_pillow_loader_returns_the_image_module(self):
        self._clear(icon_pipeline._pillow_image)
        self.assertIs(icon_pipeline._pillow_image(), _PILImage)

    def test_cairosvg_loader_returns_none_when_missing(self):
        self._clear(icon_pipeline._cairosvg)
        with mock.patch.dict(sys.modules, {'cairosvg': None}):
            self.assertIsNone(icon_pipeline._cairosvg())

    def test_cairosvg_loader_returns_module_when_present(self):
        self._clear(icon_pipeline._cairosvg)
        fake = types.ModuleType('cairosvg')
        with mock.patch.dict(sys.modules, {'cairosvg': fake}):
            self.assertIs(icon_pipeline._cairosvg(), fake)

    def test_gdk_pixbuf_loader_returns_none_without_gi(self):
        self._clear(icon_pipeline._gdk_pixbuf_svg)
        with mock.patch.dict(sys.modules, {'gi_versions': None}):
            self.assertIsNone(icon_pipeline._gdk_pixbuf_svg())

    def _fake_gi(self, format_names):
        formats = []
        for name in format_names:
            fmt = mock.Mock()
            fmt.get_name.return_value = name
            formats.append(fmt)
        gdk_pixbuf = types.SimpleNamespace(Pixbuf=types.SimpleNamespace(get_formats=lambda: formats))
        repository = types.ModuleType('gi.repository')
        repository.Gio = object()
        repository.GLib = object()
        repository.GdkPixbuf = gdk_pixbuf
        gi = types.ModuleType('gi')
        gi.repository = repository
        gi.__path__ = []
        return {'gi_versions': types.ModuleType('gi_versions'), 'gi': gi, 'gi.repository': repository}, repository

    def test_gdk_pixbuf_loader_requires_an_svg_format(self):
        self._clear(icon_pipeline._gdk_pixbuf_svg)
        modules, _ = self._fake_gi(['png', None])
        with mock.patch.dict(sys.modules, modules):
            self.assertIsNone(icon_pipeline._gdk_pixbuf_svg())

    def test_gdk_pixbuf_loader_returns_the_bindings_with_svg_format(self):
        self._clear(icon_pipeline._gdk_pixbuf_svg)
        modules, repository = self._fake_gi(['png', 'svg'])
        with mock.patch.dict(sys.modules, modules):
            result = icon_pipeline._gdk_pixbuf_svg()
        self.assertEqual(result, (repository.GdkPixbuf, repository.Gio, repository.GLib))


class ManagedIconNamingTests(_SandboxedIconDirsMixin, unittest.TestCase):
    def test_stem_prefers_slug_then_entry_id_then_fallback(self):
        self.assertEqual(icon_pipeline.get_managed_icon_name('My App'), 'my_app')
        self.assertEqual(icon_pipeline.get_managed_icon_name('!!!', 5), 'webapp-entry-5')
        self.assertEqual(icon_pipeline.get_managed_icon_name('', ''), 'webapp')
        self.assertEqual(icon_pipeline.get_managed_icon_name(None), 'webapp')

    def test_icon_paths_normalise_the_extension(self):
        self.assertEqual(icon_pipeline.get_managed_icon_path('My App', 'png'), self.apps_dir / 'my_app.png')
        self.assertEqual(icon_pipeline.get_managed_icon_path('My App'), self.apps_dir / 'my_app.png')
        self.assertEqual(icon_pipeline.get_managed_theme_icon_path('', 'svg', 3), self.theme_dir / 'webapp-entry-3.svg')
        self.assertEqual(icon_pipeline.get_managed_theme_icon_path('X', '.png'), self.theme_dir / 'x.png')

    def test_ensure_applications_dir_creates_both_directories(self):
        icon_pipeline.ensure_applications_dir()
        self.assertTrue(self.apps_dir.is_dir())
        self.assertTrue(self.theme_dir.is_dir())

    def test_allowed_stems(self):
        self.assertEqual(icon_pipeline._allowed_managed_icon_stems(7, 'My App'), {'webapp-entry-7', 'my_app'})
        self.assertEqual(icon_pipeline._allowed_managed_icon_stems(7, ''), {'webapp-entry-7'})

    def test_safe_managed_icon_path(self):
        self.assertTrue(icon_pipeline._is_safe_managed_icon_path(self.apps_dir / 'my_app.png', 1, 'My App'))
        self.assertTrue(icon_pipeline._is_safe_managed_icon_path(self.theme_dir / 'WEBAPP-ENTRY-1.png', 1, ''))
        self.assertFalse(icon_pipeline._is_safe_managed_icon_path(self.apps_dir / 'other.png', 1, 'My App'))
        self.assertFalse(icon_pipeline._is_safe_managed_icon_path(self.base / 'my_app.png', 1, 'My App'))
        self.assertFalse(icon_pipeline._is_safe_managed_icon_path(self.apps_dir / 'sub' / 'my_app.png', 1, 'My App'))

    def test_unresolvable_icon_path_is_not_safe(self):
        with mock.patch.object(Path, 'resolve', side_effect=OSError('loop')):
            self.assertFalse(icon_pipeline._is_safe_managed_icon_path('/x/my_app.png', 1, 'My App'))


class SvgSniffingTests(unittest.TestCase):
    def test_detection(self):
        self.assertFalse(icon_pipeline._looks_like_svg(b''))
        self.assertTrue(icon_pipeline._looks_like_svg(b'<?xml version="1.0"?><svg/>'))
        self.assertTrue(icon_pipeline._looks_like_svg(b'   \n<svg/>'))
        self.assertTrue(icon_pipeline._looks_like_svg(b'<!DOCTYPE svg><SVG xmlns="x"/>'))
        self.assertFalse(icon_pipeline._looks_like_svg(b'\x89PNG\r\n\x1a\n\x00\x00'))

    def test_support_flags(self):
        with mock.patch.object(icon_pipeline, '_cairosvg', return_value=object()), \
                mock.patch.object(icon_pipeline, '_gdk_pixbuf_svg', return_value=None):
            self.assertTrue(icon_pipeline.svg_support_available())
        self.assertFalse(icon_pipeline.is_svg_support_missing_error(None))
        self.assertTrue(icon_pipeline.is_svg_support_missing_error(OSError(icon_pipeline.SVG_CAIRO_MISSING_ERROR)))


class _FakeGLibError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _fake_gdk(pixbuf=None, error=None, save_result=(True, b'PNGDATA')):
    stream = mock.Mock()
    glib = types.SimpleNamespace(Error=_FakeGLibError, Bytes=types.SimpleNamespace(new=lambda data: ('bytes', data)))
    gio = types.SimpleNamespace(MemoryInputStream=types.SimpleNamespace(new_from_bytes=mock.Mock(return_value=stream)))
    if pixbuf is None and error is None:
        pixbuf = mock.Mock()
        pixbuf.save_to_bufferv.return_value = save_result
    new_from_stream = mock.Mock(side_effect=error) if error else mock.Mock(return_value=pixbuf)
    gdk = types.SimpleNamespace(Pixbuf=types.SimpleNamespace(new_from_stream_at_scale=new_from_stream))
    return (gdk, gio, glib), stream, new_from_stream


class GdkPixbufRendererTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.target = Path(self._tmp.name) / 'out' / 'icon.png'

    def test_renders_png_and_closes_the_stream(self):
        bindings, stream, new_from_stream = _fake_gdk()
        with mock.patch.object(icon_pipeline, '_gdk_pixbuf_svg', return_value=bindings):
            result = icon_pipeline._render_svg_with_gdk_pixbuf(MINIMAL_SVG, self.target)
        self.assertEqual(result, self.target)
        self.assertEqual(self.target.read_bytes(), b'PNGDATA')
        self.assertEqual(new_from_stream.call_args.args[1:4], (256, 256, True))
        stream.close.assert_called_once_with(None)

    def test_loader_error_becomes_oserror(self):
        bindings, stream, _ = _fake_gdk(error=_FakeGLibError('bad svg'))
        with mock.patch.object(icon_pipeline, '_gdk_pixbuf_svg', return_value=bindings):
            with self.assertRaisesRegex(OSError, 'bad svg'):
                icon_pipeline._render_svg_with_gdk_pixbuf(MINIMAL_SVG, self.target)
        stream.close.assert_called_once_with(None)

    def test_missing_pixbuf_becomes_oserror(self):
        bindings, _, new_from_stream = _fake_gdk()
        new_from_stream.return_value = None
        with mock.patch.object(icon_pipeline, '_gdk_pixbuf_svg', return_value=bindings):
            with self.assertRaisesRegex(OSError, 'could not be rendered'):
                icon_pipeline._render_svg_with_gdk_pixbuf(MINIMAL_SVG, self.target)
        self.assertFalse(self.target.exists())

    def test_encoding_failure_becomes_oserror(self):
        bindings, _, _ = _fake_gdk(save_result=(False, b''))
        with mock.patch.object(icon_pipeline, '_gdk_pixbuf_svg', return_value=bindings):
            with self.assertRaisesRegex(OSError, 'encoded as PNG'):
                icon_pipeline._render_svg_with_gdk_pixbuf(MINIMAL_SVG, self.target)
        self.assertFalse(self.target.exists())


class RenderDispatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.target = Path(self._tmp.name) / 'nested' / 'icon.png'

    def test_cairosvg_is_called_with_unsafe_disabled(self):
        fake = mock.Mock()
        with mock.patch.object(icon_pipeline, '_cairosvg', return_value=fake):
            result = icon_pipeline._render_svg_bytes_to_png(MINIMAL_SVG, self.target)
        self.assertEqual(result, self.target)
        self.assertTrue(self.target.parent.is_dir())
        fake.svg2png.assert_called_once_with(
            bytestring=MINIMAL_SVG,
            write_to=str(self.target),
            output_width=256,
            output_height=256,
            unsafe=False,
        )

    def test_gdk_pixbuf_is_used_without_cairosvg(self):
        with mock.patch.object(icon_pipeline, '_cairosvg', return_value=None), \
                mock.patch.object(icon_pipeline, '_gdk_pixbuf_svg', return_value=object()), \
                mock.patch.object(icon_pipeline, '_render_svg_with_gdk_pixbuf', return_value='rendered') as render:
            self.assertEqual(icon_pipeline._render_svg_bytes_to_png(MINIMAL_SVG, self.target), 'rendered')
        render.assert_called_once_with(MINIMAL_SVG, self.target)

    def test_missing_renderers_raise_the_support_error(self):
        with mock.patch.object(icon_pipeline, '_cairosvg', return_value=None), \
                mock.patch.object(icon_pipeline, '_gdk_pixbuf_svg', return_value=None):
            with self.assertRaises(OSError) as caught:
                icon_pipeline._render_svg_bytes_to_png(MINIMAL_SVG, self.target)
        self.assertTrue(icon_pipeline.is_svg_support_missing_error(caught.exception))
        self.assertFalse(self.target.exists())


class NormalizeIconTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.target = self.base / 'out' / 'icon.png'

    def test_empty_payload_is_rejected(self):
        with self.assertRaisesRegex(OSError, 'Empty icon payload'):
            icon_pipeline.normalize_icon_bytes_to_png(b'', self.target)

    def test_svg_is_detected_by_suffix_content_type_or_content(self):
        with mock.patch.object(icon_pipeline, '_render_svg_bytes_to_png', return_value='svg') as render:
            self.assertEqual(icon_pipeline.normalize_icon_bytes_to_png(b'data', self.target, source_name='a.SVG'), 'svg')
            self.assertEqual(icon_pipeline.normalize_icon_bytes_to_png(b'data', self.target, content_type='Image/SVG+XML'), 'svg')
            self.assertEqual(icon_pipeline.normalize_icon_bytes_to_png(MINIMAL_SVG, self.target), 'svg')
        self.assertEqual(render.call_count, 3)

    @unittest.skipIf(_PILImage is None, 'Pillow not installed')
    def test_raster_payload_is_converted_to_rgba_png(self):
        result = icon_pipeline.normalize_icon_bytes_to_png(_png_bytes('P'), self.target, source_name='favicon.ico')
        self.assertEqual(result, self.target)
        with _PILImage.open(self.target) as image:
            self.assertEqual(image.format, 'PNG')
            self.assertEqual(image.mode, 'RGBA')
            self.assertEqual(image.size, (3, 2))

    def test_invalid_source_path_is_rejected(self):
        with self.assertRaisesRegex(OSError, 'Invalid icon source path'):
            icon_pipeline.normalize_icon_to_png(self.base / 'missing.png', self.target)

    def test_svg_source_file_is_passed_with_svg_content_type(self):
        source = self.base / 'logo.svg'
        source.write_bytes(MINIMAL_SVG)
        with mock.patch.object(icon_pipeline, 'normalize_icon_bytes_to_png', return_value='ok') as normalize:
            self.assertEqual(icon_pipeline.normalize_icon_to_png(source, self.target), 'ok')
        normalize.assert_called_once_with(MINIMAL_SVG, self.target, source_name='logo.svg', content_type='image/svg+xml')

    @unittest.skipIf(_PILImage is None, 'Pillow not installed')
    def test_png_source_file_is_normalised(self):
        source = self.base / 'logo.png'
        source.write_bytes(_png_bytes('RGB'))
        icon_pipeline.normalize_icon_to_png(source, self.target)
        with _PILImage.open(self.target) as image:
            self.assertEqual(image.mode, 'RGBA')


if __name__ == '__main__':
    unittest.main()

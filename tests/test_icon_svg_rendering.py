import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.iconsvg.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import icon_pipeline

MINIMAL_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><rect width="16" height="16" fill="red"/></svg>'


class SvgRendererSelectionTests(unittest.TestCase):
    """A site whose only icon is an SVG must still yield a PNG on a source
    install without cairosvg, as long as gdk-pixbuf has an SVG loader."""

    @unittest.skipUnless(icon_pipeline._gdk_pixbuf_svg() is not None, 'no GdkPixbuf SVG loader available')
    def test_gdk_pixbuf_renders_when_cairosvg_is_missing(self):
        with mock.patch.object(icon_pipeline, '_cairosvg', return_value=None):
            with tempfile.TemporaryDirectory() as tmpdir:
                target = Path(tmpdir) / 'icon.png'
                icon_pipeline.normalize_icon_bytes_to_png(MINIMAL_SVG, target, source_name='favicon.svg', content_type='image/svg+xml')
                self.assertTrue(target.exists())
                self.assertGreater(target.stat().st_size, 0)
                self.assertTrue(icon_pipeline.svg_support_available())

    def test_missing_error_only_when_both_renderers_are_absent(self):
        with mock.patch.object(icon_pipeline, '_cairosvg', return_value=None), mock.patch.object(icon_pipeline, '_gdk_pixbuf_svg', return_value=None):
            self.assertFalse(icon_pipeline.svg_support_available())
            with tempfile.TemporaryDirectory() as tmpdir:
                target = Path(tmpdir) / 'icon.png'
                with self.assertRaises(OSError) as caught:
                    icon_pipeline.normalize_icon_bytes_to_png(MINIMAL_SVG, target, source_name='favicon.svg', content_type='image/svg+xml')
                self.assertTrue(icon_pipeline.is_svg_support_missing_error(caught.exception))


if __name__ == '__main__':
    unittest.main()

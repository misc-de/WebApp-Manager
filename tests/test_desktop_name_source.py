import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.desktop_name.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

from desktop_entries import MANAGED_BY_VALUE, desktop_display_name, parse_desktop_file


class DesktopNameSourceTests(unittest.TestCase):
    def test_desktop_display_name_uses_description_when_selected(self):
        entry = SimpleNamespace(id=1, title='Main Title', description='Shown Description')

        self.assertEqual(desktop_display_name(entry, {'DesktopNameSource': 'description'}), 'Shown Description')

    def test_desktop_display_name_falls_back_to_title_when_description_missing(self):
        entry = SimpleNamespace(id=1, title='Main Title', description='')

        self.assertEqual(desktop_display_name(entry, {'DesktopNameSource': 'description'}), 'Main Title')

    def test_parse_desktop_file_prefers_internal_title_and_restores_source(self):
        content = '\n'.join(
            [
                '[Desktop Entry]',
                f'ManagedBy={MANAGED_BY_VALUE}',
                'EntryId=7',
                'Name=Launcher Label',
                'X-WebApp-Title=Stored Title',
                'X-WebApp-DesktopNameSource=description',
                'Exec=firefox https://example.com',
                'Type=Application',
                'NoDisplay=false',
                '',
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / 'sample.desktop'
            path.write_text(content, encoding='utf-8')
            parsed = parse_desktop_file(path, [])

        self.assertEqual(parsed['title'], 'Stored Title')
        self.assertEqual(parsed['options']['DesktopNameSource'], 'description')


if __name__ == '__main__':
    unittest.main()

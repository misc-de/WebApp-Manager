"""Tests for the fast path in the managed-desktop-file scan.

The applications directory is mostly full of launchers this app never wrote, so
list_managed_desktop_files rejects a file on a cheap substring test before
ConfigParser sees it. The point of these tests is that the shortcut is purely a
speed-up: whatever parse_desktop_file would have accepted still comes back, and
nothing else does.
"""
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.managed_desktop_scan.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import desktop_entries
from desktop_entries import MANAGED_BY_VALUE, list_managed_desktop_files

ENGINES = [{'id': 1, 'name': 'Firefox', 'command': 'firefox'}]


def _managed(title, entry_id):
    return '\n'.join([
        '[Desktop Entry]',
        'Type=Application',
        f'ManagedBy={MANAGED_BY_VALUE}',
        f'EntryId={entry_id}',
        f'Name={title}',
        f'X-WebApp-Title={title}',
        'Exec=firefox https://example.com',
        '',
    ])


def _foreign(title):
    return '\n'.join([
        '[Desktop Entry]',
        'Type=Application',
        f'Name={title}',
        'Exec=/usr/bin/something',
        '',
    ])


class ManagedDesktopScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.applications_dir = Path(self._tmp.name)
        patcher = mock.patch.object(desktop_entries, 'APPLICATIONS_DIR', self.applications_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, name, content):
        path = self.applications_dir / name
        path.write_text(content, encoding='utf-8')
        return path

    def test_returns_only_managed_files(self):
        self._write('webapp-one.desktop', _managed('One', 1))
        self._write('webapp-two.desktop', _managed('Two', 2))
        self._write('firefox.desktop', _foreign('Firefox'))
        self._write('gimp.desktop', _foreign('GIMP'))

        found = list_managed_desktop_files(ENGINES)

        self.assertEqual([item['title'] for item in found], ['One', 'Two'])

    def test_foreign_files_never_reach_the_parser(self):
        self._write('webapp-one.desktop', _managed('One', 1))
        self._write('firefox.desktop', _foreign('Firefox'))

        with mock.patch.object(desktop_entries, 'parse_desktop_file', wraps=desktop_entries.parse_desktop_file) as parse:
            list_managed_desktop_files(ENGINES)

        parsed = [Path(call.args[0]).name for call in parse.call_args_list]
        self.assertEqual(parsed, ['webapp-one.desktop'])

    def test_foreign_managed_by_value_is_still_rejected(self):
        # The cheap test only looks for the key, so a launcher from a different
        # program that happens to carry one must still be dropped by the parser.
        self._write('other-manager.desktop', _foreign('Other').rstrip('\n') + '\nManagedBy=Some Other Tool\n')

        self.assertEqual(list_managed_desktop_files(ENGINES), [])

    def test_unreadable_file_is_skipped(self):
        self._write('webapp-one.desktop', _managed('One', 1))
        broken = self._write('broken.desktop', _managed('Broken', 2))
        broken.chmod(0o000)
        self.addCleanup(broken.chmod, 0o644)
        try:
            broken.read_bytes()
        except OSError:
            pass
        else:
            self.skipTest('files cannot be made unreadable here (running as root?)')

        found = list_managed_desktop_files(ENGINES)

        self.assertEqual([item['title'] for item in found], ['One'])

    def test_missing_directory_returns_empty(self):
        with mock.patch.object(desktop_entries, 'APPLICATIONS_DIR', self.applications_dir / 'gone'):
            self.assertEqual(list_managed_desktop_files(ENGINES), [])


if __name__ == '__main__':
    unittest.main()

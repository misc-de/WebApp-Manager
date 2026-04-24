import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.export_ff.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import desktop_entries
import launcher_wrapper
from webapp_constants import ADDRESS_KEY, MODE_DESKTOP_KEY, MODE_MOBILE_KEY


def _stub_launch_spec(*_args, mode_override=None, **_kwargs):
    mode = (mode_override or 'standard').lower()
    argv = ['firefox']
    if mode == 'kiosk':
        argv.append('--kiosk')
    elif mode == 'seamless':
        argv.extend(['--app=https://example.com/', '--start-fullscreen'])
    elif mode == 'app':
        argv.append('--app=https://example.com/')
    argv.append('https://example.com/')
    return {
        'argv': argv,
        'normalized_address': 'https://example.com/',
        'profile_info': {'browser_family': 'firefox', 'profile_path': '/tmp/fake', 'profile_name': 'fake'},
        'engine_command': '/usr/bin/firefox',
        'window_identity': 'example-com',
    }


class ExportFormFactorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._applications_dir = root / 'applications'
        self._applications_dir.mkdir()
        self._launcher_dir = root / 'launchers'

        self._patches = [
            mock.patch.object(desktop_entries, 'APPLICATIONS_DIR', self._applications_dir),
            mock.patch.object(desktop_entries, 'build_launch_command', side_effect=_stub_launch_spec),
            mock.patch.object(desktop_entries, 'ensure_applications_dir', lambda: self._applications_dir.mkdir(parents=True, exist_ok=True)),
            mock.patch.object(desktop_entries, 'delete_managed_entry_artifacts', lambda *a, **kw: None),
            mock.patch.object(launcher_wrapper, 'LAUNCHER_DIR', self._launcher_dir),
        ]
        for patcher in self._patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _make_entry(self, title='MyApp'):
        return SimpleNamespace(id=1, title=title, description='', active=True)

    def _read_exec(self, desktop_path):
        for line in desktop_path.read_text(encoding='utf-8').splitlines():
            if line.startswith('Exec='):
                return line[len('Exec='):]
        return ''

    def test_same_modes_produce_direct_exec_no_wrapper(self):
        entry = self._make_entry()
        options = {
            ADDRESS_KEY: 'https://example.com/',
            'EngineID': '1',
            MODE_MOBILE_KEY: 'seamless',
            MODE_DESKTOP_KEY: 'seamless',
        }
        engines = [{'id': 1, 'name': 'Firefox', 'command': 'firefox'}]

        result = desktop_entries.export_desktop_file(entry, options, engines, _build_test_logger('same'))

        self.assertIsNotNone(result)
        exec_line = self._read_exec(result['desktop_path'])
        self.assertIn('firefox', exec_line)
        self.assertNotIn(str(self._launcher_dir), exec_line)
        self.assertFalse(any(self._launcher_dir.glob('*.sh')) if self._launcher_dir.exists() else False)

    def test_different_modes_create_wrapper_and_exec_points_to_it(self):
        entry = self._make_entry()
        options = {
            ADDRESS_KEY: 'https://example.com/',
            'EngineID': '1',
            MODE_MOBILE_KEY: 'seamless',
            MODE_DESKTOP_KEY: 'standard',
        }
        engines = [{'id': 1, 'name': 'Firefox', 'command': 'firefox'}]

        result = desktop_entries.export_desktop_file(entry, options, engines, _build_test_logger('diff'))

        self.assertIsNotNone(result)
        exec_line = self._read_exec(result['desktop_path'])
        self.assertTrue(exec_line.startswith(str(self._launcher_dir)) or str(self._launcher_dir) in exec_line)
        wrappers = list(self._launcher_dir.glob('*.sh'))
        self.assertEqual(len(wrappers), 1)
        wrapper_content = wrappers[0].read_text(encoding='utf-8')
        self.assertIn('--app=https://example.com/', wrapper_content)
        self.assertIn('case "$FORM" in', wrapper_content)

    def test_reverting_to_same_mode_deletes_wrapper(self):
        entry = self._make_entry()
        engines = [{'id': 1, 'name': 'Firefox', 'command': 'firefox'}]

        # First export: divergent modes → wrapper exists
        desktop_entries.export_desktop_file(
            entry,
            {ADDRESS_KEY: 'https://example.com/', 'EngineID': '1', MODE_MOBILE_KEY: 'seamless', MODE_DESKTOP_KEY: 'standard'},
            engines,
            _build_test_logger('revert-1'),
        )
        self.assertEqual(len(list(self._launcher_dir.glob('*.sh'))), 1)

        # Second export: converged → wrapper removed
        desktop_entries.export_desktop_file(
            entry,
            {ADDRESS_KEY: 'https://example.com/', 'EngineID': '1', MODE_MOBILE_KEY: 'standard', MODE_DESKTOP_KEY: 'standard'},
            engines,
            _build_test_logger('revert-2'),
        )
        self.assertEqual(len(list(self._launcher_dir.glob('*.sh'))), 0)


if __name__ == '__main__':
    unittest.main()

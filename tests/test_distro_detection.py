"""Distribution detection, including the sandboxed case.

Inside a Flatpak /etc/os-release describes the runtime, not the machine. That
made a sandboxed run on FuriOS report "not FuriOS" and silently skip the
FuriOS-specific Firefox settings (browser_settings) and the background-mode
option (detail_page.options). Found by installing the aarch64 package on an
actual FuriOS phone.
"""
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = lambda name: __import__('logging').getLogger('test.distro')
sys.modules.setdefault('logger_setup', fake_logger_setup)

import distro_utils

FURIOS = 'NAME="FuriOS"\nID=furios\nID_LIKE=debian\nPRETTY_NAME="FuriOS forky"\n'
RUNTIME = 'NAME="GNOME Platform"\nID=org.gnome.Platform\nVERSION_ID=49\n'
DEBIAN = 'NAME="Debian GNU/Linux"\nID=debian\nPRETTY_NAME="Debian GNU/Linux 13"\n'


class OsReleaseSourceTests(unittest.TestCase):
    def setUp(self):
        distro_utils.os_release_data.cache_clear()
        distro_utils._os_release_text.cache_clear()
        distro_utils.is_furios_distribution.cache_clear()

    tearDown = setUp

    def _paths(self, tmpdir, host=None, etc=None):
        """Builds the lookup tuple in the module's own order."""
        paths = []
        host_path = Path(tmpdir) / 'run-host-os-release'
        etc_path = Path(tmpdir) / 'etc-os-release'
        if host is not None:
            host_path.write_text(host, encoding='utf-8')
        if etc is not None:
            etc_path.write_text(etc, encoding='utf-8')
        paths.append(host_path)
        paths.append(etc_path)
        return tuple(paths)

    def test_host_file_wins_over_the_runtime_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._paths(tmpdir, host=FURIOS, etc=RUNTIME)
            with mock.patch.object(distro_utils, '_OS_RELEASE_PATHS', paths):
                self.assertTrue(distro_utils.is_furios_distribution())
                self.assertEqual(distro_utils.os_release_data()['ID'], 'furios')

    def test_falls_back_to_etc_when_not_sandboxed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._paths(tmpdir, host=None, etc=FURIOS)
            with mock.patch.object(distro_utils, '_OS_RELEASE_PATHS', paths):
                self.assertTrue(distro_utils.is_furios_distribution())

    def test_other_distributions_are_not_furios(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._paths(tmpdir, host=DEBIAN, etc=RUNTIME)
            with mock.patch.object(distro_utils, '_OS_RELEASE_PATHS', paths):
                self.assertFalse(distro_utils.is_furios_distribution())

    def test_runtime_only_is_not_mistaken_for_a_distribution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._paths(tmpdir, host=None, etc=RUNTIME)
            with mock.patch.object(distro_utils, '_OS_RELEASE_PATHS', paths):
                self.assertFalse(distro_utils.is_furios_distribution())
                self.assertEqual(distro_utils.os_release_data()['ID'], 'org.gnome.Platform')

    def test_missing_files_are_survivable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._paths(tmpdir)
            with mock.patch.object(distro_utils, '_OS_RELEASE_PATHS', paths):
                self.assertEqual(distro_utils.os_release_data(), {})
                self.assertFalse(distro_utils.is_furios_distribution())


class LookupOrderTests(unittest.TestCase):
    def test_run_host_is_consulted_first(self):
        self.assertEqual(distro_utils._OS_RELEASE_PATHS[0], Path('/run/host/os-release'))
        self.assertIn(Path('/etc/os-release'), distro_utils._OS_RELEASE_PATHS)


if __name__ == '__main__':
    unittest.main()

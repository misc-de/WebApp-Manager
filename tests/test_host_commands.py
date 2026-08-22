"""Tests for the sandbox/host boundary.

Outside a Flatpak every helper must be a pure pass-through, so the native run
is bit-for-bit what it was. Inside one, lookups and launches have to be routed
through flatpak-spawn -- with a working directory the host can actually enter.
"""
import logging
import subprocess
import sys
import types
import unittest
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.host_commands.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import host_commands


class NativeRunTests(unittest.TestCase):
    def setUp(self):
        host_commands.running_in_flatpak.cache_clear()
        host_commands.host_which.cache_clear()

    tearDown = setUp

    def test_argv_is_untouched_outside_a_sandbox(self):
        with mock.patch.object(host_commands, 'running_in_flatpak', return_value=False):
            self.assertEqual(host_commands.host_argv(['firefox', '--x']), ['firefox', '--x'])

    def test_lookup_uses_shutil_which_outside_a_sandbox(self):
        with mock.patch.object(host_commands, 'running_in_flatpak', return_value=False), \
            mock.patch('host_commands.shutil.which', return_value='/usr/bin/firefox') as which:
            self.assertEqual(host_commands.host_which('firefox'), '/usr/bin/firefox')
        which.assert_called_once_with('firefox')

    def test_empty_command_never_reaches_a_lookup(self):
        with mock.patch('host_commands.shutil.which') as which:
            self.assertIsNone(host_commands.host_which(''))
            self.assertIsNone(host_commands.host_which(None))
        which.assert_not_called()


class SandboxedRunTests(unittest.TestCase):
    def setUp(self):
        host_commands.running_in_flatpak.cache_clear()
        host_commands.host_which.cache_clear()
        patcher = mock.patch.object(host_commands, 'running_in_flatpak', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(host_commands.host_which.cache_clear)

    def test_argv_is_wrapped_in_flatpak_spawn(self):
        self.assertEqual(
            host_commands.host_argv(['firefox', '--profile', '/p']),
            ['flatpak-spawn', '--host', 'firefox', '--profile', '/p'],
        )

    def test_env_overrides_are_forwarded_as_flags(self):
        argv = host_commands.host_argv(['firefox'], env_overrides={'MOZ_ENABLE_WAYLAND': '1', 'GDK_BACKEND': 'wayland'})
        self.assertEqual(
            argv,
            ['flatpak-spawn', '--host', '--env=GDK_BACKEND=wayland', '--env=MOZ_ENABLE_WAYLAND=1', 'firefox'],
        )

    def test_empty_argv_is_not_wrapped(self):
        self.assertEqual(host_commands.host_argv([]), [])

    def test_lookup_runs_on_the_host_from_a_directory_the_host_has(self):
        """Regression: the app's own directory is /app/... inside the sandbox
        and does not exist on the host. Inheriting it made flatpak-spawn fail,
        so every browser looked uninstalled."""
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout='/usr/bin/firefox\n', stderr='')
        with mock.patch('host_commands.subprocess.run', return_value=completed) as run:
            self.assertEqual(host_commands.host_which('firefox'), '/usr/bin/firefox')

        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ['flatpak-spawn', '--host'])
        self.assertEqual(argv[-1], 'firefox', 'the command must be an argument, never interpolated into the script')
        self.assertEqual(run.call_args.kwargs['cwd'], '/')

    def test_failed_lookup_returns_none(self):
        completed = subprocess.CompletedProcess(args=[], returncode=1, stdout='', stderr='not found')
        with mock.patch('host_commands.subprocess.run', return_value=completed):
            self.assertIsNone(host_commands.host_which('nope'))

    def test_lookup_survives_a_broken_spawn(self):
        for failure in (OSError('no flatpak-spawn'), subprocess.TimeoutExpired(cmd='x', timeout=5)):
            host_commands.host_which.cache_clear()
            with self.subTest(failure=type(failure).__name__), \
                mock.patch('host_commands.subprocess.run', side_effect=failure):
                self.assertIsNone(host_commands.host_which('firefox'))

    def test_lookup_result_is_cached(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout='/usr/bin/firefox\n', stderr='')
        with mock.patch('host_commands.subprocess.run', return_value=completed) as run:
            host_commands.host_which('firefox')
            host_commands.host_which('firefox')
        self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()

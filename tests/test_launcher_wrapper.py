import logging
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.launcher.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import launcher_wrapper


class RenderWrapperTests(unittest.TestCase):
    def test_contains_form_factor_switch(self):
        content = launcher_wrapper.render_wrapper(['firefox', '--kiosk', 'https://x/'], ['firefox', 'https://x/'])
        self.assertIn('case "$FORM" in', content)
        self.assertIn('mobile)', content)
        self.assertIn('firefox --kiosk', content)
        self.assertIn('firefox https://x/', content)

    def test_detects_phosh(self):
        content = launcher_wrapper.render_wrapper(['a'], ['b'])
        # case-insensitive bracket pattern: *[Pp]hosh*
        self.assertRegex(content, r'\[Pp\]hosh')

    def test_detects_plasma_mobile(self):
        content = launcher_wrapper.render_wrapper(['a'], ['b'])
        self.assertIn('plasma-mobile', content)

    def test_respects_webapp_form_override(self):
        content = launcher_wrapper.render_wrapper(['a'], ['b'])
        self.assertIn('WEBAPP_FORM', content)

    def test_detects_furios(self):
        content = launcher_wrapper.render_wrapper(['a'], ['b'])
        self.assertIn('furios', content.lower())

    def test_argvs_are_shell_escaped(self):
        """URLs with shell metacharacters must be quoted."""
        content = launcher_wrapper.render_wrapper(
            ['firefox', 'https://example.com/?foo=bar&baz=qux'],
            ['firefox', 'https://example.com/?foo=bar&baz=qux'],
        )
        # shlex.join wraps the ampersand URL in single quotes
        self.assertIn("'https://example.com/?foo=bar&baz=qux'", content)


class WriteWrapperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = launcher_wrapper.LAUNCHER_DIR
        launcher_wrapper.LAUNCHER_DIR = Path(self._tmp.name)

    def tearDown(self):
        launcher_wrapper.LAUNCHER_DIR = self._orig_dir
        self._tmp.cleanup()

    def test_write_creates_file(self):
        path = launcher_wrapper.write_wrapper('slug', ['a', 'b'], ['c', 'd'])
        self.assertTrue(path.exists())
        self.assertTrue(path.is_file())

    def test_written_script_is_executable_by_owner(self):
        path = launcher_wrapper.write_wrapper('slug', ['a'], ['b'])
        mode = path.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR, 'owner execute bit must be set')

    def test_write_is_idempotent(self):
        path = launcher_wrapper.write_wrapper('slug', ['a'], ['b'])
        mtime_first = path.stat().st_mtime_ns
        launcher_wrapper.write_wrapper('slug', ['a'], ['b'])
        mtime_second = path.stat().st_mtime_ns
        self.assertEqual(mtime_first, mtime_second, 'unchanged content must not rewrite file')

    def test_write_rewrites_on_change(self):
        path = launcher_wrapper.write_wrapper('slug', ['a'], ['b'])
        first = path.read_text(encoding='utf-8')
        launcher_wrapper.write_wrapper('slug', ['a', 'xx'], ['b'])
        second = path.read_text(encoding='utf-8')
        self.assertNotEqual(first, second)

    def test_write_rejects_empty_slug(self):
        with self.assertRaises(ValueError):
            launcher_wrapper.write_wrapper('', ['a'], ['b'])

    def test_delete_wrapper_removes_file(self):
        path = launcher_wrapper.write_wrapper('slug', ['a'], ['b'])
        self.assertTrue(launcher_wrapper.delete_wrapper('slug'))
        self.assertFalse(path.exists())

    def test_delete_missing_wrapper_returns_false(self):
        self.assertFalse(launcher_wrapper.delete_wrapper('ghost'))

    def test_delete_empty_slug_returns_false(self):
        self.assertFalse(launcher_wrapper.delete_wrapper(''))

    def test_cleanup_orphaned_wrappers_keeps_active(self):
        launcher_wrapper.write_wrapper('active', ['a'], ['b'])
        launcher_wrapper.write_wrapper('stale', ['a'], ['b'])
        removed = launcher_wrapper.cleanup_orphaned_wrappers({'active'})
        remaining = {p.stem for p in launcher_wrapper.list_wrappers()}
        self.assertEqual(remaining, {'active'})
        self.assertEqual([p.stem for p in removed], ['stale'])


if __name__ == '__main__':
    unittest.main()

"""Coverage-driven tests for the real logger_setup module.

Every other test file replaces ``logger_setup`` in ``sys.modules`` with a stub,
so this file loads the real source under a private module name, with
XDG_STATE_HOME pointed at a temp directory. The log file therefore never lands
in the user's real state directory, and the stub other files rely on stays in
place.
"""
import importlib.util
import logging
import os
import tempfile
import unittest
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest import mock

_SOURCE = Path(__file__).resolve().parent.parent / 'logger_setup.py'


def _load_real_logger_setup(state_home: str):
    name = f'_real_logger_setup_{uuid.uuid4().hex}'
    spec = importlib.util.spec_from_file_location(name, _SOURCE)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, {'XDG_STATE_HOME': state_home}):
        spec.loader.exec_module(module)
    return module


class _LoggerSetupCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.module = _load_real_logger_setup(self._tmp.name)
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        for var in ('WEBAPP_MANAGER_LOG_LEVEL', 'WEBAPP_MANAGER_LOG_MAX_BYTES', 'WEBAPP_MANAGER_LOG_BACKUP_COUNT'):
            os.environ.pop(var, None)

    def fresh_logger(self):
        name = f'test.cov_logger_setup.{uuid.uuid4().hex}'
        logger = self.module.get_logger(name)
        self.addCleanup(self._dispose, logger)
        return logger

    @staticmethod
    def _dispose(logger):
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


class ImportTests(_LoggerSetupCase):
    def test_log_dir_follows_xdg_state_home(self):
        expected = Path(self._tmp.name) / 'webapp' / 'app.log'
        self.assertEqual(self.module.get_log_file_path(), expected)
        self.assertTrue(expected.parent.is_dir())


class LevelResolutionTests(_LoggerSetupCase):
    def test_default_is_info(self):
        self.assertEqual(self.module._resolve_log_level(), logging.INFO)

    def test_names_and_numbers_are_accepted(self):
        os.environ['WEBAPP_MANAGER_LOG_LEVEL'] = ' debug '
        self.assertEqual(self.module._resolve_log_level(), logging.DEBUG)
        os.environ['WEBAPP_MANAGER_LOG_LEVEL'] = '35'
        self.assertEqual(self.module._resolve_log_level(), 35)

    def test_unknown_name_falls_back_to_info(self):
        os.environ['WEBAPP_MANAGER_LOG_LEVEL'] = 'loud'
        self.assertEqual(self.module._resolve_log_level(), logging.INFO)


class IntEnvTests(_LoggerSetupCase):
    def test_missing_and_invalid_values_use_the_default(self):
        self.assertEqual(self.module._resolve_int_env('WEBAPP_MANAGER_LOG_MAX_BYTES', 123), 123)
        os.environ['WEBAPP_MANAGER_LOG_MAX_BYTES'] = 'lots'
        self.assertEqual(self.module._resolve_int_env('WEBAPP_MANAGER_LOG_MAX_BYTES', 123), 123)

    def test_values_are_clamped_to_the_minimum(self):
        os.environ['WEBAPP_MANAGER_LOG_MAX_BYTES'] = '10'
        self.assertEqual(self.module._resolve_int_env('WEBAPP_MANAGER_LOG_MAX_BYTES', 123, minimum=1024), 1024)
        os.environ['WEBAPP_MANAGER_LOG_MAX_BYTES'] = '4096'
        self.assertEqual(self.module._resolve_int_env('WEBAPP_MANAGER_LOG_MAX_BYTES', 123, minimum=1024), 4096)


class GetLoggerTests(_LoggerSetupCase):
    def test_logger_gets_rotating_file_and_stream_handler(self):
        os.environ['WEBAPP_MANAGER_LOG_LEVEL'] = 'WARNING'
        os.environ['WEBAPP_MANAGER_LOG_MAX_BYTES'] = '2048'
        os.environ['WEBAPP_MANAGER_LOG_BACKUP_COUNT'] = '-4'
        logger = self.fresh_logger()
        self.assertEqual(logger.level, logging.WARNING)
        self.assertFalse(logger.propagate)
        file_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        stream_handlers = [h for h in logger.handlers if type(h) is logging.StreamHandler]
        self.assertEqual(len(file_handlers), 1)
        self.assertEqual(len(stream_handlers), 1)
        self.assertEqual(file_handlers[0].maxBytes, 2048)
        self.assertEqual(file_handlers[0].backupCount, 0)
        self.assertEqual(Path(file_handlers[0].baseFilename), self.module.LOG_FILE)

    def test_defaults_without_environment(self):
        logger = self.fresh_logger()
        file_handler = next(h for h in logger.handlers if isinstance(h, RotatingFileHandler))
        self.assertEqual(file_handler.maxBytes, self.module.DEFAULT_LOG_MAX_BYTES)
        self.assertEqual(file_handler.backupCount, self.module.DEFAULT_LOG_BACKUP_COUNT)

    def test_messages_reach_the_log_file(self):
        logger = self.fresh_logger()
        stream_handler = next(h for h in logger.handlers if type(h) is logging.StreamHandler)
        stream_handler.setLevel(logging.CRITICAL + 1)  # keep test output quiet
        logger.info('hello %s', 'file')
        for handler in logger.handlers:
            handler.flush()
        text = self.module.LOG_FILE.read_text(encoding='utf-8')
        self.assertRegex(text, r'INFO \[test\.cov_logger_setup\.[0-9a-f]+\] hello file')

    def test_second_call_reuses_the_configured_logger(self):
        logger = self.fresh_logger()
        handlers = list(logger.handlers)
        self.assertIs(self.module.get_logger(logger.name), logger)
        self.assertEqual(logger.handlers, handlers)


if __name__ == '__main__':
    unittest.main()

"""Coverage-driven tests for database: migrations, backups and failure paths."""
import logging
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_database.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import database
from database import Database


class _FailingCursor:
    """Stands in for the sqlite cursor and fails every statement."""

    rowcount = 0
    lastrowid = None

    def execute(self, *args, **kwargs):
        raise sqlite3.OperationalError('disk I/O error')


def _raw_options(db, entry_id=None):
    sql = 'SELECT entry_id, option_key, option_value FROM options'
    params = ()
    if entry_id is not None:
        sql += ' WHERE entry_id=?'
        params = (entry_id,)
    return sorted(db.conn.execute(sql + ' ORDER BY id', params).fetchall(), key=lambda row: (row[0], str(row[1])))


class _DbCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / 'nested' / 'dir' / 'test.db'

    def open_expecting_failure(self):
        """Construct a Database that must fail, and check it closed its connection."""
        connections = []
        real_connect = sqlite3.connect

        def connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connections.append(connection)
            return connection

        try:
            with mock.patch.object(database.sqlite3, 'connect', side_effect=connect):
                Database(str(self.db_path))
        finally:
            for connection in connections:
                # A failed construction must not leave its connection open.
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute('SELECT 1')

    def open(self):
        db = Database(str(self.db_path))
        self.addCleanup(db.close)
        return db


class ConstructionTests(_DbCase):
    def test_missing_parent_directories_are_created(self):
        db = self.open()
        self.assertTrue(self.db_path.exists())
        self.assertEqual(db.db_name, str(self.db_path))

    def test_connection_pragmas_are_set(self):
        db = self.open()
        self.assertEqual(db.conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        self.assertEqual(db.conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')


class MigrationTests(_DbCase):
    def _v2(self, cursor):
        cursor.execute('ALTER TABLE entries ADD COLUMN extra TEXT')

    def test_upgrade_writes_a_backup_first_and_bumps_the_version(self):
        db = self.open()
        db.add_entry('Before upgrade')
        db.close()
        with mock.patch.object(database, 'SCHEMA_VERSION', 2), \
                mock.patch.dict(database.MIGRATIONS, {2: self._v2}):
            upgraded = self.open()
            self.assertEqual(upgraded.schema_version(), 2)
        backups = list(self.db_path.parent.glob('test.db.bak-v1-to-v2-*'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(str(backups[0]))) as backup:
            self.assertEqual(backup.execute('PRAGMA user_version').fetchone()[0], 1)
        columns = [row[1] for row in upgraded.conn.execute('PRAGMA table_info(entries)')]
        self.assertIn('extra', columns)

    def test_failed_backup_does_not_block_the_upgrade(self):
        self.open().close()
        with mock.patch.object(database, 'SCHEMA_VERSION', 2), \
                mock.patch.dict(database.MIGRATIONS, {2: self._v2}), \
                mock.patch.object(database.shutil, 'copy2', side_effect=OSError('read-only')), \
                self.assertLogs(database.LOG, level='WARNING') as logs:
            upgraded = self.open()
            self.assertEqual(upgraded.schema_version(), 2)
        self.assertIn('Failed to back up', logs.output[0])

    def test_in_memory_database_is_never_backed_up(self):
        db = Database(':memory:')
        self.addCleanup(db.close)
        with mock.patch.object(database.shutil, 'copy2') as copy:
            db._backup_before_migration(1, 2)
        copy.assert_not_called()

    def test_missing_migration_step_is_an_error(self):
        self.open().close()
        with mock.patch.object(database, 'SCHEMA_VERSION', 2), \
                self.assertRaisesRegex(RuntimeError, 'schema version 2'):
            self.open_expecting_failure()

    def test_failing_migration_is_rolled_back(self):
        self.open().close()

        def broken(cursor):
            cursor.execute('CREATE TABLE half_done (x)')
            raise sqlite3.OperationalError('boom')

        with mock.patch.object(database, 'SCHEMA_VERSION', 2), \
                mock.patch.dict(database.MIGRATIONS, {2: broken}), \
                self.assertRaises(sqlite3.OperationalError):
            self.open_expecting_failure()
        db = self.open()
        self.assertEqual(db.schema_version(), 1)
        tables = {row[0] for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('half_done', tables)


class EntryFailureTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)

    def test_add_entry_reports_failure_as_none(self):
        self.db.cursor = _FailingCursor()
        with self.assertLogs(database.LOG, level='ERROR'):
            self.assertIsNone(self.db.add_entry('x'))

    def test_update_description_only(self):
        entry_id = self.db.add_entry('Title', 'old')
        self.assertTrue(self.db.update_entry(entry_id, description='new'))
        self.assertEqual(self.db.get_entry(entry_id), (entry_id, 'Title', 'new', 1))

    def test_update_of_missing_entry_returns_false(self):
        self.assertFalse(self.db.update_entry(999, title='x'))

    def test_failed_delete_rolls_back_and_reraises(self):
        entry_id = self.db.add_entry('Keep me')
        real_cursor = self.db.cursor
        self.db.cursor = _FailingCursor()
        with self.assertLogs(database.LOG, level='ERROR'), self.assertRaises(sqlite3.OperationalError):
            self.db.delete_entry(entry_id)
        self.db.cursor = real_cursor
        self.assertIsNotNone(self.db.get_entry(entry_id))
        self.assertFalse(self.db.conn.in_transaction)


class OptionTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.entry_id = self.db.add_entry('Entry')

    def test_add_option_without_commit_leaves_transaction_open(self):
        self.db.add_option(self.entry_id, 'Address', 'https://a.example/', commit=False)
        self.assertTrue(self.db.conn.in_transaction)
        self.db.conn.rollback()
        self.assertEqual(self.db.get_options_for_entry(self.entry_id), [])

    def test_add_option_stores_under_the_canonical_key(self):
        self.db.add_option(self.entry_id, 'Keep Session', '1')
        self.assertEqual(_raw_options(self.db), [(self.entry_id, 'Previous Session', '1')])

    def test_failed_bulk_write_leaves_nothing_behind(self):
        self.db.add_option(self.entry_id, 'Notifications', '0')
        calls = []

        def flaky(entry_id, key, value):
            calls.append(key)
            if len(calls) == 2:
                raise sqlite3.IntegrityError('constraint')
            real_upsert(entry_id, key, value)

        real_upsert = self.db._upsert_option
        with mock.patch.object(self.db, '_upsert_option', side_effect=flaky), \
                self.assertRaises(sqlite3.IntegrityError):
            self.db.add_options(self.entry_id, {'Notifications': '1', 'Address': 'x'})
        self.assertEqual(_raw_options(self.db), [(self.entry_id, 'Notifications', '0')])


class CanonicalizationTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.first = self.db.add_entry('First')
        self.second = self.db.add_entry('Second')

    def _insert_raw(self, entry_id, key, value):
        self.db.conn.execute(
            'INSERT INTO options (entry_id, option_key, option_value) VALUES (?, ?, ?)',
            (entry_id, key, value),
        )
        self.db.conn.commit()

    def test_per_entry_rewrite_uses_the_newest_value_and_leaves_others_alone(self):
        self._insert_raw(self.first, 'Previous Session', '0')
        self._insert_raw(self.first, 'Keep Session', '1')
        self._insert_raw(self.second, 'Keep Session', '1')
        self.db.canonicalize_option_keys(self.first)
        self.assertEqual(_raw_options(self.db, self.first), [(self.first, 'Previous Session', '1')])
        self.assertEqual(_raw_options(self.db, self.second), [(self.second, 'Keep Session', '1')])

    def test_null_key_alone_does_not_force_a_rewrite(self):
        self._insert_raw(self.first, None, None)
        self.db.canonicalize_option_keys()
        self.assertEqual(_raw_options(self.db), [(self.first, None, None)])

    def test_rewrite_stores_null_key_and_value_as_empty_strings(self):
        self._insert_raw(self.first, None, None)
        self._insert_raw(self.first, 'Keep Session', '1')
        self.db.canonicalize_option_keys()
        self.assertEqual(_raw_options(self.db), [(self.first, '', ''), (self.first, 'Previous Session', '1')])

    def test_failed_rewrite_rolls_back_the_delete(self):
        self._insert_raw(self.first, 'Keep Session', '1')
        with mock.patch.object(self.db, '_upsert_option', side_effect=sqlite3.OperationalError('full')), \
                self.assertRaises(sqlite3.OperationalError):
            self.db.canonicalize_option_keys()
        self.assertEqual(_raw_options(self.db), [(self.first, 'Keep Session', '1')])


if __name__ == '__main__':
    unittest.main()

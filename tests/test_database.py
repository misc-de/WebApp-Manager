import logging
import sys
import types
import unittest
from pathlib import Path
import tempfile


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.database.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

from database import Database, MIGRATIONS, SCHEMA_VERSION


def _make_memory_db():
    return Database(':memory:')


class DatabaseCrudTests(unittest.TestCase):
    def test_add_entry_returns_id(self):
        db = _make_memory_db()
        entry_id = db.add_entry('Example', 'Description')
        self.assertIsInstance(entry_id, int)
        self.assertGreater(entry_id, 0)

    def test_list_entries_sorted_case_insensitive(self):
        db = _make_memory_db()
        db.add_entry('charlie')
        db.add_entry('Alpha')
        db.add_entry('bravo')
        titles = [row[1] for row in db.list_entries()]
        self.assertEqual(titles, ['Alpha', 'bravo', 'charlie'])

    def test_update_entry_partial_fields(self):
        db = _make_memory_db()
        entry_id = db.add_entry('Original', 'desc')
        db.update_entry(entry_id, title='Renamed')
        row = db.get_entry(entry_id)
        self.assertEqual(row[1], 'Renamed')
        self.assertEqual(row[2], 'desc')

    def test_update_entry_active_flag(self):
        db = _make_memory_db()
        entry_id = db.add_entry('X')
        db.update_entry(entry_id, active=False)
        self.assertEqual(db.get_entry(entry_id)[3], 0)
        db.update_entry(entry_id, active=True)
        self.assertEqual(db.get_entry(entry_id)[3], 1)

    def test_update_entry_noop_without_fields(self):
        db = _make_memory_db()
        entry_id = db.add_entry('X')
        self.assertFalse(db.update_entry(entry_id))

    def test_delete_entry_cascades_options(self):
        db = _make_memory_db()
        entry_id = db.add_entry('ToDelete')
        db.add_option(entry_id, 'Address', 'https://example.com/')
        db.add_option(entry_id, 'Notifications', '1')
        self.assertEqual(len(db.get_options_for_entry(entry_id)), 2)
        self.assertTrue(db.delete_entry(entry_id))
        self.assertEqual(db.get_options_for_entry(entry_id), [])
        self.assertIsNone(db.get_entry(entry_id))

    def test_delete_missing_entry_returns_false(self):
        db = _make_memory_db()
        self.assertFalse(db.delete_entry(9999))


class DatabaseOptionsTests(unittest.TestCase):
    def test_add_option_upserts_on_duplicate_key(self):
        db = _make_memory_db()
        entry_id = db.add_entry('X')
        db.add_option(entry_id, 'Address', 'https://a.example/')
        db.add_option(entry_id, 'Address', 'https://b.example/')
        rows = db.get_options_for_entry(entry_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], 'https://b.example/')

    def test_add_options_writes_all_keys_transactionally(self):
        db = _make_memory_db()
        entry_id = db.add_entry('X')
        db.add_options(entry_id, {'Address': 'https://x/', 'Notifications': '1'})
        values = {row[2]: row[3] for row in db.get_options_for_entry(entry_id)}
        self.assertEqual(values, {'Address': 'https://x/', 'Notifications': '1'})

    def test_list_option_values_returns_four_tuples(self):
        db = _make_memory_db()
        entry_id = db.add_entry('X')
        db.add_option(entry_id, 'Address', 'https://x/')
        rows = db.list_option_values()
        self.assertEqual(len(rows), 1)
        row_id, ref_entry_id, key, value = rows[0]
        self.assertIsInstance(row_id, int)
        self.assertEqual(ref_entry_id, entry_id)
        self.assertEqual(key, 'Address')
        self.assertEqual(value, 'https://x/')


class DatabaseCanonicalizationTests(unittest.TestCase):
    def _row_ids_for_entry(self, db, entry_id):
        return sorted(row[0] for row in db.get_options_for_entry(entry_id))

    def test_no_rewrite_when_already_canonical(self):
        """Perf-gate: a clean DB must not re-insert rows on reopen."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / 'canon.db'
            db = Database(str(db_path))
            entry_id = db.add_entry('Canon')
            db.add_options(entry_id, {
                'Address': 'https://canon.example/',
                'Notifications': '1',
                'Previous Session': '0',
            })
            initial_ids = self._row_ids_for_entry(db, entry_id)
            db.close()

            reopened = Database(str(db_path))
            reopened_ids = self._row_ids_for_entry(reopened, entry_id)
            reopened.close()

        self.assertEqual(initial_ids, reopened_ids, 'row ids must be preserved when nothing has to be canonicalized')

    def test_canonicalize_drops_alias_duplicates(self):
        """An alias stored alongside the canonical key collapses to one row."""
        db = _make_memory_db()
        entry_id = db.add_entry('Alias')
        db.cursor.execute(
            'INSERT INTO options (entry_id, option_key, option_value) VALUES (?, ?, ?)',
            (entry_id, 'Keep Session', '1'),
        )
        db.conn.commit()
        db.canonicalize_option_keys()

        rows = db.get_options_for_entry(entry_id)
        keys = {row[2] for row in rows}
        self.assertIn('Previous Session', keys)
        self.assertNotIn('Keep Session', keys)

    def test_canonicalize_preserves_value(self):
        db = _make_memory_db()
        entry_id = db.add_entry('AliasValue')
        db.cursor.execute(
            'INSERT INTO options (entry_id, option_key, option_value) VALUES (?, ?, ?)',
            (entry_id, 'Allow Notifications', '1'),
        )
        db.conn.commit()
        db.canonicalize_option_keys()

        rows = db.get_options_for_entry(entry_id)
        values = {row[2]: row[3] for row in rows}
        self.assertEqual(values.get('Notifications'), '1')

    def test_canonicalize_per_entry_does_not_touch_others(self):
        db = _make_memory_db()
        entry_a = db.add_entry('A')
        entry_b = db.add_entry('B')
        db.add_option(entry_a, 'Address', 'https://a/')
        db.add_option(entry_b, 'Address', 'https://b/')
        entry_b_ids = self._row_ids_for_entry(db, entry_b)

        db.canonicalize_option_keys(entry_id=entry_a)

        self.assertEqual(self._row_ids_for_entry(db, entry_b), entry_b_ids)


class DatabaseMigrationTests(unittest.TestCase):
    def test_fresh_db_is_at_current_schema_version(self):
        db = _make_memory_db()
        self.assertEqual(db.schema_version(), SCHEMA_VERSION)

    def test_migration_dict_contains_all_versions(self):
        for version in range(1, SCHEMA_VERSION + 1):
            self.assertIn(version, MIGRATIONS, f'missing migration for version {version}')

    def test_migrations_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / 'mig.db'
            db = Database(str(db_path))
            entry_id = db.add_entry('Migration', 'desc')
            db.add_option(entry_id, 'Address', 'https://migration.example/')
            db.close()

            reopened = Database(str(db_path))
            self.assertEqual(reopened.schema_version(), SCHEMA_VERSION)
            rows = reopened.get_options_for_entry(entry_id)
            reopened.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], 'Address')

    def test_zero_version_db_is_initialized(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / 'empty.db'
            connection = __import__('sqlite3').connect(str(db_path))
            connection.execute('PRAGMA user_version = 0')
            connection.close()

            db = Database(str(db_path))
            self.assertEqual(db.schema_version(), SCHEMA_VERSION)
            self.assertIsNotNone(db.add_entry('AfterInit'))
            db.close()


if __name__ == '__main__':
    unittest.main()

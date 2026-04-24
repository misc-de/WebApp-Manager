import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from browser_option_logic import option_key_from_any
from logger_setup import get_logger

LOG = get_logger(__name__)

SCHEMA_VERSION = 1


def _migration_v1(cursor):
    cursor.execute(
        '''CREATE TABLE IF NOT EXISTS entries (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            title TEXT,
                            description TEXT,
                            active INTEGER DEFAULT 1
                        )'''
    )
    cursor.execute(
        '''CREATE TABLE IF NOT EXISTS options (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            entry_id INTEGER,
                            option_key TEXT,
                            option_value TEXT,
                            FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE
                        )'''
    )
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_options_entry_id ON options(entry_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_options_entry_key ON options(entry_id, option_key)')
    cursor.execute(
        '''DELETE FROM options
           WHERE id NOT IN (
               SELECT MAX(id) FROM options GROUP BY entry_id, option_key
           )'''
    )
    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_options_entry_key_unique ON options(entry_id, option_key)')


MIGRATIONS = {
    1: _migration_v1,
}


class Database:
    def __init__(self, db_name='webappmanager.db'):
        db_path = Path(db_name).expanduser()
        if db_path.parent and str(db_path.parent) not in ('', '.'):
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_name = str(db_path)
        self.conn = sqlite3.connect(self.db_name)
        self.conn.execute('PRAGMA foreign_keys = ON')
        self.conn.execute('PRAGMA journal_mode = WAL')
        self.conn.execute('PRAGMA synchronous = NORMAL')
        self.cursor = self.conn.cursor()
        self.apply_migrations()
        self.canonicalize_option_keys()

    def _current_user_version(self):
        return int(self.cursor.execute('PRAGMA user_version').fetchone()[0] or 0)

    def _backup_before_migration(self, from_version, to_version):
        if self.db_name in (':memory:', '') or not Path(self.db_name).exists():
            return
        timestamp = datetime.now().strftime('%Y%m%dT%H%M%S')
        backup_path = Path(f'{self.db_name}.bak-v{from_version}-to-v{to_version}-{timestamp}')
        try:
            shutil.copy2(self.db_name, backup_path)
            LOG.info('Database backup written to %s', backup_path)
        except OSError as error:
            LOG.warning('Failed to back up database before migration: %s', error)

    def apply_migrations(self):
        current = self._current_user_version()
        if current >= SCHEMA_VERSION:
            return
        if current < SCHEMA_VERSION and current > 0:
            self._backup_before_migration(current, SCHEMA_VERSION)
        for version in range(current + 1, SCHEMA_VERSION + 1):
            migration = MIGRATIONS.get(version)
            if migration is None:
                raise RuntimeError(f'Missing migration for schema version {version}')
            try:
                self.conn.execute('BEGIN')
                migration(self.cursor)
                self.cursor.execute(f'PRAGMA user_version = {int(version)}')
                self.conn.commit()
                LOG.info('Applied database migration to schema version %s', version)
            except sqlite3.Error:
                self.conn.rollback()
                raise

    def schema_version(self):
        return self._current_user_version()

    def add_entry(self, title, description=''):
        try:
            self.cursor.execute('INSERT INTO entries (title, description) VALUES (?, ?)', (title, description))
            self.conn.commit()
            return self.cursor.lastrowid
        except sqlite3.Error as error:
            LOG.error('Failed to add entry: %s', error)
            return None

    def _canonical_option_key(self, option_key):
        key = option_key_from_any(option_key)
        if key is None:
            return '' if option_key is None else str(option_key)
        return key

    def canonicalize_option_keys(self, entry_id=None):
        try:
            if entry_id is None:
                rows = self.cursor.execute('SELECT id, entry_id, option_key, option_value FROM options ORDER BY entry_id ASC, id ASC').fetchall()
            else:
                rows = self.cursor.execute('SELECT id, entry_id, option_key, option_value FROM options WHERE entry_id=? ORDER BY id ASC', (entry_id,)).fetchall()
            grouped = {}
            needs_rewrite = False
            seen_pairs = set()
            for _row_id, current_entry_id, raw_key, raw_value in rows:
                key = self._canonical_option_key(raw_key)
                raw_key_str = '' if raw_key is None else str(raw_key)
                pair = (int(current_entry_id), key)
                if key != raw_key_str or pair in seen_pairs:
                    needs_rewrite = True
                seen_pairs.add(pair)
                bucket = grouped.setdefault(int(current_entry_id), {})
                bucket[key] = '' if raw_value is None else str(raw_value)
            if not needs_rewrite:
                return
            self.conn.execute('BEGIN')
            if entry_id is None:
                self.cursor.execute('DELETE FROM options')
                for current_entry_id, options in grouped.items():
                    for option_key, option_value in options.items():
                        self._upsert_option(current_entry_id, option_key, option_value)
            else:
                self.cursor.execute('DELETE FROM options WHERE entry_id=?', (entry_id,))
                for option_key, option_value in grouped.get(int(entry_id), {}).items():
                    self._upsert_option(int(entry_id), option_key, option_value)
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()
            raise

    def _upsert_option(self, entry_id, option_key, option_value):
        option_key = self._canonical_option_key(option_key)
        self.cursor.execute(
            '''INSERT INTO options (entry_id, option_key, option_value) VALUES (?, ?, ?)
               ON CONFLICT(entry_id, option_key) DO UPDATE SET option_value=excluded.option_value''',
            (entry_id, option_key, option_value),
        )

    def add_option(self, entry_id, option_key, option_value, commit=True):
        self._upsert_option(entry_id, option_key, option_value)
        if commit:
            self.conn.commit()

    def add_options(self, entry_id, options_dict):
        try:
            self.conn.execute('BEGIN')
            for option_key, option_value in options_dict.items():
                self._upsert_option(entry_id, option_key, option_value)
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()
            raise

    def get_options_for_entry(self, entry_id):
        self.cursor.execute('SELECT * FROM options WHERE entry_id=?', (entry_id,))
        return self.cursor.fetchall()

    def list_entries(self):
        self.cursor.execute('SELECT id, title, description, active FROM entries ORDER BY title COLLATE NOCASE ASC')
        return self.cursor.fetchall()

    def list_option_values(self):
        self.cursor.execute('SELECT id, entry_id, option_key, option_value FROM options')
        return self.cursor.fetchall()

    def get_entry(self, entry_id):
        self.cursor.execute('SELECT id, title, description, active FROM entries WHERE id=?', (entry_id,))
        return self.cursor.fetchone()

    def update_entry(self, entry_id, *, title=None, description=None, active=None):
        updates = []
        values = []
        if title is not None:
            updates.append('title=?')
            values.append(title)
        if description is not None:
            updates.append('description=?')
            values.append(description)
        if active is not None:
            updates.append('active=?')
            values.append(1 if bool(active) else 0)
        if not updates:
            return False
        values.append(entry_id)
        self.cursor.execute(f"UPDATE entries SET {', '.join(updates)} WHERE id=?", tuple(values))
        self.conn.commit()
        return self.cursor.rowcount > 0

    def delete_entry(self, entry_id):
        try:
            self.conn.execute('BEGIN')
            self.cursor.execute('DELETE FROM entries WHERE id=?', (entry_id,))
            deleted = self.cursor.rowcount > 0
            self.conn.commit()
            return deleted
        except sqlite3.Error as error:
            self.conn.rollback()
            LOG.error('Failed to delete entry %s: %s', entry_id, error)
            raise

    def close(self):
        self.conn.close()

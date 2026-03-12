import sqlite3
from pathlib import Path

from browser_option_logic import option_key_from_any
from logger_setup import get_logger

LOG = get_logger(__name__)


class Database:
    def __init__(self, db_name='entries.db'):
        db_path = Path(db_name).expanduser()
        if db_path.parent and str(db_path.parent) not in ('', '.'):
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_name = str(db_path)
        self.conn = sqlite3.connect(self.db_name)
        self.conn.execute('PRAGMA foreign_keys = ON')
        self.conn.execute('PRAGMA journal_mode = WAL')
        self.conn.execute('PRAGMA synchronous = NORMAL')
        self.cursor = self.conn.cursor()
        self.create_tables()
        self.canonicalize_option_keys()

    def create_tables(self):
        self.cursor.execute(
            '''CREATE TABLE IF NOT EXISTS entries (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                title TEXT,
                                description TEXT,
                                active INTEGER DEFAULT 1
                            )'''
        )
        self.cursor.execute(
            '''CREATE TABLE IF NOT EXISTS options (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                entry_id INTEGER,
                                option_key TEXT,
                                option_value TEXT,
                                FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE
                            )'''
        )
        self.cursor.execute('CREATE INDEX IF NOT EXISTS idx_options_entry_id ON options(entry_id)')
        self.cursor.execute('CREATE INDEX IF NOT EXISTS idx_options_entry_key ON options(entry_id, option_key)')
        self.cursor.execute(
            '''DELETE FROM options
               WHERE id NOT IN (
                   SELECT MAX(id) FROM options GROUP BY entry_id, option_key
               )'''
        )
        self.cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_options_entry_key_unique ON options(entry_id, option_key)')
        self.conn.commit()

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
            for row_id, current_entry_id, raw_key, raw_value in rows:
                key = self._canonical_option_key(raw_key)
                bucket = grouped.setdefault(int(current_entry_id), {})
                bucket[key] = '' if raw_value is None else str(raw_value)
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

    def close(self):
        self.conn.close()

"""One owner for Memory DB connections, transactions and process-local locking."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from threading import RLock


class MemoryStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'memory.db'
        self._lock = RLock()

    @contextmanager
    def locked(self):
        """Serialize multi-transaction publication and in-process worker state."""
        with self._lock:
            yield

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            try:
                import sqlite_vec
                db.enable_load_extension(True)
                sqlite_vec.load(db)
            except Exception:
                pass
            finally:
                db.enable_load_extension(False)
            yield db
        finally:
            db.close()

    @contextmanager
    def read(self):
        """A consistent read snapshot; callers must not mutate it."""
        with self.locked(), self._connection() as db, db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            yield db

    @contextmanager
    def transaction(self):
        """Reserve the SQLite writer before reading state used by a mutation.

        Components participate by accepting this connection; they never commit it
        or open a nested write transaction. Exceptions roll back the entire unit.
        """
        with self.locked(), self._connection() as db, db:
            db.execute('BEGIN IMMEDIATE')
            yield db

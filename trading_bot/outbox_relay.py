"""One Telegram sender can deliver a separate futures paper outbox.

No commands, trade state or offsets are changed in the futures database.
"""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from .app import process_lock
from .testnet_observer import ObservationError


class FuturesOutboxRelay:
    delivery_label = '[Futures paper]'

    def __init__(self, path):
        path = Path(path).resolve()
        self.lock = process_lock(str(path)+'.delivery.lock')
        self.lock.__enter__()
        try:
            self.db = sqlite3.connect(path.as_uri()+'?mode=rw', uri=True, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA synchronous=FULL')
            try:
                row = self.db.execute("SELECT value FROM futures_meta WHERE key='identity'").fetchone()
                if row is None or json.loads(row[0]).get('mode') != 'futures-paper':
                    raise ObservationError('Relay requires the futures paper database')
                self.db.execute('SELECT id,message FROM runtime_outbox LIMIT 1')
            except BaseException:
                self.db.close()
                raise
        except BaseException:
            self.lock.__exit__(None,None,None)
            raise

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def close(self):
        self.db.close()
        self.lock.__exit__(None,None,None)

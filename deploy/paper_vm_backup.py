"""Consistent local backups of both paper accounts and observation history."""
import os
from pathlib import Path
import sqlite3
import tempfile

home = Path('/var/lib/tradingbot')
destination = home / 'paper-backups'
destination.mkdir(mode=0o700, exist_ok=True)
sources = [
    (home/'Trading_Bot_paper_state/runtime-paper.db', 'spot.db', 'runtime_meta'),
    (home/'Trading_Bot_futures_state/futures-paper.db', 'futures.db', 'futures_meta'),
    (home/'Trading_Bot_paper_state/monitor/observation.db', 'observation.db', 'samples'),
]
for source, name, required in sources:
    if not source.is_file():
        raise RuntimeError('Missing source: ' + str(source))
    fd, tmp = tempfile.mkstemp(prefix='.backup-', dir=destination)
    os.close(fd)
    try:
        with sqlite3.connect(source.as_uri()+'?mode=ro', uri=True) as src:
            if not src.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?', ('table', required)).fetchone():
                raise RuntimeError('Unexpected database schema: ' + str(source))
            with sqlite3.connect(tmp) as dst:
                src.backup(dst)
                if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Backup integrity failed: ' + str(source))
        with open(tmp, 'rb') as fd:
            os.fsync(fd.fileno())
        os.replace(tmp, destination/name)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

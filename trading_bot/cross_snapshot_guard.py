"""Detect financial history changes before considering snapshot restoration.

This is a necessary condition, not permission to restore. The caller must stop
and fence all writers and separately verify queue/observer state and recovery.
Read-only comparisons alone cannot fence a concurrent process or prove that
replacing a live SQLite database is safe.
"""
import sqlite3
from contextlib import closing
from itertools import zip_longest
from pathlib import Path


def _open(path):
    return sqlite3.connect(Path(path).resolve(strict=True).as_uri()+'?mode=ro',uri=True,isolation_level=None)


def financial_history_unchanged(baseline, current):
    if Path(baseline).resolve(strict=True)==Path(current).resolve(strict=True):
        raise ValueError('Distinct baseline and current snapshots required')
    with closing(_open(baseline)) as old, closing(_open(current)) as new:
        for db in (old,new):
            db.execute('BEGIN')
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise ValueError('Financial snapshot failed integrity check')
        for table,order in (('ledger_events','id'),('ledger_postings','event_id,ordinal')):
            sql='SELECT * FROM '+table+' ORDER BY '+order
            if any(a!=b for a,b in zip_longest(old.execute(sql),new.execute(sql))):
                return False
        return True

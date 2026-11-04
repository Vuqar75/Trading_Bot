"""Conservative read-only guard for margin cutover rollback.

Callers must first stop and fence every writer. The guard does not restore any
database, does not prove a service stop, and is not distributed fencing.
"""
import sqlite3
from contextlib import closing
from itertools import zip_longest
from pathlib import Path

from .cross_snapshot_guard import financial_history_unchanged


def _open(path):
    return sqlite3.connect(Path(path).resolve(strict=True).as_uri()+'?mode=ro',uri=True,isolation_level=None)


def may_restore_pre_margin_code(baseline_financial,current_financial,baseline_queue,current_queue):
    """Permit old units only if journal and queue are exactly unchanged.

    This is intentionally stricter than necessary: after any new event or
    Telegram update an operator must preserve and reconcile current data.
    """
    reasons=[]
    if not financial_history_unchanged(baseline_financial,current_financial):reasons.append('financial_history_changed')
    if Path(baseline_queue).resolve(strict=True)==Path(current_queue).resolve(strict=True):raise ValueError('Distinct queue snapshots required')
    with closing(_open(baseline_queue)) as old,closing(_open(current_queue)) as new:
        for db in (old,new):
            db.execute('BEGIN')
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Queue integrity check failed')
        for table,order in (('unified_state','key'),('unified_updates','id'),('unified_jobs','id'),('unified_proposals','id'),('unified_outbox','id')):
            sql='SELECT * FROM '+table+' ORDER BY '+order
            if any(a!=b for a,b in zip_longest(old.execute(sql),new.execute(sql))):
                reasons.append('queue_changed');break
        old.rollback();new.rollback()
    return {'safe_for_old_code':not reasons,'reasons':reasons}

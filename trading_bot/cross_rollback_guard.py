"""Read-only gate for reverting cross code without restoring any database.

Both market writers, cross and queue consumers must already be stopped by the
caller.  A successful result permits only restoration of old service units;
it never permits overwriting the current financial or queue database.
"""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .paper_shared_store import SharedPaperStore
from .paper_wallet_ledger import PaperWalletLedger


def _read(path):
    return sqlite3.connect(Path(path).resolve(strict=True).as_uri()+'?mode=ro',uri=True,isolation_level=None)


def check_cross_code_rollback(financial,queue,baseline_queue):
    reasons=[]
    with closing(_read(financial)) as db,closing(_read(queue)) as q,closing(_read(baseline_queue)) as oldq:
        for con in (db,q,oldq):con.row_factory=sqlite3.Row;con.execute('BEGIN')
        shared=object.__new__(SharedPaperStore);shared.db=db
        shared.ledger=object.__new__(PaperWalletLedger);shared.ledger.db=db
        if not shared.ledger.reconcile()['balanced']:reasons.append('journal_integrity')
        try:shared._assert_consistent('spot');shared._assert_consistent('futures')
        except ValueError:reasons.append('existing_market_drift')
        for item in db.execute("SELECT DISTINCT account,asset FROM ledger_postings WHERE account LIKE 'futures_cross:%'"):
            if shared.ledger.balance(item['account'],item['asset']):reasons.append('cross_balance:'+item['account']+':'+item['asset'])
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cross_positions'").fetchone():
            from .cross_paper_book import CrossPaperBook
            book=object.__new__(CrossPaperBook);book.shared=shared;book.db=db;book.ledger=shared.ledger
            try:book._check()
            except ValueError:reasons.append('cross_drift')
            if book._positions():reasons.append('cross_positions')
        for row in db.execute("SELECT kind,details FROM ledger_events WHERE kind LIKE 'CROSS_%' OR kind='TRANSFER'"):
            details=json.loads(row['details'])
            if row['kind'].startswith('CROSS_') or 'futures_cross' in (details.get('source'),details.get('destination')):
                reasons.append('cross_financial_history');break
        baseline=oldq.execute("SELECT value FROM unified_state WHERE key='offset'").fetchone()
        if not baseline:reasons.append('baseline_queue_missing')
        else:
            offset=int(baseline[0])
            if q.execute("SELECT 1 FROM unified_updates WHERE id>=? AND (market='futures_cross' OR command IN ('/pause_all','/status','/mode')) LIMIT 1",(offset,)).fetchone() or q.execute("SELECT 1 FROM unified_jobs WHERE update_id>=? AND (market='futures_cross' OR instr(command,'futures_cross')>0) LIMIT 1",(offset,)).fetchone():
                reasons.append('new_cross_command')
            current_offset=q.execute("SELECT value FROM unified_state WHERE key='offset'").fetchone()
            if not current_offset or int(current_offset[0])<offset:reasons.append('queue_offset_regressed')
            for item in oldq.execute('SELECT id,fingerprint,command,market FROM unified_updates'):
                current=q.execute('SELECT id,fingerprint,command,market FROM unified_updates WHERE id=?',(item['id'],)).fetchone()
                if current is None or tuple(current)!=tuple(item):
                    reasons.append('queue_history_changed');break
        mode=q.execute("SELECT value FROM unified_state WHERE key='mode'").fetchone()
        if not mode or mode[0] not in ('spot','futures'):reasons.append('cross_mode_selected')
        if q.execute("SELECT 1 FROM unified_jobs WHERE status='PENDING' LIMIT 1").fetchone():reasons.append('pending_commands')
        for con in (db,q,oldq):con.rollback()
    return {'safe_for_old_code':not reasons,'reasons':reasons}

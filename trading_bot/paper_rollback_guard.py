"""Read-only gate against overwriting newer paper financial state on rollback.

This never performs a rollback. It must be used only with workers stopped and a
separately verified current queue/observer backup; a successful check is not a
distributed fencing guarantee.
"""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from .paper_shared_store import SharedPaperStore
from .paper_wallet_import import read_snapshot
from .paper_wallet_ledger import PaperWalletLedger
from .paper_wallet_ledger import amount


def _rows(db,sql):
    return [tuple(r) for r in db.execute(sql)]


def check_rollback(shared_path,spot_archive,futures_archive):
    spot,_=read_snapshot(spot_archive,'runtime_meta','paper')
    future,_=read_snapshot(futures_archive,'futures_meta','futures-paper')
    reasons=[]
    with closing(sqlite3.connect(Path(shared_path).resolve(strict=True).as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        shared=object.__new__(SharedPaperStore);shared.db=db
        shared.ledger=object.__new__(PaperWalletLedger);shared.ledger.db=db
        db.execute('BEGIN')
        if not shared.ledger.reconcile()['balanced']:reasons.append('journal_integrity')
        try:
            current_spot=shared._assert_consistent('spot');current_future=shared._assert_consistent('futures')
        except ValueError:
            reasons.append('financial_drift');current_spot=None;current_future=None
        if db.execute("SELECT 1 FROM ledger_events WHERE kind IN ('FINANCIAL_CHANGE','TRANSFER') LIMIT 1").fetchone():
            reasons.append('new_financial_events')
        if current_spot and (amount(current_spot['cash'])!=amount(spot['cash']) or current_spot.get('position')!=spot.get('position') or current_spot.get('pnl')!=spot.get('pnl')):reasons.append('spot_changed')
        def normalize_pos(p):
            return {k:v for k,v in p.items() if k!='funding_cursor'} if isinstance(p,dict) else p
        if current_future and (amount(current_future['cash'])!=amount(future['cash']) or current_future['pnl']!=future.get('pnl') or normalize_pos(current_future['position'])!=normalize_pos(future.get('position'))):reasons.append('futures_changed')
        db.execute('ATTACH DATABASE ? AS old_spot',(Path(spot_archive).resolve().as_uri()+'?mode=ro',))
        db.execute('ATTACH DATABASE ? AS old_future',(Path(futures_archive).resolve().as_uri()+'?mode=ro',))
        for table in ('runtime_orders','runtime_fills','runtime_closes'):
            if _rows(db,f'SELECT * FROM main.{table} ORDER BY rowid')!=_rows(db,f'SELECT * FROM old_spot.{table} ORDER BY rowid'):reasons.append('spot_history:'+table)
        if _rows(db,"SELECT * FROM main.futures_events WHERE event!='EQUITY' ORDER BY id")!=_rows(db,"SELECT * FROM old_future.futures_events WHERE event!='EQUITY' ORDER BY id"):reasons.append('futures_history')
        if _rows(db,'SELECT * FROM main.futures_funding ORDER BY rowid')!=_rows(db,'SELECT * FROM old_future.futures_funding ORDER BY rowid'):reasons.append('funding_history')
        db.rollback()
    return {'safe_for_financial_rollback':not reasons,'reasons':reasons}

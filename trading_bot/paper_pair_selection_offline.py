"""Offline-only atomic Spot pair selection experiment; not wired to Telegram.

The caller supplies previously obtained public metadata and quote snapshots.
No network operation occurs while the financial and command DBs are locked.
"""
import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from .paper_pair_catalog import spot_usdt_candidate
from .paper_shared_store import SharedPaperStore


def select_spot_pair_offline(shared, queue_path, *, request_id, expected_symbol,
                             expected_version, symbol, metadata, price, quantity,
                             observed_at, clock=time.time, max_age=120):
    if (not isinstance(shared,SharedPaperStore) or not isinstance(request_id,str) or
            not re.fullmatch(r'[A-Za-z0-9:_-]{1,100}',request_id) or
            type(expected_version) is not int or expected_version<0 or
            not isinstance(expected_symbol,str) or not isinstance(symbol,str)):
        raise ValueError('Invalid selection identity')
    fingerprint=hashlib.sha256(json.dumps([expected_symbol,expected_version,symbol],
        separators=(',',':')).encode()).hexdigest()
    path=Path(queue_path)
    if path.is_symlink() or not path.is_file():raise ValueError('Existing queue database required')
    path=path.resolve(strict=True)
    shared_file=Path(shared.db.execute('PRAGMA database_list').fetchone()[2]).resolve(strict=True)
    if path==shared_file:raise ValueError('Financial and queue databases must be distinct')
    with closing(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=5)) as queue:
        if queue.execute('PRAGMA journal_mode').fetchone()[0].lower()!='delete':
            raise ValueError('Rollback journal required for queue')
        try:
            with shared.transaction():
                queue.execute('BEGIN IMMEDIATE')
                shared.db.execute('CREATE TABLE IF NOT EXISTS pair_spot_selection_events(request_id TEXT PRIMARY KEY,version INTEGER NOT NULL UNIQUE,fingerprint TEXT NOT NULL,result TEXT NOT NULL)')
                for verb in ('UPDATE','DELETE'):
                    shared.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_pair_spot_events_{verb.lower()} BEFORE {verb} ON pair_spot_selection_events BEGIN SELECT RAISE(ABORT,'immutable pair selection'); END")
                old=shared.db.execute('SELECT fingerprint,result FROM pair_spot_selection_events WHERE request_id=?',(request_id,)).fetchone()
                if old is not None:
                    if old['fingerprint']!=fingerprint:raise ValueError('Conflicting request identity')
                    return dict(json.loads(old['result']),replayed=True)
                now=clock()
                if (isinstance(observed_at,bool) or not isinstance(observed_at,(int,float)) or
                        not 0<=now-observed_at<=max_age):
                    raise ValueError('Pair data stale or future dated')
                preview=spot_usdt_candidate(metadata,symbol,quantity,price)
                if shared.deployment_hold():raise ValueError('Deployment hold active')
                if not shared.ledger.reconcile()['balanced']:raise ValueError('Journal mismatch')
                snapshot=shared._assert_consistent('spot')
                if snapshot['paused'] is not True:raise ValueError('Spot must be paused')
                if (Decimal(snapshot['position']['quantity'])!=0 or
                        Decimal(snapshot['position']['cost'])!=0 or not snapshot['position']['fees_known']):
                    raise ValueError('Spot position or unknown fees prevent selection')
                if shared._get('spot','error') or shared._get('spot','blocked'):
                    raise ValueError('Spot requires recovery')
                if shared.db.execute("SELECT 1 FROM runtime_orders WHERE phase!='SETTLED' LIMIT 1").fetchone():
                    raise ValueError('Spot order unresolved')
                if queue.execute("SELECT 1 FROM unified_jobs WHERE market='spot' AND status='PENDING' LIMIT 1").fetchone():
                    raise ValueError('Spot command pending')
                if queue.execute("SELECT 1 FROM unified_proposals WHERE market='spot' AND status='OPEN' LIMIT 1").fetchone():
                    raise ValueError('Spot confirmation pending')
                if shared.db.execute("SELECT 1 FROM transfer_proposals WHERE status='OPEN' AND (source='spot' OR destination='spot') LIMIT 1").fetchone():
                    raise ValueError('Spot transfer pending')
                identity=shared._get('spot','identity')
                if not isinstance(identity,dict) or identity.get('mode')!='paper':raise ValueError('Paper identity required')
                current=identity.get('symbol','BTCUSDT')
                version=shared._get('spot','pair_selection_version',0)
                latest=shared.db.execute('SELECT version,result FROM pair_spot_selection_events ORDER BY version DESC LIMIT 1').fetchone()
                if ((latest is None and version!=0) or
                        (latest is not None and (latest['version']!=version or json.loads(latest['result'])['symbol']!=current))):
                    raise ValueError('Pair selection history conflicts with identity')
                if current!=expected_symbol or version!=expected_version:raise ValueError('Stale pair selection')
                for row in shared.db.execute('SELECT client_id FROM runtime_orders'):
                    if shared._get('spot','pair_order_symbol:'+row['client_id']) is None:
                        raise ValueError('Historical order symbol migration required')
                previous_cash=snapshot['cash'];previous_pnl=snapshot['pnl']
                updated=dict(identity);updated['symbol']=symbol
                shared._set('spot','identity',updated)
                shared._set('spot','pair_selection_version',version+1)
                for name in ('signal','quote','last_candle'):
                    shared._set('spot',name,None)
                result={'fingerprint':fingerprint,'from':current,'symbol':symbol,'version':version+1,
                        'base_asset':preview['base_asset'],'observed_at':observed_at}
                shared.db.execute('INSERT INTO pair_spot_selection_events VALUES(?,?,?,?)',
                                  (request_id,version+1,fingerprint,json.dumps(result,sort_keys=True)))
                after=shared._assert_consistent('spot')
                if after['cash']!=previous_cash or after['pnl']!=previous_pnl or after['paused'] is not True:
                    raise ValueError('Selection changed financial state')
                return dict(result,replayed=False)
        finally:
            if queue.in_transaction:queue.rollback()

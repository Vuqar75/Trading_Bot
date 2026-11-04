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

from .paper_pair_catalog import spot_usdt_candidate, usd_m_perpetual_candidate
from .paper_shared_store import SharedPaperStore


def _confirmed_job(queue, market, job_id, request_id, previous, version, target, now):
    """Validate the immutable received command while both stores are locked."""
    if job_id is None:return -1
    if type(job_id) is not int or job_id<=0:raise ValueError('Invalid pair job')
    row=queue.execute('SELECT command,payload,status FROM unified_jobs WHERE id=? AND market=?',(job_id,market)).fetchone()
    if not row or row[0]!='/confirmed_pair' or row[2]!='PENDING':raise ValueError('Pair confirmation job unavailable')
    payload=json.loads(row[1]);token=payload.get('proposal')
    if not isinstance(token,str) or request_id!='pair:'+token:
        raise ValueError('Pair confirmation identity mismatch')
    proposal=queue.execute('SELECT market,previous,version,target,expires,status FROM unified_pair_proposals WHERE id=?',(token,)).fetchone()
    if (not proposal or proposal[0]!=market or json.loads(proposal[1])!=sorted(previous) or
        proposal[2]!=version or json.loads(proposal[3])!=sorted(target) or proposal[5]!='CONFIRMED' or
        payload.get('previous')!=sorted(previous) or payload.get('version')!=version or
        payload.get('target')!=sorted(target) or payload.get('expires')!=proposal[4]):
        raise ValueError('Pair confirmation does not match selection')
    if now>=proposal[4]:raise ValueError('Pair confirmation expired')
    return job_id


def select_spot_pair_offline(shared, queue_path, *, request_id, expected_symbol,
                             expected_version, symbol, metadata, price, quantity,
                             observed_at, clock=time.time, max_age=120, confirm_job_id=None):
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
                current_job=_confirmed_job(queue,'spot',confirm_job_id,request_id,[expected_symbol],expected_version,[symbol],now)
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
                if queue.execute("SELECT 1 FROM unified_jobs WHERE market='spot' AND status='PENDING' AND id!=? LIMIT 1",(current_job,)).fetchone():
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


def select_isolated_pair_offline(shared, queue_path, *, request_id, expected_symbol,
                                 expected_version, symbol, metadata, price, quantity,
                                 observed_at, clock=time.time, max_age=120, confirm_job_id=None):
    """Offline-only USD-M isolated selection on paused, flat shared copies."""
    if (not isinstance(shared,SharedPaperStore) or not isinstance(request_id,str) or
            not re.fullmatch(r'[A-Za-z0-9:_-]{1,100}',request_id) or
            type(expected_version) is not int or expected_version<0 or
            not isinstance(expected_symbol,str) or not isinstance(symbol,str)):
        raise ValueError('Invalid selection identity')
    fingerprint=hashlib.sha256(json.dumps([expected_symbol,expected_version,symbol],separators=(',',':')).encode()).hexdigest()
    path=Path(queue_path)
    if path.is_symlink() or not path.is_file():raise ValueError('Existing queue database required')
    path=path.resolve(strict=True)
    if path==Path(shared.db.execute('PRAGMA database_list').fetchone()[2]).resolve(strict=True):
        raise ValueError('Financial and queue databases must be distinct')
    with closing(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=5)) as queue:
        if queue.execute('PRAGMA journal_mode').fetchone()[0].lower()!='delete':raise ValueError('Rollback journal required for queue')
        try:
            with shared.transaction():
                queue.execute('BEGIN IMMEDIATE')
                shared.db.execute('CREATE TABLE IF NOT EXISTS pair_isolated_selection_events(request_id TEXT PRIMARY KEY,version INTEGER NOT NULL UNIQUE,fingerprint TEXT NOT NULL,result TEXT NOT NULL)')
                for verb in ('UPDATE','DELETE'):
                    shared.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_pair_isolated_events_{verb.lower()} BEFORE {verb} ON pair_isolated_selection_events BEGIN SELECT RAISE(ABORT,'immutable pair selection'); END")
                old=shared.db.execute('SELECT fingerprint,result FROM pair_isolated_selection_events WHERE request_id=?',(request_id,)).fetchone()
                if old is not None:
                    if old['fingerprint']!=fingerprint:raise ValueError('Conflicting request identity')
                    return dict(json.loads(old['result']),replayed=True)
                now=clock()
                current_job=_confirmed_job(queue,'futures',confirm_job_id,request_id,[expected_symbol],expected_version,[symbol],now)
                if isinstance(observed_at,bool) or not isinstance(observed_at,(int,float)) or not 0<=now-observed_at<=max_age:
                    raise ValueError('Pair data stale or future dated')
                preview=usd_m_perpetual_candidate(metadata,symbol,quantity,price)
                if shared.deployment_hold():raise ValueError('Deployment hold active')
                if not shared.ledger.reconcile()['balanced']:raise ValueError('Journal mismatch')
                snapshot=shared._assert_consistent('futures')
                if snapshot['paused'] is not True:raise ValueError('Isolated futures must be paused')
                if snapshot['position'] is not None:raise ValueError('Existing futures position prevents selection')
                if shared._get('futures','error') or shared._get('futures','blocked'):raise ValueError('Futures requires recovery')
                if queue.execute("SELECT 1 FROM unified_jobs WHERE market='futures' AND status='PENDING' AND id!=? LIMIT 1",(current_job,)).fetchone():raise ValueError('Futures command pending')
                if queue.execute("SELECT 1 FROM unified_proposals WHERE market='futures' AND status='OPEN' LIMIT 1").fetchone():raise ValueError('Futures confirmation pending')
                if shared.db.execute("SELECT 1 FROM transfer_proposals WHERE status='OPEN' AND (source='futures_isolated' OR destination='futures_isolated') LIMIT 1").fetchone():raise ValueError('Futures transfer pending')
                identity=shared._get('futures','identity')
                if not isinstance(identity,dict) or identity.get('mode')!='futures-paper':raise ValueError('Futures paper identity required')
                current=identity.get('symbol','BTCUSDT');version=shared._get('futures','pair_selection_version',0)
                latest=shared.db.execute('SELECT version,result FROM pair_isolated_selection_events ORDER BY version DESC LIMIT 1').fetchone()
                if ((latest is None and version!=0) or (latest is not None and (latest['version']!=version or json.loads(latest['result'])['symbol']!=current))):
                    raise ValueError('Pair selection history conflicts with identity')
                if current!=expected_symbol or version!=expected_version:raise ValueError('Stale pair selection')
                # Pre-selection events are legacy BTC only; never rewrite them.
                if latest is None and current!='BTCUSDT':raise ValueError('Legacy futures identity requires review')
                if latest is None:
                    for row in shared.db.execute("SELECT payload FROM futures_events WHERE event IN ('OPEN','CLOSE')"):
                        payload=json.loads(row['payload'])
                        if payload.get('symbol','BTCUSDT')!='BTCUSDT':raise ValueError('Legacy futures event symbol conflicts')
                before_cash=snapshot['cash'];before_pnl=snapshot['pnl']
                updated=dict(identity);updated['symbol']=symbol
                shared._set('futures','identity',updated);shared._set('futures','pair_selection_version',version+1)
                for name in ('signal','quote','last_candle','reverse'):
                    shared._set('futures',name,None)
                result={'fingerprint':fingerprint,'from':current,'symbol':symbol,'version':version+1,
                        'base_asset':preview['base_asset'],'observed_at':observed_at}
                shared.db.execute('INSERT INTO pair_isolated_selection_events VALUES(?,?,?,?)',
                                  (request_id,version+1,fingerprint,json.dumps(result,sort_keys=True)))
                after=shared._assert_consistent('futures')
                if after['cash']!=before_cash or after['pnl']!=before_pnl or after['paused'] is not True:
                    raise ValueError('Selection changed financial state')
                return dict(result,replayed=False)
        finally:
            if queue.in_transaction:queue.rollback()


def select_cross_pairs_offline(shared, queue_path, *, request_id, expected_symbols,
                               expected_version, symbols, metadata, prices, quantities,
                               observed_at, clock=time.time, max_age=120, confirm_job_id=None):
    """Offline-only cross set change; never remove protection for a position."""
    if (not isinstance(shared,SharedPaperStore) or not isinstance(request_id,str) or
            not re.fullmatch(r'[A-Za-z0-9:_-]{1,100}',request_id) or type(expected_version) is not int or expected_version<0 or
            not isinstance(expected_symbols,(tuple,list)) or not isinstance(symbols,(tuple,list)) or
            not 1<=len(symbols)<=2 or any(not isinstance(s,str) for s in (*expected_symbols,*symbols)) or len(set(symbols))!=len(symbols)):
        raise ValueError('Invalid cross selection identity')
    expected=sorted(expected_symbols);selected=sorted(symbols)
    fingerprint=hashlib.sha256(json.dumps([expected,expected_version,selected],separators=(',',':')).encode()).hexdigest()
    path=Path(queue_path)
    if path.is_symlink() or not path.is_file():raise ValueError('Existing queue database required')
    path=path.resolve(strict=True)
    if path==Path(shared.db.execute('PRAGMA database_list').fetchone()[2]).resolve(strict=True):raise ValueError('Financial and queue databases must be distinct')
    with closing(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=5)) as queue:
        if queue.execute('PRAGMA journal_mode').fetchone()[0].lower()!='delete':raise ValueError('Rollback journal required for queue')
        try:
            with shared.transaction():
                queue.execute('BEGIN IMMEDIATE')
                shared.db.execute('CREATE TABLE IF NOT EXISTS pair_cross_selection_events(request_id TEXT PRIMARY KEY,version INTEGER NOT NULL UNIQUE,fingerprint TEXT NOT NULL,result TEXT NOT NULL)')
                for verb in ('UPDATE','DELETE'):
                    shared.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_pair_cross_events_{verb.lower()} BEFORE {verb} ON pair_cross_selection_events BEGIN SELECT RAISE(ABORT,'immutable pair selection'); END")
                old=shared.db.execute('SELECT fingerprint,result FROM pair_cross_selection_events WHERE request_id=?',(request_id,)).fetchone()
                if old is not None:
                    if old['fingerprint']!=fingerprint:raise ValueError('Conflicting request identity')
                    return dict(json.loads(old['result']),replayed=True)
                now=clock()
                current_job=_confirmed_job(queue,'futures_cross',confirm_job_id,request_id,expected,expected_version,selected,now)
                if isinstance(observed_at,bool) or not isinstance(observed_at,(int,float)) or not 0<=now-observed_at<=max_age:raise ValueError('Pair data stale or future dated')
                if not all(isinstance(v,dict) for v in (metadata,prices,quantities)) or any(s not in metadata or s not in prices or s not in quantities for s in selected):
                    raise ValueError('Cross metadata incomplete')
                for item in selected:usd_m_perpetual_candidate(metadata[item],item,quantities[item],prices[item])
                from .cross_paper_book import CrossPaperBook
                book=object.__new__(CrossPaperBook);book.shared=shared;book.db=shared.db;book.ledger=shared.ledger
                if shared.deployment_hold():raise ValueError('Deployment hold active')
                book._check()
                state=shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()
                if not state or state[0]!=1:raise ValueError('Cross must be paused')
                if book._positions():raise ValueError('Existing cross positions prevent selection')
                if book.runtime_error() or book.funding_conflict():raise ValueError('Cross requires recovery')
                if shared.ledger.balance('futures_cross:reserved') or shared.ledger.balance('futures_cross:liability'):
                    raise ValueError('Cross reserve or liability prevents selection')
                if queue.execute("SELECT 1 FROM unified_jobs WHERE market='futures_cross' AND status='PENDING' AND id!=? LIMIT 1",(current_job,)).fetchone():raise ValueError('Cross command pending')
                if queue.execute("SELECT 1 FROM unified_proposals WHERE market='futures_cross' AND status='OPEN' LIMIT 1").fetchone():raise ValueError('Cross confirmation pending')
                if shared.db.execute("SELECT 1 FROM transfer_proposals WHERE status='OPEN' AND (source='futures_cross' OR destination='futures_cross') LIMIT 1").fetchone():raise ValueError('Cross transfer pending')
                current=sorted(book._symbols())
                row=shared.db.execute("SELECT value FROM cross_runtime WHERE key='pair_version'").fetchone()
                version=json.loads(row[0]) if row else 0
                if current!=expected or version!=expected_version:raise ValueError('Stale cross pair selection')
                free=shared.ledger.balance('futures_cross:free')
                result={'fingerprint':fingerprint,'from':current,'symbols':selected,'version':version+1,'observed_at':observed_at}
                shared.db.execute('INSERT INTO pair_cross_selection_events VALUES(?,?,?,?)',(request_id,version+1,fingerprint,json.dumps(result,sort_keys=True)))
                shared.db.execute("INSERT OR REPLACE INTO cross_runtime VALUES('pair_symbols',?)",(json.dumps(selected),))
                shared.db.execute("INSERT OR REPLACE INTO cross_runtime VALUES('pair_version',?)",(json.dumps(version+1),))
                shared.db.execute('UPDATE cross_quotes SET funding_complete=0')
                book._check()
                if shared.ledger.balance('futures_cross:free')!=free:raise ValueError('Cross selection changed funds')
                return dict(result,replayed=False)
        finally:
            if queue.in_transaction:queue.rollback()


def select_margin_pairs_offline(shared, queue_path, *, request_id, expected_symbols,
                                expected_version, symbols, metadata, prices, quantities,
                                observed_at, clock=time.time, max_age=120, confirm_job_id=None):
    """Change margin's offline pair set without altering balances or liabilities."""
    if (not isinstance(shared,SharedPaperStore) or not isinstance(request_id,str) or
        not re.fullmatch(r'[A-Za-z0-9:_-]{1,100}',request_id) or type(expected_version) is not int or expected_version<0 or
        not isinstance(expected_symbols,(list,tuple)) or not isinstance(symbols,(list,tuple)) or
        not 1<=len(symbols)<=2 or any(not isinstance(s,str) for s in (*expected_symbols,*symbols)) or len(set(symbols))!=len(symbols)):
        raise ValueError('Invalid margin selection identity')
    expected=sorted(expected_symbols);selected=sorted(symbols)
    fingerprint=hashlib.sha256(json.dumps([expected,expected_version,selected],separators=(',',':')).encode()).hexdigest()
    path=Path(queue_path)
    if path.is_symlink() or not path.is_file():raise ValueError('Existing queue database required')
    path=path.resolve(strict=True)
    if path==Path(shared.db.execute('PRAGMA database_list').fetchone()[2]).resolve(strict=True):raise ValueError('Financial and queue databases must be distinct')
    with closing(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=5)) as queue:
        if queue.execute('PRAGMA journal_mode').fetchone()[0].lower()!='delete':raise ValueError('Rollback journal required for queue')
        try:
            with shared.transaction():
                queue.execute('BEGIN IMMEDIATE')
                shared.db.execute('CREATE TABLE IF NOT EXISTS pair_margin_selection_events(request_id TEXT PRIMARY KEY,version INTEGER NOT NULL UNIQUE,fingerprint TEXT NOT NULL,result TEXT NOT NULL)')
                for verb in ('UPDATE','DELETE'):
                    shared.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_pair_margin_events_{verb.lower()} BEFORE {verb} ON pair_margin_selection_events BEGIN SELECT RAISE(ABORT,'immutable pair selection'); END")
                old=shared.db.execute('SELECT fingerprint,result FROM pair_margin_selection_events WHERE request_id=?',(request_id,)).fetchone()
                if old is not None:
                    if old['fingerprint']!=fingerprint:raise ValueError('Conflicting request identity')
                    return dict(json.loads(old['result']),replayed=True)
                now=clock()
                current_job=_confirmed_job(queue,'margin_cross',confirm_job_id,request_id,expected,expected_version,selected,now)
                if isinstance(observed_at,bool) or not isinstance(observed_at,(int,float)) or not 0<=now-observed_at<=max_age:raise ValueError('Pair data stale or future dated')
                if not all(isinstance(v,dict) for v in (metadata,prices,quantities)) or any(s not in metadata or s not in prices or s not in quantities for s in selected):
                    raise ValueError('Margin metadata incomplete')
                for item in selected:spot_usdt_candidate(metadata[item],item,quantities[item],prices[item])
                if shared.deployment_hold():raise ValueError('Deployment hold active')
                if queue.execute("SELECT 1 FROM unified_jobs WHERE market='margin_cross' AND status='PENDING' AND id!=? LIMIT 1",(current_job,)).fetchone():raise ValueError('Margin command pending')
                if queue.execute("SELECT 1 FROM unified_proposals WHERE market='margin_cross' AND status='OPEN' LIMIT 1").fetchone():raise ValueError('Margin confirmation pending')
                if shared.db.execute("SELECT 1 FROM transfer_proposals WHERE status='OPEN' AND (source='margin_cross' OR destination='margin_cross') LIMIT 1").fetchone():raise ValueError('Margin transfer pending')
                from .margin_paper_book import MarginPaperBook, PAIRS
                if not shared.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='margin_runtime'").fetchone():raise ValueError('Margin runtime unavailable')
                row=shared.db.execute("SELECT value FROM margin_runtime WHERE key='pair_symbols'").fetchone()
                current=json.loads(row[0]) if row else sorted(PAIRS)
                row=shared.db.execute("SELECT value FROM margin_runtime WHERE key='pair_version'").fetchone()
                version=json.loads(row[0]) if row else 0
                latest=shared.db.execute('SELECT version,result FROM pair_margin_selection_events ORDER BY version DESC LIMIT 1').fetchone()
                if ((latest is None and version!=0) or (latest is not None and
                    (latest['version']!=version or json.loads(latest['result'])['symbols']!=current))):raise ValueError('Margin pair history conflicts with identity')
                if current!=expected or version!=expected_version:raise ValueError('Stale margin pair selection')
                assets={r[0] for r in shared.db.execute('SELECT DISTINCT asset FROM margin_loan_rates') if r[0] not in ('USDT','BTC','ETH')}
                assets.update(s[:-4] for s in selected if s not in PAIRS)
                extras={asset+'USDT':asset for asset in assets}
                book=MarginPaperBook(shared.ledger,experimental_pairs=extras,selected_symbols=selected)
                book._check()
                state=shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()
                error=shared.db.execute("SELECT value FROM margin_runtime WHERE key='error'").fetchone()
                if not state or state[0]!=1:raise ValueError('Margin must be paused')
                if not error or json.loads(error[0]) is not None:raise ValueError('Margin requires recovery')
                if book._positions() or shared.db.execute("SELECT 1 FROM margin_loans WHERE status='OPEN' LIMIT 1").fetchone():raise ValueError('Margin position or debt prevents selection')
                for asset in ('USDT',*book.pairs.values()):
                    if shared.ledger.balance('margin_cross:reserved',asset):raise ValueError('Margin reserve prevents selection')
                before={(account,asset):shared.ledger.balance('margin_cross:'+account,asset)
                        for account in ('free','reserved','principal','interest') for asset in ('USDT',*book.pairs.values())}
                result={'fingerprint':fingerprint,'from':current,'symbols':selected,'version':version+1,'observed_at':observed_at}
                shared.db.execute('INSERT INTO pair_margin_selection_events VALUES(?,?,?,?)',(request_id,version+1,fingerprint,json.dumps(result,sort_keys=True)))
                shared.db.execute("INSERT OR REPLACE INTO margin_runtime VALUES('pair_symbols',?)",(json.dumps(selected),))
                shared.db.execute("INSERT OR REPLACE INTO margin_runtime VALUES('pair_version',?)",(json.dumps(version+1),))
                after={(account,asset):shared.ledger.balance('margin_cross:'+account,asset)
                       for account in ('free','reserved','principal','interest') for asset in ('USDT',*book.pairs.values())}
                if before!=after:raise ValueError('Margin selection changed funds')
                return dict(result,replayed=False)
        finally:
            if queue.in_transaction:queue.rollback()

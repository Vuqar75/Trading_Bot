"""Offline consolidation of two stopped paper snapshots and their ledger.

The produced file is *not* usable by existing live runtimes until those runtimes
are adapted to the shared schema and financial journaling. Never run against live
databases; the caller must establish a coordinated stop and save original files.
"""
import hashlib
import os
import json
import sqlite3
import tempfile
from pathlib import Path
from .paper_wallet_import import import_paper_snapshots, read_snapshot
from .paper_wallet_ledger import PaperWalletLedger, amount, encode

SPOT_TABLES = ('runtime_meta','runtime_orders','runtime_fills','runtime_commands','runtime_outbox','runtime_closes')
FUTURES_TABLES = ('futures_meta','futures_events','futures_funding','futures_commands','runtime_outbox')


def _digest(path):
    with open(path,'rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()


def consolidate_snapshots(spot_path, futures_path, destination, *, captured_at, fault=lambda p: None):
    spot = Path(spot_path).resolve(strict=True)
    futures = Path(futures_path).resolve(strict=True)
    destination = Path(destination).absolute()
    if spot == futures or destination in (spot,futures) or destination.exists():
        raise ValueError('Distinct source snapshots and a new destination are required')
    if not destination.parent.is_dir():
        raise ValueError('Destination directory must already exist')
    spot_meta,spot_hash = read_snapshot(spot,'runtime_meta','paper')
    future_meta,future_hash = read_snapshot(futures,'futures_meta','futures-paper')
    fd,tmpname = tempfile.mkstemp(prefix='.paper-consolidation-',suffix='.db',dir=destination.parent)
    os.close(fd); os.chmod(tmpname,0o600)
    tmp = Path(tmpname)
    try:
        ledger = PaperWalletLedger(tmp)
        try:
            if ledger.db.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower() != 'delete':
                raise ValueError('Rollback journal required')
            info = import_paper_snapshots(ledger,spot,futures,captured_at=captured_at)
            if (info['spot_sha256'],info['futures_sha256']) != (spot_hash,future_hash):
                raise ValueError('Source snapshots changed')
            db = ledger.db
            db.execute('ATTACH DATABASE ? AS source_spot', (spot.as_uri()+'?mode=ro',))
            db.execute('ATTACH DATABASE ? AS source_future', (futures.as_uri()+'?mode=ro',))
            try:
                db.execute('BEGIN IMMEDIATE')
                for namespace,expected in (('source_spot',SPOT_TABLES),('source_future',FUTURES_TABLES)):
                    names = {r[0] for r in db.execute(f"SELECT name FROM {namespace}.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                    if names != set(expected):raise ValueError('Unexpected source tables')
                    for source in expected:
                        target = 'futures_outbox' if namespace == 'source_future' and source == 'runtime_outbox' else source
                        ddl = db.execute(f"SELECT sql FROM {namespace}.sqlite_master WHERE type='table' AND name=?",(source,)).fetchone()[0]
                        if source != target:
                            prefix='CREATE TABLE runtime_outbox'
                            if not ddl.startswith(prefix):raise ValueError('Unexpected futures outbox schema')
                            ddl=ddl.replace(prefix,'CREATE TABLE futures_outbox',1)
                        db.execute(ddl)
                    for source in expected:
                        target = 'futures_outbox' if namespace == 'source_future' and source == 'runtime_outbox' else source
                        db.execute(f'INSERT INTO main."{target}" SELECT * FROM {namespace}."{source}"')
                        old_count=db.execute(f'SELECT count(*) FROM {namespace}."{source}"').fetchone()[0]
                        new_count=db.execute(f'SELECT count(*) FROM main."{target}"').fetchone()[0]
                        if old_count != new_count:raise ValueError('Source row count mismatch')
                db.execute('INSERT INTO ledger_meta VALUES(?,?)',('consolidated_schema','1'))
                db.execute('CREATE TABLE shared_market_anchor(market TEXT PRIMARY KEY,event_id TEXT NOT NULL REFERENCES ledger_events(id))')
                for market,meta in (('spot',spot_meta),('futures',future_meta)):
                    pos=meta.get('position')
                    if isinstance(pos,dict):pos={k:v for k,v in pos.items() if k!='funding_cursor'}
                    projection={'cash':encode(amount(meta['cash'])),'position':pos,'pnl':meta.get('pnl')}
                    event_id='opening:projection:'+market
                    ledger._post(event_id,'FINANCIAL_METADATA',{'market':market,'before':None,'after':projection,'details':{'source':'opening snapshot'}},[],created=captured_at)
                    db.execute('INSERT INTO shared_market_anchor VALUES(?,?)',(market,event_id))
                if db.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('Foreign key check failed')
                fault('before_copy_commit')
                db.commit()
                fault('after_copy_commit')
            except BaseException:
                db.rollback();raise
            finally:
                db.execute('DETACH DATABASE source_future')
                db.execute('DETACH DATABASE source_spot')
            if _digest(spot)!=spot_hash or _digest(futures)!=future_hash:
                raise ValueError('Source changed while consolidating')
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or not ledger.reconcile()['balanced']:
                raise ValueError('Consolidated snapshot failed integrity or ledger checks')
        finally:
            ledger.close()
        with open(tmp,'rb') as stream:os.fsync(stream.fileno())
        fault('before_publish')
        os.link(tmp,destination)  # Exclusive atomic publication, never overwrites.
        directory = os.open(destination.parent,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
        fault('after_publish')
        return {'spot_sha256':spot_hash,'futures_sha256':future_hash,
                'spot_position':spot_meta.get('position'),'futures_position':future_meta.get('position')}
    finally:
        if tmp.exists():tmp.unlink()

"""Offline import from separately captured, stopped paper SQLite snapshots.

The caller must establish a coordinated stop and preserve the original files.
This module does not stop processes, update old stores, or activate the ledger.
"""
import hashlib
import json
import sqlite3
from pathlib import Path
from decimal import Decimal
from .paper_wallet_ledger import amount


def read_snapshot(path, table, expected_mode):
    path = Path(path).resolve(strict=True)
    if not path.is_file() or any(Path(str(path)+suffix).exists() for suffix in ('-wal','-journal')):
        raise ValueError('Use a standalone, consistent SQLite backup')
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Invalid source snapshot')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
            raise ValueError('Wrong source schema')
        meta = {k:json.loads(v) for k,v in db.execute('SELECT key,value FROM '+table)}
        if meta.get('identity',{}).get('mode') != expected_mode:
            raise ValueError('Source is not the expected paper mode')
        if meta.get('paused') is not True:
            raise ValueError('Source snapshot must have paper entries paused')
        if meta.get('blocked') or meta.get('error') or meta.get('close_request'):
            raise ValueError('Source requires recovery before import')
        if table == 'runtime_meta' and db.execute("SELECT 1 FROM runtime_orders WHERE phase!='SETTLED' LIMIT 1").fetchone():
            raise ValueError('Source has unresolved orders')
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    if before != after:
        raise ValueError('Source changed while importing')
    return meta,before


def import_paper_snapshots(ledger, spot_path, futures_path, *, captured_at):
    """Read both archived snapshots and import their current state exactly once."""
    if Path(spot_path).resolve() == Path(futures_path).resolve():
        raise ValueError('Distinct source snapshots required')
    spot,spot_hash = read_snapshot(spot_path,'runtime_meta','paper')
    futures,futures_hash = read_snapshot(futures_path,'futures_meta','futures-paper')
    spot_pos = spot.get('position')
    if not isinstance(spot_pos,dict) or not isinstance(spot_pos.get('fees_known'),bool):
        raise ValueError('Spot position or fee state is unknown')
    fut_pos = futures.get('position')
    if fut_pos is not None and (not isinstance(fut_pos,dict) or not fut_pos.get('id') or fut_pos.get('direction') not in ('LONG','SHORT')):
        raise ValueError('Futures position is unknown')
    margin = amount(fut_pos['margin']) if fut_pos else Decimal(0)
    funding = amount(fut_pos['funding']) if fut_pos else Decimal(0)
    source = json.dumps({'spot_sha256':spot_hash,'futures_sha256':futures_hash},sort_keys=True)
    positions = {'spot':spot_pos,'futures':fut_pos,
                 'spot_realized_pnl':spot.get('pnl'), 'futures_realized_pnl':futures.get('pnl'),
                 'spot_identity':spot['identity'],'futures_identity':futures['identity'],
                 'spot_paused_at_capture':spot.get('paused'),'futures_paused_at_capture':futures.get('paused')}
    ledger.import_snapshot(spot_cash=spot['cash'],spot_btc=spot_pos['quantity'],
                           futures_cash=futures['cash'],futures_margin=margin,funding=funding,
                           position_metadata=positions,source=source,captured_at=captured_at)
    return {'spot_sha256':spot_hash,'futures_sha256':futures_hash,'spot_fees_known':spot_pos['fees_known']}

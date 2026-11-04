"""Offline migration of legacy Spot order identities; not a pair switch.

Only the legacy BTCUSDT paper schema is accepted. No financial postings,
positions or orders are modified. The caller must migrate an isolated copy.
"""
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def tag_legacy_spot_orders(path, *, fault=lambda stage: None, timeout=5):
    target = Path(path).resolve(strict=True)
    with closing(sqlite3.connect(target.as_uri()+'?mode=rw', uri=True,
                                 isolation_level=None, timeout=timeout)) as db:
        db.execute('PRAGMA synchronous=FULL')
        if db.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'delete':
            raise ValueError('Rollback journal required')
        db.execute('BEGIN IMMEDIATE')
        try:
            def meta(key):
                row=db.execute('SELECT value FROM runtime_meta WHERE key=?',(key,)).fetchone()
                return json.loads(row[0]) if row else None
            identity=meta('identity')
            if not isinstance(identity,dict) or identity.get('mode')!='paper' or identity.get('symbol','BTCUSDT')!='BTCUSDT':
                raise ValueError('Explicit legacy BTC paper identity required')
            if meta('paused') is not True:
                raise ValueError('Spot must be paused')
            position=meta('position')
            if not isinstance(position,dict) or position.get('symbol','BTCUSDT')!='BTCUSDT':
                raise ValueError('Legacy position identity unknown')
            if db.execute("SELECT 1 FROM runtime_orders WHERE phase!='SETTLED' LIMIT 1").fetchone():
                raise ValueError('Unresolved order requires recovery')
            for row in db.execute('SELECT payload FROM runtime_fills'):
                fill=json.loads(row[0])
                if not isinstance(fill,dict) or fill.get('symbol')!='BTCUSDT':
                    raise ValueError('Legacy fill symbol conflicts')
            ids=[row[0] for row in db.execute('SELECT client_id FROM runtime_orders ORDER BY client_id')]
            for client in ids:
                key='pair_order_symbol:'+client
                current=meta(key)
                if current is not None and current!='BTCUSDT':
                    raise ValueError('Existing order symbol conflicts')
                if current is None:
                    db.execute('INSERT INTO runtime_meta VALUES(?,?)',(key,json.dumps('BTCUSDT')))
            previous=meta('pair_legacy_btc_migration')
            if previous is not None and previous!={'schema':1,'symbol':'BTCUSDT'}:
                raise ValueError('Migration identity conflicts')
            if previous is None:
                db.execute('INSERT INTO runtime_meta VALUES(?,?)',('pair_legacy_btc_migration',json.dumps({'schema':1,'symbol':'BTCUSDT'},sort_keys=True)))
            fault('before_commit')
            db.commit()
            fault('after_commit')
            return {'orders':len(ids),'symbol':'BTCUSDT','replayed':previous is not None}
        except BaseException:
            if db.in_transaction:db.rollback()
            raise

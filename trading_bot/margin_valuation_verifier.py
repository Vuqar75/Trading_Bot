"""Compare two private backup copies after exactly one paper allocation."""
import shutil
import json
import sqlite3
import tempfile
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from .paper_shared_store import SharedPaperStore
from .paper_wallet_live_report import live_report
from .paper_wallet_ledger import amount


def verify_allocation_value(before,after,token,*,now):
    if not isinstance(token,str) or not token.startswith('T-'):raise ValueError('Invalid transfer identity')
    before=Path(before).resolve(strict=True);after=Path(after).resolve(strict=True)
    if before==after:raise ValueError('Distinct backup files required')
    with closing(sqlite3.connect(before.as_uri()+'?mode=ro',uri=True)) as a,closing(sqlite3.connect(after.as_uri()+'?mode=ro',uri=True)) as b:
        for db in (a,b):
            db.execute('BEGIN')
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Backup integrity failure')
        old={r[0]:tuple(r) for r in a.execute('SELECT * FROM ledger_events')}
        new={r[0]:tuple(r) for r in b.execute('SELECT * FROM ledger_events')}
        if any(new.get(key)!=row for key,row in old.items()):raise ValueError('Original financial history changed')
        expected={'transfer:'+token,'shared:transfer-projection:'+token+':futures'}
        if set(new)-set(old)!=expected:raise ValueError('Unexpected financial events during allocation')
        transfer=b.execute('SELECT kind,details FROM ledger_events WHERE id=?',('transfer:'+token,)).fetchone()
        details=json.loads(transfer[1]) if transfer else {}
        postings=b.execute('SELECT account,asset,amount FROM ledger_postings WHERE event_id=? ORDER BY ordinal',('transfer:'+token,)).fetchall()
        if (not transfer or transfer[0]!='TRANSFER' or details.get('source')!='futures_isolated' or details.get('destination')!='margin_cross' or
            amount(details.get('quantity','NaN'))!=50 or [(p[0],p[1],amount(p[2])) for p in postings]!=[('futures_isolated:free','USDT',Decimal(-50)),('margin_cross:free','USDT',Decimal(50))]):
            raise ValueError('Allocation postings differ')
        for key in old:
            if a.execute('SELECT * FROM ledger_postings WHERE event_id=? ORDER BY ordinal',(key,)).fetchall()!=b.execute('SELECT * FROM ledger_postings WHERE event_id=? ORDER BY ordinal',(key,)).fetchall():
                raise ValueError('Original postings changed')
        old_quotes={table:a.execute('SELECT value FROM '+table+" WHERE key='quote'").fetchone() for table in ('runtime_meta','futures_meta')}
        cross=a.execute('SELECT * FROM cross_quotes ORDER BY symbol').fetchall()
        a.rollback();b.rollback()
    with tempfile.TemporaryDirectory(prefix='margin-fixed-price-') as directory:
        root=Path(directory);first=root/'before.db';second=root/'after.db'
        shutil.copyfile(before,first);shutil.copyfile(after,second)
        with closing(sqlite3.connect(second)) as db:
            for table,row in old_quotes.items():
                if row is None:raise ValueError('Missing fixed quote')
                db.execute('INSERT OR REPLACE INTO '+table+"(key,value) VALUES('quote',?)",(row[0],))
            db.execute('DELETE FROM cross_quotes')
            if cross:db.executemany('INSERT INTO cross_quotes VALUES('+','.join('?' for _ in cross[0])+')',cross)
            db.commit()
        a=SharedPaperStore(first);b=SharedPaperStore(second)
        try:
            left=live_report(a,now=now);right=live_report(b,now=now)
            if left['total'] is None or right['total'] is None or left['valuation_ok'] is not True or right['valuation_ok'] is not True:
                raise ValueError('Fixed-price valuation incomplete')
            if left['total']!=right['total']:raise ValueError('Capital changed after allocation')
            for key in ('spot_realized','futures_realized','spot_open','futures_open'):
                if left[key]!=right[key]:raise ValueError('Trading result changed after allocation')
            if left['cross'] and right['cross']:
                for key in ('realized','gross','fees','funding_total'):
                    if left['cross'][key]!=right['cross'][key]:raise ValueError('Cross result changed after allocation')
            return {'capital_before':str(left['total']),'capital_after':str(right['total']),'same_fixed_quotes':True,'history_preserved':True}
        finally:a.close();b.close()

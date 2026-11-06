"""Research-only immutable evidence for public USD-M funding publications.

This is not a funding-coverage oracle and does not apply financial events.
An independent, verified schedule and recovery policy are prerequisites for
advancing a position's funding cursor. No missing observation becomes zero.
"""
import json,sqlite3
from decimal import Decimal,InvalidOperation

def record(db,row,*,observed_ms):
 if not isinstance(row,dict) or row.get('symbol')!='BTCUSDT':raise ValueError('funding_symbol_invalid')
 t=row.get('fundingTime')
 if type(t) is not int or type(observed_ms) is not int or not 0<t<=observed_ms:raise ValueError('funding_time_invalid')
 try:rate=Decimal(row['fundingRate']);mark=Decimal(row['markPrice'])
 except (KeyError,InvalidOperation,TypeError,ValueError):raise ValueError('funding_value_invalid') from None
 if not rate.is_finite() or abs(rate)>=1 or not mark.is_finite() or mark<=0:raise ValueError('funding_value_invalid')
 payload=json.dumps({'symbol':'BTCUSDT','fundingTime':t,'fundingRate':str(rate),'markPrice':str(mark)},sort_keys=True,separators=(',',':'))
 db.execute('BEGIN IMMEDIATE')
 try:
  db.execute('CREATE TABLE IF NOT EXISTS funding_evidence(event_ms INTEGER PRIMARY KEY,payload TEXT NOT NULL,first_observed_ms INTEGER NOT NULL)')
  db.execute('CREATE TABLE IF NOT EXISTS funding_conflicts(event_ms INTEGER NOT NULL,payload TEXT NOT NULL,observed_ms INTEGER NOT NULL,UNIQUE(event_ms,payload))')
  old=db.execute('SELECT payload,first_observed_ms FROM funding_evidence WHERE event_ms=?',(t,)).fetchone()
  conflict=bool(old and old[0]!=payload)
  if conflict:db.execute('INSERT OR IGNORE INTO funding_conflicts VALUES(?,?,?)',(t,payload,observed_ms))
  elif not old:db.execute('INSERT INTO funding_evidence VALUES(?,?,?)',(t,payload,observed_ms))
  db.commit()
 except BaseException:
  db.rollback();raise
 if conflict:raise ValueError('funding_event_changed_requires_review')
 return {'replayed':bool(old),'first_observed_ms':old[1] if old else observed_ms,'event_ms':t}

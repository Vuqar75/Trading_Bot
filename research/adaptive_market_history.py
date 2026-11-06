"""Durable immutable versions of publicly observed research candles."""
import hashlib,json,sqlite3

def ingest(db,*,market,candles,observed_ms):
 if market not in ('spot','futures') or not isinstance(candles,dict) or candles.get('market')!=market or candles.get('symbol')!='BTCUSDT' or type(observed_ms) is not int:raise ValueError('market_evidence_invalid')
 expected='binance_spot_klines' if market=='spot' else 'binance_usdm_klines'
 if candles.get('source')!=expected:raise ValueError('market_source_invalid')
 rows=candles.get('bars',[])+[candles.get('skip_bar')]
 if not rows or any(not isinstance(row,(list,tuple)) or len(row)!=5 or type(row[0]) is not int or row[0]+900000>observed_ms for row in rows) or any(b[0]-a[0]!=900000 for a,b in zip(rows,rows[1:])):raise ValueError('market_candle_sequence_invalid')
 db.execute('BEGIN IMMEDIATE');conflicts=[]
 try:
  db.execute('CREATE TABLE IF NOT EXISTS market_candles(market TEXT NOT NULL,start_ms INTEGER NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,first_received_ms INTEGER NOT NULL,PRIMARY KEY(market,start_ms))')
  db.execute('CREATE TABLE IF NOT EXISTS market_conflicts(market TEXT NOT NULL,start_ms INTEGER NOT NULL,payload TEXT NOT NULL,observed_ms INTEGER NOT NULL,UNIQUE(market,start_ms,payload))')
  for row in rows:
   payload=json.dumps(list(row),separators=(',',':'),allow_nan=False);sha=hashlib.sha256(payload.encode()).hexdigest()
   old=db.execute('SELECT sha256 FROM market_candles WHERE market=? AND start_ms=?',(market,row[0])).fetchone()
   if old and old[0]!=sha:
    db.execute('INSERT OR IGNORE INTO market_conflicts VALUES(?,?,?,?)',(market,row[0],payload,observed_ms));conflicts.append(row[0])
   elif not old:db.execute('INSERT INTO market_candles VALUES(?,?,?,?,?)',(market,row[0],payload,sha,observed_ms))
  blocked=bool(db.execute('SELECT 1 FROM market_conflicts WHERE market=? LIMIT 1',(market,)).fetchone());db.commit()
 except BaseException:
  db.rollback();raise
 return {'market':market,'observed':len(rows),'changed':len(conflicts),'entry_blocked':blocked}

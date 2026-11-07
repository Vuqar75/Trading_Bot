"""Append-only, financially guarded versioned market evidence for the study."""
import hashlib,json,sqlite3,time,tempfile,os,stat
from contextlib import ExitStack,closing
from decimal import Decimal
from pathlib import Path
from .adaptive_exposure_guard import require_clear
from .adaptive_study_supervisor import cycle_lock
from trading_bot.paper_wallet_ledger import PaperWalletLedger

def _schema(db):
 db.execute('CREATE TABLE IF NOT EXISTS market_epochs(id INTEGER PRIMARY KEY,market TEXT NOT NULL,start_ms INTEGER NOT NULL,created_ms INTEGER NOT NULL,proof TEXT NOT NULL,source TEXT NOT NULL)')
 db.execute('CREATE TABLE IF NOT EXISTS epoch_candles(epoch_id INTEGER NOT NULL,start_ms INTEGER NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,first_received_ms INTEGER NOT NULL,PRIMARY KEY(epoch_id,start_ms))')
 db.execute('CREATE TABLE IF NOT EXISTS epoch_conflicts(epoch_id INTEGER NOT NULL,start_ms INTEGER NOT NULL,payload TEXT NOT NULL,observed_ms INTEGER NOT NULL,UNIQUE(epoch_id,start_ms,payload))')

def start_epoch(root,*,market,clock=lambda:int(time.time()*1000)):
 if market!='futures':raise ValueError('only_futures_epoch_supported')
 root=Path(root)
 if root.is_symlink() or not root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):raise ValueError('sandbox_root_required')
 info=root.stat()
 if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:raise ValueError('untrusted_root')
 with cycle_lock(root),ExitStack() as stack:
  proof={}
  for name in ('ai','baseline'):
   base=root/name
   f=stack.enter_context(closing(sqlite3.connect((base/'financial.db').resolve(strict=True).as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=5)));f.row_factory=sqlite3.Row
   f.execute('ATTACH DATABASE ? AS route_queue',(str((base/'queue.db').resolve(strict=True)),));f.execute('BEGIN IMMEDIATE');require_clear(f)
   r=stack.enter_context(closing(sqlite3.connect((base/'route.db').resolve(strict=True).as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=5)));r.row_factory=sqlite3.Row;r.execute('BEGIN IMMEDIATE')
   route=r.execute('SELECT phase FROM route_state WHERE id=1').fetchone()
   if not route or route[0]!='IDLE' or r.execute('SELECT 1 FROM route_exit_pending LIMIT 1').fetchone():raise ValueError('route_not_flat')
   ledger=object.__new__(PaperWalletLedger);ledger.db=f;ledger.fault=lambda _:None
   if not ledger.reconcile()['balanced']:raise ValueError('ledger_unbalanced')
   if f.execute("SELECT 1 FROM transfer_proposals WHERE status='OPEN' LIMIT 1").fetchone():raise ValueError('transfer_pending')
   def meta(table,key):
    row=f.execute('SELECT value FROM '+table+' WHERE key=?',(key,)).fetchone()
    if not row:raise ValueError('missing_balance')
    value=Decimal(str(json.loads(row[0])))
    if not value.is_finite():raise ValueError('invalid_balance')
    return str(value)
   proof[name]={'spot_cash':meta('runtime_meta','cash'),'futures_cash':meta('futures_meta','cash'),'ledger_events':f.execute('SELECT count(*) FROM ledger_events').fetchone()[0]}
  stamp=clock()
  if type(stamp) is not int:raise ValueError('invalid_time')
  e=stack.enter_context(closing(sqlite3.connect((root/'market-evidence.db').resolve(strict=True).as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=5)));e.execute('BEGIN IMMEDIATE');_schema(e)
  prior=e.execute('SELECT id,start_ms FROM market_epochs WHERE market=? ORDER BY id DESC LIMIT 1',(market,)).fetchone()
  if prior and (stamp<=prior[1] or not e.execute('SELECT 1 FROM epoch_conflicts WHERE epoch_id=? LIMIT 1',(prior[0],)).fetchone()):raise ValueError('epoch_already_active')
  data=json.dumps(proof,sort_keys=True,separators=(',',':'));source='binance_usdm_klines_v2'
  e.execute('INSERT INTO market_epochs(market,start_ms,created_ms,proof,source) VALUES(?,?,?,?,?)',(market,stamp,stamp,data,source));e.commit()
  return {'start_ms':stamp,'market':market,'source':source,'financial_proof_sha256':hashlib.sha256(data.encode()).hexdigest(),'proof':proof}

def ingest_epoch(db,*,market,candles,observed_ms):
 if market!='futures' or candles.get('market')!=market or candles.get('symbol')!='BTCUSDT' or candles.get('source')!='binance_usdm_klines':raise ValueError('invalid_market')
 rows=candles.get('bars',[])+[candles.get('skip_bar')];warm=candles.get('warm_received_ms',observed_ms)
 if type(observed_ms) is not int or type(warm) is not int or warm>observed_ms or not rows or any(not isinstance(x,(tuple,list)) or len(x)!=5 or type(x[0]) is not int or x[0]%900000 or x[0]+900000>(observed_ms if i==len(rows)-1 else warm) for i,x in enumerate(rows)) or any(b[0]-a[0]!=900000 for a,b in zip(rows,rows[1:])):raise ValueError('invalid_candles')
 db.execute('BEGIN IMMEDIATE')
 try:
  _schema(db);epoch=db.execute('SELECT id,start_ms FROM market_epochs WHERE market=? ORDER BY id DESC LIMIT 1',(market,)).fetchone()
  if not epoch:raise ValueError('no_epoch')
  eid,start=epoch
  for i,row in enumerate(rows):
   raw=json.dumps(list(row),separators=(',',':'),allow_nan=False);digest=hashlib.sha256(raw.encode()).hexdigest();old=db.execute('SELECT sha256 FROM epoch_candles WHERE epoch_id=? AND start_ms=?',(eid,row[0])).fetchone()
   if old and old[0]!=digest:db.execute('INSERT OR IGNORE INTO epoch_conflicts VALUES(?,?,?,?)',(eid,row[0],raw,observed_ms))
   elif not old:db.execute('INSERT INTO epoch_candles VALUES(?,?,?,?,?)',(eid,row[0],raw,digest,observed_ms if i==len(rows)-1 else warm))
  conflict=bool(db.execute('SELECT 1 FROM epoch_conflicts WHERE epoch_id=? LIMIT 1',(eid,)).fetchone())
  db.commit();return {'epoch_id':eid,'start_ms':start,'conflict':conflict,'new_signal':len(rows)>1 and rows[-2][0]+900000>start,'entry_blocked':conflict or len(rows)<2 or rows[-2][0]+900000<=start}
 except BaseException:db.rollback();raise

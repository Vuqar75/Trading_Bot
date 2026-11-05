"""Read-only preconditions; never change a live financial or queue database."""
import json,sqlite3
from decimal import Decimal
from pathlib import Path
from contextlib import closing

def check(financial,queue):
 with closing(sqlite3.connect(Path(financial).resolve().as_uri()+'?mode=ro',uri=True)) as d:
  def meta(key):
   r=d.execute('select value from runtime_meta where key=?',(key,)).fetchone();return json.loads(r[0]) if r else None
  identity=meta('identity');position=meta('position');interval=meta('selected_interval')
  if not isinstance(identity,dict) or identity.get('mode')!='paper' or identity.get('symbol','BTCUSDT')!='BTCUSDT' or Decimal(identity.get('amount','0'))!=10 or interval not in (None,'15m'):raise ValueError('Spot setting changed')
  if not isinstance(position,dict) or Decimal(position['quantity'])!=0 or not position.get('fees_known'):raise ValueError('Spot position or fees require review')
  if meta('donchian60') is not None:raise ValueError('Donchian already initialized')
  if meta('paused') is not True or d.execute("select count(*) from runtime_orders where phase!='SETTLED'").fetchone()[0]:raise ValueError('Spot not safely paused')
  amounts={}
  for account,asset,value in d.execute("select account,asset,amount from ledger_postings where account in ('spot:reserved','spot:position')"):amounts[(account,asset)]=amounts.get((account,asset),Decimal(0))+Decimal(value)
  if any(amounts.values()):raise ValueError('Spot reservation exists')
  if d.execute("select count(*) from transfer_proposals where status='OPEN'").fetchone()[0]:raise ValueError('Transfer proposal requires review')
 with closing(sqlite3.connect(Path(queue).resolve().as_uri()+'?mode=ro',uri=True)) as q:
  if q.execute("select count(*) from unified_jobs where status='PENDING'").fetchone()[0] or q.execute("select count(*) from unified_proposals where status='OPEN'").fetchone()[0] or q.execute("select count(*) from unified_state where key like 'pair-proposal:%' and json_extract(value,'$.status')='OPEN'").fetchone()[0]:raise ValueError('Queued operation requires review')
 return True

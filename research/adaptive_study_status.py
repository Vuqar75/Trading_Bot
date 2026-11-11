"""Read-only study account status. Unknown valuation stays unknown."""
import json,sqlite3
from contextlib import closing
from decimal import Decimal,localcontext
from pathlib import Path
from .adaptive_market_proof import quote as valid_quote
from .adaptive_router_status import status as route_status

def snapshot(path,*,quotes,now_ms):
 path=Path(path);manifest=json.loads((path/'manifest.json').read_text())
 def meta(db,table,key,default=None):
  row=db.execute('SELECT value FROM '+table+' WHERE key=?',(key,)).fetchone()
  return json.loads(row[0]) if row else default
 with closing(sqlite3.connect((path/'financial.db').resolve().as_uri()+'?mode=ro',uri=True)) as db:
  db.execute('BEGIN');db.row_factory=sqlite3.Row
  cash=Decimal(meta(db,'runtime_meta','cash'))+Decimal(meta(db,'futures_meta','cash'))
  realised=Decimal(meta(db,'runtime_meta','pnl'))+Decimal(meta(db,'futures_meta','pnl'))
  spot=meta(db,'runtime_meta','position');future=meta(db,'futures_meta','position')
  quantity=Decimal(spot['quantity']);opened=[];unrealised=Decimal(0);valuation_known=True;funding_known=True;test_open_net=None;test_position=False
  if quantity:
   opened.append({'market':'spot','direction':'LONG','quantity':str(quantity)})
   try:
    q=valid_quote(quotes['spot'],market='spot',now_ms=now_ms);price=Decimal(q['price'])*Decimal('.9995')
    if not spot.get('fees_known'):raise ValueError('fee_unknown')
    unrealised+=quantity*price*Decimal('.999')-Decimal(spot['cost'])
   except (KeyError,ValueError,TypeError):valuation_known=False
  if future:
   for key,payload in db.execute("SELECT business_key,payload FROM futures_events WHERE event='OPEN' AND business_key LIKE 'operator-test:%'"):
    if json.loads(payload).get('id')==future.get('id'):test_position=True;break
   opened.append({'market':'futures','direction':future['direction'],'quantity':future['quantity'],'classification':'execution_test' if test_position else 'strategy'})
   try:
    q=valid_quote(quotes['futures'],market='futures',now_ms=now_ms)
    if future['direction'] not in ('LONG','SHORT'):raise ValueError('unsupported_direction')
    price=Decimal(q['price'])*(Decimal('1.0005') if future['direction']=='SHORT' else Decimal('.9995'))
    cursor=future.get('funding_cursor',-1)
    if cursor<now_ms:
     exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='adaptive_funding_schedule'").fetchone()
     due=db.execute('SELECT next_due_ms,observed_ms FROM adaptive_funding_schedule WHERE position_id=?',(future['id'],)).fetchone() if exists else None
     if not due or due[0]<=now_ms or not 0<=now_ms-cursor<=5000 or not 0<=now_ms-due[1]<=5000:raise ValueError('funding_unknown')
    qty=Decimal(future['quantity']);margin=Decimal(future['margin']);gross=(Decimal(future['entry'])-price)*qty*(1 if future['direction']=='SHORT' else -1)
    net=margin+gross+Decimal(future['funding'])-Decimal(future['entry_fee'])-qty*price*Decimal('.001')
    if test_position:test_open_net=max(Decimal(0),net)-margin
    else:unrealised+=max(Decimal(0),net)-margin
   except (KeyError,ValueError,TypeError):valuation_known=False;funding_known=False
  sums={};counts={}
  with localcontext() as context:
   context.prec=120
   for r in db.execute('SELECT event_id,asset,amount FROM ledger_postings'):
    key=(r['event_id'],r['asset']);sums[key]=sums.get(key,Decimal(0))+Decimal(r['amount']);counts[r['event_id']]=counts.get(r['event_id'],0)+1
  balanced=all(x==0 for x in sums.values()) and all(counts.get(r['id'],0)==r['posting_count'] for r in db.execute('SELECT id,posting_count FROM ledger_events'))
  spot_fees=Decimal(0);fees_known=True
  for row in db.execute('SELECT payload FROM runtime_fills'):
   fill=json.loads(row[0])
   if 'commission' not in fill:fees_known=False;continue
   fee=Decimal(str(fill['commission']))
   if fee and fill.get('commissionAsset')!='USDT':fees_known=False
   else:spot_fees+=fee
  closed_futures=[];test_closes=[]
  for key,payload in db.execute("SELECT business_key,payload FROM futures_events WHERE event='CLOSE'"):
   item=json.loads(payload)
   if key.startswith('operator-test:') and key.endswith(':exit') and item.get('reason')=='TEST':test_closes.append(dict(item,entry_action=key[:-5],exit_action=key))
   else:closed_futures.append(item)
  # The actual paper cash keeps every test fee and PnL. These adjustments are
  # only the separately labelled strategy statistics; no ledger is changed.
  realised-=sum((Decimal(x['net']) for x in test_closes),Decimal(0))
  fees=spot_fees+sum((Decimal(x['fees']) for x in closed_futures),Decimal(0))
  funding=sum((Decimal(x['funding']) for x in closed_futures),Decimal(0))
  if future and not test_position:fees+=Decimal(future['entry_fee']);funding+=Decimal(future['funding'])
  closes=db.execute('SELECT count(*) FROM runtime_closes').fetchone()[0]+len(closed_futures)
  conflicts=bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='funding_conflicts'").fetchone() and db.execute('SELECT 1 FROM funding_conflicts LIMIT 1').fetchone())
  if conflicts:valuation_known=False
 with closing(sqlite3.connect((path/'observations.db').resolve().as_uri()+'?mode=ro',uri=True)) as obs:
  observations=[json.loads(r[0]) for r in obs.execute('SELECT payload FROM study_decisions ORDER BY data_ms')]
  if obs.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='study_epoch_decisions'").fetchone():
   observations.extend(json.loads(r[0]) for r in obs.execute('SELECT payload FROM study_epoch_decisions ORDER BY data_ms'))
   observations.sort(key=lambda item:item['data_ms'])
  blocked_count=obs.execute("SELECT count(*) FROM samples WHERE status IN ('blocked','entry_blocked','decision_unavailable')").fetchone()[0]
  last_blocked=obs.execute("SELECT payload FROM samples WHERE status IN ('blocked','entry_blocked','decision_unavailable') ORDER BY id DESC LIMIT 1").fetchone()
 known=[Decimal(x['estimated_api_usd']) for x in observations if x.get('estimated_api_usd') is not None]
 unknown=sum(x['source']=='AI' and x.get('estimated_api_usd') is None for x in observations)
 route=route_status(path/'route.db',path/'queue.db',now_ms=now_ms)
 net=realised+unrealised if valuation_known else None
 after_api=(net-sum(known,Decimal(0))) if net is not None and manifest['source']=='AI' and known and not unknown else None
 blocked=json.loads(last_blocked[0]) if last_blocked else None
 latest=observations[-1] if observations else None
 return {'source':manifest['source'],'start_ms':manifest['start_ms'],'checked_ms':now_ms,'cash_usdt':str(cash),'positions':opened,'exposure_valid':len(opened)<=1,'realised_pnl_usdt':str(realised) if not conflicts else None,'open_pnl_usdt':str(unrealised) if valuation_known else None,'net_pnl_usdt':str(net) if net is not None else None,'net_after_known_api_usd':str(after_api) if after_api is not None else None,'fees_usdt':str(fees) if fees_known else None,'funding_usdt':str(funding) if not conflicts and funding_known else None,'closed_trades':closes,'operator_test_closes':test_closes,'operator_test_open_net':str(test_open_net) if test_open_net is not None else None,'ledger_balanced':balanced,'funding_conflict':conflicts,'decisions':len(observations),'latest_decision':latest,'recorded_blocked_checks':blocked_count,'latest_blocked_check':blocked,'known_estimated_api_usd':str(sum(known,Decimal(0))) if known else None,'unknown_api_usage':unknown,'provider_bill_confirmed':False,'route':route}

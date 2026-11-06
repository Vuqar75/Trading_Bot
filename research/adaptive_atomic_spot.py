"""Offline-only Spot entry: real paper journal, guarded under SQLite write lock.

This is not wired into the trading bot. It refuses non-temporary databases and
non-paper exchanges. It currently supports only an entry, not a strategy worker.
"""
import copy,sqlite3,tempfile,time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from .adaptive_exposure_guard import require_clear
from .adaptive_route_policy import route,closed_bar_signal,decision_digest,candle_digest
from trading_bot.runtime_exchange import PaperExchange,order_quantity
from trading_bot.runtime_store import RuntimeStore

def enter(store,queue_path,*,job_id,action_id,decision,snapshot_id,data_ms,decision_version,
          bars,skip_bar,quote,quote_ms,filters,filters_ms,decision_source,clock=lambda:int(time.time()*1000),fault=lambda stage:None):
 """Prepare/claim one Spot order; retry after uncertain stages requires recovery."""
 if type(store) is not RuntimeStore or not store.shared:raise ValueError('shared_spot_store_required')
 dbfile=Path(store.db.execute('PRAGMA database_list').fetchone()[2]);queue_path=Path(queue_path)
 root=Path(tempfile.gettempdir()).resolve()
 if (dbfile.is_symlink() or queue_path.is_symlink() or not dbfile.resolve().is_relative_to(root)
     or queue_path.resolve().parent!=dbfile.resolve().parent):raise ValueError('sandbox_path_required')
 if not isinstance(bars,list) or not bars or not isinstance(skip_bar,(list,tuple)) or len(skip_bar)!=5:raise ValueError('invalid_bars')
 strategy='range' if decision.get('mode')=='RANGE' else 'trend1'
 signal=closed_bar_signal(bars,strategy,now_ms=bars[-1][0]+900000)
 if strategy=='trend1' and (not signal or signal[0]!='TREND' or not signal[1]>signal[2]):raise ValueError('no_long_signal')
 if strategy=='range' and (not signal or signal[0]!='BUY'):raise ValueError('no_range_signal')
 expected_ms=bars[-1][0]+1800000
 if skip_bar[0]!=bars[-1][0]+900000:raise ValueError('skip_bar_mismatch')
 closed_bar_signal([skip_bar],'range',now_ms=expected_ms)
 value=Decimal(str(quote))
 if not value.is_finite() or value<=0:raise ValueError('invalid_quote')
 if strategy=='range':
  lo,hi,mid=map(Decimal,map(str,signal[1:]))
  if not (lo<=value<=hi and Decimal(str(skip_bar[3]))>=lo and Decimal(str(skip_bar[2]))<=hi and mid/value-1>=Decimal('.006')):raise ValueError('range_entry_invalid')
 filters=copy.deepcopy(filters)
 quantity=order_quantity(filters,Decimal('10')/(value*Decimal('1.0005')),value)
 price=value*Decimal('1.0005')
 original=store.transaction
 attached={r[1] for r in store.db.execute('PRAGMA database_list')}
 if 'route_queue' in attached:raise ValueError('queue_already_attached')
 store.db.execute('ATTACH DATABASE ? AS route_queue',(str(queue_path.resolve()),))
 @contextmanager
 def locked_check(*,own=False):
  with original():
   prior=store.db.execute('SELECT side,quantity,price,reason FROM runtime_orders WHERE business_key=?',(action_id,)).fetchone()
   if prior and not own:
    if (prior['side']!='BUY' or Decimal(prior['quantity'])!=Decimal(quantity) or
        Decimal(prior['price'])!=price or prior['reason']!='ADAPTIVE_RESEARCH'):raise ValueError('action_identity_conflict')
    yield
    return
   now=clock()
   admission=route(decision,snapshot_id=snapshot_id,data_ms=data_ms,now_ms=now,symbol='BTCUSDT',decision_version=decision_version,market='spot',eligible=True,research_sandbox=True)
   if not admission['allowed'] or admission['strategy']!=strategy:raise ValueError(admission.get('reason','route_changed'))
   if type(quote_ms) is not int or not 0<=now-quote_ms<=5000 or not expected_ms<=now<=expected_ms+5000:raise ValueError('stale_quote_or_signal')
   if type(filters_ms) is not int or not 0<=now-filters_ms<=120000:raise ValueError('stale_filters')
   job=store.db.execute('SELECT market,command,status,payload FROM route_queue.unified_jobs WHERE id=?',(job_id,)).fetchone()
   if not job or (job['market'],job['command'],job['status'])!=('spot','/research_adaptive_entry','PENDING'):raise ValueError('queue_job_invalid')
   import json
   payload=json.loads(job['payload'])
   if payload.get('action_id')!=action_id or payload.get('decision_sha256')!=decision_digest(decision) or payload.get('candles_sha256')!=candle_digest(bars,skip_bar):raise ValueError('queue_identity_mismatch')
   latest=store.db.execute("SELECT value FROM route_queue.unified_state WHERE key='adaptive:latest'").fetchone()
   current=json.loads(latest[0]) if latest else {}
   if store.db.execute("SELECT 1 FROM route_queue.unified_state WHERE key='adaptive:decision_conflict'").fetchone():raise ValueError('decision_conflict_requires_review')
   if current.get('sha256')!=decision_digest(decision) or current.get('symbol')!='BTCUSDT' or current.get('version')!=decision_version or current.get('source')!=decision_source:raise ValueError('decision_superseded')
   require_clear(store.db,own_job_id=job_id,own_business_key=action_id if own else None)
   from .adaptive_experiment_fence import authorize,clear
   authorize(store.db,action_id,'spot')
   try:yield
   finally:clear(store.db,action_id,'spot')
 try:
  store.transaction=lambda:locked_check()
  try:client=store.prepare('BUY',quantity,price,action_id,'ADAPTIVE_RESEARCH',expected_symbol='BTCUSDT',expected_pair_version=store.get('pair_selection_version',0))
  finally:store.transaction=original
  if client is None:
   prior=store.db.execute('SELECT phase,status FROM runtime_orders WHERE business_key=?',(action_id,)).fetchone()
   if not prior:raise ValueError('existing_action_missing')
   if prior['phase']=='SETTLED' and prior['status']=='FILLED':return {'status':'filled_existing'}
   if prior['phase']=='SETTLED':return {'status':'not_filled'}
   return {'status':'reconciliation_required'}
  fault('after_prepare')
  store.transaction=lambda:locked_check(own=True)
  try:claimed=store.claim(client,expected_symbol='BTCUSDT',expected_pair_version=store.get('pair_selection_version',0))
  finally:store.transaction=original
  if claimed is None:return {'status':'not_claimed'}
  fault('after_claim')
  exchange=PaperExchange();exchange.submit(client,'BUY',quantity)
  fault('after_submit')
  saved=store.db.execute('SELECT * FROM runtime_orders WHERE client_id=?',(client,)).fetchone()
  store.apply(client,exchange.inspect_saved(saved))
  fault('after_fill')
  return {'status':'filled','client_id':client}
 finally:
  store.transaction=original
  store.db.execute('DETACH DATABASE route_queue')

def exit_on_stop(store,queue_path,*,route_state,job_id,action_id,position_id,quote,quote_ms,bars=None,skip_bar=None,risk_bar=None,
                 clock=lambda:int(time.time()*1000),fault=lambda stage:None):
 """Offline trend/range protection; does not require any AI response."""
 if type(store) is not RuntimeStore or not store.shared:raise ValueError('shared_spot_store_required')
 dbfile=Path(store.db.execute('PRAGMA database_list').fetchone()[2]);queue_path=Path(queue_path);root=Path(tempfile.gettempdir()).resolve()
 if dbfile.is_symlink() or queue_path.is_symlink() or not dbfile.resolve().is_relative_to(root) or queue_path.resolve().parent!=dbfile.resolve().parent:raise ValueError('sandbox_path_required')
 price=Decimal(str(quote))
 if not price.is_finite() or price<=0:raise ValueError('invalid_quote')
 from .adaptive_route_state import RouteState
 if type(route_state) is not RouteState or route_state.path.resolve().parent!=dbfile.resolve().parent:raise ValueError('sandbox_route_required')
 prior=store.db.execute('SELECT reason FROM runtime_orders WHERE business_key=?',(action_id,)).fetchone()
 state=route_state.status();strategy=state.get('strategy');target=None;entry_ms=None;reference=price
 if prior:
  reason=prior['reason']
  if reason not in ('ADAPTIVE_STOP','ADAPTIVE_TARGET','ADAPTIVE_TIME','ADAPTIVE_SIGNAL'):raise ValueError('action_identity_conflict')
 else:
  pos=store.get('position')
  if strategy not in ('trend1','range'):raise ValueError('route_strategy_missing')
  action=route_state.db.execute('SELECT signal_json FROM route_actions WHERE action_id=?',(state['action_id'],)).fetchone()
  import json
  signal_data=json.loads(action[0]) if action else {};entry_ms=signal_data.get('entry_boundary_ms')
  if strategy=='range':target=Decimal(signal_data['target'])
  threshold=Decimal(pos['entry_price'])*(Decimal('.99') if strategy=='range' else Decimal('.98'))
  if risk_bar is not None:
   closed_bar_signal([risk_bar],'range',now_ms=risk_bar[0]+900000)
   if type(entry_ms) is not int or risk_bar[0]<entry_ms or not risk_bar[0]+900000<=clock()<=risk_bar[0]+905000:raise ValueError('risk_bar_stale')
  if price<=threshold or (risk_bar is not None and Decimal(str(risk_bar[3]))<=threshold):
   reason='ADAPTIVE_STOP';reference=min(price,threshold,Decimal(str(risk_bar[1])) if risk_bar is not None else price)
  elif strategy=='range' and target is not None and (price>=target or (risk_bar is not None and Decimal(str(risk_bar[2]))>=target)):
   reason='ADAPTIVE_TARGET';reference=min(price,target)
  elif type(entry_ms) is int and clock()-entry_ms>=(86400000 if strategy=='range' else 604800000):reason='ADAPTIVE_TIME'
  elif strategy=='trend1' and bars and skip_bar:
   sig=closed_bar_signal(bars,'trend1',now_ms=bars[-1][0]+900000)
   if not sig or sig[0]!='TREND' or not sig[1]<sig[4] or skip_bar[0]!=bars[-1][0]+900000:raise ValueError('protective_exit_not_met')
   closed_bar_signal([skip_bar],'range',now_ms=skip_bar[0]+900000);reason='ADAPTIVE_SIGNAL'
  else:raise ValueError('protective_exit_not_met')
 original=store.transaction;store.db.execute('ATTACH DATABASE ? AS route_queue',(str(queue_path.resolve()),))
 @contextmanager
 def locked_check(*,own=False):
  with original():
   prior=store.db.execute('SELECT side,reason FROM runtime_orders WHERE business_key=?',(action_id,)).fetchone()
   if prior and not own:
    if (prior['side'],prior['reason'])!=('SELL',reason):raise ValueError('action_identity_conflict')
    yield;return
   now=clock()
   if type(quote_ms) is not int or not 0<=now-quote_ms<=5000:raise ValueError('stale_protection_quote')
   job=store.db.execute('SELECT market,command,status,payload FROM route_queue.unified_jobs WHERE id=?',(job_id,)).fetchone()
   import json
   if not job or (job['market'],job['command'],job['status'])!=('spot','/research_adaptive_exit','PENDING') or json.loads(job['payload']).get('action_id')!=action_id:raise ValueError('queue_job_invalid')
   expected_bar=list(risk_bar) if risk_bar is not None else None
   if json.loads(job['payload']).get('risk_bar')!=expected_bar:raise ValueError('risk_bar_identity_mismatch')
   if risk_bar is not None and not risk_bar[0]+900000<=now<=risk_bar[0]+905000:raise ValueError('risk_bar_stale')
   pos=store.get('position')
   state=route_state.status()
   if state['phase'] not in ('OPEN','EXIT_PENDING') or (state['market'],state['strategy'],state['position_id'])!=('spot',strategy,position_id):raise ValueError('route_position_mismatch')
   if pos.get('id')!=position_id or Decimal(pos.get('quantity','0'))<=0:raise ValueError('position_changed')
   if reason=='ADAPTIVE_TARGET':
    if strategy!='range' or target is None or (price<target and (risk_bar is None or Decimal(str(risk_bar[2]))<target)):raise ValueError('range_target_not_met')
   elif reason=='ADAPTIVE_STOP' and price>Decimal(pos['entry_price'])*(Decimal('.99') if strategy=='range' else Decimal('.98')) and (risk_bar is None or Decimal(str(risk_bar[3]))>Decimal(pos['entry_price'])*(Decimal('.99') if strategy=='range' else Decimal('.98'))):raise ValueError('protective_stop_not_met')
   elif reason=='ADAPTIVE_TIME' and (type(entry_ms) is not int or now-entry_ms<(86400000 if strategy=='range' else 604800000)):raise ValueError('time_exit_not_due')
   elif reason=='ADAPTIVE_SIGNAL' and (not bars or not skip_bar or not skip_bar[0]+900000<=now<=skip_bar[0]+905000):raise ValueError('signal_exit_window_missed')
   yield
 try:
  position=store.get('position');quantity=position['quantity']
  store.transaction=lambda:locked_check()
  try:client=store.prepare('SELL',quantity,reference*Decimal('.9995'),action_id,reason,target=position_id,expected_symbol='BTCUSDT',expected_pair_version=store.get('pair_selection_version',0))
  finally:store.transaction=original
  if client is None:
   row=store.db.execute('SELECT phase,status FROM runtime_orders WHERE business_key=?',(action_id,)).fetchone()
   return {'status':'filled_existing' if row and row['phase']=='SETTLED' and row['status']=='FILLED' else 'reconciliation_required'}
  fault('after_prepare')
  # A fresh price and position must be checked after any wait on the financial DB.
  store.transaction=lambda:locked_check(own=True)
  try:store.claim(client,expected_symbol='BTCUSDT',expected_pair_version=store.get('pair_selection_version',0))
  finally:store.transaction=original
  fault('after_claim');exchange=PaperExchange();exchange.submit(client,'SELL',quantity);fault('after_submit')
  saved=store.db.execute('SELECT * FROM runtime_orders WHERE client_id=?',(client,)).fetchone();store.apply(client,exchange.inspect_saved(saved));fault('after_fill')
  return {'status':'filled','client_id':client}
 finally:
  store.transaction=original;store.db.execute('DETACH DATABASE route_queue')

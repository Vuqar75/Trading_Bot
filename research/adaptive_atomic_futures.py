"""Isolated research SHORT entry through the existing futures paper ledger."""
import copy,json,tempfile,time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from .adaptive_exposure_guard import require_clear
from .adaptive_route_policy import route,closed_bar_signal,decision_digest,candle_digest
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_pair_catalog import usd_m_perpetual_candidate

def enter_short(store,queue_path,*,job_id,action_id,decision,snapshot_id,data_ms,decision_version,
                bars,skip_bar,quote,quote_ms,filters,filters_ms,funding_evidence,decision_source,clock=lambda:int(time.time()*1000),fault=lambda stage:None):
 if type(store) is not FuturesStore or not store.shared or store.symbol!='BTCUSDT':raise ValueError('shared_isolated_store_required')
 if store.fee!=Decimal('10') or store.slip!=Decimal('5'):raise ValueError('unexpected_execution_costs')
 dbfile=Path(store.db.execute('PRAGMA database_list').fetchone()[2]);queue_path=Path(queue_path);root=Path(tempfile.gettempdir()).resolve()
 if dbfile.is_symlink() or queue_path.is_symlink() or not dbfile.resolve().is_relative_to(root) or queue_path.resolve().parent!=dbfile.resolve().parent:raise ValueError('sandbox_path_required')
 if not isinstance(bars,list) or not bars or not isinstance(skip_bar,(tuple,list)) or len(skip_bar)!=5:raise ValueError('invalid_bars')
 sig=closed_bar_signal(bars,'trend1',now_ms=bars[-1][0]+900000)
 if not sig or sig[0]!='TREND' or not sig[1]<sig[3]:raise ValueError('no_short_signal')
 expected_ms=bars[-1][0]+1800000
 if skip_bar[0]!=bars[-1][0]+900000:raise ValueError('skip_bar_mismatch')
 closed_bar_signal([skip_bar],'range',now_ms=expected_ms)
 mark=Decimal(str(quote))
 if not mark.is_finite() or mark<=0:raise ValueError('invalid_quote')
 fill=mark*(1-store.slip/10000)
 filters=copy.deepcopy(filters);info=usd_m_perpetual_candidate(filters,'BTCUSDT',Decimal('10')/fill,fill)
 quantity=Decimal(info['quantity']);margin=quantity*fill
 if not 0<margin<=10:raise ValueError('budget_invalid')
 original=store.transaction;original_margin=store.margin;store.db.execute('ATTACH DATABASE ? AS route_queue',(str(queue_path.resolve()),))
 def checked_clock():
  now=clock();admission=route(decision,snapshot_id=snapshot_id,data_ms=data_ms,now_ms=now,symbol='BTCUSDT',decision_version=decision_version,market='futures',eligible=True,research_sandbox=True)
  if not admission['allowed'] or admission['strategy']!='trend1':raise ValueError(admission.get('reason','route_changed'))
  if type(quote_ms) is not int or not 0<=now-quote_ms<=5000 or not expected_ms<=now<=expected_ms+5000:raise ValueError('stale_quote_or_signal')
  if type(filters_ms) is not int or not 0<=now-filters_ms<=120000:raise ValueError('stale_filters')
  if (not isinstance(funding_evidence,dict) or funding_evidence.get('symbol')!='BTCUSDT' or funding_evidence.get('complete') is not True or
      type(funding_evidence.get('observed_ms')) is not int or not 0<=now-funding_evidence['observed_ms']<=120000 or
      type(funding_evidence.get('next_due_ms')) is not int or funding_evidence['next_due_ms']<=now or
      funding_evidence.get('source') not in ('synthetic','public')):raise ValueError('funding_evidence_unavailable')
  return now
 @contextmanager
 def locked_check():
  with original():
   old=store.db.execute('SELECT event,payload FROM futures_events WHERE business_key=?',(action_id,)).fetchone()
   if old:
    payload=json.loads(old['payload'])
    if old['event']!='OPEN' or payload.get('direction')!='SHORT' or Decimal(payload['quantity'])!=quantity or Decimal(payload['entry'])!=fill:raise ValueError('action_identity_conflict')
    yield;return
   checked_clock()
   job=store.db.execute('SELECT market,command,status,payload FROM route_queue.unified_jobs WHERE id=?',(job_id,)).fetchone()
   if not job or (job['market'],job['command'],job['status'])!=('futures','/research_adaptive_entry','PENDING'):raise ValueError('queue_job_invalid')
   payload=json.loads(job['payload'])
   if payload.get('action_id')!=action_id or payload.get('decision_sha256')!=decision_digest(decision) or payload.get('candles_sha256')!=candle_digest(bars,skip_bar) or payload.get('funding_sha256')!=decision_digest(funding_evidence):raise ValueError('queue_identity_mismatch')
   latest=store.db.execute("SELECT value FROM route_queue.unified_state WHERE key='adaptive:latest'").fetchone()
   current=json.loads(latest[0]) if latest else {}
   if store.db.execute("SELECT 1 FROM route_queue.unified_state WHERE key='adaptive:decision_conflict'").fetchone():raise ValueError('decision_conflict_requires_review')
   if current.get('sha256')!=decision_digest(decision) or current.get('symbol')!='BTCUSDT' or current.get('version')!=decision_version or current.get('source')!=decision_source:raise ValueError('decision_superseded')
   if store.get('leverage')!=1:raise ValueError('isolated_leverage_not_1x')
   require_clear(store.db,own_job_id=job_id)
   yield
 try:
  store.margin=margin;store.transaction=lambda:locked_check()
  try:opened=store.open_position('SHORT',mark,clock(),action_id,clock_ms=checked_clock)
  finally:store.transaction=original;store.margin=original_margin
  fault('after_open')
  if opened:return {'status':'filled'}
  row=store.db.execute("SELECT payload FROM futures_events WHERE business_key=? AND event='OPEN'",(action_id,)).fetchone()
  if row:return {'status':'filled_existing'}
  return {'status':'not_opened'}
 finally:
  store.transaction=original;store.margin=original_margin;store.db.execute('DETACH DATABASE route_queue')

def exit_on_stop(store,queue_path,*,route_state,job_id,action_id,position_id,quote,quote_ms,bars=None,skip_bar=None,risk_bar=None,
                 clock=lambda:int(time.time()*1000),fault=lambda stage:None):
 from .adaptive_route_state import RouteState
 if type(store) is not FuturesStore or not store.shared or type(route_state) is not RouteState:raise ValueError('offline_types_required')
 dbfile=Path(store.db.execute('PRAGMA database_list').fetchone()[2]);queue_path=Path(queue_path);root=Path(tempfile.gettempdir()).resolve()
 if dbfile.is_symlink() or queue_path.is_symlink() or not dbfile.resolve().is_relative_to(root) or queue_path.resolve().parent!=dbfile.resolve().parent or route_state.path.resolve().parent!=dbfile.resolve().parent:raise ValueError('sandbox_path_required')
 mark=Decimal(str(quote))
 if not mark.is_finite() or mark<=0:raise ValueError('invalid_quote')
 previous=store.db.execute('SELECT event,payload FROM futures_events WHERE business_key=?',(action_id,)).fetchone()
 state=route_state.status();entry_ms=None;reference=mark
 if previous:
  event=json.loads(previous['payload']);reason=event.get('reason')
  if previous['event']!='CLOSE' or reason not in ('STOP','TIME','SIGNAL'):raise ValueError('action_identity_conflict')
 else:
  position=store.get('position');action=route_state.db.execute('SELECT signal_json FROM route_actions WHERE action_id=?',(state['action_id'],)).fetchone()
  signal_data=json.loads(action[0]) if action else {};entry_ms=signal_data.get('entry_boundary_ms')
  if not position:raise ValueError('route_position_mismatch')
  threshold=Decimal(position['entry'])*Decimal('1.02')
  if risk_bar is not None:
   closed_bar_signal([risk_bar],'range',now_ms=risk_bar[0]+900000)
   if type(entry_ms) is not int or risk_bar[0]<entry_ms or not risk_bar[0]+900000<=clock()<=risk_bar[0]+905000:raise ValueError('risk_bar_stale')
  if mark>=threshold or (risk_bar is not None and Decimal(str(risk_bar[2]))>=threshold):
   reason='STOP';reference=max(mark,threshold,Decimal(str(risk_bar[1])) if risk_bar is not None else mark)
  elif type(entry_ms) is int and clock()-entry_ms>=604800000:reason='TIME'
  elif bars and skip_bar:
   sig=closed_bar_signal(bars,'trend1',now_ms=bars[-1][0]+900000)
   if not sig or sig[0]!='TREND' or not sig[1]>sig[5] or skip_bar[0]!=bars[-1][0]+900000:raise ValueError('protective_exit_not_met')
   closed_bar_signal([skip_bar],'range',now_ms=skip_bar[0]+900000);reason='SIGNAL'
  else:raise ValueError('protective_exit_not_met')
 def checked_exit_clock():
  now=clock()
  if type(quote_ms) is not int or not 0<=now-quote_ms<=5000:raise ValueError('stale_protection_quote')
  if risk_bar is not None and not risk_bar[0]+900000<=now<=risk_bar[0]+905000:raise ValueError('risk_bar_stale')
  pos=store.get('position')
  if not pos or pos.get('id')!=position_id:raise ValueError('route_position_mismatch')
  if pos.get('funding_cursor',-1)<now:raise ValueError('funding_coverage_missing')
  if reason=='SIGNAL' and (not bars or not skip_bar or not skip_bar[0]+900000<=now<=skip_bar[0]+905000):raise ValueError('signal_exit_window_missed')
  return now
 original=store.transaction;store.db.execute('ATTACH DATABASE ? AS route_queue',(str(queue_path.resolve()),))
 @contextmanager
 def checked():
  with original():
   previous=store.db.execute('SELECT event,payload FROM futures_events WHERE business_key=?',(action_id,)).fetchone()
   if previous:
    if previous['event']!='CLOSE':raise ValueError('action_identity_conflict')
    yield;return
   now=checked_exit_clock()
   job=store.db.execute('SELECT market,command,status,payload FROM route_queue.unified_jobs WHERE id=?',(job_id,)).fetchone()
   if not job or (job['market'],job['command'],job['status'])!=('futures','/research_adaptive_exit','PENDING') or json.loads(job['payload']).get('action_id')!=action_id:raise ValueError('queue_job_invalid')
   expected_bar=list(risk_bar) if risk_bar is not None else None
   if json.loads(job['payload']).get('risk_bar')!=expected_bar:raise ValueError('risk_bar_identity_mismatch')
   state=route_state.status();pos=store.get('position')
   if state['phase'] not in ('OPEN','EXIT_PENDING') or (state['market'],state['strategy'],state['position_id'])!=('futures','trend1',position_id) or not pos or pos['id']!=position_id:raise ValueError('route_position_mismatch')
   if reason=='STOP' and mark<Decimal(pos['entry'])*Decimal('1.02') and (risk_bar is None or Decimal(str(risk_bar[2]))<Decimal(pos['entry'])*Decimal('1.02')):raise ValueError('protective_stop_not_met')
   if reason=='TIME' and (type(entry_ms) is not int or now-entry_ms<604800000):raise ValueError('time_exit_not_due')
   if reason=='SIGNAL' and (not bars or not skip_bar or not skip_bar[0]+900000<=now<=skip_bar[0]+905000):raise ValueError('signal_exit_window_missed')
   if pos.get('funding_cursor',-1)<now:raise ValueError('funding_coverage_missing')
   yield
 try:
  now=clock();store.transaction=lambda:checked()
  try:closed=store.close_position(reference,now,action_id,reason,position_id,clock_ms=checked_exit_clock)
  finally:store.transaction=original
  fault('after_close')
  if closed:return {'status':'filled'}
  row=store.db.execute("SELECT 1 FROM futures_events WHERE business_key=? AND event='CLOSE'",(action_id,)).fetchone()
  return {'status':'filled_existing' if row else 'reconciliation_required'}
 finally:store.transaction=original;store.db.execute('DETACH DATABASE route_queue')

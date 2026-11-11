"""Explicit offline queue worker for the limited research Spot path."""
import json
from pathlib import Path
from .adaptive_atomic_spot import enter,exit_on_stop
from .adaptive_atomic_futures import enter_short,enter_long
from .adaptive_atomic_futures import exit_on_stop as exit_futures_on_stop
from .adaptive_route_state import RouteState
from .adaptive_route_policy import closed_bar_signal,decision_digest,candle_digest,route,VERSION,FUTURES_VERSION
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue

def process_entry(queue,store,state,*,decision,signal,data_ms,decision_version,bars,skip_bar,quote,quote_ms,filters,filters_ms,clock,fault=lambda stage:None):
 if type(queue) is not UnifiedQueue or type(store) is not RuntimeStore or type(state) is not RouteState:raise ValueError('offline_types_required')
 jobs=[j for j in queue.pending('spot') if j['command']=='/research_adaptive_entry']
 if len(jobs)!=1:raise ValueError('exactly_one_entry_job_required')
 job=jobs[0];payload=json.loads(job['payload']);action=payload['action_id'];snap=decision['snapshot_id']
 if payload.get('decision_sha256')!=decision_digest(decision) or payload.get('candles_sha256')!=candle_digest(bars,skip_bar):raise ValueError('queue_identity_mismatch')
 prior=state.db.execute('SELECT 1 FROM route_actions WHERE action_id=?',(action,)).fetchone()
 if not prior:
  admission=route(decision,snapshot_id=snap,data_ms=data_ms,now_ms=clock(),symbol='BTCUSDT',decision_version=decision_version,market='spot',eligible=True,research_sandbox=True)
  if not admission['allowed']:queue.acknowledge(job['id'],admission['reason'],applied=False,notify=False);return {'status':'BLOCKED','reason':admission['reason']}
 signal=dict(signal)
 signal['entry_boundary_ms']=skip_bar[0]+900000
 if decision.get('mode')=='RANGE':
  derived=closed_bar_signal(bars,'range',now_ms=bars[-1][0]+900000)
  if not derived or derived[0]!='BUY':queue.acknowledge(job['id'],'no range signal',applied=False,notify=False);return {'status':'NO_SIGNAL'}
  signal.update({'range_low':str(derived[1]),'range_high':str(derived[2]),'target':str(derived[3])})
 else:
  derived=closed_bar_signal(bars,'trend1',now_ms=bars[-1][0]+900000)
  if not derived or derived[0]!='TREND' or not derived[1]>derived[2]:queue.acknowledge(job['id'],'no trend signal',applied=False,notify=False);return {'status':'NO_SIGNAL'}
 result=state.begin_entry(action,decision,signal,snapshot_id=snap,data_ms=data_ms,symbol='BTCUSDT',decision_version=decision_version,quote_ms=quote_ms,expiry_ms=skip_bar[0]+900000+5000,eligible=True,research_sandbox=True)
 if result['phase']=='CLOSED':
  if not state.db.execute('SELECT 1 FROM route_exits WHERE entry_action=?',(action,)).fetchone():raise ValueError('closed_receipt_missing')
  queue.acknowledge(job['id'],'research entry already closed',notify=False);return {'status':'IDLE'}
 if result['phase']=='RECONCILE':
  try:state.confirm_open(action,Path(store.db.execute('PRAGMA database_list').fetchone()[2]))
  except ValueError:
   try:state.confirm_spot_not_filled(action,Path(store.db.execute('PRAGMA database_list').fetchone()[2]))
   except ValueError:return {'status':'reconciliation_required'}
   queue.acknowledge(job['id'],'research entry expired',applied=False,notify=False);return {'status':'CANCELED'}
  queue.acknowledge(job['id'],'research entry reconciled',notify=False);return {'status':'OPEN'}
 if result['phase'] not in ('ENTRY_PENDING','OPEN'):return {'status':'reconciliation_required'}
 fault('after_route_commit')
 result=enter(store,Path(queue.db.execute('PRAGMA database_list').fetchone()[2]),job_id=job['id'],action_id=action,decision=decision,snapshot_id=snap,data_ms=data_ms,decision_version=decision_version,bars=bars,skip_bar=skip_bar,quote=quote,quote_ms=quote_ms,filters=filters,filters_ms=filters_ms,decision_source=signal['source'],clock=clock,fault=fault)
 if result['status']=='reconciliation_required':state.require_reconciliation(action);return result
 if result['status']=='not_filled':
  state.confirm_spot_not_filled(action,Path(store.db.execute('PRAGMA database_list').fetchone()[2]));queue.acknowledge(job['id'],'research entry expired',applied=False,notify=False);return {'status':'CANCELED'}
 if result['status'] not in ('filled','filled_existing'):return result
 fault('before_confirm_open');state.confirm_open(action,Path(store.db.execute('PRAGMA database_list').fetchone()[2]));fault('before_queue_ack')
 queue.acknowledge(job['id'],'research entry filled',notify=False)
 return {'status':'OPEN'}

def process_stop(queue,store,state,*,quote,quote_ms,clock,bars=None,skip_bar=None,fault=lambda stage:None):
 jobs=[j for j in queue.pending('spot') if j['command']=='/research_adaptive_exit']
 if len(jobs)!=1:raise ValueError('exactly_one_exit_job_required')
 job=jobs[0];payload=json.loads(job['payload']);entry=payload['entry_action'];action=payload['action_id'];pos=state.status()['position_id']
 state.begin_exit(entry,action);fault('after_exit_route_commit')
 if payload.get('bars') is not None:
  if bars is not None and candle_digest(bars,skip_bar)!=candle_digest(payload['bars'],payload['skip_bar']):raise ValueError('exit_candle_identity_conflict')
  bars,skip_bar=payload['bars'],payload['skip_bar']
 result=exit_on_stop(store,Path(queue.db.execute('PRAGMA database_list').fetchone()[2]),route_state=state,job_id=job['id'],action_id=action,position_id=pos,quote=quote,quote_ms=quote_ms,bars=bars,skip_bar=skip_bar,risk_bar=payload.get('risk_bar'),clock=clock,fault=fault)
 if result['status'] not in ('filled','filled_existing'):return result
 fault('before_confirm_exit');state.confirm_spot_closed(entry,action,Path(store.db.execute('PRAGMA database_list').fetchone()[2]));fault('before_queue_ack')
 queue.acknowledge(job['id'],'research exit filled',notify=False)
 return {'status':'IDLE'}

def process_short(queue,store,state,**kwargs):
 return process_futures_entry(queue,store,state,direction='SHORT',**kwargs)

def process_long(queue,store,state,**kwargs):
 return process_futures_entry(queue,store,state,direction='LONG',route_policy=FUTURES_VERSION,**kwargs)

def process_futures_entry(queue,store,state,*,direction,decision,signal,data_ms,decision_version,bars,skip_bar,quote,quote_ms,filters,filters_ms,funding_evidence,clock,fault=lambda stage:None,execution_fetch=None,filter_mark_proof=None,route_policy=VERSION):
 from trading_bot.futures_store import FuturesStore
 if type(queue) is not UnifiedQueue or type(store) is not FuturesStore or type(state) is not RouteState:raise ValueError('offline_types_required')
 if direction not in ('LONG','SHORT') or signal.get('direction')!=direction:raise ValueError('signal_direction_mismatch')
 jobs=[j for j in queue.pending('futures') if j['command']=='/research_adaptive_entry']
 if len(jobs)!=1:raise ValueError('exactly_one_entry_job_required')
 job=jobs[0];action=json.loads(job['payload'])['action_id'];snap=decision['snapshot_id']
 payload=json.loads(job['payload'])
 if payload.get('route_policy',VERSION)!=route_policy:raise ValueError('queue_policy_mismatch')
 if payload.get('decision_sha256')!=decision_digest(decision) or payload.get('candles_sha256')!=candle_digest(bars,skip_bar):raise ValueError('queue_identity_mismatch')
 prior=state.db.execute('SELECT 1 FROM route_actions WHERE action_id=?',(action,)).fetchone()
 if not prior:
  admission=route(decision,snapshot_id=snap,data_ms=data_ms,now_ms=clock(),symbol='BTCUSDT',decision_version=decision_version,market='futures',eligible=True,research_sandbox=True,policy=route_policy)
  if not admission['allowed']:queue.acknowledge(job['id'],admission['reason'],applied=False,notify=False);return {'status':'BLOCKED','reason':admission['reason']}
 signal=dict(signal);signal['entry_boundary_ms']=skip_bar[0]+900000
 derived=closed_bar_signal(bars,'trend1',now_ms=bars[-1][0]+900000)
 if not derived or derived[0]!='TREND' or not (derived[1]>derived[2] if direction=='LONG' else derived[1]<derived[3]):queue.acknowledge(job['id'],'no '+direction.lower()+' signal',applied=False,notify=False);return {'status':'NO_SIGNAL'}
 result=state.begin_entry(action,decision,signal,snapshot_id=snap,data_ms=data_ms,symbol='BTCUSDT',decision_version=decision_version,quote_ms=quote_ms,expiry_ms=skip_bar[0]+900000+5000,eligible=True,research_sandbox=True,route_policy=route_policy)
 if result['phase']=='CLOSED':
  if not state.db.execute('SELECT 1 FROM route_exits WHERE entry_action=?',(action,)).fetchone():raise ValueError('closed_receipt_missing')
  queue.acknowledge(job['id'],'research entry already closed',notify=False);return {'status':'IDLE'}
 if result['phase']=='OPEN':
  state.confirm_open(action,Path(store.db.execute('PRAGMA database_list').fetchone()[2]));queue.acknowledge(job['id'],'research futures entry reconciled',notify=False);return {'status':'OPEN'}
 if result['phase'] not in ('ENTRY_PENDING','OPEN'):return {'status':'reconciliation_required'}
 fault('after_route_commit')
 proof=None;fixed_ms=None
 if execution_fetch is not None:
  fixed_ms=clock() # sampled after the route intent committed
  try:
   proof=execution_fetch(not_before_ms=fixed_ms);quote=proof['price'];quote_ms=proof['event_ms']
  except (ValueError,KeyError,TypeError,OSError):
   try:state.confirm_futures_not_filled_v2(action,Path(store.db.execute('PRAGMA database_list').fetchone()[2]))
   except ValueError:return {'status':'reconciliation_required','reason':'execution_quote_unavailable'}
   queue.acknowledge(job['id'],'fresh post-signal trade unavailable',applied=False,notify=False)
   return {'status':'CANCELED','reason':'execution_quote_unavailable'}
 opener=enter_long if direction=='LONG' else enter_short
 policy_args={} if direction=='LONG' else {'route_policy':route_policy}
 result=opener(store,Path(queue.db.execute('PRAGMA database_list').fetchone()[2]),job_id=job['id'],action_id=action,decision=decision,snapshot_id=snap,data_ms=data_ms,decision_version=decision_version,bars=bars,skip_bar=skip_bar,quote=quote,quote_ms=quote_ms,filters=filters,filters_ms=filters_ms,funding_evidence=funding_evidence,decision_source=signal['source'],clock=clock,fault=fault,execution_proof=proof,signal_fixed_ms=fixed_ms,filter_mark_proof=filter_mark_proof,**policy_args)
 if result['status'] not in ('filled','filled_existing'):return result
 fault('before_confirm_open');state.confirm_open(action,Path(store.db.execute('PRAGMA database_list').fetchone()[2]));fault('before_queue_ack')
 queue.acknowledge(job['id'],'research futures filled',notify=False)
 return {'status':'OPEN'}

def process_short_stop(queue,store,state,*,quote,quote_ms,clock,bars=None,skip_bar=None,fault=lambda stage:None,execution_fetch=None,current_mark_proof=None,execution_filters=None,filters_ms=None):
 jobs=[j for j in queue.pending('futures') if j['command']=='/research_adaptive_exit']
 if len(jobs)!=1:raise ValueError('exactly_one_exit_job_required')
 job=jobs[0];payload=json.loads(job['payload']);entry=payload['entry_action'];action=payload['action_id'];pos=state.status()['position_id']
 state.begin_exit(entry,action);fault('after_exit_route_commit')
 if payload.get('bars') is not None:
  if bars is not None and candle_digest(bars,skip_bar)!=candle_digest(payload['bars'],payload['skip_bar']):raise ValueError('exit_candle_identity_conflict')
  bars,skip_bar=payload['bars'],payload['skip_bar']
 proof=None;fixed_ms=None
 if execution_fetch is not None:
  fixed_ms=clock() # protective intent has already committed
  try:proof=execution_fetch(not_before_ms=fixed_ms);quote=proof['price'];quote_ms=proof['event_ms']
  except (ValueError,KeyError,TypeError,OSError):return {'status':'EXIT_PENDING','reason':'execution_quote_unavailable'}
 try:
  result=exit_futures_on_stop(store,Path(queue.db.execute('PRAGMA database_list').fetchone()[2]),route_state=state,job_id=job['id'],action_id=action,position_id=pos,quote=quote,quote_ms=quote_ms,bars=bars,skip_bar=skip_bar,risk_bar=payload.get('risk_bar'),clock=clock,fault=fault,execution_proof=proof,trigger_fixed_ms=fixed_ms,mark_proof=payload.get('trigger_mark'),mark_checked_ms=payload.get('trigger_checked_ms'),current_mark_proof=current_mark_proof,execution_filters=execution_filters,filters_ms=filters_ms)
 except ValueError as exc:
  if execution_fetch is not None and str(exc) in ('market_take_bound_unavailable','market_take_bound_invalid','market_take_bound_exceeded','stale_filters','quote_invalid','quote_stale_or_future'):return {'status':'EXIT_PENDING','reason':str(exc)}
  raise
 if result['status'] not in ('filled','filled_existing'):return result
 fault('before_confirm_exit');state.confirm_futures_closed(entry,action,Path(store.db.execute('PRAGMA database_list').fetchone()[2]));fault('before_queue_ack')
 queue.acknowledge(job['id'],'research futures exit filled',notify=False)
 return {'status':'IDLE'}

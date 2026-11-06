"""Explicit offline queue worker for the limited research Spot path."""
import json
from pathlib import Path
from .adaptive_atomic_spot import enter,exit_on_stop
from .adaptive_atomic_futures import enter_short
from .adaptive_atomic_futures import exit_on_stop as exit_short_on_stop
from .adaptive_route_state import RouteState
from .adaptive_route_policy import closed_bar_signal,decision_digest,candle_digest,route
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

def process_short(queue,store,state,*,decision,signal,data_ms,decision_version,bars,skip_bar,quote,quote_ms,filters,filters_ms,funding_evidence,clock,fault=lambda stage:None):
 from trading_bot.futures_store import FuturesStore
 if type(queue) is not UnifiedQueue or type(store) is not FuturesStore or type(state) is not RouteState:raise ValueError('offline_types_required')
 jobs=[j for j in queue.pending('futures') if j['command']=='/research_adaptive_entry']
 if len(jobs)!=1:raise ValueError('exactly_one_entry_job_required')
 job=jobs[0];action=json.loads(job['payload'])['action_id'];snap=decision['snapshot_id']
 payload=json.loads(job['payload'])
 if payload.get('decision_sha256')!=decision_digest(decision) or payload.get('candles_sha256')!=candle_digest(bars,skip_bar):raise ValueError('queue_identity_mismatch')
 prior=state.db.execute('SELECT 1 FROM route_actions WHERE action_id=?',(action,)).fetchone()
 if not prior:
  admission=route(decision,snapshot_id=snap,data_ms=data_ms,now_ms=clock(),symbol='BTCUSDT',decision_version=decision_version,market='futures',eligible=True,research_sandbox=True)
  if not admission['allowed']:queue.acknowledge(job['id'],admission['reason'],applied=False,notify=False);return {'status':'BLOCKED','reason':admission['reason']}
 signal=dict(signal);signal['entry_boundary_ms']=skip_bar[0]+900000
 derived=closed_bar_signal(bars,'trend1',now_ms=bars[-1][0]+900000)
 if not derived or derived[0]!='TREND' or not derived[1]<derived[3]:queue.acknowledge(job['id'],'no short signal',applied=False,notify=False);return {'status':'NO_SIGNAL'}
 result=state.begin_entry(action,decision,signal,snapshot_id=snap,data_ms=data_ms,symbol='BTCUSDT',decision_version=decision_version,quote_ms=quote_ms,expiry_ms=skip_bar[0]+900000+5000,eligible=True,research_sandbox=True)
 if result['phase']=='CLOSED':
  if not state.db.execute('SELECT 1 FROM route_exits WHERE entry_action=?',(action,)).fetchone():raise ValueError('closed_receipt_missing')
  queue.acknowledge(job['id'],'research entry already closed',notify=False);return {'status':'IDLE'}
 if result['phase'] not in ('ENTRY_PENDING','OPEN'):return {'status':'reconciliation_required'}
 fault('after_route_commit')
 result=enter_short(store,Path(queue.db.execute('PRAGMA database_list').fetchone()[2]),job_id=job['id'],action_id=action,decision=decision,snapshot_id=snap,data_ms=data_ms,decision_version=decision_version,bars=bars,skip_bar=skip_bar,quote=quote,quote_ms=quote_ms,filters=filters,filters_ms=filters_ms,funding_evidence=funding_evidence,decision_source=signal['source'],clock=clock,fault=fault)
 if result['status'] not in ('filled','filled_existing'):return result
 fault('before_confirm_open');state.confirm_open(action,Path(store.db.execute('PRAGMA database_list').fetchone()[2]));fault('before_queue_ack')
 queue.acknowledge(job['id'],'research short filled',notify=False)
 return {'status':'OPEN'}

def process_short_stop(queue,store,state,*,quote,quote_ms,clock,bars=None,skip_bar=None,fault=lambda stage:None):
 jobs=[j for j in queue.pending('futures') if j['command']=='/research_adaptive_exit']
 if len(jobs)!=1:raise ValueError('exactly_one_exit_job_required')
 job=jobs[0];payload=json.loads(job['payload']);entry=payload['entry_action'];action=payload['action_id'];pos=state.status()['position_id']
 state.begin_exit(entry,action);fault('after_exit_route_commit')
 if payload.get('bars') is not None:
  if bars is not None and candle_digest(bars,skip_bar)!=candle_digest(payload['bars'],payload['skip_bar']):raise ValueError('exit_candle_identity_conflict')
  bars,skip_bar=payload['bars'],payload['skip_bar']
 result=exit_short_on_stop(store,Path(queue.db.execute('PRAGMA database_list').fetchone()[2]),route_state=state,job_id=job['id'],action_id=action,position_id=pos,quote=quote,quote_ms=quote_ms,bars=bars,skip_bar=skip_bar,risk_bar=payload.get('risk_bar'),clock=clock,fault=fault)
 if result['status'] not in ('filled','filled_existing'):return result
 fault('before_confirm_exit');state.confirm_futures_closed(entry,action,Path(store.db.execute('PRAGMA database_list').fetchone()[2]));fault('before_queue_ack')
 queue.acknowledge(job['id'],'research short exit filled',notify=False)
 return {'status':'IDLE'}

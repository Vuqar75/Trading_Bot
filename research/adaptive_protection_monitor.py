"""Offline-only protective job scheduling; never calls a model or an exchange."""
import hashlib,json,tempfile,time
from decimal import Decimal
from pathlib import Path
from .adaptive_route_state import RouteState
from .adaptive_route_policy import closed_bar_signal
from .adaptive_atomic_futures import stop_hit,channel_exit
from trading_bot.unified_queue import UnifiedQueue

def observe(queue,store,state,*,quote,quote_ms,bars=None,skip_bar=None,risk_bar=None,clock=lambda:int(time.time()*1000),source_mark=None):
 if type(queue) is not UnifiedQueue or type(state) is not RouteState:raise ValueError('offline_types_required')
 paths=[Path(queue.db.execute('PRAGMA database_list').fetchone()[2]),Path(store.db.execute('PRAGMA database_list').fetchone()[2]),state.path]
 root=Path(tempfile.gettempdir()).resolve()
 if any(p.is_symlink() or not p.resolve().is_relative_to(root) for p in paths) or len({p.resolve().parent for p in paths})!=1:raise ValueError('sandbox_paths_required')
 now=clock();route=state.status();reason=None;problem=None;mark=None;channel='not_applicable';channel_problem=None;intrabar='unavailable';risk_data=None
 try:
  if route['phase'] not in ('OPEN','EXIT_PENDING'):raise ValueError('no_open_route_position')
  mark=Decimal(str(quote))
  if not mark.is_finite() or mark<=0 or type(quote_ms) is not int or not 0<=now-quote_ms<=5000:raise ValueError('price_unavailable_or_stale')
  p=store.get('position')
  if not p or p.get('id')!=route['position_id']:raise ValueError('route_position_mismatch')
  if route['market']=='spot' and Decimal(str(p.get('quantity','0')))<=0:raise ValueError('position_already_closed_requires_reconciliation')
  action=state.db.execute('SELECT signal_json FROM route_actions WHERE action_id=?',(route['action_id'],)).fetchone()
  if not action:raise ValueError('entry_signal_missing')
  signal=json.loads(action[0]);opened=signal.get('entry_boundary_ms')
  if type(opened) is not int:raise ValueError('entry_time_missing')
  if risk_bar is not None:
   try:
    closed_bar_signal([risk_bar],'range',now_ms=risk_bar[0]+900000)
    if risk_bar[0]<opened or not risk_bar[0]+900000<=now<=risk_bar[0]+905000:raise ValueError('risk_bar_stale')
    risk_data=risk_bar;intrabar='checked'
   except (ValueError,TypeError,IndexError):intrabar='unavailable'
  if route['market']=='spot':
   entry=Decimal(p['entry_price']);strategy=route['strategy']
   if strategy not in ('trend1','range'):raise ValueError('unknown_strategy')
   stop=entry*(Decimal('.99') if strategy=='range' else Decimal('.98'))
   if mark<=stop or (risk_data is not None and Decimal(str(risk_data[3]))<=stop):reason='stop'
   elif strategy=='range' and (mark>=Decimal(signal['target']) or (risk_data is not None and Decimal(str(risk_data[2]))>=Decimal(signal['target']))):reason='target'
   elif type(opened) is int and now-opened>=(86400000 if strategy=='range' else 604800000):reason='time'
  elif route['market']=='futures' and route['strategy']=='trend1':
   if p.get('direction')!=route['direction']:raise ValueError('route_position_mismatch')
   # Continue evaluating price protection even when settlement is blocked by
   # missing funding. Expose the trigger, but do not create a falsely settled
   # close until the funding history is confirmed.
   from .adaptive_funding_tracker import covered
   if not covered(store,p,now):problem='funding_coverage_missing'
   if stop_hit(p,mark,risk_data):reason='stop'
   elif type(opened) is int and now-opened>=604800000:reason='time'
  else:raise ValueError('market_policy_absent')
  if route['strategy']=='trend1':
   channel='unavailable'
   if bars is not None and skip_bar is not None:
    try:
     sig=closed_bar_signal(bars,'trend1',now_ms=bars[-1][0]+900000)
     if skip_bar[0]!=bars[-1][0]+900000:raise ValueError('skip_bar_mismatch')
     closed_bar_signal([skip_bar],'range',now_ms=skip_bar[0]+900000)
     if not skip_bar[0]+900000<=now<=skip_bar[0]+905000:raise ValueError('signal_exit_window_missed')
     if not sig or sig[0]!='TREND':raise ValueError('channel_data_unavailable')
     channel='checked'
     if reason is None and ((route['market']=='spot' and sig[1]<sig[4]) or (route['market']=='futures' and channel_exit(p,sig))):reason='signal'
    except (ValueError,TypeError,IndexError):channel_problem='channel_data_invalid_or_stale'
 except (ValueError,KeyError,TypeError,ArithmeticError) as exc:problem=str(exc)
 summary={'checked_ms':now,'position_id':route.get('position_id'),'state':'unavailable' if problem else ('partial' if channel=='unavailable' or intrabar=='unavailable' else 'checked'),'reason':problem,'trigger':reason,'channel':channel,'channel_reason':channel_problem,'intrabar':intrabar}
 with queue.transaction():
  wait_key='adaptive:funding_wait:'+str(route.get('action_id'))
  prior_wait=queue.db.execute('SELECT value FROM unified_state WHERE key=?',(wait_key,)).fetchone()
  if route.get('market')=='futures' and problem=='funding_coverage_missing' and reason:
   if not prior_wait:
    wait={'position_id':route.get('position_id'),'entry_action':route.get('action_id'),'trigger':reason,'first_seen_ms':now,'reason':'funding_coverage_missing'}
    if source_mark is not None:wait.update({'trigger_mark':source_mark,'risk_bar':risk_data,'bars':bars if reason=='signal' else None,'skip_bar':skip_bar if reason=='signal' else None})
    queue.db.execute('INSERT INTO unified_state VALUES(?,?)',(wait_key,json.dumps(wait,sort_keys=True)))
   else:wait=json.loads(prior_wait[0])
   summary['funding_wait']=wait
  elif prior_wait and route.get('phase') in ('OPEN','EXIT_PENDING'):
   summary['funding_wait']=json.loads(prior_wait[0])
   # Once a v2 protective condition was observed, later funding recovery does
   # not erase it merely because the market price subsequently moved back.
   wait=summary['funding_wait']
   if route.get('market')=='futures' and not problem and not reason and wait.get('trigger_mark'):
    reason=wait['trigger'];source_mark=wait['trigger_mark'];risk_data=wait.get('risk_bar');bars=wait.get('bars');skip_bar=wait.get('skip_bar');summary['trigger']=reason
  queue.db.execute("INSERT OR REPLACE INTO unified_state VALUES('adaptive:protection',?)",(json.dumps(summary,sort_keys=True),))
  if route['phase']=='EXIT_PENDING':
   pending=state.db.execute('SELECT exit_action FROM route_exit_pending WHERE entry_action=?',(route['action_id'],)).fetchone()
   jobs=[j for j in queue.pending(route['market']) if j['command']=='/research_adaptive_exit' and json.loads(j['payload']).get('action_id')==(pending[0] if pending else None)]
   return dict(summary,exit_pending=True,job_id=jobs[0]['id'] if len(jobs)==1 else None,reconciliation_required=len(jobs)!=1)
  if reason and not problem:
   action_id='protect:'+hashlib.sha256((route['action_id']+':'+reason).encode()).hexdigest()[:24]
   key='adaptive:protect:'+action_id;prior=queue.db.execute('SELECT value FROM unified_state WHERE key=?',(key,)).fetchone()
   if prior:return dict(summary,job_id=int(prior[0]),replayed=True)
   update_id=int(hashlib.sha256(action_id.encode()).hexdigest()[:15],16)
   if queue.db.execute('SELECT 1 FROM unified_updates WHERE id=?',(update_id,)).fetchone():raise ValueError('synthetic_update_collision')
   market=route['market'];detail={'action_id':action_id,'entry_action':route['action_id'],'risk_bar':risk_data,'bars':bars if reason=='signal' else None,'skip_bar':skip_bar if reason=='signal' else None}
   if market=='futures' and source_mark is not None:
    wait=summary.get('funding_wait') or {};detail.update({'trigger':reason,'trigger_mark':source_mark,'trigger_checked_ms':wait.get('first_seen_ms',now) if wait.get('trigger_mark')==source_mark else now})
   payload=json.dumps(detail,sort_keys=True,allow_nan=False)
   queue.db.execute('INSERT INTO unified_updates VALUES(?,?,?,?)',(update_id,'offline:'+action_id,'/research_adaptive_exit',market))
   job=queue.db.execute("INSERT INTO unified_jobs(update_id,market,command,payload) VALUES(?,?,'/research_adaptive_exit',?)",(update_id,market,payload)).lastrowid
   queue.db.execute('INSERT INTO unified_state VALUES(?,?)',(key,str(job)))
   return dict(summary,job_id=job,replayed=False)
 return summary

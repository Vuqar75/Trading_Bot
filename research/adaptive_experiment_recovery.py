"""No-network reconciliation of already committed research actions."""
import json
from pathlib import Path
from .adaptive_route_state import RouteState
from trading_bot.unified_queue import UnifiedQueue

def reconcile(queue,state,financial_path,*,now_ms=None):
 if type(queue) is not UnifiedQueue or type(state) is not RouteState:raise ValueError('research_state_required')
 financial_path=Path(financial_path);current=state.status();phase=current['phase'];action=current['action_id'];market=current['market']
 if phase=='IDLE':
  recovered=0
  for wallet in ('spot','futures'):
   for job in queue.pending(wallet):
    payload=json.loads(job['payload']);key=payload.get('action_id')
    if job['command']=='/research_adaptive_exit':
     receipt=state.db.execute('SELECT 1 FROM route_exits WHERE entry_action=? AND exit_action=?',(payload.get('entry_action'),key)).fetchone()
     if receipt:queue.acknowledge(job['id'],'recovered committed exit',notify=False);recovered+=1
    elif job['command']=='/research_adaptive_entry':
     receipt=state.db.execute("SELECT 1 FROM route_actions WHERE action_id=? AND phase='CANCELED'",(key,)).fetchone()
     if receipt:queue.acknowledge(job['id'],'recovered unfilled entry',applied=False,notify=False);recovered+=1
     elif type(now_ms) is int and not state.db.execute('SELECT 1 FROM route_actions WHERE action_id=?',(key,)).fetchone():
      saved=queue.db.execute('SELECT value FROM unified_state WHERE key=?',('adaptive:decision:'+str(payload.get('decision_sha256')),)).fetchone()
      try:
       data=json.loads(saved[0]) if saved else {};expiry=data['decision']['expires_ms'];skip=payload['skip_bar'][0]
       expired=type(expiry) is int and type(skip) is int and (now_ms>=expiry or now_ms>skip+905000)
      except (ValueError,KeyError,IndexError,TypeError):expired=False
      if expired:queue.acknowledge(job['id'],'unsubmitted stale signal',applied=False,notify=False);recovered+=1
  return {'status':'idle','acknowledged':recovered}
 if phase in ('ENTRY_PENDING','RECONCILE','OPEN'):
  try:result=state.confirm_open(action,financial_path)
  except ValueError:
   if market=='spot' and phase in ('ENTRY_PENDING','RECONCILE'):
    try:state.confirm_spot_not_filled(action,financial_path)
    except ValueError:return {'status':'unresolved_entry'}
    jobs=[j for j in queue.pending(market) if j['command']=='/research_adaptive_entry' and json.loads(j['payload']).get('action_id')==action]
    if len(jobs)>1:raise ValueError('ambiguous_entry_queue')
    if jobs:queue.acknowledge(jobs[0]['id'],'recovered unfilled entry',applied=False,notify=False)
    return {'status':'canceled'}
   return {'status':'unresolved_entry'}
  jobs=[j for j in queue.pending(market) if j['command']=='/research_adaptive_entry' and json.loads(j['payload']).get('action_id')==action]
  if len(jobs)>1:raise ValueError('ambiguous_entry_queue')
  if jobs:queue.acknowledge(jobs[0]['id'],'recovered committed entry',notify=False)
  return {'status':'open','position_id':result['position_id']}
 if phase=='EXIT_PENDING':
  pending=state.db.execute('SELECT exit_action FROM route_exit_pending WHERE entry_action=?',(action,)).fetchone()
  if not pending:return {'status':'unresolved_exit'}
  exit_action=pending[0]
  try:
   if market=='spot':state.confirm_spot_closed(action,exit_action,financial_path)
   elif market=='futures':state.confirm_futures_closed(action,exit_action,financial_path)
   else:raise ValueError('market_policy_absent')
  except ValueError:return {'status':'unresolved_exit'}
  jobs=[j for j in queue.pending(market) if j['command']=='/research_adaptive_exit' and json.loads(j['payload']).get('action_id')==exit_action]
  if len(jobs)>1:raise ValueError('ambiguous_exit_queue')
  if jobs:queue.acknowledge(jobs[0]['id'],'recovered committed exit',notify=False)
  return {'status':'closed'}
 return {'status':'unresolved_state'}

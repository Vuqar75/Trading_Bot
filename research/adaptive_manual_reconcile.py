"""Reconcile an already completed, confirmed manual close in a sandbox."""
import json,tempfile
from pathlib import Path
from .adaptive_route_state import RouteState
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue

def reconcile_spot(queue,store,state):
 if type(queue) is not UnifiedQueue or type(store) is not RuntimeStore or not store.shared or type(state) is not RouteState:raise ValueError('offline_types_required')
 paths=[Path(queue.db.execute('PRAGMA database_list').fetchone()[2]),Path(store.db.execute('PRAGMA database_list').fetchone()[2]),state.path]
 root=Path(tempfile.gettempdir()).resolve()
 if any(p.is_symlink() or not p.resolve().is_relative_to(root) for p in paths) or len({p.resolve().parent for p in paths})!=1:raise ValueError('sandbox_paths_required')
 route=state.status()
 if route['phase']!='OPEN' or route['market']!='spot':raise ValueError('route_not_open')
 candidates=[]
 for row in store.db.execute("SELECT client_id,business_key FROM runtime_orders WHERE side='SELL' AND reason='MANUAL' AND phase='SETTLED' AND status='FILLED'"):
  binding=store.get('manual_binding:'+row['client_id'])
  if not isinstance(binding,dict) or binding.get('target')!=route['position_id'] or not isinstance(binding.get('update_id'),int):continue
  job=queue.db.execute("SELECT payload,status FROM unified_jobs WHERE update_id=? AND market='spot' AND command='/confirmed_close'",(binding['update_id'],)).fetchone()
  if not job or job['status']!='DONE':continue
  payload=json.loads(job['payload'])
  if payload.get('position')!=route['position_id'] or payload.get('quantity')!=binding.get('quantity'):continue
  proposal=queue.db.execute('SELECT status,position,quantity FROM unified_proposals WHERE id=?',(payload.get('proposal'),)).fetchone()
  if proposal and tuple(proposal)==('CONFIRMED',route['position_id'],binding['quantity']):candidates.append(row['business_key'])
 if len(candidates)!=1:raise ValueError('confirmed_manual_close_not_proven')
 state.begin_exit(route['action_id'],candidates[0])
 return state.confirm_spot_closed(route['action_id'],candidates[0],paths[1])

def reconcile_futures(queue,store,state):
 from trading_bot.futures_store import FuturesStore
 if type(queue) is not UnifiedQueue or type(store) is not FuturesStore or not store.shared or type(state) is not RouteState:raise ValueError('offline_types_required')
 paths=[Path(queue.db.execute('PRAGMA database_list').fetchone()[2]),Path(store.db.execute('PRAGMA database_list').fetchone()[2]),state.path];root=Path(tempfile.gettempdir()).resolve()
 if any(p.is_symlink() or not p.resolve().is_relative_to(root) for p in paths) or len({p.resolve().parent for p in paths})!=1:raise ValueError('sandbox_paths_required')
 route=state.status()
 if route['phase']!='OPEN' or route['market']!='futures':raise ValueError('route_not_open')
 candidates=[]
 for key,payload in store.db.execute("SELECT business_key,payload FROM futures_events WHERE event='CLOSE'"):
  event=json.loads(payload)
  if event.get('id')!=route['position_id'] or event.get('reason')!='MANUAL':continue
  for job in queue.db.execute("SELECT payload,status FROM unified_jobs WHERE market='futures' AND command='/confirmed_close'"):
   received=json.loads(job['payload'])
   if job['status']!='DONE' or received.get('position')!=route['position_id'] or received.get('quantity')!=event.get('quantity'):continue
   proposal=queue.db.execute('SELECT status,position,quantity FROM unified_proposals WHERE id=?',(received.get('proposal'),)).fetchone()
   if proposal and tuple(proposal)==('CONFIRMED',route['position_id'],event['quantity']):candidates.append(key)
 if len(candidates)!=1:raise ValueError('confirmed_manual_close_not_proven')
 state.begin_exit(route['action_id'],candidates[0]);return state.confirm_futures_closed(route['action_id'],candidates[0],paths[1])

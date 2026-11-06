"""Read-only, conservative snapshot of offline route and queue state."""
import json,sqlite3,tempfile,time
from contextlib import closing
from pathlib import Path

def status(route_path,queue_path,*,now_ms=None):
 route_path=Path(route_path);queue_path=Path(queue_path);root=Path(tempfile.gettempdir()).resolve()
 if any(p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(root) for p in (route_path,queue_path)) or route_path.resolve().parent!=queue_path.resolve().parent:raise ValueError('sandbox_paths_required')
 now_ms=int(time.time()*1000) if now_ms is None else now_ms
 with closing(sqlite3.connect(route_path.resolve().as_uri()+'?mode=ro',uri=True)) as route:
  route.row_factory=sqlite3.Row;route.execute('BEGIN')
  row=route.execute('SELECT * FROM route_state WHERE id=1').fetchone()
  if not row:raise ValueError('route_state_missing')
  action=route.execute('SELECT decision_json,signal_json FROM route_actions WHERE action_id=?',(row['action_id'],)).fetchone() if row['action_id'] else None
  decision=json.loads(action[0]) if action else {};signal=json.loads(action[1]) if action else {}
  result={'phase':row['phase'],'position_id':row['position_id'],'action_id':row['action_id'],'market':row['market'],'strategy':row['strategy'],'direction':row['direction'],
          'source':signal.get('source'),'decision_version':signal.get('decision_version'),'symbol':signal.get('symbol'),'snapshot_id':decision.get('snapshot_id'),'mode':decision.get('mode'),'expires_ms':decision.get('expires_ms'),
          'decision_fresh':bool(type(decision.get('expires_ms')) is int and type(decision.get('data_ms')) is int and decision['data_ms']<=now_ms<decision['expires_ms']),
          'entry_allowed':False,'entry_reason':'fresh_admission_not_yet_checked',
          'protection':'not_evaluated' if row['phase'] in ('OPEN','EXIT_PENDING','RECONCILE') else 'no_open_route_position'}
 with closing(sqlite3.connect(queue_path.resolve().as_uri()+'?mode=ro',uri=True)) as queue:
  result['pending_jobs']=queue.execute("SELECT count(*) FROM unified_jobs WHERE status='PENDING'").fetchone()[0]
  if queue.execute("SELECT 1 FROM unified_state WHERE key='adaptive:decision_conflict'").fetchone():result['entry_reason']='decision_conflict_requires_review'
  row=queue.execute("SELECT value FROM unified_state WHERE key='adaptive:latest'").fetchone()
  if row:
   latest=json.loads(row[0]);result['entry_mode']=result['mode']
   for key in ('source','version','symbol','snapshot_id','mode','expires_ms'):
    if key in latest:result['decision_version' if key=='version' else key]=latest[key]
   result['decision_fresh']=bool(type(latest.get('data_ms')) is int and type(latest.get('expires_ms')) is int and latest['data_ms']<=now_ms<latest['expires_ms'])
  row=queue.execute("SELECT value FROM unified_state WHERE key='adaptive:protection'").fetchone()
  if row:
   last=json.loads(row[0])
   if last.get('position_id')==result['position_id'] and result['phase'] in ('OPEN','EXIT_PENDING','RECONCILE'):result['last_protection_check']=last
 return result

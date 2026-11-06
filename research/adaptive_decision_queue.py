"""Record timely shadow decisions on the existing isolated durable queue."""
import hashlib,json,tempfile
from pathlib import Path
from .adaptive_route_policy import decision_digest,candle_digest,route
from .adaptive_shadow import validate_model
from trading_bot.unified_queue import UnifiedQueue

def _sandbox(queue):
 if type(queue) is not UnifiedQueue:raise ValueError('offline_queue_required')
 path=Path(queue.db.execute('PRAGMA database_list').fetchone()[2]);root=Path(tempfile.gettempdir()).resolve()
 if path.is_symlink() or not path.resolve().is_relative_to(root):raise ValueError('sandbox_queue_required')

def record(queue,decision,*,source,symbol,version,now_ms):
 _sandbox(queue)
 if source not in ('AI','baseline') or symbol!='BTCUSDT' or version!='regular-regime-horizon-v2':raise ValueError('decision_identity_invalid')
 validate_model(decision,decision.get('snapshot_id'),decision.get('data_ms'),now_ms)
 digest=decision_digest(decision);record={'sha256':digest,'source':source,'symbol':symbol,'version':version,'mode':decision['mode'],'snapshot_id':decision['snapshot_id'],'data_ms':decision['data_ms'],'expires_ms':decision['expires_ms']}
 conflict=False
 with queue.transaction():
  if queue.db.execute("SELECT 1 FROM unified_state WHERE key='adaptive:decision_conflict'").fetchone():raise ValueError('decision_conflict_requires_review')
  row=queue.db.execute("SELECT value FROM unified_state WHERE key='adaptive:latest'").fetchone()
  if row:
   old=json.loads(row[0])
   if old['data_ms']>record['data_ms']:raise ValueError('decision_reordering')
   if old['data_ms']==record['data_ms'] and old['sha256']!=digest:
    queue.db.execute("INSERT INTO unified_state VALUES('adaptive:decision_conflict',?)",(json.dumps({'data_ms':record['data_ms'],'first':old['sha256'],'second':digest},sort_keys=True),));conflict=True
  if not conflict:
   queue.db.execute("INSERT OR REPLACE INTO unified_state VALUES('adaptive:latest',?)",(json.dumps(record,sort_keys=True),))
   key='adaptive:decision:'+digest
   saved=queue.db.execute('SELECT value FROM unified_state WHERE key=?',(key,)).fetchone()
   data=json.dumps({'identity':record,'decision':decision},sort_keys=True,ensure_ascii=False)
   if saved and saved[0]!=data:raise ValueError('decision_identity_conflict')
   queue.db.execute('INSERT OR IGNORE INTO unified_state VALUES(?,?)',(key,data))
 if conflict:raise ValueError('decision_conflict_requires_review')
 return record

def enqueue(queue,decision,*,source,symbol,version,now_ms,bars,skip_bar,funding_evidence=None):
 """Persist a research decision and immutable candle fixture on a temp queue."""
 _sandbox(queue)
 identity=record(queue,decision,source=source,symbol=symbol,version=version,now_ms=now_ms)
 choice=route(decision,snapshot_id=decision['snapshot_id'],data_ms=decision['data_ms'],now_ms=now_ms,symbol=symbol,decision_version=version,eligible=True,research_sandbox=True)
 if not choice['allowed']:return {'status':'BLOCKED','reason':choice['reason']}
 candle_hash=candle_digest(bars,skip_bar);action='research:'+hashlib.sha256((source+':'+identity['sha256']+':'+candle_hash).encode()).hexdigest()[:32]
 update=int(hashlib.sha256(action.encode()).hexdigest()[:15],16);market=choice['market']
 data={'action_id':action,'decision_sha256':identity['sha256'],'candles_sha256':candle_hash,'bars':bars,'skip_bar':skip_bar}
 if market=='futures':
  if not isinstance(funding_evidence,dict):raise ValueError('funding_evidence_unavailable')
  data.update({'funding_sha256':decision_digest(funding_evidence),'funding_evidence':funding_evidence})
 payload=json.dumps(data,sort_keys=True,separators=(',',':'),allow_nan=False)
 with queue.transaction():
  latest=queue.db.execute("SELECT value FROM unified_state WHERE key='adaptive:latest'").fetchone()
  if not latest or json.loads(latest[0]).get('sha256')!=identity['sha256']:raise ValueError('decision_superseded')
  prior=queue.db.execute('SELECT fingerprint FROM unified_updates WHERE id=?',(update,)).fetchone()
  if prior:
   if prior[0]!='offline:'+action:raise ValueError('synthetic_update_collision')
   job=queue.db.execute('SELECT id,payload,status FROM unified_jobs WHERE update_id=? AND market=?',(update,market)).fetchone()
   if not job or job['payload']!=payload:raise ValueError('queue_identity_conflict')
   return {'status':job['status'],'job_id':job['id'],'action_id':action,'replayed':True}
  queue.db.execute("INSERT INTO unified_updates VALUES(?,?,'/research_adaptive_entry',?)",(update,'offline:'+action,market))
  job=queue.db.execute("INSERT INTO unified_jobs(update_id,market,command,payload) VALUES(?,?,'/research_adaptive_entry',?)",(update,market,payload)).lastrowid
  return {'status':'PENDING','job_id':job,'action_id':action,'replayed':False}

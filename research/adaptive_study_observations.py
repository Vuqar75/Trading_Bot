"""Idempotent prospective decision/cost records in an isolated study DB."""
import json
from decimal import Decimal
from .adaptive_snapshot_proof import verify,verify_baseline

def save(db,document,*,source,now_ms,start_ms):
 if source=='AI':
  view=verify(document,now_ms=now_ms,start_ms=start_ms);usage=document['usage']
  cost=None if usage is None else str((Decimal(usage['input_tokens'])*Decimal('.1')+Decimal(usage['output_tokens'])*Decimal('.5'))/Decimal(1000000))
  identity={'source':'AI','snapshot_id':view['snapshot_id'],'data_ms':view['data_ms'],'mode':view['mode'],'baseline':view['baseline'],'answer_ms':view['answer_ms'],'estimated_api_usd':cost,'provider_bill_confirmed':False}
 elif source=='baseline':
  view=verify_baseline(document,now_ms=now_ms,start_ms=start_ms)
  identity={'source':'baseline','snapshot_id':view['snapshot_id'],'data_ms':view['data_ms'],'mode':view['mode'],'baseline':view['mode'],'answer_ms':None,'estimated_api_usd':None,'provider_bill_confirmed':False}
 else:raise ValueError('source_invalid')
 # Preserve the exact authenticated public input and decision; latest-only
 # publication files will later be replaced. Both accounts keep their own copy.
 payload=json.dumps(identity,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
 evidence=json.dumps(document,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
 db.execute('BEGIN IMMEDIATE')
 try:
  db.execute('CREATE TABLE IF NOT EXISTS study_decisions(snapshot_id TEXT PRIMARY KEY,data_ms INTEGER NOT NULL,boundary_ms INTEGER UNIQUE NOT NULL,payload TEXT NOT NULL,evidence TEXT NOT NULL)')
  old=db.execute('SELECT payload,evidence FROM study_decisions WHERE snapshot_id=? OR boundary_ms=?',(identity['snapshot_id'],view['boundary_ms'])).fetchone()
  if old and (old[0]!=payload or old[1]!=evidence):raise ValueError('study_decision_conflict')
  db.execute('INSERT OR IGNORE INTO study_decisions VALUES(?,?,?,?,?)',(identity['snapshot_id'],identity['data_ms'],view['boundary_ms'],payload,evidence));db.commit()
 except BaseException:
  db.rollback();raise
 return {**identity,'replayed':bool(old)}

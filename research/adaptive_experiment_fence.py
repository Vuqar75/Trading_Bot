"""Opt-in research database fences. Never install in a working paper database.

These triggers prevent legacy opening methods from accidentally bypassing the
study adapter. They are not a defence against a hostile database owner who can
drop triggers or write arbitrary SQL; filesystem and service isolation remain
mandatory. Queue and financial admission are checked by the adapter while the
same financial write transaction holds the SQLite lock.
"""
import json,tempfile
from decimal import Decimal
from pathlib import Path

def install(db):
 path=Path(db.execute('PRAGMA database_list').fetchone()[2]);root=Path(tempfile.gettempdir()).resolve()
 if path.is_symlink() or not path.resolve().is_relative_to(root) or db.in_transaction:raise ValueError('temporary_database_required')
 tables={x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
 if not {'runtime_orders','futures_events','cross_positions','margin_positions'}<=tables:raise ValueError('shared_schema_required')
 db.execute('BEGIN IMMEDIATE')
 try:
  if db.execute('SELECT 1 FROM runtime_orders LIMIT 1').fetchone() or db.execute('SELECT 1 FROM futures_events LIMIT 1').fetchone() or db.execute('SELECT 1 FROM cross_positions LIMIT 1').fetchone() or db.execute('SELECT 1 FROM margin_positions LIMIT 1').fetchone():raise ValueError('fresh_experiment_required')
  spot=db.execute("SELECT value FROM runtime_meta WHERE key='position'").fetchone();future=db.execute("SELECT value FROM futures_meta WHERE key='position'").fetchone()
  if not spot or Decimal(str(json.loads(spot[0]).get('quantity','NaN')))!=0 or (future and json.loads(future[0]) is not None):raise ValueError('fresh_experiment_required')
  db.execute('CREATE TABLE IF NOT EXISTS adaptive_admission_token(action_id TEXT PRIMARY KEY,market TEXT NOT NULL)')
  db.execute("CREATE TRIGGER IF NOT EXISTS adaptive_spot_fence BEFORE INSERT ON runtime_orders WHEN NEW.side='BUY' AND NOT EXISTS(SELECT 1 FROM adaptive_admission_token WHERE action_id=NEW.business_key AND market='spot') BEGIN SELECT RAISE(ABORT,'adaptive admission required'); END")
  db.execute("CREATE TRIGGER IF NOT EXISTS adaptive_spot_consume AFTER INSERT ON runtime_orders WHEN NEW.side='BUY' BEGIN DELETE FROM adaptive_admission_token WHERE action_id=NEW.business_key AND market='spot'; END")
  db.execute("CREATE TRIGGER IF NOT EXISTS adaptive_futures_fence BEFORE INSERT ON futures_events WHEN NEW.event='OPEN' AND NOT EXISTS(SELECT 1 FROM adaptive_admission_token WHERE action_id=NEW.business_key AND market='futures') BEGIN SELECT RAISE(ABORT,'adaptive admission required'); END")
  db.execute("CREATE TRIGGER IF NOT EXISTS adaptive_futures_consume AFTER INSERT ON futures_events WHEN NEW.event='OPEN' BEGIN DELETE FROM adaptive_admission_token WHERE action_id=NEW.business_key AND market='futures'; END")
  for table in ('cross_positions','margin_positions'):
   db.execute('CREATE TRIGGER IF NOT EXISTS adaptive_'+table+'_fence BEFORE INSERT ON '+table+" BEGIN SELECT RAISE(ABORT,'adaptive market unavailable'); END")
  db.commit()
 except BaseException:
  db.rollback();raise

def authorize(db,action_id,market):
 """Call only after require_clear inside the already-held write transaction."""
 if not db.in_transaction:raise ValueError('write_transaction_required')
 enabled=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='adaptive_admission_token'").fetchone()
 if enabled:db.execute('INSERT INTO adaptive_admission_token VALUES(?,?)',(action_id,market))
 return bool(enabled)

def clear(db,action_id,market):
 if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='adaptive_admission_token'").fetchone():db.execute('DELETE FROM adaptive_admission_token WHERE action_id=? AND market=?',(action_id,market))

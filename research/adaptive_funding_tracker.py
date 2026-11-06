"""Durable, conservative funding cursor for an isolated research position.

Funding data are supplied by the public USD-M adapter. This module performs no
network IO, and cannot infer an unobserved funding schedule after a long outage.
"""
from .adaptive_funding_evidence import validate_mark,match_observed_due,record
from pathlib import Path
import tempfile
from trading_bot.futures_store import FuturesStore

def _sandbox(store):
 if type(store) is not FuturesStore or not store.shared:raise ValueError('research_futures_required')
 path=Path(store.db.execute('PRAGMA database_list').fetchone()[2])
 if path.is_symlink() or not path.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):raise ValueError('sandbox_path_required')

def register(store,*,position_id,next_due_ms,observed_ms):
 _sandbox(store)
 pos=store.get('position')
 if not pos or pos.get('id')!=position_id or type(next_due_ms) is not int or type(observed_ms) is not int or not 0<=pos['opened_ms']-observed_ms<=120000 or next_due_ms<=pos['opened_ms']:raise ValueError('initial_funding_schedule_invalid')
 db=store.db;db.execute('BEGIN IMMEDIATE')
 try:
  db.execute('CREATE TABLE IF NOT EXISTS adaptive_funding_schedule(position_id TEXT PRIMARY KEY,next_due_ms INTEGER NOT NULL,observed_ms INTEGER NOT NULL)')
  old=db.execute('SELECT next_due_ms,observed_ms FROM adaptive_funding_schedule WHERE position_id=?',(position_id,)).fetchone()
  if old and tuple(old)!=(next_due_ms,observed_ms):raise ValueError('initial_funding_schedule_conflict')
  db.execute('INSERT OR IGNORE INTO adaptive_funding_schedule VALUES(?,?,?)',(position_id,next_due_ms,observed_ms));db.commit()
 except BaseException:
  db.rollback();raise

def advance(store,*,position_id,mark_response,received_ms,history=None,fault=lambda point:None):
 """Advance only across observed, verified events; persist evidence first."""
 _sandbox(store);pos=store.get('position');mark=validate_mark(mark_response,received_ms=received_ms)
 if not pos or pos.get('id')!=position_id:raise ValueError('funding_position_changed')
 db=store.db;row=db.execute('SELECT next_due_ms FROM adaptive_funding_schedule WHERE position_id=?',(position_id,)).fetchone()
 if not row:raise ValueError('funding_schedule_missing')
 old_due=row[0]
 if received_ms<old_due:
  if mark['next_due_ms']!=old_due:raise ValueError('funding_schedule_changed')
  store.apply_funding(position_id,[],received_ms)
  db.execute('BEGIN IMMEDIATE')
  try:
   updated=db.execute('UPDATE adaptive_funding_schedule SET observed_ms=? WHERE position_id=? AND next_due_ms=?',(received_ms,position_id,old_due))
   if updated.rowcount!=1:raise ValueError('funding_schedule_conflict')
   db.commit()
  except BaseException:db.rollback();raise
  return {'status':'covered','through_ms':received_ms}
 if (mark['next_due_ms']<=received_ms or mark['next_due_ms']<=old_due or not isinstance(history,dict) or
     history.get('source')!='binance_usdm_funding_rate' or history.get('symbol')!='BTCUSDT' or
     type(history.get('start_ms')) is not int or history['start_ms']>old_due or
     type(history.get('end_ms')) is not int or history['end_ms']<received_ms or
     type(history.get('received_ms')) is not int or history['received_ms']<history['end_ms'] or
     history['end_ms']-history['start_ms']>7*86400000 or not isinstance(history.get('rows'),list) or len(history['rows'])>=1000):
  raise ValueError('funding_event_unconfirmed')
 # If the collector missed more than one due event, the next_due observed now
 # alone does not prove that intermediate funding events were all captured.
 matches=[r for r in history['rows'] if isinstance(r,dict) and r.get('symbol')=='BTCUSDT' and type(r.get('fundingTime')) is int and old_due<=r['fundingTime']<=received_ms]
 if len(matches)!=1:raise ValueError('funding_history_incomplete_or_ambiguous')
 event=match_observed_due(matches,due_ms=old_due,received_ms=received_ms)
 record(db,matches[0],observed_ms=history['received_ms']);fault('after_evidence')
 core={k:event[k] for k in ('time','rate','mark')}
 store.apply_funding(position_id,[core],received_ms);fault('after_application')
 db.execute('BEGIN IMMEDIATE')
 try:
  row=db.execute('SELECT next_due_ms FROM adaptive_funding_schedule WHERE position_id=?',(position_id,)).fetchone()
  if not row or row[0] not in (old_due,mark['next_due_ms']):raise ValueError('funding_schedule_conflict')
  if row[0]==old_due:db.execute('UPDATE adaptive_funding_schedule SET next_due_ms=?,observed_ms=? WHERE position_id=?',(mark['next_due_ms'],received_ms,position_id))
  db.commit()
 except BaseException:
  db.rollback();raise
 return {'status':'applied','event_ms':event['time'],'through_ms':received_ms}

def covered(store,position,now_ms):
 """Short handoff after a fresh mark, never across the observed due time."""
 cursor=position.get('funding_cursor',-1)
 if type(cursor) is not int or type(now_ms) is not int:return False
 if cursor>=now_ms:return True
 if not 0<=now_ms-cursor<=5000:return False
 db=store.db
 if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='adaptive_funding_schedule'").fetchone():return False
 row=db.execute('SELECT next_due_ms,observed_ms FROM adaptive_funding_schedule WHERE position_id=?',(position.get('id'),)).fetchone()
 return bool(row and row[0]>now_ms and type(row[1]) is int and 0<=now_ms-row[1]<=5000)

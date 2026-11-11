"""Isolated research route journal. Does not place or schedule any order."""
import json,sqlite3,tempfile,time
from pathlib import Path
from decimal import Decimal
from .adaptive_route_policy import route,VERSION

class RouteState:
 def __init__(self,path,*,clock=lambda:int(time.time()*1000)):
  self.path=Path(path);self.clock=clock
  if self.path.is_symlink() or not self.path.parent.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):raise ValueError('sandbox_path_required')
  self.db=sqlite3.connect(self.path,isolation_level=None,timeout=15)
  self.db.row_factory=sqlite3.Row;self.db.execute('PRAGMA synchronous=FULL')
  names={row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
  if names and names not in ({'route_state','route_actions'},{'route_state','route_actions','route_exits'},{'route_state','route_actions','route_exits','route_exit_pending'}):
   self.db.close();raise ValueError('dedicated_route_database_required')
  self.db.executescript('CREATE TABLE IF NOT EXISTS route_state(id INTEGER PRIMARY KEY CHECK(id=1), phase TEXT NOT NULL, action_id TEXT, strategy TEXT, market TEXT, direction TEXT, position_id TEXT);'
                        "INSERT OR IGNORE INTO route_state(id,phase) VALUES(1,'IDLE');"
                        'CREATE TABLE IF NOT EXISTS route_actions(action_id TEXT PRIMARY KEY, decision_json TEXT NOT NULL, signal_json TEXT NOT NULL, phase TEXT NOT NULL);'
                        'CREATE TABLE IF NOT EXISTS route_exits(entry_action TEXT PRIMARY KEY,exit_action TEXT NOT NULL,position_id TEXT NOT NULL);'
                        'CREATE TABLE IF NOT EXISTS route_exit_pending(entry_action TEXT PRIMARY KEY,exit_action TEXT NOT NULL,position_id TEXT NOT NULL);')
 def close(self):self.db.close()
 def status(self):return dict(self.db.execute('SELECT * FROM route_state WHERE id=1').fetchone())
 def begin_entry(self,action_id,decision,signal,*,snapshot_id,data_ms,symbol,decision_version,quote_ms,expiry_ms,eligible=False,research_sandbox=False,route_policy=VERSION):
  if not isinstance(action_id,str) or not action_id or len(action_id)>100:raise ValueError('invalid_action')
  if not isinstance(signal,dict) or signal.get('symbol')!=symbol or signal.get('snapshot_id')!=snapshot_id:raise ValueError('signal_mismatch')
  if signal.get('source') not in ('AI','baseline'):raise ValueError('decision_source_required')
  if 'route_policy' in signal and signal['route_policy']!=route_policy:raise ValueError('signal_policy_mismatch')
  signal=dict(signal,decision_version=decision_version)
  if route_policy!=VERSION:signal['route_policy']=route_policy
  elif 'route_policy' in signal:raise ValueError('signal_policy_mismatch')
  canonical=lambda x:json.dumps(x,sort_keys=True,separators=(',',':'))
  self.db.execute('BEGIN IMMEDIATE')
  try:
   previous=self.db.execute('SELECT * FROM route_actions WHERE action_id=?',(action_id,)).fetchone()
   if previous:
    if previous['decision_json']!=canonical(decision) or previous['signal_json']!=canonical(signal):raise ValueError('action_identity_conflict')
    self.db.commit();return {'replayed':True,'phase':previous['phase']}
   now=self.clock()
   result=route(decision,snapshot_id=snapshot_id,data_ms=data_ms,now_ms=now,symbol=symbol,decision_version=decision_version,eligible=eligible,research_sandbox=research_sandbox,policy=route_policy)
   if not result['allowed']:raise ValueError(result['reason'])
   if type(quote_ms) is not int or type(expiry_ms) is not int or not 0<=now-quote_ms<=5000 or now>=expiry_ms:raise ValueError('stale_quote_or_signal')
   if self.status()['phase']!='IDLE':raise ValueError('exposure_or_pending')
   if signal.get('strategy')!=result['strategy'] or signal.get('direction')!=result['direction']:raise ValueError('signal_route_mismatch')
   self.db.execute('INSERT INTO route_actions VALUES(?,?,?,?)',(action_id,canonical(decision),canonical(signal),'ENTRY_PENDING'))
   self.db.execute('UPDATE route_state SET phase=?,action_id=?,strategy=?,market=?,direction=? WHERE id=1',('ENTRY_PENDING',action_id,result['strategy'],result['market'],result['direction']))
   self.db.commit();return {'replayed':False,**result,'phase':'ENTRY_PENDING'}
  except BaseException:
   if self.db.in_transaction:self.db.rollback()
   raise
 def require_reconciliation(self,action_id):
  self.db.execute('BEGIN IMMEDIATE')
  try:
   row=self.db.execute('SELECT phase,action_id FROM route_state WHERE id=1').fetchone()
   if row['action_id']!=action_id or row['phase'] not in ('ENTRY_PENDING','EXIT_PENDING'):raise ValueError('action_state_mismatch')
   self.db.execute("UPDATE route_state SET phase='RECONCILE' WHERE id=1")
   self.db.execute("UPDATE route_actions SET phase='RECONCILE' WHERE action_id=?",(action_id,));self.db.commit()
  except BaseException:
   if self.db.in_transaction:self.db.rollback()
   raise
 def confirm_spot_not_filled(self,action_id,financial_path):
  p=Path(financial_path)
  if p.is_symlink() or not p.is_file():raise ValueError('invalid_financial_fixture')
  p=p.resolve()
  if not p.is_relative_to(Path(tempfile.gettempdir()).resolve()) or p.parent!=self.path.resolve().parent:raise ValueError('fixture_outside_sandbox')
  self.db.execute('BEGIN IMMEDIATE');financial=None
  try:
   row=self.db.execute('SELECT * FROM route_state WHERE id=1').fetchone()
   if row['action_id']!=action_id or row['phase'] not in ('ENTRY_PENDING','RECONCILE') or row['market']!='spot':raise ValueError('action_state_mismatch')
   financial=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True,timeout=10,isolation_level=None);financial.execute('BEGIN')
   order=financial.execute('SELECT client_id,side,phase,status FROM runtime_orders WHERE business_key=?',(action_id,)).fetchone()
   if not order or order[1]!='BUY' or order[2]!='SETTLED' or order[3] not in ('EXPIRED','CANCELED','REJECTED') or financial.execute('SELECT 1 FROM runtime_fills WHERE client_id=?',(order[0],)).fetchone():raise ValueError('unfilled_order_not_proven')
   self.db.execute("UPDATE route_actions SET phase='CANCELED' WHERE action_id=?",(action_id,))
   self.db.execute("UPDATE route_state SET phase='IDLE',action_id=NULL,strategy=NULL,market=NULL,direction=NULL,position_id=NULL WHERE id=1")
   self.db.commit();return {'phase':'IDLE'}
  except BaseException:
   if self.db.in_transaction:self.db.rollback()
   raise
  finally:
   if financial is not None:financial.close()
 def confirm_futures_not_filled_v2(self,action_id,financial_path):
  """Cancel only under the same route+financial locks used by v2 entry."""
  p=Path(financial_path)
  if p.is_symlink() or not p.is_file():raise ValueError('invalid_financial_fixture')
  p=p.resolve()
  if not p.is_relative_to(Path(tempfile.gettempdir()).resolve()) or p.parent!=self.path.resolve().parent:raise ValueError('fixture_outside_sandbox')
  self.db.execute('ATTACH DATABASE ? AS financial_guard',(str(p),))
  try:
   self.db.execute('BEGIN IMMEDIATE')
   state=self.db.execute('SELECT phase,action_id,market FROM route_state WHERE id=1').fetchone()
   if not state or tuple(state)!=('ENTRY_PENDING',action_id,'futures'):raise ValueError('action_state_mismatch')
   if self.db.execute('SELECT 1 FROM financial_guard.futures_events WHERE business_key=?',(action_id,)).fetchone():raise ValueError('financial_event_exists')
   row=self.db.execute("SELECT value FROM financial_guard.futures_meta WHERE key='position'").fetchone()
   if row and json.loads(row[0]) is not None:raise ValueError('position_exists')
   self.db.execute("UPDATE route_actions SET phase='CANCELED' WHERE action_id=?",(action_id,))
   self.db.execute("UPDATE route_state SET phase='IDLE',action_id=NULL,strategy=NULL,market=NULL,direction=NULL,position_id=NULL WHERE id=1")
   self.db.commit();return {'phase':'IDLE'}
  except BaseException:
   if self.db.in_transaction:self.db.rollback()
   raise
  finally:self.db.execute('DETACH DATABASE financial_guard')
 def confirm_open(self,action_id,financial_path):
  """Advance only after observing a matching real paper ledger commit.

  The route and financial DB are distinct: this observation is not atomic with
  future changes. A caller must reconcile again before allowing another entry.
  """
  p=Path(financial_path)
  if p.is_symlink() or not p.is_file():raise ValueError('invalid_financial_fixture')
  p=p.resolve();root=Path(tempfile.gettempdir()).resolve()
  if not p.is_relative_to(root) or p.parent!=self.path.resolve().parent:raise ValueError('fixture_outside_sandbox')
  self.db.execute('BEGIN IMMEDIATE');financial=None
  try:
   row=self.db.execute('SELECT * FROM route_state WHERE id=1').fetchone()
   if row['action_id']!=action_id or row['phase'] not in ('ENTRY_PENDING','OPEN','RECONCILE'):raise ValueError('action_state_mismatch')
   financial=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True,timeout=10,isolation_level=None);financial.row_factory=sqlite3.Row
   financial.execute('BEGIN')
   if row['market']=='spot':
    order=financial.execute('SELECT rowid,client_id,side,phase,status FROM runtime_orders WHERE business_key=?',(action_id,)).fetchone()
    item=financial.execute("SELECT value FROM runtime_meta WHERE key='position'").fetchone()
    position=json.loads(item[0]) if item else None
    if (not order or (order['side'],order['phase'],order['status'])!=('BUY','SETTLED','FILLED') or
        not position or Decimal(position.get('quantity','0'))<=0 or
        financial.execute('SELECT 1 FROM runtime_orders WHERE rowid>? LIMIT 1',(order['rowid'],)).fetchone() or
        not financial.execute('SELECT 1 FROM runtime_fills WHERE client_id=?',(order['client_id'],)).fetchone()):raise ValueError('entry_not_confirmed')
   elif row['market']=='futures':
    order=financial.execute("SELECT payload FROM futures_events WHERE business_key=? AND event='OPEN'",(action_id,)).fetchone()
    item=financial.execute("SELECT value FROM futures_meta WHERE key='position'").fetchone()
    position=json.loads(item[0]) if item else None
    recorded=json.loads(order[0]) if order else None
    if not recorded or not position or position.get('id')!=recorded.get('id') or row['direction'] not in ('LONG','SHORT') or position.get('direction')!=row['direction'] or recorded.get('direction')!=row['direction'] or position.get('symbol','BTCUSDT')!='BTCUSDT':raise ValueError('entry_not_confirmed')
   else:raise ValueError('market_policy_absent')
   if row['phase']=='OPEN' and row['position_id']!=position['id']:raise ValueError('position_changed')
   self.db.execute("UPDATE route_state SET phase='OPEN',position_id=? WHERE id=1",(position['id'],))
   self.db.execute("UPDATE route_actions SET phase='OPEN' WHERE action_id=?",(action_id,));self.db.commit()
   return {'phase':'OPEN','position_id':position['id'],'replayed':row['phase']=='OPEN'}
  except BaseException:
   if self.db.in_transaction:self.db.rollback()
   raise
  finally:
   if financial is not None:financial.close()
 def confirm_spot_closed(self,entry_action_id,exit_action_id,financial_path):
  """Observe a committed full paper exit; no financial rows are modified."""
  p=Path(financial_path)
  if p.is_symlink() or not p.is_file():raise ValueError('invalid_financial_fixture')
  p=p.resolve();root=Path(tempfile.gettempdir()).resolve()
  if not p.is_relative_to(root) or p.parent!=self.path.resolve().parent:raise ValueError('fixture_outside_sandbox')
  self.db.execute('BEGIN IMMEDIATE');financial=None
  try:
   action=self.db.execute('SELECT phase FROM route_actions WHERE action_id=?',(entry_action_id,)).fetchone()
   if action and action['phase']=='CLOSED':
    prior=self.db.execute('SELECT exit_action FROM route_exits WHERE entry_action=?',(entry_action_id,)).fetchone()
    if not prior or prior[0]!=exit_action_id:raise ValueError('exit_identity_conflict')
    self.db.commit();return {'phase':'IDLE','replayed':True}
   row=self.db.execute('SELECT * FROM route_state WHERE id=1').fetchone()
   pending=self.db.execute('SELECT exit_action,position_id FROM route_exit_pending WHERE entry_action=?',(entry_action_id,)).fetchone()
   if row['action_id']!=entry_action_id or row['phase']!='EXIT_PENDING' or row['market']!='spot' or not pending or tuple(pending)!=(exit_action_id,row['position_id']):raise ValueError('action_state_mismatch')
   financial=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True,timeout=10,isolation_level=None);financial.row_factory=sqlite3.Row;financial.execute('BEGIN')
   order=financial.execute('SELECT client_id,side,phase,status FROM runtime_orders WHERE business_key=?',(exit_action_id,)).fetchone()
   pos=financial.execute("SELECT value FROM runtime_meta WHERE key='position'").fetchone()
   binding=financial.execute('SELECT value FROM runtime_meta WHERE key=?',('binding:'+order['client_id'],)).fetchone() if order else None
   if (not order or (order['side'],order['phase'],order['status'])!=('SELL','SETTLED','FILLED') or
       not pos or Decimal(json.loads(pos[0]).get('quantity','-1'))!=0 or not binding or json.loads(binding[0])!=row['position_id'] or
       financial.execute("SELECT 1 FROM runtime_orders WHERE phase!='SETTLED' LIMIT 1").fetchone()):raise ValueError('exit_not_confirmed')
   self.db.execute("UPDATE route_actions SET phase='CLOSED' WHERE action_id=?",(entry_action_id,))
   self.db.execute('INSERT INTO route_exits VALUES(?,?,?)',(entry_action_id,exit_action_id,row['position_id']))
   self.db.execute('DELETE FROM route_exit_pending WHERE entry_action=?',(entry_action_id,))
   self.db.execute("UPDATE route_state SET phase='IDLE',action_id=NULL,strategy=NULL,market=NULL,direction=NULL,position_id=NULL WHERE id=1")
   self.db.commit();return {'phase':'IDLE','replayed':False}
  except BaseException:
   if self.db.in_transaction:self.db.rollback()
   raise
  finally:
   if financial is not None:financial.close()
 def confirm_futures_closed(self,entry_action_id,exit_action_id,financial_path):
  p=Path(financial_path)
  if p.is_symlink() or not p.is_file():raise ValueError('invalid_financial_fixture')
  p=p.resolve()
  if not p.is_relative_to(Path(tempfile.gettempdir()).resolve()) or p.parent!=self.path.resolve().parent:raise ValueError('fixture_outside_sandbox')
  self.db.execute('BEGIN IMMEDIATE');financial=None
  try:
   action=self.db.execute('SELECT phase FROM route_actions WHERE action_id=?',(entry_action_id,)).fetchone()
   if action and action['phase']=='CLOSED':
    prior=self.db.execute('SELECT exit_action FROM route_exits WHERE entry_action=?',(entry_action_id,)).fetchone()
    if not prior or prior[0]!=exit_action_id:raise ValueError('exit_identity_conflict')
    self.db.commit();return {'phase':'IDLE','replayed':True}
   row=self.db.execute('SELECT * FROM route_state WHERE id=1').fetchone()
   pending=self.db.execute('SELECT exit_action,position_id FROM route_exit_pending WHERE entry_action=?',(entry_action_id,)).fetchone()
   if row['action_id']!=entry_action_id or row['phase']!='EXIT_PENDING' or row['market']!='futures' or not pending or tuple(pending)!=(exit_action_id,row['position_id']):raise ValueError('action_state_mismatch')
   financial=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True,timeout=10,isolation_level=None);financial.execute('BEGIN')
   order=financial.execute("SELECT payload FROM futures_events WHERE business_key=? AND event='CLOSE'",(exit_action_id,)).fetchone()
   pos=financial.execute("SELECT value FROM futures_meta WHERE key='position'").fetchone()
   event=json.loads(order[0]) if order else None
   if not event or event.get('id')!=row['position_id'] or not pos or json.loads(pos[0]) is not None:raise ValueError('exit_not_confirmed')
   self.db.execute('INSERT INTO route_exits VALUES(?,?,?)',(entry_action_id,exit_action_id,row['position_id']))
   self.db.execute('DELETE FROM route_exit_pending WHERE entry_action=?',(entry_action_id,))
   self.db.execute("UPDATE route_actions SET phase='CLOSED' WHERE action_id=?",(entry_action_id,))
   self.db.execute("UPDATE route_state SET phase='IDLE',action_id=NULL,strategy=NULL,market=NULL,direction=NULL,position_id=NULL WHERE id=1")
   self.db.commit();return {'phase':'IDLE','replayed':False}
  except BaseException:
   if self.db.in_transaction:self.db.rollback()
   raise
  finally:
   if financial is not None:financial.close()
 def begin_exit(self,entry_action_id,exit_action_id):
  if not isinstance(exit_action_id,str) or not exit_action_id or len(exit_action_id)>100:raise ValueError('invalid_action')
  self.db.execute('BEGIN IMMEDIATE')
  try:
   closed=self.db.execute('SELECT exit_action FROM route_exits WHERE entry_action=?',(entry_action_id,)).fetchone()
   if closed:
    if closed[0]!=exit_action_id:raise ValueError('exit_identity_conflict')
    self.db.commit();return {'phase':'CLOSED','replayed':True}
   row=self.db.execute('SELECT * FROM route_state WHERE id=1').fetchone()
   if row['action_id']!=entry_action_id or row['phase'] not in ('OPEN','EXIT_PENDING'):raise ValueError('action_state_mismatch')
   prior=self.db.execute('SELECT exit_action,position_id FROM route_exit_pending WHERE entry_action=?',(entry_action_id,)).fetchone()
   if prior and tuple(prior)!=(exit_action_id,row['position_id']):raise ValueError('exit_identity_conflict')
   self.db.execute('INSERT OR IGNORE INTO route_exit_pending VALUES(?,?,?)',(entry_action_id,exit_action_id,row['position_id']))
   self.db.execute("UPDATE route_state SET phase='EXIT_PENDING' WHERE id=1");self.db.commit()
   return {'phase':'EXIT_PENDING','replayed':bool(prior)}
  except BaseException:
   if self.db.in_transaction:self.db.rollback()
   raise

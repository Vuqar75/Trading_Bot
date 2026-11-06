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
  if names and names!={'route_state','route_actions'}:
   self.db.close();raise ValueError('dedicated_route_database_required')
  self.db.executescript('CREATE TABLE IF NOT EXISTS route_state(id INTEGER PRIMARY KEY CHECK(id=1), phase TEXT NOT NULL, action_id TEXT, strategy TEXT, market TEXT, direction TEXT, position_id TEXT);'
                        "INSERT OR IGNORE INTO route_state(id,phase) VALUES(1,'IDLE');"
                        'CREATE TABLE IF NOT EXISTS route_actions(action_id TEXT PRIMARY KEY, decision_json TEXT NOT NULL, signal_json TEXT NOT NULL, phase TEXT NOT NULL);')
 def close(self):self.db.close()
 def status(self):return dict(self.db.execute('SELECT * FROM route_state WHERE id=1').fetchone())
 def begin_entry(self,action_id,decision,signal,*,snapshot_id,data_ms,symbol,decision_version,quote_ms,expiry_ms,eligible=False,research_sandbox=False):
  if not isinstance(action_id,str) or not action_id or len(action_id)>100:raise ValueError('invalid_action')
  if not isinstance(signal,dict) or signal.get('symbol')!=symbol or signal.get('snapshot_id')!=snapshot_id:raise ValueError('signal_mismatch')
  canonical=lambda x:json.dumps(x,sort_keys=True,separators=(',',':'))
  self.db.execute('BEGIN IMMEDIATE')
  try:
   previous=self.db.execute('SELECT * FROM route_actions WHERE action_id=?',(action_id,)).fetchone()
   if previous:
    if previous['decision_json']!=canonical(decision) or previous['signal_json']!=canonical(signal):raise ValueError('action_identity_conflict')
    self.db.commit();return {'replayed':True,'phase':previous['phase']}
   now=self.clock()
   result=route(decision,snapshot_id=snapshot_id,data_ms=data_ms,now_ms=now,symbol=symbol,decision_version=decision_version,eligible=eligible,research_sandbox=research_sandbox)
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
   if row['action_id']!=action_id or row['phase']!='ENTRY_PENDING':raise ValueError('action_state_mismatch')
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
    if not recorded or not position or position.get('id')!=recorded.get('id') or position.get('direction')!='SHORT' or position.get('symbol','BTCUSDT')!='BTCUSDT':raise ValueError('entry_not_confirmed')
   else:raise ValueError('market_policy_absent')
   self.db.execute("UPDATE route_state SET phase='OPEN',position_id=? WHERE id=1",(position['id'],))
   self.db.execute("UPDATE route_actions SET phase='OPEN' WHERE action_id=?",(action_id,));self.db.commit()
   return {'phase':'OPEN','position_id':position['id']}
  except BaseException:
   if self.db.in_transaction:self.db.rollback()
   raise
  finally:
   if financial is not None:financial.close()

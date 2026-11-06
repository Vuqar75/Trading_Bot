import sqlite3,tempfile,threading,unittest
from decimal import Decimal
from pathlib import Path
from research.adaptive_route_state import RouteState
from research.adaptive_shadow import PERIOD
from trading_bot.app import Application
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.runtime_store import RuntimeStore
from trading_bot.futures_store import FuturesStore

class DurableRouteTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'route.sqlite';self.now=20000*PERIOD
  self.d={'mode':'TREND_UP','snapshot_id':'s','data_ms':self.now,'expires_ms':self.now+PERIOD,'reasons':['r'],'uncertainties':['u']}
  self.signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'LONG'}
  self.kw={'snapshot_id':'s','data_ms':self.now,'symbol':'BTCUSDT','decision_version':'regular-regime-horizon-v2','quote_ms':self.now,'expiry_ms':self.now+10000,'eligible':True,'research_sandbox':True}
 def store(self,clock=None):
  s=RouteState(self.path,clock=clock or (lambda:self.now));self.addCleanup(s.close);return s
 def test_one_pending_action_replays_and_survives_restart(self):
  s=self.store();self.assertFalse(s.begin_entry('a',self.d,self.signal,**self.kw)['replayed'])
  self.assertTrue(s.begin_entry('a',self.d,self.signal,**self.kw)['replayed'])
  with self.assertRaisesRegex(ValueError,'exposure_or_pending'):s.begin_entry('b',self.d,self.signal,**self.kw)
  second=self.store();self.assertEqual(second.status()['phase'],'ENTRY_PENDING');second.require_reconciliation('a');self.assertEqual(s.status()['phase'],'RECONCILE')
 def test_stale_after_real_sqlite_wait(self):
  s=self.store();locked=threading.Event();released=threading.Event();clock=lambda:self.now+(6000 if released.is_set() else 0)
  def hold():
   db=sqlite3.connect(self.path,isolation_level=None);db.execute('BEGIN IMMEDIATE');locked.set();released.wait(3);db.rollback();db.close()
  worker=self.store(clock);t=threading.Thread(target=hold);t.start();self.assertTrue(locked.wait(2))
  timer=threading.Timer(.1,released.set);timer.start()
  try:
   with self.assertRaisesRegex(ValueError,'stale_quote'):worker.begin_entry('a',self.d,self.signal,**self.kw)
  finally:released.set();timer.cancel();t.join()
  self.assertEqual(s.status()['phase'],'IDLE')
 def test_concurrent_decisions_leave_only_one_pending(self):
  self.store();barrier=threading.Barrier(2);out=[]
  def run(action):
   s=RouteState(self.path,clock=lambda:self.now)
   try:
    barrier.wait()
    try:out.append(s.begin_entry(action,self.d,self.signal,**self.kw)['phase'])
    except ValueError as e:out.append(str(e))
   finally:s.close()
  threads=[threading.Thread(target=run,args=(x,)) for x in ('a','b')]
  for t in threads:t.start()
  for t in threads:t.join(5);self.assertFalse(t.is_alive())
  self.assertCountEqual(out,['ENTRY_PENDING','exposure_or_pending'])
 def test_open_requires_real_matching_paper_fill(self):
  route=self.store();route.begin_entry('a',self.d,self.signal,**self.kw)
  path=self.path.parent/'financial.sqlite';store=RuntimeStore(path,{'mode':'paper','symbol':'BTCUSDT'})
  try:
   app=Application(store,PaperExchange(),None);store.command(1,True,'/resume')
   with self.assertRaisesRegex(ValueError,'dedicated_route_database'):RouteState(path)
   with self.assertRaisesRegex(ValueError,'not_confirmed'):route.confirm_open('a',path)
   self.assertTrue(app.execute('BUY','0.1',Decimal('100'),'a','TEST'))
   result=route.confirm_open('a',path);self.assertEqual(result['phase'],'OPEN');self.assertEqual(route.status()['position_id'],store.get('position')['id'])
  finally:store.close()
 def test_short_open_requires_real_isolated_event(self):
  route=self.store();decision=dict(self.d,mode='TREND_DOWN');signal=dict(self.signal,direction='SHORT')
  route.begin_entry('short',decision,signal,**self.kw)
  path=self.path.parent/'isolated.sqlite';store=FuturesStore(path,slippage_bps='0')
  try:
   store.command(1,True,'/resume')
   with self.assertRaisesRegex(ValueError,'not_confirmed'):route.confirm_open('short',path)
   self.assertTrue(store.open_position('SHORT',Decimal('100'),1000,'short'))
   result=route.confirm_open('short',path);self.assertEqual(result['position_id'],store.get('position')['id'])
  finally:store.close()

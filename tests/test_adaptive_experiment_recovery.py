import json,unittest
from research.adaptive_experiment_recovery import reconcile,recover_spot
from research.adaptive_route_state import RouteState
from research.adaptive_spot_worker import process_entry,process_stop
from tests import test_adaptive_atomic_spot as fixture
from tests.test_runtime import symbol_info

class RecoveryTest(unittest.TestCase):
 def setUp(self):
  self.f=fixture.AtomicSpotTest();self.f.setUp();self.addCleanup(self.f.doCleanups);f=self.f
  self.state=RouteState(f.root/'route',clock=lambda:f.now);self.addCleanup(self.state.close)
  self.args={'decision':f.decision,'signal':{'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'LONG'},'data_ms':f.now-1000,'decision_version':'regular-regime-horizon-v2','bars':f.bars,'skip_bar':(196*900000,100.,101.,99.,100.),'quote':'100','quote_ms':f.now,'filters':symbol_info(),'filters_ms':f.now,'clock':lambda:f.now}
 def test_lost_entry_ack_reconciles_without_quote_or_model(self):
  f=self.f
  def fault(point):
   if point=='before_queue_ack':raise RuntimeError('crash')
  with self.assertRaises(RuntimeError):process_entry(f.q,f.store,self.state,**self.args,fault=fault)
  self.assertEqual(reconcile(f.q,self.state,f.fin)['status'],'open')
  self.assertEqual(reconcile(f.q,self.state,f.fin)['status'],'open')
  self.assertEqual(f.store.db.execute('SELECT count(*) FROM runtime_fills').fetchone()[0],1)
  self.assertFalse(f.q.pending('spot'))
 def test_lost_exit_ack_after_route_close_is_recovered(self):
  f=self.f;process_entry(f.q,f.store,self.state,**self.args)
  with f.q.transaction():
   f.q.db.execute("INSERT INTO unified_updates VALUES(2,'exit','/research_adaptive_exit','spot')")
   f.q.db.execute("INSERT INTO unified_jobs(update_id,market,command,payload) VALUES(2,'spot','/research_adaptive_exit',?)",(json.dumps({'action_id':'exit','entry_action':'entry'}),))
  def fault(point):
   if point=='before_queue_ack':raise RuntimeError('crash')
  with self.assertRaises(RuntimeError):process_stop(f.q,f.store,self.state,quote='97',quote_ms=f.now,clock=lambda:f.now,fault=fault)
  self.assertEqual(reconcile(f.q,self.state,f.fin),{'status':'idle','acknowledged':1})
  self.assertEqual(reconcile(f.q,self.state,f.fin),{'status':'idle','acknowledged':0})
  self.assertEqual(f.store.db.execute('SELECT count(*) FROM runtime_fills').fetchone()[0],2)
 def test_unsubmitted_preparation_expires_and_pause_is_preserved(self):
  f=self.f
  def fault(point):
   if point=='after_prepare':raise RuntimeError('crash')
  with self.assertRaises(RuntimeError):process_entry(f.q,f.store,self.state,**self.args,fault=fault)
  self.assertEqual(recover_spot(f.store)['status'],'recovered')
  self.assertFalse(f.store.get('paused'))
  self.assertEqual(reconcile(f.q,self.state,f.fin)['status'],'canceled')
  self.assertEqual(f.store.db.execute('SELECT count(*) FROM runtime_fills').fetchone()[0],0)
 def test_claimed_paper_order_recovers_once_and_pause_is_preserved(self):
  f=self.f
  def fault(point):
   if point=='after_claim':raise RuntimeError('crash')
  with self.assertRaises(RuntimeError):process_entry(f.q,f.store,self.state,**self.args,fault=fault)
  self.assertEqual(recover_spot(f.store)['status'],'recovered')
  self.assertFalse(f.store.get('paused'))
  self.assertEqual(reconcile(f.q,self.state,f.fin)['status'],'open')
  self.assertEqual(f.store.db.execute('SELECT count(*) FROM runtime_fills').fetchone()[0],1)

import fcntl,json,os,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from research import adaptive_shadow as s
class ShadowTest(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.now=20000*s.PERIOD+120000
 def fetch(self,path,params=None):
  if path=='/api/v3/time':return {'serverTime':self.now}
  if path=='/api/v3/ticker/bookTicker':return {'symbol':'BTCUSDT','bidPrice':'99.9','askPrice':'100.1'}
  if path=='/api/v3/depth':return {'lastUpdateId':7,'bids':[['99.9','2']],'asks':[['100.1','2']]}
  if path=='/api/v3/klines':
   minutes={'15m':15,'1h':60,'4h':240}[params['interval']];step=minutes*60000;current=self.now//step*step
   return [[t,'100','101','99','100','10',t+step-1] for t in range(current-(params['limit']-1)*step,current+step,step)]
  raise AssertionError('unexpected path')
 def run_shadow(self,fetch=None):return s.run(self.root/'state',self.root/'public',fetch=fetch or self.fetch,clock=lambda:self.now,news_fetch=lambda **k:([],[]))
 def test_idempotence_and_no_trading_or_budget(self):
  x=self.run_shadow();y=self.run_shadow();self.assertFalse(x['entry_allowed']);self.assertEqual(x['baseline'],'RANGE');self.assertEqual(x['snapshot_id'],y['snapshot_id']);self.assertTrue(x['baseline_decision_recorded'])
  d=sqlite3.connect(self.root/'state/shadow.sqlite');self.assertEqual(d.execute('select count(*) from decisions').fetchone()[0],1);self.assertEqual(d.execute('select count(*) from snapshots').fetchone()[0],1);self.assertEqual({r[0] for r in d.execute("select name from sqlite_master where type='table'")},{'decisions','snapshots'});d.close()
  self.now+=1000;z=self.run_shadow();self.assertFalse(z['baseline_decision_recorded']);self.assertEqual(z['decision_snapshot_id'],x['snapshot_id'])
 def test_missing_data_and_lock_fail_closed(self):
  self.run_shadow();x=self.run_shadow(lambda *args,**kwargs:(_ for _ in ()).throw(ValueError('public_data_unavailable')));self.assertFalse(x['entry_allowed']);self.assertEqual(x['last_success_ms'],self.now)
  with open(self.root/'state/shadow.lock','a') as f:
   fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
   with self.assertRaisesRegex(ValueError,'already_running'):self.run_shadow()
 def test_model_validation_and_no_trade_gate(self):
  x={'mode':'TREND_UP','snapshot_id':'x','data_ms':self.now,'expires_ms':self.now+s.PERIOD,'reasons':['trend'],'uncertainties':['coverage']}
  self.assertEqual(s.validate_model(x,'x',self.now,self.now)['mode'],'TREND_UP');self.assertFalse(s.gate(x,data_healthy=True,budget_ready=True,eligible=True)['allow'])
  for change in ({'snapshot_id':'wrong'},{'expires_ms':self.now},{'mode':'BUY'},{'data_ms':self.now-1},{'order':'BUY'}):
   with self.assertRaises(ValueError):s.validate_model(x|change,'x',self.now,self.now)
  self.assertFalse(s.gate({'mode':'NO_TRADE'},data_healthy=True,budget_ready=True,eligible=True)['allow'])
 def test_no_backdated_decision_after_window(self):
  self.now=20000*s.PERIOD+301000;x=self.run_shadow();self.assertFalse(x['baseline_decision_recorded']);d=sqlite3.connect(self.root/'state/shadow.sqlite');self.assertEqual(d.execute('select count(*) from decisions').fetchone()[0],0);d.close()
 def test_crash_after_commit_never_duplicates_decision(self):
  original=os.replace
  def fail_status(src,dst):
   if Path(dst).name=='status.json':raise OSError('injected')
   return original(src,dst)
  with patch.object(s.os,'replace',side_effect=fail_status):
   with self.assertRaises(OSError):self.run_shadow()
  self.run_shadow();d=sqlite3.connect(self.root/'state/shadow.sqlite');self.assertEqual(d.execute('select count(*) from decisions').fetchone()[0],1);d.close()

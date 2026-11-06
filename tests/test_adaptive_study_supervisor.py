import tempfile,threading,unittest
from pathlib import Path
from research.adaptive_study_supervisor import bootstrap,writer_lock,run_once,collect_markets,serve,select_baseline
from research.adaptive_snapshot_proof import capture
from research.adaptive_shadow import PERIOD
from tests import test_adaptive_shadow as shadow_fixture

class SupervisorTest(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)/'study'
 def test_one_writer_and_no_reinitialization(self):
  start=bootstrap(self.root,clock=lambda:1000)
  with writer_lock(self.root):
   with self.assertRaisesRegex(ValueError,'already_running'):
    with writer_lock(self.root):pass
  self.assertEqual(bootstrap(self.root,clock=lambda:2000),start)
 def test_missing_decisions_are_not_trades_or_zero_valuations(self):
  bootstrap(self.root,clock=lambda:1000)
  result=run_once(self.root,ai_path=self.root/'no-ai',baseline_path=self.root/'no-base',publisher_uid=0,markets={},clock=lambda:2000)
  for name in ('ai','baseline'):
   self.assertEqual(result['cycles'][name]['status'],'decision_unavailable')
   self.assertEqual(result['experiments'][name]['cash_usdt'],'1000')
   self.assertEqual(result['experiments'][name]['positions'],[])
   self.assertTrue(result['experiments'][name]['ledger_balanced'])
 def test_one_loop_publishes_explicit_source_unavailability(self):
  class Unavailable:
   def spot_quote(self,**kw):raise ValueError('offline')
   def futures_quote(self,**kw):raise ValueError('offline')
   def closed_15m(self,*args,**kw):raise ValueError('offline')
  stopped=[False]
  serve(self.root,ai_path=self.root/'missing-ai',baseline_path=self.root/'missing-base',publisher_uid=0,version='test',stop=lambda:stopped[0],clock=lambda:1000,sleeper=lambda seconds:stopped.__setitem__(0,True),provider=Unavailable())
  import json
  out=json.loads((self.root/'status.json').read_text())
  self.assertEqual(out['data_errors']['spot_quote'],'unavailable')
  self.assertEqual(out['cycles']['ai']['status'],'decision_unavailable')
  self.assertEqual(out['experiments']['baseline']['positions'],[])
 def test_baseline_falls_back_after_ai_window_and_is_reused(self):
  import os
  now=20000*PERIOD+360001;self.root.mkdir(mode=0o700)
  f=shadow_fixture.ShadowTest();f.setUp();self.addCleanup(f.doCleanups);f.now=now
  calls=[]
  def collect():calls.append(1);return capture(f.fetch,clock=lambda:now,news_fetch=lambda **kw:([],[]))
  path,uid,origin=select_baseline(self.root,self.root/'missing',publisher_uid=0,start_ms=now//PERIOD*PERIOD-1,clock=lambda:now,capture_fetch=collect)
  self.assertEqual((uid,origin),(os.getuid(),'independent_baseline'));self.assertTrue(path.is_file())
  select_baseline(self.root,self.root/'missing',publisher_uid=0,start_ms=now//PERIOD*PERIOD-1,clock=lambda:now+1,capture_fetch=collect)
  self.assertEqual(len(calls),1)
 def test_funding_audit_rotates_bounded_history_without_discarding_old(self):
  import json,sqlite3
  from contextlib import closing
  from research.adaptive_funding_evidence import record
  bootstrap(self.root,clock=lambda:1000);old=2000;now=15*86400000
  row={'symbol':'BTCUSDT','fundingTime':old,'fundingRate':'0.001','markPrice':'100'}
  with closing(sqlite3.connect(self.root/'ai'/'financial.db',isolation_level=None)) as db:record(db,row,observed_ms=old+1)
  class Provider:
   def spot_quote(self,**kw):raise ValueError('offline')
   def futures_quote(self,**kw):return {'symbol':'BTCUSDT','source':'binance_usdm_mark','price':'100','event_ms':now,'received_ms':now,'next_due_ms':now+10000}
   def closed_15m(self,*args,**kw):raise ValueError('offline')
   def funding_history(self,start,end,**kw):return {'source':'binance_usdm_funding_rate','symbol':'BTCUSDT','start_ms':start,'end_ms':end,'received_ms':now,'rows':[row]}
  stopped=[False]
  serve(self.root,ai_path=self.root/'missing-ai',baseline_path=self.root/'missing-base',publisher_uid=0,version='test',stop=lambda:stopped[0],clock=lambda:now,sleeper=lambda seconds:stopped.__setitem__(0,True),provider=Provider(),capture_fetch=lambda:(_ for _ in ()).throw(ValueError('offline')))
  state=json.loads((self.root/'funding-audit.json').read_text())
  self.assertEqual(state['status'],'checked');self.assertEqual(state['next_start_ms'],old+7*86400000)
 def test_both_markets_collect_concurrently_and_quote_after_candles(self):
  bootstrap(self.root,clock=lambda:1000);gate=threading.Barrier(2);calls=[];now=9000000
  class Provider:
   def closed_15m(self,market,**kw):
    calls.append(('candles',market));gate.wait(timeout=2)
    return {'bars':[(0,100,101,99,100)],'skip_bar':(900000,100,101,99,100)}
   def spot_quote(self,**kw):calls.append(('quote','spot'));return {'received_ms':now}
   def futures_quote(self,**kw):calls.append(('quote','futures'));return {'received_ms':now}
   def filters(self,*args,**kw):raise AssertionError('fresh cached filters must be reused')
  cached={k:{'received_ms':now} for k in ('spot','futures')}
  markets,errors=collect_markets(self.root,signal_window=True,clock=lambda:now,provider=Provider(),filter_cache=cached)
  self.assertFalse(errors)
  for market in ('spot','futures'):self.assertLess(calls.index(('candles',market)),calls.index(('quote',market)))
  self.assertEqual(set(markets),{'spot','futures'})

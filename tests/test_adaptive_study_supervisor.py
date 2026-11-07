import tempfile,threading,unittest
from pathlib import Path
from research.adaptive_study_supervisor import bootstrap,writer_lock,run_once,collect_markets,serve,select_baseline,next_delay,update_watermarks
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
 def test_prior_cycle_crossing_boundary_schedules_immediate_recheck(self):
  boundary=20000*900000
  self.assertEqual(next_delay(boundary-1000,boundary+20),.1)
  self.assertEqual(next_delay(boundary-1000,boundary+1999),.1)
  self.assertEqual(next_delay(boundary-1000,boundary+2001),30.)
  self.assertAlmostEqual(next_delay(boundary-1000,boundary-20),.32)
 def test_missing_boundary_is_recorded_once_without_late_candles(self):
  self.root.mkdir(mode=0o700);boundary=20000*900000
  markets={'spot':{},'futures':{}}
  first=update_watermarks(self.root,markets,now_ms=boundary+2001,start_ms=boundary-10000,signal_window=False)
  again=update_watermarks(self.root,markets,now_ms=boundary+4000,start_ms=boundary-10000,signal_window=False)
  for market in markets:
   self.assertEqual(first[market]['missed_windows'],1)
   self.assertEqual(again[market]['missed_windows'],1)
   self.assertNotIn('last_closed_boundary_ms',again[market])
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
  self.assertEqual(out['data_errors']['spot_quote'],'unavailable_or_preboundary')
  self.assertEqual(out['cycles']['ai']['status'],'decision_unavailable')
  self.assertEqual(out['experiments']['baseline']['positions'],[])
 def test_epoch_data_unavailable_fails_closed_and_preserves_accounts(self):
  import json
  from research.adaptive_market_epoch import start_epoch
  bootstrap(self.root,clock=lambda:1000);start_epoch(self.root,market='futures',clock=lambda:2000)
  class Offline:
   def spot_quote(self,**kw):raise ValueError('offline')
   def futures_quote(self,**kw):raise ValueError('offline')
   def filters(self,*a,**kw):raise ValueError('offline')
   def closed_15m(self,*a,**kw):raise ValueError('offline')
  stopped=[False]
  serve(self.root,ai_path=self.root/'missing-ai',baseline_path=self.root/'missing-base',publisher_uid=0,version='test',stop=lambda:stopped[0],clock=lambda:3000,sleeper=lambda seconds:stopped.__setitem__(0,True),provider=Offline())
  result=json.loads((self.root/'status.json').read_text())
  self.assertEqual(result['data_errors']['futures_history'],'epoch_data_unavailable')
  self.assertEqual(result['usdm_research']['epoch_start_ms'],2000)
  self.assertEqual(result['usdm_research']['limits_status'],'enforced_v2_research_only')
  for name in ('ai','baseline'):self.assertEqual(result['experiments'][name]['cash_usdt'],'1000')
 def test_capture_coverage_is_explicit_and_rejects_untrusted_data(self):
  import json,os
  from research.adaptive_study_supervisor import capture_coverage
  self.root.mkdir(mode=0o700);p=self.root/'usdm-capture-summary.json'
  self.assertEqual(capture_coverage(self.root)['status'],'unavailable')
  p.write_text(json.dumps({'sha256':'a'*64,'start_ms':1,'end_ms':2,'snapshots':360,'trade_gaps':86,'unusable_time':2}));p.chmod(0o600)
  self.assertEqual(capture_coverage(self.root)['sequence'],'incomplete')
  p.chmod(0o644);self.assertEqual(capture_coverage(self.root)['status'],'invalid')
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
  bootstrap(self.root,clock=lambda:1000);old=2000;now=15*86400000+10000
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
 def test_both_markets_collect_concurrently_and_validate_candle_after_quote(self):
  bootstrap(self.root,clock=lambda:1000);gate=threading.Barrier(2);calls=[];now=9000000
  class Provider:
   def execution_candles(self,market,warm,price,**kw):
    calls.append(('candles',market));gate.wait(timeout=2)
    return {'bars':[(0,100,101,99,100)],'skip_bar':(900000,100,101,99,100)}
   def spot_quote(self,**kw):calls.append(('quote','spot'));return {'event_ms':now,'received_ms':now}
   def futures_quote(self,**kw):calls.append(('quote','futures'));return {'event_ms':now,'received_ms':now}
   def filters(self,*args,**kw):raise AssertionError('fresh cached filters must be reused')
  cached={k:{'received_ms':now} for k in ('spot','futures')}
  histories={k:{'skip_bar':(1,)} for k in ('spot','futures')}
  markets,errors=collect_markets(self.root,signal_window=True,clock=lambda:now,provider=Provider(),filter_cache=cached,history_cache=histories)
  self.assertFalse(errors)
  for market in ('spot','futures'):self.assertLess(calls.index(('quote',market)),calls.index(('candles',market)))
  self.assertEqual(set(markets),{'spot','futures'})
 def test_warm_history_is_collected_before_boundary_and_reused(self):
  bootstrap(self.root,clock=lambda:1000);boundary=20000*900000;now=[boundary-30000];history={};filters={};calls=[]
  class Provider:
   def closed_15m(self,market,**kw):
    calls.append(('warm',market));return {'market':market,'symbol':'BTCUSDT','source':'binance_spot_klines' if market=='spot' else 'binance_usdm_klines','received_ms':now[0],'bars':[(boundary-2700000,100.,101.,99.,100.)],'skip_bar':(boundary-1800000,100.,101.,99.,100.)}
   def execution_candles(self,market,warm,price,**kw):
    from research.adaptive_public_feed import execution_candles
    raw=[[t,'100','101','99','100','10',t+899999] for t in (boundary-1800000,boundary-900000,boundary)]
    return execution_candles(market,warm,price,fetch=lambda *x:raw,clock=lambda:now[0])
   def filters(self,market,**kw):return {'received_ms':now[0]}
   def spot_quote(self,**kw):return {'symbol':'BTCUSDT','source':'binance_spot_trade','price':'100','event_ms':now[0],'received_ms':now[0]}
   def futures_quote(self,**kw):return {'symbol':'BTCUSDT','source':'binance_usdm_mark','price':'100','event_ms':now[0],'received_ms':now[0]}
  p=Provider();collect_markets(self.root,signal_window=False,clock=lambda:now[0],provider=p,filter_cache=filters,history_cache=history)
  self.assertEqual(len(calls),2);now[0]=boundary+300
  markets,errors=collect_markets(self.root,signal_window=True,clock=lambda:now[0],provider=p,filter_cache=filters,history_cache=history)
  self.assertFalse(errors)
  for market in ('spot','futures'):self.assertEqual(markets[market]['candles']['skip_bar'][0],boundary-900000)
 def test_preboundary_public_quote_is_retried_with_strict_bound(self):
  bootstrap(self.root,clock=lambda:1000);boundary=20000*900000;now=boundary+300;counts={'spot':0,'futures':0}
  class Provider:
   def q(self,market):
    counts[market]+=1;event=boundary-1 if counts[market]==1 else now
    return {'event_ms':event,'received_ms':now}
   def spot_quote(self,**kw):return self.q('spot')
   def futures_quote(self,**kw):return self.q('futures')
   def execution_candles(self,*args,**kw):return {'bars':[],'skip_bar':(boundary-900000,100,101,99,100)}
  cached={k:{'received_ms':now} for k in ('spot','futures')};warm={k:{'skip_bar':(1,)} for k in cached}
  markets,errors=collect_markets(self.root,signal_window=True,clock=lambda:now,provider=Provider(),filter_cache=cached,history_cache=warm,sleeper=lambda _:None)
  self.assertFalse(errors);self.assertEqual(counts,{'spot':2,'futures':2})
  self.assertEqual(markets['futures']['quote']['event_ms'],now)

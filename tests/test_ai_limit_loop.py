import unittest,tempfile,os,json
from pathlib import Path
from unittest.mock import patch
from research.ai_limit_runtime import ResponseReader,Publisher,atomic
class LoopTests(unittest.TestCase):
 def test_reader_caches_only_successful_unchanged_atomic_identity(self):
  with tempfile.TemporaryDirectory() as temp:
   p=Path(temp)/'response';atomic(p,{'id':1},mode=0o600);reader=ResponseReader()
   with patch('research.ai_limit_runtime.ingest',return_value='done') as ingest:
    reader.apply(None,p,os.geteuid());self.assertEqual(reader.apply(None,p,os.geteuid()),'unchanged');self.assertEqual(ingest.call_count,1)
    atomic(p,{'id':2},mode=0o600);reader.apply(None,p,os.geteuid());self.assertEqual(ingest.call_count,2)
   with patch('research.ai_limit_runtime.ingest',side_effect=ValueError('failure')) as ingest:
    atomic(p,{'id':3},mode=0o600)
    for _ in range(2):
     with self.assertRaises(ValueError):reader.apply(None,p,os.geteuid())
    self.assertEqual(ingest.call_count,2)
 def test_publication_is_bounded_and_forced_once_is_immediate(self):
  pub=Publisher()
  with patch('research.ai_limit_runtime.atomic') as write,patch('research.ai_limit_runtime.time.monotonic_ns',side_effect=[1000000000,1010000000,1250000000,1250000001]):
   self.assertTrue(pub.publish('unused',{}));self.assertFalse(pub.publish('unused',{}));self.assertTrue(pub.publish('unused',{}));self.assertTrue(pub.publish('unused',{},force=True));self.assertEqual(write.call_count,3)

 def test_group_rolls_back_metadata_and_verifies_once_before_commit(self):
  from research.ai_limit_book import Book
  with tempfile.TemporaryDirectory() as temp:
   b=Book(Path(temp)/'paper')
   try:
    with self.assertRaises(RuntimeError):
     with b.grouped_transaction():
      with b.transaction():b.set('rollback_probe',1)
      raise RuntimeError('crash')
    self.assertIsNone(b.get('rollback_probe'))
    with patch.object(b,'verify',wraps=b.verify) as verify:
     with b.grouped_transaction():
      with b.transaction():b.set('probe',1)
      with b.transaction():b.set('probe',2)
     self.assertEqual(verify.call_count,1)
    self.assertTrue(b.verify()['balanced'])
   finally:b.close()

 def test_quiet_socket_read_timeout_keeps_connection_and_cursor(self):
  import json
  from research.ai_limit_stream import StreamFeed
  f=StreamFeed(symbols=['BTCUSDT'],clock=lambda:100000)
  calls=[]
  class Socket:
   def recv(self):
    calls.append(1)
    if len(calls)<=3:raise TimeoutError()
    f.stop.set()
    return json.dumps({'e':'aggTrade','s':'BTCUSDT','a':1,'T':100000,'p':'100','q':'1','m':True})
   def close(self):pass
  f.connect=lambda url:Socket();f.run('BTCUSDT')
  state=f.states['BTCUSDT']
  self.assertEqual(state['read_timeouts'],3);self.assertEqual(state['reconnects'],1)
  self.assertTrue(state['connected']);self.assertEqual(state['last'],1)
  self.assertEqual(state['reason'],'bootstrap')
 def test_true_socket_error_remains_latched_and_classified(self):
  from research.ai_limit_stream import StreamFeed
  f=StreamFeed(symbols=['BTCUSDT']);f.states['BTCUSDT']['fault']=False
  class Socket:
   def recv(self):f.stop.set();raise OSError('not stored')
   def close(self):pass
  f.connect=lambda url:Socket();f.run('BTCUSDT')
  self.assertTrue(f.states['BTCUSDT']['fault']);self.assertFalse(f.states['BTCUSDT']['connected'])
  self.assertEqual(f.states['BTCUSDT']['last_fault']['category'],'OSError')


class ReleaseProfileTests(unittest.TestCase):
 def test_release_profile_keeps_stop_and_money_identical_without_sql_tracing(self):
  from tests import test_ai_quote_stream as fixtures
  from research.ai_limit_book import Book
  from research.ai_limit_runtime import configure_diagnostics
  outcomes=[]
  for detailed in (False,True):
   t=fixtures.QuoteStreamTests();t.setUp();probe=None
   try:
    t.b.close();t.b=Book(t.root,clock=lambda:t.now);t.poller.book=t.b
    probe=configure_diagnostics(t.b,t.f,detailed)
    self.assertEqual(probe is not None,detailed)
    self.assertIs(t.b.db.probe,probe);self.assertIs(t.f.probe,probe)
    if probe:probe.begin()
    t.opened();t.now+=1;t.book(101,'104');t.mark('104')
    result=t.poller.cycle();state=t.b.plans()[0][1]
    self.assertEqual(state['open_qty'],'0');self.assertEqual(state['close_reason'],'SL')
    self.assertTrue(t.b.verify()['balanced'])
    outcomes.append((result['balances'],state['gross'],state['fees'],state['status']))
    if probe:self.assertIn('book_verify',probe.phases)
    else:self.assertFalse((t.root/'phase-diagnostics.json').exists())
   finally:
    if probe:probe.close()
    t.tearDown()
  self.assertEqual(outcomes[0],outcomes[1])

 def test_release_profile_does_not_construct_profiler_or_erase_previous_evidence(self):
  from research.ai_limit_runtime import configure_diagnostics
  from research.ai_limit_book import Book
  from research.ai_limit_stream import StreamFeed
  with tempfile.TemporaryDirectory() as temp:
   b=Book(Path(temp)/'paper');f=StreamFeed(symbols=['BTCUSDT'])
   evidence=b.root/'phase-diagnostics.json';evidence.write_text('previous-evidence')
   try:
    with patch('research.ai_limit_runtime.Probe',side_effect=AssertionError('detailed profiling must be opt-in')):
     self.assertIsNone(configure_diagnostics(b,f))
    self.assertEqual(evidence.read_text(),'previous-evidence')
    self.assertIsNone(f.probe);self.assertTrue(b.verify()['balanced'])
   finally:f.close();b.close()

 def test_serve_once_entrypoint_publishes_without_profiler_or_network(self):
  import contextlib,io,sys,time
  from research.ai_limit_runtime import main
  from research.ai_limit_stream import StreamFeed
  from research.ai_limit_plan import VERSION
  def start(feed):
   now=feed.clock()
   feed.states['BTCUSDT'].update(connected=True)
   for channel in ('book','market'):feed.quote_connection('BTCUSDT',channel,True)
   feed.receive('BTCUSDT',{'e':'aggTrade','s':'BTCUSDT','a':1,'T':now,'p':'100','q':'1','m':True})
   feed.receive_quote('BTCUSDT',{'e':'depthUpdate','s':'BTCUSDT','U':1,'u':1,'pu':0,'E':now,'T':now,'b':[['100','1']],'a':[['100.1','1']]},'book')
   feed.receive_quote('BTCUSDT',{'e':'markPriceUpdate','s':'BTCUSDT','E':now,'p':'100','T':now+28800000},'market')
   return feed
  def filters(feed,symbol,**kwargs):
   return {'as_of_ms':feed.clock(),'tick_size':'.1','step_size':'.001','min_qty':'.001','min_notional':'50'}
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);public=root/'orders.json';state=root/'paper'
   args=['paper','serve','--state',str(state),'--public',str(public),'--response',str(root/'absent.json'),'--analyst-uid',str(os.geteuid()),'--enable-paper','--once']
   with patch.object(sys,'argv',args),patch.object(StreamFeed,'start',start),patch.object(StreamFeed,'filters',filters),patch.object(StreamFeed,'get',side_effect=AssertionError('network forbidden')),patch('research.ai_limit_runtime.Probe',side_effect=AssertionError('profiler forbidden')),patch('research.ai_limit_runtime.signal.signal'),contextlib.redirect_stdout(io.StringIO()):
    main()
   result=json.loads(public.read_text())
   self.assertEqual(result['version'],VERSION);self.assertTrue(result['ledger_balanced'])
   self.assertEqual(result['balances']['free'],'1000');self.assertFalse(result['paused'])
   self.assertEqual(result['plans'],[]);self.assertFalse((state/'phase-diagnostics.json').exists())
